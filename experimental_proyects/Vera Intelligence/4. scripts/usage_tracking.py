"""Registro local y privado del consumo de tokens de Gemini."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOKEN_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "cached_content_token_count",
    "tool_use_prompt_token_count",
    "total_token_count",
)


def extract_usage_metadata(response: object) -> dict[str, Any] | None:
    """Extrae únicamente contadores; nunca prompts, respuestas ni tool payloads."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None

    result: dict[str, Any] = {}
    for field in TOKEN_FIELDS:
        value = getattr(usage, field, None)
        result[field] = int(value) if value is not None else 0

    traffic_type = getattr(usage, "traffic_type", None)
    if traffic_type is not None:
        result["traffic_type"] = getattr(traffic_type, "value", str(traffic_type))
    return result


class UsageRecorder:
    """Agrega una entrada JSONL por respuesta exitosa de Gemini."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record_response(
        self,
        response: object,
        *,
        client_id: str,
        model: str,
        session_id: str,
        interaction_id: str,
        call_index: int,
        call_kind: str,
        attempts: int,
        tools_called: list[str] | None = None,
        retry_reason: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        usage = extract_usage_metadata(response)
        if usage is None:
            return None

        event = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "client_id": client_id,
            "model": model,
            "session_id": session_id,
            "interaction_id": interaction_id,
            "call_index": call_index,
            "call_kind": call_kind,
            "attempts": attempts,
            **usage,
        }
        # tools_called (2026-09-14, pedido explícito: "costo de cada tool") -las tools que se
        # ejecutaron en el turno ANTERIOR y cuyo resultado se le está devolviendo al modelo en
        # ESTA llamada (por eso viaja en la llamada "tool_results", nunca en "initial"): el costo
        # de procesar el resultado de una tool es, en la práctica, el costo real de haber usado esa
        # tool. Lista vacía/None -> se omite el campo (llamada "initial"/sin tool previa, ej. saludo
        # o primer turno de cualquier pregunta), en vez de guardar `[]` en cada evento.
        if tools_called:
            event["tools_called"] = sorted(tools_called)
        # retry_reason (2026-09-15, pedido explícito: "medí los reintentos") -por qué ESTA llamada
        # es un reintento (call_kind "client_safe_rewrite"/"evidence_repair"), no sólo que lo es.
        # Sin esto, el log ya mostraba CUÁNTOS reintentos había (por call_kind) pero nunca el motivo
        # -encontramos una tasa de 17,8% de rewrites en mens_fashion_alto (vs. 0% en la mayoría de
        # los demás clientes) sin poder confirmar la causa exacta contra el log existente. None/vacío
        # -> se omite, igual que tools_called (llamadas "initial"/"tool_results" nunca tienen motivo).
        if retry_reason:
            event["retry_reason"] = retry_reason
        self._append(event)
        return event

    def _append(self, event: dict[str, Any]) -> None:
        _append_jsonl(self.path, event)


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        if hasattr(os, "fchmod"):
            # No disponible en Windows: los permisos POSIX no aplican ahi
            # y os.open ya paso 0o600 al crear el archivo.
            os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


class InteractionOutcomeRecorder:
    """Registro local de CÓMO terminó cada interacción de usuario (2026-09-15) -complementa a
    UsageRecorder (que registra cada llamada individual a Gemini, ver retry_reason arriba): esto es
    un resumen por interacción, pensado para responder "¿con qué frecuencia el usuario recibe un
    fallback genérico en vez de una respuesta real?" y "¿cuántas respuestas exitosas citan un
    número sin respaldo directo en SQL?" sin tener que reconstruirlo cruzando eventos de
    gemini_calls.jsonl por interaction_id."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(
        self,
        *,
        client_id: str,
        session_id: str,
        interaction_id: str,
        outcome: str,
        rewrite_count: int,
        evidence_repairs: int,
        **extra: Any,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "client_id": client_id,
            "session_id": session_id,
            "interaction_id": interaction_id,
            "outcome": outcome,
            "rewrite_count": rewrite_count,
            "evidence_repairs": evidence_repairs,
        }
        # Campos opcionales según el outcome (verification_errors, violations, offending_terms,
        # unbacked_numbers) -sólo se guardan si vienen con contenido real, mismo criterio que
        # tools_called/retry_reason arriba: un evento sin motivo no debe llevar una clave vacía.
        for key, value in extra.items():
            if value:
                event[key] = value
        _append_jsonl(self.path, event)
        return event


def load_usage_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Registro de uso inválido en línea {line_number}.") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


