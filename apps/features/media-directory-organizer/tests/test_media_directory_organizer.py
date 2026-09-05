from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from collection_detail.save import (
    CatalogRollbackConflictError,
    browse_save_yaml_from_ui_body,
    catalog_write_transaction,
)
from media_directory_organizer.catalog import CatalogWork, MediaCatalog, PressRecord
from media_directory_organizer.classification import DEFAULT_CLASSIFIER_REGISTRY
from media_directory_organizer.landing import discover_catalog_work_draft
from media_directory_organizer.service import apply_plan, build_plan
from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.web import (
    apply_organizer_catalog_shortcut_repair_from_ui_body,
    apply_organizer_landing_shortcuts_from_ui_body,
    apply_organizer_landing_from_ui_body,
    apply_organizer_from_ui_body,
    organizer_config_payload,
    preview_organizer_catalog_shortcut_repair_from_ui_body,
    preview_organizer_landing_shortcuts_from_ui_body,
    preview_organizer_landing_from_ui_body,
    preview_organizer_from_ui_body,
)
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import entry_collection_type_data


BASE_WORK = "Little Busters!"
REFRAIN_WORK = "Little Busters! Refrain"
EX_WORK = "Little Busters! EX"

BASE_JSUM_TARGET = "Little Busters!_BDRip(Jsum)"
BASE_MW_TARGET = "Little Busters!_BDRip(MW)"
REFRAIN_JSUM_TARGET = "Little Busters! Refrain_BDRip(Jsum)"
REFRAIN_VCB_TARGET = "Little Busters! Refrain_BDRip(VCBM)"
EX_JSUM_TARGET = "Little Busters! EX_BDRip(Jsum)"
EX_VCB_TARGET = "Little Busters! EX_BDRip(VCBM)"

CATALOG_YAML = """\
- attributes:
  - type: date
    data:
      start: '20121006'
      end: '20130406'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: Little Busters!_BDRip(Jsum)
      - press_format: BDRip
        press_group: MW
        press_path: Little Busters!_BDRip(MW)
      markers: []
      path: 'WORK_ROOT'
  - type: country
    data: japan
  - type: name
    data: Little Busters!
- attributes:
  - type: date
    data:
      start: '20131005'
      end: '20131228'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: Little Busters! Refrain_BDRip(Jsum)
      - press_format: BDRip
        press_group: VCB
        press_path: Little Busters! Refrain_BDRip(VCBM)
      markers: []
      path: 'WORK_ROOT'
  - type: country
    data: japan
  - type: name
    data: Little Busters! Refrain
- attributes:
  - type: date
    data:
      start: '20140129'
      end: '20140730'
  - type: collection-type
    data:
      domain: animation
      release_type: ova
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: Little Busters! EX_BDRip(Jsum)
      - press_format: BDRip
        press_group: VCB
        press_path: Little Busters! EX_BDRip(VCBM)
      markers: []
      path: 'WORK_ROOT'
  - type: country
    data: japan
  - type: name
    data: Little Busters! EX
"""


EXPECTED_TARGETS = {
    (REFRAIN_WORK, "JSUM"): REFRAIN_JSUM_TARGET,
    (REFRAIN_WORK, "VCB"): REFRAIN_VCB_TARGET,
    (EX_WORK, "JSUM"): EX_JSUM_TARGET,
    (EX_WORK, "VCB"): EX_VCB_TARGET,
}


def _settings(catalog_root: Path, allowed_root: Path, work_root: Path) -> OrganizerSettings:
    return OrganizerSettings(
        catalog_root=catalog_root,
        allowed_resource_roots=(allowed_root,),
        format_markers={"BDRip": ("bdrip", "bd 1920x1080")},
        group_markers={
            "VCB": ("vcb-studio", "vcb"),
            "JSUM": ("jsum",),
            "MW": ("mawen1250",),
        },
        group_suffixes={"JSUM": "Jsum", "VCB": "VCBM", "MW": "MW"},
        max_files=100,
        default_work_root=work_root,
        work_aliases={
            REFRAIN_WORK: (
                "Little Busters! Refrain",
                "Little Busters! ～Refrain～",
                "リトルバスターズ！～Refrain～",
            ),
            EX_WORK: ("Little Busters! EX", "リトルバスターズ！EX"),
        },
    )


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


