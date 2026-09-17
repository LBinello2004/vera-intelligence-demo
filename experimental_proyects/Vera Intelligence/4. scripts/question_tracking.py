"""Registro de CADA pregunta real hecha a Vera Intelligence, por cliente -versionado en git, no
local (mismo criterio que feedback_tracking.py, ver ese módulo para el porqué de "en git, no
.runtime/").

Pedido explícito (2026-09-14): "guardar las preguntas que hace cada cliente para entender cómo
utilizan Vera Intelligence cada uno en específico". A diferencia de feedback_tracking.py (que sólo
registra un evento cuando alguien vota 👍/👎), esto registra TODA pregunta real que se envía, haya
o no voto después -el objetivo es el patrón de uso completo por cliente (qué temas pregunta cada
uno, con qué frecuencia, qué herramientas termina necesitando), no sólo los casos que alguien
calificó. El saludo inicial NO cuenta como pregunta acá -lo dispara la interfaz, no un usuario de
negocio eligiendo qué preguntar-, ver el gate en streamlit_app.py.

Reorganizado (2026-09-14, mismo día, a pedido explícito): el log vivía en un único archivo
compartido bajo un top-level "preguntas_por_cliente/" -movido a DENTRO de la carpeta de cada
cliente (`2. clientes/<carpeta>/preguntas/questions_events.jsonl`), misma lógica de organización
que ya usa el resto del proyecto (cada cliente es dueño de su propia data: config.yaml, data_map/,
preguntas/ -el banco de evaluación ya vive ahí, este log de uso real ahora también). IMPORTANTE:
la ruta se resuelve por el NOMBRE DE CARPETA real (`client_folder`, ej. "agrosuper_bajo"), nunca por
`client_config.ClientConfig.client_id` (la identidad estable, ej. "agrosuper") -son cosas distintas
que sólo coinciden por casualidad para algunos clientes (mismo problema ya documentado en
`data_map_auto_update.run_gate()`/`promote()`: el sufijo de grado _alto/_medio/_bajo vive sólo en el
nombre de carpeta). Usar `client_id` para resolver una ruta de archivo rompe en cuanto folder !=
client_id.

**Google Sheets, además del JSONL local (2026-09-14)**: mismo mecanismo aditivo que
`feedback_tracking.py` -ver su docstring y `sheets_logging.py`. `sheets_logger` opcional,
default None (comportamiento idéntico a antes en todos los tests existentes).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from client_config import CLIENTS_ROOT

try:
    from sheets_logging import SheetsLogger
except ImportError:  # pragma: no cover
    SheetsLogger = None  # type: ignore[assignment,misc]


class QuestionRecorder:
    """Agrega una entrada JSONL por pregunta real, en el archivo del cliente correspondiente
    -mismo patrón de escritura (append atómico, 0o600) que
    `usage_tracking.UsageRecorder`/`feedback_tracking.FeedbackRecorder`. `sheets_logger` opcional,
    ver `FeedbackRecorder`."""

    def __init__(
        self, clients_root: Path = CLIENTS_ROOT, sheets_logger: "SheetsLogger | None" = None
    ) -> None:
        self.clients_root = clients_root
        self.sheets_logger = sheets_logger

    def log_path(self, client_folder: str) -> Path:
        return self.clients_root / client_folder / "preguntas" / "questions_events.jsonl"

    def record(
        self,
        *,
        client_folder: str,
        client_id: str,
        client_display_name: str,
        session_id: str,
        question: str,
        answer: str,
        tool_names: list[str],
    ) -> dict[str, Any]:
        event = {
            "schema_version": 1,
            # Fecha Y hora, no sólo fecha -pedido explícito (2026-09-14)- en ISO 8601 UTC, mismo
            # criterio que el resto de los logs del proyecto (usage_tracking.py/feedback_tracking.py).
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "client_id": client_id,
            # Nombre comercial legible (ej. "Agrosuper"), además del client_id -pedido explícito de
            # que quede claro "qué cliente hizo la pregunta" al leer el archivo a mano, sin tener
            # que cruzar client_id contra config.yaml. Redundante con la ubicación del archivo
            # (ya está dentro de la carpeta de ESE cliente), pero se mantiene para que cada línea
            # siga siendo autocontenida si alguna vez se copia o se agrega fuera de contexto.
            "client_display_name": client_display_name,
            "session_id": session_id,
            "question": question,
            # answer (2026-09-15, pedido explícito: "que también se guarde la respuesta de Vera
            # Intelligence"): antes este log sólo tenía la pregunta -para ver qué había respondido
            # Vera a una pregunta real había que cruzar session_id contra el feedback (que sólo
            # existe si alguien votó) o contra .runtime/ (local, no compartido). Mismo campo que ya
            # usa feedback_tracking.py, mismo criterio.
            "answer": answer,
            "tool_names": tool_names,
        }
        self._append(self.log_path(client_folder), event)
        if self.sheets_logger is not None:
            try:
                self.sheets_logger.append_row("preguntas", event)
            except Exception:  # noqa: BLE001 -ver el mismo comentario en feedback_tracking.py.
                pass
        return event

    def _append(self, path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor = os.open(
            str(path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            if hasattr(os, "fchmod"):
                # No disponible en Windows: los permisos POSIX no aplican ahi
                # y os.open ya paso 0o600 al crear el archivo.
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)


def load_question_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Registro de pregunta inválido en línea {line_number}.") from exc
        if isinstance(event, dict):
            events.append(event)
    return events
