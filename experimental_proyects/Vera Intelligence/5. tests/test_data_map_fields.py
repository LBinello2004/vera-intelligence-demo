"""Campos declarados en grupos `fields_*` (caso Tigo/performance): el planificador, el gate, el chequeo de columnas y el registro de cambios
tienen que verlos igual que a los de `fields`. Antes solo se leía `fields` y el planificador declaraba "no medido" métricas que existen."""
from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import data_map_column_drift as drift  # noqa: E402
import data_map_fields as dmf  # noqa: E402
import data_map_gate as gate  # noqa: E402
import data_map_log as dml  # noqa: E402
import question_planner as qp  # noqa: E402

SI_NO = ["Sí", "No", "N/A"]


def tigo_like() -> dict:
    return {
        "metadata": {"v": 1},
        "sources": {
            "performance": {
                "source": "dashboard_v2.vw_x_rendimiento",
                "tenant_field": "seller_id",
                "fields_a_apertura": {
                    "description": "Etapa de apertura.",
                    "a07_valida_identidad": {"configured_values": SI_NO, "type": "text", "description": "Valida la identidad antes de gestionar."},
                },
                "fields_a_retencion": {
                    "description": "Retención.",
                    "a30_retencion_exitosa": {"configured_values": SI_NO, "type": "text", "description": "Retención exitosa."},
                },
                "fields_t_triggers": {"t01_pide_dinero": {"configured_values": ["Sí", "No"], "type": "text", "description": "Pide dinero."}},
            },
            "demography": {
                "source": "dashboard_v2.vw_x_demografia",
                "tenant_field": "seller_id",
                "fields": {"region": {"type": "text", "description": "Región."}},
            },
        },
    }


class HelperTests(unittest.TestCase):
    def test_junta_fields_y_los_grupos_y_no_cuenta_la_description_del_grupo_como_campo(self):
        src = tigo_like()["sources"]["performance"]
        self.assertEqual(set(dmf.source_fields(src)), {"a07_valida_identidad", "a30_retencion_exitosa", "t01_pide_dinero"})
        mixed = {"fields": {"uno": {"type": "text"}}, "fields_g": {"description": "x", "dos": {"type": "text"}}}
        self.assertEqual(set(dmf.source_fields(mixed)), {"uno", "dos"})
        self.assertEqual(dmf.group_keys(mixed), ["fields_g"])

    def test_entradas_raras_no_rompen(self):
        for weird in (None, "texto", [], {}, {"fields": None}, {"fields": "x"}, {"fields_g": "no es dict"}, {"fields_g": {"description": "solo texto"}}):
            self.assertEqual(dmf.source_fields(weird), {})

    def test_el_contenido_que_no_es_de_campos_incluye_las_descripciones_de_grupo(self):
        a = tigo_like()["sources"]["performance"]
        rest = dmf.non_field_content(a)
        self.assertEqual(rest["source"], "dashboard_v2.vw_x_rendimiento")
        self.assertEqual(rest["fields_a_apertura::meta"], {"description": "Etapa de apertura."})
        self.assertNotIn("fields_a_apertura", rest)
        self.assertNotIn("fields_t_triggers::meta", rest)         # ese grupo no tiene texto propio


class PlannerCatalogTests(unittest.TestCase):
    def test_el_catalogo_del_planificador_ve_los_campos_agrupados(self):
        catalog, names = qp.build_field_catalog(tigo_like())
        self.assertTrue({"a07_valida_identidad", "a30_retencion_exitosa", "t01_pide_dinero", "region"} <= names)
        self.assertIn("performance.a30_retencion_exitosa: Retención exitosa.", catalog)

    def test_un_campo_agrupado_que_el_planificador_nombra_ya_no_se_degrada_a_no_medido(self):
        _, names = qp.build_field_catalog(tigo_like())
        plan = {"metricas": [{"pedida": "validar identidad", "campo": "performance.a07_valida_identidad", "nota": ""},
                             {"pedida": "inventada", "campo": "performance.no_existe", "nota": ""}]}
        grounded = qp.ground_plan(plan, names)
        self.assertEqual(grounded["metricas"][0]["campo"], "performance.a07_valida_identidad")
        self.assertIsNone(grounded["metricas"][1]["campo"])


