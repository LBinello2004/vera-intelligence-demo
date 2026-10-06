"""compute_stats (2026-10-05): matemática determinista sobre resultados de SQL ya obtenidos."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import stats_tool  # noqa: E402
import vi_agent  # noqa: E402
from answer_verification import add_result, evidence_id, verify_answer  # noqa: E402
from test_vi_agent import FakeChat, FakeFunctionCall, FakeResponse  # noqa: E402


def sql_result(rows: list[list], columns=("tienda", "exitos", "evaluadas")) -> dict:
    return {"columns": list(columns), "rows": rows, "row_count": len(rows)}


def store_with(payload: dict) -> tuple[dict, str]:
    store: dict = {}
    key = add_result(store, payload)
    return store, key


class MathTests(unittest.TestCase):
    def test_wilson_matches_reference_values(self) -> None:
        lo, hi = stats_tool.wilson(50, 100)
        self.assertAlmostEqual(lo, 0.4038, places=4)
        self.assertAlmostEqual(hi, 0.5962, places=4)
        lo, hi = stats_tool.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=4)
        self.assertEqual(stats_tool.wilson(0, 0), (0.0, 1.0))

    def test_newcombe_matches_published_example(self) -> None:
        # Newcombe (1998), ejemplo 1: 56/70 vs 48/80 -> 0,2000 (0,0524 a 0,3339).
        d, lo, hi = stats_tool.newcombe_diff(56, 70, 48, 80)
        self.assertAlmostEqual(d, 0.2, places=4)
        self.assertAlmostEqual(lo, 0.0524, places=4)
        self.assertAlmostEqual(hi, 0.3339, places=4)


class OperationTests(unittest.TestCase):
    def test_proportion_per_group(self) -> None:
        store, key = store_with(sql_result([["A", 40, 100], ["B", 3, 10]]))
        out = stats_tool.compute(store, "proporcion", key, "exitos", "evaluadas", "tienda")
        rows = [dict(zip(out["columns"], r)) for r in out["rows"]]
        self.assertEqual(rows[0]["tasa_pct"], 40.0)
        self.assertAlmostEqual(rows[0]["ic95_min_pct"], 30.94, places=2)
        self.assertAlmostEqual(rows[0]["ic95_max_pct"], 49.8, places=2)
        self.assertEqual(rows[0]["base_suficiente"], "si")
        self.assertEqual(rows[1]["base_suficiente"], "no")  # 10 < MIN_N

    def test_ranking_requires_min_n_and_flags_technical_ties(self) -> None:
        store, key = store_with(sql_result([
            ["Alta", 80, 100], ["Media", 70, 100], ["Baja", 20, 100], ["Chica", 9, 10]]))
        out = stats_tool.compute(store, "ranking", key, "exitos", "evaluadas", "tienda")
        rows = [dict(zip(out["columns"], r)) for r in out["rows"]]
        self.assertEqual([r["grupo"] for r in rows], ["Alta", "Media", "Baja", "Chica"])
        self.assertEqual([r["puesto"] for r in rows], [1, 2, 3, None])
        # 80/100 vs 70/100: los intervalos se superponen -> empate técnico, no se puede decir que uno gane.
        self.assertEqual(rows[0]["separado_del_siguiente"], "no")
        # 70/100 vs 20/100: claramente separados.
        self.assertEqual(rows[1]["separado_del_siguiente"], "si")
        self.assertEqual(rows[2]["separado_del_siguiente"], "ultimo")
        self.assertEqual(rows[3]["separado_del_siguiente"], "sin_puesto")

    def test_ranking_ascending_order(self) -> None:
        store, key = store_with(sql_result([["Alta", 80, 100], ["Baja", 20, 100]]))
        out = stats_tool.compute(store, "ranking", key, "exitos", "evaluadas", "tienda", orden="asc")
        self.assertEqual([r[1] for r in out["rows"]], ["Baja", "Alta"])
        self.assertEqual(out["rows"][0][7], "si")

    def test_compare_two_groups_in_one_result(self) -> None:
        store, key = store_with(sql_result([["julio", 56, 70], ["agosto", 48, 80]]))
        out = stats_tool.compute(store, "comparar", key, "exitos", "evaluadas", "tienda", grupo_a="julio", grupo_b="agosto")
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(row["diferencia_pct_puntos"], 20.0)
        self.assertEqual(row["ic95_dif_min_pct"], 5.24)
        self.assertEqual(row["ic95_dif_max_pct"], 33.39)
        self.assertEqual(row["diferencia_distinguible"], "si")
        self.assertEqual(row["aviso_base"], "ok")

    def test_compare_flags_indistinguishable_and_unbalanced_bases(self) -> None:
        store, key = store_with(sql_result([["a", 50, 100], ["b", 55, 100], ["c", 130, 300]]))
        out = stats_tool.compute(store, "comparar", key, "exitos", "evaluadas", "tienda", grupo_a="a", grupo_b="b")
        self.assertEqual(dict(zip(out["columns"], out["rows"][0]))["diferencia_distinguible"], "no")
        out = stats_tool.compute(store, "comparar", key, "exitos", "evaluadas", "tienda", grupo_a="a", grupo_b="c")
        self.assertEqual(dict(zip(out["columns"], out["rows"][0]))["aviso_base"], "bases_muy_distintas")
        store2, key2 = store_with(sql_result([["a", 5, 10], ["b", 9, 12]]))
        out = stats_tool.compute(store2, "comparar", key2, "exitos", "evaluadas", "tienda", grupo_a="a", grupo_b="b")
        self.assertEqual(dict(zip(out["columns"], out["rows"][0]))["aviso_base"], "base_insuficiente")

    def test_compare_across_two_results(self) -> None:
        store, key_a = store_with(sql_result([["mes", 56, 70]]))
        key_b = add_result(store, sql_result([["mes", 48, 80]], columns=("tienda", "exitos", "evaluadas")) | {"nota": "otro período"})
        out = stats_tool.compute(store, "comparar", key_a, "exitos", "evaluadas", fuente_id_b=key_b)
        self.assertEqual(dict(zip(out["columns"], out["rows"][0]))["diferencia_pct_puntos"], 20.0)

    def test_is_deterministic(self) -> None:
        store, key = store_with(sql_result([["A", 40, 100], ["B", 30, 100]]))
        first = stats_tool.compute_json(store, operacion="ranking", fuente_id=key, columna_exitos="exitos",
                                        columna_total="evaluadas", columna_grupo="tienda")
        second = stats_tool.compute_json(store, operacion="ranking", fuente_id=key, columna_exitos="exitos",
                                         columna_total="evaluadas", columna_grupo="tienda")
        self.assertEqual(first, second)


class InputValidationTests(unittest.TestCase):
    def check(self, payload: dict, expected: str, **kwargs) -> None:
        store, key = store_with(payload)
        args = {"operacion": "proporcion", "fuente_id": key, "columna_exitos": "exitos", "columna_total": "evaluadas",
                "columna_grupo": "tienda"} | kwargs
        with self.assertRaises(ValueError) as ctx:
            stats_tool.compute(store, **args)
        self.assertIn(expected, str(ctx.exception))

    def test_rejects_a_made_up_source(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            stats_tool.compute({}, "proporcion", "sql_inventado", "exitos", "evaluadas")
        self.assertIn("fuente_id inexistente", str(ctx.exception))

    def test_rejects_rates_instead_of_counts(self) -> None:
        self.check(sql_result([["A", 0.4, 100]]), "CONTEO")

    def test_rejects_successes_above_total(self) -> None:
        self.check(sql_result([["A", 120, 100]]), "más éxitos")

    def test_rejects_zero_base(self) -> None:
        self.check(sql_result([["A", 0, 0]]), "0 evaluadas")

    def test_rejects_unknown_column_and_lists_available(self) -> None:
        self.check(sql_result([["A", 4, 10]]), "Columnas disponibles", columna_exitos="si")

    def test_rejects_truncated_results(self) -> None:
        self.check(sql_result([["A", 4, 10]]) | {"truncated": True}, "truncado")

    def test_rejects_repeated_groups(self) -> None:
        self.check(sql_result([["A", 4, 10], ["A", 5, 10]]), "grupos repetidos")

    def test_rejects_bad_operation(self) -> None:
        self.check(sql_result([["A", 4, 10]]), "operacion inválida", operacion="regresion")

    def test_compare_needs_group_labels_when_several_groups(self) -> None:
        self.check(sql_result([["A", 4, 40], ["B", 5, 40]]), "indicá grupo_a", operacion="comparar")


class EvidenceIntegrationTests(unittest.TestCase):
    def test_figures_from_the_tool_are_verifiable_like_sql_cells(self) -> None:
        store, key = store_with(sql_result([["A", 40, 100], ["B", 30, 100]]))
        out = stats_tool.compute(store, "proporcion", key, "exitos", "evaluadas", "tienda")
        add_result(store, out)
        ok = verify_answer("La tienda A tiene 40% (entre 31% y 50%) sobre 100 conversaciones.", store)
        self.assertEqual(ok.errors, [])
        bad = verify_answer("La tienda A tiene 55% de tasa.", store)
        self.assertTrue(any("cifra sin respaldo" in e for e in bad.errors))

    def test_loop_runs_the_tool_with_the_interaction_evidence(self) -> None:
        sql_json = json.dumps(sql_result([["A", 40, 100], ["B", 30, 100]]))
        sql_id = evidence_id(json.loads(sql_json))
        chat = FakeChat([
            FakeResponse("", function_calls=[FakeFunctionCall("run_readonly_sql", {"sql": "SELECT 1"})]),
            FakeResponse("", function_calls=[FakeFunctionCall("compute_stats", {
                "operacion": "proporcion", "fuente_id": sql_id, "columna_exitos": "exitos",
                "columna_total": "evaluadas", "columna_grupo": "tienda"})]),
            FakeResponse("La tienda A tiene 40% de tasa sobre 100 conversaciones evaluadas."),
        ])
        log: list[dict] = []
        with patch.dict(vi_agent.TOOL_FUNCTIONS, {"run_readonly_sql": lambda sql: sql_json}):
            answer = vi_agent.run_tool_loop(chat, "¿Cómo le va a cada tienda?", tool_calls_log=log)
        self.assertEqual([c["name"] for c in log], ["run_readonly_sql", "compute_stats"])
        self.assertIsNone(log[1]["error"])
        computed = json.loads(log[1]["result"])
        self.assertEqual(computed["rows"][0][3], 40.0)
        self.assertIn("40%", answer)

    def test_loop_reports_an_error_for_a_made_up_id(self) -> None:
        chat = FakeChat([
            FakeResponse("", function_calls=[FakeFunctionCall("compute_stats", {
                "operacion": "proporcion", "fuente_id": "sql_inventado", "columna_exitos": "exitos",
                "columna_total": "evaluadas"})]),
            FakeResponse("No pude calcularlo."),
        ])
        log: list[dict] = []
        vi_agent.run_tool_loop(chat, "¿Y el ranking?", tool_calls_log=log)
        self.assertIn("fuente_id inexistente", log[0]["error"])

    def test_tool_is_always_exposed_and_documented_in_the_prompt(self) -> None:
        vi_agent.configure_client("forever_21_bajo")  # cliente sin búsqueda vectorial ni extracción
        names = {getattr(t, "__name__", None) for t in vi_agent._build_tools_list()}
        self.assertIn("compute_stats", names)
        self.assertIn("compute_stats", vi_agent._build_extra_tools_section())
        vi_agent.configure_client("mens_fashion_alto")


if __name__ == "__main__":
    unittest.main()
