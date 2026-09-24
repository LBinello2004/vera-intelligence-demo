"""Evaluación del banco dorado contra la VERDAD de Postgres (no contra otra respuesta del agente).

Por qué existe (2026-09-24): el gate de `data_map_auto_update.py` compara los números de la
respuesta con la versión vigente contra la versión candidata. Con respuestas no determinísticas eso
es ruido -en Steren falló 2 de 2 veces por variación de redacción y por un cambio de base
INTENCIONAL (V3 filtra usefulforanalysis)-, y además sólo corre las primeras
`MAX_GOLDEN_QUESTIONS` preguntas, por eso la q13 de Steren tuvo una columna inexistente sin que
nadie lo viera. Acá, para cada pregunta: (1) se ejecuta el SQL de verdad del banco EN VIVO
(`sql_verdad` si existe; si no, `sql` con el filtro de analizables agregado automáticamente, o el original si la vista no expone la columna) -las preguntas con SQL roto se reportan, no se saltan en
silencio-; (2) se corre el agente N veces con el Data Map elegido; (3) se mide qué fracción de las
cifras enteras de la verdad aparece en la respuesta (`cobertura`) y cuántas veces el agente cayó al
mensaje de "no pude verificar" (`fallback`). Sirve para comparar versiones (V1 vs V3) con la misma vara.

Llama a Gemini (N corridas x preguntas): usarlo a propósito, no en un loop. Uso desde la raíz:
    python "experimental_proyects/Vera Intelligence/4. scripts/golden_groundtruth_eval.py" --client steren_alto --data-map V3 --runs 3
`--data-map` acepta el sufijo de versión ("V3") o una ruta. Sin él, usa la versión vigente.
Deja el detalle (incluidas las respuestas) en .runtime/golden_eval/<cliente>/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
REPO_ROOT = PROJECT_ROOT.parents[1]
CLIENTS_ROOT = PROJECT_ROOT / "2. clientes"
for _path in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

FALLBACK_PREFIX = "No pude verificar"
MIN_KEY_NUMBER = 10  # los enteros chicos (1, 2, 5...) coinciden por azar con cualquier texto


def expected_numbers(rows: list[tuple]) -> tuple[set[int], set[float]]:
    """Cifras clave de la verdad: enteros >= 10 de las primeras filas, y el % de una fila (num, base)."""
    integers: set[int] = set()
    percentages: set[float] = set()
    for row in rows[:5]:
        for value in row:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if float(value) == int(value) and value >= MIN_KEY_NUMBER:
                    integers.add(int(value))
    if len(rows) == 1 and len(rows[0]) == 2:
        numerator, base = rows[0]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (numerator, base)) and base:
            percentages.add(round(100 * numerator / base, 1))
    return integers, percentages


def answer_numbers(text: str) -> tuple[set[int], set[float]]:
    """Enteros (sin separadores de miles, '.' o ',') y decimales de una respuesta, sin bloques de código."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    integers: set[int] = set()
    decimals: set[float] = set()
    for match in re.finditer(r"\d[\d.,]*", text):
        token = match.group().rstrip(".,")
        digits = re.sub(r"[.,]", "", token)
        if digits:
            integers.add(int(digits))
        decimal = re.fullmatch(r"(\d+)[.,](\d{1,2})", token)
        if decimal:
            decimals.add(float(f"{decimal.group(1)}.{decimal.group(2)}"))
    return integers, decimals


def score_answer(answer: str, expected_ints: set[int], expected_pcts: set[float]) -> dict:
    ints, decimals = answer_numbers(answer)
    return {
        "fallback": answer.startswith(FALLBACK_PREFIX),
        "cov_int": (len(expected_ints & ints) / len(expected_ints)) if expected_ints else None,
        "cov_pct": (
            1.0 if any(abs(p - d) <= 0.15 for p in expected_pcts for d in decimals) else 0.0
        ) if expected_pcts else None,
    }


def analyzable_variant(sql: str) -> str:
    """Versión del SQL del banco con la norma de analizables (2026-09-24), para bancos sin `sql_verdad`.

    Agrega `AND usefulforanalysis IS TRUE` justo después del filtro de tenant del primer WHERE. Si el
    SQL ya lo tiene, o no tiene un filtro de tenant reconocible, lo devuelve igual. El llamador debe
    probar la variante y volver al SQL original si la vista no expone la columna.
    """
    if "usefulforanalysis" in sql:
        return sql
    return re.sub(
        r"(WHERE\s+(?:\w+\.)?seller(?:_id|_name)?\s*=\s*'[^']*')",
        r"\1 AND usefulforanalysis IS TRUE",
        sql,
        count=1,
        flags=re.I,
    )


