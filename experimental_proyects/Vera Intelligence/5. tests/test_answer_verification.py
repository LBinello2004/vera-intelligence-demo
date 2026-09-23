from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"4. scripts"))
from answer_verification import add_result, verify_answer, with_limitations, history_results, FALLBACK
import vi_agent

class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.id = add_result(self.store, {"columns":["total","cumplimientos","n_evaluados","tasa"],"rows":[[500,100,500,20]],"truncated":False})
    def ref(self, column, row=0):
        return {"id":self.id,"row":row,"column":column}
    def claim(self,text,op,columns,**kwargs):
        return {"text":text,"operation":op,"sources":[self.ref(x) for x in columns],**kwargs}
    def block(self,claim):
        return "\n```vera-evidence\n"+json.dumps([claim])+"\n```"
    def test_rulebook_threshold_is_not_confused_with_previous_sql_results(self):
        v=verify_answer("El criterio considera cumplimiento a partir del 80%.",self.store,current_ids=set(),rulebook_texts=["El criterio usa 80%."])
        self.assertFalse(v.errors);self.assertFalse(v.limitations)

    def test_observed_numbers_require_evidence_even_without_sql(self):
        self.assertTrue(verify_answer("Hubo 12 conversaciones y la tasa fue 20%.",{}).errors)
    def test_numeric_rule_is_allowed_only_with_its_trusted_definition(self):
        a="El criterio considera cumplimiento a partir del 80%."
        self.assertFalse(verify_answer(a,{},rulebook_texts=["El criterio usa 80%."]).errors)
        self.assertTrue(verify_answer(a,{}).errors)

    def test_small_count_hallucination_is_blocked(self):
        self.assertTrue(verify_answer("Hubo 12 conversaciones.",self.store).errors)
    def test_row_count_does_not_support_a_claim(self):
        s={};add_result(s,{"row_count":999,"columns":["total"],"rows":[[500]]})
        self.assertTrue(verify_answer("Hubo 999 conversaciones.",s).errors)
    def test_count_match_is_exact_not_half_percent(self):
        self.assertTrue(verify_answer("Hubo 501 conversaciones.",self.store).errors)
    def test_thousands_and_dates_are_not_confused(self):
        s={};add_result(s,{"columns":["total"],"rows":[[247556]]})
        self.assertFalse(verify_answer("Del 7 al 13 de septiembre de 2026 hubo 247.556 conversaciones.",s).errors)
        self.assertFalse(verify_answer("Entre 2026-09-07 y 2026-09-13 hubo 247,556 conversaciones.",s).errors)
    def test_percentage_recomputed_from_real_cells(self):
        v=verify_answer("La tasa fue 20%."+self.block(self.claim("20%","percentage",["cumplimientos","n_evaluados"])),self.store)
        self.assertFalse(v.errors)
        self.assertNotIn("vera-evidence",v.answer)
        # Ya no se agrega el aviso "Bases evaluadas observadas: ..." (2026-09-21, pedido explícito).
        self.assertFalse(any("Bases evaluadas" in note for note in v.limitations))
    def test_bad_calculation_fails_even_if_wrong_number_exists_in_sql(self):
        v=verify_answer("La tasa fue 100%."+self.block(self.claim("100%","percentage",["cumplimientos","n_evaluados"])),self.store)
        self.assertIn("cálculo incorrecto",v.errors)
    def test_non_metric_declaration_exempts_a_number_from_evidence(self):
        # 2026-09-16 (ver "8. README.md" > MEJORES PRÁCTICAS INTERNAS): un número citado
        # textualmente de una conversación real (ej. "venta de tres piezas") no es una cifra de
        # resultados -el modelo puede declararlo como 'non_metric' en vez de perder la cita entera
        # en un ciclo de evidence_repair, como pasó en vivo antes de este fix.
        claim = {"text": "3", "operation": "non_metric"}
        v = verify_answer(
            "El vendedor concretó una venta de 3 piezas." + self.block(claim), self.store
        )
        self.assertFalse(v.errors)

    def test_non_metric_does_not_exempt_a_different_number(self):
        # La declaración es por texto exacto -declarar '3' como non_metric no exime a otra cifra
        # distinta que sí necesita respaldo real.
        claim = {"text": "3", "operation": "non_metric"}
        v = verify_answer(
            "El vendedor concretó una venta de 3 piezas y la tasa fue del 47%."
            + self.block(claim),
            self.store,
        )
        self.assertTrue(any("47%" in err for err in v.errors))

    def test_non_metric_cannot_be_reused_to_dodge_a_real_metric(self):
        # Declarar 'non_metric' para una cifra que SÍ es una tasa/conteo derivado de los datos no
        # la hace desaparecer de otras verificaciones (ej. bases evaluadas) -sólo la exime del
        # chequeo puntual de "cifra sin respaldo", no reemplaza al cálculo real.
        claim = {"text": "20%", "operation": "non_metric"}
        v = verify_answer("La tasa real fue 20%." + self.block(claim), self.store)
        self.assertFalse(v.errors)  # 20% igual matchea contra las celdas reales (percentages)
        self.assertFalse(
            any("cálculo incorrecto" in err for err in v.errors)
        )  # no se corrió calculate() para esta claim, como corresponde

    def test_count_cannot_automatically_become_a_percentage(self):
        s={};add_result(s,{"columns":["total"],"rows":[[20]]})
        self.assertTrue(verify_answer("La tasa fue 20%.",s).errors)
    def test_difference_ratio_mean_sum_and_relative_change(self):
        for op,columns,text in [("difference",["total","cumplimientos"],"400"),("sum",["total","cumplimientos"],"600"),("mean",["total","cumplimientos"],"300"),("ratio",["total","cumplimientos"],"5"),("relative_change",["total","cumplimientos"],"400%")]:
            with self.subTest(op=op):
                v=verify_answer("Resultado: "+text+self.block(self.claim(text,op,columns)),self.store)
                self.assertFalse(v.errors)
    def test_declared_rounding_is_accepted(self):
        s={};key=add_result(s,{"columns":["a","b"],"rows":[[1,3]]})
        c={"text":"33,3%","operation":"percentage","sources":[{"id":key,"row":0,"column":x} for x in ['a','b']]}
        self.assertFalse(verify_answer("Resultado: 33,3%."+self.block(c),s).errors)
        c['text']='33,9%'
        self.assertTrue(verify_answer("Resultado: 33,9%."+self.block(c),s).errors)
    def test_percentage_with_three_decimal_places_is_not_thousands(self):
        s={};add_result(s,{"columns":["tasa","n_evaluados"],"rows":[[12.345,50]]})
        self.assertFalse(verify_answer("La tasa fue 12,345%.",s).errors)
        self.assertTrue(verify_answer("La tasa fue 12,349%.",s).errors)

    def test_zero_denominator_is_rejected(self):
        s={};key=add_result(s,{"columns":["a","b"],"rows":[[0,0]]})
        c={"text":"0%","operation":"percentage","sources":[{"id":key,"row":0,"column":x} for x in ['a','b']]}
        self.assertIn("denominador cero",verify_answer("Tasa 0%."+self.block(c),s).errors)
    def test_missing_and_negative_row_references_are_rejected(self):
        for row in [-1,100,True]:
            c=self.claim("100","identity",['cumplimientos']);c['sources'][0]['row']=row
            self.assertTrue(verify_answer("Resultado: 100."+self.block(c),self.store).errors)
    def test_arbitrary_operation_cannot_execute_code(self):
        self.assertIn("operación inválida",verify_answer("Resultado: 100."+self.block(self.claim("100","__import__",['cumplimientos'])),self.store).errors)
    def test_malformed_and_unclosed_evidence_are_blocked(self):
        for suffix in ['\n```vera-evidence nope```','\n```vera-evidence {']:
            self.assertTrue(verify_answer("Hubo 500 conversaciones."+suffix,self.store).errors)
    def test_graph_hallucination_is_rejected(self):
        answer='Hubo 500 conversaciones.\n```vera-chart {"type":"bar","labels":["A"],"values":[999]} ```'
        self.assertIn("valor del gráfico sin respaldo",verify_answer(answer,self.store).errors)
    def test_derived_graph_value_is_checked(self):
        s={};key=add_result(s,{"columns":["a","b"],"rows":[[1,4]]})
        c={"text":"25%","operation":"percentage","sources":[{"id":key,"row":0,"column":x} for x in ['a','b']]}
        a='Resultado: 25%.\n```vera-chart {"type":"bar","labels":["A"],"values":[25]} ```'+self.block(c)
        self.assertFalse(verify_answer(a,s).errors)
    def test_derived_graph_value_accepts_a_valid_rounding(self):
        # Bug real corregido 2026-09-17: comparaba el valor del gráfico con `==` exacto en vez de
        # la misma tolerancia de redondeo que ya usa `matches()` para citas en texto -esta fue la
        # causa individual más frecuente de evidence_repair en producción (48/331 reintentos
        # reales, cada uno una llamada extra pagada a Gemini).
        s = {}; key = add_result(s, {"columns": ["a", "b"], "rows": [[6894, 10000]]})
        c = {"text": "68,94%", "operation": "percentage", "sources": [{"id": key, "row": 0, "column": x} for x in ['a', 'b']]}
        a = 'Resultado: 68,94%.\n```vera-chart {"type":"bar","labels":["A"],"values":[68.9]} ```' + self.block(c)
        self.assertFalse(verify_answer(a, s).errors)
    def test_derived_graph_value_still_rejects_a_real_mismatch(self):
        s = {}; key = add_result(s, {"columns": ["a", "b"], "rows": [[6894, 10000]]})
        c = {"text": "68,94%", "operation": "percentage", "sources": [{"id": key, "row": 0, "column": x} for x in ['a', 'b']]}
        a = 'Resultado: 68,94%.\n```vera-chart {"type":"bar","labels":["A"],"values":[70]} ```' + self.block(c)
        self.assertIn("valor del gráfico sin respaldo", verify_answer(a, s).errors)
    def test_bare_calendar_year_is_not_a_metric_needing_backing(self):
        # Bug real corregido 2026-09-17 (encontrado analizando .runtime/usage/gemini_calls.jsonl:
        # 57,3% de 630 interacciones reales dispararon un evidence_repair): mencionar el año en
        # prosa de negocio nunca es una cifra de resultados, pero antes disparaba "cifra sin
        # respaldo: 2026" igual que un número derivado real.
        for text in [
            "En lo que va de 2026 el desempeño mejoró.",
            "Comparado con 2025, el año 2026 mostró una mejora.",
        ]:
            self.assertFalse(verify_answer(text, {}).errors, text)
    def test_a_real_count_matching_a_year_still_needs_backing(self):
        self.assertTrue(verify_answer("Se registraron 2026 conversaciones este mes.", {}).errors)
    def test_verified_value_backs_a_differently_formatted_mention(self):
        # Bug real corregido 2026-09-17 (mismo análisis de evidence_repair, ver "8. README.md"):
        # antes exigía coincidencia EXACTA de string entre la prosa y el texto declarado en
        # vera-evidence, no sólo que el valor coincidiera con tolerancia -un modelo real puede
        # escribir "20,0%" en la respuesta y declarar "20%" en su propia evidencia (mismo valor
        # real, formato distinto) sin que eso sea una cifra sin respaldo.
        s = {}; key = add_result(s, {"columns": ["cumplimientos", "n_evaluados"], "rows": [[100, 500]]})
        c = {"text": "20%", "operation": "percentage", "sources": [{"id": key, "row": 0, "column": x} for x in ['cumplimientos', 'n_evaluados']]}
        for texto in ["El cumplimiento fue de 20,0%.", "El cumplimiento fue de 20.0%."]:
            with self.subTest(texto=texto):
                self.assertFalse(verify_answer(texto + self.block(c), s).errors)
    def test_a_genuinely_wrong_calculation_is_still_rejected(self):
        # El fix de arriba saca la igualdad de string, pero calculate() sigue exigiendo que el
        # texto declarado sea una redondeo válido del valor real -esto no se debilitó.
        s = {}; key = add_result(s, {"columns": ["cumplimientos", "n_evaluados"], "rows": [[100, 500]]})
        c = {"text": "99%", "operation": "percentage", "sources": [{"id": key, "row": 0, "column": x} for x in ['cumplimientos', 'n_evaluados']]}
        v = verify_answer("El cumplimiento fue de 99%." + self.block(c), s)
        self.assertIn("cálculo incorrecto", v.errors)
    def test_unknown_base_produces_visible_limitation(self):
        s={};add_result(s,{"columns":["tasa"],"rows":[[20]]})
        v=verify_answer("La tasa fue 20%.",s)
        self.assertTrue(any("porcentaje sin base" in e for e in v.errors))
    def test_single_observation_is_not_generalized(self):
        s={};add_result(s,{"columns":["tasa","n_evaluados"],"rows":[[100,1]]})
        v=verify_answer("La tasa fue 100%.",s)
        self.assertTrue(any("una sola observación" in x for x in v.limitations))
    def test_null_indicator_is_not_zero(self):
        s={};add_result(s,{"columns":["promedio","n_evaluados"],"rows":[[None,0]]})
        self.assertTrue(verify_answer("El promedio fue 0.",s).errors)
        v=verify_answer("No se puede evaluar el promedio.",s)
        self.assertFalse(v.errors)
        self.assertTrue(v.limitations)
    def test_truncation_prevents_complete_ranking_claim(self):
        s={};add_result(s,{"columns":["total"],"rows":[[500]],"truncated":True})
        v=verify_answer("Este es el ranking completo: 500 conversaciones.",s)
        self.assertTrue(v.errors)
        self.assertTrue(v.limitations)
    def test_confidence_is_not_asserted_with_unknown_base(self):
        self.assertTrue(verify_answer("La muestra es una muestra representativa con tasa 20%.",self.store).errors)
    def test_empty_results_do_not_mean_failure(self):
        s={};add_result(s,{"columns":["tasa"],"rows":[]})
        v=verify_answer("No hay información disponible.",s)
        self.assertTrue(any("no demuestra incumplimiento" in x for x in v.limitations))
    def test_old_partial_result_does_not_taint_new_complete_result(self):
        old=add_result(self.store,{"columns":["total"],"rows":[[999]],"truncated":True})
        v=verify_answer("Hubo 500 conversaciones.",self.store,current_ids={self.id})
        self.assertFalse(v.errors);self.assertFalse(v.limitations)
        self.assertTrue(verify_answer("Hubo 999 conversaciones.",self.store,current_ids={self.id}).errors)
    def test_history_retains_provenance_for_followups(self):
        payload=self.store[self.id]
        chat=SimpleNamespace(get_history=lambda **_: [SimpleNamespace(parts=[SimpleNamespace(function_response=SimpleNamespace(name='run_readonly_sql',response={'result':payload}))])])
        self.assertEqual(history_results(chat),self.store)
    def test_limitations_are_before_interface_blocks(self):
        sin_base={};add_result(sin_base,{"columns":["tasa","n_evaluados"],"rows":[[100,1]]})
        v=verify_answer('La tasa fue 100%.\n```vera-suggestions ["Continuar"]```',sin_base)
        a=with_limitations(v)
        self.assertLess(a.index('una sola observación'),a.index('```vera-suggestions'))

    def test_bases_of_cited_rows_are_never_appended_as_a_notice(self):
        v=verify_answer("La tasa fue 20%.",self.store)
        self.assertNotIn('Bases evaluadas',with_limitations(v))

    def test_base_cannot_be_negative_or_fractional(self):
        for base in [-1,2.5]:
            s={};add_result(s,{"columns":["tasa","n_evaluados"],"rows":[[20,base]]})
            self.assertIn("base evaluada inválida",verify_answer("La tasa fue 20%.",s).errors)
    def test_money_denominator_is_not_reported_as_sample_size(self):
        s={};key=add_result(s,{"columns":["monto_a","monto_total"],"rows":[[100,500]]})
        c={"text":"20%","operation":"percentage","sources":[{"id":key,"row":0,"column":x} for x in ['monto_a','monto_total']]}
        v=verify_answer("Resultado: 20%."+self.block(c),s)
        self.assertFalse(v.errors)
        self.assertTrue(any('No se informó' in x for x in v.limitations))
        self.assertFalse(any('Bases evaluadas' in x for x in v.limitations))

    def test_only_bases_of_cited_rows_are_reported(self):
        s={};add_result(s,{"columns":["criterio","tasa","n_evaluados"],"rows":[["A",5.2,77],["B",25.7,1797],["C",99.1,256]]})
        v=verify_answer("Tasas: 5,2% sobre 77 evaluaciones y 25,7% sobre 1.797.",s)
        self.assertFalse(v.errors)
        self.assertNotIn('256',with_limitations(v))
        self.assertNotIn('Bases evaluadas',with_limitations(v))
    def test_negated_representativeness_does_not_trigger_a_repair(self):
        v=verify_answer("La tasa fue 20%. No se puede afirmar que sea una muestra representativa.",self.store)
        self.assertFalse(v.errors)
    def test_evaluation_scale_is_not_an_observed_metric(self):
        self.assertFalse(verify_answer("Hubo 500 conversaciones; usamos una escala de 1 a 5.",self.store).errors)

