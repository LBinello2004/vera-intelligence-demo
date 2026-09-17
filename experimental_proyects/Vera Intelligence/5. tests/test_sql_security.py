from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from client_config import load_client_config  # noqa: E402
from sql_security import validate_readonly_sql  # noqa: E402


class SqlSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = load_client_config()

    def assert_blocked(self, sql: str) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_sql(sql, self.client)

    def test_allows_authorized_aggregate_with_exact_tenant(self) -> None:
        validate_readonly_sql(
            """
            SELECT COUNT(DISTINCT conversation_id) AS total
            FROM dashboard_v2.vw_mens_fashion_demografia
            WHERE seller_id = 'Mens Fashion' AND usefulforanalysis IS TRUE
            """,
            self.client,
        )

    def test_allows_cte_over_authorized_source(self) -> None:
        validate_readonly_sql(
            """
            WITH base AS (
              SELECT conversation_id
              FROM dashboard_v2.vw_mens_fashion_demografia
              WHERE seller_id = 'Mens Fashion'
            )
            SELECT COUNT(*) FROM base
            """,
            self.client,
        )

    def test_requires_descriptive_profile_tenant_field(self) -> None:
        validate_readonly_sql(
            """
            SELECT conversation_id, errores_oportunidades
            FROM dashboard_v2.vw_mens_fashion_insights_descriptivos_generales
            WHERE seller = 'Mens Fashion'
            LIMIT 5
            """,
            self.client,
        )
        self.assert_blocked(
            """
            SELECT conversation_id
            FROM dashboard_v2.vw_mens_fashion_insights_descriptivos_generales
            WHERE seller_id = 'Mens Fashion'
            """
        )

    def test_blocks_missing_or_wrong_tenant(self) -> None:
        self.assert_blocked(
            "SELECT COUNT(*) FROM dashboard_v2.vw_mens_fashion_demografia"
        )
        self.assert_blocked(
            """
            SELECT COUNT(*) FROM dashboard_v2.vw_mens_fashion_demografia
            WHERE seller_id = 'Otro Cliente'
            """
        )

    def test_blocks_extra_sources_even_if_an_authorized_source_is_present(self) -> None:
        self.assert_blocked(
            """
            SELECT COUNT(*)
            FROM dashboard_v2.vw_mens_fashion_demografia d, pg_catalog.pg_user u
            WHERE seller_id = 'Mens Fashion'
            """
        )

    def test_requires_tenant_filter_for_each_source_in_a_join(self) -> None:
        self.assert_blocked(
            """
            SELECT COUNT(*)
            FROM dashboard_v2.vw_mens_fashion_demografia d
            JOIN dashboard_v2.vw_mens_fashion_rendimiento_vendedor p
              ON p.conversation_id = d.conversation_id
            WHERE d.seller_id = 'Mens Fashion'
            """
        )
        validate_readonly_sql(
            """
            SELECT COUNT(*)
            FROM dashboard_v2.vw_mens_fashion_demografia d
            JOIN dashboard_v2.vw_mens_fashion_rendimiento_vendedor p
              ON p.conversation_id = d.conversation_id
            WHERE d.seller_id = 'Mens Fashion'
              AND p.seller_id = 'Mens Fashion'
            """,
            self.client,
        )

    def test_allows_unqualified_tenant_filter_inside_single_table_cte(self) -> None:
        """Regresión del bug real documentado en huerpel_ventas_alto: un filtro de
        tenant sin calificar, escrito dentro de una CTE de una sola tabla, se
        rechazaba aunque fuera perfectamente válido (no hay ambigüedad posible con
        una sola tabla en ese scope)."""
        validate_readonly_sql(
            """
            WITH demo AS (
              SELECT conversation_id
              FROM dashboard_v2.vw_mens_fashion_demografia
              WHERE seller_id = 'Mens Fashion'
            ),
            perf AS (
              SELECT conversation_id
              FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
              WHERE seller_id = 'Mens Fashion'
            )
            SELECT COUNT(*) FROM demo FULL JOIN perf USING (conversation_id)
            """,
            self.client,
        )

    def test_allows_unqualified_tenant_filter_inside_union_all_branches(self) -> None:
        """Mismo patrón documentado como workaround en el Data Map de Huerpel Ventas
        (joins.cross_source_totals_pattern): dos SELECT independientes unidos por
        UNION ALL, cada uno con su propio filtro sin calificar."""
        validate_readonly_sql(
            """
            SELECT COUNT(*) AS total FROM dashboard_v2.vw_mens_fashion_demografia
            WHERE seller_id = 'Mens Fashion'
            UNION ALL
            SELECT COUNT(*) AS total FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
            WHERE seller_id = 'Mens Fashion'
            """,
            self.client,
        )

    def test_still_blocks_cte_missing_tenant_filter_even_if_sibling_cte_has_one(self) -> None:
        """Guardrail de seguridad: el fix de arriba NO debe permitir que el filtro
        de UNA CTE 'preste' aislamiento a otra CTE sin su propio filtro."""
        self.assert_blocked(
            """
            WITH demo AS (
              SELECT conversation_id
              FROM dashboard_v2.vw_mens_fashion_demografia
              WHERE seller_id = 'Mens Fashion'
            ),
            perf AS (
              SELECT conversation_id
              FROM dashboard_v2.vw_mens_fashion_rendimiento_vendedor
            )
            SELECT COUNT(*) FROM demo FULL JOIN perf USING (conversation_id)
            """
        )

    def test_still_blocks_wrong_tenant_inside_cte(self) -> None:
        self.assert_blocked(
            """
            WITH demo AS (
              SELECT conversation_id
              FROM dashboard_v2.vw_mens_fashion_demografia
              WHERE seller_id = 'Otro Cliente'
            )
            SELECT COUNT(*) FROM demo
            """
        )

    def test_still_requires_qualified_filter_for_join_inside_same_cte(self) -> None:
        """Guardrail de seguridad: si el JOIN pasa a estar DENTRO de la misma CTE
        (dos tablas físicas en el mismo scope), la exención de calificador NO debe
        aplicar -sigue exigiendo el alias exacto por tabla, mismo criterio que
        test_requires_tenant_filter_for_each_source_in_a_join fuera de una CTE."""
        self.assert_blocked(
            """
            WITH combinado AS (
              SELECT d.conversation_id
              FROM dashboard_v2.vw_mens_fashion_demografia d
              JOIN dashboard_v2.vw_mens_fashion_rendimiento_vendedor p
                ON p.conversation_id = d.conversation_id
              WHERE d.seller_id = 'Mens Fashion'
            )
            SELECT COUNT(*) FROM combinado
            """
        )
        validate_readonly_sql(
            """
            WITH combinado AS (
              SELECT d.conversation_id
              FROM dashboard_v2.vw_mens_fashion_demografia d
              JOIN dashboard_v2.vw_mens_fashion_rendimiento_vendedor p
                ON p.conversation_id = d.conversation_id
              WHERE d.seller_id = 'Mens Fashion'
                AND p.seller_id = 'Mens Fashion'
            )
            SELECT COUNT(*) FROM combinado
            """,
            self.client,
        )

    def test_blocks_unqualified_source(self) -> None:
        self.assert_blocked(
            """
            SELECT COUNT(*) FROM recordings
            WHERE seller_id = 'Mens Fashion'
            """
        )

    def test_blocks_multiple_statements_and_mutations(self) -> None:
        self.assert_blocked(
            """
            SELECT * FROM dashboard_v2.vw_mens_fashion_demografia
            WHERE seller_id = 'Mens Fashion'; SELECT 1
            """
        )
        self.assert_blocked("DELETE FROM dashboard_v2.vw_mens_fashion_demografia")

    def test_blocks_arbitrary_functions(self) -> None:
        self.assert_blocked(
            """
            SELECT pg_read_file('/etc/passwd')
            FROM dashboard_v2.vw_mens_fashion_demografia
            WHERE seller_id = 'Mens Fashion'
            """
        )

    def test_all_golden_queries_remain_authorized(self) -> None:
        bank_path = (
            Path(__file__).resolve().parents[1]
            / "2. clientes"
            / "mens_fashion_alto"
            / "preguntas"
            / "preguntas_evaluacion.yaml"
        )
        bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))
        for item in bank["preguntas"]:
            with self.subTest(question_id=item["id"]):
                validate_readonly_sql(item["sql"], self.client)


class SqlSecurityFarma24GoldenBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = load_client_config("farma24_alto")

    def test_all_golden_queries_remain_authorized(self) -> None:
        bank_path = (
            Path(__file__).resolve().parents[1]
            / "2. clientes"
            / "farma24_alto"
            / "preguntas"
            / "preguntas_evaluacion.yaml"
        )
        bank = yaml.safe_load(bank_path.read_text(encoding="utf-8"))
        for item in bank["preguntas"]:
            with self.subTest(question_id=item["id"]):
                validate_readonly_sql(item["sql"], self.client)


if __name__ == "__main__":
    unittest.main()
