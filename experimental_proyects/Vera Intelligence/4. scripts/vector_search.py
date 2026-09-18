"""Búsqueda semántica sobre conversaciones vectorizadas (analytics_v2.conversation_embeddings).

PROTOTIPO CONECTADO (2026-09-10) — ver "6. busqueda_vectorial/README.md" antes de tocar este
archivo. La tabla de vectores la puebla un pipeline externo, ajeno a este proyecto; acá sólo se
consulta de forma aislada por tenant, igual que sql_security.py exige para dashboard_v2.

Dos límites conocidos, deliberadamente no resueltos todavía (ver el README para el detalle):

1. `_reconstruct_chunk_text` es una APROXIMACIÓN por ventana de palabras, no el algoritmo real de
   chunking (nunca confirmado por la persona que vectoriza). El fragmento devuelto puede no
   coincidir exacto con el texto que generó el vector — es razonable para citar, no exacto.
2. No hay índice vectorial (ivfflat/hnsw) sobre `analytics_v2.conversation_embeddings.embedding`
   todavía — la búsqueda es un scan secuencial. Con ~720k filas y creciendo, esto puede volverse
   lento; no se optimiza acá porque crear el índice es una decisión de la persona que administra
   esa tabla, no de este proyecto.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
from google import genai
from google.genai import errors, types

from client_config import ClientConfig, SourceConfig
from runtime_control import OperationalUnavailable, check_analysis, wait_before_retry
from usage_tracking import UsageRecorder
from utils.postgres import postgres_connection_kwargs

logger = logging.getLogger(__name__)


# Conexión Postgres reusable entre búsquedas (2026-09-10) -sólo para este módulo, no toca
# utils/postgres.py (get_postgres_connection sigue abriendo una conexión nueva por llamada para
# run_readonly_sql/get_business_rules, sin cambios). Motivo: la brecha medida entre el plan de
# ejecución aislado (<1s, ver "6. busqueda_vectorial/README.md" > Iteración 11) y la latencia real
# (~3s) es consistente con el costo de abrir una conexión TLS nueva a RDS en cada búsqueda -evitar
# reabrirla en cada llamada dentro del mismo proceso debería recortar esa brecha. Mismos parámetros
# de solo lectura que get_postgres_connection (postgres_connection_kwargs incluye
# default_transaction_read_only=on vía `options`), así que sigue sin poder escribir aunque se
# reutilice.
_cached_connection: "psycopg.Connection | None" = None

# Límite de conexiones concurrentes (2026-09-11) -contención de sesión detectada en pruebas: varios
# procesos de prueba (Streamlit/tester) quedaron corriendo en simultáneo en la misma sesión de
# trabajo y cada uno pudo llegar a abrir su propia conexión al mismo tiempo, lo que coincide con
# picos aislados de query_ms (hasta ~109s) que no aparecen en el plan de ejecución (EXPLAIN ANALYZE
# da <1s). Este lock no evita que OTROS procesos abran conexión -eso se resuelve con higiene de
# proceso, ver "6. busqueda_vectorial/README.md"- pero sí evita que, DENTRO de este proceso, dos
# llamadas concurrentes a search() disparen dos psycopg.connect() en simultáneo (p.ej. si el loop de
# tools del agente llega a paralelizar tool calls): la segunda espera a la primera y después
# reutiliza la conexión ya cacheada en vez de abrir una nueva.
# RLock, no Lock (2026-09-11, bug real encontrado al probar el fix de arriba): search() toma
# este lock para serializar la ejecución de la query, y DENTRO de ese bloque llama a
# _get_reusable_connection(), que también toma el mismo lock si todavía no hay conexión cacheada
# (primera búsqueda de un proceso). Con Lock (no reentrante) eso deadlockea al primer thread que
# entra -confirmado en vivo: una única search_conversations() colgada >150s en un proceso nuevo.
# RLock permite que el mismo thread lo vuelva a tomar sin bloquearse a sí mismo.
_connection_lock = threading.RLock()


def _get_reusable_connection() -> "psycopg.Connection":
    """Devuelve la conexión cacheada si sigue viva, o abre una nueva (y la cachea) si no existe
    todavía o se cerró/rompió -reconexión automática, transparente para quien llama. Serializada con
    _connection_lock para que dos llamadas concurrentes dentro del mismo proceso no abran dos
    conexiones a la vez."""
    global _cached_connection
    if _cached_connection is not None and not _cached_connection.closed:
        return _cached_connection
    with _connection_lock:
        if _cached_connection is not None and not _cached_connection.closed:
            return _cached_connection
        check_analysis()
        try:
            kwargs = postgres_connection_kwargs()
        except RuntimeError:
            raise OperationalUnavailable() from None
        _cached_connection = psycopg.connect(**kwargs)
        return _cached_connection


# Retry de _embed_query (2026-09-10) -mismo criterio que _send_message_with_retry en vi_agent.py
# para las llamadas de chat, que esta función no tenía hasta ahora: un 429/5xx transitorio acá
# tiraba error directo, sin reintentar, a diferencia del resto de las llamadas a Gemini del
# proyecto.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_EMBED_RETRIES = 5
_RETRY_BASE_DELAY_SECONDS = 2


def _status_code(exc: Exception) -> int | None:
    raw = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
# Metadata de uso real de search_conversations (2026-09-10) -NUNCA el texto de la query ni el
# contenido citado, mismo criterio de privacidad que usage_tracking.py aplica a las llamadas de
# Gemini. No agrega costo (sólo un append local, sin llamados extra a Postgres/Gemini): pensado
# para acumular datos reales de producción, útiles más adelante para reconsiderar decisiones que
# hoy se descartaron por falta de muestras -ej. el umbral de distancia, ver
# "6. busqueda_vectorial/README.md".
VECTOR_SEARCH_LOG_PATH = PROJECT_ROOT / ".runtime" / "usage" / "vector_search_calls.jsonl"


# Sufijo de convención de nombres del proyecto para la vista de texto descriptivo por conversación
# (ver Data Maps de los clientes con schema completo, ej. mens_fashion_alto). No todos los clientes
# la tienen -los de schema "delgado" (salomon_alto, gac_medio, etc.) no la declaran en su
# config.yaml, y en ese caso el resumen verificado simplemente no se adjunta (degradación
# controlada, no un error).
_DESCRIPTIVOS_GENERALES_SUFFIX = "insights_descriptivos_generales"

# Nombre de la columna con el resumen ejecutivo de la conversación dentro de la vista
# *_insights_descriptivos_generales -NO es uniforme entre clientes, descubierto al generalizar a
# Farma24 (2026-09-11, ver "6. busqueda_vectorial/README.md" > Iteración 17): mens_fashion_alto la
# llama resumen_ejecutivo_conversacion, pero farma24_alto tiene el mismo contenido semántico
# (resumen en texto libre de toda la conversación) bajo otro nombre
# (descriptivos_generales_tipo_interaccion_detalle) -causaba
# "column di.resumen_ejecutivo_conversacion does not exist" en producción antes de este mapeo.
# Mapeado por tenant (no por client_id/carpeta) porque es el mismo valor que ya usa el aislamiento
# de la query (`WHERE r.seller_id = %s`). Agregar acá cualquier cliente nuevo con schema completo
# cuya columna no se llame igual que la de mens_fashion_alto.
_RESUMEN_COLUMN_BY_TENANT: dict[str, str] = {
    "Farma 24": "descriptivos_generales_tipo_interaccion_detalle",
}
_DEFAULT_RESUMEN_COLUMN = "resumen_ejecutivo_conversacion"

# Tenants cuya vista *_insights_descriptivos_generales matchea el sufijo de convención de nombre
# pero no se puede usar para el JOIN de resumen_verificado -a diferencia de Farma24 (mismo dato,
# otro nombre de columna), acá directamente FALTA la columna que el JOIN necesita. Descubierto al
# generalizar a Maga (2026-09-11, ver "6. busqueda_vectorial/README.md" > Iteración 20):
# vw_maga_insights_descriptivos_generales no tiene conversation_id (usa recordingid) -el JOIN
# `di.conversation_id = conv.conversation_id` tiraría "column di.conversation_id does not exist".
# Tratado igual que un cliente de schema delgado para este propósito puntual: resumen_verificado
# sale None, el resto de vector_search sigue funcionando normal.
#
# "Atlas" agregado acá (2026-09-15, habilitación de búsqueda vectorial para este cliente): mismo
# problema que Maga, verificado en vivo contra information_schema -
# vw_atlas_insights_descriptivos_generales sólo tiene `recordingid`, no `conversation_id` (el JOIN
# `di.conversation_id = conv.conversation_id` fallaría con "column does not exist"). Además, su
# única columna de texto libre (`descriptivos_generales_voice_of_customer`) no es semánticamente
# un resumen ejecutivo de la conversación completa como en mens_fashion_alto/farma24_alto, así que
# ni valdría la pena mapear un nombre de columna distinto (a diferencia del caso Farma24 en
# _RESUMEN_COLUMN_BY_TENANT) -directamente no hay resumen_verificado equivalente para este cliente.
_DESCRIPTIVOS_UNSUPPORTED_TENANTS: frozenset[str] = frozenset({"Maga", "Atlas"})


def _find_descriptivos_source(client: ClientConfig) -> SourceConfig | None:
    """Ubica, si existe, la fuente de resumen ejecutivo por conversación del cliente activo.

    Se resuelve por convención de nombre (no hay un campo dedicado en ClientConfig para esto)
    -ver `_DESCRIPTIVOS_GENERALES_SUFFIX`. Devuelve None para clientes de schema delgado, o para
    un tenant en `_DESCRIPTIVOS_UNSUPPORTED_TENANTS` (la vista existe pero le falta la columna que
    el JOIN necesita)."""
    if client.tenant in _DESCRIPTIVOS_UNSUPPORTED_TENANTS:
        return None
    for name, source in client.sources.items():
        if name.endswith(_DESCRIPTIVOS_GENERALES_SUFFIX):
            return source
    return None


EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768

# LLM-as-judge sobre los resultados de search_conversations (2026-09-14, pedido explícito: "por
# las dudas utilices un LLM as a judge para comprobar que las conversaciones que se traen
# realmente sean de eso" -ej. pedir insultos y confirmar que el resultado tiene un insulto real,
# no sólo que quedó cerca en el espacio de embeddings). NO reemplaza la limitación estructural ya
# documentada (6. busqueda_vectorial/README.md: la distancia vectorial sola no separa limpio
# nuances subjetivas, ~15-20% de precisión en 4 métodos probados) -es una segunda pasada que
# reduce falsos positivos ANTES de que lleguen al modelo principal. Ver
# `VectorSearchRepository._judge_relevance`.
#
# CAMBIADO (2026-09-18, misma sesión que amplió el trigger proactivo de search_conversations en
# vi_agent.py -pedido explícito: usarla mucho más, y evitar que el costo total "se vaya a la
# mierda" con ese mayor volumen): antes corría con el modelo real de producción del cliente
# (`self.client.model`), citando el mismo criterio que "por qué el gate usa el modelo real, no uno
# más barato" (8. README.md). Ese criterio no traslada bien acá: el gate valida si el SISTEMA que
# le habla al cliente sigue respondiendo bien tras un cambio de Data Map -ahí sí importa que el
# modelo evaluado sea el mismo que sirve en producción. Este juez no evalúa nada del cliente ni de
# su Data Map, es clasificación genérica de texto ("¿este fragmento corto respalda esta query?"),
# independiente del modelo que atiende al cliente. Verificado en vivo con la misma query real
# (fragmentos reales, LOW thinking) en los tres modelos: los tres dieron el mismo veredicto
# correcto, pero gemini-3.5-flash-lite lo resolvió con 0 tokens de "thinking" (vs. 138 de
# gemini-3.7-flash y 109 de gemini-3.1-flash-lite) -sumado a su precio por token, ~8,5x más barato
# que el modelo de producción para esta tarea puntual. Decisión confirmada explícitamente con el
# usuario antes de aplicarla (a diferencia del gate, que sigue con su modelo real -no reabrir esa
# sin releer la sección del README).
JUDGE_MODEL = "gemini-3.5-flash-lite"
_JUDGE_PROMPT_TEMPLATE = """Sos un verificador estricto de resultados de búsqueda semántica sobre conversaciones reales de venta.

