"""Registro de feedback (👍/👎) por respuesta de Vera Intelligence -versionado en git, no local.

Pedido explícito (2026-09-11): "se te ocurre algo que mejore cómo funciona Vera Intelligence" -
hoy no existe ninguna señal real de qué respuestas sirvieron y cuáles no, toda la iteración de
prompts de esta sesión se basó en casos puntuales que alguien notó a mano (ver 9. HISTORIAL.md).
Esto guarda una señal real, acumulable, para iterar con datos en vez de anécdota.

Guarda pregunta + respuesta (a diferencia de usage_tracking.py/vector_search.py, que
deliberadamente nunca loguean contenido) porque sin eso el feedback es inútil para el propósito
que motivó agregarlo -saber QUÉ estuvo bien o mal, no sólo que algo lo estuvo.

Vive en "7. feedback/feedback_events.jsonl" -NO en .runtime/ (gitignored) como el resto de los
logs locales del proyecto. Decisión explícita (2026-09-11, mismo día): Lucas pidió que esto SÍ
quede versionado en git -"si lo subo al repo y alguien más lo usa lo puede marcar como bien o
mal, y así vamos ampliando la base de feedback útil"-, para que la señal se acumule entre todos
los que corran Vera Intelligence en local, no sólo en la máquina de quien vota. Formato
append-only (una línea JSON por voto) a propósito: los merges de git sobre líneas nuevas al final
del archivo son triviales; un conflicto real sólo puede pasar si dos personas votan y commitean
en la misma ventana de tiempo sin sincronizar -no se resuelve automáticamente, pero es
infrecuente y el costo de un merge manual ahí es bajo.

**Google Sheets, además del JSONL local (2026-09-14, pedido explícito)**: el JSONL de git sigue
siendo la fuente de verdad y se sigue escribiendo siempre igual -pero depender de que cada
máquina haga commit/push es frágil para juntar señal real de terceros usando el tester. Un
`sheets_logger` opcional (ver sheets_logging.py) agrega la misma fila también a un Google Sheet
compartido, en el momento, sin pasar por git. Es puramente aditivo: sin `sheets_logger` (default
None, como en todos los tests existentes) el comportamiento es idéntico a antes. Un fallo al
escribir en Sheets (sin credenciales, sin red, Sheet no compartido) nunca rompe ni bloquea el
registro local -ver el docstring de `SheetsLogger.append_row`. Ya no aplica la restricción de
"local, no servicios externos" de [[feedback-prefer-local-over-external-services]] porque acá el
pedido de usar un servicio externo fue explícito de Lucas, no una decisión unilateral.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from sheets_logging import SheetsLogger
except ImportError:  # pragma: no cover -mismo criterio que el resto del proyecto: un import
    # opcional roto no debe impedir que el resto del módulo cargue.
    SheetsLogger = None  # type: ignore[assignment,misc]


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
FEEDBACK_LOG_PATH = PROJECT_ROOT / "7. feedback" / "feedback_events.jsonl"

VALID_VOTES = ("up", "down")


class FeedbackRecorder:
    """Agrega una entrada JSONL por voto -mismo patrón de escritura (append atómico, 0o600) que
    `usage_tracking.UsageRecorder`. `sheets_logger` es opcional (ver sheets_logging.py) -si se
    pasa, la misma fila se agrega también a Google Sheets, best-effort, sin afectar el registro
    local si falla."""

    def __init__(self, path: Path, sheets_logger: "SheetsLogger | None" = None) -> None:
        self.path = path
        self.sheets_logger = sheets_logger

    def record(
        self,
        *,
        client_id: str,
        session_id: str,
        message_id: str,
        vote: str,
        question: str | None,
        answer: str,
        tool_names: list[str],
    ) -> dict[str, Any]:
        if vote not in VALID_VOTES:
            raise ValueError(f"vote debe ser uno de {VALID_VOTES}, recibido: {vote!r}")
        event = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "client_id": client_id,
            "session_id": session_id,
            "message_id": message_id,
            "vote": vote,
            "question": question,
            "answer": answer,
            "tool_names": tool_names,
        }
        self._append(event)
        if self.sheets_logger is not None:
            try:
                self.sheets_logger.append_row("feedback", event)
            except Exception:  # noqa: BLE001 -defensa en profundidad: SheetsLogger.append_row ya
                # nunca lanza en producción, pero record() no debe asumirlo ciegamente. El local
                # ya se escribió arriba -no se pierde el voto por esto.
                pass
        return event

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor = os.open(
            self.path,
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


def load_feedback_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Registro de feedback inválido en línea {line_number}.") from exc
        if isinstance(event, dict):
            events.append(event)
    return events
