import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import insight_extraction as ie  # noqa: E402
import answer_verification as av  # noqa: E402

CLIENT = SimpleNamespace(tenant="T", client_id="t", vector_search=None)


def _rows(n, pos_ids=()):
    return [(f"r{i}", "texto " * 200, {"tienda": "Tienda", "fecha": "2026-09-01"}) for i in range(n)]


def _repo(total, rows, read, verify):
    return ie.InsightExtractionRepository(
        CLIENT, read_fn=lambda text, spec, key: read(text), verify_fn=lambda spec, text: verify(text),
        fetch_fn=lambda **kw: (total, rows))


class ComputeRangeTests(unittest.TestCase):
    """Estimación por lectura simple (2026-10-05, opción 2): positivos de JEV / leídas, intervalo = sólo muestreo."""

    def test_poblacion_completa_no_tiene_error_de_muestreo(self):
        r = ie.compute_range(poblacion=100, leidas=100, positivos_jev=10, confirmadas=8)
        self.assertEqual(r["modo"], "poblacion_completa")
        self.assertEqual((r["pct_minimo"], r["pct_estimado"], r["pct_maximo"]), (10.0, 10.0, 10.0))
        self.assertEqual(r["conversaciones_estimado"], 10)
        self.assertEqual(r["confirmadas_con_evidencia"], 8)

    def test_muestra_escala_a_poblacion_con_intervalo_de_muestreo(self):
        r = ie.compute_range(poblacion=10000, leidas=1000, positivos_jev=60, confirmadas=40)
        self.assertEqual(r["modo"], "muestra_aleatoria")
        self.assertEqual(r["pct_estimado"], 6.0)
        self.assertEqual(r["conversaciones_estimado"], 600)
        self.assertLess(r["pct_minimo"], 6.0)
        self.assertGreater(r["pct_maximo"], 6.0)
        self.assertGreater(r["conversaciones_maximo"], r["conversaciones_minimo"])

    def test_la_verificacion_no_cambia_el_numero(self):
        # Lección de a07 (Tigo): el verificador barato confirmaba 55 % y el fuerte 78 %; si corrigiera el número, dependería del modelo.
        a = ie.compute_range(poblacion=5000, leidas=500, positivos_jev=100, confirmadas=0)
        b = ie.compute_range(poblacion=5000, leidas=500, positivos_jev=100, confirmadas=100)
        for key in ("pct_estimado", "pct_minimo", "pct_maximo", "conversaciones_estimado"):
            self.assertEqual(a[key], b[key])

    def test_mas_lecturas_achican_el_intervalo(self):
        small = ie.compute_range(poblacion=50000, leidas=250, positivos_jev=50)
        large = ie.compute_range(poblacion=50000, leidas=2000, positivos_jev=400)
        self.assertLess(large["pct_maximo"] - large["pct_minimo"], small["pct_maximo"] - small["pct_minimo"])
        self.assertEqual(small["pct_estimado"], large["pct_estimado"])

    def test_casos_degenerados_no_rompen(self):
        r = ie.compute_range(poblacion=1000, leidas=300, positivos_jev=0)
        self.assertEqual((r["pct_estimado"], r["pct_minimo"]), (0.0, 0.0))
        self.assertGreater(r["pct_maximo"], 0.0)  # 0 de 300 no es "0 %": el techo de muestreo sube
        r = ie.compute_range(poblacion=1000, leidas=300, positivos_jev=300)
        self.assertEqual(r["pct_estimado"], 100.0)
        self.assertLess(r["pct_minimo"], 100.0)

    def test_el_resultado_trae_estimacion_dentro_del_intervalo(self):
        rows = _rows(20)
        repo = _repo(100, rows, lambda text: 0.9, lambda text: {"respuesta": True, "evidencia_ok": True, "resumen": "caso"})
        out = json.loads(repo.extract("¿Pregunta suficientemente larga?", "si", "no"))
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertLessEqual(row["pct_minimo"], row["pct_estimado"])
        self.assertLessEqual(row["pct_estimado"], row["pct_maximo"])
        self.assertIn("ERROR DE MUESTREO", out["interpretacion"])
        self.assertIn("DENOMINADOR", out["interpretacion"])  # Tigo a32: no rotular la población como un subconjunto

    def test_evidencia_requiere_cita_verificable(self):
        self.assertTrue(ie.evidence_supported("ofrecemos cuotas sin interes", "Hola, ofrecemos cuotas sin interés hoy"))
        self.assertFalse(ie.evidence_supported("cuotas", "cuotas sin interes"))
        self.assertFalse(ie.evidence_supported("frase inventada por el modelo", "otra cosa"))