# Pricing conocido por modelo, USD por millón de tokens (entrada, salida) — Standard tier, oficial
# y verificado en https://ai.google.dev/gemini-api/docs/pricing (2026-09-17). gemini-3.7-flash es
# el único en producción hoy (los 19 clientes lo usan, verificado contra los 19 config.yaml);
# gemini-3.5-flash-lite y gemini-3.1-flash-lite se agregaron sólo como opciones de prueba en
# "1. vi_agent_tester.py" (ver vi_agent.AVAILABLE_MODELS), ninguno en producción todavía. Precio de
# gemini-3.7-flash vigente sólo hasta 2026-12-31 -sube a (1.50, 7.50) desde 2027-01-01 según la
# misma fuente; recalcular acá cuando llegue esa fecha. Un modelo sin entrada acá devuelve None en
# `estimate_cost_usd` en vez de asumir un precio -mejor no estimar que estimar mal-.
MODEL_PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}

# Tarifa de tokens cacheados como fracción del precio de ENTRADA normal. CORREGIDO (2026-09-17):
# antes en 0.25 ("25% del precio normal"), pero el pricing Standard oficial de gemini-3.7-flash SÍ
# está publicado para cache (https://ai.google.dev/gemini-api/docs/pricing, confirmado también en
# "10. documentos/MEDICION_COSTO_EN_USO.md", 2026-09-14): $0,075/M cacheado contra $0,75/M de
# entrada fresca = 10%, no 25%. La medición de esa fecha ya lo había señalado como desactualizado
# ("no se utilizó el estimador del proyecto, cuya fracción de precio cacheado sigue desactualizada
# -0,25 en lugar de 0,10-") pero nunca se corrigió en código hasta ahora. Con ~99% del prompt
# saliendo de cache en sesiones reales (ver "Prompt caching de Gemini", 2026-09-11), este único
# número infla el costo estimado de cada respuesta bastante más de lo que sugiere el 15 pp de
# diferencia -es la corrección de mayor impacto real en la precisión del estimador de costo.
CACHED_INPUT_PRICE_FRACTION = 0.10


def estimate_cost_usd(summary: dict[str, Any], *, model: str) -> float | None:
    """Estimación de costo a partir de un resumen de `summarize_usage`, no una factura real.

    Los tokens de "razonamiento" (`thoughts_token_count`) se cuentan como salida -Gemini los
    factura así-. `cached_content_token_count` se descuenta del precio de entrada normal
    (`CACHED_INPUT_PRICE_FRACTION`) en vez de ignorarse -confirmado en vivo que
    `prompt_token_count` ya INCLUYE los tokens cacheados como subconjunto (no se suman aparte:
    ejemplo real, prompt=23.161, cached=23.150), así que hay que separar entrada cacheada de
    entrada nueva, no sumar una tercera categoría.

    BUG REAL corregido (2026-09-14): `tool_use_prompt_token_count` -los tokens que devuelve un
    tool nativo server-side (Gemini File Search/RAG) y que vuelven al modelo como entrada- nunca se
    sumaba acá, aunque `usage_tracking.py` ya lo registraba en cada evento desde el principio (ver
    `TOKEN_FIELDS`). Encontrado midiendo en vivo el costo real de RAG contra farma24_alto (único
    punto del proyecto que usa File Search, ver "8. README.md" > Costos): una sola pregunta que
    dispara `product_catalog` devolvió `tool_use_prompt_token_count=64.504` -más del doble del
    system_instruction cacheado completo (~26k)- que quedaba totalmente afuera de esta estimación.
    Se factura como entrada fresca (no hay descuento de cache documentado para este campo, a
    diferencia de `cached_content_token_count`) -Gemini nunca reportó ningún costo para RAG por
    fuera de tokens hasta ahora, así que tratarlo como entrada normal es la mejor estimación
    disponible sin un precio específico publicado para File Search."""
    pricing = MODEL_PRICING_PER_MILLION_TOKENS.get(model)
    if pricing is None:
        return None
    input_price, output_price = pricing
    prompt_tokens = int(summary.get("prompt_token_count") or 0)
    cached_tokens = min(int(summary.get("cached_content_token_count") or 0), prompt_tokens)
    fresh_input_tokens = prompt_tokens - cached_tokens
    tool_use_tokens = int(summary.get("tool_use_prompt_token_count") or 0)
    output_tokens = int(summary.get("candidates_token_count") or 0) + int(
        summary.get("thoughts_token_count") or 0
    )
    return (
        ((fresh_input_tokens + tool_use_tokens) / 1_000_000) * input_price
        + (cached_tokens / 1_000_000) * input_price * CACHED_INPUT_PRICE_FRACTION
        + (output_tokens / 1_000_000) * output_price
    )


def summarize_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {field: 0 for field in TOKEN_FIELDS}
    sessions: set[str] = set()
    interactions: set[str] = set()
    for event in events:
        sessions.add(str(event.get("session_id", "")))
        interactions.add(str(event.get("interaction_id", "")))
        for field in TOKEN_FIELDS:
            totals[field] += int(event.get(field) or 0)
    return {
        "calls": len(events),
        "sessions": len(sessions - {""}),
        "interactions": len(interactions - {""}),
        **totals,
    }
