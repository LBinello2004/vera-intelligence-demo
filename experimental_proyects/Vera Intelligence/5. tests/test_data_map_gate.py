"""Gate v2 de la actualización del Data Map: contra el SQL dorado (sin Gemini ni base: todo inyectado)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import data_map_gate as gate  # noqa: E402

VIEW = "dashboard_v2.vw_acme_demografia"


def data_map(fields=("tipointeraccion", "categorianocompra"), extra_sources=(), top=("metadata", "sources", "sql_rules")):
    sources = {"demography": {"source": VIEW, "fields": {f: {"description": "x"} for f in fields}}}
    for name, view in extra_sources:
        sources[name] = {"source": view, "fields": {"c": {}}}
    return {key: ({} if key != "sources" else sources) for key in top}


def changed_map(**sections):
    """Un candidato que sí cambió algo que el agente lee (reglas SQL), para que el gate le haga preguntas."""
    out = data_map()
    out["sql_rules"] = sections.pop("sql_rules", {"regla": "nueva"})
    out.update(sections)
    return out


SQL_TOTAL = "SELECT COUNT(DISTINCT conversation_id) AS total FROM dashboard_v2.vw_acme_demografia WHERE seller_id = 'Acme' AND usefulforanalysis IS TRUE"
SQL_TIPO = ("SELECT tipointeraccion, COUNT(*) AS total FROM dashboard_v2.vw_acme_demografia WHERE seller_id = 'Acme' "
            "GROUP BY tipointeraccion ORDER BY total DESC")


def result(rows, columns=("total",), truncated=False):
    return json.dumps({"columns": list(columns), "rows": rows, "row_count": len(rows), "truncated": truncated})


class SqlAnalysisTests(unittest.TestCase):
    def test_columnas_por_tabla_ignora_alias_y_funciones(self) -> None:
        cols = gate.physical_columns(SQL_TIPO)
        self.assertEqual(cols[VIEW], {"tipointeraccion", "seller_id"})   # 'total' es un alias, no una columna

    def test_columna_sin_calificar_con_dos_tablas_no_se_asigna(self) -> None:
        sql = ("SELECT a.x, y FROM dashboard_v2.vw_a a JOIN dashboard_v2.vw_b b ON a.id = b.id WHERE a.seller_id = 'T'")
        cols = gate.physical_columns(sql)
        self.assertEqual(cols["dashboard_v2.vw_a"], {"x", "id", "seller_id"})
        self.assertEqual(cols["dashboard_v2.vw_b"], {"id"})   # 'y' sin calificar con 2 tablas: no se asigna a nadie

    def test_una_cte_no_cuenta_como_tabla_fisica(self) -> None:
        sql = "WITH t AS (SELECT conversation_id FROM dashboard_v2.vw_acme_demografia) SELECT COUNT(*) FROM t"
        self.assertEqual(set(gate.physical_columns(sql)), {VIEW})

    def test_campos_que_el_candidato_perdio(self) -> None:
        old, new = data_map(), data_map(fields=("categorianocompra",))
        self.assertEqual(gate.dropped_fields(SQL_TIPO, old, new), [f"{VIEW}: faltan en el candidato tipointeraccion"])
        self.assertEqual(gate.dropped_fields(SQL_TIPO, old, old), [])

    def test_una_columna_que_el_viejo_no_declaraba_no_se_reclama(self) -> None:
        # seller_id no es un campo declarado: que falte en el candidato no es una regresión
        self.assertEqual(gate.dropped_fields(SQL_TOTAL, data_map(fields=("x",)), data_map(fields=("x",))), [])

    def test_fuente_eliminada(self) -> None:
        old, new = data_map(), {"sources": {"otra": {"source": "dashboard_v2.vw_otra", "fields": {"c": {}}}}}
        self.assertEqual(gate.dropped_fields(SQL_TIPO, old, new), [f"{VIEW}: la fuente ya no está en el candidato"])


class StructureTests(unittest.TestCase):
    def test_candidato_identico_no_tiene_problemas(self) -> None:
        self.assertEqual(gate.structural_problems(data_map(), data_map()), [])

    def test_pierde_claves_o_fuentes(self) -> None:
        problems = gate.structural_problems(data_map(extra_sources=[("b", "dashboard_v2.vw_b")]), data_map(top=("metadata", "sources")))
        self.assertTrue(any("claves de primer nivel perdidas: sql_rules" in p for p in problems))
        self.assertTrue(any("fuentes eliminadas: dashboard_v2.vw_b" in p for p in problems))

    def test_sin_fuentes_es_un_problema(self) -> None:
        self.assertEqual(gate.structural_problems(data_map(), {"metadata": {}}), ["el candidato no declara fuentes"])


class TruthAndCoverageTests(unittest.TestCase):
    def test_verdad_de_un_resultado_chico(self) -> None:
        self.assertEqual(gate.truth_from_result(result([[250804]])), ["250804"])
        self.assertEqual(gate.truth_from_result(result([["Compra", 247064], ["No compra", 10486]], ("t", "total"))), ["247064", "10486"])

    def test_un_resultado_largo_o_truncado_o_vacio_no_sirve_como_verdad(self) -> None:
        self.assertEqual(gate.truth_from_result(result([[1], [2], [3], [4]])), [])
        self.assertEqual(gate.truth_from_result(result([[1]], truncated=True)), [])
        self.assertEqual(gate.truth_from_result(result([])), [])

    def test_los_booleanos_y_textos_no_son_numeros(self) -> None:
        self.assertEqual(gate.truth_from_result(result([[True, "abc", None, 7]], ("a", "b", "c", "d"))), ["7"])

    def test_la_cobertura_ignora_numeros_de_mas_y_acepta_formatos(self) -> None:
        ok, missing = gate.covers(["250804"], "Hay **250.804** conversaciones, sobre 261.823 en total (95,8 %).")
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_falta_un_numero_clave(self) -> None:
        ok, missing = gate.covers(["250804", "10486"], "Hay 250.804 conversaciones.", max_missing=0)
        self.assertFalse(ok)
        self.assertEqual(missing, ["10486"])
        self.assertTrue(gate.covers(["250804", "10486"], "Hay 250.804 conversaciones.", max_missing=1)[0])

    def test_un_conteo_chico_si_se_encuentra(self) -> None:
        # numbers_in descarta enteros de menos de 3 dígitos; con eso un conteo de 93 hacía imposible aprobar la pregunta (hallazgo en Tigo)
        self.assertEqual(gate.covers(["93"], "Hubo **93** casos (0,07 %).", max_missing=0), (True, []))
        self.assertFalse(gate.covers(["93"], "Hubo 150 casos de 140.000.", max_missing=0)[0])

    def test_una_fraccion_vale_como_porcentaje(self) -> None:
        self.assertTrue(gate.covers(["0.2037"], "Es el 20,4 % de las conversaciones.")[0])


class FootprintTests(unittest.TestCase):
    def test_solo_metadata_no_toca_nada(self) -> None:
        old, new = data_map(), data_map()
        new["metadata"] = {"version": "9"}
        fp = gate.change_footprint(old, new)
        self.assertEqual((fp["tables"], fp["global"], fp["limitations"]), ({}, False, False))
        self.assertFalse(gate.question_affected(SQL_TOTAL, fp))

    def test_un_campo_cambiado_afecta_solo_a_las_preguntas_que_lo_usan(self) -> None:
        old, new = data_map(), data_map()
        new["sources"]["demography"]["fields"]["categorianocompra"]["description"] = "otra"
        fp = gate.change_footprint(old, new)
        self.assertEqual(fp["tables"], {VIEW: {"categorianocompra"}})
        self.assertFalse(gate.question_affected(SQL_TIPO, fp))          # usa tipointeraccion
        sql = SQL_TIPO.replace("tipointeraccion", "categorianocompra")
        self.assertTrue(gate.question_affected(sql, fp))

    def test_una_seccion_global_afecta_a_todo_y_limitations_aparte(self) -> None:
        fp = gate.change_footprint(data_map(), changed_map())
        self.assertTrue(fp["global"] and gate.question_affected(SQL_TOTAL, fp))
        new = data_map(top=("metadata", "sources", "sql_rules", "limitations"))
        new["limitations"] = ["nueva"]
        fp = gate.change_footprint(data_map(top=("metadata", "sources", "sql_rules", "limitations")), new)
        self.assertEqual((fp["global"], fp["limitations"]), (False, True))

    def test_sql_que_no_se_puede_analizar_se_considera_afectado(self) -> None:
        fp = {"tables": {VIEW: {"x"}}, "global": False, "limitations": False}
        self.assertTrue(gate.question_affected("esto no es sql", fp))
        self.assertTrue(gate.question_affected(None, fp))


class RunGateTests(unittest.TestCase):
    def bank(self, **extra):
        return {"preguntas": [dict({"id": "q01", "pregunta": "¿Cuántas conversaciones analizables hay?", "sql": SQL_TOTAL}, **extra)]}

    def run_gate(self, bank=None, new=None, answers=None, truth=250804, **kwargs):
        asked = []
        answers = list(answers if answers is not None else ["Hay 250.804 conversaciones analizables."])

        def ask(question):
            asked.append(question)
            return answers.pop(0) if answers else answers_last[0]

        answers_last = [answers[-1]] if answers else ["sin respuesta"]
        report = gate.run_gate_v2(
            old_data_map=data_map(), new_data_map=new or changed_map(), bank=bank or self.bank(),
            run_sql=lambda sql: result([[truth]]), ask=ask, **kwargs)
        return report, asked

    def test_pasa_cuando_el_candidato_responde_con_el_numero_correcto(self) -> None:
        report, asked = self.run_gate()
        self.assertTrue(report["passed"])
        self.assertEqual(report["llm_questions_checked"], 1)
        self.assertEqual(len(asked), 1)

    def test_lo_que_el_data_map_vigente_tampoco_menciona_no_es_una_regresion(self) -> None:
        bank = self.bank(max_missing_numbers=0)
        report, _ = self.run_gate(answers=["Hay 100 conversaciones."] * 2, bank=bank, baseline_ask=lambda q: "Hay 100 conversaciones.")
        self.assertTrue(report["passed"])
        self.assertFalse(report["questions"][0]["regression"])

    def test_si_el_vigente_si_lo_menciona_y_el_candidato_no_es_regresion(self) -> None:
        bank = self.bank(max_missing_numbers=0)
        report, _ = self.run_gate(answers=["Hay 100 conversaciones."] * 2, bank=bank, baseline_ask=lambda q: "Hay 250.804 conversaciones.")
        self.assertFalse(report["passed"])
        self.assertTrue(report["questions"][0]["regression"])

    def test_el_vigente_solo_se_consulta_si_el_candidato_falla(self) -> None:
        calls = []
        report, _ = self.run_gate(baseline_ask=lambda q: calls.append(q) or "x")
        self.assertTrue(report["passed"])
        self.assertEqual(calls, [])

    def test_un_cambio_que_no_afecta_a_ninguna_pregunta_no_le_pregunta_al_agente(self) -> None:
        new = data_map()
        new["metadata"] = {"version": "9"}
        report, asked = self.run_gate(new=new)
        self.assertTrue(report["passed"])
        self.assertEqual((asked, report["llm_questions_checked"], report["questions_affected"]), ([], 0, 0))

    def test_si_solo_cambio_limitations_se_pregunta_una_sola_centinela(self) -> None:
        top = ("metadata", "sources", "sql_rules", "limitations")
        new = data_map(top=top)
        new["limitations"] = ["nueva"]
        report, asked = gate.run_gate_v2(old_data_map=data_map(top=top), new_data_map=new, bank=self.bank(), run_sql=lambda s: result([[250804]]),
                                         ask=lambda q: "Hay 250.804."), None
        self.assertEqual(report["llm_questions_checked"], 1)

    def test_el_vigente_ya_respondido_no_se_vuelve_a_preguntar(self) -> None:
        cache = {}
        calls = []
        bank = self.bank(max_missing_numbers=0)
        for _ in range(2):
            self.run_gate(answers=["Hay 100."] * 2, bank=bank, baseline_ask=lambda q: calls.append(q) or "Hay 100.", baseline_cache=cache)
        self.assertEqual(len(calls), 2)       # sólo en la primera corrida (2 intentos); la segunda usa el caché

    def test_un_error_determinista_del_sql_dorado_se_informa_pero_no_bloquea(self) -> None:
        class UndefinedColumn(Exception):
            sqlstate = "42883"

        def broken(sql):
            raise UndefinedColumn("operator does not exist: text = boolean")

        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank=self.bank(), run_sql=broken, ask=lambda q: "x")
        self.assertTrue(report["passed"])
        self.assertEqual(report["bank_problems"][0]["id"], self.bank()["preguntas"][0]["id"])

    def test_una_caida_de_la_base_si_bloquea(self) -> None:
        def down(sql):
            raise ConnectionError("connection refused")

        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank=self.bank(), run_sql=down, ask=lambda q: "x")
        self.assertFalse(report["passed"])
        self.assertEqual(report["bank_problems"], [])

    def test_una_respuesta_floja_no_es_regresion_si_un_reintento_extra_la_cubre(self) -> None:
        answers = ["Hay 100."] * 2 + ["Hay 100."] + ["Hay 250.804."]       # 2 intentos, 1 más de arranque de la comparación y el extra ok
        report, asked = self.run_gate(answers=answers, bank=self.bank(max_missing_numbers=0), baseline_ask=lambda q: "Hay 250.804.")
        self.assertTrue(report["passed"])
        self.assertFalse(report["questions"][0]["regression"])
        self.assertGreater(report["questions"][0]["attempts"], 2)

    def test_si_ningun_intento_extra_lo_cubre_sigue_siendo_regresion(self) -> None:
        report, _ = self.run_gate(answers=["Hay 100."], bank=self.bank(max_missing_numbers=0), baseline_ask=lambda q: "Hay 250.804.")
        self.assertFalse(report["passed"])
        self.assertTrue(report["questions"][0]["regression"])

    def test_el_sql_dorado_solo_se_ejecuta_para_las_preguntas_que_se_van_a_usar(self) -> None:
        questions = [{"id": f"q{i}", "pregunta": f"p{i}", "sql": SQL_TOTAL} for i in range(6)]
        executed: list = []

        def run_sql(sql):
            executed.append(sql)
            return result([[250804]])

        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank={"preguntas": questions}, run_sql=run_sql,
                                  ask=lambda q: "Hay 250.804.", llm_questions=2)
        self.assertEqual((len(executed), report["llm_questions_checked"]), (2, 2))      # no se corrieron las 6: alcanzan las 2 que se preguntan

    def test_si_el_cambio_no_afecta_a_nadie_no_se_ejecuta_ningun_sql_dorado(self) -> None:
        new = data_map()
        new["metadata"] = {"version": "9"}
        executed: list = []
        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=new, bank=self.bank(),
                                  run_sql=lambda sql: executed.append(sql) or result([[1]]), ask=lambda q: "x")
        self.assertEqual((executed, report["passed"]), ([], True))

    def test_las_preguntas_al_agente_corren_a_la_vez(self) -> None:
        import threading
        import time as _time
        active, peak, lock = [0], [0], threading.Lock()

        def ask(question):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.2)
            with lock:
                active[0] -= 1
            return "Hay 250.804."

        questions = [{"id": f"q{i}", "pregunta": f"p{i}", "sql": SQL_TOTAL} for i in range(4)]
        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank={"preguntas": questions},
                                  run_sql=lambda sql: result([[250804]]), ask=ask)
        self.assertTrue(report["passed"])
        self.assertEqual(peak[0], 4)
        self.assertEqual([r["id"] for r in report["questions"]], ["q0", "q1", "q2", "q3"])

    def multi(self, answer, baseline, cache=None, truth=(100000, 2000, 120000), max_missing=1):
        bank = {"preguntas": [{"id": "q1", "pregunta": "p", "sql": SQL_TOTAL, "max_missing_numbers": max_missing}]}
        return gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank=bank,
                                run_sql=lambda sql: result([list(truth)], columns=("a", "b", "c")), ask=lambda q: answer,
                                baseline_ask=lambda q: baseline, baseline_cache=cache if cache is not None else {})

    def test_un_numero_nuevo_que_falta_se_tolera_pero_dos_son_regresion(self) -> None:
        # El vigente da los tres números; el candidato omite uno (un denominador con otra base): pasa. Si omite dos, es regresión.
        both = "Son 100.000 de 120.000, con 2.000 casos."
        self.assertTrue(self.multi("Son 100.000 casos y 2.000 sin dato.", both)["passed"])
        report = self.multi("Hay 100.000 casos.", both)
        self.assertFalse(report["passed"])
        self.assertTrue(report["questions"][0]["regression"])

    def test_el_cache_del_vigente_se_guarda_por_posicion_y_sirve_aunque_los_numeros_cambien(self) -> None:
        cache: dict = {}
        self.multi("Hay 100.000 casos.", "Son 100.000 casos y 2.000 sin dato.", cache=cache)       # el vigente omite el tercer número
        self.assertEqual(cache, {"q1": [2]})
        calls: list = []
        # al día siguiente la tabla creció: la verdad cambia, el caché por posición sigue valiendo y no se vuelve a preguntar al vigente
        report = gate.run_gate_v2(
            old_data_map=data_map(), new_data_map=changed_map(),
            bank={"preguntas": [{"id": "q1", "pregunta": "p", "sql": SQL_TOTAL, "max_missing_numbers": 0}]},
            run_sql=lambda sql: result([[101000, 2020, 121500]], columns=("a", "b", "c")),
            ask=lambda q: "Hay 101.000 casos y 2.020 sin dato.", baseline_ask=lambda q: calls.append(q) or "x", baseline_cache=cache)
        self.assertTrue(report["passed"])
        self.assertEqual(calls, [])

    def test_un_cache_en_el_formato_viejo_con_valores_se_vuelve_a_medir(self) -> None:
        cache = {"q1": ["75791", "1904"]}                 # números de otro día: no sirven (causó un falso rechazo real en Maga)
        report = self.multi("Hay 100.000 casos.", "Son 100.000 casos y 2.000 sin dato.", cache=cache)
        self.assertEqual(cache["q1"], [2])
        self.assertTrue(all(isinstance(i, int) for i in cache["q1"]))

    def test_el_contexto_de_mas_no_lo_rechaza(self) -> None:
        # El caso real de Farma 24 (17/9): la respuesta nueva agrega el total y el % de cobertura.
        report, _ = self.run_gate(answers=["Hay **250.804** analizables sobre 261.823 interacciones (95,8 % de cobertura)."])
        self.assertTrue(report["passed"])

    def test_una_respuesta_con_un_numero_equivocado_se_rechaza_despues_de_reintentar(self) -> None:
        report, asked = self.run_gate(answers=["Hay 100 conversaciones.", "Hay 100 conversaciones."], bank=self.bank(max_missing_numbers=0))
        self.assertFalse(report["passed"])
        self.assertEqual(len(asked), 2)
        self.assertEqual(report["questions"][0]["missing_numbers"], ["250804"])

    def test_la_segunda_pregunta_puede_salvar_la_primera(self) -> None:
        report, asked = self.run_gate(answers=["Hay 100 conversaciones.", "Hay 250.804 conversaciones."], bank=self.bank(max_missing_numbers=0))
        self.assertTrue(report["passed"])
        self.assertEqual(report["questions"][0]["attempts"], 2)

    def test_un_campo_perdido_rechaza_aunque_la_respuesta_este_bien(self) -> None:
        bank = {"preguntas": [{"id": "q03", "pregunta": "¿Cómo se reparten las interacciones?", "sql": SQL_TIPO}]}
        report, _ = self.run_gate(bank=bank, new=data_map(fields=("categorianocompra",)), answers=["250.804"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["questions"][0]["dropped_fields"], [f"{VIEW}: faltan en el candidato tipointeraccion"])

    def test_un_problema_de_estructura_rechaza(self) -> None:
        report, _ = self.run_gate(new=data_map(top=("metadata", "sources")))
        self.assertFalse(report["passed"])
        self.assertTrue(report["structural_problems"])

    def test_si_el_sql_dorado_no_se_puede_ejecutar_no_se_promueve_solo(self) -> None:
        def boom(sql):
            raise RuntimeError("timeout")

        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank=self.bank(), run_sql=boom,
                                  ask=lambda q: "x")
        self.assertFalse(report["passed"])
        self.assertFalse(report["questions"][0]["sql_ok"])

    def test_omit_from_numeric_gate_no_pregunta_al_modelo(self) -> None:
        report, asked = self.run_gate(bank=self.bank(omit_from_numeric_gate=True))
        self.assertTrue(report["passed"])
        self.assertEqual(asked, [])

    def test_se_pregunta_a_lo_sumo_llm_questions_preguntas(self) -> None:
        bank = {"preguntas": [{"id": f"q{i}", "pregunta": f"pregunta {i}", "sql": SQL_TOTAL} for i in range(6)]}
        report, asked = self.run_gate(bank=bank, answers=["250.804"] * 6, llm_questions=2)
        self.assertEqual(len(asked), 2)
        self.assertEqual(report["llm_questions_checked"], 2)
        self.assertTrue(report["passed"])

    def test_resultados_largos_se_verifican_solo_de_forma_determinista(self) -> None:
        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank=self.bank(),
                                  run_sql=lambda sql: result([[i] for i in range(10)]), ask=lambda q: self.fail("no debe preguntar"))
        self.assertTrue(report["passed"])
        self.assertEqual(report["llm_questions_checked"], 0)

    def test_si_el_agente_falla_se_rechaza_con_el_error(self) -> None:
        def ask(question):
            raise RuntimeError("503 del modelo")

        report = gate.run_gate_v2(old_data_map=data_map(), new_data_map=changed_map(), bank=self.bank(),
                                  run_sql=lambda sql: result([[250804]]), ask=ask)
        self.assertFalse(report["passed"])
        self.assertIn("503", report["questions"][0]["error"])


if __name__ == "__main__":
    unittest.main()
