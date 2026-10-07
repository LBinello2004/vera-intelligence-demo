"""Actualización automática del Data Map de un cliente ante un cambio de prompt en Langfuse.

Flujo (sin revisión humana, con guardrails automáticos en su lugar):
1. Detecta si algún business_rulebook del cliente subió de versión en Langfuse
   (label production), comparando contra el snapshot cacheado en .runtime/.
2. Si cambió, regenera el Data Map: le pasa a Gemini (mismo modelo que sirve al
   cliente, CLIENT_CONFIG.model) el diff del prompt + el Data Map vigente, con
   acceso real a run_readonly_sql para que verifique empíricamente contra
   Postgres cada regla nueva o modificada antes de escribirla -mismo método
   que la auditoría manual V1->V2->V3 documentada en README.md-. Escribe el
   resultado como una versión NUEVA (Vn+1), nunca pisa la vigente.
3. Gate automático: corre el banco dorado del cliente (máximo
   MAX_GOLDEN_QUESTIONS preguntas, ver README.md > "Incorporar otro cliente")
   contra la versión vigente y la candidata y valida dos cosas mecánicas, sin juicio de
   modelo: (a) todo el SQL dorado sigue pasando validate_readonly_sql contra
   las fuentes/tenant vigentes, (b) compara los números de las respuestas reales de
   ambas versiones, con la tolerancia configurada por pregunta. No compara contra
   cifras congeladas de respuesta_esperada ni demuestra equivalencia semántica.
4. Sólo si el gate pasa completo, promueve: reescribe la línea `data_map:` de
   clientes/<id>/config.yaml para apuntar a la versión nueva. Si falla, la
   versión candidata queda escrita en disco (para inspección posterior) pero
   config.yaml no se toca -el cliente sigue sirviendo con la versión anterior-.

Cada corrida deja un registro auditable en
.runtime/data_map_updates/<client_id>/<timestamp>.json (gitignored).

Limitaciones conocidas, documentadas a propósito (ver README.md):
- El gate es mecánico (SQL + números), no un juicio semántico de si la
  respuesta es correcta -evita que Gemini se autoevalúe, pero no reemplaza
  una revisión de contenido-.
- El banco dorado es finito (<=20 preguntas): pasarlo no prueba que el área
  específica que cambió esté cubierta, sólo que no rompió lo que ya se medía.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

import data_map_gate
import data_map_log
import data_map_store as dms
import vi_agent
from business_rules import BusinessRulesRepository
from client_config import ClientConfig
from google.genai import types


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"

MAX_GOLDEN_QUESTIONS = 10
MAX_REGENERATION_TOOL_CALLS = 25

# Reintentos automáticos (2026-10-07). Hasta ahora un cambio rechazado por el gate quedaba olvidado (la foto de prompts se actualizaba al
# detectarlo) y un humano tenía que "decirle que lo vuelva a ver". Ahora el cambio queda PENDIENTE hasta promoverse y se reintenta
# solo, con tope para no gastar Gemini en vano. Sólo se pide revisión humana cuando se agotan los intentos de esa versión.
RUN_ATTEMPTS = 2                  # intentos (regenerar + gate) dentro de una misma corrida
MAX_ATTEMPTS_PER_VERSION = 4      # intentos totales por cambio de prompt, sumando corridas
REGENERATION_REPAIRS = 2          # veces que se le devuelve a Gemini un YAML/formato inválido para que lo corrija
LOCK_TTL_SECONDS = 90 * 60        # un candado de otra corrida se considera muerto después de esto

# Abaratar sin empeorar (2026-10-07):
# - Un cambio de prompt que sólo toca espacios, mayúsculas, tildes o puntuación no cambia ninguna regla: se marca como procesado sin regenerar.
# - Un cambio recién detectado espera SETTLE_HOURS antes de regenerarse: en el historial 5 de 20 eventos fueron el mismo cliente editando
#   su prompt varias veces en pocos días (se regeneraba, y se pagaba, cada vez). 18 h y no 24 para que un cron diario (misma hora cada
#   día, con segundos de diferencia) siempre las cumpla. 0 lo desactiva.
# - Cascada de modelo: el primer intento regenera con un modelo más barato (VI_REGEN_CHEAP_MODEL; vacío = desactivado) y si el gate lo
#   rechaza los intentos siguientes usan el modelo del cliente. El gate es la red de seguridad en ambos casos.
SETTLE_HOURS_DEFAULT = 0.0
REGEN_CHEAP_MODEL_ENV = "VI_REGEN_CHEAP_MODEL"
SETTLE_HOURS_ENV = "VI_SETTLE_HOURS"

# Costo del gate (2026-09-17, pedido explícito de bajar el costo al mínimo): cada pregunta del
# banco dorado corre DOS VECES (vieja vs. candidata, ver docstring del módulo), así que estos dos
# valores multiplican directo el costo total del gate. GATE_MAX_TOOL_CALLS_PER_QUESTION baja de 20
# (el límite general de run_tool_loop en producción) a un tope más ajustado: las preguntas del
# banco dorado son consultas puntuales de negocio, no conversaciones abiertas, así que no deberían
# necesitar el mismo margen que una sesión real con un cliente.
#
# CORREGIDO (2026-09-17, mismo día, encontrado en vivo antes de commitear este cambio):
# GATE_THINKING_LEVEL se había puesto en MINIMAL (por debajo de LOW, buscando ahorrar más) sin
# haberlo probado contra una llamada real -el propio historial de este archivo ya lo señalaba
# ("no se volvió a medir el costo real end-to-end con esta combinación todavía"). Probado en vivo:
# `gemini-3.7-flash` (el modelo real de los 19 clientes, el mismo que usa el gate) RECHAZA
# `MINIMAL` directamente con 400 INVALID_ARGUMENT ("Thinking level MINIMAL is not supported for
# this model"). Si este archivo se hubiera commiteado así, la PRÓXIMA corrida del poller diario
# (lunes a viernes, los 19 clientes) fallaba en TODAS las preguntas de TODOS los clientes, cada una
# reportando `gate_fallo_no_promovido` sin relación a ningún cambio real del Data Map -el
# `except Exception` de `run_gate()` atrapa el error y lo cuenta como "números no coinciden", no
# como una falla de infraestructura visible aparte. LOW (el mismo nivel mínimo que ya usa
# vi_agent.DEFAULT_THINKING_LEVEL en producción) es el más bajo que esta familia de modelo soporta
# -no hay margen para bajar más el thinking level sin cambiar de modelo.
GATE_MAX_TOOL_CALLS_PER_QUESTION = 8
GATE_THINKING_LEVEL = types.ThinkingLevel.LOW
UPDATES_LOG_DIR = PROJECT_ROOT / ".runtime" / "data_map_updates"

VERSION_SUFFIX_RE = re.compile(r"\s+V(\d+)\.yaml$", re.IGNORECASE)


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 1. Detección de cambios de prompt (Langfuse, label production)
# --------------------------------------------------------------------------


@dataclass
class RulebookChange:
    key: str
    business_scope: str
    old_version: int | None
    new_version: int
    old_text: str | None
    new_text: str


def detect_rulebook_changes(client_config: ClientConfig) -> list[RulebookChange]:
    """Compara la versión cacheada de cada rulebook contra Langfuse (label production).

    Reutiliza BusinessRulesRepository, que ya cachea rules_version + criteria_text
    en .runtime/business_rules/<client_id>/<key>.json -no hace falta un mecanismo
    de polling nuevo, sólo comparar antes/después de refrescar.

    Si un rulebook nunca se cacheó en disco (primera corrida, o
    fetch_current_prompts.py nunca se corrió para ese cliente), NO se trata como
    "cambió el prompt": sólo se establece la foto base para la próxima comparación.
    Tratar un cache vacío como cambio dispararía una regeneración cara e
    injustificada en el primer uso -exactamente lo que pasó al probar este script
    contra farma24, donde conversation_insights nunca se había cacheado en disco.
    """
    repository = BusinessRulesRepository(client_config, PROJECT_ROOT)
    changes: list[RulebookChange] = []
    for key in repository.available_rulebooks():
        old_payload = repository._read_cache(key)  # snapshot previo, si existe
        new_payload = json.loads(repository.get(key, refresh=True))  # persiste la foto base si no había
        if old_payload is None:
            continue  # primera vez que se ve este rulebook: se establece línea de base, no es un "cambio"
        old_version = old_payload["rules_version"]
        new_version = new_payload["rules_version"]
        if old_version == new_version:
            continue
        changes.append(
            RulebookChange(
                key=key,
                business_scope=new_payload["business_scope"],
                old_version=old_version,
                new_version=new_version,
                old_text=old_payload["criteria_text"],
                new_text=new_payload["criteria_text"],
            )
        )
    return changes


def detect_pending_changes(
    client_config: ClientConfig, store, client_folder: str
) -> tuple[list[RulebookChange], dict[str, int | str]]:
    """Cambios de prompt SIN PROCESAR de un cliente, y la versión actual de cada rulebook.

    Compara la versión de Langfuse contra las versiones YA PROCESADAS del almacén (no contra la foto que usa el agente para responder,
    que se refresca apenas se mira). Así un cambio cuyo Data Map no se promovió sigue pendiente y se reintenta. Primera vez que se ve
    un rulebook: si el agente ya tenía una foto vieja, esa es la línea de base; si no, se siembra la actual (no es un "cambio")."""
    repository = BusinessRulesRepository(client_config, PROJECT_ROOT)
    processed = store.processed_versions(client_folder)
    pending = store.pending(client_folder)
    changes: list[RulebookChange] = []
    current: dict[str, int | str] = {}
    for key in repository.available_rulebooks():
        old_payload = repository._read_cache(key)  # lo que el agente venía usando, antes de refrescar
        new_payload = json.loads(repository.get(key, refresh=True))
        new_version = new_payload["rules_version"]  # int en Langfuse; la cadena "local" en los rulebooks del repo
        current[key] = new_version
        baseline = processed.get(key)
        if baseline is None:
            if old_payload is None:
                continue  # primera vez que se ve: línea de base, no es un cambio
            baseline = old_payload["rules_version"]
        if new_version == baseline:
            continue
        remembered = pending.get(key) or {}
        if old_payload is not None and old_payload["rules_version"] == baseline:
            old_text = old_payload["criteria_text"]
        elif remembered.get("old_version") == baseline:
            old_text = remembered.get("old_text")  # un reintento: el agente ya refrescó su foto, el texto viejo quedó guardado
        else:
            old_text = None
        changes.append(RulebookChange(key=key, business_scope=new_payload["business_scope"], old_version=baseline,
                                      new_version=new_version, old_text=old_text, new_text=new_payload["criteria_text"]))
    if changes:
        now = time.time()
        store.put_pending(client_folder, {
            c.key: {"old_version": c.old_version, "new_version": c.new_version, "old_text": c.old_text,
                    "first_seen_at": (pending.get(c.key) or {}).get("first_seen_at")
                    if (pending.get(c.key) or {}).get("new_version") == c.new_version and (pending.get(c.key) or {}).get("first_seen_at")
                    else now} for c in changes})
    return changes, current


def _squash(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\W_]+", "", plain)


def is_cosmetic_change(change: RulebookChange) -> bool:
    """True si el texto nuevo sólo difiere del viejo en espacios, mayúsculas, tildes o puntuación. Sin texto viejo no se puede afirmar."""
    return change.old_text is not None and _squash(change.old_text) == _squash(change.new_text)


def settle_hours(override: float | None = None) -> float:
    if override is not None:
        return float(override)
    try:
        return float(os.environ.get(SETTLE_HOURS_ENV, SETTLE_HOURS_DEFAULT))
    except ValueError:
        return SETTLE_HOURS_DEFAULT


def regen_model_for_attempt(client_config: ClientConfig, attempt: int) -> str:
    """Modelo de la regeneración: el barato configurado en el primer intento y el del cliente en los siguientes."""
    cheap = (os.environ.get(REGEN_CHEAP_MODEL_ENV) or "").strip()
    return cheap if (cheap and attempt <= 1) else client_config.model


def _change_signature(changes: list[RulebookChange]) -> str:
    return ",".join(sorted(f"{c.key}@{c.new_version}" for c in changes))


def _unified_diff(change: RulebookChange) -> str:
    if change.old_text is None:
        return f"(rulebook nuevo, sin snapshot previo -version {change.new_version}-, texto completo abajo)\n{change.new_text}"
    diff_lines = difflib.unified_diff(
        change.old_text.splitlines(),
        change.new_text.splitlines(),
        fromfile=f"{change.key}_v{change.old_version}",
        tofile=f"{change.key}_v{change.new_version}",
        lineterm="",
    )
    diff_text = "\n".join(diff_lines)
    return diff_text or "(el texto cambió de versión en Langfuse pero el diff textual quedó vacío)"


# --------------------------------------------------------------------------
# 2. Regeneración del Data Map (Gemini + run_readonly_sql real)
# --------------------------------------------------------------------------


REGENERATION_SYSTEM_INSTRUCTION = """Sos el auditor automático de Data Maps de Vera Intelligence para {client_name}.

