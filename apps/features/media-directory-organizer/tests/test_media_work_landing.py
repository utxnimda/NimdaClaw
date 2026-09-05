from __future__ import annotations

import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from collection_detail.save import preview_catalog_work_append
from media_directory_organizer.landing import (
    apply_work_landing,
    apply_work_landing_shortcut_retry,
    preview_work_landing,
    preview_work_landing_shortcut_retry,
)
from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.service import MediaRollbackError
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import (
    entry_collection_type_data,
    entry_display_name,
)


WORK_NAME = "Manual Landing Work"


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
        format_markers={},
        group_markers={},
        group_suffixes={"VCB": "VCB", "JSUM": "JSUM"},
        max_files=100,
        default_work_root=None,
        work_aliases={},
    )


def _draft(root: Path, *, country: str = "japan") -> dict[str, object]:
    return {
        "name": WORK_NAME,
        "date": {"start": "2097-04-01", "end": "2097-06-30"},
        "domain": "animation" if country == "japan" else "tv-drama",
        "country": country,
        "release_type": "tv",
        "path": str(root),
        "presses": [
            {
                "source_names": ["incoming-vcb"],
                "press_format": "BDRip",
                "press_group": "VCB",
                "press_path": f"{WORK_NAME}_BDRip(VCB)",
            },
            {
                "source_names": ["incoming-jsum"],
                "press_format": "BDRip",
                "press_group": "JSUM",
                "press_path": f"{WORK_NAME}_BDRip(JSUM)",
            },
        ],
    }


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


class _LandingFixture:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.catalog_root = base / "catalog"
        self.resource_root = base / "resources"
        self.work_root = self.resource_root / WORK_NAME
        self.shortcut_root = base / "shortcuts"
        self.index_path = base / "cache" / "link-index.yaml"
        self.catalog_root.mkdir()
        self.work_root.mkdir(parents=True)
        self.shortcut_root.mkdir()
        self.vcb_file = self.work_root / "incoming-vcb" / "Episode 01.mkv"
        self.jsum_file = self.work_root / "incoming-jsum" / "Episode 02.mkv"
        self.vcb_file.parent.mkdir()
        self.jsum_file.parent.mkdir()
        self.vcb_file.write_bytes(b"vcb-video")
        self.jsum_file.write_bytes(b"jsum-video")
        self.unrelated_shortcut = self.shortcut_root / "existing" / "Unrelated.lnk"
        self.unrelated_shortcut.parent.mkdir()
        self.unrelated_shortcut.write_bytes(b"keep-this-shortcut")
        self.browse_settings = _browse_settings(self.catalog_root)
        self.organizer_settings = _organizer_settings(
            self.catalog_root,
            self.resource_root,
        )

    @property
    def body(self) -> dict[str, object]:
        return {"root": str(self.work_root), "draft_work": _draft(self.work_root)}


@contextmanager
def _shortcut_sandbox(
    fixture: _LandingFixture,
    *,
    existing_targets: dict[str, str] | None = None,
) -> Iterator[list[tuple[Path, Path]]]:
    created: list[tuple[Path, Path]] = []
    target_map = {
        str(Path(path).resolve()): target
        for path, target in (existing_targets or {}).items()
    }

    def shortcut_target(path: Path) -> str:
        return target_map.get(str(path.resolve()), "")

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


