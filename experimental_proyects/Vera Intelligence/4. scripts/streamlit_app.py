"""Interfaz web (Streamlit) de Vera Intelligence — chat con estética de producto en vez de terminal.

Usa el motor compartido vi_agent.py y añade presentación y control de cancelación de la sesión.
Es la interfaz por default de "1. vi_agent_tester.py" (ejecutarlo sin --cli levanta
esto) — no se corre directamente salvo para debug puntual, ver "Cómo correrlo" abajo.

Nació como experimento en "3. experimentos/interfaz_streamlit/" (2026-09-09) y se promovió acá
el mismo día a pedido explícito del usuario ("quedó perfecta"), como reemplazo del modo por
default del tester -no como reemplazo del motor, que sigue siendo vi_agent.py-.

Sigue siendo de un solo usuario local: sin login, sin persistencia entre sesiones del navegador,
sin selección de cliente restringida por rol -eso requiere la capa de servicio que "8. README.md"
marca como bloqueante para producción real, fuera de alcance acá-.

Cómo correrlo directamente (normalmente no hace falta, "1. vi_agent_tester.py" ya hace esto):
    .venv/Scripts/python.exe -m streamlit run "experimental_proyects/Vera Intelligence/4. scripts/streamlit_app.py"

Para preseleccionar un cliente (mismo --client que el tester), pasarlo después de "--":
    streamlit run streamlit_app.py -- --client mens_fashion_alto
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
import uuid
from contextlib import contextmanager

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import streamlit as st  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import vi_agent  # noqa: E402
import audio_playback  # noqa: E402
from client_config import available_clients_with_display_names, load_client_config  # noqa: E402
from feedback_tracking import FEEDBACK_LOG_PATH, FeedbackRecorder  # noqa: E402
from question_tracking import QuestionRecorder  # noqa: E402
from sheets_logging import SheetsLogger  # noqa: E402
from usage_tracking import estimate_cost_usd, load_usage_events, summarize_usage  # noqa: E402
from runtime_control import ACTIVE_CLIENT_LOCK, AnalysisCancelled, AnalysisControl, OperationalUnavailable  # noqa: E402


# SheetsLogger.from_env() nunca falla ni bloquea el arranque -si faltan credenciales o el Sheet
# todavía no fue compartido con el service account, queda deshabilitado (ver sheets_logging.py) y
# todo sigue funcionando exactamente igual que antes, sólo sin la copia remota.
_SHEETS_LOGGER = SheetsLogger.from_env()
_FEEDBACK_RECORDER = FeedbackRecorder(FEEDBACK_LOG_PATH, sheets_logger=_SHEETS_LOGGER)
_QUESTION_RECORDER = QuestionRecorder(sheets_logger=_SHEETS_LOGGER)


# Cache en disco del saludo inicial (2026-09-14) -evaluado y no implementado en su momento (ver
# "8. README.md" > "Potencial de mejora" > velocidad) por parecer más invasivo que otros fixes de
# esa ronda; retomado a pedido explícito. Sin esto, `_ensure_greeting` dispara una llamada real a
# Gemini cada vez que alguien abre una pestaña nueva, hace clic en "Nueva conversación" o cambia de
# cliente -aunque el contenido depende casi enteramente del Data Map del cliente (estable entre
# sesiones, cambia sólo cuando se promueve una versión nueva). Se invalida con el MISMO fingerprint
# que ya usa el cache de contexto de Gemini (`vi_agent._content_fingerprint` sobre
# system_instruction + tools) -mismo criterio de "cuándo cambió de verdad el contenido", aplicado acá
# al TEXTO DE SALIDA en vez de al prompt de entrada. Reusar la función privada de vi_agent en vez de
# reimplementar el hash es el mismo patrón que ya usa data_map_auto_update.py (vi_agent._send_message_with_retry,
# vi_agent._build_rulebook_options, etc.) -este proyecto no duplica ese tipo de lógica entre módulos.
GREETING_CACHE_DIR = vi_agent.PROJECT_ROOT / ".runtime" / "greeting_cache"


def _greeting_cache_path(client_id: str) -> Path:
    return GREETING_CACHE_DIR / f"{client_id}.json"


def _greeting_cache_fingerprint() -> str:
    """Mismo fingerprint que usa el cache de contexto de Gemini para ESTE cliente activo -si el
    Data Map o las tools cambian, ambos caches se invalidan juntos, sin necesitar dos mecanismos de
    invalidación distintos para el mismo cambio de contenido. Se combina además con GREETING_PROMPT
    -a diferencia del cache de contexto de Gemini (que sólo depende del Data Map/tools, nunca de la
    pregunta puntual), el saludo cacheado SÍ depende del texto exacto de su propio prompt: si se
    edita GREETING_PROMPT en código, un saludo viejo guardado con la redacción anterior tiene que
    invalidarse aunque el Data Map no haya cambiado."""
    base = vi_agent._content_fingerprint(
        vi_agent.build_system_instruction(), vi_agent._build_tools_list()
    )
    # + greeting_vector_search_clause() (2026-09-18): el prompt real que se envía incluye esta
    # cláusula opcional -si cambia (o si vector_search se activa/desactiva para el cliente), un
    # saludo cacheado con la versión anterior tiene que invalidarse también.
    prompt_text = GREETING_PROMPT + vi_agent.greeting_vector_search_clause()
    return hashlib.sha256((base + "\x00" + prompt_text).encode("utf-8")).hexdigest()


def _load_cached_greeting(
    client_id: str, fingerprint: str
) -> tuple[str, list[dict], list[str]] | None:
    """None si no hay cache reusable -archivo ausente/corrupto, o fingerprint distinto (Data Map
    promovido a una versión nueva desde que se guardó este saludo)."""
    path = _greeting_cache_path(client_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("fingerprint") != fingerprint:
        return None
    greeting = data.get("greeting")
    if not isinstance(greeting, str) or not greeting:
        return None
    charts = data.get("charts") if isinstance(data.get("charts"), list) else []
    suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    return greeting, charts, suggestions


def _save_cached_greeting(
    client_id: str, fingerprint: str, greeting: str, charts: list[dict], suggestions: list[str]
) -> None:
    """Mejor esfuerzo -nunca debe romper el saludo real si falla el guardado (mismo criterio que
    `_log_search_event` en vector_search.py y el resto del logging local de este proyecto)."""
    try:
        GREETING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": fingerprint,
            "greeting": greeting,
            "charts": charts,
            "suggestions": suggestions,
        }
        _greeting_cache_path(client_id).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


GREETING_PROMPT = (
    "Presentate en 1-2 oraciones como Vera Intelligence y armá un menú de 3-4 puntos (no 5) de los "
    "tipos de análisis de negocio en los que podés ayudar a la gerencia de {client_name}, basado "
    "ÚNICAMENTE en las áreas que cubren los campos y criterios reales disponibles para este cliente "
    "-nunca una categoría de negocio genérica que suene relevante pero que no puedas resolver de "
    "verdad con lo que tenés disponible (sin ese respaldo real, no la incluyas). Cada punto: título "
    "corto en negrita seguido de UNA sola frase breve (máximo ~15 palabras), sin sub-cláusulas ni "
    "ejemplos entre paréntesis -nada de listas dentro de un punto. No uses jerga técnica ni menciones "
    "cómo obtenés la información. Cerrá con una sola pregunta corta sobre en qué le gustaría "
    "enfocarse hoy, sin repetir el menú. Toda la respuesta tiene que entrar cómoda en una pantalla "
    "sin scrollear mucho -priorizá que sea corta por sobre completa."
    # El bloque ```vera-suggestions``` con las preguntas de ejemplo NO se pide acá -ya es
    # obligatorio en toda respuesta vía SUGERENCIAS DE SEGUIMIENTO en SYSTEM_INSTRUCTION_TEMPLATE
    # (vi_agent.py, 2026-09-11), incluida ésta (la primera de la conversación). Pedirlo acá
    # también sería redundante -el modelo ya lo hace por default.
)

# Pregunta de descubrimiento fija (2026-09-11) -ver "6. busqueda_vectorial/README.md" > Iteración
# 16: el ángulo con más valor de negocio real de search_conversations es justamente este tipo de
# pregunta abierta y basada en un evento concreto ("qué pidió el cliente que no se resolvió"), no
# una nuance abstracta -encontró patrones reales sin que nadie los buscara a propósito. No se deja
# en manos del modelo (que podría o no incluirla entre las 3 que arma en GREETING_PROMPT) -se
# inyecta siempre como una de las sugerencias, sólo para clientes con vector_search habilitado, en
# vez de depender de que salga por azar.
DISCOVERY_SUGGESTION = (
    "¿Hay algo que los clientes están pidiendo que hoy no estemos resolviendo?"
)


COACHING_SUGGESTION = "¿Qué le recomendarías al equipo para mejorar esta semana?"


_MAX_CONTEXTUAL_SUGGESTIONS = 4


def _with_fixed_suggestions(model_suggestions: list[str]) -> list[str]:
    """Garantiza que las chips fijas (coaching siempre, descubrimiento sólo con vector_search)
    estén presentes, SIN pisarse entre sí -coaching primero, descubrimiento al final, lo que arme
    el modelo en el medio, hasta un máximo de 3 sugerencias del modelo (st.columns(len(...)) es
    dinámico, así que técnicamente soporta más, pero 3 + 2 fijas ya es bastante chip para una fila).

    SÓLO para el saludo inicial (2026-09-14, ver `_contextual_suggestions` para el resto de los
    turnos) -pedido explícito: forzar coaching/descubrimiento en CADA turno significaba que 1 o 2
    de las 3-4 chips visibles nunca cambiaban, lo cual el usuario notó y describió exactamente como
    "no son adaptativas, se repiten siempre las mismas". Se mantienen fijas sólo acá, en el primer
    contacto, para que esas dos capacidades no dependan de que el modelo las mencione por azar
    justo en el saludo -desde la segunda respuesta en adelante, todas las chips son 100%
    contextuales (ver `_contextual_suggestions`).

    Bug real encontrado el 2026-09-11: la versión anterior componía dos funciones independientes
    (`_with_discovery_suggestion(_with_coaching_suggestion(suggestions))`), cada una "reemplazando"
    una posición fija (coaching el primer lugar, descubrimiento el último) sin saber de la otra. Con
    0 o 1 sugerencias de entrada -el caso real del camino de fallback del saludo, que siempre llama
    con `[]`- la segunda función en aplicarse (descubrimiento) terminaba pisando el resultado de la
    primera, y coaching desaparecía en los 7 clientes con vector_search cada vez que el saludo
    fallaba. Acá se arma la lista final de una sola vez, así que ninguna chip fija puede pisar a la
    otra sin importar cuántas sugerencias mandó el modelo."""
    has_discovery = bool(vi_agent.CLIENT_CONFIG.vector_search)
    dynamic = [
        s for s in model_suggestions if s not in (COACHING_SUGGESTION, DISCOVERY_SUGGESTION)
    ][:3]
    result = [COACHING_SUGGESTION, *dynamic]
    if has_discovery:
        result.append(DISCOVERY_SUGGESTION)
    return result


def _contextual_suggestions(model_suggestions: list[str]) -> list[str]:
    """Chips para toda respuesta DESPUÉS del saludo -a diferencia de `_with_fixed_suggestions`,
    nunca fuerza coaching ni descubrimiento acá: son 100% lo que el modelo sugirió atado al
    contenido específico de la respuesta que acaba de dar (ver SUGERENCIAS DE SEGUIMIENTO en
    vi_agent.SYSTEM_INSTRUCTION_TEMPLATE). Sólo recorta a un máximo razonable para una fila de
    chips -`extract_suggestion_blocks` ya limpia/dedupe, esto no repite ese trabajo."""
    return model_suggestions[:_MAX_CONTEXTUAL_SUGGESTIONS]


def _launch_args() -> argparse.Namespace:
    """Lee --client/--model/--internal-debug de sys.argv (todo lo que Streamlit deja después de
    "--") sin pisar sus propios flags. --model acá es sólo la PRESELECCIÓN inicial del selectbox de
    la barra lateral (ver main()) -a diferencia de --internal-debug, sí se puede cambiar después
    desde la UI, porque elegir modelo es una decisión de prueba visible y reversible, no un
    toggle que exponga trazas técnicas. --internal-debug sólo puede venir de acá -del comando con el
    que se arrancó el proceso-, nunca de un toggle en la UI, para que sea imposible habilitarlo sin
    querer desde una interfaz que un cliente pudiera ver."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--client", default=None)
    parser.add_argument("--model", default=None, choices=vi_agent.AVAILABLE_MODELS)
    parser.add_argument("--internal-debug", "--debug", dest="internal_debug", action="store_true")
    known, _ = parser.parse_known_args(sys.argv[1:])
    return known


