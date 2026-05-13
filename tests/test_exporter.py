from __future__ import annotations

import csv
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.exporter import _stringify


class TestCSVInjectionProtection(unittest.TestCase):
    def test_formula_equals_prefixed(self) -> None:
        self.assertEqual(_stringify("=1+1"), "'=1+1")
        self.assertEqual(_stringify("=SUM(A1:B10)"), "'=SUM(A1:B10)")
        self.assertEqual(_stringify("=CMD|'/C calc'"), "'=CMD|'/C calc'")

    def test_formula_plus_prefixed(self) -> None:
        self.assertEqual(_stringify("+1+1"), "'+1+1")
        self.assertEqual(_stringify("+CMD|'/C calc'"), "'+CMD|'/C calc'")

    def test_formula_minus_prefixed(self) -> None:
        self.assertEqual(_stringify("-1-1"), "'-1-1")
        self.assertEqual(_stringify("-CMD|'/C calc'"), "'-CMD|'/C calc'")

    def test_formula_at_prefixed(self) -> None:
        self.assertEqual(_stringify("@1+1"), "'@1+1")
        self.assertEqual(_stringify("@SUM(A1)"), "'@SUM(A1)")

    def test_control_char_tab_prefixed(self) -> None:
        self.assertEqual(_stringify("\tsome value"), "'\tsome value")

    def test_control_char_carriage_return_prefixed(self) -> None:
        self.assertEqual(_stringify("\rsome value"), "'\rsome value")

    def test_control_char_newline_prefixed(self) -> None:
        self.assertEqual(_stringify("\nsome value"), "'\nsome value")

    def test_normal_string_not_prefixed(self) -> None:
        self.assertEqual(_stringify("normal text"), "normal text")
        self.assertEqual(_stringify("invoice 12345"), "invoice 12345")
        self.assertEqual(_stringify("Vendor Name"), "Vendor Name")

    def test_none_returns_empty(self) -> None:
        self.assertEqual(_stringify(None), "")

    def test_bool_returns_true_false(self) -> None:
        self.assertEqual(_stringify(True), "true")
        self.assertEqual(_stringify(False), "false")

    def test_list_returns_str(self) -> None:
        self.assertEqual(_stringify(["a", "b"]), "['a', 'b']")

    def test_dict_returns_str(self) -> None:
        self.assertEqual(_stringify({"key": "value"}), "{'key': 'value'}")

    def test_negative_numbers_not_prefixed(self) -> None:
        self.assertEqual(_stringify(123), "123")
        self.assertEqual(_stringify(0), "0")
        self.assertEqual(_stringify(-5), "-5")
        self.assertEqual(_stringify(-123), "-123")
        self.assertEqual(_stringify(-99.99), "-99.99")

    def test_formula_minus_with_operators_prefixed(self) -> None:
        self.assertEqual(_stringify("-1+1"), "'-1+1")
        self.assertEqual(_stringify("-1-2"), "'-1-2")
        self.assertEqual(_stringify("-5*10"), "'-5*10")

    def test_leading_space_preserved(self) -> None:
        self.assertEqual(_stringify("  leading space"), "  leading space")

    def test_embedded_equals_not_prefixed(self) -> None:
        self.assertEqual(_stringify("price = $10"), "price = $10")

    def test_csv_output_safe(self) -> None:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([_stringify("=1+1"), _stringify("normal"), _stringify("+cmd")])
        csv_line = output.getvalue()
        self.assertIn("'=1+1", csv_line)
        self.assertIn("normal", csv_line)
        self.assertIn("'+cmd", csv_line)


if __name__ == "__main__":
    unittest.main()