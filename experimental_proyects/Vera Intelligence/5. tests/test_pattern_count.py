"""Conteo de casos de un patrón (2026-10-05): n de m entre las conversaciones MÁS PARECIDAS, sin dar nunca una frecuencia."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import insight_extraction as ie  # noqa: E402
import vector_search  # noqa: E402
import vi_agent  # noqa: E402
from answer_verification import add_result, evidence_id, verify_answer  # noqa: E402
from client_config import load_client_config  # noqa: E402
from test_vi_agent import FakeChat, FakeFunctionCall, FakeResponse  # noqa: E402

PATRON_RARO = "no pregunta si el cliente tiene tarjeta de Banco Provincia antes de cobrar"
PATRON_DENSO = "menciona que hay promociones vigentes sin ofrecer nada concreto al cliente"


def make_repo(total: int = 1200, exhausted: bool = False, rank_calls: list | None = None,
              rare_top: int = 40, confirm_dense: float = 0.5):
    """Repositorio con dobles: patrón raro (sólo en las `rare_top` más parecidas) y patrón denso (1 de cada 2)."""
    calls = rank_calls if rank_calls is not None else []

    def rank(query, **kwargs):
        calls.append((query, kwargs))
        return [{"recording_id": f"r{i}", "tienda": f"T{i % 3}", "fecha": "2026-08-01", "distancia": i / 10000}
                for i in range(total)], exhausted

    def text(ids):
        return {rid: f"transcripcion {rid} " + "x" * 300 for rid in ids}

    def card(patron):
        if "falla" in patron:
            raise ValueError("sin ficha")
        return {"pregunta": f"¿Ocurre: {patron[:40]}?", "criterio_si": "sí ocurre", "criterio_no": "no ocurre"}

    def read(text_, spec, _key):
        index = int(text_.split()[1][1:])
        return (1.0 if index < rare_top else 0.0) if "tarjeta" in spec["pregunta"] else (1.0 if index % 2 == 0 else 0.0)

    seen: dict[str, int] = {}

    def verify(spec, text_):
        if "tarjeta" in spec["pregunta"]:
            return {"respuesta": True, "evidencia_ok": True, "resumen": "no preguntó por la tarjeta"}
        seen[text_] = seen.get(text_, 0) + 1
        ok = len(seen) % 2 == 0 if confirm_dense == 0.5 else True
        return {"respuesta": True, "evidencia_ok": ok, "resumen": "mencionó promociones"}

    return ie.InsightExtractionRepository(None, read_fn=read, verify_fn=verify, card_fn=card, rank_fn=rank, text_fn=text)


class FloorTests(unittest.TestCase):
    def test_floor_is_exact_when_every_positive_was_verified(self) -> None:
        self.assertEqual(ie.piso_verificado(20, 20, 17), 17)

    def test_floor_uses_the_lower_bound_when_only_part_was_verified(self) -> None:
        floor = ie.piso_verificado(200, 30, 30)  # 30 de 30 confirmadas: cota inferior de Wilson ~0,886 -> 177
        self.assertEqual(floor, 177)
        self.assertGreaterEqual(floor, 30)

    def test_floor_never_goes_below_the_confirmed_ones(self) -> None:
        self.assertGreaterEqual(ie.piso_verificado(500, 30, 3), 3)

    def test_zero_without_positives_or_without_verification(self) -> None:
        self.assertEqual(ie.piso_verificado(0, 0, 0), 0)
        self.assertEqual(ie.piso_verificado(10, 0, 0), 0)


class CountPatternsTests(unittest.TestCase):
    def run_count(self, repo, patrones, **kwargs):
        raw = repo.count_patterns("el vendedor ofrece pagar con tarjeta", patrones, **kwargs)
        payload = json.loads(raw)
        rows = [dict(zip(payload["columns"], r)) for r in payload["rows"]]
        return payload, rows

    def test_rare_pattern_reads_the_base_block_and_reports_a_verified_floor(self) -> None:
        payload, rows = self.run_count(make_repo(), [PATRON_RARO])
        total = next(r for r in rows if r["tramo"] == "total")
        self.assertEqual(total["conversaciones_leidas"], ie.PATTERN_M_BASE)
        self.assertEqual(total["marcadas_por_el_lector"], 40)
        self.assertEqual((total["verificadas_con_cita"], total["confirmadas_con_cita"]), (30, 30))
        self.assertEqual(total["minimo_con_el_patron"], ie.piso_verificado(40, 30, 30))
        self.assertEqual(total["tope_alcanzado"], "no")
        self.assertEqual(payload["tipo_resultado"], "soporte_de_patron")
        self.assertIn("ESTO NO ES UNA FRECUENCIA", payload["interpretacion"])

    def test_tramos_show_where_the_pattern_concentrates(self) -> None:
        _, rows = self.run_count(make_repo(), [PATRON_RARO])
        by_tramo = {r["tramo"]: r for r in rows}
        self.assertEqual(by_tramo["1-100"]["marcadas_por_el_lector"], 40)
        self.assertEqual(by_tramo["101-300"]["marcadas_por_el_lector"], 0)
        self.assertEqual(by_tramo["301-1000"]["conversaciones_leidas"], 700)
        self.assertNotIn("1001-2000", by_tramo)

    def test_dense_pattern_extends_the_reading_and_flags_the_cap(self) -> None:
        payload, rows = self.run_count(make_repo(total=1200), [PATRON_DENSO])
        total = next(r for r in rows if r["tramo"] == "total")
        self.assertEqual(total["conversaciones_leidas"], 1200)           # amplió más allá de 1.000
        self.assertEqual(total["marcadas_por_el_lector"], 600)
        self.assertEqual(total["tope_alcanzado"], "si")                   # el final seguía denso y hay más población
        self.assertEqual(next(r for r in rows if r["tramo"] == "1001-2000")["conversaciones_leidas"], 200)

    def test_a_dense_pattern_in_an_exhausted_population_is_not_a_cap(self) -> None:
        _, rows = self.run_count(make_repo(total=1200, exhausted=True), [PATRON_DENSO])
        total = next(r for r in rows if r["tramo"] == "total")
        self.assertEqual(total["conversaciones_leidas"], 1200)            # se leyó toda la población
        self.assertEqual(total["tope_alcanzado"], "no")

    def test_only_dense_patterns_are_extended(self) -> None:
        _, rows = self.run_count(make_repo(total=1200), [PATRON_RARO, PATRON_DENSO])
        totals = {r["patron"]: r for r in rows if r["tramo"] == "total"}
        self.assertEqual(totals["P1"]["conversaciones_leidas"], 1000)
        self.assertEqual(totals["P2"]["conversaciones_leidas"], 1200)

    def test_one_ranking_is_shared_by_all_the_patterns(self) -> None:
        calls: list = []
        self.run_count(make_repo(rank_calls=calls), [PATRON_RARO, PATRON_DENSO],
                       store_name="Roca", date_from="2026-01-01", campo_estructurado="campo", valor_estructurado="valor")
        self.assertEqual(len(calls), 1)
        query, kwargs = calls[0]
        self.assertEqual(query, "el vendedor ofrece pagar con tarjeta")
        self.assertEqual((kwargs["store_name"], kwargs["date_from"], kwargs["limit"]), ("Roca", "2026-01-01", ie.PATTERN_M_MAX))
        self.assertEqual((kwargs["campo_estructurado"], kwargs["valor_estructurado"]), ("campo", "valor"))

    def test_same_call_twice_is_served_from_memory(self) -> None:
        calls: list = []
        repo = make_repo(rank_calls=calls)
        first = repo.count_patterns("el vendedor ofrece pagar con tarjeta", [PATRON_RARO])
        second = repo.count_patterns("el vendedor ofrece pagar con tarjeta", [PATRON_RARO])
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_examples_carry_no_quotes_or_identifiers(self) -> None:
        payload, _ = self.run_count(make_repo(), [PATRON_RARO])
        for example in payload["ejemplos"]:
            self.assertEqual(set(example), {"patron", "tienda", "fecha", "situacion"})

    def test_unmeasurable_pattern_is_reported_and_does_not_break_the_others(self) -> None:
        payload, rows = self.run_count(make_repo(), [PATRON_RARO, "este patron falla al armar la ficha de lectura"])
        self.assertEqual(payload["no_medidos"], ["este patron falla al armar la ficha de lectura"])
        self.assertEqual({r["patron"] for r in rows}, {"P1"})

    def test_all_patterns_unmeasurable_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            make_repo().count_patterns("el vendedor ofrece pagar con tarjeta", ["este patron falla al armar la ficha uno"])

    def test_input_validation(self) -> None:
        repo = make_repo()
        with self.assertRaises(ValueError):
            repo.count_patterns("", [PATRON_RARO])
        with self.assertRaises(ValueError):
            repo.count_patterns("consulta valida de prueba", [])
        with self.assertRaises(ValueError):
            repo.count_patterns("consulta valida de prueba", ["corto"])
        with self.assertRaises(ValueError):
            repo.count_patterns("consulta valida de prueba", [f"patron numero {i} con texto suficiente" for i in range(4)])
        with self.assertRaises(ValueError):
            repo.count_patterns("consulta valida de prueba", [PATRON_RARO], date_from="01/01/2026")

    def test_unavailable_without_a_ranking_function(self) -> None:
        repo = ie.InsightExtractionRepository(None)
        with self.assertRaises(RuntimeError):
            repo.count_patterns("consulta valida de prueba", [PATRON_RARO])


class NoPercentageGuardTests(unittest.TestCase):
    def store(self) -> tuple[dict, str]:
        payload = json.loads(make_repo().count_patterns("el vendedor ofrece pagar con tarjeta", [PATRON_RARO]))
        store: dict = {}
        key = add_result(store, payload)
        return store, key

    def test_identity_of_a_count_is_accepted_but_a_percentage_is_rejected(self) -> None:
        store, key = self.store()
        payload = store[key]
        row = next(i for i, r in enumerate(payload["rows"]) if r[1] == "total")
        ref = lambda col: {"id": key, "row": row, "column": col}  # noqa: E731
        ok = verify_answer(
            "Se revisaron 1000 conversaciones: el lector marcó 40.\n```vera-evidence\n"
            + json.dumps([{"text": "40", "operation": "identity", "sources": [ref("marcadas_por_el_lector")]},
                          {"text": "1000", "operation": "identity", "sources": [ref("conversaciones_leidas")]}])
            + "\n```", store)
        self.assertEqual(ok.errors, [])
        bad = verify_answer(
            "El patrón aparece en el 4% de las conversaciones.\n```vera-evidence\n"
            + json.dumps([{"text": "4%", "operation": "percentage",
                           "sources": [ref("marcadas_por_el_lector"), ref("conversaciones_leidas")]}]) + "\n```", store)
        self.assertTrue(any("no admite porcentajes" in e for e in bad.errors), bad.errors)

    def test_a_percentage_written_without_evidence_is_also_unsupported(self) -> None:
        store, _ = self.store()
        result = verify_answer("El patrón aparece en el 4% de las conversaciones revisadas.", store)
        self.assertTrue(any("cifra sin respaldo" in e for e in result.errors))


class AgentWiringTests(unittest.TestCase):
    def tearDown(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")

    def test_exposed_only_with_extraction_and_vector_search(self) -> None:
        with patch.dict(os.environ, {"VI_INSIGHT_EXTRACTION": ""}):
            vi_agent.configure_client("mens_fashion_alto")  # búsqueda sí, extracción no
            names = {getattr(t, "__name__", None) for t in vi_agent._build_tools_list()}
            self.assertNotIn("count_pattern_cases", names)
            self.assertNotIn("count_pattern_cases", vi_agent._build_extra_tools_section())
        with patch.dict(os.environ, {"VI_INSIGHT_EXTRACTION": "mens_fashion"}):
            vi_agent.configure_client("mens_fashion_alto")
            if vi_agent._INSIGHT_EXTRACTION_REPOSITORY is None:
                self.skipTest("el client_id de mens_fashion_alto no coincide con la variable de prueba")
            names = {getattr(t, "__name__", None) for t in vi_agent._build_tools_list()}
            self.assertIn("count_pattern_cases", names)
            section = vi_agent._build_extra_tools_section()
            self.assertIn("count_pattern_cases", section)
            self.assertIn("PROHIBIDO", section)

    def test_the_prompt_explains_conditional_behaviors_for_extract_insight(self) -> None:
        # Tigo a32 (2026-10-05): un campo que sólo aplica a clientes de hogar se lee sobre todas las conversaciones y no es
        # comparable con el SQL; el agente debe acotar la población o decir que el % incluye las que no aplican.
        with patch.dict(os.environ, {"VI_INSIGHT_EXTRACTION": "mens_fashion"}):
            vi_agent.configure_client("mens_fashion_alto")
            if vi_agent._INSIGHT_EXTRACTION_REPOSITORY is None:
                self.skipTest("el client_id de mens_fashion_alto no coincide con la variable de prueba")
            section = vi_agent._build_extra_tools_section()
        self.assertIn("CONDUCTAS CONDICIONALES", section)
        self.assertIn("incluidas las que no aplican", section)

    def test_the_extraction_repository_receives_the_vector_ranking(self) -> None:
        client = load_client_config("mens_fashion_alto")
        with patch.dict(os.environ, {"VI_INSIGHT_EXTRACTION": client.client_id}):
            vi_agent.configure_client("mens_fashion_alto")
            repo = vi_agent._INSIGHT_EXTRACTION_REPOSITORY
            self.assertIsNotNone(repo)
            self.assertEqual(repo._rank, vi_agent._VECTOR_SEARCH_REPOSITORY.rank_conversations)

    def test_calling_the_tool_without_configuration_raises(self) -> None:
        with patch.dict(os.environ, {"VI_INSIGHT_EXTRACTION": ""}):
            vi_agent.configure_client("mens_fashion_alto")
            with self.assertRaises(RuntimeError):
                vi_agent.count_pattern_cases("consulta valida de prueba", [PATRON_RARO])

    def test_loop_registers_the_result_as_evidence(self) -> None:
        result_json = make_repo().count_patterns("el vendedor ofrece pagar con tarjeta", [PATRON_RARO])
        chat = FakeChat([
            FakeResponse("", function_calls=[FakeFunctionCall("count_pattern_cases", {
                "query": "el vendedor ofrece pagar con tarjeta", "patrones": [PATRON_RARO]})]),
            FakeResponse("De las 1000 conversaciones revisadas (las más parecidas, no una muestra), el lector marcó 40."),
        ])
        log: list[dict] = []
        with patch.dict(vi_agent.TOOL_FUNCTIONS, {"count_pattern_cases": lambda **kwargs: result_json}):
            answer = vi_agent.run_tool_loop(chat, "¿Qué patrones hay al ofrecer pagar con tarjeta?", tool_calls_log=log)
        self.assertEqual([c["name"] for c in log], ["count_pattern_cases"])
        self.assertIn("40", answer)
        self.assertNotIn("sin respaldo", answer)


class RankConversationsTests(unittest.TestCase):
    def repo(self):
        client = load_client_config("mens_fashion_alto")
        return vector_search.VectorSearchRepository(client)

    def run_rank(self, repo, rows, **kwargs):
        """Ejecuta rank_conversations con doble de SQL: devuelve (resultado, lista de (sql, params) ejecutados)."""
        executed: list[tuple[str, tuple]] = []

        def fake_execute(sql, params, **kw):
            executed.append((sql, params))
            return ([("STORE1",)] if sql.startswith("SELECT DISTINCT") else rows), 1.0

        with patch.object(vector_search, "_embed_query", return_value=[0.0] * vector_search.EMBEDDING_DIMENSION), patch.dict(
                os.environ, {"VERA_AI_API_KEY": "test-key"}), patch.object(vector_search, "_execute_retrieval_sql", side_effect=fake_execute):
            return repo.rank_conversations("texto de la consulta", **kwargs), executed

    @staticmethod
    def row(i: int, n_inner: int, conversation_id=None):
        return (f"r{i}", "Tienda", None, conversation_id or f"c{i}", i / 100, n_inner)

    def test_returns_ranked_conversations_deduplicated_and_flags_exhaustion(self) -> None:
        rows = [self.row(0, 5), self.row(1, 5), self.row(1, 5, conversation_id="c0"), self.row(2, 5)]  # una repetida
        (ranked, exhausted), _ = self.run_rank(self.repo(), rows, limit=500)
        self.assertEqual([r["recording_id"] for r in ranked], ["r0", "r1", "r2"])
        self.assertTrue(exhausted)  # el ordenamiento interno trajo 5 fragmentos de 1.500 pedidos: no quedaban más

    def test_a_cut_by_the_chunk_limit_is_not_reported_as_exhausted(self) -> None:
        (_, exhausted), _ = self.run_rank(self.repo(), [self.row(0, 1500)], limit=500)
        self.assertFalse(exhausted)

    def test_a_cut_by_the_conversation_limit_is_not_reported_as_exhausted(self) -> None:
        rows = [self.row(i, 5) for i in range(3)]
        (ranked, exhausted), _ = self.run_rank(self.repo(), rows, limit=2)
        self.assertEqual(len(ranked), 2)
        self.assertFalse(exhausted)

    def test_fast_sql_orders_by_tenant_first_and_filters_afterwards(self) -> None:
        tenant = load_client_config("mens_fashion_alto").tenant
        _, executed = self.run_rank(self.repo(), [self.row(0, 1)], limit=1000)
        sql, params = executed[-1]
        inner = sql.split("SELECT top.recording_id")[0]
        self.assertIn("ce.seller_id = %s", inner)
        self.assertNotIn("JOIN", inner)          # los joins van DESPUÉS de ordenar
        self.assertIn("MATERIALIZED", inner)
        self.assertIn("conv.useful_for_analysis IS TRUE", sql)
        self.assertIn("r.seller_id = %s", sql)   # el tenant se vuelve a exigir afuera
        self.assertEqual(params[-1], tenant)
        chunk_limit = min(1000 * vector_search._RANKING_CHUNKS_PER_CONVERSATION, vector_search._RANKING_MAX_CHUNKS)
        self.assertEqual(params[-2], chunk_limit)

    def test_store_and_employee_filters_are_resolved_inside_and_rechecked_outside(self) -> None:
        _, executed = self.run_rank(self.repo(), [self.row(0, 1)], limit=100, store_name="Roca", employee_name="Ana",
                                    date_from="2026-01-01", date_to="2026-02-01")
        resolves = [e for e in executed if e[0].startswith("SELECT DISTINCT")]
        self.assertEqual(len(resolves), 2)  # tiendas y vendedores
        sql, params = executed[-1]
        inner, outer = sql.split("SELECT top.recording_id")
        for fragment in ("ce.store_id = ANY(%s)", "ce.employee_id = ANY(%s)", "ce.conversation_started_at::date >= %s::date",
                         "ce.conversation_started_at::date <= %s::date"):
            self.assertIn(fragment, inner)
        for fragment in ("r.store_name ILIKE %s", "r.employee_full_name ILIKE %s", "r.started_at::date >= %s::date"):
            self.assertIn(fragment, outer)
        self.assertIn("%Roca%", params)
        self.assertIn("%Ana%", params)

    def test_an_unknown_store_returns_an_empty_exhausted_ranking(self) -> None:
        repo = self.repo()
        with patch.object(vector_search, "_embed_query", return_value=[0.0] * vector_search.EMBEDDING_DIMENSION), patch.dict(
                os.environ, {"VERA_AI_API_KEY": "test-key"}), patch.object(
                vector_search, "_execute_retrieval_sql", return_value=([], 1.0)):
            result = repo.rank_conversations("texto de la consulta", store_name="No existe")
        self.assertEqual(result, ([], True))

    def test_requires_both_structured_filters_or_none(self) -> None:
        with self.assertRaises(ValueError):
            self.repo().rank_conversations("texto de la consulta", campo_estructurado="algo")

    def test_rejects_an_unknown_structured_field(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.repo().rank_conversations("texto de la consulta", campo_estructurado="campo_que_no_existe", valor_estructurado="x")
        self.assertIn("campo_estructurado inválido", str(ctx.exception))

    def test_rejects_an_empty_query_and_a_bad_date(self) -> None:
        with self.assertRaises(ValueError):
            self.repo().rank_conversations("  ")
        with self.assertRaises(ValueError):
            self.repo().rank_conversations("texto de la consulta", date_from="ayer")

    def test_slow_path_for_structured_filters_keeps_the_full_query_without_transcripts(self) -> None:
        repo = self.repo()
        captured: dict = {}

        def fake_execute(sql, params, **kwargs):
            captured["sql"], captured["params"] = sql, params
            return [], 1.0

        with patch.object(vector_search, "_execute_retrieval_sql", side_effect=fake_execute):
            repo._retrieve(vector_literal="[0]", top_k=1000, store_name=None, employee_name=None, employee_exact=None,
                           date_from=None, date_to=None, criterio=None, resultado_filtro=None, performance_source=None,
                           ranking_only=True)
        sql = captured["sql"]
        self.assertIn("r.seller_id = %s", sql)
        self.assertIn("conv.useful_for_analysis IS TRUE", sql)
        self.assertIn("NULL AS transcript", sql)
        self.assertNotIn("JOIN raw_v2.conversations_raw cr ON", sql)

    def test_normal_search_sql_is_unchanged(self) -> None:
        repo = self.repo()
        captured: dict = {}

        def fake_execute(sql, params, **kwargs):
            captured["sql"] = sql
            return [], 1.0

        with patch.object(vector_search, "_execute_retrieval_sql", side_effect=fake_execute):
            repo._retrieve(vector_literal="[0]", top_k=5, store_name=None, employee_name=None, employee_exact=None,
                           date_from=None, date_to=None, criterio=None, resultado_filtro=None, performance_source=None)
        self.assertIn("JOIN raw_v2.conversations_raw cr ON", captured["sql"])
        self.assertNotIn("NULL AS transcript", captured["sql"])


if __name__ == "__main__":
    unittest.main()
