"""Refresco diario del Data Map para correr en una VM (cron / tarea programada). 2026-10-07.

Reemplaza a la rutina de Claude que lanzaba `data_map_auto_update.py` cliente por cliente: no necesita un modelo para eso, no depende de
que una app esté abierta y deja un reporte con código de salida para que un monitor sepa si hizo falta mirar algo.

Qué hace, en orden:
1. Candado global (un solo refresco a la vez en la máquina; vence a las 6 h por si un proceso murió).
2. Publica PENDIENTES: si una corrida anterior promovió un Data Map pero no llegó a publicarlo (push fallido), lo publica primero.
3. Se asegura de que el repo esté limpio y al día (`git pull --ff-only`). Si hay cambios ajenos sin commitear, NO toca nada y avisa.
4. Lista los clientes (carpetas de `2. clientes/` con `config.yaml`) y corre cada uno en su PROPIO proceso
   (`data_map_auto_update.py --client X`: `vi_agent` guarda el cliente activo en variables globales, así que no se pueden mezclar en un
   mismo proceso), con límite de tiempo y reintentos ante fallas de proceso o de red.
5. Escribe el reporte del día (`.runtime/daily_refresh/AAAA-MM-DD.json` y `.md`).
6. Si algún cliente se PROMOVIÓ: un solo commit con exactamente `config.yaml` y el Data Map nuevo de cada uno y push. Un solo push por
   día = a lo sumo una actualización de la app (Streamlit Cloud toma el push solo; no hace falta reiniciarla a mano).
7. Avisa por webhook (opcional: `VI_NOTIFY_WEBHOOK`, compatible con Slack) si hubo promociones, clientes a revisar, errores o un push fallido.

Códigos de salida: 0 todo bien (haya o no promociones) · 1 algún cliente requiere revisión o falló · 2 no se pudo empezar (candado, repo
sucio, pull) · 3 se promovió pero falló la publicación.

Uso (desde cualquier carpeta):  python "<repo>/experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py"
Opciones: --clients a,b · --no-pull · --no-push · --gate v2|legacy · --timeout-minutes 60 · --retries 2 · --dry-run
Ejemplo de cron (05:30 de lunes a viernes):
  30 5 * * 1-5  cd /ruta/al/repo && /ruta/al/venv/bin/python "experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py" >> /var/log/vi_refresh.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import data_map_log

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
RUNTIME = PROJECT_ROOT / ".runtime"
REPORT_DIR = RUNTIME / "daily_refresh"
LOCK_PATH = RUNTIME / "daily_refresh.lock"
UPDATER = SCRIPT_DIR / "data_map_auto_update.py"

LOCK_STALE_SECONDS = 6 * 3600
DEFAULT_TIMEOUT_MINUTES = 60
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 30
NEEDS_HUMAN = {"gate_fallo_no_promovido", "error_regeneracion", "deriva_de_columnas_no_promovido", "reintentos_agotados"}
PROCESS_ERROR = "error_de_proceso"
DATA_MAP_LINE = re.compile(r'^data_map:\s*"(.*)"[ \t]*$', re.MULTILINE)

EXIT_OK, EXIT_ATTENTION, EXIT_CANNOT_START, EXIT_PUBLISH_FAILED = 0, 1, 2, 3


# --------------------------------------------------------------------------------------------- git
class Git:
    """Mínimo envoltorio de git sobre un repo. `run` es inyectable en los tests."""

    def __init__(self, cwd: Path, runner: Callable[..., subprocess.CompletedProcess] | None = None) -> None:
        self.cwd = cwd
        self._top: Path | None = None
        self._runner = runner or subprocess.run

    def run(self, *args: str, check: bool = False) -> tuple[int, str]:
        # Desde que se conoce la raíz del repo, TODOS los comandos corren ahí: git interpreta las rutas relativas respecto de la carpeta
        # desde la que se lo ejecuta (no de la raíz) y las rutas que arma este módulo son relativas a la raíz.
        proc = self._runner(["git", "-C", str(self._top or self.cwd), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} falló ({proc.returncode}): {out.strip()[:300]}")
        return proc.returncode, out

    def toplevel(self) -> Path | None:
        if self._top is None:
            code, out = self.run("rev-parse", "--show-toplevel")
            if code == 0 and out.strip():
                self._top = Path(out.strip())
        return self._top


def _relative_to_git(path: Path, top: Path) -> str:
    return path.resolve().relative_to(top.resolve()).as_posix()


# --------------------------------------------------------------------------------------------- clientes y proceso
def list_clients(clients_root: Path = CLIENTS_ROOT, only: list[str] | None = None) -> list[str]:
    """Carpetas de clientes reales (con config.yaml), en orden. Sin lista fija: los clientes cambian con el tiempo."""
    found = sorted(p.name for p in clients_root.iterdir() if p.is_dir() and (p / "config.yaml").is_file())
    if only:
        missing = [name for name in only if name not in found]
        if missing:
            raise ValueError("Clientes inexistentes: " + ", ".join(missing))
        return [name for name in found if name in only]
    return found


def parse_summary(stdout: str) -> dict | None:
    """El JSON final que imprime data_map_auto_update.py (el último objeto de primer nivel de la salida)."""
    starts = [m.start() for m in re.finditer(r"^\{", stdout, re.MULTILINE)]
    for start in reversed(starts):
        try:
            value = json.loads(stdout[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "status" in value:
            return value
    return None


def run_client_process(client: str, *, gate: str, dry_run: bool, timeout_seconds: float,
                       runner: Callable[..., subprocess.CompletedProcess] | None = None) -> dict:
    cmd = [sys.executable, str(UPDATER), "--client", client, "--gate", gate] + (["--dry-run"] if dry_run else [])
    try:
        proc = (runner or subprocess.run)(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                          timeout=timeout_seconds, cwd=str(SCRIPT_DIR))
    except subprocess.TimeoutExpired:
        return {"client_id": client, "status": PROCESS_ERROR, "error": f"timeout de {timeout_seconds / 60:.0f} min"}
    summary = parse_summary(proc.stdout or "")
    if summary is None or proc.returncode not in (0, 1):
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-600:]
        return {"client_id": client, "status": PROCESS_ERROR, "error": f"código {proc.returncode}: {tail}"}
    summary.setdefault("client_id", client)
    return summary


def run_with_retries(client: str, *, retries: int, sleep: Callable[[float], None] = time.sleep, **kwargs) -> dict:
    """Reintenta SÓLO fallas de proceso (red, timeout, caída de la base): un `gate_fallo` ya tiene sus reintentos propios."""
    summary: dict = {}
    for attempt in range(retries + 1):
        summary = run_client_process(client, **kwargs)
        if summary.get("status") != PROCESS_ERROR:
            break
        if attempt < retries:
            sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    summary["process_attempts"] = attempt + 1
    return summary


# --------------------------------------------------------------------------------------------- candado global
def acquire_lock(path: Path = LOCK_PATH, now: Callable[[], float] = time.time) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                started = float(json.loads(path.read_text(encoding="utf-8")).get("started_at", 0))
            except (OSError, ValueError):
                started = 0.0
            if now() - started < LOCK_STALE_SECONDS:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": now()}, handle)
        return True
    return False


def release_lock(path: Path = LOCK_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------------------------- publicación
def _config_data_map(clients_root: Path, client: str) -> str | None:
    text = (clients_root / client / "config.yaml").read_text(encoding="utf-8")
    match = DATA_MAP_LINE.search(text)
    return match.group(1) if match else None


def publishable_paths(git: Git, top: Path, clients_root: Path, project_root: Path) -> dict[str, list[str]]:
    """{cliente: [rutas relativas al repo]} de lo promovido y todavía sin publicar: `config.yaml` modificado + el Data Map al que apunta."""
    code, out = git.run("status", "--porcelain", "-uno", "--", _relative_to_git(clients_root, top))
    result: dict[str, list[str]] = {}
    if code != 0:
        return result
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        parts = Path(path).parts
        if len(parts) >= 2 and parts[-1] == "config.yaml" and parts[-2] != "2. clientes":
            client = parts[-2]
            data_map = _config_data_map(clients_root, client)
            paths = [path]
            if data_map:
                paths.append(_relative_to_git(project_root / data_map, top))
            for log_file in data_map_log.log_paths(clients_root, client):
                if log_file.is_file():
                    paths.append(_relative_to_git(log_file, top))
            result[client] = paths
    return result


def other_tracked_changes(git: Git, top: Path, publishable: dict[str, list[str]]) -> list[str]:
    """Cambios sin commitear en archivos versionados que NO son lo que este script publica (alguien tocó algo a mano)."""
    code, out = git.run("status", "--porcelain", "-uno")
    own = {path for paths in publishable.values() for path in paths}
    return [line[3:].strip().strip('"') for line in out.splitlines() if code == 0 and line[3:].strip().strip('"') not in own]


def _commit_message(clients: dict[str, list[str]], notes: dict[str, str] | None) -> str:
    names = ", ".join(sorted(clients))
    lines = [f"Data Map actualizado automáticamente: {names}", ""]
    for name in sorted(clients):
        if notes and notes.get(name):
            lines.append(f"- {name}: {notes[name]}")
    lines += ["", "Generado por run_daily_refresh.py (gate v2). El detalle de cada cambio y cómo revertirlo está en",
              "2. clientes/<cliente>/data_map/CAMBIOS_AUTOMATICOS.md."]
    return "\n".join(lines)


def publish(git: Git, clients: dict[str, list[str]], *, push: bool, message: str | None = None, notes: dict[str, str] | None = None) -> dict:
    """Un commit con exactamente esas rutas y push. Si el push es rechazado, un `pull --rebase` y un segundo intento."""
    if not clients:
        return {"published": False, "reason": "nada para publicar"}
    paths = sorted({p for values in clients.values() for p in values})
    names = ", ".join(sorted(clients))
    try:
        git.run("add", "-f", "--", *paths, check=True)   # -f: el .jsonl del registro cae bajo un `*.jsonl` ignorado en algunos repos
        git.run("commit", "-m", message or _commit_message(clients, notes), "--", *paths, check=True)
    except RuntimeError as exc:
        return {"published": False, "committed": False, "error": str(exc)[-400:], "clients": sorted(clients)}
    if not push:
        return {"published": False, "committed": True, "reason": "--no-push", "clients": sorted(clients)}
    code, out = git.run("push")
    if code != 0:
        git.run("pull", "--rebase")
        code, out = git.run("push")
    if code != 0:
        return {"published": False, "committed": True, "error": out.strip()[-400:], "clients": sorted(clients)}
    return {"published": True, "committed": True, "clients": sorted(clients)}


# --------------------------------------------------------------------------------------------- reporte y avisos
def build_report(results: list[dict], started: datetime, finished: datetime, publication: dict | None) -> dict:
    counts: dict[str, int] = {}
    for item in results:
        counts[item.get("status", "?")] = counts.get(item.get("status", "?"), 0) + 1
    return {
        "date": started.date().isoformat(), "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "clients_run": len(results), "counts": counts,
        "needs_human": sorted(i["client_id"] for i in results if i.get("status") in NEEDS_HUMAN),
        "errors": sorted(i["client_id"] for i in results if i.get("status") == PROCESS_ERROR),
        "promoted": sorted(i["client_id"] for i in results if i.get("status") == "promovido"),
        "publication": publication, "results": results,
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    if report["needs_human"] or report["errors"] or (report["publication"] or {}).get("error"):
        who = ", ".join(report["needs_human"] + report["errors"]) or "publicación"
        lines.append(f"⚠️ ACCIÓN REQUERIDA: {who}")
    lines.append(f"Refresco del {report['date']}: {report['clients_run']} clientes · " +
                 ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
    if report["promoted"]:
        lines.append("Promovidos: " + ", ".join(report["promoted"]))
    publication = report.get("publication")
    if publication:
        lines.append("Publicación: " + ("publicado" if publication.get("published") else
                                          f"NO publicado ({publication.get('error') or publication.get('reason')})"))
    for item in report["results"]:
        if item.get("status") in NEEDS_HUMAN or item.get("status") == PROCESS_ERROR:
            detail = item.get("error") or item.get("note") or item.get("candidate_path") or ""
            lines.append(f"- {item['client_id']}: {item['status']} {str(detail)[:200]}")
    return "\n".join(lines) + "\n"


def alert(text: str, post: Callable[[str, dict], None] | None = None) -> bool:
    """Aviso suelto por webhook (cortes antes de correr los clientes). Nunca rompe la corrida."""
    url = (os.environ.get("VI_NOTIFY_WEBHOOK") or "").strip()
    if not url:
        return False
    try:
        if post is None:
            import requests

            def post(target: str, payload: dict) -> None:
                requests.post(target, json=payload, timeout=15).raise_for_status()
        post(url, {"text": text})
        return True
    except Exception:  # noqa: BLE001
        return False


def notify(report: dict, markdown: str, post: Callable[[str, dict], None] | None = None) -> bool:
    """Webhook opcional (`VI_NOTIFY_WEBHOOK`, formato {"text": ...} de Slack). Sólo avisa si hay algo que contar; nunca rompe la corrida."""
    url = (os.environ.get("VI_NOTIFY_WEBHOOK") or "").strip()
    interesting = report["promoted"] or report["needs_human"] or report["errors"] or (report["publication"] or {}).get("error")
    if url and not interesting and (os.environ.get("VI_NOTIFY_HEARTBEAT") or "").strip() in ("1", "true", "yes"):
        # Latido opcional: un aviso corto de "todo bien" cada día, para que la AUSENCIA del mensaje delate una VM o un cron caídos
        # (si el proceso no corre, nadie puede avisar que no corrió).
        markdown = f"✅ Refresco del {report['date']}: {report['clients_run']} clientes revisados, sin cambios ni nada que revisar."
        interesting = True
    if not url or not interesting:
        return False
    try:
        if post is None:
            import requests

            def post(target: str, payload: dict) -> None:
                requests.post(target, json=payload, timeout=15).raise_for_status()
        post(url, {"text": markdown})
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------------- orquestación
def run_daily(args: argparse.Namespace, *, git: Git | None = None, runner: Callable[..., subprocess.CompletedProcess] | None = None,
              sleep: Callable[[float], None] = time.sleep, post: Callable[[str, dict], None] | None = None,
              clients_root: Path = CLIENTS_ROOT, project_root: Path = PROJECT_ROOT, report_dir: Path = REPORT_DIR,
              lock_path: Path = LOCK_PATH) -> int:
    started = datetime.now(timezone.utc)
    if not acquire_lock(lock_path):
        print("Ya hay un refresco en curso en esta máquina (candado vigente). No hago nada.", file=sys.stderr)
        return EXIT_CANNOT_START
    try:
        git = git or Git(project_root)
        top = git.toplevel() if (args.pull or args.push) else None
        publication: dict | None = None

        if top is not None:
            pending = publishable_paths(git, top, clients_root, project_root)
            foreign = other_tracked_changes(git, top, pending)
            if foreign:
                message = "Hay cambios sin commitear que no son del refresco; no toco nada: " + ", ".join(foreign[:8])
                print(message, file=sys.stderr)
                alert("⚠️ ACCIÓN REQUERIDA: refresco del Data Map no corrió. " + message, post)
                return EXIT_CANNOT_START
            if pending and args.push:
                publication = publish(git, pending, push=True)  # promociones de una corrida anterior que no se publicaron
                if not publication.get("published"):
                    message = "No pude publicar lo pendiente de una corrida anterior: " + str(publication.get("error"))
                    print(message, file=sys.stderr)
                    alert("⚠️ ACCIÓN REQUERIDA: " + message, post)
                    return EXIT_PUBLISH_FAILED
            if args.pull:
                code, out = git.run("pull", "--ff-only")
                if code != 0:
                    message = "`git pull --ff-only` falló; no sigo para no trabajar sobre un repo desactualizado: " + out.strip()[-300:]
                    print(message, file=sys.stderr)
                    alert("⚠️ ACCIÓN REQUERIDA: refresco del Data Map no corrió. " + message, post)
                    return EXIT_CANNOT_START

        try:
            clients = list_clients(clients_root, args.clients.split(",") if args.clients else None)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_CANNOT_START

        results: list[dict] = []
        for client in clients:
            summary = run_with_retries(client, retries=args.retries, sleep=sleep, gate=args.gate, dry_run=args.dry_run,
                                       timeout_seconds=args.timeout_minutes * 60, runner=runner)
            results.append(summary)
            print(f"[{client}] {summary.get('status')}", flush=True)

        publication = None
        promoted = [r for r in results if r.get("status") == "promovido"]
        if promoted and top is not None and not args.dry_run:
            pending = publishable_paths(git, top, clients_root, project_root)
            ours = {name: paths for name, paths in pending.items() if name in {r["client_id"] for r in promoted}}
            notes = {r["client_id"]: f"V{r.get('version_anterior')} → V{r.get('version_nueva')}" for r in promoted if r.get("version_nueva") is not None}
            publication = publish(git, ours, push=args.push, notes=notes)

        report = build_report(results, started, datetime.now(timezone.utc), publication)
        markdown = render_markdown(report)
        report_dir.mkdir(parents=True, exist_ok=True)
        stem = started.strftime("%Y-%m-%d")
        (report_dir / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (report_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
        print(markdown, flush=True)
        notify(report, markdown, post)

        if publication and publication.get("error") and args.push:   # un error real de git; 'nada para publicar' no lo es
            return EXIT_PUBLISH_FAILED
        return EXIT_ATTENTION if (report["needs_human"] or report["errors"]) else EXIT_OK
    finally:
        release_lock(lock_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresco diario del Data Map para una VM (ver el docstring del módulo).")
    parser.add_argument("--clients", default="", help="Lista separada por comas (por defecto, todos los de 2. clientes/).")
    parser.add_argument("--no-pull", dest="pull", action="store_false", help="No hacer git pull al empezar.")
    parser.add_argument("--no-push", dest="push", action="store_false", help="Commitear lo promovido pero no hacer push.")
    parser.add_argument("--gate", choices=("v2", "legacy"), default="v2")
    parser.add_argument("--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_MINUTES, help="Límite por cliente.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Reintentos ante fallas de proceso (no ante rechazos del gate).")
    parser.add_argument("--dry-run", action="store_true", help="Genera candidatas sin gate ni promoción ni publicación.")
    parser.add_argument("--test-notify", action="store_true", help="Manda un mensaje de prueba al webhook (VI_NOTIFY_WEBHOOK) y termina.")
    return parser.parse_args(argv)


def load_env() -> None:
    """Carga el `.env` de la raíz del repo (sin pisar variables ya exportadas): ahí viven VI_NOTIFY_WEBHOOK y VI_NOTIFY_HEARTBEAT.
    Los procesos de cada cliente lo cargan solos (vi_agent.load_environment); este envoltorio también lo necesita para avisar."""
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT.parents[1] / ".env", override=False)
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except Exception:  # noqa: BLE001 - sin python-dotenv o sin .env, rigen las variables del entorno
        pass


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    load_env()
    args = parse_args()
    if args.test_notify:
        sent = alert("✅ Prueba del refresco diario del Data Map: si ves este mensaje, el aviso por Slack está bien configurado.")
        print("Mensaje de prueba enviado." if sent else "NO se pudo enviar: falta VI_NOTIFY_WEBHOOK o el webhook rechazó el mensaje.", file=sys.stderr)
        sys.exit(0 if sent else EXIT_CANNOT_START)
    sys.exit(run_daily(args))


if __name__ == "__main__":
    main()
