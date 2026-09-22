from __future__ import annotations

import inspect
import os
import json
import sys
import tempfile
import unittest
import yaml
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from google import genai  # noqa: E402

import vi_agent  # noqa: E402
from client_config import available_client_ids, load_client_config  # noqa: E402
from response_policy import client_answer_violations  # noqa: E402
from usage_tracking import InteractionOutcomeRecorder, UsageRecorder, load_usage_events  # noqa: E402


class FakeResponse:
    def __init__(self, text: str, usage_metadata=None, function_calls=None) -> None:
        self.text = text
        self.function_calls = function_calls or []
        self.usage_metadata = usage_metadata


class FakeFunctionCall:
    def __init__(self, name: str, args: dict) -> None:
        self.name = name
        self.args = args


class FakeChat:
    def __init__(self, responses: list[str | FakeResponse]) -> None:
        self.responses = iter(responses)
        self.messages: list[object] = []

    def send_message(self, message):
        self.messages.append(message)
        response = next(self.responses)
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


class ViAgentTests(unittest.TestCase):
    def test_mvp_exposes_no_unrestricted_langfuse_tools(self) -> None:
        # TOOL_FUNCTIONS es la tabla de despacho completa del loop manual -run_readonly_sql y
        # get_business_rules siempre, search_conversations agregada 2026-09-10 (ver
        # "6. busqueda_vectorial/README.md"). Ninguna de las tres se anuncia al modelo salvo que
        # _build_tools_list() la incluya -eso sí depende de CLIENT_CONFIG por cliente, cubierto
        # abajo por VectorSearchToolExposureTests. RAG (file_search) nunca pasa por esta tabla:
        # es un tool nativo server-side de Gemini, no una función Python del loop manual.
        self.assertEqual(
            set(vi_agent.TOOL_FUNCTIONS),
            {"get_business_rules", "run_readonly_sql", "search_conversations"},
        )
        # El template en sí no hardcodea nombres de tool -se arman dinámicamente en
        # _build_extra_tools_section() según lo que declare config.yaml del cliente activo.
        self.assertNotIn("search_conversations", vi_agent.SYSTEM_INSTRUCTION_TEMPLATE)
        self.assertNotIn("file_search", vi_agent.SYSTEM_INSTRUCTION_TEMPLATE)

    def test_technical_question_is_answered_without_calling_the_model(self) -> None:
        chat = FakeChat([])
        answer = vi_agent.run_tool_loop(chat, "¿Qué modelo de IA estás usando?")

        self.assertEqual(
            answer, vi_agent.implementation_question_response(vi_agent.CLIENT_CONFIG.display_name)
        )
        self.assertEqual(chat.messages, [])

    def test_mixed_question_keeps_the_business_part(self) -> None:
        chat = FakeChat(["Hay 17.536 conversaciones analizables."])
        chat.get_history = lambda **_: [SimpleNamespace(parts=[SimpleNamespace(function_response=SimpleNamespace(
            name="run_readonly_sql", response={"result":{"columns":["total"],"rows":[[17536]]}}
        ))])]
        answer = vi_agent.run_tool_loop(
            chat,
            "¿Qué tecnología usan y cuántas conversaciones analizables hay?",
        )

        self.assertEqual(answer, "Hay 17.536 conversaciones analizables.")
        self.assertEqual(len(chat.messages), 1)

    def test_unsafe_draft_is_rewritten_before_delivery(self) -> None:
        chat = FakeChat(
            [
                "La columna resultado_general en PostgreSQL indica 30%.",
                "La compra total representa el 30% de las conversaciones analizadas.",
            ]
        )
        chat.get_history = lambda **_: [SimpleNamespace(parts=[SimpleNamespace(function_response=SimpleNamespace(
            name="run_readonly_sql", response={"result":{"columns":["tasa","n_evaluados"],"rows":[[30,10]]}}
        ))])]
        answer = vi_agent.run_tool_loop(chat, "¿Cuál es la tasa de compra total?")

        self.assertTrue(answer.startswith("La compra total representa el 30% de las conversaciones analizadas."))
        self.assertEqual(len(chat.messages), 2)
        self.assertIn("Reescribí", str(chat.messages[1]))

    def test_repeated_unsafe_drafts_return_safe_fallback(self) -> None:
        chat = FakeChat(
            ["SQL resultado_total"] * (vi_agent.MAX_CLIENT_REWRITES + 1)
        )
        answer = vi_agent.run_tool_loop(chat, "¿Cuál es la tasa de compra total?")

        self.assertEqual(answer, vi_agent.UNSAFE_ANSWER_FALLBACK)

    def test_each_successful_model_response_records_usage(self) -> None:
        usage = SimpleNamespace(
            prompt_token_count=25,
            candidates_token_count=5,
            thoughts_token_count=2,
            cached_content_token_count=0,
            tool_use_prompt_token_count=0,
            total_token_count=32,
            traffic_type=None,
        )
        chat = FakeChat(
            [
                FakeResponse("La columna resultado_general indica 30%.", usage),
                FakeResponse(
                    "La compra total representa el 30% de las conversaciones analizadas.",
                    usage,
                ),
            ]
        )
        chat.get_history = lambda **_: [SimpleNamespace(parts=[SimpleNamespace(function_response=SimpleNamespace(
            name="run_readonly_sql", response={"result":{"columns":["tasa","n_evaluados"],"rows":[[30,10]]}}
        ))])]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            answer = vi_agent.run_tool_loop(
                chat,
                "¿Cuál es el resultado?",
                session_id="session-test",
                interaction_id="interaction-test",
                usage_recorder=UsageRecorder(path),
            )
            events = load_usage_events(path)

        self.assertTrue(answer.startswith("La compra total representa el 30% de las conversaciones analizadas."))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["session_id"], "session-test")
        self.assertEqual(events[0]["interaction_id"], "interaction-test")
        self.assertEqual(events[0]["call_index"], 1)
        self.assertEqual(events[0]["call_kind"], "initial")
        self.assertEqual(events[0]["total_token_count"], 32)
        self.assertEqual(events[1]["call_index"], 2)
        self.assertEqual(events[1]["call_kind"], "client_safe_rewrite")
        # retry_reason (2026-09-15, ver "6. busqueda_vectorial/README.md" > "Iteración 26") -el
        # motivo concreto del rewrite, no sólo que ocurrió. "resultado_general" es el término
        # snake_case real que dispara la violación en el borrador de arriba.
        self.assertNotIn("retry_reason", events[0], "la llamada inicial nunca es un reintento")
        self.assertEqual(
            events[1]["retry_reason"],
            {
                "violations": ["detalles internos o tecnológicos", "identificadores internos"],
                "offending_terms": ["columna", "resultado_general"],
            },
        )

    def test_on_tool_call_fires_before_each_tool_execution(self) -> None:
        # Progreso en vivo para la UI (2026-09-11, ver "8. README.md" > "Potencial de mejora" >
        # "Indicador de progreso por tool call") -on_tool_call se dispara con el nombre técnico y
        # los args crudos, ANTES de ejecutar la tool; streamlit_app.py es responsable de traducirlo
        # a una etiqueta de negocio antes de mostrarlo (esta capa nunca decide qué se ve).
        chat = FakeChat(
            [
                FakeResponse(
                    "",
                    function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})],
                ),
                FakeResponse("La respuesta final, sin nada técnico."),
            ]
        )
        calls_seen: list[tuple[str, dict]] = []
        with patch.object(vi_agent, "run_readonly_sql", return_value='{"row_count": 0, "rows": []}'):
            answer = vi_agent.run_tool_loop(
                chat,
                "¿Cuál es el total?",
                on_tool_call=lambda name, args: calls_seen.append((name, args)),
            )
        self.assertEqual(answer, "La respuesta final, sin nada técnico.")
        self.assertEqual(calls_seen, [("run_readonly_sql", {"sql": "SELECT 1"})])

    def test_usage_event_records_which_tool_produced_it(self) -> None:
        # tools_called (2026-09-14, pedido explícito: "costo de cada tool") -el evento de uso de la
        # llamada "tool_results" tiene que quedar atado a qué tool(s) generaron el resultado que esa
        # llamada le está devolviendo al modelo, para poder desglosar costo por tool después (ver
        # usage_report.py --by-tool).
        usage = SimpleNamespace(
            prompt_token_count=100, candidates_token_count=10, thoughts_token_count=0,
            cached_content_token_count=0, tool_use_prompt_token_count=0, total_token_count=110,
            traffic_type=None,
        )
        chat = FakeChat(
            [
                FakeResponse(
                    "", usage,
                    function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})],
                ),
                FakeResponse("Respuesta final.", usage),
            ]
        )
        with patch.object(
            vi_agent, "run_readonly_sql", return_value='{"row_count": 0, "rows": []}'
        ), tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            vi_agent.run_tool_loop(chat, "¿Cuál es el total?", usage_recorder=UsageRecorder(path))
            events = load_usage_events(path)

        self.assertEqual(events[0]["call_kind"], "initial")
        self.assertNotIn("tools_called", events[0], "la primera llamada no procesó ningún tool")
        self.assertEqual(events[1]["call_kind"], "tool_results")
        self.assertEqual(events[1]["tools_called"], ["run_readonly_sql"])

    def test_usage_event_records_multiple_tools_from_a_parallel_turn(self) -> None:
        usage = SimpleNamespace(
            prompt_token_count=100, candidates_token_count=10, thoughts_token_count=0,
            cached_content_token_count=0, tool_use_prompt_token_count=0, total_token_count=110,
            traffic_type=None,
        )
        chat = FakeChat(
            [
                FakeResponse(
                    "", usage,
                    function_calls=[
                        FakeFunctionCall("get_business_rules", {"rulebook": "sales_evaluation"}),
                        FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"}),
                    ],
                ),
                FakeResponse("Respuesta final.", usage),
            ]
        )
        with patch.object(
            vi_agent, "run_readonly_sql", return_value='{"row_count": 0, "rows": []}'
        ), patch.object(
            vi_agent, "get_business_rules", return_value='{"criterios": []}'
        ), tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            vi_agent.run_tool_loop(chat, "¿Cuál es el total?", usage_recorder=UsageRecorder(path))
            events = load_usage_events(path)

        # Ordenado alfabéticamente, no por orden de ejecución -mismo criterio que el resto del
        # proyecto para que la clave sea estable sin importar el orden en que Gemini las pidió.
        self.assertEqual(events[1]["tools_called"], ["get_business_rules", "run_readonly_sql"])

    def test_multiple_tool_calls_in_the_same_turn_run_in_parallel(self) -> None:
        # Paralelización de tool calls (2026-09-11, ver "8. README.md" > "Potencial de mejora") -
        # medido en vivo (SQL real + búsqueda vectorial real, mens_fashion_alto): ~30-40% menos
        # latencia en runs calientes. Acá se valida con dos tools falsas que duermen 0.2s cada
        # una: si corrieran secuencial el tiempo total sería >=0.4s, en paralelo debe quedar bien
        # por debajo -y el orden de las respuestas debe seguir coincidiendo con el orden de los
        # function_calls originales (Gemini empareja por nombre, no por posición, pero no hay
        # razón para desordenarlas).
        import time

        chat = FakeChat(
            [
                FakeResponse(
                    "",
                    function_calls=[
                        FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"}),
                        FakeFunctionCall("get_business_rules", {"rulebook": "sales_evaluation"}),
                    ],
                ),
                FakeResponse("La respuesta final, sin nada técnico."),
            ]
        )

        def _slow_sql(sql: str) -> str:
            time.sleep(0.2)
            return '{"row_count": 0, "rows": []}'

        def _slow_rules(rulebook: str) -> str:
            time.sleep(0.2)
            return '{"criterios": []}'

        with patch.object(vi_agent, "run_readonly_sql", side_effect=_slow_sql), patch.object(
            vi_agent, "get_business_rules", side_effect=_slow_rules
        ), patch.dict(
            vi_agent.TOOL_FUNCTIONS,
            {"run_readonly_sql": _slow_sql, "get_business_rules": _slow_rules},
        ):
            tool_calls_log: list[dict] = []
            start = time.perf_counter()
            answer = vi_agent.run_tool_loop(
                chat, "¿Cuál es el total?", tool_calls_log=tool_calls_log
            )
            elapsed = time.perf_counter() - start

        self.assertTrue(answer.startswith("La respuesta final, sin nada técnico."))
        self.assertIn("no encontró registros", answer)
        self.assertLess(elapsed, 0.35)
        self.assertEqual(
            [entry["name"] for entry in tool_calls_log],
            ["run_readonly_sql", "get_business_rules"],
        )


