"""EXPERIMENTO, no incorporado al proyecto principal todavía — ver README.md de esta carpeta.

Resumen ejecutivo periódico: corre un set fijo de preguntas de negocio contra vi_agent.py.

A diferencia del resto del proyecto (100% reactivo: alguien tiene que pensar qué preguntar),
este script hace a Vera Intelligence proactivo -pensado para correr semanalmente vía una tarea
programada, igual que el poller de actualización del Data Map (ver README.md del proyecto >
"Actualización automática del Data Map"), reusando el mismo patrón: un script simple invocado
por cron, sin tocar nada nuevo de seguridad ni de infraestructura.

Vive fuera de scripts/ y de clientes/<id>/preguntas/ a propósito -es un experimento, no forma
parte todavía del proyecto real; importa vi_agent.py desde ../../scripts/ sin moverlo ni
duplicarlo-. Las preguntas de cada cliente están en preguntas/<client_id>.yaml -mismo formato
liviano que preguntas_evaluacion.yaml, sin SQL de referencia porque esto no mide precisión, es
el contenido real del resumen-.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

EXPERIMENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EXPERIMENT_DIR.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vi_agent  # noqa: E402


PREGUNTAS_DIR = EXPERIMENT_DIR / "preguntas"
DIGESTS_ROOT = EXPERIMENT_DIR / ".runtime" / "digests"


def build_digest(client_id: str) -> str:
    vi_agent.configure_client(client_id)
    vi_agent.load_environment()
    client_config = vi_agent.CLIENT_CONFIG

    bank_path = PREGUNTAS_DIR / f"{client_id}.yaml"
    bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))

    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f"# Resumen ejecutivo — {client_config.display_name}", f"Generado: {today}", ""]

    for item in bank["preguntas"]:
        chat = vi_agent.build_chat()
        answer = vi_agent.run_tool_loop(chat, item["pregunta"], max_tool_calls=20, debug=False)
        # El bloque ```vera-suggestions``` (obligatorio en toda respuesta desde 2026-09-11, ver
        # SUGERENCIAS DE SEGUIMIENTO en vi_agent.SYSTEM_INSTRUCTION_TEMPLATE) es para chips
        # clickeables de una interfaz interactiva -no tiene sentido en un digest estático, se saca.
        answer, _suggestions = vi_agent.extract_suggestion_blocks(answer)
        lines.append(f"## {item['pregunta']}")
        lines.append("")
        lines.append(answer)
        lines.append("")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="[EXPERIMENTO] Genera el resumen ejecutivo periódico de un cliente."
    )
    parser.add_argument(
        "--client",
        default=None,
        help=(
            "client_id (carpeta bajo preguntas/, ej. farma24). "
            "Si se omite, se pregunta de forma interactiva al arrancar."
        ),
    )
    return parser.parse_args()


def _available_bank_client_ids() -> list[str]:
    """client_id disponibles = los que tienen banco de preguntas propio en preguntas/."""
    return sorted(p.stem for p in PREGUNTAS_DIR.glob("*.yaml"))


def _prompt_for_client() -> str:
    """Pide el client_id por consola, validando contra preguntas/ disponibles."""
    disponibles = _available_bank_client_ids()
    if not disponibles:
        raise RuntimeError(f"No hay bancos de preguntas configurados bajo {PREGUNTAS_DIR}.")
    if len(disponibles) == 1:
        print(f"Único cliente disponible: {disponibles[0]}", flush=True)
        return disponibles[0]

    listado = ", ".join(disponibles)
    while True:
        try:
            entrada = input(f"Cliente a atender ({listado})> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            raise SystemExit(0) from None
        if entrada in disponibles:
            return entrada
        print(f"client_id inválido: {entrada!r}. Opciones: {listado}.", flush=True)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    args = parse_args()
    client_id = args.client or _prompt_for_client()
    digest = build_digest(client_id)

    out_dir = DIGESTS_ROOT / client_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    out_path.write_text(digest, encoding="utf-8")

    print(digest)
    print(f"\n(guardado en {out_path})", file=sys.stderr)


if __name__ == "__main__":
    main()