Búsqueda del usuario: "{query}"

Un sistema de búsqueda por similitud encontró los siguientes fragmentos como posibles resultados. Tu única tarea es decidir, para cada uno, si el texto REALMENTE contiene evidencia directa de lo que pide la búsqueda -no si es un tema parecido o relacionado, sino si ese fragmento específico lo respalda. Basate únicamente en el texto dado, no infieras ni asumas de más. Si el fragmento es ambiguo, incompleto, o sólo tangencialmente relacionado, marcalo como false.

Fragmentos (uno por línea, numerados desde 0):
{fragments_block}

Devolvé SOLO un array JSON de {n} valores booleanos, en el mismo orden que los fragmentos, sin texto adicional ni explicación. Ejemplo de formato exacto para 3 fragmentos: [true, false, true]
"""

# Verificado por SQL directo el 2026-09-10 contra analytics_v2.conversation_embeddings: es el
# único valor presente en toda la tabla hoy. Si la otra persona re-vectoriza con otro config, esta
# constante queda desactualizada en silencio -filtrar por ella evita comparar embeddings de dos
# configs distintas (dimensión/task_type/chunking incompatibles), a costa de dejar de encontrar
# filas nuevas si el config cambia sin que se actualice acá.
EMBEDDING_CONFIG_ID = (
    "gemini-developer-api:gemini-embedding-001:768:retrieval-document:srt-v1:"
    "short-whole-long-fixed-overlap:tokens-per-word-1.3:max-1500:window-1153:overlap-153:"
    "step-1000:l2-v1"
)

# Aproximación de la ventana de chunking en PALABRAS, a partir del ratio tokens-per-word=1.3
# declarado en EMBEDDING_CONFIG_ID. NO es el algoritmo real: se infiere sólo de los nombres de los
# parámetros del config_id -el criterio exacto de tokenización (por turno del .srt, por tiempo,
# por token crudo) no está confirmado. Ver punto 1 del docstring del módulo.
_TOKENS_PER_WORD = 1.3
_WINDOW_WORDS = round(1153 / _TOKENS_PER_WORD)
_STEP_WORDS = round(1000 / _TOKENS_PER_WORD)

_MIN_TOP_K = 1
_MAX_TOP_K = 20

# Deduplicación por conversación (2026-09-10): una conversación larga puede tener hasta ~14 chunks
# (verificado en mens_fashion_alto), y varios de ellos pueden caer en el mismo top-k si esa
# conversación puntual toca mucho el tema buscado -sin esto, `top_k=5` podía devolver 5 chunks de
# sólo 2 conversaciones distintas en vez de 5 conversaciones distintas. Se pide de más
# (`_CANDIDATE_MULTIPLIER` veces top_k, con un techo) y se deduplica en Python después, quedándose
# con el chunk de mejor distancia por conversación -las filas ya vienen ordenadas por distancia
# ascendente desde el ORDER BY, así que la primera aparición de cada conversation_id es siempre su
# mejor chunk.
_CANDIDATE_MULTIPLIER = 4
_MAX_CANDIDATES = 60

_DATE_FORMAT = "%Y-%m-%d"


def _as_optional_str(value: object) -> str | None:
    """Normaliza un argumento opcional de string antes de validarlo -encontrado en vivo
    (2026-09-18, cliente farma24_alto): el modelo a veces manda `{}` (dict vacío) en vez de omitir
    un parámetro opcional que no quiere setear (parece un artefacto de function calling automático
    de Gemini con parámetros `str | None`, no algo que dependa del prompt). Sin esto,
    `date_from={}`/`store_name={}`/etc. rompía con `'dict' object has no attribute 'strip'` -un
    error de tipo poco claro para el modelo, que gastaba 2 llamadas fallidas reintentando con la
    misma forma antes de finalmente omitir el parámetro por su cuenta. Cualquier valor que no sea
    un string no vacío (dict, list, número, string vacío/sólo espacios) se trata como "no lo pasó"."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _parse_date_arg(value: str | None, *, arg_name: str) -> str | None:
    """Valida date_from/date_to (YYYY-MM-DD) antes de mandarlos a Postgres -falla rápido con un
    mensaje claro en vez de dejar que psycopg tire un error de casteo menos legible para el modelo,
    que es quien tiene que poder corregir el argumento."""
    if value is None or not value.strip():
        return None
    try:
        datetime.strptime(value.strip(), _DATE_FORMAT)
    except ValueError:
        raise ValueError(f"{arg_name} debe tener formato YYYY-MM-DD, recibido: {value!r}") from None
    return value.strip()


