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
sucio, pull) · 3 se promovió pero falló la publicación · 4 error inesperado del propio script (avisa por webhook).

Uso (desde cualquier carpeta):  python "<repo>/experimental_proyects/Vera Intelligence/4. scripts/run_daily_refresh.py"
Opciones: --parallel 4 · --clients a,b · --no-pull · --no-push · --gate v2|legacy · --timeout-minutes 60 · --retries 2 · --dry-run
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
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import data_map_log
import data_map_store as dms

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
RUNTIME = PROJECT_ROOT / ".runtime"
REPORT_DIR = RUNTIME / "daily_refresh"
LOCK_PATH = RUNTIME / "daily_refresh.lock"
UPDATER = SCRIPT_DIR / "data_map_auto_update.py"

LOCK_STALE_SECONDS = 12 * 3600      # peor caso: 19 clientes en tandas de 4 con el tope por cliente
DEFAULT_TIMEOUT_MINUTES = 90     # límite por cliente (el 2026-10-09 se subió de 15 a 90): uno colgado no puede frenar al resto para siempre
DEFAULT_PARALLEL = 4            # clientes corriendo a la vez (cada uno en su propio proceso); se cambia con --parallel o VI_REFRESH_PARALLEL en el .env
DEFAULT_WAIT_ETL_MINUTES = 10   # cuánto esperar a que la ETL termine sus REFRESH MATERIALIZED VIEW antes de arrancar (0 = no esperar); VI_REFRESH_WAIT_ETL_MINUTES
ETL_POLL_SECONDS = 60
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 30
NEEDS_HUMAN = {"gate_fallo_no_promovido", "error_regeneracion", "deriva_de_columnas_no_promovido", "reintentos_agotados",
               "prompt_no_disponible"}


def needs_human(item: dict) -> bool:
    """Un cliente pide revisión por su estado o porque un prompt de sus rulebooks no se puede leer (aunque haya otro cambio promovido)."""
    return (item.get("status") in NEEDS_HUMAN or item.get("needs_human") is True
            or any(p.get("needs_human") for p in item.get("rulebook_problems") or []))
PROCESS_ERROR = "error_de_proceso"
DATA_MAP_LINE = re.compile(r'^data_map:\s*"(.*)"[ \t]*$', re.MULTILINE)

EXIT_OK, EXIT_ATTENTION, EXIT_CANNOT_START, EXIT_PUBLISH_FAILED, EXIT_CRASH = 0, 1, 2, 3, 4


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


