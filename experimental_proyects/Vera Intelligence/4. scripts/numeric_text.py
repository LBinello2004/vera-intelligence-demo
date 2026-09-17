"""Extracción y comparación de números dentro de texto en español/inglés mezclado.

Extraído de `data_map_auto_update.py` (2026-09-14) para reusarlo también en `vi_agent.py` sin crear
un import circular -`data_map_auto_update.py` ya importa `vi_agent`, así que `vi_agent` no puede
importar de vuelta desde `data_map_auto_update`. Comportamiento sin cambios: mismo código, misma
lógica, sólo movido a un módulo sin dependencias hacia ninguno de los dos. `data_map_auto_update.py`
sigue exponiendo estos nombres (los reimporta acá) para no romper nada que ya los referenciaba.
"""

from __future__ import annotations

import re

NUMBER_SPAN_RE = re.compile(r"\d[\d.,]*\d|\d")


def normalize_number(raw: str) -> str:
    """Normaliza un número a un string sin separador de miles, con '.' como decimal.

    El agente escribe números en convenciones mezcladas dentro de la misma respuesta
    (punto de miles + coma decimal al estilo español, ej. "247.556" / "95,96%"; o
    coma de miles + punto decimal al estilo inglés, ej. "247,556" / "95.96%"; o sin
    separador). Comparar los tokens crudos sin normalizar generaba falsos rechazos
    masivos (visto en la segunda corrida real de prueba de `data_map_auto_update.py`)
    -"95,96" y "95.96" son el mismo número, no dos distintos-.
    """
    dot_count, comma_count = raw.count("."), raw.count(",")
    if dot_count > 1 or comma_count > 1:
        # Múltiplos grupos: el separador repetido es de miles (nunca hay dos
        # separadores decimales en un mismo número).
        if dot_count > 1 and comma_count <= 1:
            return raw.replace(".", "").replace(",", ".")
        if comma_count > 1 and dot_count <= 1:
            return raw.replace(",", "")
        return raw.replace(".", "").replace(",", "")
    if dot_count == 1 and comma_count == 1:
        # Uno de cada uno: el que aparece primero es de miles, el último es decimal.
        if raw.rindex(".") < raw.rindex(","):
            return raw.replace(".", "").replace(",", ".")
        return raw.replace(",", "")
    if dot_count == 1:
        left, right = raw.split(".")
        return left + right if len(right) == 3 else raw  # 3 dígitos -> de miles
    if comma_count == 1:
        left, right = raw.split(",")
        return left + right if len(right) == 3 else left + "." + right
    return raw


def numbers_in(text: str) -> set[str]:
    """Números normalizados con valor de negocio real: filtra ruido incidental.

    Un número entero chico y sin separador (ej. "24" de "Farma 24", "5" de "top 5",
    "9" de un número de paso) casi nunca es una métrica -las que importan en este
    dominio son conteos de cientos para arriba o tasas con decimales-. Excluirlos
    evita falsos rechazos por texto que menciona el mismo número de forma incidental.
    """
    result = set()
    for token in NUMBER_SPAN_RE.findall(text):
        normalized = normalize_number(token)
        if "." not in normalized and len(normalized) < 3:
            continue
        result.add(normalized)
    return result


def is_float(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def close_enough(value: float, other: float) -> bool:
    """Tolerancia relativa (no fija): dos lecturas del mismo dato no son necesariamente
    simultáneas -con una base que ingiere filas nuevas todo el tiempo, incluso un par de minutos
    de diferencia puede mover un conteo en algunas unidades sin que eso sea una discrepancia real.
    Una tolerancia fija (ej. ±0.1) alcanza para una tasa pero es demasiado estricta para un conteo
    de miles; una tolerancia puramente porcentual es demasiado laxa para números chicos. Se
    combinan ambas: el mayor entre ±1 absoluto y ±0.5% del valor.
    """
    tolerance = max(1.0, 0.005 * max(abs(value), abs(other)))
    return abs(value - other) <= tolerance
