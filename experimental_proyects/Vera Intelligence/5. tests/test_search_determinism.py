"""Determinismo de la búsqueda semántica (2026-10-02): caché por conversación, semilla, consulta canónica, orden total."""
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import question_planner as qp  # noqa: E402
import search_determinism as sd  # noqa: E402
import vector_search as vs  # noqa: E402


class CacheEnabled(unittest.TestCase):
    """Habilita la caché con una ruta temporal (la suite la apaga por defecto en conftest.py)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"VI_SEARCH_CACHE": "1",
                                                 "VI_SEARCH_CACHE_PATH": str(Path(self._tmp.name) / "c.sqlite")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class CacheTests(CacheEnabled):
    def test_ida_y_vuelta_y_la_primera_escritura_gana(self):
        self.assertIsNone(sd.cache_get("judge", "k"))
        sd.cache_put("judge", "k", {"a": 1})
        sd.cache_put("judge", "k", {"a": 2})  # no pisa
        self.assertEqual(sd.cache_get("judge", "k"), {"a": 1})
        self.assertIsNone(sd.cache_get("synthesis", "k"))  # el tipo forma parte de la clave

    def test_apagada_no_guarda_ni_lee(self):
        with mock.patch.dict(os.environ, {"VI_SEARCH_CACHE": "0"}):
            sd.cache_put("judge", "x", {"a": 1})
            self.assertIsNone(sd.cache_get("judge", "x"))
        self.assertIsNone(sd.cache_get("judge", "x"))

    def test_fail_open_con_ruta_imposible(self):
        archivo = Path(self._tmp.name) / "es_un_archivo"
        archivo.write_text("no es una carpeta", encoding="utf-8")
        # La "carpeta" del archivo de caché es en realidad un archivo: no se puede crear -> debe degradar, no romper.
        with mock.patch.dict(os.environ, {"VI_SEARCH_CACHE_PATH": str(archivo / "x.sqlite")}):
            self.assertIsNone(sd.cache_get("judge", "k"))  # no lanza
            sd.cache_put("judge", "k", {"a": 1})
            self.assertEqual(sd.canonical_query("x", [1.0], "s"), ("x", [1.0], False))

    def test_semilla_estable_y_sensible_al_contenido(self):
        self.assertEqual(sd.seed_for("p", "m"), sd.seed_for("p", "m"))
        self.assertNotEqual(sd.seed_for("p", "m"), sd.seed_for("otro", "m"))
        self.assertTrue(0 <= sd.seed_for("p") < 2**31 - 1)


class CanonicalQueryTests(CacheEnabled):
    A = [1.0, 0.0, 0.0]
    CERCA = [0.99, 0.05, 0.0]   # coseno ~0,999
    LEJOS = [0.6, 0.8, 0.0]     # coseno 0,6

    def test_consulta_casi_identica_se_ajusta_a_la_canonica(self):
        t1, v1, s1 = sd.canonical_query("clientes que se van sin comprar", self.A, "sig")
        self.assertEqual((t1, v1, s1), ("clientes que se van sin comprar", self.A, False))
        t2, v2, s2 = sd.canonical_query("personas que no concretan la compra", self.CERCA, "sig")
        self.assertEqual((t2, v2, s2), ("clientes que se van sin comprar", self.A, True))

    def test_no_ajusta_si_es_distinta_o_si_cambian_los_filtros(self):
        sd.canonical_query("consulta original", self.A, "sig")
        self.assertFalse(sd.canonical_query("otra cosa", self.LEJOS, "sig")[2])
        t, _, snapped = sd.canonical_query("misma intención", self.CERCA, "otros filtros")
        self.assertFalse(snapped)
        self.assertEqual(t, "misma intención")

    def test_gana_la_primera_registrada_aunque_haya_otra_mas_parecida(self):
        sd.canonical_query("primera", [1.0, 0.0, 0.0], "sig")
        # una segunda consulta que NO se ajusta a la primera (coseno bajo) queda registrada aparte
        sd.canonical_query("segunda", [0.6, 0.8, 0.0], "sig")
        # ésta es más parecida a la segunda (coseno 1,0) que a la primera, pero cumple el umbral con ambas: gana la primera
        sd.canonical_query("tercera", [0.9, 0.3, 0.0], "sig")
        texto, _, ajustada = sd.canonical_query("cuarta", [0.93, 0.25, 0.0], "sig")
        self.assertTrue(ajustada)
        self.assertEqual(texto, "primera")

    def test_apagada_devuelve_la_consulta_tal_cual(self):
        with mock.patch.dict(os.environ, {"VI_SEARCH_CACHE": "0"}):
            self.assertEqual(sd.canonical_query("x", self.A, "sig"), ("x", self.A, False))


def _resultado(i, texto):
    return {"conversation_id": f"c{i}", "distancia": 0.2, "contexto_conversacion": texto, "fecha": "2026-09-01T10:00:00"}


class JudgeCacheTests(CacheEnabled):
    def _fake_judge_factory(self, calls, ok=True):
        def fake(query, resultados, *, ok_out=None, **kw):
            calls.append(1)
            for r in resultados:
                r["notas"] = {"situacion": f"nota {r['conversation_id']} v{len(calls)}", "que_hizo": "", "como_termino": ""}
                r["_evidencias"] = {}
            if ok and ok_out is not None:
                ok_out["ok"] = True
            return [True] * len(resultados)
        return fake

    def _correr(self, fake):
        resultados = [_resultado(1, "texto uno"), _resultado(2, "texto dos")]
        with mock.patch.object(vs, "_judge_batch", fake):
            vs._judge_relevance("consulta", resultados, model="m", api_key="k")
        return resultados

    def test_la_misma_entrada_devuelve_las_mismas_notas_sin_llamar_al_modelo(self):
        calls = []
        primera = self._correr(self._fake_judge_factory(calls))
        n_primera = len(calls)
        segunda = self._correr(self._fake_judge_factory(calls))
        self.assertEqual(n_primera, 2)
        self.assertEqual(len(calls), n_primera)  # la segunda corrida no llamó
        self.assertEqual([r["notas"] for r in primera], [r["notas"] for r in segunda])

    def test_otro_texto_otra_consulta_u_otro_contexto_no_reutilizan(self):
        calls = []
        self._correr(self._fake_judge_factory(calls))
        base = len(calls)
        with mock.patch.object(vs, "_judge_batch", self._fake_judge_factory(calls)):
            vs._judge_relevance("OTRA consulta", [_resultado(1, "texto uno")], model="m", api_key="k")
            vs._judge_relevance("consulta", [_resultado(1, "texto uno CAMBIADO")], model="m", api_key="k")
            vs._judge_relevance("consulta", [_resultado(1, "texto uno"), _resultado(2, "x")], model="m", api_key="k",
                                label_context="otro contexto")
        self.assertGreater(len(calls), base + 2)

    def test_un_fallo_del_juez_no_se_guarda(self):
        calls = []
        self._correr(self._fake_judge_factory(calls, ok=False))
        antes = len(calls)
        self._correr(self._fake_judge_factory(calls, ok=True))
        self.assertGreater(len(calls), antes)  # volvió a llamar: el fallo no quedó fijo en la caché

    def test_cambiar_el_prompt_invalida_la_caja(self):
        calls = []
        self._correr(self._fake_judge_factory(calls))
        base = len(calls)
        with mock.patch.object(vs, "_JUDGE_PROMPT_TEMPLATE", vs._JUDGE_PROMPT_TEMPLATE + " (v2)"):
            self._correr(self._fake_judge_factory(calls))
        self.assertGreater(len(calls), base)


class SynthesisCacheTests(CacheEnabled):
    def test_los_patrones_se_cachean_por_conjunto_de_notas(self):
        resultados = []
        for i, cita in enumerate(["cita número uno textual", "cita número dos textual"]):
            r = _resultado(i, f"El vendedor dijo {cita} y siguió.")
            r["notas"] = {"situacion": "s", "que_hizo": "h", "como_termino": ""}
            r["_evidencias"] = {"que_hizo": cita}
            resultados.append(r)
        llamadas = []

        def fake_generate(client, *, model, contents, config):
            llamadas.append(config.seed)
            return SimpleNamespace(text=json.dumps({"patrones": []}), usage_metadata=None)

        for _ in range(2):
            salida: dict = {}
            with mock.patch.object(vs, "_generate_content_with_retry", fake_generate), \
                    mock.patch.object(vs, "_get_reusable_embed_client", lambda key: None):
                vs._synthesize_across("consulta", resultados, model="m", api_key="k", analysis_out=salida)
            self.assertEqual(salida["patrones"], [])
        self.assertEqual(len(llamadas), 1)  # la segunda vez salió de la caché
        self.assertIsInstance(llamadas[0], int)  # y la llamada real llevó semilla


class PlanCacheTests(CacheEnabled):
    DATA_MAP = {"sources": {"x": {"fields": {"campo_a": {"description": "Algo A"}}}}}

    def _gen(self, llamadas, texto):
        def gen(**kw):
            llamadas.append(1)
            return SimpleNamespace(text=json.dumps({"objetivo": texto, "usar_busqueda": True, "busqueda": {
                "query": f"{texto} el cliente pregunta algo y el vendedor responde"}}), usage_metadata=None)
        return gen

    def test_la_misma_pregunta_da_el_mismo_plan_sin_volver_a_llamar(self):
        llamadas = []
        a = qp.build_plan("¿Qué objeciones se repiten?", client=None, model="m", data_map=self.DATA_MAP,
                          generate=self._gen(llamadas, "uno"))
        b = qp.build_plan("¿Qué objeciones se repiten?", client=None, model="m", data_map=self.DATA_MAP,
                          generate=self._gen(llamadas, "DOS (otro texto que daría el modelo)"))
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(a.plan["busqueda"], b.plan["busqueda"])

    def test_otra_pregunta_o_otro_contexto_no_reutilizan_el_plan(self):
        llamadas = []
        qp.build_plan("¿Qué objeciones se repiten?", client=None, model="m", data_map=self.DATA_MAP, generate=self._gen(llamadas, "a"))
        qp.build_plan("¿Qué quejas se repiten?", client=None, model="m", data_map=self.DATA_MAP, generate=self._gen(llamadas, "b"))
        qp.build_plan("¿Qué objeciones se repiten?", client=None, model="m", data_map=self.DATA_MAP,
                      generate=self._gen(llamadas, "c"), context="Usuario: hablamos de otra cosa")
        self.assertEqual(len(llamadas), 3)


class PatternCardTests(CacheEnabled):
    """Redactor de fichas de patrón y guarda de las citas que respaldan cada patrón (simulados; sin API)."""
    FICHA = {"pregunta": "¿El vendedor informa el precio y no ofrece una alternativa?",
             "criterio_si": "El vendedor da el precio y no propone otra opción (paráfrasis).",
             "criterio_no": "Ofrece otra opción; el cliente no consulta el precio; comentarios entre empleados.",
             "nivel_de_ambiguedad": "media"}

    def _gen(self, llamadas, payload=None, falla=False):
        def gen(**kw):
            llamadas.append(kw["contents"])
            if falla:
                raise RuntimeError("boom")
            return SimpleNamespace(text=json.dumps(payload if payload is not None else {"ficha": self.FICHA}),
                                   usage_metadata=None)
        return gen

    def test_arma_la_ficha_con_las_citas_como_contexto_interno(self):
        llamadas = []
        ficha = qp.design_pattern_card("informa el precio y no ofrece alternativa", ["el vendedor dijo el precio y nada más"],
                                       client=None, model="m", generate=self._gen(llamadas))
        self.assertEqual(ficha["pregunta"], self.FICHA["pregunta"])
        prompt = llamadas[0]
        self.assertIn("informa el precio y no ofrece alternativa", prompt)
        self.assertIn("el vendedor dijo el precio y nada más", prompt)
        self.assertIn("NO los copies", prompt)  # las citas no deben salir en la ficha

    def test_ficha_invalida_o_fallo_devuelven_none(self):
        corta = dict(self.FICHA, criterio_no="no")
        self.assertIsNone(qp.design_pattern_card("un patrón cualquiera largo", [], client=None, model="m",
                                                 generate=self._gen([], {"ficha": corta})))
        self.assertIsNone(qp.design_pattern_card("un patrón cualquiera largo", [], client=None, model="m",
                                                 generate=self._gen([], {"ficha": "texto"})))
        self.assertIsNone(qp.design_pattern_card("un patrón cualquiera largo", [], client=None, model="m",
                                                 generate=self._gen([], falla=True)))

    def test_la_misma_entrada_da_la_misma_ficha_sin_volver_a_llamar(self):
        llamadas = []
        for _ in range(2):
            qp.design_pattern_card("patrón estable de prueba largo", ["cita uno"], client=None, model="m",
                                   generate=self._gen(llamadas))
        self.assertEqual(len(llamadas), 1)

    def test_los_patrones_verificados_guardan_sus_citas(self):
        resultados = [
            {"conversation_id": "c1", "vendedor": "Ana", "contexto_conversacion": "dijo el precio sin ofrecer ninguna otra opcion al cliente"},
            {"conversation_id": "c2", "vendedor": "Luis", "contexto_conversacion": "informo cuanto cuesta y no propuso alternativas baratas"},
        ]
        raw = [{"patron": "informa el precio y no ofrece alternativas", "evidencia_1": "dijo el precio sin ofrecer ninguna otra opcion",
                "evidencia_2": "informo cuanto cuesta y no propuso alternativas"}]
        self.assertEqual(vs._verify_patrones(raw, resultados), ["informa el precio y no ofrece alternativas"])
        citas = vs.recall_pattern_evidence("informa el precio y no ofrece alternativas")
        self.assertEqual(len(citas), 2)
        self.assertIn("dijo el precio sin ofrecer ninguna otra opcion", citas)
        self.assertEqual(vs.recall_pattern_evidence("un patrón que nunca se vio"), [])

    def test_un_patron_no_verificado_no_guarda_citas(self):
        resultados = [{"conversation_id": "c1", "vendedor": "Ana", "contexto_conversacion": "texto distinto"}]
        raw = [{"patron": "patrón inventado sin respaldo real", "evidencia_1": "cita que no existe en ningún texto",
                "evidencia_2": "otra cita que tampoco existe en ningún lado"}]
        self.assertEqual(vs._verify_patrones(raw, resultados), [])
        self.assertEqual(vs.recall_pattern_evidence("patrón inventado sin respaldo real"), [])


class PatternCardTests(CacheEnabled):
    """Redactor de fichas de patrón y guarda de las citas que respaldan cada patrón (simulados; sin API)."""
    FICHA = {"pregunta": "¿El vendedor informa el precio y no ofrece una alternativa?",
             "criterio_si": "El vendedor da el precio y no propone otra opción (paráfrasis).",
             "criterio_no": "Ofrece otra opción; el cliente no consulta el precio; comentarios entre empleados.",
             "nivel_de_ambiguedad": "media"}

    def _gen(self, llamadas, payload=None, falla=False):
        def gen(**kw):
            llamadas.append(kw["contents"])
            if falla:
                raise RuntimeError("boom")
            return SimpleNamespace(text=json.dumps(payload if payload is not None else {"ficha": self.FICHA}),
                                   usage_metadata=None)
        return gen

    def test_arma_la_ficha_con_las_citas_como_contexto_interno(self):
        llamadas = []
        ficha = qp.design_pattern_card("informa el precio y no ofrece alternativa", ["el vendedor dijo el precio y nada más"],
                                       client=None, model="m", generate=self._gen(llamadas))
        self.assertEqual(ficha["pregunta"], self.FICHA["pregunta"])
        prompt = llamadas[0]
        self.assertIn("informa el precio y no ofrece alternativa", prompt)
        self.assertIn("el vendedor dijo el precio y nada más", prompt)
        self.assertIn("NO los copies", prompt)  # las citas no deben salir en la ficha

    def test_ficha_invalida_o_fallo_devuelven_none(self):
        corta = dict(self.FICHA, criterio_no="no")
        self.assertIsNone(qp.design_pattern_card("un patrón cualquiera largo", [], client=None, model="m",
                                                 generate=self._gen([], {"ficha": corta})))
        self.assertIsNone(qp.design_pattern_card("un patrón cualquiera largo", [], client=None, model="m",
                                                 generate=self._gen([], {"ficha": "texto"})))
        self.assertIsNone(qp.design_pattern_card("un patrón cualquiera largo", [], client=None, model="m",
                                                 generate=self._gen([], falla=True)))

    def test_la_misma_entrada_da_la_misma_ficha_sin_volver_a_llamar(self):
        llamadas = []
        for _ in range(2):
            qp.design_pattern_card("patrón estable de prueba largo", ["cita uno"], client=None, model="m",
                                   generate=self._gen(llamadas))
        self.assertEqual(len(llamadas), 1)

    def test_los_patrones_verificados_guardan_sus_citas(self):
        resultados = [
            {"conversation_id": "c1", "vendedor": "Ana", "contexto_conversacion": "dijo el precio sin ofrecer ninguna otra opcion al cliente"},
            {"conversation_id": "c2", "vendedor": "Luis", "contexto_conversacion": "informo cuanto cuesta y no propuso alternativas baratas"},
        ]
        raw = [{"patron": "informa el precio y no ofrece alternativas", "evidencia_1": "dijo el precio sin ofrecer ninguna otra opcion",
                "evidencia_2": "informo cuanto cuesta y no propuso alternativas"}]
        self.assertEqual(vs._verify_patrones(raw, resultados), ["informa el precio y no ofrece alternativas"])
        citas = vs.recall_pattern_evidence("informa el precio y no ofrece alternativas")
        self.assertEqual(len(citas), 2)
        self.assertIn("dijo el precio sin ofrecer ninguna otra opcion", citas)
        self.assertEqual(vs.recall_pattern_evidence("un patrón que nunca se vio"), [])

    def test_un_patron_no_verificado_no_guarda_citas(self):
        resultados = [{"conversation_id": "c1", "vendedor": "Ana", "contexto_conversacion": "texto distinto"}]
        raw = [{"patron": "patrón inventado sin respaldo real", "evidencia_1": "cita que no existe en ningún texto",
                "evidencia_2": "otra cita que tampoco existe en ningún lado"}]
        self.assertEqual(vs._verify_patrones(raw, resultados), [])
        self.assertEqual(vs.recall_pattern_evidence("patrón inventado sin respaldo real"), [])


class SynthesisRetryTests(CacheEnabled):
    def test_json_malformado_se_reintenta_con_otra_semilla(self):
        resultados = []
        for i, cita in enumerate(["cita número uno textual", "cita número dos textual"]):
            r = _resultado(i, f"El vendedor dijo {cita} y siguió.")
            r["notas"] = {"situacion": "s", "que_hizo": "h", "como_termino": ""}
            r["_evidencias"] = {"que_hizo": cita}
            resultados.append(r)
        respuestas = iter(['{"patrones": [ {"patron": "x" "mal"}', json.dumps({"patrones": []})])
        semillas = []

        def fake_generate(client, *, model, contents, config):
            semillas.append(config.seed)
            return SimpleNamespace(text=next(respuestas), usage_metadata=None)

        salida: dict = {}
        with mock.patch.object(vs, "_generate_content_with_retry", fake_generate), \
                mock.patch.object(vs, "_get_reusable_embed_client", lambda key: None):
            vs._synthesize_across("consulta", resultados, model="m", api_key="k", analysis_out=salida)
        self.assertEqual(salida["patrones"], [])
        self.assertEqual(len(semillas), 2)
        self.assertNotEqual(semillas[0], semillas[1])


class OrdenTotalTests(unittest.TestCase):
    def test_rrf_desempata_igual_sin_importar_el_orden_de_llegada(self):
        a = {"conversation_id": "a", "distancia": 0.2}
        b = {"conversation_id": "b", "distancia": 0.2}
        orden1 = [r["conversation_id"] for r in vs._reciprocal_rank_fusion([[a], [b]])]
        orden2 = [r["conversation_id"] for r in vs._reciprocal_rank_fusion([[b], [a]])]
        self.assertEqual(orden1, orden2)

    def test_el_sql_desempata_por_conversacion_y_fragmento(self):
        fuente = inspect.getsource(vs.VectorSearchRepository._retrieve)
        self.assertIn("ORDER BY ce.embedding <=> %s::vector, ce.recording_id, ce.chunk_idx", fuente)
        self.assertIn("ORDER BY top.distancia, top.recording_id, top.chunk_idx", fuente)


class BusquedaFijadaTests(unittest.TestCase):
    def test_se_conserva_solo_si_usar_busqueda_y_la_consulta_es_usable(self):
        plan = {"usar_busqueda": True, "busqueda": {
            "query": "el cliente pregunta el precio y el vendedor no ofrece alternativa",
            "query_alternativa": "el cliente consulta cuánto cuesta y el vendedor no propone otra opción",
            "store_name": "Parque Delta", "employee_name": None, "date_from": "2026-09-01", "date_to": "01/10/2026"}}
        out = qp.ground_busqueda(plan)["busqueda"]
        self.assertEqual(out["store_name"], "Parque Delta")
        self.assertEqual(out["date_from"], "2026-09-01")
        self.assertNotIn("date_to", out)       # formato inválido: se descarta
        self.assertNotIn("employee_name", out)  # null
        self.assertIsNone(qp.ground_busqueda({"usar_busqueda": False, "busqueda": {"query": "x" * 40}})["busqueda"])
        self.assertIsNone(qp.ground_busqueda({"usar_busqueda": True, "busqueda": {"query": "corta"}})["busqueda"])
        self.assertIsNone(qp.ground_busqueda({"usar_busqueda": True, "busqueda": "texto"})["busqueda"])

    def test_el_plan_renderiza_la_busqueda_fijada(self):
        plan = {"objetivo": "x", "usar_busqueda": True, "busqueda": {"query": "el cliente pide algo concreto y el vendedor responde"}}
        texto = qp.render_plan(plan)
        self.assertIn("10. BÚSQUEDA SEMÁNTICA FIJADA", texto)
        self.assertIn("TAL CUAL", texto)


if __name__ == "__main__":
    unittest.main()
