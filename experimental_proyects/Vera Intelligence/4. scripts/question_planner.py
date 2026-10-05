"""Planificador previo al loop de herramientas (2026-10-02, reescrito el mismo día).

Motivo (banco de robustez con preguntas vagas + informe de prueba del equipo): el agente falla más por NO
PLANIFICAR que por falta de capacidad -acepta una premisa sin chequearla, reemplaza en silencio una métrica que
no existe por otra parecida, responde una parte de la pregunta, cambia la definición de "cierre" o de "último mes"
entre corridas-. El planificador convierte la pregunta (sobre todo si es vaga) en un PLAN ESTRUCTURADO, en dos etapas:

  Etapa 1 (siempre)          : objetivo/tipo, partes, métricas (existe o no como campo), alcance (entidades, período,
                               desagregación, denominador, formato pedido), comparación/baseline, premisa a verificar,
                               definiciones fijas, advertencias, y -si falta una pieza CRÍTICA- una repregunta.
  Etapa 2 (sólo si hace falta): "redactor de definiciones" ESPECIALIZADO: por cada comportamiento que hay que
                               cuantificar leyendo conversaciones (herramienta extract_insight / JEV), escribe la ficha
                               (pregunta atómica, qué cuenta, qué NO, población) con un prompt dedicado y ejemplos
                               (experimento `3. experimentos/jev_extraccion/`: la definición mueve el resultado).

Qué NO hace: no ejecuta SQL, no decide cifras, no reemplaza al agente. Todo número sigue saliendo de SQL (o de
extract_insight) y verificándose igual.

Anclajes deterministas contra alucinación del planificador: los campos que nombra se validan contra el Data Map real;
las fichas se validan (pregunta, exclusiones no vacías, población con campo real); la repregunta sólo se acepta si
cumple un formato mínimo. Fail-open: cualquier error devuelve plan vacío y el agente corre como antes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field

from google.genai import types

import search_determinism as sd

logger = logging.getLogger(__name__)

PLANNER_ENV_FLAG = "VI_PLANNER"  # "0" lo desactiva (útil para A/B)
CLARIFY_ENV_FLAG = "VI_PLANNER_CLARIFY"  # "0" desactiva sólo la repregunta
MAX_CLARIFICATION_CHARS = 320

# --------------------------------------------------------------------------------------------- etapa 1
_PLANNER_PROMPT = """Sos el planificador previo de un agente de análisis de negocio (retail) que \
responde preguntas de gerencia con SQL sobre un Data Map. NO respondés la pregunta: devolvés un PLAN \
ESTRUCTURADO en JSON para que el agente no cometa errores típicos.

PREGUNTA DEL USUARIO:
{question}

CONTEXTO DE LA CONVERSACIÓN (turnos previos, el más reciente al final; "(primer mensaje)" si no hay):
{context}

CAMPOS REALES DISPONIBLES (fuente.campo: descripción corta). Es la ÚNICA lista de métricas medibles:
{catalog}

Devolvé SÓLO un JSON con exactamente estas secciones:

