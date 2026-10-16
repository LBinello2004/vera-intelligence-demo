"""Preguntas amplias y vagas (2026-10-16): el planificador no inventa métricas faltantes ni degrada un campo real por un nombre recortado,
y una pregunta general de evolución se responde con valores por defecto."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import question_planner as qp  # noqa: E402

KNOWN = {"i22_dolor_o_molestia_principal_del_cliente", "i09_causa_principal_de_retiro", "i07_causa_principal_de_no_retencion",
         "i07_detalle_causa_de_no_retencion", "i09_causa_principal_de_retiro_detalle", "a42_intenta_cierre_comercial_o_confirma_decision", "region"}


class ResolveFieldTests(unittest.TestCase):
    def test_exacto_y_con_mayusculas_o_prefijo_de_fuente(self):
        self.assertEqual(qp._resolve_field("region", KNOWN), "region")
        self.assertEqual(qp._resolve_field("demography.REGION", KNOWN), "region")

    def test_un_nombre_recortado_se_resuelve_al_unico_campo_real(self):
        self.assertEqual(qp._resolve_field("categorical_insights.i22_dolor_o_molestia_principal", KNOWN), "i22_dolor_o_molestia_principal_del_cliente")
        self.assertEqual(qp._resolve_field("a42_intenta_cierre_comercial", KNOWN), "a42_intenta_cierre_comercial_o_confirma_decision")

    def test_ambiguo_corto_o_inexistente_no_se_inventa(self):
        self.assertIsNone(qp._resolve_field("i09_causa_principal", KNOWN))    # hay dos campos que empiezan igual: ambiguo
        self.assertIsNone(qp._resolve_field("i07", KNOWN))                    # demasiado corto
        self.assertIsNone(qp._resolve_field("volumen_de_atenciones", KNOWN))
        self.assertIsNone(qp._resolve_field("", KNOWN))

    def test_el_plan_conserva_la_fuente_y_acepta_listas(self):
        plan = {"metricas": [
            {"pedida": "dolor del cliente", "campo": "categorical_insights.i22_dolor_o_molestia_principal"},
            {"pedida": "familia", "campo": ["a42_intenta_cierre_comercial", "region", "inventado_largo_que_no_existe"]},
            {"pedida": "volumen", "campo": "volumen_de_atenciones"}]}
        out = [m["campo"] for m in qp.ground_plan(plan, KNOWN)["metricas"]]
        self.assertEqual(out, ["categorical_insights.i22_dolor_o_molestia_principal_del_cliente",
                               ["a42_intenta_cierre_comercial_o_confirma_decision", "region"], None])


class PromptTests(unittest.TestCase):
    def test_los_conteos_de_conversaciones_no_son_una_metrica_faltante(self):
        self.assertIn("Los CONTEOS de conversaciones", qp._PLANNER_PROMPT)
        self.assertIn("COUNT sobre las vistas", qp._PLANNER_PROMPT)
        self.assertIn("afluencia física", qp._PLANNER_PROMPT)

    def test_una_pregunta_general_de_evolucion_no_es_critica(self):
        self.assertIn("Una pregunta GENERAL de evolución sobre el negocio", qp._PLANNER_PROMPT)
        self.assertIn("el último mes contra el mes anterior", qp._PLANNER_PROMPT)


if __name__ == "__main__":
    unittest.main()