class ExtractFlowTests(unittest.TestCase):
    def test_flujo_completo_con_lectores_falsos(self):
        rows = _rows(20)
        pos = {"r0", "r1", "r2", "r3"}
        ids = {rows[i][1]: rows[i][0] for i in range(20)}
        # el texto es igual para todos; distinguimos por orden de llamada con un contador
        order = {}

        def read(text):
            order[text] = order.get(text, 0)
            return 0.9 if len(order) <= 4 and False else 0.0
        # mejor: usar textos únicos
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T", "fecha": "2026-09-01"}) for i in range(20)]
        read = lambda text: 0.9 if text.startswith(("texto 0 ", "texto 1 ", "texto 2 ", "texto 3 ")) else 0.1
        verify = lambda text: {"respuesta": text.startswith(("texto 0 ", "texto 1 ", "texto 2 ")),
                               "evidencia_ok": True, "resumen": "situación"}
        out = json.loads(_repo(20, rows, read, verify).extract(
            "¿Se ofrece pagar en cuotas sin interés?", "ofrece cuotas sin interés", "no cuenta otra cosa"))
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(row["modo"], "poblacion_completa")
        self.assertEqual(row["conversaciones_leidas"], 20)
        self.assertEqual(row["confirmadas_con_evidencia"], 3)  # r3 rechazado por la verificación
        self.assertLessEqual(row["conversaciones_minimo"], row["conversaciones_maximo"])
        self.assertEqual(len(out["ejemplos"]), 3)

    def test_modo_literal_da_piso_sin_maximo(self):
        rows = [("a", "hablamos de cuotas sin interés hoy", {"tienda": "T", "fecha": "x"}),
                ("b", "nada de eso", {"tienda": "T", "fecha": "x"})]
        out = json.loads(_repo(2, rows, lambda t: 0.0, lambda t: {}).extract(
            "¿Se menciona cuotas sin interés?", terminos_literales="cuotas sin interés"))
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(row["confirmadas_con_evidencia"], 1)
        self.assertIsNone(row["pct_maximo"])

    def test_misma_consulta_repetida_no_vuelve_a_leer(self):
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T", "fecha": "x"}) for i in range(5)]
        calls = {"n": 0}

        def read(text):
            calls["n"] += 1
            return 0.1

        repo = _repo(5, rows, read, lambda t: {"respuesta": False, "evidencia_ok": True, "resumen": ""})
        a = repo.extract("¿Pregunta suficientemente larga?", "si", "no")
        n_first = calls["n"]
        b = repo.extract("¿Pregunta suficientemente larga?", "si", "no")
        self.assertEqual(a, b)
        self.assertEqual(calls["n"], n_first)
        repo.extract("¿Otra pregunta suficientemente larga?", "si", "no")
        self.assertGreater(calls["n"], n_first)

    def test_sin_poblacion(self):
        out = json.loads(_repo(0, [], lambda t: 0.0, lambda t: {}).extract(
            "¿Se menciona cuotas sin interés?", "si", "no"))
        self.assertEqual(out["rows"][0][0], "sin_poblacion")

    def test_validaciones(self):
        repo = _repo(1, [], lambda t: 0.0, lambda t: {})
        with self.assertRaises(ValueError):
            repo.extract("corta", "si", "no")
        with self.assertRaises(ValueError):
            repo.extract("¿Pregunta suficientemente larga?", "", "")
        with self.assertRaises(ValueError):
            repo.extract("¿Pregunta suficientemente larga?", "si", "no", date_from="01/09/2026")

    def test_lector_caido_falla_cerrado(self):
        rows = _rows(3)
        repo = _repo(3, rows, lambda t: (_ for _ in ()).throw(RuntimeError("x")), lambda t: {})
        with self.assertRaises(RuntimeError):
            repo.extract("¿Pregunta suficientemente larga?", "si", "no")

    def test_resultado_es_respaldable_como_evidencia_sql(self):
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T", "fecha": "x"}) for i in range(5)]
        payload = _repo(5, rows, lambda t: 0.1, lambda t: {"respuesta": False, "evidencia_ok": True, "resumen": ""}).extract(
            "¿Pregunta suficientemente larga?", "si", "no")
        store = {}
        key = av.add_result(store, payload)
        self.assertIsNotNone(key)
        self.assertEqual(av.resolve({"id": key, "row": 0, "column": "conversaciones_leidas"}, store), 5)