class OrdinalMarkerTests(unittest.TestCase):
    """Una numeración de lista/encabezado no es una cifra de resultados (falso positivo encontrado
    en vivo el 2026-09-21: "### 1. Jorge" agotaba los reintentos de evidencia)."""

    def tokens(self, text):
        from answer_verification import metric_tokens
        return list(metric_tokens(text))

    def test_markdown_ordinal_markers_are_not_figures(self):
        for text in (
            "1. Jorge texto",
            "1) Jorge texto",
            "### 1. Jorge Javier\ntexto",
            "**1. Jorge** texto",
            "* **1.** Jorge",
            "- 1. Jorge",
            "> 2. Ubaldo",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.tokens(text), [])

    def test_real_figures_are_still_detected(self):
        self.assertEqual(self.tokens("Cumple el 45.9% de los casos"), ["45.9%"])
        self.assertEqual(self.tokens("### Resultado 45.9 puntos"), ["45.9"])
        self.assertEqual(self.tokens("Hubo 999 conversaciones."), ["999"])


class AgentVerificationTests(unittest.TestCase):
    def fake_chat(self, answers):
        responses=iter(answers)
        return SimpleNamespace(send_message=MagicMock(side_effect=lambda _:next(responses)))
    def response(self,text='',tools=None):
        return SimpleNamespace(text=text,function_calls=tools,usage_metadata=None)
    def test_invalid_number_repairs_once_and_never_returns_bad_number(self):
        tool=SimpleNamespace(name='run_readonly_sql',args={'sql':'SELECT prueba'})
        chat=self.fake_chat([self.response(tools=[tool]),self.response('Hubo 999 conversaciones.'),self.response('Hubo 500 conversaciones.')])
        sql=MagicMock(return_value=json.dumps({'columns':['total'],'rows':[[500]]}))
        with patch.dict(vi_agent.TOOL_FUNCTIONS,{'run_readonly_sql':sql}):
            answer=vi_agent.run_tool_loop(chat,'Contá las conversaciones')
        self.assertEqual(answer,'Hubo 500 conversaciones.');self.assertEqual(chat.send_message.call_count,3)
        payload=chat.send_message.call_args_list[1].args[0][0].function_response.response
        self.assertIn('verification',payload)
    def test_repeated_invalid_number_has_bounded_fallback(self):
        # MAX_EVIDENCE_REPAIRS=2 (subido de 1, 2026-09-18): intento inicial + 2 reintentos, los 3
        # inválidos, antes de caer al fallback.
        tool=SimpleNamespace(name='run_readonly_sql',args={'sql':'SELECT prueba'})
        chat=self.fake_chat([self.response(tools=[tool]),self.response('Hubo 999 conversaciones.'),self.response('Hubo 999 conversaciones.'),self.response('Hubo 999 conversaciones.')])
        with patch.dict(vi_agent.TOOL_FUNCTIONS,{'run_readonly_sql':lambda **_:json.dumps({'columns':['total'],'rows':[[500]]})}):
            self.assertEqual(vi_agent.run_tool_loop(chat,'Contá'),FALLBACK)
        self.assertEqual(chat.send_message.call_count,4)
    def test_unverified_streamed_number_is_never_published(self):
        tool=SimpleNamespace(name='run_readonly_sql',args={'sql':'SELECT prueba'})
        turns=iter([[self.response(tools=[tool])],[self.response('Hubo 999 conversaciones.\n')],[self.response('Hubo 500 conversaciones.\n')]])
        chat=SimpleNamespace(send_message_stream=MagicMock(side_effect=lambda _:iter(next(turns))))
        deltas=[]
        with patch.dict(vi_agent.TOOL_FUNCTIONS,{'run_readonly_sql':lambda **_:json.dumps({'columns':['total'],'rows':[[500]]})}):
            answer=vi_agent.run_tool_loop(chat,'Contá',on_text_delta=deltas.append)
        self.assertNotIn('999',''.join(deltas));self.assertIn('500',''.join(deltas))
        self.assertEqual(answer,'Hubo 500 conversaciones.')


