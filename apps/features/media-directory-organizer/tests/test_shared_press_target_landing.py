from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from media_directory_organizer import landing as landing_module
from media_directory_organizer.landing import (
    apply_work_landing,
    apply_work_landing_shortcut_retry,
    preview_work_landing,
    preview_work_landing_shortcut_retry,
)
from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.web import preview_organizer_from_ui_body
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import entry_collection_type_data


FIRST_WORK = "Fate／Zero 01-12"
SECOND_WORK = "Fate／Zero 13-25"
SOURCE_NAME = "Fate／Zero_BDRip"
TARGET_NAME = "Fate／Zero_BDRip(VCBM)"
PRESS_KEY = "0:main::BDRip:VCB"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _organizer_settings(
    catalog_root: Path,
    resource_root: Path,
    work_root: Path,
) -> OrganizerSettings:
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
        default_work_root=work_root,
        work_aliases={},
    )


def _catalog_yaml(
    *,
    name: str,
    start: str,
    end: str,
    root: Path | None = None,
    press_path: str = "",
) -> str:
    root_line = (
        f"      path: '{str(root).replace(chr(39), chr(39) * 2)}'\n"
        if root is not None
        else ""
    )
    press_path_line = f"        press_path: {press_path}\n" if press_path else ""
    return f"""\
- attributes:
  - type: date
    data:
      start: '{start}'
      end: '{end}'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: VCB
{press_path_line}      markers: []
{root_line}  - type: country
    data: japan
  - type: name
    data: {name}
"""


def _catalog_yaml_with_vcb_and_jsum(
    *,
    name: str,
    start: str,
    end: str,
) -> str:
    return _catalog_yaml(name=name, start=start, end=end).replace(
        "        press_group: VCB\n",
        "        press_group: VCB\n"
        "      - press_format: BDRip\n"
        "        press_group: JSUM\n",
    )


class _SharedTargetFixture:
    def __init__(self, base: Path, *, already_landed: bool = False) -> None:
        self.base = base
        self.resource_root = base / "resources"
        self.root = self.resource_root / "Fate／Zero"
        self.catalog_root = base / "catalog"
        self.shortcut_root = base / "shortcuts"
        self.index_path = base / "cache" / "link-index.yaml"
        self.root.mkdir(parents=True)
        self.catalog_root.mkdir()
        self.shortcut_root.mkdir()
        self.source_file = self.root / SOURCE_NAME / "Fate／Zero 01-25.mkv"
        self.target_file = (
            self.root
            / TARGET_NAME
            / "Fate／Zero_BDRip_Disc"
            / "Fate／Zero 01-25.mkv"
        )
        media_file = self.target_file if already_landed else self.source_file
        media_file.parent.mkdir(parents=True)
        media_file.write_bytes(b"one-physical-release")

        self.first_catalog = self.catalog_root / "[JP][TVInfo][2011].yaml"
        self.second_catalog = self.catalog_root / "[JP][TVInfo][2012].yaml"
        self.first_catalog.write_text(
            _catalog_yaml(
                name=FIRST_WORK,
                start="20111001",
                end="20111224",
                root=self.root if already_landed else None,
                press_path=TARGET_NAME if already_landed else "",
            ),
            encoding="utf-8",
        )
        self.second_catalog.write_text(
            _catalog_yaml(
                name=SECOND_WORK,
                start="20120407",
                end="20120623",
                root=self.root if already_landed else None,
                press_path=TARGET_NAME if already_landed else "",
            ),
            encoding="utf-8",
        )
        self.organizer_settings = _organizer_settings(
            self.catalog_root,
            self.resource_root,
            self.root,
        )
        self.browse_settings = _browse_settings(self.catalog_root)

    def catalog_ref(self, source: Path, work_name: str) -> dict[str, object]:
        return {
            "yaml_source_rel": source.name,
            "index_in_file": 0,
            "work_name": work_name,
            "source_sha256": _sha256(source),
        }

    @property
    def shared_binding(self) -> dict[str, object]:
        return {
            "press_format": "BDRip",
            "press_group": "VCB",
            "press_path": TARGET_NAME,
            "members": [
                {
                    "catalog_ref": self.catalog_ref(self.first_catalog, FIRST_WORK),
                    "press_key": PRESS_KEY,
                    "source_names": [SOURCE_NAME],
                },
                {
                    "catalog_ref": self.catalog_ref(self.second_catalog, SECOND_WORK),
                    "press_key": PRESS_KEY,
                    # One physical incoming release projects to a second airing
                    # record without scanning or moving the source a second time.
                    "source_names": [],
                },
            ],
        }

    @property
    def body(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "shared_target_bindings": [self.shared_binding],
        }

    def make_neutral_target_loose(self) -> Path:
        neutral_target = "Fate／Zero_BDRip"
        (self.root / TARGET_NAME).rename(self.root / neutral_target)
        canonical_file = (
            self.root
            / neutral_target
            / "Fate／Zero_BDRip_Disc"
            / "Fate／Zero 01-25.mkv"
        )
        loose_file = self.root / neutral_target / "unclassified.mkv"
        canonical_file.rename(loose_file)
        canonical_file.parent.rmdir()
        for catalog_file in (self.first_catalog, self.second_catalog):
            catalog_file.write_text(
                catalog_file.read_text(encoding="utf-8").replace(
                    TARGET_NAME,
                    neutral_target,
                ),
                encoding="utf-8",
            )
        return loose_file


