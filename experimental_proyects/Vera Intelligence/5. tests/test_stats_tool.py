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

    def test_same_wilson_as_the_extraction_tool(self) -> None:
        import insight_extraction

        for k, n in ((0, 5), (3, 7), (50, 100), (81, 263), (10, 10)):
            self.assertEqual(stats_tool.wilson(k, n), insight_extraction.wilson(k, n))


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


EXTRACT_COLUMNS = ("grupo", "modo", "poblacion_filtrada", "conversaciones_leidas", "confirmadas_con_evidencia", "pct_estimado",
                   "pct_minimo", "pct_maximo", "conversaciones_estimado", "conversaciones_minimo", "conversaciones_maximo")


def extract_result(groups: list[tuple], extras: bool = True) -> dict:
    """Forma REAL del resultado de extract_insight(desglosar_por=...): (grupo, leídas, mín %, máx %) más, como en producción, la
    fila 'suma de los grupos' y la de grupos sin leer (con vacíos). Un 5.º elemento opcional del grupo es la estimación."""
    rows = []
    for g in groups:
        nombre, leidas, lo, hi = g[:4]
        est = g[4] if len(g) > 4 else (None if hi is None else round((lo + hi) / 2, 1))
        rows.append([nombre, "muestra_aleatoria", 5000, leidas, 0, est, lo, hi, 0, 0, 0])
    if extras:
        rows.append(["(suma de los grupos incluidos)", "suma_de_grupos", 9000, 300, 0, 20.0, 8.0, 30.0, 0, 0, 0])
        rows.append(["(otros 4 grupos sin leer)", "no_leido", 65480, 0, None, None, None, None, None, None, None])
    return {"row_count": len(rows), "truncated": False, "columns": list(EXTRACT_COLUMNS), "rows": rows}