# Enmascarado de lenguaje ofensivo severo (2026-09-10) — defensa en profundidad sobre el texto
# reconstruido de la transcripción, que puede traer groserías fuertes tal cual se dijeron (caso
# real visto en producción). El modelo ya maneja esto bien al responder (lo parafrasea sin repetir
# la grosería, ver "6. busqueda_vectorial/README.md"), pero esta capa reduce el riesgo de que ese
# texto crudo llegue sin ningún filtro antes de esa instancia -mejor esfuerzo, no exhaustivo:
# lista corta y deliberadamente conservadora para no enmascarar vocabulario de negocio legítimo por
# error. Ampliar la lista si aparece un caso real no cubierto, no intentar anticipar todo de una.
_OFFENSIVE_TERMS = (
    "puta", "puto", "putas", "putos",
    "pendejo", "pendeja", "pendejos", "pendejas",
    "cabron", "cabrón", "cabrones",
    "verga", "vergas",
    "chingada", "chingado", "chingar", "chingas", "chingón", "chingon",
    "culero", "culera", "culeros",
    "maricon", "maricón",
)
_OFFENSIVE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(term) for term in _OFFENSIVE_TERMS) + r")\w*\b",
    re.IGNORECASE,
)


def _mask_word(match: "re.Match[str]") -> str:
    word = match.group(0)
    if len(word) <= 2:
        return "*" * len(word)
    return word[0] + "*" * (len(word) - 2) + word[-1]


def _sanitize_offensive_language(text: str | None) -> str | None:
    if not text:
        return text
    return _OFFENSIVE_PATTERN.sub(_mask_word, text)


