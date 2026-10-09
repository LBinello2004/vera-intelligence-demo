"""Almacén del estado de la actualización automática del Data Map (disco local)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import data_map_store as dms  # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        (self.project / "2. clientes" / "acme_alto" / "data_map").mkdir(parents=True)
        self.store = dms.LocalStore(project_root=self.project)
        self.client = "acme_alto"

    def write_map(self, version: int) -> Path:
        path = self.project / "2. clientes" / self.client / "data_map" / f"VI Data Map Acme V{version}.yaml"
        path.write_text("metadata: {}\nsources: {}\n", encoding="utf-8")
        return path


class VersionTests(unittest.TestCase):
    def test_version_number(self) -> None:
        self.assertEqual(dms.version_number("VI Data Map Farma24 V10.yaml"), 10)
        self.assertEqual(dms.version_number(Path("x/VI Data Map Tigo V4.yaml")), 4)
        self.assertIsNone(dms.version_number("sin_version.yaml"))


class ActiveVersionTests(StoreCase):
    def test_no_hay_version_activa_hasta_que_se_promueve(self) -> None:
        self.assertIsNone(self.store.active(self.client))

    def test_promover_deja_el_puntero_el_historial_y_marca_lo_procesado(self) -> None:
        v5, v6 = self.write_map(5), self.write_map(6)
        self.store.put_pending(self.client, {"rb": {"old_version": 1, "new_version": 2}})
        self.store.promote(self.client, v5, rules_versions={"rb": 1}, changelog="cinco")
        record = self.store.promote(self.client, v6, rules_versions={"rb": 2}, changelog="seis")
        self.assertEqual(record["version"], 6)
        self.assertEqual(self.store.active(self.client)["data_map"], "2. clientes/acme_alto/data_map/VI Data Map Acme V6.yaml")
        self.assertEqual(self.store.processed_versions(self.client), {"rb": 2})
        self.assertEqual(self.store.pending(self.client), {})  # promover limpia lo pendiente

    def test_rollback_vuelve_a_la_anterior_y_sin_historial_a_config(self) -> None:
        v5, v6 = self.write_map(5), self.write_map(6)
        self.store.promote(self.client, v5)
        self.store.promote(self.client, v6)
        self.assertEqual(self.store.rollback(self.client)["version"], 5)
        self.assertEqual(self.store.active(self.client)["version"], 5)
        self.assertIsNone(self.store.rollback(self.client))   # no había nada antes de la V5
        self.assertIsNone(self.store.active(self.client))

    def test_el_data_map_resuelto_nunca_sale_del_proyecto(self) -> None:
        with self.assertRaises(ValueError):
            self.store.resolve_data_map(self.client, "../../etc/passwd")

    def test_put_data_map_valida_el_nombre(self) -> None:
        path = self.store.put_data_map(self.client, "VI Data Map Acme V7.yaml", "metadata: {}\n")
        self.assertTrue(path.is_file())
        for bad in ("../x.yaml", "a/b.yaml", "sin_extension"):
            with self.assertRaises(ValueError):
                self.store.put_data_map(self.client, bad, "x")

    def test_cliente_con_nombre_peligroso_se_rechaza(self) -> None:
        with self.assertRaises(ValueError):
            self.store.active("../otro")


class PendingAndAttemptsTests(StoreCase):
    def test_lo_procesado_se_fusiona_sin_pisar_lo_anterior(self) -> None:
        self.store.set_processed(self.client, {"a": 1})
        self.store.set_processed(self.client, {"b": 3})
        self.assertEqual(self.store.processed_versions(self.client), {"a": 1, "b": 3})

    def test_las_versiones_pueden_ser_numeros_o_la_cadena_local(self) -> None:
        self.store.set_processed(self.client, {"langfuse_rb": 7, "playbook": "local"})
        self.assertEqual(self.store.processed_versions(self.client), {"langfuse_rb": 7, "playbook": "local"})

    def test_los_intentos_se_cuentan_por_firma(self) -> None:
        self.assertEqual(self.store.attempts(self.client, "rb@2"), 0)
        self.assertEqual(self.store.add_attempt(self.client, "rb@2"), 1)
        self.assertEqual(self.store.add_attempt(self.client, "rb@2"), 2)
        self.assertEqual(self.store.attempts(self.client, "rb@3"), 0)

    def test_un_archivo_corrupto_se_lee_como_vacio(self) -> None:
        (self.store._dir(self.client) / "processed.json").write_text("{no es json", encoding="utf-8")
        self.assertEqual(self.store.processed_versions(self.client), {})


class LockTests(StoreCase):
    def test_solo_uno_toma_el_candado(self) -> None:
        self.assertTrue(self.store.acquire_lock(self.client, "A", 60))
        self.assertFalse(self.store.acquire_lock(self.client, "B", 60))
        self.assertEqual(self.store.lock_info(self.client)["owner"], "A")

    def test_otro_dueno_no_puede_soltarlo(self) -> None:
        self.store.acquire_lock(self.client, "A", 60)
        self.assertFalse(self.store.release_lock(self.client, "B"))
        self.assertIsNotNone(self.store.lock_info(self.client))
        self.assertTrue(self.store.release_lock(self.client, "A"))
        self.assertIsNone(self.store.lock_info(self.client))
        self.assertTrue(self.store.acquire_lock(self.client, "B", 60))

    def test_un_candado_vencido_se_pisa(self) -> None:
        self.assertTrue(self.store.acquire_lock(self.client, "A", 60))
        with patch.object(dms, "_now", return_value=dms._now() + 3600):
            self.assertTrue(self.store.acquire_lock(self.client, "B", 60))
        self.assertEqual(self.store.lock_info(self.client)["owner"], "B")

    def test_los_clientes_tienen_candados_independientes(self) -> None:
        self.assertTrue(self.store.acquire_lock("uno", "A", 60))
        self.assertTrue(self.store.acquire_lock("dos", "A", 60))


class MetaAndRunsTests(StoreCase):
    def test_ultimo_chequeo(self) -> None:
        self.assertIsNone(self.store.last_check(self.client))
        self.store.set_last_check(self.client, 1234.5)
        self.assertEqual(self.store.last_check(self.client), 1234.5)

    def test_dos_registros_en_el_mismo_segundo_no_se_pisan(self) -> None:
        a = self.store.write_run(self.client, {"status": "sin_cambios"})
        b = self.store.write_run(self.client, {"status": "promovido"})
        self.assertNotEqual(a, b)
        self.assertEqual(json.loads(b.read_text(encoding="utf-8"))["status"], "promovido")

    def test_la_escritura_deja_json_valido_sin_temporales(self) -> None:
        self.store.set_processed(self.client, {"x": 1})
        files = sorted(p.name for p in self.store._dir(self.client).iterdir())
        self.assertEqual(files, ["processed.json"])


class EffectivePathTests(StoreCase):
    def test_sin_version_activa_rige_config(self) -> None:
        cfg = self.write_map(5)
        self.assertEqual(dms.effective_data_map_path(self.client, cfg, self.store), cfg)

    def test_rige_la_activa_solo_si_es_mas_nueva(self) -> None:
        v5, v6 = self.write_map(5), self.write_map(6)
        self.store.promote(self.client, v6)
        self.assertEqual(dms.effective_data_map_path(self.client, v5, self.store), v6.resolve())
        self.assertEqual(dms.effective_data_map_path(self.client, v6, self.store), v6)   # igual: sin cambio
        self.store.promote(self.client, v5)   # una activa más vieja que config.yaml no pisa a config.yaml
        self.assertEqual(dms.effective_data_map_path(self.client, v6, self.store), v6)

    def test_si_el_archivo_activo_no_existe_o_el_almacen_falla_rige_config(self) -> None:
        v5 = self.write_map(5)
        v7 = self.write_map(7)
        self.store.promote(self.client, v7)
        v7.unlink()
        self.assertEqual(dms.effective_data_map_path(self.client, v5, self.store), v5)

        class Roto:
            def active(self, client):
                raise RuntimeError("bucket caído")

        self.assertEqual(dms.effective_data_map_path(self.client, v5, Roto()), v5)


class FactoryTests(unittest.TestCase):
    def test_backend_local_por_defecto_y_error_claro_para_los_demas(self) -> None:
        with patch.dict(os.environ, {"VI_STORE": ""}):
            self.assertEqual(dms.get_store().backend, "local")
        with patch.dict(os.environ, {"VI_STORE": "gcs://bucket/vera"}):
            with self.assertRaises(ValueError) as ctx:
                dms.get_store()
        self.assertIn("no implementado", str(ctx.exception))


class InflightRefundTests(unittest.TestCase):
    def test_un_intento_en_curso_se_devuelve_y_el_candado_se_libera(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = dms.LocalStore(project_root=Path(tmp))
            self.assertTrue(store.acquire_lock("acme_alto", "proc:1", 600))
            store.add_attempt("acme_alto", "rb@2")
            store.set_inflight("acme_alto", "rb@2")
            self.assertTrue(store.refund_inflight("acme_alto"))
            self.assertEqual(store.attempts("acme_alto", "rb@2"), 0)
            self.assertIsNone(store.lock_info("acme_alto"))
            self.assertFalse(store.refund_inflight("acme_alto"))          # ya no queda nada que devolver

    def test_sin_intento_en_curso_no_devuelve_nada_pero_igual_suelta_el_candado(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = dms.LocalStore(project_root=Path(tmp))
            store.acquire_lock("acme_alto", "proc:1", 600)
            store.add_attempt("acme_alto", "rb@2")
            self.assertFalse(store.refund_inflight("acme_alto"))
            self.assertEqual(store.attempts("acme_alto", "rb@2"), 1)
            self.assertIsNone(store.lock_info("acme_alto"))

    def test_clear_inflight_sin_archivo_no_falla(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dms.LocalStore(project_root=Path(tmp)).clear_inflight("acme_alto")


class AttemptRefundTests(unittest.TestCase):
    def test_un_intento_devuelto_no_baja_de_cero_y_la_racha_de_caidas_se_guarda(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = dms.LocalStore(project_root=Path(tmp))
            self.assertEqual(store.add_attempt("acme_alto", "rb@2"), 1)
            self.assertEqual(store.refund_attempt("acme_alto", "rb@2"), 0)
            self.assertEqual(store.refund_attempt("acme_alto", "rb@2"), 0)
            self.assertEqual(store.infra_failures("acme_alto"), {})
            store.set_infra_failures("acme_alto", {"count": 2, "last_date": "2026-10-09"})
            self.assertEqual(store.infra_failures("acme_alto"), {"count": 2, "last_date": "2026-10-09"})
            store.set_infra_failures("acme_alto", {})
            self.assertEqual(store.infra_failures("acme_alto"), {})


class GateBaselineCacheTests(unittest.TestCase):
    def test_el_cache_vale_solo_para_el_mismo_texto_de_data_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = dms.LocalStore(project_root=Path(tmp))
            self.assertEqual(store.gate_baseline("acme_alto", "h1"), {})
            store.set_gate_baseline("acme_alto", "h1", {"q1": ["93"], "q2": []})
            self.assertEqual(store.gate_baseline("acme_alto", "h1"), {"q1": ["93"], "q2": []})
            self.assertEqual(store.gate_baseline("acme_alto", "h2"), {})      # cambió el Data Map vigente: se vuelve a medir


if __name__ == "__main__":
    unittest.main()
