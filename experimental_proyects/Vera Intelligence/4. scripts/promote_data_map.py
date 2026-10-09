"""Promover a mano un Data Map candidato que el gate rechazó (o que nunca se promovió) y que una persona revisó y considera bueno (2026-10-09).

Ver qué hay y cuál es la vigente:
    python "4. scripts/promote_data_map.py" --client maga_alto --list
Promover el candidato más nuevo (o uno puntual) con una razón obligatoria:
    python "4. scripts/promote_data_map.py" --client maga_alto --reason "revisé el diff: solo cambia la descripción de X" [--version 4 | --file "VI Data Map Maga V4.yaml"] [--publish]

Qué verifica (sin Gemini): que el candidato conserve las claves y fuentes del vigente (estructura) y que todos sus campos existan en las vistas reales de
Postgres (deriva de columnas). Si alguna falla, NO promueve. `--skip-drift-check` omite solo la consulta a Postgres (por ejemplo, sin red).
Qué hace: apunta `config.yaml` al candidato, deja activa esa versión en el almacén, marca como procesados los prompts que estaban pendientes (así el refresco
diario no vuelve a regenerar ese mismo cambio), y agrega una entrada `promovido_manual` al registro CAMBIOS_AUTOMATICOS con la razón y cómo revertir.
Con `--publish` hace además commit y push. No borra ni pisa ninguna versión.

Lo que NO hace: no corre el gate con Gemini (para eso es el refresco automático): promover a mano es una decisión de una persona y queda escrita.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Callable

import yaml

import data_map_gate
import data_map_log
import data_map_store as dms
from revert_data_map import DATA_MAP_LINE, current_version, list_versions

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"


def _real_drift_check(client: str, path: Path) -> list[dict]:
    from data_map_column_drift import check_client

    return check_client(client, path)


def promote_manual(client: str, *, reason: str, file: str | None = None, version: int | None = None,
                   clients_root: Path = CLIENTS_ROOT, project_root: Path = PROJECT_ROOT, store=None,
                   drift_check: Callable[[str, Path], list[dict]] | None = _real_drift_check) -> dict:
    if not (reason or "").strip():
        raise ValueError("Hace falta --reason: promover a mano queda en el registro y tiene que decir por qué.")
    store = store or dms.LocalStore(project_root=project_root, clients_root=clients_root)
    current, current_path = current_version(clients_root, client, project_root)
    versions = dict(list_versions(clients_root, client))
    folder = clients_root / client / "data_map"
    if file:
        path = folder / file
        if not path.is_file():
            raise ValueError(f"No existe el archivo {file} en {folder}.")
    elif version is not None:
        if version not in versions:
            raise ValueError(f"No existe la V{version}. Versiones disponibles: " + ", ".join(f"V{v}" for v in sorted(versions)))
        path = versions[version]
    else:
        newer = [v for v in versions if v > current]
        if not newer:
            raise ValueError(f"No hay ningún candidato más nuevo que la V{current} para promover.")
        path = versions[max(newer)]
    target = dms.version_number(path.name)
    if target == current:
        raise ValueError(f"V{target} ya es la versión vigente.")

    candidate = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict) or "sources" not in candidate:
        raise ValueError(f"{path.name} no es un Data Map válido.")
    old = yaml.safe_load(current_path.read_text(encoding="utf-8")) or {}
    problems = data_map_gate.structural_problems(old, candidate)
    if problems:
        raise ValueError("El candidato pierde estructura respecto del vigente: " + "; ".join(problems))
    checks = ["estructura"]
    if drift_check is not None:
        try:
            drift = drift_check(client, path)
        except Exception as exc:  # noqa: BLE001 -sin poder mirar la base no se promueve a ciegas
            raise ValueError(f"No pude verificar las columnas contra Postgres ({type(exc).__name__}: {str(exc)[:150]}). "
                             "Si estás seguro, repetí con --skip-drift-check.") from exc
        if drift:
            raise ValueError("El candidato declara campos que no existen en la base: " + str(drift)[:400])
        checks.append("columnas")

    config = clients_root / client / "config.yaml"
    relative = path.resolve().relative_to(project_root.resolve()).as_posix()
    text = config.read_text(encoding="utf-8")
    new_text = DATA_MAP_LINE.sub(f'data_map: "{relative}"', text, count=1)
    if new_text == text:
        raise ValueError("No se pudo actualizar la línea data_map: de config.yaml.")

    processed = dict(store.processed_versions(client))
    pending = store.pending(client)
    for key, info in pending.items():
        if isinstance(info, dict) and info.get("new_version") is not None:
            processed[key] = info["new_version"]
    config.write_text(new_text, encoding="utf-8")
    store.promote(client, path, rules_versions=processed, changelog=f"promovido a mano: {reason}")
    prompts = [{"key": key, "old_version": info.get("old_version"), "new_version": info.get("new_version")}
               for key, info in pending.items() if isinstance(info, dict)]
    entry = data_map_log.build_entry(
        tipo="promovido_manual", client=client, old_version=current, new_version=target, old_file=current_path.name, new_file=path.name,
        prompts=prompts, change_summary=data_map_log.summarize_change(old, candidate),
        changelog="Promoción manual, sin el gate con Gemini. Verificado: " + " y ".join(checks) + ".", reason=reason)
    md_path, jsonl_path = data_map_log.append_entry(clients_root, client, entry)
    return {"client": client, "from": current, "to": target, "config": config, "log_md": md_path, "log_jsonl": jsonl_path, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description="Promover a mano un Data Map candidato (ver el docstring del módulo).")
    parser.add_argument("--client", required=True, help="Carpeta del cliente bajo 2. clientes/.")
    parser.add_argument("--list", action="store_true", help="Muestra la versión vigente, los candidatos más nuevos y el registro, y no cambia nada.")
    parser.add_argument("--version", type=int, default=None, help="Número de versión a promover (por defecto, el candidato más nuevo).")
    parser.add_argument("--file", default=None, help="Nombre del archivo a promover dentro de data_map/.")
    parser.add_argument("--reason", default="", help="Por qué se promueve a mano (obligatorio; queda en el registro).")
    parser.add_argument("--skip-drift-check", action="store_true", help="No verificar las columnas contra Postgres.")
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
            tag = "  <- vigente" if version == current else ("  (candidato más nuevo que la vigente)" if version > current else "")
            print(f"  V{version}: {path.name}{tag}")
        for entry in data_map_log.read_entries(CLIENTS_ROOT, args.client)[-8:]:
            print(f"  {entry['fecha']} {entry['tipo']}: V{entry.get('version_anterior')} -> V{entry.get('version_nueva')} {entry.get('motivo') or ''}")
        return
    if not args.skip_drift_check:
        try:
            from dotenv import load_dotenv

            load_dotenv(PROJECT_ROOT.parents[1] / ".env", override=False)
        except Exception:  # noqa: BLE001
            pass
    try:
        result = promote_manual(args.client, reason=args.reason, file=args.file, version=args.version,
                                drift_check=None if args.skip_drift_check else _real_drift_check)
    except ValueError as exc:
        print(f"No se promovió nada: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"{args.client}: V{result['from']} -> V{result['to']} promovido a mano (verificado: {' y '.join(result['checks'])}).")
    if args.publish:
        import run_daily_refresh as rdr

        git = rdr.Git(PROJECT_ROOT)
        top = git.toplevel()
        if top is None:
            print("No estoy dentro de un repo de git: no se publicó.", file=sys.stderr)
            sys.exit(3)
        data_map_name = (PROJECT_ROOT / re.search(r'^data_map:\s*"(.*)"', result["config"].read_text(encoding="utf-8"), re.MULTILINE).group(1)).resolve()
        paths = [rdr._relative_to_git(Path(p), top) for p in (result["config"], data_map_name, result["log_md"], result["log_jsonl"])]
        outcome = rdr.publish(git, {args.client: paths}, push=True,
                              message=f"Data Map de {args.client} promovido a mano a V{result['to']}\n\n{args.reason}".strip())
        print("Publicado." if outcome.get("published") else f"No se pudo publicar: {outcome.get('error') or outcome.get('reason')}")
        sys.exit(0 if outcome.get("published") else 3)
    print("Para publicarlo: revisá el diff y hacé commit y push, o repetí con --publish.")


if __name__ == "__main__":
    main()
