"""Registro de cambios del Data Map y herramienta para revertir."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import data_map_log as dml  # noqa: E402
import data_map_store as dms  # noqa: E402
import revert_data_map as rdm  # noqa: E402

VIEW = "dashboard_v2.vw_acme_demografia"


def data_map(fields: dict, **sections) -> dict:
    return {"metadata": {"name": "x"}, "sources": {"demography": {"source": VIEW, "fields": fields}}, **sections}


class SummarizeChangeTests(unittest.TestCase):
    def test_campos_agregados_quitados_y_descripciones(self) -> None:
        old = data_map({"a": {"description": "A"}, "b": {"description": "B"}, "c": {"description": "C"}})
        new = data_map({"a": {"description": "A"}, "b": {"description": "B cambiada"}, "d": {"description": "D"}})
        summary = dml.summarize_change(old, new)
        self.assertEqual(summary["campos_agregados"], {"demography": ["d"]})
        self.assertEqual(summary["campos_quitados"], {"demography": ["c"]})
        self.assertEqual(summary["descripciones_modificadas"], {"demography": ["b"]})
        self.assertFalse(summary["sin_cambios_de_contenido"])

    def test_valores_de_enum_y_secciones(self) -> None:
        old = data_map({"a": {"configured_values": ["x"]}}, sql_rules={"r": 1})
        new = data_map({"a": {"configured_values": ["x", "y"]}}, sql_rules={"r": 2})
        summary = dml.summarize_change(old, new)
        self.assertEqual(summary["valores_modificados"], {"demography": ["a"]})
        self.assertEqual(summary["secciones_modificadas"], ["sql_rules"])

    def test_fuentes_agregadas_y_quitadas(self) -> None:
        old, new = data_map({"a": {}}), data_map({"a": {}})
        new["sources"]["nueva"] = {"source": "dashboard_v2.vw_n", "fields": {}}
        del old["sources"]["demography"], new["sources"]["demography"]
        old["sources"]["vieja"] = {"source": "dashboard_v2.vw_v", "fields": {}}
        summary = dml.summarize_change(old, new)
        self.assertEqual((summary["fuentes_agregadas"], summary["fuentes_quitadas"]), (["nueva"], ["vieja"]))

    def test_si_solo_cambian_los_metadatos_lo_dice(self) -> None:
        old = data_map({"a": {"description": "A"}})
        new = data_map({"a": {"description": "A"}})
        new["metadata"]["version"] = "2.0.0"
        self.assertTrue(dml.summarize_change(old, new)["sin_cambios_de_contenido"])


class LogFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def entry(self, **kw):
        base = dict(tipo="promovido", client="acme_alto", old_version=1, new_version=2, old_file="V1.yaml", new_file="V2.yaml",
                    prompts=[{"key": "rb", "old_version": 3, "new_version": 4}], changelog="agregó un campo\nverificó con SQL",
                    change_summary=dml.summarize_change(data_map({"a": {}}), data_map({"a": {}, "b": {}})),
                    gate={"gate": "v2", "passed": True, "questions_evaluated": 10, "llm_questions_checked": 4}, model="gemini-x", attempts=1,
                    now=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))
        base.update(kw)
        return dml.build_entry(**base)

    def test_la_entrada_trae_todo_y_el_comando_para_revertir(self) -> None:
        entry = self.entry()
        self.assertEqual(entry["fecha"], "2026-10-07T12:00:00Z")
        self.assertIn("--client acme_alto --to 1", entry["revertir_con"])
        text = dml.render_entry(entry)
        for expected in ("Promovido: V1 → V2", "Prompt `rb`: versión 3 → 4", "campos agregados en `demography`: `b`", "agregó un campo",
                         "gate v2: pasó, 10 preguntas del banco dorado, 4 respondidas por el agente", "`gemini-x` en 1 intento", "Para revertir"):
            self.assertIn(expected, text)

    def test_una_reversion_no_ofrece_revertir_otra_vez(self) -> None:
        self.assertEqual(self.entry(tipo="revertido")["revertir_con"], "")

    def test_la_entrada_mas_nueva_va_arriba_y_el_encabezado_no_se_duplica(self) -> None:
        dml.append_entry(self.root, "acme_alto", self.entry(new_version=2, now=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        dml.append_entry(self.root, "acme_alto", self.entry(old_version=2, new_version=3, now=datetime(2026, 10, 7, tzinfo=timezone.utc)))
        md_path, jsonl_path = dml.log_paths(self.root, "acme_alto")
        text = md_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("# Cambios automáticos del Data Map de acme_alto"), 1)
        self.assertLess(text.index("V2 → V3"), text.index("V1 → V2"))
        self.assertEqual([e["version_nueva"] for e in dml.read_entries(self.root, "acme_alto")], [2, 3])   # el jsonl, de vieja a nueva
        self.assertEqual(len(jsonl_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_las_lineas_ilegibles_del_jsonl_se_ignoran(self) -> None:
        dml.append_entry(self.root, "acme_alto", self.entry())
        _, jsonl_path = dml.log_paths(self.root, "acme_alto")
        with jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write("{no es json\n")
        self.assertEqual(len(dml.read_entries(self.root, "acme_alto")), 1)

    def test_sin_registro_no_hay_entradas(self) -> None:
        self.assertEqual(dml.read_entries(self.root, "nadie"), [])


class RevertTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.clients = self.project / "2. clientes"
        self.folder = self.clients / "acme_alto"
        (self.folder / "data_map").mkdir(parents=True)
        for version in (1, 2, 3):
            (self.folder / "data_map" / f"VI Data Map Acme V{version}.yaml").write_text(
                f"metadata: {{v: {version}}}\nsources:\n  a: {{source: dashboard_v2.vw_a, fields: {{f{version}: {{}}}}}}\n", encoding="utf-8")
        self.config = self.folder / "config.yaml"
        self.config.write_text('client_id: acme\ndata_map: "2. clientes/acme_alto/data_map/VI Data Map Acme V3.yaml"\nmodel: m\n', encoding="utf-8")
        self.store = dms.LocalStore(project_root=self.project)

    def revert(self, **kw):
        return rdm.revert("acme_alto", clients_root=self.clients, project_root=self.project, store=self.store, **kw)

    def test_sin_registro_vuelve_a_la_mayor_anterior_a_la_vigente(self) -> None:
        result = self.revert(reason="prueba")
        self.assertEqual((result["from"], result["to"]), (3, 2))
        self.assertIn("V2.yaml", self.config.read_text(encoding="utf-8"))
        self.assertIn("model: m", self.config.read_text(encoding="utf-8"))      # no toca el resto del archivo
        for version in (1, 2, 3):                                               # no borra ninguna versión
            self.assertTrue((self.folder / "data_map" / f"VI Data Map Acme V{version}.yaml").is_file())

    def test_con_registro_vuelve_a_la_version_anterior_de_la_ultima_promocion(self) -> None:
        dml.append_entry(self.clients, "acme_alto", dml.build_entry(tipo="promovido", client="acme_alto", old_version=1, new_version=3))
        self.assertEqual(self.revert()["to"], 1)       # la V2 existe, pero la promoción que dejó la V3 venía de la V1

    def test_a_una_version_puntual_y_queda_registrado(self) -> None:
        self.revert(to_version=1, reason="las cifras de X no coinciden")
        entries = dml.read_entries(self.clients, "acme_alto")
        self.assertEqual((entries[-1]["tipo"], entries[-1]["version_anterior"], entries[-1]["version_nueva"]), ("revertido", 3, 1))
        self.assertEqual(entries[-1]["motivo"], "las cifras de X no coinciden")
        self.assertEqual(entries[-1]["resumen"]["campos_agregados"], {"a": ["f1"]})
        self.assertIn("Revertido: V3 → V1", dml.log_paths(self.clients, "acme_alto")[0].read_text(encoding="utf-8"))

    def test_el_almacen_queda_alineado_para_que_la_version_activa_no_pise_la_reversion(self) -> None:
        self.store.promote("acme_alto", self.folder / "data_map" / "VI Data Map Acme V3.yaml")     # la promoción que se revierte
        self.revert(to_version=1)
        self.assertEqual(self.store.active("acme_alto")["version"], 1)
        config_path = self.folder / "data_map" / "VI Data Map Acme V1.yaml"
        self.assertEqual(dms.effective_data_map_path("acme_alto", config_path, self.store), config_path)

    def test_errores_claros(self) -> None:
        with self.assertRaisesRegex(ValueError, "ya es la versión vigente"):
            self.revert(to_version=3)
        with self.assertRaisesRegex(ValueError, "No existe el archivo de la V9"):
            self.revert(to_version=9)
        (self.folder / "data_map" / "VI Data Map Acme V1.yaml").write_text("no: es un data map\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no es un Data Map válido"):
            self.revert(to_version=1)
        self.assertIn("V3.yaml", self.config.read_text(encoding="utf-8"))       # ante un error no se cambió nada

    def test_nada_a_lo_que_volver(self) -> None:
        for version in (1, 2):
            (self.folder / "data_map" / f"VI Data Map Acme V{version}.yaml").unlink()
        with self.assertRaisesRegex(ValueError, "No hay una versión anterior"):
            self.revert()

    def test_listar_versiones(self) -> None:
        self.assertEqual([v for v, _ in rdm.list_versions(self.clients, "acme_alto")], [1, 2, 3])
        self.assertEqual(rdm.current_version(self.clients, "acme_alto", self.project)[0], 3)


if __name__ == "__main__":
    unittest.main()
