"""API de prueba para los ejemplos de Lovable: la API real (api_server.py) con el agente FALSO de los tests (sin Gemini ni base de datos).
Escucha en 127.0.0.1:8597 con una clave de prueba (no es un secreto). Preguntas especiales del agente falso: "boom" (error interno),
"unavail" (sin acceso a datos), "hang" (no termina hasta que se cancela), cualquier otra cosa responde con un gráfico y sugerencias.
Uso:  python api_falsa.py        (desde esta carpeta; el entorno virtual del repo)"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]                                   # .../Vera Intelligence
sys.path.insert(0, str(PROJECT / "4. scripts"))
sys.path.insert(0, str(PROJECT / "5. tests"))

import api_server  # noqa: E402
import uvicorn  # noqa: E402
from test_api_server import FakeAgent  # noqa: E402

KEY = "k" * 32
DATA = Path(tempfile.gettempdir()) / "vi_api_fake_data"

settings = api_server.Settings(
    api_keys=(KEY,), data_dir=DATA, user_daily_limit=4, daily_limit=0, user_max_concurrent=2, max_concurrent=3,
    watch_interval_s=0, warmup=False, job_timeout_s=60, log_questions=False)
app = api_server.create_app(settings, FakeAgent())
print(f"API de prueba en http://127.0.0.1:8597 | registros en {DATA}", flush=True)
uvicorn.run(app, host="127.0.0.1", port=8597, log_level="warning")
