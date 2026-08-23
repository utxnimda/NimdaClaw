from __future__ import annotations

import copy
import unittest
from datetime import datetime
from pathlib import Path

from work_catalog_yaml.media_groups import (
    build_media_group_registry,
    default_media_group_note_path,
    default_media_group_registry_path,
    load_media_group_registry,
    media_group_classifier_family,
    validate_media_group_registry,
)


SECTIONS = (
    "release_groups",
    "translation_groups",
    "combinations",
    "catalog_only_groups",
)


def _rows_by_code(registry: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(row["code"]): row
        for section in SECTIONS
        for row in registry[section]  # type: ignore[index, union-attr]
    }


class MediaGroupRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry_path = default_media_group_registry_path()
        cls.registry = load_media_group_registry(cls.registry_path)
        cls.rows = _rows_by_code(cls.registry)

    def test_generated_registry_is_current_and_has_242_globally_unique_codes(self) -> None:
        generated_at = datetime.fromisoformat(str(self.registry["generated_at"]))
        rebuilt = build_media_group_registry(generated_at=generated_at)
        self.assertEqual(self.registry, rebuilt)

        section_codes = [
            str(row["code"])
            for section in SECTIONS
            for row in self.registry[section]
        ]
        self.assertEqual(len(section_codes), 242)
        self.assertEqual(len(set(section_codes)), 242)
        self.assertEqual(self.registry["press_group_codes"], sorted(section_codes))
        self.assertEqual(
            self.registry["statistics"],
            {
                "release_group_count": 31,
                "translation_group_count": 94,
                "combination_count": 101,
                "catalog_only_group_count": 16,
                "press_group_code_count": 242,
                "observed_catalog_code_count": 143,
            },
        )

    def test_note_combinations_have_two_to_four_known_members(self) -> None:
        combinations = self.registry["combinations"]
        member_counts = [len(row["members"]) for row in combinations]
        self.assertEqual(len(combinations), 101)
        self.assertTrue(all(2 <= count <= 4 for count in member_counts))
        self.assertEqual(max(member_counts), 4)
        self.assertEqual({
            count: member_counts.count(count)
            for count in sorted(set(member_counts))
        }, {2: 76, 3: 23, 4: 2})
        self.assertTrue(all(not row["unknown_members"] for row in combinations))

    def test_jsum_and_vcb_combinations_have_the_expected_classifier_family(self) -> None:
        expected = {
            "JSUM": "JSUM",
            "VCB": "VCB",
            "VA": "VCB",
            "VAF": "VCB",
            "VAL": "VCB",
            "VTL": "VCB",
            "VCDM": "VCB",
        }
        for code, family in expected.items():
            with self.subTest(code=code):
                self.assertEqual(self.rows[code]["classifier_family"], family)
                self.assertEqual(
                    media_group_classifier_family(code, self.registry_path),
                    family,
                )

    def test_historic_member_corrections_keep_raw_source_and_review_record(self) -> None:
        expected = {
            "FLTG": {
                "members": ["FL", "TGTD"],
                "raw_members": ["FL", "TGTDS"],
                "corrections": [{"from": "TGTDS", "to": "TGTD"}],
            },
            "VCBE": {
                "members": ["VCB", "EP"],
                "raw_members": ["VCP", "EP"],
                "corrections": [{"from": "VCP", "to": "VCB"}],
            },
        }
        note_lines = default_media_group_note_path().read_text(encoding="utf-8").splitlines()
        review_by_code = {
            str(row["code"]): row
            for row in self.registry["review_required"]
        }

        for code, values in expected.items():
            with self.subTest(code=code):
                row = self.rows[code]
                self.assertEqual(row["members"], values["members"])
                self.assertEqual(row["raw_members"], values["raw_members"])
                self.assertEqual(row["corrections"], values["corrections"])
                self.assertEqual(len(row["sources"]), 1)
                source = row["sources"][0]
                self.assertEqual(source["file"], "data/source/Animation/Note.h")
                self.assertEqual(
                    source["raw"],
                    note_lines[int(source["line"]) - 1].strip(),
                )
                self.assertIn(code, review_by_code)
                self.assertEqual(
                    review_by_code[code]["corrections"],
                    values["corrections"],
                )
                self.assertEqual(review_by_code[code]["sources"], row["sources"])

    def test_validation_rejects_a_duplicate_code_list_entry(self) -> None:
        invalid = copy.deepcopy(self.registry)
        invalid["press_group_codes"].append(invalid["press_group_codes"][0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_media_group_registry(invalid)

    def test_validation_rejects_unknown_or_recursive_combination_members(self) -> None:
        invalid = copy.deepcopy(self.registry)
        invalid["combinations"][0]["members"][0] = "ZZNOTKNOWN"
        with self.assertRaisesRegex(ValueError, "undefined or non-atomic"):
            validate_media_group_registry(invalid)


if __name__ == "__main__":
    unittest.main()
