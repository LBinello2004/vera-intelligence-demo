"""Control cooperativo de análisis y errores operativos que no puede reparar el agente."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class AnalysisCancelled(Exception):
    """El análisis fue cancelado o su sesión dejó de estar activa."""


class OperationalUnavailable(RuntimeError):
    """Error operativo definitivo para esta ejecución, con mensaje seguro para el usuario."""

    def __init__(self, message: str = "No pude acceder a la información necesaria en este momento. Intentá nuevamente más tarde.") -> None:
        super().__init__(message)
        self.user_message = message


class AnalysisControl:
    def __init__(self, is_abandoned: Callable[[], bool] | None = None) -> None:
        self._cancelled = threading.Event()
        self._is_abandoned = is_abandoned

    def cancel(self) -> None:
        self._cancelled.set()

    def check(self) -> None:
        if self._cancelled.is_set() or (self._is_abandoned is not None and self._is_abandoned()):
            self.cancel()
            raise AnalysisCancelled("Análisis cancelado.")

    def wait(self, seconds: float) -> None:
        """Backoff interrumpible, también ante desconexión de la sesión."""
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._cancelled.wait(min(remaining, 0.2))


_CONTROL: ContextVar[AnalysisControl | None] = ContextVar("vi_analysis_control", default=None)

# El cliente del MVP es global. Un rerun debe esperar a que termine el trabajo ya iniciado
# antes de cambiarlo; el control cancela las etapas siguientes, no una llamada ya en vuelo.
ACTIVE_CLIENT_LOCK = threading.RLock()


@contextmanager
def analysis_scope(control: AnalysisControl | None) -> Iterator[None]:
    token = _CONTROL.set(control)
    try:
        check_analysis()
        yield
    finally:
        _CONTROL.reset(token)


def check_analysis() -> None:
    control = _CONTROL.get()
    if control is not None:
        control.check()


def current_analysis_control() -> AnalysisControl | None:
    return _CONTROL.get()


def wait_before_retry(seconds: float) -> None:
    control = _CONTROL.get()
    if control is None:
        time.sleep(seconds)
    else:
        control.wait(seconds)