Tu trabajo es actualizar el Data Map interno de este cliente porque uno o más de sus prompts de
negocio (Langfuse, label production) subieron de versión. Está prohibido reescribir el Data Map
de memoria a partir del texto del prompt solamente: cada regla nueva o modificada que declares
tiene que estar VERIFICADA empíricamente contra Postgres QA con la herramienta run_readonly_sql
(GROUP BY, COUNT, comparación de enums, nulls, etc.) antes de que la escribas. Este es el mismo
método que ya se usó a mano en las versiones anteriores del Data Map (ver metadata.source_of_truth
y metadata.changes_from_v* del Data Map vigente que te paso abajo) -no es un método nuevo, es el
mismo, automatizado-.

REGLAS OBLIGATORIAS:
1. Sólo tocá las secciones del Data Map relacionadas con el diff de prompt que te paso (campos,
   reglas semánticas, routing, decision_flow afectados). Todo lo demás del Data Map vigente se
   copia EXACTO, carácter por carácter -no reformatees, no "mejores" texto que no cambió, no borres
   hallazgos empíricos (reliability_warning, data_quality_note, hallazgos_criticos_verificados_*)
   que el diff no contradice-.
2. Antes de escribir cualquier afirmación cuantitativa nueva (un enum, una tasa de NULL, una
   cardinalidad, una relación entre dos campos), corré al menos una consulta con run_readonly_sql
   que la verifique. Si no podés verificarla con SQL (ej. depende del contenido de una transcripción
   individual), decilo explícitamente en el texto, no la presentes como un hecho verificado.
