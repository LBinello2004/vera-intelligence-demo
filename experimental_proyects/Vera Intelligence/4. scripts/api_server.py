"""API HTTP del agente de Vera Intelligence para conectarlo a un front externo (Lovable, 2026-10-15).

Un proceso atiende UN cliente (por defecto `tigo_alto`, con la búsqueda vectorial APAGADA desde 2026-10-16: solo SQL; se enciende con VI_API_VECTOR_SEARCH=1), igual que la
app de Streamlit. El front NUNCA llama a esta API desde el navegador: lo hace una función del lado servidor (Supabase Edge Function u
otra) que guarda la clave y manda el identificador del usuario. Guía completa: `10. documentos/API_LOVABLE.md`; plan: `PLAN_VERA_INTELLIGENCE_LOVABLE.md`.

Contrato (las rutas /v1 piden `Authorization: Bearer <clave>` o `X-API-Key`, y `X-VI-User: <id del usuario>` si VI_API_REQUIRE_USER=1):
    GET    /health                 sin autenticación: {"status", "client", "vector_search", "warm"}
    GET    /v1/status              estado del servicio (preguntas en curso, sesiones, Data Map vigente, tope del día)
    POST   /v1/ask                 {"question", "session_id"?, "user_context"?: {"store", "employee", "role"}} -> 202 {"job_id", "session_id"}
    GET    /v1/jobs/{job_id}       {"status": queued|running|done|error|cancelled, "progress", "answer", "charts", "suggestions", ...}
    POST   /v1/jobs/{job_id}/cancel
    POST   /v1/feedback            {"job_id", "rating": "up"|"down", "comment"?}
    DELETE /v1/sessions/{session_id}
Una pregunta puede tardar de segundos a varios minutos (las cualitativas con búsqueda): "pedido + consulta de estado" (el front consulta cada ~2 s).

Protecciones: clave obligatoria (la API no arranca sin ella); cada sesión y cada pedido pertenecen a (clave, usuario); tope diario por clave y por
usuario; preguntas en curso por usuario y cola global acotadas (503/429 en vez de acumularse); tope de tiempo por pregunta; cuerpo máximo;
errores internos sin detalles hacia el front; texto de las preguntas fuera de los registros salvo VI_API_LOG_QUESTIONS=1; el usuario se registra
solo como un hash. Si el Data Map vigente cambia (el refresco diario promovió uno nuevo) la API deja de aceptar preguntas nuevas, espera a que
terminen las en curso y se cierra para que el supervisor (systemd Restart=always / Docker --restart always) la levante con el Data Map nuevo.

Variables de entorno (ninguna clave va en el repo):
    VI_API_KEYS              claves aceptadas, separadas por coma, de al menos 24 caracteres (obligatoria)
    VI_API_CLIENT            carpeta del cliente (default tigo_alto)
    VI_API_VECTOR_SEARCH     "1" para encender la búsqueda vectorial del cliente (default "0": apagada, solo SQL)
    VI_API_REQUIRE_USER      "0" para no exigir X-VI-User (default "1": se exige)
    VI_API_MAX_CONCURRENT    preguntas procesándose a la vez en el servicio (default 3)
    VI_API_USER_MAX_CONCURRENT  preguntas en curso (en cola o corriendo) por usuario (default 2)
    VI_API_MAX_QUEUE         pedidos en cola o corriendo en total antes de responder 503 (default 20)
    VI_API_DAILY_LIMIT       preguntas por clave y día UTC, 0 = sin tope (default 200)
    VI_API_USER_DAILY_LIMIT  preguntas por usuario y día UTC, 0 = sin tope (default 50)
    VI_API_MAX_QUESTION_CHARS  largo máximo de la pregunta (default 1000)
    VI_API_JOB_TIMEOUT_S     tope por pregunta (default 420: las cualitativas con búsqueda tardaron hasta ~275 s en la prueba de 50 preguntas)
    VI_API_SESSION_TTL_MIN   una sesión inactiva se olvida tras esto (default 60)
    VI_API_JOB_TTL_MIN       un resultado se conserva esto (default 30)
    VI_API_MAX_SESSIONS_PER_USER  sesiones abiertas por usuario; al superarlo se olvida la más vieja (default 10)
    VI_API_WATCH_INTERVAL_S  cada cuánto revisa si cambió el Data Map (default 300; 0 = no revisa)
    VI_API_DRAIN_TIMEOUT_S   espera máxima a que terminen las preguntas en curso antes de reiniciar (default 600)
    VI_API_WARMUP            "0" para no hacer la pregunta de calentamiento al arrancar (default "1")
    VI_API_DATA_DIR          dónde se guardan el registro de uso y el feedback (default <proyecto>/.runtime/api)
    VI_API_ALERT_WEBHOOK     URL de un webhook de Slack para avisos (arranque, reinicio por Data Map, errores repetidos); vacío = sin avisos
    VI_API_CORS_ORIGINS      orígenes permitidos, separados por coma (vacío = sin CORS; lo normal)
    VI_API_LOG_QUESTIONS     "1" para registrar el texto de las preguntas (default "0")
Además, las del agente (VERA_AI_API_KEY, PGPASSWORD, ver vi_agent.load_environment).

Arranque:  python api_server.py  (o: uvicorn api_server:build_app --factory --host 127.0.0.1 --port 8080), desde la carpeta `4. scripts`.
Limitaciones conocidas: sesiones, resultados y contadores viven en memoria (un reinicio los pierde; usar UN solo proceso/worker).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import signal
import threading
import time
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("vi_api")

GENERIC_ERROR = "No pude completar el análisis en este momento. Intentá nuevamente en unos minutos."
PROGRESS_LABELS = {
    "run_readonly_sql": "Consultando los datos",
    "compute_stats": "Calculando estadísticas",
    "get_business_rules": "Revisando las definiciones de negocio",
    "search_conversations": "Buscando en las conversaciones",
}
FINAL_STATUSES = {"done", "error", "cancelled"}
USER_ID_RE = re.compile(r"^[A-Za-z0-9._@:+-]{1,128}$")
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "api"
WARMUP_QUESTION = "¿Cuántas conversaciones analizables hay en total?"
FEEDBACK_MEMORY_S = 24 * 3600
FAILURE_ALERT_COUNT, FAILURE_ALERT_WINDOW_S = 5, 600


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _flag(name: str, default: str) -> bool:
    return (os.environ.get(name) or default).strip() != "0"


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...]
    client: str = "tigo_alto"
    vector_search: bool = True
    require_user: bool = True
    max_concurrent: int = 3
    user_max_concurrent: int = 2
    max_queue: int = 20
    daily_limit: int = 200
    user_daily_limit: int = 50
    max_question_chars: int = 1000
    max_body_bytes: int = 16384
    job_timeout_s: float = 420
    session_ttl_s: int = 3600
    job_ttl_s: int = 1800
    max_sessions_per_user: int = 10
    watch_interval_s: float = 300
    drain_timeout_s: float = 600
    warmup: bool = True
    data_dir: Path = DEFAULT_DATA_DIR
    alert_webhook: str = ""
    cors_origins: tuple[str, ...] = ()
    log_questions: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        keys = tuple(k.strip() for k in (os.environ.get("VI_API_KEYS") or "").split(",") if k.strip())
        if not keys:
            raise RuntimeError("Falta VI_API_KEYS: sin al menos una clave la API no arranca (no se publica nada sin autenticación).")
        if any(len(k) < 24 for k in keys):
            raise RuntimeError("Cada clave de VI_API_KEYS debe tener al menos 24 caracteres (por ejemplo: python -c \"import secrets; print(secrets.token_urlsafe(32))\").")
        return cls(
            api_keys=keys,
            client=(os.environ.get("VI_API_CLIENT") or "tigo_alto").strip(),
            vector_search=_flag("VI_API_VECTOR_SEARCH", "0"),
            require_user=_flag("VI_API_REQUIRE_USER", "1"),
            max_concurrent=max(1, _env_int("VI_API_MAX_CONCURRENT", 3)),
            user_max_concurrent=max(1, _env_int("VI_API_USER_MAX_CONCURRENT", 2)),
            max_queue=max(1, _env_int("VI_API_MAX_QUEUE", 20)),
            daily_limit=max(0, _env_int("VI_API_DAILY_LIMIT", 200)),
            user_daily_limit=max(0, _env_int("VI_API_USER_DAILY_LIMIT", 50)),
            max_question_chars=max(50, _env_int("VI_API_MAX_QUESTION_CHARS", 1000)),
            job_timeout_s=max(30, _env_int("VI_API_JOB_TIMEOUT_S", 420)),
            session_ttl_s=max(1, _env_int("VI_API_SESSION_TTL_MIN", 60)) * 60,
            job_ttl_s=max(1, _env_int("VI_API_JOB_TTL_MIN", 30)) * 60,
            max_sessions_per_user=max(1, _env_int("VI_API_MAX_SESSIONS_PER_USER", 10)),
            watch_interval_s=max(0, _env_int("VI_API_WATCH_INTERVAL_S", 300)),
            drain_timeout_s=max(10, _env_int("VI_API_DRAIN_TIMEOUT_S", 600)),
            warmup=_flag("VI_API_WARMUP", "1"),
            data_dir=Path(os.environ.get("VI_API_DATA_DIR") or DEFAULT_DATA_DIR),
            alert_webhook=(os.environ.get("VI_API_ALERT_WEBHOOK") or "").strip(),
            cors_origins=tuple(o.strip() for o in (os.environ.get("VI_API_CORS_ORIGINS") or "").split(",") if o.strip()),
            log_questions=(os.environ.get("VI_API_LOG_QUESTIONS") or "0").strip() == "1",
        )


class Agent(Protocol):
    def new_chat(self) -> object: ...

    def ask(self, chat: object, question: str, *, session_id: str, interaction_id: str,
            on_tool_call: Callable[[str], None], control: object) -> tuple[str, list[dict], list[str]]: ...

    def new_control(self) -> object: ...

    def describe(self) -> dict: ...

    def data_map_changed(self) -> bool: ...

    def warmup(self) -> None: ...


class RealAgent:
    """Adaptador sobre `vi_agent` (importa el agente recién al arrancar, no al importar este módulo)."""

    def __init__(self, settings: Settings) -> None:
        import dataclasses

        import vi_agent
        from runtime_control import AnalysisControl, AnalysisCancelled, OperationalUnavailable

        self._va, self._Control = vi_agent, AnalysisControl
        self._Cancelled, self._Unavailable = AnalysisCancelled, OperationalUnavailable
        vi_agent.configure_client(settings.client)
        vi_agent.load_environment()
        if not settings.vector_search and vi_agent.CLIENT_CONFIG.vector_search:
            # VI_API_VECTOR_SEARCH=0: solo SQL. Se apaga la búsqueda ANTES de armar cualquier chat (el prompt y la lista de tools la leen de CLIENT_CONFIG).
            vi_agent.CLIENT_CONFIG = dataclasses.replace(vi_agent.CLIENT_CONFIG, vector_search=None)
            vi_agent._VECTOR_SEARCH_REPOSITORY = None
        self._vector = bool(vi_agent.CLIENT_CONFIG.vector_search)
        self._client = settings.client

    def new_chat(self) -> object:
        return self._va.build_chat()

    def new_control(self) -> object:
        return self._Control()

    def ask(self, chat, question, *, session_id, interaction_id, on_tool_call, control):
        raw = self._va.run_tool_loop(
            chat, question, max_tool_calls=20, debug=False, session_id=session_id, interaction_id=interaction_id,
            on_tool_call=lambda name, _args: on_tool_call(name), analysis_control=control)
        answer, charts = self._va.extract_chart_blocks(raw)
        answer, suggestions = self._va.extract_suggestion_blocks(answer)
        return answer, charts, suggestions

    def describe(self) -> dict:
        return {"client": self._client, "vector_search": self._vector, "data_map": Path(str(self._va.DATA_MAP_PATH)).name}

    def data_map_changed(self) -> bool:
        """¿El Data Map vigente (config.yaml + almacén del refresco diario) ya no es el que cargó este proceso? Fail-open: ante un error, no."""
        try:
            import data_map_store
            from client_config import load_client_config

            configured = load_client_config(self._client).data_map_path
            effective = data_map_store.effective_data_map_path(self._client, configured)
            return str(effective) != str(self._va.DATA_MAP_PATH)
        except Exception:  # noqa: BLE001
            return False

    def warmup(self) -> None:
        self.ask(self.new_chat(), WARMUP_QUESTION, session_id="warmup", interaction_id="api_warmup", on_tool_call=lambda _n: None,
                 control=self.new_control())

    # el servidor traduce estas excepciones a mensajes seguros
    @property
    def cancelled_exc(self):
        return self._Cancelled

    @property
    def unavailable_exc(self):
        return self._Unavailable


Owner = tuple[str, str]      # (identificador de la clave, hash del usuario)


@dataclass
class Session:
    chat: object
    last_used: float
    owner: Owner
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Job:
    job_id: str
    session_id: str
    owner: Owner
    created: float
    status: str = "queued"
    progress: str = "En cola"
    answer: str | None = None
    charts: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    error_code: str | None = None
    message: str | None = None
    started: float | None = None
    finished: float | None = None
    control: object | None = None


CONTEXT_FIELDS = (("store", "tienda"), ("employee", "asesor"), ("role", "rol"))


def _clean_context_value(value, limit: int = 100) -> str | None:
    """Un dato de contexto del usuario (tienda, asesor, rol): solo letras, números, espacios y puntuación simple; sin saltos de línea, comillas
    ni corchetes (no puede escapar de la línea de contexto ni inyectar instrucciones); recortado a `limit` caracteres."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if c.isalnum() or c in " .,-_'/()&")
    cleaned = " ".join(cleaned.split())[:limit].strip()
    return cleaned or None