1. "objetivo": una frase con lo que el usuario quiere decidir o saber.
2. "tipo": "numerica" | "causal" | "exploratoria" | "ranking" | "comparacion" | "coaching" | "otra".
3. "partes": lista de TODAS las cosas que la pregunta pide explícitamente (ej. "cuántos", "qué pasa con la \
venta", "de qué productos hablan", "mes por separado"). Una por ítem, en orden.
4. "metricas": lista de {{"pedida": <métrica con las palabras del usuario>, "campo": <nombre EXACTO de un campo \
de la lista que la mide DIRECTAMENTE, o null>, "nota": <si campo es null: qué campo parecido existe y mide OTRA \
cosa, o "ninguno">, "cuantificable_por_lectura": <true si, sin ser un campo, es un comportamiento sí/no que se \
decide leyendo una conversación (mencionó cuotas sin interés, pidió un genérico); false para montos, precios, \
tickets, NPS y lo que no se lee de una conversación>, "via_patron": <true SÓLO si lo que se mide es la frecuencia de un \
PATRÓN QUE TODAVÍA NO SE CONOCE y lo va a descubrir la búsqueda ("el primero de esos patrones", "el patrón más \
común", "qué tan frecuente es lo que se repite"); en ese caso NO se puede redactar su definición ahora; false si la \
conducta ya está nombrada en la pregunta>}}. Sólo campo si mide lo mismo; "promociones en general" NO \
mide "cuotas sin interés"; montos, tickets, precios y diferencias de precio NO existen salvo que un campo lo \
diga; NPS puntaje no existe si sólo hay indicadores de proceso. Lo CUALITATIVO ("qué dicen", "qué ofertas \
mencionan", "por qué") NO es una métrica faltante: va en "partes" con usar_busqueda=true. Sólo listá métricas \
cuando la pregunta pide cuantificar.
5. "alcance": {{"entidades": [{{"tipo": "tienda"|"vendedor"|"producto"|"otro", "texto": <como lo nombra>}}] \
(nombres propios a buscar; si no aparecen, el agente lo dice y no sustituye), "periodo": <período pedido, \
explícito, o "sin período en la pregunta: usar TODO el histórico disponible y declararlo"; sólo una pregunta de \
tendencia, "ahora", "este mes" o "últimamente" justifica una ventana reciente>, "desagregacion": <por qué \
dimensión abrir el resultado (tienda, vendedor, mes, producto) o null>, "denominador": <sobre qué población se \
calcula el porcentaje, dicho explícito (ej. "conversaciones analizables", "las bajas")>, "formato": <formato \
pedido literal (tabla, ranking top N, mes por separado) o null>}}.
6. "comparacion": null, o {{"baseline": <contra qué se compara (período anterior, promedio de la tienda, otra \
tienda), si el usuario lo pidió o es imprescindible para decir si algo "subió" o "cayó">, "periodos": <los dos \
períodos explícitos>}}.
7. "premisa": null si la pregunta no presupone nada, o {{"afirma": <lo que da por hecho: una caída, un valor>, \
"verificar": <cómo verificarlo con los campos disponibles, o "no verificable">}}.
8. "definiciones": objeto con las definiciones que el agente debe FIJAR y declarar (ej. {{"cierre": "compra \
efectiva = conversaciones con compra / analizables"}}). SÓLO términos de negocio con campo propio (cierre, baja, \
abandono, período). NUNCA definas acá un comportamiento que se cuantifica leyendo conversaciones \
("cuantificable_por_lectura": true): eso lo define otro paso, y los campos de texto libre no se cuentan. \
Vacío si no aplica.
9. "advertencias": lista corta de riesgos concretos para ESTA pregunta (bases chicas, período parcial, \
correlación vs causa, categorías que se pisan, cobertura de análisis). Máximo 5, sólo los que apliquen.
10. "usar_busqueda": true si algo de lo pedido es cualitativo y no está en un campo; false si es numérico.
12. "busqueda": null si usar_busqueda es false; si no, la BÚSQUEDA SEMÁNTICA ya redactada, para que la misma pregunta \
dé siempre la misma búsqueda: {{"query": <la situación observable que se busca, tal como OCURRE en la conversación \
(ej. "el cliente pregunta el precio, el vendedor lo informa y no ofrece una alternativa"), NUNCA una ausencia ni una \
negación ("no ofrece")>, "query_alternativa": <otra redacción de la MISMA intención con otras palabras>, \
"angulos": <lista de 0 a 3 formulaciones EXTRA de ángulos distintos (qué dice el cliente, qué hace el vendedor, otra variante \
concreta de lo mismo), cada una una situación observable como la anterior y sin salirse de la pregunta. SÓLO si la pregunta es \
AMPLIA o exploratoria con varias caras ("qué servicios mencionan", "qué objeciones aparecen", "qué patrones se repiten", "cómo \
abordan X"); lista vacía [] si apunta a UNA conducta puntual ("invitan a la encuesta", "ofrecen cuotas sin interés")>, \
"store_name": \
<sólo si la pregunta nombra una tienda, sino null>, "employee_name": <sólo si nombra un vendedor, sino null>, \
"date_from": <YYYY-MM-DD sólo si el período es explícito, sino null>, "date_to": <ídem>}}.
11. "falta_info": {{"critica": <true SÓLO si falta una pieza sin la cual cualquier respuesta sería un acierto por \
azar>, "pregunta": <UNA repregunta corta al usuario, con 2-4 opciones concretas si se puede>, "motivo": <qué \
falta>}}. Es crítica=true SÓLO en estos tres casos:
  (a) REFERENCIA SIN ANTECEDENTE: la pregunta alude a algo que no está en ella y que sólo el usuario sabe: \
"el asesor", "esa tienda", "el gerente", "esa promoción", "el modelo", "el vendedor nuevo", "el otro", "eso", \
"comparado con antes". Un nombre propio completo ("Parque Delta", "Ricardo Mendoza") NO entra acá: se busca en los \
datos y, si no está, el agente lo dice. Principio: un SINGULAR con artículo definido y sin nombre propio ("el X", \
"la X", "ese X") sobre algo de lo que hay muchos (asesores, tiendas, productos, promociones, vendedores) apunta a UNO \
puntual que sólo el usuario conoce; pedir un panorama usa el plural o "los/las" ("los vendedores", "las tiendas").
  (b) INDICADOR AUSENTE EN UNA COMPARACIÓN O EVOLUCIÓN: pide comparar, ordenar o ver cómo cambió algo ("compará \
enero con febrero", "¿mejoró?", "¿subió o bajó?", "cómo evolucionó") sin decir QUÉ medir ni sobre qué, y el contexto no \
trae un indicador obvio. Si nombra el indicador ("la tasa de cierre", "las ventas perdidas", "las conversaciones") o \
nombra los dos objetos a comparar con nombre propio, NO es crítico.
  (c) INTERPRETACIONES DISTINTAS: hay dos lecturas que dan respuestas MUY diferentes y ninguna es la obvia.