class SmallIntegerQuantifierTests(unittest.TestCase):
    def test_small_integers_as_prose_quantifiers_are_not_metrics(self):
        from answer_verification import metric_tokens
        text = "Probá 2 acciones concretas y dejá 1 cosa clara en el cierre."
        self.assertEqual(list(metric_tokens(text)), [])

    def test_small_integers_counting_business_entities_still_need_backing(self):
        from answer_verification import metric_tokens
        self.assertEqual(list(metric_tokens("Hubo 3 ventas y 2 clientes.")), ["3", "2"])

    def test_percentages_and_larger_numbers_still_checked(self):
        from answer_verification import metric_tokens
        self.assertEqual(list(metric_tokens("Cumple 5% y 25 casos.")), ["5%", "25"])


class BaseStatedInProseTests(unittest.TestCase):
    def _verify(self, answer, rows):
        store = {}
        add_result(store, {"rows": rows})
        return verify_answer(answer, store)

    def test_base_declared_in_prose_and_present_in_sql_is_not_flagged(self):
        v = self._verify("Cumple 13.9% (115 conversaciones evaluadas).", [{"tasa_cierre": 13.9, "n": 115}])
        self.assertFalse(any("No se informó" in x for x in v.limitations))

    def test_no_base_anywhere_still_warns(self):
        v = self._verify("Cumple 13.9% en el período.", [{"tasa_cierre": 13.9}])
        self.assertTrue(any("porcentaje sin base" in e for e in v.errors))