class MediaWorkLandingSafetyTest(unittest.TestCase):
    def test_partial_media_rollback_preserves_catalog_and_exposes_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            catalog_file = fixture.catalog_root / "[JP][TVInfo][2097].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            original_move = shutil.move
            move_calls = 0

            def fail_second_move_and_rollback(source, target):
                nonlocal move_calls
                move_calls += 1
                if move_calls > 1:
                    raise OSError("simulated move/rollback failure")
                return original_move(source, target)

            with _shortcut_sandbox(fixture) as created:
                preview = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(preview["ready"], preview)
                with patch(
                    "media_directory_organizer.execution.shutil.move",
                    side_effect=fail_second_move_and_rollback,
                ):
                    with self.assertRaises(MediaRollbackError) as raised:
                        apply_work_landing(
                            {
                                **fixture.body,
                                "landing_plan_id": preview["landing_plan_id"],
                                "confirmation": preview["landing_plan_id"],
                                "acknowledge_catalog_write": True,
                                "acknowledge_move": True,
                                "acknowledge_shortcuts": True,
                            },
                            organizer_settings=fixture.organizer_settings,
                            browse_settings=fixture.browse_settings,
                        )

            payload = raised.exception.to_payload()
            self.assertEqual(payload["state"], "partial")
            self.assertEqual(payload["landing_plan_id"], preview["landing_plan_id"])
            self.assertFalse(payload["media"]["rollback_complete"])
            self.assertEqual(payload["media"]["moved_file_count"], 1)
            self.assertEqual(payload["media"]["rolled_back_file_count"], 0)
            recovery = payload["media"]["recovery_moves"][0]
            self.assertEqual(recovery["source"], preview["organizer_plan"]["moves"][0]["source"])
            self.assertTrue(Path(recovery["target"]).is_file())
            self.assertFalse(Path(recovery["source"]).exists())
            self.assertEqual(payload["catalog"]["state"], "preserved")
            self.assertFalse(payload["catalog"]["rolled_back"])
            recovery_file = payload["catalog"]["recovery_files"][0]
            self.assertEqual(Path(recovery_file["target"]), catalog_file)
            self.assertEqual(Path(recovery_file["history_path"]).read_text(encoding="utf-8"), "[]\n")
            self.assertEqual(len(load_jp_tv_yaml_file(catalog_file)), 1)
            self.assertEqual(created, [])

    def test_catalog_preview_routes_japan_and_korea_by_country_and_start_year(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            catalog_root = base / "catalog"
            catalog_root.mkdir()
            settings = _browse_settings(catalog_root)
            work_root = base / "work"
            work_root.mkdir()

            for country, expected_name in (
                ("japan", "[JP][TVInfo][2097].yaml"),
                ("korea", "[KR][TVInfo][2097].yaml"),
            ):
                patch_body = {
                    "name": f"{WORK_NAME} {country}",
                    "date": {"start": "2097-01-02", "end": "2097-03-31"},
                    "domain": "animation" if country == "japan" else "tv-drama",
                    "country": country,
                    "release_type": "tv",
                    "path": str(work_root / country),
                    "markers": [],
                    "collectioned_ordered": [
                        {
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": f"{WORK_NAME}_{country}_BDRip(VCB)",
                        }
                    ],
                }

                preview = preview_catalog_work_append(patch_body, settings=settings)

                self.assertEqual(preview["action"], "append")
                self.assertEqual(preview["yaml_source_rel"], expected_name)
                self.assertEqual(Path(preview["target"]).name, expected_name)
                self.assertFalse(Path(preview["target"]).exists())

            self.assertEqual(list(catalog_root.iterdir()), [])

    def test_preview_is_zero_write_and_apply_lands_db_media_and_scoped_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                preview = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

                self.assertTrue(preview["ready"], preview["organizer_plan"]["issues"])
                self.assertEqual(preview["catalog_change"]["action"], "append")
                self.assertEqual(
                    preview["catalog_change"]["yaml_source_rel"],
                    "[JP][TVInfo][2097].yaml",
                )
                self.assertEqual(len(preview["draft_work"]["collectioned_ordered"]), 2)
                self.assertEqual(preview["shortcut_summary"]["planned_count"], 2)
                self.assertEqual(
                    {row["status"] for row in preview["shortcuts"]},
                    {"planned"},
                )
                self.assertEqual(_tree_snapshot(fixture.base), before)
                self.assertEqual(created, [])

                apply_body = {
                    **fixture.body,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": preview["landing_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                }
                result = apply_work_landing(
                    apply_body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["media"]["moved_file_count"], 2)
            self.assertEqual(result["shortcuts"]["created_count"], 2)
            self.assertEqual(len(created), 2)
            self.assertTrue(all(path.is_file() for path, _target in created))
            self.assertTrue(all(target.is_dir() for _path, target in created))
            self.assertTrue(fixture.index_path.is_file())
            self.assertEqual(
                fixture.unrelated_shortcut.read_bytes(),
                b"keep-this-shortcut",
            )
            self.assertFalse(fixture.vcb_file.exists())
            self.assertFalse(fixture.jsum_file.exists())
            for move in preview["organizer_plan"]["moves"]:
                self.assertTrue(Path(move["target"]).is_file())

            catalog_file = fixture.catalog_root / "[JP][TVInfo][2097].yaml"
            entries = load_jp_tv_yaml_file(catalog_file)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entry_display_name(entries[0]), WORK_NAME)
            collection = entry_collection_type_data(entries[0])
            self.assertEqual(collection["path"], str(fixture.work_root))
            self.assertEqual(len(collection["collectioned"]), 2)
            self.assertEqual(
                {
                    (row["press_format"], row["press_group"], row["press_path"])
                    for row in collection["collectioned"]
                },
                {
                    ("BDRip", "VCB", f"{WORK_NAME}_BDRip(VCB)"),
                    ("BDRip", "JSUM", f"{WORK_NAME}_BDRip(JSUM)"),
                },
            )

    def test_confirmation_mismatch_and_post_preview_source_change_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            with _shortcut_sandbox(fixture):
                preview = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                bad_confirmation = "0" * 16
                if bad_confirmation == preview["landing_plan_id"]:
                    bad_confirmation = "f" * 16
                bad_body = {
                    **fixture.body,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": bad_confirmation,
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                }
                with self.assertRaises(ValueError):
                    apply_work_landing(
                        bad_body,
                        organizer_settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )

                self.assertFalse(any(fixture.catalog_root.glob("*.yaml")))
                self.assertTrue(fixture.vcb_file.is_file())
                self.assertTrue(fixture.jsum_file.is_file())
                fixture.vcb_file.write_bytes(b"changed-after-preview-and-must-not-move")
                after_external_change = _tree_snapshot(fixture.base)
                exact_body = {
                    **fixture.body,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": preview["landing_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                }
                with self.assertRaises(ValueError):
                    apply_work_landing(
                        exact_body,
                        organizer_settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )

            self.assertEqual(_tree_snapshot(fixture.base), after_external_change)
            self.assertFalse(any(fixture.catalog_root.glob("*.yaml")))
            self.assertEqual(
                fixture.unrelated_shortcut.read_bytes(),
                b"keep-this-shortcut",
            )

    def test_scoped_shortcut_conflict_blocks_landing_without_touching_other_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            with _shortcut_sandbox(fixture):
                initial = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
            conflict_path = Path(initial["shortcuts"][0]["shortcut_path"])
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            conflict_path.write_bytes(b"existing-shortcut-for-another-target")
            other_target = fixture.resource_root / "Another Work"
            other_target.mkdir()
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(
                fixture,
                existing_targets={str(conflict_path): str(other_target)},
            ) as created:
                blocked = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertFalse(blocked["ready"])
            self.assertEqual(blocked["shortcut_summary"]["conflict_count"], 1)
            self.assertEqual(
                [row["status"] for row in blocked["shortcuts"]].count("conflict"),
                1,
            )
            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), before)
            self.assertEqual(
                fixture.unrelated_shortcut.read_bytes(),
                b"keep-this-shortcut",
            )

    def test_shortcut_failure_can_retry_without_rescanning_or_moving_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _LandingFixture(Path(temp))
            with _shortcut_sandbox(fixture):
                preview = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                apply_body = {
                    **fixture.body,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": preview["landing_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                }
                with patch(
                    "collection_detail.link_index._create_windows_shortcut",
                    side_effect=OSError("simulated shortcut failure"),
                ):
                    pending = apply_work_landing(
                        apply_body,
                        organizer_settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )

            self.assertFalse(pending["ok"])
            self.assertEqual(pending["state"], "shortcut_pending")
            self.assertFalse(fixture.vcb_file.exists())
            self.assertFalse(fixture.jsum_file.exists())
            catalog_file = fixture.catalog_root / "[JP][TVInfo][2097].yaml"
            catalog_before_retry = catalog_file.read_bytes()

            retry_body = {
                "root": pending["root"],
                "draft_work": pending["draft_work"],
            }
            with _shortcut_sandbox(fixture) as created:
                retry = preview_work_landing_shortcut_retry(
                    retry_body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(retry["ready"], retry)
                self.assertEqual(retry["shortcut_summary"]["planned_count"], 2)
                completed = apply_work_landing_shortcut_retry(
                    {
                        **retry_body,
                        "retry_plan_id": retry["retry_plan_id"],
                        "confirmation": retry["retry_plan_id"],
                        "acknowledge_shortcuts": True,
                    },
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(completed["ok"])
            self.assertEqual(completed["state"], "complete")
            self.assertEqual(completed["shortcuts"]["created_count"], 2)
            self.assertEqual(len(created), 2)
            self.assertEqual(catalog_file.read_bytes(), catalog_before_retry)
            self.assertEqual(
                fixture.unrelated_shortcut.read_bytes(),
                b"keep-this-shortcut",
            )


if __name__ == "__main__":
    unittest.main()
