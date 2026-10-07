"""Volver atrás el Data Map de un cliente a una versión anterior (2026-10-07).

Listar lo que hay y lo que pasó:
    python "4. scripts/revert_data_map.py" --client tigo_alto --list
Volver a la versión anterior a la actual (la que había antes de la última promoción registrada), o a una puntual:
    python "4. scripts/revert_data_map.py" --client tigo_alto [--to 3] --reason "las cifras de X dejaron de coincidir" [--publish]

Qué hace: apunta `config.yaml` al archivo de esa versión (NO borra ninguna versión: todas quedan en `data_map/`), deja la versión activa del almacén
(`data_map_store`) alineada y agrega una entrada `revertido` al registro `CAMBIOS_AUTOMATICOS.md/.jsonl`. Con `--publish` hace además un commit
con esos tres archivos y push (el mismo mecanismo del refresco diario). Sin `--publish`, queda en el repo local para revisar y commitear a mano.

Lo que NO hace: no vuelve a regenerar. El refresco diario sólo regenera cuando el prompt de Langfuse cambia de nuevo (la versión que motivó la
promoción revertida ya figura como procesada), y la próxima candidata se numera después de la versión MÁS ALTA que exista, así que no pisa nada.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

import data_map_log
import data_map_store as dms

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
DATA_MAP_LINE = re.compile(r'^data_map:\s*"(.*)"[ \t]*$', re.MULTILINE)


def list_versions(clients_root: Path, client: str) -> list[tuple[int, Path]]:
    folder = clients_root / client / "data_map"
    found = [(dms.version_number(p.name), p) for p in folder.glob("*.yaml")] if folder.is_dir() else []
    return sorted((v, p) for v, p in found if v is not None)


def current_version(clients_root: Path, client: str, project_root: Path) -> tuple[int, Path]:
    text = (clients_root / client / "config.yaml").read_text(encoding="utf-8")
    match = DATA_MAP_LINE.search(text)
    if not match:
        raise ValueError(f"No encontré la línea data_map: en el config.yaml de {client}.")
    path = (project_root / match.group(1)).resolve()
    version = dms.version_number(path.name)
    if version is None:
        raise ValueError(f"No pude inferir la versión de {path.name}.")
    return version, path


def default_target(entries: list[dict], current: int, available: list[int]) -> int | None:
    """La versión anterior a la actual según el registro (la última promoción que dejó la actual); si no, la más alta menor que la actual."""
    for entry in reversed(entries):
        if entry.get("tipo") == "promovido" and entry.get("version_nueva") == current and entry.get("version_anterior") is not None:
            return int(entry["version_anterior"])
    older = [v for v in available if v < current]
    return max(older) if older else None


def revert(client: str, *, to_version: int | None = None, reason: str = "", clients_root: Path = CLIENTS_ROOT,
           project_root: Path = PROJECT_ROOT, store=None) -> dict:
    store = store or dms.LocalStore(project_root=project_root, clients_root=clients_root)
    current, current_path = current_version(clients_root, client, project_root)
    versions = dict(list_versions(clients_root, client))
    target = to_version if to_version is not None else default_target(data_map_log.read_entries(clients_root, client), current, sorted(versions))
    if target is None:
        raise ValueError("No hay una versión anterior a la que volver.")
    if target == current:
        raise ValueError(f"V{target} ya es la versión vigente.")
    if target not in versions:
        raise ValueError(f"No existe el archivo de la V{target}. Versiones disponibles: " + ", ".join(f"V{v}" for v in sorted(versions)))
    target_path = versions[target]
    parsed = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or "sources" not in parsed:
        raise ValueError(f"El archivo de la V{target} no es un Data Map válido.")

    config = clients_root / client / "config.yaml"
    relative = target_path.resolve().relative_to(project_root.resolve()).as_posix()
    text = config.read_text(encoding="utf-8")
    new_text = DATA_MAP_LINE.sub(f'data_map: "{relative}"', text, count=1)
    if new_text == text:
        raise ValueError("No se pudo actualizar la línea data_map: de config.yaml.")
    config.write_text(new_text, encoding="utf-8")
    store.promote(client, target_path, changelog=f"revertido a V{target}: {reason}".strip(": "))

    current_dm = yaml.safe_load(current_path.read_text(encoding="utf-8")) or {}
    entry = data_map_log.build_entry(
        tipo="revertido", client=client, old_version=current, new_version=target, old_file=current_path.name, new_file=target_path.name,
        change_summary=data_map_log.summarize_change(current_dm, parsed), reason=reason or "reversión manual")
    md_path, jsonl_path = data_map_log.append_entry(clients_root, client, entry)
    return {"client": client, "from": current, "to": target, "config": config, "log_md": md_path, "log_jsonl": jsonl_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="Volver atrás el Data Map de un cliente (ver el docstring del módulo).")
    parser.add_argument("--client", required=True, help="Carpeta del cliente bajo 2. clientes/.")
    parser.add_argument("--list", action="store_true", help="Muestra versiones disponibles y el registro de cambios, y no cambia nada.")
    parser.add_argument("--to", type=int, default=None, help="Número de versión destino (por defecto, la anterior a la actual).")
    parser.add_argument("--reason", default="", help="Por qué se revierte (queda en el registro).")
    parser.add_argument("--publish", action="store_true", help="Commit con config.yaml y el registro, y push.")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    if args.list:
        current, _ = current_version(CLIENTS_ROOT, args.client, PROJECT_ROOT)
        print(f"Versión vigente de {args.client}: V{current}")
        for version, path in list_versions(CLIENTS_ROOT, args.client):
            print(f"  V{version}{'  <- vigente' if version == current else ''}: {path.name}")
        for entry in data_map_log.read_entries(CLIENTS_ROOT, args.client)[-10:]:
            print(f"  {entry['fecha']} {entry['tipo']}: V{entry.get('version_anterior')} -> V{entry.get('version_nueva')} {entry.get('motivo') or ''}")
        return
    try:
        result = revert(args.client, to_version=args.to, reason=args.reason)
    except ValueError as exc:
        print(f"No se revirtió nada: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"{args.client}: V{result['from']} -> V{result['to']} (config.yaml y registro actualizados).")
    if args.publish:
        import run_daily_refresh as rdr

        git = rdr.Git(PROJECT_ROOT)
        top = git.toplevel()
        if top is None:
            print("No estoy dentro de un repo de git: no se publicó.", file=sys.stderr)
            sys.exit(3)
        paths = [rdr._relative_to_git(Path(result[key]), top) for key in ("config", "log_md", "log_jsonl")]
        outcome = rdr.publish(git, {args.client: paths}, push=True,
                              message=f"Data Map de {args.client} revertido a V{result['to']} (manual)\n\n{args.reason}".strip())
        print("Publicado." if outcome.get("published") else f"No se pudo publicar: {outcome.get('error') or outcome.get('reason')}")
        sys.exit(0 if outcome.get("published") else 3)
    print("Para publicarlo: revisá el diff y hacé commit y push, o repetí con --publish.")


if __name__ == "__main__":
    main()
