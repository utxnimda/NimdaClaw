from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.web import (
    apply_organizer_landing_from_ui_body,
    apply_organizer_landing_shortcuts_from_ui_body,
    preview_organizer_from_ui_body,
    preview_organizer_landing_from_ui_body,
    preview_organizer_landing_shortcuts_from_ui_body,
)
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import (
    entry_collection_type_data,
    entry_display_name,
)


EXISTING_WORK = "Mixed Existing Work"
NEW_WORK = "Mixed New Work"
EXISTING_SOURCE = "[JSUM] Mixed Existing Work [BDRip]"
NEW_SOURCE = "[VCB-Studio] Mixed New Work [BDRip]"
EXISTING_VCB_TARGET = f"{EXISTING_WORK}_BDRip(VCBM)"
EXISTING_JSUM_TARGET = f"{EXISTING_WORK}_BDRip(Jsum)"
NEW_VCB_TARGET = f"{NEW_WORK}_BDRip(VCBM)"


def _existing_work_yaml(root: Path) -> str:
    escaped_root = str(root).replace("'", "''")
    return f"""\
- attributes:
  - type: date
    data: {{start: '20980101', end: '20980331'}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      path: '{escaped_root}'
      collectioned:
      - press_format: BDRip
        press_group: VCB
        press_path: {EXISTING_VCB_TARGET}
      markers: []
  - type: country
    data: japan
  - type: name
    data: {EXISTING_WORK}
"""


def _browse_settings(catalog_root: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=catalog_root,
        resolved_default_readable=None,
        resolved_catalog_yaml_paths=(),
        enum_options={},
        enum_labels={},
        enum_section_labels={},
        app_features=(),
    )


def _organizer_settings(catalog_root: Path, resource_root: Path) -> OrganizerSettings:
    return OrganizerSettings(
        catalog_root=catalog_root,
        allowed_resource_roots=(resource_root,),
        format_markers={"BDRip": ("bdrip",)},
        group_markers={
            "VCB": ("vcb-studio", "vcb"),
            "JSUM": ("jsum",),
        },
        group_suffixes={"VCB": "VCBM", "JSUM": "Jsum"},
        max_files=100,
        default_work_root=None,
        work_aliases={},
    )


def _tree_snapshot(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return directories, files


class _MixedFixture:
    def __init__(
        self,
        base: Path,
        *,
        already_landed: bool = False,
        existing_already_landed: bool = False,
    ) -> None:
        self.base = base
        self.catalog_root = base / "catalog"
        self.resource_root = base / "resources"
        self.work_root = self.resource_root / "Mixed Binding Family"
        self.shortcut_root = base / "shortcuts"
        self.index_path = base / "cache" / "link-index.yaml"
        self.catalog_root.mkdir()
        self.work_root.mkdir(parents=True)
        self.shortcut_root.mkdir()
        self.catalog_file = self.catalog_root / "[JP][TVInfo][2098].yaml"
        self.catalog_file.write_text(
            _existing_work_yaml(self.work_root),
            encoding="utf-8",
        )
        existing_landed = already_landed or existing_already_landed
        self.existing_source = self.work_root / (
            EXISTING_JSUM_TARGET if existing_landed else EXISTING_SOURCE
        )
        self.new_source = self.work_root / (
            NEW_VCB_TARGET if already_landed else NEW_SOURCE
        )
        if existing_landed:
            self.existing_media = (
                self.existing_source
                / f"{EXISTING_WORK}_BDRip_Disc"
                / "Episode 01.mkv"
            )
        else:
            self.existing_media = self.existing_source / f"[{EXISTING_WORK}][01].mkv"
        if already_landed:
            self.new_media = (
                self.new_source
                / f"{NEW_WORK}_BDRip_Disc"
                / "Episode 01.mkv"
            )
        else:
            self.new_media = self.new_source / f"[{NEW_WORK}][01].mkv"
        self.existing_media.parent.mkdir(parents=True)
        self.new_media.parent.mkdir(parents=True)
        self.existing_media.write_bytes(b"existing-jsum-video")
        self.new_media.write_bytes(b"new-vcb-video")
        self.unrelated_shortcut = self.shortcut_root / "existing" / "Unrelated.lnk"
        self.unrelated_shortcut.parent.mkdir()
        self.unrelated_shortcut.write_bytes(b"keep-unrelated-shortcut")
        self.browse_settings = _browse_settings(self.catalog_root)
        self.organizer_settings = _organizer_settings(
            self.catalog_root,
            self.resource_root,
        )

    def catalog_ref(self) -> dict[str, object]:
        before = _tree_snapshot(self.base)
        plan = preview_organizer_from_ui_body(
            {
                "root": str(self.work_root),
                "source_work_bindings": {
                    self.existing_source.name: {
                        "mode": "manual",
                        "work_name": EXISTING_WORK,
                    }
                },
            },
            settings=self.organizer_settings,
        )["plan"]
        rows = {
            str(row["source_name"]): row
            for row in plan["source_work_bindings"]
        }
        ref = rows[self.existing_source.name]["catalog_ref"]
        if not isinstance(ref, dict):
            raise AssertionError("preview did not return an exact catalog_ref")
        if _tree_snapshot(self.base) != before:
            raise AssertionError("catalog-ref discovery preview changed the fixture")
        return ref

    def landing_body(self, catalog_ref: dict[str, object]) -> dict[str, object]:
        return {
            "root": str(self.work_root),
            "source_work_bindings": {
                str(self.existing_source): {
                    "mode": "catalog",
                    "catalog_ref": catalog_ref,
                    "presses": [
                        {
                            "source_names": [self.existing_source.name],
                            "press_format": "BDRip",
                            "press_group": "JSUM",
                            "press_path": EXISTING_JSUM_TARGET,
                        }
                    ],
                },
                self.new_source.name: {
                    "mode": "draft",
                    "draft_work": {
                        "name": NEW_WORK,
                        "date": {
                            "start": "2098-07-01",
                            "end": "2098-09-30",
                        },
                        "domain": "animation",
                        "country": "japan",
                        "release_type": "tv",
                        "path": str(self.work_root),
                        "presses": [
                            {
                                "source_names": [self.new_source.name],
                                "press_format": "BDRip",
                                "press_group": "VCB",
                                "press_path": NEW_VCB_TARGET,
                            }
                        ],
                    },
                },
            },
        }


@contextmanager
def _shortcut_sandbox(fixture: _MixedFixture) -> Iterator[list[tuple[Path, Path]]]:
    created: list[tuple[Path, Path]] = []

    def shortcut_target(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def create_shortcut(path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(target), encoding="utf-8")
        created.append((path, target))

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "collection_detail.link_index.resource_roots",
                return_value=[fixture.resource_root],
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index.shortcut_roots",
                return_value=[fixture.shortcut_root],
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._shortcut_root_for_work",
                return_value=fixture.shortcut_root,
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._layout_levels",
                return_value=("{year_label}", "{name}"),
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._shortcut_name_template",
                return_value="{press_format}{press_group_suffix}",
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._link_index_db_path",
                return_value=fixture.index_path,
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._windows_shortcut_target",
                side_effect=shortcut_target,
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=create_shortcut,
            )
        )
        yield created


