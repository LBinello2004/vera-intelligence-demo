from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sheets_logging import SheetsLogger, _HEADER_LABELS_BY_SHEET  # noqa: E402


class SheetsLoggerDisabledTests(unittest.TestCase):
    def test_disabled_without_webapp_url(self) -> None:
        logger = SheetsLogger(webapp_url=None)
        self.assertFalse(logger.enabled)
        self.assertFalse(logger.append_row("feedback", {"vote": "up"}))

    def test_enabled_with_webapp_url(self) -> None:
        logger = SheetsLogger(webapp_url="https://script.google.com/macros/s/xyz/exec")
        self.assertTrue(logger.enabled)


class SheetsLoggerAppendRowTests(unittest.TestCase):
    """Simula requests.post -no depende de red real ni de un Web App desplegado."""

    def _make_response(self, *, ok: bool, status: int = 200, body: dict | None = None) -> MagicMock:
        response = MagicMock()
        response.raise_for_status = MagicMock()
        if status >= 400:
            response.raise_for_status.side_effect = Exception(f"HTTP {status}")
        response.json.return_value = body if body is not None else {"ok": ok}
        return response

    def test_append_row_posts_header_and_row_in_correct_order(self) -> None:
        logger = SheetsLogger(webapp_url="https://script.google.com/macros/s/xyz/exec")
        event = {
            "recorded_at": "2026-09-14T00:00:00+00:00",
            "client_id": "mens_fashion_alto",
            "session_id": "s1",
            "message_id": "msg-1",
            "vote": "up",
            "question": "¿Cuál es la tasa de conversión?",
            "answer": "42%",
            "tool_names": ["run_readonly_sql", "get_business_rules"],
        }
        with patch("requests.post", return_value=self._make_response(ok=True)) as mock_post:
            result = logger.append_row("feedback", event)

        self.assertTrue(result)
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["sheet"], "feedback")
        self.assertEqual(
            kwargs["json"]["row"],
            [
                "2026-09-14T00:00:00+00:00",
                "mens_fashion_alto",
                "s1",
                "msg-1",
                "up",
                "¿Cuál es la tasa de conversión?",
                "42%",
                "run_readonly_sql, get_business_rules",
            ],
        )
        # El header enviado es texto legible para la planilla, no las claves técnicas del event.
        self.assertEqual(kwargs["json"]["header"], _HEADER_LABELS_BY_SHEET["feedback"])
        self.assertEqual(kwargs["json"]["header"][0], "Fecha y hora (UTC)")

    def test_append_row_flattens_none_question_to_empty_string(self) -> None:
        logger = SheetsLogger(webapp_url="https://script.google.com/macros/s/xyz/exec")
        with patch("requests.post", return_value=self._make_response(ok=True)) as mock_post:
            logger.append_row(
                "feedback",
                {
                    "recorded_at": "x",
                    "client_id": "c",
                    "session_id": "s",
                    "message_id": "m",
                    "vote": "down",
                    "question": None,
                    "answer": "a",
                    "tool_names": [],
                },
            )
        row = mock_post.call_args.kwargs["json"]["row"]
        self.assertEqual(row[5], "")  # question
        self.assertEqual(row[7], "")  # tool_names vacía

    def test_append_row_never_raises_on_network_failure(self) -> None:
        """Núcleo de la garantía de resiliencia: sin red / Web App caído, el llamador
        (FeedbackRecorder/QuestionRecorder) nunca debe ver una excepción -sólo False."""
        logger = SheetsLogger(webapp_url="https://script.google.com/macros/s/xyz/exec")
        with patch("requests.post", side_effect=ConnectionError("sin red")):
            result = logger.append_row("feedback", {"vote": "up"})
        self.assertFalse(result)

    def test_append_row_never_raises_on_ok_false_response(self) -> None:
        # El Web App puede responder 200 pero con un error de aplicación (ej. pestaña inválida).
        logger = SheetsLogger(webapp_url="https://script.google.com/macros/s/xyz/exec")
        with patch("requests.post", return_value=self._make_response(ok=False, body={"ok": False, "error": "boom"})):
            result = logger.append_row("feedback", {"vote": "up"})
        self.assertFalse(result)

    def test_append_row_never_raises_on_http_error(self) -> None:
        logger = SheetsLogger(webapp_url="https://script.google.com/macros/s/xyz/exec")
        with patch("requests.post", return_value=self._make_response(ok=True, status=500)):
            result = logger.append_row("feedback", {"vote": "up"})
        self.assertFalse(result)

    def test_append_row_disabled_never_calls_requests(self) -> None:
        logger = SheetsLogger(webapp_url=None)
        with patch("requests.post") as mock_post:
            logger.append_row("feedback", {"vote": "up"})
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
