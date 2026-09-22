from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from usage_tracking import UsageRecorder, load_usage_events, summarize_usage  # noqa: E402


class UsageTrackingTests(unittest.TestCase):
    def test_records_only_usage_counts_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            recorder = UsageRecorder(path)
            response = SimpleNamespace(
                text="contenido que nunca debe persistirse",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=100,
                    candidates_token_count=20,
                    thoughts_token_count=7,
                    cached_content_token_count=None,
                    tool_use_prompt_token_count=3,
                    total_token_count=130,
                    traffic_type=SimpleNamespace(value="ON_DEMAND"),
                ),
            )

            event = recorder.record_response(
                response,
                client_id="mens_fashion",
                model="gemini-test",
                session_id="session-1",
                interaction_id="interaction-1",
                call_index=1,
                call_kind="initial",
                attempts=1,
            )

            self.assertIsNotNone(event)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(response.text, raw)
            self.assertEqual(
                set(event),
                {
                    "schema_version",
                    "recorded_at",
                    "client_id",
                    "model",
                    "session_id",
                    "interaction_id",
                    "call_index",
                    "call_kind",
                    "attempts",
                    "prompt_token_count",
                    "candidates_token_count",
                    "thoughts_token_count",
                    "cached_content_token_count",
                    "tool_use_prompt_token_count",
                    "total_token_count",
                    "traffic_type",
                },
            )
            self.assertEqual(event["prompt_token_count"], 100)
            self.assertEqual(event["total_token_count"], 130)
            if hasattr(os, "fchmod"):
                # Windows/NTFS no soporta bits de permiso POSIX via os.open;
                # esta garantia solo aplica en plataformas tipo Unix.
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_summarizes_multiple_calls(self) -> None:
        events = [
            {
                "session_id": "s1",
                "interaction_id": "i1",
                "prompt_token_count": 10,
                "candidates_token_count": 2,
                "total_token_count": 12,
            },
            {
                "session_id": "s1",
                "interaction_id": "i1",
                "prompt_token_count": 20,
                "candidates_token_count": 4,
                "thoughts_token_count": 1,
                "total_token_count": 25,
            },
        ]

        summary = summarize_usage(events)

        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["interactions"], 1)
        self.assertEqual(summary["prompt_token_count"], 30)
        self.assertEqual(summary["total_token_count"], 37)

    def test_tools_called_is_stored_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            event = UsageRecorder(path).record_response(
                SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=10, candidates_token_count=1, thoughts_token_count=0,
                        cached_content_token_count=0, tool_use_prompt_token_count=0,
                        total_token_count=11, traffic_type=None,
                    )
                ),
                client_id="mens_fashion",
                model="gemini-test",
                session_id="s1",
                interaction_id="i1",
                call_index=2,
                call_kind="tool_results",
                attempts=1,
                tools_called=["run_readonly_sql", "get_business_rules"],
            )
            self.assertEqual(event["tools_called"], ["get_business_rules", "run_readonly_sql"])

    def test_no_tools_called_omits_the_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            event = UsageRecorder(path).record_response(
                SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=10, candidates_token_count=1, thoughts_token_count=0,
                        cached_content_token_count=0, tool_use_prompt_token_count=0,
                        total_token_count=11, traffic_type=None,
                    )
                ),
                client_id="mens_fashion",
                model="gemini-test",
                session_id="s1",
                interaction_id="i1",
                call_index=1,
                call_kind="initial",
                attempts=1,
            )
            self.assertNotIn("tools_called", event)

    def test_response_without_usage_metadata_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            event = UsageRecorder(path).record_response(
                SimpleNamespace(),
                client_id="mens_fashion",
                model="gemini-test",
                session_id="s1",
                interaction_id="i1",
                call_index=1,
                call_kind="initial",
                attempts=1,
            )

            self.assertIsNone(event)
            self.assertEqual(load_usage_events(path), [])


class RecordEstimatedTests(unittest.TestCase):
    """UsageRecorder.record_estimated (2026-09-22): para llamadas cuyo proveedor no devuelve
    usage_metadata (ej. embed_content de Gemini, ver _embed_query en vector_search.py) -el costo
    real quedaba invisible en gemini_calls.jsonl hasta ahora."""

    def test_writes_an_event_with_estimated_tokens_and_zero_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            event = UsageRecorder(path).record_estimated(
                client_id="mens_fashion",
                model="gemini-embedding-001",
                session_id="",
                interaction_id="i1",
                call_index=1,
                call_kind="embed_query",
                prompt_token_count=13,
            )
            self.assertEqual(event["prompt_token_count"], 13)
            self.assertEqual(event["total_token_count"], 13)
            self.assertEqual(event["candidates_token_count"], 0)
            self.assertTrue(event["is_estimated"])
            self.assertEqual(load_usage_events(path), [event])

    def test_estimated_events_are_distinguishable_from_measured_ones(self) -> None:
        # Un evento medido de verdad (record_response) nunca debe llevar is_estimated -ningún
        # reporte debe poder confundir estimado con exacto.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "usage.jsonl"
            recorder = UsageRecorder(path)
            recorder.record_response(
                SimpleNamespace(usage_metadata=SimpleNamespace(
                    prompt_token_count=100, candidates_token_count=10, thoughts_token_count=0,
                    cached_content_token_count=0, tool_use_prompt_token_count=0, total_token_count=110,
                )),
                client_id="c", model="gemini-test", session_id="s", interaction_id="i",
                call_index=1, call_kind="initial", attempts=1,
            )
            recorder.record_estimated(
                client_id="c", model="gemini-embedding-001", session_id="", interaction_id="i2",
                call_index=1, call_kind="embed_query", prompt_token_count=5,
            )
            events = load_usage_events(path)
            self.assertNotIn("is_estimated", events[0])
            self.assertTrue(events[1]["is_estimated"])


if __name__ == "__main__":
    unittest.main()
