"""Ángulos extra de una búsqueda amplia (2026-10-05): `queries_extra` en search(), en el planificador y en la herramienta."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import question_planner  # noqa: E402
import vector_search  # noqa: E402
import vi_agent  # noqa: E402
from client_config import load_client_config  # noqa: E402

ANGULO_1 = "El vendedor le ofrece al cliente tomarle la presión arterial mientras espera su pedido en el mostrador."
ANGULO_2 = "Al finalizar la compra el empleado informa que cuentan con envío a domicilio y pedidos por WhatsApp."
ANGULO_3 = "El cliente consulta si aplican vacunas en la farmacia y el farmacéutico explica días y requisitos."


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Connection:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def row(i: int, distancia: float):
    return (f"rid{i}", 0, "Tienda", f"Vendedor {i}", None, "hola", f"conv{i}", None, distancia)


class CleanExtraQueriesTests(unittest.TestCase):
    def test_keeps_valid_unique_queries_up_to_the_limit(self) -> None:
        out = vector_search._clean_extra_queries([ANGULO_1, ANGULO_2, ANGULO_3, "una cuarta formulación suficientemente larga"], [])
        self.assertEqual(out, [ANGULO_1, ANGULO_2, ANGULO_3])

    def test_drops_empty_short_long_repeated_and_equal_to_the_main_queries(self) -> None:
        principal = "consulta principal de la búsqueda amplia"
        out = vector_search._clean_extra_queries(
            ["", "corta", "x" * 500, ANGULO_1, ANGULO_1.upper(), principal.upper(), "alternativa ya presente en la búsqueda"],
            [principal, "alternativa ya presente en la búsqueda"])
        self.assertEqual(out, [ANGULO_1])

    def test_none_and_empty_mean_no_angles(self) -> None:
        for empty in (None, "", []):
            self.assertEqual(vector_search._clean_extra_queries(empty, []), [])

    def test_a_non_list_is_an_explicit_error(self) -> None:
        with self.assertRaises(ValueError):
            vector_search._clean_extra_queries("una sola formulación en texto plano", [])


class SearchWithAnglesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            patch.object(vector_search, "VECTOR_SEARCH_LOG_PATH", Path(tmp.name) / "log.jsonl"),
            patch.object(vector_search, "_MIN_CONVERSATION_WORDS", 0),
            patch.object(vector_search, "_judge_relevance", side_effect=lambda q, r, **k: [True] * len(r)),
            patch.object(vector_search, "postgres_connection_kwargs", return_value={}),
            patch.dict(os.environ, {"VERA_AI_API_KEY": "test-key"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo = vector_search.VectorSearchRepository(load_client_config("mens_fashion_alto"))
        self.embeds: list[str] = []

        def fake_embed(query, api_key, **kwargs):
            self.embeds.append(query)
            return [0.0] * vector_search.EMBEDDING_DIMENSION

        embed = patch.object(vector_search, "_embed_query", side_effect=fake_embed)
        embed.start()
        self.addCleanup(embed.stop)

    def run_search(self, primary_rows, secondary_rows=None, ephemeral_rows=(), **kwargs):
        primary, secondary = _Connection(primary_rows), _Connection(secondary_rows or [])
        ephemerals = [_Connection(r) for r in ephemeral_rows]
        queue = list(ephemerals)
        lock = threading.Lock()

        def connect(**_):
            with lock:
                return queue.pop(0)

        with patch.object(vector_search, "_get_reusable_connection", return_value=primary), patch.object(
                vector_search, "_get_reusable_connection_secondary", return_value=secondary), patch.object(
                vector_search.psycopg, "connect", side_effect=connect):
            payload = json.loads(self.repo.search("consulta original de la búsqueda", top_k=8, **kwargs))
        return payload, primary, secondary, ephemerals

    def selects(self, connection) -> int:
        return len([sql for sql, _ in connection.cursor_obj.executed if "SELECT" in sql])

    def test_each_angle_runs_in_its_own_connection_and_is_embedded(self) -> None:
        payload, primary, secondary, ephemerals = self.run_search(
            [row(1, 0.20)], [row(2, 0.21)], [[row(3, 0.22)], [row(4, 0.23)]],
            query_alternativa="alternativa de la misma intención", queries_extra=[ANGULO_1, ANGULO_2])
        self.assertEqual(sorted(self.embeds), sorted(["consulta original de la búsqueda", "alternativa de la misma intención",
                                                      ANGULO_1, ANGULO_2]))
        self.assertEqual((self.selects(primary), self.selects(secondary)), (1, 1))
        self.assertEqual([self.selects(c) for c in ephemerals], [1, 1])
        self.assertTrue(all(c.closed for c in ephemerals))  # las conexiones de un solo uso se cierran siempre
        self.assertEqual({r["conversation_id"] for r in payload["resultados"]}, {"conv1", "conv2", "conv3", "conv4"})

    def test_angles_without_an_alternative_do_not_touch_the_secondary_connection(self) -> None:
        with patch.object(vector_search, "_get_reusable_connection_secondary", side_effect=AssertionError("no debe usarse")):
            primary, ephemerals = _Connection([row(1, 0.2)]), [_Connection([row(2, 0.2)])]
            with patch.object(vector_search, "_get_reusable_connection", return_value=primary), patch.object(
                    vector_search.psycopg, "connect", side_effect=lambda **_: ephemerals.pop(0)):
                payload = json.loads(self.repo.search("consulta original de la búsqueda", queries_extra=[ANGULO_1]))
        self.assertEqual({r["conversation_id"] for r in payload["resultados"]}, {"conv1", "conv2"})

    def test_a_conversation_found_by_several_angles_is_ranked_first_and_not_repeated(self) -> None:
        shared = row(9, 0.21)
        payload, *_ = self.run_search([row(1, 0.20), shared], [], [[shared, row(3, 0.22)], [shared]],
                                      queries_extra=[ANGULO_1, ANGULO_2])
        ids = [r["conversation_id"] for r in payload["resultados"]]
        self.assertEqual(ids[0], "conv9")
        self.assertEqual(len(ids), len(set(ids)))

    def test_extras_equal_to_the_query_are_ignored_and_the_search_stays_single(self) -> None:
        payload, primary, secondary, ephemerals = self.run_search(
            [row(1, 0.2)], queries_extra=["CONSULTA ORIGINAL DE LA BÚSQUEDA"])
        self.assertEqual(self.embeds, ["consulta original de la búsqueda"])
        self.assertEqual((self.selects(primary), self.selects(secondary)), (1, 0))

    def test_without_angles_the_search_is_unchanged(self) -> None:
        _, primary, secondary, _ = self.run_search([row(1, 0.2)])
        self.assertEqual(self.embeds, ["consulta original de la búsqueda"])
        self.assertEqual((self.selects(primary), self.selects(secondary)), (1, 0))

    def test_a_failing_angle_is_skipped_and_the_rest_of_the_search_survives(self) -> None:
        primary = _Connection([row(1, 0.2)])
        good = _Connection([row(2, 0.21)])
        calls = []

        def connect(**_):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("sin conexiones")
            return good

        with patch.object(vector_search, "_get_reusable_connection", return_value=primary), patch.object(
                vector_search.psycopg, "connect", side_effect=connect):
            payload = json.loads(self.repo.search("consulta original de la búsqueda", queries_extra=[ANGULO_1, ANGULO_2]))
        self.assertEqual({r["conversation_id"] for r in payload["resultados"]}, {"conv1", "conv2"})

    def test_a_failing_alternative_still_fails_the_search(self) -> None:
        primary = _Connection([row(1, 0.2)])

        class Broken(_Connection):
            def cursor(self):
                raise RuntimeError("falla la alternativa")

        with patch.object(vector_search, "_get_reusable_connection", return_value=primary), patch.object(
                vector_search, "_get_reusable_connection_secondary", return_value=Broken([])):
            with self.assertRaises(RuntimeError):
                self.repo.search("consulta original de la búsqueda", query_alternativa="alternativa de la misma intención")


class PlannerAnglesTests(unittest.TestCase):
    def ground(self, spec):
        return question_planner.ground_busqueda({"usar_busqueda": True, "busqueda": spec})["busqueda"]

    BASE = {"query": "el vendedor menciona un servicio de la farmacia además del producto pedido",
            "query_alternativa": "al cobrar, el vendedor le avisa al cliente que la farmacia ofrece otro servicio"}

    def test_valid_angles_are_kept_in_order(self) -> None:
        spec = self.ground(dict(self.BASE, angulos=[ANGULO_1, ANGULO_2, ANGULO_3]))
        self.assertEqual(spec["queries_extra"], [ANGULO_1, ANGULO_2, ANGULO_3])

    def test_absence_phrasing_is_dropped(self) -> None:
        spec = self.ground(dict(self.BASE, angulos=[
            "El vendedor no ofrece ningún servicio adicional al terminar de atender al cliente.",
            "La farmacia nunca menciona sus servicios durante la atención del cliente.", ANGULO_1]))
        self.assertEqual(spec["queries_extra"], [ANGULO_1])

    def test_angles_too_similar_to_the_query_or_to_each_other_are_dropped(self) -> None:
        casi_igual = self.BASE["query"] + " al cliente"
        spec = self.ground(dict(self.BASE, angulos=[casi_igual, ANGULO_1, ANGULO_1 + " ahora"]))
        self.assertEqual(spec["queries_extra"], [ANGULO_1])

    def test_at_most_three_and_bad_values_are_ignored(self) -> None:
        spec = self.ground(dict(self.BASE, angulos=[ANGULO_1, ANGULO_2, ANGULO_3, "una cuarta situación observable distinta de las demás"]))
        self.assertEqual(len(spec["queries_extra"]), 3)
        for bad in ("texto", 5, None, {"a": 1}, [None, 3, ""]):
            self.assertNotIn("queries_extra", self.ground(dict(self.BASE, angulos=bad)))

    def test_no_angles_for_a_narrow_question(self) -> None:
        self.assertNotIn("queries_extra", self.ground(dict(self.BASE, angulos=[])))
        self.assertNotIn("queries_extra", self.ground(dict(self.BASE)))

    def test_the_fixed_search_section_carries_the_angles_for_the_agent(self) -> None:
        plan = {"objetivo": "x", "tipo": "cualitativa", "usar_busqueda": True,
                "busqueda": dict(self.BASE, queries_extra=[ANGULO_1, ANGULO_2])}
        text = question_planner.render_plan(plan)
        self.assertIn("queries_extra=[«" + ANGULO_1 + "»; «" + ANGULO_2 + "»]", text)
        self.assertIn("TAL CUAL", text)


class ToolWiringTests(unittest.TestCase):
    def tearDown(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")

    def test_the_tool_passes_the_angles_to_the_repository(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")
        with patch.object(vi_agent._VECTOR_SEARCH_REPOSITORY, "search", return_value="{}") as search:
            vi_agent.search_conversations("consulta de prueba para la herramienta", queries_extra=[ANGULO_1])
        self.assertEqual(search.call_args.kwargs["queries_extra"], [ANGULO_1])

    def test_the_prompt_tells_the_agent_to_pass_them_verbatim_and_never_to_invent_them(self) -> None:
        vi_agent.configure_client("mens_fashion_alto")
        section = vi_agent._build_extra_tools_section()
        self.assertIn("queries_extra", section)
        self.assertIn("nunca lo inventes", section)

    def test_the_function_declaration_accepts_the_new_list_parameter(self) -> None:
        from google.genai import types

        declaration = types.FunctionDeclaration.from_callable_with_api_option(callable=vi_agent.search_conversations)
        self.assertIn("queries_extra", declaration.parameters.properties)


if __name__ == "__main__":
    unittest.main()
