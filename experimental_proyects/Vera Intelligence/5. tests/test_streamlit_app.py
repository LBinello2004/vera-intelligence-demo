from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# streamlit_app.py llama st.set_page_config()/st.markdown() a nivel de módulo (arma la página
# apenas se importa), y streamlit no está instalado en todos los entornos que corren esta suite
# (sólo en el .venv del proyecto, ver "Cómo correrlo" en streamlit_app.py) -así que se stubea
# ANTES de importar, mismo criterio que ya se usó para verificar a mano el bug real de
# _with_fixed_suggestions (ver 9. HISTORIAL.md, 2026-09-11). pandas se stubea también, aunque sí
# está instalado acá, para no depender de qué entorno corre la suite -este módulo sólo prueba
# lógica pura (sugerencias, agrupación de tool calls, markdown de exportación), nunca el
# renderizado real de widgets/gráficos, que necesitaría un navegador para verificarse de verdad
# (ver la ronda de esta sesión que sí lo hizo en vivo con el Browser tool)."""
_fake_streamlit = MagicMock()
_fake_streamlit.session_state = {}
sys.modules["streamlit"] = _fake_streamlit
sys.modules["pandas"] = MagicMock()

import streamlit_app  # noqa: E402
import vi_agent  # noqa: E402
from runtime_control import AnalysisCancelled, AnalysisControl, OperationalUnavailable  # noqa: E402


