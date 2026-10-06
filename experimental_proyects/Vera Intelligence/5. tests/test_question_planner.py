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
        self.assertIn("NO existe", text)
        self.assertIn("primera oración", text)

    def test_render_premisa_y_partes(self):
        text = qp.render_plan({"partes": ["a", "b"], "premisa": {"afirma": "cayó", "verificar": "comparar"}})
        self.assertIn("(2) b", text)
        self.assertIn("cayó", text)

    def test_plan_vacio_no_agrega_nada(self):
        self.assertEqual(qp.render_plan({}), "")

    def test_plan_question_end_to_end_y_failopen(self):
        payload = {"partes": ["p"], "metricas": [{"pedida": "q", "campo": "campo_zzz", "nota": "n"}]}
        out = qp.plan_question("pregunta larga de prueba", client=None, model="m", data_map=DATA_MAP, generate=_gen(payload))
        self.assertIn("NO existe", out)
        boom = mock.Mock(side_effect=RuntimeError("x"))
        self.assertEqual(qp.plan_question("pregunta larga de prueba", client=None, model="m", data_map=DATA_MAP, generate=boom), "")

    def test_ficha_de_lectura_se_renderiza_y_valida_la_poblacion(self):
        ficha_ok = {"metrica": "x", "pregunta": "¿El cliente pide algo concreto?", "criterio_si": "pide algo concreto y claro",
                    "criterio_no": "lo ofrece el vendedor; solo pregunta el precio; comentario entre empleados",
                    "poblacion": {"campo": "src.campo_a", "valor": "v"}}
        ficha_campo_falso = dict(ficha_ok, metrica="y", poblacion={"campo": "src.campo_inventado", "valor": "v"})
        ficha_sin_exclusiones = dict(ficha_ok, metrica="z", criterio_no="no")
        plan = {"metricas": [{"pedida": "x", "campo": None, "nota": "n", "cuantificable_por_lectura": True}],
                "lecturas": [ficha_ok, ficha_campo_falso, ficha_sin_exclusiones]}
        qp.ground_lecturas(plan, {"campo_a"})
        self.assertEqual(len(plan["lecturas"]), 2)  # la ficha sin exclusiones se descarta
        self.assertEqual(plan["lecturas"][0]["poblacion"]["campo"], "src.campo_a")
        self.assertIsNone(plan["lecturas"][1]["poblacion"])
        con = qp.render_plan(plan, extraction_available=True)
        self.assertIn("Ficha de lectura", con)
        self.assertIn("campo_estructurado=«campo_a»", con)
        self.assertNotIn("Ficha de lectura", qp.render_plan(plan, extraction_available=False))

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

    def test_el_planificador_repregunta_conceptos_amplios_pero_no_conductas_concretas(self):
        # 2026-10-06: "¿Cuántas conversaciones hablan de deuda?" dio 31 % contando palabras de mora y 50 % leyendo menciones implícitas.
        prompt = qp._PLANNER_PROMPT
        self.assertIn("(d) CONCEPTO AMPLIO A CUANTIFICAR LEYENDO", prompt)
        self.assertIn("cuatro casos", prompt)
        self.assertIn("CONDUCTA CONCRETA ya nombrada", prompt)

    def test_la_ficha_lleva_palabras_validas_y_se_pasan_junto_a_los_criterios(self):
        plan = {"lecturas": [{"pregunta": "¿El vendedor ofrece cuotas sin interés al cliente?", "criterio_si": "Menciona cuotas sin interés.",
                              "criterio_no": "Cuotas con interés o recargo; el cliente pregunta y el vendedor dice que no.",
                              "palabras": ["cuotas sin interes", " sin recargo ", "x", 5, "a" * 60]}]}
        qp.ground_lecturas(plan, set())
        self.assertEqual(plan["lecturas"][0]["palabras"], ["cuotas sin interes", "sin recargo"])
        text = qp.render_plan({**plan, "metricas": [{"pedida": "cuotas", "campo": None, "cuantificable_por_lectura": True}]},
                              extraction_available=True)
        self.assertIn("terminos_literales=«cuotas sin interes; sin recargo»", text)
        plan["lecturas"][0]["palabras"] = "no es una lista"
        qp.ground_lecturas(plan, set())
        self.assertEqual(plan["lecturas"][0]["palabras"], [])

    def test_contar_por_lectura_no_necesita_busqueda_semantica(self):
        # 2026-10-06: "¿en qué porcentaje mencionan cuotas sin interés?" disparaba búsqueda y conteo de patrones (237 s, ~US$ 0,9)
        # porque el planificador marcaba usar_busqueda=true para todo lo que no era un campo.
        self.assertIn("contar por lectura NO necesita la búsqueda", qp._PLANNER_PROMPT)

    def test_repregunta_solo_si_es_critica_y_tiene_formato(self):
        ok = {"falta_info": {"critica": True, "pregunta": "¿A qué asesor te referís? Decime el nombre.", "motivo": "x"}}
        self.assertIsNotNone(qp._valid_clarification(ok))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": False, "pregunta": "¿Cuál tienda querés ver?"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": "true", "pregunta": "¿Cuál tienda querés ver?"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": True, "pregunta": "Decime cuál tienda querés ver"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": True, "pregunta": "¿?"}}))
        self.assertIsNone(qp._valid_clarification({"falta_info": {"critica": True, "pregunta": "¿" + "x" * 400 + "?"}}))
        self.assertIsNone(qp._valid_clarification({}))

    def _generate_for(self, stage1, stage2=None, calls=None):
        def gen(**kw):
            if calls is not None:
                calls.append("definer" if "REDACTOR DE DEFINICIONES" in kw["contents"] else "planner")
            payload = stage2 if ("REDACTOR DE DEFINICIONES" in kw["contents"]) else stage1
            return SimpleNamespace(text=json.dumps(payload), usage_metadata=None)
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

    def test_definer_solo_corre_si_hay_extraccion_y_algo_para_leer(self):
        stage1 = {"metricas": [{"pedida": "cuotas", "campo": None, "nota": "ninguno", "cuantificable_por_lectura": True}]}
        stage2 = {"fichas": [{"metrica": "cuotas", "pregunta": "¿El vendedor ofrece pagar en cuotas sin interés?",
                              "criterio_si": "ofrece cuotas sin interés explícitamente",
                              "criterio_no": "descuentos con tarjeta; cuotas con interés; mención genérica de medios de pago",
                              "poblacion": None, "nivel_de_ambiguedad": "baja"}]}
        calls = []
        res = qp.build_plan("¿Cuántas veces se ofrecen cuotas sin interés?", client=None, model="m", data_map=DATA_MAP,
                            generate=self._generate_for(stage1, stage2, calls), extraction_available=True)
        self.assertEqual(calls, ["planner", "definer"])
        self.assertIn("7. FICHAS DE LECTURA", res.text)
        calls2 = []
        res2 = qp.build_plan("¿Cuántas veces se ofrecen cuotas sin interés?", client=None, model="m", data_map=DATA_MAP,
                             generate=self._generate_for(stage1, stage2, calls2), extraction_available=False)
        self.assertEqual(calls2, ["planner"])
        self.assertNotIn("FICHAS", res2.text)

    def test_falla_del_definer_no_rompe_el_plan(self):
        stage1 = {"metricas": [{"pedida": "cuotas", "campo": None, "nota": "n", "cuantificable_por_lectura": True}]}

        def gen(**kw):
            if "REDACTOR DE DEFINICIONES" in kw["contents"]:
                raise RuntimeError("boom")
            return SimpleNamespace(text=json.dumps(stage1), usage_metadata=None)

        res = qp.build_plan("¿Cuántas veces se ofrecen cuotas sin interés?", client=None, model="m", data_map=DATA_MAP,
                            generate=gen, extraction_available=True)
        self.assertIn("MÉTRICAS", res.text)
        self.assertEqual(res.plan["lecturas"], [])

    def test_la_ficha_lleva_desglosar_por_segun_la_desagregacion_del_plan(self):
        ficha = {"metrica": "x", "pregunta": "¿El cliente pide algo concreto?", "criterio_si": "pide algo concreto y claro",
                 "criterio_no": "lo ofrece el vendedor; solo pregunta el precio; comentario entre empleados"}
        for texto, esperado in (("por tienda", "tienda"), ("evolución por mes", "mes"), ("por semana", "semana")):
            plan = {"metricas": [{"pedida": "x", "campo": None, "cuantificable_por_lectura": True}],
                    "alcance": {"desagregacion": texto}, "lecturas": [ficha]}
            self.assertIn(f"desglosar_por=«{esperado}»", qp.render_plan(plan, extraction_available=True))
        plan_sin = {"metricas": [], "alcance": {"desagregacion": None}, "lecturas": [ficha]}
        self.assertNotIn("desglosar_por", qp.render_plan(plan_sin, extraction_available=True))

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

    def test_metrica_via_patron_no_recibe_ficha_y_el_plan_indica_el_puente(self):
        stage1 = {"metricas": [{"pedida": "frecuencia del primer patrón", "campo": None, "nota": "n",
                                "cuantificable_por_lectura": True, "via_patron": True}],
                  "usar_busqueda": True}
        calls = []
        res = qp.build_plan("Mostrame los patrones y medí el primero", client=None, model="m", data_map=DATA_MAP,
                            generate=self._generate_for(stage1, {"fichas": [{"nada": 1}]}, calls), extraction_available=True)
        self.assertEqual(calls, ["planner"])          # el redactor de definiciones NO corre
        self.assertEqual(res.plan["lecturas"], [])
        self.assertNotIn("FICHAS DE LECTURA", res.text)
        self.assertIn("patron", res.text)
        self.assertIn("NO escribas pregunta ni criterios", res.text)

    def test_flag_apaga(self):
        with mock.patch.dict(os.environ, {"VI_PLANNER": "0"}):
            self.assertFalse(qp.planner_enabled())


if __name__ == "__main__":
    unittest.main()
