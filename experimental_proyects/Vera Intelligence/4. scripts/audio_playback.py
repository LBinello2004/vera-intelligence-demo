"""Resuelve una URL firmada y temporal para escuchar el audio original de una conversación.

Pedido explícito (2026-09-14): poder escuchar la conversación real detrás de un resultado de
`search_conversations`, no sólo leer el fragmento de texto reconstruido. Investigado y confirmado
en vivo antes de escribir código: el audio original SÍ es accesible -vive en Google Cloud Storage,
y las credenciales que ya usa el proyecto (`CREDENTIALS_PATH`, mismas que Firestore) tienen permiso
de lectura ahí.

Cómo se arma la URL, tres fuentes:
1. **Postgres** (`mart_v2.recordings_enriched`, la misma tabla que ya usa `vector_search.py`):
   dado un `conversation_id` (vía `core_v2.conversations`), scoped por tenant (`seller_id`, mismo
   criterio de aislamiento que el resto de `vector_search.py`), resuelve `recording_id`,
   `filename` y `duration_seconds`. Nunca se confía en un `conversation_id` sin verificar que
   pertenece al tenant activo -mismo principio de `sql_security.py`, aplicado a mano porque esta
   tabla vive fuera de `dashboard_v2`.
2. **Firestore** (colección `recordings`, documento = `recording_id` -confirmado en vivo que el
   `recording_id` de Postgres coincide exactamente con el id de documento de Firestore): resuelve
   `bucketName`. Ese campo NO está mirrorado en Postgres, así que hace falta esta segunda fuente.
   Este campo viene vacío para la mayoría de los clientes -no es un dato faltante, es el default
   implícito (ver `_DEFAULT_BUCKET`, confirmado en vivo probando un archivo real de Mens Fashion
   ahí); sólo algunos clientes (Tigo, confirmado) tienen bucket propio.
3. **Google Cloud Storage**: con bucket + `filename` ya resueltos, verifica que el blob existe
   (`blob.exists()`, una llamada real -evita devolver una URL firmada que apunte a nada si el
   bucket resuelto en el paso 2 estuviera mal) y genera una URL firmada v4, de sólo lectura, con
   vencimiento corto (`_SIGNED_URL_TTL_MINUTES`) -nunca una URL pública permanente.

Diseño explícito, mismo criterio de "caja negra" que el resto del proyecto: esta función NUNCA se
expone como tool de Gemini ni se agrega al texto que ve el modelo -la interfaz web (`streamlit_app.py`)
la llama directo, sólo cuando el usuario humano hace click en "🔊 escuchar", usando el
`conversation_id` que ya trae cada resultado de `search_conversations` en `tool_calls_log`. El
modelo nunca ve ni necesita saber `filename`/`bucketName`/la URL -evita cualquier riesgo de que un
identificador técnico o un link aparezcan en la respuesta visible.

Resiliencia: cualquier fallo (Postgres, Firestore, GCS, blob inexistente, credenciales faltantes)
se captura y devuelve `None` -nunca lanza hacia la interfaz, mismo criterio que `sheets_logging.py`
y `business_rules.py`/`rag_sources.py` con Langfuse. Es una función de "mejor esfuerzo" para una
acción opcional del usuario, no una dependencia del camino crítico de responder una pregunta.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from utils.postgres import get_postgres_connection

logger = logging.getLogger(__name__)

# Confirmado en vivo (2026-09-14): un archivo real de Mens Fashion (bucketName vacío en Firestore)
# se encontró acá. Es el bucket por default de la plataforma -sólo se declara bucketName en
# Firestore cuando un cliente tiene uno dedicado (ej. Tigo, "audios-to-analyze-tigo").
_DEFAULT_BUCKET = "audios-to-analyze-app2"

# Vencimiento corto a propósito: es una URL de sólo lectura hacia un archivo de audio real de un
# cliente -no debe quedar utilizable indefinidamente si el link se comparte o queda en el
# historial del navegador. 30 minutos alcanza para escuchar una conversación completa (la más
# larga vista hasta ahora, ~20 minutos) sin tener que regenerar la URL a mitad de escucha.
_SIGNED_URL_TTL_MINUTES = 30

# GCS devuelve "application/octet-stream" para estos archivos (Content-Type real nunca seteado al
# subirlos) -sin esto, el <audio> del navegador a veces no reconoce el formato por header y no
# reproduce aunque el archivo esté bien. Mapeo explícito y corto (mismo criterio que
# _OFFENSIVE_TERMS en vector_search.py) en vez de `mimetypes.guess_type`, cuya tabla no siempre
# conoce ".opus" según la versión de Python -mejor una lista chica y verificada que una genérica
# con huecos. Ampliar si aparece una extensión real no cubierta, no intentar anticipar todas.
_CONTENT_TYPE_BY_EXTENSION = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
}


def _guess_content_type(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    return _CONTENT_TYPE_BY_EXTENSION.get(suffix)

_firebase_lock = threading.Lock()
_firebase_app: Any = None


def _ensure_firebase_app() -> Any:
    """Inicializa (una sola vez, cacheado) la app de Firebase Admin -mismo patrón que
    `utils/firestore_export.py._init_firebase`, pero reusando la app default si otra parte del
    proceso ya la inicializó primero (`firebase_admin._apps`), en vez de fallar por doble init."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app
    with _firebase_lock:
        if _firebase_app is not None:
            return _firebase_app
        import firebase_admin
        from firebase_admin import credentials

        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app

        cred_path = os.getenv("CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not cred_path or not os.path.isfile(cred_path):
            raise RuntimeError("CREDENTIALS_PATH no configurado o el archivo no existe.")
        _firebase_app = firebase_admin.initialize_app(credentials.Certificate(cred_path))
        return _firebase_app


def _resolve_recording_from_postgres(
    *, tenant: str, conversation_id: str
) -> dict[str, Any] | None:
    """`conversation_id` -> {recording_id, filename, duration_seconds}, scoped por tenant.

    Nunca confía en un conversation_id ajeno al tenant activo -el filtro `r.seller_id = %s` va
    parametrizado en el WHERE, igual que el resto de `vector_search.py`."""
    with get_postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.recording_id, r.filename, r.duration_seconds
                FROM core_v2.conversations c
                JOIN mart_v2.recordings_enriched r ON r.recording_id = c.recording_id
                WHERE c.conversation_id = %s AND r.seller_id = %s
                LIMIT 1
                """,
                (conversation_id, tenant),
            )
            row = cursor.fetchone()
    if row is None:
        return None
    recording_id, filename, duration_seconds = row
    if not filename:
        return None
    return {
        "recording_id": recording_id,
        "filename": filename,
        "duration_seconds": float(duration_seconds) if duration_seconds is not None else None,
    }


def _resolve_bucket_name(recording_id: str) -> str:
    """Bucket real desde Firestore, o `_DEFAULT_BUCKET` si no está declarado o falla la consulta."""
    try:
        _ensure_firebase_app()
        from firebase_admin import firestore

        doc = firestore.client().collection("recordings").document(recording_id).get()
        if doc.exists:
            data = doc.to_dict() or {}
            bucket = data.get("bucketName")
            if bucket:
                return str(bucket)
    except Exception:  # noqa: BLE001 -ver docstring del módulo: nunca debe romper el flujo.
        logger.warning(
            "No se pudo resolver bucketName en Firestore para recording_id=%s -uso el bucket "
            "default.",
            recording_id,
            exc_info=True,
        )
    return _DEFAULT_BUCKET


def resolve_audio_url(*, tenant: str, conversation_id: str) -> dict[str, Any] | None:
    """Devuelve ``{"url", "duration_seconds"}`` para escuchar el audio de `conversation_id`, o
    ``None`` si no se pudo resolver por cualquier motivo (nunca lanza, ver docstring del módulo).

    Args:
        tenant: literal exacto del cliente activo (``CLIENT_CONFIG.tenant``) -aísla la búsqueda de
            `recording_id` al mismo tenant, igual que el resto de las tools de este proyecto.
        conversation_id: el mismo identificador que ya devuelve `search_conversations`/
            `run_readonly_sql` -nunca se le pide este dato al usuario ni al modelo, la interfaz lo
            toma directo de un resultado de búsqueda ya mostrado.
    """
    try:
        recording = _resolve_recording_from_postgres(tenant=tenant, conversation_id=conversation_id)
        if recording is None:
            return None

        import datetime as _dt

        from google.cloud import storage

        cred_path = os.getenv("CREDENTIALS_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not cred_path or not os.path.isfile(cred_path):
            return None

        bucket_name = _resolve_bucket_name(recording["recording_id"])
        client = storage.Client.from_service_account_json(cred_path)
        blob = client.bucket(bucket_name).blob(recording["filename"])
        if not blob.exists():
            logger.warning(
                "Blob no encontrado: bucket=%s filename=%s (recording_id=%s)",
                bucket_name,
                recording["filename"],
                recording["recording_id"],
            )
            return None

        # response_type (2026-09-14, encontrado al probar en vivo): GCS sirve estos archivos con
        # Content-Type "application/octet-stream" real (nunca seteado al subirlos) -sin esto, el
        # <audio> del navegador a veces no reconoce el formato por header y no reproduce aunque el
        # archivo esté bien. No cambia el archivo en el bucket, sólo el header de ESTA respuesta
        # firmada puntual.
        url = blob.generate_signed_url(
            version="v4",
            expiration=_dt.timedelta(minutes=_SIGNED_URL_TTL_MINUTES),
            method="GET",
            response_type=_guess_content_type(recording["filename"]),
        )
        return {"url": url, "duration_seconds": recording["duration_seconds"]}
    except Exception:  # noqa: BLE001 -ver docstring del módulo: acción opcional, nunca bloqueante.
        logger.warning(
            "No se pudo resolver el audio de conversation_id=%s.", conversation_id, exc_info=True
        )
        return None
