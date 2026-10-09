"""Almacenamiento del estado de la actualización automática del Data Map (2026-10-07).

Hasta ahora ese estado vivía en archivos sueltos de `.runtime/` y en la línea `data_map:` de cada `config.yaml`, o sea en el disco de
la compu que corría el poller. Eso no sirve cuando la app corre en varias instancias o en un servidor que se reinicia. Este módulo lo
junta detrás de una interfaz chica para poder cambiar DÓNDE se guarda (disco local hoy; un bucket, git o una VM después) sin tocar la
lógica de la actualización.

Qué guarda, por cliente (la carpeta bajo `2. clientes/`):
- la VERSIÓN ACTIVA del Data Map (`active`): cuál rige, desde cuándo y con qué changelog; más un historial para volver atrás;
- las versiones de los prompts de Langfuse YA PROCESADAS (`processed_versions`): lo que permite saber si algo cambió sin importar en
  qué máquina se mire. Sólo avanza cuando el cambio se promovió, así que un cambio rechazado por el gate se REINTENTA solo (antes la foto
  se actualizaba apenas se detectaba el cambio y un rechazo quedaba olvidado hasta que un humano volvía a correr el script);
- el cambio PENDIENTE y los INTENTOS por versión (para no reintentar para siempre y gastar Gemini en vano);
- un CANDADO con vencimiento (que no corran dos actualizaciones del mismo cliente a la vez);
- la hora del último chequeo (para no consultar Langfuse en cada consulta de cada usuario);
- el registro auditable de cada corrida.

Backend: variable `VI_STORE`. Vacía o `local` = `LocalStore` (disco, mismo comportamiento que antes). Cualquier otro valor (por ejemplo
`gcs://bucket/prefijo`) todavía no está implementado y falla con un mensaje claro.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
STORE_ENV = "VI_STORE"

VERSION_RE = re.compile(r"\s+V(\d+)\.yaml$", re.IGNORECASE)
_SAFE_CLIENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def version_number(name: str | Path) -> int | None:
    """Número de versión de un nombre como 'VI Data Map Farma24 V10.yaml' (None si no se puede inferir)."""
    match = VERSION_RE.search(Path(str(name)).name)
    return int(match.group(1)) if match else None


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(), timezone.utc).isoformat()


class LocalStore:
    """Estado en disco: `<root>/store/<cliente>/*.json` y `<root>/data_map_updates/<cliente>/<fecha>.json`.

    Las escrituras son atómicas (archivo temporal + `os.replace`) y el candado usa creación exclusiva (`O_CREAT | O_EXCL`), que es lo que
    hace de exclusión mutua entre procesos de la misma máquina."""

    backend = "local"

    def __init__(self, root: Path | None = None, clients_root: Path | None = None, project_root: Path | None = None) -> None:
        self.project_root = Path(project_root) if project_root else PROJECT_ROOT
        self.root = Path(root) if root else self.project_root / ".runtime"
        self.clients_root = Path(clients_root) if clients_root else self.project_root / "2. clientes"

    # ------------------------------------------------------------------ archivos
    def _dir(self, client: str, create: bool = True) -> Path:
        if not _SAFE_CLIENT.match(client or ""):
            raise ValueError(f"Nombre de cliente inválido: {client!r}")
        path = self.root / "store" / client
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _read(self, client: str, name: str, default: Any = None) -> Any:
        path = self._dir(client, create=False) / name  # leer no crea carpetas (se consulta en cada configure_client)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _write(self, client: str, name: str, payload: Any) -> None:
        directory = self._dir(client)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, directory / name)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ Data Map activo
    def active(self, client: str) -> dict | None:
        """{'data_map': ruta relativa al proyecto, 'version': n, 'promoted_at', 'changelog'} o None si rige el de config.yaml."""
        value = self._read(client, "active.json")
        return value if isinstance(value, dict) and value.get("data_map") else None

    def resolve_data_map(self, client: str, relative: str) -> Path:
        """Ruta local de un Data Map guardado (en disco, la misma del repo). Nunca sale del proyecto."""
        path = (self.project_root / relative).resolve()
        if not path.is_relative_to(self.project_root.resolve()):
            raise ValueError("El Data Map activo debe vivir dentro del proyecto.")
        return path

    def put_data_map(self, client: str, name: str, text: str) -> Path:
        """Guarda una versión candidata junto a las demás del cliente y devuelve su ruta local."""
        if "/" in name or "\\" in name or not name.lower().endswith(".yaml"):
            raise ValueError(f"Nombre de Data Map inválido: {name!r}")
        folder = self.clients_root / client / "data_map"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def promote(self, client: str, data_map: str | Path, *, rules_versions: dict[str, int] | None = None,
                changelog: str = "") -> dict:
        """Hace activa una versión. `data_map`: ruta relativa al proyecto (o absoluta dentro de él)."""
        relative = Path(data_map)
        if relative.is_absolute():
            relative = relative.resolve().relative_to(self.project_root.resolve())
        record = {"data_map": relative.as_posix(), "version": version_number(relative.name),
                  "promoted_at": _iso(), "changelog": changelog}
        previous = self.active(client)
        history = self._read(client, "history.json", [])
        if previous:
            history.append(previous)
        self._write(client, "history.json", history[-50:])
        self._write(client, "active.json", record)
        if rules_versions:
            self.set_processed(client, rules_versions)
        self.clear_pending(client)
        return record

    def rollback(self, client: str) -> dict | None:
        """Vuelve a la versión activa anterior; sin historial, borra el puntero y rige de nuevo el de config.yaml."""
        history = self._read(client, "history.json", [])
        if history:
            previous = history.pop()
            self._write(client, "history.json", history)
            self._write(client, "active.json", previous)
            return previous
        try:
            (self._dir(client) / "active.json").unlink()
        except FileNotFoundError:
            pass
        return None

    # ------------------------------------------------------------------ prompts procesados, pendiente e intentos
    def processed_versions(self, client: str) -> dict[str, Any]:
        value = self._read(client, "processed.json", {})
        return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}   # int, o "local" en rulebooks del repo

    def set_processed(self, client: str, versions: dict[str, Any]) -> None:
        merged = self.processed_versions(client)
        merged.update({str(k): v for k, v in versions.items()})
        self._write(client, "processed.json", merged)

    def pending(self, client: str) -> dict:
        value = self._read(client, "pending.json", {})
        return value if isinstance(value, dict) else {}

    def put_pending(self, client: str, pending: dict) -> None:
        self._write(client, "pending.json", pending)

    def clear_pending(self, client: str) -> None:
        for name in ("pending.json", "attempts.json"):
            try:
                (self._dir(client) / name).unlink()
            except FileNotFoundError:
                pass

    def attempts(self, client: str, signature: str) -> int:
        value = self._read(client, "attempts.json", {})
        return int(value.get(signature, 0)) if isinstance(value, dict) else 0

    def add_attempt(self, client: str, signature: str) -> int:
        value = self._read(client, "attempts.json", {})
        value = value if isinstance(value, dict) else {}
        value[signature] = int(value.get(signature, 0)) + 1
        self._write(client, "attempts.json", value)
        return value[signature]

    def refund_attempt(self, client: str, signature: str) -> int:
        """Devuelve un intento: una caída de Gemini, de la base o de la red no es un rechazo del candidato y no debe gastar los 4 intentos."""
        value = self._read(client, "attempts.json", {})
        value = value if isinstance(value, dict) else {}
        value[signature] = max(0, int(value.get(signature, 0)) - 1)
        self._write(client, "attempts.json", value)
        return value[signature]

    def infra_failures(self, client: str) -> dict:
        """{"count": días seguidos con caída de infraestructura, "last_date": 'AAAA-MM-DD'}."""
        value = self._read(client, "infra_failures.json", {})
        return dict(value) if isinstance(value, dict) else {}

    def set_infra_failures(self, client: str, value: dict) -> None:
        if value or self._read(client, "infra_failures.json", None) is not None:
            self._write(client, "infra_failures.json", value)

    # ------------------------------------------------------------------ candado y último chequeo
    def acquire_lock(self, client: str, owner: str, ttl_seconds: float) -> bool:
        """True si este `owner` quedó con el candado. Un candado vencido se pisa."""
        path = self._dir(client) / "lock.json"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = self.lock_info(client)
                if info and float(info.get("expires_at", 0)) > _now():
                    return False
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"owner": owner, "acquired_at": _now(), "expires_at": _now() + float(ttl_seconds)}, handle)
            return True
        return False

    def lock_info(self, client: str) -> dict | None:
        value = self._read(client, "lock.json")
        return value if isinstance(value, dict) else None

    def release_lock(self, client: str, owner: str) -> bool:
        info = self.lock_info(client)
        if info is not None and info.get("owner") != owner:
            return False
        try:
            (self._dir(client) / "lock.json").unlink()
        except FileNotFoundError:
            pass
        return True

    # ------------------------------------------------------------------ respuestas del Data Map vigente en el gate (caché)
    def gate_baseline(self, client: str, map_hash: str) -> dict:
        """{id_pregunta: [números que el Data Map vigente no menciona]} ya medido para ESE texto de Data Map (si cambió, vacío)."""
        saved = self._read(client, "gate_baseline.json", {}) or {}
        return dict(saved.get("missing") or {}) if saved.get("map_hash") == map_hash else {}

    def set_gate_baseline(self, client: str, map_hash: str, missing: dict) -> None:
        self._write(client, "gate_baseline.json", {"map_hash": map_hash, "missing": missing})

    # ------------------------------------------------------------------ prompts que no se pudieron leer (corridas seguidas)
    def unavailable(self, client: str) -> dict:
        return dict(self._read(client, "unavailable.json", {}) or {})

    def set_unavailable(self, client: str, counts: dict) -> None:
        if counts or self._read(client, "unavailable.json", None) is not None:
            self._write(client, "unavailable.json", counts)

    def last_check(self, client: str) -> float | None:
        value = self._read(client, "meta.json", {})
        ts = value.get("last_check") if isinstance(value, dict) else None
        return float(ts) if ts is not None else None

    def set_last_check(self, client: str, ts: float | None = None) -> None:
        value = self._read(client, "meta.json", {})
        value = value if isinstance(value, dict) else {}
        value["last_check"] = float(ts if ts is not None else _now())
        self._write(client, "meta.json", value)

    # ------------------------------------------------------------------ registro de corridas
    def write_run(self, client: str, payload: dict) -> Path:
        directory = self.root / "data_map_updates" / client
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{stamp}.json"
        counter = 1
        while path.exists():  # dos corridas en el mismo segundo no se pisan
            path = directory / f"{stamp}_{counter}.json"
            counter += 1
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path


def get_store() -> LocalStore:
    backend = (os.environ.get(STORE_ENV) or "").strip()
    if backend in ("", "local"):
        return LocalStore()
    raise ValueError(f"Backend de almacenamiento no implementado todavía: {backend!r} (por ahora sólo 'local').")


def new_owner(prefix: str = "proc") -> str:
    return f"{prefix}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def effective_data_map_path(client: str, config_path: Path, store: LocalStore | None = None) -> Path:
    """Ruta del Data Map que debe usar el agente: el activo del almacén si es MÁS NUEVO que el de config.yaml, si no el de config.yaml.
    Fail-open: ante cualquier problema con el almacén se usa el de config.yaml (nunca tumba al agente)."""
    try:
        store = store or get_store()
        active = store.active(client)
        if not active:
            return config_path
        active_version, config_version = active.get("version"), version_number(config_path)
        if active_version is None or config_version is None or int(active_version) <= config_version:
            return config_path
        candidate = store.resolve_data_map(client, str(active["data_map"]))
        return candidate if candidate.is_file() else config_path
    except Exception:  # noqa: BLE001
        return config_path
