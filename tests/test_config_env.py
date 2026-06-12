from __future__ import annotations

import os
import unittest

from backend.config import _env_bool, _env_float_range, _env_int_min, _env_int_range


class _EnvVar:
    """Set an env var for the duration of a with-block, then restore it."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value
        self._prev: str | None = None

    def __enter__(self) -> None:
        self._prev = os.environ.get(self.name)
        os.environ[self.name] = self.value

    def __exit__(self, *exc) -> None:
        if self._prev is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self._prev


class EnvIntMinTests(unittest.TestCase):
    def test_uses_default_when_unset(self) -> None:
        os.environ.pop("AUGOCR_TEST_INT", None)
        self.assertEqual(_env_int_min("AUGOCR_TEST_INT", 30, 1), 30)

    def test_accepts_value_at_or_above_minimum(self) -> None:
        with _EnvVar("AUGOCR_TEST_INT", "5"):
            self.assertEqual(_env_int_min("AUGOCR_TEST_INT", 30, 1), 5)

    def test_rejects_below_minimum(self) -> None:
        with _EnvVar("AUGOCR_TEST_INT", "0"):
            with self.assertRaises(ValueError):
                _env_int_min("AUGOCR_TEST_INT", 30, 1)

    def test_rejects_non_integer(self) -> None:
        with _EnvVar("AUGOCR_TEST_INT", "abc"):
            with self.assertRaises(ValueError):
                _env_int_min("AUGOCR_TEST_INT", 30, 1)


class EnvIntRangeTests(unittest.TestCase):
    def test_accepts_value_inside_range(self) -> None:
        with _EnvVar("AUGOCR_TEST_INT_RANGE", "92"):
            self.assertEqual(_env_int_range("AUGOCR_TEST_INT_RANGE", 50, 1, 100), 92)

    def test_rejects_value_outside_range(self) -> None:
        with _EnvVar("AUGOCR_TEST_INT_RANGE", "101"):
            with self.assertRaises(ValueError):
                _env_int_range("AUGOCR_TEST_INT_RANGE", 50, 1, 100)


class EnvFloatRangeTests(unittest.TestCase):
    def test_accepts_value_inside_range(self) -> None:
        with _EnvVar("AUGOCR_TEST_FLOAT", "0.75"):
            self.assertEqual(_env_float_range("AUGOCR_TEST_FLOAT", 0.9, 0.0, 1.0), 0.75)

    def test_rejects_value_above_range(self) -> None:
        with _EnvVar("AUGOCR_TEST_FLOAT", "5.0"):
            with self.assertRaises(ValueError):
                _env_float_range("AUGOCR_TEST_FLOAT", 0.9, 0.0, 1.0)

    def test_rejects_value_below_range(self) -> None:
        with _EnvVar("AUGOCR_TEST_FLOAT", "-0.1"):
            with self.assertRaises(ValueError):
                _env_float_range("AUGOCR_TEST_FLOAT", 0.9, 0.0, 1.0)

    def test_rejects_nan(self) -> None:
        with _EnvVar("AUGOCR_TEST_FLOAT", "nan"):
            with self.assertRaises(ValueError):
                _env_float_range("AUGOCR_TEST_FLOAT", 0.9, 0.0, 1.0)


class EnvBoolTests(unittest.TestCase):
    def test_accepts_common_boolean_values(self) -> None:
        for raw, expected in [("1", True), ("true", True), ("on", True), ("0", False), ("false", False), ("off", False)]:
            with self.subTest(raw=raw):
                with _EnvVar("AUGOCR_TEST_BOOL", raw):
                    self.assertEqual(_env_bool("AUGOCR_TEST_BOOL", True), expected)

    def test_rejects_unknown_boolean(self) -> None:
        with _EnvVar("AUGOCR_TEST_BOOL", "maybe"):
            with self.assertRaises(ValueError):
                _env_bool("AUGOCR_TEST_BOOL", False)


if __name__ == "__main__":
    unittest.main()
