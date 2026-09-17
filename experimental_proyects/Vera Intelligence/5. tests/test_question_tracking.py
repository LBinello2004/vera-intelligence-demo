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

from question_tracking import QuestionRecorder, load_question_events  # noqa: E402


class QuestionRecorderTests(unittest.TestCase):
    """QuestionRecorder guarda un archivo POR CLIENTE, dentro de la carpeta real de ese cliente
    (2026-09-14, reorganizado a pedido explícito -antes vivía en un único archivo compartido bajo
    un top-level preguntas_por_cliente/). La ruta se resuelve por `client_folder` (nombre de
    carpeta real, ej. "agrosuper_bajo"), nunca por `client_id` (identidad estable, ej. "agrosuper")
    -son distintos, mismo criterio que `data_map_auto_update.run_gate()`."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clients_root = Path(self.temp_dir.name)
        self.recorder = QuestionRecorder(self.clients_root)

    def test_records_question_under_the_client_folder(self) -> None:
        event = self.recorder.record(
            client_folder="agrosuper_bajo",
            client_id="agrosuper",
            client_display_name="Agrosuper",
            session_id="session-1",
            question="¿Cuántas visitas se registraron?",
            answer="Se registraron 120 visitas.",
            tool_names=["run_readonly_sql"],
        )

        self.assertEqual(event["client_id"], "agrosuper")
        self.assertEqual(event["client_display_name"], "Agrosuper")
        self.assertEqual(event["question"], "¿Cuántas visitas se registraron?")
        self.assertEqual(event["answer"], "Se registraron 120 visitas.")
        self.assertEqual(event["tool_names"], ["run_readonly_sql"])
        # Fecha Y hora -pedido explícito, no sólo fecha- en ISO 8601 (trae al menos "T" y ":").
        self.assertIn("T", event["recorded_at"])
        self.assertIn(":", event["recorded_at"])

        expected_path = self.clients_root / "agrosuper_bajo" / "preguntas" / "questions_events.jsonl"
        self.assertTrue(expected_path.is_file())
        loaded = load_question_events(expected_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["session_id"], "session-1")
        if hasattr(__import__("os"), "fchmod"):
            # Windows/NTFS no soporta bits de permiso POSIX via os.open -mismo criterio que
            # test_usage_tracking.py/test_feedback_tracking.py para la misma garantía.
            self.assertEqual(stat.S_IMODE(expected_path.stat().st_mode), 0o600)

    def test_uses_client_folder_not_client_id_for_the_path(self) -> None:
        # Caso real: agrosuper_bajo tiene client_id="agrosuper" (sin el sufijo de grado) -si la
        # ruta se resolviera por client_id en vez de client_folder, escribiría en una carpeta que
        # no existe en disco ("2. clientes/agrosuper/").
        self.recorder.record(
            client_folder="agrosuper_bajo",
            client_id="agrosuper",
            client_display_name="Agrosuper",
            session_id="s1",
            question="¿q?",
            answer="respuesta",
            tool_names=[],
        )
        self.assertFalse((self.clients_root / "agrosuper").exists())
        self.assertTrue((self.clients_root / "agrosuper_bajo").exists())

    def test_two_clients_get_separate_files(self) -> None:
        self.recorder.record(
            client_folder="agrosuper_bajo",
            client_id="agrosuper",
            client_display_name="Agrosuper",
            session_id="s1",
            question="¿q agrosuper?",
            answer="r agrosuper",
            tool_names=[],
        )
        self.recorder.record(
            client_folder="mens_fashion_alto",
            client_id="mens_fashion",
            client_display_name="Mens Fashion",
            session_id="s2",
            question="¿q mens fashion?",
            answer="r mens fashion",
            tool_names=[],
        )
        agrosuper_events = load_question_events(self.recorder.log_path("agrosuper_bajo"))
        mens_fashion_events = load_question_events(self.recorder.log_path("mens_fashion_alto"))
        self.assertEqual([e["question"] for e in agrosuper_events], ["¿q agrosuper?"])
        self.assertEqual([e["question"] for e in mens_fashion_events], ["¿q mens fashion?"])

    def test_multiple_questions_same_client_append_separate_events(self) -> None:
        self.recorder.record(
            client_folder="mens_fashion_alto",
            client_id="mens_fashion",
            client_display_name="Mens Fashion",
            session_id="s1",
            question="¿q1?",
            answer="r1",
            tool_names=[],
        )
        self.recorder.record(
            client_folder="mens_fashion_alto",
            client_id="mens_fashion",
            client_display_name="Mens Fashion",
            session_id="s1",
            question="¿q2?",
            answer="r2",
            tool_names=["get_business_rules"],
        )
        events = load_question_events(self.recorder.log_path("mens_fashion_alto"))
        self.assertEqual(len(events), 2)
        self.assertEqual([e["question"] for e in events], ["¿q1?", "¿q2?"])

    def test_load_question_events_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_question_events(Path("/no/existe/questions.jsonl")), [])

    def test_sheets_logger_receives_same_event_when_provided(self) -> None:
        sheets_logger = MagicMock()
        recorder = QuestionRecorder(self.clients_root, sheets_logger=sheets_logger)
        event = recorder.record(
            client_folder="agrosuper_bajo",
            client_id="agrosuper",
            client_display_name="Agrosuper",
            session_id="session-1",
            question="¿q?",
            answer="r",
            tool_names=[],
        )
        sheets_logger.append_row.assert_called_once_with("preguntas", event)

    def test_sheets_logger_failure_does_not_break_local_record(self) -> None:
        sheets_logger = MagicMock()
        sheets_logger.append_row.side_effect = RuntimeError("sin red")
        recorder = QuestionRecorder(self.clients_root, sheets_logger=sheets_logger)
        event = recorder.record(
            client_folder="agrosuper_bajo",
            client_id="agrosuper",
            client_display_name="Agrosuper",
            session_id="session-1",
            question="¿q?",
            answer="r",
            tool_names=[],
        )
        self.assertEqual(event["question"], "¿q?")
        loaded = load_question_events(recorder.log_path("agrosuper_bajo"))
        self.assertEqual(len(loaded), 1)


if __name__ == "__main__":
    unittest.main()
