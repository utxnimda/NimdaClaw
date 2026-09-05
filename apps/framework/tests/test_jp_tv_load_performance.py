from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from work_catalog_yaml.jp_tv import validate


def work():
    return {"attributes": [
        {"type": "date", "data": {"start": "20260101", "end": "20260331"}},
        {"type": "name", "data": "Example"},
        {"type": "collection-type", "data": {
            "domain": " animation ", "release_type": " tv ",
            "path": " Series ",
            "collectioned": [{"press_format": "_BDRip", "press_group": "-VCB", "press_path": "Work_BDRip\\_Disc"}],
            "markers": [" a ", "a", ""],
            "continuations": [{"title": " Part II ", "collectioned": [{"press_format": "_BDRip", "press_group": "-Jsum"}]}],
            "notes": {"nested": ["keep"]},
        }},
    ]}


class CatalogLoadPerformanceTest(unittest.TestCase):
    def test_collection_data_is_normalized_once_per_loaded_record(self) -> None:
        raw = {"works": [work(), work()]}
        with patch.object(validate, "_normalize_collection_type_dict", wraps=validate._normalize_collection_type_dict) as normalize:
            entries = validate.load_jp_tv_entries_from_yaml(raw)
        self.assertEqual(normalize.call_count, 2)
        data = validate.entry_collection_type_data(entries[0])
        self.assertEqual(data["domain"], "animation")
        self.assertEqual(data["release_type"], "tv")
        self.assertEqual(data["markers"], ["a"])
        self.assertEqual(data["collectioned"], [{"press_format": "BDRip", "press_group": "VCB", "press_path": "Work_BDRip/_Disc"}])
        self.assertEqual(data["continuations"], [{"title": "Part II", "collectioned": [{"press_format": "BDRip", "press_group": "Jsum"}]}])
        self.assertEqual(validate.entry_country_slug(entries[0]), "japan")

    def test_loaded_collection_does_not_share_nested_input_or_another_load(self) -> None:
        raw = {"works": [work()]}
        before = deepcopy(raw)
        first = validate.load_jp_tv_entries_from_yaml(raw)[0]
        second = validate.load_jp_tv_entries_from_yaml(raw)[0]
        first_data = validate.entry_collection_type_data(first)
        first_data["notes"]["nested"].append("edited")
        first_data["continuations"][0]["collectioned"].clear()
        self.assertEqual(raw, before)
        self.assertEqual(validate.entry_collection_type_data(second)["notes"], {"nested": ["keep"]})
        self.assertEqual(len(validate.entry_collection_type_data(second)["continuations"][0]["collectioned"]), 1)

    def test_invalid_paths_are_still_rejected_in_single_normalization_pass(self) -> None:
        raw_work = work()
        raw_work["attributes"][2]["data"]["collectioned"][0]["press_path"] = "../outside"
        with self.assertRaisesRegex(ValueError, "非法路径"):
            validate.load_jp_tv_entries_from_yaml({"works": [raw_work]})


if __name__ == "__main__":
    unittest.main()
