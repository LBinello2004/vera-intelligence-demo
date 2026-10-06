import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import question_planner as qp  # noqa: E402

DATA_MAP = {"sources": {"x": {"fields": {"campo_a": {"description": "Algo A"}, "campo_b": {}}}}}


def _gen(payload):
    return lambda **kw: SimpleNamespace(text=json.dumps(payload), usage_metadata=None)


class PlannerTests(unittest.TestCase):
    def test_campo_inexistente_se_degrada_a_no_existe(self):
        plan = {"metricas": [{"pedida": "cuotas", "campo": "src.cuotas_sin_interes", "nota": "x"}]}
        out = qp.ground_plan(plan, {"campo_a"})
        self.assertIsNone(out["metricas"][0]["campo"])
        self.assertIn("no existe", out["metricas"][0]["nota"])

    def test_campo_real_se_conserva(self):
        plan = {"metricas": [{"pedida": "a", "campo": "x.campo_a"}]}
        self.assertEqual(qp.ground_plan(plan, {"campo_a"})["metricas"][0]["campo"], "x.campo_a")

    def test_render_metrica_inexistente_pide_decirlo(self):
        text = qp.render_plan({"metricas": [{"pedida": "ticket", "campo": None, "nota": "ninguno"}]})
        self.assertIn("NO está medida", text)
        self.assertIn("primera oración", text)
        # Alcance SQL + búsqueda (2026-10-06): sin número, con alternativas honestas.
        self.assertIn("No des ningún número", text)
        self.assertIn("checklist de análisis", text)
        self.assertIn("sin frecuencia", text)

    def test_render_premisa_y_partes(self):
        text = qp.render_plan({"partes": ["a", "b"], "premisa": {"afirma": "cayó", "verificar": "comparar"}})
        self.assertIn("(2) b", text)
        self.assertIn("cayó", text)

    def test_plan_vacio_no_agrega_nada(self):
        self.assertEqual(qp.render_plan({}), "")

    def test_plan_question_end_to_end_y_failopen(self):
        payload = {"partes": ["p"], "metricas": [{"pedida": "q", "campo": "campo_zzz", "nota": "n"}]}
        out = qp.plan_question("pregunta larga de prueba", client=None, model="m", data_map=DATA_MAP, generate=_gen(payload))
        self.assertIn("NO está medida", out)
        boom = mock.Mock(side_effect=RuntimeError("x"))
        self.assertEqual(qp.plan_question("pregunta larga de prueba", client=None, model="m", data_map=DATA_MAP, generate=boom), "")

    def test_plan_renderizado_en_secciones_numeradas(self):
        plan = {"objetivo": "saber si subió", "tipo": "comparacion", "partes": ["a", "b"],
                "metricas": [{"pedida": "cierre", "campo": "campo_a"}],
                "alcance": {"periodo": "septiembre", "denominador": "analizables", "formato": "tabla por mes",
                            "desagregacion": "tienda", "entidades": [{"tipo": "tienda", "texto": "Toluca"}]},
                "comparacion": {"baseline": "agosto"}, "premisa": {"afirma": "cayó", "verificar": "comparar"},
                "definiciones": {"cierre": "compra efectiva"}, "advertencias": ["base chica"], "usar_busqueda": True}
        text = qp.render_plan(plan)
        for marca in ("1. OBJETIVO", "2. PARTES", "3. MÉTRICAS", "4. ALCANCE", "5. COMPARACIÓN", "6. PREMISA",
                      "8. DEFINICIONES", "9. CUIDADOS", "Denominador", "Formato pedido", "Abrir el resultado por"):
            self.assertIn(marca, text)

    def test_el_planificador_ya_no_conoce_la_lectura_ni_repregunta_conceptos_amplios(self):
        # 2026-10-06: se descartó la lectura de conversaciones con JEV; quedan SQL y búsqueda vectorial.
        prompt = qp._PLANNER_PROMPT
        self.assertIn("SÓLO en estos tres casos", prompt)
        self.assertNotIn("CONCEPTO AMPLIO A CUANTIFICAR", prompt)
        for viejo in ("cuantificable_por_lectura", "via_patron", "JEV", "extract_insight"):
            self.assertNotIn(viejo, prompt)

    def test_usar_busqueda_es_solo_para_lo_cualitativo(self):
        self.assertIn('"usar_busqueda": true SÓLO si algo de lo pedido es CUALITATIVO', qp._PLANNER_PROMPT)

    def test_una_metrica_sin_campo_no_dispara_ninguna_etapa_de_lectura(self):
        stage1 = {"metricas": [{"pedida": "cuotas sin interés", "campo": None, "nota": "ninguno"}], "usar_busqueda": False}
        calls = []
        res = qp.build_plan("¿Qué porcentaje menciona cuotas sin interés?", client=None, model="m", data_map=DATA_MAP,
                            generate=self._generate_for(stage1, calls))
        self.assertEqual(calls, ["planner"])               # una sola llamada: no hay redactor de fichas
        self.assertIn("NO está medida", res.text)
        self.assertNotIn("FICHAS", res.text)
        self.assertNotIn("extract_insight", res.text)

    def test_no_hay_ficha_ni_funciones_de_lectura_en_el_modulo(self):
        for nombre in ("ground_lecturas", "design_pattern_card", "_desglose_hint", "_DEFINER_PROMPT", "_PATTERN_DEFINER_PROMPT"):
            self.assertFalse(hasattr(qp, nombre), nombre)

    def test_repregunta_solo_si_es_critica_y_tiene_formato(self):
        ok = {"falta_info": {"critica": True, "pregunta": "¿A qué asesor te referís? Decime el nombre.", "motivo": "x"}}
        self.assertIsNotNone(qp._valid_clarification(ok))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": False, "pregunta": "¿Cuál tienda querés ver?"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": "true", "pregunta": "¿Cuál tienda querés ver?"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": True, "pregunta": "Decime cuál tienda querés ver"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": True, "pregunta": "¿?"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": True, "pregunta": "¿" + "x" * 400 + "?"}}))
        self.assertIsNone(qp._valid_clarification({}))

    def _generate_for(self, stage1, calls=None):
        def gen(**kw):
            if calls is not None:
                calls.append("planner")
            return SimpleNamespace(text=json.dumps(stage1), usage_metadata=None)
        return gen

    def test_build_plan_devuelve_repregunta_sin_texto(self):
        plan = {"falta_info": {"critica": True, "pregunta": "¿Cómo se llama el asesor que querés revisar?", "motivo": "x"}}
        res = qp.build_plan("¿Cómo le va al asesor?", client=None, model="m", data_map=DATA_MAP,
                            generate=self._generate_for(plan))
        self.assertEqual(res.clarification, "¿Cómo se llama el asesor que querés revisar?")
        self.assertEqual(res.text, "")

    def test_build_plan_no_repregunta_si_no_se_permite_o_esta_apagado(self):
        plan = {"objetivo": "x", "metricas": [{"pedida": "a", "campo": "campo_a"}],
                "falta_info": {"critica": True, "pregunta": "¿Cómo se llama el asesor que querés revisar?"}}
        sin = qp.build_plan("¿Cómo le va al asesor?", client=None, model="m", data_map=DATA_MAP,
                            generate=self._generate_for(plan), allow_clarification=False)
        self.assertIsNone(sin.clarification)
        self.assertIn("MÉTRICAS", sin.text)
        with mock.patch.dict(os.environ, {"VI_PLANNER_CLARIFY": "0"}):
            off = qp.build_plan("¿Cómo le va al asesor?", client=None, model="m", data_map=DATA_MAP,
                                generate=self._generate_for(plan))
        self.assertIsNone(off.clarification)

    def test_contexto_de_conversacion_ignora_el_plan_y_las_herramientas(self):
        def content(role, *texts):
            return SimpleNamespace(role=role, parts=[SimpleNamespace(text=x) for x in texts])
        tool_part = SimpleNamespace(role="user", parts=[SimpleNamespace(text=None)])
        history = [
            content("user", "¿Cómo viene Parque Delta?" + chr(10) * 2 + qp._PLAN_HEADER + chr(10) + "1. OBJETIVO ..."),
            content("model", "Parque Delta tiene una tasa de cierre de 70 %."),
            tool_part,
            content("user", "Y la otra semana?"),
        ]
        ctx = qp.conversation_context(history)
        self.assertIn("Usuario: ¿Cómo viene Parque Delta?", ctx)
        self.assertIn("Asistente: Parque Delta tiene", ctx)
        self.assertNotIn("OBJETIVO", ctx)
        self.assertEqual(qp.conversation_context([]), "")
        largo = [content("user", f"pregunta {i}") for i in range(10)]
        self.assertEqual(len(qp.conversation_context(largo, max_turns=2).splitlines()), 4)

    def test_el_contexto_llega_al_prompt_del_planificador(self):
        vistos = []

        def gen(**kw):
            vistos.append(kw["contents"])
            return SimpleNamespace(text=json.dumps({"objetivo": "x"}), usage_metadata=None)

        qp.build_plan("¿Y esa tienda?", client=None, model="m", data_map=DATA_MAP, generate=gen,
                      context="Usuario: ¿Cómo viene Parque Delta?")
        qp.build_plan("¿Cuál es la tasa de cierre?", client=None, model="m", data_map=DATA_MAP, generate=gen)
        self.assertIn("Usuario: ¿Cómo viene Parque Delta?", vistos[0])
        self.assertIn("(primer mensaje)", vistos[1])

    def test_flag_apaga(self):
        with mock.patch.dict(os.environ, {"VI_PLANNER": "0"}):
            self.assertFalse(qp.planner_enabled())


if __name__ == "__main__":
    unittest.main()