class AnalysisLifecycleTests(unittest.TestCase):
    def setUp(self):
        _fake_streamlit.session_state = {}
        self.history = [SimpleNamespace(role="user"), SimpleNamespace(role="model")]
        self.chat = MagicMock()
        self.chat.get_history.return_value = self.history
        _fake_streamlit.session_state.update({
            "client_id": "mens_fashion_alto", "model_override": vi_agent.AVAILABLE_MODELS[0], "chat": self.chat,
            "messages": [{"role": "user", "content": "Compará las ventas"}],
        })
        self.original_checker = streamlit_app._session_abandoned_checker
        self.checker = patch.object(streamlit_app, "_session_abandoned_checker", return_value=None)
        self.checker.start()
        self.prewarm = patch.object(vi_agent, "prewarm_sql_connection")
        self.warm = self.prewarm.start()
        self.addCleanup(self.prewarm.stop)
        self.addCleanup(self.checker.stop)
        self.addCleanup(_fake_streamlit.session_state.clear)

    def test_client_init_starts_warming_before_chat_and_skips_it_on_rerun(self):
        order = []
        self.warm.side_effect = lambda: order.append("warm")
        _fake_streamlit.session_state.clear()
        with patch.object(vi_agent, "configure_client"), patch.object(vi_agent, "load_environment"), patch.object(
            vi_agent, "build_chat", side_effect=lambda: order.append("chat") or self.chat
        ):
            streamlit_app._init_client("mens_fashion_alto", "Men's Fashion", vi_agent.AVAILABLE_MODELS[0])
            streamlit_app._init_client("mens_fashion_alto", "Men's Fashion", vi_agent.AVAILABLE_MODELS[0])
        self.assertEqual(order, ["warm", "chat"])

    def test_cancel_discards_incomplete_history_and_restores_context(self):
        with self.assertRaises(AnalysisCancelled):
            with streamlit_app._analysis_request("Compará las ventas") as control:
                streamlit_app._cancel_active_analysis()
                control.check()
        restored = MagicMock()
        with patch.object(vi_agent, "build_chat", return_value=restored) as build:
            streamlit_app._restore_interrupted_chat()
        build.assert_called_once_with(history=self.history)
        self.assertIs(_fake_streamlit.session_state["chat"], restored)
        self.assertEqual(_fake_streamlit.session_state["messages"][-1]["content"], "Análisis cancelado.")
        self.assertNotIn("analysis_control", _fake_streamlit.session_state)

    def test_normal_completion_keeps_chat_and_does_not_schedule_restore(self):
        with streamlit_app._analysis_request("Compará las ventas"):
            pass
        self.assertIs(_fake_streamlit.session_state["chat"], self.chat)
        self.assertNotIn("restore_history", _fake_streamlit.session_state)

    def test_streamlit_control_exception_also_schedules_restore(self):
        class ScriptStopped(BaseException):
            pass

        with self.assertRaises(ScriptStopped):
            with streamlit_app._analysis_request("Compará las ventas"):
                raise ScriptStopped()
        self.assertEqual(_fake_streamlit.session_state["restore_history"], self.history)

    def test_new_conversation_cancels_before_clearing_state(self):
        control = AnalysisControl()
        _fake_streamlit.session_state["analysis_control"] = control
        streamlit_app._reset_session_state()
        with self.assertRaises(AnalysisCancelled):
            control.check()
        self.assertNotIn("chat", _fake_streamlit.session_state)
        self.assertNotIn("restore_history", _fake_streamlit.session_state)

    def test_old_request_does_not_restore_over_a_new_session(self):
        with self.assertRaises(AnalysisCancelled):
            with streamlit_app._analysis_request("Compará las ventas") as control:
                streamlit_app._reset_session_state()
                _fake_streamlit.session_state.update({"client_id": "otro", "chat": "nuevo"})
                control.check()
        self.assertEqual(_fake_streamlit.session_state["chat"], "nuevo")
        self.assertNotIn("restore_history", _fake_streamlit.session_state)

    def test_failure_restores_context_without_a_cancelled_message(self):
        with self.assertRaises(OperationalUnavailable):
            with streamlit_app._analysis_request("Compará las ventas"):
                raise OperationalUnavailable()
        with patch.object(vi_agent, "build_chat", return_value=MagicMock()):
            streamlit_app._restore_interrupted_chat(mark_cancelled=False)
        self.assertEqual(len(_fake_streamlit.session_state["messages"]), 1)

    def test_runtime_disconnect_checker_captures_the_actual_session(self):
        runtime = MagicMock()
        runtime.is_active_session.return_value = False
        # El import real de Streamlit se reemplaza sólo para esta fábrica; el resto del archivo
        # sigue usando el stub de widgets definido arriba.
        runtime_module = SimpleNamespace(exists=lambda: True, get_instance=lambda: runtime)
        runner_module = SimpleNamespace(get_script_run_ctx=lambda **_: SimpleNamespace(session_id="browser-session"))
        with patch.dict(sys.modules, {"streamlit.runtime": runtime_module, "streamlit.runtime.scriptrunner": runner_module}):
            checker = self.original_checker()
        self.assertTrue(checker())
        runtime.is_active_session.assert_called_once_with("browser-session")

    def test_failed_restore_keeps_snapshot_and_recovers_local_reply_later(self):
        with self.assertRaises(OperationalUnavailable):
            with streamlit_app._analysis_request("Compará las ventas"):
                raise OperationalUnavailable()
        restored = MagicMock()
        answer = "No pude acceder a la información necesaria."
        with patch.object(vi_agent, "build_chat", side_effect=[OperationalUnavailable(), restored]) as build:
            streamlit_app._record_local_failure("Compará las ventas", answer)
            self.assertNotIn("chat", _fake_streamlit.session_state)
            self.assertEqual(_fake_streamlit.session_state["restore_history"], self.history)
            _fake_streamlit.session_state["messages"].append({"role": "assistant", "content": answer})
            streamlit_app._init_client("mens_fashion_alto", "Men's Fashion", vi_agent.AVAILABLE_MODELS[0])
        self.assertEqual(build.call_count, 2)
        self.assertIs(_fake_streamlit.session_state["chat"], restored)
        restored.record_history.assert_called_once()
        self.assertEqual(restored.record_history.call_args.kwargs["model_output"][0].parts[0].text, answer)
        self.assertNotIn("restore_history", _fake_streamlit.session_state)

    def test_client_switch_waits_for_cancelled_request_before_reconfiguring(self):
        signalled = threading.Event()
        errors = []
        original_cancel = streamlit_app._cancel_active_analysis

        def signal_cancel():
            original_cancel()
            signalled.set()

        def switch():
            try:
                streamlit_app._init_client("otro_cliente", "Otro cliente", vi_agent.AVAILABLE_MODELS[0])
            except BaseException as exc:
                errors.append(exc)

        with patch.object(streamlit_app, "_cancel_active_analysis", side_effect=signal_cancel), patch.object(
            vi_agent, "configure_client"
        ) as configure, patch.object(vi_agent, "load_environment"), patch.object(vi_agent, "build_chat", return_value=MagicMock()):
            with self.assertRaises(AnalysisCancelled):
                with streamlit_app._analysis_request("Compará las ventas") as control:
                    worker = threading.Thread(target=switch)
                    worker.start()
                    self.assertTrue(signalled.wait(2))
                    configure.assert_not_called()
                    control.check()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            configure.assert_called_once_with("otro_cliente", model_override=vi_agent.AVAILABLE_MODELS[0])
        self.assertNotIn("restore_history", _fake_streamlit.session_state)

    def test_late_failure_cannot_modify_the_chat_of_another_rerun(self):
        _fake_streamlit.session_state["ui_run_id"] = "nuevo"
        with patch.object(vi_agent, "build_chat") as build:
            with self.assertRaises(AnalysisCancelled):
                streamlit_app._record_local_failure("pregunta anterior", "falla anterior", ui_run_id="anterior")
        build.assert_not_called()
        self.chat.record_history.assert_not_called()
        self.assertIs(_fake_streamlit.session_state["chat"], self.chat)

    def test_superseded_rerun_cannot_start_another_analysis(self):
        _fake_streamlit.session_state["ui_run_id"] = "nuevo"
        with self.assertRaises(AnalysisCancelled):
            with streamlit_app._analysis_request("pregunta anterior", ui_run_id="anterior"):
                self.fail("El script anterior no debe iniciar trabajo.")
        self.chat.get_history.assert_not_called()


