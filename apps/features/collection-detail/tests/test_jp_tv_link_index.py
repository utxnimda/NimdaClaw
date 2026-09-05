from __future__ import annotations

import base64
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from collection_detail import link_index as link_index_mod
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.link_index import (
    collection_link_index_payload,
    generate_link_index_files_from_ui_body,
    generate_link_index_from_ui_body,
    open_link_index_path_from_ui_body,
    preview_link_index_from_ui_body,
    resource_libraries_cached_payload,
    resource_libraries_node_payload,
    save_resource_library_roots_from_ui_body,
    scan_resource_libraries_payload,
    save_link_index_from_ui_body,
)
from work_catalog_yaml.yaml_io import load_yaml


def _settings(db: Path, *paths: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=db,
        resolved_default_readable=str(paths[0]) if paths else None,
        resolved_catalog_yaml_paths=tuple(str(p) for p in paths),
        enum_options={},
        enum_labels={
            "domain": {"animation": "动画"},
            "country": {"japan": "日本"},
            "release_type": {"tv": "TV"},
        },
        enum_section_labels={},
        app_features=(),
    )


def _catalog_yaml() -> str:
    return (
        textwrap.dedent(
            """
            - attributes:
              - type: date
                data:
                  start: "20990101"
                  end: "20990331"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
                  collectioned:
                  - press_format: "BDRip"
                    press_group: "VCB"
                  - press_format: "720p"
                    press_group: "DMG"
                  markers: []
              - type: country
                data: japan
              - type: name
                data: Absolute Duo
            """
        ).strip()
        + "\n"
    )


def _catalog_yaml_mapped(name: str, work_path: str, press_path: str) -> str:
    return (
        textwrap.dedent(
            f"""
            - attributes:
              - type: date
                data:
                  start: "20980101"
                  end: "20980331"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
                  path: "{work_path}"
                  collectioned:
                  - press_format: "BDRip"
                    press_group: "VCB"
                    press_path: "{press_path}"
                  markers: []
              - type: country
                data: japan
              - type: name
                data: {name}
            """
        ).strip()
        + "\n"
    )


def _catalog_yaml_with_press(name: str, press_rows: list[tuple[str, str]]) -> str:
    return _catalog_yaml_with_press_dates(name, press_rows, "20990101", "20990331")


def _catalog_yaml_with_press_dates(name: str, press_rows: list[tuple[str, str]], start: str, end: str) -> str:
    rows = "\n".join(
        f"                  - press_format: \"{fmt}\"\n                    press_group: \"{grp}\""
        for fmt, grp in press_rows
    )
    return (
        textwrap.dedent(
            f"""
            - attributes:
              - type: date
                data:
                  start: "{start}"
                  end: "{end}"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
                  collectioned:
{rows}
                  markers: []
              - type: country
                data: japan
              - type: name
                data: {name}
            """
        ).strip()
        + "\n"
    )


def _find_link_by_relpath(node: dict, relpath: str) -> dict | None:
    if node.get("type") == "link":
        node_relpath = str(node.get("relpath") or "")
        if node_relpath == relpath:
            return node
        bracket = relpath.find("[")
        if bracket >= 0 and node_relpath.endswith(relpath[bracket:]):
            return node
    for child in node.get("children", []) or []:
        found = _find_link_by_relpath(child, relpath)
        if found:
            return found
    return None


def _find_link_by_relpath_suffix(node: dict, suffix: str) -> dict | None:
    if node.get("type") == "link" and str(node.get("relpath") or "").endswith(suffix):
        return node
    for child in node.get("children", []) or []:
        found = _find_link_by_relpath_suffix(child, suffix)
        if found:
            return found
    return None