NUNCA es crítico (se responde con un valor por defecto sensato, declarado): un período faltante (todo el histórico, \
declarado), un pedido de panorama general ("¿cómo está el negocio?", "dame un resumen de la semana", "¿cómo vienen los \
vendedores?": se muestra cierre y volumen), un término con definición fija ("el mejor vendedor" = mayor tasa de cierre), \
una métrica inexistente (se dice que no está) ni un tema cualitativo explícito.
  CONTEXTO: si una referencia de (a) o (b) tiene un antecedente CLARO y único en el CONTEXTO DE LA CONVERSACIÓN \
("esa tienda" y el turno anterior habla de una sola tienda), NO es crítica: resolvela y poné el nombre resuelto en \
alcance.entidades para que el agente lo declare. Si el contexto trae varios candidatos posibles o ninguno, sí es crítica. \
Una respuesta del usuario que contesta tu repregunta anterior cuenta como antecedente.
  En "pregunta" escribí UNA repregunta corta que diga qué falta; si hay opciones concretas ofrecé 2 o 3.

No inventes campos. Sé conciso."""

# --------------------------------------------------------------------------------------------- etapa 2
_DEFINER_PROMPT = """Sos el REDACTOR DE DEFINICIONES de un agente de análisis. Tu único trabajo: escribir, para \
cada comportamiento que hay que contar LEYENDO conversaciones reales de atención en tienda (transcripciones con \
ruido de reconocimiento de voz y hablantes mezclados), la ficha que lee un clasificador sí/no por conversación. \
La definición decide el resultado: una definición laxa infla el conteo y una estricta lo achica, así que tiene que \
reflejar EXACTAMENTE lo que pidió el usuario.

PREGUNTA DEL USUARIO:
{question}

COMPORTAMIENTOS A DEFINIR:
{metrics}

CAMPOS CATEGÓRICOS DISPONIBLES para acotar la población (fuente.campo: descripción):
{catalog}

Para cada comportamiento devolvé una ficha con:
- "metrica": la misma «pedida».
- "pregunta": sí/no sobre UNA conversación, atómica (un solo concepto), que diga QUIÉN lo dice o hace (el \
cliente, el vendedor o cualquiera). Nada de "y" que junte dos cosas.
- "criterio_si": definición operativa de qué cuenta, con 2 frases de ejemplo realistas.
- "criterio_no": exclusiones explícitas. OBLIGATORIO y tan importante como el sí: casos frontera ("es cliente de \
esa empresa" no es una oferta de esa empresa), palabras ambiguas ("claro" como afirmación no es la empresa \
Claro), lo que dice el vendedor cuando se pregunta por el cliente, mencionar el tema sin la acción pedida, \
comentarios entre empleados.
- "poblacion": null, o {{"campo": <nombre EXACTO de un campo categórico de la lista>, "valor": <valor de ese \
campo>}} cuando la pregunta es sobre un subconjunto ("de las bajas", "de las ventas perdidas").
- "nivel_de_ambiguedad": "baja" | "media" | "alta": qué tan discutible es la frontera del concepto. Si es "alta", \
el agente le dirá al usuario qué definición se usó.

Ejemplos de buenas fichas:
1) Pedido: "cuántas veces se ofrecen cuotas sin interés".
   pregunta: "¿El vendedor ofrece explícitamente pagar en cuotas sin interés al cliente durante la conversación?"
   criterio_si: "El vendedor menciona la posibilidad de abonar en cuotas sin recargo o sin interés (ej. 'Tenés 3 \
cuotas sin interés con tarjeta', 'Con esta tarjeta tenés cuotas fijas sin interés')."
   criterio_no: "Cuotas con interés o recargo; el cliente pregunta si hay cuotas y el vendedor dice que no; \
promociones bancarias o descuentos directos sin nombrar cuotas sin interés; mención genérica de 'medios de pago'."
   nivel_de_ambiguedad: "baja".
2) Pedido: "cuántos clientes piden un genérico o algo más barato".
   pregunta: "¿El cliente solicita explícitamente una versión genérica, una segunda marca o una opción más \
económica?"
   criterio_si: "El cliente lo pide o lo pregunta (ej. '¿Tenés en genérico?', '¿Hay algo más barato que este?')."
   criterio_no: "Lo ofrece el vendedor por su cuenta; el cliente sólo pregunta el precio; pide sustitución por \
falta de stock sin hablar de costo; comentarios entre empleados."
   nivel_de_ambiguedad: "media".