class ToolProgressLabelTests(unittest.TestCase):
    def test_known_tools_get_a_business_label(self) -> None:
        self.assertEqual(streamlit_app._tool_progress_label("run_readonly_sql"), "Consultando datos...")
        self.assertEqual(
            streamlit_app._tool_progress_label("get_business_rules"), "Revisando criterios de negocio..."
        )
        self.assertEqual(
            streamlit_app._tool_progress_label("search_conversations"),
            "Buscando ejemplos en conversaciones... (puede tardar hasta 3 minutos)",
        )

    def test_unknown_tool_falls_back_to_a_generic_label(self) -> None:
        # Nunca debe filtrar el nombre técnico de una tool nueva que no se haya mapeado todavía.
        self.assertEqual(streamlit_app._tool_progress_label("una_tool_nueva"), "Analizando...")


class FixedSuggestionsTests(unittest.TestCase):
    """Cubre _with_fixed_suggestions -la función que reemplazó a dos funciones independientes
    después de encontrar un bug real (2026-09-11): con 0-1 sugerencias de entrada, la versión
    vieja dejaba que la chip de descubrimiento pisara a la de coaching. Estos tests son
    justamente los casos que hubieran atrapado ese bug antes de que hiciera falta encontrarlo
    a mano."""

    def test_coaching_always_present_without_vector_search(self) -> None:
        with patch.object(vi_agent, "CLIENT_CONFIG", SimpleNamespace(vector_search=None)):
            result = streamlit_app._with_fixed_suggestions([])
        self.assertEqual(result, [streamlit_app.COACHING_SUGGESTION])

    def test_coaching_and_discovery_both_present_with_vector_search_and_empty_input(self) -> None:
        # Caso real del bug: el camino de fallback del saludo siempre llama con [].
        with patch.object(vi_agent, "CLIENT_CONFIG", SimpleNamespace(vector_search=object())):
            result = streamlit_app._with_fixed_suggestions([])
        self.assertEqual(
            result, [streamlit_app.COACHING_SUGGESTION, streamlit_app.DISCOVERY_SUGGESTION]
        )

    def test_coaching_first_dynamic_in_middle_discovery_last(self) -> None:
        with patch.object(vi_agent, "CLIENT_CONFIG", SimpleNamespace(vector_search=object())):
            result = streamlit_app._with_fixed_suggestions(["¿Pregunta A?", "¿Pregunta B?"])
        self.assertEqual(
            result,
            [
                streamlit_app.COACHING_SUGGESTION,
                "¿Pregunta A?",
                "¿Pregunta B?",
                streamlit_app.DISCOVERY_SUGGESTION,
            ],
        )

    def test_model_suggestions_capped_at_three(self) -> None:
        with patch.object(vi_agent, "CLIENT_CONFIG", SimpleNamespace(vector_search=None)):
            result = streamlit_app._with_fixed_suggestions(["A", "B", "C", "D", "E"])
        self.assertEqual(result, [streamlit_app.COACHING_SUGGESTION, "A", "B", "C"])

    def test_model_echoing_a_fixed_suggestion_does_not_duplicate_it(self) -> None:
        with patch.object(vi_agent, "CLIENT_CONFIG", SimpleNamespace(vector_search=object())):
            result = streamlit_app._with_fixed_suggestions(
                [streamlit_app.COACHING_SUGGESTION, "¿Pregunta real?", streamlit_app.DISCOVERY_SUGGESTION]
            )
        self.assertEqual(
            result,
            [streamlit_app.COACHING_SUGGESTION, "¿Pregunta real?", streamlit_app.DISCOVERY_SUGGESTION],
        )


