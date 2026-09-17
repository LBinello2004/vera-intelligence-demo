"""Logger compartido a Google Sheets para feedback_tracking.py y question_tracking.py.

Pedido explícito (2026-09-14): "conectar un google sheets para que se guarde ahí siempre que
alguien use el tester, independientemente de en qué computadora se use por lo del git" -hoy
feedback_events.jsonl y questions_events.jsonl viven en el repo y sólo se ven centralizados si
cada máquina hace commit/push del archivo (ver feedback_tracking.py, decisión original de
2026-09-11). Un Sheet compartido resuelve eso: cualquier máquina escribe al mismo lugar en el
momento, sin pasar por git.

MECANISMO -Apps Script Web App, NO la API de Google Sheets con service account (2026-09-14,
cambio de diseño): el intento original reusaba el service account de Firestore (CREDENTIALS_PATH)
con la librería `gspread`, pero requería habilitar la API de Sheets en la consola de Google Cloud
del proyecto ("retailmind-df126") -Lucas no tiene acceso de administración a esa cuenta de Google
Cloud. Rediseñado para no depender de eso en absoluto: un pequeño script (Apps Script, ver
"Cómo configurarlo" abajo) publicado DESDE LA PROPIA PLANILLA como "Web App", usando sólo la
cuenta de Google que ya es dueña/editora de la planilla -sin consola de Google Cloud, sin service
account nuevo, sin que nadie (ni quien usa el tester) tenga que loguearse en ningún momento. Este
módulo simplemente hace un POST HTTP a esa URL con la fila a agregar.

Cómo configurarlo (una sola vez, lo hace quien es dueño de la planilla):
1. Abrir la planilla → Extensiones → Apps Script.
2. Pegar el código de Apps Script (pedirlo si hace falta) y guardar.
3. Implementar → Nueva implementación → tipo "Aplicación web". Ejecutar como "Yo", acceso
   "Cualquier usuario". Copiar la URL que termina en /exec.
4. Poner esa URL en VI_SHEETS_WEBAPP_URL (.env raíz del repo).
Sin esa variable configurada, este logger queda deshabilitado (no-op) -ver `enabled`.

Diseño explícito de resiliencia (mismo criterio que business_rules.py/rag_sources.py con
Langfuse): un fallo acá (sin URL configurada, sin red, Web App caído o mal desplegado) NUNCA debe
romper el flujo que llama a esto -sólo se pierde la copia remota, la escritura local en JSONL (que
sigue pasando siempre, sin cambios) es la que garantiza que el dato no se pierde. Por eso todo
error queda contenido en `append_row`, nunca se re-lanza.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# URL del Web App de Apps Script (2026-09-14) -desplegada por Lucas directo desde la planilla, sin
# consola de Google Cloud (ver docstring del módulo). Hardcodeada como default acá, NO sólo en
# .env, a propósito: .env está gitignorado (tiene PGPASSWORD y API keys reales), así que una
# máquina nueva que sólo hace `git pull` nunca la vería si viviera únicamente ahí -contradiría el
# pedido original ("que se guarde ahí siempre... independientemente de en qué computadora se
# use"). No es un secreto del mismo tipo que esas credenciales: el Web App sólo expone `doPost`
# (agregar una fila), nada de lectura ni borrado -el peor caso de que la URL se filtre es spam de
# filas en la planilla, no acceso a la cuenta de Lucas. Sigue siendo overrideable con
# VI_SHEETS_WEBAPP_URL por si se redespliega el script y cambia la URL.
_DEFAULT_WEBAPP_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxwJqtIWbi7FDHv8WyN5ckixaQk1WQqmE9LFI1hzE-4SR9DyMUbLrzF-jWs9X7K2f3c/exec"
)

# Timeout corto a propósito: esto corre en el camino de una acción interactiva (votar, mandar una
# pregunta) -si el Web App de Apps Script está lento o caído, no puede demorar la experiencia real
# más que un instante. Un fallo acá sólo pierde la copia remota, nunca el registro local.
_REQUEST_TIMEOUT_SECONDS = 5

# Orden de extracción (claves reales del dict `event`, ver feedback_tracking.py/
# question_tracking.py) separado del texto de encabezado (`_HEADER_LABELS_BY_SHEET` abajo) a
# propósito: el primero tiene que matchear `event.get(key)` exacto, el segundo es sólo para que
# alguien abra la planilla sin contexto técnico y entienda cada columna (2026-09-14, pedido
# explícito: "que la planilla quede bien, con columnas con nombres claros").
_FIELD_ORDER_BY_SHEET: dict[str, list[str]] = {
    "feedback": [
        "recorded_at",
        "client_id",
        "session_id",
        "message_id",
        "vote",
        "question",
        "answer",
        "tool_names",
    ],
    "preguntas": [
        "recorded_at",
        "client_id",
        "client_display_name",
        "session_id",
        "question",
        "answer",
        "tool_names",
    ],
}

_HEADER_LABELS_BY_SHEET: dict[str, list[str]] = {
    "feedback": [
        "Fecha y hora (UTC)",
        "Cliente (ID interno)",
        "ID de sesión",
        "ID del mensaje",
        "Voto",
        "Pregunta del usuario",
        "Respuesta de Vera",
        "Herramientas usadas",
    ],
    "preguntas": [
        "Fecha y hora (UTC)",
        "Cliente (ID interno)",
        "Cliente (nombre comercial)",
        "ID de sesión",
        "Pregunta del usuario",
        "Respuesta de Vera",
        "Herramientas usadas",
    ],
}


class SheetsLogger:
    """Agrega filas a una pestaña de Google Sheets vía un Web App de Apps Script, best-effort.

    Deshabilitado por default (sin `webapp_url`, ver `from_env()`) -en ese caso `append_row` es un
    no-op silencioso, para que un entorno sin este setup (ej. una máquina nueva, o mientras no se
    haya desplegado el Web App todavía) siga funcionando exactamente igual que antes.
    """

    def __init__(self, webapp_url: str | None) -> None:
        self._webapp_url = webapp_url
        self._disabled = not webapp_url

    @classmethod
    def from_env(cls) -> "SheetsLogger":
        return cls(os.getenv("VI_SHEETS_WEBAPP_URL", _DEFAULT_WEBAPP_URL))

    @property
    def enabled(self) -> bool:
        return not self._disabled

    def append_row(self, sheet_name: str, event: dict[str, Any]) -> bool:
        """Agrega `event` como una fila nueva, en el orden de `_FIELD_ORDER_BY_SHEET`.

        Devuelve True/False según el resultado, pero NUNCA lanza -ver el docstring del módulo.
        Listas (ej. tool_names) se aplanan a texto separado por coma para que la celda sea legible
        a simple vista en Sheets. El Web App crea la pestaña y su encabezado (con las etiquetas
        legibles de `_HEADER_LABELS_BY_SHEET`, no las claves técnicas) la primera vez que recibe
        una fila para un `sheet_name` que todavía no existe (ver el código de Apps Script) -si la
        pestaña ya existe con un encabezado viejo, no lo pisa, sólo agrega la fila debajo.
        """
        if self._disabled:
            return False
        import requests  # import perezoso: ya es dependencia del repo, pero evita que este
        # módulo dependa de red/import pesado si nunca se usa (mismo criterio que el import
        # perezoso de gspread en la versión anterior de este archivo).

        field_order = _FIELD_ORDER_BY_SHEET.get(sheet_name, list(event.keys()))
        header = _HEADER_LABELS_BY_SHEET.get(sheet_name, field_order)
        row = [_flatten(event.get(key)) for key in field_order]
        payload = {"sheet": sheet_name, "row": row, "header": header}
        try:
            response = requests.post(self._webapp_url, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            body = response.json()
            if not body.get("ok", False):
                raise RuntimeError(f"Web App devolvió ok=false: {body.get('error')}")
            return True
        except Exception:  # noqa: BLE001 -ver docstring: un fallo acá nunca debe propagarse.
            logger.warning(
                "No se pudo escribir en Google Sheets (hoja %s) -se conserva sólo el registro "
                "local. Revisar VI_SHEETS_WEBAPP_URL o el despliegue del Apps Script si esto "
                "persiste.",
                sheet_name,
                exc_info=True,
            )
            return False


def _flatten(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return value
