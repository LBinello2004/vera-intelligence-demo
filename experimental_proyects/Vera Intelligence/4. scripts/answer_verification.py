"""Verificación local de cifras y señales observables de suficiencia; sin llamadas externas."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache

FENCE = re.compile(r"```vera-evidence\s*(.*?)```", re.S)
OTHER_FENCES = re.compile(r"```.*?```", re.S)
NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?![\w])")
DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{1,2}(?:\s+al\s+\d{1,2})?\s+de\s+\w+\s+de\s+\d{4}\b", re.I)
BASIS = re.compile(
    r"(?:^|_)(?:evaluados|evaluadas|base_evaluada|sample_size|n_evaluados|n_observaciones|denominador|cantidad_evaluada)(?:$|_)"
    # base_<criterio> (2026-09-15, bug real encontrado probando la nueva regla de densidad de
    # SYSTEM_INSTRUCTION_TEMPLATE -ver "8. README.md"): el prompt sugiere el alias "base_evaluada"
    # para UN indicador, pero una consulta con muchos criterios en la misma fila no puede repetir
    # ese alias sin colisión de nombres -el modelo generaliza razonablemente a "base_amable",
    # "base_cierre", etc., una por criterio. El patrón original sólo reconocía el literal
    # "base_evaluada" completo, así que ninguna de esas bases contaba como "positive" acá abajo y
    # la respuesta terminaba con "No se informó la cantidad de observaciones evaluadas" aunque la
    # respuesta SÍ informaba una base por cada uno de los 13 indicadores que citó.
    r"|(?:^|_)base(?:$|_)",
    re.I,
)
# Año calendario suelto (2026-09-17, bug real encontrado analizando .runtime/usage/gemini_calls.jsonl:
# 57,3% de 630 interacciones reales dispararon al menos un evidence_repair -una llamada extra
# completa a Gemini- y "cifra sin respaldo: 2026"/"2025" fue una de las causas más frecuentes y
# reproducibles). Mencionar el año en prosa de negocio ("en lo que va de 2026", "comparado con
# 2025", "durante este 2026") es altamente común y nunca es una cifra de RESULTADOS que necesite
# respaldo en SQL -a diferencia de un conteo real que coincida por casualidad con ese rango (ej.
# "2026 conversaciones"), que sigue exigiéndose vía el chequeo de sustantivo de negocio en `after`
# más abajo. Sólo aplica con un prefijo de referencia temporal explícito (nunca un año suelto sin
# contexto, para no perder un conteo real de 4 dígitos por coincidencia).
YEAR_CONTEXT_BEFORE = re.compile(
    r"(?:\b(?:en|durante|del|de|desde|hacia|para|hasta|año|años)|compar(?:ado|ada|ando)\s+con)\s*$",
    re.I,
)
METRIC = re.compile(r"tasa|porcentaje|promedio|puntuaci[oó]n|score|cumplimiento|evaluad|denominador", re.I)
# "definitiv[oa]" se sacó de acá (2026-09-22, encontrado investigando un reintento real de
# evidence_repair en vivo contra mens_fashion_alto/Ubaldo Ramos, costo extra US$0,057 sobre esa
# interacción): en retail "cierre definitivo"/"decisión definitiva" es vocabulario normal del
# proceso de venta, no una afirmación de certeza estadística -mismo patrón de falso positivo que
# "tecnología" en response_policy.py (ver ese comentario). Verificado con casos realistas antes de
# sacarlo: "Falta avanzar hacia una decisión definitiva del cliente" y "lograr un cierre definitivo
# cuando el cliente ya validó la prenda" -ambas frases de coaching legítimas, sin ninguna cifra ni
# certeza estadística de por medio- disparaban el reintento incluso con el chequeo de negación ya
# aplicado. El resto de la lista (sin duda, estadísticamente significativo, muestra
# representativa/suficiente, garantiza, demuestra concluyentemente) es menos ambigua y se mantiene.
CONFIDENT = re.compile(r"sin duda|estadísticamente significativ|estadisticamente significativ|muestra representativa|muestra suficiente|garantiza|demuestra concluyentemente", re.I)
FALLBACK = "No pude verificar las cifras con suficiente respaldo. No voy a presentar una conclusión numérica; podés pedir el detalle del indicador para revisarlo."


def number(value, *, prose=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("valor no numérico")
    percent = str(value).strip().endswith('%')
    raw = str(value).strip().rstrip('%').strip()
    if prose:
        if ',' in raw and '.' in raw:
            raw = raw.replace('.', '').replace(',', '.') if raw.rfind(',') > raw.rfind('.') else raw.replace(',', '')
        elif ',' in raw or '.' in raw:
            separator = ',' if ',' in raw else '.'
            groups = raw.split(separator)
            if len(groups) > 2 or (not percent and len(groups[-1]) == 3 and groups[0].lstrip('+-') != '0'):
                raw = ''.join(groups)
            else:
                raw = raw.replace(',', '.')
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("valor no numérico") from exc
    if not result.is_finite():
        raise ValueError("valor no finito")
    return result


def evidence_id(payload):
    return 'sql_' + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()[:16]


def rows_of(payload):
    rows = payload.get('rows', [])
    columns = payload.get('columns', [])
    if not isinstance(rows, list):
        return []
    return [row if isinstance(row, dict) else dict(zip(columns, row)) for row in rows if isinstance(row, (dict, list))]


def add_result(store, result):
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return None
    if not isinstance(result, dict) or 'rows' not in result:
        return None
    key = evidence_id(result)
    store[key] = result
    return key


def history_results(chat):
    store = {}
    get = getattr(chat, 'get_history', None)
    if not callable(get):
        return store
    for content in get(curated=True):
        for part in getattr(content, 'parts', None) or []:
            response = getattr(part, 'function_response', None)
            if response is not None and response.name == 'run_readonly_sql':
                payload = response.response or {}
                add_result(store, payload.get('result'))
    return store


def history_rulebooks(chat):
    get = getattr(chat, 'get_history', None)
    if not callable(get):
        return []
    texts = []
    for content in get(curated=True):
        for part in getattr(content, 'parts', None) or []:
            response = getattr(part, 'function_response', None)
            if response is not None and response.name == 'get_business_rules':
                result = (response.response or {}).get('result')
                if isinstance(result, str):
                    texts.append(result)
    return texts


def resolve(ref, store):
    if not isinstance(ref, dict):
        raise ValueError("referencia inválida")
    row = ref.get('row')
    if type(row) is not int or row < 0:
        raise ValueError("fila inválida")
    try:
        return number(rows_of(store[ref['id']])[row][ref['column']])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("referencia inexistente") from exc


def calculate(claim, store):
    refs = claim.get('sources', [])
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        raise ValueError("insumos inválidos")
    values = [resolve(ref, store) for ref in refs]
    op = claim.get('operation', 'identity')
    if op == 'identity' and len(values) == 1:
        return values[0]
    if op == 'sum':
        return sum(values)
    if op == 'mean':
        return sum(values) / len(values)
    if len(values) == 2:
        a, b = values
        if op == 'difference':
            return a - b
        if op in ('ratio', 'percentage', 'relative_change'):
            if b == 0:
                raise ValueError("denominador cero")
            if op == 'percentage' and not 0 <= a <= b:
                raise ValueError("proporción fuera de rango")
            return a / b if op == 'ratio' else 100 * ((a-b)/b if op == 'relative_change' else a/b)
    raise ValueError("operación inválida")


@lru_cache(maxsize=512)
def comparison_value(raw):
    parsed = number(raw, prose=True)
    # Conteos enteros exactos; redondeo según precisión escrita, sin tolerancia relativa.
    tolerance = Decimal('0') if parsed == parsed.to_integral() and '%' not in raw else Decimal(5).scaleb(parsed.as_tuple().exponent - 1)
    return parsed, tolerance


def matches(raw, value):
    parsed, tolerance = comparison_value(raw)
    return abs(parsed-value) <= tolerance


def metric_tokens(text):
    text = DATES.sub(' ', OTHER_FENCES.sub(' ', text))
    text = re.sub(r'escala (?:de|del) \d+(?:[.,]\d+)? (?:a|al) \d+(?:[.,]\d+)?', ' ', text, flags=re.I)
    for m in NUMBER.finditer(text):
        before, after = text[max(0, m.start()-35):m.start()], text[m.end():m.end()+35]
        # Marcador de lista ordenada, también con formato markdown (2026-09-21, encontrado en vivo:
        # "### 1. Jorge", "**1. Jorge**", "* **1.** Jorge" o "- 1. Jorge" se leían como una cifra
        # "1" sin respaldo y agotaban los reintentos -la respuesta de coaching de 3 vendedores
        # terminó en el mensaje genérico de "no pude verificar"). El prefijo admite espacios y
        # marcas de encabezado/cita/viñeta/negrita, y el sufijo un cierre de negrita.
        if re.search(r"(?:^|\n)[\s#>*_\-]*$", before) and re.match(r"[.)]\*{0,2}\s", after):
            continue
        if re.search(r"(?:top|paso|tienda|sucursal|farma|últimos|ultimos)\s*$", before, re.I) or re.match(r"\s*(?:días|dias|semanas|meses|años|años|preguntas|criterios|puntos de mejora|minutos|minuto|horas|hora)\b", after, re.I):
            # minutos/minuto/horas/hora (2026-09-15, bug real: la duración de una dinámica de
            # capacitación sugerida -ej. "taller de 15 minutos"- no es una cifra de RESULTADOS que
            # necesite respaldo en SQL, es un detalle de plan de acción como "3 días" o "2 meses"
            # (ya excluidos arriba) -pero le faltaba la unidad de tiempo más corta. Sin esto, una
            # recomendación de coaching con un ejemplo real personalizado entraba en un ciclo de
            # evidence_repair por un falso positivo, y la reescritura de corrección solía perder el
            # ejemplo personalizado en el camino (ver "8. README.md" > personalización).
            continue
        # Enteros chicos (1-10) usados como cuantificador de prosa ("2 momentos", "1 cosa", "3
        # acciones") no son cifras de resultados -medido 2026-09-21: eran la causa de la mayoría de
        # los evidence_repair recientes (cada uno, una llamada completa al modelo principal). Se
        # siguen exigiendo si cuentan una entidad de negocio (conversaciones, ventas, clientes...).
        if (
            re.fullmatch(r"(?:10|[1-9])", m.group())
            and not re.match(r"\s*%", after)
            and not re.match(
                r"\s+(?:conversaciones?|ventas?|compras?|registros?|evaluaciones?|observaciones?|clientes?|tiendas?|productos?|unidades?|puntos?|casos?|vendedores?|veces|de cada)\b",
                after, re.I,
            )
        ):
            continue
        if re.match(r"\s*de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b", after, re.I):
            continue
        if (
            re.fullmatch(r"(?:19|20)\d{2}", m.group())
            and YEAR_CONTEXT_BEFORE.search(before)
            and not re.match(
                r"\s*(?:conversaciones|ventas|compras|registros|evaluaciones|observaciones|clientes|tiendas|productos|unidades|puntos|casos)\b",
                after, re.I,
            )
        ):
            continue
        end = m.end()
        suffix = re.match(r"\s*%", text[end:])
        yield m.group() + ('%' if suffix else '')


@dataclass
class Verification:
    answer: str
    errors: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def verify_answer(answer, store, *, current_ids=None, rulebook_texts=()):
    blocks = list(FENCE.finditer(answer))
    cleaned = FENCE.sub('', answer).strip()
    verdict = Verification(cleaned)
    claims = []
    if len(blocks) > 1:
        verdict.errors.append('más de un bloque de evidencia')
    elif blocks:
        try:
            claims = json.loads(blocks[0].group(1))
            if not isinstance(claims, list) or len(claims) > 100:
                raise ValueError()
        except (ValueError, TypeError):
            verdict.errors.append('evidencia inválida')
            claims = []
    if '```vera-evidence' in cleaned:
        verdict.errors.append('evidencia incompleta')
        verdict.answer = cleaned.split('```vera-evidence')[0].strip()
    if current_ids is not None and not current_ids and rulebook_texts:
        prose = OTHER_FENCES.sub('', cleaned)
        normative = re.search(r"criterio|regla|considera|evalúa|evalua|escala", prose, re.I)
        observed = re.search(r"registraron|se analizaron|fue|fueron|observad|alcanz[oó]|hubo", prose, re.I)
        tokens = list(metric_tokens(cleaned))
        if normative and not observed and all(any(raw in list(metric_tokens(text)) for text in rulebook_texts) for raw in tokens):
            return verdict  # Una guía consultada no usa las cifras de un análisis anterior.
    if not store:
        prose = OTHER_FENCES.sub('', cleaned)
        observed = re.search(r"registraron|se analizaron|fue|fueron|observad|alcanz[oó]|hubo", prose, re.I)
        rule_context = re.search(r"criterio|regla|considera|evalúa|evalua|escala", prose, re.I) and not observed
        quantified = re.findall(r"[-+]?\d[\d.,]*\s*%|\d[\d.,]*\s+(?:conversaciones|ventas|compras|registros|evaluaciones|observaciones|clientes|tiendas|productos)\b", prose, re.I)
        for raw in quantified:
            token = next(iter(metric_tokens(raw)), None)
            supported_rule = rule_context and token is not None and any(token in list(metric_tokens(text)) for text in rulebook_texts)
            if not supported_rule:
                verdict.errors.append('cifra de resultados sin evidencia disponible')
        return verdict
    active_ids = set(store) if current_ids is None else (set(current_ids) or set(list(store)[-1:]))
    active_store = {key: store[key] for key in active_ids if key in store}
    if current_ids is not None and not current_ids and not claims and not list(metric_tokens(cleaned)) and "```vera-chart" not in cleaned:
        return verdict
    direct = []
    percentages = []
    for payload in active_store.values():
        for row in rows_of(payload):
            for column, value in row.items():
                if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                    try:
                        direct.append(number(value))
                        if re.search(r"tasa|porcentaje|percent|pct|rate", column, re.I):
                            percentages.append(number(value))
                    except ValueError: pass
    verified = []
    bases = []
    non_metric_texts = set()
    for claim in claims:
        try:
            if not isinstance(claim, dict) or not isinstance(claim.get('text'), str):
                raise ValueError('declaración inválida')
            if claim.get('operation') == 'non_metric':
                # 2026-09-16, ver "8. README.md" > MEJORES PRÁCTICAS INTERNAS/personalización:
                # escape de auto-declaración para un número que el texto contiene pero que NO es
                # una cifra de resultados derivada de los datos (ej. un número dentro de una cita
                # textual de search_conversations, un conteo de pasos de un plan de acción) -antes
                # de esto, cada caso nuevo de este tipo necesitaba un parche puntual en
                # metric_tokens() (ver sus comentarios de minutos/horas y categoría repetida), un
                # patrón que no escala. Sin 'sources': no hay nada que calcular ni verificar contra
                # SQL, sólo se registra el texto exacto para que el chequeo de abajo no lo exija.
                non_metric_texts.add(claim['text'])
                continue
            expected = calculate(claim, store)
            if not matches(claim['text'], expected):
                raise ValueError('cálculo incorrecto')
            verified.append((claim['text'], expected))
            for ref in [*claim.get('sources', []), *([claim['base']] if 'base' in claim else [])]:
                active_store[ref['id']] = store[ref['id']]
            if claim.get('operation') == 'percentage' and BASIS.search(claim['sources'][1]['column']):
                bases.append(resolve(claim['sources'][1], store))
            if 'base' in claim:
                bases.append(resolve(claim['base'], store))
        except (ValueError, TypeError, ArithmeticError) as exc:
            verdict.errors.append(str(exc))
    for raw in metric_tokens(cleaned):
        if raw in non_metric_texts:
            continue
        candidates = percentages if "%" in raw else direct
        # CORREGIDO (2026-09-17, mismo hallazgo de evidence_repair -ver "8. README.md"): antes
        # exigía `raw == text` (coincidencia EXACTA de string) contra el texto declarado en
        # vera-evidence, no sólo que el VALOR coincida con tolerancia (como ya se exige para
        # `candidates`, celdas crudas de SQL). Un modelo real puede escribir el mismo número
        # verificado con formato levemente distinto en la prosa que en su propia declaración (ej.
        # prosa "20,0%" contra declaración "20%", o "5.490" contra "5490") sin que eso implique
        # ninguna cifra sin respaldo real -el valor SÍ está verificado, sólo cambió el formato de
        # texto. Reproducido en vivo: "El cumplimiento fue de 20,0%." con una declaración válida de
        # "20%" disparaba "cifra sin respaldo: 20,0%" aunque el 20% ya estaba computado y verificado
        # contra las celdas reales. Se saca la igualdad de string y se deja sólo `matches()` -mismo
        # criterio de tolerancia que ya aplica al pool de `candidates`, no una relajación nueva de
        # seguridad: un valor en `verified` ya pasó por `calculate()` contra celdas reales, así que
        # respaldar con él a cualquier mención de ese mismo valor (con tolerancia) es el mismo
        # modelo de "pool de valores verificados" que ya rige para `direct`/`percentages`.
        if not any(matches(raw, value) for value in candidates) and not any(matches(raw, value) for _, value in verified):
            verdict.errors.append('cifra sin respaldo: ' + raw)
    for match in re.finditer(r"```vera-chart\s*(.*?)```", cleaned, re.S):
        try:
            chart = json.loads(match.group(1))
            values = [v for key in ('values', 'x_values', 'sizes') for v in chart.get(key, [])]
            values += [v for series in chart.get('series', []) for v in series.get('values', [])]
            for v in values:
                actual = number(v)
                # CORREGIDO (2026-09-17, bug real -ver YEAR_CONTEXT_BEFORE arriba para el hallazgo
                # que motivó esta misma investigación): comparaba con `==` exacto de Decimal en vez
                # de con la MISMA tolerancia de redondeo que ya usa `matches()` para citas en texto
                # (ver comparison_value). Un gráfico con un valor mostrado redondeado (ej. 68.9,
                # mientras el cálculo verificado en vera-evidence guarda 68.94...) fallaba este
                # chequeo aunque el número mostrado fuera una redondeo válido del mismo dato -
                # "valor del gráfico sin respaldo" fue la causa individual más frecuente de
                # evidence_repair en producción (48 de 331 reintentos reales post-non_metric,
                # ver .runtime/usage/gemini_calls.jsonl), cada uno una llamada extra pagada a
                # Gemini. Igual criterio que comparison_value: un valor entero exige coincidencia
                # exacta (conteos no admiten redondeo), uno con decimales admite media unidad del
                # último dígito mostrado.
                tolerance = (
                    Decimal('0') if actual == actual.to_integral()
                    else Decimal(5).scaleb(actual.as_tuple().exponent - 1)
                )
                if not any(abs(actual - x) <= tolerance for x in direct) and not any(
                    abs(actual - x) <= tolerance for _, x in verified
                ):
                    verdict.errors.append('valor del gráfico sin respaldo')
        except (ValueError, TypeError, AttributeError):
            verdict.errors.append('gráfico no verificable')
    cited = list(metric_tokens(cleaned))
    for payload in active_store.values():
        rows = rows_of(payload)
        if payload.get('truncated') is True:
            verdict.limitations.append('Parte del detalle está incompleta; las conclusiones que dependan de ese detalle son provisionales.')
        if not rows:
            verdict.limitations.append('Una parte del análisis no encontró registros; la ausencia de registros no demuestra incumplimiento.')
        for row in rows:
            for column, value in row.items():
                if value is None and METRIC.search(column):
                    verdict.limitations.append('Hay indicadores sin evaluación disponible; no deben interpretarse como un resultado de cero.')
                if BASIS.search(column):
                    rate_columns = [(key, cell) for key, cell in row.items() if re.search(r"tasa|porcentaje|percent|pct|rate|promedio|score|puntuaci[oó]n", key, re.I)]
                    relevant = not rate_columns
                    for key, cell in rate_columns:
                        try:
                            relevant = relevant or cell is None or any(matches(raw, number(cell)) for raw in cited)
                        except ValueError:
                            pass
                    if relevant:
                        try: bases.append(number(value))
                        except ValueError: pass
    if any(base < 0 or base != base.to_integral() for base in bases):
        verdict.errors.append('base evaluada inválida')
    if any(base <= 0 for base in bases):
        verdict.limitations.append('Hay indicadores sin observaciones evaluadas; no permiten concluir sobre el desempeño.')
    if any(base == 1 for base in bases):
        verdict.limitations.append('Al menos un indicador se basa en una sola observación; no permite generalizar al equipo.')
    prose = OTHER_FENCES.sub('', cleaned)
    if '%' in prose or re.search(r"puntuaci[oó]n|promedio|cumplimiento", prose, re.I):
        positive = sorted(set(base for base in bases if base > 0))
        # Una base que la prosa declara ("115 conversaciones evaluadas") y que coincide con una celda
        # real de SQL también la informa, aunque la columna no se llame base_* (2026-09-21, visto en
        # vivo: coaching con las bases dichas en el texto igual terminaba con "No se informó la
        # cantidad de observaciones evaluadas").
        if not positive:
            for m in re.finditer(r"(\d[\d.,]*)\s+(?:conversaciones|observaciones|ventas|registros|evaluaciones)\b", prose, re.I):
                try:
                    if any(matches(m.group(1), value) and value > 0 for value in direct):
                        positive.append(number(m.group(1)))
                except (ValueError, ArithmeticError):
                    pass
        if not positive:
            # Sin reintento (2026-09-21): si la fila de la tasa citada trae en otra columna una
            # cantidad entera positiva con nombre de conteo (evaluad*, total, cantidad, n_*,
            # conversaciones...), esa es la base -se informa acá, en código, en vez de gastar una
            # llamada completa al modelo principal para que la agregue.
            # CORREGIDO (2026-09-23, bug real reportado por Lucas con captura de pantalla -"Base
            # evaluada de los indicadores citados: 186 conversaciones. Base evaluada de los
            # indicadores citados: 123 conversaciones...." repetido 8 veces): este bucle recorre
            # TODAS las filas de TODOS los payloads, y antes agregaba una línea de limitations POR
            # FILA calificada -con una consulta de 8 criterios (una fila por criterio, patrón UNION
            # ALL muy común en este proyecto), cada fila con su propio N generaba su propia línea. El
            # dedup final (`list(dict.fromkeys(...))`) no las colapsa porque el N es distinto en cada
            # una -8 oraciones casi idénticas, ilegible. La corrección de prompt de la Iteración 50
            # (formatear la base en línea) reduce cuándo se llega a este fallback, pero no lo
            # elimina: si el modelo no declaró la base en su bloque de evidencia, este código sigue
            # siendo el que la aporta, y debe hacerlo UNA sola vez, no una por fila.
            fallback_bases: list[int] = []
            for payload in active_store.values():
                for row in rows_of(payload):
                    if not any(
                        isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool)
                        and re.search(r"tasa|porcentaje|percent|pct|rate", col, re.I)
                        and any(matches(raw, number(cell)) for raw in cited if "%" in raw)
                        for col, cell in row.items()
                    ):
                        continue
                    for col, cell in row.items():
                        if (
                            isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool)
                            and re.search(r"evaluad|total|cantidad|conversaciones|observaciones|(?:^|_)n(?:$|_)|count", col, re.I)
                            and not re.search(r"tasa|porcentaje|percent|pct|rate", col, re.I)
                            and cell > 0 and cell == int(cell)
                        ):
                            positive.append(number(cell))
                            if int(cell) not in fallback_bases:
                                fallback_bases.append(int(cell))
                            break
            if len(fallback_bases) == 1:
                verdict.limitations.append(f"Base evaluada de los indicadores citados: {fallback_bases[0]} conversaciones.")
            elif fallback_bases:
                listado = ", ".join(str(n) for n in fallback_bases)
                verdict.limitations.append(f"Base evaluada de los indicadores citados, respectivamente: {listado} conversaciones.")
        if not positive:
            if any("%" in raw and any(matches(raw, value) for value in percentages) for raw in cited):
                # Un porcentaje sin su base no se publica (pedido explícito 2026-09-21): el error
                # dispara una corrección que debe informar la cantidad evaluada o quitar la cifra.
                verdict.errors.append('porcentaje sin base evaluada: informá junto a cada porcentaje la cantidad de conversaciones evaluadas (columna de base del SQL) o no presentes ese porcentaje')
            else:
                verdict.limitations.append('No se informó la cantidad de observaciones evaluadas; no se puede determinar la solidez de estos indicadores.')
        # Se quitó (2026-09-21, pedido explícito) el aviso 'Bases evaluadas observadas: 9, 826, ...':
        # una lista de números sueltos al pie de la respuesta que no le decía nada útil a gerencia.
        # Sigue avisándose cuando NO hay ninguna base informada (rama de arriba).
    unavailable = re.search(r"no (?:se puede|hay evaluación|hay evaluacion|se evalu|fue evalu)|sin evaluación|sin evaluacion|no disponible|no permite", prose, re.I)
    metric_cells = [(column, value) for payload in active_store.values() for row in rows_of(payload) for column, value in row.items() if METRIC.search(column) and not BASIS.search(column)]
    null_metrics = bool(metric_cells) and all(value is None for _, value in metric_cells)
    if (null_metrics or any(base <= 0 for base in bases)) and ('%' in prose or re.search(r"promedio|puntuaci[oó]n|cumplimiento", prose, re.I)) and not unavailable:
        verdict.errors.append('indicador presentado como evaluado sin observaciones disponibles')
    if active_store and all(payload.get('truncated') is True for payload in active_store.values()) and re.search(r"ranking completo|todos los|ningún|ningun|ninguna|no hubo", prose, re.I):
        verdict.errors.append('conclusión completa a partir de detalle parcial')
    certainty = [m for m in CONFIDENT.finditer(prose) if not re.search(r"\bno\b|no permite|no se puede|sin evidencia", re.split(r"[.!?\n]", prose[:m.start()])[-1][-60:], re.I)]
    if certainty:
        verdict.errors.append('conclusión de certeza sin evidencia suficiente')
    verdict.limitations = list(dict.fromkeys(verdict.limitations))
    verdict.errors = list(dict.fromkeys(verdict.errors))[:10]
    return verdict


def with_limitations(verdict):
    if not verdict.limitations:
        return verdict.answer
    # Advertencia antes de gráficos/sugerencias, nunca dentro de un bloque de interfaz.
    split = re.search(r"```vera-(?:chart|suggestions)", verdict.answer)
    at = split.start() if split else len(verdict.answer)
    return verdict.answer[:at].rstrip() + '\n\n' + ' '.join(verdict.limitations) + '\n\n' + verdict.answer[at:].lstrip()