def _write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class MediaDirectoryOrganizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._shortcut_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._shortcut_temp.cleanup)
        shortcut_base = Path(self._shortcut_temp.name)
        shortcut_root = shortcut_base / "shortcuts"
        shortcut_root.mkdir()
        self._shortcut_root = shortcut_root
        index_path = shortcut_base / "cache" / "link-index.yaml"

        def shortcut_target(path: Path) -> str:
            try:
                return path.read_text(encoding="utf-8") if path.is_file() else ""
            except (OSError, UnicodeError):
                return ""

        def create_shortcut(path: Path, target: Path) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(target), encoding="utf-8")

        patchers = (
            patch(
                "collection_detail.link_index.resource_roots",
                return_value=[Path(tempfile.gettempdir())],
            ),
            patch(
                "collection_detail.link_index.shortcut_roots",
                return_value=[shortcut_root],
            ),
            patch(
                "collection_detail.link_index._shortcut_root_for_work",
                return_value=shortcut_root,
            ),
            patch(
                "collection_detail.link_index._layout_levels",
                return_value=("{year_label}", "{name}"),
            ),
            patch(
                "collection_detail.link_index._shortcut_name_template",
                return_value="{press_format}{press_group_suffix}",
            ),
            patch(
                "collection_detail.link_index._link_index_db_path",
                return_value=index_path,
            ),
            patch(
                "collection_detail.link_index._windows_shortcut_target",
                side_effect=shortcut_target,
            ),
            patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=create_shortcut,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_shared_root_discovers_and_lands_two_works_with_two_presses_each(self) -> None:
        self.maxDiff = None
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Zombie Land Saga"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            source_names = (
                "[2018][Zombieland Saga][BDRIP][1080P][1-12Fin+SP]",
                "[Nekomoe kissaten&VCB-Studio] Zombie Land Saga [Ma10p_1080p]",
                "[2021][Zombie Land Saga Revenge][BDRIP][1080P][1-12Fin+SP]",
                "[VCB-Studio] Zombie Land Saga Revenge [Ma10p_1080p]",
            )
            originals = [
                _write_file(root / source / f"[{source}][01].mkv", source.encode("utf-8"))
                for source in source_names
            ]
            settings = replace(
                _settings(catalog_root, base, root),
                format_markers={
                    **_settings(catalog_root, base, root).format_markers,
                    "1080p": ("1080p",),
                },
                group_suffixes={"JSUM": "Jsum", "VCB": "VCB", "VCBM": "VCBM"},
            )
            detail = _browse_settings(catalog_root)

            initial = preview_organizer_from_ui_body(
                {"root": str(root)}, settings=settings
            )["plan"]
            drafts = initial["registration"]["work_drafts"]
            self.assertEqual(
                [draft["name"] for draft in drafts],
                ["Zombie Land Saga", "Zombie Land Saga Revenge"],
            )
            self.assertEqual([len(draft["presses"]) for draft in drafts], [2, 2])
            discovered = {
                str(press["source_names"][0]): {
                    "work_name": draft["name"],
                    "press_format": press["press_format"],
                    "press_group": press["press_group"],
                    "press_path": press["press_path"],
                    "needs_confirmation": press["needs_confirmation"],
                }
                for draft in drafts
                for press in draft["presses"]
            }
            self.assertEqual(
                discovered,
                {
                    source_names[0]: {
                        "work_name": "Zombie Land Saga",
                        "press_format": "BDRip",
                        "press_group": "JSUM",
                        "press_path": "Zombie Land Saga_BDRip(Jsum)",
                        "needs_confirmation": True,
                    },
                    source_names[1]: {
                        "work_name": "Zombie Land Saga",
                        "press_format": "BDRip",
                        "press_group": "VCBM",
                        "press_path": "Zombie Land Saga_BDRip(VCBM)",
                        "needs_confirmation": True,
                    },
                    source_names[2]: {
                        "work_name": "Zombie Land Saga Revenge",
                        "press_format": "BDRip",
                        "press_group": "JSUM",
                        "press_path": "Zombie Land Saga Revenge_BDRip(Jsum)",
                        "needs_confirmation": True,
                    },
                    source_names[3]: {
                        "work_name": "Zombie Land Saga Revenge",
                        "press_format": "BDRip",
                        "press_group": "VCB",
                        "press_path": "Zombie Land Saga Revenge_BDRip(VCB)",
                        "needs_confirmation": True,
                    },
                },
            )
            for draft in drafts:
                draft["date"] = {
                    "start": "2018-10-04" if draft["name"] == "Zombie Land Saga" else "2021-04-08",
                    "end": "",
                }

            preview = preview_organizer_landing_from_ui_body(
                {"root": str(root), "draft_works": drafts},
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(preview["ready"], preview["organizer_plan"].get("issues"))
            self.assertEqual(len(preview["catalog_changes"]), 2)
            self.assertEqual(len(preview["shortcuts"]), 4)
            self.assertEqual(
                {row["work_name"] for row in preview["shortcuts"]},
                {"Zombie Land Saga", "Zombie Land Saga Revenge"},
            )
            jsum_targets = {
                row["work_name"]: Path(row["target_path"]).name
                for row in preview["shortcuts"]
                if row["press_group"] == "JSUM"
            }
            self.assertEqual(
                jsum_targets,
                {
                    "Zombie Land Saga": "Zombie Land Saga_BDRip(Jsum)",
                    "Zombie Land Saga Revenge": "Zombie Land Saga Revenge_BDRip(Jsum)",
                },
            )
            self.assertEqual(
                {
                    (row["work_name"], row["press_group"]): Path(row["target_path"]).name
                    for row in preview["shortcuts"]
                },
                {
                    ("Zombie Land Saga", "JSUM"): "Zombie Land Saga_BDRip(Jsum)",
                    ("Zombie Land Saga", "VCBM"): "Zombie Land Saga_BDRip(VCBM)",
                    ("Zombie Land Saga Revenge", "JSUM"): "Zombie Land Saga Revenge_BDRip(Jsum)",
                    ("Zombie Land Saga Revenge", "VCB"): "Zombie Land Saga Revenge_BDRip(VCB)",
                },
            )
            self.assertFalse(any(catalog_root.glob("*.yaml")))
            self.assertTrue(all(path.is_file() for path in originals))

            with patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=OSError("simulated initial shortcut failure"),
            ):
                result = apply_organizer_landing_from_ui_body(
                    {
                        "root": str(root),
                        "draft_works": drafts,
                        "landing_plan_id": preview["landing_plan_id"],
                        "confirmation": preview["landing_plan_id"],
                        "acknowledge_catalog_write": True,
                        "acknowledge_move": True,
                        "acknowledge_shortcuts": True,
                    },
                    settings=settings,
                    browse_settings=detail,
                )
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["state"], "shortcut_pending")
            self.assertEqual(result["media"]["moved_file_count"], 4)
            self.assertEqual(
                sum(len(load_jp_tv_yaml_file(path)) for path in catalog_root.glob("*.yaml")),
                2,
            )
            work_refs = result["shortcut_retry"]["work_refs"]
            self.assertEqual(
                {ref["work_name"]: len(ref["presses"]) for ref in work_refs},
                {"Zombie Land Saga": 2, "Zombie Land Saga Revenge": 2},
            )

            legacy_target = root / "Zombie Legacy_BDRip(VCB)"
            _write_file(legacy_target / "legacy.mkv", b"legacy")
            (catalog_root / "[JP][TVInfo][2010].yaml").write_text(
                """\
- attributes:
  - type: date
    data:
      start: '20100101'
      end: ''
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: VCB
        press_path: Zombie Legacy_BDRip(VCB)
      markers: []
      path: '__ROOT__'
  - type: country
    data: japan
  - type: name
    data: Zombie Legacy
""".replace("__ROOT__", str(root).replace("'", "''")),
                encoding="utf-8",
            )
            retry = preview_organizer_landing_shortcuts_from_ui_body(
                {"root": str(root), "work_refs": work_refs},
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(retry["ready"], retry["issues"])
            self.assertEqual(retry["shortcut_summary"]["total_count"], 4)
            self.assertNotIn("Zombie Legacy", {row["work_name"] for row in retry["shortcuts"]})
            retried = apply_organizer_landing_shortcuts_from_ui_body(
                {
                    "root": str(root),
                    "work_refs": work_refs,
                    "retry_plan_id": retry["retry_plan_id"],
                    "confirmation": retry["retry_plan_id"],
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(retried["ok"], retried)
            self.assertEqual(retried["shortcuts"]["created_count"], 4)

    def test_multi_work_landing_composes_same_year_catalog_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Shared"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            drafts = []
            for name in ("Shared One", "Shared Two"):
                source = f"[{name}][BDRip][VCB]"
                _write_file(root / source / f"[{name}][01].mkv", name.encode("utf-8"))
                drafts.append(
                    {
                        "name": name,
                        "date": {"start": "2020-01-01", "end": ""},
                        "domain": "animation",
                        "country": "japan",
                        "release_type": "tv",
                        "path": str(root),
                        "presses": [
                            {
                                "source_names": [source],
                                "press_format": "BDRip",
                                "press_group": "VCB",
                                "press_path": f"{name}_BDRip(VCBM)",
                            }
                        ],
                    }
                )
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            preview = preview_organizer_landing_from_ui_body(
                {"root": str(root), "draft_works": drafts},
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(preview["ready"], preview["organizer_plan"].get("issues"))
            self.assertEqual(
                [change["index_in_file"] for change in preview["catalog_changes"]],
                [0, 1],
            )
            self.assertEqual(
                len({change["after_sha256"] for change in preview["catalog_changes"]}),
                1,
            )
            result = apply_organizer_landing_from_ui_body(
                {
                    "root": str(root),
                    "draft_works": drafts,
                    "landing_plan_id": preview["landing_plan_id"],
                    "confirmation": preview["landing_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_move": True,
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(result["ok"], result)
            catalog_file = catalog_root / "[JP][TVInfo][2020].yaml"
            self.assertEqual(len(load_jp_tv_yaml_file(catalog_file)), 2)

    def test_multi_work_discovery_prefers_title_brackets_and_preserves_combined_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            catalog_root = base / "db"
            catalog_root.mkdir()

            split_root = base / "Bracket Batch"
            split_root.mkdir()
            _write_file(
                split_root
                / "[VCB-Studio][Kekkai Sensen & Beyond][BDRIP]"
                / "episode.mkv",
                b"one",
            )
            _write_file(
                split_root / "[2020][Other Work][BDRIP][1080P]" / "episode.mkv",
                b"two",
            )
            split_plan = preview_organizer_from_ui_body(
                {"root": str(split_root)},
                settings=_settings(catalog_root, base, split_root),
            )["plan"]
            self.assertEqual(
                {draft["name"] for draft in split_plan["registration"]["work_drafts"]},
                {"Kekkai Sensen & Beyond", "Other Work"},
            )

            combined_root = base / "Combined Batch"
            combined_root.mkdir()
            _write_file(
                combined_root / "[2020][Work A+Work B][BDRIP]" / "episode.mkv",
                b"combined",
            )
            _write_file(
                combined_root / "[2021][Work C][BDRIP]" / "episode.mkv",
                b"single",
            )
            combined_plan = preview_organizer_from_ui_body(
                {"root": str(combined_root)},
                settings=_settings(catalog_root, base, combined_root),
            )["plan"]
            self.assertEqual(combined_plan["registration"]["work_drafts"], [])
            self.assertEqual(combined_plan["registration"]["draft"]["name"], "Combined Batch")
            combined_source = next(
                source
                for source in combined_plan["registration"]["sources"]
                if "Work A+Work B" in source["name"]
            )
            self.assertEqual(combined_source["work_title_hint"], "Work A+Work B")
            self.assertEqual(combined_source["work_title_hint_kind"], "combined")
            self.assertEqual(combined_source["suggested_work_name"], "")

    def test_catalog_prefix_similarity_without_exact_anchor_requires_registration(self) -> None:
        catalog = MediaCatalog(
            works=(
                CatalogWork(
                    name="Air Gear",
                    path="",
                    domain="animation",
                    country="japan",
                    release_type="tv",
                    presses=(),
                    source_file="catalog.yaml",
                ),
            ),
            catalog_root=Path("catalog"),
        )

        self.assertEqual(catalog.matching_family_for_root(Path("Air")), ())
        self.assertEqual(
            [work.name for work in catalog.matching_family_for_root(Path("Air Gear"))],
            ["Air Gear"],
        )

    def test_nonprefix_works_share_root_and_organize_in_place_without_collapsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "グリザイアの果実"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            works = tuple(
                CatalogWork(
                    name=name,
                    path="",
                    domain="animation",
                    country="japan",
                    release_type="tv",
                    presses=(
                        PressRecord("BDRip", "JSUM"),
                        PressRecord("BDRip", "VCB"),
                    ),
                    source_file=str(catalog_root / source_file),
                )
                for name, source_file in (
                    ("グリザイアの果実", "[JP][TVInfo][2014].yaml"),
                    ("グリザイアの迷宮", "[JP][TVInfo][2015].yaml"),
                    ("グリザイアの楽園", "[JP][TVInfo][2015].yaml"),
                )
            )
            catalog = MediaCatalog(works=works, catalog_root=catalog_root)
            settings = _settings(catalog_root, base, root)
            expected_files: dict[tuple[str, str], Path] = {}
            for work in works:
                for group, suffix in (("JSUM", "Jsum"), ("VCB", "VCBM")):
                    press_dir = root / f"{work.name}_BDRip({suffix})"
                    expected_files[(work.name, group)] = _write_file(
                        press_dir / f"[{work.name}][01].mkv",
                        f"{work.name}-{group}".encode("utf-8"),
                    )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(
                set(plan["family_works"]),
                {"グリザイアの果実", "グリザイアの迷宮", "グリザイアの楽園"},
            )
            self.assertEqual(len(plan["assignments"]), 6)
            self.assertEqual(len(plan["moves"]), 6)
            self.assertEqual(plan["unresolved_files"], [])
            self.assertNotIn(
                "planned-collision",
                {issue["code"] for issue in plan["issues"]},
            )
            for assignment in plan["assignments"]:
                self.assertTrue(assignment["in_place"])
                self.assertEqual(
                    Path(assignment["source_dir"]),
                    Path(assignment["target_dir"]),
                )
                self.assertTrue(
                    Path(assignment["target_relpath"]).name.startswith(
                        assignment["work_name"] + "_"
                    )
                )

            applied = apply_plan(plan, confirmation=plan["plan_id"])
            self.assertEqual(applied["moved_file_count"], 6)
            self.assertEqual(applied["cleanup_warnings"], [])
            for (work_name, group), original in expected_files.items():
                suffix = "Jsum" if group == "JSUM" else "VCBM"
                destination = (
                    root
                    / f"{work_name}_BDRip({suffix})"
                    / f"{work_name}_BDRip_Disc"
                    / original.name
                )
                self.assertTrue(destination.is_file(), destination)
                self.assertFalse(original.is_file())

            settled = build_plan(root, catalog=catalog, settings=settings)
            self.assertFalse(settled["ready"])
            self.assertEqual(settled["assignments"], [])
            self.assertEqual(settled["moves"], [])
            self.assertEqual(settled["unresolved_files"], [])
            self.assertEqual(settled["issues"], [])

    def test_nonprefix_shared_root_preview_requires_three_exact_database_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "グリザイアの果実"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][test].yaml"
            work_names = ("グリザイアの果実", "グリザイアの迷宮", "グリザイアの楽園")
            catalog_file.write_text(
                "".join(
                    f"""\
- attributes:
  - type: date
    data: {{start: '20150101', end: '20150301'}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
      - press_format: BDRip
        press_group: VCB
      markers: []
  - type: country
    data: japan
  - type: name
    data: {name}
"""
                    for name in work_names
                ),
                encoding="utf-8",
            )
            media_files: list[Path] = []
            for name in work_names:
                for suffix in ("Jsum", "VCBM"):
                    media_files.append(
                        _write_file(
                            root / f"{name}_BDRip({suffix})" / f"[{name}][01].mkv",
                            f"{name}-{suffix}".encode("utf-8"),
                        )
                    )
            catalog_before = catalog_file.read_bytes()
            media_before = {
                str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in media_files
            }

            settings = _settings(catalog_root, base, root)
            plan = preview_organizer_from_ui_body(
                {"root": str(root)},
                settings=settings,
            )["plan"]

            self.assertFalse(plan["ready"])
            self.assertEqual(len(plan["assignments"]), 6)
            self.assertEqual(len(plan["moves"]), 6)
            self.assertEqual(plan["unresolved_files"], [])
            self.assertNotIn(
                "planned-collision",
                {issue["code"] for issue in plan["issues"]},
            )
            path_issues = [
                issue
                for issue in plan["issues"]
                if issue["code"] == "shortcut-catalog-path-missing"
            ]
            self.assertEqual(
                {issue["work_name"] for issue in path_issues},
                set(work_names),
            )
            self.assertTrue(plan["repair_required"])
            candidates = plan["repair"]["candidates"]
            self.assertEqual(
                {candidate["catalog_ref"]["work_name"] for candidate in candidates},
                set(work_names),
            )
            for candidate in candidates:
                draft = candidate["draft_work"]
                self.assertEqual(Path(draft["path"]), root)
                self.assertEqual(
                    {press["press_path"] for press in draft["presses"]},
                    {
                        f"{draft['name']}_BDRip(Jsum)",
                        f"{draft['name']}_BDRip(VCBM)",
                    },
                )
            self.assertEqual(catalog_file.read_bytes(), catalog_before)
            self.assertEqual(
                {
                    str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in media_files
                },
                media_before,
            )

            first = candidates[0]
            repair_body = {
                "root": str(root),
                "draft_work": first["draft_work"],
                "catalog_ref": first["catalog_ref"],
            }
            unsafe_combined_preview = (
                preview_organizer_catalog_shortcut_repair_from_ui_body(
                    {**repair_body, "include_media_move": True},
                    settings=settings,
                    browse_settings=_browse_settings(catalog_root),
                )
            )
            self.assertFalse(unsafe_combined_preview["ready"])
            self.assertIn(
                "repair-media-plan-multiple-works",
                {issue["code"] for issue in unsafe_combined_preview["issues"]},
            )
            self.assertEqual(catalog_file.read_bytes(), catalog_before)
            self.assertEqual(
                {
                    str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in media_files
                },
                media_before,
            )

            repair_preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                repair_body,
                settings=settings,
                browse_settings=_browse_settings(catalog_root),
            )
            self.assertTrue(repair_preview["ready"], repair_preview["issues"])
            self.assertFalse(repair_preview["media_move_planned"])
            self.assertEqual(repair_preview["shortcut_summary"]["total_count"], 2)
            self.assertEqual(repair_preview["shortcut_summary"]["planned_count"], 2)
            result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                {
                    **repair_body,
                    "repair_plan_id": repair_preview["repair_plan_id"],
                    "confirmation": repair_preview["repair_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=_browse_settings(catalog_root),
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["shortcuts"]["created_count"], 2)
            after_first_repair = preview_organizer_from_ui_body(
                {"root": str(root)},
                settings=settings,
            )["plan"]
            self.assertEqual(
                {
                    candidate["catalog_ref"]["work_name"]
                    for candidate in after_first_repair["repair"]["candidates"]
                },
                set(work_names) - {first["catalog_ref"]["work_name"]},
            )
            self.assertEqual(
                {
                    str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in media_files
                },
                media_before,
            )

    def test_arbitrary_database_press_path_identifies_in_place_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Canonical Work"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            source = root / "Release Bucket 01"
            media = _write_file(source / "episode.mkv", b"episode")
            legacy_empty = source / "CDs" / "Empty Album"
            legacy_empty.mkdir(parents=True)
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Canonical Work",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord(
                                "BDRip",
                                "VCB",
                                press_path=source.name,
                            ),
                        ),
                        source_file=str(catalog_root / "catalog.yaml"),
                    ),
                ),
                catalog_root=catalog_root,
            )

            plan = build_plan(
                root,
                catalog=catalog,
                settings=_settings(catalog_root, base, root),
            )

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(len(plan["assignments"]), 1)
            assignment = plan["assignments"][0]
            self.assertEqual(assignment["work_name"], "Canonical Work")
            self.assertEqual(assignment["press_format"], "BDRip")
            self.assertEqual(assignment["press_group"], "VCB")
            self.assertEqual(assignment["target_authority"], "database_press_path")
            self.assertEqual(assignment["target_relpath"], source.name)
            self.assertTrue(assignment["in_place"])
            self.assertEqual(Path(assignment["source_dir"]), source)
            self.assertEqual(Path(assignment["target_dir"]), source)
            self.assertEqual(
                Path(plan["moves"][0]["target"]),
                source / "Release Bucket 01_Disc" / media.name,
            )
            result = apply_plan(plan, confirmation=plan["plan_id"])
            self.assertEqual(result["moved_file_count"], 1)
            self.assertEqual(result["cleaned_directory_count"], 2)
            self.assertTrue(source.is_dir())
            self.assertFalse((source / "CDs").exists())
            self.assertTrue(
                (source / "Release Bucket 01_Disc" / media.name).is_file()
            )

    def test_press_named_sources_with_legacy_content_finish_internal_layout_before_settling(
        self,
    ) -> None:
        cases = (
            ("Exact Work", "Exact Work_BDRip", "Exact Work_BDRip", ""),
            (
                "Exact Work",
                "Exact Work_BDRip(VCBM)",
                "Exact Work_BDRip",
                "Exact Work_BDRip(VCBM)",
            ),
            (
                "Exact_Work_Name",
                "Exact_Work_Name_BDRip(VCBM)",
                "Exact_Work_Name_BDRip",
                "Exact_Work_Name_BDRip(VCBM)",
            ),
        )
        for work_name, source_name, category_stem, press_path in cases:
            with self.subTest(source=source_name), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "Collection Root"
                root.mkdir()
                catalog_root = base / "db"
                catalog_root.mkdir()
                source = root / source_name
                existing = _write_file(
                    source / f"{category_stem}_Disc" / "existing.mkv",
                    b"already-canonical",
                )
                root_video = _write_file(source / "episode.mkv", b"loose-video")
                cd_audio = _write_file(
                    source / "CDs" / "Album" / "track.flac",
                    b"legacy-cd",
                )
                scan_image = _write_file(
                    source / "Scans" / "booklet.jpg",
                    b"legacy-scan",
                )
                existing_before = (
                    existing.read_bytes(),
                    existing.stat().st_mtime_ns,
                )
                catalog = MediaCatalog(
                    works=(
                        CatalogWork(
                            name=work_name,
                            path=str(root),
                            domain="animation",
                            country="japan",
                            release_type="tv",
                            presses=(
                                PressRecord(
                                    "BDRip",
                                    "VCB",
                                    press_path=press_path,
                                ),
                            ),
                            source_file=str(catalog_root / "catalog.yaml"),
                        ),
                    ),
                    catalog_root=catalog_root,
                )
                settings = _settings(catalog_root, base, root)

                plan = build_plan(root, catalog=catalog, settings=settings)

                self.assertTrue(plan["ready"], plan["issues"])
                self.assertEqual(plan["issues"], [])
                self.assertEqual(plan["summary"]["source_directory_count"], 1)
                self.assertEqual(len(plan["assignments"]), 1)
                assignment = plan["assignments"][0]
                self.assertEqual(assignment["work_name"], work_name)
                self.assertEqual(Path(assignment["source_dir"]), source)
                self.assertEqual(Path(assignment["target_dir"]), source)
                self.assertTrue(assignment["in_place"])
                self.assertEqual(
                    assignment["target_authority"],
                    "database_press_path" if press_path else "derived_from_catalog",
                )
                moves = {
                    Path(move["source"]): Path(move["target"])
                    for move in plan["moves"]
                }
                self.assertEqual(set(moves), {root_video, cd_audio, scan_image})
                self.assertEqual(
                    moves[root_video],
                    source / f"{category_stem}_Disc" / root_video.name,
                )
                self.assertEqual(
                    moves[cd_audio],
                    source / f"{category_stem}_CD" / "Album" / cd_audio.name,
                )
                self.assertEqual(
                    moves[scan_image],
                    source / f"{category_stem}_Image" / scan_image.name,
                )
                self.assertNotIn(
                    existing.resolve(),
                    {Path(move["source"]).resolve() for move in plan["moves"]},
                )
                binding_names = {
                    str(row["source_name"])
                    for row in plan["source_work_bindings"]
                }
                self.assertEqual(binding_names, {source_name})
                self.assertNotIn(f"{category_stem}_CD", binding_names)
                self.assertNotEqual(
                    plan["source_work_bindings"][0]["state"],
                    "settled",
                )

                result = apply_plan(plan, confirmation=plan["plan_id"])

                self.assertEqual(result["moved_file_count"], 3)
                self.assertEqual(
                    (existing.read_bytes(), existing.stat().st_mtime_ns),
                    existing_before,
                )
                self.assertFalse((source / "CDs").exists())
                self.assertFalse((source / "Scans").exists())
                checked = build_plan(root, catalog=catalog, settings=settings)
                self.assertEqual(checked["assignments"], [])
                self.assertEqual(checked["moves"], [])
                self.assertEqual(checked["issues"], [])
                checked_rows = {
                    str(row["source_name"]): row
                    for row in checked["source_work_bindings"]
                }
                self.assertEqual(set(checked_rows), {source_name})
                self.assertEqual(checked_rows[source_name]["state"], "settled")

    def test_empty_press_targets_are_not_settled_until_canonical_media_exists(self) -> None:
        for with_empty_category in (False, True):
            with self.subTest(empty_category=with_empty_category), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "Exact Work"
                root.mkdir()
                catalog_root = base / "db"
                catalog_root.mkdir()
                target = root / "Exact Work_BDRip(VCBM)"
                target.mkdir()
                if with_empty_category:
                    (target / "Exact Work_BDRip_Disc").mkdir()
                catalog = MediaCatalog(
                    works=(
                        CatalogWork(
                            name="Exact Work",
                            path=str(root),
                            domain="animation",
                            country="japan",
                            release_type="tv",
                            presses=(
                                PressRecord(
                                    "BDRip",
                                    "VCB",
                                    press_path=target.name,
                                ),
                            ),
                            source_file=str(catalog_root / "catalog.yaml"),
                        ),
                    ),
                    catalog_root=catalog_root,
                )
                settings = _settings(catalog_root, base, root)

                empty = build_plan(root, catalog=catalog, settings=settings)

                self.assertFalse(empty["ready"])
                self.assertEqual(empty["assignments"], [])
                self.assertEqual(empty["moves"], [])
                self.assertIn("empty-source", {issue["code"] for issue in empty["issues"]})
                empty_row = next(
                    row
                    for row in empty["source_work_bindings"]
                    if row["source_name"] == target.name
                )
                self.assertNotEqual(empty_row["state"], "settled")

                canonical_media = _write_file(
                    target / "Exact Work_BDRip_Disc" / "episode.mkv",
                    b"canonical-media",
                )
                settled = build_plan(root, catalog=catalog, settings=settings)

                self.assertEqual(settled["assignments"], [])
                self.assertEqual(settled["moves"], [])
                self.assertEqual(settled["issues"], [])
                settled_row = next(
                    row
                    for row in settled["source_work_bindings"]
                    if row["source_name"] == target.name
                )
                self.assertEqual(settled_row["state"], "settled")
                self.assertTrue(canonical_media.is_file())

    def test_registration_title_strips_press_suffix_but_not_category_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Unmatched Collection Root"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            press_source = root / "Exact_Work_Name_BDRip(VCBM)"
            plain_press_source = root / "Exact_Plain_Name_BDRip"
            web_press_source = root / "Web_Show_Name_WebRip(JSUM)"
            substring_source = root / "WebRipples_BDRip"
            category_source = root / "Exact_Work_Name_BDRip_CD"
            press_source.mkdir()
            plain_press_source.mkdir()
            web_press_source.mkdir()
            substring_source.mkdir()
            category_source.mkdir()
            catalog = MediaCatalog(
                works=(),
                catalog_root=catalog_root,
            )
            settings = _settings(catalog_root, base, root)
            settings = replace(
                settings,
                format_markers={
                    **settings.format_markers,
                    "WebRip": ("webrip", "web-rip", "web-dl"),
                },
            )

            discovery = discover_catalog_work_draft(
                root,
                catalog=catalog,
                settings=settings,
            )

            sources = {row["name"]: row for row in discovery["sources"]}
            self.assertEqual(
                sources[press_source.name]["work_title_hint"],
                "Exact_Work_Name",
            )
            self.assertEqual(
                sources[press_source.name]["suggested_work_name"],
                "Exact_Work_Name",
            )
            self.assertEqual(
                sources[press_source.name]["work_title_hint_kind"],
                "single",
            )
            self.assertEqual(
                sources[plain_press_source.name]["work_title_hint"],
                "Exact_Plain_Name",
            )
            self.assertEqual(
                sources[plain_press_source.name]["suggested_work_name"],
                "Exact_Plain_Name",
            )
            self.assertEqual(
                sources[web_press_source.name]["work_title_hint"],
                "Web_Show_Name",
            )
            self.assertEqual(
                sources[web_press_source.name]["suggested_work_name"],
                "Web_Show_Name",
            )
            self.assertEqual(
                sources[substring_source.name]["work_title_hint"],
                "WebRipples",
            )
            self.assertEqual(
                sources[substring_source.name]["suggested_work_name"],
                "WebRipples",
            )
            self.assertEqual(sources[category_source.name]["work_title_hint"], "")
            self.assertEqual(sources[category_source.name]["suggested_work_name"], "")
            self.assertEqual(
                sources[category_source.name]["work_title_hint_kind"],
                "ambiguous",
            )
            web_plan = preview_organizer_from_ui_body(
                {"root": str(root)},
                settings=settings,
            )["plan"]
            binding = next(
                row
                for row in web_plan["source_work_bindings"]
                if row["source_name"] == press_source.name
            )
            self.assertEqual(binding["suggested_work_name"], "Exact_Work_Name")
            self.assertEqual(
                binding["registration"]["draft"]["name"],
                "Exact_Work_Name",
            )

    def test_press_directory_title_does_not_bind_another_existing_family_work(self) -> None:
        for source_name in ("Exact Work_BDRip", "Exact Work_BDRip(VCBM)"):
            with self.subTest(source=source_name), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root = base / "Existing Family"
                root.mkdir()
                catalog_root = base / "db"
                catalog_root.mkdir()
                source = root / source_name
                media = _write_file(source / "episode.mkv", b"neutral-video")
                exact_target = "Exact Work Canonical"
                other_target = "Other Work Canonical"
                catalog = MediaCatalog(
                    works=(
                        CatalogWork(
                            name="Exact Work",
                            path=str(root),
                            domain="animation",
                            country="japan",
                            release_type="tv",
                            presses=(
                                PressRecord("BDRip", "VCB", press_path=exact_target),
                            ),
                            source_file=str(catalog_root / "catalog.yaml"),
                            source_index=0,
                        ),
                        CatalogWork(
                            name="Other Work",
                            path=str(root),
                            domain="animation",
                            country="japan",
                            release_type="tv",
                            presses=(
                                PressRecord("BDRip", "VCB", press_path=other_target),
                            ),
                            source_file=str(catalog_root / "catalog.yaml"),
                            source_index=1,
                        ),
                    ),
                    catalog_root=catalog_root,
                )

                plan = build_plan(
                    root,
                    catalog=catalog,
                    settings=_settings(catalog_root, base, root),
                )

                self.assertTrue(plan["ready"], plan["issues"])
                self.assertEqual(len(plan["assignments"]), 1)
                assignment = plan["assignments"][0]
                self.assertEqual(assignment["work_name"], "Exact Work")
                self.assertEqual(Path(assignment["source_dir"]), source)
                self.assertEqual(
                    Path(assignment["target_dir"]),
                    root / exact_target,
                )
                self.assertNotEqual(Path(assignment["target_dir"]), root / other_target)
                self.assertEqual(len(plan["moves"]), 1)
                self.assertEqual(Path(plan["moves"][0]["source"]), media)

    def test_unique_directory_work_authority_overrides_other_work_in_file_brackets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Existing Family"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            source = root / "Exact Work_BDRip"
            misleading = _write_file(
                source / "[Other Work][01].mkv",
                b"misleading-file-title",
            )
            exact_target = "Exact Work Canonical"
            other_target = "Other Work Canonical"
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Exact Work",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("BDRip", "VCB", press_path=exact_target),
                        ),
                        source_file=str(catalog_root / "catalog.yaml"),
                        source_index=0,
                    ),
                    CatalogWork(
                        name="Other Work",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("BDRip", "VCB", press_path=other_target),
                        ),
                        source_file=str(catalog_root / "catalog.yaml"),
                        source_index=1,
                    ),
                ),
                catalog_root=catalog_root,
            )

            plan = build_plan(
                root,
                catalog=catalog,
                settings=_settings(catalog_root, base, root),
            )

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["issues"], [])
            self.assertEqual(len(plan["assignments"]), 1)
            assignment = plan["assignments"][0]
            self.assertEqual(assignment["work_name"], "Exact Work")
            self.assertEqual(Path(assignment["target_dir"]), root / exact_target)
            self.assertEqual(len(plan["moves"]), 1)
            self.assertEqual(Path(plan["moves"][0]["source"]), misleading)
            self.assertTrue(
                Path(plan["moves"][0]["target"]).is_relative_to(root / exact_target)
            )
            self.assertFalse(
                Path(plan["moves"][0]["target"]).is_relative_to(root / other_target)
            )

    def test_unregistered_press_directory_title_never_inherits_shorter_family_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Fate"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            source = root / "Fate_Apocrypha_BDRip"
            media = _write_file(source / "[Fate][01].mkv", b"must-not-collapse")
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Fate",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("BDRip", "VCB"),),
                        source_file=str(catalog_root / "catalog.yaml"),
                    ),
                ),
                catalog_root=catalog_root,
            )

            plan = build_plan(
                root,
                catalog=catalog,
                settings=_settings(catalog_root, base, root),
            )

            self.assertFalse(plan["ready"])
            self.assertEqual(plan["assignments"], [])
            self.assertEqual(plan["moves"], [])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertTrue(media.is_file())
            self.assertIn(
                "source-work-catalog-missing",
                {issue["code"] for issue in plan["issues"]},
            )
            binding = next(
                row
                for row in plan["source_work_bindings"]
                if row["source_name"] == source.name
            )
            self.assertEqual(binding["state"], "unresolved")
            self.assertEqual(binding["suggested_work_name"], "Fate_Apocrypha")
            self.assertTrue(binding["registration_required"])
            self.assertEqual(binding["next_action"], "search")
            self.assertEqual(binding["candidates"], [])

    def test_press_directory_exact_work_without_press_requests_catalog_press_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Pressless Work"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            source = root / "Pressless Work_BDRip(VCBM)"
            media = _write_file(source / "episode.mkv", b"awaiting-press-row")
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Pressless Work",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(),
                        source_file=str(catalog_root / "catalog.yaml"),
                        source_index=0,
                    ),
                ),
                catalog_root=catalog_root,
            )

            plan = build_plan(
                root,
                catalog=catalog,
                settings=_settings(catalog_root, base, root),
            )

            self.assertFalse(plan["ready"])
            self.assertEqual(plan["assignments"], [])
            self.assertEqual(plan["moves"], [])
            self.assertTrue(media.is_file())
            self.assertIn(
                "source-work-catalog-press-required",
                {issue["code"] for issue in plan["issues"]},
            )
            binding = next(
                row
                for row in plan["source_work_bindings"]
                if row["source_name"] == source.name
            )
            self.assertEqual(binding["suggested_work_name"], "Pressless Work")
            self.assertEqual(
                [row["work"]["name"] for row in binding["resolved_works"]],
                ["Pressless Work"],
            )
            self.assertTrue(binding["catalog_press_required"])
            self.assertTrue(binding["catalog_repair_required"])
            self.assertTrue(binding["registration_required"])
            self.assertEqual(binding["next_action"], "complete_manual")
            self.assertEqual(
                binding["inferred_press"],
                [
                    {
                        "press_format": "BDRip",
                        "press_group": "VCB",
                        "suggested_press_path": "",
                    }
                ],
            )

    def test_catalog_canonical_webrip_format_routes_nonexact_source_without_marker_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Catalog Web Family"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            source = root / "Catalog_Web_Show_WebRip(JSUM)"
            media = _write_file(source / "episode.mkv", b"web-video")
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Catalog_Web_Show",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("WebRip", "JSUM"),),
                        source_file=str(catalog_root / "catalog.yaml"),
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = _settings(catalog_root, base, root)
            self.assertNotIn("WebRip", settings.format_markers)

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["issues"], [])
            self.assertEqual(len(plan["assignments"]), 1)
            assignment = plan["assignments"][0]
            self.assertEqual(assignment["work_name"], "Catalog_Web_Show")
            self.assertEqual(assignment["press_format"], "WebRip")
            self.assertEqual(assignment["press_group"], "JSUM")
            self.assertEqual(Path(assignment["source_dir"]), source)
            self.assertEqual(
                Path(assignment["target_dir"]),
                root / "Catalog_Web_Show_WebRip",
            )
            self.assertFalse(assignment["in_place"])
            self.assertEqual(len(plan["moves"]), 1)
            self.assertEqual(Path(plan["moves"][0]["source"]), media)
            self.assertEqual(
                Path(plan["moves"][0]["target"]),
                root
                / "Catalog_Web_Show_WebRip"
                / "Catalog_Web_Show_WebRip_Disc"
                / media.name,
            )

    def test_settled_press_with_empty_legacy_dirs_does_not_block_pending_press(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Shared Work"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            settled = root / "Archive Set A"
            settled_media = _write_file(
                settled / "Archive Set A_Disc" / "01.mkv",
                b"settled",
            )
            for name in ("CDs", "Scans", "SPs"):
                (settled / name).mkdir()
            pending = root / "Mystery Bucket"
            pending_media = _write_file(
                pending / "CDs" / "Album" / "track.flac",
                b"pending",
            )
            settled_before = (
                settled_media.read_bytes(),
                settled_media.stat().st_mtime_ns,
            )
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Shared Work",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("BDRip", "JSUM", press_path=settled.name),
                            PressRecord("BDRip", "VCB", press_path=pending.name),
                        ),
                        source_file=str(catalog_root / "catalog.yaml"),
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = _settings(catalog_root, base, root)

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["issues"], [])
            self.assertEqual(len(plan["assignments"]), 1)
            assignment = plan["assignments"][0]
            self.assertEqual(Path(assignment["source_dir"]), pending)
            self.assertEqual(assignment["target_authority"], "database_press_path")
            self.assertTrue(assignment["in_place"])
            self.assertEqual(len(plan["moves"]), 1)
            self.assertEqual(Path(plan["moves"][0]["source"]), pending_media)
            self.assertNotIn(
                settled_media.resolve(),
                {Path(move["source"]).resolve() for move in plan["moves"]},
            )

            result = apply_plan(plan, confirmation=plan["plan_id"])

            self.assertEqual(result["moved_file_count"], 1)
            self.assertGreaterEqual(result["cleaned_directory_count"], 2)
            self.assertEqual(
                (settled_media.read_bytes(), settled_media.stat().st_mtime_ns),
                settled_before,
            )
            self.assertFalse((pending / "CDs").exists())
            self.assertTrue(
                any(
                    path.name == pending_media.name
                    for path in (pending / "Mystery Bucket_CD").rglob("*")
                    if path.is_file()
                )
            )

            checked = build_plan(root, catalog=catalog, settings=settings)
            self.assertEqual(checked["assignments"], [])
            self.assertEqual(checked["moves"], [])
            self.assertEqual(checked["issues"], [])

    def _fixture(
        self, temp: str
    ) -> tuple[
        Path,
        MediaCatalog,
        OrganizerSettings,
        dict[str, Path],
        dict[str, Path],
    ]:
        base = Path(temp)
        root = base / BASE_WORK
        root.mkdir()
        catalog_root = base / "db"
        catalog_root.mkdir()
        yaml_text = CATALOG_YAML.replace("WORK_ROOT", str(root).replace("'", "''"))
        (catalog_root / "[JP][TVInfo][test].yaml").write_text(yaml_text, encoding="utf-8")

        # Already-organized base-work targets must not be scanned as input.
        (root / BASE_JSUM_TARGET).mkdir()
        (root / BASE_MW_TARGET).mkdir()

        sources = {
            "jsum": root / "[2013-14][Little Busters! Refrain+EX][BDRip][JSUM]",
            "ex_vcb": root / "Little Busters! EX 2014 [BD 1920x1080] - VCB-Studio",
            "refrain_vcb": root
            / "Little Busters! Refrain 2013 [BD 1920x1080] - VCB-Studio",
        }

        files = {
            # One JSUM source intentionally mixes two works. The shared Logo is
            # the sole ambiguous file until an explicit per-file override is supplied.
            "jsum_ex": _write_file(
                sources["jsum"] / "EX" / "[Little Busters! EX][01].mkv", b"jsum-ex"
            ),
            "jsum_refrain": _write_file(
                sources["jsum"]
                / "Refrain"
                / "[Little Busters! Refrain][01].mkv",
                b"jsum-refrain",
            ),
            "jsum_refrain_ja": _write_file(
                sources["jsum"]
                / "Japanese"
                / "リトルバスターズ！～Refrain～ [02].mkv",
                b"jsum-refrain-ja",
            ),
            "shared_logo": _write_file(
                sources["jsum"]
                / "Shared"
                / "[Little Busters! Refrain+EX][Logo].mkv",
                b"shared-logo",
            ),
            # Both VCB sources contain a top-level main episode and nested trees.
            "ex_vcb_main": _write_file(
                sources["ex_vcb"] / "Little Busters! EX - 01.mkv", b"ex-main"
            ),
            "ex_vcb_audio": _write_file(
                sources["ex_vcb"] / "Little Busters! EX OST.flac", b"ex-audio"
            ),
            "ex_vcb_scan": _write_file(
                sources["ex_vcb"]
                / "bd-scans"
                / "Booklet"
                / "Little Busters! EX 001.png",
                b"ex-scan",
            ),
            "ex_vcb_fallback": _write_file(
                sources["ex_vcb"]
                / "Extras"
                / "Little Busters! EX font.ttf",
                b"ex-font",
            ),
            "refrain_vcb_main": _write_file(
                sources["refrain_vcb"] / "Little Busters! Refrain - 01.mkv",
                b"refrain-main",
            ),
            "refrain_vcb_special": _write_file(
                sources["refrain_vcb"] / "Little Busters! Refrain NCOP.mkv",
                b"refrain-ncop",
            ),
            "refrain_vcb_cd": _write_file(
                sources["refrain_vcb"]
                / "CDs"
                / "Disc 1"
                / "Little Busters! Refrain track.flac",
                b"refrain-cd",
            ),
            "refrain_vcb_sp": _write_file(
                sources["refrain_vcb"]
                / "SPs"
                / "Menu"
                / "Little Busters! Refrain Menu.mkv",
                b"refrain-sp",
            ),
        }

        settings = _settings(catalog_root, base, root)
        return root, MediaCatalog.load(catalog_root), settings, sources, files

    @staticmethod
    def _logo_override(files: dict[str, Path]) -> dict[str, str]:
        return {str(files["shared_logo"]): REFRAIN_WORK}

    def _resolved_plan(
        self,
        root: Path,
        catalog: MediaCatalog,
        settings: OrganizerSettings,
        files: dict[str, Path],
        **kwargs: object,
    ) -> dict[str, object]:
        return build_plan(
            root,
            catalog=catalog,
            settings=settings,
            file_work_overrides=self._logo_override(files),
            **kwargs,
        )

    def test_initial_preview_has_four_routes_and_only_shared_logo_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)

            plan = build_plan(root, catalog=catalog, settings=settings)
            repeated = build_plan(root, catalog=catalog, settings=settings)

            self.assertFalse(plan["ready"])
            self.assertEqual(plan["plan_id"], repeated["plan_id"])
            self.assertEqual(plan["version"], 4)
            self.assertEqual(plan["family_works"], [BASE_WORK, REFRAIN_WORK, EX_WORK])
            self.assertEqual(plan["summary"]["route_count"], 4)
            self.assertEqual(plan["summary"]["assignment_count"], 4)
            self.assertEqual(plan["summary"]["scanned_file_count"], len(files))
            self.assertEqual(plan["summary"]["classified_file_count"], len(files) - 1)
            self.assertEqual(plan["summary"]["unresolved_file_count"], 1)
            self.assertEqual({issue["code"] for issue in plan["issues"]}, {"work-ambiguous"})
            self.assertEqual(len(plan["unresolved_files"]), 1)
            self.assertEqual(plan["unresolved_files"][0]["source"], str(files["shared_logo"]))
            self.assertEqual(
                set(plan["unresolved_files"][0]["candidates"]),
                {REFRAIN_WORK, EX_WORK},
            )
            self.assertEqual(
                {(row["work_name"], row["press_group"]) for row in plan["assignments"]},
                set(EXPECTED_TARGETS),
            )

    def test_unique_disc_owner_absorbs_titles_found_only_in_ancillary_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Series A"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Series A",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("BDRip", "JSUM", "Series A_BDRip"),),
                        source_file="catalog.yaml",
                    ),
                    CatalogWork(
                        name="Series B",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("BDRip", "JSUM", "Series B_BDRip"),),
                        source_file="catalog.yaml",
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={"JSUM": ("jsum",)},
                group_suffixes={"JSUM": "JSUM"},
                max_files=100,
                default_work_root=root,
                work_aliases={},
            )
            source = root / "[Series A][BDRip][JSUM]"
            main = _write_file(source / "[Series A][01].mkv", b"main-a")
            wrong_title_cd = _write_file(
                source / "CDs" / "[Series B] OST.flac", b"series-b-title-only"
            )
            wrong_title_video = _write_file(
                source / "CDs" / "[Series B] bonus.mkv", b"cd-video-not-disc"
            )
            unknown_title_image = _write_file(
                source / "Scans" / "[Unknown Bonus Work] cover.jpg", b"unknown-title"
            )
            cd_only_source = root / "Series A Music [BDRip][JSUM]"
            cd_only_wrong_title = _write_file(
                cd_only_source / "CDs" / "[Series B] Character Song.flac",
                b"cd-only-series-b-title",
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["unresolved_files"], [])
            moves = {row["source"]: row for row in plan["moves"]}
            self.assertEqual(
                set(moves),
                {
                    str(main),
                    str(wrong_title_cd),
                    str(wrong_title_video),
                    str(unknown_title_image),
                    str(cd_only_wrong_title),
                },
            )
            self.assertEqual({row["work_name"] for row in moves.values()}, {"Series A"})
            for ancillary in (wrong_title_cd, wrong_title_video, unknown_title_image):
                move = moves[str(ancillary)]
                self.assertEqual(move["classification_stage"], "disc-source-owner")
                self.assertIn("Disc 正片资源", move["classification_reason"])
            self.assertEqual(
                moves[str(cd_only_wrong_title)]["classification_stage"],
                "source-current-owner",
            )
            self.assertIn(
                "没有其他作品的 Disc 正片资源",
                moves[str(cd_only_wrong_title)]["classification_reason"],
            )

    def test_multiple_disc_owners_keep_mixed_release_split_by_nearest_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Series A"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=tuple(
                    CatalogWork(
                        name=name,
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("BDRip", "JSUM", f"{name}_BDRip"),),
                        source_file="catalog.yaml",
                    )
                    for name in ("Series A", "Series B")
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={"JSUM": ("jsum",)},
                group_suffixes={"JSUM": "JSUM"},
                max_files=100,
                default_work_root=root,
                work_aliases={},
            )
            source = root / "[Series A+Series B][BDRip][JSUM]"
            main_a = _write_file(source / "A" / "[Series A][01].mkv", b"main-a")
            main_b = _write_file(source / "B" / "[Series B][01].mkv", b"main-b")
            a_scan_with_b_title = _write_file(
                source / "A" / "Scans" / "[Series B] cover.png", b"a-extra"
            )
            b_cd = _write_file(
                source / "B" / "CDs" / "[Series B] OST.flac", b"b-extra"
            )
            shared_b_cd = _write_file(
                source / "Shared" / "[Series B] bonus.flac", b"shared-b"
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            moves = {row["source"]: row for row in plan["moves"]}
            self.assertEqual(moves[str(main_a)]["work_name"], "Series A")
            self.assertEqual(moves[str(main_b)]["work_name"], "Series B")
            self.assertEqual(moves[str(a_scan_with_b_title)]["work_name"], "Series A")
            self.assertEqual(
                moves[str(a_scan_with_b_title)]["classification_stage"],
                "disc-subtree-owner",
            )
            self.assertEqual(moves[str(b_cd)]["work_name"], "Series B")
            # The Shared subtree has no local Disc anchor and the source root
            # has both works, so its explicit title match remains authoritative.
            self.assertEqual(moves[str(shared_b_cd)]["work_name"], "Series B")
            self.assertEqual(moves[str(shared_b_cd)]["classification_stage"], "filename")

    def test_unknown_bracket_title_blocks_singleton_but_inherits_unique_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "血界戦線"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="血界戦線",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("BDRip", "JSUM", "血界戦線_BDRip(JSUM)"),
                            PressRecord("BDRip", "VCB", "血界戦線_BDRip(VCB)"),
                        ),
                        source_file="catalog.yaml",
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={
                    "JSUM": ("jsum",),
                    "VCB": ("vcb-studio", "vcb"),
                },
                group_suffixes={"JSUM": "JSUM", "VCB": "VCB"},
                max_files=100,
                default_work_root=root,
                work_aliases={"血界戦線": ("Kekkai Sensen",)},
            )
            unknown_source = (
                root / "[2017][Kekkai Sensen & Beyond][BDRIP][1080P][1-12Fin+SP][JSUM]"
            )
            unknown_file = _write_file(
                unknown_source
                / "[Kekkai Sensen & Beyond][01][BDRIP][1080P][H264_FLACx2].mkv",
                b"beyond",
            )
            technical_source = (
                root
                / "[Nekomoe kissaten&VCB-Studio][2015][BDRIP][1080P][1-12Fin+SP]"
            )
            technical_file = _write_file(
                technical_source
                / (
                    "[01][TFCC-89539][FLAC+jpg][BDRIP][1080P]"
                    "[BD 1920x1080 AVC FLAC][H264_FLACx2].mkv"
                ),
                b"base",
            )
            unique_source = root / "[血界戦線][BDRIP][JSUM]"
            unique_source_file = _write_file(
                unique_source
                / "[Yamada-kun to 7-nin no Majo][01][BDRIP][1080P].mkv",
                b"unique-source",
            )
            base_episode_file = _write_file(
                unique_source / "Kekkai Sensen - 02.mkv",
                b"base-episode",
            )
            base_extras_file = _write_file(
                unique_source / "Kekkai Sensen Extras.mkv",
                b"base-extras",
            )
            related_source = root / "[血界戦線][BDRIP][VCB] related variant"
            related_source_file = _write_file(
                related_source
                / "[Kekkai Sensen & Beyond][01][BDRIP][1080P].mkv",
                b"related-variant",
            )
            outside_source = root / "[JSUM] Kekkai Sensen & Beyond [BDRip]"
            outside_source_file = _write_file(
                outside_source / "Kekkai Sensen & Beyond - 01.mkv",
                b"outside-related-variant",
            )
            numbered_source = root / "[JSUM] Kekkai Sensen 2 [BDRip]"
            numbered_source_file = _write_file(
                numbered_source / "Kekkai Sensen 2 - 01.mkv",
                b"numbered-related-variant",
            )
            extra_story_source = root / "[JSUM] Kekkai Sensen Extra Story [BDRip]"
            extra_story_file = _write_file(
                extra_story_source / "Kekkai Sensen Extra Story - 01.mkv",
                b"extra-story",
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            unresolved = [
                row for row in plan["unresolved_files"] if row["source"] == str(unknown_file)
            ]
            self.assertEqual(len(unresolved), 1)
            self.assertIn("Kekkai Sensen & Beyond", unresolved[0]["reason"])
            self.assertEqual(
                unresolved[0]["unmatched_work_name_hints"],
                ["Kekkai Sensen & Beyond"],
            )
            issue = next(
                row
                for row in plan["issues"]
                if row["code"] == "work-unresolved" and row["path"] == str(unknown_file)
            )
            self.assertIn("Kekkai Sensen & Beyond", issue["message"])
            self.assertEqual(issue["work_name_hints"], ["Kekkai Sensen & Beyond"])
            self.assertEqual(
                issue["unmatched_work_name_hints"], ["Kekkai Sensen & Beyond"]
            )
            self.assertFalse(any(row["source"] == str(unknown_file) for row in plan["moves"]))

            technical_move = next(
                row for row in plan["moves"] if row["source"] == str(technical_file)
            )
            self.assertEqual(technical_move["work_name"], "血界戦線")
            self.assertFalse(
                any(
                    hint
                    in {
                        "2015",
                        "BDRIP",
                        "1080P",
                        "1-12Fin+SP",
                        "TFCC-89539",
                        "FLAC+jpg",
                        "BD 1920x1080 AVC FLAC",
                        "H264_FLACx2",
                    }
                    for row in plan["unresolved_files"]
                    for hint in row["unmatched_work_name_hints"]
                )
            )
            unique_source_move = next(
                row for row in plan["moves"] if row["source"] == str(unique_source_file)
            )
            self.assertEqual(unique_source_move["work_name"], "血界戦線")
            self.assertEqual(unique_source_move["classification_stage"], "source-default")
            self.assertIn(
                "来源目录只包含唯一作品",
                unique_source_move["classification_reason"],
            )
            self.assertIn(
                "Yamada-kun to 7-nin no Majo",
                unique_source_move["classification_reason"],
            )
            base_episode_move = next(
                row for row in plan["moves"] if row["source"] == str(base_episode_file)
            )
            self.assertEqual(base_episode_move["work_name"], "血界戦線")
            self.assertEqual(base_episode_move["classification_stage"], "filename")
            base_extras_move = next(
                row for row in plan["moves"] if row["source"] == str(base_extras_file)
            )
            self.assertEqual(base_extras_move["work_name"], "血界戦線")
            related_unresolved = [
                row
                for row in plan["unresolved_files"]
                if row["source"] == str(related_source_file)
            ]
            self.assertEqual(len(related_unresolved), 1)
            self.assertIn("独立续作或特别篇", related_unresolved[0]["reason"])
            self.assertFalse(
                any(row["source"] == str(related_source_file) for row in plan["moves"])
            )
            outside_unresolved = [
                row
                for row in plan["unresolved_files"]
                if row["source"] == str(outside_source_file)
            ]
            self.assertEqual(len(outside_unresolved), 1)
            self.assertIn("Kekkai Sensen & Beyond", outside_unresolved[0]["reason"])
            self.assertIn(
                "Kekkai Sensen & Beyond",
                outside_unresolved[0]["unmatched_work_name_hints"],
            )
            self.assertFalse(
                any(row["source"] == str(outside_source_file) for row in plan["moves"])
            )
            numbered_unresolved = [
                row
                for row in plan["unresolved_files"]
                if row["source"] == str(numbered_source_file)
            ]
            self.assertEqual(len(numbered_unresolved), 1)
            self.assertIn("Kekkai Sensen 2", numbered_unresolved[0]["reason"])
            self.assertFalse(
                any(row["source"] == str(numbered_source_file) for row in plan["moves"])
            )
            extra_story_unresolved = [
                row
                for row in plan["unresolved_files"]
                if row["source"] == str(extra_story_file)
            ]
            self.assertEqual(len(extra_story_unresolved), 1)
            self.assertIn("Kekkai Sensen Extra Story", extra_story_unresolved[0]["reason"])
            self.assertFalse(
                any(row["source"] == str(extra_story_file) for row in plan["moves"])
            )
            self.assertTrue(unknown_file.is_file())
            self.assertTrue(technical_file.is_file())
            self.assertTrue(unique_source_file.is_file())
            self.assertTrue(base_episode_file.is_file())
            self.assertTrue(base_extras_file.is_file())
            self.assertTrue(related_source_file.is_file())
            self.assertTrue(outside_source_file.is_file())
            self.assertTrue(numbered_source_file.is_file())
            self.assertTrue(extra_story_file.is_file())
            self.assertFalse((root / "血界戦線_BDRip(JSUM)").exists())
            self.assertFalse((root / "血界戦線_BDRip(VCB)").exists())

    def test_unregistered_romanized_title_inherits_single_catalog_work_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "山田くんと7人の魔女"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="山田くんと7人の魔女",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("BDRip", "JSUM", "山田くんと7人の魔女_BDRip(JSUM)"),
                            PressRecord("BDRip", "VCB", "山田くんと7人の魔女_BDRip(VCB)"),
                        ),
                        source_file="catalog.yaml",
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={"JSUM": ("jsum",), "VCB": ("vcb-studio", "vcb")},
                group_suffixes={"JSUM": "JSUM", "VCB": "VCB"},
                max_files=20,
                default_work_root=root,
                work_aliases={},
            )
            jsum_file = _write_file(
                root
                / "[2015][Yamada-kun to 7-nin no Majo][BDRIP][JSUM]"
                / "[Yamada-kun to 7-nin no Majo][01][BDRIP][1080P].mkv",
                b"jsum",
            )
            jsum_music = _write_file(
                root
                / "[2015][Yamada-kun to 7-nin no Majo][BDRIP][JSUM]"
                / (
                    "[EAC] [150520] 山田くんと7人の魔女 "
                    "OP「くちづけDiamond」[DVD付限定盤]／WEAVER.rar"
                ),
                b"jsum-op",
            )
            vcb_file = _write_file(
                root
                / "[VCB-Studio] Yamada-kun to 7-nin no Majo [BDRIP]"
                / "[Yamada-kun to 7-nin no Majo][02][BDRIP][1080P].mkv",
                b"vcb",
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertEqual(
                {row["source"] for row in plan["moves"]},
                {str(jsum_file), str(jsum_music), str(vcb_file)},
            )
            self.assertEqual(
                {row["work_name"] for row in plan["moves"]},
                {"山田くんと7人の魔女"},
            )
            moves = {row["source"]: row for row in plan["moves"]}
            self.assertTrue(
                all(
                    "Yamada-kun to 7-nin no Majo"
                    in moves[str(source_file)]["classification_reason"]
                    for source_file in (jsum_file, vcb_file)
                )
            )
            self.assertIn(
                "catalog-singleton-unregistered-alias",
                {row["classification_stage"] for row in plan["moves"]},
            )

            mixed_source = root / "[BDRIP][JSUM] genuinely mixed"
            first_unknown = _write_file(
                mixed_source / "[Completely Different Work A][01][BDRIP].mkv",
                b"mixed-a",
            )
            second_unknown = _write_file(
                mixed_source / "[Completely Different Work B][01][BDRIP].mkv",
                b"mixed-b",
            )
            ambiguous = build_plan(root, catalog=catalog, settings=settings)
            mixed_unresolved = [
                row
                for row in ambiguous["unresolved_files"]
                if row["source_dir"] == str(mixed_source)
            ]
            self.assertEqual(
                {row["source"] for row in mixed_unresolved},
                {str(first_unknown), str(second_unknown)},
            )
            self.assertTrue(
                all("确认该目录是单作品还是多作品" in row["reason"] for row in mixed_unresolved)
            )

    def test_source_work_override_applies_to_every_file_and_conflicts_with_file_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Combined Library"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=tuple(
                    CatalogWork(
                        name=name,
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("BDRip", "JSUM", f"{name}_BDRip(JSUM)"),),
                        source_file="catalog.yaml",
                    )
                    for name in ("Alpha Work", "Beta Work")
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={"JSUM": ("jsum",)},
                group_suffixes={"JSUM": "JSUM"},
                max_files=20,
                default_work_root=root,
                work_aliases={},
            )
            source = root / "[BDRip][JSUM] combined"
            known_file = _write_file(source / "[Alpha Work][01][BDRip].mkv", b"alpha")
            unknown_file = _write_file(
                source / "[Mystery Romanized Name][02][BDRip].mkv",
                b"unknown",
            )

            unresolved = build_plan(root, catalog=catalog, settings=settings)
            self.assertEqual(
                {row["source"] for row in unresolved["unresolved_files"]},
                {str(unknown_file)},
            )

            resolved = build_plan(
                root,
                catalog=catalog,
                settings=settings,
                source_work_overrides={str(source): "Beta Work"},
            )
            self.assertTrue(resolved["ready"], resolved["issues"])
            self.assertEqual(resolved["unresolved_files"], [])
            self.assertEqual(
                {row["source"] for row in resolved["moves"]},
                {str(known_file), str(unknown_file)},
            )
            self.assertEqual(
                {row["work_name"] for row in resolved["moves"]},
                {"Beta Work"},
            )
            self.assertEqual(
                {row["classification_stage"] for row in resolved["moves"]},
                {"manual-source"},
            )

            conflict = build_plan(
                root,
                catalog=catalog,
                settings=settings,
                source_work_overrides={str(source): "Beta Work"},
                file_work_overrides={str(known_file): "Alpha Work"},
            )
            self.assertFalse(conflict["ready"])
            self.assertIn(
                "source-file-work-override-conflict",
                {issue["code"] for issue in conflict["issues"]},
            )

    def test_exact_catalog_code_alias_outranks_technical_code_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Catalogued Work"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Catalogued Work",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("BDRip", "JSUM", "Catalogued Work_BDRip"),
                        ),
                        source_file="catalog.yaml",
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={"JSUM": ("jsum",)},
                group_suffixes={"JSUM": "JSUM"},
                max_files=10,
                default_work_root=root,
                work_aliases={"Catalogued Work": ("TFCC-89539",)},
            )
            source = root / "[TFCC-89539][BDRIP][JSUM]"
            source_file = _write_file(
                source / "[TFCC-89539][01][FLAC+jpg].mkv",
                b"registered-catalog-code-title",
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertEqual(plan["moves"][0]["source"], str(source_file))
            self.assertEqual(plan["moves"][0]["work_name"], "Catalogued Work")
            self.assertIn("方括号完整匹配", plan["moves"][0]["classification_reason"])
            self.assertTrue(source_file.is_file())
            self.assertFalse((root / "Catalogued Work_BDRip").exists())

    def test_exact_bracket_alias_prefers_beyond_over_base_alias_substring(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "血界戦線"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="血界戦線",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(PressRecord("BDRip", "JSUM", "血界戦線_BDRip"),),
                        source_file="catalog.yaml",
                    ),
                    CatalogWork(
                        name="血界戦線 & Beyond",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord(
                                "BDRip",
                                "JSUM",
                                "血界戦線 & Beyond_BDRip",
                            ),
                        ),
                        source_file="catalog.yaml",
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = OrganizerSettings(
                catalog_root=catalog_root,
                allowed_resource_roots=(base,),
                format_markers={"BDRip": ("bdrip",)},
                group_markers={"JSUM": ("jsum",)},
                group_suffixes={"JSUM": "JSUM"},
                max_files=100,
                default_work_root=root,
                work_aliases={
                    "血界戦線": ("Kekkai Sensen",),
                    "血界戦線 & Beyond": ("Kekkai Sensen & Beyond",),
                },
            )
            source = (
                root / "[2017][Kekkai Sensen & Beyond][BDRIP][1080P][1-12Fin+SP][JSUM]"
            )
            source_file = _write_file(
                source / "[Kekkai Sensen & Beyond][01][H264_FLACx2].mkv",
                b"beyond",
            )
            unknown_group_source = (
                root / "[SomeRelease][JSUM] Kekkai Sensen & Beyond [BDRIP][1080P]"
            )
            unknown_group_file = _write_file(
                unknown_group_source / "episode 02.mkv",
                b"unknown-group-known-title",
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertEqual(len(plan["assignments"]), 2)
            self.assertEqual(
                {row["work_name"] for row in plan["assignments"]},
                {"血界戦線 & Beyond"},
            )
            self.assertEqual(
                {row["target_relpath"] for row in plan["assignments"]},
                {"血界戦線 & Beyond_BDRip"},
            )
            moves = {row["source"]: row for row in plan["moves"]}
            self.assertEqual(set(moves), {str(source_file), str(unknown_group_file)})
            self.assertEqual(moves[str(source_file)]["classification_stage"], "filename")
            self.assertIn(
                "方括号完整匹配", moves[str(source_file)]["classification_reason"]
            )
            self.assertEqual(
                moves[str(unknown_group_file)]["classification_stage"],
                "source-default",
            )
            self.assertTrue(source_file.is_file())
            self.assertTrue(unknown_group_file.is_file())
            self.assertFalse((root / "血界戦線 & Beyond_BDRip").exists())

    def test_file_override_resolves_every_file_with_exact_targets_and_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)

            plan = self._resolved_plan(root, catalog, settings, files)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["issues"], [])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertEqual(plan["summary"]["route_count"], 4)
            self.assertEqual(plan["summary"]["file_count"], len(files))
            self.assertEqual(plan["summary"]["fallback_file_count"], 0)
            self.assertEqual(plan["summary"]["others_file_count"], 1)
            self.assertEqual(plan["summary"]["database_target_count"], 4)

            assignments = {
                (row["work_name"], row["press_group"]): row
                for row in plan["assignments"]
            }
            self.assertEqual(set(assignments), set(EXPECTED_TARGETS))
            for key, expected_target in EXPECTED_TARGETS.items():
                self.assertEqual(assignments[key]["target_relpath"], expected_target)
                self.assertEqual(assignments[key]["target_authority"], "database_press_path")

            moves = plan["moves"]
            move_sources = [row["source"] for row in moves]
            move_targets = [row["target"] for row in moves]
            self.assertEqual(set(move_sources), {str(path) for path in files.values()})
            self.assertEqual(len(move_sources), len(set(move_sources)))
            self.assertEqual(len(move_targets), len(set(move_targets)))
            self.assertTrue(
                all(Path(row["source"]).name == Path(row["target"]).name for row in moves)
            )

            jsum_routes = {
                row["route_id"] for row in plan["assignments"] if row["press_group"] == "JSUM"
            }
            jsum_moves = [row for row in moves if row["route_id"] in jsum_routes]
            self.assertEqual(len(jsum_moves), 4)
            for move in jsum_moves:
                self.assertEqual(move["classifier_id"], "jsum-layout")
                self.assertNotIn("(Jsum)", Path(move["target_relative_path"]).parts[0])
            self.assertEqual(
                {move["layout_category"] for move in jsum_moves},
                {"Disc", "Others"},
            )

            moves_by_source = {row["source"]: row for row in moves}
            expected_vcb_layouts = {
                "ex_vcb_main": Path("Little Busters! EX_BDRip_Disc") / files["ex_vcb_main"].name,
                "ex_vcb_audio": Path("Little Busters! EX_BDRip_CD") / files["ex_vcb_audio"].name,
                "ex_vcb_scan": Path("Little Busters! EX_BDRip_Image") / "Booklet" / files["ex_vcb_scan"].name,
                "ex_vcb_fallback": Path("Little Busters! EX_BDRip_Fonts") / files["ex_vcb_fallback"].name,
                "refrain_vcb_main": Path("Little Busters! Refrain_BDRip_Disc") / files["refrain_vcb_main"].name,
                "refrain_vcb_special": Path("Little Busters! Refrain_BDRip_OP+ED") / files["refrain_vcb_special"].name,
                "refrain_vcb_cd": Path("Little Busters! Refrain_BDRip_CD") / "Disc 1" / files["refrain_vcb_cd"].name,
                "refrain_vcb_sp": Path("Little Busters! Refrain_BDRip_Menu") / files["refrain_vcb_sp"].name,
            }
            for file_key, expected_relative in expected_vcb_layouts.items():
                self.assertEqual(
                    Path(moves_by_source[str(files[file_key])]["target_relative_path"]),
                    expected_relative,
                )

            fonts_move = moves_by_source[str(files["ex_vcb_fallback"])]
            self.assertEqual(fonts_move["classifier_id"], "vcb-layout")
            self.assertEqual(fonts_move["layout_stage"], "press-group")
            self.assertEqual(fonts_move["layout_category"], "Fonts")
            logo_move = moves_by_source[str(files["shared_logo"])]
            self.assertEqual(logo_move["work_name"], REFRAIN_WORK)
            self.assertEqual(logo_move["classification_stage"], "manual")

    def test_build_plan_groups_single_disc_video_with_matching_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            video = files["ex_vcb_main"]
            simplified = _write_file(
                video.with_name(f"{video.stem}.sc.ass"),
                b"simplified-subtitle",
            )
            traditional = _write_file(
                video.with_name(f"{video.stem}.tc.ass"),
                b"traditional-subtitle",
            )

            plan = self._resolved_plan(root, catalog, settings, files)

            self.assertTrue(plan["ready"], plan["issues"])
            moves = {row["source"]: row for row in plan["moves"]}
            episode_dir = (
                Path("Little Busters! EX_BDRip_Disc")
                / "Little Busters! EX_BDRip_01"
            )
            for source_file in (video, simplified, traditional):
                move = moves[str(source_file)]
                self.assertEqual(
                    Path(move["target_relative_path"]),
                    episode_dir / source_file.name,
                )
                self.assertEqual(
                    move["disc_version_directory"],
                    "Little Busters! EX_BDRip_01",
                )

    def test_resolution_only_disc_and_subtitles_flatten_to_press_root_and_settle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Overlord"
            target = root / "Overlord IV_1080p"
            episode = target / "Overlord IV_1080p_01"
            catalog_root = base / "db"
            root.mkdir()
            catalog_root.mkdir()
            names = (
                "[Sakurato] Overlord IV [01][HEVC-10bit 1080P@60FPS AAC][CHS&CHT].mkv",
                "[Sakurato] Overlord IV [01][HEVC-10bit 1080P AAC][CHS&CHT]_Subtitles03.ass",
                "[Sakurato] Overlord IV [01][HEVC-10bit 1080P AAC][CHS&CHT]_Subtitles04.ass",
            )
            source_files = tuple(
                _write_file(episode / name, name.encode("utf-8"))
                for name in names
            )
            catalog = MediaCatalog(
                works=(
                    CatalogWork(
                        name="Overlord IV",
                        path=str(root),
                        domain="animation",
                        country="japan",
                        release_type="tv",
                        presses=(
                            PressRecord("1080p", "ST", press_path=target.name),
                        ),
                        source_file=str(catalog_root / "[JP][TVInfo][2022].yaml"),
                    ),
                ),
                catalog_root=catalog_root,
            )
            settings = replace(
                _settings(catalog_root, base, root),
                format_markers={"1080p": ("1080p",)},
                group_markers={"ST": ("sakurato",)},
                group_suffixes={"ST": "ST"},
            )

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(len(plan["moves"]), 3)
            self.assertEqual(plan["summary"]["category_directory_count"], 1)
            self.assertEqual(plan["assignments"][0]["category_directories"], [str(target)])
            for source_file in source_files:
                move = next(row for row in plan["moves"] if row["source"] == str(source_file))
                self.assertEqual(Path(move["target"]), target / source_file.name)
                self.assertEqual(Path(move["target_relative_path"]), Path(source_file.name))
                self.assertEqual(move["layout_category"], "Disc")
                self.assertEqual(move["disc_version_directory"], "")
                self.assertEqual(Path(move["category_dir"]), target)

            applied = apply_plan(plan, confirmation=plan["plan_id"])
            self.assertEqual(applied["moved_file_count"], 3)
            self.assertFalse(episode.exists())
            self.assertTrue(all((target / name).is_file() for name in names))

            settled = build_plan(root, catalog=catalog, settings=settings)
            self.assertFalse(settled["ready"])
            self.assertEqual(settled["issues"], [])
            self.assertEqual(settled["assignments"], [])
            self.assertEqual(settled["moves"], [])

    def test_build_plan_groups_only_multi_version_disc_episodes_and_flattens_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, sources, files = self._fixture(temp)
            source = sources["ex_vcb"]
            commentary = _write_file(
                source / "Versions" / "Little Busters! EX - 01 commentary.mkv",
                b"commentary-video",
            )
            commentary_sub = _write_file(
                source / "Versions" / "Little Busters! EX - 01 commentary.zh-Hans.ass",
                b"commentary-subtitle",
            )
            commentary_audio = _write_file(
                source / "Versions" / "Little Busters! EX - 01 commentary.mka",
                b"commentary-audio",
            )
            commentary_checksum = _write_file(
                source / "Versions" / "Little Busters! EX - 01 commentary.mkv.sha256",
                b"checksum",
            )
            single = _write_file(source / "Versions" / "Little Busters! EX - 02.mkv", b"single")
            existing_episode_dir = source / "Little Busters! EX_BDRip_03"
            existing_v1 = _write_file(existing_episode_dir / "[03].mkv", b"03-v1")
            existing_v2 = _write_file(existing_episode_dir / "[03v2].mkv", b"03-v2")

            plan = self._resolved_plan(root, catalog, settings, files)

            self.assertTrue(plan["ready"], plan["issues"])
            moves = {row["source"]: row for row in plan["moves"]}
            disc_dir = Path("Little Busters! EX_BDRip_Disc")
            episode_01 = disc_dir / "Little Busters! EX_BDRip_01"
            for source_file in (
                files["ex_vcb_main"],
                commentary,
                commentary_sub,
                commentary_audio,
                commentary_checksum,
            ):
                move = moves[str(source_file)]
                self.assertEqual(
                    Path(move["target_relative_path"]),
                    episode_01 / source_file.name,
                )
                self.assertEqual(
                    move["disc_version_directory"],
                    "Little Busters! EX_BDRip_01",
                )
                self.assertNotIn("Versions", Path(move["target_relative_path"]).parts)

            self.assertEqual(
                Path(moves[str(single)]["target_relative_path"]),
                disc_dir / "Versions" / single.name,
            )
            self.assertEqual(moves[str(single)]["disc_version_directory"], "")
            for source_file in (existing_v1, existing_v2):
                target_parts = Path(moves[str(source_file)]["target_relative_path"]).parts
                self.assertEqual(target_parts.count("Little Busters! EX_BDRip_03"), 1)
                self.assertEqual(
                    Path(*target_parts),
                    disc_dir / "Little Busters! EX_BDRip_03" / source_file.name,
                )

    def test_generic_fallback_classifies_unknown_group_into_others(self) -> None:
        relative = Path("Unknown Tree") / "bundle.bin"

        decision = DEFAULT_CLASSIFIER_REGISTRY.classify("UNKNOWN-GROUP", relative)

        self.assertEqual(decision.relative_path, relative)
        self.assertEqual(decision.classifier_id, "fallback-layout")
        self.assertEqual(decision.rule_id, "generic-others-fallback")
        self.assertEqual(decision.stage, "fallback")
        self.assertEqual(decision.category, "Others")

    def test_route_override_changes_only_the_selected_route_and_plan_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            original = self._resolved_plan(root, catalog, settings, files)
            selected = next(
                row
                for row in original["assignments"]
                if row["work_name"] == EX_WORK and row["press_group"] == "JSUM"
            )

            revised = self._resolved_plan(
                root,
                catalog,
                settings,
                files,
                route_target_overrides={selected["route_id"]: "Manual EX JSUM"},
            )

            self.assertTrue(revised["ready"], revised["issues"])
            self.assertNotEqual(original["plan_id"], revised["plan_id"])
            original_by_route = {row["route_id"]: row for row in original["assignments"]}
            revised_by_route = {row["route_id"]: row for row in revised["assignments"]}
            self.assertEqual(set(original_by_route), set(revised_by_route))
            for route_id, original_assignment in original_by_route.items():
                revised_assignment = revised_by_route[route_id]
                if route_id == selected["route_id"]:
                    self.assertEqual(revised_assignment["target_relpath"], "Manual EX JSUM")
                    self.assertEqual(revised_assignment["target_authority"], "user_override")
                else:
                    self.assertEqual(
                        revised_assignment["target_relpath"],
                        original_assignment["target_relpath"],
                    )
                    self.assertEqual(
                        revised_assignment["target_authority"],
                        original_assignment["target_authority"],
                    )
            self.assertEqual(revised["summary"]["manual_target_count"], 1)

    def test_legacy_source_override_is_blocked_when_source_has_multiple_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, sources, files = self._fixture(temp)

            plan = self._resolved_plan(
                root,
                catalog,
                settings,
                files,
                target_overrides={str(sources["jsum"]): "Unsafe Legacy Target"},
            )

            self.assertFalse(plan["ready"])
            self.assertEqual(
                {issue["code"] for issue in plan["issues"]},
                {"legacy-override-ambiguous"},
            )
            self.assertFalse(
                any(row["source_dir"] == str(sources["jsum"]) for row in plan["assignments"])
            )
            self.assertFalse(
                any(Path(row["source"]).is_relative_to(sources["jsum"]) for row in plan["moves"])
            )

    def test_existing_destination_blocks_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            collision = (
                root
                / REFRAIN_VCB_TARGET
                / "Little Busters! Refrain_BDRip_OP+ED"
                / files["refrain_vcb_special"].name
            )
            _write_file(collision, b"existing")

            plan = self._resolved_plan(root, catalog, settings, files)

            self.assertFalse(plan["ready"])
            self.assertIn("destination-exists", {issue["code"] for issue in plan["issues"]})

    def test_apply_rejects_tampered_plan_and_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            plan = self._resolved_plan(root, catalog, settings, files)

            with self.assertRaisesRegex(ValueError, "确认码不匹配"):
                apply_plan(plan, confirmation="wrong")

            original_source = Path(plan["moves"][0]["source"])
            plan["moves"][0]["target"] = str(root / "tampered" / original_source.name)
            with self.assertRaisesRegex(ValueError, "计划内容与计划 ID 不一致"):
                apply_plan(plan, confirmation=plan["plan_id"])
            self.assertTrue(original_source.is_file())

    def test_apply_moves_every_file_without_renaming_and_cleans_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, sources, files = self._fixture(temp)
            plan = self._resolved_plan(root, catalog, settings, files)

            result = apply_plan(plan, confirmation=plan["plan_id"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["moved_file_count"], len(files))
            self.assertTrue(
                (
                    root
                    / REFRAIN_JSUM_TARGET
                    / "Little Busters! Refrain_BDRip_Others"
                    / "Shared"
                    / files["shared_logo"].name
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / EX_VCB_TARGET
                    / "Little Busters! EX_BDRip_CD"
                    / files["ex_vcb_audio"].name
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / EX_VCB_TARGET
                    / "Little Busters! EX_BDRip_Image"
                    / "Booklet"
                    / files["ex_vcb_scan"].name
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / REFRAIN_VCB_TARGET
                    / "Little Busters! Refrain_BDRip_OP+ED"
                    / files["refrain_vcb_special"].name
                ).is_file()
            )
            self.assertTrue(all(not source.exists() for source in sources.values()))

    def test_web_config_preview_and_explicit_empty_selection_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, _sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                config = organizer_config_payload()
                preview = preview_organizer_from_ui_body(request)
                with self.assertRaisesRegex(ValueError, "source_names"):
                    preview_organizer_from_ui_body({"root": str(root), "source_names": []})

            self.assertTrue(config["ok"])
            self.assertEqual(config["version"], 4)
            self.assertTrue(config["capabilities"]["per_file_routing"])
            self.assertTrue(config["capabilities"]["route_target_overrides"])
            self.assertTrue(config["capabilities"]["file_work_overrides"])
            self.assertTrue(config["capabilities"]["source_work_overrides"])
            self.assertTrue(config["capabilities"]["source_press_overrides"])
            self.assertEqual(config["paths"]["catalog_root"], str(settings.catalog_root))
            self.assertEqual(config["paths"]["default_work_root"], str(root))
            self.assertIn(REFRAIN_WORK, config["classification"]["work_aliases"])
            self.assertTrue(preview["ok"])
            self.assertTrue(preview["plan"]["ready"], preview["plan"]["issues"])

    def test_group_unresolved_exposes_candidates_and_manual_preview_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, _files = self._fixture(temp)
            source = root / "Little Busters! EX [BDRip] unidentified"
            source_file = _write_file(source / "Little Busters! EX - 02.mkv", b"manual-group")

            unresolved = build_plan(
                root,
                catalog=catalog,
                settings=settings,
                source_names=[source.name],
            )

            self.assertFalse(unresolved["ready"])
            group_issues = [
                issue for issue in unresolved["issues"] if issue["code"] == "group-unresolved"
            ]
            self.assertEqual(len(group_issues), 1)
            self.assertEqual(group_issues[0]["path"], str(source))
            self.assertEqual(group_issues[0]["source_key"], str(source))
            self.assertEqual(group_issues[0]["press_format"], "BDRip")
            self.assertEqual(group_issues[0]["press_groups"], ["JSUM", "VCB"])

            invalid_choice = build_plan(
                root,
                catalog=catalog,
                settings=settings,
                source_names=[source.name],
                source_press_overrides={
                    str(source): {
                        "press_format": "BDRip",
                        "press_group": "NOT-IN-CATALOG",
                    }
                },
            )
            self.assertFalse(invalid_choice["ready"])
            self.assertEqual(invalid_choice["moves"], [])
            self.assertIn(
                "group-unresolved", {issue["code"] for issue in invalid_choice["issues"]}
            )

            request = {
                "root": str(root),
                "source_names": [source.name],
                "source_press_overrides": {
                    str(source): {"press_group": "JSUM"}
                },
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                preview = preview_organizer_from_ui_body(request)["plan"]

            self.assertTrue(preview["ready"], preview["issues"])
            self.assertEqual(preview["assignments"][0]["press_group"], "JSUM")
            self.assertEqual(preview["assignments"][0]["target_relpath"], EX_JSUM_TARGET)
            self.assertTrue(source_file.is_file())
            self.assertFalse((root / EX_JSUM_TARGET).exists())

    def test_format_ambiguous_exposes_candidates_and_supports_incremental_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, _files = self._fixture(temp)
            source = root / "Little Busters! EX unidentified"
            source_file = _write_file(source / "Little Busters! EX - 03.mkv", b"manual-format")

            unresolved = build_plan(
                root,
                catalog=catalog,
                settings=settings,
                source_names=[source.name],
            )

            self.assertFalse(unresolved["ready"])
            format_issues = [
                issue for issue in unresolved["issues"] if issue["code"] == "format-ambiguous"
            ]
            self.assertEqual(len(format_issues), 1)
            self.assertEqual(format_issues[0]["path"], str(source))
            self.assertEqual(format_issues[0]["source_key"], str(source))
            self.assertEqual(format_issues[0]["press_formats"], ["BDRip"])
            self.assertEqual(format_issues[0]["detected_press_formats"], [])

            format_only_request = {
                "root": str(root),
                "source_names": [source.name],
                "source_press_overrides": {
                    str(source): {"press_format": "BDRip"}
                },
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                format_only = preview_organizer_from_ui_body(format_only_request)["plan"]
                fully_resolved = preview_organizer_from_ui_body(
                    {
                        **format_only_request,
                        "source_press_overrides": {
                            str(source): {
                                "press_format": "BDRip",
                                "press_group": "VCB",
                            }
                        },
                    }
                )["plan"]

            self.assertFalse(format_only["ready"])
            group_issues = [
                issue for issue in format_only["issues"] if issue["code"] == "group-unresolved"
            ]
            self.assertEqual(len(group_issues), 1)
            self.assertEqual(group_issues[0]["press_format"], "BDRip")
            self.assertEqual(group_issues[0]["press_groups"], ["JSUM", "VCB"])
            self.assertTrue(fully_resolved["ready"], fully_resolved["issues"])
            self.assertEqual(fully_resolved["assignments"][0]["press_format"], "BDRip")
            self.assertEqual(fully_resolved["assignments"][0]["press_group"], "VCB")
            self.assertTrue(source_file.is_file())
            self.assertFalse((root / EX_VCB_TARGET).exists())

    def test_web_rejects_invalid_source_press_override_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, _sources, _files = self._fixture(temp)
            source = root / "Little Busters! EX [BDRip] unidentified"
            source_file = _write_file(source / "Little Busters! EX - 02.mkv", b"manual-group")

            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                with self.assertRaisesRegex(ValueError, "source_press_overrides"):
                    preview_organizer_from_ui_body(
                        {"root": str(root), "source_press_overrides": []}
                    )
                with self.assertRaisesRegex(ValueError, "press_group"):
                    preview_organizer_from_ui_body(
                        {
                            "root": str(root),
                            "source_press_overrides": {
                                str(source): {"press_format": "BDRip", "press_group": ""}
                            },
                        }
                    )
                with self.assertRaisesRegex(ValueError, "至少要提供"):
                    preview_organizer_from_ui_body(
                        {
                            "root": str(root),
                            "source_press_overrides": {str(source): {}},
                        }
                    )

                outside = root.parent / "outside-source"
                outside.mkdir()
                outside_plan = preview_organizer_from_ui_body(
                    {
                        "root": str(root),
                        "source_names": [source.name],
                        "source_press_overrides": {
                            str(outside): {"press_format": "BDRip"}
                        },
                    }
                )["plan"]

            self.assertTrue(source_file.is_file())
            self.assertFalse((root / EX_JSUM_TARGET).exists())
            self.assertIn(
                "source-press-override-outside-root",
                {issue["code"] for issue in outside_plan["issues"]},
            )

    def test_web_apply_rebuilds_with_source_press_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, _sources, _files = self._fixture(temp)
            source = root / "Little Busters! EX [BDRip] unidentified"
            source_file = _write_file(source / "Little Busters! EX - 02.mkv", b"manual-group")
            request = {
                "root": str(root),
                "source_names": [source.name],
                "source_press_overrides": {
                    str(source): {"press_format": "BDRip", "press_group": "VCB"}
                },
            }

            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                plan = preview_organizer_from_ui_body(request)["plan"]
                result = apply_organizer_from_ui_body(
                    {
                        **request,
                        "plan_id": plan["plan_id"],
                        "confirmation": plan["plan_id"],
                        "shortcut_plan_id": plan["shortcut_plan_id"],
                        "shortcut_confirmation": plan["shortcut_plan_id"],
                        "acknowledge_move": True,
                        "acknowledge_shortcuts": True,
                    },
                    settings=settings,
                    browse_settings=_browse_settings(settings.catalog_root),
                )

            self.assertTrue(result["ok"])
            self.assertFalse(source_file.exists())
            self.assertTrue(
                (root / EX_VCB_TARGET / "Little Busters! EX_BDRip_Disc" / source_file.name).is_file()
            )

    def test_web_apply_rebuild_detects_changes_before_any_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                preview = preview_organizer_from_ui_body(request)["plan"]
                with self.assertRaisesRegex(ValueError, "acknowledge_move"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": preview["plan_id"],
                            "shortcut_plan_id": preview["shortcut_plan_id"],
                            "shortcut_confirmation": preview["shortcut_plan_id"],
                        }
                    )
                with self.assertRaisesRegex(ValueError, "confirmation 与 plan_id 不一致"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": "0" * 16,
                            "shortcut_plan_id": preview["shortcut_plan_id"],
                            "shortcut_confirmation": preview["shortcut_plan_id"],
                            "acknowledge_move": True,
                            "acknowledge_shortcuts": True,
                        }
                    )
                wrong_shortcut_id = (
                    "f" * 16 if preview["shortcut_plan_id"] != "f" * 16 else "e" * 16
                )
                with self.assertRaisesRegex(ValueError, "快捷方式计划已发生变化"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": preview["plan_id"],
                            "shortcut_plan_id": wrong_shortcut_id,
                            "shortcut_confirmation": wrong_shortcut_id,
                            "acknowledge_move": True,
                            "acknowledge_shortcuts": True,
                        },
                        settings=settings,
                        browse_settings=_browse_settings(settings.catalog_root),
                    )

                files["ex_vcb_main"].write_bytes(b"changed-after-preview")
                with self.assertRaisesRegex(ValueError, "计划已发生变化"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": preview["plan_id"],
                            "shortcut_plan_id": preview["shortcut_plan_id"],
                            "shortcut_confirmation": preview["shortcut_plan_id"],
                            "acknowledge_move": True,
                            "acknowledge_shortcuts": True,
                            "moves": [
                                {
                                    "source": "C:\\not-trusted",
                                    "target": "C:\\also-not-trusted",
                                }
                            ],
                        },
                        settings=settings,
                        browse_settings=_browse_settings(settings.catalog_root),
                    )

            self.assertTrue(files["ex_vcb_main"].is_file())
            self.assertTrue(all(source.exists() for source in sources.values()))
            self.assertFalse((root / EX_VCB_TARGET).exists())

    def test_web_apply_uses_fresh_server_plan_and_ignores_client_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                plan = preview_organizer_from_ui_body(request)["plan"]
                self.assertEqual(plan["shortcut_summary"]["total_count"], 4)
                self.assertEqual(len(plan["shortcut_scope"]["work_refs"]), 2)
                self.assertRegex(plan["shortcut_plan_id"], r"^[0-9a-f]{16}$")
                self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])
                result = apply_organizer_from_ui_body(
                    {
                        **request,
                        "plan_id": plan["plan_id"],
                        "confirmation": plan["plan_id"],
                        "shortcut_plan_id": plan["shortcut_plan_id"],
                        "shortcut_confirmation": plan["shortcut_plan_id"],
                        "acknowledge_move": True,
                        "acknowledge_shortcuts": True,
                        "moves": [
                            {
                                "source": "C:\\not-trusted",
                                "target": "C:\\also-not-trusted",
                            }
                        ],
                    },
                    settings=settings,
                    browse_settings=_browse_settings(settings.catalog_root),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["plan_id"], plan["plan_id"])
            self.assertEqual(result["shortcut_plan_id"], plan["shortcut_plan_id"])
            self.assertEqual(result["shortcuts"]["created_count"], 4)
            self.assertEqual(result["execution"]["moved_file_count"], len(files))
            self.assertTrue(
                (
                    root
                    / REFRAIN_JSUM_TARGET
                    / "Little Busters! Refrain_BDRip_Others"
                    / "Shared"
                    / files["shared_logo"].name
                ).is_file()
            )
            self.assertTrue(all(not source.exists() for source in sources.values()))

    def test_shortcut_preview_blocks_missing_database_path_and_press_path(self) -> None:
        cases = (
            ("work_path", "shortcut-catalog-path-missing"),
            ("press_path", "shortcut-press-path-missing"),
        )
        for case, expected_code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root, _catalog, settings, sources, _files = self._fixture(temp)
                catalog_file = settings.catalog_root / "[JP][TVInfo][test].yaml"
                yaml_text = catalog_file.read_text(encoding="utf-8")
                if case == "work_path":
                    yaml_text = yaml_text.replace(
                        f"path: '{str(root)}'",
                        "path: ''",
                    )
                else:
                    yaml_text = yaml_text.replace(
                        f"press_path: {EX_VCB_TARGET}",
                        "press_path: ''",
                    )
                catalog_file.write_text(yaml_text, encoding="utf-8")
                request = {
                    "root": str(root),
                    "source_names": [sources["ex_vcb"].name],
                }

                plan = preview_organizer_from_ui_body(
                    request,
                    settings=settings,
                )["plan"]

                self.assertFalse(plan["ready"])
                self.assertIn(expected_code, {issue["code"] for issue in plan["issues"]})
                self.assertTrue(plan["repair_required"])
                self.assertEqual(
                    plan["repair"]["preview_endpoint"],
                    "/api/media-directory-organizer/landing/repair/preview",
                )
                self.assertTrue(plan["repair"]["candidates"])
                summary_key = (
                    "missing_catalog_path_count"
                    if case == "work_path"
                    else "missing_press_path_count"
                )
                self.assertGreaterEqual(plan["shortcut_summary"][summary_key], 1)
                self.assertTrue(all(path.is_dir() for path in sources.values()))
                if case == "work_path":
                    retry = preview_organizer_landing_shortcuts_from_ui_body(
                        {"root": str(root)},
                        settings=settings,
                        browse_settings=_browse_settings(settings.catalog_root),
                    )
                    self.assertFalse(retry["ready"])
                    self.assertIn(
                        "shortcut-catalog-path-missing",
                        {issue["code"] for issue in retry["issues"]},
                    )
                    self.assertEqual(
                        retry["shortcut_summary"]["missing_catalog_path_count"],
                        1,
                    )

    def test_missing_press_path_can_repair_move_and_create_shortcut_in_one_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            catalog_file = settings.catalog_root / "[JP][TVInfo][test].yaml"
            catalog_file.write_text(
                catalog_file.read_text(encoding="utf-8").replace(
                    f"press_path: {EX_VCB_TARGET}",
                    "press_path: ''",
                ),
                encoding="utf-8",
            )
            before_catalog = catalog_file.read_bytes()
            request = {
                "root": str(root),
                "source_names": [sources["ex_vcb"].name],
            }
            plan = preview_organizer_from_ui_body(request, settings=settings)["plan"]
            self.assertFalse(plan["ready"])
            self.assertIn(
                "shortcut-press-path-missing",
                {issue["code"] for issue in plan["issues"]},
            )
            candidate = next(
                item
                for item in plan["repair"]["candidates"]
                if item["draft_work"]["name"] == EX_WORK
            )
            repair_body = {
                **request,
                "include_media_move": True,
                "draft_work": candidate["draft_work"],
                "catalog_ref": candidate["catalog_ref"],
            }

            forged_draft = json.loads(json.dumps(candidate["draft_work"]))
            for row in forged_draft["presses"]:
                if row["press_group"] == "VCB":
                    row["press_path"] = "Wrong_Not_Planned_BDRip(VCBM)"
            blocked = preview_organizer_catalog_shortcut_repair_from_ui_body(
                {**repair_body, "draft_work": forged_draft},
                settings=settings,
                browse_settings=_browse_settings(settings.catalog_root),
            )
            self.assertFalse(blocked["ready"])
            self.assertIn(
                "repair-media-target-unregistered",
                {issue["code"] for issue in blocked["issues"]},
            )
            self.assertEqual(catalog_file.read_bytes(), before_catalog)
            self.assertTrue(sources["ex_vcb"].is_dir())

            detail = _browse_settings(settings.catalog_root)
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                repair_body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(preview["ready"], preview["issues"])
            self.assertTrue(preview["media_move_planned"])
            self.assertEqual(preview["catalog_change"]["action"], "update")
            self.assertEqual(preview["shortcut_summary"]["total_count"], 1)
            self.assertEqual(
                preview["shortcut_summary"]["planned_target_creation_count"],
                1,
            )
            self.assertFalse(preview["shortcuts"][0]["target_exists_before_move"])
            self.assertFalse((root / EX_VCB_TARGET).exists())
            self.assertEqual(catalog_file.read_bytes(), before_catalog)

            apply_body = {
                **repair_body,
                "draft_work": preview["draft_work"],
                "catalog_ref": preview["catalog_ref"],
                "repair_plan_id": preview["repair_plan_id"],
                "confirmation": preview["repair_plan_id"],
                "acknowledge_catalog_write": True,
                "acknowledge_move": True,
                "acknowledge_shortcuts": True,
            }
            result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["media"]["moved_file_count"], 4)
            self.assertEqual(result["shortcuts"]["created_count"], 1)
            self.assertFalse(sources["ex_vcb"].exists())
            self.assertTrue(
                (
                    root
                    / EX_VCB_TARGET
                    / "Little Busters! EX_BDRip_Disc"
                    / files["ex_vcb_main"].name
                ).is_file()
            )
            saved = entry_collection_type_data(load_jp_tv_yaml_file(catalog_file)[2])
            self.assertEqual(Path(saved["path"]).resolve(), root.resolve())
            self.assertIn(
                EX_VCB_TARGET,
                {row.get("press_path") for row in saved["collectioned"]},
            )
            repeated = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(repeated["ok"])
            self.assertTrue(repeated["replayed"])
            self.assertEqual(repeated["media"]["moved_file_count"], 4)
            self.assertEqual(repeated["shortcuts"]["created_count"], 0)
            self.assertTrue(
                (
                    root
                    / EX_VCB_TARGET
                    / "Little Busters! EX_BDRip_Disc"
                    / files["ex_vcb_main"].name
                ).is_file()
            )

    def test_integrated_repair_shortcut_failure_retries_without_second_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            catalog_file = settings.catalog_root / "[JP][TVInfo][test].yaml"
            catalog_file.write_text(
                catalog_file.read_text(encoding="utf-8").replace(
                    f"press_path: {EX_VCB_TARGET}",
                    "press_path: ''",
                ),
                encoding="utf-8",
            )
            detail = _browse_settings(settings.catalog_root)
            request = {
                "root": str(root),
                "source_names": [sources["ex_vcb"].name],
            }
            plan = preview_organizer_from_ui_body(request, settings=settings)["plan"]
            candidate = next(
                item
                for item in plan["repair"]["candidates"]
                if item["draft_work"]["name"] == EX_WORK
            )
            repair_body = {
                **request,
                "include_media_move": True,
                "draft_work": candidate["draft_work"],
                "catalog_ref": candidate["catalog_ref"],
            }
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                repair_body,
                settings=settings,
                browse_settings=detail,
            )
            apply_body = {
                **repair_body,
                "draft_work": preview["draft_work"],
                "catalog_ref": preview["catalog_ref"],
                "repair_plan_id": preview["repair_plan_id"],
                "confirmation": preview["repair_plan_id"],
                "acknowledge_catalog_write": True,
                "acknowledge_move": True,
                "acknowledge_shortcuts": True,
            }
            with patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=OSError("simulated integrated shortcut failure"),
            ):
                pending = apply_organizer_catalog_shortcut_repair_from_ui_body(
                    apply_body,
                    settings=settings,
                    browse_settings=detail,
                )

            self.assertFalse(pending["ok"])
            self.assertEqual(pending["state"], "shortcut_pending")
            self.assertEqual(pending["media"]["moved_file_count"], 4)
            self.assertFalse(sources["ex_vcb"].exists())
            moved_file = (
                root
                / EX_VCB_TARGET
                / "Little Busters! EX_BDRip_Disc"
                / files["ex_vcb_main"].name
            )
            self.assertTrue(moved_file.is_file())
            saved = entry_collection_type_data(load_jp_tv_yaml_file(catalog_file)[2])
            self.assertIn(
                EX_VCB_TARGET,
                {row.get("press_path") for row in saved["collectioned"]},
            )

            retry_body = {
                "root": pending["shortcut_retry"]["root"],
                "work_refs": pending["shortcut_retry"]["work_refs"],
            }
            retry = preview_organizer_landing_shortcuts_from_ui_body(
                retry_body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(retry["ready"], retry["issues"])
            self.assertEqual(retry["shortcut_summary"]["total_count"], 1)
            completed = apply_organizer_landing_shortcuts_from_ui_body(
                {
                    **retry_body,
                    "retry_plan_id": retry["retry_plan_id"],
                    "confirmation": retry["retry_plan_id"],
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(completed["ok"])
            self.assertEqual(completed["shortcuts"]["created_count"], 1)
            self.assertTrue(moved_file.is_file())

    def test_shortcut_conflict_blocks_before_any_media_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            first = preview_organizer_from_ui_body(request, settings=settings)["plan"]
            conflict = Path(first["shortcuts"][0]["shortcut_path"])
            conflict.parent.mkdir(parents=True, exist_ok=True)
            conflict.write_text(str(root / "wrong-target"), encoding="utf-8")

            blocked = preview_organizer_from_ui_body(request, settings=settings)["plan"]

            self.assertFalse(blocked["ready"])
            self.assertEqual(blocked["shortcut_summary"]["conflict_count"], 1)
            self.assertIn("shortcut-conflict", {issue["code"] for issue in blocked["issues"]})
            self.assertTrue(all(path.is_file() for path in files.values()))
            self.assertTrue(all(path.is_dir() for path in sources.values()))

    def test_shortcut_failure_returns_restart_safe_retry_without_second_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            source = sources["ex_vcb"]
            request = {
                "root": str(root),
                "source_names": [source.name],
            }
            detail = _browse_settings(settings.catalog_root)
            plan = preview_organizer_from_ui_body(request, settings=settings)["plan"]
            apply_body = {
                **request,
                "plan_id": plan["plan_id"],
                "confirmation": plan["plan_id"],
                "shortcut_plan_id": plan["shortcut_plan_id"],
                "shortcut_confirmation": plan["shortcut_plan_id"],
                "acknowledge_move": True,
                "acknowledge_shortcuts": True,
            }
            with patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=OSError("simulated shortcut failure"),
            ):
                pending = apply_organizer_from_ui_body(
                    apply_body,
                    settings=settings,
                    browse_settings=detail,
                )

            self.assertFalse(pending["ok"])
            self.assertEqual(pending["state"], "shortcut_pending")
            self.assertFalse(source.exists())
            moved_file = (
                root
                / EX_VCB_TARGET
                / "Little Busters! EX_BDRip_Disc"
                / files["ex_vcb_main"].name
            )
            self.assertTrue(moved_file.is_file())
            retry_body = {
                "root": pending["shortcut_retry"]["root"],
                "work_refs": pending["shortcut_retry"]["work_refs"],
            }

            retry = preview_organizer_landing_shortcuts_from_ui_body(
                retry_body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(retry["ready"], retry["issues"])
            self.assertEqual(retry["shortcut_summary"]["total_count"], 1)
            completed = apply_organizer_landing_shortcuts_from_ui_body(
                {
                    **retry_body,
                    "retry_plan_id": retry["retry_plan_id"],
                    "confirmation": retry["retry_plan_id"],
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(completed["ok"])
            self.assertEqual(completed["shortcuts"]["created_count"], 1)
            self.assertTrue(moved_file.is_file())

    def test_root_only_shortcut_retry_uses_all_exact_database_works_without_moving(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            for target in (
                REFRAIN_JSUM_TARGET,
                REFRAIN_VCB_TARGET,
                EX_JSUM_TARGET,
                EX_VCB_TARGET,
            ):
                (root / target).mkdir()
            detail = _browse_settings(settings.catalog_root)
            source_snapshot = {
                name: (path.stat().st_size, path.stat().st_mtime_ns)
                for name, path in files.items()
            }

            preview = preview_organizer_landing_shortcuts_from_ui_body(
                {"root": str(root)},
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(preview["ready"], preview["issues"])
            self.assertEqual(len(preview["work_refs"]), 3)
            self.assertEqual(preview["shortcut_summary"]["total_count"], 6)
            completed = apply_organizer_landing_shortcuts_from_ui_body(
                {
                    "root": str(root),
                    "retry_plan_id": preview["retry_plan_id"],
                    "confirmation": preview["retry_plan_id"],
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )

            self.assertEqual(completed["shortcuts"]["created_count"], 6)
            self.assertTrue(all(path.is_dir() for path in sources.values()))
            self.assertEqual(
                source_snapshot,
                {
                    name: (path.stat().st_size, path.stat().st_mtime_ns)
                    for name, path in files.items()
                },
            )

    def test_already_organized_root_reports_complete_instead_of_empty_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Already Organized"
            root.mkdir()
            target = root / "Already Organized_BDRip(VCBM)"
            media = _write_file(
                target / "Already Organized_BDRip_Disc" / "episode.mkv",
                b"already-landed",
            )
            legacy_empty_directories = [
                target / "CDs",
                target / "Scans",
                target / "SPs",
            ]
            for directory in legacy_empty_directories:
                directory.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            (catalog_root / "[JP][TVInfo][2024].yaml").write_text(
                f"""\
- attributes:
  - type: date
    data:
      start: '20240101'
      end: '20240301'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: VCB
        press_path: {target.name}
      markers: []
      path: '{str(root).replace("'", "''")}'
  - type: country
    data: japan
  - type: name
    data: Already Organized
""",
                encoding="utf-8",
            )
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)

            pending = preview_organizer_from_ui_body(
                {"root": str(root)},
                settings=settings,
            )["plan"]

            self.assertFalse(pending["ready"])
            self.assertEqual(pending["media_state"], "already_organized")
            self.assertEqual(pending["shortcut_state"], "pending")
            self.assertEqual(pending["issues"], [])
            self.assertNotIn(
                "shortcut-scope-empty",
                {issue["code"] for issue in pending.get("shortcut_issues") or []},
            )
            self.assertEqual(pending["shortcut_summary"]["planned_count"], 1)
            self.assertTrue(media.is_file())
            self.assertTrue(all(path.is_dir() for path in legacy_empty_directories))

            retry_body = {
                "root": str(root),
                "work_refs": pending["shortcut_scope"]["work_refs"],
            }
            retry = preview_organizer_landing_shortcuts_from_ui_body(
                retry_body,
                settings=settings,
                browse_settings=detail,
            )
            completed = apply_organizer_landing_shortcuts_from_ui_body(
                {
                    **retry_body,
                    "retry_plan_id": retry["retry_plan_id"],
                    "confirmation": retry["retry_plan_id"],
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )
            self.assertEqual(completed["shortcuts"]["created_count"], 1)

            checked = preview_organizer_from_ui_body(
                {"root": str(root)},
                settings=settings,
            )["plan"]
            self.assertEqual(checked["media_state"], "already_organized")
            self.assertEqual(checked["shortcut_state"], "complete")
            self.assertEqual(checked["issues"], [])
            self.assertEqual(checked["shortcut_summary"]["planned_count"], 0)
            self.assertEqual(checked["shortcut_summary"]["already_exists_count"], 1)
            self.assertTrue(media.is_file())
            self.assertTrue(all(path.is_dir() for path in legacy_empty_directories))

    def test_already_organized_root_with_missing_press_path_stays_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Missing Press Path"
            root.mkdir()
            media = _write_file(
                root / "Missing Press Path_BDRip" / "Missing Press Path_BDRip_Disc" / "episode.mkv",
                b"already-landed",
            )
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2024].yaml"
            catalog_file.write_text(
                f"""\
- attributes:
  - type: date
    data:
      start: '20240101'
      end: '20240301'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: VCB
        press_path: ''
      markers: []
      path: '{str(root).replace("'", "''")}'
  - type: country
    data: japan
  - type: name
    data: Missing Press Path
""",
                encoding="utf-8",
            )
            catalog_before = catalog_file.read_bytes()
            media_before = (media.stat().st_size, media.stat().st_mtime_ns)

            plan = preview_organizer_from_ui_body(
                {"root": str(root)},
                settings=_settings(catalog_root, base, root),
            )["plan"]

            self.assertEqual(plan["assignments"], [])
            self.assertEqual(plan["moves"], [])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertEqual(plan["media_state"], "already_organized")
            self.assertEqual(plan["shortcut_state"], "blocked")
            issue_codes = {issue["code"] for issue in plan["issues"]}
            self.assertIn("shortcut-press-path-missing", issue_codes)
            self.assertNotIn("shortcut-scope-empty", issue_codes)
            self.assertFalse(plan["ready"])
            self.assertTrue(plan["repair_required"])
            self.assertEqual(len(plan["repair"]["candidates"]), 1)
            self.assertEqual(
                plan["repair"]["candidates"][0]["catalog_ref"]["work_name"],
                "Missing Press Path",
            )
            self.assertEqual(
                plan["repair"]["candidates"][0]["draft_work"]["presses"][0]["press_path"],
                "",
            )
            self.assertEqual(catalog_file.read_bytes(), catalog_before)
            self.assertEqual(
                (media.stat().st_size, media.stat().st_mtime_ns),
                media_before,
            )
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])

    def test_catalog_shortcut_repair_appends_db_creates_shortcut_and_replays_without_moving(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Recovered Work"
            root.mkdir()
            target = root / "Recovered Work_BDRip(VCBM)"
            target.mkdir()
            sentinel = _write_file(target / "episode.mkv", b"already-organized")
            before_sentinel = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2020].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            before_catalog = catalog_file.read_bytes()
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            body = {
                "root": str(root),
                "draft_work": {
                    "name": "Recovered Work",
                    "date": {"start": "2020-01-01", "end": "2020-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [
                        {
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": target.name,
                        }
                    ],
                },
            }

            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body,
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(preview["ready"], preview["issues"])
            self.assertEqual(preview["catalog_change"]["action"], "append")
            self.assertRegex(preview["repair_plan_id"], r"^[0-9a-f]{16}$")
            self.assertEqual(catalog_file.read_bytes(), before_catalog)
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])
            apply_body = {
                **body,
                "repair_plan_id": preview["repair_plan_id"],
                "confirmation": preview["repair_plan_id"],
                "acknowledge_catalog_write": True,
                "acknowledge_shortcuts": True,
            }
            result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["media"]["moved_file_count"], 0)
            self.assertTrue(result["media"]["skipped"])
            self.assertEqual(result["shortcuts"]["created_count"], 1)
            self.assertIn("path:", catalog_file.read_text(encoding="utf-8"))
            self.assertIn(target.name, catalog_file.read_text(encoding="utf-8"))
            self.assertEqual(
                (sentinel.read_bytes(), sentinel.stat().st_mtime_ns),
                before_sentinel,
            )

            self.assertEqual(len(list((base / "History").glob("*.yaml"))), 1)

            after_first_apply = catalog_file.read_bytes()
            repeated = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(repeated["ok"])
            self.assertTrue(repeated["replayed"])
            self.assertEqual(repeated["shortcuts"]["created_count"], 0)
            self.assertEqual(repeated["shortcuts"]["already_exists_count"], 1)
            self.assertEqual(len(load_jp_tv_yaml_file(catalog_file)), 1)
            self.assertEqual(catalog_file.read_bytes(), after_first_apply)
            self.assertEqual(len(list(self._shortcut_root.rglob("*.lnk"))), 1)
            self.assertEqual(
                (sentinel.read_bytes(), sentinel.stat().st_mtime_ns),
                before_sentinel,
            )

            forged = "0" * 16 if preview["repair_plan_id"] != "0" * 16 else "1" * 16
            with self.assertRaisesRegex(ValueError, "修复计划已发生变化"):
                apply_organizer_catalog_shortcut_repair_from_ui_body(
                    {
                        **apply_body,
                        "repair_plan_id": forged,
                        "confirmation": forged,
                    },
                    settings=settings,
                    browse_settings=detail,
                )

            with self.assertRaisesRegex(ValueError, "用于另一份修复请求"):
                apply_organizer_catalog_shortcut_repair_from_ui_body(
                    {
                        **apply_body,
                        "draft_work": {
                            **apply_body["draft_work"],
                            "name": "Another Recovered Work",
                        },
                    },
                    settings=settings,
                    browse_settings=detail,
                )

            catalog_file.write_bytes(catalog_file.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "数据库又发生变化"):
                apply_organizer_catalog_shortcut_repair_from_ui_body(
                    apply_body,
                    settings=settings,
                    browse_settings=detail,
                )

    def test_catalog_shortcut_repair_updates_only_selected_work_and_preserves_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Recovered Existing"
            root.mkdir()
            target = root / "Recovered Existing_BDRip(Jsum)"
            target.mkdir()
            sentinel = _write_file(target / "episode.mkv", b"already-organized")
            before_sentinel = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2021].yaml"
            catalog_file.write_text(
                """\
- attributes:
  - type: date
    data:
      start: '20210101'
      end: '20210301'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
      continuations:
      - title: Bonus continuation
        collectioned:
        - press_format: BDRip
          press_group: VCB
          press_path: Existing_Bonus(VCBM)
      markers:
      - subs
  - type: country
    data: japan
  - type: name
    data: Recovered Existing
""",
                encoding="utf-8",
            )
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            body = {
                "root": str(root),
                "catalog_ref": {
                    "yaml_source_rel": catalog_file.name,
                    "index_in_file": 0,
                    "work_name": "Recovered Existing",
                },
                "draft_work": {
                    "name": "Recovered Existing",
                    "date": {"start": "2021-01-01", "end": "2021-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [
                        {
                            "press_format": "BDRip",
                            "press_group": "JSUM",
                            "press_path": target.name,
                        }
                    ],
                },
            }

            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(preview["ready"], preview["issues"])
            self.assertEqual(preview["catalog_change"]["action"], "update")
            apply_body = {
                **body,
                "catalog_ref": preview["catalog_ref"],
                "draft_work": preview["draft_work"],
                "repair_plan_id": preview["repair_plan_id"],
                "confirmation": preview["repair_plan_id"],
                "acknowledge_catalog_write": True,
                "acknowledge_shortcuts": True,
            }
            result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )

            saved = catalog_file.read_text(encoding="utf-8")
            self.assertTrue(result["ok"])
            self.assertIn(str(root).replace("\\", "/"), saved)
            self.assertIn(target.name, saved)
            self.assertIn("Bonus continuation", saved)
            self.assertIn("Existing_Bonus(VCBM)", saved)
            self.assertIn("- subs", saved)
            self.assertEqual(
                (sentinel.read_bytes(), sentinel.stat().st_mtime_ns),
                before_sentinel,
            )
            self.assertEqual(len(list((base / "History").glob("*.yaml"))), 1)

            after_first_apply = catalog_file.read_bytes()
            self.assertTrue(apply_body["catalog_ref"]["source_sha256"])
            repeated = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(repeated["replayed"])
            self.assertEqual(repeated["shortcuts"]["created_count"], 0)
            self.assertEqual(catalog_file.read_bytes(), after_first_apply)
            self.assertEqual(len(list((base / "History").glob("*.yaml"))), 1)

    def test_catalog_shortcut_repair_requires_explicit_prefix_candidate_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Fate／Zero"
            root.mkdir()
            target = root / "Fate／Zero 01-12_BDRip(VCBM)"
            target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2011].yaml"
            entries = []
            for name in ("Fate／Zero 01-12", "Fate／Zero 13-25"):
                entries.append(
                    f"""\
- attributes:
  - type: date
    data: {{start: '20111001', end: '20111224'}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: VCB
      markers: []
  - type: country
    data: japan
  - type: name
    data: {name}
"""
                )
            catalog_file.write_text("".join(entries), encoding="utf-8")
            before = catalog_file.read_bytes()
            settings = _settings(catalog_root, base, root)
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                {
                    "root": str(root),
                    "draft_work": {
                        "name": "Fate／Zero",
                        "date": {"start": "2011-10-01", "end": "2011-12-24"},
                        "domain": "animation",
                        "country": "japan",
                        "release_type": "tv",
                        "path": str(root),
                        "presses": [
                            {
                                "press_format": "BDRip",
                                "press_group": "VCB",
                                "press_path": target.name,
                            }
                        ],
                    },
                },
                settings=settings,
                browse_settings=_browse_settings(catalog_root),
            )

            self.assertFalse(preview["ready"])
            self.assertEqual(preview["catalog_change"]["action"], "selection_required")
            self.assertEqual(len(preview["repair_candidates"]), 2)
            self.assertIn(
                "repair-catalog-selection-required",
                {issue["code"] for issue in preview["issues"]},
            )
            self.assertEqual(catalog_file.read_bytes(), before)
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])

    def test_catalog_repair_preserves_same_pair_continuation_and_legacy_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Legacy Continuation"
            root.mkdir()
            target = root / "Legacy Continuation_BDRip"
            target.mkdir()
            bonus_target = root / "Legacy_Bonus"
            bonus_target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2019].yaml"
            catalog_file.write_text(
                """\
- attributes:
  - type: date
    data: {start: '20190101', end: '20190301'}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: '----'
      continuations:
      - title: Legacy bonus
        collectioned:
        - press_format: BDRip
          press_group: '----'
          press_path: Legacy_Bonus
      markers: [music, subs]
  - type: country
    data: japan
  - type: name
    data: Legacy Continuation
""",
                encoding="utf-8",
            )
            body = {
                "root": str(root),
                "catalog_ref": {
                    "yaml_source_rel": catalog_file.name,
                    "index_in_file": 0,
                    "work_name": "Legacy Continuation",
                },
                "draft_work": {
                    "name": "Legacy Continuation",
                    "date": {"start": "2019-01-01", "end": "2019-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [
                        {
                            "press_key": "0:main::BDRip:----",
                            "segment": "main",
                            "press_format": "BDRip",
                            "press_group": "----",
                            "press_path": target.name,
                        },
                        {
                            "press_key": "1:continuation:0:BDRip:----",
                            "segment": "continuation",
                            "continuation_index": 0,
                            "continuation_title": "Legacy bonus",
                            "press_format": "BDRip",
                            "press_group": "----",
                            "press_path": bonus_target.name,
                        },
                    ],
                },
            }
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body, settings=settings, browse_settings=detail
            )
            self.assertTrue(preview["ready"], preview["issues"])
            self.assertEqual(preview["shortcut_summary"]["total_count"], 1)
            self.assertEqual(preview["shortcut_summary"]["deduplicated_press_count"], 1)
            result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                {
                    **body,
                    "repair_plan_id": preview["repair_plan_id"],
                    "confirmation": preview["repair_plan_id"],
                    "acknowledge_catalog_write": True,
                    "acknowledge_shortcuts": True,
                },
                settings=settings,
                browse_settings=detail,
            )
            saved = catalog_file.read_text(encoding="utf-8")
            self.assertTrue(result["ok"])
            self.assertIn(f"press_path: {target.name}", saved)
            self.assertIn("title: Legacy bonus", saved)
            self.assertIn("press_path: Legacy_Bonus", saved)
            self.assertEqual(saved.count("press_group: '----'"), 2)
            saved_data = entry_collection_type_data(load_jp_tv_yaml_file(catalog_file)[0])
            self.assertEqual(saved_data["markers"], ["music", "subs"])

    def test_catalog_repair_rejects_duplicate_existing_press_key_without_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Duplicate Press Key"
            root.mkdir()
            first_target = root / "Duplicate Press Key_BDRip(VCBM)"
            second_target = root / "Duplicate Press Key_BDRip(VCBM)_Other"
            first_target.mkdir()
            second_target.mkdir()
            sentinel = _write_file(first_target / "episode.mkv", b"unchanged-media")
            media_before = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2020].yaml"
            catalog_file.write_text(
                """\
- attributes:
  - type: date
    data: {start: '20200101', end: '20200301'}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: VCB
      markers: []
  - type: country
    data: japan
  - type: name
    data: Duplicate Press Key
""",
                encoding="utf-8",
            )
            catalog_before = catalog_file.read_bytes()
            body = {
                "root": str(root),
                "catalog_ref": {
                    "yaml_source_rel": catalog_file.name,
                    "index_in_file": 0,
                    "work_name": "Duplicate Press Key",
                },
                "draft_work": {
                    "name": "Duplicate Press Key",
                    "date": {"start": "2020-01-01", "end": "2020-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [
                        {
                            "press_key": "0:main::BDRip:VCB",
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": first_target.name,
                        },
                        {
                            "press_key": "0:main::BDRip:VCB",
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": second_target.name,
                        },
                    ],
                },
            }

            with self.assertRaisesRegex(ValueError, "重复选择同一数据库压制记录"):
                preview_organizer_catalog_shortcut_repair_from_ui_body(
                    body,
                    settings=_settings(catalog_root, base, root),
                    browse_settings=_browse_settings(catalog_root),
                )

            self.assertEqual(catalog_file.read_bytes(), catalog_before)
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])
            self.assertEqual(
                (sentinel.read_bytes(), sentinel.stat().st_mtime_ns),
                media_before,
            )

    def test_catalog_repair_independent_append_requires_explicit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Shared Family"
            root.mkdir()
            existing_target = root / "Existing_BDRip(VCBM)"
            existing_target.mkdir()
            target = root / "Independent_BDRip(VCBM)"
            target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2024].yaml"
            catalog_file.write_text(
                f"""\
- attributes:
  - type: date
    data: {{start: '20240101', end: '20240301'}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      path: '{str(root).replace(chr(92), '/')}'
      collectioned:
      - press_format: BDRip
        press_group: VCB
        press_path: {existing_target.name}
      markers: []
  - type: country
    data: japan
  - type: name
    data: Existing Family Child
""",
                encoding="utf-8",
            )
            body = {
                "root": str(root),
                "draft_work": {
                    "name": "Independent Family Child",
                    "date": {"start": "2024-04-01", "end": "2024-06-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [{
                        "press_format": "BDRip",
                        "press_group": "VCB",
                        "press_path": target.name,
                    }],
                },
            }
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            blocked = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body, settings=settings, browse_settings=detail
            )
            self.assertEqual(blocked["catalog_change"]["action"], "selection_required")
            self.assertTrue(blocked["independent_append_allowed"])

            explicit_body = {**body, "catalog_intent": "append_independent"}
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                explicit_body, settings=settings, browse_settings=detail
            )
            self.assertTrue(preview["ready"], preview["issues"])
            self.assertEqual(preview["catalog_change"]["action"], "append")
            apply_body = {
                **explicit_body,
                "repair_plan_id": preview["repair_plan_id"],
                "confirmation": preview["repair_plan_id"],
                "acknowledge_catalog_write": True,
                "acknowledge_shortcuts": True,
            }
            result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(len(load_jp_tv_yaml_file(catalog_file)), 2)
            catalog_after_first_apply = catalog_file.read_bytes()
            history_after_first_apply = list((base / "History").glob("*.yaml"))

            repeated = apply_organizer_catalog_shortcut_repair_from_ui_body(
                apply_body,
                settings=settings,
                browse_settings=detail,
            )

            self.assertTrue(repeated["replayed"])
            self.assertEqual(repeated["shortcuts"]["created_count"], 0)
            self.assertEqual(catalog_file.read_bytes(), catalog_after_first_apply)
            self.assertEqual(len(load_jp_tv_yaml_file(catalog_file)), 2)
            self.assertEqual(
                list((base / "History").glob("*.yaml")),
                history_after_first_apply,
            )

    def test_catalog_shortcut_apply_rebuilds_plan_inside_catalog_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Locked Repair"
            root.mkdir()
            target = root / "Locked Repair_BDRip(VCBM)"
            target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            (catalog_root / "[JP][TVInfo][2026].yaml").write_text("[]\n", encoding="utf-8")
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            body = {
                "root": str(root),
                "draft_work": {
                    "name": "Locked Repair",
                    "date": {"start": "2026-01-01", "end": "2026-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [{
                        "press_format": "BDRip",
                        "press_group": "VCB",
                        "press_path": target.name,
                    }],
                },
            }
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body,
                settings=settings,
                browse_settings=detail,
            )
            from media_directory_organizer import landing as landing_module

            original_preview = landing_module.preview_catalog_shortcut_repair
            competing_started = threading.Event()
            competing_acquired = threading.Event()
            competitor_threads: list[threading.Thread] = []

            def competing_writer() -> None:
                competing_started.set()
                with catalog_write_transaction(catalog_root):
                    competing_acquired.set()

            def preview_while_locked(*args: object, **kwargs: object) -> dict[str, object]:
                competitor = threading.Thread(target=competing_writer, daemon=True)
                competitor_threads.append(competitor)
                competitor.start()
                self.assertTrue(competing_started.wait(1.0))
                self.assertFalse(
                    competing_acquired.wait(0.2),
                    "a competing catalog writer acquired the lock during plan rebuild",
                )
                return original_preview(*args, **kwargs)

            with patch.object(
                landing_module,
                "preview_catalog_shortcut_repair",
                side_effect=preview_while_locked,
            ):
                result = apply_organizer_catalog_shortcut_repair_from_ui_body(
                    {
                        **body,
                        "repair_plan_id": preview["repair_plan_id"],
                        "confirmation": preview["repair_plan_id"],
                        "acknowledge_catalog_write": True,
                        "acknowledge_shortcuts": True,
                    },
                    settings=settings,
                    browse_settings=detail,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(competing_acquired.wait(2.0))
            for competitor in competitor_threads:
                competitor.join(timeout=2.0)
                self.assertFalse(competitor.is_alive())

    def test_catalog_repair_never_auto_selects_duplicate_exact_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Duplicate Exact"
            root.mkdir()
            target = root / "Duplicate Exact_BDRip(VCBM)"
            target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2025].yaml"
            entry = f"""\
- attributes:
  - type: date
    data: {{start: '20250101', end: '20250301'}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      path: '{str(root).replace(chr(92), '/')}'
      collectioned:
      - press_format: BDRip
        press_group: VCB
        press_path: {target.name}
      markers: []
  - type: country
    data: japan
  - type: name
    data: Duplicate Exact
"""
            catalog_file.write_text(entry + entry, encoding="utf-8")
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                {
                    "root": str(root),
                    "draft_work": {
                        "name": "Duplicate Exact",
                        "date": {"start": "2025-01-01", "end": "2025-03-01"},
                        "domain": "animation",
                        "country": "japan",
                        "release_type": "tv",
                        "path": str(root),
                        "presses": [{
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": target.name,
                        }],
                    },
                },
                settings=_settings(catalog_root, base, root),
                browse_settings=_browse_settings(catalog_root),
            )
            self.assertFalse(preview["ready"])
            self.assertEqual(preview["catalog_change"]["action"], "selection_required")
            self.assertEqual(len(preview["repair_candidates"]), 2)
            self.assertFalse(preview["independent_append_allowed"])

    def test_catalog_shortcut_repair_blocks_missing_target_and_rolls_back_db_on_link_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Recovered Failure"
            root.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2022].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            before = catalog_file.read_bytes()
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            draft = {
                "name": "Recovered Failure",
                "date": {"start": "2022-01-01", "end": "2022-03-01"},
                "domain": "animation",
                "country": "japan",
                "release_type": "tv",
                "path": str(root),
                "presses": [
                    {
                        "press_format": "BDRip",
                        "press_group": "VCB",
                        "press_path": "Recovered Failure_BDRip(VCBM)",
                    }
                ],
            }
            missing = preview_organizer_catalog_shortcut_repair_from_ui_body(
                {"root": str(root), "draft_work": draft},
                settings=settings,
                browse_settings=detail,
            )
            self.assertFalse(missing["ready"])
            self.assertIn("repair-target-missing", {issue["code"] for issue in missing["issues"]})
            self.assertEqual(catalog_file.read_bytes(), before)

            (root / "Recovered Failure_BDRip(VCBM)").mkdir()
            ready = preview_organizer_catalog_shortcut_repair_from_ui_body(
                {"root": str(root), "draft_work": draft},
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(ready["ready"], ready["issues"])
            with patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=OSError("simulated repair shortcut failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated repair shortcut failure"):
                    apply_organizer_catalog_shortcut_repair_from_ui_body(
                        {
                            "root": str(root),
                            "draft_work": draft,
                            "repair_plan_id": ready["repair_plan_id"],
                            "confirmation": ready["repair_plan_id"],
                            "acknowledge_catalog_write": True,
                            "acknowledge_shortcuts": True,
                        },
                        settings=settings,
                        browse_settings=detail,
                    )
            self.assertEqual(catalog_file.read_bytes(), before)
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])

    def test_catalog_shortcut_repair_cas_rollback_preserves_later_catalog_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Recovered Concurrent"
            root.mkdir()
            target = root / "Recovered Concurrent_BDRip(VCBM)"
            target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2024].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            body = {
                "root": str(root),
                "draft_work": {
                    "name": "Recovered Concurrent",
                    "date": {"start": "2024-01-01", "end": "2024-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [
                        {
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": target.name,
                        }
                    ],
                },
            }
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body,
                settings=settings,
                browse_settings=detail,
            )
            self.assertTrue(preview["ready"], preview["issues"])

            def later_catalog_write_then_fail(*_args: object, **_kwargs: object) -> None:
                browse_save_yaml_from_ui_body(
                    {
                        "rows": [
                            {
                                "yaml_source_rel": catalog_file.name,
                                "index_in_file": 0,
                                "name": "Recovered Concurrent - later writer",
                            }
                        ]
                    },
                    settings=detail,
                )
                raise OSError("simulated shortcut failure after another DB write")

            with patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=later_catalog_write_then_fail,
            ):
                with self.assertRaisesRegex(
                    CatalogRollbackConflictError,
                    "数据库已有后续改动，未自动回滚",
                ):
                    apply_organizer_catalog_shortcut_repair_from_ui_body(
                        {
                            **body,
                            "repair_plan_id": preview["repair_plan_id"],
                            "confirmation": preview["repair_plan_id"],
                            "acknowledge_catalog_write": True,
                            "acknowledge_shortcuts": True,
                        },
                        settings=settings,
                        browse_settings=detail,
                    )

            saved = catalog_file.read_text(encoding="utf-8")
            self.assertIn("Recovered Concurrent - later writer", saved)
            self.assertIn(target.name, saved)
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])

    def test_catalog_repair_handler_reports_preserved_database_as_partial_409(self) -> None:
        from work_catalog_yaml.jp_tv import browse_api

        with patch.object(
            browse_api,
            "apply_organizer_catalog_shortcut_repair_from_ui_body",
            side_effect=CatalogRollbackConflictError("simulated later catalog write"),
        ):
            response = browse_api._post_media_directory_organizer_catalog_shortcut_repair_apply_api.__wrapped__({})

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "partial")
        self.assertTrue(payload["catalog"]["later_changes_preserved"])
        self.assertFalse(payload["catalog"]["rolled_back"])
        self.assertIn("已保留且未回滚", payload["error"])

    def test_catalog_shortcut_repair_rejects_symlink_target_before_resolving(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Recovered Symlink"
            root.mkdir()
            real_target = base / "ordinary-target"
            real_target.mkdir()
            linked_target = root / "Recovered Symlink_BDRip(VCBM)"
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked_target), str(real_target)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if created.returncode != 0:
                    self.skipTest(f"directory junctions are unavailable: {created.stderr}")
            else:
                try:
                    linked_target.symlink_to(real_target, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"directory symlinks are unavailable on this platform: {exc}")
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2025].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            before = catalog_file.read_bytes()

            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                {
                    "root": str(root),
                    "draft_work": {
                        "name": "Recovered Symlink",
                        "date": {"start": "2025-01-01", "end": "2025-03-01"},
                        "domain": "animation",
                        "country": "japan",
                        "release_type": "tv",
                        "path": str(root),
                        "presses": [
                            {
                                "press_format": "BDRip",
                                "press_group": "VCB",
                                "press_path": linked_target.name,
                            }
                        ],
                    },
                },
                settings=_settings(catalog_root, base, root),
                browse_settings=_browse_settings(catalog_root),
            )

            self.assertFalse(preview["ready"])
            self.assertIn(
                "repair-target-unsafe",
                {issue["code"] for issue in preview["issues"]},
            )
            self.assertEqual(preview["shortcuts"], [])
            self.assertEqual(catalog_file.read_bytes(), before)

    def test_catalog_shortcut_repair_rebuilds_and_rejects_database_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Recovered Drift"
            root.mkdir()
            target = root / "Recovered Drift_BDRip(VCBM)"
            target.mkdir()
            catalog_root = base / "db"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[JP][TVInfo][2023].yaml"
            catalog_file.write_text("[]\n", encoding="utf-8")
            settings = _settings(catalog_root, base, root)
            detail = _browse_settings(catalog_root)
            body = {
                "root": str(root),
                "draft_work": {
                    "name": "Recovered Drift",
                    "date": {"start": "2023-01-01", "end": "2023-03-01"},
                    "domain": "animation",
                    "country": "japan",
                    "release_type": "tv",
                    "path": str(root),
                    "presses": [
                        {
                            "press_format": "BDRip",
                            "press_group": "VCB",
                            "press_path": target.name,
                        }
                    ],
                },
            }
            preview = preview_organizer_catalog_shortcut_repair_from_ui_body(
                body,
                settings=settings,
                browse_settings=detail,
            )
            catalog_file.write_text("[]\n\n", encoding="utf-8")
            drifted = catalog_file.read_bytes()

            with self.assertRaisesRegex(ValueError, "修复计划已发生变化"):
                apply_organizer_catalog_shortcut_repair_from_ui_body(
                    {
                        **body,
                        "repair_plan_id": preview["repair_plan_id"],
                        "confirmation": preview["repair_plan_id"],
                        "acknowledge_catalog_write": True,
                        "acknowledge_shortcuts": True,
                    },
                    settings=settings,
                    browse_settings=detail,
                )

            self.assertEqual(catalog_file.read_bytes(), drifted)
            self.assertEqual(list(self._shortcut_root.rglob("*.lnk")), [])


if __name__ == "__main__":
    unittest.main()
