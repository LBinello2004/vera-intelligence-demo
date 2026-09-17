from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import usage_report  # noqa: E402


class ToolsKeyGroupingTests(unittest.TestCase):
    """`_tools_key`/`_group_by_tools` (2026-09-14, pedido explícito: "costo de cada tool") -
    agrupan eventos de uso por la combinación exacta de tools del turno, sin repartir el costo de
    un turno multi-tool entre cada tool por separado (ver docstring de `_print_by_tool_breakdown`
    para el motivo: esa división sería precisión inventada)."""

    def test_event_without_tools_called_gets_the_no_tool_label(self) -> None:
        self.assertEqual(usage_report._tools_key({}), usage_report._NO_TOOL_LABEL)
        self.assertEqual(usage_report._tools_key({"tools_called": []}), usage_report._NO_TOOL_LABEL)

    def test_single_tool_key_is_just_its_name(self) -> None:
        event = {"tools_called": ["run_readonly_sql"]}
        self.assertEqual(usage_report._tools_key(event), "run_readonly_sql")

    def test_multi_tool_key_joins_sorted_names(self) -> None:
        event = {"tools_called": ["run_readonly_sql", "get_business_rules"]}
        self.assertEqual(usage_report._tools_key(event), "run_readonly_sql+get_business_rules")

    def test_groups_events_by_exact_tool_combination(self) -> None:
        events = [
            {"call_index": 1},  # initial, sin tools_called
            {"call_index": 2, "tools_called": ["run_readonly_sql"]},
            {"call_index": 3, "tools_called": ["run_readonly_sql"]},
            {"call_index": 4, "tools_called": ["get_business_rules", "run_readonly_sql"]},
        ]
        groups = usage_report._group_by_tools(events)
        self.assertEqual(len(groups[usage_report._NO_TOOL_LABEL]), 1)
        self.assertEqual(len(groups["run_readonly_sql"]), 2)
        self.assertEqual(len(groups["get_business_rules+run_readonly_sql"]), 1)


if __name__ == "__main__":
    unittest.main()