3. Si una regla nueva del prompt entra en conflicto con un reliability_warning o hallazgo empírico
   ya documentado, no lo borres sin verificar primero si sigue siendo cierto contra datos reales.
4. Actualizá metadata.name, metadata.version y metadata.generated_at a la versión siguiente
   ({next_version_label}, {next_semver}, fecha {today}). Agregá metadata.changes_from_v{prev_version_number}
   explicando qué cambió y por qué, y un bloque metadata.hallazgos_criticos_verificados_{today} (aunque
   quede vacío o con "sin hallazgos nuevos verificados" si no encontraste nada distinto de lo que ya
   decía el Data Map) -mismo formato que las versiones anteriores-.
5. El único SQL permitido es un SELECT o WITH...SELECT de solo lectura sobre las fuentes ya
   autorizadas de este cliente, con el tenant exacto -la misma validación de sql_security.py que usa
   el agente en producción se aplica acá también, así que una consulta inválida simplemente falla-.
6. Tenés como máximo {max_tool_calls} llamadas a run_readonly_sql. Usalas con criterio: priorizá
   verificar lo que cambió, no reauditar el Data Map entero.

FORMATO DE RESPUESTA FINAL (exacto, sin texto antes ni después de los delimitadores):
===DATA_MAP_YAML===
<<contenido COMPLETO del nuevo archivo YAML, empezando en "metadata:">>
===END_DATA_MAP_YAML===
===CHANGELOG===
<<2-6 líneas en español explicando qué cambió y qué verificaste, para el registro auditable>>
===END_CHANGELOG===

--- Diff de prompt(s) que motivó esta actualización ---
{prompt_diff}
--- fin del diff ---