class ContextualSuggestionsTests(unittest.TestCase):
    """_contextual_suggestions -usada para TODO turno después del saludo (2026-09-14, ver
    _with_fixed_suggestions). A diferencia de esa función, nunca fuerza coaching/descubrimiento:
    es lo que pide justamente que las chips posteriores al saludo sean 100% adaptativas al
    contexto, sin ninguna fija repitiéndose turno a turno."""

    def test_returns_model_suggestions_unchanged_under_the_cap(self) -> None:
        result = streamlit_app._contextual_suggestions(["¿Pregunta A?", "¿Pregunta B?"])
        self.assertEqual(result, ["¿Pregunta A?", "¿Pregunta B?"])

    def test_caps_at_max_contextual_suggestions(self) -> None:
        items = [f"Pregunta {i}" for i in range(10)]
        result = streamlit_app._contextual_suggestions(items)
        self.assertEqual(result, items[: streamlit_app._MAX_CONTEXTUAL_SUGGESTIONS])

    def test_never_injects_coaching_or_discovery(self) -> None:
        # A diferencia de _with_fixed_suggestions, esta función no debe agregar nada por su cuenta
        # -si el modelo no sugirió coaching/descubrimiento para esta respuesta puntual, no aparecen.
        result = streamlit_app._contextual_suggestions(["¿Pregunta A?"])
        self.assertNotIn(streamlit_app.COACHING_SUGGESTION, result)
        self.assertNotIn(streamlit_app.DISCOVERY_SUGGESTION, result)

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(streamlit_app._contextual_suggestions([]), [])


class RenderToolSummaryTests(unittest.TestCase):
    """_render_tool_summary agrupa por tool y siempre usa st.caption -nunca el expander de
    _render_debug_trace, que sólo corre con --internal-debug."""

    def setUp(self) -> None:
        _fake_streamlit.reset_mock()

    def test_empty_tool_calls_renders_nothing(self) -> None:
        streamlit_app._render_tool_summary([])
        _fake_streamlit.caption.assert_not_called()

    def test_groups_repeated_calls_to_the_same_tool(self) -> None:
        streamlit_app._render_tool_summary(
            [
                {"name": "run_readonly_sql", "elapsed_ms": 400.0},
                {"name": "run_readonly_sql", "elapsed_ms": 200.0},
                {"name": "get_business_rules", "elapsed_ms": 200.0},
            ]
        )
        _fake_streamlit.caption.assert_called_once()
        (caption_text,), _ = _fake_streamlit.caption.call_args
        self.assertIn("0.8s", caption_text)  # total: (400+200+200)ms
        self.assertIn("run_readonly_sql` 2×", caption_text)
        self.assertIn("get_business_rules` 1×", caption_text)

    def test_never_shows_call_args_or_result(self) -> None:
        # A diferencia de _render_debug_trace, este panel es siempre visible -nunca debe filtrar
        # argumentos (puede incluir SQL) ni el resultado de la tool.
        streamlit_app._render_tool_summary(
            [
                {
                    "name": "run_readonly_sql",
                    "args": {"sql": "SELECT seller_id FROM dashboard_v2.secreto"},
                    "result": "algo sensible",
                    "elapsed_ms": 100.0,
                }
            ]
        )
        (caption_text,), _ = _fake_streamlit.caption.call_args
        self.assertNotIn("SELECT", caption_text)
        self.assertNotIn("algo sensible", caption_text)

    def test_missing_elapsed_ms_treated_as_zero_instead_of_raising(self) -> None:
        streamlit_app._render_tool_summary([{"name": "run_readonly_sql"}])
        (caption_text,), _ = _fake_streamlit.caption.call_args
        self.assertIn("0.0s", caption_text)


