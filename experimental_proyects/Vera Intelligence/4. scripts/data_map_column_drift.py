"""Chequeo de deriva entre los campos declarados en un Data Map y las columnas reales en Postgres.

Por qué existe (2026-09-24): los 8 campos de la fuente `insights_descriptivos` de Steren estaban
declarados SIN el prefijo `descriptivos_` que tienen las columnas físicas de la vista. Todo SQL de
"Voz del Cliente" (objeciones, quejas, promociones...) fallaba con "column does not exist" y el
modelo caía a la búsqueda semántica sin poder usar el dato estructurado -y nadie lo notó, porque el
gate del banco dorado sólo corre las primeras `MAX_GOLDEN_QUESTIONS` preguntas y compara respuestas
entre sí, no contra la base. Este chequeo es de sólo lectura (information_schema) y no llama a Gemini.

Uso, desde la raíz del repo:
    python "experimental_proyects/Vera Intelligence/4. scripts/data_map_column_drift.py"            # todos
    python "experimental_proyects/Vera Intelligence/4. scripts/data_map_column_drift.py" --client steren_alto
    ... --client steren_alto --data-map "experimental_proyects/Vera Intelligence/2. clientes/steren_alto/data_map/VI Data Map Steren V3.yaml"
Sale con código 1 si encuentra deriva (útil antes de promover un Data Map).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
REPO_ROOT = PROJECT_ROOT.parents[1]
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
for _path in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def find_drift(data_map: dict, columns_by_view: dict[str, set[str]]) -> list[dict]:
    """Devuelve una entrada por fuente con campos declarados que no existen como columna.

    `columns_by_view` mapea "schema.vista" -> conjunto de columnas reales. Una vista ausente del
    mapa se reporta como `view_not_found`. Se ignoran las claves que agrupan otros campos
    (`campos: {...}` en la spec): son documentación, no columnas físicas (ej. Tigo,
    `calidad_del_asesor_escala`).
    """
    findings: list[dict] = []
    for source_key, source in (data_map.get("sources") or {}).items():
        view = (source or {}).get("source", "")
        if "." not in view:
            continue
        if view not in columns_by_view or not columns_by_view[view]:
            findings.append({"source": source_key, "view": view, "view_not_found": True, "missing": []})
            continue
        real = columns_by_view[view]
        missing = []
        for field_name, spec in ((source or {}).get("fields") or {}).items():
            if isinstance(spec, dict) and "campos" in spec:
                continue
            if field_name not in real:
                missing.append(field_name)
        if missing:
            findings.append({"source": source_key, "view": view, "view_not_found": False, "missing": missing})
    return findings


def fetch_columns(views: set[str]) -> dict[str, set[str]]:
    from utils.postgres import get_postgres_connection

    columns: dict[str, set[str]] = {}
    with get_postgres_connection() as conn, conn.cursor() as cur:
        for view in sorted(views):
            schema, table = view.split(".", 1)
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (schema, table),
            )
            columns[view] = {row[0] for row in cur.fetchall()}
    return columns


def check_client(client_folder: str, data_map_path: Path | None = None) -> list[dict]:
    """`data_map_path` permite revisar una versión candidata (sin promover) en vez de la vigente."""
    from client_config import load_client_config

    config = load_client_config(client_folder)
    data_map = yaml.safe_load((data_map_path or config.data_map_path).read_text(encoding="utf-8"))
    views = {
        s["source"] for s in (data_map.get("sources") or {}).values()
        if isinstance(s, dict) and "." in s.get("source", "")
    }
    return find_drift(data_map, fetch_columns(views))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", help="Carpeta del cliente bajo '2. clientes/'. Sin esto, revisa todos.")
    parser.add_argument("--data-map", type=Path, help="Ruta a una versión candidata (requiere --client).")
    args = parser.parse_args()
    if args.data_map and not args.client:
        parser.error("--data-map requiere --client")
    folders = [args.client] if args.client else sorted(
        p.name for p in CLIENTS_ROOT.iterdir() if (p / "config.yaml").exists()
    )
    drift_found = False
    for folder in folders:
        for finding in check_client(folder, args.data_map):
            drift_found = True
            if finding["view_not_found"]:
                print(f"{folder}: la vista {finding['view']} (fuente {finding['source']}) no existe o no tiene columnas")
            else:
                print(f"{folder}: fuente {finding['source']} declara campos que NO existen en {finding['view']}: {finding['missing']}")
    print("Con deriva." if drift_found else f"Sin deriva en {len(folders)} cliente(s).")
    return 1 if drift_found else 0


if __name__ == "__main__":
    sys.exit(main())