class RulebookOptionsGeneralizationTests(unittest.TestCase):
    """Regresión (2026-09-11, ver '3. experimentos/coaching_playbook/'): la guía de negocio de
    'HERRAMIENTAS INTERNAS' del prompt nombraba a mano sólo sales_evaluation/conversation_insights
    -agregar coaching_playbook a un cliente sin este cambio significaba que el modelo lo veía en
    rulebook_keys pero sin ninguna guía de cuándo usarlo. Ahora se arma sola desde business_scope."""

    def test_lists_every_rulebook_with_its_business_scope(self) -> None:
        original_config = vi_agent.CLIENT_CONFIG
        try:
            vi_agent.configure_client("mens_fashion_alto")
            options = vi_agent._build_rulebook_options()
        finally:
            vi_agent.CLIENT_CONFIG = original_config
        self.assertIn("sales_evaluation (para", options)
        self.assertIn("conversation_insights (para", options)

    def test_a_new_rulebook_appears_without_touching_the_template(self) -> None:
        config = load_client_config("mens_fashion_alto")
        from client_config import BusinessRulebookConfig

        config.business_rulebooks["coaching_playbook"] = BusinessRulebookConfig(
            key="coaching_playbook",
            name="clientes/Mens Fashion/coaching_playbook",
            label="production",
            business_scope="Guía de coaching para vendedores de bajo desempeño",
        )
        options = vi_agent._build_rulebook_options(config)
        self.assertIn("coaching_playbook (para guía de coaching", options)


class NaturalBusinessWordsRegressionTests(unittest.TestCase):
    """Regresión del bug encontrado el 2026-09-08: campos de tipo `text` sin
    guion bajo (ej. "tecnologia", "color", "region") se cargaban como
    identificador interno a bloquear -mismo bug de fondo que ya se había
    corregido para "tecnología" en Salomon, pero por el camino de
    `_load_internal_identifiers`, no el de `_ANSWER_LEAK_TERMS`- y afectaba a
    7 de los 19 clientes, no sólo a uno.

    El fix original (2026-09-08) fue una whitelist manual de las 13 palabras
    encontradas. El 2026-09-09 se reemplazó por `_NATURAL_WORD_MAX_LENGTH` en
    vi_agent.py (heurística por longitud, validada exhaustivamente contra los
    19 Data Maps reales -ver `AllClientsIdentifierLengthInvariantTests` abajo-)
    para que un cliente nuevo con su propia palabra de negocio corta quede
    cubierto sin mantenimiento manual. Estos tests siguen probando el
    comportamiento observable (¿la palabra queda bloqueada o no?), no el
    mecanismo interno -por eso no hizo falta tocarlos al migrar-."""

    def _assert_word_not_blocked(self, client_id: str, word: str, sentence: str) -> None:
        client = load_client_config(client_id)
        identifiers = vi_agent._load_internal_identifiers(client.data_map_path)
        self.assertNotIn(
            word, identifiers, f"{word!r} quedó como identificador interno para {client_id}"
        )
        self.assertEqual(
            client_answer_violations(sentence, internal_identifiers=identifiers), []
        )

    def test_atlas_mattress_vocabulary_is_not_blocked(self) -> None:
        self._assert_word_not_blocked(
            "atlas_alto",
            "tecnologia",
            "El cliente valoró la tecnología de espuma con gel del modelo Manhattan "
            "en la medida queen size, y el colchón fue comprado.",
        )

    def test_forever21_and_shoe_box_apparel_vocabulary_is_not_blocked(self) -> None:
        for client_id in ("forever_21_bajo", "shoe_box_bajo"):
            with self.subTest(client_id=client_id):
                self._assert_word_not_blocked(
                    client_id, "color", "El cliente preguntó por otro color y otra talla."
                )

    def test_steren_tigo_gac_hyundai_vocabulary_is_not_blocked(self) -> None:
        self._assert_word_not_blocked(
            "steren_alto", "categoria", "El vendedor mostró productos de otra categoría."
        )
        self._assert_word_not_blocked(
            "tigo_alto",
            "sentimiento",
            "El sentimiento del cliente sobre la región fue positivo.",
        )
        for client_id in ("gac_medio", "hyundai_bajo"):
            with self.subTest(client_id=client_id):
                self._assert_word_not_blocked(
                    client_id, "saludo", "El vendedor comenzó con un saludo cordial."
                )

    def test_compound_field_names_still_blocked(self) -> None:
        """Guardrail: el fix de arriba no debe volver permisiva la detección de
        identificadores realmente internos -un nombre de campo compuesto y
        concatenado (que nadie escribiría así en una charla real) sigue
        debiendo bloquearse."""
        client = load_client_config("atlas_alto")
        identifiers = vi_agent._load_internal_identifiers(client.data_map_path)
        self.assertIn("explicacaracteristicasybeneficiosdelproducto", identifiers)
        self.assertEqual(
            client_answer_violations(
                "El criterio evaluado fue explicacaracteristicasybeneficiosdelproducto.",
                internal_identifiers=identifiers,
            ),
            ["identificadores internos"],
        )


class AllClientsIdentifierLengthInvariantTests(unittest.TestCase):
    """Prueba, contra los 19 Data Maps reales a la vez (no casos elegidos a mano), el invariante
    en el que se apoya `_NATURAL_WORD_MAX_LENGTH` (vi_agent.py, 2026-09-09): NINGÚN nombre de
    campo/fuente sin guion bajo de ≤12 caracteres queda bloqueado. Es la validación exhaustiva que
    en 2026-09-08 se había evaluado y descartado por falta de tiempo -si algún cliente nuevo
    rompe este invariante (un nombre corto que sí debería bloquearse), este test lo va a agarrar
    antes que un usuario en producción; la respuesta correcta ahí es sumarlo a
    `_FORCE_BLOCK_SHORT_IDENTIFIERS`, no bajar el umbral general sin volver a mirar los 19 casos."""

    def test_no_client_blocks_a_short_no_underscore_identifier(self) -> None:
        for client_id in available_client_ids():
            with self.subTest(client_id=client_id):
                client = load_client_config(client_id)
                identifiers = vi_agent._load_internal_identifiers(client.data_map_path)
                too_short = [word for word in identifiers if len(word) <= vi_agent._NATURAL_WORD_MAX_LENGTH]
                self.assertEqual(
                    too_short, [], f"{client_id}: identificador(es) corto(s) bloqueado(s) sin querer"
                )


