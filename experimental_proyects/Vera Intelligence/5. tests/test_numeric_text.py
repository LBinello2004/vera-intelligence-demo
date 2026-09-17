from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numeric_text  # noqa: E402


class NormalizeNumberTests(unittest.TestCase):
    """normalize_number -extraído verbatim de data_map_auto_update.py (2026-09-14) para reusarlo
    en vi_agent.py sin import circular. Mismos casos que ya cubría ese archivo, para confirmar que
    la extracción no cambió el comportamiento."""

    def test_spanish_style_thousands_dot_decimal_comma(self) -> None:
        self.assertEqual(numeric_text.normalize_number("247.556"), "247556")
        self.assertEqual(numeric_text.normalize_number("95,96"), "95.96")

    def test_english_style_thousands_comma_decimal_dot(self) -> None:
        self.assertEqual(numeric_text.normalize_number("247,556"), "247556")
        self.assertEqual(numeric_text.normalize_number("95.96"), "95.96")

    def test_no_separator_passes_through(self) -> None:
        self.assertEqual(numeric_text.normalize_number("500"), "500")


class NumbersInTests(unittest.TestCase):
    def test_filters_small_incidental_integers(self) -> None:
        self.assertEqual(numeric_text.numbers_in("Farma 24, top 5, paso 9"), set())

    def test_keeps_large_counts_and_decimals(self) -> None:
        result = numeric_text.numbers_in("247.556 conversaciones, tasa de 42,5%")
        self.assertEqual(result, {"247556", "42.5"})


class CloseEnoughTests(unittest.TestCase):
    def test_within_tolerance(self) -> None:
        self.assertTrue(numeric_text.close_enough(247556, 247557))

    def test_outside_tolerance(self) -> None:
        self.assertFalse(numeric_text.close_enough(100, 200))


class IsFloatTests(unittest.TestCase):
    def test_numeric_token(self) -> None:
        self.assertTrue(numeric_text.is_float("42.5"))

    def test_non_numeric_token(self) -> None:
        self.assertFalse(numeric_text.is_float("abc"))


if __name__ == "__main__":
    unittest.main()
