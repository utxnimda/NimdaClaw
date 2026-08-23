from __future__ import annotations

import unittest
from types import SimpleNamespace

from media_directory_organizer.inference import (
    infer_source_press,
    safe_windows_component,
    suggest_press_paths,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        format_markers={
            "BDRip": ("bdrip", "blu-ray", "bd 1920x1080"),
            "DVDRip": ("dvdrip", "dvd rip"),
            "2160p": ("2160p", "uhd"),
            "1080p": ("1080p",),
            "720p": ("720p",),
        },
        group_markers={
            "VCB": ("vcb-studio", "vcb studio", "vcb"),
            "JSUM": ("jsum",),
            "MW": ("mawen1250", "mawen"),
        },
        group_suffixes={"VCB": "VCB", "JSUM": "Jsum", "MW": "MW", "VCBM": "VCBM"},
    )


def _registry() -> dict[str, object]:
    return {
        "release_groups": [
            {"code": "VCB", "kind": "release", "names": ["VCB-Studio"], "aliases": []},
            {"code": "JSUM", "kind": "release", "names": ["Jsum"], "aliases": []},
        ],
        "translation_groups": [
            {"code": "NK", "kind": "translation", "names": ["Nekomoe kissaten"], "aliases": []},
            {"code": "AI", "kind": "translation", "names": [], "aliases": []},
        ],
        "combinations": [
            {"code": "VCBM", "kind": "combination", "members": ["VCB", "NK"]},
        ],
        "catalog_only_groups": [],
    }


class SourcePressInferenceTest(unittest.TestCase):
    def test_optical_format_outranks_resolution_and_jsum_signature_requires_confirmation(self) -> None:
        result = infer_source_press(
            "[2017][Fate Apocrypha][BDRIP][1080P][1-25Fin+SP]",
            settings=_settings(),
            group_registry=_registry(),
        )

        self.assertEqual(result["suggested_press_format"], "BDRip")
        self.assertEqual(result["format_candidates"][0]["value"], "BDRip")
        self.assertIn("1080p", [row["value"] for row in result["format_candidates"]])
        self.assertEqual(result["suggested_press_group"], "JSUM")
        self.assertEqual(result["group_confidence"], "medium")
        self.assertTrue(result["needs_confirmation"])

    def test_multiple_real_media_formats_remain_ambiguous(self) -> None:
        result = infer_source_press(
            "[Title][BDRIP][DVDRIP][1080P]",
            settings=_settings(),
        )

        self.assertEqual(result["suggested_press_format"], "")
        self.assertEqual(
            {row["value"] for row in result["format_candidates"] if row["kind"] == "media-format"},
            {"BDRip", "DVDRip"},
        )
        self.assertTrue(result["needs_confirmation"])

    def test_vcb_studio_outweighs_mawen_uploader(self) -> None:
        result = infer_source_press(
            "Title [BD 1920x1080 AVC FLAC] - mawen1250&VCB-Studio",
            settings=_settings(),
            group_registry=_registry(),
        )

        self.assertEqual(result["suggested_press_format"], "BDRip")
        self.assertEqual(result["suggested_press_group"], "VCB")
        candidates = {row["value"]: row for row in result["group_candidates"]}
        self.assertGreater(candidates["VCB"]["score"], candidates["MW"]["score"])
        self.assertEqual(candidates["MW"]["kind"], "uploader")

    def test_registry_direct_code_and_exact_members_find_combination(self) -> None:
        direct = infer_source_press(
            "Release [VCBM]",
            settings=_settings(),
            group_registry=_registry(),
        )
        members = infer_source_press(
            "Release by VCB-Studio + Nekomoe kissaten",
            settings=_settings(),
            group_registry=_registry(),
        )

        self.assertEqual(direct["suggested_press_group"], "VCBM")
        self.assertEqual(members["suggested_press_group"], "VCBM")
        self.assertEqual(direct["group_confidence"], "high")
        self.assertEqual(members["group_confidence"], "high")

    def test_short_registry_code_does_not_match_inside_a_word(self) -> None:
        result = infer_source_press(
            "Kobayashi-san Chi no Maid Dragon",
            settings=_settings(),
            group_registry=_registry(),
        )

        self.assertNotIn("AI", [row["value"] for row in result["group_candidates"]])