st.set_page_config(
    page_title="Vera Intelligence",
    page_icon="✨",
    layout="centered",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    /* Achica el header por default de Streamlit -no aporta nada acá y roba espacio vertical. */
    header[data-testid="stHeader"] { height: 0; }
    .block-container { padding-top: 1.5rem; max-width: 46rem; }

    .vera-hero {
        display: flex; align-items: center; gap: .6rem;
        padding: .9rem 1.1rem; border-radius: 14px; margin-bottom: 1rem;
        background: linear-gradient(135deg, #6D5BD0 0%, #8F7FF7 100%);
        color: white;
    }
    .vera-hero .dot {
        width: 10px; height: 10px; border-radius: 50%;
        background: #35D07F; box-shadow: 0 0 0 3px rgba(53,208,127,.25);
    }
    .vera-hero .title { font-weight: 700; font-size: 1.05rem; line-height: 1.1; }
    .vera-hero .subtitle { font-size: .8rem; opacity: .85; }

    [data-testid="stChatMessage"] { border-radius: 14px; }

    /* Chips de preguntas sugeridas -st.button no tiene una variante "chip" nativa, así que se
       angostan y redondean acá en vez de usar el ancho completo por default. */
    .st-key-vera-suggestions button {
        border-radius: 14px !important;
        font-size: .82rem !important;
        padding: .5rem .9rem !important;
        height: auto !important;
        min-height: 2.4rem;
        white-space: normal !important;
        line-height: 1.25 !important;
    }
    .st-key-vera-suggestions button p {
        white-space: normal !important;
        overflow-wrap: break-word;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# Progreso en vivo por tool call (2026-09-11) -etiquetas de negocio para cada tool técnica del
# loop manual (ver vi_agent.TOOL_FUNCTIONS), nunca el nombre real: mismo criterio de caja negra
# que la respuesta final (nunca mostrar snake_case/nombres internos a gerencia). Default genérico
# para cualquier tool nueva que se agregue sin actualizar este mapeo -mejor un mensaje genérico
# que uno técnico filtrado por accidente.
_TOOL_PROGRESS_LABELS = {
    "run_readonly_sql": "Consultando datos...",
    "get_business_rules": "Revisando criterios de negocio...",
    # Con statement_timeout en 180s (ver utils/postgres.py) esta búsqueda puede tardar bastante más
    # que las demás -sin el índice ANN sobre analytics_v2.conversation_embeddings, medimos en vivo
    # casos de hasta 148,8s (ver "6. busqueda_vectorial/README.md" > "Iteración 25"). Antes de esa
    # medición, un usuario que esperaba 2-3 minutos frente a un spinner sin ninguna pista podía
    # pensar que la app se colgó -este texto pone la expectativa por adelantado en vez de que se
    # entere recién si le toca un caso lento.
    "search_conversations": "Buscando ejemplos en conversaciones... (puede tardar hasta 3 minutos)",
}


def _tool_progress_label(tool_name: str, args: dict | None = None) -> str:
    """Texto de progreso por tool. Para la búsqueda de conversaciones, si los argumentos dicen qué se
    está leyendo (un vendedor, el equipo, la comparación con quienes mejor cumplen) el texto lo
    dice -una espera de ~10-20 s con un mensaje genérico parece una falla (2026-09-21)."""
    if tool_name == "search_conversations" and isinstance(args, dict):
        employee = args.get("employee_name")
        who = (
            f"las conversaciones de {employee.strip()}"
            if isinstance(employee, str) and employee.strip()
            else "conversaciones del equipo"
        )
        text = f"Leyendo {who}"
        if args.get("comparar_con_mejores") is True:
            text += " y las de compañeros con mejor resultado"
        return text + "... (suele tardar entre 10 y 20 segundos)"
    return _TOOL_PROGRESS_LABELS.get(tool_name, "Analizando...")


def _build_chart_figure(chart: dict):
    """Arma la figura de Plotly para un chart ya validado por vi_agent.extract_chart_blocks.

    Separado de `_render_chart` (2026-09-14, pedido explícito: "muchísimo mejores gráficos, más
    opciones, que no haga siempre gráficos de barra") para poder probar la construcción de la
    figura sin necesitar `streamlit` real -sólo llama a Plotly, nunca a `st.*`. Reemplaza los
    *_chart nativos de Streamlit (bar_chart/line_chart/scatter_chart, cubrían sólo 4 tipos con
    estética muy básica) por Plotly, agregado como dependencia real a pedido del usuario -ver
    requirements.txt."""
    chart_type = chart["type"]
    title = chart.get("title") or None

    if chart_type in ("bar", "hbar"):
        df = pd.DataFrame({"categoria": chart["labels"], "valor": chart["values"]})
        if chart_type == "bar":
            fig = px.bar(df, x="categoria", y="valor", title=title, text_auto=True)
        else:
            fig = px.bar(df, y="categoria", x="valor", orientation="h", title=title, text_auto=True)
            # El primer valor de `labels` queda arriba, no abajo -mismo orden en que el modelo
            # arma el ranking en el texto de la respuesta (ej. 1ro, 2do, 3ro de arriba hacia abajo).
            fig.update_yaxes(autorange="reversed")
        fig.update_layout(xaxis_title=None, yaxis_title=None, showlegend=False)
        return fig

    if chart_type in ("line", "area"):
        df = pd.DataFrame({"categoria": chart["labels"], "valor": chart["values"]})
        plot_fn = px.line if chart_type == "line" else px.area
        fig = plot_fn(df, x="categoria", y="valor", title=title, markers=(chart_type == "line"))
        fig.update_layout(xaxis_title=None, yaxis_title=None, showlegend=False)
        return fig

    if chart_type in ("pie", "donut"):
        df = pd.DataFrame({"categoria": chart["labels"], "valor": chart["values"]})
        fig = px.pie(
            df, names="categoria", values="valor", title=title,
            hole=0.5 if chart_type == "donut" else 0.0,
        )
        fig.update_traces(textinfo="percent+label")
        return fig

    if chart_type in ("grouped_bar", "stacked_bar"):
        rows = [
            {"categoria": label, "serie": serie["name"], "valor": value}
            for serie in chart["series"]
            for label, value in zip(chart["labels"], serie["values"])
        ]
        df = pd.DataFrame(rows)
        fig = px.bar(
            df, x="categoria", y="valor", color="serie", title=title,
            barmode="group" if chart_type == "grouped_bar" else "stack",
        )
        fig.update_layout(xaxis_title=None, yaxis_title=None, legend_title=None)
        return fig

    # scatter, bubble
    df = pd.DataFrame({"x": chart["x_values"], "y": chart["values"]})
    kwargs = {}
    if chart_type == "bubble":
        df["tamaño"] = chart["sizes"]
        kwargs["size"] = "tamaño"
    fig = px.scatter(df, x="x", y="y", title=title, **kwargs)
    fig.update_layout(xaxis_title=None, yaxis_title=None, showlegend=False)
    return fig


def _render_chart(chart: dict) -> None:
    """Renderiza un chart ya validado por vi_agent.extract_chart_blocks con Plotly, con el tema
    nativo de Streamlit (`theme="streamlit"`) para que los colores/tipografía sigan el estilo del
    resto de la app sin tener que themear manualmente."""
    st.plotly_chart(_build_chart_figure(chart), theme="streamlit", use_container_width=True)


def _render_tool_summary(tool_calls: list[dict]) -> None:
    """Caption liviano con qué tool técnica se llamó y cuánto tardó -SIEMPRE visible, a diferencia
    de `_render_debug_trace` (sólo con --internal-debug, que además muestra args/SQL/resultado
    completos). Pedido explícito (2026-09-11): la idea 2 de la ronda de mejoras ("panel de latencia
    por tool") había quedado atrás del gate de debug por defecto -y, a diferencia del plan original,
    el usuario pidió el NOMBRE TÉCNICO de la tool acá (no sólo la etiqueta de negocio), justamente
    para poder confirmar en pruebas si una pregunta disparó la tool esperada (ej. verificar que
    search_conversations se use para preguntas de ejemplos puntuales, no sólo run_readonly_sql) sin
    tener que activar --internal-debug para verlo. Sigue sin mostrar argumentos, SQL generado ni
    resultado -eso sigue exclusivo de `_render_debug_trace`, mismo criterio de caja negra que la
    respuesta final para lo que sí podría revelar identificadores físicos/PII. Agrupa por tool para
    no repetir una línea por cada llamada cuando el modelo iteró la misma tool varias veces."""
    if not tool_calls:
        return
    by_tool: dict[str, tuple[int, float]] = {}
    for call in tool_calls:
        name = call["name"]
        count, ms = by_tool.get(name, (0, 0.0))
        by_tool[name] = (count + 1, ms + (call.get("elapsed_ms") or 0))
    total_seconds = sum(ms for _, ms in by_tool.values()) / 1000
    breakdown = " · ".join(
        f"`{name}` {count}× {ms / 1000:.1f}s" for name, (count, ms) in by_tool.items()
    )
    st.caption(f"🔧 {total_seconds:.1f}s — {breakdown}")


def _render_debug_trace(tool_calls: list[dict]) -> None:
    """Expander colapsado con las tool calls de una respuesta -sólo se llama cuando
    --internal-debug está activo (ver main()), nunca por default. Mismo contenido que --cli
    imprime en stderr, pero legible en la interfaz web en vez de en la consola donde corre
    streamlit.

    Total en el label del expander (2026-09-11) -suma de `elapsed_ms` de todas las llamadas de
    esta respuesta (ver run_tool_loop en vi_agent.py), no el tiempo de la respuesta completa
    (que además incluye las llamadas al modelo entre tool calls, no medidas acá): da una idea
    rápida de cuánto de la espera fue ejecutar tools vs. esperar a Gemini, sin tener que sumar
    a mano cada línea de abajo."""
    if not tool_calls:
        return
    total_ms = sum(call.get("elapsed_ms") or 0 for call in tool_calls)
    label = (
        f"🔧 Trazas internas ({len(tool_calls)} llamada{'s' if len(tool_calls) != 1 else ''}"
        f", {total_ms:.0f} ms en tools)"
    )
    with st.expander(label):
        for call in tool_calls:
            elapsed = call.get("elapsed_ms")
            timing = f" · {elapsed:.0f} ms" if elapsed is not None else ""
            st.markdown(f"**{call['name']}**`({call['args']})`{timing}")
            if call.get("error"):
                st.error(call["error"])
                continue
            result = call.get("result")
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            if len(text) > 1500:
                text = text[:1500] + "… (truncado)"
            st.code(text, language="json")


# Las conversaciones se CITAN (texto entre comillas, caso puntual, botón "🔊 Escuchar") sólo cuando la
# pregunta pide ejemplos, citas, audios o casos reales (pedido explícito, 2026-09-21). En el resto de
# las respuestas -coaching, diagnósticos- las conversaciones informan el consejo pero no se muestran
# como citas. Misma lista de señales que el prompt de search_conversations (vi_agent.py).
_EXAMPLE_REQUEST_RE = re.compile(
    r"ejemplo|\bcit[aeo]s?\b|\bcitame|textual|\baudios?\b|escuch|grabaci|casos? real(?:es)?\b|"
    r"en qu[eé] conversaci|qu[eé] (?:dijo|dicen?|le dijo)|c[oó]mo (?:lo )?(?:dijo|dice)|"
    r"qui[eé]n (?:dijo|dice|lo dijo)|mostr[aá]me\b.*\bconversaci",
    re.IGNORECASE,
)


def _asks_for_examples(question: str | None) -> bool:
    """¿La pregunta del usuario pide ejemplos/citas/audios de conversaciones? Lógica pura, testeable."""
    return bool(question and _EXAMPLE_REQUEST_RE.search(question))


def _extract_citable_conversations(tool_calls: list[dict]) -> list[dict]:
    """Lógica pura (sin `st`, testeable directo): de todas las llamadas a search_conversations de
    esta respuesta, arma la lista de conversaciones reales distintas que se pueden ofrecer para
    escuchar -deduplicadas por conversation_id (una búsqueda puede haber traído el mismo resultado
    más de una vez si el modelo reformuló), en el orden en que aparecieron. Tool calls con error,
    resultado no parseable, o sin conversation_id se ignoran en silencio -mismo criterio de
    "mejor esfuerzo" que el resto de esta función (ver _render_audio_players).

    Incluye tanto `resultados` (conversaciones del vendedor) como `companeros` (2026-09-22, bug
    real: en modo `comparar_con_mejores` sólo se leía `resultados`, así que las conversaciones de
    compañeros con mejor resultado -las que sí muestran cómo se hace bien- nunca aparecían acá,
    aunque la sección "PRIVACIDAD" del prompt ya asumía que el usuario podía escucharlas sin el
    nombre del compañero en el texto; confirmado con Lucas que los nombres reales quedan visibles
    en este panel a propósito, ver "6. busqueda_vectorial/README.md")."""
    conversations: list[dict] = []
    seen_conversation_ids: set[str] = set()
    for call in tool_calls:
        if call.get("name") != "search_conversations" or call.get("error"):
            continue
        try:
            payload = json.loads(call.get("result") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for grupo_key in ("resultados", "companeros"):
            for resultado in payload.get(grupo_key, []) or []:
                conversation_id = resultado.get("conversation_id")
                if not conversation_id or conversation_id in seen_conversation_ids:
                    continue
                seen_conversation_ids.add(conversation_id)
                conversations.append(resultado)
    return conversations


def _extract_search_usage(tool_calls: list[dict]) -> dict:
    """Lógica pura (sin `st`): ¿esta respuesta usó `search_conversations`? Cuántas veces, y qué
    avisos de calidad de datos trajo -más allá del texto fijo "los fragmentos son una
    reconstrucción aproximada" que aparece siempre y no aporta nada nuevo acá-. Pedido explícito de
    Lucas (2026-09-22): poder darse cuenta, al usar el chat, de que una respuesta se apoyó en
    búsqueda vectorial y no sólo en SQL -hoy ese dato sólo aparecía como el nombre técnico
    `search_conversations` dentro de `_render_tool_summary`, mezclado con las demás tools y fácil
    de pasar por alto. También surge acá, por primera vez visible para quien lee la respuesta (antes
    sólo viajaba en el JSON que ve el modelo), cualquier aviso de calidad de datos que la tool haya
    agregado -ej. la nota de cobertura de embeddings baja para Atlas/Salomon, ver
    `_LOW_EMBEDDING_COVERAGE_TENANTS` en vector_search.py, Iteración 42."""
    count = 0
    avisos: list[str] = []
    for call in tool_calls:
        if call.get("name") != "search_conversations" or call.get("error"):
            continue
        count += 1
        try:
            payload = json.loads(call.get("result") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        aviso = payload.get("aviso")
        if not isinstance(aviso, str):
            continue
        # La primera parte (separada por "; ") es siempre el texto fijo de reconstrucción
        # aproximada -no aporta nada nuevo, se omite para no repetir lo obvio en cada respuesta.
        partes = [p.strip() for p in aviso.split(";")]
        for parte in partes[1:]:
            if parte and parte not in avisos:
                avisos.append(parte)
    return {"used": count > 0, "count": count, "avisos": avisos}


def _format_conversation_date(fecha: object) -> str:
    """Convierte la fecha ISO cruda que devuelve search_conversations (ej.
    "2026-07-25T20:53:37.809000+00:00") a un formato legible ("25/07/2026 20:53") para el panel de
    audio -antes se mostraba tal cual, con microsegundos y offset UTC incluidos (2026-09-22, bug
    real reportado por Lucas). Si no se puede parsear, se devuelve el valor original sin romper el
    render -mismo criterio "mejor esfuerzo" que el resto de esta función."""
    if not isinstance(fecha, str) or not fecha:
        return ""
    try:
        parsed = datetime.fromisoformat(fecha)
    except ValueError:
        return fecha
    return parsed.strftime("%d/%m/%Y %H:%M")


def _render_audio_players(tool_calls: list[dict], message_key: str) -> None:
    """Botón "🔊 Escuchar" por cada conversación real citada por search_conversations en esta
    respuesta -pedido explícito (2026-09-14): poder escuchar el audio original, no sólo leer el
    fragmento de texto reconstruido.

    La URL firmada se resuelve on-demand -recién al hacer click, nunca eager para todos los
    resultados de golpe- vía `audio_playback.resolve_audio_url`. Ese módulo (y por lo tanto esta
    función) NUNCA pasa por el modelo: `conversation_id` es el único dato que se toma de un
    resultado ya mostrado, `filename`/`bucketName`/la URL en sí quedan siempre del lado del
    servidor -ver el docstring de audio_playback.py para el porqué (identificadores técnicos,
    regla de caja negra).

    `message_key` identifica la respuesta (no cada conversación individual) para namespacing de
    las keys de los widgets -tiene que ser estable durante toda la vida de este mensaje en
    session_state, ver el `message_id` generado ANTES de renderizar en el bloque de pregunta
    nueva de main(), y `message["message_id"]` para el historial ya persistido.

    Sin tests unitarios a propósito -mismo criterio que el resto de las funciones con
    columnas/botones de este archivo (ver el comentario al importar streamlit en
    5. tests/test_streamlit_app.py): se verifica en vivo con el navegador, no con `st` mockeado."""
    conversations = _extract_citable_conversations(tool_calls)
    if not conversations:
        return

    # "analizadas", no "citadas" (2026-09-22): desde que search_conversations nunca cita texto
    # literal en la respuesta (ver response_policy.py, NUNCA CITES TEXTUAL) y sólo menciona
    # tienda/fecha de un caso cuando se piden ejemplos explícitamente, "citadas" describía mal lo
    # que este panel en realidad ofrece -las conversaciones que la tool encontró y usó como
    # insumo del análisis, se hayan mencionado por nombre en el texto o no.
    with st.expander(f"🔊 Escuchar conversaciones analizadas ({len(conversations)})"):
        for resultado in conversations:
            conversation_id = resultado["conversation_id"]
            label = " · ".join(
                str(bit) for bit in (
                    resultado.get("tienda"),
                    resultado.get("vendedor"),
                    _format_conversation_date(resultado.get("fecha")),
                )
                if bit
            ) or "conversación"
            state_key = f"audio_url_{message_key}_{conversation_id}"
            col_label, col_button = st.columns([4, 1])
            col_label.markdown(label)
            if state_key not in st.session_state:
                if col_button.button("🔊 Escuchar", key=f"audio_btn_{message_key}_{conversation_id}"):
                    with st.spinner("Buscando el audio..."):
                        st.session_state[state_key] = (
                            audio_playback.resolve_audio_url(
                                tenant=vi_agent.CLIENT_CONFIG.tenant,
                                conversation_id=conversation_id,
                            )
                            or {}
                        )
                    st.rerun()
            else:
                resolved = st.session_state[state_key]
                if resolved.get("url"):
                    st.audio(resolved["url"])
                else:
                    st.caption("No se pudo cargar el audio de esta conversación.")


def _render_vector_search_badge(tool_calls: list[dict]) -> None:
    """Aviso liviano, SIEMPRE visible, cuando `search_conversations` aportó a esta respuesta -ver
    `_extract_search_usage` para el motivo (pedido explícito de Lucas, 2026-09-22). Complementa a
    `_render_tool_summary` (ese caption ya lo decía con el nombre técnico de la tool, pero sin
    destacarlo ni mostrar los avisos de calidad de datos que trajo)."""
    info = _extract_search_usage(tool_calls)
    if not info["used"]:
        return
    label = "🔎 Esta respuesta se apoya en búsqueda semántica sobre conversaciones reales"
    if info["count"] > 1:
        label += f" ({info['count']} búsquedas)"
    st.caption(label)
    for aviso in info["avisos"]:
        st.caption(f"⚠️ {aviso}")


def _render_message(
    content: str,
    charts: list[dict],
    tool_calls: list[dict] | None = None,
    *,
    debug: bool = False,
    message_key: str | None = None,
    show_audio: bool = True,
) -> None:
    if content:
        st.markdown(content)
    for chart in charts:
        _render_chart(chart)
    if tool_calls:
        _render_vector_search_badge(tool_calls)
        _render_tool_summary(tool_calls)
        if debug:
            _render_debug_trace(tool_calls)
        if message_key and show_audio:
            _render_audio_players(tool_calls, message_key)


def _record_feedback(message: dict, vote: str) -> None:
    """Loguea un voto 👍/👎 sobre `message` -ver feedback_tracking.py para el motivo y qué guarda
    (pregunta + respuesta, a diferencia del resto de los logs locales del proyecto). Actualiza
    `message["feedback"]` en session_state para que el botón ya votado quede deshabilitado hasta
    el próximo rerun -sin esto, un click accidental duplicado en el mismo mensaje volvería a
    loguear el mismo voto."""
    message["feedback"] = vote
    tool_names = sorted({call["name"] for call in message.get("tool_calls") or []})
    _FEEDBACK_RECORDER.record(
        client_id=vi_agent.CLIENT_CONFIG.client_id,
        session_id=st.session_state.get("session_id", ""),
        message_id=message["message_id"],
        vote=vote,
        question=message.get("question"),
        answer=message["content"],
        tool_names=tool_names,
    )


def _render_feedback_buttons(message: dict) -> None:
    """👍/👎 debajo de cada respuesta del asistente -pedido explícito (2026-09-11): hoy no hay
    ninguna señal real de qué respuestas sirven, toda la iteración de prompts de esta sesión se
    basó en casos puntuales notados a mano. El botón ya votado queda deshabilitado (`disabled=`)
    para dejar claro que el voto quedó registrado, pero el OTRO sigue habilitado -cambiar de voto
    es válido y loguea un evento nuevo (sólo el último vale para ese mensaje, ver
    feedback_tracking.py)."""
    current_vote = message.get("feedback")
    col_up, col_down, _spacer = st.columns([1, 1, 10])
    with col_up:
        if st.button(
            "👍", key=f"fb_up_{message['message_id']}", disabled=current_vote == "up"
        ):
            _record_feedback(message, "up")
            st.rerun()
    with col_down:
        if st.button(
            "👎", key=f"fb_down_{message['message_id']}", disabled=current_vote == "down"
        ):
            _record_feedback(message, "down")
            st.rerun()


def _session_abandoned_checker():
    """Captura la sesión real; is_active_session es seguro desde cualquier thread."""
    from streamlit.runtime import exists, get_instance
    from streamlit.runtime.scriptrunner import get_script_run_ctx

    ctx = get_script_run_ctx(suppress_warning=True)
    if ctx is None or not exists():
        return None
    runtime = get_instance()
    session_id = ctx.session_id
    return lambda: not runtime.is_active_session(session_id)


def _cancel_active_analysis() -> None:
    control = st.session_state.get("analysis_control")
    if control is not None:
        control.cancel()
        st.session_state["restore_history"] = st.session_state.get("analysis_history", [])
        st.session_state["cancelled_question"] = st.session_state.get("analysis_question")


@contextmanager
def _ui_run_scope(run_id: str | None):
    """No permite que un script sustituido escriba en el chat de otro rerun."""
    with ACTIVE_CLIENT_LOCK:
        if run_id is not None and st.session_state.get("ui_run_id") != run_id:
            st.stop()
            raise AnalysisCancelled("Análisis cancelado.")
        yield


@contextmanager
def _analysis_request(question: str | None = None, *, ui_run_id: str | None = None):
    """Serializa el cliente global y conserva el historial anterior a un intercambio incompleto."""
    control = AnalysisControl(_session_abandoned_checker())
    with _ui_run_scope(ui_run_id):
        history = list(st.session_state["chat"].get_history(curated=True))
        st.session_state["analysis_control"] = control
        st.session_state["analysis_history"] = history
        st.session_state["analysis_question"] = question
        try:
            control.check()
            yield control
            control.check()
        except BaseException:
            # Incluye StopException/RerunException de Streamlit: no son Exception.
            if st.session_state.get("analysis_control") is control:
                st.session_state["restore_history"] = history
                st.session_state["cancelled_question"] = question
            control.check()
            raise
        finally:
            control.cancel()
            if st.session_state.get("analysis_control") is control:
                for key in ("analysis_control", "analysis_history", "analysis_question"):
                    st.session_state.pop(key, None)


def _restore_interrupted_chat(*, mark_cancelled: bool = True) -> None:
    if "restore_history" not in st.session_state:
        return
    with ACTIVE_CLIENT_LOCK:
        history = st.session_state["restore_history"]
        st.session_state["chat"] = vi_agent.build_chat(history=history)
        st.session_state.pop("restore_history", None)
        local_exchange = st.session_state.pop("restore_local_exchange", None)
        if local_exchange is not None:
            vi_agent.record_local_exchange(st.session_state["chat"], *local_exchange)
        question = st.session_state.pop("cancelled_question", None)
        messages = st.session_state.get("messages", [])
        if mark_cancelled and question and messages and messages[-1].get("role") == "user" and messages[-1].get("content") == question:
            vi_agent.record_local_exchange(st.session_state["chat"], question, "Análisis cancelado.")
            messages.append({
                "role": "assistant", "content": "Análisis cancelado.", "charts": [],
                "tool_calls": [], "message_id": uuid.uuid4().hex,
                "question": question, "feedback": None,
            })


def _record_local_failure(question: str | None, answer: str, *, ui_run_id: str | None = None) -> None:
    with _ui_run_scope(ui_run_id):
        try:
            _restore_interrupted_chat(mark_cancelled=False)
        except OperationalUnavailable:
            # Conservar el snapshot y no reutilizar el chat incompleto hasta recuperar acceso.
            st.session_state.pop("chat", None)
            if question is not None:
                st.session_state["restore_local_exchange"] = (question, answer)
            return
        if question is not None:
            vi_agent.record_local_exchange(st.session_state["chat"], question, answer)


def _reset_session_state(*, preserve_ui_run: bool = False) -> None:
    _cancel_active_analysis()
    with ACTIVE_CLIENT_LOCK:
        for key in ("chat", "session_id", "messages", "greeted", "suggestions", "pending_question",
                    "analysis_control", "analysis_history", "analysis_question", "restore_history", "cancelled_question",
                    "restore_local_exchange"):
            st.session_state.pop(key, None)
        if not preserve_ui_run:
            st.session_state.pop("ui_run_id", None)


def _init_client(client_id: str, display_name: str, model_override: str) -> None:
    """Configura vi_agent para client_id/model_override y arranca una sesión de chat nueva en
    session_state. No hace nada si ya estamos en ese mismo cliente Y modelo (Streamlit re-ejecuta
    el script entero en cada interacción, así que esto se llama todo el tiempo -sólo debe
    inicializar una vez por combinación). Cambiar el modelo desde la barra lateral reinicia la
    sesión igual que cambiar de cliente -mismo criterio: el chat de Gemini ya tiene el modelo
    anterior fijado internamente, no hay forma de "cambiarle el modelo" a una sesión en curso."""
    _cancel_active_analysis()
    same_selection = (
        st.session_state.get("client_id") == client_id
        and st.session_state.get("model_override") == model_override
    )
    if same_selection and ("chat" in st.session_state or "restore_history" in st.session_state):
        _restore_interrupted_chat()
        return
    _reset_session_state(preserve_ui_run=True)
    st.session_state["client_id"] = client_id
    st.session_state["model_override"] = model_override
    with st.spinner(f"Conectando con {display_name}..."), ACTIVE_CLIENT_LOCK:
        vi_agent.configure_client(client_id, model_override=model_override)
        vi_agent.load_environment()
        vi_agent.prewarm_sql_connection()
        st.session_state["chat"] = vi_agent.build_chat()
        st.session_state["session_id"] = uuid.uuid4().hex
        st.session_state["messages"] = []
        st.session_state["greeted"] = False


def _ensure_greeting(display_name: str, *, debug: bool) -> None:
    if st.session_state.get("greeted"):
        return
    ui_run_id = st.session_state.get("ui_run_id")
    try:
        client_id = st.session_state["client_id"]
        fingerprint = _greeting_cache_fingerprint()
        cached = _load_cached_greeting(client_id, fingerprint)
        tool_calls_log: list[dict] = []
        if cached is not None:
            # Saludo servido desde disco -el Data Map/tools de este cliente no cambiaron desde el
            # último saludo real generado, así que el texto sigue siendo válido sin volver a pagar
            # la llamada completa a Gemini. tool_calls_log queda vacío a propósito: no hubo tool
            # calls en ESTA ejecución, mostrar los de la corrida original sería engañoso en el
            # panel de debug.
            greeting, charts, suggestions = cached
        else:
            with st.status("Preparando la presentación...", state="running") as status, _analysis_request(ui_run_id=ui_run_id) as control:
                st.button("Cancelar presentación", key="cancel_greeting", on_click=_cancel_active_analysis)
                raw_greeting = vi_agent.run_tool_loop(
                    st.session_state["chat"],
                    GREETING_PROMPT.format(client_name=display_name)
                    + vi_agent.greeting_vector_search_clause(),
                    max_tool_calls=20,
                    debug=False,
                    session_id=st.session_state["session_id"],
                    tool_calls_log=tool_calls_log,
                    analysis_control=control,
                    on_tool_call=lambda name, _args: status.update(label=_tool_progress_label(name, _args)),
                )
                status.update(label="Listo", state="complete")
            greeting, charts = vi_agent.extract_chart_blocks(raw_greeting)
            greeting, suggestions = vi_agent.extract_suggestion_blocks(greeting)
            _save_cached_greeting(client_id, fingerprint, greeting, charts, suggestions)
        with _ui_run_scope(ui_run_id):
            st.session_state["messages"].append(
                {
                    "role": "assistant",
                    "content": greeting,
                    "charts": charts,
                    "tool_calls": tool_calls_log,
                    "message_id": uuid.uuid4().hex,
                    "question": None,  # saludo inicial, no responde a una pregunta puntual
                    "feedback": None,
                }
            )
            st.session_state["suggestions"] = _with_fixed_suggestions(suggestions)
    except AnalysisCancelled:
        st.stop()
        return
    except Exception:  # noqa: BLE001
        _record_local_failure(None, "", ui_run_id=ui_run_id)
        with _ui_run_scope(ui_run_id):
            st.session_state["messages"].append(
                {
                    "role": "assistant",
                    "content": f"¡Hola! Soy Vera Intelligence, tu asesor de negocio para {display_name}. ¿En qué puedo ayudarte hoy?",
                    "charts": [],
                    "tool_calls": [],
                    "message_id": uuid.uuid4().hex,
                    "question": None,
                    "feedback": None,
                }
            )
            st.session_state["suggestions"] = _with_fixed_suggestions([])
    with _ui_run_scope(ui_run_id):
        st.session_state["greeted"] = True


def _render_suggestions() -> None:
    """Chips clickeables con preguntas de seguimiento -se refrescan después de CADA respuesta del
    asistente (2026-09-11, pedido explícito: "que siempre aparezcan nuevas burbujas según el
    contexto"), no sólo tras el saludo inicial como antes. `st.session_state["suggestions"]` se
    reescribe en cada turno (ver `_ensure_greeting` y el flujo de preguntas en `main()`) con lo que
    el modelo sugirió para la respuesta que ACABA de dar -el modelo ya las genera atadas al
    contenido específico de esa respuesta (ver SUGERENCIAS DE SEGUIMIENTO en
    SYSTEM_INSTRUCTION_TEMPLATE, vi_agent.py), así que esta función sólo necesita mostrar lo que
    ya está en session_state, nunca decidir contenido."""
    suggestions = st.session_state.get("suggestions") or []
    if not suggestions:
        return
    # st.container(key=...) -no dos st.markdown con tags de apertura/cierre sueltos, como antes-
    # es lo que realmente envuelve los botones en el DOM (agrega la clase .st-key-vera-suggestions
    # al contenedor), así que el CSS de abajo los puede alcanzar de verdad. Con los dos st.markdown
    # sueltos el <div class="vera-suggestions"> quedaba vacío -cada llamada a un elemento de
    # Streamlit es su propio nodo hermano, no anida lo que viene después- y el CSS nunca aplicaba,
    # de ahí el texto cortado con "..." en los botones.
    with st.container(key="vera-suggestions"):
        cols = st.columns(len(suggestions))
        for col, suggestion in zip(cols, suggestions):
            with col:
                if st.button(
                    suggestion, key=f"suggestion_{hash(suggestion)}", use_container_width=True
                ):
                    st.session_state["pending_question"] = suggestion
                    st.rerun()


def _conversation_as_markdown(display_name: str) -> str:
    lines = [f"# Conversación con Vera Intelligence — {display_name}", ""]
    for message in st.session_state.get("messages", []):
        speaker = "Usuario" if message["role"] == "user" else "Vera Intelligence"
        lines.append(f"**{speaker}:**")
        lines.append(message["content"])
        lines.append("")
    return "\n".join(lines)


def _render_usage_sidebar() -> None:
    """Costo/consumo de la sesión actual -sin llamada extra al modelo, sólo relee el log local
    que cada llamada real ya escribe (ver usage_tracking.UsageRecorder). Pensado para el tester:
    quien está probando preguntas ve en vivo qué le cuesta cada una."""
    session_id = st.session_state.get("session_id")
    if not session_id:
        return
    events = [
        event
        for event in load_usage_events(vi_agent.USAGE_LOG_PATH)
        if event.get("session_id") == session_id
    ]
    if not events:
        return
    summary = summarize_usage(events)
    model = events[-1].get("model", "")
    cost = estimate_cost_usd(summary, model=model)
    st.caption("Consumo de esta sesión")
    c1, c2 = st.columns(2)
    c1.metric("Llamadas", summary["calls"])
    c2.metric("Tokens totales", f"{summary['total_token_count']:,}".replace(",", "."))
    if cost is not None:
        st.metric("Costo estimado", f"US$ {cost:.4f}")
    else:
        st.caption(f"Sin pricing conocido para el modelo `{model}`.")


def _has_vector_search(client_id: str) -> bool:
    """True si el cliente tiene búsqueda vectorial habilitada en su config.yaml -sólo para el
    badge del selector de cliente, nunca afecta qué tools se exponen (eso lo decide
    vi_agent._build_tools_list a partir de CLIENT_CONFIG, no esta función)."""
    try:
        return load_client_config(client_id).vector_search is not None
    except Exception:  # noqa: BLE001
        return False


def _check_shared_password() -> bool:
    """Portón de contraseña compartida (2026-09-17, pedido explícito: la app quedó desplegada con
    link público -cualquiera que lo consiga entra directo, sin registro de quién- y esto agrega una
    fricción mínima antes de mostrar datos reales de negocio de los 19 clientes). No es login
    individual (no identifica quién entró, no hay control de acceso por persona) -es sólo una traba
    contra el "alguien reenvía el link sin querer" que preocupaba. Para eso, ver la alternativa de
    login con Google + dominio ya evaluada y descartada por ahora en "8. README.md".

    La contraseña real vive ÚNICAMENTE como secret (`VI_DEMO_PASSWORD`), nunca en este archivo -así
    no queda en el historial de git de un repo que puede terminar público. Si el secret no está
    configurado, la app queda cerrada por default (fail-closed): mejor un demo roto por olvido de
    configuración que un demo abierto sin que nadie se dé cuenta.

    CORREGIDO (2026-09-18, bug real: local mostraba "no configurada" con VI_DEMO_PASSWORD sí
    presente en ".env"): esta es la PRIMERA acción de main(), pero el resto de la app sólo carga
    el ".env" (vi_agent.load_environment(), dentro de _init_client()) recién después de elegir
    cliente -mucho más tarde en el flujo. En el primer render, os.getenv("VI_DEMO_PASSWORD") corría
    contra variables de entorno del proceso, nunca contra lo que hay en ".env". Streamlit Cloud no
    lo sufre (sus secrets ya son variables de entorno reales desde que arranca el proceso), pero
    cualquier corrida local sí -por eso hace falta cargar el ".env" acá explícitamente, antes de
    leer la variable, sin depender de que el usuario ya haya interactuado con la app."""
    load_dotenv(vi_agent.REPO_ROOT / ".env")
    load_dotenv(vi_agent.PROJECT_ROOT / ".env", override=True)
    expected = os.getenv("VI_DEMO_PASSWORD")
    if st.session_state.get("shared_password_ok"):
        return True
    st.markdown("### 🔒 Acceso restringido")
    if not expected:
        st.error(
            "Esta app no tiene configurada la contraseña de acceso (VI_DEMO_PASSWORD) -avisale a "
            "quien la administra."
        )
        return False
    entered = st.text_input("Contraseña", type="password", key="shared_password_input")
    if st.button("Entrar"):
        if entered == expected:
            st.session_state["shared_password_ok"] = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    return False


def main() -> None:
    if not _check_shared_password():
        st.stop()
    _cancel_active_analysis()
    ui_run_id = uuid.uuid4().hex
    with ACTIVE_CLIENT_LOCK:
        st.session_state["ui_run_id"] = ui_run_id
    names = available_clients_with_display_names()
    if not names:
        st.error("No hay clientes configurados bajo `2. clientes/`.")
        return

    ordered = sorted(names.items(), key=lambda item: item[1].lower())
    display_names = [name for _, name in ordered]
    client_ids_by_display_name = {name: client_id for client_id, name in ordered}
    display_names_by_client_id = {client_id: name for client_id, name in ordered}
    vector_search_badge_by_display_name = {
        name: _has_vector_search(client_id) for client_id, name in ordered
    }

    launch_args = _launch_args()
    preselected_client_id = launch_args.client
    debug = launch_args.internal_debug
    default_index = 0
    if preselected_client_id and preselected_client_id in display_names_by_client_id:
        default_index = display_names.index(display_names_by_client_id[preselected_client_id])
    default_model_index = (
        vi_agent.AVAILABLE_MODELS.index(launch_args.model) if launch_args.model else 0
    )

    with st.sidebar:
        st.markdown("### Vera Intelligence")
        st.caption("Asesor de negocio en lenguaje natural")
        chosen_display_name = st.selectbox(
            "Cliente",
            display_names,
            index=default_index,
            key="client_selector",
            on_change=_cancel_active_analysis,
            format_func=lambda name: (
                f"🔎 {name}" if vector_search_badge_by_display_name.get(name) else name
            ),
        )
        client_id = client_ids_by_display_name[chosen_display_name]
        if vector_search_badge_by_display_name.get(chosen_display_name):
            st.caption("🔎 Este cliente tiene búsqueda semántica de conversaciones habilitada.")
        model_override = st.selectbox(
            "Modelo (Gemini)",
            vi_agent.AVAILABLE_MODELS,
            index=default_model_index,
            key="model_selector",
            on_change=_cancel_active_analysis,
            format_func=lambda m: f"{m} (en producción)" if m == vi_agent.AVAILABLE_MODELS[0] else m,
            help="Sólo para esta sesión de prueba -no cambia el modelo real en producción.",
        )
        if st.button("↻ Nueva conversación", use_container_width=True, on_click=_reset_session_state):
            st.rerun()
        if len(st.session_state.get("messages", [])) > 1:
            st.download_button(
                "⬇ Descargar conversación",
                data=_conversation_as_markdown(chosen_display_name),
                file_name=f"vera_intelligence_{client_id}.md",
                mime="text/markdown",
                use_container_width=True,
            )
        st.divider()
        _render_usage_sidebar()

    try:
        _init_client(client_id, chosen_display_name, model_override)
    except OperationalUnavailable as exc:
        st.error(exc.user_message)
        return

    st.markdown(
        f"""
        <div class="vera-hero">
            <div class="dot"></div>
            <div>
                <div class="title">{chosen_display_name}</div>
                <div class="subtitle">Asesor de negocio en vivo · Vera Intelligence</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if debug:
        st.sidebar.caption("🔧 Trazas internas activas")

    _ensure_greeting(chosen_display_name, debug=debug)

    previous_question: str | None = None
    for message in st.session_state["messages"]:
        avatar = "✨" if message["role"] == "assistant" else "🧑‍💼"
        with st.chat_message(message["role"], avatar=avatar):
            _render_message(
                message["content"],
                message.get("charts", []),
                message.get("tool_calls"),
                debug=debug,
                message_key=message.get("message_id"),
                show_audio=_asks_for_examples(previous_question),
            )
            if message["role"] == "assistant" and "message_id" in message:
                _render_feedback_buttons(message)
        if message["role"] == "user":
            previous_question = message["content"]

    messages = st.session_state["messages"]
    if messages and messages[-1]["role"] == "assistant":
        _render_suggestions()

    question = st.chat_input("Escribí tu pregunta de negocio...")
    if not question and st.session_state.get("pending_question"):
        question = st.session_state.pop("pending_question")
    if question:
        with _ui_run_scope(ui_run_id):
            st.session_state["messages"].append(
                {"role": "user", "content": question, "charts": [], "tool_calls": []}
            )
        with st.chat_message("user", avatar="🧑‍💼"):
            st.markdown(question)

        with st.chat_message("assistant", avatar="✨"):
            tool_calls_log: list[dict] = []
            with st.status("Analizando...", state="running") as status:
                # Streaming del turno final (2026-09-14, pedido explícito del usuario, aceptando
                # que es una mejora de latencia PERCIBIDA, no de costo/latencia real -ver el
                # comentario extenso sobre _send_message_stream_with_retry/_StreamingAnswerBuffer
                # en vi_agent.py para las garantías de seguridad exactas). `streaming_preview` vive
                # DENTRO de st.status -mientras está "running" se ve en vivo; al pasar a
                # "complete"/"error" el status colapsa y el preview deja de mostrarse, justo cuando
                # `_render_message` (más abajo) ya renderiza la respuesta final persistida -no hay
                # duplicado visual, es la misma transición que ya existía (status colapsa al
                # terminar) con contenido en vivo mientras tanto en vez de un placeholder vacío.
                streaming_preview = st.empty()
                streamed_text = {"value": ""}

                def _on_text_delta(delta: str) -> None:
                    streamed_text["value"] += delta
                    streaming_preview.markdown(streamed_text["value"])

                def _on_stream_invalidated() -> None:
                    # Un borrador ya mostrado dejó de ser válido (violación de seguridad
                    # detectada, o falló a mitad de stream y se reintenta) -limpiar lo mostrado,
                    # nunca dejar en pantalla un draft descartado.
                    streamed_text["value"] = ""
                    streaming_preview.empty()

                try:
                    with _analysis_request(question, ui_run_id=ui_run_id) as control:
                        st.button("Cancelar análisis", key="cancel_analysis", on_click=_cancel_active_analysis)
                        raw_answer = vi_agent.run_tool_loop(
                            st.session_state["chat"],
                            question,
                            max_tool_calls=20,
                            debug=False,
                            session_id=st.session_state["session_id"],
                            tool_calls_log=tool_calls_log,
                            on_tool_call=lambda name, _args: status.update(
                                label=_tool_progress_label(name, _args)
                            ),
                            on_text_delta=_on_text_delta,
                            on_stream_invalidated=_on_stream_invalidated,
                            analysis_control=control,
                        )
                    streaming_preview.empty()
                    answer, charts = vi_agent.extract_chart_blocks(raw_answer)
                    answer, suggestions = vi_agent.extract_suggestion_blocks(answer)
                    status.update(label="Listo", state="complete")
                except AnalysisCancelled:
                    st.stop()
                    return
                except OperationalUnavailable as exc:
                    _record_local_failure(question, exc.user_message, ui_run_id=ui_run_id)
                    streaming_preview.empty()
                    answer, charts, suggestions = exc.user_message, [], []
                    status.update(label="Información no disponible", state="error")
                except Exception:  # noqa: BLE001
                    streaming_preview.empty()
                    answer = (
                        "No pude completar el análisis en este momento. "
                        "Intentá nuevamente en unos minutos."
                    )
                    charts = []
                    suggestions = []
                    _record_local_failure(question, answer, ui_run_id=ui_run_id)
                    status.update(label="No se pudo completar", state="error")
            with _ui_run_scope(ui_run_id):
                # Registro de USO por cliente (2026-09-14, ver question_tracking.py) -TODA pregunta
                # real, haya o no voto de feedback después (eso sigue exclusivo de _record_feedback).
                # tool_names sale de tool_calls_log tal como esté -si run_tool_loop tiró una excepción
                # a mitad de camino, refleja lo que sí alcanzó a ejecutarse, no se descarta el evento.
                _QUESTION_RECORDER.record(
                    # client_folder: nombre real de carpeta bajo "2. clientes/" (ej. "agrosuper_bajo"),
                    # NO vi_agent.CLIENT_CONFIG.client_id (la identidad estable, ej. "agrosuper") -son
                    # distintos por el sufijo de grado _alto/_medio/_bajo, y la ruta del archivo tiene
                    # que resolverse por la carpeta real que existe en disco. `client_id` acá (variable
                    # local de main(), ver available_clients_with_display_names) YA es el nombre de
                    # carpeta, no la identidad estable -mismo criterio que `client_folder` en
                    # data_map_auto_update.run_gate().
                    client_folder=client_id,
                    client_id=vi_agent.CLIENT_CONFIG.client_id,
                    client_display_name=vi_agent.CLIENT_CONFIG.display_name,
                    session_id=st.session_state.get("session_id", ""),
                    question=question,
                    answer=answer,
                    tool_names=sorted({call["name"] for call in tool_calls_log}),
                )
                # message_id generado ACÁ, antes de renderizar -no después del append de abajo-
                # para que la key de los botones de audio (_render_audio_players) sea la misma en
                # esta primera renderización y en cualquier re-render posterior del historial
                # (2026-09-14): si se generara un id distinto en cada lugar, un click en "Escuchar"
                # justo después de responder se "perdería" en el próximo rerun de Streamlit.
                message_id = uuid.uuid4().hex
                _render_message(
                    answer, charts, tool_calls_log, debug=debug, message_key=message_id,
                    show_audio=_asks_for_examples(question),
                )
        with _ui_run_scope(ui_run_id):
            st.session_state["messages"].append(
                {
                    "role": "assistant",
                    "content": answer,
                    "charts": charts,
                    "tool_calls": tool_calls_log,
                    "message_id": message_id,
                    "question": question,
                    "feedback": None,
                }
            )
            # Refresca las chips con lo que el modelo sugirió para ESTA respuesta puntual (2026-09-11,
            # ajustado 2026-09-14) -antes sólo se armaban una vez, después del saludo; ahora se
            # reescriben en cada turno para que el usuario pueda seguir tocando burbujas relacionadas
            # con lo que acaba de preguntar. `_contextual_suggestions`, no `_with_fixed_suggestions`
            # -acá NUNCA se fuerza coaching/descubrimiento (eso sólo pasa una vez, en el saludo): con
            # las fijas en cada turno, 1-2 de las 3-4 chips visibles nunca cambiaban, y el usuario lo
            # notó como "no son adaptativas, se repiten siempre las mismas".
            if vi_agent.courtesy_response(question) is None:
                st.session_state["suggestions"] = _contextual_suggestions(suggestions)
        st.rerun()  # refresca el sidebar de consumo con el evento de uso recién escrito


if __name__ == "__main__":
    main()