class BaseFromSqlRowTests(unittest.TestCase):
    def test_count_column_with_other_name_becomes_the_base_without_error(self):
        s = {}
        add_result(s, {"columns": ["tasa_cierre", "total_conversaciones"], "rows": [[13.9, 115]]})
        v = verify_answer("El cierre es 13.9%.", s)
        self.assertFalse(v.errors)
        self.assertIn("115 conversaciones", with_limitations(v))

    def test_no_count_column_still_errors(self):
        s = {}
        add_result(s, {"columns": ["tasa_cierre"], "rows": [[13.9]]})
        self.assertTrue(any("porcentaje sin base" in e for e in verify_answer("El cierre es 13.9%.", s).errors))

    def test_multiple_rows_needing_fallback_base_produce_a_single_limitation(self):
        # Bug real (2026-09-23, reportado por Lucas con captura de pantalla): una consulta con un
        # criterio por fila (patrón UNION ALL, muy común en este proyecto -una fila por criterio, en
        # vez de una columna por criterio) generaba una línea de limitations POR FILA calificada
        # -"Base evaluada de los indicadores citados: 186 conversaciones. Base evaluada de los
        # indicadores citados: 123 conversaciones...." repetido 8 veces, ilegible. Debe quedar UNA
        # sola oración con todos los valores encontrados.
        s = {}
        add_result(s, {
            "columns": ["criterio", "tasa", "total_conversaciones"],
            "rows": [
                ["Trato amable", 95.8, 186],
                ["Cierre de venta", 44.9, 123],
            ],
        })
        v = verify_answer("Trato amable: 95.8%. Cierre de venta: 44.9%.", s)
        self.assertFalse(v.errors)
        limitation_lines = [l for l in v.limitations if "Base evaluada" in l]
        self.assertEqual(len(limitation_lines), 1)
        self.assertIn("186", limitation_lines[0])
        self.assertIn("123", limitation_lines[0])