class AsksForExamplesTests(unittest.TestCase):
    """Las conversaciones se citan (y se ofrece escucharlas) sólo si la pregunta pide ejemplos,
    citas, audios o casos reales -pedido explícito, 2026-09-21."""

    def test_detects_requests_for_examples_quotes_and_audio(self) -> None:
        for question in (
            "Dame un ejemplo de cómo maneja objeciones",
            "¿Tenés algún caso real de venta cruzada?",
            "Quiero escuchar el audio de esa conversación",
            "Citame lo que dijo el vendedor",
            "¿Qué dijo el cliente cuando no compró?",
            "¿Quién lo dijo?",
            "¿En qué conversación pasó?",
            "Mostrame conversaciones donde piden confección a medida",
            "Pasame las grabaciones",
        ):
            with self.subTest(question=question):
                self.assertTrue(streamlit_app._asks_for_examples(question))

    def test_ignores_ordinary_analysis_and_coaching_questions(self) -> None:
        for question in (
            "¿Qué le recomendarías al equipo para mejorar esta semana?",
            "Dame coaching para Ubaldo Ramos",
            "Dame información y consejos sobre los peores tres vendedores",
            "¿Cómo viene la tasa de cierre este mes?",
            "¿Qué impacto tiene la confección a medida al rescatar ventas?",
            "",
            None,
        ):
            with self.subTest(question=question):
                self.assertFalse(streamlit_app._asks_for_examples(question))


class ExtractCitableConversationsTests(unittest.TestCase):
    """`_extract_citable_conversations` (2026-09-14, ver "Escuchar audio de conversaciones") es la
    parte pura de `_render_audio_players` -sin llamadas a `st`, a propósito, para poder testear la
    lógica de extracción/deduplicación sin mockear columnas/botones (ver el comentario al importar
    streamlit arriba: el renderizado en sí se verifica en vivo, no acá)."""

    def _search_call(self, resultados: list[dict], *, error: str | None = None) -> dict:
        return {
            "name": "search_conversations",
            "error": error,
            "result": json.dumps({"resultados": resultados, "aviso": "..."}),
        }

    def test_empty_tool_calls_returns_empty_list(self) -> None:
        self.assertEqual(streamlit_app._extract_citable_conversations([]), [])

    def test_ignores_calls_to_other_tools(self) -> None:
        tool_calls = [{"name": "run_readonly_sql", "error": None, "result": "{}"}]
        self.assertEqual(streamlit_app._extract_citable_conversations(tool_calls), [])

    def test_ignores_search_calls_with_an_error(self) -> None:
        tool_calls = [self._search_call([{"conversation_id": "c1"}], error="algo falló")]
        self.assertEqual(streamlit_app._extract_citable_conversations(tool_calls), [])

    def test_ignores_results_without_conversation_id(self) -> None:
        tool_calls = [self._search_call([{"conversation_id": None, "tienda": "A"}])]
        self.assertEqual(streamlit_app._extract_citable_conversations(tool_calls), [])

    def test_extracts_conversations_from_a_single_call(self) -> None:
        resultados = [{"conversation_id": "c1", "tienda": "A"}, {"conversation_id": "c2", "tienda": "B"}]
        tool_calls = [self._search_call(resultados)]
        self.assertEqual(streamlit_app._extract_citable_conversations(tool_calls), resultados)

    def test_deduplicates_by_conversation_id_across_calls(self) -> None:
        # El modelo pudo haber reformulado y llamado search_conversations dos veces en la misma
        # respuesta -no debe ofrecer el mismo botón de audio dos veces para la misma conversación.
        tool_calls = [
            self._search_call([{"conversation_id": "c1", "tienda": "A"}]),
            self._search_call([{"conversation_id": "c1", "tienda": "A"}, {"conversation_id": "c2", "tienda": "B"}]),
        ]
        result = streamlit_app._extract_citable_conversations(tool_calls)
        self.assertEqual([r["conversation_id"] for r in result], ["c1", "c2"])

    def test_malformed_json_result_is_ignored_without_raising(self) -> None:
        tool_calls = [{"name": "search_conversations", "error": None, "result": "esto no es JSON"}]
        self.assertEqual(streamlit_app._extract_citable_conversations(tool_calls), [])

    def test_missing_result_field_is_ignored_without_raising(self) -> None:
        tool_calls = [{"name": "search_conversations", "error": None}]
        self.assertEqual(streamlit_app._extract_citable_conversations(tool_calls), [])

    def test_includes_companeros_group_from_comparar_con_mejores(self) -> None:
        # Bug real (2026-09-22): en modo comparar_con_mejores sólo se leía "resultados" -las
        # conversaciones de compañeros con mejor resultado nunca ofrecían botón de audio.
        tool_calls = [{
            "name": "search_conversations",
            "error": None,
            "result": json.dumps({
                "resultados": [{"conversation_id": "c1", "vendedor": "Ubaldo Ramos"}],
                "companeros": [{"conversation_id": "c2", "vendedor": "Un Compañero"}],
                "aviso": "...",
            }),
        }]
        result = streamlit_app._extract_citable_conversations(tool_calls)
        self.assertEqual([r["conversation_id"] for r in result], ["c1", "c2"])

    def test_deduplicates_across_resultados_and_companeros(self) -> None:
        tool_calls = [{
            "name": "search_conversations",
            "error": None,
            "result": json.dumps({
                "resultados": [{"conversation_id": "c1"}],
                "companeros": [{"conversation_id": "c1"}, {"conversation_id": "c2"}],
            }),
        }]
        result = streamlit_app._extract_citable_conversations(tool_calls)
        self.assertEqual([r["conversation_id"] for r in result], ["c1", "c2"])