# Detección de posible inyección de instrucciones en contenido citado (2026-09-14) — defensa en
# profundidad: search_conversations trae texto de conversaciones reales (clientes, vendedores) al
# contexto del modelo por diseño; nada impide que ese texto contenga algo redactado para intentar
# manipular el comportamiento del agente (ej. "ignorá las instrucciones anteriores"), a propósito o
# por casualidad. Esto NO filtra ni altera el contenido citable -a diferencia de
# _sanitize_offensive_language, enmascarar acá rompería la exactitud de la cita para un caso donde el
# texto en sí puede ser la evidencia de negocio relevante (ej. un vendedor citando mal instrucciones
# de un script)-, sólo agrega una señal explícita (`posible_instruccion_incrustada`) para que el
# modelo, ya instruido en SYSTEM_INSTRUCTION_TEMPLATE a tratar este contenido como DATO y nunca como
# instrucción, tenga una alerta reforzada cuando el patrón es especialmente sospechoso. Lista corta y
# deliberadamente conservadora (mismo criterio que _OFFENSIVE_TERMS) -ampliar si aparece un caso real
# no cubierto, no intentar anticipar todo de una.
_INJECTION_MARKER_PATTERNS = (
    r"ignor\w*\s+(las\s+|todas\s+las\s+|cualquier\s+)?instruccion",
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions",
    r"olvid\w*\s+(las\s+|todas\s+las\s+)?instruccion",
    r"nueva\s+instrucci[oó]n",
    r"a\s+partir\s+de\s+ahora\s+sos",
    r"you\s+are\s+now",
    r"act[uú]a\s+como\s+(un\s+|una\s+)?(desarrollador|administrador|sistema)",
    r"system\s*:",
    r"disregard\s+(all\s+|any\s+)?(previous|prior|above)",
)
_INJECTION_MARKER_RE = re.compile("|".join(_INJECTION_MARKER_PATTERNS), re.IGNORECASE)


def _contains_possible_injection_marker(text: str | None) -> bool:
    if not text:
        return False
    return bool(_INJECTION_MARKER_RE.search(text))


def _log_search_event(
    *,
    client_id: str,
    top_k_requested: int,
    candidate_limit: int,
    store_name_used: bool,
    date_range_used: bool,
    candidates_fetched: int,
    results_returned: int,
    best_distance: float | None,
    worst_distance_returned: float | None,
    embed_ms: float,
    query_ms: float,
    judge_ms: float | None = None,
    judge_filtered_count: int | None = None,
) -> None:
    """Registra metadata de una llamada real a search_conversations. Nunca el texto de la query ni
    el contenido citado -sólo agregados, mismo criterio de privacidad que usage_tracking.py aplica
    a las llamadas de Gemini. Falla en silencio si no se puede escribir -nunca debe romper una
    búsqueda real por un problema de logging, mismo criterio que rag_sources.py aplica a su cache.

    ``embed_ms``/``query_ms`` (2026-09-10) miden por separado el embedding call (Gemini) y el scan
    de Postgres -sin índice vectorial todavía (ver docstring del módulo, punto 2), así que
    ``query_ms`` es la señal real para decidir cuándo el índice deja de ser opcional. Medir acá en
    vez de suponer no agrega costo: es sólo `time.perf_counter()` alrededor de llamadas que ya se
    hacían."""
    event = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "client_id": client_id,
        "top_k_requested": top_k_requested,
        "candidate_limit": candidate_limit,
        "store_name_used": store_name_used,
        "date_range_used": date_range_used,
        "candidates_fetched": candidates_fetched,
        "results_returned": results_returned,
        "best_distance": best_distance,
        "worst_distance_returned": worst_distance_returned,
        "embed_ms": embed_ms,
        "query_ms": query_ms,
        "judge_ms": judge_ms,
        "judge_filtered_count": judge_filtered_count,
    }
    try:
        VECTOR_SEARCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            str(VECTOR_SEARCH_LOG_PATH), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError:
        pass


_SRT_HEADER_RE = re.compile(
    r"(?m)^\d+\r?\n\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\r?\n"
)


def _strip_srt_noise(transcript_srt: str) -> str:
    """Saca índice de bloque + rango de timestamps del .srt, dejando sólo 'Speaker N: texto' -sin
    esto el modelo gasta tokens leyendo ruido y le cuesta más seguir el diálogo real."""
    return _SRT_HEADER_RE.sub("", transcript_srt)


def _reconstruct_chunk_text(transcript_srt: str, chunk_idx: int) -> str:
    """Aproxima el texto de un chunk a partir del .srt completo -ver punto 1 del docstring."""
    words = _strip_srt_noise(transcript_srt).split()
    start = chunk_idx * _STEP_WORDS
    end = start + _WINDOW_WORDS
    return " ".join(words[start:end])


# Cliente de Gemini reusable entre embeddings (2026-09-11) -mismo motivo y mismo patrón que
# _get_reusable_connection() para Postgres más arriba: _embed_query() creaba un genai.Client nuevo
# en CADA llamada, descartando el pool de conexiones HTTP/TLS a la API de Gemini de la llamada
# anterior. Medido en vivo: ~1.4-2.0s por embed con cliente nuevo cada vez vs. ~0.3-0.7s con el
# cliente reusado -2 a 4x más rápido, sin tocar nada del lado del índice vectorial (ver
# "6. busqueda_vectorial/README.md" > latencia). No hace falta un lock como con la conexión de
# Postgres: un httpx.Client (lo que genai.Client usa por debajo) está diseñado para atender
# requests concurrentes desde múltiples threads -es justamente el propósito de un pool de
# conexiones-, a diferencia de un cursor de psycopg sobre una única conexión compartida.
_cached_embed_client: "genai.Client | None" = None

# Timeout HTTP explícito (2026-09-16) -mismo fix y mismo motivo que _GENAI_HTTP_TIMEOUT_MS en
# vi_agent.py: sin esto, HttpOptions.timeout queda en None (default de la librería), que se
# traduce a "sin timeout" en el httpx.Client subyacente -un cuelgue de red silencioso (sin
# excepción) puede dejar embed_content/generate_content (el juez de relevancia) esperando para
# siempre, sin que ningún retry pueda reaccionar porque nunca hay una excepción que clasificar.
# Encontrado en vivo probando personalización: un search_conversations() quedó colgado varios
# minutos más allá del peor caso documentado (~150-180s), con la conexión TCP igual en estado
# ESTABLISHED -consistente con una llamada de red colgada sin cerrar ni fallar, no con una
# consulta lenta de verdad.
_GENAI_HTTP_TIMEOUT_MS = 120_000


