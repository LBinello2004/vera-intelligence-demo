"""Registro de cambios del Data Map por cliente (2026-10-07): qué cambió, por qué, qué se verificó y cómo volver atrás.

Cada promoción automática (y cada reversión) agrega una entrada a dos archivos que viajan en el MISMO commit que el cambio:
- `2. clientes/<cliente>/data_map/CAMBIOS_AUTOMATICOS.md`: legible, la entrada más nueva arriba;
- `2. clientes/<cliente>/data_map/CAMBIOS_AUTOMATICOS.jsonl`: una línea por entrada, para herramientas (`revert_data_map.py`).

Una entrada tiene: fecha, tipo (`promovido`, `revertido` o `prompt_cosmetico`), versión anterior y nueva, qué versiones de prompt de Langfuse
lo motivaron, un RESUMEN de qué cambió en el Data Map (fuentes y campos agregados o quitados, descripciones modificadas, secciones
tocadas), el changelog de Gemini, el resultado del gate, el modelo que regeneró, los intentos y el comando exacto para revertir.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MD_NAME = "CAMBIOS_AUTOMATICOS.md"
JSONL_NAME = "CAMBIOS_AUTOMATICOS.jsonl"
HEADER = (
    "# Cambios automáticos del Data Map de {client}\n\n"
    "Registro generado por la actualización automática (`data_map_auto_update.py`). La entrada más nueva va arriba. Cada cambio llega al repo en el\n"
    "mismo commit que este archivo. Para volver atrás: `python \"4. scripts/revert_data_map.py\" --client {client} --list` y después\n"
    "`... --client {client} --to <N>` (ver `10. documentos/REFRESCO_DIARIO_VM.md`).\n\n"
)
SEPARATOR = "\n---\n\n"


def log_paths(clients_root: Path, client: str) -> tuple[Path, Path]:
    folder = clients_root / client / "data_map"
    return folder / MD_NAME, folder / JSONL_NAME


# --------------------------------------------------------------------------------------------- resumen de qué cambió
def _fields(source: dict) -> dict[str, dict]:
    fields = (source or {}).get("fields") or {}
    return {str(k): (v if isinstance(v, dict) else {"value": v}) for k, v in fields.items()}


def summarize_change(old: dict, new: dict) -> dict:
    """Qué cambió entre dos Data Maps: fuentes y campos agregados/quitados, descripciones modificadas y secciones tocadas."""
    old_sources, new_sources = (old or {}).get("sources") or {}, (new or {}).get("sources") or {}
    summary: dict = {
        "fuentes_agregadas": sorted(set(new_sources) - set(old_sources)),
        "fuentes_quitadas": sorted(set(old_sources) - set(new_sources)),
        "campos_agregados": {}, "campos_quitados": {}, "descripciones_modificadas": {}, "valores_modificados": {},
    }
    for name in sorted(set(old_sources) & set(new_sources)):
        before, after = _fields(old_sources[name]), _fields(new_sources[name])
        added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
        if added:
            summary["campos_agregados"][name] = added
        if removed:
            summary["campos_quitados"][name] = removed
        descriptions = sorted(f for f in set(before) & set(after) if before[f].get("description") != after[f].get("description"))
        if descriptions:
            summary["descripciones_modificadas"][name] = descriptions
        values = sorted(f for f in set(before) & set(after)
                        if before[f].get("configured_values") != after[f].get("configured_values")
                        or before[f].get("observed_values") != after[f].get("observed_values"))
        if values:
            summary["valores_modificados"][name] = values
    touched = sorted(k for k in set(old or {}) | set(new or {}) if k not in ("metadata", "sources") and (old or {}).get(k) != (new or {}).get(k))
    summary["secciones_modificadas"] = touched
    summary["sin_cambios_de_contenido"] = not any((
        summary["fuentes_agregadas"], summary["fuentes_quitadas"], summary["campos_agregados"], summary["campos_quitados"],
        summary["descripciones_modificadas"], summary["valores_modificados"], touched))
    return summary


def build_entry(*, tipo: str, client: str, old_version, new_version, old_file: str = "", new_file: str = "",
                prompts: list[dict] | None = None, change_summary: dict | None = None, changelog: str = "",
                gate: dict | None = None, model: str = "", attempts: int | None = None, reason: str = "",
                now: datetime | None = None) -> dict:
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    revert_to = old_version if tipo in ("promovido", "promovido_manual") and old_version is not None else None
    revert = (f'python "4. scripts/revert_data_map.py" --client {client} --to {revert_to}' if revert_to is not None else "")
    return {"fecha": when, "tipo": tipo, "cliente": client, "version_anterior": old_version, "version_nueva": new_version,
            "archivo_anterior": old_file, "archivo_nuevo": new_file, "prompts": prompts or [], "resumen": change_summary or {},
            "changelog": changelog.strip(), "gate": gate or {}, "modelo_regeneracion": model, "intentos": attempts,
            "motivo": reason, "revertir_con": revert}


# --------------------------------------------------------------------------------------------- escritura
def _lines(label: str, mapping: dict) -> list[str]:
    return [f"  - {label} `{name}`: {', '.join(f'`{item}`' for item in items)}" for name, items in sorted(mapping.items())]


def render_entry(entry: dict) -> str:
    title = {"promovido": "Promovido", "promovido_manual": "Promovido a mano", "revertido": "Revertido", "prompt_cosmetico": "Cambio de prompt cosmético (sin regenerar)"}.get(
        entry["tipo"], entry["tipo"])
    versions = ""
    if entry.get("version_anterior") is not None or entry.get("version_nueva") is not None:
        versions = f": V{entry.get('version_anterior')} → V{entry.get('version_nueva')}"
    out = [f"## {entry['fecha']} · {title}{versions}", ""]
    if entry.get("motivo"):
        out += [f"**Motivo:** {entry['motivo']}", ""]
    for prompt in entry.get("prompts") or []:
        out.append(f"- Prompt `{prompt.get('key')}`: versión {prompt.get('old_version')} → {prompt.get('new_version')} (Langfuse, label production)")
    summary = entry.get("resumen") or {}
    if summary:
        if summary.get("sin_cambios_de_contenido"):
            out.append("- Resumen: el contenido de las fuentes y secciones no cambió (sólo metadatos).")
        else:
            out.append("- Qué cambió en el Data Map:")
            for label, key in (("campos agregados en", "campos_agregados"), ("campos quitados de", "campos_quitados"),
                               ("descripciones modificadas en", "descripciones_modificadas"), ("valores de enum modificados en", "valores_modificados")):
                out += _lines(label, summary.get(key) or {})
            for label, key in (("fuentes agregadas", "fuentes_agregadas"), ("fuentes quitadas", "fuentes_quitadas"),
                               ("secciones modificadas", "secciones_modificadas")):
                if summary.get(key):
                    out.append(f"  - {label}: {', '.join(f'`{item}`' for item in summary[key])}")
    if entry.get("changelog"):
        out += ["- Changelog de la regeneración:", ""] + [f"  > {line}" for line in entry["changelog"].splitlines() if line.strip()] + [""]
    gate = entry.get("gate") or {}
    if gate:
        bits = [f"gate {gate.get('gate', '?')}: {'pasó' if gate.get('passed') else 'no pasó'}"]
        if gate.get("questions_evaluated") is not None:
            bits.append(f"{gate['questions_evaluated']} preguntas del banco dorado")
        if gate.get("llm_questions_checked") is not None:
            bits.append(f"{gate['llm_questions_checked']} respondidas por el agente con el Data Map nuevo")
        out.append("- Verificación: " + ", ".join(bits) + ".")
    if entry.get("modelo_regeneracion"):
        out.append(f"- Regenerado con `{entry['modelo_regeneracion']}` en {entry.get('intentos') or '?'} intento(s).")
    if entry.get("archivo_nuevo"):
        out.append(f"- Archivo: `{entry['archivo_nuevo']}`" + (f" (anterior: `{entry['archivo_anterior']}`)" if entry.get("archivo_anterior") else ""))
    if entry.get("revertir_con"):
        out.append(f"- Para revertir: `{entry['revertir_con']}`")
    return "\n".join(out).rstrip() + "\n"


def append_entry(clients_root: Path, client: str, entry: dict) -> tuple[Path, Path]:
    """Agrega la entrada al .md (arriba de todo, debajo del encabezado) y al .jsonl (al final). Crea los archivos si no existen."""
    md_path, jsonl_path = log_paths(clients_root, client)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    header = HEADER.format(client=client)
    previous = ""
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8")
        previous = text[len(header):] if text.startswith(header) else text
    block = render_entry(entry)
    md_path.write_text(header + block + (SEPARATOR + previous.lstrip("\n") if previous.strip() else ""), encoding="utf-8")
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return md_path, jsonl_path


def read_entries(clients_root: Path, client: str) -> list[dict]:
    """Entradas del registro, de la más vieja a la más nueva. Las líneas ilegibles se ignoran."""
    _, jsonl_path = log_paths(clients_root, client)
    entries: list[dict] = []
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries
