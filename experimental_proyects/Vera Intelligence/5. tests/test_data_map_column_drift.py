from __future__ import annotations

import unittest

from data_map_column_drift import find_drift


class FindDriftTests(unittest.TestCase):
    def test_reports_fields_missing_from_the_real_view(self):
        # Caso real (Steren, 2026-09-24): campos declarados sin el prefijo físico `descriptivos_`.
        data_map = {"sources": {"insights_descriptivos": {
            "source": "dashboard_v2.vw_steren_insights_descriptivos",
            "fields": {"objeciones_mencionadas_cliente": {}, "cierre_venta": {}},
        }}}
        columns = {"dashboard_v2.vw_steren_insights_descriptivos": {
            "descriptivos_objeciones_mencionadas_cliente", "descriptivos_cierre_venta", "seller_name"}}
        findings = find_drift(data_map, columns)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["missing"], ["objeciones_mencionadas_cliente", "cierre_venta"])

    def test_no_findings_when_every_field_exists(self):
        data_map = {"sources": {"demography": {
            "source": "dashboard_v2.vw_x_demografia", "fields": {"tipointeraccion": {}}}}}
        self.assertEqual(find_drift(data_map, {"dashboard_v2.vw_x_demografia": {"tipointeraccion", "otra"}}), [])

    def test_grouping_keys_with_campos_are_documentation_not_columns(self):
        # Tigo: `calidad_del_asesor_escala` agrupa 18 campos en `campos:`; no es una columna.
        data_map = {"sources": {"categorical_insights": {
            "source": "dashboard_v2.vw_tigo_insights_categoricos",
            "fields": {"calidad_del_asesor_escala": {"description": "x", "campos": {"a03": "..."}}}}}}
        self.assertEqual(find_drift(data_map, {"dashboard_v2.vw_tigo_insights_categoricos": {"a03"}}), [])

    def test_missing_view_is_reported(self):
        data_map = {"sources": {"s": {"source": "dashboard_v2.vw_gone", "fields": {"a": {}}}}}
        findings = find_drift(data_map, {"dashboard_v2.vw_gone": set()})
        self.assertTrue(findings[0]["view_not_found"])

    def test_sources_without_schema_are_skipped(self):
        data_map = {"sources": {"s": {"source": "sin_schema", "fields": {"a": {}}}}}
        self.assertEqual(find_drift(data_map, {}), [])


if __name__ == "__main__":
    unittest.main()
