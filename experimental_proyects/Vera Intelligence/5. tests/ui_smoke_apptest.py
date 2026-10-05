"""Prueba de humo AUTOMÁTICA de la interfaz Streamlit, sin navegador, sin red y sin costo (2026-10-02).

Usa el simulador oficial `streamlit.testing.v1.AppTest`: ejecuta `streamlit_app.py` de verdad (portón de contraseña,
selector de cliente, chat, render de respuesta) con el AGENTE SIMULADO (`vi_agent.run_tool_loop`/`build_chat`), así que
verifica la interfaz y su integración con el loop, no la calidad de las respuestas (eso lo miden los bancos vivos).

Corre en su PROPIO proceso a propósito: `test_streamlit_app.py` reemplaza `streamlit` por un doble en `sys.modules` y
convivir en el mismo proceso rompería esta prueba. Por eso no empieza con `test_` (pytest no la recolecta).

    python "experimental_proyects/Vera Intelligence/5. tests/ui_smoke_apptest.py"      # exit 0 = todo bien
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "4. scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[2]))

TEST_PASSWORD = "contraseña-de-prueba-ui"
os.environ["VI_DEMO_PASSWORD"] = TEST_PASSWORD  # valor de prueba propio; no es el secreto de la demo
os.environ.setdefault("VERA_AI_API_KEY", "clave-falsa-no-se-usa")
os.environ.setdefault("PGPASSWORD", "clave-falsa-no-se-usa")

from streamlit.testing.v1 import AppTest  # noqa: E402

import vi_agent  # noqa: E402

APP = str(SCRIPTS / "streamlit_app.py")
RESPUESTA = "Respuesta simulada del agente para la prueba de humo."
REPREGUNTA = "¿A qué asesor te referís? (indicame su nombre o si preferís ver el desempeño general del equipo)"

_resultados: list[tuple[str, bool, str]] = []


def paso(nombre: str, ok: bool, detalle: str = "") -> None:
    _resultados.append((nombre, bool(ok), detalle))
    print(("OK  " if ok else "FALLA"), nombre, ("-> " + detalle) if (detalle and not ok) else "")


def textos(at: AppTest) -> str:
    partes = [m.value for m in at.markdown] + [c.value for c in at.caption] + [e.value for e in at.error]
    return "\n".join(str(p) for p in partes)


def main() -> int:
    fake_chat = MagicMock()
    fake_chat.get_history.return_value = []
    respuestas = iter([RESPUESTA, REPREGUNTA, RESPUESTA, RESPUESTA, RESPUESTA])

    def fake_loop(chat, question, **kwargs):
        fake_loop.preguntas.append(question)
        return next(respuestas)

    fake_loop.preguntas = []

    with patch.object(vi_agent, "build_chat", return_value=fake_chat), \
            patch.object(vi_agent, "run_tool_loop", side_effect=fake_loop), \
            patch.object(vi_agent, "load_environment", return_value=None):
        at = AppTest.from_file(APP, default_timeout=90).run()
        paso("1. arranca sin excepciones y pide contraseña", not at.exception and "Acceso restringido" in textos(at),
             str([e.value for e in at.exception]))

        at.text_input[0].set_value("contraseña-equivocada")
        at.button[0].click().run()
        paso("2. una contraseña incorrecta NO deja pasar", "Contraseña incorrecta" in textos(at) and not at.chat_input)

        at.text_input[0].set_value(TEST_PASSWORD)
        at.button[0].click().run()
        paso("3. la contraseña correcta entra sin excepciones", not at.exception and len(at.chat_input) == 1,
             str([e.value for e in at.exception]))

        opciones = list(at.selectbox[0].options) if at.selectbox else []
        paso("4. el selector de cliente lista los clientes", len(opciones) >= 10, f"{len(opciones)} opciones")

        mf = next((o for o in opciones if "Men" in o and "Fashion" in o), None)
        if mf:
            at.selectbox[0].select(mf).run()
            paso("5. cambiar de cliente no rompe", not at.exception, str([e.value for e in at.exception]))

        if not at.chat_input:
            paso("6-9. hay un cuadro de pregunta para seguir probando", False,
                 "la página no mostró el chat (probable excepción en el paso anterior)")
            print("Corte: sin chat no se pueden probar los pasos siguientes")
            return 1
        at.chat_input[0].set_value("¿Cómo le está yendo al asesor?").run()
        paso("6. una pregunta llega al agente tal cual", fake_loop.preguntas[-1:] == ["¿Cómo le está yendo al asesor?"],
             str(fake_loop.preguntas))
        paso("7. se muestra la respuesta del agente", not at.exception and RESPUESTA in textos(at),
             str([e.value for e in at.exception]))

        at.chat_input[0].set_value("Quiero ver el desempeño general de los asesores").run()
        paso("8. la segunda pregunta (conversación) también responde", not at.exception and len(fake_loop.preguntas) >= 2)

        botones = [b for b in at.button if "Nueva conversación" in (b.label or "")]
        if botones:
            botones[0].click().run()
            paso("9. 'Nueva conversación' limpia sin excepciones", not at.exception, str([e.value for e in at.exception]))

    fallas = [r for r in _resultados if not r[1]]
    print(f"\n{len(_resultados) - len(fallas)}/{len(_resultados)} pasos OK")
    return 1 if fallas else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
