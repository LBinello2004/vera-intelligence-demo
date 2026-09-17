"""EXPERIMENTO — tester interactivo del coaching playbook, SIN publicar nada en Langfuse.

Calca vi_agent_tester.py, pero:
1. No toca scripts/vi_agent.py -lo importa tal cual, sin monkeypatch de su código fuente-.
2. Agrega al system_instruction un párrafo extra que le avisa al modelo que existe una tercera
   herramienta de reglas de negocio, "coaching_playbook", con su business_scope.
3. Intercepta la función que despacha get_business_rules (TOOL_FUNCTIONS, sólo en memoria de este
   proceso) para que, cuando el modelo pida "coaching_playbook", en vez de ir a Langfuse (que fallaría,
   el config.yaml real no lo declara) devuelva el contenido de draft_prompt_coaching_playbook.md de
   esta misma carpeta.

Nada de esto persiste ni afecta a ninguna otra corrida de vi_agent.py/vi_agent_tester.py -es un
monkeypatch en memoria, vive y muere con este proceso-.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EXPERIMENT_DIR.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vi_agent  # noqa: E402
from client_config import available_client_ids  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


DRAFT_PROMPT_PATH = EXPERIMENT_DIR / "draft_prompt_coaching_playbook.md"
COACHING_BUSINESS_SCOPE = (
    "Guía de coaching para formular recomendaciones a vendedores con bajo desempeño en un criterio puntual"
)

COACHING_EXTRA_INSTRUCTION = f"""

HERRAMIENTA EXPERIMENTAL ADICIONAL (sólo en este tester, no existe en producción todavía):
3. get_business_rules("coaching_playbook"): usala cuando te pidan formular una recomendación de
   coaching para un vendedor con bajo desempeño en un criterio puntual del checklist, y necesites una
   guía explícita de cómo estructurar esa recomendación. {COACHING_BUSINESS_SCOPE}. Es un rulebook
   EXPERIMENTAL con un borrador local (no viene de Langfuse todavía) — tratalo igual que a los demás:
   incorporá su contenido como una explicación de negocio, ocultá que existe un "borrador" o un
   "archivo" detrás, nunca lo menciones como fuente técnica ante el usuario.
"""


def _load_draft_text() -> str:
    return DRAFT_PROMPT_PATH.read_text(encoding="utf-8")


def _install_coaching_playbook_stub() -> None:
    """Reemplaza, sólo en memoria, la función que TOOL_FUNCTIONS usa para get_business_rules."""
    original_get_business_rules = vi_agent.get_business_rules
    draft_text = _load_draft_text()

    def _get_business_rules_with_coaching_stub(rulebook: str) -> str:
        if rulebook.strip().lower() == "coaching_playbook":
            return json.dumps(
                {
                    "business_scope": COACHING_BUSINESS_SCOPE,
                    "rules_version": "draft-local-sin-langfuse",
                    "criteria_text": draft_text,
                    "source_status": "experimental_local_draft",
                },
                ensure_ascii=False,
            )
        return original_get_business_rules(rulebook)

    vi_agent.TOOL_FUNCTIONS["get_business_rules"] = _get_business_rules_with_coaching_stub


def _build_system_instruction_with_coaching() -> str:
    return vi_agent.build_system_instruction() + COACHING_EXTRA_INSTRUCTION


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="[EXPERIMENTO] Tester interactivo del coaching playbook (borrador local, sin Langfuse)."
    )
    parser.add_argument("--client", default=None, help="client_id (carpeta bajo clientes/, ej. mens_fashion, farma24).")
    parser.add_argument(
        "--internal-debug", "--debug", dest="internal_debug", action="store_true",
        help="Muestra trazas técnicas internas (qué herramienta se llamó, con qué argumentos).",
    )
    return parser.parse_args()


def _prompt_for_client() -> str:
    disponibles = available_client_ids()
    if not disponibles:
        raise RuntimeError("No hay clientes configurados bajo clientes/.")
    if len(disponibles) == 1:
        print(f"Único cliente disponible: {disponibles[0]}", flush=True)
        return disponibles[0]
    listado = ", ".join(disponibles)
    while True:
        entrada = input(f"Cliente a atender ({listado})> ").strip().lower()
        if entrada in disponibles:
            return entrada
        print(f"client_id inválido: {entrada!r}. Opciones: {listado}.", flush=True)


def main() -> None:
    args = parse_args()
    client_id = args.client or _prompt_for_client()

    vi_agent.configure_client(client_id)
    vi_agent.load_environment()
    _install_coaching_playbook_stub()
    system_instruction = _build_system_instruction_with_coaching()
    chat = vi_agent.build_chat(system_instruction=system_instruction)

    modo = " (trazas internas activas)" if args.internal_debug else ""
    print(
        f"[EXPERIMENTO] Vera Intelligence + coaching_playbook (borrador local) - "
        f"{vi_agent.CLIENT_CONFIG.display_name}{modo}. Enter vacío para salir.\n",
        flush=True,
    )
    session_id = uuid.uuid4().hex
    while True:
        try:
            question = input("Pregunta> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            break
        if not question:
            break
        print("(pensando...)", flush=True)
        try:
            answer = vi_agent.run_tool_loop(
                chat, question, max_tool_calls=20, debug=args.internal_debug, session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            if args.internal_debug:
                print(f"[internal] Error al responder: {exc}", file=sys.stderr, flush=True)
            answer = "No pude completar el análisis en este momento. Intentá nuevamente en unos minutos."
        print(f"\n{answer}\n", flush=True)


if __name__ == "__main__":
    main()
