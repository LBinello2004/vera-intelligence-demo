"""Gate v2 de la actualización automática del Data Map: contra el SQL dorado, no respuesta contra respuesta (2026-10-07).

Por qué un gate nuevo. El gate original (`data_map_auto_update.run_gate`) corre cada pregunta del banco dorado DOS veces con el agente
-Data Map vigente y candidato- y compara TODOS los números de las dos respuestas. Medido sobre el historial real (16 de 20 cambios de
prompt rechazados a la primera): el SQL dorado nunca fue el problema (0 inválidos en 188 preguntas) y el rechazo venía de números de
contexto sueltos que una respuesta traía y la otra no (la candidata agrega el total y el % de cobertura y se la rechaza por "números
nuevos"). Además `validate_readonly_sql` valida contra las fuentes de `config.yaml`, no contra el Data Map candidato, así que el chequeo
de SQL nunca evaluaba al candidato. Y cuesta 20 a 25 minutos.

Qué hace este gate:
1. ESTRUCTURA (determinista, segundos): el candidato conserva las claves de primer nivel y las fuentes del Data Map vigente.
2. CAMPOS: ningún campo que el SQL dorado de una pregunta usa y que el Data Map vigente declaraba desaparece del candidato.
3. VERDAD: se ejecuta el SQL dorado contra la base (el mismo que ya valida la corrida) para tener los números correctos de hoy.
4. RESPUESTA (con modelo, sólo del candidato y sólo para unas pocas preguntas de resultado chico): el agente con el Data Map candidato
   tiene que contener esos números (con la misma tolerancia de siempre; los números de más no cuentan). Si falla, se pregunta una segunda
   vez antes de rechazar (la redacción de un modelo varía entre corridas).

Dos correcciones tras probarlo con Tigo (2026-10-07; el Data Map vigente fallaba su propio gate): (a) los números de la verdad se buscan
incluyendo enteros chicos (antes un conteo de 93 era inhallable); (b) si el candidato no menciona un número, se le hace la misma pregunta
al Data Map VIGENTE y sólo se rechaza si el vigente sí lo mencionaba (regresión). Así los denominadores que el SQL dorado cuenta distinto
de como los reporta el agente ya no rechazan a nadie.

Lo que NO resuelve (igual que el gate original): que un cambio de prompt de negocio haya quedado BIEN interpretado en el Data Map. Es un
control mecánico de que el candidato no rompe lo que ya se medía.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Callable

from sqlglot import exp, parse

from numeric_text import NUMBER_SPAN_RE, close_enough, is_float, normalize_number

MAX_TRUTH_ROWS = 3          # resultados más largos que esto no se usan como verdad numérica (una respuesta no los lista completos)
DEFAULT_LLM_QUESTIONS = 4   # preguntas que se le hacen al agente con el Data Map candidato
DEFAULT_MAX_MISSING = 1     # números de la verdad que pueden faltar en la respuesta (override: `max_missing_numbers` de la pregunta)
ANSWER_ATTEMPTS = 2         # veces que se pregunta antes de rechazar por números
REGRESSION_EXTRA_ATTEMPTS = 3   # intentos extra (sólo si ya parece una regresión) antes de rechazar


# --------------------------------------------------------------------------------------------- análisis del Data Map
def declared_fields(data_map: dict) -> dict[str, set[str]]:
    """{'schema.vista': {campos declarados}} de un Data Map. Incluye los campos dentro de grupos `campos:`."""
    out: dict[str, set[str]] = {}
    for source in ((data_map or {}).get("sources") or {}).values():
        if not isinstance(source, dict):
            continue
        view = str(source.get("source") or "").strip().lower()
        if "." not in view:
            continue
        names = out.setdefault(view, set())
        for field_name, spec in (source.get("fields") or {}).items():
            names.add(str(field_name).lower())
            if isinstance(spec, dict) and isinstance(spec.get("campos"), dict):
                names.update(str(inner).lower() for inner in spec["campos"])
    return out


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def change_footprint(old: dict, new: dict) -> dict:
    """Qué tocó el candidato respecto del Data Map vigente, para saber qué preguntas del banco pueden verse afectadas.

    `tables`: {'schema.vista': {campos cambiados, agregados o quitados}} (con "*" si cambió algo de la fuente que no es un campo);
    `global`: cambió una sección que el agente lee para TODAS las preguntas (reglas SQL, ruteo, joins, perfiles...);
    `limitations`: sólo cambió la sección de limitaciones (el regenerador suele agregar ahí su línea de historial de versiones).
    `metadata` no cuenta: el agente no decide nada con ella."""
    old, new = old or {}, new or {}
    tables: dict[str, set[str]] = {}
    old_sources, new_sources = old.get("sources") or {}, new.get("sources") or {}
    for name in set(old_sources) | set(new_sources):
        before, after = old_sources.get(name), new_sources.get(name)
        if _canon(before) == _canon(after):
            continue
        view = str(((after if isinstance(after, dict) else before) or {}).get("source") or name).strip().lower()
        touched = tables.setdefault(view, set())
        if not isinstance(before, dict) or not isinstance(after, dict):
            touched.add("*")
            continue
        fields_before, fields_after = before.get("fields") or {}, after.get("fields") or {}
        for field_name in set(fields_before) | set(fields_after):
            if _canon(fields_before.get(field_name)) != _canon(fields_after.get(field_name)):
                touched.add(str(field_name).lower())
                for spec in (fields_before.get(field_name), fields_after.get(field_name)):
                    if isinstance(spec, dict) and isinstance(spec.get("campos"), dict):
                        touched.update(str(inner).lower() for inner in spec["campos"])
        if _canon({k: v for k, v in before.items() if k != "fields"}) != _canon({k: v for k, v in after.items() if k != "fields"}):
            touched.add("*")
    skipped = {"metadata", "sources", "limitations"}
    global_changed = any(_canon(old.get(k)) != _canon(new.get(k)) for k in (set(old) | set(new)) - skipped)
    return {"tables": tables, "global": global_changed, "limitations": _canon(old.get("limitations")) != _canon(new.get("limitations"))}


def question_affected(sql: str | None, footprint: dict) -> bool:
    """¿El cambio puede afectar la respuesta a esta pregunta? Conservador: si no se puede analizar el SQL, sí."""
    if footprint["global"]:
        return True
    if not footprint["tables"]:
        return False
    if not sql:
        return True
    try:
        used = physical_columns(sql)
    except Exception:  # noqa: BLE001
        return True
    if not used:
        return True
    for table, columns in used.items():
        touched = footprint["tables"].get(table)
        if touched and ("*" in touched or columns & touched):
            return True
    return False


def structural_problems(old: dict, new: dict) -> list[str]:
    """Qué perdió el candidato respecto del vigente en su estructura (claves de primer nivel y fuentes)."""
    problems: list[str] = []
    if not isinstance(new, dict) or not isinstance(new.get("sources"), dict) or not new["sources"]:
        return ["el candidato no declara fuentes"]
    lost_keys = sorted(set(old or {}) - set(new))
    if lost_keys:
        problems.append("claves de primer nivel perdidas: " + ", ".join(lost_keys))
    lost_sources = sorted(set(declared_fields(old)) - set(declared_fields(new)))
    if lost_sources:
        problems.append("fuentes eliminadas: " + ", ".join(lost_sources))
    return problems


# --------------------------------------------------------------------------------------------- análisis del SQL dorado
def _table_name(table: exp.Table) -> str:
    name = table.name.lower()
    return f"{table.db.lower()}.{name}" if table.db else name


def physical_columns(sql: str) -> dict[str, set[str]]:
    """{'schema.vista': {columnas}} de las tablas físicas que usa el SQL.

    Conservador para no inventar rechazos: una columna sin calificar sólo se le asigna a una tabla si el scope (SELECT) tiene UNA tabla;
    los alias definidos en el mismo SELECT (`count(*) AS total ... ORDER BY total`) no cuentan como columnas."""
    statements = parse(sql, read="postgres")
    if len(statements) != 1:
        return {}
    statement = statements[0]
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    out: dict[str, set[str]] = {}
    for select in statement.find_all(exp.Select):
        tables = {}
        for table in select.find_all(exp.Table):
            if table.find_ancestor(exp.Select) is not select:
                continue
            name = _table_name(table)
            if not table.db or name in cte_names:
                continue
            tables[(table.alias_or_name or table.name).lower()] = name
        if not tables:
            continue
        aliases = {item.alias.lower() for item in select.expressions if isinstance(item, exp.Alias) and item.alias}
        for column in select.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not select:
                continue
            name = column.name.lower()
            qualifier = column.table.lower() if column.table else ""
            if qualifier:
                table = tables.get(qualifier)
            elif name in aliases or len(tables) != 1:
                table = None
            else:
                table = next(iter(tables.values()))
            if table:
                out.setdefault(table, set()).add(name)
    return out


def dropped_fields(sql: str, old: dict, new: dict) -> list[str]:
    """Campos que el SQL dorado usa, que el Data Map vigente declaraba y que el candidato ya no declara."""
    used, before, after = physical_columns(sql), declared_fields(old), declared_fields(new)
    problems: list[str] = []
    for table, columns in used.items():
        if table not in before:
            continue
        needed = columns & before[table]
        if not needed:
            continue
        if table not in after:
            problems.append(f"{table}: la fuente ya no está en el candidato")
        elif needed - after[table]:
            problems.append(f"{table}: faltan en el candidato {', '.join(sorted(needed - after[table]))}")
    return problems


# --------------------------------------------------------------------------------------------- verdad numérica y cobertura
def truth_from_result(result: str | dict) -> list[str]:
    """Números de un resultado SQL (`columns`/`rows`) si es chico (<= MAX_TRUTH_ROWS filas); [] si no sirve como verdad."""
    payload = json.loads(result) if isinstance(result, str) else result
    rows = payload.get("rows") or []
    if not rows or len(rows) > MAX_TRUTH_ROWS or payload.get("truncated") is True:
        return []
    numbers: list[str] = []
    for row in rows:
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                continue
            token = normalize_number(str(value))
            if is_float(token) and token not in numbers:
                numbers.append(token)
    return numbers


def all_numbers_in(text: str) -> set[str]:
    """Todos los números de un texto, también los enteros chicos. `numeric_text.numbers_in` descarta los de menos de 3 dígitos (ruido al
    verificar números inventados), pero acá se busca un número CONOCIDO: con ese filtro un conteo de 93 nunca podía aparecer y la pregunta
    era imposible de aprobar para cualquier candidato (incluido el Data Map vigente)."""
    return {normalize_number(token) for token in NUMBER_SPAN_RE.findall(text)}


def covers(truth: list[str], answer: str, max_missing: int = DEFAULT_MAX_MISSING) -> tuple[bool, list[str]]:
    """¿La respuesta contiene los números de la verdad? Los números de más no cuentan. Una fracción (0,2037) vale también como 20,37 %."""
    found = all_numbers_in(answer)
    floats = [float(token) for token in found if is_float(token)]
    missing: list[str] = []
    for token in truth:
        value = float(token)
        candidates = [value] + ([value * 100] if 0 < abs(value) < 1 else [])
        if token in found or any(close_enough(c, f) for c in candidates for f in floats):
            continue
        missing.append(token)
    return len(missing) <= max_missing, missing


# --------------------------------------------------------------------------------------------- el gate
def run_gate_v2(
    *, old_data_map: dict, new_data_map: dict, bank: dict, run_sql: Callable[[str], str | dict],
    ask: Callable[[str], str], max_questions: int = 10, llm_questions: int = DEFAULT_LLM_QUESTIONS,
    baseline_ask: Callable[[str], str] | None = None, baseline_cache: dict | None = None,
    assume_all_affected: bool = False,
) -> dict:
    """Devuelve un dict auditable: `passed`, `structural_problems`, `questions` (una entrada por pregunta) y contadores.

    `run_sql(sql)` ejecuta el SQL dorado (de sólo lectura); `ask(pregunta)` hace contestar al agente con el Data Map candidato."""
    all_questions = bank.get("preguntas") or []
    questions = all_questions[:max_questions]
    structural = structural_problems(old_data_map, new_data_map)
    footprint = change_footprint(old_data_map, new_data_map)
    affected = {item.get("id"): assume_all_affected or question_affected(item.get("sql"), footprint) for item in questions}
    sentinel_id = None
    if not any(affected.values()) and footprint["limitations"]:
        # sólo cambió `limitations`: una pregunta centinela (la primera del banco) basta para ver que no se rompió la lectura del mapa
        sentinel_id = questions[0].get("id") if questions else None
    baseline_cache = baseline_cache if baseline_cache is not None else {}
    records: list[dict] = []
    llm_used = 0
    to_check: list[tuple[dict, dict]] = []
    for item in questions:
        record = {"id": item.get("id"), "affected_by_change": bool(affected.get(item.get("id")) or item.get("id") == sentinel_id), "sql_ok": True, "sql_error": None, "dropped_fields": [], "truth_numbers": [],
                  "llm_checked": False, "numbers_ok": True, "missing_numbers": [], "attempts": 0, "answer": "", "error": None}
        sql = item.get("sql")
        if sql:
            try:
                record["dropped_fields"] = dropped_fields(sql, old_data_map, new_data_map)
            except Exception as exc:  # noqa: BLE001 -un SQL que sqlglot no entiende no es culpa del candidato
                record["sql_error"] = f"no pude analizar los campos del SQL dorado: {exc}"
            # El SQL dorado solo se ejecuta si va a servir de verdad (pregunta afectada por el cambio y todavía hay cupo de preguntas al agente):
            # en Farma cada consulta puede tardar decenas de segundos y las diez juntas eran la mitad del gate.
            if record["affected_by_change"] and llm_used < llm_questions and not item.get("omit_from_numeric_gate"):
                try:
                    record["truth_numbers"] = truth_from_result(run_sql(sql))
                except Exception as exc:  # noqa: BLE001 -si no se puede verificar, no se promueve solo
                    record["sql_error"] = str(exc)[:300]
                    if str(getattr(exc, "sqlstate", "") or "").startswith("42"):
                        # Error determinista del propio SQL dorado (columna que cambió de tipo, vista renombrada): no depende del Data Map, así que
                        # rechazaría a TODO candidato para siempre. Se informa como problema del banco (hace falta arreglarlo) sin bloquear.
                        record["bank_problem"] = record["sql_error"]
                    else:
                        record["sql_ok"] = False       # caída de la base, timeout, permisos: no se pudo verificar
        if record["truth_numbers"] and record["affected_by_change"] and llm_used < llm_questions and not item.get("omit_from_numeric_gate"):
            llm_used += 1
            record["llm_checked"] = True
            to_check.append((item, record))
        records.append(record)

    def positions(truth: list[str], missing: list[str]) -> list[int]:
        """Qué números de la verdad faltan, por POSICIÓN y no por valor: los conteos cambian todos los días (la tabla crece), las posiciones no."""
        return sorted({truth.index(token) for token in missing if token in truth})

    def check_llm(item: dict, record: dict) -> None:
        max_missing = int(item.get("max_missing_numbers", DEFAULT_MAX_MISSING))
        try:
            for attempt in range(1, ANSWER_ATTEMPTS + 1):
                record["attempts"] = attempt
                answer = ask(item["pregunta"])
                record["answer"] = answer[:600]
                ok, missing = covers(record["truth_numbers"], answer, max_missing)
                record["numbers_ok"], record["missing_numbers"] = ok, missing
                if ok:
                    break
            if not record["numbers_ok"] and baseline_ask is not None:
                # Sin regresión: lo que el candidato no menciona pero el Data Map VIGENTE tampoco (denominadores que el SQL dorado cuenta
                # distinto, números de un banco armado cuando la tabla era más chica) no es culpa del candidato.
                truth = record["truth_numbers"]
                baseline_missing = baseline_cache.get(item.get("id"))
                # El caché guarda POSICIONES (enteros). Un caché con valores (formato viejo, números de otro día) no sirve: se vuelve a medir.
                if not isinstance(baseline_missing, list) or any(not isinstance(i, int) for i in baseline_missing):
                    baseline_missing = None
                    for _ in range(ANSWER_ATTEMPTS):
                        _, missing_before = covers(truth, baseline_ask(item["pregunta"]), max_missing)
                        found = positions(truth, missing_before)
                        if baseline_missing is None or len(found) < len(baseline_missing):
                            baseline_missing = found
                        if not baseline_missing:
                            break
                    baseline_cache[item.get("id")] = baseline_missing   # el vigente no cambia: no se vuelve a preguntar
                record["baseline_missing"] = [truth[i] for i in baseline_missing if i < len(truth)]
                allowed = set(baseline_missing)

                def regression_free(missing: list[str]) -> bool:
                    # Se tolera lo que el vigente tampoco mencionaba, más hasta `max_missing` números nuevos (por ejemplo, un denominador que el
                    # candidato cuenta con otra base): perder DOS cifras que el vigente sí daba ya es una regresión.
                    return len(set(positions(truth, missing)) - allowed) <= max_missing

                if regression_free(record["missing_numbers"]):
                    record["numbers_ok"] = True
                    record["regression"] = False
                else:
                    # Un modelo no responde igual dos veces (medido en Tigo: el mismo Data Map omitió una cifra en 2 intentos seguidos).
                    # Antes de llamarlo regresión, unos intentos más: basta que UNA respuesta no pierda nada que el vigente sí diera.
                    for extra in range(1, REGRESSION_EXTRA_ATTEMPTS + 1):
                        record["attempts"] += 1
                        answer = ask(item["pregunta"])
                        ok, missing = covers(record["truth_numbers"], answer, max_missing)
                        if ok or regression_free(missing):
                            record["answer"], record["missing_numbers"] = answer[:600], missing
                            record["numbers_ok"], record["regression"] = True, False
                            break
                    else:
                        record["regression"] = True
        except Exception as exc:  # noqa: BLE001
            record["numbers_ok"] = False
            record["error"] = str(exc)[:300]

    # Las preguntas al agente corren a la vez (cada una es una conversación independiente con Gemini; el SQL que hagan se serializa solo por el
    # candado de la conexión). Antes iban una detrás de otra: 4 preguntas con varios intentos eran la mayor parte del gate.
    if to_check:
        with ThreadPoolExecutor(max_workers=len(to_check)) as pool:
            list(pool.map(lambda pair: check_llm(*pair), to_check))
    passed = not structural and all(
        r["sql_ok"] and not r["dropped_fields"] and r["numbers_ok"] and r["error"] is None for r in records)
    return {"passed": passed, "structural_problems": structural, "questions": records, "footprint": {
                "tables": {k: sorted(v) for k, v in footprint["tables"].items()}, "global": footprint["global"],
                "limitations": footprint["limitations"]},
            "bank_problems": [{"id": r["id"], "error": r["bank_problem"]} for r in records if r.get("bank_problem")],
            "questions_affected": sum(1 for r in records if r["affected_by_change"]), "bank_size": len(all_questions),
            "questions_evaluated": len(questions), "llm_questions_checked": llm_used, "llm_questions_max": llm_questions}
