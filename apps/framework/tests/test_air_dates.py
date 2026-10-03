from datetime import date, datetime
import unittest

from work_catalog_yaml.jp_tv.dates import normalize_air_date
from work_catalog_yaml.jp_tv.validate import CatalogAttribute, JpTvEntry, attribute_to_plain, entry_air_dates


class AirDateTest(unittest.TestCase):
    def test_compact_iso_and_legacy_separator_formats(self):
        for value in ("20240209", 20240209, " 2024-02-09 ", "2024/2/9", date(2024, 2, 9)):
            with self.subTest(value=value):
                self.assertEqual(normalize_air_date(value, validate_calendar=True), "20240209")

    def test_blank_and_unknown_components_are_not_filled(self):
        for value, expected in (("", ""), (None, ""), ("00000000", "00000000"),
                                ("2012-00-00", "20120000"), ("2012-04-00", "20120400"),
                                ("200x-xx-xx", "200XXXXX")):
            self.assertEqual(normalize_air_date(value, validate_calendar=True), expected)

    def test_malformed_or_ambiguous_values_are_rejected(self):
        for value in (True, False, 20240101.0, "2024", "2024-01", "1/2/2024", "2024-1/2",
                      "2024-01-02T00:00:00", datetime(2024, 1, 2), "today", "2024011"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_air_date(value)

    def test_calendar_checks_are_explicit_and_keep_legacy_reads_available(self):
        for value in ("20230229", "20240431", "20241300", "20240132"):
            self.assertEqual(normalize_air_date(value), value)
            with self.assertRaises(ValueError):
                normalize_air_date(value, validate_calendar=True)
        self.assertEqual(normalize_air_date("2024-02-29", validate_calendar=True), "20240229")

    def test_read_and_generic_export_keep_storage_contract(self):
        attribute = CatalogAttribute("date", {"start": "2024-01-02", "end": "2024/3/4"})
        self.assertEqual(entry_air_dates(JpTvEntry([attribute])), ("20240102", "20240304"))
        self.assertEqual(attribute_to_plain(attribute)["data"], {"start": "20240102", "end": "20240304"})
        self.assertEqual(attribute.data["start"], "2024-01-02")


if __name__ == "__main__":
    unittest.main()