class RangeModeTests(unittest.TestCase):
    """Sobre lo que devuelve extract_insight: no hay SQL ni conteos, sólo rangos por grupo."""

    ARGS = {"columna_min": "pct_minimo", "columna_max": "pct_maximo", "columna_total": "conversaciones_leidas",
            "columna_grupo": "grupo"}

    def test_ranking_uses_range_overlap_not_the_midpoint(self) -> None:
        store, key = store_with(extract_result([("Centro", 100, 40.0, 60.0), ("Norte", 100, 30.0, 50.0),
                                                ("Sur", 100, 5.0, 15.0), ("Chica", 20, 70.0, 90.0)]))
        out = stats_tool.compute(store, "ranking", key, **self.ARGS)
        rows = [dict(zip(out["columns"], r)) for r in out["rows"]]
        self.assertEqual([r["grupo"] for r in rows], ["Centro", "Norte", "Sur", "Chica"])
        self.assertEqual([r["puesto"] for r in rows], [1, 2, 3, None])
        self.assertEqual(rows[0]["separado_del_siguiente"], "no")   # 40-60 vs 30-50 se superponen: empate técnico
        self.assertEqual(rows[1]["separado_del_siguiente"], "si")   # 30-50 vs 5-15: separados
        self.assertEqual(rows[3]["base_suficiente"], "no")          # 20 leídas < 60
        self.assertEqual(rows[0]["punto_medio_pct"], 50.0)
        # contra QUIÉN se distingue cada uno (no sólo el vecino): Centro (40-60) supera a Sur (5-15) aunque no a Norte (30-50)
        self.assertEqual(rows[0]["claramente_por_encima_de"], "Sur")
        self.assertEqual(rows[2]["claramente_por_debajo_de"], "Centro; Norte")
        self.assertEqual(rows[1]["claramente_por_encima_de"], "Sur")
        self.assertEqual(rows[3]["claramente_por_encima_de"], "sin_puesto")

    def test_a_distant_group_is_not_declared_above_one_it_overlaps(self) -> None:
        # Caso real (Farma 24, cuotas sin interés): Dalia 12,5-39,6 supera a Central 0,2-11,2 pero NO a Kaplan 1,6-17,4.
        store, key = store_with(extract_result([("Dalia", 100, 12.5, 39.6), ("Kaplan", 100, 1.6, 17.4),
                                                ("Vieytes", 100, 0.6, 15.0), ("Central", 100, 0.2, 11.2)]))
        out = stats_tool.compute(store, "ranking", key, **self.ARGS)
        dalia = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(dalia["grupo"], "Dalia")
        self.assertEqual(dalia["claramente_por_encima_de"], "Central")
        self.assertNotIn("Kaplan", dalia["claramente_por_encima_de"])

    def test_compare_two_periods_with_interval_arithmetic(self) -> None:
        store, key = store_with(extract_result([("2026-08", 120, 10.0, 20.0), ("2026-09", 110, 30.0, 45.0)]))
        out = stats_tool.compute(store, "comparar", key, grupo_a="2026-09", grupo_b="2026-08", **self.ARGS)
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(row["rango_dif_min_pct"], 10.0)   # 30 - 20
        self.assertEqual(row["rango_dif_max_pct"], 35.0)   # 45 - 10
        self.assertEqual(row["diferencia_distinguible"], "si")
        self.assertEqual(row["aviso_base"], "ok")

    def test_compare_overlapping_ranges_is_not_distinguishable(self) -> None:
        store, key = store_with(extract_result([("A", 100, 20.0, 40.0), ("B", 100, 30.0, 50.0)]))
        out = stats_tool.compute(store, "comparar", key, grupo_a="A", grupo_b="B", **self.ARGS)
        self.assertEqual(dict(zip(out["columns"], out["rows"][0]))["diferencia_distinguible"], "no")

    def test_proportion_reports_the_range_width(self) -> None:
        store, key = store_with(extract_result([("A", 100, 20.0, 40.0)]))
        out = stats_tool.compute(store, "proporcion", key, **self.ARGS)
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual((row["rango_min_pct"], row["rango_max_pct"], row["ancho_pct"]), (20.0, 40.0, 20.0))

    def test_the_summary_and_unread_rows_of_a_real_breakdown_are_skipped_and_reported(self) -> None:
        # Bug real (2026-10-05, dos corridas en vivo en Farma 24): compute_stats fallaba con "(otros 10 grupos sin leer)" por tener
        # vacíos el mínimo y el máximo, y la respuesta salía sin la herramienta.
        store, key = store_with(extract_result([("Centro", 100, 40.0, 60.0), ("Sur", 100, 5.0, 15.0)]))
        out = stats_tool.compute(store, "ranking", key, **self.ARGS)
        self.assertEqual([r[1] for r in out["rows"]], ["Centro", "Sur"])
        self.assertIn("(suma de los grupos incluidos)", out["notas"][-1])
        self.assertIn("(otros 4 grupos sin leer)", out["notas"][-1])

    def test_a_result_with_only_non_comparable_rows_is_an_error(self) -> None:
        store, key = store_with(extract_result([]))
        with self.assertRaises(ValueError) as ctx:
            stats_tool.compute(store, "ranking", key, **self.ARGS)
        self.assertIn("no tiene filas comparables", str(ctx.exception))

    def test_the_point_estimate_orders_the_groups_when_every_row_has_one(self) -> None:
        # A: rango 10-50 (punto medio 30) con estimación 15; B: rango 20-30 (punto medio 25) con estimación 25.
        store, key = store_with(extract_result([("A", 100, 10.0, 50.0, 15.0), ("B", 100, 20.0, 30.0, 25.0)]))
        by_mid = stats_tool.compute(store, "ranking", key, **self.ARGS)
        by_est = stats_tool.compute(store, "ranking", key, columna_estimado="pct_estimado", **self.ARGS)
        self.assertEqual([r[1] for r in by_mid["rows"]], ["A", "B"])
        self.assertEqual([r[1] for r in by_est["rows"]], ["B", "A"])
        self.assertIn("estimado_pct", by_est["columns"])
        self.assertIn("punto_medio_pct", by_mid["columns"])
        self.assertEqual(by_est["rows"][0][2], 25.0)

    def test_without_an_estimate_in_some_row_it_falls_back_to_the_midpoint_for_all(self) -> None:
        store, key = store_with(extract_result([("A", 100, 10.0, 50.0, 15.0), ("B", 100, 20.0, 30.0)]))
        store[key]["rows"][1][5] = None  # B sin estimación (p. ej. sin calibración)
        out = stats_tool.compute(store, "ranking", key, columna_estimado="pct_estimado", **self.ARGS)
        self.assertIn("punto_medio_pct", out["columns"])
        self.assertNotIn("estimado_pct", out["columns"])

    def test_an_estimate_outside_its_range_is_rejected(self) -> None:
        store, key = store_with(extract_result([("A", 100, 10.0, 20.0, 50.0)]))
        with self.assertRaises(ValueError):
            stats_tool.compute(store, "proporcion", key, columna_estimado="pct_estimado", **self.ARGS)

    def test_compare_uses_the_estimates_for_the_difference(self) -> None:
        store, key = store_with(extract_result([("2026-08", 120, 10.0, 20.0, 12.0), ("2026-09", 110, 30.0, 45.0, 38.0)]))
        out = stats_tool.compute(store, "comparar", key, grupo_a="2026-09", grupo_b="2026-08", columna_estimado="pct_estimado", **self.ARGS)
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(row["diferencia_pct_puntos"], 26.0)
        self.assertEqual((row["rango_dif_min_pct"], row["rango_dif_max_pct"]), (10.0, 35.0))

    def test_literal_mode_without_maximum_is_rejected(self) -> None:
        payload = extract_result([("A", 100, 20.0, None)])
        store, key = store_with(payload)
        with self.assertRaises(ValueError) as ctx:
            stats_tool.compute(store, "ranking", key, **self.ARGS)
        self.assertIn("piso", str(ctx.exception))

    def test_rejects_an_invalid_range(self) -> None:
        store, key = store_with(extract_result([("A", 100, 50.0, 20.0)]))
        with self.assertRaises(ValueError):
            stats_tool.compute(store, "proporcion", key, **self.ARGS)

    def test_range_mode_needs_both_columns(self) -> None:
        store, key = store_with(extract_result([("A", 100, 20.0, 40.0)]))
        with self.assertRaises(ValueError):
            stats_tool.compute(store, "proporcion", key, columna_min="pct_minimo")

    def test_figures_are_verifiable_without_any_sql_result(self) -> None:
        store, key = store_with(extract_result([("Centro", 100, 40.0, 60.0), ("Sur", 100, 5.0, 15.0)]))
        out = stats_tool.compute(store, "ranking", key, **self.ARGS)
        only_tools = {}
        add_result(only_tools, extract_result([("Centro", 100, 40.0, 60.0), ("Sur", 100, 5.0, 15.0)]))
        add_result(only_tools, out)
        ok = verify_answer("Centro está entre 40% y 60% (100 conversaciones evaluadas) y Sur entre 5% y 15%.", only_tools)
        self.assertEqual(ok.errors, [])

    def test_loop_chains_extract_insight_and_compute_stats_without_sql(self) -> None:
        extract_json = json.dumps(extract_result([("Centro", 100, 40.0, 60.0, 50.0), ("Sur", 100, 5.0, 15.0, 10.0)]))
        extract_id = evidence_id(json.loads(extract_json))
        chat = FakeChat([
            FakeResponse("", function_calls=[FakeFunctionCall("extract_insight", {
                "pregunta": "¿ofrece cuotas?", "criterio_si": "sí", "criterio_no": "no", "desglosar_por": "tienda"})]),
            FakeResponse("", function_calls=[FakeFunctionCall("compute_stats", {
                "operacion": "ranking", "fuente_id": extract_id, "columna_min": "pct_minimo", "columna_max": "pct_maximo",
                "columna_total": "conversaciones_leidas", "columna_grupo": "grupo", "columna_estimado": "pct_estimado"})]),
            FakeResponse("Centro, entre 40% y 60%, está por encima de Sur, entre 5% y 15%, sobre 100 conversaciones evaluadas cada una."),
        ])
        log: list[dict] = []
        with patch.dict(vi_agent.TOOL_FUNCTIONS, {"extract_insight": lambda **kwargs: extract_json}):
            answer = vi_agent.run_tool_loop(chat, "¿En qué tienda ofrecen más cuotas?", tool_calls_log=log)
        self.assertEqual([c["name"] for c in log], ["extract_insight", "compute_stats"])
        self.assertIsNone(log[1]["error"])
        computed = json.loads(log[1]["result"])
        first = dict(zip(computed["columns"], computed["rows"][0]))
        self.assertEqual((first["grupo"], first["separado_del_siguiente"], first["claramente_por_encima_de"]),
                         ("Centro", "si", "Sur"))
        self.assertIn("Centro", answer)


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