def summarize(results: list[dict]) -> dict[str, dict]:
    """Por pregunta: corridas, fallbacks y cobertura media (sólo de corridas que no cayeron al fallback)."""
    summary: dict[str, dict] = {}
    for item in results:
        entry = summary.setdefault(item["id"], {"runs": 0, "fallbacks": 0, "cov": []})
        entry["runs"] += 1
        entry["fallbacks"] += int(item["fallback"])
        if not item["fallback"] and item["cov_int"] is not None:
            entry["cov"].append(item["cov_int"])
    for entry in summary.values():
        entry["cov_mean"] = sum(entry["cov"]) / len(entry["cov"]) if entry["cov"] else None
    return summary


def resolve_data_map(client_folder: str, selector: str | None, current: Path) -> Path:
    if not selector:
        return current
    path = Path(selector)
    if path.exists():
        return path
    matches = sorted((CLIENTS_ROOT / client_folder / "data_map").glob(f"*{selector}.yaml"))
    if not matches:
        raise SystemExit(f"No encontré un Data Map que termine en '{selector}' para {client_folder}")
    return matches[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", required=True)
    parser.add_argument("--data-map", help="Sufijo de versión (V3) o ruta. Default: la vigente.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--questions", help="ids separados por coma (default: todas)")
    args = parser.parse_args()

    import vi_agent
    from data_map_auto_update import _build_system_instruction
    from utils.postgres import get_postgres_connection

    vi_agent.configure_client(args.client)
    vi_agent.load_environment()
    data_map = resolve_data_map(args.client, args.data_map, vi_agent.CLIENT_CONFIG.data_map_path)
    instruction = _build_system_instruction(vi_agent.CLIENT_CONFIG, data_map.read_text(encoding="utf-8"))
    bank_path = CLIENTS_ROOT / args.client / "preguntas" / "preguntas_evaluacion.yaml"
    questions = yaml.safe_load(bank_path.read_text(encoding="utf-8"))["preguntas"]
    if args.questions:
        wanted = set(args.questions.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    out_dir = REPO_ROOT / ".runtime" / "golden_eval" / args.client
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{data_map.stem.split()[-1]}.json"
    print(f"Data Map: {data_map.name} | preguntas: {len(questions)} | corridas: {args.runs}")

    results: list[dict] = []
    broken: list[str] = []
    for question in questions:
        candidates = [question["sql_verdad"]] if question.get("sql_verdad") else [
            analyzable_variant(question["sql"]), question["sql"]
        ]
        rows, last_error = None, None
        for sql in dict.fromkeys(candidates):  # la variante filtrada primero; el original si la vista no expone la columna
            try:
                with get_postgres_connection() as conn, conn.cursor() as cur:
                    cur.execute(sql)
                    rows = cur.fetchall()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if rows is None:  # se reporta, no se salta en silencio
            broken.append(question["id"])
            print(f"  SQL_ROTO {question['id']}: {str(last_error).splitlines()[0][:100]}", flush=True)
            continue
        expected_ints, expected_pcts = expected_numbers(rows)
        for run in range(args.runs):
            try:
                answer = vi_agent.run_tool_loop(
                    vi_agent.build_chat(system_instruction=instruction),
                    question["pregunta"], max_tool_calls=8, debug=False,
                )
            except Exception as exc:  # noqa: BLE001
                answer = f"ERROR {exc!r}"[:300]
            score = score_answer(answer, expected_ints, expected_pcts)
            results.append({"id": question["id"], "run": run, **score, "answer": answer})
            print(f"  {question['id']} #{run} {'FALLBACK' if score['fallback'] else 'ok'} cobertura={score['cov_int']}", flush=True)
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'pregunta':40}{'fallbacks':>10}{'cobertura':>11}")
    for qid, entry in summarize(results).items():
        cov = f"{entry['cov_mean']:.2f}" if entry["cov_mean"] is not None else "-"
        print(f"{qid[:40]:40}{entry['fallbacks']}/{entry['runs']:<8}{cov:>11}")
    total_fb = sum(int(r["fallback"]) for r in results)
    print(f"\nTOTAL fallbacks: {total_fb}/{len(results)} | SQL rotos en el banco: {broken or 'ninguno'}")
    print(f"Detalle: {out_path}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
