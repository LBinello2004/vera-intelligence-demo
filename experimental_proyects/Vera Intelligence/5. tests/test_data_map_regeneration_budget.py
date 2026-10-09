"""Presupuesto de la regeneración del Data Map: pocas consultas y poco tiempo si solo cambiaron reglas; sin fallar al agotarse."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import data_map_auto_update as dmu  # noqa: E402

FINAL = ("===DATA_MAP_YAML===\nmetadata: {name: v2}\nsources: {}\n===END_DATA_MAP_YAML===\n"
         "===CHANGELOG===\nsolo se actualizó la versión del prompt\n===END_CHANGELOG===")
PROMPT_OLD = 'Evalua.\n"saludo": "Sí|No",\n"cierre": "Sí|No"\nRegla vieja.'
PROMPT_RULES = 'Evalua.\n"saludo": "Sí|No",\n"cierre": "Sí|No"\nRegla vieja y una regla nueva.'
PROMPT_FIELDS = 'Evalua.\n"saludo": "Sí|No",\n"cierre": "Sí|No",\n"ofreceGarantia": "Sí|No"\nRegla vieja.'


def change(old, new):
    return dmu.RulebookChange("rb", "ventas", 1, 2, old, new)


class BudgetHelperTests(unittest.TestCase):
    def test_un_cambio_solo_de_reglas_tiene_presupuesto_chico(self) -> None:
        self.assertFalse(dmu.structure_changed([change(PROMPT_OLD, PROMPT_RULES)]))
        self.assertEqual(dmu.regeneration_budget([change(PROMPT_OLD, PROMPT_RULES)]), (dmu.REGEN_RULES_TOOL_CALLS, dmu.REGEN_RULES_SECONDS))

    def test_agregar_o_quitar_campos_de_salida_tiene_mas_margen(self) -> None:
        self.assertTrue(dmu.structure_changed([change(PROMPT_OLD, PROMPT_FIELDS)]))
        self.assertTrue(dmu.structure_changed([change(PROMPT_FIELDS, PROMPT_OLD)]))
        self.assertEqual(dmu.regeneration_budget([change(PROMPT_OLD, PROMPT_FIELDS)]),
                         (dmu.REGEN_STRUCTURE_TOOL_CALLS, dmu.REGEN_STRUCTURE_SECONDS))

    def test_sin_texto_viejo_se_asume_que_cambio_la_estructura(self) -> None:
        self.assertTrue(dmu.structure_changed([change(None, PROMPT_RULES)]))

    def test_el_presupuesto_de_reglas_es_menor_que_el_de_estructura(self) -> None:
        self.assertLess(dmu.REGEN_RULES_TOOL_CALLS, dmu.REGEN_STRUCTURE_TOOL_CALLS)
        self.assertLess(dmu.REGEN_RULES_SECONDS, dmu.REGEN_STRUCTURE_SECONDS)


class RegenerationLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        folder = Path(self._tmp.name)
        self.v1 = folder / "VI Data Map Acme V1.yaml"
        self.v1.write_text("metadata: {name: v1}\nsources: {}\n", encoding="utf-8")
        self.config = SimpleNamespace(data_map_path=self.v1, display_name="Acme", model="m", client_id="acme", tenant="Acme")
        self.executed: list = []
        self.sent: list = []

    def run_loop(self, scripted, **kwargs):
        responses = list(scripted)

        def fake_send(chat, message, **kw):
            self.sent.append(message)
            return responses.pop(0)

        def fake_sql(sql):
            self.executed.append(sql)
            return '{"row_count": 1, "truncated": false, "columns": ["n"], "rows": [[1]]}'

        client = SimpleNamespace(chats=SimpleNamespace(create=lambda **kw: object()))
        with patch.dict("os.environ", {"VERA_AI_API_KEY": "x"}), patch.object(dmu.vi_agent.genai, "Client", lambda **kw: client), \
                patch.object(dmu.vi_agent, "_send_message_with_retry", fake_send), patch.object(dmu.vi_agent, "run_readonly_sql", fake_sql):
            return dmu.regenerate_data_map(self.config, [change(PROMPT_OLD, PROMPT_RULES)], **kwargs)

    @staticmethod
    def calls(n):
        return SimpleNamespace(function_calls=[SimpleNamespace(name="run_readonly_sql", args={"sql": f"select {i}"}) for i in range(n)], text=None)

    @staticmethod
    def final():
        return SimpleNamespace(function_calls=[], text=FINAL)

    def test_dentro_del_presupuesto_ejecuta_las_consultas_y_termina(self) -> None:
        result = self.run_loop([self.calls(2), self.final()], tool_budget=4)
        self.assertIsNone(result.error)
        self.assertEqual(len(self.executed), 2)
        self.assertTrue(result.candidate_path.is_file())

    def test_al_agotar_las_consultas_no_falla_y_le_pide_a_gemini_el_yaml_final(self) -> None:
        result = self.run_loop([self.calls(3), self.final()], tool_budget=2)
        self.assertIsNone(result.error)
        self.assertEqual(self.executed, [])                          # no se ejecutó la tanda que excedía el presupuesto
        self.assertIn("AHORA el YAML final", str(self.sent[1][0].function_response.response))
        self.assertTrue(result.candidate_path.is_file())

    def test_al_agotar_el_tiempo_tampoco_falla(self) -> None:
        result = self.run_loop([self.calls(1), self.final()], tool_budget=10, soft_deadline_seconds=-1)
        self.assertIsNone(result.error)
        self.assertEqual(self.executed, [])
        self.assertIn("el tiempo", str(self.sent[1][0].function_response.response))

    def test_si_gemini_insiste_en_pedir_consultas_se_corta_con_error(self) -> None:
        result = self.run_loop([self.calls(3), self.calls(3), self.calls(3), self.final()], tool_budget=2)
        self.assertIsNotNone(result.error)
        self.assertIsNone(result.candidate_path)


if __name__ == "__main__":
    unittest.main()