class StratifiedSamplingTests(unittest.TestCase):
    def test_paso_y_offset_son_deterministas_y_acotados(self):
        self.assertEqual(ie.systematic_step(250503, 600), 418)
        self.assertEqual(ie.systematic_step(100, 600), 1)
        self.assertLessEqual(250503 / ie.systematic_step(250503, 600), 600)
        a = ie.systematic_offset("misma consulta", 418)
        self.assertEqual(a, ie.systematic_offset("misma consulta", 418))
        self.assertTrue(0 <= a < 418)
        self.assertNotEqual(ie.systematic_offset("otra", 418), ie.systematic_offset("misma consulta", 418))

    def test_manifiesto_ok_cuando_la_muestra_se_parece_al_universo(self):
        pop = {("2026-08", "A"): 600, ("2026-08", "B"): 200, ("2026-09", "A"): 150, ("2026-09", "B"): 50}
        sample = [("2026-08", "A")] * 60 + [("2026-08", "B")] * 20 + [("2026-09", "A")] * 15 + [("2026-09", "B")] * 5
        m = ie.coverage_manifest(pop, sample)
        self.assertEqual(m["alerta_cobertura"], "ok")
        self.assertEqual((m["meses_en_poblacion"], m["meses_en_muestra"]), (2, 2))
        self.assertEqual((m["tiendas_en_poblacion"], m["tiendas_en_muestra"]), (2, 2))
        self.assertLess(m["distancia_distribucion_pct"], 1.0)

    def test_manifiesto_alerta_si_falta_una_tienda_grande_o_hay_sesgo(self):
        pop = {("2026-08", "A"): 500, ("2026-08", "B"): 500}
        sin_b = ie.coverage_manifest(pop, [("2026-08", "A")] * 100)
        self.assertEqual(sin_b["alerta_cobertura"], "baja")
        self.assertEqual(sin_b["tiendas_en_muestra"], 1)
        sesgada = ie.coverage_manifest(pop, [("2026-08", "A")] * 90 + [("2026-08", "B")] * 10)
        self.assertEqual(sesgada["alerta_cobertura"], "baja")

    def test_resultado_incluye_el_manifiesto_como_columnas_citables(self):
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T1" if i < 3 else "T2", "fecha": "2026-09-01"}) for i in range(5)]
        pop = {("2026-09", "T1"): 3, ("2026-09", "T2"): 2}
        repo = ie.InsightExtractionRepository(
            CLIENT, read_fn=lambda text, spec, key: 0.1,
            verify_fn=lambda spec, text: {"respuesta": False, "evidencia_ok": True, "resumen": ""},
            fetch_fn=lambda **kw: (5, rows, pop))
        out = json.loads(repo.extract("¿Pregunta suficientemente larga?", "si", "no"))
        row = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(row["alerta_cobertura"], "ok")
        self.assertEqual(row["tiendas_en_muestra"], 2)
        self.assertEqual(row["distancia_distribucion_pct"], 0.0)


