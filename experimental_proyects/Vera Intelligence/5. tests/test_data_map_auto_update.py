from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import data_map_auto_update as dmu  # noqa: E402


class RelativeLanguageLintTests(unittest.TestCase):
    """Cubre el linter agregado el 2026-09-09 tras el caso real de high_life_alto (ver
    README.md > "Playbook: qué hacer cuando el poller devuelve gate_fallo_no_promovido"):
    una pregunta dorada redactada con "vigente"/"reciente"/"actualmente" deja de tener un
    significado fijo en cuanto el rulebook correspondiente sube de versión."""

    def test_detects_each_keyword(self) -> None:
        for keyword in dmu.RELATIVE_LANGUAGE_KEYWORDS:
            with self.subTest(keyword=keyword):
                self.assertTrue(dmu._contains_relative_language(f"¿Algo sobre el criterio {keyword}?"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(dmu._contains_relative_language("¿Qué pasa en el checklist VIGENTE?"))

    def test_stable_question_not_flagged(self) -> None:
        self.assertFalse(
            dmu._contains_relative_language("¿Qué porcentaje de vendedores explicó el programa de lealtad?")
        )

    def test_false_positive_guard_actual_alone_not_flagged(self) -> None:
        # "actual" solo (no "actualmente") no dispara el lint -ej. huerpel_ventas_alto pregunta por
        # el "vehículo actual del cliente" (un trade-in), nada que ver con qué rulebook está vigente.
        self.assertFalse(
            dmu._contains_relative_language("¿Con qué frecuencia el asesor ofrece la valuación del vehículo actual del cliente?")
        )

    def test_lint_bank_flags_only_relative_questions(self) -> None:
        bank = {
            "preguntas": [
                {"id": "q_stable", "pregunta": "¿Cuántas conversaciones hay en total?"},
                {"id": "q_vigente", "pregunta": "¿Qué pasa en el checklist vigente?"},
                {"id": "q_reciente", "pregunta": "¿Y en las conversaciones recientes?"},
            ]
        }
        self.assertEqual(dmu.lint_golden_bank_relative_language(bank), ["q_vigente", "q_reciente"])

    def test_lint_bank_empty_when_no_matches(self) -> None:
        bank = {"preguntas": [{"id": "q1", "pregunta": "¿Cuáles son los productos más mencionados?"}]}
        self.assertEqual(dmu.lint_golden_bank_relative_language(bank), [])


class NumbersMatchToleranceTests(unittest.TestCase):
    """La auto-relajación de tolerancia (AUTO_RELAXED_MAX_MISSING_NUMBERS) sólo debe activarse
    para preguntas trampa con lenguaje relativo y SIN calibración manual explícita -nunca debe
    pisar un max_missing_numbers puesto a mano, ni relajar preguntas comunes."""

    def test_default_tolerance_is_strict(self) -> None:
        old = {"100", "200.5", "300"}
        new = {"100"}
        passed, missing, added = dmu._numbers_match(old, new, max_missing=dmu.MAX_MISSING_NUMBERS_PER_QUESTION)
        self.assertFalse(passed)
        self.assertEqual(missing, {"200.5", "300"})

    def test_relaxed_tolerance_accepts_more_missing(self) -> None:
        old = {"100", "200", "300", "400"}
        new = {"100"}
        passed, missing, _ = dmu._numbers_match(old, new, max_missing=dmu.AUTO_RELAXED_MAX_MISSING_NUMBERS)
        self.assertTrue(passed)
        self.assertEqual(len(missing), 3)

    def test_relaxed_tolerance_still_rejects_gross_mismatch(self) -> None:
        old = {"100", "200", "300", "400", "500", "600", "700"}
        new = {"100"}
        passed, _, _ = dmu._numbers_match(old, new, max_missing=dmu.AUTO_RELAXED_MAX_MISSING_NUMBERS)
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()