def _apply_body(body: dict[str, object], preview: dict[str, object]) -> dict[str, object]:
    plan_id = str(preview["landing_plan_id"])
    return {
        **body,
        "landing_plan_id": plan_id,
        "confirmation": plan_id,
        "acknowledge_catalog_write": True,
        "acknowledge_move": True,
        "acknowledge_shortcuts": True,
    }


def _catalog_rows(fixture: _MixedFixture) -> dict[str, dict[str, object]]:
    entries = load_jp_tv_yaml_file(fixture.catalog_file)
    return {
        entry_display_name(entry): entry_collection_type_data(entry)
        for entry in entries
    }


class MixedSourceLandingTest(unittest.TestCase):
    def test_apply_updates_existing_and_appends_new_without_duplicate_press(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _MixedFixture(Path(temp))
            with _shortcut_sandbox(fixture) as created:
                catalog_ref = fixture.catalog_ref()
                body = fixture.landing_body(catalog_ref)
                before = _tree_snapshot(fixture.base)
                preview = preview_organizer_landing_from_ui_body(
                    body,
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

                self.assertEqual(_tree_snapshot(fixture.base), before)
                self.assertEqual(created, [])
                self.assertTrue(preview["ready"], preview)
                self.assertEqual(preview["state"], "mixed_source_landing_preview")
                self.assertEqual(
                    set(preview["source_work_bindings"]),
                    {EXISTING_SOURCE, NEW_SOURCE},
                )
                self.assertEqual(
                    {row["action"] for row in preview["catalog_changes"]},
                    {"update", "append"},
                )
                self.assertEqual(len(preview["organizer_plan"]["moves"]), 2)
                self.assertEqual(preview["shortcut_summary"]["planned_count"], 2)

                with self.assertRaisesRegex(ValueError, "确认"):
                    apply_organizer_landing_from_ui_body(
                        {
                            **body,
                            "landing_plan_id": preview["landing_plan_id"],
                            "confirmation": preview["landing_plan_id"],
                        },
                        settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), before)

                result = apply_organizer_landing_from_ui_body(
                    _apply_body(body, preview),
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["media"]["moved_file_count"], 2)
            self.assertEqual(result["shortcuts"]["created_count"], 2)
            self.assertEqual(len(created), 2)
            self.assertFalse(fixture.existing_media.exists())
            self.assertFalse(fixture.new_media.exists())
            for move in preview["organizer_plan"]["moves"]:
                self.assertTrue(Path(move["target"]).is_file())

            rows = _catalog_rows(fixture)
            self.assertEqual(set(rows), {EXISTING_WORK, NEW_WORK})
            existing_presses = rows[EXISTING_WORK]["collectioned"]
            self.assertEqual(
                [
                    (
                        row["press_format"],
                        row["press_group"],
                        row["press_path"],
                    )
                    for row in existing_presses
                ],
                [
                    ("BDRip", "VCB", EXISTING_VCB_TARGET),
                    ("BDRip", "JSUM", EXISTING_JSUM_TARGET),
                ],
            )
            self.assertEqual(
                len(
                    {
                        (row["press_format"], row["press_group"])
                        for row in existing_presses
                    }
                ),
                2,
            )
            self.assertEqual(
                [
                    (
                        row["press_format"],
                        row["press_group"],
                        row["press_path"],
                    )
                    for row in rows[NEW_WORK]["collectioned"]
                ],
                [("BDRip", "VCB", NEW_VCB_TARGET)],
            )
            self.assertEqual(
                fixture.unrelated_shortcut.read_bytes(),
                b"keep-unrelated-shortcut",
            )

    def test_already_canonical_targets_apply_with_zero_media_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _MixedFixture(Path(temp), already_landed=True)
            with _shortcut_sandbox(fixture) as created:
                catalog_ref = fixture.catalog_ref()
                body = fixture.landing_body(catalog_ref)
                before = _tree_snapshot(fixture.base)
                media_before = _tree_snapshot(fixture.work_root)
                preview = preview_organizer_landing_from_ui_body(
                    body,
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

                self.assertEqual(_tree_snapshot(fixture.base), before)
                self.assertEqual(created, [])
                self.assertTrue(preview["ready"], preview)
                self.assertTrue(preview["media_no_move"])
                self.assertTrue(preview["organizer_plan"]["ready"])
                self.assertTrue(preview["organizer_plan"]["mixed_no_move"])
                self.assertEqual(preview["organizer_plan"]["moves"], [])
                result = apply_organizer_landing_from_ui_body(
                    _apply_body(body, preview),
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["media"]["moved_file_count"], 0)
            self.assertTrue(result["media"]["already_canonical"])
            self.assertEqual(_tree_snapshot(fixture.work_root), media_before)
            self.assertTrue(fixture.existing_media.is_file())
            self.assertTrue(fixture.new_media.is_file())
            self.assertEqual(result["shortcuts"]["created_count"], 2)
            self.assertEqual(len(created), 2)
            rows = _catalog_rows(fixture)
            self.assertEqual(set(rows), {EXISTING_WORK, NEW_WORK})
            self.assertEqual(
                [
                    (row["press_format"], row["press_group"])
                    for row in rows[EXISTING_WORK]["collectioned"]
                ],
                [("BDRip", "VCB"), ("BDRip", "JSUM")],
            )

    def test_partial_canonical_target_is_preserved_while_other_source_moves_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _MixedFixture(Path(temp), existing_already_landed=True)
            with _shortcut_sandbox(fixture) as created:
                catalog_ref = fixture.catalog_ref()
                body = fixture.landing_body(catalog_ref)
                before = _tree_snapshot(fixture.base)
                canonical_bytes = fixture.existing_media.read_bytes()
                preview = preview_organizer_landing_from_ui_body(
                    body,
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

                self.assertEqual(_tree_snapshot(fixture.base), before)
                self.assertEqual(created, [])
                self.assertTrue(preview["ready"], preview)
                self.assertFalse(preview["media_no_move"])
                self.assertEqual(len(preview["organizer_plan"]["moves"]), 1)
                self.assertTrue(
                    all(
                        not str(move["source"]).startswith(str(fixture.existing_source))
                        for move in preview["organizer_plan"]["moves"]
                    )
                )
                result = apply_organizer_landing_from_ui_body(
                    _apply_body(body, preview),
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["media"]["moved_file_count"], 1)
            self.assertTrue(fixture.existing_media.is_file())
            self.assertEqual(fixture.existing_media.read_bytes(), canonical_bytes)
            self.assertFalse(fixture.new_media.exists())
            self.assertEqual(result["shortcuts"]["created_count"], 2)
            self.assertEqual(len(created), 2)

    def test_missing_direct_source_and_unregistered_group_fail_closed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _MixedFixture(Path(temp))
            with _shortcut_sandbox(fixture) as created:
                catalog_ref = fixture.catalog_ref()

                missing_body = fixture.landing_body(catalog_ref)
                missing_bindings = dict(missing_body["source_work_bindings"])
                missing_bindings.pop(fixture.new_source.name)
                missing_body["source_work_bindings"] = missing_bindings
                before_missing = _tree_snapshot(fixture.base)
                with self.assertRaisesRegex(ValueError, "每个|尚未分配"):
                    preview_organizer_landing_from_ui_body(
                        missing_body,
                        settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), before_missing)

                invalid_group_body = fixture.landing_body(catalog_ref)
                invalid_binding = invalid_group_body["source_work_bindings"][
                    str(fixture.existing_source)
                ]
                invalid_binding["presses"][0]["press_group"] = "NOT_REGISTERED"
                before_invalid = _tree_snapshot(fixture.base)
                with self.assertRaisesRegex(ValueError, "未登记"):
                    preview_organizer_landing_from_ui_body(
                        invalid_group_body,
                        settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), before_invalid)

            self.assertEqual(created, [])

    def test_media_failure_rolls_back_existing_update_and_new_append(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _MixedFixture(Path(temp))
            with _shortcut_sandbox(fixture) as created:
                catalog_ref = fixture.catalog_ref()
                body = fixture.landing_body(catalog_ref)
                preview = preview_organizer_landing_from_ui_body(
                    body,
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(preview["ready"], preview)
                catalog_before = fixture.catalog_file.read_bytes()
                media_before = _tree_snapshot(fixture.work_root)
                shortcuts_before = _tree_snapshot(fixture.shortcut_root)
                with patch(
                    "media_directory_organizer.landing.apply_plan",
                    side_effect=RuntimeError("simulated media failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "simulated media failure"):
                        apply_organizer_landing_from_ui_body(
                            _apply_body(body, preview),
                            settings=fixture.organizer_settings,
                            browse_settings=fixture.browse_settings,
                        )

            self.assertEqual(created, [])
            self.assertEqual(fixture.catalog_file.read_bytes(), catalog_before)
            self.assertEqual(_tree_snapshot(fixture.work_root), media_before)
            self.assertEqual(_tree_snapshot(fixture.shortcut_root), shortcuts_before)
            self.assertFalse(fixture.index_path.exists())
            rows = _catalog_rows(fixture)
            self.assertEqual(set(rows), {EXISTING_WORK})
            self.assertEqual(len(rows[EXISTING_WORK]["collectioned"]), 1)
            self.assertTrue(fixture.existing_media.is_file())
            self.assertTrue(fixture.new_media.is_file())

    def test_shortcut_failure_retries_exact_two_work_refs_without_second_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _MixedFixture(Path(temp))
            with _shortcut_sandbox(fixture):
                catalog_ref = fixture.catalog_ref()
                body = fixture.landing_body(catalog_ref)
                preview = preview_organizer_landing_from_ui_body(
                    body,
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(preview["ready"], preview)
                with patch(
                    "collection_detail.link_index._create_windows_shortcut",
                    side_effect=OSError("simulated shortcut failure"),
                ):
                    pending = apply_organizer_landing_from_ui_body(
                        _apply_body(body, preview),
                        settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )

            self.assertFalse(pending["ok"])
            self.assertEqual(pending["state"], "shortcut_pending")
            work_refs = pending["shortcut_retry"]["work_refs"]
            self.assertEqual(len(work_refs), 2)
            self.assertEqual(
                {row["work_name"] for row in work_refs},
                {EXISTING_WORK, NEW_WORK},
            )
            self.assertTrue(all(len(row["presses"]) == 1 for row in work_refs))
            self.assertFalse(fixture.existing_media.exists())
            self.assertFalse(fixture.new_media.exists())
            catalog_before_retry = fixture.catalog_file.read_bytes()
            media_before_retry = _tree_snapshot(fixture.work_root)
            retry_body = {
                "root": pending["root"],
                "work_refs": work_refs,
            }

            with _shortcut_sandbox(fixture) as created:
                retry = preview_organizer_landing_shortcuts_from_ui_body(
                    retry_body,
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(retry["ready"], retry)
                self.assertEqual(retry["shortcut_summary"]["planned_count"], 2)
                completed = apply_organizer_landing_shortcuts_from_ui_body(
                    {
                        **retry_body,
                        "retry_plan_id": retry["retry_plan_id"],
                        "confirmation": retry["retry_plan_id"],
                        "acknowledge_shortcuts": True,
                    },
                    settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(completed["ok"], completed)
            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["shortcuts"]["created_count"], 2)
            self.assertEqual(len(created), 2)
            self.assertEqual(fixture.catalog_file.read_bytes(), catalog_before_retry)
            self.assertEqual(_tree_snapshot(fixture.work_root), media_before_retry)
            self.assertEqual(
                fixture.unrelated_shortcut.read_bytes(),
                b"keep-unrelated-shortcut",
            )


if __name__ == "__main__":
    unittest.main()
