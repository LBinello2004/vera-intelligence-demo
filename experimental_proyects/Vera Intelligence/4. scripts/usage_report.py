"""Muestra consumo agregado sin leer ni exponer conversaciones."""

from __future__ import annotations

import argparse
from pathlib import Path

from usage_tracking import TOKEN_FIELDS, estimate_cost_usd, load_usage_events, summarize_usage


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LOG = SCRIPT_DIR.parent / ".runtime" / "usage" / "gemini_calls.jsonl"

_NO_TOOL_LABEL = "(sin tool previa -pregunta inicial, saludo, o reintento de estilo)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resumen local de tokens de Vera Intelligence.")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument(
        "--last",
        type=int,
        help="Limita el resumen a las últimas N llamadas registradas.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Muestra los contadores de cada llamada sin exponer su contenido.",
    )
    parser.add_argument(
        "--by-tool",
        action="store_true",
        help=(
            "Desglosa llamadas/tokens/costo por combinación de tools usadas en el turno anterior "
            "(ver `tools_called` en usage_tracking.py, 2026-09-14) -no es un dashboard, sólo un "
            "agrupado sobre el mismo log."
        ),
    )
    return parser.parse_args()


def _tools_key(event: dict) -> str:
    tools = event.get("tools_called")
    return "+".join(tools) if tools else _NO_TOOL_LABEL


def _group_by_tools(events: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for event in events:
        groups.setdefault(_tools_key(event), []).append(event)
    return groups


def _print_by_tool_breakdown(events: list[dict]) -> None:
    """Agrupa por la combinación exacta de tools del turno (ej. "run_readonly_sql" vs.
    "get_business_rules+run_readonly_sql" cuando se piden juntas) en vez de intentar repartir el
    costo de un turno con varias tools entre cada una por separado -esa división sería una
    precisión inventada que el dato real no respalda (el costo es del TURNO, no de una tool
    aislada dentro de él). El costo estimado agrupa además por modelo dentro de cada combinación,
    por si alguna vez conviven varios modelos en el mismo log."""
    groups = _group_by_tools(events)
    print("\nDesglose por tool (combinación exacta usada en el turno):")
    for key in sorted(groups, key=lambda k: (k == _NO_TOOL_LABEL, k)):
        group_events = groups[key]
        by_model: dict[str, list[dict]] = {}
        for event in group_events:
            by_model.setdefault(str(event.get("model", "")), []).append(event)
        total_cost = 0.0
        cost_known = True
        for model, model_events in by_model.items():
            cost = estimate_cost_usd(summarize_usage(model_events), model=model)
            if cost is None:
                cost_known = False
                continue
            total_cost += cost
        summary = summarize_usage(group_events)
        cost_str = f"US$ {total_cost:.4f}" if cost_known else "US$ ? (modelo sin precio conocido)"
        print(
            f"  {key}: {summary['calls']} llamadas | "
            f"{summary['total_token_count']} tokens totales | costo estimado {cost_str}"
        )


def main() -> None:
    args = parse_args()
    events = load_usage_events(args.log)
    if args.last is not None:
        if args.last < 1:
            raise SystemExit("--last debe ser mayor que cero.")
        events = events[-args.last :]
    summary = summarize_usage(events)

    if args.details:
        for event in events:
            print(
                f"{event.get('recorded_at', 'sin fecha')} | "
                f"interacción={str(event.get('interaction_id', ''))[:8]} | "
                f"llamada={event.get('call_index', '?')} | "
                f"tipo={event.get('call_kind', 'desconocido')} | "
                f"entrada={int(event.get('prompt_token_count') or 0)} | "
                f"salida={int(event.get('candidates_token_count') or 0)} | "
                f"razonamiento={int(event.get('thoughts_token_count') or 0)} | "
                f"total={int(event.get('total_token_count') or 0)}"
            )
        if events:
            print()

    print(f"Llamadas: {summary['calls']}")
    print(f"Sesiones: {summary['sessions']}")
    print(f"Interacciones: {summary['interactions']}")
    labels = {
        "prompt_token_count": "Entrada",
        "candidates_token_count": "Salida",
        "thoughts_token_count": "Razonamiento",
        "cached_content_token_count": "Contenido en caché",
        "tool_use_prompt_token_count": "Uso de herramientas",
        "total_token_count": "Total",
    }
    for field in TOKEN_FIELDS:
        label = labels[field]
        print(f"{label}: {summary[field]}")

    if args.by_tool:
        _print_by_tool_breakdown(events)


if __name__ == "__main__":
    main()