class GroupedExtractionTests(unittest.TestCase):
    def _frame(self, stores, per_store, days=("2026-08-10", "2026-09-10")):
        return [(f"{st}-{i}", st, days[i % len(days)]) for st in stores for i in range(per_store[st])]

    def test_group_key(self):
        self.assertEqual(ie.group_key("tienda", "Toreo", "2026-09-10"), "Toreo")
        self.assertEqual(ie.group_key("mes", "x", "2026-09-10"), "2026-09")
        self.assertEqual(ie.group_key("semana", "x", "2026-09-10"), "2026-S37")

    def test_tiendas_se_limitan_a_las_mas_grandes_y_se_cuentan_las_excluidas(self):
        sizes = {f"T{i:02d}": 500 - i * 10 for i in range(20)}
        info = ie.stratified_group_sample(self._frame(list(sizes), sizes), "tienda", "seed")
        self.assertEqual(len(info["orden"]), ie.GROUP_MAX)
        self.assertEqual(info["orden"][0], "T00")  # la más grande primero
        self.assertEqual(info["grupos_totales"], 20)
        self.assertEqual(info["no_incluidas"], sum(sizes[f"T{i:02d}"] for i in range(12, 20)))
        for key in info["orden"]:
            n = len(info["seleccion"][key])
            self.assertTrue(ie.GROUP_MIN_SAMPLE <= n <= ie.GROUP_MAX_SAMPLE)
            self.assertEqual(info["poblacion"][key], sizes[key])
            self.assertEqual(len({it[0] for it in info["seleccion"][key]}), n)  # sin repetidos

    def test_grupo_chico_se_lee_completo_y_la_seleccion_es_determinista(self):
        sizes = {"Chica": 25, "Grande": 900}
        frame = self._frame(list(sizes), sizes)
        a = ie.stratified_group_sample(frame, "tienda", "semilla")
        b = ie.stratified_group_sample(frame, "tienda", "semilla")
        c = ie.stratified_group_sample(frame, "tienda", "otra semilla")
        self.assertEqual(len(a["seleccion"]["Chica"]), 25)
        self.assertEqual(a["seleccion"], b["seleccion"])
        self.assertNotEqual(a["seleccion"]["Grande"], c["seleccion"]["Grande"])

    def test_periodos_son_cronologicos_y_toman_los_mas_recientes(self):
        frame = [(f"r{m}-{i}", "T", f"2026-{m:02d}-15") for m in range(1, 13) for i in range(3)] + [("x", "T", "2025-12-15")]
        info = ie.stratified_group_sample(frame, "mes", "s")
        self.assertEqual(info["orden"], sorted(info["orden"]))
        self.assertEqual(len(info["orden"]), 12)
        self.assertNotIn("2025-12", info["orden"])
        self.assertEqual(info["no_incluidas"], 1)

    def _grouped_repo(self):
        info = {"orden": ["A", "B"], "poblacion": {"A": 200, "B": 40}, "no_incluidas": 55, "grupos_totales": 4,
                "seleccion": {}}
        rows = ([(f"a{i}", f"texto a{i} " * 100, {"tienda": "A", "fecha": "2026-09-01", "grupo": "A"}) for i in range(10)]
                + [(f"b{i}", f"texto b{i} " * 100, {"tienda": "B", "fecha": "2026-09-01", "grupo": "B"}) for i in range(10)])
        pos = ("texto a0 ", "texto a1 ", "texto b0 ")
        return ie.InsightExtractionRepository(
            CLIENT, read_fn=lambda text, spec, key: 0.9 if text.startswith(pos) else 0.1,
            verify_fn=lambda spec, text: ({"respuesta": True, "evidencia_ok": True, "resumen": "caso"} if text.startswith(pos)
                                          else {"respuesta": False, "evidencia_ok": True, "resumen": ""}),
            fetch_fn=lambda **kw: (295, rows, None, info))

    def test_desglose_devuelve_una_fila_por_grupo_mas_suma_y_no_leidos(self):
        out = json.loads(self._grouped_repo().extract(
            "¿Pregunta suficientemente larga?", "si", "no", desglosar_por="tienda"))
        self.assertEqual(out["columns"][0], "grupo")
        grupos = [r[0] for r in out["rows"]]
        self.assertEqual(grupos[:2], ["A", "B"])
        self.assertTrue(grupos[2].startswith("(suma de los grupos"))
        self.assertTrue(grupos[3].startswith("(otros 2 grupos sin leer)"))
        self.assertEqual(out["rows"][3][2], 55)
        a = dict(zip(out["columns"], out["rows"][0]))
        self.assertEqual(a["poblacion_filtrada"], 200)
        self.assertEqual(a["conversaciones_leidas"], 10)
        self.assertLessEqual(a["pct_minimo"], a["pct_maximo"])
        self.assertEqual(out["desglosado_por"], "tienda")

    def test_desglose_valida_argumentos(self):
        repo = self._grouped_repo()
        with self.assertRaises(ValueError):
            repo.extract("¿Pregunta suficientemente larga?", "si", "no", desglosar_por="vendedor")
        with self.assertRaises(ValueError):
            repo.extract("¿Pregunta suficientemente larga?", terminos_literales="cuotas", desglosar_por="mes")


