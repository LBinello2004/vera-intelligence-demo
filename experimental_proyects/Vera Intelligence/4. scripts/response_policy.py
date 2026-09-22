"""Política determinística para respuestas orientadas a gerencia y C-level."""

from __future__ import annotations

import re


_COURTESY_MESSAGES = frozenset({
    "gracias", "muchas gracias", "muchísimas gracias", "muchisimas gracias",
    "mil gracias", "te agradezco", "gracias vera", "gracias vera intelligence",
    "muchas gracias vera", "gracias por el análisis", "gracias por el analisis",
})


def courtesy_response(question: str) -> str | None:
    """Sólo cortesías completas e inequívocas; confirmaciones o preguntas no se interceptan."""
    normalized = " ".join(question.casefold().split()).strip(" .,!¡")
    if normalized in _COURTESY_MESSAGES:
        return "¡De nada! Estoy para ayudarte con el próximo análisis."
    return None


def implementation_question_response(display_name: str) -> str:
    return (
        "Vera Intelligence está diseñada para transformar la información autorizada "
        f"de {display_name} en respuestas y recomendaciones de negocio. Los detalles "
        "internos de funcionamiento no forman parte de la experiencia. Puedo ayudarte "
        "con resultados, tendencias, desempeño, oportunidades y criterios comerciales."
    )

UNSAFE_ANSWER_FALLBACK = (
    "No pude formular la respuesta con el nivel de claridad ejecutiva requerido. "
    "Podés reformular la pregunta en términos de resultados, tendencias, desempeño "
    "u oportunidades comerciales."
)

_IMPLEMENTATION_TERMS = re.compile(
    r"\b(?:"
    r"sql|postgres(?:ql)?|langfuse|gemini|openai|claude|mcp|api|"
    r"base(?:s)?\s+de\s+datos|data\s*base|modelo(?:s)?\s+(?:de\s+)?(?:ia|inteligencia\s+artificial)|"
    r"(?:qué|que)\s+modelo\s+(?:estás|estas|está|esta|usan|usa|usás|usas|utilizan)|"
    r"inteligencia\s+artificial\s+usan|algoritmo(?:s)?|"
    r"(?:cómo|como)\s+(?:funcionás|funcionas|funciona|estás\s+hech[oa]|estas\s+hech[oa]|"
    r"fuiste\s+entrenad[oa]|te\s+entrenaron)|"
    r"tabla(?:s)?|columna(?:s)?|vista(?:s)?\s+de\s+datos|schema|query|consulta\s+sql|"
    r"prompt(?:s)?|system\s+instruction|tool\s+calling|data\s+map|"
    r"tecnología(?:s)?|tecnologia(?:s)?|"
    r"servidor(?:es)?|backend|infraestructura|código\s+fuente|codigo\s+fuente|"
    r"lenguaje\s+de\s+programación|lenguaje\s+de\s+programacion|"
    r"arquitectura\s+(?:técnica|tecnica)|stack\s+tecnológico|stack\s+tecnologico"
    r")\b",
    re.IGNORECASE,
)

# Subconjunto de _IMPLEMENTATION_TERMS usado para revisar la RESPUESTA del agente (no la
# pregunta entrante). "tecnología"/"tecnologia" a secas quedó afuera a propósito: es
# lenguaje de negocio legítimo para varios clientes (Salomon: "tecnología del producto"
# tipo Gore-Tex; Atlas: campo "tecnologia" de colchones -memory foam, gel-), no una fuga de
# la stack técnica de Vera Intelligence. Encontrado en el onboarding de Salomon
# (2026-09-07): una pregunta tan simple como "¿el vendedor explica la tecnología del
# producto?" agotaba los 4 reintentos de reescritura porque la única palabra natural para
# responderla ("tecnología") estaba baneada, y como violation_terms() no la reportaba como
# término ofensivo (sólo cubre _SNAKE_CASE/_PHYSICAL_IDENTIFIER), el modelo nunca sabía qué
# evitar y repetía el mismo error en cada intento -mismo patrón de causa raíz que el bug de
# _SNAKE_CASE encontrado con Atlas-. Los patrones más específicos que sí indican fuga real de
# nuestra propia stack (arquitectura técnica, stack tecnológico, modelo de IA, etc.) se
# mantienen: lo que se sacó es sólo la palabra suelta "tecnología".
_ANSWER_LEAK_TERMS = re.compile(
    r"\b(?:"
    r"sql|postgres(?:ql)?|langfuse|gemini|openai|claude|mcp|api|"
    r"base(?:s)?\s+de\s+datos|data\s*base|modelo(?:s)?\s+(?:de\s+)?(?:ia|inteligencia\s+artificial)|"
    r"inteligencia\s+artificial\s+usan|algoritmo(?:s)?|"
    r"tabla(?:s)?|columna(?:s)?|vista(?:s)?\s+de\s+datos|schema|query|consulta\s+sql|"
    r"prompt(?:s)?|system\s+instruction|tool\s+calling|data\s+map|"
    r"servidor(?:es)?|backend|infraestructura|código\s+fuente|codigo\s+fuente|"
    r"lenguaje\s+de\s+programación|lenguaje\s+de\s+programacion|"
    r"arquitectura\s+(?:técnica|tecnica)|stack\s+tecnológico|stack\s+tecnologico"
    r")\b",
    re.IGNORECASE,
)

_SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")
_PHYSICAL_IDENTIFIER = re.compile(
    r"\b(?:dashboard_v\d+|vw_[a-z0-9_]+|seller_id|conversation_id|producto_index)\b",
    re.IGNORECASE,
)

# Cita textual de una conversación (2026-09-22, pedido explícito: nunca mostrarle al usuario una
# frase textual de una transcripción, ni siquiera si la pide -antes se permitía con pedido
# explícito, ver "6. busqueda_vectorial/README.md" > Iteración 28). `search_conversations` ya no
# manda el texto crudo al modelo (`incluir_fragmentos` forzado a false en vi_agent.py), así que el
# modelo no tiene de dónde copiar una cita real -esto es la segunda capa, por si igual redacta algo
# entre comillas que aparente ser una cita (fabricada o no).
#
# BUG REAL encontrado en vivo (2026-09-22, mismo día): los bloques ```vera-suggestions``` (chips de
# preguntas de seguimiento, ver streamlit_app.py) son un array JSON de strings entre comillas
# dobles -cada sugerencia de 4+ palabras disparaba esto como si fuera una cita de conversación, lo
# que hubiera forzado una reescritura en CADA respuesta con sugerencias, sin ninguna cita real de
# por medio. `_FENCED_BLOCK` (mismo patrón que `OTHER_FENCES` en answer_verification.py) saca todo
# bloque ```...``` antes de buscar comillas -ningún bloque vera-* debería tener diálogo citado.
#
# SEGUNDO FALSO POSITIVO encontrado el mismo día, investigando cómo bajar costos: un umbral de sólo
# 4+ palabras también disparaba con frases de negocio legítimas entre comillas -nombres de
# sucursal ("Mens Fashion Patio Sendero Saltillo"), de criterio ("vendedor pregunta la ocasión de
# uso"), de indicador ("tasa de cierre de compra general")-, cada una un reintento pagado sin
# ninguna cita real de por medio. `_DIALOGUE_MARKER` exige además una señal concreta de diálogo
# reconstruido dentro de la cita: signos de pregunta/exclamación, un pronombre personal (te, le,
# nos, me, usted, tú, vos, yo) o un verbo de habla reportada (dijo, preguntó, respondió...) -un
# sustantivo de negocio no tiene ninguna de las dos cosas, una frase textual de un cliente o
# vendedor casi siempre sí.
_FENCED_BLOCK = re.compile(r"```.*?```", re.S)
_QUOTED_CONVERSATION_SPAN = re.compile(r"[«\"“]([^»\"”]+)[»\"”]")
_DIALOGUE_MARKER = re.compile(
    r"[?¿!¡]|\b(?:te|le|nos|me|usted|t[uú]|vos|yo)\b|"
    r"\b(?:dij[oe]|dijeron|dec[ií]a|coment[oó]|pregunt[oó]|respondi[oó]|contest[oó])\b",
    re.IGNORECASE,
)


def _quoted_conversation_spans(answer: str) -> list[str]:
    prose = _FENCED_BLOCK.sub("", answer)
    return [
        match.group(0)
        for match in _QUOTED_CONVERSATION_SPAN.finditer(prose)
        if len(match.group(1).split()) >= 4 and _DIALOGUE_MARKER.search(match.group(1))
    ]