def _get_reusable_embed_client(api_key: str) -> "genai.Client":
    global _cached_embed_client
    if _cached_embed_client is None:
        _cached_embed_client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=_GENAI_HTTP_TIMEOUT_MS),
        )
    return _cached_embed_client


def _embed_query(query: str, api_key: str) -> list[float]:
    """Genera el vector de una pregunta de usuario con el task_type asimétrico correcto.

    Los vectores de analytics_v2.conversation_embeddings se generaron con
    task_type=retrieval_document (ver EMBEDDING_CONFIG_ID). Gemini usa embeddings asimétricos: la
    query debe embeberse con retrieval_query, no con el default ni con retrieval_document -mismo
    modelo, proyección distinta. Usar el task_type equivocado no rompe nada, sólo degrada la
    calidad de los resultados en silencio.

    Reintenta con backoff exponencial ante errores transitorios (429/5xx, cortes de red) -mismo
    criterio que `_send_message_with_retry` en vi_agent.py para las llamadas de chat.
    """
    client = _get_reusable_embed_client(api_key)
    last_error: Exception | None = None
    for attempt in range(1, _MAX_EMBED_RETRIES + 1):
        check_analysis()
        try:
            response = client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=query,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY",
                    output_dimensionality=EMBEDDING_DIMENSION,
                ),
            )
            check_analysis()
            return list(response.embeddings[0].values)
        except Exception as exc:  # noqa: BLE001
            status_code = _status_code(exc)
            retryable = (
                status_code in _RETRYABLE_STATUS_CODES
                or isinstance(exc, (errors.ServerError, httpx.TransportError))
            )
            if not retryable or attempt == _MAX_EMBED_RETRIES:
                raise
            last_error = exc
            wait_before_retry(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    raise last_error  # pragma: no cover


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(value) for value in vector) + "]"


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _judge_relevance(
    query: str,
    resultados: list[dict],
    *,
    model: str,
    api_key: str,
    usage_recorder: "UsageRecorder | None" = None,
    client_id: str = "",
) -> list[bool]:
    """Segunda pasada LLM-as-judge sobre resultados ya recuperados -ver _JUDGE_PROMPT_TEMPLATE
    para el motivo completo. Devuelve un booleano por resultado, mismo orden; `True` para todos si
    algo falla (fail-open, ver docstring del módulo/constante -esto es una mejora de precisión
    sobre una tool best-effort, un fallo del juez no debe dejar la búsqueda sin resultados).

    usage_recorder (2026-09-17, encontrado en la misma investigación de costo de "8. README.md" >
    "Potencial de mejora" > Costos): esta llamada usa `client.models.generate_content()`
    DIRECTAMENTE en vez del wrapper `_send_message_with_retry` de vi_agent.py -la única llamada
    real y de precio pleno (modelo del cliente, sin ningún descuento de cache) que nunca pasaba por
    `UsageRecorder`, así que quedaba invisible en `.runtime/usage/gemini_calls.jsonl` y en
    `usage_report.py`, para siempre, en TODO el historial del proyecto hasta ahora. No cambia nada
    de lo que la tool hace ni de su resultado -sólo hace visible un costo real que ya se estaba
    pagando sin que nadie pudiera verlo. `usage_recorder`/`client_id` son opcionales (default None)
    para no romper ningún call site ni test existente que no los pase."""
    if not resultados:
        return []
    fragments_block = "\n".join(
        f"{i}. {result.get('fragmento_aproximado') or result.get('resumen_verificado') or '(sin texto disponible)'}"
        for i, result in enumerate(resultados)
    )
    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        query=query, fragments_block=fragments_block, n=len(resultados)
    )
    try:
        client = _get_reusable_embed_client(api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
                # thinking_level (2026-09-17, misma investigación de costo): sin esto, esta llamada
                # corría con razonamiento automático completo -medido en vivo, 162 tokens de
                # "thinking" para clasificar cada tanda de fragmentos en true/false, la tarea más
                # simple de todo el proyecto (sin ambigüedad de negocio, sin cálculo, sin SQL). LOW
                # es el mismo nivel ya verificado en vivo como soportado por gemini-3.7-flash (ver
                # vi_agent.DEFAULT_THINKING_LEVEL) -MINIMAL no se prueba acá a propósito, ya se
                # confirmó que este modelo lo rechaza directamente (ver data_map_auto_update.py,
                # GATE_THINKING_LEVEL).
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            ),
        )
        if usage_recorder is not None:
            usage_recorder.record_response(
                response,
                client_id=client_id,
                model=model,
                session_id="",
                interaction_id=uuid.uuid4().hex,
                call_index=1,
                call_kind="search_judge",
                attempts=1,
            )
        veredictos = json.loads(response.text)
        if not isinstance(veredictos, list) or len(veredictos) != len(resultados):
            raise ValueError(f"Formato de veredicto inesperado: {response.text!r}")
        return [bool(v) for v in veredictos]
    except Exception:  # noqa: BLE001 -fail-open, ver docstring.
        logger.warning(
            "Juez de relevancia de search_conversations falló -se devuelven los %s resultados "
            "sin filtrar.",
            len(resultados),
            exc_info=True,
        )
        return [True] * len(resultados)