class DataMapChangelogStrippingTests(unittest.TestCase):
    """`_strip_changelog_metadata_for_prompt` (2026-09-17, misma investigación de costo que
    _build_extra_tools_section): saca metadata.changes_from_v*/hallazgos_criticos_verificados_*/
    pendiente_de_verificar del texto que se manda a Gemini -historial de auditoría para un humano,
    nunca información operativa- sin tocar el archivo .yaml en disco."""

    def test_removes_only_changelog_keys_keeps_rest_byte_identical(self) -> None:
        raw = (
            "metadata:\n"
            "  name: X\n"
            "  version: 1\n"
            "  scope: algo real que el modelo necesita\n"
            "  changes_from_v2: >-\n"
            "    texto largo de varias\n"
            "    lineas sobre que cambio\n"
            "  hallazgos_criticos_verificados_2026-09-08:\n"
            "    - 'hallazgo uno'\n"
            "    - 'hallazgo dos'\n"
            "  pendiente_de_verificar: >-\n"
            "    lo que falta auditar despues\n"
            "sources:\n"
            "  tabla_real:\n"
            "    fields: {}\n"
        )
        stripped = vi_agent._strip_changelog_metadata_for_prompt(raw)
        parsed = yaml.safe_load(stripped)
        self.assertEqual(
            parsed["metadata"],
            {"name": "X", "version": 1, "scope": "algo real que el modelo necesita"},
        )
        self.assertEqual(parsed["sources"], {"tabla_real": {"fields": {}}})
        self.assertNotIn("changes_from_v2", stripped)
        self.assertNotIn("hallazgo uno", stripped)
        self.assertNotIn("pendiente_de_verificar", stripped)

    def test_client_with_no_changelog_keys_is_untouched(self) -> None:
        raw = "metadata:\n  name: X\nsources:\n  t:\n    fields: {}\n"
        self.assertEqual(vi_agent._strip_changelog_metadata_for_prompt(raw), raw)

    def test_never_touches_the_file_on_disk(self) -> None:
        client = load_client_config("farma24_alto")
        before = client.data_map_path.read_text(encoding="utf-8")
        vi_agent._strip_changelog_metadata_for_prompt(before)
        after = client.data_map_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_all_19_real_data_maps_keep_every_non_changelog_section_identical(self) -> None:
        """Validación exhaustiva contra los Data Maps reales, no casos elegidos a mano -mismo
        criterio que AllClientsIdentifierLengthInvariantTests arriba."""
        excluded_prefixes = ("changes_from_", "hallazgos_criticos_verificados_")
        for client_id in available_client_ids():
            with self.subTest(client_id=client_id):
                client = load_client_config(client_id)
                raw = client.data_map_path.read_text(encoding="utf-8")
                stripped = vi_agent._strip_changelog_metadata_for_prompt(raw)
                old_data = yaml.safe_load(raw)
                new_data = yaml.safe_load(stripped)
                self.assertEqual(old_data.keys(), new_data.keys())
                expected_meta = {
                    k: v for k, v in old_data.get("metadata", {}).items()
                    if not (k.startswith(excluded_prefixes) or k == "pendiente_de_verificar")
                }
                self.assertEqual(expected_meta, new_data.get("metadata", {}))
                for key in old_data:
                    if key != "metadata":
                        self.assertEqual(old_data[key], new_data.get(key), key)


class UnbackedAnswerNumbersTests(unittest.TestCase):
    """unbacked_answer_numbers (2026-09-14, pedido explícito del usuario: confiabilidad de las
    respuestas) -señal informativa (sólo --internal-debug), nunca bloquea ni reescribe la
    respuesta real. Ver el comentario extenso en vi_agent.py para por qué no es un gate."""

    def test_number_present_literally_in_sql_result_is_backed(self) -> None:
        answer = "Se registraron 247.556 conversaciones analizables."
        sql_results = ['{"row_count": 1, "rows": [{"total": 247556}]}']
        self.assertEqual(vi_agent.unbacked_answer_numbers(answer, sql_results), set())

    def test_number_absent_from_any_sql_result_is_unbacked(self) -> None:
        answer = "La tasa de cierre fue del 999%."
        sql_results = ['{"row_count": 1, "rows": [{"total": 500, "cerradas": 100}]}']
        self.assertEqual(vi_agent.unbacked_answer_numbers(answer, sql_results), {"999"})

    def test_number_within_drift_tolerance_counts_as_backed(self) -> None:
        # close_enough (numeric_text.py) tolera ±1 absoluto o ±0.5% -mismo criterio que ya usa
        # data_map_auto_update.py para la deriva natural de una base que crece.
        answer = "Se registraron 247.557 conversaciones."
        sql_results = ['{"rows": [{"total": 247556}]}']
        self.assertEqual(vi_agent.unbacked_answer_numbers(answer, sql_results), set())

    def test_no_answer_numbers_returns_empty_set(self) -> None:
        answer = "El equipo tuvo un desempeño sólido en general."
        self.assertEqual(vi_agent.unbacked_answer_numbers(answer, ['{"rows": []}']), set())

    def test_no_sql_results_at_all_flags_every_answer_number(self) -> None:
        # Sin ninguna llamada a run_readonly_sql en la interacción (ej. la respuesta vino sólo de
        # get_business_rules), cualquier número citado no tiene con qué contrastarse.
        answer = "El criterio bajó al 320%."
        self.assertEqual(vi_agent.unbacked_answer_numbers(answer, []), {"320"})

    def test_small_incidental_numbers_are_not_flagged(self) -> None:
        # numbers_in() (numeric_text.py) ya filtra enteros chicos sin decimales (ruido incidental,
        # ej. "Farma 24", "top 5") -unbacked_answer_numbers hereda ese filtro sin reimplementarlo.
        answer = "Mirá el top 5 de este mes."
        self.assertEqual(vi_agent.unbacked_answer_numbers(answer, []), set())


class StructuredAnswerPolicyTests(unittest.TestCase):
    def _answer(self, chart: dict) -> str:
        return "Ventas por período.\n```vera-chart\n" + json.dumps(chart) + "\n```\n"

    def _charts(self) -> list[dict]:
        return [
            {"type": chart_type, "title": "Ventas", "labels": ["Agosto"],
             "series": [{"name": "Actual", "values": [12]}, {"name": "Anterior", "values": [10]}]}
            for chart_type in ("grouped_bar", "stacked_bar")
        ] + [
            {"type": "scatter", "title": "Ventas", "x_values": [1, 2], "values": [3, 4]},
            {"type": "bubble", "title": "Ventas", "x_values": [1, 2], "values": [3, 4], "sizes": [5, 6]},
        ]

    def test_valid_structures_do_not_trigger_a_model_rewrite(self) -> None:
        for chart in self._charts():
            with self.subTest(chart_type=chart["type"]):
                answer = self._answer(chart)
                chat = FakeChat([answer])
                self.assertEqual(vi_agent.run_tool_loop(chat, "Compará las ventas."), answer.strip())
                self.assertEqual(len(chat.messages), 1)

    def test_streaming_valid_charts_do_not_block_following_text(self) -> None:
        for chart in self._charts():
            with self.subTest(chart_type=chart["type"]):
                buf = vi_agent._StreamingAnswerBuffer()
                answer = self._answer(chart) + "Hay una oportunidad de mejora.\n"
                released = "".join(buf.feed(char) for char in answer)
                self.assertFalse(buf.unsafe)
                self.assertIn("Hay una oportunidad de mejora.", released)
                self.assertNotIn("vera-chart", released)

    def test_all_visible_chart_texts_are_still_checked(self) -> None:
        for location in ("title", "labels", "series"):
            with self.subTest(location=location):
                chart = self._charts()[0]
                if location == "title":
                    chart["title"] = "Postgres"
                elif location == "labels":
                    chart["labels"] = ["resultado_general"]
                else:
                    chart["series"][0]["name"] = "vendedoramable"
                answer = self._answer(chart)
                checked = vi_agent._answer_policy_text(answer)
                self.assertTrue(client_answer_violations(
                    checked, internal_identifiers=frozenset({"vendedoramable"})
                ))
                buf = vi_agent._StreamingAnswerBuffer(internal_identifiers=frozenset({"vendedoramable"}))
                buf.feed(answer)
                self.assertTrue(buf.unsafe)

    def test_rewrite_terms_exclude_schema_and_include_the_actual_leak(self) -> None:
        chart = self._charts()[0]
        chart["title"] = "resultado_general"
        chat = FakeChat([self._answer(chart), "Las ventas crecieron."])
        self.assertEqual(vi_agent.run_tool_loop(chat, "Compará las ventas."), "Las ventas crecieron.")
        self.assertIn("resultado_general", chat.messages[1])
        self.assertNotIn("grouped_bar", chat.messages[1])

    def test_invalid_and_unclosed_blocks_are_not_exempted(self) -> None:
        for answer in (
            '```vera-chart\n{"type":"grouped_bar","labels":["A"]}\n```',
            '```vera-chart\n{"type":"grouped_bar", invalid}\n```',
            '```vera-chart\n{"type":"grouped_bar"',
        ):
            with self.subTest(answer=answer):
                self.assertEqual(vi_agent._answer_policy_text(answer), answer)
                self.assertTrue(client_answer_violations(vi_agent._answer_policy_text(answer)))

    def test_unicode_escaped_chart_and_suggestion_text_is_checked(self) -> None:
        chart = self._charts()[0]
        chart["title"] = "Postgres"
        answer = self._answer(chart).replace("Postgres", r"\u0050ostgres")
        self.assertTrue(client_answer_violations(vi_agent._answer_policy_text(answer)))
        suggestions = '```vera-suggestions\n["Consultá ' + r'\u0050ostgres' + '"]\n```'
        self.assertTrue(client_answer_violations(vi_agent._answer_policy_text(suggestions)))

    def test_plain_text_identifiers_remain_unsafe_next_to_a_valid_chart(self) -> None:
        answer = "resultado_general\n" + self._answer(self._charts()[0])
        self.assertTrue(client_answer_violations(vi_agent._answer_policy_text(answer)))


class CompactSqlResultTests(unittest.TestCase):
    def _connection(self, columns: list[str], rows: list[tuple]) -> MagicMock:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [SimpleNamespace(name=name) for name in columns]
        cursor.fetchmany.return_value = rows
        connection = MagicMock()
        connection.cursor.return_value = cursor
        return connection

    def test_preserves_order_nulls_types_and_existing_safe_conversions(self) -> None:
        from datetime import date
        from decimal import Decimal

        columns = ["tienda", "ventas", "dato", "activo", "fecha", "importe"]
        connection = self._connection(columns, [("México", 247556, None, True, date(2026, 9, 14), Decimal("12.50"))])
        with patch.object(vi_agent, "_validate_sql"), patch.object(
            vi_agent, "_get_reusable_sql_connection", return_value=connection
        ):
            result = json.loads(vi_agent.run_readonly_sql("SELECT ..."))
        self.assertEqual(result, {
            "row_count": 1, "truncated": False, "columns": columns,
            "rows": [["México", 247556, None, True, "2026-09-14", 12.5]],
        })
        connection.cursor.return_value.fetchmany.assert_called_once_with(201)

    def test_empty_result_still_describes_columns(self) -> None:
        result = vi_agent._fetch_readonly_rows(self._connection(["tienda", "ventas"], []), "SELECT ...")
        self.assertEqual(result, {
            "row_count": 0, "truncated": False, "columns": ["tienda", "ventas"], "rows": [],
        })

    def test_limit_and_truncation_are_preserved(self) -> None:
        for count in (200, 201):
            with self.subTest(count=count):
                result = vi_agent._fetch_readonly_rows(
                    self._connection(["ventas"], [(i,) for i in range(count)]), "SELECT ..."
                )
                self.assertEqual(result["row_count"], 200)
                self.assertEqual(result["truncated"], count > 200)
                self.assertEqual(result["rows"], [[i] for i in range(200)])

    def test_duplicate_column_names_do_not_discard_values(self) -> None:
        result = vi_agent._fetch_readonly_rows(self._connection(["total", "total"], [(100, 200)]), "SELECT ...")
        self.assertEqual(result["columns"], ["total", "total"])
        self.assertEqual(result["rows"], [[100, 200]])

    def test_numeric_evidence_accepts_compact_results(self) -> None:
        result = json.dumps({"row_count": 1, "truncated": False, "columns": ["total"], "rows": [[247556]]})
        self.assertEqual(vi_agent.unbacked_answer_numbers("Hay 247.556 conversaciones.", [result]), set())
        self.assertEqual(vi_agent.unbacked_answer_numbers("Hay 888.888 conversaciones.", [result]), {"888888"})


