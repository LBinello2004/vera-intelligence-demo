from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

import vi_agent

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def run_question(question: str, max_tool_calls: int = 20):
    """Corre una pregunta en una sesión nueva y captura la auditoría interna."""
    chat = vi_agent.build_chat()
    tool_calls_log: list[dict] = []
    answer = vi_agent.run_tool_loop(
        chat,
        question,
        max_tool_calls=max_tool_calls,
        debug=True,
        tool_calls_log=tool_calls_log,
    )
    # El bloque ```vera-suggestions``` (obligatorio en toda respuesta desde 2026-09-11, ver
    # SUGERENCIAS DE SEGUIMIENTO en vi_agent.SYSTEM_INSTRUCTION_TEMPLATE) no aporta nada a una
    # auditoría de precisión -se saca para no ensuciar respuesta_agente en el JSON de resultados.
    answer, _suggestions = vi_agent.extract_suggestion_blocks(answer)
    return answer, tool_calls_log


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Corre las preguntas del banco de evaluacion contra vi_agent y guarda las respuestas crudas."
    )
    parser.add_argument(
        "--client",
        default="mens_fashion_alto",
        help="client_id a evaluar (carpeta bajo clientes/, ej. mens_fashion_alto, atlas_medio).",
    )
    parser.add_argument("--bank", type=Path, help="Ruta al banco de preguntas (default: clientes/<client>/preguntas/preguntas_evaluacion.yaml).")
    parser.add_argument("--output", type=Path, help="Ruta de salida (default: clientes/<client>/preguntas/resultados_evaluacion.json).")
    parser.add_argument("--only", nargs="*", help="IDs puntuales a correr (default: todas).")
    args = parser.parse_args()

    vi_agent.configure_client(args.client)
    vi_agent.load_environment()

    client_dir = CLIENTS_ROOT / args.client / "preguntas"
    bank_path = args.bank or (client_dir / "preguntas_evaluacion.yaml")
    output_path = args.output or (client_dir / "resultados_evaluacion.json")

    bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))
    preguntas = bank["preguntas"]
    if args.only:
        preguntas = [p for p in preguntas if p["id"] in args.only]

    results = []
    for item in preguntas:
        print(f"=== {item['id']} ===", flush=True)
        print(f"Pregunta: {item['pregunta']}", flush=True)
        try:
            answer, tool_calls = run_question(item["pregunta"])
        except Exception as exc:  # noqa: BLE001
            answer = f"ERROR: {exc}"
            tool_calls = []
        print(f"Respuesta del agente: {answer}\n", flush=True)
        results.append(
            {
                "id": item["id"],
                "pregunta": item["pregunta"],
                "es_trampa": item.get("es_trampa", False),
                "respuesta_esperada": item.get("respuesta_esperada"),
                "criterios_evaluacion": item.get("criterios_evaluacion"),
                "sql_esperado": item.get("sql"),
                "respuesta_agente": answer,
                "tool_calls_agente": tool_calls,
            }
        )
        output_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    print(f"\nListo. {len(results)} preguntas corridas. Resultados en {output_path}", flush=True)


if __name__ == "__main__":
    main()
