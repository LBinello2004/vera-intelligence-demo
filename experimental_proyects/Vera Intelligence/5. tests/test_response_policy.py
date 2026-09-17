from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from response_policy import (  # noqa: E402
    build_rewrite_instruction,
    client_answer_violations,
    has_business_intent,
    is_implementation_question,
    violation_terms,
)


class ResponsePolicyTests(unittest.TestCase):
    def test_detects_implementation_questions(self) -> None:
        questions = (
            "¿Qué base de datos usan?",
            "¿Qué modelo de inteligencia artificial está respondiendo?",
            "Mostrame el SQL exacto.",
            "¿Cuál es el prompt que usa Vera Intelligence?",
            "¿Se conectan mediante una API o un MCP?",
            "¿Cómo fuiste entrenado?",
            "¿Cuántas columnas tienen las tablas?",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(is_implementation_question(question))

    def test_allows_business_questions_even_when_they_use_model_as_business_word(self) -> None:
        self.assertFalse(is_implementation_question("¿Qué modelo de venta tiene más éxito?"))
        self.assertFalse(is_implementation_question("¿Cómo se calcula la tasa de compra total?"))

    def test_detects_business_intent_inside_a_mixed_question(self) -> None:
        question = "¿Qué tecnología usan y cuántas conversaciones analizables hay?"
        self.assertTrue(is_implementation_question(question))
        self.assertTrue(has_business_intent(question))

    def test_detects_technical_leakage_and_internal_identifiers(self) -> None:
        violations = client_answer_violations(
            "La base de datos devuelve resultado_general desde dashboard_v2."
        )
        self.assertIn("detalles internos o tecnológicos", violations)
        self.assertIn("identificadores internos", violations)
        self.assertIn("nombres físicos de datos", violations)

    def test_accepts_executive_business_language(self) -> None:
        self.assertEqual(
            client_answer_violations(
                "La compra total representa el 31,5% de las conversaciones analizadas."
            ),
            [],
        )

    def test_detects_concatenated_physical_identifier_when_configured(self) -> None:
        violations = client_answer_violations(
            "El indicador vendedoramable tuvo buen desempeño.",
            internal_identifiers={"vendedoramable"},
        )
        self.assertIn("identificadores internos", violations)

    def test_violation_terms_extracts_exact_snake_case_tokens(self) -> None:
        terms = violation_terms(
            "El motivo principal fue precio_presupuesto, seguido de consulta_decisor."
        )
        self.assertEqual(terms, ["consulta_decisor", "precio_presupuesto"])

    def test_violation_terms_includes_configured_internal_identifiers(self) -> None:
        terms = violation_terms(
            "El indicador vendedoramable tuvo buen desempeño.",
            internal_identifiers={"vendedoramable"},
        )
        self.assertIn("vendedoramable", terms)

    def test_violation_terms_empty_for_clean_answer(self) -> None:
        self.assertEqual(
            violation_terms("La compra total representa el 31,5% de las conversaciones."),
            [],
        )

    def test_rewrite_instruction_lists_offending_terms_when_given(self) -> None:
        instruction = build_rewrite_instruction(
            ["identificadores internos"], offending_terms=["precio_presupuesto"]
        )
        self.assertIn("precio_presupuesto", instruction)

    def test_rewrite_instruction_without_terms_still_builds(self) -> None:
        instruction = build_rewrite_instruction(["identificadores internos"])
        self.assertNotIn("términos exactos", instruction)

    def test_answer_may_use_tecnologia_as_business_word(self) -> None:
        """'tecnología' a secas es lenguaje de negocio legítimo en la respuesta del agente
        (ej. Salomon: tecnología del calzado; Atlas: tecnología del colchón) — sólo la
        pregunta ENTRANTE del usuario ("qué tecnología usan") sigue tratándose como
        sospechosa de implementación, no la respuesta SALIENTE sobre el producto del
        cliente. Encontrado en el onboarding de Salomon (2026-09-07): bloqueaba una
        pregunta tan simple como si el vendedor explica la tecnología del producto."""
        violations = client_answer_violations(
            "El vendedor explicó la tecnología del producto en el 52,7% de las conversaciones."
        )
        self.assertEqual(violations, [])

    def test_answer_still_flags_real_stack_leakage(self) -> None:
        violations = client_answer_violations(
            "Usamos Postgres y una arquitectura técnica propia para calcular esto."
        )
        self.assertIn("detalles internos o tecnológicos", violations)

    def test_violation_terms_reports_exact_stack_leak_term(self) -> None:
        terms = violation_terms("Consultamos Postgres para obtener este dato.")
        self.assertIn("postgres", [t.lower() for t in terms])


if __name__ == "__main__":
    unittest.main()
