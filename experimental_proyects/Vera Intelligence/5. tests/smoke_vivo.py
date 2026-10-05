"""Chequeo AUTOMÁTICO previo a un deploy (2026-10-02): corre todo lo que se puede verificar sin una persona.

Etapas (cada una puede apagarse con --sin-<etapa>):
  1. tests    : toda la suite offline (pytest).
  2. ui       : interfaz Streamlit con el simulador oficial (ui_smoke_apptest.py), sin red y sin costo.
  3. repregunta: banco vivo del planificador (ajuste + retención, ~40 llamadas baratas a Gemini, sin datos de clientes).
  4. estabilidad: la misma búsqueda 3 veces y una paráfrasis dan lo mismo (caché + consulta canónica).
  5. extremo : preguntas REALES de punta a punta contra el agente (SQL real, ~US$ 0,1-0,2) con aserciones de conducta:
               repregunta si falta una pieza, no inventa un asesor inexistente, no fabrica un NPS que no existe,
               responde un número simple con respaldo (sin caer en "no pude verificar las cifras").

Sale con código 0 sólo si TODO pasa; imprime un resumen. Las aserciones de la etapa 4 son heurísticas (palabras clave):
detectan roturas gruesas, no reemplazan la revisión humana de la calidad.

    python "experimental_proyects/Vera Intelligence/5. tests/smoke_vivo.py" [--cliente mens_fashion_alto] [--sin-extremo]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
ROOT = PROJECT.parents[1]
PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

resultados: list[tuple[str, bool, str]] = []


def registrar(nombre: str, ok: bool, detalle: str = "") -> None:
    resultados.append((nombre, ok, detalle))
    print(("OK    " if ok else "FALLA ") + nombre + (f"  [{detalle}]" if detalle else ""), flush=True)


def correr(cmd: list[str], timeout: int) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def etapa_tests() -> None:
    code, out = correr([PY, "-m", "pytest", str(HERE), "-q", "-p", "no:cacheprovider"], 900)
    resumen = (re.findall(r"\d+ passed[^\n]*|\d+ failed[^\n]*", out) or ["sin resumen"])[-1]
    registrar("1. suite offline", code == 0, resumen)


def etapa_ui() -> None:
    code, out = correr([PY, str(HERE / "ui_smoke_apptest.py")], 900)
    resumen = (re.findall(r"\d+/\d+ pasos OK", out) or ["sin resumen"])[-1]
    registrar("2. interfaz Streamlit (simulador)", code == 0, resumen)


def etapa_repregunta(cliente: str) -> None:
    banco = PROJECT / "3. experimentos" / "planificador" / "banco_repregunta.py"
    for etiqueta, extra in (("ajuste", []), ("retención", ["--holdout"])):
        code, out = correr([PY, str(banco), cliente, *extra], 1500)
        resumen = (re.findall(r"Aciertos[^\n]*", out) or ["sin resumen"])[-1]
        registrar(f"3. banco de repregunta ({etiqueta})", code == 0, resumen)


def etapa_estabilidad(cliente: str) -> None:
    """La misma búsqueda 3 veces y una paráfrasis: mismas conversaciones, orden, notas y patrones (determinismo)."""
    code, out = correr([PY, str(PROJECT / "3. experimentos" / "busqueda_determinista" / "estabilidad.py"), cliente], 1500)
    corridas = [l for l in out.splitlines() if l.startswith("corrida 1 vs")]
    ok_corridas = bool(corridas) and all("=True" in l and "=False" not in l for l in corridas)
    parafrasis = [l for l in out.splitlines() if "mismas conversaciones que la original" in l]
    ok_parafrasis = bool(parafrasis) and all("original=True" in l for l in parafrasis)
    registrar("5a. misma búsqueda 3 veces: idéntica", code == 0 and ok_corridas, f"{len(corridas)} comparaciones")
    registrar("5b. paráfrasis de la consulta: mismo resultado", ok_parafrasis, f"{len(parafrasis)} paráfrasis")


def etapa_extremo(cliente: str) -> None:
    sys.path.insert(0, str(PROJECT / "4. scripts"))
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    import vi_agent as va

    va.configure_client(cliente)
    va.load_environment()

    def preguntar(q: str):
        chat = va.build_chat()
        log: list[dict] = []
        t0 = time.time()
        ans = va.run_tool_loop(chat, q, tool_calls_log=log)
        return ans, [t["name"] for t in log], time.time() - t0

    def minus(s: str) -> str:
        return s.lower()

    # a) falta una pieza clave -> repregunta, sin herramientas
    ans, tools, seg = preguntar("¿Cómo le está yendo al asesor?")
    registrar("4a. repregunta si falta el asesor", not tools and "?" in ans and len(ans) < 400,
              f"herramientas={tools} ({seg:.0f}s)")

    # b) asesor inexistente -> lo dice, no inventa
    ans, tools, seg = preguntar("¿Cómo le está yendo al asesor Casimiro Valderrama Ibáñez?")
    a = minus(ans)
    registrar("4b. asesor inexistente: lo dice y no inventa", any(w in a for w in ("no se encontr", "no encontr", "no se registran", "no hay registros", "no figura", "no existe"))
              and not re.search(r"\d+[.,]?\d*\s*%", ans), f"({seg:.0f}s)")

    # c) métrica inexistente (ticket) -> límite explícito
    ans, tools, seg = preguntar("El ticket promedio de Parque Delta bajó 15 % en septiembre, ¿qué lo explica?")
    a = minus(ans)
    registrar("4c. ticket inexistente: declara que no lo tiene", any(w in a for w in ("no tengo", "no dispongo", "no cuento", "no hay dato", "no es posible confirmar", "no se dispone")),
              f"({seg:.0f}s)")

    # d) número simple -> respaldado, sin caer en el fallback de verificación
    ans, tools, seg = preguntar("¿Cuál es la tasa de cierre?")
    registrar("4d. número simple con SQL y sin 'no pude verificar'",
              "run_readonly_sql" in tools and "%" in ans and "no pude verificar" not in minus(ans),
              f"herramientas={tools} ({seg:.0f}s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cliente", default="mens_fashion_alto")
    for etapa in ("tests", "ui", "repregunta", "estabilidad", "extremo"):
        ap.add_argument(f"--sin-{etapa}", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    if not a.sin_tests:
        etapa_tests()
    if not a.sin_ui:
        etapa_ui()
    if not a.sin_repregunta:
        etapa_repregunta(a.cliente)
    if not a.sin_estabilidad:
        etapa_estabilidad(a.cliente)
    if not a.sin_extremo:
        etapa_extremo(a.cliente)
    fallas = [r for r in resultados if not r[1]]
    print(f"\n{len(resultados) - len(fallas)}/{len(resultados)} chequeos OK en {time.time() - t0:.0f}s"
          + ("" if not fallas else " -- FALLARON: " + "; ".join(n for n, _, _ in fallas)))
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
