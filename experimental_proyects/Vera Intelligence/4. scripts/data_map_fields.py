"""Campos de una fuente de un Data Map, vengan donde vengan (2026-10-15).

La mayoría de los Data Maps declaran los campos de una fuente en `fields: {nombre: {...}}`. El de Tigo (fuente `performance`) los declara
agrupados en varias claves `fields_<grupo>: {description: "...", nombre: {...}, ...}` (por ejemplo `fields_a_retencion`,
`fields_t_compliance_triggers`) y no tiene `fields`. Todo el código que enumera campos tiene que usar `source_fields`: el planificador
leyó solo `fields`, no vio ninguno de los 63 campos de rendimiento de Tigo, y respondió "no está medido" para métricas que sí existen
(validación de identidad, retención exitosa por región o asesor, conductas de riesgo).
"""
from __future__ import annotations

GROUP_PREFIX = "fields_"


def group_keys(source) -> list[str]:
    """Claves `fields_*` de una fuente que son grupos de campos (un dict)."""
    if not isinstance(source, dict):
        return []
    return [k for k, v in source.items() if isinstance(k, str) and k.startswith(GROUP_PREFIX) and isinstance(v, dict)]


def source_fields(source) -> dict:
    """{nombre_de_campo: especificación} de `fields` más todos los grupos `fields_*`. En un grupo, las entradas que no son dict (como la
    `description` del grupo) no son campos. Si un nombre se repite, rige el último (los grupos van después de `fields`)."""
    out: dict = {}
    if not isinstance(source, dict):
        return out
    base = source.get("fields")
    if isinstance(base, dict):
        out.update(base)
    for key in group_keys(source):
        for name, spec in source[key].items():
            if isinstance(spec, dict):
                out[name] = spec
    return out


def non_field_content(source) -> dict:
    """Todo lo de una fuente que NO son especificaciones de campos: sus claves sueltas más los textos propios de cada grupo (su `description`).
    Sirve para detectar cambios en la fuente que no son de un campo puntual."""
    if not isinstance(source, dict):
        return {}
    skip = {"fields", *group_keys(source)}
    rest = {k: v for k, v in source.items() if k not in skip}
    for key in group_keys(source):
        meta = {k: v for k, v in source[key].items() if not isinstance(v, dict)}
        if meta:
            rest[f"{key}::meta"] = meta
    return rest