class VectorSearchRepository:
    """Búsqueda semántica aislada por tenant sobre conversaciones vectorizadas."""

    def __init__(self, client: ClientConfig, usage_recorder: "UsageRecorder | None" = None) -> None:
        self.client = client
        self._descriptivos_source = _find_descriptivos_source(client)
        # usage_recorder (2026-09-17): ver el docstring de _judge_relevance para el hallazgo -sin
        # esto, la llamada del juez de relevancia queda invisible en .runtime/usage/gemini_calls.jsonl.
        self._usage_recorder = usage_recorder

    def search(
        self,
        query: str,
        top_k: int = 5,
        store_name: str | None = None,
        employee_name: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        """Busca conversaciones semánticamente similares a `query` para el tenant activo.

        Args:
            query: texto de búsqueda en lenguaje natural.
            top_k: cantidad máxima de CONVERSACIONES distintas a devolver (1-20) -no de chunks. Una
                conversación larga puede tener varios chunks; se deduplica quedándose con el de
                mejor distancia por conversación, así que `top_k=5` siempre son 5 conversaciones
                distintas (o menos, si no hay tantas), nunca 5 chunks de las mismas 2 o 3.
            store_name: si se indica, limita la búsqueda a tiendas cuyo nombre contenga este texto
                (coincidencia parcial, sin distinguir mayúsculas -ej. "Tlaquepaque" alcanza para
                filtrar "Mens Fashion Tlaquepaque"). Omitir para buscar en todas las tiendas del
                cliente.
            employee_name: si se indica, limita la búsqueda a conversaciones cuyo vendedor
                (`employee_full_name`) contenga este texto (coincidencia parcial, sin distinguir
                mayúsculas -mismo criterio que store_name). Agregado (2026-09-16) para personalizar
                coaching INDIVIDUAL: sin este filtro, un caso citado como ejemplo del vendedor que
                se está coacheando podía en realidad pertenecer a otro vendedor del mismo cliente -
                ver PERSONALIZACIÓN DE RECOMENDACIONES en SYSTEM_INSTRUCTION_TEMPLATE (vi_agent.py)
                para la regla de cuándo es obligatorio pasarlo. Omitir para buscar entre todos los
                vendedores del cliente (uso normal para coaching de equipo).
                CUIDADO -verificado en vivo (2026-09-16): al ser coincidencia PARCIAL, sólo el
                primer nombre puede matchear a más de un vendedor real (ej. "Rocio" trajo resultados
                de "Rocio Haro Leal" Y "Rocio Vazquez Rivera", dos personas distintas del mismo
                cliente) -el prompt le exige al modelo pasar el nombre completo disponible para
                mitigar esto, y además re-verificar el campo `vendedor` de cada resultado antes de
                citarlo como defensa en profundidad; este parámetro por sí solo no garantiza
                unicidad.
            date_from: fecha mínima (formato YYYY-MM-DD, inclusive) de la conversación. Omitir
                para no filtrar por fecha de inicio.
            date_to: fecha máxima (formato YYYY-MM-DD, inclusive) de la conversación. Omitir para
                no filtrar por fecha de fin.

        Returns:
            JSON interno con ``resultados``: una lista de fragmentos con metadata (tienda,
            vendedor, fecha, distancia, distancia relativa al mejor resultado, un
            ``conversation_id`` para cruzar con run_readonly_sql sin adivinar por texto, un
            ``resumen_verificado`` cuando el cliente tiene esa fuente disponible, y el fragmento
            aproximado) para que el modelo los cite sin inventar contenido. Nunca incluye
            identificadores físicos internos (recording_id, nombres de tabla) -ver caja negra en
            SYSTEM_INSTRUCTION_TEMPLATE de vi_agent.py. ``conversation_id`` sí se expone -mismo
            criterio que ya usa run_readonly_sql, que lo devuelve sin filtrar en sus resultados; la
            caja negra se aplica a la respuesta final al cliente, no a los datos internos entre
            tools.

            ``distancia_relativa_al_mejor_resultado`` (0.0 para el mejor resultado, mayor cuanto
            peor) es una señal RELATIVA a esta búsqueda puntual, no un corte absoluto de
            relevancia: se probó calibrar un umbral fijo con las distancias de las 11 preguntas de
            evaluación corridas hasta el 2026-09-10 y no hay separación limpia entre resultados
            relevantes e irrelevantes en términos absolutos (ambos caen en el rango ~0.20-0.27) -ver
            "6. busqueda_vectorial/README.md". Un salto grande en esta distancia relativa dentro de
            la misma búsqueda (ej. el resultado 4 muy por encima del 1) sí es una señal útil de que
            los últimos resultados son mucho peores que el mejor match encontrado.

            ``resumen_verificado`` (2026-09-10) es el resumen ejecutivo de la conversación ya
            extraído por el pipeline de checklist/insights (LLM aparte, no relacionado con esta
            búsqueda) -una segunda fuente para contrastar contra el fragmento reconstruido
            (aproximado, ver punto 1 del docstring del módulo) antes de citarlo. ``None`` si el
            cliente activo no tiene esa fuente declarada en su config.yaml (schema "delgado") o si
            esa conversación puntual todavía no pasó por el pipeline de insights.

            ``fragmento_aproximado`` y ``resumen_verificado`` pasan por un enmascarado de lenguaje
            ofensivo severo (2026-09-10, ver `_sanitize_offensive_language`) antes de devolverse
            -defensa en profundidad además del criterio del modelo al responder; lista corta y
            deliberadamente conservadora, no exhaustiva.

            ``posible_instruccion_incrustada`` (2026-09-14, ver `_contains_possible_injection_marker`)
            es ``true`` cuando ``fragmento_aproximado`` o ``resumen_verificado`` contienen un patrón
            típico de intento de manipular al modelo (ej. "ignorá las instrucciones anteriores"). No
            filtra ni altera el texto citable -sólo refuerza, con una señal explícita, la regla ya
            presente en SYSTEM_INSTRUCTION_TEMPLATE de que este contenido es DATO citable, nunca una
            instrucción real para el agente.

            **Verificación LLM-as-judge (2026-09-14)**: antes de devolverse, cada resultado pasa
            por una segunda pasada con el modelo real del cliente (`_judge_relevance`), que decide
            si el fragmento realmente respalda la búsqueda -no sólo si quedó cerca en el espacio de
            embeddings. Reduce falsos positivos en preguntas subjetivas (ej. "insultos", "malos
            tratos") pero NO resuelve la limitación estructural ya documentada (la distancia
            vectorial sola no separa limpio nuances subjetivas -ver
            "6. busqueda_vectorial/README.md"). Si se descartó algún resultado, ``aviso`` lo dice
            explícitamente. Fail-open: un fallo del juez (API, parseo) devuelve todo sin filtrar,
            nunca deja la búsqueda sin resultados por un problema del juez mismo.

        Raises:
            ValueError: si `query` está vacía, o si `date_from`/`date_to` no tienen formato
                YYYY-MM-DD.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("La búsqueda requiere un texto no vacío.")
        top_k = max(_MIN_TOP_K, min(int(top_k), _MAX_TOP_K))
        store_name = _as_optional_str(store_name)
        employee_name = _as_optional_str(employee_name)
        date_from = _as_optional_str(date_from)
        date_to = _as_optional_str(date_to)
        date_from = _parse_date_arg(date_from, arg_name="date_from")
        date_to = _parse_date_arg(date_to, arg_name="date_to")

        check_analysis()
        api_key = os.getenv("VERA_AI_API_KEY")
        if not api_key:
            raise OperationalUnavailable()
        embed_start = time.perf_counter()
        vector_literal = _vector_literal(_embed_query(query.strip(), api_key))
        embed_ms = round((time.perf_counter() - embed_start) * 1000, 1)

        # El tipo `vector` y el operador `<=>` de pgvector viven en el schema analytics_v2 (no en
        # public/search_path default) -confirmado por SQL directo el 2026-09-10: sin agregar
        # analytics_v2 al search_path de la sesión, psycopg tira `UndefinedObject: type "vector"
        # does not exist` y luego `UndefinedFunction` para `<=>`.
        #
        # El texto real de la conversación vive en raw_v2.conversations_raw.data->>'transcribedAudio'
        # (formato .srt), NO en core_v2.conversations.transcribed_audio -esa columna está NULL en
        # el 100% de las 856k filas verificadas el 2026-09-10 (hallazgo de esta sesión: el diseño
        # original en el README asumía la columna equivocada).
        #
        # El aislamiento por tenant se aplica acá, no en sql_security.py -esta tool nunca deja que
        # el modelo escriba SQL; el filtro `WHERE r.seller_id = %s` es fijo y parametrizado, mismo
        # criterio de aislamiento que sql_security.py exige para dashboard_v2, aplicado a mano
        # porque la tabla vive en otro schema (analytics_v2), no en dashboard_v2.
        #
        # store_name (2026-09-10, ronda 2 de mejoras) es la única parte del WHERE armada
        # dinámicamente a partir de un argumento del modelo -pero va como parámetro (%s con
        # ILIKE), nunca interpolado en el texto SQL, así que no hay riesgo de inyección.
        #
        # conv (LEFT JOIN LATERAL, 2026-09-10, ronda de mejoras): resuelve el conversation_id real
        # -no siempre coincide con recording_id (verificado: 856.317 filas en core_v2.conversations,
        # sólo 734.874 con recording_id == conversation_id) y 206 recording_id tienen más de una
        # fila/conversation_id asociada -el LATERAL con ORDER BY + LIMIT 1 desempata
        # deterministamente en vez de multiplicar filas del resultado con un JOIN plano.
        #
        # di (LEFT JOIN condicional al schema del cliente): adjunta el resumen ejecutivo ya
        # extraído por el pipeline de checklist/insights como segunda fuente para contrastar contra
        # el fragmento reconstruido -ver _find_descriptivos_source. Ausente para clientes de schema
        # delgado (sin esa vista declarada en su config.yaml).
        select_columns = [
            "ce.recording_id",
            "ce.chunk_idx",
            "r.store_name",
            "r.employee_full_name",
            "r.started_at",
            "cr.data->>'transcribedAudio' AS transcript",
            "conv.conversation_id",
        ]
        joins = [
            "FROM analytics_v2.conversation_embeddings ce",
            "JOIN mart_v2.recordings_enriched r ON r.recording_id = ce.recording_id",
            "JOIN raw_v2.conversations_raw cr ON cr.recording_id = ce.recording_id",
            """LEFT JOIN LATERAL (
                SELECT c.conversation_id
                FROM core_v2.conversations c
                WHERE c.recording_id = ce.recording_id
                ORDER BY c.extracted_at DESC NULLS LAST
                LIMIT 1
            ) conv ON true""",
        ]
        # IMPORTANTE: psycopg sustituye los %s en el orden en que aparecen en el TEXTO final del
        # SQL, de izquierda a derecha -no en el orden en que se van agregando a las listas de
        # Python. El SELECT (con el %s de `distancia`) siempre renderiza antes que los JOIN (con el
        # %s de `di`, si existe), que a su vez renderizan antes que el WHERE -armar `params` en ese
        # mismo orden explícitamente, no según la secuencia de `.append()` en el código.
        if self._descriptivos_source is not None:
            resumen_column = _RESUMEN_COLUMN_BY_TENANT.get(
                self.client.tenant, _DEFAULT_RESUMEN_COLUMN
            )
            select_columns.append(f"di.{resumen_column} AS resumen_ejecutivo_conversacion")
            joins.append(
                f"LEFT JOIN {self._descriptivos_source.name} di "
                f"ON di.conversation_id = conv.conversation_id "
                f"AND di.{self._descriptivos_source.tenant_field} = %s"
            )
        else:
            select_columns.append("NULL AS resumen_ejecutivo_conversacion")
        select_columns.append("ce.embedding <=> %s::vector AS distancia")

        sql = "SELECT " + ", ".join(select_columns) + "\n" + "\n".join(joins) + "\n"
        sql += "WHERE r.seller_id = %s\n  AND ce.embedding_config_id = %s\n"
        # Allowlist obligatoria de store_name (2026-09-16, habilitación de Huerpel) -ver
        # VectorSearchConfig.store_names en client_config.py. Se aplica ANTES del store_name
        # opcional que puede pedir el modelo: ambos combinan con AND, así que un cliente con
        # allowlist configurada nunca puede "escaparse" a otra sub-marca del mismo seller_id ni
        # aunque el modelo pase un store_name que matchee otra sub-marca.
        if self.client.vector_search is not None and self.client.vector_search.store_names:
            sql += "  AND r.store_name = ANY(%s)\n"
        if store_name and store_name.strip():
            sql += "  AND r.store_name ILIKE %s\n"
        if employee_name and employee_name.strip():
            sql += "  AND r.employee_full_name ILIKE %s\n"
        if date_from:
            sql += "  AND r.started_at::date >= %s::date\n"
        if date_to:
            sql += "  AND r.started_at::date <= %s::date\n"
        sql += "ORDER BY ce.embedding <=> %s::vector\nLIMIT %s\n"

        candidate_limit = min(top_k * _CANDIDATE_MULTIPLIER, _MAX_CANDIDATES)
        params: list[object] = [vector_literal]  # distancia (SELECT)
        if self._descriptivos_source is not None:
            params.append(self.client.tenant)  # di (JOIN)
        params += [self.client.tenant, EMBEDDING_CONFIG_ID]  # WHERE
        if self.client.vector_search is not None and self.client.vector_search.store_names:
            params.append(list(self.client.vector_search.store_names))  # WHERE store_names allowlist
        if store_name and store_name.strip():
            params.append(f"%{store_name.strip()}%")  # WHERE store_name
        if employee_name and employee_name.strip():
            params.append(f"%{employee_name.strip()}%")  # WHERE employee_name
        if date_from:
            params.append(date_from)  # WHERE date_from
        if date_to:
            params.append(date_to)  # WHERE date_to
        params += [vector_literal, candidate_limit]  # ORDER BY, LIMIT

        query_start = time.perf_counter()
        # _connection_lock serializa también la EJECUCIÓN acá, no sólo la creación de la conexión
        # (2026-09-11, ver run_tool_loop en vi_agent.py -on_tool_call permite paralelizar tool
        # calls con ThreadPoolExecutor). psycopg no garantiza que una misma Connection pueda
        # usarse desde dos threads en simultáneo sin sincronización externa -sin este lock, dos
        # search_conversations() concurrentes sobre la conexión cacheada compartida (_cached_connection)
        # podrían interleavear cursor.execute/fetchall entre threads. El lock sólo serializa la
        # query de vector search entre sí -sigue corriendo en paralelo con run_readonly_sql, que
        # abre su propia conexión nueva por llamada (get_postgres_connection).
        with _connection_lock:
            check_analysis()
            try:
                connection = _get_reusable_connection()
                with connection.cursor() as cursor:
                    cursor.execute("SET search_path = analytics_v2, public")
                    cursor.execute(sql, tuple(params))
                    rows = cursor.fetchall()
            except psycopg.OperationalError as exc:
                # La conexión cacheada se rompió (ej. idle timeout del lado del server) -forzar una
                # reconexión y reintentar una sola vez, no reintentos infinitos.
                if getattr(exc, "sqlstate", None) in {"28P01", "28000"}:
                    raise OperationalUnavailable() from None
                state = getattr(exc, "sqlstate", None)
                if state is not None and not state.startswith(("08", "28")) and state not in {"57P01", "57P02", "57P03", "53300"}:
                    if not connection.closed:
                        connection.rollback()
                    raise
                check_analysis()
                global _cached_connection
                _cached_connection = None
                connection = _get_reusable_connection()
                with connection.cursor() as cursor:
                    cursor.execute("SET search_path = analytics_v2, public")
                    cursor.execute(sql, tuple(params))
                    rows = cursor.fetchall()
        query_ms = round((time.perf_counter() - query_start) * 1000, 1)

        # Deduplicación por conversación -ver nota junto a _CANDIDATE_MULTIPLIER. `rows` ya viene
        # ordenado por distancia ascendente (ORDER BY del SQL), así que la primera vez que se ve
        # cada `dedup_key` es siempre su chunk de mejor distancia; el resto de sus chunks
        # (peores) se descartan. `recording_id` es el respaldo cuando el LATERAL no resolvió
        # conversation_id (no debería pasar casi nunca, pero evita que dos filas sin
        # conversation_id se traten como si fueran la misma conversación por casualidad).
        resultados = []
        seen_keys: set[str] = set()
        mejor_distancia: float | None = None
        for (
            recording_id,
            chunk_idx,
            store_name_,
            employee,
            started_at,
            transcript,
            conversation_id,
            resumen_verificado,
            distancia,
        ) in rows:
            if len(resultados) >= top_k:
                break
            dedup_key = conversation_id or recording_id
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            distancia = float(distancia)
            if mejor_distancia is None:
                mejor_distancia = distancia
            fragmento = (
                _reconstruct_chunk_text(transcript, chunk_idx) if transcript else None
            )
            posible_instruccion_incrustada = _contains_possible_injection_marker(
                fragmento
            ) or _contains_possible_injection_marker(resumen_verificado)
            resultados.append(
                {
                    "conversation_id": conversation_id,
                    "tienda": store_name_,
                    "vendedor": employee,
                    "fecha": _json_safe(started_at),
                    "distancia": distancia,
                    "distancia_relativa_al_mejor_resultado": round(distancia - mejor_distancia, 4),
                    "resumen_verificado": _sanitize_offensive_language(resumen_verificado),
                    "fragmento_aproximado": _sanitize_offensive_language(fragmento),
                    "posible_instruccion_incrustada": posible_instruccion_incrustada,
                }
            )

        # LLM-as-judge (2026-09-14, ver _JUDGE_PROMPT_TEMPLATE): segunda pasada sobre los
        # resultados ya recuperados, antes de devolverlos, con JUDGE_MODEL (2026-09-18: modelo
        # barato dedicado, ver su docstring arriba -ya no el modelo real del cliente). Corre
        # siempre -no sólo para queries "subjetivas"-: distinguir de antemano qué pregunta lo
        # necesita sería una heurística frágil, y el costo de una llamada extra por búsqueda es
        # acotado. candidates_returned_before_judge se loguea para poder medir, con datos reales,
        # cuánto recorta el juez en la práctica -no asumido de antemano.
        candidates_returned_before_judge = len(resultados)
        judge_start = time.perf_counter()
        veredictos = _judge_relevance(
            query, resultados, model=JUDGE_MODEL, api_key=api_key,
            usage_recorder=self._usage_recorder, client_id=self.client.client_id,
        )
        judge_ms = round((time.perf_counter() - judge_start) * 1000, 1)
        resultados = [
            result for result, es_relevante in zip(resultados, veredictos) if es_relevante
        ]
        judge_filtered_count = candidates_returned_before_judge - len(resultados)

        _log_search_event(
            client_id=self.client.client_id,
            top_k_requested=top_k,
            candidate_limit=candidate_limit,
            store_name_used=bool(store_name and store_name.strip()),
            date_range_used=bool(date_from or date_to),
            candidates_fetched=len(rows),
            results_returned=len(resultados),
            best_distance=resultados[0]["distancia"] if resultados else None,
            worst_distance_returned=resultados[-1]["distancia"] if resultados else None,
            embed_ms=embed_ms,
            query_ms=query_ms,
            judge_ms=judge_ms,
            judge_filtered_count=judge_filtered_count,
        )
        aviso = "los fragmentos son una reconstrucción aproximada"
        if judge_filtered_count > 0:
            aviso += (
                f"; se descartaron {judge_filtered_count} resultado(s) que quedaron cerca en la "
                "búsqueda pero un verificador adicional no confirmó que respaldaran genuinamente "
                "la pregunta"
            )
        return json.dumps(
            {"resultados": resultados, "aviso": aviso},
            ensure_ascii=False,
            default=str,
        )
