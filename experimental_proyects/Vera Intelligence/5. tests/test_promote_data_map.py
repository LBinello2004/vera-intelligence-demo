"""Promoción manual de un Data Map candidato que el gate rechazó."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import data_map_log as dml  # noqa: E402
import data_map_store as dms  # noqa: E402
import promote_data_map as pdm  # noqa: E402
import revert_data_map as rdm  # noqa: E402

BASE = "metadata: {{v: {v}}}\nsources:\n  a: {{source: dashboard_v2.vw_a, fields: {{f{v}: {{}}, comun: {{}}}}}}\nsql_rules: {{r: 1}}\n"


class PromoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.clients = self.project / "2. clientes"
        self.folder = self.clients / "acme_alto"
        (self.folder / "data_map").mkdir(parents=True)
        for version in (1, 2):
            (self.folder / "data_map" / f"VI Data Map Acme V{version}.yaml").write_text(BASE.format(v=version), encoding="utf-8")
        self.config = self.folder / "config.yaml"
        self.config.write_text('client_id: acme\ndata_map: "2. clientes/acme_alto/data_map/VI Data Map Acme V1.yaml"\nmodel: m\n', encoding="utf-8")
        self.store = dms.LocalStore(project_root=self.project)
        self.store.put_pending("acme_alto", {"rb": {"old_version": 3, "new_version": 4, "old_text": "x", "first_seen_at": 1.0}})

    def promote(self, **kw):
        kw.setdefault("reason", "revisé el diff: solo cambia una descripción")
        kw.setdefault("drift_check", lambda client, path: [])
        return pdm.promote_manual("acme_alto", clients_root=self.clients, project_root=self.project, store=self.store, **kw)

    def test_promueve_el_candidato_mas_nuevo_y_actualiza_config_almacen_y_registro(self) -> None:
        result = self.promote()
        self.assertEqual((result["from"], result["to"], result["checks"]), (1, 2, ["estructura", "columnas"]))
        text = self.config.read_text(encoding="utf-8")
        self.assertIn("V2.yaml", text)
        self.assertIn("model: m", text)                                       # no toca el resto
        self.assertEqual(self.store.active("acme_alto")["version"], 2)
        entries = dml.read_entries(self.clients, "acme_alto")
        self.assertEqual((entries[-1]["tipo"], entries[-1]["motivo"]), ("promovido_manual", "revisé el diff: solo cambia una descripción"))
        self.assertIn("--to 1", entries[-1]["revertir_con"])
        self.assertIn("Promovido a mano: V1 → V2", dml.log_paths(self.clients, "acme_alto")[0].read_text(encoding="utf-8"))
        self.assertEqual(entries[-1]["prompts"], [{"key": "rb", "old_version": 3, "new_version": 4}])

    def test_los_prompts_pendientes_quedan_procesados_para_que_no_se_regenere_el_mismo_cambio(self) -> None:
        self.promote()
        self.assertEqual(self.store.processed_versions("acme_alto"), {"rb": 4})
        self.assertEqual(self.store.pending("acme_alto"), {})

    def test_se_puede_elegir_por_version_o_por_archivo_y_la_razon_es_obligatoria(self) -> None:
        (self.folder / "data_map" / "VI Data Map Acme V3.yaml").write_text(BASE.format(v=3), encoding="utf-8")
        self.assertEqual(self.promote(version=2)["to"], 2)
        with self.assertRaisesRegex(ValueError, "Hace falta --reason"):
            self.promote(reason="  ")
        self.assertEqual(self.promote(file="VI Data Map Acme V3.yaml")["to"], 3)

    def test_errores_claros(self) -> None:
        with self.assertRaisesRegex(ValueError, "ya es la versión vigente"):
            self.promote(version=1)
        with self.assertRaisesRegex(ValueError, "No existe la V9"):
            self.promote(version=9)
        with self.assertRaisesRegex(ValueError, "No existe el archivo"):
            self.promote(file="no_existe.yaml")
        (self.folder / "data_map" / "VI Data Map Acme V2.yaml").unlink()
        with self.assertRaisesRegex(ValueError, "ningún candidato más nuevo"):
            self.promote()

    def test_un_candidato_que_pierde_estructura_no_se_promueve(self) -> None:
        (self.folder / "data_map" / "VI Data Map Acme V2.yaml").write_text("metadata: {v: 2}\nsources:\n  otra: {source: dashboard_v2.vw_otra, fields: {x: {}}}\n",
                                                                           encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "pierde estructura"):
            self.promote()
        self.assertIn("V1.yaml", self.config.read_text(encoding="utf-8"))      # ante un error no se cambió nada

    def test_campos_inexistentes_en_la_base_o_una_base_inaccesible_no_se_promueve(self) -> None:
        with self.assertRaisesRegex(ValueError, "no existen en la base"):
            self.promote(drift_check=lambda client, path: [{"source": "a", "missing": ["f2"]}])

        def down(client, path):
            raise ConnectionError("sin red")

        with self.assertRaisesRegex(ValueError, "skip-drift-check"):
            self.promote(drift_check=down)
        self.assertEqual(self.promote(drift_check=None)["checks"], ["estructura"])     # --skip-drift-check

    def test_despues_se_puede_revertir_a_la_version_anterior(self) -> None:
        self.promote()
        result = rdm.revert("acme_alto", clients_root=self.clients, project_root=self.project, store=self.store)
        self.assertEqual((result["from"], result["to"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