class FormatConversationDateTests(unittest.TestCase):
    """_format_conversation_date (2026-09-22): la fecha ISO cruda de search_conversations pasa a
    formato legible en el panel de audio -bug real reportado por Lucas, se mostraba tal cual con
    microsegundos y offset UTC."""

    def test_formats_iso_datetime_with_microseconds_and_offset(self) -> None:
        self.assertEqual(
            streamlit_app._format_conversation_date("2026-07-25T20:53:37.809000+00:00"),
            "25/07/2026 20:53",
        )

    def test_formats_iso_datetime_without_microseconds(self) -> None:
        self.assertEqual(
            streamlit_app._format_conversation_date("2026-07-25T20:53:37+00:00"),
            "25/07/2026 20:53",
        )

    def test_unparseable_value_is_returned_unchanged(self) -> None:
        self.assertEqual(streamlit_app._format_conversation_date("no es una fecha"), "no es una fecha")

    def test_empty_or_missing_value_returns_empty_string(self) -> None:
        self.assertEqual(streamlit_app._format_conversation_date(""), "")
        self.assertEqual(streamlit_app._format_conversation_date(None), "")


class ConversationAsMarkdownTests(unittest.TestCase):
    def test_renders_speakers_and_content_in_order(self) -> None:
        _fake_streamlit.session_state = {
            "messages": [
                {"role": "user", "content": "¿Cuál es el total?"},
                {"role": "assistant", "content": "El total es 42."},
            ]
        }
        markdown = streamlit_app._conversation_as_markdown("Cliente Demo")
        self.assertIn("# Conversación con Vera Intelligence — Cliente Demo", markdown)
        lines = markdown.splitlines()
        self.assertLess(lines.index("**Usuario:**"), lines.index("¿Cuál es el total?"))
        self.assertLess(lines.index("**Vera Intelligence:**"), lines.index("El total es 42."))

    def test_no_messages_still_renders_the_header(self) -> None:
        _fake_streamlit.session_state = {"messages": []}
        markdown = streamlit_app._conversation_as_markdown("Cliente Demo")
        self.assertEqual(markdown, "# Conversación con Vera Intelligence — Cliente Demo\n")