3) Pedido: "en qué porcentaje de las bajas el cliente menciona una oferta de Claro o Movistar".
   pregunta: "¿El cliente menciona haber recibido o tener una oferta, precio o plan propuesto por Claro o \
Movistar?"
   criterio_si: "El cliente refiere una oferta, precio o plan concreto de Claro o Movistar (ej. 'En Claro me \
ofrecen el doble de gigas por menos plata', 'Movistar me dejó la fibra a mitad de precio')."
   criterio_no: "La palabra 'claro' como afirmación ('claro, entiendo'); decir que es o fue cliente de esas \
empresas sin una oferta o precio; menciones hechas sólo por el asesor; nombrar la empresa sin relación con una \
oferta."
   poblacion: {{"campo": "i03_solicitud_o_tramite_principal", "valor": "cancelacion_retiro"}}.
   nivel_de_ambiguedad: "alta".

Devolvé SÓLO un JSON: {{"fichas": [ ... ]}}. No ampliés ni reduzcas lo que pidió el usuario."""


_PATTERN_DEFINER_PROMPT = """Sos el REDACTOR DE DEFINICIONES de un agente de análisis. Un PATRÓN fue DESCUBIERTO por búsqueda semántica \
en conversaciones reales de atención en tienda (transcripciones con ruido de reconocimiento de voz y hablantes mezclados). \
Ahora hay que medir QUÉ TAN FRECUENTE es ese patrón sobre una muestra aleatoria de TODAS las conversaciones, así que tenés \
que escribir la ficha que lee un clasificador sí/no por conversación. La definición decide el resultado: una laxa infla el \
conteo y una estricta lo achica; tiene que describir EXACTAMENTE el patrón, ni más ni menos.

PATRÓN: {patron}

CASOS QUE LO RESPALDAN (INTERNOS: sirven para entender el patrón; NO los copies, NO los cites, NO pongas frases textuales \
suyas en la ficha):
{citas}

Devolvé una ficha con:
- "pregunta": sí/no sobre UNA conversación, atómica (un solo concepto), que diga QUIÉN lo hace (el cliente, el vendedor o \
cualquiera). Mide la conducta tal como OCURRE, no la ausencia de un criterio.
- "criterio_si": definición operativa de qué cuenta, con 2 ejemplos PARAFRASEADOS y genéricos (nunca frases copiadas de los \
casos).
- "criterio_no": exclusiones explícitas, OBLIGATORIAS y tan importantes como el sí: casos frontera, el parecido que NO es el \
patrón, lo que dice el otro participante, mencionar el tema sin la conducta.
- "nivel_de_ambiguedad": "baja" | "media" | "alta".