class ConfidentLanguageTests(unittest.TestCase):
    """CONFIDENT (2026-09-22): "definitiv[oa]" se sacó del patrón -encontrado investigando un
    reintento real en vivo (mens_fashion_alto, coaching de Ubaldo Ramos) que resultó ser un falso
    positivo: "cierre definitivo"/"decisión definitiva" es vocabulario normal de venta retail, no
    una afirmación de certeza estadística. Ver el comentario junto a CONFIDENT en
    answer_verification.py.

    Con `store` vacío, `verify_answer` retorna antes de llegar al chequeo de CONFIDENT (rama
    "cifra de resultados sin evidencia" más arriba) -estos tests arman un store mínimo (mismo
    patrón que VerificationTests.setUp) para que la prosa sí llegue a esa verificación."""

    def setUp(self):
        self.store = {}
        self.id = add_result(
            self.store,
            {"columns": ["tasa", "n_evaluados"], "rows": [[13.9, 115]], "truncated": False},
        )

    def _verify(self, prose: str):
        # 13,9% con base 115 presente en el store: no dispara ningún otro error, así que un
        # `errors` no vacío sólo puede venir de CONFIDENT en estos tests.
        text = f"El cierre fue 13,9% (115 conversaciones evaluadas). {prose}"
        return verify_answer(text, self.store, current_ids={self.id})

    def test_cierre_definitivo_is_legitimate_sales_vocabulary_not_flagged(self):
        # Caso real que disparaba el reintento antes del fix.
        v = self._verify("El foco de mejora es lograr un cierre definitivo cuando el cliente ya validó la prenda.")
        self.assertFalse(any("certeza" in e for e in v.errors))

    def test_decision_definitiva_is_not_flagged(self):
        v = self._verify("Falta avanzar hacia una decisión definitiva del cliente.")
        self.assertFalse(any("certeza" in e for e in v.errors))

    def test_genuine_certainty_language_is_still_flagged(self):
        for phrase in (
            "Sin duda, el vendedor mejora si sigue el plan.",
            "Esto demuestra concluyentemente que el problema es el cierre.",
            "El plan garantiza mejores resultados.",
            "El resultado es estadísticamente significativo.",
        ):
            with self.subTest(phrase=phrase):
                v = self._verify(phrase)
                self.assertTrue(any("certeza" in e for e in v.errors))
