"""Punto de entrada de Vera Intelligence para probar el agente.

Por default levanta la interfaz web (Streamlit, ver "4. scripts/streamlit_app.py") — chat con
estética de producto, selector de cliente en la barra lateral, se abre solo en el navegador. Es
el modo recomendado para cualquiera que clone el repo: no hace falta memorizar el comando de
Streamlit, alcanza con ejecutar este archivo.

`--cli` da el modo anterior, texto plano en la terminal -sin navegador, útil para un smoke test
rápido o un entorno sin GUI-. `--internal-debug` funciona en ambos modos (agregado 2026-09-10): en
`--cli` imprime las trazas en stderr; en la interfaz web las muestra en un expander colapsado debajo
de cada respuesta (nunca visible sin pasar el flag al arrancar, y nunca en la barra de cliente -no
hay forma de habilitarlo desde la UI misma). Ambos modos comparten el mismo motor (vi_agent.py) y el
mismo selector de cliente por nombre comercial (nunca por client_id/carpeta).

`--model` (agregado 2026-09-17, pedido explícito del usuario) elige, sólo para esta sesión de
prueba, qué modelo de Gemini responde -sin tocar el `model:` real de ningún config.yaml (ver
vi_agent.AVAILABLE_MODELS para las opciones habilitadas y por qué). En `--cli` se pregunta de forma
interactiva si se omite; en la interfaz web aparece como un selectbox en la barra lateral junto al
selector de cliente.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vi_agent  # noqa: E402
from client_config import available_clients_with_display_names  # noqa: E402

# En PowerShell/cmd la consola no siempre es UTF-8 por defecto: sin esto,
# imprimir acentos o "¿" puede tirar UnicodeEncodeError y cortar el script.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


SEPARATOR = "─" * 60

GREETING_PROMPT = (
    "Presentate brevemente como Vera Intelligence y armá un menú corto (4-5 puntos) de los tipos de "
    "análisis de negocio en los que podés ayudar a la gerencia de {client_name}, basado ÚNICAMENTE en "
    "las áreas que cubren los campos y criterios reales disponibles para este cliente (nunca una "
    "categoría de negocio genérica que suene relevante pero que no puedas resolver de verdad con lo "
    "que tenés disponible -sin ese respaldo real, no la incluyas). No uses jerga técnica ni menciones "
    "cómo obtenés la información. Cerrá preguntando en qué análisis o indicador de negocio le "
    "gustaría enfocarse hoy."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VI (Vera Intelligence) - modo interactivo para probar preguntas."
    )
    parser.add_argument(
        "--client",
        default=None,
        help=(
            "client_id a atender (carpeta bajo clientes/, ej. mens_fashion_alto, atlas_medio). "
            "Si se omite, se pregunta de forma interactiva al arrancar."
        ),
    )
    parser.add_argument(
        "--internal-debug",
        "--debug",
        dest="internal_debug",
        action="store_true",
        help=(
            "Muestra trazas técnicas internas (tool calls); nunca usar en una interfaz de cliente. "
            "Aplica tanto a --cli (stderr) como a la interfaz web (expander colapsado por respuesta)."
        ),
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Modo texto plano en la terminal, sin navegador (default: interfaz web Streamlit).",
    )
    parser.add_argument(
        "--model",
        default=None,
        choices=vi_agent.AVAILABLE_MODELS,
        help=(
            "Modelo de Gemini a usar en esta sesión de prueba, en vez del que fija el config.yaml "
            f"del cliente ({', '.join(vi_agent.AVAILABLE_MODELS)}). Si se omite, se pregunta de "
            "forma interactiva al arrancar (--cli) o se elige desde la barra lateral (interfaz web)."
        ),
    )
    return parser.parse_args()


def _prompt_for_client() -> str:
    """Menú interactivo de selección de cliente: numerado, por nombre comercial (nunca por
    client_id/carpeta), acepta tanto el número como el nombre escrito."""
    names = available_clients_with_display_names()
    if not names:
        raise RuntimeError("No hay clientes configurados bajo clientes/.")
    if len(names) == 1:
        (only_client_id, only_display_name), = names.items()
        print(f"Único cliente disponible: {only_display_name}", flush=True)
        return only_client_id

    ordered = sorted(names.items(), key=lambda item: item[1].lower())
    by_number = {str(i): client_id for i, (client_id, _) in enumerate(ordered, start=1)}
    by_name = {display_name.lower(): client_id for client_id, display_name in names.items()}

    print(f"{SEPARATOR}\nClientes disponibles:", flush=True)
    for i, (_, display_name) in enumerate(ordered, start=1):
        print(f"  {i:>2}. {display_name}", flush=True)
    print(flush=True)

    while True:
        try:
            entrada = input("Elegí un cliente (número o nombre)> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            raise SystemExit(0) from None
        if not entrada:
            continue
        if entrada in by_number:
            return by_number[entrada]
        if entrada.lower() in by_name:
            return by_name[entrada.lower()]
        print(
            f"No reconozco {entrada!r}. Elegí uno de los números de la lista o escribí el "
            "nombre del cliente tal como aparece.",
            flush=True,
        )


def _prompt_for_model() -> str:
    """Menú interactivo de selección de modelo, mismo patrón numerado que `_prompt_for_client`
    (acepta número o nombre completo). El primero de la lista es el que ya corre en producción."""
    options = vi_agent.AVAILABLE_MODELS
    by_number = {str(i): model for i, model in enumerate(options, start=1)}

    print(f"{SEPARATOR}\nModelos disponibles:", flush=True)
    for i, model in enumerate(options, start=1):
        etiqueta = " (en producción)" if i == 1 else ""
        print(f"  {i:>2}. {model}{etiqueta}", flush=True)
    print(flush=True)

    while True:
        try:
            entrada = input("Elegí un modelo (número o nombre, Enter para el de producción)> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            raise SystemExit(0) from None
        if not entrada:
            return options[0]
        if entrada in by_number:
            return by_number[entrada]
        if entrada in options:
            return entrada
        print(
            f"No reconozco {entrada!r}. Elegí uno de los números de la lista o escribí el "
            "nombre del modelo tal como aparece.",
            flush=True,
        )


EXIT_WORDS = {"salir", "exit", "quit", "chau", "adios", "adiós"}


def launch_streamlit(args: argparse.Namespace) -> None:
    """Lanza "4. scripts/streamlit_app.py" como subproceso -mismo intérprete que corrió este
    script (sys.executable), así que respeta el venv activo sin depender de que "streamlit" esté
    en el PATH del sistema-. Streamlit abre el navegador solo; este proceso queda bloqueado
    corriendo el server hasta que el usuario lo corte con Ctrl+C, igual que cualquier dev server."""
    streamlit_app = SCRIPTS_DIR / "streamlit_app.py"
    print(f"{SEPARATOR}\n  Vera Intelligence — abriendo interfaz web...\n{SEPARATOR}\n", flush=True)
    print("(Ctrl+C para cerrar. Para el modo de terminal en texto plano: --cli)\n", flush=True)
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(streamlit_app),
        "--server.headless=true",  # sin esto, la primera corrida en cualquier máquina nueva se
        # queda esperando un email de onboarding por stdin antes de arrancar el server.
        "--browser.gatherUsageStats=false",
    ]
    extra_args: list[str] = []
    if args.client:
        extra_args += ["--client", args.client]
    if args.model:
        extra_args += ["--model", args.model]
    if args.internal_debug:
        extra_args += ["--internal-debug"]
    if extra_args:
        cmd += ["--", *extra_args]
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        pass
    except FileNotFoundError:
        print(
            "No se encontró Streamlit en este entorno. Instalalo con "
            "`pip install streamlit` (ya está en requirements.txt) o usá --cli.",
            file=sys.stderr,
            flush=True,
        )


def _strip_charts_for_cli(raw_answer: str) -> str:
    """El modo terminal no puede renderizar los bloques ```vera-chart``` ni ```vera-suggestions```
    (ver vi_agent.extract_chart_blocks/extract_suggestion_blocks) como gráfico o chips reales
    -sólo la interfaz web lo hace-, así que acá se descartan y se deja una nota corta en vez de
    mostrar el JSON crudo. El bloque de sugerencias es obligatorio en TODA respuesta desde
    2026-09-11 (antes sólo aparecía en el saludo de streamlit_app.py, nunca en --cli), así que
    limpiarlo acá dejó de ser opcional -sin esto, cada respuesta en terminal terminaría con un
    ```vera-suggestions [...]``` crudo."""
    text, charts = vi_agent.extract_chart_blocks(raw_answer)
    text, _suggestions = vi_agent.extract_suggestion_blocks(text)
    if charts:
        text += "\n\n(Hay uno o más gráficos disponibles para esta respuesta en la interfaz web — corré sin --cli.)"
    return text


def run_cli(args: argparse.Namespace) -> None:
    if not args.client:
        print(f"{SEPARATOR}\n  Vera Intelligence — modo de prueba interactivo (terminal)\n{SEPARATOR}\n", flush=True)

    client_id = args.client or _prompt_for_client()
    model_override = args.model or _prompt_for_model()
    try:
        vi_agent.configure_client(client_id, model_override=model_override)
        vi_agent.load_environment()
        vi_agent.prewarm_sql_connection()
        chat = vi_agent.build_chat()
    except Exception as exc:  # noqa: BLE001
        if args.internal_debug:
            print(f"[internal] No se pudo inicializar VI: {exc}", file=sys.stderr, flush=True)
        print(
            "Vera Intelligence no está disponible en este momento. "
            "Intentá nuevamente en unos minutos.",
            flush=True,
        )
        return

    modo = " (trazas internas activas)" if args.internal_debug else ""
    print(
        f"\n{SEPARATOR}\n  {vi_agent.CLIENT_CONFIG.display_name} — modelo: {vi_agent.MODEL}{modo}\n{SEPARATOR}\n",
        flush=True,
    )

    session_id = uuid.uuid4().hex
    greeting_history = list(chat.get_history(curated=True))
    try:
        raw_greeting = vi_agent.run_tool_loop(
            chat,
            GREETING_PROMPT.format(client_name=vi_agent.CLIENT_CONFIG.display_name)
            + vi_agent.greeting_vector_search_clause(),
            max_tool_calls=20,
            debug=args.internal_debug,
            session_id=session_id,
        )
        print(f"{_strip_charts_for_cli(raw_greeting)}\n", flush=True)
    except Exception as exc:  # noqa: BLE001
        chat = vi_agent.build_chat(history=greeting_history)
        if args.internal_debug:
            print(f"[internal] No se pudo generar la presentación inicial: {exc}", file=sys.stderr, flush=True)

    print(f"{SEPARATOR}\nEscribí tu pregunta, o 'salir' (Enter vacío también funciona) para terminar.\n", flush=True)
    while True:
        try:
            question = input("Pregunta> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            break
        if not question or question.lower() in EXIT_WORDS:
            print("Listo, ¡hasta la próxima!", flush=True)
            break
        if vi_agent.courtesy_response(question) is None:
            print("(pensando...)", flush=True)
        history = list(chat.get_history(curated=True))
        control = vi_agent.AnalysisControl()
        try:
            raw_answer = vi_agent.run_tool_loop(
                chat,
                question,
                max_tool_calls=20,
                debug=args.internal_debug,
                session_id=session_id,
                analysis_control=control,
            )
            answer = _strip_charts_for_cli(raw_answer)
        except KeyboardInterrupt:
            control.cancel()
            print("\nAnálisis cancelado.", flush=True)
            break
        except vi_agent.OperationalUnavailable as exc:
            chat = vi_agent.build_chat(history=history)
            answer = exc.user_message
            vi_agent.record_local_exchange(chat, question, answer)
        except Exception as exc:  # noqa: BLE001
            chat = vi_agent.build_chat(history=history)
            if args.internal_debug:
                print(f"[internal] Error al responder: {exc}", file=sys.stderr, flush=True)
            answer = (
                "No pude completar el análisis en este momento. "
                "Intentá nuevamente en unos minutos."
            )
            vi_agent.record_local_exchange(chat, question, answer)
        print(f"\n{answer}\n{SEPARATOR}\n", flush=True)


def main() -> None:
    args = parse_args()
    if args.cli:
        run_cli(args)
    else:
        launch_streamlit(args)


if __name__ == "__main__":
    main()
