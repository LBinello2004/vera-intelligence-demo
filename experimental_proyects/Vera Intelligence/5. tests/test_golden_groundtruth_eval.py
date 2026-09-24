from __future__ import annotations

import unittest

from golden_groundtruth_eval import analyzable_variant, answer_numbers, expected_numbers, score_answer, summarize


class ExpectedNumbersTests(unittest.TestCase):
    def test_takes_integers_from_first_rows_and_ignores_small_ones(self):
        ints, _ = expected_numbers([("Cables", 2823), ("Energia", 2639), ("Otro", 7)])
        self.assertEqual(ints, {2823, 2639})

    def test_single_numerator_base_row_yields_a_percentage(self):
        ints, pcts = expected_numbers([(7856, 10858)])
        self.assertEqual(ints, {7856, 10858})
        self.assertEqual(pcts, {72.4})

    def test_ignores_none_and_bool(self):
        ints, pcts = expected_numbers([(None, True)])
        self.assertEqual((ints, pcts), (set(), set()))


class AnswerNumbersTests(unittest.TestCase):
    def test_thousand_separators_in_both_locales(self):
        ints, _ = answer_numbers("Hay 10.858 conversaciones y 7,856 compras")
        self.assertTrue({10858, 7856} <= ints)

    def test_decimals_and_ignores_code_blocks(self):
        text = 'El 19,0% terminó sin compra.\n```vera-chart\n{"values": [99999]}\n```'
        ints, decimals = answer_numbers(text)
        self.assertIn(19.0, decimals)
        self.assertNotIn(99999, ints)


class ScoreAndSummaryTests(unittest.TestCase):
    def test_coverage_counts_expected_integers_present(self):
        score = score_answer("Cables 2.823 y Energia 2.639", {2823, 2639, 1423}, set())
        self.assertAlmostEqual(score["cov_int"], 2 / 3)
        self.assertFalse(score["fallback"])

    def test_fallback_is_detected_and_excluded_from_mean_coverage(self):
        results = [
            {"id": "q1", "fallback": True, "cov_int": 0.0},
            {"id": "q1", "fallback": False, "cov_int": 1.0},
        ]
        summary = summarize(results)["q1"]
        self.assertEqual((summary["runs"], summary["fallbacks"]), (2, 1))
        self.assertEqual(summary["cov_mean"], 1.0)
        self.assertTrue(score_answer("No pude verificar las cifras...", {100}, set())["fallback"])


class AnalyzableVariantTests(unittest.TestCase):
    def test_adds_the_filter_after_the_tenant_condition(self):
        sql = "SELECT COUNT(*) FROM v WHERE seller_id = 'Steren' GROUP BY 1"
        self.assertEqual(
            analyzable_variant(sql),
            "SELECT COUNT(*) FROM v WHERE seller_id = 'Steren' AND usefulforanalysis IS TRUE GROUP BY 1",
        )

    def test_leaves_sql_that_already_filters_or_has_no_tenant_untouched(self):
        with_filter = "SELECT 1 FROM v WHERE seller_id = 'X' AND usefulforanalysis IS TRUE"
        self.assertEqual(analyzable_variant(with_filter), with_filter)
        no_tenant = "SELECT 1 FROM v"
        self.assertEqual(analyzable_variant(no_tenant), no_tenant)


if __name__ == "__main__":
    unittest.main()
