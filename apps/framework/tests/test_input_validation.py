from __future__ import annotations

import sys
import unittest

from work_catalog_yaml.input_validation import parse_record_index


class RecordIndexValidationTest(unittest.TestCase):
    def test_accepts_integer_and_decimal_string_identities(self) -> None:
        for raw, expected in ((0, 0), (12, 12), (" 12 ", 12), ("0012", 12), (sys.maxsize, sys.maxsize)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_record_index(raw), expected)

    def test_rejects_coercion_and_out_of_range_identities(self) -> None:
        for raw in (True, False, 0.0, 0.5, 1.9, float("inf"), None, [], {}, -1, "-1", "0.0", "+1", "1e0", "0x1", "１２", "", sys.maxsize + 1, "9" * 5000):
            with self.subTest(raw=str(raw)[:30]), self.assertRaisesRegex(ValueError, "record.row 非法"):
                parse_record_index(raw, label="record.row")


if __name__ == "__main__":
    unittest.main()