class PatternBridgeTests(unittest.TestCase):
    """Puente patrón -> número (2026-10-02). Armado SIN probar en vivo: estos tests usan lectores y redactor simulados."""
    FICHA = {"pregunta": "¿El vendedor informa el precio y no ofrece una alternativa más barata?",
             "criterio_si": "El vendedor dice el precio y no propone otra opción (ej. paráfrasis genéricas).",
             "criterio_no": "El vendedor sí ofrece una alternativa; el cliente no consulta el precio; comentarios entre empleados.",
             "nivel_de_ambiguedad": "media", "poblacion": {"campo": "x", "valor": "y"}}
    PATRON = "informa el precio pero no ofrece una alternativa más económica"

    def _repo(self, card_calls, fetch_kwargs=None, card=None):
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T", "fecha": "2026-09-01"}) for i in range(6)]

        def fetch(**kw):
            if fetch_kwargs is not None:
                fetch_kwargs.update(kw)
            return 6, rows

        def card_fn(patron):
            card_calls.append(patron)
            if isinstance(card, Exception):
                raise card
            return dict(card or self.FICHA)

        return ie.InsightExtractionRepository(
            CLIENT, read_fn=lambda text, spec, key: 0.9 if text.startswith("texto 0 ") else 0.1,
            verify_fn=lambda spec, text: {"respuesta": text.startswith("texto 0 "), "evidencia_ok": True, "resumen": "caso"},
            fetch_fn=fetch, card_fn=card_fn)

    def test_el_patron_arma_la_ficha_y_el_resultado_la_declara(self):
        llamadas = []
        out = json.loads(self._repo(llamadas).extract(patron=self.PATRON))
        self.assertEqual(llamadas, [self.PATRON])
        self.assertEqual(out["tipo_resultado"], "extraccion_de_patron")
        self.assertEqual(out["patron"], self.PATRON)
        self.assertEqual(out["ficha_usada"]["pregunta"], self.FICHA["pregunta"])
        self.assertIn("muestra aleatoria", out["interpretacion"])
        self.assertIn("conversaciones_leidas", out["columns"])
        self.assertEqual(out["pregunta_evaluada"], self.FICHA["pregunta"])

    def test_la_poblacion_la_fija_quien_llama_no_la_ficha(self):
        kwargs = {}
        self._repo([], kwargs).extract(patron=self.PATRON, store_name="Parque Delta")
        self.assertEqual(kwargs["store_name"], "Parque Delta")
        self.assertEqual(kwargs["campo"], "")   # la ficha propuso campo/valor, se ignoran
        self.assertEqual(kwargs["valor"], "")

    def test_con_criterios_explicitos_no_se_usa_el_redactor_de_patrones(self):
        llamadas = []
        out = json.loads(self._repo(llamadas).extract(
            "¿Pregunta suficientemente larga?", "si cuenta", "no cuenta", patron=self.PATRON))
        self.assertEqual(llamadas, [])
        self.assertNotIn("patron", out)

    def test_validaciones_y_fallo_del_redactor(self):
        with self.assertRaises(ValueError):
            self._repo([]).extract(patron="corto")
        with self.assertRaises(ValueError):
            self._repo([], card=ValueError("No pude armar una definición confiable")).extract(patron=self.PATRON)
        with self.assertRaises(ValueError):  # sin patrón ni criterios sigue siendo un error claro
            self._repo([]).extract()

    def test_misma_consulta_de_patron_no_vuelve_a_leer(self):
        llamadas = []
        repo = self._repo(llamadas)
        a = repo.extract(patron=self.PATRON)
        b = repo.extract(patron=self.PATRON)
        self.assertEqual(a, b)

    def test_el_agente_expone_el_parametro_y_la_regla(self):
        import vi_agent

        capturado = {}

        class FakeRepo:
            def extract(self, *args, **kwargs):
                capturado.update(kwargs)
                return "{}"

        with mock.patch.object(vi_agent, "_INSIGHT_EXTRACTION_REPOSITORY", FakeRepo()):
            vi_agent.extract_insight(patron=self.PATRON, store_name="Parque Delta")
            texto = vi_agent._build_extra_tools_section()
        self.assertEqual(capturado["patron"], self.PATRON)
        self.assertEqual(capturado["store_name"], "Parque Delta")
        self.assertIn("PATRÓN → NÚMERO", texto)
        self.assertIn("desglosar_por, patron)", texto)
        self.assertIn("hasta DOS", texto)