class PlannerFamilyAndDimensionTests(unittest.TestCase):
    """Métricas que son una FAMILIA de campos y atributos para agrupar (asesor, región): no son "métrica no medida"."""

    def test_el_catalogo_lista_las_dimensiones_para_agrupar_sin_tratarlas_como_metricas(self):
        dm = tigo_like()
        dm["profiles"] = {"standard_conversation": {"optional_dimensions": ["store_name", "employee_full_name", "region"]}}
        catalog, names = qp.build_field_catalog(dm)
        self.assertIn("DIMENSIONES PARA AGRUPAR O FILTRAR", catalog)
        self.assertIn("employee_full_name", catalog)
        self.assertNotIn("employee_full_name", names)          # no es una métrica: el planificador no puede usarla como `campo`
        self.assertNotIn("DIMENSIONES", qp.build_field_catalog(tigo_like())[0])

    def test_una_metrica_con_lista_de_campos_conserva_solo_los_que_existen(self):
        _, names = qp.build_field_catalog(tigo_like())
        plan = {"metricas": [
            {"pedida": "criterios", "campo": ["performance.a07_valida_identidad", "performance.a30_retencion_exitosa", "performance.inventado"]},
            {"pedida": "uno solo", "campo": ["performance.a07_valida_identidad", "performance.inventado"]},
            {"pedida": "ninguno", "campo": ["performance.x", "performance.y"]},
            {"pedida": "vacía", "campo": []},
            {"pedida": "mal tipo", "campo": [1, None]}]}
        out = [m["campo"] for m in qp.ground_plan(plan, names)["metricas"]]
        self.assertEqual(out, [["performance.a07_valida_identidad", "performance.a30_retencion_exitosa"], "performance.a07_valida_identidad", None, None, None])

    def test_el_plan_dice_que_la_familia_existe_y_cada_caso_sin_campo_sigue_siendo_no_medida(self):
        plan = {"objetivo": "x", "tipo": "numerica", "partes": ["a"], "metricas": [
            {"pedida": "criterios del checklist", "campo": ["performance.a07_valida_identidad", "performance.a30_retencion_exitosa"], "nota": None},
            {"pedida": "deuda", "campo": None, "nota": "ninguno"}]}
        text = qp.render_plan(plan)
        self.assertIn("«criterios del checklist»: existe, se mide con VARIOS campos: performance.a07_valida_identidad, performance.a30_retencion_exitosa.", text)
        self.assertIn("no la declares como no medida", text)
        self.assertIn("«deuda»: NO está medida", text)

    def test_el_prompt_explica_la_familia_de_campos_y_las_dimensiones(self):
        self.assertIn("una LISTA de nombres exactos", qp._PLANNER_PROMPT)
        self.assertIn("DIMENSIONES", qp._PLANNER_PROMPT)
        self.assertIn('"el mejor asesor" NO son una métrica faltante', qp._PLANNER_PROMPT)


class GateTests(unittest.TestCase):
    def test_los_campos_declarados_incluyen_los_agrupados(self):
        self.assertEqual(gate.declared_fields(tigo_like())["dashboard_v2.vw_x_rendimiento"],
                         {"a07_valida_identidad", "a30_retencion_exitosa", "t01_pide_dinero"})

    def test_un_cambio_dentro_de_un_grupo_marca_ese_campo_y_no_toda_la_fuente(self):
        old, new = tigo_like(), tigo_like()
        new["sources"]["performance"]["fields_a_retencion"]["a30_retencion_exitosa"]["description"] = "Retención lograda (cambió)."
        footprint = gate.change_footprint(old, new)
        self.assertEqual(footprint["tables"], {"dashboard_v2.vw_x_rendimiento": {"a30_retencion_exitosa"}})
        self.assertFalse(footprint["global"])

    def test_un_campo_quitado_de_un_grupo_se_detecta(self):
        old, new = tigo_like(), tigo_like()
        del new["sources"]["performance"]["fields_t_triggers"]["t01_pide_dinero"]
        self.assertEqual(gate.change_footprint(old, new)["tables"], {"dashboard_v2.vw_x_rendimiento": {"t01_pide_dinero"}})

    def test_cambiar_el_texto_de_un_grupo_o_una_clave_de_la_fuente_marca_toda_la_fuente(self):
        for mutate in (lambda m: m["sources"]["performance"]["fields_a_retencion"].update(description="otra"),
                       lambda m: m["sources"]["performance"].update(tenant_field="otro")):
            old, new = tigo_like(), tigo_like()
            mutate(new)
            self.assertEqual(gate.change_footprint(old, new)["tables"], {"dashboard_v2.vw_x_rendimiento": {"*"}})

    def test_sin_cambios_no_hay_huella(self):
        self.assertEqual(gate.change_footprint(tigo_like(), deepcopy(tigo_like()))["tables"], {})


class DriftTests(unittest.TestCase):
    def test_el_chequeo_de_columnas_incluye_los_campos_agrupados(self):
        columns = {"dashboard_v2.vw_x_rendimiento": {"a07_valida_identidad", "t01_pide_dinero"}, "dashboard_v2.vw_x_demografia": {"region"}}
        findings = drift.find_drift(tigo_like(), columns)
        self.assertEqual(findings, [{"source": "performance", "view": "dashboard_v2.vw_x_rendimiento", "view_not_found": False,
                                     "missing": ["a30_retencion_exitosa"]}])
        columns["dashboard_v2.vw_x_rendimiento"].add("a30_retencion_exitosa")
        self.assertEqual(drift.find_drift(tigo_like(), columns), [])


class LogTests(unittest.TestCase):
    def test_el_resumen_de_cambios_ve_los_campos_agrupados(self):
        old, new = tigo_like(), tigo_like()
        new["sources"]["performance"]["fields_a_retencion"]["a31_nuevo"] = {"type": "text", "description": "Nuevo."}
        del new["sources"]["performance"]["fields_t_triggers"]["t01_pide_dinero"]
        summary = dml.summarize_change(old, new)
        self.assertEqual(summary["campos_agregados"], {"performance": ["a31_nuevo"]})
        self.assertEqual(summary["campos_quitados"], {"performance": ["t01_pide_dinero"]})


if __name__ == "__main__":
    unittest.main()
