from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import business_rules  # noqa: E402
from business_rules import BusinessRulesRepository  # noqa: E402
from client_config import load_client_config  # noqa: E402
from runtime_control import OperationalUnavailable  # noqa: E402


class BusinessRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = load_client_config()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        # _fetch() reusa una requests.Session cacheada a nivel de módulo (2026-09-11, ver
        # _get_reusable_http_session) en vez de requests.get() suelto -sin resetear acá, una
        # sesión (mockeada o real) cacheada por un test anterior sobreviviría al siguiente,
        # ignorando el patch de ESTE test. Mismo criterio que los resets de cliente/conexión
        # reusados en test_vi_agent.py/test_vector_search.py.
        business_rules._cached_http_session = None
        self.addCleanup(setattr, business_rules, "_cached_http_session", None)

    @patch.dict(
        "os.environ",
        {
            "LANGFUSE_PUBLIC_KEY": "public",
            "LANGFUSE_SECRET_KEY": "secret",
            "LANGFUSE_BASE_URL": "https://langfuse.example",
        },
        clear=False,
    )
    @patch("business_rules._get_reusable_http_session")
    def test_fetches_only_fixed_production_rulebook(self, get_session: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"version": 9, "prompt": "Criterio vigente"}
        session = Mock()
        session.get.return_value = response
        get_session.return_value = session
        repository = BusinessRulesRepository(self.client, self.project_root)

        payload = json.loads(repository.get("sales_evaluation"))

        self.assertEqual(payload["rules_version"], 9)
        self.assertEqual(payload["criteria_text"], "Criterio vigente")
        self.assertNotIn("prompt", payload)
        called_url = session.get.call_args.args[0]
        self.assertIn("clientes%2FHaber%2FMens%20Fashion%2Fchecklist", called_url)
        self.assertEqual(session.get.call_args.kwargs["params"], {"label": "production"})

    @patch("business_rules._get_reusable_http_session")
    def test_rejects_unconfigured_rulebook_without_network_call(self, get_session: Mock) -> None:
        repository = BusinessRulesRepository(self.client, self.project_root)
        with self.assertRaises(ValueError):
            repository.get("otro_cliente")
        get_session.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "LANGFUSE_PUBLIC_KEY": "public",
            "LANGFUSE_SECRET_KEY": "secret",
            "LANGFUSE_BASE_URL": "https://langfuse.example",
        },
        clear=False,
    )
    @patch("business_rules._get_reusable_http_session")
    def test_falls_back_to_last_known_good_snapshot(self, get_session: Mock) -> None:
        success = Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"version": 7, "prompt": "Regla guardada"}
        session = Mock()
        session.get.return_value = success
        get_session.return_value = session
        first = BusinessRulesRepository(self.client, self.project_root)
        first.get("conversation_insights")

        session.get.side_effect = requests.ConnectionError("offline")
        second = BusinessRulesRepository(self.client, self.project_root)
        payload = json.loads(second.get("conversation_insights"))

        self.assertEqual(payload["criteria_text"], "Regla guardada")
        self.assertEqual(payload["source_status"], "last_known_good")

    @patch("business_rules._get_reusable_http_session")
    def test_local_rulebook_reads_the_repo_file_without_touching_langfuse(
        self, get_session: Mock
    ) -> None:
        # coaching_playbook (2026-09-11, ver "3. experimentos/coaching_playbook/README.md") es
        # source="local" -a pedido explícito de no publicar nada en Langfuse. Regresión: nunca
        # debe llamar a la sesión HTTP, y el contenido tiene que salir tal cual del archivo del
        # repo.
        repository = BusinessRulesRepository(self.client, self.project_root)

        payload = json.loads(repository.get("coaching_playbook"))

        get_session.assert_not_called()
        self.assertEqual(payload["source_status"], "local_file")
        self.assertEqual(payload["rules_version"], "local")
        self.assertIn("coach comercial senior", payload["criteria_text"])

    def test_unavailable_rulebook_without_snapshot_is_a_terminal_operational_error(self) -> None:
        repository = BusinessRulesRepository(self.client, self.project_root)
        with patch.object(repository, "_fetch", side_effect=RuntimeError("credenciales secretas")):
            with self.assertRaises(OperationalUnavailable) as caught:
                repository.get("sales_evaluation")
        self.assertNotIn("secretas", caught.exception.user_message)

    def test_missing_local_file_is_a_terminal_operational_error(self) -> None:
        from dataclasses import replace

        rulebooks = dict(self.client.business_rulebooks)
        rulebooks["coaching_playbook"] = replace(rulebooks["coaching_playbook"], path=self.project_root / "missing.md")
        repository = BusinessRulesRepository(replace(self.client, business_rulebooks=rulebooks), self.project_root)
        with self.assertRaises(OperationalUnavailable):
            repository.get("coaching_playbook")


if __name__ == "__main__":
    unittest.main()