class StreamingAnswerBufferTests(unittest.TestCase):
    """`_StreamingAnswerBuffer` (2026-09-14, pedido explícito del usuario: streaming con
    seguridad) -sólo libera líneas completas ya validadas contra client_answer_violations, nunca
    contenido dentro de un fence ``` sin cerrar. Ver el comentario extenso arriba de la clase en
    vi_agent.py para las garantías exactas y el riesgo residual documentado a propósito."""

    def test_holds_back_text_without_a_complete_line(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        self.assertEqual(buf.feed("Sin salto de línea todavía"), "")

    def test_releases_once_a_line_completes(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        buf.feed("Primera línea")
        released = buf.feed(" completa.\nSegunda línea en curso")
        self.assertEqual(released, "Primera línea completa.\n")

    def test_accumulates_across_multiple_feeds_before_releasing(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        self.assertEqual(buf.feed("Hola "), "")
        self.assertEqual(buf.feed("mundo"), "")
        self.assertEqual(buf.feed(".\n"), "Hola mundo.\n")

    def test_holds_back_content_inside_an_unclosed_fence(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        released = buf.feed("Texto normal.\n```vera-chart\n{\"type\": \"bar\",\n")
        self.assertEqual(released, "Texto normal.\n")

    def test_strips_chart_block_once_the_fence_closes(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        buf.feed("Texto normal.\n```vera-chart\n")
        buf.feed('{"type": "bar", "labels": ["A"], "values": [1]}\n```\n')
        released = buf.feed("Después del gráfico.\n")
        # El bloque vera-chart nunca debe aparecer como texto visible -se extrae/renderiza aparte,
        # igual que en el flujo no-streameado (extract_chart_blocks).
        self.assertNotIn("vera-chart", released)
        self.assertNotIn('"type": "bar"', released)
        self.assertIn("Después del gráfico.", released)

    def test_strips_suggestions_block_once_the_fence_closes(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        buf.feed("Texto normal.\n```vera-suggestions\n")
        released = buf.feed('["¿Pregunta 1?"]\n```\nMás texto.\n')
        self.assertNotIn("vera-suggestions", released)
        self.assertNotIn("¿Pregunta 1?", released)
        self.assertIn("Más texto.", released)

    def test_marks_unsafe_and_stops_releasing_on_violation(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        released = buf.feed("Algo normal.\nEl campo resultado_general indica 30%.\n")
        self.assertFalse(released)  # nada se libera de la línea con el identificador interno
        self.assertTrue(buf.unsafe)
        # Un feed posterior tampoco libera nada, incluso si en sí mismo sería inocuo -el turno
        # completo queda congelado una vez detectada una violación.
        self.assertEqual(buf.feed("Texto totalmente inocuo.\n"), "")

    def test_full_text_returns_everything_fed_regardless_of_release(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer()
        buf.feed("Línea completa.\nLínea sin cerrar")
        self.assertEqual(buf.full_text(), "Línea completa.\nLínea sin cerrar")

    def test_internal_identifiers_from_data_map_are_also_caught(self) -> None:
        buf = vi_agent._StreamingAnswerBuffer(internal_identifiers=frozenset({"mi_campo_secreto"}))
        released = buf.feed("El valor de mi_campo_secreto es 5.\n")
        self.assertEqual(released, "")
        self.assertTrue(buf.unsafe)


class StreamingRunToolLoopTests(unittest.TestCase):
    """run_tool_loop con on_text_delta (2026-09-14) -usa un chat falso que streamea en chunks de
    texto, verificando que sólo texto ya validado llegue al callback, que los turnos de
    tool-calling sigan funcionando sin cambios, y que on_stream_invalidated se dispare cuando el
    texto ya liberado para un turno se descarta (reescritura de seguridad)."""

    def _fake_streaming_chat(self, turns: list[list[SimpleNamespace]]) -> SimpleNamespace:
        turns_iter = iter(turns)
        messages: list[object] = []

        def send_message_stream(message):
            messages.append(message)
            return iter(next(turns_iter))

        chat = SimpleNamespace(send_message_stream=send_message_stream, messages=messages)
        return chat

    def _chunk(self, text="", function_calls=None) -> SimpleNamespace:
        usage = SimpleNamespace(
            prompt_token_count=10, candidates_token_count=1, thoughts_token_count=0,
            cached_content_token_count=0, tool_use_prompt_token_count=0, total_token_count=11,
            traffic_type=None,
        )
        return SimpleNamespace(text=text, function_calls=function_calls, usage_metadata=usage)

    def test_streams_safe_text_incrementally(self) -> None:
        chat = self._fake_streaming_chat(
            [[self._chunk("Primera línea.\n"), self._chunk("Segunda línea.")]]
        )
        deltas: list[str] = []
        answer = vi_agent.run_tool_loop(
            chat, "¿Cuál es el total?", on_text_delta=deltas.append
        )
        self.assertEqual(answer, "Primera línea.\nSegunda línea.")
        self.assertEqual(deltas, ["Primera línea.\n"])  # la 2da línea nunca cierra, no se libera

    def test_tool_call_turn_streams_no_text_and_still_dispatches_the_tool(self) -> None:
        chat = self._fake_streaming_chat(
            [
                [self._chunk(function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})])],
                [self._chunk("Respuesta final.\n")],
            ]
        )
        deltas: list[str] = []
        with patch.object(vi_agent, "run_readonly_sql", return_value='{"row_count": 0, "rows": []}'):
            answer = vi_agent.run_tool_loop(
                chat, "¿Cuál es el total?", on_text_delta=deltas.append
            )
        self.assertEqual(answer, "Respuesta final.")
        self.assertEqual(deltas, ["Respuesta final.\n"])

    def test_violation_invalidates_stream_and_triggers_rewrite(self) -> None:
        chat = self._fake_streaming_chat(
            [
                [self._chunk("El campo resultado_general indica 30%.\n")],
                [self._chunk("La compra representa el 30% de las conversaciones.\n")],
            ]
        )
        chat.get_history = lambda **_: [SimpleNamespace(parts=[SimpleNamespace(function_response=SimpleNamespace(
            name="run_readonly_sql", response={"result":{"columns":["tasa","n_evaluados"],"rows":[[30,10]]}}
        ))])]
        deltas: list[str] = []
        invalidated = []
        answer = vi_agent.run_tool_loop(
            chat,
            "¿Cuál es el resultado?",
            on_text_delta=deltas.append,
            on_stream_invalidated=lambda: invalidated.append(True),
        )
        self.assertTrue(answer.startswith("La compra representa el 30% de las conversaciones."))
        # El borrador CON el identificador interno nunca se liberó -sólo el texto de la reescritura
        # (segura) que vino después, que sí es válido mostrar.
        self.assertTrue("".join(deltas).startswith("La compra representa el 30% de las conversaciones."))
        self.assertNotIn("resultado_general", "".join(deltas))
        self.assertEqual(len(invalidated), 1)


class ChartBlockExtractionTests(unittest.TestCase):
    """extract_chart_blocks (2026-09-09, ver VISUALIZACIÓN en SYSTEM_INSTRUCTION_TEMPLATE): separa
    los bloques ```vera-chart {...}``` de la respuesta del modelo en texto limpio + charts
    validados, para que la interfaz web (streamlit_app.py) los renderice como gráfico nativo sin
    una llamada adicional al modelo -mismo response, sólo se le pide un formato extra-."""

    def test_bar_chart_extracted_and_removed_from_text(self) -> None:
        answer = (
            'Texto de negocio.\n\n```vera-chart\n'
            '{"type": "bar", "title": "T", "labels": ["A", "B"], "values": [1, 2]}\n```'
        )
        cleaned, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(cleaned, "Texto de negocio.")
        self.assertEqual(
            charts, [{"type": "bar", "title": "T", "values": [1.0, 2.0], "labels": ["A", "B"]}]
        )

    def test_line_chart(self) -> None:
        answer = '```vera-chart\n{"type": "line", "labels": ["Jun", "Jul"], "values": [1.5, 2.5]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts[0]["type"], "line")
        self.assertEqual(charts[0]["labels"], ["Jun", "Jul"])

    def test_scatter_uses_x_values_instead_of_labels(self) -> None:
        answer = '```vera-chart\n{"type": "scatter", "x_values": [1, 2, 3], "values": [4, 5, 6]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [{"type": "scatter", "title": "", "values": [4.0, 5.0, 6.0], "x_values": [1.0, 2.0, 3.0]}])

    def test_bubble_requires_sizes(self) -> None:
        answer = '```vera-chart\n{"type": "bubble", "x_values": [1, 2], "values": [4, 5]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [], "sin sizes, un bubble no es válido -no debe degradar a otro tipo-")

        answer_ok = '```vera-chart\n{"type": "bubble", "x_values": [1, 2], "values": [4, 5], "sizes": [10, 20]}\n```'
        _, charts_ok = vi_agent.extract_chart_blocks(answer_ok)
        self.assertEqual(charts_ok[0]["sizes"], [10.0, 20.0])

    def test_invalid_json_is_dropped_silently_and_stripped_from_text(self) -> None:
        answer = "Texto.\n```vera-chart\n{esto no es json}\n```"
        cleaned, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(cleaned, "Texto.")
        self.assertEqual(charts, [])

    def test_mismatched_lengths_dropped(self) -> None:
        answer = '```vera-chart\n{"type": "bar", "labels": ["A"], "values": [1, 2]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [])

    def test_unknown_type_drops_the_block_instead_of_defaulting_to_bar(self) -> None:
        # Regresión directa del cambio 2026-09-14 (pedido explícito: "que no haga siempre
        # gráficos de barra") -antes CUALQUIER type inválido/ausente degradaba en silencio a
        # "bar", lo que sesgaba de hecho hacia barra incluso con un JSON mal formado. Ahora se
        # descarta el bloque entero en vez de mostrar un tipo que el modelo no pidió.
        answer = 'Texto.\n```vera-chart\n{"type": "pastel", "labels": ["A"], "values": [1]}\n```'
        cleaned, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(cleaned, "Texto.")
        self.assertEqual(charts, [])

    def test_missing_type_drops_the_block(self) -> None:
        answer = '```vera-chart\n{"labels": ["A"], "values": [1]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [])

    def test_hbar_uses_labels_and_values_like_bar(self) -> None:
        answer = '```vera-chart\n{"type": "hbar", "labels": ["A", "B"], "values": [3, 7]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(
            charts, [{"type": "hbar", "title": "", "labels": ["A", "B"], "values": [3.0, 7.0]}]
        )

    def test_area_uses_labels_and_values_like_line(self) -> None:
        answer = '```vera-chart\n{"type": "area", "labels": ["Jun", "Jul"], "values": [10, 20]}\n```'
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts[0]["type"], "area")
        self.assertEqual(charts[0]["labels"], ["Jun", "Jul"])

    def test_pie_and_donut_use_labels_and_values(self) -> None:
        for chart_type in ("pie", "donut"):
            with self.subTest(chart_type=chart_type):
                answer = (
                    f'```vera-chart\n{{"type": "{chart_type}", "labels": ["Positivo", "Negativo"], '
                    '"values": [70, 30]}\n```'
                )
                _, charts = vi_agent.extract_chart_blocks(answer)
                self.assertEqual(charts[0]["type"], chart_type)
                self.assertEqual(charts[0]["values"], [70.0, 30.0])

    def test_grouped_bar_requires_at_least_two_series(self) -> None:
        answer = (
            '```vera-chart\n{"type": "grouped_bar", "labels": ["Tienda A", "Tienda B"], '
            '"series": [{"name": "S1", "values": [1, 2]}]}\n```'
        )
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [], "una sola serie no es un gráfico multi-serie válido")

    def test_grouped_bar_with_valid_series(self) -> None:
        answer = (
            '```vera-chart\n{"type": "grouped_bar", "title": "Cumplimiento por tienda", '
            '"labels": ["Tienda A", "Tienda B"], '
            '"series": [{"name": "Criterio 1", "values": [40, 60]}, '
            '{"name": "Criterio 2", "values": [55, 45]}]}\n```'
        )
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(
            charts,
            [
                {
                    "type": "grouped_bar",
                    "title": "Cumplimiento por tienda",
                    "labels": ["Tienda A", "Tienda B"],
                    "series": [
                        {"name": "Criterio 1", "values": [40.0, 60.0]},
                        {"name": "Criterio 2", "values": [55.0, 45.0]},
                    ],
                }
            ],
        )

    def test_stacked_bar_series_length_must_match_labels(self) -> None:
        answer = (
            '```vera-chart\n{"type": "stacked_bar", "labels": ["A", "B", "C"], '
            '"series": [{"name": "S1", "values": [1, 2]}, {"name": "S2", "values": [3, 4]}]}\n```'
        )
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [])

    def test_series_item_without_name_is_dropped(self) -> None:
        answer = (
            '```vera-chart\n{"type": "grouped_bar", "labels": ["A"], '
            '"series": [{"values": [1]}, {"name": "S2", "values": [2]}]}\n```'
        )
        _, charts = vi_agent.extract_chart_blocks(answer)
        self.assertEqual(charts, [])

    def test_no_block_returns_text_unchanged(self) -> None:
        cleaned, charts = vi_agent.extract_chart_blocks("Sólo texto, sin gráfico.")
        self.assertEqual(cleaned, "Sólo texto, sin gráfico.")
        self.assertEqual(charts, [])


class SuggestionBlockExtractionTests(unittest.TestCase):
    """extract_suggestion_blocks (2026-09-09): mismo patrón que extract_chart_blocks, para las
    preguntas sugeridas clickeables de streamlit_app.py. Desde el 2026-09-11 (SUGERENCIAS DE
    SEGUIMIENTO en SYSTEM_INSTRUCTION_TEMPLATE) SÍ es parte del prompt compartido y aparece en
    TODA respuesta de negocio, no sólo en el saludo inicial -antes era exclusivo de
    GREETING_PROMPT."""

    def test_extracts_and_strips_block(self) -> None:
        answer = (
            'Menú de bienvenida.\n\n```vera-suggestions\n'
            '["¿Cuál fue la tasa de compra?", "¿Quiénes venden más?"]\n```'
        )
        cleaned, suggestions = vi_agent.extract_suggestion_blocks(answer)
        self.assertEqual(cleaned, "Menú de bienvenida.")
        self.assertEqual(suggestions, ["¿Cuál fue la tasa de compra?", "¿Quiénes venden más?"])

    def test_drops_non_string_and_empty_items(self) -> None:
        answer = '```vera-suggestions\n["Pregunta válida", "", 123, null, "  "]\n```'
        _, suggestions = vi_agent.extract_suggestion_blocks(answer)
        self.assertEqual(suggestions, ["Pregunta válida"])

    def test_caps_at_max_suggestions(self) -> None:
        items = [f"Pregunta {i}" for i in range(10)]
        answer = "```vera-suggestions\n" + str(items).replace("'", '"') + "\n```"
        _, suggestions = vi_agent.extract_suggestion_blocks(answer)
        self.assertLessEqual(len(suggestions), vi_agent._MAX_SUGGESTIONS)

    def test_invalid_json_dropped_silently(self) -> None:
        cleaned, suggestions = vi_agent.extract_suggestion_blocks("Texto.\n```vera-suggestions\n[no json]\n```")
        self.assertEqual(cleaned, "Texto.")
        self.assertEqual(suggestions, [])

    def test_not_a_list_dropped(self) -> None:
        _, suggestions = vi_agent.extract_suggestion_blocks('```vera-suggestions\n{"a": 1}\n```')
        self.assertEqual(suggestions, [])

    def test_no_block_returns_text_unchanged(self) -> None:
        cleaned, suggestions = vi_agent.extract_suggestion_blocks("Sólo texto.")
        self.assertEqual(cleaned, "Sólo texto.")
        self.assertEqual(suggestions, [])


class EstimateCostTests(unittest.TestCase):
    """estimate_cost_usd (usage_tracking.py, 2026-09-09) — usado por el sidebar de consumo en
    vivo del tester (streamlit_app.py, a pedido explícito del usuario: "es justamente para
    testear y mostraría lo que cuesta cada cosa")."""

    def test_known_model_computes_expected_cost(self) -> None:
        from usage_tracking import estimate_cost_usd

        summary = {
            "prompt_token_count": 1_000_000,
            "candidates_token_count": 500_000,
            "thoughts_token_count": 500_000,
        }
        cost = estimate_cost_usd(summary, model="gemini-3.7-flash")
        # 1M de entrada a $0.75/M + 1M de salida (candidates+thoughts) a $3.75/M = $4.50
        self.assertAlmostEqual(cost, 4.50, places=6)

    def test_cached_tokens_are_discounted_not_ignored(self) -> None:
        from usage_tracking import estimate_cost_usd

        # Caso real (2026-09-11, ver "8. README.md" > "Prompt caching de Gemini"):
        # prompt_token_count YA incluye cached_content_token_count como subconjunto, no se suman
        # aparte -1M de prompt con 990k cacheados son sólo 10k de entrada "fresca" a precio lleno.
        summary = {
            "prompt_token_count": 1_000_000,
            "cached_content_token_count": 990_000,
            "candidates_token_count": 0,
            "thoughts_token_count": 0,
        }
        cost = estimate_cost_usd(summary, model="gemini-3.7-flash")
        # 10k frescos a $0.75/M + 990k cacheados a $0.75/M*0.10 = 0.0075 + 0.07425 = 0.08175
        # (fracción corregida 2026-09-17: 0.10, no 0.25 -ver CACHED_INPUT_PRICE_FRACTION)
        self.assertAlmostEqual(cost, 0.08175, places=6)

    def test_cached_tokens_never_exceed_prompt_tokens_in_the_estimate(self) -> None:
        from usage_tracking import estimate_cost_usd

        # Guardrail defensivo: un summary mal formado con cached > prompt no debe devolver un
        # costo negativo de "entrada fresca".
        summary = {"prompt_token_count": 100, "cached_content_token_count": 500}
        cost = estimate_cost_usd(summary, model="gemini-3.7-flash")
        self.assertGreaterEqual(cost, 0.0)

    def test_unknown_model_returns_none(self) -> None:
        from usage_tracking import estimate_cost_usd

        self.assertIsNone(estimate_cost_usd({"prompt_token_count": 100}, model="modelo-inexistente"))

    def test_zero_usage_is_zero_cost(self) -> None:
        from usage_tracking import estimate_cost_usd

        self.assertEqual(estimate_cost_usd({}, model="gemini-3.7-flash"), 0.0)

    def test_tool_use_prompt_tokens_are_counted_as_input(self) -> None:
        from usage_tracking import estimate_cost_usd

        # Regresión directa del bug real encontrado el 2026-09-14 midiendo el costo de RAG contra
        # farma24_alto: tool_use_prompt_token_count (contenido de File Search devuelto al modelo
        # como entrada) no se sumaba acá aunque usage_tracking.py ya lo registraba por evento -un
        # caso real llegó a 64.504 tokens en una sola pregunta, más del doble del prompt cacheado.
        summary = {
            "prompt_token_count": 0,
            "cached_content_token_count": 0,
            "tool_use_prompt_token_count": 1_000_000,
            "candidates_token_count": 0,
            "thoughts_token_count": 0,
        }
        cost = estimate_cost_usd(summary, model="gemini-3.7-flash")
        # 1M de tokens de RAG a precio de entrada lleno ($0.75/M), sin descuento de cache.
        self.assertAlmostEqual(cost, 0.75, places=6)

    def test_tool_use_prompt_tokens_add_to_fresh_input_not_replace_it(self) -> None:
        from usage_tracking import estimate_cost_usd

        summary = {
            "prompt_token_count": 500_000,
            "cached_content_token_count": 0,
            "tool_use_prompt_token_count": 500_000,
            "candidates_token_count": 0,
            "thoughts_token_count": 0,
        }
        cost = estimate_cost_usd(summary, model="gemini-3.7-flash")
        # 500k prompt fresco + 500k de tool_use = 1M de entrada a $0.75/M = $0.75, no $0.375.
        self.assertAlmostEqual(cost, 0.75, places=6)

    def test_embedding_model_has_no_output_cost(self) -> None:
        # gemini-embedding-001 (2026-09-22): sólo cobra entrada, sin tokens de salida facturables
        # -ver el comentario en MODEL_PRICING_PER_MILLION_TOKENS para la fuente del precio.
        from usage_tracking import estimate_cost_usd

        summary = {"prompt_token_count": 1_000_000, "candidates_token_count": 0, "thoughts_token_count": 0}
        cost = estimate_cost_usd(summary, model="gemini-embedding-001")
        self.assertAlmostEqual(cost, 0.15, places=6)


class RunToolLoopNumericEvidenceDebugTests(unittest.TestCase):
    """El diagnóstico interno sigue oculto; las cifras inválidas ya se bloquean en producción."""

    def _run_with_captured_stderr(self, chat, question, **kwargs) -> tuple[str, str]:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            answer = vi_agent.run_tool_loop(chat, question, debug=True, **kwargs)
        return answer, buffer.getvalue()

    def test_flags_a_number_not_present_in_any_sql_result(self) -> None:
        # MAX_EVIDENCE_REPAIRS=2 (subido de 1, 2026-09-18): intento inicial + 2 reintentos inválidos.
        chat = FakeChat(
            [
                FakeResponse(
                    "", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]
                ),
                FakeResponse("La tasa de cierre fue del 999%."),
                FakeResponse("La tasa de cierre fue del 999%."),
                FakeResponse("La tasa de cierre fue del 999%."),
            ]
        )
        fake_sql = MagicMock(return_value='{"rows": [{"total": 500, "cerradas": 100}]}')
        with patch.object(vi_agent, "run_readonly_sql", fake_sql), patch.dict(
            vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": fake_sql}
        ):
            answer, stderr_output = self._run_with_captured_stderr(chat, "¿Cuál es la tasa?")
        self.assertEqual(answer, vi_agent.UNVERIFIED_ANSWER_FALLBACK)
        self.assertIn("validación de evidencia", stderr_output)
        self.assertIn("999", stderr_output)

    def test_no_warning_when_every_number_is_backed(self) -> None:
        chat = FakeChat(
            [
                FakeResponse(
                    "", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]
                ),
                FakeResponse("Se registraron 247.556 conversaciones."),
            ]
        )
        fake_sql = MagicMock(return_value='{"rows": [{"total": 247556}]}')
        with patch.object(vi_agent, "run_readonly_sql", fake_sql), patch.dict(
            vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": fake_sql}
        ):
            answer, stderr_output = self._run_with_captured_stderr(chat, "¿Cuántas conversaciones?")
        self.assertEqual(answer, "Se registraron 247.556 conversaciones.")
        self.assertNotIn("números sin respaldo directo", stderr_output)

    def test_never_printed_without_debug(self) -> None:
        # MAX_EVIDENCE_REPAIRS=2 (subido de 1, 2026-09-18): intento inicial + 2 reintentos inválidos.
        chat = FakeChat(
            [
                FakeResponse(
                    "", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]
                ),
                FakeResponse("La tasa de cierre fue del 999%."),
                FakeResponse("La tasa de cierre fue del 999%."),
                FakeResponse("La tasa de cierre fue del 999%."),
            ]
        )
        import contextlib
        import io

        buffer = io.StringIO()
        fake_sql = MagicMock(return_value='{"rows": [{"total": 500}]}')
        with patch.object(vi_agent, "run_readonly_sql", fake_sql), patch.dict(
            vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": fake_sql}
        ), contextlib.redirect_stderr(buffer):
            vi_agent.run_tool_loop(chat, "¿Cuál es la tasa?")  # debug=False (default)
        self.assertNotIn("números sin respaldo directo", buffer.getvalue())


