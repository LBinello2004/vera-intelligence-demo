"""Envoltorio diario del refresco del Data Map (VM). Usa repos de git REALES en carpetas temporales (un remoto bare y un clon de trabajo);
los procesos de cada cliente son un doble que devuelve el JSON final de data_map_auto_update.py y hace sus efectos en disco."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import run_daily_refresh as rdr  # noqa: E402

INNER = Path("experimental_proyects") / "Vera Intelligence"


def sh(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


def init_identity(repo: Path) -> None:
    sh(repo, "config", "user.email", "test@example.com")
    sh(repo, "config", "user.name", "Test")
    sh(repo, "config", "commit.gpgsign", "false")


class RepoCase(unittest.TestCase):
    CLIENTS = ("acme_alto", "beta_medio")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], capture_output=True, check=True)
        self.repo = base / "work"
        subprocess.run(["git", "clone", str(self.remote), str(self.repo)], capture_output=True, check=True)
        init_identity(self.repo)
        sh(self.repo, "checkout", "-b", "main")
        self.project = self.repo / INNER
        self.clients_root = self.project / "2. clientes"
        for name in self.CLIENTS:
            (self.clients_root / name / "data_map").mkdir(parents=True)
            (self.clients_root / name / "config.yaml").write_text(
                f'client_id: {name}\ndata_map: "2. clientes/{name}/data_map/VI Data Map {name} V1.yaml"\n', encoding="utf-8")
            (self.clients_root / name / "data_map" / f"VI Data Map {name} V1.yaml").write_text("metadata: {}\n", encoding="utf-8")
        (self.clients_root / "_shared").mkdir()            # no es un cliente: no tiene config.yaml
        (self.clients_root / "_shared" / "nota.md").write_text("x", encoding="utf-8")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-m", "base")
        sh(self.repo, "push", "-u", "origin", "main")
        self.report_dir = base / "reports"
        self.lock = base / "daily.lock"
        self.outcomes: dict[str, object] = {}
        self.calls: list[str] = []
        self.posts: list[dict] = []

    # ------------------------------------------------------------------ doble del proceso de cada cliente
    def fake_runner(self, cmd, **kwargs):
        client = cmd[cmd.index("--client") + 1]
        self.calls.append(client)
        outcome = self.outcomes.get(client, "sin_cambios")
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        if outcome == "crash":
            return SimpleNamespace(returncode=1, stdout="Traceback (most recent call last):\nboom", stderr="")
        status = outcome
        if status == "promovido":
            folder = self.clients_root / client
            new_map = folder / "data_map" / f"VI Data Map {client} V2.yaml"
            new_map.write_text("metadata: {v: 2}\n", encoding="utf-8")
            rdr.data_map_log.append_entry(self.clients_root, client, rdr.data_map_log.build_entry(
                tipo="promovido", client=client, old_version=1, new_version=2))
            config = folder / "config.yaml"
            config.write_text(config.read_text(encoding="utf-8").replace(" V1.yaml", " V2.yaml"), encoding="utf-8")
        if status == "gate_fallo_no_promovido":      # deja una candidata sin versionar, como el script real
            (self.clients_root / client / "data_map" / f"VI Data Map {client} V2.yaml").write_text("metadata: {rechazada: 1}\n",
                                                                                                    encoding="utf-8")
        summary = {"client_id": client, "status": status}
        banner = "ATENCIÓN: ...\n" if status in rdr.NEEDS_HUMAN else ""
        return SimpleNamespace(returncode=1 if status in rdr.NEEDS_HUMAN else 0, stdout=banner + json.dumps(summary, indent=2), stderr="")

    def run_daily(self, *argv: str) -> int:
        args = rdr.parse_args(list(argv))
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x"}):
            return rdr.run_daily(args, git=rdr.Git(self.project), runner=self.fake_runner, sleep=lambda s: None,
                                 post=lambda url, payload: self.posts.append(payload), clients_root=self.clients_root,
                                 project_root=self.project, report_dir=self.report_dir, lock_path=self.lock)

    def remote_log(self) -> list[str]:
        return sh(self.remote, "log", "--format=%s", "main").splitlines()

    def remote_files(self, ref: str = "main") -> set[str]:
        return set(sh(self.remote, "ls-tree", "-r", "--name-only", ref).splitlines())


class DailyRunTests(RepoCase):
    def test_sin_cambios_no_commitea_nada_y_deja_el_reporte(self) -> None:
        code = self.run_daily()
        self.assertEqual(code, rdr.EXIT_OK)
        self.assertEqual(self.remote_log(), ["base"])
        report = json.loads((self.report_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.json").read_text(encoding="utf-8"))
        self.assertEqual((report["clients_run"], report["counts"]), (2, {"sin_cambios": 2}))
        self.assertEqual(self.posts, [])                  # nada que contar: no molesta

    def test_solo_corre_los_clientes_reales(self) -> None:
        self.run_daily()
        self.assertEqual(sorted(self.calls), ["acme_alto", "beta_medio"])   # _shared no tiene config.yaml

    def test_lo_promovido_se_publica_en_un_solo_commit_con_exactamente_sus_archivos(self) -> None:
        self.outcomes = {"acme_alto": "promovido"}
        code = self.run_daily()
        self.assertEqual(code, rdr.EXIT_OK)
        self.assertEqual(len(self.remote_log()), 2)
        self.assertIn("acme_alto", self.remote_log()[0])
        added = self.remote_files()
        base = "experimental_proyects/Vera Intelligence/2. clientes/acme_alto"
        self.assertIn(f"{base}/data_map/VI Data Map acme_alto V2.yaml", added)
        self.assertIn(f"{base}/data_map/CAMBIOS_AUTOMATICOS.md", added)      # el registro viaja en el mismo commit
        self.assertIn(f"{base}/data_map/CAMBIOS_AUTOMATICOS.jsonl", added)
        self.assertIn('VI Data Map acme_alto V2.yaml"', sh(self.remote, "show", "main:" + f"{base}/config.yaml"))
        self.assertEqual(sh(self.repo, "status", "--porcelain", "-uno").strip(), "")   # nada sin commitear
        self.assertTrue(any("promovido" in str(p) or "Promovidos: acme_alto" in str(p) for p in self.posts))

    def test_varios_promovidos_van_en_un_solo_commit_un_solo_push(self) -> None:
        self.outcomes = {"acme_alto": "promovido", "beta_medio": "promovido"}
        self.run_daily()
        self.assertEqual(len(self.remote_log()), 2)       # base + UN commit
        self.assertEqual(sum(1 for f in self.remote_files() if f.endswith("V2.yaml")), 2)

    def test_una_candidata_rechazada_no_se_publica_ni_bloquea_el_repo(self) -> None:
        self.outcomes = {"acme_alto": "gate_fallo_no_promovido", "beta_medio": "promovido"}
        code = self.run_daily()
        self.assertEqual(code, rdr.EXIT_ATTENTION)
        files = self.remote_files()
        self.assertNotIn("experimental_proyects/Vera Intelligence/2. clientes/acme_alto/data_map/VI Data Map acme_alto V2.yaml", files)
        self.assertIn("experimental_proyects/Vera Intelligence/2. clientes/beta_medio/data_map/VI Data Map beta_medio V2.yaml", files)
        self.assertEqual(self.run_daily(), rdr.EXIT_ATTENTION)     # el archivo sin versionar no hace que el siguiente día se corte
        self.assertEqual(self.calls.count("acme_alto"), 2)

    def test_un_cliente_que_requiere_revision_da_codigo_1_alerta_y_va_primero_en_el_reporte(self) -> None:
        self.outcomes = {"beta_medio": "reintentos_agotados"}
        self.assertEqual(self.run_daily(), rdr.EXIT_ATTENTION)
        markdown = (self.report_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.md").read_text(encoding="utf-8")
        self.assertTrue(markdown.startswith("⚠️ ACCIÓN REQUERIDA: beta_medio"))
        self.assertTrue(self.posts and "beta_medio" in self.posts[0]["text"])

    def test_una_falla_de_proceso_se_reintenta_y_se_recupera(self) -> None:
        self.outcomes = {"acme_alto": ["crash", "crash", "sin_cambios"]}
        self.assertEqual(self.run_daily("--retries", "2"), rdr.EXIT_OK)
        self.assertEqual(self.calls.count("acme_alto"), 3)

    def test_el_error_de_un_cliente_muestra_la_causa_y_no_el_inicio_del_traceback(self) -> None:
        trace = ("Traceback (most recent call last):\n  File \"x.py\", line 173, in raise_for_response\n    cls.raise_error(\n"
                 "google.genai.errors.ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'message': 'Quota exceeded'}}\n")
        self.assertTrue(rdr.last_error_line(trace).startswith("google.genai.errors.ClientError: 429"))
        self.assertEqual(rdr.last_error_line(""), "")
        runner = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr=trace)      # noqa: E731
        result = rdr.run_client_process("acme_alto", gate="v2", dry_run=False, timeout_seconds=5, runner=runner)
        self.assertTrue(result["error"].startswith("código 1: google.genai.errors.ClientError: 429"))

    def test_una_caida_transitoria_de_infraestructura_se_reintenta_pero_una_de_configuracion_no(self) -> None:
        def runner_for(payload):
            calls = []

            def runner(cmd, **kwargs):
                calls.append(1)
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            return runner, calls

        transient, calls = runner_for({"client_id": "x", "status": "infraestructura_no_disponible", "needs_human": False})
        rdr.run_with_retries("x", retries=2, sleep=lambda s: None, gate="v2", dry_run=False, timeout_seconds=5, runner=transient)
        self.assertEqual(len(calls), 3)
        config, calls = runner_for({"client_id": "x", "status": "infraestructura_no_disponible", "needs_human": True, "kind": "configuracion"})
        result = rdr.run_with_retries("x", retries=2, sleep=lambda s: None, gate="v2", dry_run=False, timeout_seconds=5, runner=config)
        self.assertEqual(len(calls), 1)
        self.assertTrue(rdr.needs_human(result))

    def run_with_preflight(self, preflight_fn, *argv):
        args = rdr.parse_args(list(argv))
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x"}):
            return rdr.run_daily(args, git=rdr.Git(self.project), runner=self.fake_runner, sleep=lambda s: None,
                                 post=lambda url, payload: self.posts.append(payload), clients_root=self.clients_root,
                                 project_root=self.project, report_dir=self.report_dir, lock_path=self.lock, preflight_fn=preflight_fn)

    def test_si_la_verificacion_previa_falla_no_corre_ningun_cliente_y_avisa_una_vez(self) -> None:
        code = self.run_with_preflight(lambda: ["Gemini no responde: ServerError: 503", "Postgres no responde: OperationalError"])
        self.assertEqual(code, rdr.EXIT_CANNOT_START)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("Gemini no responde", self.posts[0]["text"])
        self.assertIn("Postgres no responde", self.posts[0]["text"])

    def test_si_la_verificacion_previa_pasa_corre_normal_y_se_puede_omitir(self) -> None:
        self.assertEqual(self.run_with_preflight(lambda: []), rdr.EXIT_OK)
        self.assertEqual(sorted(self.calls), ["acme_alto", "beta_medio"])
        self.calls.clear()
        self.assertEqual(self.run_with_preflight(lambda: ["no debía llamarse"], "--no-preflight"), rdr.EXIT_OK)
        self.assertEqual(len(self.calls), 2)

    def test_la_verificacion_previa_detecta_variables_faltantes_sin_tocar_la_red(self) -> None:
        with patch.dict(os.environ, {"VERA_AI_API_KEY": "", "PGPASSWORD": "x", "LANGFUSE_PUBLIC_KEY": "x", "LANGFUSE_SECRET_KEY": "x",
                                     "LANGFUSE_BASE_URL": "https://x"}):
            problems = rdr.preflight()
        self.assertEqual(len(problems), 1)
        self.assertIn("VERA_AI_API_KEY", problems[0])

    def test_el_reporte_trae_la_duracion_y_el_cliente_mas_lento(self) -> None:
        self.run_daily()
        report = json.loads((self.report_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.json").read_text(encoding="utf-8"))
        self.assertIn("duration_seconds", report)
        self.assertTrue(all("seconds" in item for item in report["results"]))
        self.assertEqual(len(report["slowest"]), 2)
        slow = {"date": "2026-10-09", "clients_run": 1, "counts": {"promovido": 1}, "needs_human": [], "errors": [], "promoted": [],
                "publication": None, "results": [], "duration_seconds": 1800.0, "slowest": [{"client_id": "farma24_alto", "seconds": 1500.0}]}
        self.assertIn("más lento: farma24_alto (25.0 min)", rdr.render_markdown(slow))

    def test_un_timeout_no_se_reintenta_y_no_frena_a_los_demas(self) -> None:
        self.outcomes = {"acme_alto": "timeout"}
        self.assertEqual(self.run_daily("--retries", "2"), rdr.EXIT_ATTENTION)
        self.assertEqual(self.calls.count("acme_alto"), 1)             # rehacer una hora de trabajo no suele cambiar el resultado
        self.assertEqual(self.calls.count("beta_medio"), 1)

    def test_los_clientes_corren_a_la_vez_y_el_resultado_conserva_el_orden(self) -> None:
        import threading
        active, peak, lock = [0], [0], threading.Lock()
        original = self.fake_runner

        def slow(cmd, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.3)
            with lock:
                active[0] -= 1
            return original(cmd, **kwargs)

        args = rdr.parse_args(["--parallel", "2"])
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": ""}):
            rdr.run_daily(args, git=rdr.Git(self.project), runner=slow, sleep=lambda s: None, post=lambda u, p: None,
                          clients_root=self.clients_root, project_root=self.project, report_dir=self.report_dir, lock_path=self.lock)
        self.assertEqual(peak[0], 2)
        report = json.loads((self.report_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.json").read_text(encoding="utf-8"))
        self.assertEqual([r["client_id"] for r in report["results"]], ["acme_alto", "beta_medio"])

    def test_si_la_falla_persiste_queda_como_error_de_proceso(self) -> None:
        self.outcomes = {"acme_alto": "crash"}
        self.assertEqual(self.run_daily("--retries", "1"), rdr.EXIT_ATTENTION)
        report = json.loads((self.report_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.json").read_text(encoding="utf-8"))
        self.assertEqual(report["errors"], ["acme_alto"])
        self.assertEqual(self.calls.count("acme_alto"), 2)
        self.assertEqual(self.calls.count("beta_medio"), 1)            # un cliente caído no frena a los demás

    def test_un_rechazo_del_gate_no_se_reintenta_desde_el_envoltorio(self) -> None:
        self.outcomes = {"acme_alto": "gate_fallo_no_promovido"}
        self.run_daily("--retries", "3")
        self.assertEqual(self.calls.count("acme_alto"), 1)             # ya tiene sus reintentos propios en data_map_auto_update

    def test_dry_run_no_publica(self) -> None:
        self.outcomes = {"acme_alto": "promovido"}
        self.run_daily("--dry-run")
        self.assertEqual(self.remote_log(), ["base"])

    def test_no_push_commitea_en_local_pero_no_publica(self) -> None:
        self.outcomes = {"acme_alto": "promovido"}
        self.assertEqual(self.run_daily("--no-push"), rdr.EXIT_OK)
        self.assertEqual(self.remote_log(), ["base"])
        self.assertEqual(len(sh(self.repo, "log", "--format=%s").splitlines()), 2)

    def test_clientes_inexistentes_cortan_antes_de_correr_nada(self) -> None:
        self.assertEqual(self.run_daily("--clients", "acme_alto,fantasma"), rdr.EXIT_CANNOT_START)
        self.assertEqual(self.calls, [])

    def test_filtro_de_clientes(self) -> None:
        self.run_daily("--clients", "beta_medio")
        self.assertEqual(self.calls, ["beta_medio"])


class RepoStateTests(RepoCase):
    def test_cambios_ajenos_sin_commitear_cortan_sin_tocar_nada(self) -> None:
        (self.project / "2. clientes" / "acme_alto" / "data_map" / "VI Data Map acme_alto V1.yaml").write_text("editado a mano\n",
                                                                                                          encoding="utf-8")
        self.assertEqual(self.run_daily(), rdr.EXIT_CANNOT_START)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("no toco nada" in p["text"] for p in self.posts))
        self.assertIn("editado a mano", (self.project / "2. clientes" / "acme_alto" / "data_map" / "VI Data Map acme_alto V1.yaml").read_text(encoding="utf-8"))

    def test_lo_promovido_y_no_publicado_de_una_corrida_anterior_se_publica_primero(self) -> None:
        # simula una corrida que promovió pero cuyo push falló: config.yaml modificado + data map nuevo sin versionar
        folder = self.clients_root / "acme_alto"
        (folder / "data_map" / "VI Data Map acme_alto V2.yaml").write_text("metadata: {v: 2}\n", encoding="utf-8")
        config = folder / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace(" V1.yaml", " V2.yaml"), encoding="utf-8")
        self.assertEqual(self.run_daily(), rdr.EXIT_OK)
        self.assertEqual(len(self.remote_log()), 2)
        self.assertIn("experimental_proyects/Vera Intelligence/2. clientes/acme_alto/data_map/VI Data Map acme_alto V2.yaml", self.remote_files())
        self.assertEqual(sorted(self.calls), ["acme_alto", "beta_medio"])        # y siguió con el refresco del día

    def test_si_el_remoto_avanzo_el_push_se_rehace_con_rebase(self) -> None:
        other = Path(self._tmp.name) / "otro"
        subprocess.run(["git", "clone", str(self.remote), str(other)], capture_output=True, check=True)
        init_identity(other)
        (other / "README.md").write_text("otro cambio", encoding="utf-8")
        sh(other, "add", "-A")
        sh(other, "commit", "-m", "cambio de otro")
        sh(other, "push", "origin", "main")
        git = rdr.Git(self.project)

        class StaleGit(rdr.Git):       # el pull del arranque ya pasó: simulamos que el remoto avanzó después
            pass

        folder = self.clients_root / "acme_alto"
        (folder / "data_map" / "VI Data Map acme_alto V2.yaml").write_text("metadata: {v: 2}\n", encoding="utf-8")
        config = folder / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace(" V1.yaml", " V2.yaml"), encoding="utf-8")
        top = git.toplevel()
        pending = rdr.publishable_paths(git, top, self.clients_root, self.project)
        result = rdr.publish(git, pending, push=True)
        self.assertTrue(result["published"], result)
        self.assertEqual(self.remote_log()[:2], [self.remote_log()[0], "cambio de otro"])
        self.assertIn("acme_alto", self.remote_log()[0])

    def test_si_el_push_no_se_puede_el_resultado_lo_dice_y_el_codigo_es_3(self) -> None:
        self.outcomes = {"acme_alto": "promovido"}
        sh(self.repo, "remote", "set-url", "origin", str(Path(self._tmp.name) / "no_existe.git"))
        with patch.object(rdr.Git, "run", wraps=None) as _:
            pass
        # `git pull --ff-only` del arranque también falla con un remoto inexistente: se corta antes (código 2) y avisa
        code = self.run_daily("--no-pull")
        self.assertEqual(code, rdr.EXIT_PUBLISH_FAILED)
        report = json.loads((self.report_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.json").read_text(encoding="utf-8"))
        self.assertTrue(report["publication"]["committed"])
        self.assertFalse(report["publication"]["published"])
        self.assertTrue(self.posts)

    def test_si_el_pull_falla_no_corre_nada(self) -> None:
        sh(self.repo, "remote", "set-url", "origin", str(Path(self._tmp.name) / "no_existe.git"))
        self.assertEqual(self.run_daily(), rdr.EXIT_CANNOT_START)
        self.assertEqual(self.calls, [])


class LockAndHelpersTests(unittest.TestCase):
    def test_candado_global_exclusivo_y_se_pisa_si_esta_vencido(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "x.lock"
            self.assertTrue(rdr.acquire_lock(lock))
            self.assertFalse(rdr.acquire_lock(lock))
            self.assertTrue(rdr.acquire_lock(lock, now=lambda: time.time() + rdr.LOCK_STALE_SECONDS + 5))
            rdr.release_lock(lock)
            self.assertFalse(lock.exists())

    def test_se_suelta_el_candado_aunque_falle_algo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "x.lock"
            args = rdr.parse_args(["--no-pull", "--no-push", "--clients", "inexistente"])
            code = rdr.run_daily(args, clients_root=Path(tmp), lock_path=lock, report_dir=Path(tmp) / "r", project_root=Path(tmp))
            self.assertEqual(code, rdr.EXIT_CANNOT_START)
            self.assertFalse(lock.exists())

    def test_un_segundo_refresco_simultaneo_no_corre(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "x.lock"
            rdr.acquire_lock(lock)
            code = rdr.run_daily(rdr.parse_args(["--no-pull", "--no-push"]), lock_path=lock, report_dir=Path(tmp) / "r")
            self.assertEqual(code, rdr.EXIT_CANNOT_START)

    def test_parse_summary_ignora_el_ruido_y_toma_el_ultimo_json(self) -> None:
        out = "log {suelto}\n" + json.dumps({"status": "sin_cambios", "client_id": "a"}, indent=2)
        self.assertEqual(rdr.parse_summary(out)["status"], "sin_cambios")
        self.assertIsNone(rdr.parse_summary("sin json\n"))
        self.assertIsNone(rdr.parse_summary("{\n  \"otra_cosa\": 1\n}"))

    def test_el_aviso_no_rompe_si_el_webhook_falla_y_no_molesta_sin_novedades(self) -> None:
        quiet = {"promoted": [], "needs_human": [], "errors": [], "publication": None}
        loud = {"promoted": ["a"], "needs_human": [], "errors": [], "publication": None}

        def boom(url, payload):
            raise RuntimeError("webhook caído")

        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x"}):
            self.assertFalse(rdr.notify(quiet, "x", post=lambda u, p: self.fail("no debía avisar")))
            self.assertFalse(rdr.notify(loud, "x", post=boom))
            self.assertTrue(rdr.notify(loud, "x", post=lambda u, p: None))
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": ""}):
            self.assertFalse(rdr.notify(loud, "x", post=lambda u, p: self.fail("sin webhook no avisa")))

    def test_load_env_lee_el_env_de_la_raiz_sin_pisar_lo_exportado(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a" / "b").mkdir(parents=True)
            (root / ".env").write_text("VI_NOTIFY_WEBHOOK=https://hooks.example/desde-env\nVI_NOTIFY_HEARTBEAT=1\n", encoding="utf-8")
            with patch.object(rdr, "PROJECT_ROOT", root / "a" / "b"), patch.dict(os.environ, {"VI_NOTIFY_HEARTBEAT": "0"}, clear=False):
                os.environ.pop("VI_NOTIFY_WEBHOOK", None)
                rdr.load_env()
                self.assertEqual(os.environ["VI_NOTIFY_WEBHOOK"], "https://hooks.example/desde-env")
                self.assertEqual(os.environ["VI_NOTIFY_HEARTBEAT"], "0")          # lo ya exportado manda
                os.environ.pop("VI_NOTIFY_WEBHOOK", None)

    def test_un_prompt_inaccesible_cuenta_como_revision_humana_aunque_haya_otro_resultado(self) -> None:
        problem = {"key": "rb", "prompt": "clientes/GAC/checklist", "http_status": 404, "needs_human": True}
        item = {"client_id": "gac_ventas_medio", "status": "promovido", "rulebook_problems": [problem]}
        self.assertTrue(rdr.needs_human(item))
        self.assertTrue(rdr.needs_human({"client_id": "x", "status": "prompt_no_disponible"}))
        self.assertFalse(rdr.needs_human({"client_id": "x", "status": "sin_cambios", "rulebook_problems": [{"needs_human": False}]}))
        report = rdr.build_report([item], datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 10, 8, tzinfo=timezone.utc), None)
        self.assertEqual(report["needs_human"], ["gac_ventas_medio"])
        text = rdr.render_markdown(report)
        self.assertIn("ACCIÓN REQUERIDA: gac_ventas_medio", text)
        self.assertIn("clientes/GAC/checklist", text)

    def test_un_error_inesperado_avisa_por_webhook_y_sale_con_codigo_4(self) -> None:
        sent: list[dict] = []
        args = rdr.parse_args([])
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x"}),                 patch.object(rdr, "run_daily", side_effect=KeyError("config")), patch.object(rdr.traceback, "print_exc"):
            code = rdr.run_guarded(args, post=lambda url, payload: sent.append(payload))
        self.assertEqual(code, rdr.EXIT_CRASH)
        self.assertEqual(rdr.EXIT_CRASH, 4)
        self.assertEqual(len(sent), 1)
        self.assertIn("ACCIÓN REQUERIDA", sent[0]["text"])
        self.assertIn("KeyError", sent[0]["text"])

    def test_sin_error_run_guarded_devuelve_el_codigo_de_run_daily_y_no_avisa(self) -> None:
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x"}), patch.object(rdr, "run_daily", return_value=1):
            self.assertEqual(rdr.run_guarded(rdr.parse_args([]), post=lambda u, p: self.fail("no debía avisar")), 1)

    def test_el_latido_opcional_avisa_un_todo_bien_corto_solo_si_se_activa(self) -> None:
        quiet = {"promoted": [], "needs_human": [], "errors": [], "publication": None, "date": "2026-10-08", "clients_run": 19}
        sent: list[dict] = []
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x", "VI_NOTIFY_HEARTBEAT": "1"}):
            self.assertTrue(rdr.notify(quiet, "texto largo del reporte", post=lambda u, p: sent.append(p)))
        self.assertEqual(len(sent), 1)
        self.assertIn("19 clientes", sent[0]["text"])
        self.assertNotIn("texto largo", sent[0]["text"])
        with patch.dict(os.environ, {"VI_NOTIFY_WEBHOOK": "https://hooks.example/x", "VI_NOTIFY_HEARTBEAT": ""}):
            self.assertFalse(rdr.notify(quiet, "x", post=lambda u, p: self.fail("sin latido no avisa")))


if __name__ == "__main__":
    unittest.main()