class RecordFeedbackTests(unittest.TestCase):
    """_record_feedback -pedido explícito (2026-09-11): señal real de qué respuestas sirven, en
    vez de sólo casos anecdóticos notados a mano. Nunca llama al recorder real -sólo verifica que
    arma el evento correcto y actualiza el estado del mensaje en session_state."""

    def setUp(self) -> None:
        self._recorder_patch = patch.object(streamlit_app, "_FEEDBACK_RECORDER", MagicMock())
        self.mock_recorder = self._recorder_patch.start()
        self.addCleanup(self._recorder_patch.stop)
        self._client_config_patch = patch.object(
            vi_agent, "CLIENT_CONFIG", SimpleNamespace(client_id="mens_fashion_alto")
        )
        self._client_config_patch.start()
        self.addCleanup(self._client_config_patch.stop)
        _fake_streamlit.session_state = {"session_id": "session-1"}

    def test_marks_the_message_with_the_vote(self) -> None:
        message = {
            "message_id": "msg-1",
            "content": "La tasa es 42%.",
            "question": "¿Cuál es la tasa?",
            "tool_calls": [],
            "feedback": None,
        }
        streamlit_app._record_feedback(message, "up")
        self.assertEqual(message["feedback"], "up")

    def test_forwards_question_answer_and_tool_names_to_the_recorder(self) -> None:
        message = {
            "message_id": "msg-1",
            "content": "La tasa es 42%.",
            "question": "¿Cuál es la tasa?",
            "tool_calls": [
                {"name": "run_readonly_sql", "elapsed_ms": 100.0},
                {"name": "run_readonly_sql", "elapsed_ms": 50.0},
                {"name": "get_business_rules", "elapsed_ms": 20.0},
            ],
            "feedback": None,
        }
        streamlit_app._record_feedback(message, "down")
        self.mock_recorder.record.assert_called_once_with(
            client_id="mens_fashion_alto",
            session_id="session-1",
            message_id="msg-1",
            vote="down",
            question="¿Cuál es la tasa?",
            answer="La tasa es 42%.",
            # deduplicado y ordenado -nunca una línea por llamada repetida de la misma tool.
            tool_names=["get_business_rules", "run_readonly_sql"],
        )

    def test_greeting_message_forwards_none_as_question(self) -> None:
        message = {
            "message_id": "msg-greeting",
            "content": "¡Hola!",
            "question": None,
            "tool_calls": None,
            "feedback": None,
        }
        streamlit_app._record_feedback(message, "up")
        self.assertIsNone(self.mock_recorder.record.call_args.kwargs["question"])
        self.assertEqual(self.mock_recorder.record.call_args.kwargs["tool_names"], [])


class GreetingCacheTests(unittest.TestCase):
    """Cache en disco del saludo inicial (2026-09-14) -evita pagar una llamada real a Gemini en
    cada apertura de sesión/cambio de cliente cuando el Data Map no cambió. Usa un directorio
    temporal en vez de GREETING_CACHE_DIR real para no tocar `.runtime/` del proyecto."""

    def setUp(self) -> None:
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self._dir_patch = patch.object(
            streamlit_app, "GREETING_CACHE_DIR", Path(self._tempdir.name)
        )
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)

    def test_no_cache_file_returns_none(self) -> None:
        self.assertIsNone(streamlit_app._load_cached_greeting("mens_fashion_alto", "fp1"))

    def test_saves_and_reloads_a_greeting(self) -> None:
        streamlit_app._save_cached_greeting(
            "mens_fashion_alto", "fp1", "¡Hola! Soy Vera.", [], ["¿Cómo venimos este mes?"]
        )
        cached = streamlit_app._load_cached_greeting("mens_fashion_alto", "fp1")
        self.assertIsNotNone(cached)
        greeting, charts, suggestions = cached
        self.assertEqual(greeting, "¡Hola! Soy Vera.")
        self.assertEqual(charts, [])
        self.assertEqual(suggestions, ["¿Cómo venimos este mes?"])

    def test_different_fingerprint_is_a_cache_miss(self) -> None:
        streamlit_app._save_cached_greeting("mens_fashion_alto", "fp1", "¡Hola!", [], [])
        self.assertIsNone(streamlit_app._load_cached_greeting("mens_fashion_alto", "fp2"))

    def test_different_client_does_not_share_cache(self) -> None:
        streamlit_app._save_cached_greeting("mens_fashion_alto", "fp1", "¡Hola Mens!", [], [])
        self.assertIsNone(streamlit_app._load_cached_greeting("farma24_alto", "fp1"))

    def test_corrupted_cache_file_is_treated_as_a_miss(self) -> None:
        streamlit_app.GREETING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (streamlit_app.GREETING_CACHE_DIR / "mens_fashion_alto.json").write_text(
            "esto no es json valido", encoding="utf-8"
        )
        self.assertIsNone(streamlit_app._load_cached_greeting("mens_fashion_alto", "fp1"))

    def test_save_failure_never_raises(self) -> None:
        # Directorio no creable (un archivo en el medio del path) -mismo criterio "mejor esfuerzo"
        # que el resto del logging local del proyecto: nunca debe romper el saludo real.
        blocking_file = Path(self._tempdir.name) / "blocking"
        blocking_file.write_text("bloqueando", encoding="utf-8")
        with patch.object(streamlit_app, "GREETING_CACHE_DIR", blocking_file / "greeting_cache"):
            streamlit_app._save_cached_greeting("mens_fashion_alto", "fp1", "¡Hola!", [], [])  # no debe tirar