class JpTvLinkIndexTest(unittest.TestCase):
    def test_rebuild_rejects_shortcut_collisions_before_clearing_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, finish = root / "DB", root / "Finish"
            db.mkdir()
            finish.mkdir()
            existing = finish / "keep.txt"
            existing.write_text("keep", encoding="utf-8")
            targets = [root / "A", root / "B"]
            for target in targets:
                target.mkdir()
            items = [{"shortcut_root": str(finish), "shortcut_relpath": "same.lnk", "target_path": str(target)} for target in targets]
            with (
                patch.object(link_index_mod, "_feature_config", return_value={"paths": {"shortcut_root": str(finish)}}),
                patch.object(link_index_mod, "_link_index_file_generation_plan", return_value=(items, True)),
                patch.object(link_index_mod, "_create_windows_shortcut") as create,
            ):
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=_settings(db))
                self.assertEqual(preview["file_generation"]["conflict_count"], 1)
                with self.assertRaisesRegex(ValueError, "同一个快捷方式路径"):
                    generate_link_index_files_from_ui_body({"confirm_clear": True, "confirm_clear_twice": True}, settings=_settings(db))
                create.assert_not_called()
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

    def test_scoped_shortcuts_reject_duplicate_path_with_different_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, media, finish = root / "DB", root / "media", root / "Finish"
            db.mkdir()
            targets = [media / "A", media / "B"]
            for target in targets:
                target.mkdir(parents=True)
            items = [{"shortcut_root": str(finish), "shortcut_relpath": "same.lnk", "shortcut_path": str(finish / "same.lnk"), "target_path": str(target)} for target in targets]
            with (
                patch.object(link_index_mod, "_feature_config", return_value={"paths": {"shortcut_root": str(finish), "resource_roots": [str(media)]}}),
                patch.object(link_index_mod, "_create_windows_shortcut") as create,
            ):
                with self.assertRaisesRegex(FileExistsError, "同一个快捷方式路径"):
                    link_index_mod.apply_scoped_shortcuts_for_work(items, settings=_settings(db))
                create.assert_not_called()
            self.assertFalse(finish.exists())

    def test_scoped_shortcut_apply_wraps_entire_process_transaction_in_catalog_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            settings = _settings(db)
            events: list[str] = []

            class TransactionProbe:
                def __enter__(self) -> None:
                    events.append("enter")

                def __exit__(self, *_args: object) -> None:
                    events.append("exit")

            def apply_probe(
                items: list[dict[str, object]],
                *,
                settings: JpTvBrowseSettings,
            ) -> dict[str, object]:
                self.assertEqual(items, [])
                self.assertEqual(settings.filesystem_root, db)
                self.assertEqual(events, ["enter"])
                return {"ok": True}

            with (
                patch.object(
                    link_index_mod,
                    "catalog_write_transaction",
                    return_value=TransactionProbe(),
                ) as transaction,
                patch.object(
                    link_index_mod,
                    "_apply_scoped_shortcuts_for_work_process_locked",
                    side_effect=apply_probe,
                ),
            ):
                result = link_index_mod.apply_scoped_shortcuts_for_work([], settings=settings)

            self.assertEqual(result, {"ok": True})
            transaction.assert_called_once_with(db)
            self.assertEqual(events, ["enter", "exit"])

    def setUp(self) -> None:
        self._index_db_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._index_db_temp.cleanup)
        self._index_db_path_patch = patch.object(
            link_index_mod,
            "_link_index_db_path",
            return_value=Path(self._index_db_temp.name) / "link-index.yaml",
        )
        self._index_db_path_patch.start()
        self.addCleanup(self._index_db_path_patch.stop)
        link_index_mod._SHORTCUT_SCAN_CACHE["signature"] = None
        link_index_mod._SHORTCUT_SCAN_CACHE["leaves"] = []
        link_index_mod._LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
        link_index_mod._LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None
        link_index_mod._FEATURE_CONFIG_CACHE["signature"] = None
        link_index_mod._FEATURE_CONFIG_CACHE["data"] = None

    def test_current_config_json_omits_legacy_media_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config = {
                "paths": {
                    "media_root": str(base / "legacy-media"),
                    "resource_roots": [str(base / "library")],
                    "shortcut_root": str(base / "finish"),
                }
            }
            with patch.object(link_index_mod, "_feature_config", return_value=config):
                payload = link_index_mod.collection_link_index_config_json()

            self.assertNotIn("media_root", payload)
            self.assertEqual(payload["resource_roots"], [str((base / "library").resolve())])
            self.assertEqual(payload["shortcut_roots"], [str((base / "finish").resolve())])

    def test_shortcut_root_profile_routes_korea_television_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            default_root = base / "japan" / "Finish"
            korea_root = base / "korea" / "Finish"
            target = base / "media" / "K Drama" / "K Drama_1080p"
            target.mkdir(parents=True)
            config = {
                "paths": {
                    "shortcut_root": str(default_root),
                    "shortcut_roots": [
                        {
                            "match": {
                                "domain": "television",
                                "country": "korea",
                                "release_type": "tv",
                            },
                            "root": str(korea_root),
                        }
                    ],
                }
            }
            press = {
                "press_key": "0:main::1080p:----",
                "press_format": "1080p",
                "press_group": "----",
                "press_path": target.name,
                "label": "1080p-----",
            }
            base_work = {
                "work_key": "catalog.yaml#0",
                "yaml_source_rel": "catalog.yaml",
                "index_in_file": 0,
                "name": "K Drama",
                "path": str(target.parent),
                "year": "2026",
                "year_label": "[2026]",
                "begin_date": "20260101",
                "end_date": "20260201",
                "date_range_label": "[20260101][20260201]",
                "release_type": "tv",
            }
            with patch.object(link_index_mod, "_feature_config", return_value=config):
                korea_item = link_index_mod._index_entry_from_work_press(
                    {**base_work, "domain": "television", "country": "korea"},
                    press,
                    [],
                )
                japan_item = link_index_mod._index_entry_from_work_press(
                    {**base_work, "domain": "animation", "country": "japan"},
                    press,
                    [],
                )
            self.assertEqual(Path(korea_item["shortcut_root"]), korea_root.resolve())
            self.assertEqual(Path(japan_item["shortcut_root"]), default_root.resolve())
            self.assertTrue(Path(korea_item["shortcut_path"]).is_relative_to(korea_root.resolve()))
            self.assertTrue(Path(japan_item["shortcut_path"]).is_relative_to(default_root.resolve()))

    def test_scan_shortcut_leaves_marks_link_only_when_target_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = root / "finish"
            target = media / "Good Work" / "BDRip"
            target.mkdir(parents=True)
            (finish / "[2099]").mkdir(parents=True)
            good = finish / "[2099]" / "Good.lnk"
            bad = finish / "[2099]" / "Broken.lnk"
            good.write_text("", encoding="utf-8")
            bad.write_text("", encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }

            target_infos = {
                str(good.resolve()): {"target_path": str(target), "target_resolved": True, "error": ""},
                str(bad.resolve()): {
                    "target_path": str(media / "Missing Work" / "BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                leaves = link_index_mod._scan_shortcut_leaves(refresh_targets=True)

            by_name = {item["name"]: item for item in leaves}
            self.assertTrue(by_name["Good.lnk"]["shortcut_exists"])
            self.assertTrue(by_name["Good.lnk"]["target_exists"])
            self.assertFalse(by_name["Broken.lnk"]["target_exists"])

            changed_infos = {
                str(good.resolve()): {
                    "target_path": str(media / "Moved Work" / "BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
                str(bad.resolve()): {
                    "target_path": str(media / "Missing Work" / "BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
            }
            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=changed_infos,
            ):
                cached = link_index_mod._scan_shortcut_leaves()
                refreshed = link_index_mod._scan_shortcut_leaves(refresh_targets=True)

            cached_by_name = {item["name"]: item for item in cached}
            refreshed_by_name = {item["name"]: item for item in refreshed}
            self.assertTrue(cached_by_name["Good.lnk"]["target_exists"])
            self.assertFalse(refreshed_by_name["Good.lnk"]["target_exists"])

    def test_scan_shortcut_leaves_uses_persistent_cache_without_reparsing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = root / "finish"
            target = media / "Good Work" / "BDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "Good.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }
            target_infos = {
                str(shortcut.resolve()): {"target_path": str(target), "target_resolved": True, "error": ""},
            }

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch("collection_detail.link_index._windows_shortcut_targets", return_value=target_infos),
                patch("collection_detail.link_index.feature_data_root", return_value=root / "data"),
            ):
                first = link_index_mod._scan_shortcut_leaves(refresh_targets=True)

            self.assertTrue((root / "data" / "cache" / "link-index-shortcut-scan-cache.yaml").is_file())
            self.assertEqual(first[0]["target_path"], str(target))
            link_index_mod._SHORTCUT_SCAN_CACHE["signature"] = None
            link_index_mod._SHORTCUT_SCAN_CACHE["leaves"] = []

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch(
                    "collection_detail.link_index._windows_shortcut_targets",
                    side_effect=AssertionError("persistent cache should avoid target parsing"),
                ),
                patch("collection_detail.link_index.feature_data_root", return_value=root / "data"),
            ):
                cached = link_index_mod._scan_shortcut_leaves()

            self.assertEqual(cached[0]["target_path"], str(target))
            self.assertTrue(cached[0]["target_exists"])

    def test_windows_shortcut_targets_decodes_unicode_target_paths(self) -> None:
        shortcut = Path(r"C:\Links\BDRip(VCB).lnk")
        target = r"Y:\涼宮ハルヒの憂鬱\涼宮ハルヒの憂鬱 2009_BDRip(VCB)"
        payload = json.dumps(
            [{"path": str(shortcut), "target": target, "error": ""}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        proc = type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
            },
        )()

        with patch("collection_detail.link_index.subprocess.run", return_value=proc):
            result = link_index_mod._windows_shortcut_targets([shortcut])

        self.assertEqual(result[str(shortcut.resolve())]["target_path"], target)
        self.assertTrue(result[str(shortcut.resolve())]["target_resolved"])

    def test_resource_libraries_default_to_legacy_media_root_and_scan_two_levels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = media / "Finish"
            (media / "Series A" / "Work A_BDRip").mkdir(parents=True)
            (media / "Series A" / "Work A_BDRip" / "ep01.mkv").write_bytes(b"x")
            (media / "Series A" / "Work B_720p-HKGX").mkdir(parents=True)
            (media / "Series A" / "not-a-resource.txt").write_text("x", encoding="utf-8")
            (media / "$RECYCLE.BIN" / "Deleted Work_BDRip").mkdir(parents=True)
            (media / "Loose").mkdir(parents=True)
            (finish / "[2099]").mkdir(parents=True)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_excludes": {str(media): ["$recycle"]},
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ):
                payload = scan_resource_libraries_payload()
                cached = resource_libraries_cached_payload()
                root_node = resource_libraries_node_payload("root:0")["node"]
                series_a_loaded = resource_libraries_node_payload("root:0/Series A")["node"]
                work_a = resource_libraries_node_payload("root:0/Series A/Work A_BDRip")["node"]

            self.assertEqual(payload["summary"]["root_count"], 1)
            self.assertEqual(payload["summary"]["series_count"], 2)
            self.assertEqual(payload["summary"]["item_count"], 2)
            self.assertEqual(payload["summary"]["file_count"], 2)
            self.assertEqual(payload["summary"]["dir_count"], 4)
            self.assertEqual(payload["summary"]["size"], 2)
            self.assertEqual(payload["summary"]["direct_child_count"], 2)
            self.assertEqual(payload["summary"]["total_child_count"], 6)
            self.assertTrue(cached["cached"])
            self.assertEqual(cached["summary"]["item_count"], 2)
            self.assertTrue(str(cached["cache_path"]).endswith("resource-library-scan-cache.yaml"))
            self.assertFalse(cached["tree"]["children"][0]["children_loaded"])
            self.assertEqual(root_node["size"], 2)
            self.assertEqual(root_node["direct_child_count"], 2)
            self.assertEqual(root_node["total_child_count"], 6)
            self.assertIsInstance(root_node["mtime"], int)
            series_nodes = root_node["children"]
            series_a = next(node for node in series_nodes if node["name"] == "Series A")
            self.assertFalse(series_a["children_loaded"])
            self.assertEqual(series_a["size"], 2)
            self.assertEqual(series_a["direct_child_count"], 3)
            self.assertEqual(series_a["total_child_count"], 4)
            self.assertEqual(series_a_loaded["children"][0]["name"], "Work A_BDRip")
            self.assertEqual(series_a_loaded["files"][0]["name"], "not-a-resource.txt")
            self.assertEqual(series_a_loaded["size"], 2)
            self.assertEqual(series_a_loaded["direct_child_count"], 3)
            self.assertEqual(series_a_loaded["total_child_count"], 4)
            self.assertEqual(work_a["files"][0]["name"], "ep01.mkv")
            self.assertEqual(work_a["size"], 1)
            self.assertEqual(work_a["direct_child_count"], 1)
            self.assertEqual(work_a["total_child_count"], 1)
            self.assertNotIn("Finish", {root["name"] for root in payload["roots"][0]["series"]})
            self.assertNotIn("$RECYCLE.BIN", {root["name"] for root in payload["roots"][0]["series"]})
            first = payload["items"][0]
            self.assertEqual(first["series_name"], "Series A")
            self.assertEqual(first["work_name"], "Work A")
            self.assertEqual(first["press_info"], "BDRip")
            self.assertEqual(first["relpath"], "Series A/Work A_BDRip")

    def test_resource_libraries_scan_defers_deep_resource_children(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = media / "Finish"
            deep = media / "Series A" / "Work A_BDRip" / "Disc 1"
            deep.mkdir(parents=True)
            (deep / "ep01.mkv").write_bytes(b"x")
            (finish / "[2099]").mkdir(parents=True)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ):
                payload = scan_resource_libraries_payload()
                work_a = resource_libraries_node_payload("root:0/Series A/Work A_BDRip")["node"]
                disc_1 = resource_libraries_node_payload("root:0/Series A/Work A_BDRip/Disc 1")["node"]

            self.assertEqual(payload["summary"]["item_count"], 1)
            self.assertEqual(work_a["children"][0]["name"], "Disc 1")
            self.assertFalse(work_a["children"][0]["children_loaded"])
            self.assertEqual(work_a["children"][0]["size"], 1)
            self.assertEqual(work_a["size"], 1)
            self.assertEqual(work_a["total_child_count"], 2)
            self.assertEqual(work_a["files"], [])
            self.assertEqual(disc_1["files"][0]["name"], "ep01.mkv")
            self.assertEqual(disc_1["size"], 1)

    def test_save_resource_library_roots_writes_feature_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.yaml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    version: 1
                    paths:
                      media_root: "E:/Old"
                    link_index:
                      scan_max_dirs: 2000
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            lib_a = root / "Media A"
            lib_b = root / "Media B"

            with patch("collection_detail.link_index.feature_config_path", return_value=cfg_path):
                result = save_resource_library_roots_from_ui_body(
                    {
                        "roots": [
                            {"path": str(lib_a), "excludes": ["$recycle", "System Volume Information"]},
                            str(lib_b),
                            {"path": str(lib_a), "excludes": ["ignored-duplicate"]},
                        ]
                    },
                )

            raw = load_yaml(cfg_path)
            self.assertEqual(raw["paths"]["resource_roots"], [str(lib_a.resolve()), str(lib_b.resolve())])
            self.assertEqual(
                raw["paths"]["resource_excludes"],
                {str(lib_a.resolve()): ["$recycle", "System Volume Information"]},
            )
            self.assertEqual(result["config"]["resource_roots"], [str(lib_a.resolve()), str(lib_b.resolve())])
            self.assertEqual(result["config"]["resource_excludes"][str(lib_a.resolve())], ["$recycle", "System Volume Information"])

    def test_missing_shortcut_target_gets_resource_library_fix_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            shortcut = finish / "[2098]" / "[20980101][20980331] Air Gear" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("shortcut", encoding="utf-8")
            missing_target = root / "missing" / "Air Gear_BDRip"
            resource = root / "resource"
            fixed_target = resource / "Air Gear" / "Air Gear_BDRip"
            fixed_target.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("BDRip", "----")]), encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {"shortcut_name": "{press_format}"},
            }
            st = _settings(db, fp)

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value={str(shortcut.resolve()): {"target_path": str(missing_target), "target_resolved": True}},
            ):
                scan_resource_libraries_payload()
                payload = generate_link_index_from_ui_body({}, settings=st)

            self.assertEqual(payload["plan_summary"]["ready"], 1)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 0)
            tree_link = _find_link_by_relpath(payload["tree"], "[2099]/[20990101][20990331] Air Gear/BDRip.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertEqual(tree_link["target_path"], str(fixed_target.resolve()))
            self.assertEqual(tree_link["source"], "index_db")

    def test_resource_fix_uses_db_default_match_for_remaining_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            resource = root / "resource"
            work_name = "Haruhi 2009"
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_vcb = shortcut_dir / "BDRip(VCB).lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("shortcut", encoding="utf-8")
            shortcut_vcb.write_text("shortcut", encoding="utf-8")
            fixed_plain = resource / work_name / f"{work_name}_BDRip(CASO)"
            fixed_vcb = resource / work_name / f"{work_name}_BDRip(VCB)"
            fixed_plain.mkdir(parents=True)
            fixed_vcb.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press(work_name, [("BDRip", "VCB"), ("BDRip", "CASO")]), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{date_range_label} {name}"],
                    "shortcut_name": "{press_format}{press_group_suffix}",
                },
            }
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(root / "missing" / work_name / f"{work_name}_BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
                str(shortcut_vcb.resolve()): {
                    "target_path": str(root / "missing" / work_name / f"{work_name}_BDRip(VCB)"),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                scan_resource_libraries_payload()
                payload = generate_link_index_from_ui_body({}, settings=st)

            self.assertEqual(payload["plan_summary"]["ready"], 2)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 0)
            plain_node = _find_link_by_relpath(payload["tree"], f"[2099]/[20990101][20990331] {work_name}/BDRip(CASO).lnk")
            vcb_node = _find_link_by_relpath(payload["tree"], f"[2099]/[20990101][20990331] {work_name}/BDRip(VCB).lnk")
            self.assertIsNotNone(plain_node)
            self.assertIsNotNone(vcb_node)
            assert plain_node is not None
            assert vcb_node is not None
            self.assertEqual(plain_node["target_path"], str(fixed_plain.resolve()))
            self.assertEqual(vcb_node["target_path"], str(fixed_vcb.resolve()))

    def test_resource_fix_falls_back_to_shortcut_press_and_resource_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            resource = root / "resource"
            work_name = "Haruhi 2009"
            series_name = "Haruhi"
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("shortcut", encoding="utf-8")
            fixed_plain = resource / series_name / f"{work_name}_BDRip"
            older_plain = resource / series_name / f"{series_name}_BDRip"
            fixed_plain.mkdir(parents=True)
            older_plain.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press(work_name, [("BDRip", "VCB"), ("BDRip", "CASO")]), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{date_range_label} {name}"],
                    "shortcut_name": "{press_format}{press_group_suffix}",
                },
            }
            resource_items = [
                {
                    "root": str(resource),
                    "series_name": series_name,
                    "name": f"{work_name}_BDRip",
                    "work_name": "",
                    "press_info": "BDRip",
                    "relpath": f"{series_name}/{work_name}_BDRip",
                    "path": str(fixed_plain),
                },
                {
                    "root": str(resource),
                    "series_name": series_name,
                    "name": f"{series_name}_BDRip",
                    "work_name": series_name,
                    "press_info": "BDRip",
                    "relpath": f"{series_name}/{series_name}_BDRip",
                    "path": str(older_plain),
                }
            ]
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(root / "missing" / series_name / f"{work_name}_BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._resource_fix_items_from_cache",
                return_value=resource_items,
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = generate_link_index_from_ui_body({}, settings=st)

            self.assertEqual(payload["plan_summary"]["ready"], 2)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 0)
            plain_node = _find_link_by_relpath(payload["tree"], f"[2099]/[20990101][20990331] {work_name}/BDRip(VCB).lnk")
            self.assertIsNotNone(plain_node)
            assert plain_node is not None
            self.assertEqual(plain_node["target_path"], str(fixed_plain.resolve()))

    def test_preview_uses_catalog_mapping_to_build_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            (media / "Absolute Duo" / "BDRip VCB").mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{domain_label}", "{country_label}", "{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)
                work = payload["works"][0]
                press0 = work["press"][0]
                out = preview_link_index_from_ui_body(
                    {
                        "works": [
                            {
                                "yaml_source_rel": work["yaml_source_rel"],
                                "index_in_file": work["index_in_file"],
                                "path": "Absolute Duo",
                                "press": [
                                    {
                                        "press_key": press0["press_key"],
                                        "press_path": "BDRip VCB",
                                    },
                                ],
                            },
                        ],
                    },
                    settings=st,
                )

            self.assertEqual(out["plan_summary"]["ready"], 1)
            plan0 = out["plan"][0]
            self.assertTrue(plan0["shortcut_path"].endswith(r"动画\日本\[2099]\Absolute Duo\BDRip-VCB.lnk"))
            self.assertTrue(plan0["target_path"].endswith(r"media\Absolute Duo\BDRip VCB"))
            self.assertEqual(out["tree"]["children"][0]["name"], "动画")
            self.assertIsNotNone(
                _find_link_by_relpath(out["tree"], "鍔ㄧ敾/鏃ユ湰/[2099]/Absolute Duo/BDRip-VCB.lnk")
            )

    def test_default_shortcut_layout_matches_existing_finish_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(
                _catalog_yaml_with_press("Mapped Work", [("BDRip", "VCB"), ("DVDRip", "----")]),
                encoding="utf-8",
            )
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = preview_link_index_from_ui_body(
                    {
                        "works": [
                            {
                                "yaml_source_rel": "[JP][TVInfo][2098].yaml",
                                "index_in_file": 0,
                                "path": "Mapped Work",
                                "press": [
                                    {"press_key": "0:main::BDRip:VCB", "press_path": "Mapped Work_BDRip"},
                                    {"press_key": "1:main::DVDRip:----", "press_path": "Mapped Work_DVDRip"},
                                ],
                            }
                        ]
                    },
                    settings=st,
                )

            relpaths = sorted(item["shortcut_relpath"] for item in payload["plan"])
            self.assertEqual(
                relpaths,
                [
                    "[2099]/[20990101][20990331] Mapped Work/BDRip(VCB).lnk",
                    "[2099]/[20990101][20990331] Mapped Work/DVDRip.lnk",
                ],
            )

    def test_default_shortcut_layout_compacts_iso_catalog_dates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(
                _catalog_yaml_with_press_dates(
                    "Mapped Work",
                    [("BDRip", "VCB")],
                    "2099-01-01",
                    "2099-03-31",
                ),
                encoding="utf-8",
            )
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = preview_link_index_from_ui_body(
                    {
                        "works": [
                            {
                                "yaml_source_rel": "[JP][TVInfo][2099].yaml",
                                "index_in_file": 0,
                                "path": "Mapped Work",
                                "press": [
                                    {
                                        "press_key": "0:main::BDRip:VCB",
                                        "press_path": "Mapped Work_BDRip",
                                    }
                                ],
                            }
                        ]
                    },
                    settings=st,
                )

            self.assertEqual(payload["works"][0]["begin_date"], "20990101")
            self.assertEqual(payload["works"][0]["end_date"], "20990331")
            self.assertEqual(
                payload["plan"][0]["shortcut_relpath"],
                "[2099]/[20990101][20990331] Mapped Work/BDRip(VCB).lnk",
            )

    def test_scoped_shortcut_preview_compacts_iso_and_preserves_compact_dates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media_root = root / "media"
            target = media_root / "Mapped Work" / "Mapped Work_BDRip"
            target.mkdir(parents=True)
            cfg = {
                "paths": {
                    "resource_roots": [str(media_root)],
                    "shortcut_root": str(root / "finish"),
                }
            }
            press = {
                "press_format": "BDRip",
                "press_group": "VCB",
                "press_path": target.name,
                "target_path": str(target),
            }

            with patch.object(link_index_mod, "_feature_config", return_value=cfg):
                for start, end in (
                    ("2099-01-01", "2099-03-31"),
                    ("20990101", "20990331"),
                ):
                    with self.subTest(start=start, end=end):
                        planned = link_index_mod.preview_scoped_shortcuts_for_work(
                            {
                                "name": "Mapped Work",
                                "path": str(target.parent),
                                "date": {"start": start, "end": end},
                                "domain": "animation",
                                "country": "japan",
                                "release_type": "tv",
                            },
                            [press],
                        )

                        self.assertEqual(
                            planned[0]["shortcut_relpath"],
                            "[2099]/[20990101][20990331] Mapped Work/BDRip(VCB).lnk",
                        )
                        self.assertEqual(planned[0]["index_entry"]["begin_date"], "20990101")
                        self.assertEqual(planned[0]["index_entry"]["end_date"], "20990331")

    def test_save_and_generate_write_catalog_path_and_press_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            (media / "Absolute Duo" / "BDRip VCB").mkdir(parents=True)
            (media / "Absolute Duo" / "720p DMG").mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)
                work = payload["works"][0]
                body = {
                    "works": [
                        {
                            "yaml_source_rel": work["yaml_source_rel"],
                            "index_in_file": work["index_in_file"],
                            "path": "Absolute Duo",
                            "press": [
                                {"press_key": work["press"][0]["press_key"], "press_path": "BDRip VCB"},
                                {"press_key": work["press"][1]["press_key"], "press_path": "720p DMG"},
                            ],
                        },
                    ],
                }
                saved = save_link_index_from_ui_body(body, settings=st)
                created: list[tuple[Path, Path]] = []

                def fake_shortcut(shortcut_path: Path, target_path: Path) -> None:
                    created.append((shortcut_path, target_path))

                with patch("collection_detail.link_index._create_windows_shortcut", side_effect=fake_shortcut):
                    generated = generate_link_index_from_ui_body(body, settings=st)

            saved_doc = load_yaml(fp)
            coll = saved_doc[0]["attributes"][1]["data"]
            self.assertEqual(coll["path"], "Absolute Duo")
            self.assertEqual(coll["collectioned"][0]["press_path"], "BDRip VCB")
            self.assertEqual(coll["collectioned"][1]["press_path"], "720p DMG")
            self.assertEqual(saved["plan_summary"]["ready"], 2)
            self.assertEqual(generated["index_db"]["item_count"], 2)
            self.assertEqual(generated["plan_summary"]["created"], 0)
            self.assertEqual(len(created), 0)

    def test_generate_link_index_files_requires_confirm_and_can_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            backup_root = root / "backup"
            target = media / "Mapped Work" / "BDRip VCB"
            target.mkdir(parents=True)
            finish.mkdir(parents=True)
            (finish / "old.txt").write_text("old", encoding="utf-8")
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            created: list[tuple[Path, Path]] = []

            def fake_shortcut(shortcut_path: Path, target_path: Path) -> None:
                created.append((shortcut_path, target_path))
                shortcut_path.parent.mkdir(parents=True, exist_ok=True)
                shortcut_path.write_text(str(target_path), encoding="utf-8")

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch("collection_detail.link_index._create_windows_shortcut", side_effect=fake_shortcut):
                generate_link_index_from_ui_body({}, settings=st)
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=st)
                self.assertTrue(preview["file_generation"]["root_non_empty"])
                with self.assertRaises(ValueError):
                    generate_link_index_files_from_ui_body({}, settings=st)
                result = generate_link_index_files_from_ui_body(
                    {
                        "confirm_clear": True,
                        "confirm_clear_twice": True,
                        "backup_dir": str(backup_root),
                    },
                    settings=st,
                )

            backup_path = Path(result["file_generation"]["backup_path"])
            link_path = finish / "[2098]" / "Mapped Work" / "BDRip-VCB.lnk"
            self.assertEqual(result["file_generation"]["created"], 1)
            self.assertEqual(result["file_generation"]["removed_count"], 1)
            self.assertTrue((backup_path / "old.txt").is_file())
            self.assertFalse((finish / "old.txt").exists())
            self.assertTrue(link_path.is_file())
            self.assertEqual(link_path.read_text(encoding="utf-8"), str(target.resolve()))
            self.assertEqual(created, [(link_path.resolve(), target.resolve())])

    def test_save_accepts_absolute_work_path_but_keeps_press_path_relative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            real = root / "real-media" / "Absolute Duo"
            (real / "Absolute Duo_BDRip").mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)
                work = payload["works"][0]
                body = {
                    "works": [
                        {
                            "yaml_source_rel": work["yaml_source_rel"],
                            "index_in_file": work["index_in_file"],
                            "path": str(real),
                            "press": [
                                {
                                    "press_key": work["press"][0]["press_key"],
                                    "press_path": "Absolute Duo_BDRip",
                                },
                            ],
                        },
                    ],
                }
                saved = save_link_index_from_ui_body(body, settings=st)

            saved_doc = load_yaml(fp)
            coll = saved_doc[0]["attributes"][1]["data"]
            self.assertEqual(Path(coll["path"]), real)
            self.assertEqual(coll["collectioned"][0]["press_path"], "Absolute Duo_BDRip")
            self.assertEqual(saved["plan_summary"]["ready"], 1)

    def test_payload_scans_all_db_yaml_and_marks_unmapped_disk_links(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            (media / "Mapped Work" / "BDRip VCB").mkdir(parents=True)
            (finish / "Loose").mkdir(parents=True)
            (finish / "Loose" / "Orphan.lnk").write_text("", encoding="utf-8")
            fp_visible = db / "[JP][TVInfo][2099].yaml"
            fp_extra = db / "[JP][TVInfo][2098].yaml"
            fp_visible.write_text(_catalog_yaml(), encoding="utf-8")
            fp_extra.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp_visible)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)

            self.assertEqual(len(payload["works"]), 2)
            self.assertEqual(payload["plan_summary"]["total"], 3)
            self.assertEqual(payload["plan_summary"]["ready"], 1)
            self.assertEqual(payload["plan_summary"]["empty_target_path"], 2)
            self.assertEqual(payload["plan_summary"]["unmapped_on_disk"], 0)
            self.assertFalse(payload["disk_summary"]["db_match_cached"])
            self.assertEqual(payload["mapping_summary"]["unconfigured_press"], 2)

    def test_generate_keeps_empty_target_items_in_index_db_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ):
                generated = generate_link_index_from_ui_body({}, settings=st)

            index_doc = load_yaml(Path(generated["index_db"]["path"]))
            self.assertEqual(generated["index_db"]["item_count"], 2)
            self.assertEqual(generated["plan_summary"]["empty_target_path"], 2)
            self.assertEqual(len(index_doc["items"]), 2)
            self.assertTrue(all(not item.get("target_path") for item in index_doc["items"]))
            tree_link = _find_link_by_relpath(generated["tree"], "[2099]/Absolute Duo/BDRip-VCB.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertEqual(tree_link["target_path"], "")

    def test_payload_marks_db_linked_when_existing_shortcut_targets_same_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Mapped Work" / "BDRip VCB"
            target.mkdir(parents=True)
            shortcut = finish / "[2098]" / "[20980101] Mapped Work" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }
            target_infos = {
                str(shortcut.resolve()): {
                    "target_path": str(target),
                    "target_resolved": True,
                    "error": "",
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = collection_link_index_payload(st)

            self.assertEqual(payload["plan_summary"]["unmapped_on_disk"], 0)
            tree_link = _find_link_by_relpath(payload["tree"], "[2098]/Mapped Work/BDRip-VCB.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertFalse(tree_link["shortcut_exists"])
            self.assertTrue(tree_link["link_exists"])
            self.assertTrue(tree_link["db_linked"])
            self.assertEqual(tree_link["target_path"], str(target))

    def test_generate_renames_target_matched_shortcut_inside_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Mapped Work" / "BDRip VCB"
            target.mkdir(parents=True)
            old_shortcut = finish / "[2098]" / "[20980101] Mapped Work" / "BDRip.lnk"
            old_shortcut.parent.mkdir(parents=True)
            old_shortcut.write_text("shortcut", encoding="utf-8")
            expected_shortcut = finish / "[2098]" / "[20980101] Mapped Work" / "BDRip(VCB).lnk"
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
            }

            def fake_targets(paths: list[Path]) -> dict[str, dict[str, str]]:
                return {
                    str(path.resolve()): {
                        "target_path": str(target),
                        "target_resolved": True,
                        "error": "",
                    }
                    for path in paths
                }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                side_effect=fake_targets,
            ):
                result = generate_link_index_from_ui_body({}, settings=st)

            self.assertTrue(old_shortcut.exists())
            self.assertFalse(expected_shortcut.exists())
            self.assertEqual(result["index_db"]["item_count"], 1)
            self.assertEqual(result["plan_summary"]["renamed"], 0)
            self.assertEqual(result["plan_summary"]["created"], 0)
            self.assertEqual(result["plan_summary"]["unmapped_on_disk"], 0)
            self.assertEqual(result["plan"][0]["shortcut_relpath"], "[2098]/[20980101][20980331] Mapped Work/BDRip(VCB).lnk")

    def test_lite_payload_uses_cached_tree_without_reloading_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                first = collection_link_index_payload(st, lite=True)

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._load_catalog_works",
                side_effect=AssertionError("cached lite payload should not reload DB works"),
            ):
                second = collection_link_index_payload(st, lite=True)

            self.assertIs(first, second)
            self.assertNotIn("works", second)
            self.assertNotIn("plan", second)

    def test_open_shortcut_allows_target_outside_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = root / "finish"
            target = root / "resource" / "Absolute Duo" / "BDRip"
            shortcut = finish / "[2099]" / "Absolute Duo" / "BDRip.lnk"
            media.mkdir()
            target.mkdir(parents=True)
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch("collection_detail.link_index._windows_shortcut_target", return_value=str(target)),
                patch("collection_detail.link_index.os.name", "nt"),
                patch("collection_detail.link_index.os.startfile", create=True) as startfile,
            ):
                result = open_link_index_path_from_ui_body({"path": str(shortcut)})

            self.assertEqual(Path(result["path"]), target.resolve())
            self.assertEqual(Path(result["source_path"]), shortcut.resolve())
            self.assertTrue(result["resolved_from_shortcut"])
            startfile.assert_called_once_with(str(target.resolve()))


if __name__ == "__main__":
    unittest.main()
