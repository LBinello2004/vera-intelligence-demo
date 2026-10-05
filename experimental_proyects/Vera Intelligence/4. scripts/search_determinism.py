"""Determinismo de la búsqueda semántica (2026-10-02).

Medición previa (misma búsqueda 3 veces, sin el modelo principal): el vector de la consulta es idéntico y el recuperador
SQL es determinista, pero el juez de relevancia, las notas por conversación y los patrones los escribe un modelo
(temperature=0 sin `seed`) y variaban en cada corrida: 0 de 4 notas idénticas, patrones presentes en 1 corrida de 3.

Qué hace este módulo (todo fail-open: ante cualquier error la búsqueda sigue como antes, sin caché):

  1. CACHÉ PERSISTENTE (sqlite, `.runtime/search_cache.sqlite`, fuera de git): el veredicto + notas de CADA conversación y
     los patrones de cada conjunto se guardan con una clave que incluye la versión del prompt, el modelo, la consulta
     canónica, el contexto del checklist y el HASH del texto analizado. La misma entrada devuelve siempre lo mismo, sin
     llamar al modelo; cambiar el prompt, el modelo o el texto de la conversación invalida solo.
  2. SEMILLA estable: cuando igual hay que llamar al modelo, `seed` se deriva del contenido del pedido (mismo pedido =
     misma semilla) para reducir la variación; no la garantiza, por eso existe la caché.
  3. CONSULTA CANÓNICA: el modelo principal redacta la consulta distinta cada vez ("clientes que se van sin comprar" /
     "personas que no concretan la compra"), y eso cambia todo lo que sigue. Si una consulta nueva es casi idéntica
     (coseno >= 0,92) a una ya vista con LOS MISMOS filtros, se usa la PRIMERA registrada (texto y vector).

Límites honestos: sqlite local = vive mientras viva el servidor (en Streamlit Cloud se pierde al reiniciar; una caché
durable requiere una base escribible, y Postgres de este proyecto es de sólo lectura). Estable no significa verdadero: la
nota guardada es siempre la misma pero sigue siendo el juicio de un modelo.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Calibrado con 6 pares de paráfrasis (coseno mínimo 0,925) y 6 pares de consultas relacionadas pero DISTINTAS (máximo 0,868),
# todas de Mens Fashion: 0,92 unifica todas las paráfrasis y deja 0,05 de margen sobre la distinta más parecida. Muestra
# chica y de un solo dominio: ver `3. experimentos/busqueda_determinista/calibrar_umbral.py` para recalibrar con otro cliente.
SNAP_THRESHOLD = 0.92
_DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".runtime" / "search_cache.sqlite"
_MAX_CANONICAL_PER_SIGNATURE = 400


def enabled() -> bool:
    return os.getenv("VI_SEARCH_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _path() -> Path:
    override = os.getenv("VI_SEARCH_CACHE_PATH")
    return Path(override) if override else _DEFAULT_PATH


def stable_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(json.dumps(part, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def seed_for(*parts: object) -> int:
    """Semilla de 31 bits derivada del contenido: mismo pedido, misma semilla."""
    return int(stable_hash(*parts)[:8], 16) % (2**31 - 1)


_init_lock = threading.Lock()
_initialized: set[str] = set()


def _connect() -> sqlite3.Connection:
    """Conexión nueva por operación (sqlite no comparte conexiones entre threads). El modo WAL y las tablas se crean UNA sola
    vez por proceso y archivo: con el juez corriendo en paralelo, varias conexiones intentando `PRAGMA journal_mode=WAL` a la
    vez daban "database is locked" y la caché quedaba silenciosamente desactivada."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        key = str(path)
        if key not in _initialized:
            with _init_lock:
                if key not in _initialized:
                    for attempt in range(5):  # otro PROCESO puede estar inicializando el mismo archivo
                        try:
                            conn.execute("PRAGMA journal_mode=WAL")
                            conn.execute("CREATE TABLE IF NOT EXISTS kv (kind TEXT NOT NULL, key TEXT NOT NULL, "
                                         "value TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (kind, key))")
                            conn.execute("CREATE TABLE IF NOT EXISTS canon (sig TEXT NOT NULL, text TEXT NOT NULL, "
                                         "vec TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (sig, text))")
                            conn.commit()
                            _initialized.add(key)
                            break
                        except sqlite3.OperationalError:
                            if attempt == 4:
                                raise
                            time.sleep(0.2 * (attempt + 1))
        return conn
    except Exception:
        conn.close()
        raise


def cache_get(kind: str, key: str):
    """Valor guardado (ya deserializado) o None. Nunca lanza."""
    if not enabled():
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT value FROM kv WHERE kind=? AND key=?", (kind, key)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001 -fail-open
        logger.warning("search_determinism.cache_get falló; se sigue sin caché.", exc_info=True)
        return None


def cache_put(kind: str, key: str, value) -> None:
    """Guarda `value` (JSON-serializable). La primera escritura gana (determinismo): no pisa una existente."""
    if not enabled():
        return
    try:
        conn = _connect()
        try:
            conn.execute("INSERT OR IGNORE INTO kv (kind, key, value, created) VALUES (?,?,?,?)",
                         (kind, key, json.dumps(value, ensure_ascii=False), time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -fail-open
        logger.warning("search_determinism.cache_put falló; el resultado no se guardó.", exc_info=True)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def canonical_query(text: str, vector: list[float], signature: str, *, threshold: float = SNAP_THRESHOLD):
    """Devuelve `(texto, vector, ajustada)`. Si ya hay una consulta registrada con la MISMA `signature` (filtros) cuyo
    coseno con `vector` es >= `threshold`, devuelve la PRIMERA registrada que lo cumpla y `ajustada=True`;
    si no, registra ésta como canónica y la devuelve igual. Determinista dado el contenido del registro."""
    if not enabled():
        return text, vector, False
    try:
        conn = _connect()
        try:
            # Gana la PRIMERA consulta registrada que cumpla el umbral (no la más parecida): el primer texto visto define el
            # grupo y los siguientes se ajustan a él, así los grupos no se fragmentan ni dependen del orden de llegada.
            rows = conn.execute("SELECT text, vec FROM canon WHERE sig=? ORDER BY created ASC, text ASC LIMIT ?",
                                (signature, _MAX_CANONICAL_PER_SIGNATURE)).fetchall()
            best = None
            for stored_text, stored_vec in rows:
                vec = json.loads(stored_vec)
                if _cosine(vector, vec) >= threshold:
                    best = (stored_text, vec)
                    break
            if best is not None:
                return best[0], best[1], best[0] != text
            conn.execute("INSERT OR IGNORE INTO canon (sig, text, vec, created) VALUES (?,?,?,?)",
                         (signature, text, json.dumps(vector), time.time()))
            conn.commit()
            return text, vector, False
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -fail-open
        logger.warning("search_determinism.canonical_query falló; se usa la consulta tal cual.", exc_info=True)
        return text, vector, False