class PatternBridgeTests(unittest.TestCase):
    """Puente patrón -> número (2026-10-02). Armado SIN probar en vivo: estos tests usan lectores y redactor simulados."""
    FICHA = {"pregunta": "¿El vendedor informa el precio y no ofrece una alternativa más barata?",
             "criterio_si": "El vendedor dice el precio y no propone otra opción (ej. paráfrasis genéricas).",
             "criterio_no": "El vendedor sí ofrece una alternativa; el cliente no consulta el precio; comentarios entre empleados.",
             "nivel_de_ambiguedad": "media", "poblacion": {"campo": "x", "valor": "y"}}
    PATRON = "informa el precio pero no ofrece una alternativa más económica"

    def _repo(self, card_calls, fetch_kwargs=None, card=None):
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T", "fecha": "2026-09-01"}) for i in range(6)]

        def fetch(**kw):
            if fetch_kwargs is not None:
                fetch_kwargs.update(kw)
            return 6, rows

        def card_fn(patron):
            card_calls.append(patron)
            if isinstance(card, Exception):
                raise card
            return dict(card or self.FICHA)

        return ie.InsightExtractionRepository(
            CLIENT, read_fn=lambda text, spec, key: 0.9 if text.startswith("texto 0 ") else 0.1,
            verify_fn=lambda spec, text: {"respuesta": text.startswith("texto 0 "), "evidencia_ok": True, "resumen": "caso"},
            fetch_fn=fetch, card_fn=card_fn)

    def test_el_patron_arma_la_ficha_y_el_resultado_la_declara(self):
        llamadas = []
        out = json.loads(self._repo(llamadas).extract(patron=self.PATRON))
        self.assertEqual(llamadas, [self.PATRON])
        self.assertEqual(out["tipo_resultado"], "extraccion_de_patron")
        self.assertEqual(out["patron"], self.PATRON)
        self.assertEqual(out["ficha_usada"]["pregunta"], self.FICHA["pregunta"])
        self.assertIn("muestra aleatoria", out["interpretacion"])
        self.assertIn("conversaciones_leidas", out["columns"])
        self.assertEqual(out["pregunta_evaluada"], self.FICHA["pregunta"])

    def test_la_poblacion_la_fija_quien_llama_no_la_ficha(self):
        kwargs = {}
        self._repo([], kwargs).extract(patron=self.PATRON, store_name="Parque Delta")
        self.assertEqual(kwargs["store_name"], "Parque Delta")
        self.assertEqual(kwargs["campo"], "")   # la ficha propuso campo/valor, se ignoran
        self.assertEqual(kwargs["valor"], "")

    def test_con_criterios_explicitos_no_se_usa_el_redactor_de_patrones(self):
        llamadas = []
        out = json.loads(self._repo(llamadas).extract(
            "¿Pregunta suficientemente larga?", "si cuenta", "no cuenta", patron=self.PATRON))
        self.assertEqual(llamadas, [])
        self.assertNotIn("patron", out)

    def test_validaciones_y_fallo_del_redactor(self):
        with self.assertRaises(ValueError):
            self._repo([]).extract(patron="corto")
        with self.assertRaises(ValueError):
            self._repo([], card=ValueError("No pude armar una definición confiable")).extract(patron=self.PATRON)
        with self.assertRaises(ValueError):  # sin patrón ni criterios sigue siendo un error claro
            self._repo([]).extract()

    def test_misma_consulta_de_patron_no_vuelve_a_leer(self):
        llamadas = []
        repo = self._repo(llamadas)
        a = repo.extract(patron=self.PATRON)
        b = repo.extract(patron=self.PATRON)
        self.assertEqual(a, b)

    def test_el_agente_expone_el_parametro_y_la_regla(self):
        import vi_agent

        capturado = {}

        class FakeRepo:
            def extract(self, *args, **kwargs):
                capturado.update(kwargs)
                return "{}"

        with mock.patch.object(vi_agent, "_INSIGHT_EXTRACTION_REPOSITORY", FakeRepo()):
            vi_agent.extract_insight(patron=self.PATRON, store_name="Parque Delta")
            texto = vi_agent._build_extra_tools_section()
        self.assertEqual(capturado["patron"], self.PATRON)
        self.assertEqual(capturado["store_name"], "Parque Delta")
        self.assertIn("PATRÓN → NÚMERO", texto)
        self.assertIn("desglosar_por, patron)", texto)
        self.assertIn("hasta DOS", texto)