class InteractionOutcomeLoggingTests(unittest.TestCase):
    """interaction_outcomes.jsonl (2026-09-15, ver "6. busqueda_vectorial/README.md" >
    "Iteración 26") -evidencia de confiabilidad por interacción completa, no por llamada individual
    a Gemini (eso ya lo cubre gemini_calls.jsonl vía UsageRecorder). Motivado por "medí los
    reintentos": antes de esto, agotar MAX_CLIENT_REWRITES/MAX_EVIDENCE_REPAIRS y devolver el
    fallback genérico no dejaba ningún rastro fuera de `debug`."""

    def _load_events(self, path) -> list[dict]:
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_successful_answer_logs_outcome_without_unbacked_numbers(self) -> None:
        chat = FakeChat(
            [
                FakeResponse(
                    "", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]
                ),
                FakeResponse("Se registraron 247.556 conversaciones."),
            ]
        )
        fake_sql = MagicMock(return_value='{"rows": [{"total": 247556}]}')
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            vi_agent, "run_readonly_sql", fake_sql
        ), patch.dict(vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": fake_sql}):
            path = Path(temp_dir) / "interaction_outcomes.jsonl"
            answer = vi_agent.run_tool_loop(
                chat,
                "¿Cuántas conversaciones?",
                interaction_outcome_recorder=vi_agent.InteractionOutcomeRecorder(path),
            )
            events = self._load_events(path)
        self.assertEqual(answer, "Se registraron 247.556 conversaciones.")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "answered")
        self.assertNotIn("unbacked_numbers", events[0])

    def test_successful_answer_with_an_unbacked_number_is_logged(self) -> None:
        # unbacked_answer_numbers() en sí ya tiene su propia batería exhaustiva (ver
        # UnbackedAnswerNumbersTests más arriba) -acá sólo se verifica el CABLEADO: que su
        # resultado realmente llegue al log cuando la respuesta pasa verify_answer (evidencia
        # válida) sin errores. Mockeada para no depender de construir un número que sea "lo
        # suficientemente sospechoso para unbacked_answer_numbers pero no tanto como para que
        # verify_answer lo rechace antes" -esa franja exacta es frágil y no es lo que se prueba acá.
        chat = FakeChat(
            [
                FakeResponse(
                    "", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]
                ),
                FakeResponse("Se registraron 247.556 conversaciones."),
            ]
        )
        fake_sql = MagicMock(return_value='{"rows": [{"total": 247556}]}')
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            vi_agent, "run_readonly_sql", fake_sql
        ), patch.dict(vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": fake_sql}), patch.object(
            vi_agent, "unbacked_answer_numbers", return_value={"999"}
        ):
            path = Path(temp_dir) / "interaction_outcomes.jsonl"
            vi_agent.run_tool_loop(
                chat,
                "¿Cuántas conversaciones?",
                interaction_outcome_recorder=vi_agent.InteractionOutcomeRecorder(path),
            )
            events = self._load_events(path)
        self.assertEqual(events[0]["outcome"], "answered")
        self.assertEqual(events[0]["unbacked_numbers"], ["999"])

    def test_unsafe_fallback_is_logged_when_rewrites_are_exhausted(self) -> None:
        chat = FakeChat(["SQL resultado_total"] * (vi_agent.MAX_CLIENT_REWRITES + 1))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "interaction_outcomes.jsonl"
            answer = vi_agent.run_tool_loop(
                chat,
                "¿Cuál es la tasa de compra total?",
                interaction_outcome_recorder=vi_agent.InteractionOutcomeRecorder(path),
            )
            events = self._load_events(path)
        self.assertEqual(answer, vi_agent.UNSAFE_ANSWER_FALLBACK)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "unsafe_fallback")
        self.assertEqual(events[0]["rewrite_count"], vi_agent.MAX_CLIENT_REWRITES)
        self.assertIn("resultado_total", events[0]["offending_terms"])

    def test_unverified_fallback_is_logged_when_repairs_are_exhausted(self) -> None:
        # MAX_EVIDENCE_REPAIRS=2 (subido de 1, 2026-09-18): intento inicial + 2 reintentos inválidos.
        chat = FakeChat(
            [
                FakeResponse(
                    "", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]
                ),
                FakeResponse("La tasa de cierre fue del 999%."),
                FakeResponse("La tasa de cierre fue del 999%."),
                FakeResponse("La tasa de cierre fue del 999%."),
            ]
        )
        fake_sql = MagicMock(return_value='{"rows": [{"total": 500, "cerradas": 100}]}')
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            vi_agent, "run_readonly_sql", fake_sql
        ), patch.dict(vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": fake_sql}):
            path = Path(temp_dir) / "interaction_outcomes.jsonl"
            answer = vi_agent.run_tool_loop(
                chat,
                "¿Cuál es la tasa?",
                interaction_outcome_recorder=vi_agent.InteractionOutcomeRecorder(path),
            )
            events = self._load_events(path)
        self.assertEqual(answer, vi_agent.UNVERIFIED_ANSWER_FALLBACK)
        self.assertEqual(events[0]["outcome"], "unverified_fallback")
        self.assertIn("verification_errors", events[0])


class ReadonlySqlConnectionRecoveryTests(unittest.TestCase):
    """run_readonly_sql -bug real encontrado en vivo (2026-09-14, mientras se probaba que
    get_business_rules + run_readonly_sql se pidan en el mismo turno): un error de SQL (columna
    inexistente, sintaxis, etc.) dentro de una transacción deja la conexión Postgres reusada
    "abortada" del lado del servidor -sin un rollback explícito, CUALQUIER consulta siguiente en
    esa misma conexión falla con "current transaction is aborted", aunque sea una consulta válida.
    Confirmado en vivo contra mens_fashion_alto: una columna mal escrita hizo fallar 3 reintentos
    seguidos del modelo con ese mismo mensaje genérico, en vez de dejarlo corregir la consulta."""

    # Fuente real declarada en el Data Map de mens_fashion_alto (cliente default de este módulo de
    # tests) -_validate_sql corre de verdad acá (a diferencia de otros tests de este archivo que
    # mockean TOOL_FUNCTIONS entero y nunca llegan a validar), así que el SQL de prueba tiene que
    # referenciar una fuente autorizada de verdad o `sql_security.py` lo rechaza antes de llegar a
    # la conexión/cursor que estos tests quieren ejercitar.
    _VALID_SOURCE = "dashboard_v2.vw_mens_fashion_demografia"
    _VALID_WHERE = "WHERE seller_id = 'Mens Fashion'"

    def setUp(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")
        vi_agent._cached_sql_connection = None

    def tearDown(self) -> None:
        vi_agent._cached_sql_connection = None

    def _fake_connection_with_failing_cursor(self, exc: Exception) -> MagicMock:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.execute.side_effect = exc
        connection = MagicMock()
        connection.closed = False
        connection.cursor.return_value = cursor
        return connection

    def test_sql_error_rolls_back_the_connection_instead_of_leaving_it_poisoned(self) -> None:
        fake_connection = self._fake_connection_with_failing_cursor(
            RuntimeError("column x does not exist")
        )
        with patch.object(vi_agent, "_get_reusable_sql_connection", return_value=fake_connection):
            with self.assertRaises(RuntimeError):
                vi_agent.run_readonly_sql(f"SELECT x FROM {self._VALID_SOURCE} {self._VALID_WHERE}")
        fake_connection.rollback.assert_called_once()

    def test_rollback_is_skipped_if_the_connection_already_closed(self) -> None:
        # Guardrail defensivo: si la conexión se cerró por otro motivo mientras tanto, no debe
        # intentarse un rollback() sobre una conexión cerrada (tiraría su propio error, tapando el
        # error real de SQL que el modelo necesita ver).
        fake_connection = self._fake_connection_with_failing_cursor(
            RuntimeError("column x does not exist")
        )
        fake_connection.closed = True
        with patch.object(vi_agent, "_get_reusable_sql_connection", return_value=fake_connection):
            with self.assertRaises(RuntimeError):
                vi_agent.run_readonly_sql(f"SELECT x FROM {self._VALID_SOURCE} {self._VALID_WHERE}")
        fake_connection.rollback.assert_not_called()

    def test_operational_error_still_reconnects_instead_of_rolling_back(self) -> None:
        # Guardrail de no-regresión: OperationalError (conexión rota, ej. idle timeout) sigue
        # yendo por la rama de reconexión existente, no por el rollback nuevo -son dos fallas
        # distintas con dos recuperaciones distintas.
        broken_connection = self._fake_connection_with_failing_cursor(
            vi_agent.psycopg.OperationalError("conexión caída")
        )
        good_cursor = MagicMock()
        good_cursor.__enter__.return_value = good_cursor
        good_cursor.__exit__.return_value = False
        good_cursor.description = []
        good_cursor.fetchmany.return_value = []
        good_connection = MagicMock()
        good_connection.closed = False
        good_connection.cursor.return_value = good_cursor

        connections = iter([broken_connection, good_connection])
        with patch.object(
            vi_agent, "_get_reusable_sql_connection", side_effect=lambda: next(connections)
        ):
            result = vi_agent.run_readonly_sql(
                f"SELECT 1 FROM {self._VALID_SOURCE} {self._VALID_WHERE}"
            )
        self.assertEqual(json.loads(result), {"row_count": 0, "truncated": False, "columns": [], "rows": []})
        broken_connection.rollback.assert_not_called()


class VectorSearchToolExposureTests(unittest.TestCase):
    """search_conversations (2026-09-10, ver "6. busqueda_vectorial/README.md") sólo debe
    anunciarse al modelo -y sólo debe poder resolverse en el loop manual- para un cliente cuyo
    config.yaml declara vector_search. mens_fashion_alto es hoy el único piloto; farma24_alto no
    lo declara, y sirve de control negativo para no exponer la tool a todos por accidente."""

    def tearDown(self) -> None:
        # Todos los demás tests del módulo asumen el cliente default (mens_fashion_alto) cargado
        # al importar vi_agent -restaurarlo evita que el orden de ejecución de tests filtre
        # estado entre clases.
        vi_agent.configure_client("mens_fashion_alto")

    def test_pilot_client_exposes_the_tool(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")
        self.assertIsNotNone(vi_agent._VECTOR_SEARCH_REPOSITORY)
        tool_names = {getattr(tool, "__name__", None) for tool in vi_agent._build_tools_list()}
        self.assertIn("search_conversations", tool_names)
        self.assertIn("search_conversations", vi_agent._build_extra_tools_section())

    def test_client_without_vector_search_does_not_expose_the_tool(self) -> None:
        # Ampliación a (casi) todos los clientes (2026-09-16): salomon_alto pasó a tener
        # vector_search habilitado -ver "8. README.md". agrosuper_bajo sigue sin el bloque a
        # propósito (13 embeddings verificados, volumen insuficiente), control válido acá.
        vi_agent.configure_client("agrosuper_bajo")
        self.assertIsNone(vi_agent._VECTOR_SEARCH_REPOSITORY)
        tool_names = {getattr(tool, "__name__", None) for tool in vi_agent._build_tools_list()}
        self.assertNotIn("search_conversations", tool_names)
        self.assertNotIn("search_conversations", vi_agent._build_extra_tools_section())

    def test_calling_the_tool_without_configuration_raises(self) -> None:
        vi_agent.configure_client("agrosuper_bajo")
        with self.assertRaises(RuntimeError):
            vi_agent.search_conversations("cualquier consulta")

    def test_search_conversations_no_longer_exposes_incluir_fragmentos_to_the_model(self) -> None:
        # Pedido explícito (2026-09-22): nunca mostrarle al usuario una cita textual, ni siquiera
        # si la pide -el modelo ya no puede pedir el texto crudo de la conversación en absoluto.
        params = inspect.signature(vi_agent.search_conversations).parameters
        self.assertNotIn("incluir_fragmentos", params)

    def test_search_conversations_always_calls_the_repository_with_fragments_disabled(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")
        fake_repo = MagicMock()
        fake_repo.search.return_value = "{}"
        with patch.object(vi_agent, "_VECTOR_SEARCH_REPOSITORY", fake_repo):
            vi_agent.search_conversations("cualquier consulta")
        self.assertEqual(fake_repo.search.call_args.kwargs["incluir_fragmentos"], False)


class GeminiCachingTests(unittest.TestCase):
    """Prompt caching de Gemini (2026-09-11) -ver "8. README.md" > "Potencial de mejora". Nunca
    llama a la API real: client.caches.create se mockea siempre. El objetivo de estos tests no es
    probar el SDK de Gemini, es probar que build_chat() nunca se rompe si el caching falla o no
    aplica, y que reusa un cache guardado en vez de crear uno nuevo en cada build_chat()."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._cache_dir_patch = patch.object(
            vi_agent, "GEMINI_CACHE_DIR", Path(self._tmpdir.name)
        )
        self._cache_dir_patch.start()
        self.addCleanup(self._cache_dir_patch.stop)
        # build_chat() reusa un genai.Client cacheado entre llamadas (2026-09-11, ver
        # _get_reusable_genai_client) -sin resetear acá, el fake_client mockeado de un test
        # anterior con la misma api_key ("test-key") quedaría pegado para el siguiente test en
        # vez de que build_chat() llame a genai.Client de nuevo (y por lo tanto ignore el
        # patch.object(vi_agent.genai, "Client", ...) de ESTE test). Mismo criterio que
        # EmbedQueryRetryTests en test_vector_search.py para _cached_embed_client.
        vi_agent._cached_genai_client = None
        vi_agent._cached_genai_client_api_key = None
        self.addCleanup(setattr, vi_agent, "_cached_genai_client", None)
        self.addCleanup(setattr, vi_agent, "_cached_genai_client_api_key", None)

    def test_fingerprint_stable_for_same_content_different_for_different_content(self) -> None:
        tools = [vi_agent.run_readonly_sql]
        fp1 = vi_agent._content_fingerprint("instruccion A", tools)
        fp2 = vi_agent._content_fingerprint("instruccion A", tools)
        fp3 = vi_agent._content_fingerprint("instruccion B", tools)
        fp4 = vi_agent._content_fingerprint("instruccion A", [])
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)
        self.assertNotEqual(fp1, fp4)

    def test_no_metadata_file_returns_none(self) -> None:
        self.assertIsNone(vi_agent._load_cached_content_name("cliente_x", "fp"))

    def test_changing_tool_description_invalidates_existing_content_cache(self) -> None:
        import time as _time

        def query(sql: str) -> str:
            """Devuelve filas como objetos."""
            return sql

        old_fp = vi_agent._content_fingerprint("instruccion", [query])
        vi_agent._save_cached_content_metadata(
            "cliente_x", old_fp, name="caches/anterior", expire_at_epoch=_time.time() + 3600
        )
        query.__doc__ = "Devuelve columns y filas como listas de valores."
        new_fp = vi_agent._content_fingerprint("instruccion", [query])
        self.assertNotEqual(old_fp, new_fp)
        self.assertIsNone(vi_agent._load_cached_content_name("cliente_x", new_fp))

    def test_expired_cache_returns_none(self) -> None:
        import time as _time

        vi_agent._save_cached_content_metadata(
            "cliente_x", "fp", name="caches/abc", expire_at_epoch=_time.time() - 10
        )
        self.assertIsNone(vi_agent._load_cached_content_name("cliente_x", "fp"))

    def test_different_fingerprint_returns_none(self) -> None:
        import time as _time

        vi_agent._save_cached_content_metadata(
            "cliente_x", "fp_vieja", name="caches/abc", expire_at_epoch=_time.time() + 3600
        )
        self.assertIsNone(vi_agent._load_cached_content_name("cliente_x", "fp_nueva"))

    def test_valid_cache_is_reused(self) -> None:
        import time as _time

        vi_agent._save_cached_content_metadata(
            "cliente_x", "fp", name="caches/abc", expire_at_epoch=_time.time() + 3600
        )
        self.assertEqual(vi_agent._load_cached_content_name("cliente_x", "fp"), "caches/abc")

    def test_unsupported_marker_is_reused_without_retrying(self) -> None:
        vi_agent._save_cached_content_metadata("cliente_x", "fp", unsupported=True)
        self.assertEqual(vi_agent._load_cached_content_name("cliente_x", "fp"), "unsupported")

    def test_get_or_create_reuses_existing_valid_cache_without_calling_the_api(self) -> None:
        import time as _time

        vi_agent._save_cached_content_metadata(
            "cliente_x", vi_agent._content_fingerprint("si", []), name="caches/abc",
            expire_at_epoch=_time.time() + 3600,
        )
        fake_client = SimpleNamespace(caches=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(
            AssertionError("no debería llamar a caches.create si ya hay un cache válido")
        )))
        result = vi_agent._get_or_create_cached_content(fake_client, "cliente_x", "si", [])
        self.assertEqual(result, "caches/abc")

    def test_get_or_create_creates_and_persists_a_new_cache(self) -> None:
        created = SimpleNamespace(name="caches/nuevo")
        fake_client = SimpleNamespace(caches=SimpleNamespace(create=lambda **_: created))
        # tools=[] -> _cacheable_tools no necesita tocar client.models._api_client (ver test de
        # _cacheable_tools aparte para el camino con funciones Python reales).
        result = vi_agent._get_or_create_cached_content(fake_client, "cliente_y", "si", [])
        self.assertEqual(result, "caches/nuevo")
        # Segunda llamada con el mismo contenido: reusa sin volver a crear.
        fake_client_2 = SimpleNamespace(
            caches=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(
                AssertionError("debería haber reusado el cache recién creado")
            ))
        )
        result_2 = vi_agent._get_or_create_cached_content(fake_client_2, "cliente_y", "si", [])
        self.assertEqual(result_2, "caches/nuevo")

    def test_get_or_create_forwards_tool_config_into_the_cache(self) -> None:
        # Regresión de un bug real encontrado por un revisor externo (2026-09-11): tool_config
        # nunca se pasaba acá -un cliente con RAG (tool nativo server-side) quedaba sin
        # include_server_side_tool_invocations en el cache, y CADA llamada fallaba en producción
        # con 400 INVALID_ARGUMENT en cuanto el caching entraba en juego (farma24_alto,
        # maga_alto). tool_config tiene que viajar DENTRO de CreateCachedContentConfig, igual que
        # tools -ver docstring de _get_or_create_cached_content.
        captured = {}

        def _create(**kwargs):
            captured["config"] = kwargs["config"]
            return SimpleNamespace(name="caches/con-tool-config")

        fake_client = SimpleNamespace(caches=SimpleNamespace(create=_create))
        tool_config = vi_agent.types.ToolConfig(include_server_side_tool_invocations=True)
        result = vi_agent._get_or_create_cached_content(
            fake_client, "cliente_rag", "si", [], tool_config
        )
        self.assertEqual(result, "caches/con-tool-config")
        self.assertEqual(captured["config"].tool_config, tool_config)

    def test_get_or_create_returns_none_and_marks_unsupported_on_api_error(self) -> None:
        def _raise(**_):
            raise vi_agent.errors.ClientError(400, {"error": {"message": "too few tokens"}})

        fake_client = SimpleNamespace(caches=SimpleNamespace(create=_raise))
        result = vi_agent._get_or_create_cached_content(fake_client, "cliente_z", "si", [])
        self.assertIsNone(result)
        self.assertEqual(
            vi_agent._load_cached_content_name(
                "cliente_z", vi_agent._content_fingerprint("si", [])
            ),
            "unsupported",
        )
        # Un segundo intento con el mismo contenido no vuelve a llamar a la API.
        fake_client_2 = SimpleNamespace(caches=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(
            AssertionError("no debería reintentar un fingerprint marcado unsupported")
        )))
        result_2 = vi_agent._get_or_create_cached_content(fake_client_2, "cliente_z", "si", [])
        self.assertIsNone(result_2)

    def test_cacheable_tools_converts_plain_functions_and_keeps_native_tool_objects(self) -> None:
        # Caso real que rompía antes del fix: pasar funciones Python planas tal cual a
        # CreateCachedContentConfig -pydantic.ValidationError ("Input should be a valid
        # dictionary or object to extract fields from"). _cacheable_tools tiene que convertirlas.
        real_client = genai.Client(api_key="test-key-no-network-call-yet")
        native_tool = vi_agent.types.Tool(
            file_search=vi_agent.types.FileSearch(file_search_store_names=["fake-store"])
        )
        result = vi_agent._cacheable_tools(
            real_client, [vi_agent.run_readonly_sql, vi_agent.get_business_rules, native_tool]
        )
        # Las dos funciones se agrupan en un único Tool con dos function_declarations.
        function_tools = [t for t in result if t.function_declarations]
        self.assertEqual(len(function_tools), 1)
        self.assertEqual(len(function_tools[0].function_declarations), 2)
        declared_names = {d.name for d in function_tools[0].function_declarations}
        self.assertEqual(declared_names, {"run_readonly_sql", "get_business_rules"})
        # El tool nativo (RAG) se conserva tal cual, sin intentar convertirlo.
        self.assertIn(native_tool, result)

    @staticmethod
    def _make_fake_client(*, caches_create):
        """Envoltorio con `.models` de un genai.Client real (necesario: _cacheable_tools usa
        client.models._api_client para convertir funciones Python a declaraciones, no puede
        mockearse sin perder esa conversión real) -`.caches`/`.chats` son propiedades de sólo
        lectura en el SDK, no se pueden pisar en la instancia real, así que se envuelve en vez de
        monkeypatchear. Nunca pega a la red: caches.create/chats.create son los únicos puntos de
        entrada y quedan controlados por el test."""
        real_client = genai.Client(api_key="test-key")
        captured_config = {}

        class _FakeChats:
            def create(self, *, model, config):
                captured_config["config"] = config
                return "fake-chat"

        fake_client = SimpleNamespace(
            models=real_client.models,
            caches=SimpleNamespace(create=caches_create),
            chats=_FakeChats(),
        )
        return fake_client, captured_config

    def test_reusable_client_sets_an_explicit_http_timeout(self) -> None:
        # 2026-09-16: regresión de un cuelgue real encontrado en vivo -HttpOptions.timeout no se
        # configuraba en ningún lado, así que su default (None) se traducía a "sin timeout" en el
        # httpx.Client subyacente. Un cuelgue de red silencioso (sin excepción) dejaba la llamada
        # esperando para siempre, sin que la lógica de reintentos pudiera reaccionar. Ver
        # _GENAI_HTTP_TIMEOUT_MS.
        vi_agent.configure_client("salomon_alto")
        fake_client, _ = self._make_fake_client(
            caches_create=lambda **_: SimpleNamespace(name="caches/timeout-test")
        )
        with patch.object(
            vi_agent.genai, "Client", return_value=fake_client
        ) as client_ctor, patch.dict(os.environ, {"VERA_AI_API_KEY": "test-key"}):
            vi_agent.build_chat(system_instruction="instruccion corta de prueba")
        _, kwargs = client_ctor.call_args
        self.assertIn("http_options", kwargs)
        self.assertEqual(kwargs["http_options"].timeout, vi_agent._GENAI_HTTP_TIMEOUT_MS)

    def test_build_chat_falls_back_to_direct_system_instruction_when_caching_unavailable(
        self,
    ) -> None:
        vi_agent.configure_client("salomon_alto")
        fake_client, captured_config = self._make_fake_client(
            caches_create=lambda **_: (_ for _ in ()).throw(
                vi_agent.errors.ClientError(400, {"error": {"message": "too small"}})
            )
        )

        with patch.object(vi_agent.genai, "Client", return_value=fake_client), patch.dict(
            os.environ, {"VERA_AI_API_KEY": "test-key"}
        ):
            chat = vi_agent.build_chat(system_instruction="instruccion corta de prueba")
        self.assertEqual(chat, "fake-chat")
        config = captured_config["config"]
        self.assertIsNone(config.cached_content)
        self.assertEqual(config.system_instruction, "instruccion corta de prueba")
        self.assertTrue(config.tools)

    def test_build_chat_uses_cached_content_when_available(self) -> None:
        vi_agent.configure_client("salomon_alto")
        fake_client, captured_config = self._make_fake_client(
            caches_create=lambda **_: SimpleNamespace(name="caches/nuevo-build-chat")
        )

        with patch.object(vi_agent.genai, "Client", return_value=fake_client), patch.dict(
            os.environ, {"VERA_AI_API_KEY": "test-key"}
        ):
            vi_agent.build_chat(system_instruction="instruccion corta de prueba")
        config = captured_config["config"]
        self.assertEqual(config.cached_content, "caches/nuevo-build-chat")
        # system_instruction/tools/tool_config NO van sueltos cuando hay cached_content -Gemini
        # rechaza la combinación (ver _cacheable_tools); todo lo que hacía falta ya está adentro
        # del cache.
        self.assertIsNone(config.system_instruction)
        self.assertIsNone(config.tools)
        self.assertIsNone(config.tool_config)

    def test_build_chat_applies_default_temperature(self) -> None:
        # Consistencia numérica entre corridas idénticas (2026-09-14, pedido explícito del
        # usuario) -antes nunca se fijaba, corría con el default de la API de Gemini.
        vi_agent.configure_client("salomon_alto")
        fake_client, captured_config = self._make_fake_client(
            caches_create=lambda **_: SimpleNamespace(name="caches/temp-test")
        )
        with patch.object(vi_agent.genai, "Client", return_value=fake_client), patch.dict(
            os.environ, {"VERA_AI_API_KEY": "test-key"}
        ):
            vi_agent.build_chat(system_instruction="instruccion corta de prueba")
        self.assertEqual(captured_config["config"].temperature, vi_agent.DEFAULT_TEMPERATURE)

    def test_build_chat_temperature_override(self) -> None:
        vi_agent.configure_client("salomon_alto")
        fake_client, captured_config = self._make_fake_client(
            caches_create=lambda **_: SimpleNamespace(name="caches/temp-test-2")
        )
        with patch.object(vi_agent.genai, "Client", return_value=fake_client), patch.dict(
            os.environ, {"VERA_AI_API_KEY": "test-key"}
        ):
            vi_agent.build_chat(system_instruction="instruccion corta de prueba", temperature=None)
        self.assertIsNone(captured_config["config"].temperature)


if __name__ == "__main__":
    unittest.main()
