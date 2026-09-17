from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from client_config import available_client_ids, load_client_config  # noqa: E402


class ClientConfigTests(unittest.TestCase):
    def test_mens_fashion_config_is_scoped_and_complete(self) -> None:
        client = load_client_config()

        self.assertEqual(client.client_id, "mens_fashion")
        self.assertEqual(client.display_name, "Men's Fashion")
        self.assertEqual(client.tenant, "Mens Fashion")
        self.assertEqual(len(client.sources), 6)
        self.assertEqual(
            set(client.business_rulebooks),
            {"sales_evaluation", "conversation_insights", "coaching_playbook"},
        )
        self.assertTrue(client.data_map_path.is_file())
        self.assertTrue(
            all(
                rulebook.label == "production"
                for rulebook in client.business_rulebooks.values()
                if rulebook.source == "langfuse"
            )
        )
        # coaching_playbook (2026-09-11, ver "3. experimentos/coaching_playbook/") es local -sin
        # label, con un path que existe dentro del proyecto.
        coaching = client.business_rulebooks["coaching_playbook"]
        self.assertEqual(coaching.source, "local")
        self.assertIsNone(coaching.label)
        self.assertTrue(coaching.path.is_file())


class LocalRulebookConfigTests(unittest.TestCase):
    """Regresión (2026-09-11, ver '3. experimentos/coaching_playbook/README.md'): rulebooks con
    source='local' se validan distinto a los de Langfuse -sin label, con un path que debe existir
    dentro del proyecto."""

    def setUp(self) -> None:
        import tempfile

        import yaml

        from client_config import CLIENTS_ROOT, PROJECT_ROOT

        self.temp_dir = tempfile.TemporaryDirectory(dir=CLIENTS_ROOT)
        self.addCleanup(self.temp_dir.cleanup)
        self.client_folder = Path(self.temp_dir.name)
        self.client_id = self.client_folder.name
        self.project_root = PROJECT_ROOT
        self.yaml = yaml

    def _write_config(self, rulebook_payload: dict) -> None:
        base_client = load_client_config()  # mens_fashion_alto, ya validado
        payload = {
            "client_id": self.client_id,
            "display_name": "Cliente de prueba",
            "tenant": "Cliente de prueba",
            "model": "gemini-3.7-flash",
            "data_map": str(
                base_client.data_map_path.relative_to(self.project_root)
            ).replace("\\", "/"),
            "sources": {
                next(iter(base_client.sources)): {
                    "tenant_field": next(iter(base_client.sources.values())).tenant_field
                }
            },
            "business_rulebooks": {"coaching_playbook": rulebook_payload},
        }
        (self.client_folder / "config.yaml").write_text(
            self.yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8"
        )

    def test_local_source_accepts_an_existing_repo_path(self) -> None:
        self._write_config(
            {
                "source": "local",
                "path": "2. clientes/_shared/business_rulebooks/coaching_playbook.md",
                "business_scope": "Guía de coaching",
            }
        )
        client = load_client_config(self.client_id)
        rulebook = client.business_rulebooks["coaching_playbook"]
        self.assertEqual(rulebook.source, "local")
        self.assertIsNone(rulebook.label)
        self.assertTrue(rulebook.path.is_file())

    def test_local_source_rejects_a_missing_file(self) -> None:
        self._write_config(
            {
                "source": "local",
                "path": "2. clientes/_shared/business_rulebooks/no_existe.md",
                "business_scope": "Guía de coaching",
            }
        )
        with self.assertRaises(ValueError):
            load_client_config(self.client_id)

    def test_unknown_source_value_is_rejected(self) -> None:
        self._write_config(
            {
                "source": "s3",
                "path": "2. clientes/_shared/business_rulebooks/coaching_playbook.md",
                "business_scope": "Guía de coaching",
            }
        )
        with self.assertRaises(ValueError):
            load_client_config(self.client_id)


class AllClientsInvariantTests(unittest.TestCase):
    """Chequeos estructurales sobre TODOS los clientes a la vez -nada en
    client_config.py valida esto por cliente individual, así que sólo se
    detecta corriendo la lista completa. Pensado para atrapar el error de
    copiar el config.yaml de un cliente existente como plantilla para uno
    nuevo y olvidar actualizar un campo."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.clients = {cid: load_client_config(cid) for cid in available_client_ids()}

    def test_all_client_configs_load_without_error(self) -> None:
        self.assertGreaterEqual(len(self.clients), 15, "se esperaban ~19 clientes activos")

    def test_client_id_is_unique_across_all_clients(self) -> None:
        """client_config.client_id (distinto del nombre de carpeta, ver
        data_map_auto_update.py::run_gate) se usa como namespace de cache para
        business_rules.py y rag_sources.py, y como etiqueta en
        usage_tracking.py -una colisión mezclaría el cache de reglas de
        negocio o de RAG entre dos clientes distintos, o falsearía el costo
        reportado por cliente. Nada en load_client_config lo valida por
        cliente individual, sólo se detecta comparando contra el resto."""
        seen: dict[str, str] = {}
        for folder, client in self.clients.items():
            self.assertNotIn(
                client.client_id,
                seen,
                f"client_id {client.client_id!r} usado por '{folder}' y por '{seen.get(client.client_id)}'",
            )
            seen[client.client_id] = folder

    def test_no_two_clients_share_tenant_and_an_overlapping_source(self) -> None:
        """El tenant NO tiene que ser único por sí solo -las tres sub-marcas de
        Huerpel comparten tenant='Huerpel' a propósito, ver README > 'Onboarding
        de Huerpel'-. Lo que sí tiene que ser siempre cierto, porque es la base
        real del aislamiento de sql_security.py, es que dos clientes con el
        MISMO tenant nunca declaren la MISMA fuente física: si la compartieran,
        una consulta bien aislada por tenant de un cliente igual podría
        devolver filas que el Data Map del otro cliente no documenta ni
        autoriza para ese propósito."""
        by_tenant: dict[str, list[tuple[str, set[str]]]] = {}
        for folder, client in self.clients.items():
            by_tenant.setdefault(client.tenant, []).append((folder, set(client.sources)))
        for tenant, entries in by_tenant.items():
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    folder_a, sources_a = entries[i]
                    folder_b, sources_b = entries[j]
                    overlap = sources_a & sources_b
                    self.assertFalse(
                        overlap,
                        f"'{folder_a}' y '{folder_b}' comparten tenant {tenant!r} Y las fuentes {overlap}",
                    )


if __name__ == "__main__":
    unittest.main()
