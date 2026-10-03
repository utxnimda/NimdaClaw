from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collection_detail.catalog_bindings import (
    catalog_work_matches_target_name,
    plan_catalog_bindings,
    split_explicit_title_year,
)


def _work(name: str = "Example", *, index: int = 0, year: str = "2005", path: str = "", presses=None) -> dict:
    return {
        "yaml_source_rel": f"works-{year}.yaml", "index_in_file": index,
        "work_key": f"works-{year}.yaml#{index}", "name": name, "country": "japan",
        "domain": "animation", "release_type": "tv", "begin_date": year + "0101", "end_date": year + "0331",
        "path": path, "press": presses if presses is not None else [{
            "press_key": "0:main::BDRip:VCB", "press_format": "BDRip", "press_group": "VCB", "press_path": "",
        }], "unknown": {"keep": [1, 2]},
    }


def _item(work: dict, target: Path | str, *, position: int = 0, **overrides) -> dict:
    press = work["press"][position]
    fields = ("yaml_source_rel", "index_in_file", "work_key", "name", "country", "domain", "release_type", "begin_date", "end_date")
    return {**{key: work[key] for key in fields}, **deepcopy(press), "entry_key": f"entry-{work['name']}-{position}",
            "target_path": str(target), "target_source": "index_db_previous", **overrides}


class CatalogBindingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.media = self.base / "media"
        self.root = self.media / "Example"
        self.target = self.root / "Example_BDRip"
        self.target.mkdir(parents=True)

    def plan(self, works, items, **kwargs):
        return plan_catalog_bindings(works, items, resource_roots=kwargs.pop("resource_roots", [self.media]), **kwargs)

    def assertIssue(self, plan, code, *, blocking=True):
        found = [row for row in plan["issues"] if row["code"] == code]
        self.assertTrue(found, plan)
        self.assertEqual(found[0]["blocking"], blocking)
        self.assertEqual(found[0]["message"], found[0]["error"])

    def test_missing_bindings_generate_writer_compatible_additions_without_mutation_or_io_writes(self):
        work = _work()
        item = _item(work, self.target)
        original = deepcopy([work, item])
        with patch("builtins.open", side_effect=AssertionError("planner cannot open/write files")):
            out = self.plan([work], [item])
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["mappings"], [{"yaml_source_rel": "works-2005.yaml", "index_in_file": 0,
            "path": str(self.root), "press": [{"press_key": work["press"][0]["press_key"], "press_path": "Example_BDRip"}]}])
        self.assertEqual([work, item], original)
        self.assertEqual(out["summary"]["mapped_press_count"], 1)

    def test_all_identity_fields_must_match_even_when_directory_exists(self):
        work = _work()
        for key, bad in (("name", "Other"), ("country", "korea"), ("domain", "television"),
                         ("release_type", "movie"), ("begin_date", "20060101"), ("end_date", "20051231"),
                         ("press_format", "1080p"), ("press_group", "JSUM")):
            with self.subTest(field=key):
                out = self.plan([work], [_item(work, self.target, **{key: bad})])
                self.assertEqual(out["mappings"], [])
                self.assertIssue(out, "index-identity-mismatch")

    def test_file_index_and_press_key_do_not_fall_back_to_name_or_position(self):
        work = _work()
        for override in ({"yaml_source_rel": "other.yaml"}, {"index_in_file": 1}, {"index_in_file": True},
                         {"index_in_file": -1}, {"yaml_source_rel": "../works-2005.yaml"}, {"press_key": "1:main::BDRip:VCB"}):
            with self.subTest(override=override):
                self.assertEqual(self.plan([work], [_item(work, self.target, **override)])["mappings"], [])

    def test_duplicate_index_or_duplicate_work_identity_is_ambiguous(self):
        work = _work(); item = _item(work, self.target)
        out = self.plan([work], [item, deepcopy(item)])
        self.assertEqual(out["mappings"], [])
        self.assertIssue(out, "index-ambiguous")
        out = self.plan([work, deepcopy(work)], [item])
        self.assertEqual(out["mappings"], [])
        self.assertIssue(out, "work-identity-ambiguous")

    def test_complete_valid_catalog_is_unchanged_and_conflicting_index_is_reported(self):
        work = _work(path=str(self.root))
        work["press"][0]["press_path"] = self.target.name
        out = self.plan([work], [_item(work, self.target)])
        self.assertEqual(out["mappings"], [])
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["summary"]["unchanged_press_count"], 1)
        other = self.root / "Other_BDRip"; other.mkdir()
        out = self.plan([work], [_item(work, other)])
        self.assertEqual(out["mappings"], [])
        self.assertIssue(out, "catalog-target-conflict")

    def test_existing_root_is_preserved_and_only_missing_press_fields_are_sent(self):
        second = {"press_key": "1:main::DVDRip:JSUM", "press_format": "DVDRip", "press_group": "JSUM", "press_path": ""}
        work = _work(path=str(self.root)); work["press"][0]["press_path"] = self.target.name
        work["press"].append(second)
        dvd = self.root / "Example_DVDRip"; dvd.mkdir()
        out = self.plan([work], [_item(work, self.target), _item(work, dvd, position=1)])
        self.assertEqual(out["mappings"][0]["path"], str(self.root))
        self.assertEqual(out["mappings"][0]["press"], [{"press_key": second["press_key"], "press_path": dvd.name}])

    def test_incorrect_nonempty_catalog_fields_are_not_overwritten(self):
        work = _work(path=str(self.media / "Missing"))
        out = self.plan([work], [_item(work, self.target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "catalog-work-path-invalid")
        for press_path in ("../Example_BDRip", "Missing_BDRip", str(self.target)):
            with self.subTest(path=press_path):
                work = _work(path=str(self.root)); work["press"][0]["press_path"] = press_path
                out = self.plan([work], [_item(work, self.target)])
                self.assertEqual(out["mappings"], []); self.assertIssue(out, "catalog-press-path-invalid")

    def test_existing_press_path_without_root_must_agree_with_inferred_root(self):
        work = _work(); work["press"][0]["press_path"] = self.target.name
        out = self.plan([work], [_item(work, self.target)])
        self.assertEqual(out["mappings"][0]["press"], [])
        self.assertEqual(out["mappings"][0]["path"], str(self.root))
        work["press"][0]["press_path"] = "Different_BDRip"
        out = self.plan([work], [_item(work, self.target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "catalog-press-path-conflict")

    def test_shortcut_target_must_agree_with_index_when_provided(self):
        work = _work(); item = _item(work, self.target)
        other = self.root / "Example_DVDRip"; other.mkdir()
        out = self.plan([work], [item], shortcut_targets={item["entry_key"]: {"target_path": str(other), "shortcut_path": "fixture.lnk"}})
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "shortcut-target-conflict")
        out = self.plan([work], [item], shortcut_targets={item["entry_key"]: {"target_path": str(self.target)}})
        self.assertEqual(len(out["mappings"]), 1)

    def test_no_target_and_missing_directory_remain_unresolved(self):
        work = _work()
        for items, code in (([], "index-missing"), ([_item(work, "")], "index-target-empty"),
                            ([_item(work, self.root / "Missing_BDRip")], "index-target-missing")):
            out = self.plan([work], items)
            self.assertEqual(out["mappings"], []); self.assertIssue(out, code, blocking=False)

    def test_target_must_be_below_a_resource_root(self):
        work = _work(); outside = self.base / "outside" / "Example_BDRip"; outside.mkdir(parents=True)
        for target in (outside, self.media):
            out = self.plan([work], [_item(work, target)])
            self.assertEqual(out["mappings"], []); self.assertIssue(out, "index-target-unsafe")

    def test_any_reparse_ancestor_is_rejected_before_resolution(self):
        work = _work(); original = Path.lstat
        def patched(path, *args, **kwargs):
            if path == self.root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", patched):
            out = self.plan([work], [_item(work, self.target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "index-target-unsafe")

    def test_resource_root_and_press_directory_cannot_be_guessed_as_work_root(self):
        work = _work()
        target = self.media / "Example_BDRip"; target.mkdir()
        out = self.plan([work], [_item(work, target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "work-root-unsafe")
        nested = target / "Example_BDRip"; nested.mkdir()
        out = self.plan([work], [_item(work, nested)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "work-root-unsafe")

    def test_different_parent_targets_cannot_infer_a_higher_collection_root(self):
        work = _work(); work["press"].append({"press_key": "1:main::DVDRip:VCB", "press_format": "DVDRip", "press_group": "VCB", "press_path": ""})
        other = self.media / "AnotherCollection" / "Example_DVDRip"; other.mkdir(parents=True)
        out = self.plan([work], [_item(work, self.target), _item(work, other, position=1)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "work-root-unsafe")

    def test_missing_root_requires_every_press_target_before_inferring(self):
        work = _work(); work["press"].append({"press_key": "1:main::DVD:", "press_format": "DVD", "press_group": "", "press_path": ""})
        out = self.plan([work], [_item(work, self.target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "work-root-unresolved", blocking=False)

    def test_continuation_identity_and_same_parent_are_preserved(self):
        work = _work(); continuation = {"press_key": "1:continuation:0:DVDRip:JSUM", "press_format": "DVDRip", "press_group": "JSUM", "press_path": "", "segment": "continuation", "continuation_index": 0, "continuation_title": "Bonus"}
        work["press"].append(continuation)
        dvd = self.root / "Example_DVDRip"; dvd.mkdir()
        out = self.plan([work], [_item(work, self.target), _item(work, dvd, position=1)])
        self.assertEqual(out["mappings"][0]["press"][1]["press_key"], continuation["press_key"])
        self.assertEqual(len(out["mappings"][0]["press"]), 2)

    def test_format_only_resource_candidate_requires_confirmation(self):
        work = _work()
        out = self.plan([work], [_item(work, self.target, target_source="resource_format_fallback")])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "index-target-format-only", blocking=False)
        self.assertEqual(len(self.plan([work], [_item(work, self.target, target_source="resource_exact_press")])["mappings"]), 1)

    def test_wrong_work_folder_is_blocked_even_if_index_identity_and_shortcut_agree(self):
        work = _work("Fate／Stay Night Unlimited Blade Works 00-12", year="2014")
        item = _item(work, self.target)
        out = self.plan([work], [item], shortcut_targets={item["entry_key"]: {"target_path": str(self.target)}})
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "target-name-needs-confirmation")

    def test_parent_name_cannot_hide_wrong_named_target_under_existing_root(self):
        work = _work(path=str(self.root)); other = self.root / "Different Season_BDRip"; other.mkdir()
        out = self.plan([work], [_item(work, other)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "target-name-needs-confirmation")
        pure = self.root / "BDRip"; pure.mkdir()
        self.assertEqual(len(self.plan([work], [_item(work, pure)])["mappings"]), 1)

    def test_two_explicit_disjoint_broadcast_ranges_can_share_one_target_across_years(self):
        target = self.media / "Fate Zero" / "Fate Zero_BDRip"; target.mkdir(parents=True)
        first = _work("Fate／Zero 01-12", year="2011")
        second = _work("Fate / Zero 13-24", year="2012")
        out = self.plan([first, second], [_item(first, target), _item(second, target)])
        self.assertEqual(out["issues"], [])
        self.assertEqual(len(out["mappings"]), 2)
        self.assertEqual(out["mappings"][0]["path"], out["mappings"][1]["path"])

    def test_different_titles_or_unqualified_remakes_sharing_target_are_not_automatically_bound(self):
        first = _work(); second = _work("Other", index=1)
        out = self.plan([first, second], [_item(first, self.target), _item(second, self.target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "shared-target-needs-confirmation")
        second = _work(year="2022")
        out = self.plan([first, second], [_item(first, self.target), _item(second, self.target)])
        self.assertEqual(out["mappings"], []); self.assertIssue(out, "shared-target-needs-confirmation")

    def test_year_and_season_digits_are_never_removed_for_fuzzy_name_matching(self):
        for name in ("Example 2022", "Example Season 2", "Example 2011-2012"):
            work = _work(name)
            out = self.plan([work], [_item(work, self.target)])
            self.assertEqual(out["mappings"], []); self.assertIssue(out, "target-name-needs-confirmation")

    def test_offline_root_does_not_block_an_empty_target_plan(self):
        work = _work()
        out = self.plan([work], [_item(work, "")], resource_roots=[self.base / "Offline"])
        self.assertEqual(out["mappings"], [])
        self.assertEqual(out["summary"]["blocking_issue_count"], 0)

    def test_distinct_press_groups_sharing_unbound_target_require_confirmation(self):
        work = _work()
        work["press"].append({"press_key": "1:main::BDRip:JSUM", "press_format": "BDRip", "press_group": "JSUM", "press_path": ""})
        items = [_item(work, self.target), _item(work, self.target, position=1)]
        out = self.plan([work], items)
        self.assertEqual(out["mappings"], [])
        self.assertIssue(out, "shared-press-target-needs-confirmation")
        work["path"] = str(self.root)
        for press in work["press"]:
            press["press_path"] = self.target.name
        out = self.plan([work], [_item(work, self.target), _item(work, self.target, position=1)])
        self.assertEqual(out["mappings"], [])
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["summary"]["unchanged_press_count"], 2)

    def test_existing_canonical_shared_target_across_titles_remains_authoritative(self):
        first = _work(path=str(self.root)); second = _work("Other", index=1, path=str(self.root))
        first["press"][0]["press_path"] = self.target.name
        second["press"][0]["press_path"] = self.target.name
        out = self.plan([first, second], [_item(first, self.target), _item(second, self.target)])
        self.assertEqual(out["mappings"], [])
        self.assertEqual(out["issues"], [])

    def test_episode_ranges_do_not_require_a_separator_before_the_first_digit(self):
        target = self.media / "戦勇" / "戦勇_BDRip"; target.mkdir(parents=True)
        first = _work("戦勇。01-13", year="2013")
        second = _work("戦勇。14-26", year="2014")
        out = self.plan([first, second], [_item(first, target), _item(second, target)])
        self.assertEqual(out["issues"], [])
        self.assertEqual(len(out["mappings"]), 2)

    def test_a_series_family_root_need_not_equal_the_complete_work_name(self):
        target = self.media / "Macross" / "超時空要塞 Macross_BDRip"; target.mkdir(parents=True)
        work = _work("超時空要塞 Macross")
        out = self.plan([work], [_item(work, target)])
        self.assertEqual(out["issues"], [])
        self.assertEqual(out["mappings"][0]["path"], str(target.parent))

    def test_explicit_remake_year_qualifier_requires_the_matching_broadcast_year(self):
        new_work = _work("うる星やつら", year="2022")
        old_work = _work("うる星やつら", year="1981")
        for suffix in (" 2022", "_2022", "(2022)", " [2022]", "（２０２２）"):
            with self.subTest(suffix=suffix):
                target = self.media / "うる星やつら" / ("うる星やつら" + suffix + "_BDRip")
                target.mkdir(parents=True)
                self.assertTrue(catalog_work_matches_target_name(new_work, target, "BDRip"))
                self.assertFalse(catalog_work_matches_target_name(old_work, target, "BDRip"))
                plan = self.plan([new_work], [_item(new_work, target)])
                self.assertEqual(plan["issues"], [])
                self.assertEqual(plan["mappings"][0]["press"][0]["press_path"], target.name)
                plan = self.plan([old_work], [_item(old_work, target)])
                self.assertEqual(plan["mappings"], [])
                self.assertIssue(plan, "target-name-needs-confirmation")

    def test_year_syntax_helper_never_splits_unseparated_or_embedded_digits(self):
        for title in ("Space1999", "2001", "作品2022", "作品2022続編", "作品 2022 第二季", "作品 20221"):
            with self.subTest(title=title):
                self.assertEqual(split_explicit_title_year(title), (title, ""))
        self.assertEqual(split_explicit_title_year("作品 (２０２２)"), ("作品", "2022"))
        self.assertEqual(split_explicit_title_year("作品 [2022]"), ("作品", "2022"))

    def test_catalog_title_year_is_literal_even_when_broadcast_year_differs(self):
        for title in ("Space1999", "Space 1999", "Haruhi 2009", "2001", "2001: A Space Odyssey"):
            with self.subTest(title=title):
                work = _work(title, year="2099")
                target = self.media / "Family" / (title + "_BDRip")
                self.assertTrue(catalog_work_matches_target_name(work, target, "BDRip"))
                self.assertFalse(catalog_work_matches_target_name(work, self.media / "Space 2099_BDRip", "BDRip"))
        work = _work("Space 1999", year="2022")
        self.assertTrue(catalog_work_matches_target_name(work, self.media / "Space 1999 (2022)_BDRip", "BDRip"))

    def test_year_qualifier_does_not_hide_season_numbers_or_unknown_dates(self):
        work = _work("Example Season 2", year="2022")
        self.assertFalse(catalog_work_matches_target_name(work, self.media / "Example (2022)_BDRip", "BDRip"))
        self.assertFalse(catalog_work_matches_target_name(work, self.media / "Example Season 3 (2022)_BDRip", "BDRip"))
        self.assertTrue(catalog_work_matches_target_name(work, self.media / "Example Season 2 (2022)_BDRip", "BDRip"))
        for unknown in ("", "20XX0101", "00000101"):
            work["begin_date"] = unknown
            self.assertFalse(catalog_work_matches_target_name(work, self.media / "Example Season 2 (2022)_BDRip", "BDRip"))

    def test_year_qualified_parent_is_only_used_for_pure_press_directory(self):
        work = _work("うる星やつら", year="2022")
        self.assertTrue(catalog_work_matches_target_name(work, self.media / "うる星やつら (2022)" / "BDRip", "BDRip"))
        self.assertFalse(catalog_work_matches_target_name(work, self.media / "うる星やつら (2022)" / "Other_BDRip", "BDRip"))

    def test_year_qualified_target_does_not_bypass_shared_year_or_press_group_checks(self):
        target = self.media / "うる星やつら" / "うる星やつら 2022_BDRip"
        target.mkdir(parents=True)
        old = _work("うる星やつら", year="1981")
        new = _work("うる星やつら", year="2022")
        plan = self.plan([new], [_item(new, target), _item(old, target)])
        self.assertEqual(plan["mappings"], [])
        self.assertIssue(plan, "shared-target-needs-confirmation")
        new["press"].append({"press_key": "1:main::BDRip:JSUM", "press_format": "BDRip", "press_group": "JSUM", "press_path": ""})
        plan = self.plan([new], [_item(new, target), _item(new, target, position=1)])
        self.assertEqual(plan["mappings"], [])
        self.assertIssue(plan, "shared-press-target-needs-confirmation")


if __name__ == "__main__":
    unittest.main()
