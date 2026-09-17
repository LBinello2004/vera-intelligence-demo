"""Resumen local de calidad/uso de search_conversations por cliente, a partir del log agregado
que ya escribe `_log_search_event` en vector_search.py -nunca texto de query ni contenido citado,
mismo criterio de privacidad que usage_tracking.py.

Pensado para responder la pregunta operativa real, sin abrir Postgres ni Langfuse: de los clientes
con `vector_search` habilitado (ver "8. README.md" > ampliación a todos los clientes viables),
¿cuáles están rindiendo bien y cuáles necesitan atención? -por ejemplo, un `client_id` con una tasa
alta de búsquedas vacías o con muchos resultados descartados por el juez interno es candidato a
revisar antes que otro con métricas sanas, en vez de tratarlos a todos con la misma prioridad sólo
porque comparten el mismo mecanismo.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Any

from usage_tracking import load_usage_events
from vector_search import VECTOR_SEARCH_LOG_PATH


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return ordered[index]


def summarize_by_client(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Agrupa eventos por `client_id` y calcula, por cliente:

    - calls: cantidad de búsquedas registradas.
    - empty_rate: fracción de búsquedas que no devolvieron ningún resultado (0.0-1.0).
    - avg_results_returned: promedio de resultados devueltos (después del juez interno).
    - judge_discard_rate: de los candidatos que llegaron al juez interno (resultados devueltos +
      descartados por el juez), qué fracción descartó -una tasa alta sugiere que la query o el
      corpus de ese cliente calzan peor semánticamente, no necesariamente un bug.
    - avg_query_ms / p95_query_ms: latencia del scan de Postgres -sin índice vectorial todavía (ver
      README), útil para ver si algún cliente puntual sufre más que el resto (ej. por volumen).
    - first_call_at / last_call_at: rango de fechas cubierto, para saber si el resumen es sobre
      poca o mucha actividad real.

    Devuelve `{}` si `events` está vacío (sin actividad registrada todavía)."""
    by_client: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        client_id = str(event.get("client_id") or "(desconocido)")
        by_client.setdefault(client_id, []).append(event)

    summary: dict[str, dict[str, Any]] = {}
    for client_id, client_events in by_client.items():
        calls = len(client_events)
        empty_calls = sum(1 for e in client_events if (e.get("results_returned") or 0) == 0)
        results_returned = [int(e.get("results_returned") or 0) for e in client_events]
        judge_filtered = [int(e.get("judge_filtered_count") or 0) for e in client_events]
        judge_seen_total = sum(results_returned) + sum(judge_filtered)
        query_ms_values = [
            float(e["query_ms"]) for e in client_events if e.get("query_ms") is not None
        ]
        recorded_ats = sorted(
            str(e["recorded_at"]) for e in client_events if e.get("recorded_at")
        )
        summary[client_id] = {
            "calls": calls,
            "empty_rate": round(empty_calls / calls, 3) if calls else None,
            "avg_results_returned": round(statistics.mean(results_returned), 2)
            if results_returned
            else None,
            "judge_discard_rate": round(sum(judge_filtered) / judge_seen_total, 3)
            if judge_seen_total
            else None,
            "avg_query_ms": round(statistics.mean(query_ms_values), 1)
            if query_ms_values
            else None,
            "p95_query_ms": _percentile(query_ms_values, 0.95),
            "first_call_at": recorded_ats[0] if recorded_ats else None,
            "last_call_at": recorded_ats[-1] if recorded_ats else None,
        }
    return summary


_ATTENTION_EMPTY_RATE = 0.4
_ATTENTION_JUDGE_DISCARD_RATE = 0.6
_ATTENTION_MIN_CALLS = 5


def flag_clients_needing_attention(summary: dict[str, dict[str, Any]]) -> list[str]:
    """Señala clientes cuyas métricas sugieren revisar la búsqueda vectorial antes que la del
    resto -umbrales deliberadamente conservadores (`_ATTENTION_MIN_CALLS`) para no marcar un
    cliente con actividad todavía chica como "problemático" por ruido estadístico; ajustar estos
    umbrales con más datos reales es más confiable que afinarlos a ciegas ahora."""
    flagged = []
    for client_id, metrics in summary.items():
        if metrics["calls"] < _ATTENTION_MIN_CALLS:
            continue
        if (metrics["empty_rate"] or 0) >= _ATTENTION_EMPTY_RATE:
            flagged.append(client_id)
        elif (metrics["judge_discard_rate"] or 0) >= _ATTENTION_JUDGE_DISCARD_RATE:
            flagged.append(client_id)
    return flagged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resumen local de calidad/uso de search_conversations por cliente."
    )
    parser.add_argument("--log", type=Path, default=VECTOR_SEARCH_LOG_PATH)
    return parser.parse_args()


def main() -> None:
    # En PowerShell/cmd la consola no siempre es UTF-8 por defecto -sin esto, imprimir acentos
    # puede salir como caracteres corruptos (mismo fix ya usado en "1. vi_agent_tester.py").
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    args = parse_args()
    events = load_usage_events(args.log)
    if not events:
        print(f"Sin actividad registrada todavía en {args.log}.")
        return
    summary = summarize_by_client(events)
    print(f"{'cliente':<24}{'llamadas':>9}{'% vacías':>10}{'resultados prom.':>18}"
          f"{'% descartado juez':>19}{'query_ms prom.':>16}{'query_ms p95':>14}")
    for client_id in sorted(summary, key=lambda c: summary[c]["calls"], reverse=True):
        m = summary[client_id]
        empty_pct = "" if m["empty_rate"] is None else f"{m['empty_rate'] * 100:.0f}%"
        avg_results = "" if m["avg_results_returned"] is None else m["avg_results_returned"]
        judge_pct = (
            "" if m["judge_discard_rate"] is None else f"{m['judge_discard_rate'] * 100:.0f}%"
        )
        avg_ms = "" if m["avg_query_ms"] is None else m["avg_query_ms"]
        p95_ms = "" if m["p95_query_ms"] is None else m["p95_query_ms"]
        print(
            f"{client_id:<24}{m['calls']:>9}{empty_pct:>10}{avg_results:>18}"
            f"{judge_pct:>19}{avg_ms:>16}{p95_ms:>14}"
        )
    flagged = flag_clients_needing_attention(summary)
    if flagged:
        print(
            "\nClientes a revisar primero (alta tasa de búsquedas vacías o de descarte del juez, "
            f"con actividad suficiente para no ser ruido): {', '.join(flagged)}."
        )


if __name__ == "__main__":
    main()
