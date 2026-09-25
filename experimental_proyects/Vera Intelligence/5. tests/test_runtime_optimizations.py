from __future__ import annotations

import sys
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vi_agent
import vector_search
from runtime_control import (
    AnalysisCancelled, AnalysisControl, OperationalUnavailable, analysis_scope,
    check_analysis, current_analysis_control,
)


class Chat:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.messages = []
        self.history = []

    def send_message(self, message):
        self.messages.append(message)
        response = next(self.responses)
        return response if not isinstance(response, str) else SimpleNamespace(
            text=response, function_calls=[], usage_metadata=None
        )

    def get_history(self, curated=False):
        return list(self.history)

    def record_history(self, user_input, model_output, is_valid):
        assert is_valid
        self.history.extend([user_input, *model_output])


def sql_call():
    return SimpleNamespace(text="", usage_metadata=None, function_calls=[
        SimpleNamespace(name="run_readonly_sql", args={"sql": "SELECT ..."})
    ])


class ControlTests(unittest.TestCase):
    def test_cancelled_control_raises_before_work(self):
        control = AnalysisControl()
        control.cancel()
        with self.assertRaises(AnalysisCancelled):
            with analysis_scope(control):
                self.fail("No debe iniciar trabajo.")

    def test_disconnected_session_is_cancelled(self):
        with self.assertRaises(AnalysisCancelled):
            AnalysisControl(lambda: True).check()

    def test_context_is_restored_after_failure(self):
        outer = AnalysisControl()
        inner = AnalysisControl()
        with analysis_scope(outer):
            with self.assertRaises(AnalysisCancelled):
                with analysis_scope(inner):
                    inner.cancel()
                    check_analysis()
            self.assertIs(current_analysis_control(), outer)
            check_analysis()
        self.assertIsNone(current_analysis_control())

    def test_cancel_interrupts_retry_wait(self):
        control = AnalysisControl()
        started = threading.Event()
        stopped = threading.Event()

        def waiting():
            started.set()
            try:
                control.wait(30)
            except AnalysisCancelled:
                stopped.set()

        worker = threading.Thread(target=waiting)
        worker.start()
        self.assertTrue(started.wait(2))
        control.cancel()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(stopped.is_set())


