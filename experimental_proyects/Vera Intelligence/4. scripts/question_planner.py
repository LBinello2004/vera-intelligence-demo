"""Planificador previo al loop de herramientas (2026-10-02, reescrito el mismo día).

Motivo (banco de robustez con preguntas vagas + informe de prueba del equipo): el agente falla más por NO
PLANIFICAR que por falta de capacidad -acepta una premisa sin chequearla, reemplaza en silencio una métrica que
no existe por otra parecida, responde una parte de la pregunta, cambia la definición de "cierre" o de "último mes"
entre corridas-. El planificador convierte la pregunta (sobre todo si es vaga) en un PLAN ESTRUCTURADO:

  Etapa 1 (siempre)          : objetivo/tipo, partes, métricas (existe o no como campo), alcance (entidades, período,
                               desagregación, denominador, formato pedido), comparación/baseline, premisa a verificar,
                               definiciones fijas, advertencias, y -si falta una pieza CRÍTICA- una repregunta.
Qué NO hace: no ejecuta SQL, no decide cifras, no reemplaza al agente. Todo número sigue saliendo de SQL y verificándose igual.
(Hasta 2026-10-06 había una etapa 2, un redactor de fichas para medir comportamientos leyendo conversaciones con JEV; se descartó.)

Anclajes deterministas contra alucinación del planificador: los campos que nombra se validan contra el Data Map real;
la repregunta sólo se acepta si
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
cosa, o "ninguno">}}. Sólo campo si mide lo mismo; "promociones en general" NO mide "cuotas sin interés"; montos, \
tickets, precios y diferencias de precio NO existen salvo que un campo lo diga; NPS puntaje no existe si sólo hay \
indicadores de proceso. Lo CUALITATIVO ("qué dicen", "qué ofertas mencionan", "por qué") NO es una métrica faltante: \
va en "partes" con usar_busqueda=true. Sólo listá métricas cuando la pregunta pide cuantificar.
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
abandono, período). Vacío si no aplica.
9. "advertencias": lista corta de riesgos concretos para ESTA pregunta (bases chicas, período parcial, \
correlación vs causa, categorías que se pisan, cobertura de análisis). Máximo 5, sólo los que apliquen.
10. "usar_busqueda": true SÓLO si algo de lo pedido es CUALITATIVO: qué dicen, cómo, por qué, ejemplos, patrones, \
coaching. false si pide únicamente cuánto / qué porcentaje / ranking / comparación (eso sale de un campo por SQL, o no está \
medido). Si pide el número Y lo cualitativo ("cuántas tienen un cierre bajo y qué pasa en ellas"), true.
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
una métrica inexistente (se dice que no está: ni siquiera un concepto amplio como "deuda" o "problemas" justifica repreguntar) \
ni un tema cualitativo explícito.
  CONTEXTO: si una referencia de (a) o (b) tiene un antecedente CLARO y único en el CONTEXTO DE LA CONVERSACIÓN \
("esa tienda" y el turno anterior habla de una sola tienda), NO es crítica: resolvela y poné el nombre resuelto en \
alcance.entidades para que el agente lo declare. Si el contexto trae varios candidatos posibles o ninguno, sí es crítica. \
Una respuesta del usuario que contesta tu repregunta anterior cuenta como antecedente.
  En "pregunta" escribí UNA repregunta corta que diga qué falta; si hay opciones concretas ofrecé 2 o 3.

No inventes campos. Sé conciso."""

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


def render_plan(plan: dict) -> str:
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
        else:
            met.append(
                f"«{m['pedida']}»: NO está medida (ningún campo del Data Map la mide). Decilo en la primera oración y no "
                "la reemplaces en silencio. No des ningún número, porcentaje ni conteo, ni lo estimes con la búsqueda. "
                f"Si existe un campo cercano ({m.get('nota') or 'ninguno'}) que realmente ayude a la decisión, podés "
                "mostrarlo rotulado como OTRA medición (qué mide distinto); si no aporta, no agregues métrica de relleno. "
                "Ofrecé ver ejemplos y patrones cualitativos de las conversaciones (sin frecuencia) y sugerí que el equipo "
                "agregue esa medición al checklist de análisis.")
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

        return PlanResult(text=render_plan(plan), plan=plan)
    except Exception:  # noqa: BLE001 -fail-open
        logger.warning("El planificador de preguntas falló; el agente sigue sin plan.", exc_info=True)
        return PlanResult()


def plan_question(question: str, **kwargs) -> str:
    """Compatibilidad: sólo el texto del plan ('' si falla, no aporta o hay repregunta)."""
    kwargs.setdefault("allow_clarification", False)
    return build_plan(question, **kwargs).text