class PressPathSuggestionTest(unittest.TestCase):
    def test_windows_component_uses_readable_fullwidth_replacements(self) -> None:
        self.assertEqual(
            safe_windows_component('Fate/Apocrypha: A? <B>|C*"D"'),
            "Fate／Apocrypha： A？ ＜B＞｜C＊＂D＂",
        )
        self.assertEqual(safe_windows_component("CON"), "_CON")

    def test_single_jsum_fate_path_has_no_group_suffix(self) -> None:
        rows = suggest_press_paths(
            "Fate/Apocrypha",
            [
                {
                    "source_names": ["incoming"],
                    "press_format": "BDRip",
                    "press_group": "JSUM",
                    "press_path": "",
                }
            ],
            settings=_settings(),
            root_name="Fate／Apocrypha",
        )

        self.assertEqual(rows[0]["press_path"], "Fate／Apocrypha_BDRip")
        self.assertEqual(rows[0]["suggested_press_path"], "Fate／Apocrypha_BDRip")
        self.assertEqual(rows[0]["source_names"], ["incoming"])

    def test_different_groups_with_same_format_each_receive_suffix(self) -> None:
        rows = suggest_press_paths(
            "Little Busters! EX",
            [
                {"source_names": ["jsum"], "press_format": "BDRip", "press_group": "JSUM"},
                {"source_names": ["vcb"], "press_format": "BDRip", "press_group": "VCB"},
            ],
            settings=_settings(),
        )

        self.assertEqual(rows[0]["press_path"], "Little Busters! EX_BDRip(Jsum)")
        self.assertEqual(rows[1]["press_path"], "Little Busters! EX_BDRip(VCB)")

    def test_duplicate_group_does_not_force_suffix(self) -> None:
        rows = suggest_press_paths(
            "Work",
            [
                {"source_names": ["a"], "press_format": "BDRip", "press_group": "JSUM"},
                {"source_names": ["b"], "press_format": "BDRip", "press_group": "jsum"},
            ],
            settings=_settings(),
        )

        self.assertEqual([row["press_path"] for row in rows], ["Work_BDRip", "Work_BDRip"])

    def test_manual_path_is_preserved_but_new_suggestion_is_returned(self) -> None:
        original = {
            "source_names": ["incoming"],
            "press_format": "BDRip",
            "press_group": "JSUM",
            "press_path": "My Reviewed Target",
            "suggested_press_path": "Old Work_BDRip",
        }
        rows = suggest_press_paths(
            "New Work",
            [original],
            settings=_settings(),
        )

        self.assertEqual(rows[0]["press_path"], "My Reviewed Target")
        self.assertEqual(rows[0]["suggested_press_path"], "New Work_BDRip")
        self.assertTrue(rows[0]["press_path_is_manual"])
        self.assertTrue(rows[0]["press_path_preserved"])
        self.assertEqual(original["suggested_press_path"], "Old Work_BDRip")
        self.assertEqual(original["source_names"], ["incoming"])

    def test_previous_machine_suggestion_updates_when_work_name_changes(self) -> None:
        rows = suggest_press_paths(
            "New Work",
            [
                {
                    "source_names": ["incoming"],
                    "press_format": "BDRip",
                    "press_group": "JSUM",
                    "press_path": "Old Work_BDRip",
                    "suggested_press_path": "Old Work_BDRip",
                }
            ],
            settings=_settings(),
        )

        self.assertEqual(rows[0]["press_path"], "New Work_BDRip")
        self.assertFalse(rows[0]["press_path_is_manual"])
        self.assertFalse(rows[0]["press_path_preserved"])


if __name__ == "__main__":
    unittest.main()