def last_error_line(text: str) -> str:
    """La línea de la excepción de un traceback (la última que menciona Error/Exception) o, si no hay, la última línea. El aviso de Slack
    mostraba el INICIO del texto (`File ... line 173, in raise_for_response`) y no la causa, que está al final."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if "Error" in line or "Exception" in line:
            return line[:400]
    return lines[-1][:400] if lines else ""


def refund_after_timeout(client: str, store=None) -> bool:
    """El proceso del cliente fue matado por el tope de tiempo: el límite es nuestro, no un rechazo del candidato. Devuelve el intento y suelta el candado."""
    try:
        return bool((store or dms.get_store()).refund_inflight(client))
    except Exception:  # noqa: BLE001 -nunca debe romper el informe del día
        return False


def run_client_process(client: str, *, gate: str, dry_run: bool, timeout_seconds: float,
                       runner: Callable[..., subprocess.CompletedProcess] | None = None) -> dict:
    cmd = [sys.executable, str(UPDATER), "--client", client, "--gate", gate] + (["--dry-run"] if dry_run else [])
    try:
        proc = (runner or subprocess.run)(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                          timeout=timeout_seconds, cwd=str(SCRIPT_DIR))
    except subprocess.TimeoutExpired:
        # `timeout: True`: NO se reintenta (rehacer una regeneración que ya agotó el límite no suele cambiar nada y repite el gasto). El cambio de prompt
        # sigue pendiente y se retoma en la corrida siguiente.
        refunded = refund_after_timeout(client)
        return {"client_id": client, "status": PROCESS_ERROR, "timeout": True, "attempt_refunded": refunded,
                "error": f"timeout de {timeout_seconds / 60:.0f} min" + (" (el intento se devolvió)" if refunded else "")}
    summary = parse_summary(proc.stdout or "")
    if summary is None or proc.returncode not in (0, 1):
        full = ((proc.stderr or "") + (proc.stdout or "")).strip()
        return {"client_id": client, "status": PROCESS_ERROR,
                "error": f"código {proc.returncode}: {last_error_line(full)} || final del log: {full[-500:]}"}
    summary.setdefault("client_id", client)
    return summary


def run_with_retries(client: str, *, retries: int, sleep: Callable[[float], None] = time.sleep, **kwargs) -> dict:
    """Reintenta SÓLO fallas de proceso (red, timeout, caída de la base): un `gate_fallo` ya tiene sus reintentos propios."""
    summary: dict = {}
    for attempt in range(retries + 1):
        summary = run_client_process(client, **kwargs)
        transient_infra = summary.get("status") == "infraestructura_no_disponible" and not summary.get("needs_human")
        if (summary.get("status") != PROCESS_ERROR and not transient_infra) or summary.get("timeout"):
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
        "needs_human": sorted(i["client_id"] for i in results if needs_human(i)),
        "errors": sorted(i["client_id"] for i in results if i.get("status") == PROCESS_ERROR),
        "promoted": sorted(i["client_id"] for i in results if i.get("status") == "promovido"),
        "publication": publication, "results": results,
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "slowest": sorted(({"client_id": i["client_id"], "seconds": i["seconds"]} for i in results if i.get("seconds") is not None),
                          key=lambda item: -item["seconds"])[:3],
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    if report["needs_human"] or report["errors"] or (report["publication"] or {}).get("error"):
        who = ", ".join(report["needs_human"] + report["errors"]) or "publicación"
        lines.append(f"⚠️ ACCIÓN REQUERIDA: {who}")
    lines.append(f"Refresco del {report['date']}: {report['clients_run']} clientes · " +
                 ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
    if report.get("duration_seconds") is not None:
        slow = report.get("slowest") or []
        lines.append(f"Duración: {report['duration_seconds'] / 60:.1f} min" +
                     (f" · más lento: {slow[0]['client_id']} ({slow[0]['seconds'] / 60:.1f} min)" if slow and slow[0]["seconds"] >= 120 else ""))
    lines.extend(report.get("notes") or [])
    if report["promoted"]:
        lines.append("Promovidos: " + ", ".join(report["promoted"]))
    publication = report.get("publication")
    if publication:
        lines.append("Publicación: " + ("publicado" if publication.get("published") else
                                          f"NO publicado ({publication.get('error') or publication.get('reason')})"))
    for item in report["results"]:
        if needs_human(item) or item.get("status") == PROCESS_ERROR:
            detail = item.get("error") or item.get("note") or item.get("candidate_path") or ""
            for problem in item.get("rulebook_problems") or []:
                if problem.get("needs_human"):
                    detail = f"{detail} | prompt no disponible: {problem.get('prompt')} (HTTP {problem.get('http_status')})".strip(" |")
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
              lock_path: Path = LOCK_PATH, preflight_fn: Callable[[], list[str]] | None = None,
              etl_busy_fn: Callable[[], bool | None] | None = None) -> int:
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

        if args.preflight and (preflight_fn is not None or runner is None):
            problems = (preflight_fn or preflight)()
            if problems:
                message = "No arranco: " + " | ".join(problems)
                print(message, file=sys.stderr)
                alert("⚠️ ACCIÓN REQUERIDA: refresco del Data Map no corrió. " + message, post)
                return EXIT_CANNOT_START

        run_notes: list[str] = []
        parallel = int(args.parallel if args.parallel is not None else env_number("VI_REFRESH_PARALLEL", DEFAULT_PARALLEL))
        wait_minutes = args.wait_etl_minutes if args.wait_etl_minutes is not None else env_number("VI_REFRESH_WAIT_ETL_MINUTES", DEFAULT_WAIT_ETL_MINUTES)
        if wait_minutes > 0 and (etl_busy_fn is not None or runner is None):
            etl = wait_for_etl(etl_busy_fn or etl_refreshing, sleep, wait_minutes)
            if etl["waited_minutes"] > 0 or etl["still_busy"]:
                run_notes.append(f"La ETL estaba refrescando vistas: esperé {etl['waited_minutes']:g} min" +
                             ("; seguía ocupada, así que corrí de a un cliente a la vez." if etl["still_busy"] else " y arranqué cuando terminó."))
            if etl["still_busy"]:
                parallel = 1

        try:
            clients = list_clients(clients_root, args.clients.split(",") if args.clients else None)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_CANNOT_START

        def run_one(client: str) -> dict:
            started_client = time.time()
            summary = run_with_retries(client, retries=args.retries, sleep=sleep, gate=args.gate, dry_run=args.dry_run,
                                       timeout_seconds=args.timeout_minutes * 60, runner=runner)
            summary["seconds"] = round(time.time() - started_client, 1)
            print(f"[{client}] {summary.get('status')} ({summary['seconds']:.0f} s)", flush=True)
            return summary

        # Varios clientes a la vez (procesos independientes: cada uno guarda su estado en su propia carpeta). El resultado conserva el orden de la lista.
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            results: list[dict] = list(pool.map(run_one, clients))

        publication = None
        promoted = [r for r in results if r.get("status") == "promovido"]
        if promoted and top is not None and not args.dry_run:
            pending = publishable_paths(git, top, clients_root, project_root)
            ours = {name: paths for name, paths in pending.items() if name in {r["client_id"] for r in promoted}}
            notes = {r["client_id"]: f"V{r.get('version_anterior')} → V{r.get('version_nueva')}" for r in promoted if r.get("version_nueva") is not None}
            publication = publish(git, ours, push=args.push, notes=notes)

        report = build_report(results, started, datetime.now(timezone.utc), publication)
        report["notes"] = run_notes + ([f"Clientes a la vez: {parallel}"] if parallel != DEFAULT_PARALLEL else [])
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
    parser.add_argument("--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_MINUTES, help="Límite por cliente (un timeout no se reintenta).")
    parser.add_argument("--parallel", type=int, default=None,
                        help=f"Cuántos clientes se procesan a la vez (por defecto VI_REFRESH_PARALLEL del .env, o {DEFAULT_PARALLEL}).")
    parser.add_argument("--wait-etl-minutes", type=float, default=None,
                        help=f"Minutos máximos de espera a que la ETL termine de refrescar vistas antes de arrancar (por defecto "
                             f"VI_REFRESH_WAIT_ETL_MINUTES o {DEFAULT_WAIT_ETL_MINUTES}; 0 = no esperar).")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Reintentos ante fallas de proceso (no ante rechazos del gate).")
    parser.add_argument("--dry-run", action="store_true", help="Genera candidatas sin gate ni promoción ni publicación.")
    parser.add_argument("--no-preflight", dest="preflight", action="store_false",
                        help="No verificar antes de empezar que el .env, Postgres, Langfuse y Gemini respondan.")
    parser.add_argument("--test-notify", action="store_true", help="Manda un mensaje de prueba al webhook (VI_NOTIFY_WEBHOOK) y termina.")
    return parser.parse_args(argv)


def env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def etl_refreshing() -> bool | None:
    """¿La ETL está refrescando vistas materializadas ahora? None si no se pudo consultar (en ese caso no se frena nada)."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT.parents[1]))
        from utils.postgres import get_postgres_connection

        with get_postgres_connection() as connection:
            row = connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid() "
                "AND query ILIKE 'refresh materialized view%'").fetchone()
        return bool(row and row[0] > 0)
    except Exception:  # noqa: BLE001 -si no se puede mirar, se sigue: esto es cortesía, no un requisito
        return None


