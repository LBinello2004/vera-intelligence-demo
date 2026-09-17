from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from feedback_tracking import FeedbackRecorder, load_feedback_events  # noqa: E402


class FeedbackRecorderTests(unittest.TestCase):
    def test_records_vote_with_question_and_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.jsonl"
            recorder = FeedbackRecorder(path)

            event = recorder.record(
                client_id="mens_fashion_alto",
                session_id="session-1",
                message_id="msg-1",
                vote="up",
                question="¿Cuál es la tasa de conversión?",
                answer="La tasa de conversión es 42%.",
                tool_names=["run_readonly_sql"],
            )

            self.assertEqual(event["vote"], "up")
            self.assertEqual(event["question"], "¿Cuál es la tasa de conversión?")
            self.assertEqual(event["answer"], "La tasa de conversión es 42%.")
            self.assertEqual(event["tool_names"], ["run_readonly_sql"])
            loaded = load_feedback_events(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["message_id"], "msg-1")
            if hasattr(__import__("os"), "fchmod"):
                # Windows/NTFS no soporta bits de permiso POSIX via os.open -mismo criterio que
                # test_usage_tracking.py para el mismo tipo de garantía.
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_rejects_invalid_vote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = FeedbackRecorder(Path(temp_dir) / "feedback.jsonl")
            with self.assertRaises(ValueError):
                recorder.record(
                    client_id="mens_fashion_alto",
                    session_id="session-1",
                    message_id="msg-1",
                    vote="meh",
                    question=None,
                    answer="respuesta",
                    tool_names=[],
                )

    def test_greeting_feedback_has_no_question(self) -> None:
        # El saludo inicial no responde a una pregunta puntual del usuario -question=None es
        # válido y se persiste tal cual (ver "question": None en _ensure_greeting, streamlit_app.py).
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.jsonl"
            recorder = FeedbackRecorder(path)
            event = recorder.record(
                client_id="mens_fashion_alto",
                session_id="session-1",
                message_id="msg-greeting",
                vote="down",
                question=None,
                answer="¡Hola! Soy Vera Intelligence...",
                tool_names=[],
            )
            self.assertIsNone(event["question"])

    def test_multiple_votes_append_separate_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.jsonl"
            recorder = FeedbackRecorder(path)
            recorder.record(
                client_id="mens_fashion_alto",
                session_id="s1",
                message_id="msg-1",
                vote="up",
                question="q1",
                answer="a1",
                tool_names=[],
            )
            recorder.record(
                client_id="mens_fashion_alto",
                session_id="s1",
                message_id="msg-2",
                vote="down",
                question="q2",
                answer="a2",
                tool_names=[],
            )
            events = load_feedback_events(path)
            self.assertEqual(len(events), 2)
            self.assertEqual([e["message_id"] for e in events], ["msg-1", "msg-2"])

    def test_load_feedback_events_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_feedback_events(Path("/no/existe/feedback.jsonl")), [])

    def test_sheets_logger_receives_same_event_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sheets_logger = MagicMock()
            recorder = FeedbackRecorder(Path(temp_dir) / "feedback.jsonl", sheets_logger=sheets_logger)
            event = recorder.record(
                client_id="mens_fashion_alto",
                session_id="s1",
                message_id="msg-1",
                vote="up",
                question="q1",
                answer="a1",
                tool_names=["run_readonly_sql"],
            )
            sheets_logger.append_row.assert_called_once_with("feedback", event)

    def test_sheets_logger_failure_does_not_break_local_record(self) -> None:
        # Garantía central: un sheets_logger.append_row que lanza (aunque SheetsLogger real nunca
        # lo hace, ver sheets_logging.py) no debe impedir el registro local ni la respuesta normal
        # de record() -defensa en profundidad en el propio punto de llamada.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.jsonl"
            sheets_logger = MagicMock()
            sheets_logger.append_row.side_effect = RuntimeError("sin red")
            recorder = FeedbackRecorder(path, sheets_logger=sheets_logger)
            event = recorder.record(
                client_id="mens_fashion_alto",
                session_id="s1",
                message_id="msg-1",
                vote="up",
                question="q1",
                answer="a1",
                tool_names=[],
            )
            self.assertEqual(event["message_id"], "msg-1")
            # El registro local ya se escribió ANTES de llamar a sheets_logger -no se pierde.
            self.assertEqual(len(load_feedback_events(path)), 1)


if __name__ == "__main__":
    unittest.main()