class GreetingCacheFingerprintTests(unittest.TestCase):
    """`_greeting_cache_fingerprint` combina el mismo fingerprint que usa el cache de contexto de
    Gemini (Data Map + tools) con GREETING_PROMPT -así que cambiar cualquiera de los dos invalida
    el saludo cacheado."""

    def setUp(self) -> None:
        self._build_si_patch = patch.object(
            vi_agent, "build_system_instruction", return_value="system instruction v1"
        )
        self._build_si_patch.start()
        self.addCleanup(self._build_si_patch.stop)
        self._build_tools_patch = patch.object(vi_agent, "_build_tools_list", return_value=[])
        self._build_tools_patch.start()
        self.addCleanup(self._build_tools_patch.stop)

    def test_same_inputs_produce_the_same_fingerprint(self) -> None:
        self.assertEqual(
            streamlit_app._greeting_cache_fingerprint(), streamlit_app._greeting_cache_fingerprint()
        )

    def test_different_system_instruction_changes_the_fingerprint(self) -> None:
        first = streamlit_app._greeting_cache_fingerprint()
        with patch.object(
            vi_agent, "build_system_instruction", return_value="system instruction v2 (Data Map nuevo)"
        ):
            second = streamlit_app._greeting_cache_fingerprint()
        self.assertNotEqual(first, second)

    def test_different_greeting_prompt_changes_the_fingerprint(self) -> None:
        first = streamlit_app._greeting_cache_fingerprint()
        with patch.object(streamlit_app, "GREETING_PROMPT", "otro texto de saludo distinto"):
            second = streamlit_app._greeting_cache_fingerprint()
        self.assertNotEqual(first, second)


class SharedPasswordGateTests(unittest.TestCase):
    """`_check_shared_password` (2026-09-17, pedido explícito: la app quedó con link público sin
    ningún registro de quién entra) -no es login individual, sólo una traba mínima contra
    reenvíos accidentales del link. La contraseña real nunca vive en el código, sólo en el
    secret VI_DEMO_PASSWORD."""

    def setUp(self):
        _fake_streamlit.session_state = {}
        _fake_streamlit.text_input.reset_mock(return_value=True, side_effect=True)
        _fake_streamlit.button.reset_mock(return_value=True, side_effect=True)
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(_fake_streamlit.session_state.clear)

    def test_fails_closed_without_a_configured_secret(self) -> None:
        os.environ.pop("VI_DEMO_PASSWORD", None)
        self.assertFalse(streamlit_app._check_shared_password())

    def test_wrong_password_does_not_grant_access(self) -> None:
        os.environ["VI_DEMO_PASSWORD"] = "446655"
        _fake_streamlit.text_input.return_value = "otra-cosa"
        _fake_streamlit.button.return_value = True
        self.assertFalse(streamlit_app._check_shared_password())
        self.assertNotIn("shared_password_ok", _fake_streamlit.session_state)

    def test_correct_password_grants_access_and_marks_session(self) -> None:
        os.environ["VI_DEMO_PASSWORD"] = "446655"
        _fake_streamlit.text_input.return_value = "446655"
        _fake_streamlit.button.return_value = True
        streamlit_app._check_shared_password()
        self.assertTrue(_fake_streamlit.session_state.get("shared_password_ok"))

    def test_already_unlocked_session_skips_the_prompt(self) -> None:
        os.environ["VI_DEMO_PASSWORD"] = "446655"
        _fake_streamlit.session_state["shared_password_ok"] = True
        _fake_streamlit.text_input.return_value = ""
        _fake_streamlit.button.return_value = False
        self.assertTrue(streamlit_app._check_shared_password())


if __name__ == "__main__":
    unittest.main()


class SearchProgressLabelTests(unittest.TestCase):
    def test_label_names_the_seller_and_the_comparison(self) -> None:
        label = streamlit_app._tool_progress_label(
            "search_conversations", {"employee_name": "Ubaldo Ramos", "comparar_con_mejores": True}
        )
        self.assertIn("Ubaldo Ramos", label)
        self.assertIn("compañeros con mejor resultado", label)

    def test_label_without_seller_talks_about_the_team(self) -> None:
        label = streamlit_app._tool_progress_label("search_conversations", {"query": "x"})
        self.assertIn("equipo", label)

    def test_other_tools_ignore_args(self) -> None:
        self.assertEqual(
            streamlit_app._tool_progress_label("run_readonly_sql", {"query": "select 1"}), "Consultando datos..."
        )