@contextmanager
def _shortcut_sandbox(
    fixture: _SharedTargetFixture,
) -> Iterator[list[tuple[Path, Path]]]:
    created: list[tuple[Path, Path]] = []

    def shortcut_target(path: Path) -> str:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

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


class SharedPressTargetLandingTest(unittest.TestCase):
    def test_one_physical_release_lands_two_catalog_records_and_exact_retry_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp))
            before = _tree_snapshot(fixture.base)
            original_apply_plan = landing_module.apply_plan

            with _shortcut_sandbox(fixture), patch.object(
                landing_module,
                "apply_plan",
                wraps=original_apply_plan,
            ) as media_apply:
                blocked = preview_work_landing(
                    fixture.body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertFalse(blocked["ready"])
                self.assertIn(
                    "shared-target-confirmation-required",
                    {issue["code"] for issue in blocked["issues"]},
                )
                self.assertEqual(_tree_snapshot(fixture.base), before)

                reviewed_body = {
                    **fixture.body,
                    "acknowledge_shared_targets": True,
                }
                preview = preview_work_landing(
                    reviewed_body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(preview["ready"], preview.get("issues"))
                self.assertNotEqual(
                    blocked["landing_plan_id"],
                    preview["landing_plan_id"],
                )
                self.assertEqual(
                    preview["shared_target_summary"],
                    {
                        "binding_count": 1,
                        "database_record_count": 2,
                        "physical_target_count": 1,
                        "shortcut_count": 2,
                    },
                )
                self.assertEqual(len(preview["catalog_changes"]), 2)
                self.assertEqual(
                    {change["action"] for change in preview["catalog_changes"]},
                    {"update"},
                )
                self.assertEqual(len(preview["shortcuts"]), 2)
                self.assertEqual(
                    {row["work_name"] for row in preview["shortcuts"]},
                    {FIRST_WORK, SECOND_WORK},
                )
                self.assertEqual(
                    {row["target_path"] for row in preview["shortcuts"]},
                    {str(fixture.root / TARGET_NAME)},
                )
                self.assertEqual(
                    len({row["shortcut_path"] for row in preview["shortcuts"]}),
                    2,
                )
                self.assertEqual(len(preview["organizer_plan"]["moves"]), 1)
                moved_target = Path(preview["organizer_plan"]["moves"][0]["target"])
                self.assertEqual(
                    moved_target.relative_to(fixture.root).parts[0],
                    TARGET_NAME,
                )
                self.assertEqual(_tree_snapshot(fixture.base), before)
                self.assertEqual(media_apply.call_count, 0)

                common_apply = {
                    **reviewed_body,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": preview["landing_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                }
                with self.assertRaisesRegex(ValueError, "共享|shared"):
                    apply_work_landing(
                        {
                            key: value
                            for key, value in common_apply.items()
                            if key != "acknowledge_shared_targets"
                        },
                        organizer_settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), before)
                self.assertEqual(media_apply.call_count, 0)

                with patch(
                    "collection_detail.link_index._create_windows_shortcut",
                    side_effect=OSError("simulated shared shortcut failure"),
                ):
                    pending = apply_work_landing(
                        common_apply,
                        organizer_settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )

                self.assertFalse(pending["ok"])
                self.assertEqual(pending["state"], "shortcut_pending")
                self.assertEqual(pending["media"]["moved_file_count"], 1)
                self.assertEqual(media_apply.call_count, 1)
                self.assertFalse(fixture.source_file.exists())
                self.assertEqual(moved_target.read_bytes(), b"one-physical-release")
                work_refs = pending["shortcut_retry"]["work_refs"]
                self.assertEqual(
                    {ref["work_name"] for ref in work_refs},
                    {FIRST_WORK, SECOND_WORK},
                )
                self.assertEqual([len(ref["presses"]) for ref in work_refs], [1, 1])
                self.assertEqual(
                    {
                        press["press_path"]
                        for ref in work_refs
                        for press in ref["presses"]
                    },
                    {TARGET_NAME},
                )

                catalog_before_retry = {
                    path: path.read_bytes()
                    for path in (fixture.first_catalog, fixture.second_catalog)
                }
                media_before_retry = _tree_snapshot(fixture.root / TARGET_NAME)
                retry_body = {
                    "root": str(fixture.root),
                    "work_refs": work_refs,
                }
                retry = preview_work_landing_shortcut_retry(
                    retry_body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(retry["ready"], retry.get("issues"))
                self.assertEqual(len(retry["shortcuts"]), 2)
                self.assertEqual(
                    {row["target_path"] for row in retry["shortcuts"]},
                    {str(fixture.root / TARGET_NAME)},
                )
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

                self.assertTrue(completed["ok"], completed)
                self.assertEqual(completed["shortcuts"]["created_count"], 2)
                self.assertEqual(media_apply.call_count, 1)
                self.assertEqual(
                    _tree_snapshot(fixture.root / TARGET_NAME),
                    media_before_retry,
                )
                self.assertEqual(
                    {
                        path: path.read_bytes()
                        for path in (fixture.first_catalog, fixture.second_catalog)
                    },
                    catalog_before_retry,
                )

            for catalog_file in (fixture.first_catalog, fixture.second_catalog):
                entries = load_jp_tv_yaml_file(catalog_file)
                self.assertEqual(len(entries), 1)
                collection = entry_collection_type_data(entries[0])
                self.assertEqual(collection["path"], str(fixture.root))
                self.assertEqual(collection["collectioned"][0]["press_path"], TARGET_NAME)

    def test_existing_canonical_target_can_preview_two_empty_catalog_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp), already_landed=True)
            fixture.first_catalog.write_text(
                _catalog_yaml(
                    name=FIRST_WORK,
                    start="20111001",
                    end="20111224",
                ),
                encoding="utf-8",
            )
            fixture.second_catalog.write_text(
                _catalog_yaml(
                    name=SECOND_WORK,
                    start="20120407",
                    end="20120623",
                ),
                encoding="utf-8",
            )
            binding = fixture.shared_binding
            binding["members"][0]["source_names"] = [TARGET_NAME]
            body = {
                "root": str(fixture.root),
                "shared_target_bindings": [binding],
                "acknowledge_shared_targets": True,
            }
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                preview = preview_work_landing(
                    body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(preview["ready"], preview.get("issues"))
            self.assertEqual(
                {change["action"] for change in preview["catalog_changes"]},
                {"update"},
            )
            self.assertEqual(preview["organizer_plan"]["moves"], [])
            self.assertEqual(len(preview["shortcuts"]), 2)
            self.assertEqual(
                {row["target_path"] for row in preview["shortcuts"]},
                {str(fixture.root / TARGET_NAME)},
            )
            organizer_issue_codes = {
                issue["code"]
                for issue in preview["organizer_plan"].get("issues") or []
            }
            self.assertTrue(
                organizer_issue_codes.isdisjoint(
                    {
                        "source-not-found",
                        "override-source-not-found",
                        "source-press-override-not-found",
                        "source-work-override-not-found",
                    }
                ),
                organizer_issue_codes,
            )
            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), before)

            media_before_apply = _tree_snapshot(fixture.root)
            original_apply_plan = landing_module.apply_plan
            with _shortcut_sandbox(fixture) as retry_created, patch.object(
                landing_module,
                "apply_plan",
                wraps=original_apply_plan,
            ) as media_apply:
                apply_body = {
                    **body,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": preview["landing_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                }
                with patch(
                    "collection_detail.link_index._create_windows_shortcut",
                    side_effect=subprocess.TimeoutExpired(
                        cmd="create canonical shortcut",
                        timeout=5,
                    ),
                ):
                    pending = apply_work_landing(
                        apply_body,
                        organizer_settings=fixture.organizer_settings,
                        browse_settings=fixture.browse_settings,
                    )

                self.assertFalse(pending["ok"])
                self.assertEqual(pending["state"], "shortcut_pending")
                self.assertEqual(len(pending["catalog"]), 2)
                self.assertEqual(
                    {change["action"] for change in pending["catalog"]},
                    {"update"},
                )
                self.assertEqual(pending["media"]["moved_file_count"], 0)
                self.assertEqual(media_apply.call_count, 1)
                self.assertEqual(_tree_snapshot(fixture.root), media_before_apply)
                self.assertEqual(retry_created, [])

                work_refs = pending["shortcut_retry"]["work_refs"]
                self.assertEqual(
                    {ref["work_name"] for ref in work_refs},
                    {FIRST_WORK, SECOND_WORK},
                )
                self.assertEqual([len(ref["presses"]) for ref in work_refs], [1, 1])
                catalog_before_retry = {
                    path: path.read_bytes()
                    for path in (fixture.first_catalog, fixture.second_catalog)
                }
                retry_body = {
                    "root": str(fixture.root),
                    "work_refs": work_refs,
                }
                retry = preview_work_landing_shortcut_retry(
                    retry_body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(retry["ready"], retry.get("issues"))
                self.assertEqual(len(retry["shortcuts"]), 2)
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

                self.assertTrue(completed["ok"], completed)
                self.assertEqual(completed["shortcuts"]["created_count"], 2)
                self.assertEqual(media_apply.call_count, 1)
                self.assertEqual(_tree_snapshot(fixture.root), media_before_apply)
                self.assertEqual(
                    {
                        path: path.read_bytes()
                        for path in (fixture.first_catalog, fixture.second_catalog)
                    },
                    catalog_before_retry,
                )
                self.assertEqual(len(retry_created), 2)

            for catalog_file in (fixture.first_catalog, fixture.second_catalog):
                entries = load_jp_tv_yaml_file(catalog_file)
                self.assertEqual(len(entries), 1)
                collection = entry_collection_type_data(entries[0])
                self.assertEqual(collection["path"], str(fixture.root))
                self.assertEqual(
                    collection["collectioned"][0]["press_path"],
                    TARGET_NAME,
                )

    def test_empty_existing_shared_target_blocks_zero_move_catalog_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp), already_landed=True)
            fixture.target_file.unlink()
            fixture.first_catalog.write_text(
                _catalog_yaml(
                    name=FIRST_WORK,
                    start="20111001",
                    end="20111224",
                ),
                encoding="utf-8",
            )
            fixture.second_catalog.write_text(
                _catalog_yaml(
                    name=SECOND_WORK,
                    start="20120407",
                    end="20120623",
                ),
                encoding="utf-8",
            )
            binding = fixture.shared_binding
            binding["members"][0]["source_names"] = [TARGET_NAME]
            body = {
                "root": str(fixture.root),
                "shared_target_bindings": [binding],
                "acknowledge_shared_targets": True,
            }
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                preview = preview_work_landing(
                    body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

                self.assertFalse(preview["ready"])
                self.assertIn(
                    "shared-target-empty",
                    {issue["code"] for issue in preview["issues"]},
                )
                self.assertEqual(preview["organizer_plan"]["moves"], [])

            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), before)

    def test_mixed_shared_bindings_block_an_empty_unrouted_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp))
            fixture.first_catalog.write_text(
                _catalog_yaml_with_vcb_and_jsum(
                    name=FIRST_WORK,
                    start="20111001",
                    end="20111224",
                ),
                encoding="utf-8",
            )
            fixture.second_catalog.write_text(
                _catalog_yaml_with_vcb_and_jsum(
                    name=SECOND_WORK,
                    start="20120407",
                    end="20120623",
                ),
                encoding="utf-8",
            )
            empty_target_name = "Fate／Zero_BDRip(Jsum)"
            empty_target = fixture.root / empty_target_name
            empty_target.mkdir()
            empty_category = empty_target / "Fate／Zero_BDRip_Disc"
            empty_category.mkdir()
            first_ref = fixture.catalog_ref(fixture.first_catalog, FIRST_WORK)
            second_ref = fixture.catalog_ref(fixture.second_catalog, SECOND_WORK)
            bindings = [
                {
                    "press_format": "BDRip",
                    "press_group": "VCB",
                    "press_path": TARGET_NAME,
                    "members": [
                        {
                            "catalog_ref": first_ref,
                            "press_key": PRESS_KEY,
                            "source_names": [SOURCE_NAME],
                        },
                        {
                            "catalog_ref": second_ref,
                            "press_key": PRESS_KEY,
                            "source_names": [],
                        },
                    ],
                },
                {
                    "press_format": "BDRip",
                    "press_group": "JSUM",
                    "press_path": empty_target_name,
                    "members": [
                        {
                            "catalog_ref": first_ref,
                            "press_key": "1:main::BDRip:JSUM",
                            "source_names": [],
                        },
                        {
                            "catalog_ref": second_ref,
                            "press_key": "1:main::BDRip:JSUM",
                            "source_names": [],
                        },
                    ],
                },
            ]
            body = {
                "root": str(fixture.root),
                "shared_target_bindings": bindings,
                "acknowledge_shared_targets": True,
            }
            empty_before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                blocked = preview_work_landing(
                    body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

                self.assertFalse(blocked["ready"])
                self.assertIn(
                    "shared-target-empty",
                    {issue["code"] for issue in blocked["issues"]},
                )
                self.assertEqual(len(blocked["organizer_plan"]["moves"]), 1)
                self.assertEqual(len(blocked["shortcuts"]), 4)
                self.assertEqual(created, [])
                self.assertEqual(_tree_snapshot(fixture.base), empty_before)

                existing_media = empty_category / "already-present.mkv"
                existing_media.write_bytes(b"already-present")
                populated_before = _tree_snapshot(fixture.base)
                ready = preview_work_landing(
                    body,
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )

            self.assertTrue(
                ready["ready"],
                {
                    "issues": ready.get("issues"),
                    "organizer_ready": ready["organizer_plan"].get("ready"),
                    "organizer_issues": ready["organizer_plan"].get("issues"),
                    "unresolved": ready["organizer_plan"].get("unresolved_files"),
                    "summary": ready["organizer_plan"].get("summary"),
                },
            )
            self.assertNotIn(
                "shared-target-empty",
                {issue["code"] for issue in ready["issues"]},
            )
            self.assertEqual(len(ready["organizer_plan"]["moves"]), 1)
            self.assertEqual(len(ready["shortcuts"]), 4)
            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), populated_before)

    def test_legacy_shared_database_target_previews_without_ambiguity_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp), already_landed=True)
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                plan = preview_organizer_from_ui_body(
                    {"root": str(fixture.root)},
                    settings=fixture.organizer_settings,
                )["plan"]

                self.assertNotIn(
                    "database-press-target-ambiguous",
                    {issue["code"] for issue in plan.get("issues") or []},
                )
                self.assertEqual(plan["media_state"], "already_organized")
                self.assertEqual(plan["shortcut_summary"]["total_count"], 2)
                self.assertEqual(len(plan["shortcut_scope"]["work_refs"]), 2)
                retry = preview_work_landing_shortcut_retry(
                    {
                        "root": str(fixture.root),
                        "work_refs": plan["shortcut_scope"]["work_refs"],
                    },
                    organizer_settings=fixture.organizer_settings,
                    browse_settings=fixture.browse_settings,
                )
                self.assertTrue(retry["ready"], retry.get("issues"))
                self.assertEqual(len(retry["shortcuts"]), 2)
                self.assertEqual(
                    {row["target_path"] for row in retry["shortcuts"]},
                    {str(fixture.root / TARGET_NAME)},
                )

            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), before)

    def test_legacy_equivalent_bindings_with_loose_media_are_one_physical_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp), already_landed=True)
            loose_file = fixture.make_neutral_target_loose()
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                plan = preview_organizer_from_ui_body(
                    {"root": str(fixture.root)},
                    settings=fixture.organizer_settings,
                )["plan"]

            self.assertNotIn(
                "database-press-target-ambiguous",
                {issue["code"] for issue in plan.get("issues") or []},
            )
            self.assertFalse(plan["ready"])
            self.assertIn(
                "work-ambiguous",
                {issue["code"] for issue in plan.get("issues") or []},
            )
            self.assertTrue(loose_file.is_file())
            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), before)

    def test_legacy_non_equivalent_bindings_remain_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _SharedTargetFixture(Path(temp), already_landed=True)
            # A neutral legacy directory name must not let marker detection
            # silently choose one of two genuinely different press bindings.
            fixture.make_neutral_target_loose()
            fixture.second_catalog.write_text(
                fixture.second_catalog.read_text(encoding="utf-8").replace(
                    "press_group: VCB",
                    "press_group: JSUM",
                ),
                encoding="utf-8",
            )
            before = _tree_snapshot(fixture.base)

            with _shortcut_sandbox(fixture) as created:
                plan = preview_organizer_from_ui_body(
                    {"root": str(fixture.root)},
                    settings=fixture.organizer_settings,
                )["plan"]

            self.assertIn(
                "database-press-target-ambiguous",
                {issue["code"] for issue in plan.get("issues") or []},
            )
            self.assertEqual(created, [])
            self.assertEqual(_tree_snapshot(fixture.base), before)


if __name__ == "__main__":
    unittest.main()