--- Data Map vigente de {client_name} (versión {prev_version_label}) ---
{current_data_map}
--- fin del Data Map vigente ---
"""


@dataclass
class RegenerationResult:
    candidate_path: Path | None = None
    changelog: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None


def _next_data_map_path(current_path: Path) -> tuple[Path, str, str]:
    """Devuelve (ruta_nueva, etiqueta_version 'V4', semver '4.0.0')."""
    match = VERSION_SUFFIX_RE.search(current_path.name)
    if not match:
        raise ValueError(
            f"No se pudo inferir el número de versión del nombre de archivo: {current_path.name}"
        )
    current_number = int(match.group(1))
    stem_prefix = current_path.name[: match.start()]
    existing = [dms.version_number(f.name) for f in current_path.parent.glob("*.yaml") if f.name.startswith(stem_prefix)]
    next_number = max([current_number] + [v for v in existing if v is not None]) + 1
    stem_without_version = current_path.name[: match.start()]
    next_name = f"{stem_without_version} V{next_number}.yaml"
    return current_path.with_name(next_name), f"V{next_number}", f"{next_number}.0.0"


def _extract_delimited(raw_answer: str) -> tuple[str, str]:
    yaml_match = re.search(
        r"===DATA_MAP_YAML===\s*(.*?)\s*===END_DATA_MAP_YAML===", raw_answer, re.DOTALL
    )
    changelog_match = re.search(
        r"===CHANGELOG===\s*(.*?)\s*===END_CHANGELOG===", raw_answer, re.DOTALL
    )
    if not yaml_match or not changelog_match:
        raise ValueError(
            "La respuesta de Gemini no siguió el formato de delimitadores esperado "
            "(===DATA_MAP_YAML===/===CHANGELOG===)."
        )
    return yaml_match.group(1).strip(), changelog_match.group(1).strip()


def _parse_candidate(raw_answer: str) -> tuple[str, str]:
    """(yaml, changelog) de la respuesta de Gemini; ValueError/YAMLError si no tiene el formato o la forma esperada."""
    data_map_yaml, changelog = _extract_delimited(raw_answer)
    parsed = yaml.safe_load(data_map_yaml)
    if not isinstance(parsed, dict) or "metadata" not in parsed or "sources" not in parsed:
        raise ValueError("El YAML generado no tiene la forma esperada (falta metadata o sources).")
    return data_map_yaml, changelog


def gate_feedback(detail: dict) -> str:
    """Por qué el gate rechazó un candidato, en lenguaje que el regenerador pueda usar en el intento siguiente."""
    lines: list[str] = []
    for problem in detail.get("gate_structural_problems") or []:
        lines.append(f"- Estructura: {problem}.")
    for q in detail.get("gate_detail") or []:
        if not isinstance(q, dict):
            continue
        if q.get("dropped_fields"):
            lines.append(f"- La pregunta {q.get('id')} usa campos que el mapa vigente declaraba y el candidato perdió: {'; '.join(q['dropped_fields'])}. Conservalos.")
        if q.get("regression"):
            lines.append(f"- Con tu mapa, la respuesta a {q.get('id')} dejó de incluir {', '.join(q.get('missing_numbers') or [])} "
                         f"(el mapa vigente sí lo incluía). Respuesta obtenida: {(q.get('answer') or '')[:300]!r}. "
                         "Revisá qué cambiaste en los campos o reglas que esa pregunta usa.")
        if q.get("error"):
            lines.append(f"- Error al responder {q.get('id')}: {q['error']}")
    for item in detail.get("column_drift") or []:
        lines.append(f"- Deriva de columnas: {item}")
    return "\n".join(lines)


def _repair_message(exc: Exception) -> str:
    return (f"Tu respuesta no se pudo usar: {exc}. Devolvé de nuevo el YAML COMPLETO corregido y el changelog, con exactamente los "
            "mismos delimitadores (===DATA_MAP_YAML=== ... ===END_DATA_MAP_YAML=== y ===CHANGELOG=== ... ===END_CHANGELOG===), "
            "sin llamar más herramientas y sin texto fuera de los delimitadores. Cuidado con los ':' dentro de valores de texto: "
            "poné entre comillas los valores que los contengan.")


def regenerate_data_map(client_config: ClientConfig, changes: list[RulebookChange], *, store=None,
                        client_folder: str | None = None, model: str | None = None,
                        feedback: str | None = None) -> RegenerationResult:
    if not changes:
        return RegenerationResult()

    current_path = client_config.data_map_path
    next_path, next_version_label, next_semver = _next_data_map_path(current_path)
    prev_match = VERSION_SUFFIX_RE.search(current_path.name)
    prev_version_number = prev_match.group(1) if prev_match else "?"
    prev_version_label = f"V{prev_version_number}"

    prompt_diff = "\n\n".join(
        f"### {change.business_scope} ({change.key}), version {change.old_version} -> {change.new_version}\n"
        + _unified_diff(change)
        for change in changes
    )

    system_instruction = REGENERATION_SYSTEM_INSTRUCTION.format(
        client_name=client_config.display_name,
        next_version_label=next_version_label,
        next_semver=next_semver,
        prev_version_number=prev_version_number,
        prev_version_label=prev_version_label,
        today=datetime.now(timezone.utc).date().isoformat(),
        max_tool_calls=MAX_REGENERATION_TOOL_CALLS,
        prompt_diff=prompt_diff,
        current_data_map=current_path.read_text(encoding="utf-8"),
    )

    # http_options.timeout (2026-09-16): mismo fix que vi_agent._GENAI_HTTP_TIMEOUT_MS/
    # vector_search._GENAI_HTTP_TIMEOUT_MS -sin esto, un cuelgue de red silencioso puede dejar
    # esta llamada esperando para siempre en vez de fallar y poder reintentarse.
    client = vi_agent.genai.Client(
        api_key=vi_agent.os.environ["VERA_AI_API_KEY"],
        http_options=types.HttpOptions(timeout=vi_agent._GENAI_HTTP_TIMEOUT_MS),
    )
    chat = client.chats.create(
        model=model or client_config.model,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[vi_agent.run_readonly_sql],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )

    tool_calls_log: list[dict] = []
    message: object = (
        "Actualizá el Data Map siguiendo exactamente las reglas e instrucciones del system prompt."
        + (f"\n\nUn intento anterior fue RECHAZADO por el control automático. Corregí exactamente eso, sin tocar lo demás:\n{feedback}"
           if feedback else "")
    )
    tool_call_count = 0

    repairs_left = REGENERATION_REPAIRS
    for _ in range(MAX_REGENERATION_TOOL_CALLS + 5 + REGENERATION_REPAIRS):
        response = vi_agent._send_message_with_retry(chat, message, debug=False)
        function_calls = response.function_calls or []
        if not function_calls:
            raw_answer = (response.text or "").strip()
            try:
                data_map_yaml, changelog = _parse_candidate(raw_answer)
            except (ValueError, yaml.YAMLError) as exc:
                if repairs_left > 0:
                    # Visto en el historial (roberts_alto, 16/9): un YAML con un ':' sin comillas tiró la regeneración entera. Se le
                    # devuelve el error a Gemini para que lo corrija en la misma conversación, en vez de abandonar.
                    repairs_left -= 1
                    message = _repair_message(exc)
                    continue
                return RegenerationResult(
                    tool_calls=tool_calls_log,
                    error=f"Respuesta de Gemini inválida, no se escribió ningún archivo: {exc}",
                )
            if store is not None and client_folder:
                next_path = store.put_data_map(client_folder, next_path.name, data_map_yaml)
            else:
                next_path.write_text(data_map_yaml, encoding="utf-8")
            return RegenerationResult(
                candidate_path=next_path,
                changelog=changelog,
                tool_calls=tool_calls_log,
            )

        if tool_call_count + len(function_calls) > MAX_REGENERATION_TOOL_CALLS:
            return RegenerationResult(
                tool_calls=tool_calls_log,
                error="Se agotó el límite de llamadas a run_readonly_sql antes de terminar.",
            )

        function_responses = []
        for call in function_calls:
            tool_call_count += 1
            try:
                if call.name != "run_readonly_sql":
                    raise ValueError("Herramienta no autorizada para regeneración.")
                result = vi_agent.run_readonly_sql(**call.args)
                payload = {"result": vi_agent._tool_response_value(result)}
                error = None
            except Exception as exc:  # noqa: BLE001
                result = None
                error = str(exc)
                payload = {"error": error}
            tool_calls_log.append({"name": call.name, "args": dict(call.args), "result": result, "error": error})
            function_responses.append(types.Part.from_function_response(name=call.name, response=payload))
        message = function_responses

    return RegenerationResult(
        tool_calls=tool_calls_log, error="Se agotaron los turnos sin una respuesta final."
    )


# --------------------------------------------------------------------------
# 3. Gate automático: diferencial (viejo vs. candidato, en vivo) + SQL, sin
#    juicio de modelo y sin comparar contra un numero congelado.
# --------------------------------------------------------------------------
#
# NOTA IMPORTANTE (encontrada en la primera corrida real end-to-end, farma24,
# 2026-09-02): comparar contra `respuesta_esperada` (congelado al armar el
# banco) NO SIRVE como gate en una base que crece a diario -Postgres QA de
# Farma24 ingiere ~650-700 filas nuevas por día, así que un conteo absoluto
# como "247196 conversaciones analizables" queda desactualizado en horas, no
# en meses-. La primera prueba real de este script falló 19 de 20 preguntas
# comparando contra numeros congelados, con el agente respondiendo
# correctamente el numero REAL y actual (247556, no 247196). Un gate así
# fallaría todos los dias sin importar si el Data Map esta bien o mal, lo
# cual no es un gate, es un bloqueo permanente. La solución: correr cada
# pregunta dos veces, EN EL MISMO MOMENTO, una vez contra el Data Map
# VIGENTE (Vn) y otra contra el CANDIDATO (Vn+1), y comparar esas dos
# respuestas entre sí. Como ambas corridas ven la misma foto de Postgres, la
# diferencia entre ambas refleja el cambio del Data Map, no el crecimiento
# natural de la base.


# Extracción/comparación de números (2026-09-14): movida a numeric_text.py para poder reusarla
# también en vi_agent.py (ver docstring de ese módulo para el motivo -evita un import circular).
# Comportamiento idéntico; se reimportan acá con los mismos nombres que ya usaba este archivo para
# no romper nada que los referencie.
from numeric_text import (  # noqa: E402
    close_enough as _close_enough,
    is_float as _is_float,
    numbers_in as _numbers,
)


# Tolerancia a variabilidad normal del modelo: dos llamadas independientes a Gemini,
# aunque vean exactamente los mismos datos y el mismo Data Map, no redactan la
# respuesta palabra por palabra -pueden omitir un dato secundario de contexto sin que
# eso sea una regresión-. Verificado en la primera corrida real de q05: 12 de 13
# números coincidieron exacto: el único "faltante" era una cifra de contexto auxiliar
# que la respuesta nueva no repitió, con una estructura de respuesta distinta (tabla
# en vez de prosa) pero el mismo análisis. Exigir 0 faltantes rechazaría
# actualizaciones buenas por este ruido; permitir demasiados dejaría pasar una
# regresión real. 1 número faltante por pregunta es el punto elegido.
MAX_MISSING_NUMBERS_PER_QUESTION = 1


# --------------------------------------------------------------------------
# 2.5 Lenguaje relativo en el banco dorado: linter + auto-relajación de tolerancia
# --------------------------------------------------------------------------
#
# HALLAZGO REAL (high_life_alto, 2026-09-09, ver README.md > "Playbook: qué hacer
# cuando el poller devuelve gate_fallo_no_promovido"): una pregunta redactada como
# "¿...en el checklist vigente?" o "...conversaciones recientes?" no tiene un
# significado fijo -depende de qué rulebook esté activo el día que se lee-. Cuando
# ese rulebook sube de versión (exactamente el evento que dispara este script), la
# respuesta vieja y la candidata legítimamente citan períodos/campos distintos, y el
# gate diferencial las rechaza aunque la candidata esté bien. Esto ya pasó DOS veces
# seguidas en high_life_alto (viejo->v8, v8->v10) y en ambas requirió recalibrar el
# banco a mano después de que el gate fallara. En vez de esperar a que vuelva a pasar
# en este u otro cliente, se detecta el patrón de antemano y se compensa automático.

RELATIVE_LANGUAGE_KEYWORDS = ("vigente", "vigentes", "reciente", "recientes", "actualmente")

# Tolerancia aplicada SÓLO cuando una pregunta trampa con lenguaje relativo no tiene ya
# una calibración manual explícita (`max_missing_numbers` en el banco). No es tan laxa
# como `omit_from_numeric_gate` (sigue exigiendo que los números no faltantes se acerquen
# lo suficiente) -sólo reconoce que una migración de rulebook real produce más
# reencuadre legítimo del esperado por el default estricto. Calibrado sobre los 3 casos
# reales de high_life_alto V2 (4, 7 y 7 números "faltantes" que resultaron ser
# reencuadre correcto, no una regresión, ver metadata.changes_from_v2 de
# VI Data Map High Life V3.yaml).
AUTO_RELAXED_MAX_MISSING_NUMBERS = 5


def _contains_relative_language(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in RELATIVE_LANGUAGE_KEYWORDS)


def lint_golden_bank_relative_language(bank: dict) -> list[str]:
    """IDs de preguntas cuyo campo ``pregunta`` depende de lenguaje relativo al rubric de
    hoy ("vigente", "reciente(s)", "actualmente"). No falla nada por sí solo -es una
    señal temprana, pensada para aparecer en CADA corrida del poller (incluso las que
    terminan en "sin_cambios", el chequeo es sólo texto, no cuesta Gemini ni Postgres)
    para que alguien las reescriba ancladas a una fecha o versión de rubric fija ANTES
    de que la próxima migración las vuelva ambiguas -en vez de descubrirlo recién cuando
    el gate ya rechazó una candidata buena-.
    """
    return [
        item["id"]
        for item in bank.get("preguntas", [])
        if _contains_relative_language(item.get("pregunta", ""))
    ]


def _numbers_match(
    old: set[str], new: set[str], *, max_missing: int = MAX_MISSING_NUMBERS_PER_QUESTION
) -> tuple[bool, set[str], set[str]]:
    """Compara los números de la respuesta vieja vs. la candidata (con tolerancia de deriva natural).

    ``max_missing`` es calibrable por pregunta (ver "max_missing_numbers" en el banco dorado) para
    preguntas donde se verificó con datos reales que el modelo agrega/omite detalle opcional de
    forma consistente entre corridas válidas (ver README.md > "Actualización automática del Data
    Map", hallazgo 4). El default global sigue siendo estricto (1) para no tapar regresiones reales
    en preguntas que nunca se calibraron a mano.
    """

    def _covers(target: set[str], source: set[str]) -> set[str]:
        missing = set()
        for token in target:
            if token in source:
                continue
            try:
                value = float(token)
            except ValueError:
                missing.add(token)
                continue
            if not any(_close_enough(value, float(s)) for s in source if _is_float(s)):
                missing.add(token)
        return missing

    missing_in_new = _covers(old, new)  # números que daba la versión vieja y desaparecieron
    added_in_new = _covers(new, old)  # números nuevos que no estaban antes (informativo, no reprueba)
    passed = len(missing_in_new) <= max_missing
    return (passed, missing_in_new, added_in_new)


@dataclass
class GateQuestionResult:
    id: str
    sql_ok: bool
    sql_error: str | None
    numbers_ok: bool
    missing_numbers: list[str]  # números que la versión vieja daba y la candidata perdió (reprueba)
    added_numbers: list[str]  # números nuevos en la candidata, sin equivalente en la vieja (informativo)
    old_answer: str
    new_answer: str
    error: str | None = None
    auto_relaxed_tolerance: bool = False  # ver AUTO_RELAXED_MAX_MISSING_NUMBERS más arriba


@dataclass
class GateResult:
    passed: bool
    questions: list[GateQuestionResult]
    bank_size: int
    questions_evaluated: int
    skipped_over_cap: int


def _build_system_instruction(client_config: ClientConfig, data_map_text: str) -> str:
    # Corregido (2026-09-11): esta llamada quedó desactualizada respecto a vi_agent.py -nombraba
    # rulebook_keys/_build_rag_tools_section, que ya no existen (renombrados a
    # rulebook_options/_build_extra_tools_section cuando se generalizó el tools section para
    # búsqueda vectorial). run_gate() rompía con AttributeError en cuanto se invocaba; nadie lo
    # notó porque no corrió en esta sesión hasta ahora. _build_extra_tools_section() sigue
    # leyendo el CLIENT_CONFIG global del proceso, no client_config -asume que quien llama a
    # run_gate() ya corrió configure_client() para este mismo cliente antes (igual que antes).
    return vi_agent.SYSTEM_INSTRUCTION_TEMPLATE.format(
        client_name=client_config.display_name,
        tenant=client_config.tenant,
        rulebook_options=vi_agent._build_rulebook_options(client_config),
        extra_tools_section=vi_agent._build_extra_tools_section(),
        data_map=data_map_text,
    )


def run_gate(client_config: ClientConfig, candidate_data_map_path: Path, client_folder: str) -> GateResult:
    """``client_folder`` es el nombre real de carpeta bajo ``clientes/`` (el --client con el
    que se invocó este script), NO ``client_config.client_id``. Antes de que las carpetas
    llevaran un sufijo cosmético de grado de funcionamiento (_alto/_medio/_bajo, ver README.md),
    ambos valores coincidían siempre y este método usaba el segundo por comodidad -pero
    ``client_config.client_id`` es la identidad estable del cliente (vive en config.yaml, se usa
    para namespacing de cache y tracking) y NO cambia cuando la carpeta se renombra, así que
    resolver rutas de archivo con ese campo se rompe en cuanto folder != client_id. Usar siempre
    el nombre de carpeta real para construir rutas del filesystem."""
    bank_path = CLIENTS_ROOT / client_folder / "preguntas" / "preguntas_evaluacion.yaml"
    bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))
    all_questions = bank["preguntas"]
    questions = all_questions[:MAX_GOLDEN_QUESTIONS]
    skipped = max(0, len(all_questions) - MAX_GOLDEN_QUESTIONS)

    old_instruction = _build_system_instruction(
        client_config, client_config.data_map_path.read_text(encoding="utf-8")
    )
    new_instruction = _build_system_instruction(
        client_config, candidate_data_map_path.read_text(encoding="utf-8")
    )

    results: list[GateQuestionResult] = []
    for item in questions:
        sql_ok, sql_error = True, None
        if item.get("sql"):
            try:
                vi_agent.validate_readonly_sql(item["sql"], client_config)
            except Exception as exc:  # noqa: BLE001
                sql_ok, sql_error = False, str(exc)

        try:
            old_chat = vi_agent.build_chat(system_instruction=old_instruction, thinking_level=GATE_THINKING_LEVEL)
            old_answer = vi_agent.run_tool_loop(
                old_chat, item["pregunta"], max_tool_calls=GATE_MAX_TOOL_CALLS_PER_QUESTION, debug=False
            )
            new_chat = vi_agent.build_chat(system_instruction=new_instruction, thinking_level=GATE_THINKING_LEVEL)
            new_answer = vi_agent.run_tool_loop(
                new_chat, item["pregunta"], max_tool_calls=GATE_MAX_TOOL_CALLS_PER_QUESTION, debug=False
            )
            # El bloque ```vera-suggestions``` (SUGERENCIAS DE SEGUIMIENTO en
            # SYSTEM_INSTRUCTION_TEMPLATE, 2026-09-11) ahora es obligatorio en TODA respuesta, no
            # sólo el saludo -sin sacarlo antes de comparar números, preguntas de seguimiento con
            # fechas/cifras (texto libre del modelo, no determinístico) podrían generar un
            # "número faltante" que no tiene nada que ver con una regresión real del Data Map.
            old_answer, _ = vi_agent.extract_suggestion_blocks(old_answer)
            new_answer, _ = vi_agent.extract_suggestion_blocks(new_answer)

            auto_relaxed = False
            if "max_missing_numbers" in item:
                max_missing = item["max_missing_numbers"]
            elif item.get("es_trampa") and _contains_relative_language(item.get("pregunta", "")):
                # Ver AUTO_RELAXED_MAX_MISSING_NUMBERS: pregunta trampa sin calibración manual
                # explícita, pero cuyo texto depende de "vigente"/"reciente"/"actualmente" -exactamente
                # el patrón que rompió el gate de high_life_alto dos veces seguidas (2026-09-09).
                max_missing = AUTO_RELAXED_MAX_MISSING_NUMBERS
                auto_relaxed = True
            else:
                max_missing = MAX_MISSING_NUMBERS_PER_QUESTION
            numbers_ok, missing, added = _numbers_match(
                _numbers(old_answer), _numbers(new_answer), max_missing=max_missing
            )
            if item.get("omit_from_numeric_gate"):
                # Preguntas donde la respuesta correcta cambia de contenido entre corridas por
                # diseño (ej. "dame algunos ejemplos" -> el modelo elige distintas conversaciones
                # cada vez, ver README.md > "Nota de continuidad", hallazgo 4). Comparar números
                # ahí no mide una regresión real -sólo importa que el SQL siga siendo válido-.
                numbers_ok = True
            results.append(
                GateQuestionResult(
                    id=item["id"],
                    sql_ok=sql_ok,
                    sql_error=sql_error,
                    numbers_ok=numbers_ok,
                    missing_numbers=sorted(missing),
                    added_numbers=sorted(added),
                    old_answer=old_answer,
                    new_answer=new_answer,
                    auto_relaxed_tolerance=auto_relaxed,
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                GateQuestionResult(
                    id=item["id"],
                    sql_ok=sql_ok,
                    sql_error=sql_error,
                    numbers_ok=False,
                    missing_numbers=[],
                    added_numbers=[],
                    old_answer="",
                    new_answer="",
                    error=str(exc),
                )
            )

    passed = all(r.sql_ok and r.numbers_ok and r.error is None for r in results)
    return GateResult(
        passed=passed,
        questions=results,
        bank_size=len(all_questions),
        questions_evaluated=len(questions),
        skipped_over_cap=skipped,
    )


def run_gate_v2(client_config: ClientConfig, candidate_data_map_path: Path, client_folder: str, *, assume_all_affected: bool = False) -> dict:
    """Gate contra el SQL dorado (ver data_map_gate.py): estructura, campos, números de verdad y respuesta del candidato."""
    bank_path = CLIENTS_ROOT / client_folder / "preguntas" / "preguntas_evaluacion.yaml"
    bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))
    old_data_map = yaml.safe_load(client_config.data_map_path.read_text(encoding="utf-8")) or {}
    new_text = candidate_data_map_path.read_text(encoding="utf-8")
    new_data_map = yaml.safe_load(new_text) or {}
    new_instruction = _build_system_instruction(client_config, new_text)

    old_instruction = _build_system_instruction(client_config, client_config.data_map_path.read_text(encoding="utf-8"))

    def make_ask(instruction: str):
        def ask(question: str) -> str:
            chat = vi_agent.build_chat(system_instruction=instruction, thinking_level=GATE_THINKING_LEVEL)
            answer = vi_agent.run_tool_loop(chat, question, max_tool_calls=GATE_MAX_TOOL_CALLS_PER_QUESTION, debug=False)
            return vi_agent.extract_suggestion_blocks(answer)[0]
        return ask

    ask, baseline_ask = make_ask(new_instruction), make_ask(old_instruction)

    def run_sql(sql: str):
        vi_agent.validate_readonly_sql(sql, client_config)
        return vi_agent.run_readonly_sql(sql)

    # Lo que el Data Map vigente responde no cambia mientras el vigente no cambie: se mide una vez y se guarda (ahorra preguntas en cada
    # reintento y en cada corrida). Si el almacén falla, el gate sigue funcionando sin caché.
    store, map_hash, cache = None, hashlib.sha256(client_config.data_map_path.read_bytes()).hexdigest(), {}
    try:
        store = dms.get_store()
        cache = store.gate_baseline(client_folder, map_hash)
    except Exception:  # noqa: BLE001
        store = None
    report = data_map_gate.run_gate_v2(old_data_map=old_data_map, new_data_map=new_data_map, bank=bank, run_sql=run_sql, ask=ask,
                                       baseline_ask=baseline_ask, baseline_cache=cache, max_questions=MAX_GOLDEN_QUESTIONS,
                                       assume_all_affected=assume_all_affected)
    if store is not None and cache:
        try:
            store.set_gate_baseline(client_folder, map_hash, cache)
        except Exception:  # noqa: BLE001
            pass
    return report


# --------------------------------------------------------------------------
# 4. Promoción (config.yaml -> nueva versión) y registro auditable
# --------------------------------------------------------------------------


def promote(candidate_path: Path, client_folder: str) -> None:
    config_path = CLIENTS_ROOT / client_folder / "config.yaml"
    text = config_path.read_text(encoding="utf-8")
    relative_candidate = candidate_path.relative_to(PROJECT_ROOT).as_posix()
    # OJO: sin anclar el final con [ \t]* (no \s*) la regex se come la línea en blanco
    # siguiente -\s incluye el salto de línea, y con re.MULTILINE el motor lo consume de
    # todos modos buscando el próximo $-. Encontrado validando promote() en aislado
    # (2026-09-02): el archivo seguía siendo YAML válido pero perdía una línea en blanco
    # que no tenía nada que ver con el cambio.
    new_text = re.sub(
        r'^data_map:\s*".*"[ \t]*$',
        f'data_map: "{relative_candidate}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if new_text == text:
        raise ValueError("No se encontró la línea data_map: en config.yaml, no se promovió nada.")
    config_path.write_text(new_text, encoding="utf-8")


def _lint_bank_for_client(client_folder: str) -> list[str]:
    """Corre lint_golden_bank_relative_language sobre el banco dorado del cliente, tolerando que
    el archivo no exista todavía (clientes nuevos a medio armar) sin romper la corrida del poller
    por un chequeo que es puramente informativo."""
    bank_path = CLIENTS_ROOT / client_folder / "preguntas" / "preguntas_evaluacion.yaml"
    if not bank_path.exists():
        return []
    bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))
    return lint_golden_bank_relative_language(bank)


def _write_audit_log(client_id: str, payload: dict) -> Path:
    log_dir = UPDATES_LOG_DIR / client_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return log_path


# --------------------------------------------------------------------------
# Orquestador
# --------------------------------------------------------------------------


def run_for_client(client_id: str, *, dry_run: bool = False, gate: str = "v2", store=None, lock_owner: str | None = None,
                   run_attempts: int = RUN_ATTEMPTS, settle: float | None = None) -> dict:
    """``client_id`` acá es el nombre real de carpeta bajo ``clientes/`` (lo que llega por
    ``--client``, típicamente listado dinámicamente por el poller) -no necesariamente igual
    a ``ClientConfig.client_id`` (la identidad estable del cliente, ver `run_gate`).

    Con candado por cliente: si otro proceso ya está actualizando este cliente, devuelve `en_curso_por_otro_proceso` sin hacer nada.
    `lock_owner`: el dueño de un candado que ya tomó quien lanzó este proceso (ver data_map_refresh.py); se suelta al terminar."""
    store = store or dms.get_store()
    owner = lock_owner or dms.new_owner("poller")
    if lock_owner is None and not store.acquire_lock(client_id, owner, LOCK_TTL_SECONDS):
        return {"client_id": client_id, "status": "en_curso_por_otro_proceso"}
    try:
        summary = _run_locked(client_id, dry_run=dry_run, gate=gate, store=store, run_attempts=run_attempts, settle=settle)
        store.set_last_check(client_id)
        return summary
    finally:
        store.release_lock(client_id, owner)


def _run_locked(client_id: str, *, dry_run: bool, gate: str, store, run_attempts: int, settle: float | None = None) -> dict:
    client_folder = client_id
    vi_agent.configure_client(client_id)
    vi_agent.load_environment()
    client_config = vi_agent.CLIENT_CONFIG

    bank_warnings = _lint_bank_for_client(client_folder)

    def finish(summary: dict) -> dict:
        if bank_warnings:
            summary["golden_bank_relative_language_warnings"] = bank_warnings
        store.write_run(client_id, summary)
        return summary

    changes, current_versions = detect_pending_changes(client_config, store, client_folder)
    if not changes:
        store.set_processed(client_id, current_versions)  # siembra la línea de base y deja al cliente al día
        return finish({"client_id": client_id, "status": "sin_cambios"})

    cosmetic = [c for c in changes if is_cosmetic_change(c)]
    if len(cosmetic) == len(changes):
        # Todos los cambios son de forma (espacios, mayúsculas, tildes, puntuación): ninguna regla cambió, no hay nada que regenerar.
        store.set_processed(client_id, current_versions)
        store.clear_pending(client_id)
        return finish({"client_id": client_id, "status": "cambio_cosmetico_sin_regenerar",
                       "changes": [{"key": c.key, "old_version": c.old_version, "new_version": c.new_version} for c in changes]})
    changes = [c for c in changes if c not in cosmetic]

    signature = _change_signature(changes)
    changes_summary = [{"key": c.key, "old_version": c.old_version, "new_version": c.new_version} for c in changes]
    wait_hours = settle_hours(settle)
    if wait_hours > 0 and not dry_run:
        seen = [float((store.pending(client_id).get(c.key) or {}).get("first_seen_at") or 0) for c in changes]
        ready_at = (max(seen) if seen else 0) + wait_hours * 3600
        if time.time() < ready_at:
            return finish({"client_id": client_id, "status": "esperando_estabilidad", "changes": changes_summary,
                           "regenera_a_partir_de": datetime.fromtimestamp(ready_at, timezone.utc).isoformat(),
                           "note": f"El prompt cambió hace menos de {wait_hours:g} h: se espera por si lo siguen editando (ahorra regenerar varias veces)."})
    if store.attempts(client_id, signature) >= MAX_ATTEMPTS_PER_VERSION:
        return finish({"client_id": client_id, "status": "reintentos_agotados", "changes": changes_summary,
                       "attempts": store.attempts(client_id, signature),
                       "note": "Se agotaron los intentos automáticos para este cambio de prompt: hace falta revisión humana."})

    failure: dict = {}
    feedback: str | None = None
    for _ in range(max(1, run_attempts)):
        attempt = store.add_attempt(client_id, signature)
        model = regen_model_for_attempt(client_config, attempt)
        regeneration = regenerate_data_map(client_config, changes, store=store, client_folder=client_folder, model=model, feedback=feedback)
        if regeneration.error:
            failure = {"status": "error_regeneracion", "changes": [c.key for c in changes], "error": regeneration.error,
                       "tool_calls": len(regeneration.tool_calls)}
        elif dry_run:
            return finish({"client_id": client_id, "status": "candidata_generada_sin_gate_dry_run",
                           "candidate_path": str(regeneration.candidate_path), "changelog": regeneration.changelog})
        else:
            outcome = _evaluate_candidate(client_config, regeneration, client_folder, gate)
            if outcome["passed"]:
                summary = {"client_id": client_id, "changes": changes_summary, "candidate_path": str(regeneration.candidate_path),
                           "changelog": regeneration.changelog, "attempt": attempt, "regeneration_model": model,
                           "version_anterior": dms.version_number(client_config.data_map_path),
                           "version_nueva": dms.version_number(regeneration.candidate_path), **outcome["detail"]}
                try:
                    clients_root = getattr(store, "clients_root", CLIENTS_ROOT)
                    old_dm = yaml.safe_load(client_config.data_map_path.read_text(encoding="utf-8")) or {}
                    new_dm = yaml.safe_load(regeneration.candidate_path.read_text(encoding="utf-8")) or {}
                    entry = data_map_log.build_entry(
                        tipo="promovido", client=client_id, old_version=summary["version_anterior"], new_version=summary["version_nueva"],
                        old_file=client_config.data_map_path.name, new_file=regeneration.candidate_path.name, prompts=changes_summary,
                        change_summary=data_map_log.summarize_change(old_dm, new_dm), changelog=regeneration.changelog or "",
                        gate={"gate": outcome["detail"].get("gate"), "passed": True,
                              "questions_evaluated": outcome["detail"].get("gate_questions_evaluated"),
                              "llm_questions_checked": outcome["detail"].get("gate_llm_questions_checked")},
                        model=model, attempts=attempt)
                    md_path, _ = data_map_log.append_entry(clients_root, client_folder, entry)
                    summary["registro_de_cambios"] = str(md_path)
                except Exception as exc:  # noqa: BLE001 - el registro no debe impedir una promoción ya validada
                    summary["registro_de_cambios_error"] = str(exc)[:200]
                store.promote(client_folder, regeneration.candidate_path, rules_versions=current_versions,
                              changelog=regeneration.changelog or "")
                try:
                    promote(regeneration.candidate_path, client_folder)  # config.yaml: ruta de repo (best-effort si no es escribible)
                    summary["config_yaml_actualizado"] = True
                except Exception as exc:  # noqa: BLE001
                    summary["config_yaml_actualizado"] = False
                    summary["config_yaml_error"] = str(exc)[:200]
                summary["status"] = "promovido"
                return finish(summary)
            failure = {"client_id": client_id, "changes": changes_summary, "candidate_path": str(regeneration.candidate_path),
                       "changelog": regeneration.changelog, "attempt": attempt, "status": outcome["status"], **outcome["detail"]}
            feedback = gate_feedback(outcome["detail"]) or None
        if store.attempts(client_id, signature) >= MAX_ATTEMPTS_PER_VERSION:
            break

    failure.setdefault("client_id", client_id)
    failure["attempts"] = store.attempts(client_id, signature)
    if failure["attempts"] < MAX_ATTEMPTS_PER_VERSION:
        # Todavía quedan intentos: el cambio sigue pendiente y se reintenta solo en la próxima corrida (no requiere a nadie).
        failure["status_del_intento"] = failure["status"]
        failure["status"] = "reintento_pendiente"
    return finish(failure)


def _evaluate_candidate(client_config: ClientConfig, regeneration: RegenerationResult, client_folder: str, gate_mode: str) -> dict:
    """Gate + guardia de deriva de columnas. Devuelve {'passed', 'status' (si falló), 'detail' (para el registro)}."""
    detail: dict = {}
    if gate_mode == "legacy":
        gate = run_gate(client_config, regeneration.candidate_path, client_folder)
        passed = gate.passed
        detail.update({
            "gate": "legacy", "gate_bank_size": gate.bank_size, "gate_questions_evaluated": gate.questions_evaluated,
            "gate_skipped_over_cap": gate.skipped_over_cap, "gate_passed": gate.passed,
            "gate_detail": [{
                "id": q.id, "sql_ok": q.sql_ok, "sql_error": q.sql_error, "numbers_ok": q.numbers_ok,
                "missing_numbers": q.missing_numbers, "added_numbers": q.added_numbers, "old_answer": q.old_answer,
                "new_answer": q.new_answer, "error": q.error, "auto_relaxed_tolerance": q.auto_relaxed_tolerance,
            } for q in gate.questions]})
    else:
        report = run_gate_v2(client_config, regeneration.candidate_path, client_folder)
        passed = report["passed"]
        detail.update({
            "gate": "v2", "gate_bank_size": report["bank_size"], "gate_questions_evaluated": report["questions_evaluated"],
            "gate_llm_questions_checked": report["llm_questions_checked"], "gate_passed": passed,
            "gate_structural_problems": report["structural_problems"], "gate_detail": report["questions"],
            "gate_questions_affected": report.get("questions_affected"), "gate_footprint": report.get("footprint"),
            "gate_bank_problems": report.get("bank_problems") or []})
    if not passed:
        return {"passed": False, "status": "gate_fallo_no_promovido", "detail": detail}
    # Guardia de deriva de columnas (2026-09-24, ver data_map_column_drift.py): un candidato que declara campos inexistentes en la
    # vista real no se promueve aunque el gate pase.
    try:
        from data_map_column_drift import check_client

        drift = check_client(client_folder, regeneration.candidate_path)
    except Exception as exc:  # noqa: BLE001 - la guardia no debe romper la corrida si la base no responde
        detail["column_drift_check_error"] = str(exc)[:200]
        drift = []
    if drift:
        detail["column_drift"] = drift
        return {"passed": False, "status": "deriva_de_columnas_no_promovido", "detail": detail}
    return {"passed": True, "status": "promovido", "detail": detail}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detecta cambios de prompt en Langfuse, regenera el Data Map del cliente con "
            "verificación empírica contra Postgres, corre el gate automático (golden bank, "
            f"máximo {MAX_GOLDEN_QUESTIONS} preguntas) y promueve sólo si pasa completo."
        )
    )
    parser.add_argument("--client", required=True, help="client_id (carpeta bajo clientes/).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Genera la versión candidata pero no corre el gate ni promueve (para inspección manual).",
    )
    parser.add_argument("--gate", choices=("v2", "legacy"), default="v2",
                        help="v2 (por defecto): contra el SQL dorado, segundos a pocos minutos. legacy: respuesta vieja contra nueva, 20-25 min.")
    parser.add_argument("--attempts", type=int, default=RUN_ATTEMPTS, help="Intentos (regenerar + gate) dentro de esta corrida.")
    parser.add_argument("--settle-hours", type=float, default=None,
                        help=f"Horas que debe llevar sin cambiar un prompt antes de regenerar (por defecto {SETTLE_HOURS_DEFAULT:g}; 0 lo desactiva).")
    parser.add_argument("--lock-owner", default=None,
                        help="Uso interno: dueño de un candado ya tomado por quien lanzó este proceso (data_map_refresh.py).")
    return parser.parse_args()


NEEDS_HUMAN_STATUSES = {"gate_fallo_no_promovido", "error_regeneracion", "deriva_de_columnas_no_promovido", "reintentos_agotados"}


def main() -> None:
    args = parse_args()
    summary = run_for_client(args.client, dry_run=args.dry_run, gate=args.gate, lock_owner=args.lock_owner,
                             run_attempts=args.attempts, settle=args.settle_hours)
    status = summary.get("status")
    if status in NEEDS_HUMAN_STATUSES:
        # Banner explícito, imposible de pasar por alto en un log o en la salida del poller:
        # hubo un cambio de prompt real que el gate NO pudo validar solo. Alguien tiene que
        # mirarlo -config.yaml sigue en la versión anterior, el cliente no se vio afectado,
        # pero la actualización quedó pendiente de revisión humana, no completada.
        print(
            "\n"
            "=" * 70 + "\n"
            f"ATENCIÓN: cliente {args.client!r} tuvo un cambio de prompt real que el gate\n"
            f"automático NO pudo validar (status={status!r}). config.yaml sigue en la versión\n"
            "anterior -el cliente activo no se vio afectado-, pero la actualización candidata\n"
            "quedó sin promover y requiere revisión humana. Ver 'candidate_path' y 'gate_detail'\n"
            "en el JSON de abajo, y el registro en .runtime/data_map_updates/<client_id>/.\n"
            + "=" * 70 + "\n",
            file=sys.stderr,
            flush=True,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    # Código de salida para un programador de tareas o un monitor: distinto de cero si alguien tiene que mirar este cliente.
    sys.exit(1 if status in NEEDS_HUMAN_STATUSES else 0)


if __name__ == "__main__":
    main()
