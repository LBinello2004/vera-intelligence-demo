from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from vector_search_quality_report import (  # noqa: E402
    flag_clients_needing_attention,
    summarize_by_client,
)


def _event(client_id: str, *, results_returned: int, judge_filtered_count: int = 0,
           query_ms: float = 50_000.0, recorded_at: str = "2026-09-16T00:00:00+00:00") -> dict:
    return {
        "client_id": client_id,
        "results_returned": results_returned,
        "judge_filtered_count": judge_filtered_count,
        "query_ms": query_ms,
        "recorded_at": recorded_at,
    }


class SummarizeByClientTests(unittest.TestCase):
    def test_empty_events_returns_empty_summary(self) -> None:
        self.assertEqual(summarize_by_client([]), {})

    def test_groups_by_client_id(self) -> None:
        events = [_event("mens_fashion", results_returned=2), _event("boggi", results_returned=1)]
        summary = summarize_by_client(events)
        self.assertEqual(set(summary), {"mens_fashion", "boggi"})
        self.assertEqual(summary["mens_fashion"]["calls"], 1)

    def test_empty_rate_counts_zero_result_calls(self) -> None:
        events = [
            _event("dalton", results_returned=0),
            _event("dalton", results_returned=0),
            _event("dalton", results_returned=3),
        ]
        summary = summarize_by_client(events)
        self.assertAlmostEqual(summary["dalton"]["empty_rate"], 2 / 3, places=3)

    def test_judge_discard_rate_uses_pre_judge_total(self) -> None:
        # 2 resultados devueltos + 3 descartados por el juez = 5 candidatos vistos por el juez;
        # tasa de descarte = 3/5.
        events = [_event("salomon", results_returned=2, judge_filtered_count=3)]
        summary = summarize_by_client(events)
        self.assertAlmostEqual(summary["salomon"]["judge_discard_rate"], 0.6)

    def test_judge_discard_rate_is_none_without_any_candidates(self) -> None:
        events = [_event("gac", results_returned=0, judge_filtered_count=0)]
        summary = summarize_by_client(events)
        self.assertIsNone(summary["gac"]["judge_discard_rate"])

    def test_query_ms_percentile_and_average(self) -> None:
        events = [_event("huerpel_ventas", results_returned=1, query_ms=ms) for ms in (10, 20, 30, 40, 100)]
        summary = summarize_by_client(events)
        self.assertEqual(summary["huerpel_ventas"]["avg_query_ms"], 40.0)
        self.assertEqual(summary["huerpel_ventas"]["p95_query_ms"], 100)

    def test_first_and_last_call_at_track_recorded_at_range(self) -> None:
        events = [
            _event("boggi", results_returned=1, recorded_at="2026-09-10T00:00:00+00:00"),
            _event("boggi", results_returned=1, recorded_at="2026-09-16T00:00:00+00:00"),
        ]
        summary = summarize_by_client(events)
        self.assertEqual(summary["boggi"]["first_call_at"], "2026-09-10T00:00:00+00:00")
        self.assertEqual(summary["boggi"]["last_call_at"], "2026-09-16T00:00:00+00:00")

    def test_missing_client_id_falls_back_to_placeholder(self) -> None:
        summary = summarize_by_client([{"results_returned": 1}])
        self.assertIn("(desconocido)", summary)


class FlagClientsNeedingAttentionTests(unittest.TestCase):
    def test_low_volume_client_is_never_flagged_even_with_bad_metrics(self) -> None:
        # 3 llamadas, todas vacías -pero por debajo de _ATTENTION_MIN_CALLS, así que no se marca
        # (ruido estadístico, no una señal confiable todavía).
        events = [_event("forever_21", results_returned=0) for _ in range(3)]
        summary = summarize_by_client(events)
        self.assertEqual(flag_clients_needing_attention(summary), [])

    def test_high_empty_rate_with_enough_volume_is_flagged(self) -> None:
        events = [_event("dalton", results_returned=0) for _ in range(6)] + [
            _event("dalton", results_returned=2)
        ]
        summary = summarize_by_client(events)
        self.assertIn("dalton", flag_clients_needing_attention(summary))

    def test_high_judge_discard_rate_with_enough_volume_is_flagged(self) -> None:
        events = [_event("hyundai", results_returned=1, judge_filtered_count=4) for _ in range(6)]
        summary = summarize_by_client(events)
        self.assertIn("hyundai", flag_clients_needing_attention(summary))

    def test_healthy_client_is_not_flagged(self) -> None:
        events = [_event("salomon", results_returned=4, judge_filtered_count=1) for _ in range(10)]
        summary = summarize_by_client(events)
        self.assertEqual(flag_clients_needing_attention(summary), [])


if __name__ == "__main__":
    unittest.main()