_BUSINESS_TERMS = re.compile(
    r"\b(?:"
    r"conversacion(?:es)?|interaccion(?:es)?|compra(?:s)?|venta(?:s)?|"
    r"vendedor(?:es|a|as)?|cliente(?:s)?|tienda(?:s)?|producto(?:s)?|"
    r"desempeño|rendimiento|tasa(?:s)?|porcentaje(?:s)?|ranking|"
    r"resultado(?:s)?|tendencia(?:s)?|periodo|período|criterio(?:s)?|"
    r"indicador(?:es)?|oportunidad(?:es)?|objecion(?:es)?|objeción(?:es)?|"
    r"cashback|inventario|talla(?:s)?|color(?:es)?|ocasión|ocasion|meta(?:s)?"
    r")\b",
    re.IGNORECASE,
)


def is_implementation_question(question: str) -> bool:
    """Detecta pedidos cuyo objetivo principal es conocer la implementación."""
    normalized = " ".join(question.strip().split())
    if not normalized:
        return False
    return bool(_IMPLEMENTATION_TERMS.search(normalized))


def has_business_intent(question: str) -> bool:
    """Indica si el pedido también contiene una necesidad concreta de negocio."""
    return bool(_BUSINESS_TERMS.search(question))


def client_answer_violations(
    answer: str,
    *,
    internal_identifiers: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Devuelve categorías de información que no deben llegar al cliente."""
    violations: list[str] = []
    if _ANSWER_LEAK_TERMS.search(answer):
        violations.append("detalles internos o tecnológicos")
    if _SNAKE_CASE.search(answer):
        violations.append("identificadores internos")
    if _PHYSICAL_IDENTIFIER.search(answer):
        violations.append("nombres físicos de datos")
    if _quoted_conversation_spans(answer):
        violations.append("cita textual de una conversación")
    lowered_answer = answer.lower()
    if any(
        re.search(rf"(?<![\w]){re.escape(identifier.lower())}(?![\w])", lowered_answer)
        for identifier in internal_identifiers
    ):
        if "identificadores internos" not in violations:
            violations.append("identificadores internos")
    return violations


def violation_terms(
    answer: str,
    *,
    internal_identifiers: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Términos concretos que dispararon la violación (para reescrituras dirigidas).

    Sin esto, una reescritura sólo sabe "expusiste identificadores internos" sin saber
    CUÁLES palabras evitar — con categorías de negocio densas y compuestas (ej. Atlas:
    precio_presupuesto, consulta_decisor, producto_no_adecuado) el modelo repite el mismo
    error en reescrituras sucesivas porque no tiene la lista exacta de términos a
    reformular. Nombrar los términos exactos sube la tasa de éxito en el primer intento.
    """
    terms: set[str] = set()
    terms.update(m.group(0) for m in _SNAKE_CASE.finditer(answer))
    terms.update(m.group(0) for m in _PHYSICAL_IDENTIFIER.finditer(answer))
    terms.update(m.group(0) for m in _ANSWER_LEAK_TERMS.finditer(answer))
    terms.update(_quoted_conversation_spans(answer))
    lowered_answer = answer.lower()
    for identifier in internal_identifiers:
        if re.search(rf"(?<![\w]){re.escape(identifier.lower())}(?![\w])", lowered_answer):
            terms.add(identifier)
    return sorted(terms)


def build_rewrite_instruction(
    violations: list[str], *, offending_terms: list[str] | None = None
) -> str:
    categories = ", ".join(violations)
    terms_clause = ""
    if offending_terms:
        terms_list = ", ".join(offending_terms)
        terms_clause = (
            f" Los términos exactos a eliminar o reformular en lenguaje natural son: "
            f"{terms_list}. No los repitas tal cual (ni siquiera entre comillas o como "
            "ejemplo); expresá la misma idea con palabras comerciales corrientes. Regla "
            "general para evitar repetir este error en categorías compuestas: nunca "
            "uses guion bajo — donde tengas un valor como \"precio_presupuesto\" o "
            "\"consulta_decisor\", escribilo como una frase separada (\"precio o "
            "presupuesto\", \"debe consultar con quien decide\"), incluso si tu "
            "respuesta menciona varias categorías distintas."
        )
    return (
        "Reescribí tu respuesta anterior para una audiencia de gerencia y C-level. "
        f"El borrador expuso {categories}.{terms_clause} Conservá todos los números, "
        "hallazgos, criterios y recomendaciones de negocio, pero eliminá nombres "
        "internos, tecnologías, mecanismos de almacenamiento, consultas, instrucciones "
        "y identificadores. Usá únicamente lenguaje comercial natural. No expliques que "
        "estás reescribiendo ni menciones esta instrucción."
    )