class RuntimeAgentTests(unittest.TestCase):
    def setUp(self):
        vi_agent.configure_client("mens_fashion_alto")

    def test_courtesy_uses_no_model_and_keeps_history(self):
        for question in ("gracias", "¡Muchas gracias!", "GRACIAS VERA", "gracias por el análisis."):
            with self.subTest(question=question):
                chat = Chat()
                previous = SimpleNamespace(role="model", parts=[])
                chat.history.append(previous)
                answer = vi_agent.run_tool_loop(chat, question)
                self.assertIn("De nada", answer)
                self.assertEqual(chat.messages, [])
                self.assertIs(chat.history[0], previous)
                self.assertEqual(chat.history[-2].parts[0].text, question)
                self.assertEqual(chat.history[-1].parts[0].text, answer)

    def test_mixed_courtesy_and_confirmations_still_use_the_model(self):
        for question in ("Gracias, ¿cuántas ventas hubo?", "gracias y compará agosto", "dale", "sí", "perfecto"):
            with self.subTest(question=question):
                chat = Chat(["Puedo ayudarte con el análisis."])
                self.assertEqual(vi_agent.run_tool_loop(chat, question), "Puedo ayudarte con el análisis.")
                self.assertEqual(len(chat.messages), 1)

    def test_cancel_before_first_model_call(self):
        control = AnalysisControl()
        control.cancel()
        chat = Chat()
        with self.assertRaises(AnalysisCancelled):
            vi_agent.run_tool_loop(chat, "Compará las ventas", analysis_control=control)
        self.assertEqual(chat.messages, [])

    def test_cancel_after_model_response_prevents_tools(self):
        control = AnalysisControl()
        chat = Chat()

        def respond(_message):
            control.cancel()
            return sql_call()

        chat.send_message = MagicMock(side_effect=respond)
        tool = MagicMock()
        with patch.dict(vi_agent.TOOL_FUNCTIONS, run_readonly_sql=tool):
            with self.assertRaises(AnalysisCancelled):
                vi_agent.run_tool_loop(chat, "Compará las ventas", analysis_control=control)
        tool.assert_not_called()
        chat.send_message.assert_called_once()

    def test_access_failure_after_cancellation_is_discarded_as_cancelled(self):
        control = AnalysisControl()
        chat = Chat()

        def fail(_message):
            control.cancel()
            raise vi_agent.errors.ClientError(403, {"error": {"message": "secret"}})

        chat.send_message = MagicMock(side_effect=fail)
        with self.assertRaises(AnalysisCancelled):
            vi_agent.run_tool_loop(chat, "Compará las ventas", analysis_control=control)
        chat.send_message.assert_called_once()

    def test_cancel_during_tool_prevents_next_model_turn(self):
        control = AnalysisControl()
        chat = Chat([sql_call()])

        def tool(sql):
            control.cancel()
            return '{"columns":["ventas"],"rows":[[100]]}'

        with patch.dict(vi_agent.TOOL_FUNCTIONS, run_readonly_sql=tool):
            with self.assertRaises(AnalysisCancelled):
                vi_agent.run_tool_loop(chat, "Compará las ventas", analysis_control=control)
        self.assertEqual(len(chat.messages), 1)

    def test_parallel_workers_receive_the_same_control(self):
        control = AnalysisControl()
        response = sql_call()
        response.function_calls.append(SimpleNamespace(name="get_business_rules", args={"rulebook": "coaching_playbook"}))
        chat = Chat([response, "Hay una oportunidad de mejora."])
        seen = []

        def tool(**kwargs):
            seen.append(current_analysis_control())
            return "{}"

        with patch.dict(vi_agent.TOOL_FUNCTIONS, run_readonly_sql=tool, get_business_rules=tool):
            vi_agent.run_tool_loop(chat, "Analizá las ventas", analysis_control=control)
        self.assertEqual(seen, [control, control])

    def test_cancel_stream_closes_iterator_and_does_not_deliver_late_text(self):
        control = AnalysisControl()
        closed = []
        delivered = []
        chat = Chat()

        def stream(_message):
            try:
                yield SimpleNamespace(text="Primera línea.\n", function_calls=[], usage_metadata=None)
                yield SimpleNamespace(text="Respuesta tardía.\n", function_calls=[], usage_metadata=None)
            finally:
                closed.append(True)

        chat.send_message_stream = MagicMock(side_effect=stream)

        def receive(delta):
            delivered.append(delta)
            control.cancel()

        with self.assertRaises(AnalysisCancelled):
            vi_agent.run_tool_loop(chat, "Compará las ventas", analysis_control=control, on_text_delta=receive)
        self.assertEqual(closed, [True])
        self.assertNotIn("Respuesta tardía", "".join(delivered))
        chat.send_message_stream.assert_called_once()

    def test_fatal_tool_errors_do_not_trigger_another_model_call(self):
        for error in (
            OperationalUnavailable(), vi_agent.psycopg.errors.InvalidPassword("secret"),
            vi_agent.psycopg.errors.ConnectionFailure("host"),
            vi_agent.psycopg.errors.InsufficientPrivilege("view"),
        ):
            with self.subTest(error=type(error).__name__):
                chat = Chat([sql_call()])
                logs = []
                with patch.dict(vi_agent.TOOL_FUNCTIONS, run_readonly_sql=MagicMock(side_effect=error)):
                    with self.assertRaises(OperationalUnavailable) as caught:
                        vi_agent.run_tool_loop(chat, "Compará las ventas", tool_calls_log=logs)
                self.assertEqual(len(chat.messages), 1)
                self.assertEqual(len(logs), 1)
                self.assertNotIn("secret", caught.exception.user_message)

    def test_correctable_sql_errors_still_reach_the_model(self):
        for cls in (
            vi_agent.psycopg.errors.UndefinedColumn, vi_agent.psycopg.errors.UndefinedTable,
            vi_agent.psycopg.errors.QueryCanceled, vi_agent.psycopg.errors.SerializationFailure,
            vi_agent.psycopg.errors.DeadlockDetected,
        ):
            with self.subTest(error=cls.__name__):
                chat = Chat([sql_call(), "Puedo ayudarte con el análisis."])
                with patch.dict(vi_agent.TOOL_FUNCTIONS, run_readonly_sql=MagicMock(side_effect=cls("corregible"))):
                    self.assertEqual(vi_agent.run_tool_loop(chat, "Compará las ventas"), "Puedo ayudarte con el análisis.")
                self.assertEqual(len(chat.messages), 2)
                self.assertEqual(chat.messages[1][0].function_response.response, {"error": "corregible"})

    def test_model_authentication_error_is_terminal(self):
        chat = Chat()
        chat.send_message = MagicMock(side_effect=vi_agent.errors.ClientError(403, {"error": {"message": "secret"}}))
        with self.assertRaises(OperationalUnavailable) as caught:
            vi_agent.run_tool_loop(chat, "Compará las ventas")
        chat.send_message.assert_called_once()
        self.assertNotIn("secret", str(caught.exception))

    def test_transient_model_error_keeps_retries_but_stops_after_exhaustion(self):
        chat = Chat()
        chat.send_message = MagicMock(side_effect=vi_agent.errors.ServerError(503, {"error": {"message": "offline"}}))
        with patch.object(vi_agent, "wait_before_retry") as wait:
            with self.assertRaises(OperationalUnavailable):
                vi_agent.run_tool_loop(chat, "Compará las ventas")
        self.assertEqual(chat.send_message.call_count, vi_agent.MAX_RETRIES)
        self.assertEqual(wait.call_count, vi_agent.MAX_RETRIES - 1)

    def test_retry_delay_grows_exponentially_with_bounded_jitter(self):
        # 2026-09-24: sin jitter, varios procesos que reciben el mismo 503 de "alta demanda"
        # reintentan sincronizados y vuelven a chocar; con 6 intentos la espera total nominal es ~62 s.
        for attempt in range(1, vi_agent.MAX_RETRIES):
            nominal = vi_agent.RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            low = nominal * (1 - vi_agent.RETRY_JITTER_FRACTION)
            high = nominal * (1 + vi_agent.RETRY_JITTER_FRACTION)
            for _ in range(50):
                self.assertTrue(low - 0.01 <= vi_agent._retry_delay(attempt) <= high + 0.01)
        self.assertGreaterEqual(vi_agent.MAX_RETRIES, 6)

    def test_query_timeout_rolls_back_without_reconnecting(self):
        connection = MagicMock()
        connection.closed = False
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = vi_agent.psycopg.errors.QueryCanceled("timeout")
        with patch.object(vi_agent, "_validate_sql"), patch.object(vi_agent, "_get_reusable_sql_connection", return_value=connection) as get:
            with self.assertRaises(vi_agent.psycopg.errors.QueryCanceled):
                vi_agent.run_readonly_sql("SELECT ...")
        get.assert_called_once()
        connection.rollback.assert_called_once()

    def test_correctable_error_after_reconnection_also_rolls_back(self):
        broken = MagicMock()
        broken.closed = True
        broken.cursor.return_value.__enter__.return_value.execute.side_effect = vi_agent.psycopg.OperationalError("conexión caída")
        restored = MagicMock()
        restored.closed = False
        restored.cursor.return_value.__enter__.return_value.execute.side_effect = vi_agent.psycopg.errors.UndefinedColumn("columna incorrecta")
        with patch.object(vi_agent, "_validate_sql"), patch.object(
            vi_agent, "_get_reusable_sql_connection", side_effect=[broken, restored]
        ):
            with self.assertRaises(vi_agent.psycopg.errors.UndefinedColumn):
                vi_agent.run_readonly_sql("SELECT ...")
        restored.rollback.assert_called_once()

    def test_authentication_failure_during_cache_creation_stops_before_model(self):
        client = MagicMock()
        client.caches.create.side_effect = vi_agent.errors.ClientError(401, {"error": {"message": "secret"}})
        with tempfile.TemporaryDirectory() as directory, patch.object(
            vi_agent, "GEMINI_CACHE_DIR", Path(directory)
        ), patch.object(vi_agent, "_get_reusable_genai_client", return_value=client), patch.object(
            vi_agent, "_build_tools_list", return_value=[]
        ), patch.dict("os.environ", {"VERA_AI_API_KEY": "fake-key"}):
            with self.assertRaises(OperationalUnavailable):
                vi_agent.build_chat(system_instruction="Instrucción de prueba")
        client.chats.create.assert_not_called()

    def test_disconnect_during_tool_prevents_next_model_turn(self):
        connected = {"value": True}
        control = AnalysisControl(lambda: not connected["value"])
        chat = Chat([sql_call()])

        def tool(sql):
            connected["value"] = False
            return "{}"

        with patch.dict(vi_agent.TOOL_FUNCTIONS, run_readonly_sql=tool):
            with self.assertRaises(AnalysisCancelled):
                vi_agent.run_tool_loop(chat, "Compará las ventas", analysis_control=control)
        self.assertEqual(len(chat.messages), 1)

    def test_cancel_prevents_another_embedding_attempt(self):
        control = AnalysisControl()
        client = MagicMock()
        client.models.embed_content.side_effect = vi_agent.errors.ServerError(503, {"error": {"message": "offline"}})
        with analysis_scope(control), patch.object(vector_search, "_get_reusable_embed_client", return_value=client), patch.object(
            vector_search, "wait_before_retry", side_effect=lambda _seconds: control.cancel()
        ):
            with self.assertRaises(AnalysisCancelled):
                vector_search._embed_query("consulta", "fake-key")
        client.models.embed_content.assert_called_once()


if __name__ == "__main__":
    unittest.main()