Devolvé SÓLO un JSON: {{"ficha": {{"pregunta": "...", "criterio_si": "...", "criterio_no": "...", "nivel_de_ambiguedad": "..."}}}}."""


_PLAN_HEADER = (
    "[PLAN INTERNO DE LA CONSULTA -guía para vos, nunca lo menciones ni lo cites en la respuesta]"
)


@dataclass
class PlanResult:
    text: str = ""
    clarification: str | None = None
    plan: dict = field(default_factory=dict)


def conversation_context(history, *, max_turns: int = 3, max_chars: int = 350) -> str:
    """Resumen de los últimos turnos (pregunta del usuario + arranque de la respuesta) para que el planificador pueda
    decidir si una referencia ('esa tienda', 'el asesor') tiene antecedente. Ignora el plan interno anexado y las
    llamadas a herramientas. `history`: lista de Content de Gemini (o cualquier cosa con .role / .parts[].text)."""
    turns: list[tuple[str, str]] = []
    for content in history or []:
        role = getattr(content, "role", "")
        text = " ".join((getattr(part, "text", None) or "") for part in (getattr(content, "parts", None) or [])).strip()
        if not text or role not in ("user", "model"):
            continue
        text = text.split(_PLAN_HEADER)[0].strip()  # el plan interno no es parte de lo que dijo el usuario
        turns.append(("Usuario" if role == "user" else "Asistente", " ".join(text.split())[:max_chars]))
    return "\n".join(f"{who}: {txt}" for who, txt in turns[-2 * max_turns:])


def planner_enabled() -> bool:
    return os.getenv(PLANNER_ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def clarification_enabled() -> bool:
    return os.getenv(CLARIFY_ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def build_field_catalog(data_map: dict, *, max_desc: int = 110) -> tuple[str, set[str]]:
    """Catálogo compacto `fuente.campo: descripción` y el conjunto de nombres de campo reales."""
    lines: list[str] = []
    names: set[str] = set()
    sources = (data_map or {}).get("sources") or {}
    for source_key, source in sources.items():
        if not isinstance(source, dict):
            continue
        fields = source.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        for field_name, spec in fields.items():
            names.add(str(field_name).lower())
            desc = ""
            if isinstance(spec, dict):
                desc = " ".join(str(spec.get("description") or "").split())[:max_desc]
            lines.append(f"{source_key}.{field_name}: {desc}")
    return "\n".join(lines), names


def ground_plan(plan: dict, known_fields: set[str]) -> dict:
    """Degrada a `campo: null` cualquier campo que el planificador nombre y no exista de verdad."""
    metricas = plan.get("metricas")
    if not isinstance(metricas, list):
        plan["metricas"] = []
        return plan
    for item in metricas:
        if not isinstance(item, dict):
            continue
        campo = item.get("campo")
        if campo is None:
            continue
        bare = str(campo).split(".")[-1].strip().lower()
        if bare not in known_fields:
            item["campo"] = None
            item["nota"] = f"el campo '{campo}' no existe; " + str(item.get("nota") or "")
    return plan


def ground_lecturas(plan: dict, known_fields: set[str]) -> dict:
    """Valida las fichas de lectura: descarta las inválidas y la población con un campo inexistente.

    Una ficha es válida sólo si tiene pregunta, criterio_si y exclusiones (criterio_no) no triviales: sin
    exclusiones explícitas la definición queda laxa y el conteo se infla (experimento jev_extraccion)."""
    fichas = plan.get("lecturas")
    if not isinstance(fichas, list):
        plan["lecturas"] = []
        return plan
    valid = []
    for ficha in fichas:
        if not isinstance(ficha, dict):
            continue
        if (len(str(ficha.get("pregunta") or "").strip()) < 15
                or len(str(ficha.get("criterio_si") or "").strip()) < 15
                or len(str(ficha.get("criterio_no") or "").strip()) < 25):
            continue
        pob = ficha.get("poblacion")
        if isinstance(pob, dict):
            campo = str(pob.get("campo") or "").split(".")[-1].strip().lower()
            if campo not in known_fields or not pob.get("valor"):
                ficha["poblacion"] = None
        else:
            ficha["poblacion"] = None
        valid.append(ficha)
    plan["lecturas"] = valid
    return plan


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_ANGULOS_MAX = 3
# Una formulación que describe una AUSENCIA ("no ofrece", "sin mencionar") busca justo lo que no ocurre: se descarta.
_ABSENCE_RE = re.compile(r"^\W*(?:el\s+|la\s+|un\s+|una\s+)?(?:\w+\s+){0,3}?(?:no|nunca|jamás|jamas|sin|ni)\s", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) > 3}


def _ground_angulos(raw: object, already: list[str]) -> list[str]:
    """Valida los ángulos extra de una búsqueda amplia (2026-10-05): lista de textos de 25 a 300 caracteres, sin repetir (ni
    parecerse en más del 80 % de sus palabras a la consulta, a la alternativa o a otro ángulo), sin ausencias, máximo 3. Un
    valor que no es lista se ignora (la búsqueda sigue sin ángulos): nunca se rompe el plan por esto."""
    if not isinstance(raw, (list, tuple)):
        return []
    kept: list[str] = []
    seen = [_tokens(x) for x in already if x]
    for item in raw:
        text = " ".join(str(item or "").split())
        if not (25 <= len(text) <= 300) or _ABSENCE_RE.match(text):
            continue
        tokens = _tokens(text)
        if not tokens or any(len(tokens & other) / max(len(tokens | other), 1) >= 0.8 for other in seen):
            continue
        seen.append(tokens)
        kept.append(text)
        if len(kept) >= _ANGULOS_MAX:
            break
    return kept


def ground_busqueda(plan: dict) -> dict:
    """Valida la búsqueda fijada: sólo se conserva si usar_busqueda es true y la consulta es usable; los filtros que no
    cumplan su formato se descartan (nunca se pasan al agente valores inventados con formato inválido)."""
    spec = plan.get("busqueda")
    plan["busqueda"] = None
    if not plan.get("usar_busqueda") or not isinstance(spec, dict):
        return plan
    query = " ".join(str(spec.get("query") or "").split())
    if not (15 <= len(query) <= 300):
        return plan
    clean: dict = {"query": query}
    alt = " ".join(str(spec.get("query_alternativa") or "").split())
    if 15 <= len(alt) <= 300 and alt.lower() != query.lower():
        clean["query_alternativa"] = alt
    angulos = _ground_angulos(spec.get("angulos"), [query, alt])
    if angulos:
        clean["queries_extra"] = angulos
    for key in ("store_name", "employee_name"):
        val = " ".join(str(spec.get(key) or "").split())
        if val and val.lower() not in {"null", "none"}:
            clean[key] = val
    for key in ("date_from", "date_to"):
        val = str(spec.get(key) or "").strip()
        if _DATE_RE.match(val):
            clean[key] = val
    plan["busqueda"] = clean
    return plan


def _valid_clarification(plan: dict) -> str | None:
    """La repregunta sólo se acepta si el planificador la marcó crítica Y cumple un formato mínimo."""
    falta = plan.get("falta_info")
    if not isinstance(falta, dict) or falta.get("critica") is not True:
        return None
    pregunta = " ".join(str(falta.get("pregunta") or "").split())
    if not (15 <= len(pregunta) <= MAX_CLARIFICATION_CHARS) or "?" not in pregunta:
        return None
    return pregunta


def _section(title: str, body: list[str]) -> str:
    return f"{title}\n" + "\n".join(f"   - {line}" for line in body) if body else ""


def _desglose_hint(plan: dict) -> str:
    """Traduce alcance.desagregacion a un valor de `desglosar_por` de extract_insight ('' si no aplica)."""
    alcance = plan.get("alcance") if isinstance(plan.get("alcance"), dict) else {}
    text = str(alcance.get("desagregacion") or "").lower()
    if any(w in text for w in ("tienda", "sucursal", "local")):
        return "tienda"
    if "semana" in text:
        return "semana"
    if any(w in text for w in ("mes", "mensual", "evoluci", "período", "periodo", "fecha")):
        return "mes"
    return ""


def render_plan(plan: dict, *, extraction_available: bool = False) -> str:
    """Plan estructurado en secciones numeradas para el agente principal; vacío si no aporta nada."""
    sections: list[str] = []

    obj = []
    if str(plan.get("objetivo") or "").strip():
        obj.append(f"Objetivo: {plan['objetivo']}")
    if str(plan.get("tipo") or "").strip():
        obj.append(f"Tipo de pregunta: {plan['tipo']}")
    sections.append(_section("1. OBJETIVO", obj))

    partes = [str(p) for p in plan.get("partes") or [] if str(p).strip()]
    sections.append(_section(
        "2. PARTES (respondé TODAS, o decí explícitamente por qué una no se puede)",
        [f"({i}) {p}" for i, p in enumerate(partes, 1)]))

    met: list[str] = []
    for m in plan.get("metricas") or []:
        if not isinstance(m, dict) or not m.get("pedida"):
            continue
        if m.get("campo"):
            met.append(f"«{m['pedida']}»: existe, medila con el campo {m['campo']}.")
        elif extraction_available and m.get("via_patron"):
            met.append(
                f"«{m['pedida']}»: es la frecuencia de un PATRÓN que todavía no se conoce. NO hay ficha: primero "
                "search_conversations (usá la búsqueda fijada de la sección 10) y, con el patrón que devuelva, llamá "
                "extract_insight pasando SÓLO `patron` = el texto EXACTO del patrón (copiado de `patrones`) y los MISMOS "
                "filtros y campo_estructurado que usó la búsqueda; NO escribas pregunta ni criterios. El número sale de "
                "esa muestra aleatoria, nunca de la búsqueda.")
        elif extraction_available and m.get("cuantificable_por_lectura"):
            met.append(
                f"«{m['pedida']}»: NO es un campo del Data Map, pero es un comportamiento sí/no de la conversación: "
                "cuantificala con extract_insight (ver ficha en la sección 7) y aclarale al usuario que es una "
                "estimación por lectura de conversaciones, no un campo medido.")
        else:
            met.append(
                f"«{m['pedida']}»: NO existe como dato. Decilo en la primera oración y no la reemplaces en silencio. "
                f"Si existe un campo cercano ({m.get('nota') or 'ninguno'}) que realmente ayude a la decisión, podés "
                "mostrarlo rotulado como OTRA medición; si no aporta, no agregues métrica de relleno. No inventes ni "
                "estimes el número.")
    sections.append(_section("3. MÉTRICAS", met))

    alc: list[str] = []
    alcance = plan.get("alcance") if isinstance(plan.get("alcance"), dict) else {}
    ents = [e for e in alcance.get("entidades") or [] if isinstance(e, dict) and e.get("texto")]
    if ents:
        alc.append("Nombres a buscar en los datos: "
                   + ", ".join(f"{e.get('tipo', 'otro')} «{e['texto']}»" for e in ents)
                   + ". Si alguno no aparece, decilo; no lo sustituyas por otro parecido.")
    if alcance.get("periodo"):
        alc.append(f"Período: {alcance['periodo']}. Declaralo en la respuesta.")
    if alcance.get("desagregacion"):
        alc.append(f"Abrir el resultado por: {alcance['desagregacion']}.")
    if alcance.get("denominador"):
        alc.append(f"Denominador de los porcentajes: {alcance['denominador']}. Decilo en la respuesta.")
    if alcance.get("formato"):
        alc.append(f"Formato pedido (respetalo literalmente): {alcance['formato']}.")
    sections.append(_section("4. ALCANCE", alc))

    cmpb = plan.get("comparacion")
    if isinstance(cmpb, dict) and cmpb.get("baseline"):
        txt = f"Comparar contra: {cmpb['baseline']}"
        if cmpb.get("periodos"):
            txt += f" ({cmpb['periodos']})"
        sections.append(_section("5. COMPARACIÓN / BASELINE", [txt + ". Mostrá ambos números con su base."]))

    premisa = plan.get("premisa")
    if isinstance(premisa, dict) and premisa.get("afirma"):
        sections.append(_section("6. PREMISA A VERIFICAR PRIMERO", [
            f"La pregunta da por hecho: «{premisa['afirma']}». Verificalo ({premisa.get('verificar') or 'no verificable'}) "
            "y decí al principio si se confirma o no; si no es verificable, decilo y no expliques el supuesto cambio "
            "como si fuera cierto."]))

    fichas: list[str] = []
    if extraction_available:
        for ficha in plan.get("lecturas") or []:
            if not isinstance(ficha, dict) or not ficha.get("pregunta"):
                continue
            pob = ficha.get("poblacion") if isinstance(ficha.get("poblacion"), dict) else None
            pob_txt = (f", campo_estructurado=«{str(pob['campo']).split('.')[-1]}», valor_estructurado=«{pob['valor']}»"
                       if pob else "")
            amb = f" [ambigüedad {ficha['nivel_de_ambiguedad']}: decile al usuario qué definición se usó]" \
                if ficha.get("nivel_de_ambiguedad") == "alta" else ""
            desg = _desglose_hint(plan)
            desg_txt = f", desglosar_por=«{desg}»" if desg else ""
            fichas.append(
                "Ficha de lectura para extract_insight (usala TAL CUAL, no la reescribas): "
                f"pregunta=«{ficha['pregunta']}», criterio_si=«{ficha.get('criterio_si', '')}», "
                f"criterio_no=«{ficha.get('criterio_no', '')}»{pob_txt}{desg_txt}.{amb}")
    sections.append(_section("7. FICHAS DE LECTURA", fichas))

    defs = plan.get("definiciones")
    if isinstance(defs, dict) and defs:
        sections.append(_section("8. DEFINICIONES FIJAS (declaralas en la respuesta)",
                                 [f"{k}: {v}" for k, v in defs.items()]))

    warn = [f"{w}" for w in (plan.get("advertencias") or [])[:5] if str(w).strip()]
    if plan.get("usar_busqueda"):
        warn.append("Parte de lo pedido es cualitativa: complementá con la herramienta de búsqueda semántica "
                    "(sin cifras de la búsqueda).")
    sections.append(_section("9. CUIDADOS", warn))

    spec = plan.get("busqueda") if isinstance(plan.get("busqueda"), dict) else None
    if spec and spec.get("query"):
        partes_b = [f"query=«{spec['query']}»"]
        for key in ("query_alternativa", "store_name", "employee_name", "date_from", "date_to"):
            if spec.get(key):
                partes_b.append(f"{key}=«{spec[key]}»")
        if spec.get("queries_extra"):
            partes_b.append("queries_extra=[" + "; ".join(f"«{q}»" for q in spec["queries_extra"]) + "]")
        sections.append(_section("10. BÚSQUEDA SEMÁNTICA FIJADA", [
            "Para la parte cualitativa llamá search_conversations con " + ", ".join(partes_b) + " TAL CUAL (están fijados "
            "para que la misma pregunta dé siempre la misma búsqueda). Si otra regla exige criterio/resultado o "
            "comparar_con_mejores (coaching), mantené esos parámetros y usá igual esta query."]))

    body = [s for s in sections if s]
    # Un plan que sólo trae "objetivo/tipo" no aporta nada que el agente no deduzca solo.
    if len(body) <= 1 and not (plan.get("metricas") or plan.get("alcance") or plan.get("premisa")):
        return ""
    return _PLAN_HEADER + "\n" + "\n".join(body)


def _call_json(gen, model: str, prompt: str, *, thinking, usage_recorder, client_id, session_id, interaction_id,
               call_kind: str) -> dict | None:
    # Caché del plan (2026-10-02, determinismo): el prompt ya contiene la pregunta, el contexto de la conversación y el catálogo
    # de campos, así que su huella identifica la entrada exacta. Misma entrada = mismo plan (y misma búsqueda fijada) aunque el
    # modelo, con temperature=0, redacte distinto cada vez (medido: 3 corridas dieron 3 consultas distintas, coseno 0,90-0,95).
    cache_key = sd.stable_hash(call_kind, model, prompt)
    cached = sd.cache_get("planner", cache_key)
    if isinstance(cached, dict):
        return cached
    config = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0.0, seed=sd.seed_for(prompt, model),
        thinking_config=types.ThinkingConfig(thinking_level=thinking))
    response = gen(model=model, contents=prompt, config=config)
    if usage_recorder is not None:
        try:
            usage_recorder.record_response(
                response, client_id=client_id, model=model, session_id=session_id,
                interaction_id=interaction_id or uuid.uuid4().hex, call_index=0, call_kind=call_kind, attempts=1)
        except Exception:  # noqa: BLE001
            pass
    parsed = json.JSONDecoder().raw_decode((response.text or "").strip())[0]
    if isinstance(parsed, dict):
        sd.cache_put("planner", cache_key, parsed)
        return parsed
    return None


def build_plan(
    question: str,
    *,
    client,
    model: str,
    data_map: dict,
    usage_recorder=None,
    client_id: str = "",
    session_id: str = "",
    interaction_id: str = "",
    generate=None,
    extraction_available: bool = False,
    allow_clarification: bool = True,
    context: str = "",
) -> PlanResult:
    """Plan estructurado (+ repregunta si falta algo crítico). Fail-open: error -> PlanResult vacío."""
    try:
        catalog, known = build_field_catalog(data_map)
        if not catalog:
            return PlanResult()
        gen = generate or client.models.generate_content
        common = dict(usage_recorder=usage_recorder, client_id=client_id, session_id=session_id,
                      interaction_id=interaction_id)
        plan = _call_json(gen, model, _PLANNER_PROMPT.format(question=question.strip(), catalog=catalog,
                                               context=context.strip() or "(primer mensaje)"),
                          thinking=types.ThinkingLevel.LOW, call_kind="planner", **common)
        if plan is None:
            return PlanResult()
        plan = ground_busqueda(ground_plan(plan, known))

        clarification = _valid_clarification(plan) if (allow_clarification and clarification_enabled()) else None
        if clarification:
            return PlanResult(text="", clarification=clarification, plan=plan)

        # Etapa 2: redactor de definiciones, sólo si hay algo que cuantificar leyendo conversaciones.
        by_reading = [m for m in plan.get("metricas") or []
                      if isinstance(m, dict) and not m.get("campo") and m.get("cuantificable_por_lectura")
                      and not m.get("via_patron")]
        plan["lecturas"] = []
        if extraction_available and by_reading:
            try:
                metrics_txt = "\n".join(f"- «{m.get('pedida')}»" for m in by_reading)
                defined = _call_json(
                    gen, model,
                    _DEFINER_PROMPT.format(question=question.strip(), metrics=metrics_txt, catalog=catalog),
                    thinking=types.ThinkingLevel.MEDIUM, call_kind="planner_definer", **common)
                plan["lecturas"] = (defined or {}).get("fichas") or []
            except Exception:  # noqa: BLE001 -sin ficha el agente redacta la suya (comportamiento anterior)
                logger.warning("El redactor de definiciones falló.", exc_info=True)
            ground_lecturas(plan, known)
        return PlanResult(text=render_plan(plan, extraction_available=extraction_available), plan=plan)
    except Exception:  # noqa: BLE001 -fail-open
        logger.warning("El planificador de preguntas falló; el agente sigue sin plan.", exc_info=True)
        return PlanResult()


def design_pattern_card(
    patron: str,
    citas: list[str],
    *,
    client,
    model: str,
    catalog: str = "",
    known: set[str] | None = None,
    usage_recorder=None,
    client_id: str = "",
    generate=None,
) -> dict | None:
    """Ficha de lectura (pregunta, criterio_si, criterio_no) que mide la frecuencia de un PATRÓN descubierto por la
    búsqueda semántica. Mismo validador que las fichas del planificador (exclusiones no triviales). Cacheada por prompt
    (misma entrada = misma ficha). Devuelve None ante cualquier fallo: el agente cae a escribir los criterios él."""
    try:
        gen = generate or client.models.generate_content
        citas_txt = "\n".join(f"- «{c}»" for c in citas if c) or "(sin casos disponibles)"
        parsed = _call_json(
            gen, model, _PATTERN_DEFINER_PROMPT.format(patron=" ".join(str(patron).split()), citas=citas_txt),
            thinking=types.ThinkingLevel.MEDIUM, usage_recorder=usage_recorder, client_id=client_id,
            session_id="", interaction_id="", call_kind="planner_pattern")
        ficha = (parsed or {}).get("ficha")
        if not isinstance(ficha, dict):
            return None
        plan = {"lecturas": [ficha]}
        ground_lecturas(plan, known if known is not None else set())
        return plan["lecturas"][0] if plan["lecturas"] else None
    except Exception:  # noqa: BLE001 -fail-open
        logger.warning("El redactor de fichas de patrón falló.", exc_info=True)
        return None


def plan_question(question: str, **kwargs) -> str:
    """Compatibilidad: sólo el texto del plan ('' si falla, no aporta o hay repregunta)."""
    kwargs.setdefault("allow_clarification", False)
    return build_plan(question, **kwargs).text
