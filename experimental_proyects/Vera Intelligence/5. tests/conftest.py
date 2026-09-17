"""Fixtures compartidas para toda la suite de "5. tests".

Sólo contiene protección de aislamiento de logs de PRODUCCIÓN -no helpers de negocio (esos siguen
viviendo en cada archivo de test, como FakeChat/FakeResponse en test_vi_agent.py).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vi_agent  # noqa: E402
from usage_tracking import InteractionOutcomeRecorder, UsageRecorder  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _redirect_usage_log():
    """Redirige `vi_agent._USAGE_RECORDER`/`USAGE_LOG_PATH` a un archivo temporal para TODA la
    suite (2026-09-15).

    BUG REAL encontrado armando `_redirect_interaction_outcome_log` (abajo): este log YA estaba
    contaminado -260 de 896 líneas de `gemini_calls.jsonl` (29%) eran ruido de tests (firma
    reconocible: `prompt_token_count=10, candidates_token_count=1, total_token_count=11`, del mismo
    `usage_metadata` de prueba que reaparece en decenas de tests). La mayoría de los tests SÍ pasan
    `usage_recorder=UsageRecorder(path_temporal)` explícito y quedan a salvo, pero cualquiera que no
    lo haga cae en `_USAGE_RECORDER` global -que apunta al archivo real- y unos pocos SÍ arman un
    `usage_metadata` completo en su `FakeResponse` (la mayoría no, por eso el problema no era total).
    Contaminó justo el análisis de "medí los reintentos" de esta misma sesión: 52 de los 55
    `client_safe_rewrite` contados entonces eran de tests, no de mens_fashion en producción -sólo 4
    eran reales. Mismo patrón de fixture que `_redirect_interaction_outcome_log`."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "gemini_calls.jsonl"
        with patch.object(vi_agent, "USAGE_LOG_PATH", log_path), patch.object(
            vi_agent, "_USAGE_RECORDER", UsageRecorder(log_path)
        ):
            yield


@pytest.fixture(autouse=True, scope="session")
def _redirect_interaction_outcome_log():
    """Redirige `vi_agent._INTERACTION_OUTCOME_RECORDER` a un archivo temporal para TODA la suite.

    Bug real ya encontrado el 2026-09-11 para un log hermano (`vector_search_calls.jsonl`, ver
    `_RedirectsUsageLogTestCase` en test_vector_search.py): sin esto, 828 de 938 eventos reales
    terminaron siendo ruido de tests. Ese log se salvó de tests SIN este patch por una coincidencia
    frágil (record_response de gemini_calls.jsonl no escribe si `usage_metadata` es None, y
    FakeResponse en los tests nunca lo trae) -InteractionOutcomeRecorder (2026-09-15, ver
    "6. busqueda_vectorial/README.md" > "Iteración 26") no tiene ese mismo freno natural: escribe
    siempre que `_record_interaction_outcome` lo llama, y eso pasa en casi cualquier `return` real
    de `run_tool_loop` -es decir, en la enorme mayoría de los tests de varios archivos
    (test_vi_agent.py, test_answer_verification.py, test_runtime_optimizations.py...), no sólo uno.
    Un fixture de sesión acá evita tener que duplicar un `setUp`/`tearDown` por archivo y protege
    también cualquier test nuevo que llame a run_tool_loop sin pensarlo.

    BUG REAL encontrado armando este mismo fixture: patchear sólo `_INTERACTION_OUTCOME_RECORDER`
    no alcanza -30 eventos de test terminaron igual en el archivo real la primera vez. Motivo:
    varios tests (ej. `AllClientsIdentifierLengthInvariantTests`) llaman a
    `vi_agent.configure_client(...)` para cambiar de cliente activo, y esa función RECONSTRUYE
    `_INTERACTION_OUTCOME_RECORDER = InteractionOutcomeRecorder(INTERACTION_LOG_PATH)` desde cero
    cada vez -pisando el objeto ya patcheado con uno nuevo apuntando otra vez al archivo real. Se
    necesitan los DOS patches: `INTERACTION_LOG_PATH` para que cualquier reconstrucción futura (via
    configure_client) siga apuntando al temporal, y `_INTERACTION_OUTCOME_RECORDER` para cubrir el
    objeto que ya existía desde el primer `configure_client()` al importar el módulo (antes de que
    este fixture llegue a correr)."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "interaction_outcomes.jsonl"
        with patch.object(vi_agent, "INTERACTION_LOG_PATH", log_path), patch.object(
            vi_agent, "_INTERACTION_OUTCOME_RECORDER", InteractionOutcomeRecorder(log_path)
        ):
            yield
