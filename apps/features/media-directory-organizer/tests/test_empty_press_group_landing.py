from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from collection_detail.save import preview_catalog_work_append
from media_directory_organizer.landing import (
    _merged_update_patch,
    _normalize_shared_target_bindings,
    apply_work_landing,
    preview_work_landing,
)
from media_directory_organizer.landing_drafts import _normalize_draft
from media_directory_organizer.suggestions import _draft_mapping, _local_proposed_work
from media_directory_organizer.web import _source_press_overrides_from_body
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import entry_collection_type_data

from test_media_work_landing import _LandingFixture, _shortcut_sandbox, _tree_snapshot
from test_shared_press_target_landing import (
    FIRST_WORK,
    SECOND_WORK,
    _SharedTargetFixture,
    _catalog_yaml,
)


class EmptyPressGroupLandingTest(unittest.TestCase):
    def test_existing_placeholder_matches_empty_draft_without_rewriting_catalog(self) -> None:
        for placeholder in ("----", "---"):
            with self.subTest(placeholder=placeholder), tempfile.TemporaryDirectory() as temp:
                fixture = _LandingFixture(Path(temp))
                catalog_file = fixture.catalog_root / "[JP][TVInfo][2011].yaml"
                target_name = "Fate／Zero_BDRip"
                catalog_file.write_text(
                    _catalog_yaml(name=FIRST_WORK, start="20111001", end="20111224",
                                  root=fixture.work_root, press_path=target_name)
                    .replace("press_group: VCB", f"press_group: '{placeholder}'"),
                    encoding="utf-8",
                )
                before = catalog_file.read_bytes()
                draft = {
                    "name": FIRST_WORK,
                    "date": {"start": "2011-10-01", "end": "2011-12-24"},
                    "domain": "animation", "country": "japan", "release_type": "tv",
                    "path": str(fixture.work_root),
                    "collectioned_ordered": [{
                        "press_format": "BDRip", "press_group": "",
                        "press_path": target_name,
                    }],
                }
                result = preview_catalog_work_append(draft, settings=fixture.browse_settings)
                self.assertEqual(result["action"], "already_exists")
                self.assertEqual(catalog_file.read_bytes(), before)
                wrong = deepcopy(draft)
                wrong["collectioned_ordered"][0]["press_group"] = "VCB"
                with self.assertRaisesRegex(ValueError, "压制记录不同"):
                    preview_catalog_work_append(wrong, settings=fixture.browse_settings)
                self.assertEqual(catalog_file.read_bytes(), before)

    def test_overrides_preserve_explicit_empty_and_absent_group(self) -> None:
        explicit = _source_press_overrides_from_body({
            "source_press_overrides": {"incoming": {"press_group": ""}},
        })
        self.assertEqual(explicit, {"incoming": {"press_group": ""}})
        automatic = _source_press_overrides_from_body({
            "source_press_overrides": {"incoming": {"press_format": "DVDRip"}},
        })
        self.assertEqual(automatic, {"incoming": {"press_format": "DVDRip"}})
        for invalid in (None, False, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _source_press_overrides_from_body({
                    "source_press_overrides": {"incoming": {"press_group": invalid}},
                })

    def test_empty_group_can_land_catalog_media_and_shortcut_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            catalog_file = fixture.catalog_root / "[JP][TVInfo][2097].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            body = fixture.body
            first = body["draft_work"]["presses"][0]
            first.update(press_group="", press_group_confirmed=True,
                         press_path="Manual Landing Work_BDRip")
            before = _tree_snapshot(fixture.work_root)
            with _shortcut_sandbox(fixture) as created:
                preview = preview_work_landing(
                    body, organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(preview["ready"], preview.get("issues"))
                self.assertEqual(_tree_snapshot(fixture.work_root), before)
                empty_shortcut = next(row for row in preview["shortcuts"]
                                      if not row["press_group"])
                self.assertEqual(Path(empty_shortcut["shortcut_path"]).name, "BDRip.lnk")
                result = apply_work_landing(
                    {**body, "landing_plan_id": preview["landing_plan_id"],
                     "confirmation": preview["landing_plan_id"],
                     "acknowledge_catalog_write": True, "acknowledge_move": True,
                     "acknowledge_shortcuts": True},
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(created), 2)
            collection = entry_collection_type_data(load_jp_tv_yaml_file(catalog_file)[0])
            self.assertEqual(collection["collectioned"][0]["press_group"], "")
            self.assertEqual(collection["collectioned"][0]["press_path"],
                             "Manual Landing Work_BDRip")

    def test_unconfirmed_group_still_requires_choice_and_placeholder_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            draft = fixture.body["draft_work"]
            draft["presses"][0]["press_group"] = "----"
            normalized, overrides, _sources = _normalize_draft(draft, root=fixture.work_root)
            self.assertEqual(normalized["collectioned_ordered"][0]["press_group"], "")
            self.assertEqual(next(iter(overrides.values()))["press_group"], "")
            draft["presses"][0]["press_group_confirmed"] = False
            with self.assertRaisesRegex(ValueError, "尚未确认"):
                _normalize_draft(draft, root=fixture.work_root)

    def test_selected_empty_group_is_not_overwritten_by_group_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            draft = fixture.body["draft_work"]
            draft["presses"] = [draft["presses"][0]]
            draft["presses"][0].update(press_group="", press_group_confirmed=True)
            mapped = _draft_mapping(draft, root=fixture.work_root)
            proposed, warnings, _confidence = _local_proposed_work(
                mapped, root=fixture.work_root, settings=fixture.organizer_settings,
            )
            self.assertEqual(proposed["presses"][0]["press_group"], "")
            self.assertEqual(proposed["presses"][0]["suggested_press_group"], "VCB")
            self.assertFalse(any("未能唯一识别压制/字幕组" in message for message in warnings))
            draft["presses"][0]["press_group_confirmed"] = False
            mapped = _draft_mapping(draft, root=fixture.work_root)
            automatic, _warnings, _confidence = _local_proposed_work(
                mapped, root=fixture.work_root, settings=fixture.organizer_settings,
            )
            self.assertEqual(automatic["presses"][0]["press_group"], "VCB")

    def test_repair_empty_group_is_exact_and_preserves_existing_placeholder(self) -> None:
        root = Path(".").resolve()
        record = {"work": {"name": "Bleach", "date": {}, "domain": "animation",
                            "country": "japan", "release_type": "tv"},
                  "presses": [
                      {"press_key": "empty", "press_format": "DVDRip",
                       "press_group": "----", "press_path": "old"},
                      {"press_key": "vcb", "press_format": "DVDRip",
                       "press_group": "VCB", "press_path": "vcb"},
                  ]}
        requested = {"name": "Bleach", "collectioned_ordered": [
            {"press_format": "DVDRip", "press_group": "", "press_path": "Bleach_DVDRip"},
        ]}
        merged = _merged_update_patch(record, requested, root=root)
        self.assertEqual(merged["collectioned_ordered"][0]["press_group"], "----")
        self.assertEqual(merged["collectioned_ordered"][0]["press_key"], "empty")
        requested["collectioned_ordered"][0]["press_key"] = "vcb"
        with self.assertRaisesRegex(ValueError, "不能改格式或组"):
            _merged_update_patch(record, requested, root=root)

    def test_shared_empty_group_requires_exact_matching_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp))
            for catalog_file in (fixture.first_catalog, fixture.second_catalog):
                catalog_file.write_text(catalog_file.read_text(encoding="utf-8")
                                        .replace("press_group: VCB", "press_group: ''"),
                                        encoding="utf-8")
            binding = fixture.shared_binding
            binding["press_group"] = ""
            binding["press_path"] = "Fate／Zero_BDRip"
            binding["members"][0]["catalog_ref"] = fixture.catalog_ref(fixture.first_catalog, FIRST_WORK)
            binding["members"][1]["catalog_ref"] = fixture.catalog_ref(fixture.second_catalog, SECOND_WORK)
            for member in binding["members"]:
                member["press_key"] = "0:main::BDRip:"
            normalized = _normalize_shared_target_bindings(
                [binding], root=fixture.root, catalog_root=fixture.catalog_root,
            )
            self.assertEqual(normalized[0][0]["press_group"], "")
            wrong = deepcopy(binding)
            wrong["press_group"] = "VCB"
            with self.assertRaisesRegex(ValueError, "完全一致"):
                _normalize_shared_target_bindings(
                    [wrong], root=fixture.root, catalog_root=fixture.catalog_root,
                )


if __name__ == "__main__":
    unittest.main()
