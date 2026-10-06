"""compute_stats: estadística determinista sobre resultados que ya están en la conversación (sin LLM, sin red, sólo `math`).

Trabaja sobre el resultado de una consulta SQL con CONTEOS: éxitos y evaluadas por grupo, con intervalo de Wilson 95 % y de Newcombe
para la diferencia entre dos grupos. (Hasta 2026-10-06 tenía además un modo RANGO para los resultados de `extract_insight`, la lectura
de conversaciones con JEV, que se descartó: ver `3. experimentos/jev_extraccion/README.md`.)

El modelo NO pasa números: pasa el `id` de un resultado anterior (campo `verification.id`) y los NOMBRES de las columnas. Así no
puede inventar un dato, y la salida (con la forma de un resultado SQL, `columns`/`rows`) entra a la misma verificación de cifras.

Operaciones: proporcion (tasa por grupo), ranking (ordenado, con N mínimo y empates técnicos) y comparar (diferencia entre
dos grupos o períodos y si se distingue de cero, más un aviso si las bases son muy distintas: un período puede estar incompleto).
"""
from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from answer_verification import rows_of

MIN_N = 30          # evaluadas mínimas (conteos) para ocupar un puesto o sacar una conclusión
MAX_FILAS = 200     # tope de grupos por llamada
Z = 1.96
OPERACIONES = ("proporcion", "ranking", "comparar")


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def newcombe_diff(k1: int, n1: int, k2: int, n2: int, z: float = Z) -> tuple[float, float, float]:
    """Diferencia p1 - p2 con intervalo de Newcombe (Wilson híbrido): (diferencia, mínimo, máximo)."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    return d, d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


def _decimal(value: Any, what: str, row_label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what} de «{row_label}» está vacío o no es numérico.")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"{what} de «{row_label}» no es un número: {value!r}.") from exc
    if not d.is_finite():
        raise ValueError(f"{what} de «{row_label}» no es finito.")
    return d


def _count(value: Any, what: str, row_label: str) -> int:
    d = _decimal(value, what, row_label)
    if d != d.to_integral() or d < 0:
        raise ValueError(f"{what} de «{row_label}» debe ser un conteo entero no negativo (llegó {value!r}); "
                         "pedí en la consulta el CONTEO (count), no la tasa.")
    return int(d)


def _pct(x: float) -> float:
    return round(100 * x, 2)


def _source_rows(store: dict, fuente_id: str, columnas: list[str]) -> list[dict]:
    if not isinstance(fuente_id, str) or fuente_id not in store:
        raise ValueError("fuente_id inexistente: usá el `id` que devolvió la herramienta (campo verification.id) "
                         "de una consulta de ESTA conversación.")
    payload = store[fuente_id]
    if payload.get("truncated") is True:
        raise ValueError("ese resultado está truncado: no sirve para un ranking ni una comparación.")
    rows = rows_of(payload)
    if not rows:
        raise ValueError("ese resultado no tiene filas.")
    if len(rows) > MAX_FILAS:
        raise ValueError(f"demasiadas filas ({len(rows)}); el máximo es {MAX_FILAS} grupos.")
    for col in columnas:
        if col and col not in rows[0]:
            raise ValueError(f"la columna {col!r} no está en ese resultado. Columnas disponibles: " + ", ".join(rows[0]))
    return rows


def _label(row: dict, i: int, total: int, col_g: str) -> str:
    return str(row.get(col_g)) if col_g else (f"fila_{i + 1}" if total > 1 else "total")


def _unique(groups: list[dict]) -> list[dict]:
    labels = [g["grupo"] for g in groups]
    if len(set(labels)) != len(labels):
        raise ValueError("hay grupos repetidos en la columna de grupo: dejá una fila por grupo antes de calcular.")
    return groups


def _groups_counts(rows: list[dict], col_k: str, col_n: str, col_g: str) -> list[dict]:
    out = []
    for i, row in enumerate(rows):
        label = _label(row, i, len(rows), col_g)
        k = _count(row.get(col_k), "El conteo de éxitos", label)
        n = _count(row.get(col_n), "El conteo de evaluadas", label)
        if n == 0:
            raise ValueError(f"«{label}» tiene 0 evaluadas: no se puede calcular una tasa.")
        if k > n:
            raise ValueError(f"«{label}» tiene más éxitos ({k}) que evaluadas ({n}): revisá las columnas.")
        lo, hi = wilson(k, n)
        out.append({"grupo": label, "exitos": k, "evaluadas": n, "p": k / n, "lo": lo, "hi": hi,
                    "suficiente": n >= MIN_N})
    return _unique(out)


def _row_counts(g: dict) -> dict:
    return {"grupo": g["grupo"], "exitos": g["exitos"], "evaluadas": g["evaluadas"], "tasa_pct": _pct(g["p"]),
            "ic95_min_pct": _pct(g["lo"]), "ic95_max_pct": _pct(g["hi"]), "base_suficiente": "si" if g["suficiente"] else "no"}


def _separated(g: dict, nxt: dict, orden: str) -> bool:
    return (g["lo"] > nxt["hi"]) if orden == "desc" else (g["hi"] < nxt["lo"])


def compute(store: dict, operacion: str, fuente_id: str, columna_exitos: str = "", columna_total: str = "",
            columna_grupo: str = "", grupo_a: str = "", grupo_b: str = "", fuente_id_b: str = "",
            orden: str = "desc") -> dict:
    """Devuelve un resultado con la forma de SQL (`columns`/`rows`) más `metodo` y `notas`."""
    operacion = (operacion or "").strip().lower()
    if operacion not in OPERACIONES:
        raise ValueError("operacion inválida: usá " + ", ".join(OPERACIONES) + ".")
    if not (columna_exitos and columna_total):
        raise ValueError("pasá columna_exitos y columna_total (conteos).")
    if orden not in ("asc", "desc"):
        raise ValueError("orden inválido: usá asc o desc.")
    cols_src = [columna_exitos, columna_total, columna_grupo]

    def load(fid: str) -> list[dict]:
        return _groups_counts(_source_rows(store, fid, cols_src), columna_exitos, columna_total, columna_grupo)

    groups = load(fuente_id)

    if operacion == "proporcion":
        data = [_row_counts(g) for g in groups]
        cols = ["grupo", "exitos", "evaluadas", "tasa_pct", "ic95_min_pct", "ic95_max_pct", "base_suficiente"]
        notas = [f"Intervalo de Wilson al 95 %; con menos de {MIN_N} evaluadas no se concluye sobre el grupo."]
    elif operacion == "ranking":
        elegibles = sorted((g for g in groups if g["suficiente"]), key=lambda g: (-g["p"] if orden == "desc" else g["p"], g["grupo"]))
        chicos = [g for g in groups if not g["suficiente"]]
        data = []

        def nombres(items: list[dict]) -> str:
            return "; ".join(h["grupo"] for h in items) or "ninguno"

        for i, g in enumerate(elegibles):
            marca = ("si" if _separated(g, elegibles[i + 1], orden) else "no") if i + 1 < len(elegibles) else "ultimo"
            otros = [h for h in elegibles if h is not g]
            data.append({"puesto": i + 1, **_row_counts(g), "separado_del_siguiente": marca,
                         # Contra QUIÉN se distingue cada grupo (no sólo el vecino): un grupo lejano en el ranking puede
                         # seguir superponiéndose con otro, y afirmar "A supera a B" exige que B figure acá.
                         "claramente_por_encima_de": nombres([h for h in otros if g["lo"] > h["hi"]]),
                         "claramente_por_debajo_de": nombres([h for h in otros if g["hi"] < h["lo"]])})
        for g in chicos:
            data.append({"puesto": None, **_row_counts(g), "separado_del_siguiente": "sin_puesto",
                         "claramente_por_encima_de": "sin_puesto", "claramente_por_debajo_de": "sin_puesto"})
        sep = ["separado_del_siguiente", "claramente_por_encima_de", "claramente_por_debajo_de", "base_suficiente"]
        cols = ["puesto", "grupo", "exitos", "evaluadas", "tasa_pct", "ic95_min_pct", "ic95_max_pct"] + sep
        notas = [f"Sólo ocupan puesto los grupos con al menos {MIN_N} evaluadas (los demás figuran sin puesto).",
                 "separado_del_siguiente='no' = empate técnico con el grupo que sigue: los intervalos se superponen.",
                 "Para afirmar que A es mayor que B, B tiene que figurar en claramente_por_encima_de de A (los intervalos no se "
                 "superponen). Si no figura, no se puede decir que A supere a B, aunque esté más arriba en el ranking."]
    else:  # comparar
        groups_b = load(fuente_id_b) if fuente_id_b else groups

        def pick(pool: list[dict], label: str, side: str) -> dict:
            if label:
                found = [g for g in pool if g["grupo"] == label]
                if len(found) != 1:
                    raise ValueError(f"no encontré el grupo {label!r} ({side}); grupos disponibles: "
                                     + ", ".join(g["grupo"] for g in pool))
                return found[0]
            if len(pool) != 1:
                raise ValueError(f"indicá grupo_{side} (hay {len(pool)} grupos en ese resultado).")
            return pool[0]

        a, b = pick(groups, grupo_a, "a"), pick(groups_b, grupo_b, "b")
        d, lo, hi = newcombe_diff(a["exitos"], a["evaluadas"], b["exitos"], b["evaluadas"])
        na, nb = a["evaluadas"], b["evaluadas"]
        if not (a["suficiente"] and b["suficiente"]):
            aviso = "base_insuficiente"
        else:
            aviso = "bases_muy_distintas" if max(na, nb) / min(na, nb) > 2 else "ok"
        distinguible = "si" if (lo > 0 or hi < 0) else "no"
        cols = ["grupo_a", "grupo_b", "exitos_a", "evaluadas_a", "tasa_a_pct", "exitos_b", "evaluadas_b", "tasa_b_pct",
                "diferencia_pct_puntos", "ic95_dif_min_pct", "ic95_dif_max_pct", "diferencia_distinguible", "aviso_base"]
        data = [{"grupo_a": a["grupo"], "grupo_b": b["grupo"], "exitos_a": a["exitos"], "evaluadas_a": a["evaluadas"],
                 "tasa_a_pct": _pct(a["p"]), "exitos_b": b["exitos"], "evaluadas_b": b["evaluadas"], "tasa_b_pct": _pct(b["p"]),
                 "diferencia_pct_puntos": _pct(d), "ic95_dif_min_pct": _pct(lo), "ic95_dif_max_pct": _pct(hi),
                 "diferencia_distinguible": distinguible, "aviso_base": aviso}]
        notas = ["Diferencia = grupo_a - grupo_b en puntos porcentuales, intervalo de Newcombe al 95 %.",
                 "diferencia_distinguible='no' = el intervalo incluye el 0: no se puede afirmar que haya diferencia.",
                 "aviso_base='bases_muy_distintas' = una base es más del doble que la otra: el período más chico puede estar "
                 "incompleto; decilo antes de concluir."]
    return {"columns": cols, "rows": [[r[c] for c in cols] for r in data],
            "metodo": f"compute_stats/{operacion}/conteos", "notas": notas}


def compute_json(store: dict, **kwargs: Any) -> str:
    return json.dumps(compute(store, **kwargs), ensure_ascii=False)