class VerifierModelsTests(unittest.TestCase):
    def test_positivos_con_modelo_barato_y_negativos_con_modelo_fuerte(self):
        rows = [(f"r{i}", f"texto {i} " * 100, {"tienda": "T", "fecha": "x"}) for i in range(10)]
        used = []

        def fake_verify(self, spec, transcript, model=ie.POSITIVE_VERIFY_MODEL):
            used.append((transcript.startswith(("texto 0 ", "texto 1 ")), model))
            return {"respuesta": False, "evidencia_ok": True, "resumen": ""}

        repo = ie.InsightExtractionRepository(
            CLIENT, read_fn=lambda text, spec, key: 0.9 if text.startswith(("texto 0 ", "texto 1 ")) else 0.1,
            fetch_fn=lambda **kw: (10, rows))
        with mock.patch.object(ie.InsightExtractionRepository, "_gemini_verify", fake_verify):
            repo.extract("¿Pregunta suficientemente larga?", "si", "no")
        positives = {m for is_pos, m in used if is_pos}
        negatives = {m for is_pos, m in used if not is_pos}
        self.assertEqual(positives, {ie.POSITIVE_VERIFY_MODEL})
        self.assertEqual(negatives, set())  # los negativos ya no se revisan: el número es la lectura simple
        self.assertEqual(ie.VERIFY_NEGATIVES_SAMPLE, 0)
        self.assertEqual(ie.VERIFY_POSITIVES_MAX, 12)


class InsightExtractionToolTests(unittest.TestCase):
    def test_apagada_por_defecto_y_se_habilita_por_flag(self):
        self.assertFalse(ie.extraction_enabled(False))
        self.assertTrue(ie.extraction_enabled(True))
        with mock.patch.dict("os.environ", {"VI_INSIGHT_EXTRACTION": "1"}):
            self.assertTrue(ie.extraction_enabled(False))

    def test_la_variable_acepta_una_lista_de_clientes(self):
        with mock.patch.dict("os.environ", {"VI_INSIGHT_EXTRACTION": "farma24, Tigo"}):
            self.assertTrue(ie.extraction_enabled(False, "farma24"))
            self.assertTrue(ie.extraction_enabled(False, "tigo"))       # sin distinguir mayúsculas
            self.assertFalse(ie.extraction_enabled(False, "mens_fashion"))
            self.assertFalse(ie.extraction_enabled(False))               # sin client_id no se asume
            self.assertTrue(ie.extraction_enabled(True, "mens_fashion"))  # el config.yaml del cliente manda
        with mock.patch.dict("os.environ", {"VI_INSIGHT_EXTRACTION": "all"}):
            self.assertTrue(ie.extraction_enabled(False, "cualquiera"))
        with mock.patch.dict("os.environ", {"VI_INSIGHT_EXTRACTION": ""}):
            self.assertFalse(ie.extraction_enabled(False, "farma24"))


if __name__ == "__main__":
    unittest.main()