def build_context_line(raw) -> str:
    """Línea que se antepone a la pregunta con quién pregunta (tienda, asesor, rol), para resolver "mi tienda", "yo" o "mi equipo" sin repreguntar.
    Estos datos los arma la función de Supabase desde datos del usuario que el usuario NO puede editar (`app_metadata`), nunca desde lo que escribe.
    Devuelve "" si no hay nada usable."""
    if not isinstance(raw, dict):
        return ""
    parts = [f"{label} «{value}»" for key, label in CONTEXT_FIELDS if (value := _clean_context_value(raw.get(key)))]
    if not parts:
        return ""
    return ("[Datos de quien pregunta, tomados del sistema y no escritos por esa persona; usalos para resolver «mi tienda», «yo», «mi equipo» y similares: "
            + "; ".join(parts) + ".]")


def key_id(key: str) -> str:
    """Identificador estable y no reversible de una clave (contadores y registros; la clave nunca se registra)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]


class Recorder:
    """Registro local (JSONL) de interacciones y feedback. Nunca en Postgres; el texto de la pregunta solo si se pidió."""

    def __init__(self, directory: Path, log_questions: bool) -> None:
        self.dir, self.log_questions, self._lock = Path(directory), log_questions, threading.Lock()

    def _append(self, name: str, row: dict) -> None:
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with (self.dir / name).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001 -un registro que falla nunca debe romper una respuesta
            log.exception("no pude escribir %s", name)

    def interaction(self, job: Job, question: str, data_map: str | None) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(), "job_id": job.job_id, "session": job.session_id[:8], "key": job.owner[0],
               "user": job.owner[1], "status": job.status, "error_code": job.error_code, "tools": job.tools,
               "seconds": round((job.finished or job.created) - (job.started or job.created), 1), "answer_chars": len(job.answer or ""),
               "charts": len(job.charts), "data_map": data_map}
        if self.log_questions:
            row["question"] = question
        self._append("interactions.jsonl", row)

    def feedback(self, job_id: str, owner: Owner, rating: str, comment: str) -> None:
        self._append("feedback.jsonl", {"ts": datetime.now(timezone.utc).isoformat(), "job_id": job_id, "key": owner[0], "user": owner[1],
                                        "rating": rating, "comment": comment})


class Alerter:
    """Avisos opcionales a un webhook de Slack, con enfriamiento por tipo (no repite el mismo aviso hasta pasado `cooldown_s`)."""

    def __init__(self, webhook: str, *, clock: Callable[[], float] = time.time, send: Callable[[str], None] | None = None,
                 cooldown_s: float = 1800) -> None:
        self.webhook, self.clock, self._send, self.cooldown_s = webhook, clock, send, cooldown_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def alert(self, kind: str, text: str) -> bool:
        if not self.webhook and self._send is None:
            return False
        with self._lock:
            now = self.clock()
            if now - self._last.get(kind, -1e18) < self.cooldown_s:
                return False
            self._last[kind] = now
        if self._send is not None:
            self._send(text)
        else:
            threading.Thread(target=self._deliver, args=(text,), daemon=True).start()
        return True

    def _deliver(self, text: str) -> None:
        try:
            req = urllib.request.Request(self.webhook, data=json.dumps({"text": text}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:  # noqa: BLE001 -un aviso que falla no debe afectar al servicio
            log.warning("no pude enviar el aviso al webhook")


class ApiState:
    def __init__(self, settings: Settings, agent: Agent, *, clock: Callable[[], float] = time.time, alerter: Alerter | None = None,
                 recorder: Recorder | None = None, restart: Callable[[], None] | None = None) -> None:
        self.settings, self.agent, self.clock = settings, agent, clock
        self.alerter = alerter or Alerter(settings.alert_webhook, clock=clock)
        self.recorder = recorder or Recorder(settings.data_dir, settings.log_questions)
        self.restart = restart or (lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.sessions: dict[str, Session] = {}
        self.jobs: dict[str, Job] = {}
        self.questions: dict[str, str] = {}                      # job_id -> texto (solo hasta registrar; no se expone)
        self.finished_owner: dict[str, tuple[Owner, float]] = {}  # para aceptar feedback después de vencer el resultado
        self.usage_key: dict[tuple[str, str], int] = {}
        self.usage_user: dict[tuple[Owner, str], int] = {}
        self.failures: deque[float] = deque()
        self.draining, self.drain_started, self.warm = False, 0.0, False
        self.started_at = clock()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent, thread_name_prefix="vi-api")
        self._stop = threading.Event()

    # ---- autenticación e identidad
    def authenticate(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        presented = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        if not presented:
            return None
        matched = None
        for known in self.settings.api_keys:           # sin cortocircuito: tiempo parejo sin importar cuál coincide
            if hmac.compare_digest(presented.encode(), known.encode()):
                matched = known
        return key_id(matched) if matched else None

    def owner_for(self, request: Request, kid: str) -> Owner | None:
        raw = request.headers.get("x-vi-user", "").strip()
        if not raw:
            return None if self.settings.require_user else (kid, "-")
        if not USER_ID_RE.match(raw):
            return None
        return (kid, user_hash(raw))

    # ---- topes
    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def take_quota(self, owner: Owner) -> str | None:
        """Descuenta una pregunta del día de la clave y del usuario. Devuelve None si pudo, o "key"/"user" si se agotó ese tope."""
        today, kid = self._today(), owner[0]
        with self.lock:
            for d in (self.usage_key, self.usage_user):
                for k in [k for k in d if k[1] != today]:
                    del d[k]
            if self.settings.daily_limit and self.usage_key.get((kid, today), 0) >= self.settings.daily_limit:
                return "key"
            if self.settings.user_daily_limit and self.usage_user.get((owner, today), 0) >= self.settings.user_daily_limit:
                return "user"
            self.usage_key[(kid, today)] = self.usage_key.get((kid, today), 0) + 1
            self.usage_user[(owner, today)] = self.usage_user.get((owner, today), 0) + 1
            return None

    def refund_quota(self, owner: Owner) -> None:
        today = self._today()
        with self.lock:
            if self.usage_key.get((owner[0], today), 0) > 0:
                self.usage_key[(owner[0], today)] -= 1
            if self.usage_user.get((owner, today), 0) > 0:
                self.usage_user[(owner, today)] -= 1

    def active_jobs(self, owner: Owner | None = None) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j.status not in FINAL_STATUSES and (owner is None or j.owner == owner))

    # ---- limpieza
    def purge(self) -> None:
        now = self.clock()
        with self.lock:
            for sid in [s for s, v in self.sessions.items() if now - v.last_used > self.settings.session_ttl_s]:
                del self.sessions[sid]
            for jid in [j for j, v in self.jobs.items() if v.status in FINAL_STATUSES and now - (v.finished or v.created) > self.settings.job_ttl_s]:
                del self.jobs[jid]
            for jid in [j for j, (_o, ts) in self.finished_owner.items() if now - ts > FEEDBACK_MEMORY_S]:
                del self.finished_owner[jid]

    # ---- sesiones y trabajos
    def find_session(self, session_id: str, owner: Owner) -> Session | None:
        with self.lock:
            s = self.sessions.get(session_id)
            if s is None or s.owner != owner:
                return None
            s.last_used = self.clock()
            return s

    def create_session(self, owner: Owner) -> tuple[str, Session]:
        with self.lock:
            mine = sorted(((v.last_used, k) for k, v in self.sessions.items() if v.owner == owner))
            while len(mine) >= self.settings.max_sessions_per_user:   # se olvida la más vieja del usuario
                del self.sessions[mine.pop(0)[1]]
            sid = uuid.uuid4().hex
            s = Session(chat=self.agent.new_chat(), last_used=self.clock(), owner=owner)
            self.sessions[sid] = s
            return sid, s

    def submit(self, question: str, sid: str, session: Session, owner: Owner, context: str = "") -> Job:
        job = Job(job_id=uuid.uuid4().hex, session_id=sid, owner=owner, created=self.clock())
        with self.lock:
            self.jobs[job.job_id] = job
            self.questions[job.job_id] = question
        self.pool.submit(self._run, job, session, question, context)
        return job

    def _run(self, job: Job, session: Session, question: str, context: str = "") -> None:
        with session.lock:                              # una pregunta a la vez por sesión (el historial es de la sesión)
            if job.status == "cancelled":
                self._finish(job)
                return
            job.status, job.started, job.progress = "running", self.clock(), "Analizando la pregunta"
            control = self.agent.new_control()
            job.control = control
            timer = threading.Timer(self.settings.job_timeout_s, lambda: self._timeout(job))
            timer.daemon = True
            timer.start()
            try:
                answer, charts, suggestions = self.agent.ask(
                    session.chat, (context + "\n" + question) if context else question, session_id=job.session_id, interaction_id="api_" + job.job_id[:16],
                    on_tool_call=lambda name: self._progress(job, name), control=control)
                if job.status == "running":
                    job.answer, job.charts, job.suggestions, job.status = answer, charts, suggestions, "done"
            except Exception as exc:  # noqa: BLE001
                self._fail(job, exc)
            finally:
                timer.cancel()
                self._finish(job)

    def _finish(self, job: Job) -> None:
        job.finished = job.finished or self.clock()
        if job.status == "done":
            job.progress = "Listo"
        with self.lock:
            question = self.questions.pop(job.job_id, "")
            self.finished_owner[job.job_id] = (job.owner, self.clock())
        try:
            data_map = self.agent.describe().get("data_map")
        except Exception:  # noqa: BLE001
            data_map = None
        self.recorder.interaction(job, question, data_map)
        log.info("job=%s session=%s key=%s status=%s seconds=%.1f tools=%s", job.job_id[:8], job.session_id[:8], job.owner[0],
                 job.status, (job.finished or 0) - (job.started or job.created), ",".join(job.tools))

    def _progress(self, job: Job, name: str) -> None:
        job.tools.append(name)
        job.progress = PROGRESS_LABELS.get(name, "Procesando")

    def _note_failure(self, code: str) -> None:
        now = self.clock()
        with self.lock:
            self.failures.append(now)
            while self.failures and now - self.failures[0] > FAILURE_ALERT_WINDOW_S:
                self.failures.popleft()
            count = len(self.failures)
        if count >= FAILURE_ALERT_COUNT:
            self.alerter.alert("failures", f":warning: API de Vera Intelligence: {count} preguntas fallaron en los últimos {FAILURE_ALERT_WINDOW_S // 60} min "
                                           f"(último motivo: {code}). Revisar el servicio, Gemini y la base.")

    def _timeout(self, job: Job) -> None:
        if job.status == "running":
            self.refund_quota(job.owner)                  # antes de cambiar el estado: quien vea "error" ya encuentra el cupo devuelto
            job.status, job.error_code, job.message = "error", "timeout", "La consulta tardó demasiado. Probá con una pregunta más acotada."
            self._note_failure("timeout")
            if job.control is not None:
                job.control.cancel()

    def _fail(self, job: Job, exc: Exception) -> None:
        if job.status != "running":                      # ya cancelado o vencido: no se pisa ese estado
            return
        cancelled = getattr(self.agent, "cancelled_exc", None)
        unavailable = getattr(self.agent, "unavailable_exc", None)
        if unavailable is not None and isinstance(exc, unavailable):
            self.refund_quota(job.owner)
            job.status, job.error_code, job.message = "error", "unavailable", getattr(exc, "user_message", GENERIC_ERROR)
            self._note_failure("unavailable")
        elif cancelled is not None and isinstance(exc, cancelled):
            job.status, job.message = "cancelled", "Análisis cancelado."
        else:
            log.exception("job=%s falló", job.job_id[:8])   # el detalle queda en el servidor, nunca viaja al front
            job.status, job.error_code, job.message = "error", "internal", GENERIC_ERROR
            self._note_failure("internal")

    def cancel(self, job: Job) -> None:
        if job.status in FINAL_STATUSES:
            return
        was_queued = job.status == "queued"
        job.status, job.message, job.finished = "cancelled", "Análisis cancelado.", self.clock()
        if job.control is not None:
            job.control.cancel()
        if was_queued:
            self.refund_quota(job.owner)

    # ---- Data Map vigente y reinicio ordenado
    def check_data_map(self) -> bool:
        """Si el Data Map vigente cambió, deja de aceptar preguntas nuevas (503) y queda pendiente el reinicio. True si está en cierre."""
        if not self.draining:
            try:
                changed = self.agent.data_map_changed()
            except Exception:  # noqa: BLE001
                changed = False
            if changed:
                self.draining, self.drain_started = True, self.clock()
                log.warning("el Data Map vigente cambió: no acepto preguntas nuevas y me reinicio al terminar las que están en curso")
                self.alerter.alert("restart", ":arrows_counterclockwise: API de Vera Intelligence: se promovió un Data Map nuevo; "
                                              "la API se reinicia al terminar las preguntas en curso.")
        return self.draining

    def maybe_restart(self) -> bool:
        if not self.draining:
            return False
        if self.active_jobs() == 0 or self.clock() - self.drain_started >= self.settings.drain_timeout_s:
            log.warning("reiniciando para tomar el Data Map nuevo")
            self.restart()
            return True
        return False

    def start_background(self) -> None:
        if self.settings.watch_interval_s > 0:
            threading.Thread(target=self._watch_loop, name="vi-api-watch", daemon=True).start()
        if self.settings.warmup:
            threading.Thread(target=self._warm, name="vi-api-warmup", daemon=True).start()
        else:
            self.warm = True
        info = self.agent.describe()
        self.alerter.alert("start", f":white_check_mark: API de Vera Intelligence iniciada (cliente {info.get('client')}, "
                                    f"búsqueda vectorial: {info.get('vector_search')}).")

    def _warm(self) -> None:
        try:
            self.agent.warmup()
        except Exception:  # noqa: BLE001 -el calentamiento es una cortesía: si falla, la API sirve igual
            log.warning("la pregunta de calentamiento falló")
        self.warm = True

    def _watch_loop(self) -> None:
        last_check = 0.0
        while not self._stop.is_set():
            now = self.clock()
            if not self.draining and now - last_check >= self.settings.watch_interval_s:
                last_check = now
                self.check_data_map()
            if self.draining and self.maybe_restart():
                return
            self._stop.wait(2 if self.draining else min(5, self.settings.watch_interval_s))

    def stop(self) -> None:
        self._stop.set()


def _json(data: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **(headers or {})})


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return _json({"error": code, "message": message}, status, headers)


def _public_job(job: Job, now: float) -> dict:
    out = {"job_id": job.job_id, "session_id": job.session_id, "status": job.status, "progress": job.progress,
           "seconds": round((job.finished or now) - (job.started or job.created), 1)}
    if job.status == "done":
        out.update(answer=job.answer, charts=job.charts, suggestions=job.suggestions, tools=job.tools)
    elif job.status in {"error", "cancelled"}:
        out.update(error_code=job.error_code or job.status, message=job.message)
    return out


def create_app(settings: Settings, agent: Agent, **state_kwargs) -> Starlette:
    state = ApiState(settings, agent, **state_kwargs)
    UNAUTH = (401, "unauthorized", "Clave inválida o ausente.")
    NOUSER = (400, "user_required", "Falta un identificador de usuario válido en X-VI-User.")

    def identify(request: Request):
        """-> (owner, None) o (None, respuesta de error)."""
        kid = state.authenticate(request)
        if kid is None:
            return None, _error(*UNAUTH)
        owner = state.owner_for(request, kid)
        if owner is None:
            return None, _error(*NOUSER)
        return owner, None

    async def read_json(request: Request):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            return None, _error(413, "payload_too_large", "El pedido es demasiado grande.")
        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            return None, _error(413, "payload_too_large", "El pedido es demasiado grande.")
        try:
            return json.loads(raw.decode("utf-8")), None
        except Exception:  # noqa: BLE001
            return None, _error(400, "bad_request", "El cuerpo debe ser JSON en UTF-8.")

    async def health(request: Request):
        return _json({"status": "ok", **{k: v for k, v in agent.describe().items() if k in ("client", "vector_search")}, "warm": state.warm})

    async def status(request: Request):
        owner, err = identify(request)
        if err:
            return err
        with state.lock:
            running = sum(1 for j in state.jobs.values() if j.status == "running")
            queued = sum(1 for j in state.jobs.values() if j.status == "queued")
            used_key = state.usage_key.get((owner[0], state._today()), 0)
            used_user = state.usage_user.get((owner, state._today()), 0)
            sessions = len(state.sessions)
        return _json({"running": running, "queued": queued, "sessions": sessions, "draining": state.draining, "warm": state.warm,
                      "uptime_s": round(state.clock() - state.started_at), "data_map": agent.describe().get("data_map"),
                      "today": {"key_used": used_key, "key_limit": settings.daily_limit, "user_used": used_user, "user_limit": settings.user_daily_limit}})

    async def ask(request: Request):
        state.purge()
        owner, err = identify(request)
        if err:
            return err
        body, err = await read_json(request)
        if err:
            return err
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or not question.strip():
            return _error(400, "bad_request", "Falta \"question\" (texto no vacío).")
        question = "".join(c for c in question.strip() if c in "\n\t" or ord(c) >= 32)   # sin caracteres de control
        if len(question) > settings.max_question_chars:
            return _error(400, "question_too_long", f"La pregunta supera {settings.max_question_chars} caracteres.")
        session_id = body.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            return _error(400, "bad_request", "\"session_id\" debe ser texto.")
        raw_context = body.get("user_context")
        if raw_context is not None and not isinstance(raw_context, dict):
            return _error(400, "bad_request", "\"user_context\" debe ser un objeto con store, employee y role (texto).")
        context_line = build_context_line(raw_context)
        if state.draining:
            return _error(503, "restarting", "El servicio se está actualizando; reintentá en unos segundos.", {"Retry-After": "20"})
        if state.active_jobs() >= settings.max_queue:
            return _error(503, "busy", "Hay muchas consultas en curso; reintentá en un momento.", {"Retry-After": "30"})
        if state.active_jobs(owner) >= settings.user_max_concurrent:
            return _error(429, "too_many_running", "Ya tenés consultas en curso; esperá a que terminen.")
        session = None
        if session_id:
            session = state.find_session(session_id, owner)
            if session is None:
                return _error(404, "session_not_found", "La sesión no existe o venció; empezá una nueva sin session_id.")
        denied = state.take_quota(owner)
        if denied:
            return _error(429, "daily_limit" if denied == "key" else "user_daily_limit", "Se alcanzó el tope diario de preguntas.")
        if session is None:
            session_id, session = state.create_session(owner)
        job = state.submit(question, session_id, session, owner, context_line)
        log.info("ask job=%s key=%s user=%s%s", job.job_id[:8], owner[0], owner[1], f" q={question!r}" if settings.log_questions else "")
        return _json({"job_id": job.job_id, "session_id": session_id, "status": job.status}, 202)

    def _owned_job(request: Request, owner: Owner) -> Job | None:
        job = state.jobs.get(request.path_params["job_id"])
        return job if job is not None and job.owner == owner else None

    async def job_status(request: Request):
        state.purge()
        owner, err = identify(request)
        if err:
            return err
        job = _owned_job(request, owner)
        if job is None:
            return _error(404, "job_not_found", "No existe ese pedido (o ya venció).")
        return _json(_public_job(job, state.clock()))

    async def job_cancel(request: Request):
        owner, err = identify(request)
        if err:
            return err
        job = _owned_job(request, owner)
        if job is None:
            return _error(404, "job_not_found", "No existe ese pedido (o ya venció).")
        state.cancel(job)
        return _json(_public_job(job, state.clock()))

    async def feedback(request: Request):
        state.purge()
        owner, err = identify(request)
        if err:
            return err
        body, err = await read_json(request)
        if err:
            return err
        job_id = body.get("job_id") if isinstance(body, dict) else None
        rating = body.get("rating") if isinstance(body, dict) else None
        comment = body.get("comment", "") if isinstance(body, dict) else ""
        if rating not in ("up", "down") or not isinstance(job_id, str) or not isinstance(comment, str):
            return _error(400, "bad_request", "Hace falta \"job_id\", \"rating\" (\"up\" o \"down\") y, opcional, \"comment\" (texto).")
        known = state.finished_owner.get(job_id)
        job = state.jobs.get(job_id)
        if not ((known and known[0] == owner) or (job is not None and job.owner == owner)):
            return _error(404, "job_not_found", "No existe ese pedido (o ya venció).")
        state.recorder.feedback(job_id, owner, rating, comment.strip()[:1000])
        return _json({"recorded": True})

    async def delete_session(request: Request):
        owner, err = identify(request)
        if err:
            return err
        sid = request.path_params["session_id"]
        with state.lock:
            s = state.sessions.get(sid)
            if s is None or s.owner != owner:
                return _error(404, "session_not_found", "La sesión no existe o venció.")
            del state.sessions[sid]
        return _json({"deleted": True})

    middleware = []
    if settings.cors_origins:
        middleware.append(Middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST", "DELETE"],
                                     allow_headers=["authorization", "content-type", "x-api-key", "x-vi-user"]))
    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/v1/status", status, methods=["GET"]),
        Route("/v1/ask", ask, methods=["POST"]),
        Route("/v1/jobs/{job_id}", job_status, methods=["GET"]),
        Route("/v1/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
        Route("/v1/feedback", feedback, methods=["POST"]),
        Route("/v1/sessions/{session_id}", delete_session, methods=["DELETE"]),
    ], middleware=middleware)
    app.state.api = state
    return app


def build_app() -> Starlette:
    """Fábrica para uvicorn (`--factory`): lee el entorno, prepara el agente (cliente fijo), arranca la vigilancia y el calentamiento."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    agent = RealAgent(settings)
    app = create_app(settings, agent)
    app.state.api.start_background()
    log.info("API lista: cliente=%s vector_search=%s concurrentes=%s tope_clave=%s tope_usuario=%s", settings.client,
             agent.describe()["vector_search"], settings.max_concurrent, settings.daily_limit, settings.user_daily_limit)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(), host=os.environ.get("VI_API_HOST", "127.0.0.1"), port=_env_int("VI_API_PORT", 8080), workers=1)
