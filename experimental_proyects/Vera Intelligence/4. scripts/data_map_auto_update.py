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
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

import vi_agent
from business_rules import BusinessRulesRepository
from client_config import ClientConfig
from google.genai import types


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"

MAX_GOLDEN_QUESTIONS = 10
MAX_REGENERATION_TOOL_CALLS = 25

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
    changed: bool
    candidate_path: Path | None = None
    changelog: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    raw_answer: str | None = None
    error: str | None = None


def _next_data_map_path(current_path: Path) -> tuple[Path, str, str]:
    """Devuelve (ruta_nueva, etiqueta_version 'V4', semver '4.0.0')."""
    match = VERSION_SUFFIX_RE.search(current_path.name)
    if not match:
        raise ValueError(
            f"No se pudo inferir el número de versión del nombre de archivo: {current_path.name}"
        )
    current_number = int(match.group(1))
    next_number = current_number + 1
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


def regenerate_data_map(client_config: ClientConfig, changes: list[RulebookChange]) -> RegenerationResult:
    if not changes:
        return RegenerationResult(changed=False)

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
        model=client_config.model,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[vi_agent.run_readonly_sql],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )

    tool_calls_log: list[dict] = []
    message: object = (
        "Actualizá el Data Map siguiendo exactamente las reglas e instrucciones del system prompt."
    )
    tool_call_count = 0

    for _ in range(MAX_REGENERATION_TOOL_CALLS + 5):
        response = vi_agent._send_message_with_retry(chat, message, debug=False)
        function_calls = response.function_calls or []
        if not function_calls:
            raw_answer = (response.text or "").strip()
            try:
                data_map_yaml, changelog = _extract_delimited(raw_answer)
                parsed = yaml.safe_load(data_map_yaml)
                if not isinstance(parsed, dict) or "metadata" not in parsed or "sources" not in parsed:
                    raise ValueError(
                        "El YAML generado no tiene la forma esperada (falta metadata o sources)."
                    )
            except (ValueError, yaml.YAMLError) as exc:
                return RegenerationResult(
                    changed=True,
                    tool_calls=tool_calls_log,
                    raw_answer=raw_answer,
                    error=f"Respuesta de Gemini inválida, no se escribió ningún archivo: {exc}",
                )
            next_path.write_text(data_map_yaml, encoding="utf-8")
            return RegenerationResult(
                changed=True,
                candidate_path=next_path,
                changelog=changelog,
                tool_calls=tool_calls_log,
                raw_answer=raw_answer,
            )

        if tool_call_count + len(function_calls) > MAX_REGENERATION_TOOL_CALLS:
            return RegenerationResult(
                changed=True,
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
        changed=True, tool_calls=tool_calls_log, error="Se agotaron los turnos sin una respuesta final."
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
    NUMBER_SPAN_RE,
    close_enough as _close_enough,
    is_float as _is_float,
    normalize_number as _normalize_number,
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


# --------------------------------------------------------------------------
# 4. Promoción (config.yaml -> nueva versión) y registro auditable
# --------------------------------------------------------------------------


def promote(client_config: ClientConfig, candidate_path: Path, client_folder: str) -> None:
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


def run_for_client(client_id: str, *, dry_run: bool = False) -> dict:
    """``client_id`` acá es el nombre real de carpeta bajo ``clientes/`` (lo que llega por
    ``--client``, típicamente listado dinámicamente por el poller) -no necesariamente igual
    a ``ClientConfig.client_id`` (la identidad estable del cliente, ver `run_gate`)."""
    client_folder = client_id
    vi_agent.configure_client(client_id)
    vi_agent.load_environment()
    client_config = vi_agent.CLIENT_CONFIG

    bank_warnings = _lint_bank_for_client(client_folder)

    changes = detect_rulebook_changes(client_config)
    if not changes:
        summary = {"client_id": client_id, "status": "sin_cambios"}
        if bank_warnings:
            summary["golden_bank_relative_language_warnings"] = bank_warnings
        _write_audit_log(client_id, summary)
        return summary

    regeneration = regenerate_data_map(client_config, changes)
    if regeneration.error:
        summary = {
            "client_id": client_id,
            "status": "error_regeneracion",
            "changes": [c.key for c in changes],
            "error": regeneration.error,
            "tool_calls": len(regeneration.tool_calls),
        }
        if bank_warnings:
            summary["golden_bank_relative_language_warnings"] = bank_warnings
        _write_audit_log(client_id, summary)
        return summary

    if dry_run:
        summary = {
            "client_id": client_id,
            "status": "candidata_generada_sin_gate_dry_run",
            "candidate_path": str(regeneration.candidate_path),
            "changelog": regeneration.changelog,
        }
        if bank_warnings:
            summary["golden_bank_relative_language_warnings"] = bank_warnings
        _write_audit_log(client_id, summary)
        return summary

    gate = run_gate(client_config, regeneration.candidate_path, client_folder)
    summary = {
        "client_id": client_id,
        "changes": [{"key": c.key, "old_version": c.old_version, "new_version": c.new_version} for c in changes],
        "candidate_path": str(regeneration.candidate_path),
        "changelog": regeneration.changelog,
        "gate_bank_size": gate.bank_size,
        "gate_questions_evaluated": gate.questions_evaluated,
        "gate_skipped_over_cap": gate.skipped_over_cap,
        "gate_passed": gate.passed,
        "gate_detail": [
            {
                "id": q.id,
                "sql_ok": q.sql_ok,
                "sql_error": q.sql_error,
                "numbers_ok": q.numbers_ok,
                "missing_numbers": q.missing_numbers,
                "added_numbers": q.added_numbers,
                "old_answer": q.old_answer,
                "new_answer": q.new_answer,
                "error": q.error,
                "auto_relaxed_tolerance": q.auto_relaxed_tolerance,
            }
            for q in gate.questions
        ],
    }
    if bank_warnings:
        summary["golden_bank_relative_language_warnings"] = bank_warnings

    if gate.passed:
        promote(client_config, regeneration.candidate_path, client_folder)
        summary["status"] = "promovido"
    else:
        summary["status"] = "gate_fallo_no_promovido"

    _write_audit_log(client_id, summary)
    return summary


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
    return parser.parse_args()


NEEDS_HUMAN_STATUSES = {"gate_fallo_no_promovido", "error_regeneracion"}


def main() -> None:
    args = parse_args()
    summary = run_for_client(args.client, dry_run=args.dry_run)
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


if __name__ == "__main__":
    main()