def wait_for_etl(busy_fn: Callable[[], bool | None], sleep: Callable[[float], None], max_minutes: float) -> dict:
    """Espera (hasta `max_minutes`) a que la ETL termine de refrescar vistas. Devuelve {"waited_minutes", "still_busy"}."""
    waited = 0.0
    busy = busy_fn()
    while busy and waited < max_minutes * 60:
        sleep(ETL_POLL_SECONDS)
        waited += ETL_POLL_SECONDS
        busy = busy_fn()
    return {"waited_minutes": round(waited / 60, 1), "still_busy": bool(busy)}


REQUIRED_ENV = ("VERA_AI_API_KEY", "PGPASSWORD", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")


def preflight() -> list[str]:
    """Verifica lo mínimo para que valga la pena arrancar (variables del .env, Postgres, Langfuse y Gemini) y devuelve la lista de problemas.
    Sin esto, una clave vencida o una caída de red hacía fallar los 19 clientes uno por uno, con mensajes confusos y horas perdidas; ahora es UN aviso
    claro antes de empezar. Cada chequeo se repite una vez tras una espera corta, para no cortar el día por un parpadeo."""
    problems: list[str] = []
    missing = [name for name in REQUIRED_ENV if not (os.environ.get(name) or "").strip()]
    if missing:
        return ["Faltan variables en el .env: " + ", ".join(missing)]

    def check_postgres() -> None:
        sys.path.insert(0, str(PROJECT_ROOT.parents[1]))
        from utils.postgres import get_postgres_connection

        with get_postgres_connection() as connection:
            connection.execute("SELECT 1")

    def check_langfuse() -> None:
        import requests

        base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
        requests.get(base + "/api/public/health", timeout=15).raise_for_status()

    def check_gemini() -> None:
        from google import genai

        client = genai.Client(api_key=os.environ["VERA_AI_API_KEY"])
        next(iter(client.models.list(config={"page_size": 1})), None)

    for name, check in (("Postgres", check_postgres), ("Langfuse", check_langfuse), ("Gemini", check_gemini)):
        error: Exception | None = None
        for attempt in range(2):
            try:
                check()
                error = None
                break
            except Exception as exc:  # noqa: BLE001 -cualquier falla acá significa "no se puede trabajar"
                error = exc
                if attempt == 0:
                    time.sleep(20)
        if error is not None:
            problems.append(f"{name} no responde: {type(error).__name__}: {str(error)[:200]}")
    return problems


def run_guarded(args: argparse.Namespace, post: Callable[[str, dict], None] | None = None) -> int:
    """`run_daily` + red de seguridad: un error inesperado (un bug, un archivo ilegible) deja el traceback en el log, AVISA por webhook
    y sale con código 4, en vez de morir en silencio (sin esto, el latido diario faltaría pero nadie sabría por qué)."""
    try:
        return run_daily(args)
    except Exception as exc:  # noqa: BLE001 -justamente para no perder ningún error
        traceback.print_exc()
        alert(f"⚠️ ACCIÓN REQUERIDA: el refresco del Data Map falló con un error inesperado ({type(exc).__name__}: {str(exc)[:300]}). "
              "Mirar el log de la VM.", post)
        return EXIT_CRASH


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
    sys.exit(run_guarded(args))


if __name__ == "__main__":
    main()
