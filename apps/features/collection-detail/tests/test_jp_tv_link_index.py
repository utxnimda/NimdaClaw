from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from collection_detail import link_index as link_index_mod
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.link_index import (
    apply_link_index_associations_from_ui_body,
    apply_link_index_target_fixes_from_ui_body,
    collection_link_index_payload,
    generate_link_index_from_ui_body,
    link_index_association_payload,
    open_link_index_path_from_ui_body,
    preview_link_index_from_ui_body,
    reject_link_index_association_from_ui_body,
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


def _catalog_yaml_with_press_paths(
    name: str,
    press_rows: list[tuple[str, str, str]],
    *,
    work_path: str = "",
) -> str:
    rows = "\n".join(
        f"                  - press_format: \"{fmt}\"\n                    press_group: \"{grp}\"\n                    press_path: \"{press_path}\""
        for fmt, grp, press_path in press_rows
    )
    path_line = f'                  path: "{work_path}"\n' if work_path else ""
    return (
        textwrap.dedent(
            f"""
            - attributes:
              - type: date
                data:
                  start: "20990101"
                  end: "20990331"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
{path_line.rstrip()}
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
    if node.get("type") == "link" and node.get("relpath") == relpath:
        return node
    for child in node.get("children", []) or []:
        found = _find_link_by_relpath(child, relpath)
        if found:
            return found
    return None


class JpTvLinkIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        link_index_mod._SHORTCUT_SCAN_CACHE["signature"] = None
        link_index_mod._SHORTCUT_SCAN_CACHE["leaves"] = []
        link_index_mod._DISK_ASSOC_CACHE["signature"] = None
        link_index_mod._DISK_ASSOC_CACHE["rows"] = []
        link_index_mod._LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
        link_index_mod._LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None

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

    def test_resource_libraries_default_to_media_root_and_scan_two_levels(self) -> None:
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
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {"shortcut_name": "{press_format}"},
            }
            st = _settings(db)

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value={str(shortcut.resolve()): {"target_path": str(missing_target), "target_resolved": True}},
            ):
                scan_resource_libraries_payload()
                payload = collection_link_index_payload(st, refresh_links=True)

            self.assertEqual(payload["plan_summary"]["missing_target"], 0)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 1)
            tree_link = _find_link_by_relpath(payload["tree"], "[2098]/[20980101][20980331] Air Gear/BDRip.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertEqual(tree_link["target_fix"]["target_path"], str(fixed_target.resolve()))

    def test_apply_link_target_fix_rewrites_shortcut_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            shortcut = finish / "[2098]" / "[20980101][20980331] Air Gear" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("shortcut", encoding="utf-8")
            fixed_target = root / "resource" / "Air Gear" / "Air Gear_BDRip"
            fixed_target.mkdir(parents=True)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(root / "resource")],
                },
                "link_index": {"shortcut_name": "{press_format}"},
            }
            st = _settings(db)
            repaired: list[tuple[Path, Path]] = []

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=lambda shortcut_path, target_path: repaired.append((shortcut_path, target_path)),
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value={str(shortcut.resolve()): {"target_path": str(fixed_target), "target_resolved": True}},
            ):
                scan_resource_libraries_payload()
                result = apply_link_index_target_fixes_from_ui_body(
                    {
                        "items": [
                            {
                                "shortcut_path": str(shortcut),
                                "target_path": str(fixed_target),
                            }
                        ]
                    },
                    settings=st,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(repaired, [(shortcut.resolve(), fixed_target.resolve())])
            self.assertEqual(result["fixes"][0]["shortcut_path"], str(shortcut.resolve()))
            self.assertEqual(result["fixes"][0]["target_path"], str(fixed_target.resolve()))

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
            self.assertEqual(
                out["tree"]["children"][0]["children"][0]["children"][0]["children"][0]["children"][0]["name"],
                "BDRip-VCB.lnk",
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
            self.assertEqual(generated["plan_summary"]["created"], 2)
            self.assertEqual(len(created), 2)

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

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._association_rows_for_disk_leaves",
                side_effect=AssertionError("plain tree payload should not do DB association matching"),
            ):
                payload = collection_link_index_payload(st)

            self.assertEqual(len(payload["works"]), 2)
            self.assertEqual(payload["plan_summary"]["total"], 1)
            self.assertEqual(payload["plan_summary"]["ready"], 1)
            self.assertEqual(payload["plan_summary"]["unmapped_on_disk"], 1)
            self.assertFalse(payload["disk_summary"]["db_match_cached"])
            self.assertEqual(payload["mapping_summary"]["unconfigured_press"], 2)

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
            tree_link = _find_link_by_relpath(payload["tree"], "[2098]/[20980101] Mapped Work/BDRip-VCB.lnk")
            self.assertIsNotNone(tree_link)
            self.assertFalse(tree_link["shortcut_exists"])
            self.assertTrue(tree_link["link_exists"])
            self.assertTrue(tree_link["db_linked"])
            self.assertEqual(tree_link["matched_shortcut_relpath"], "[2098]/[20980101] Mapped Work/BDRip.lnk")

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

            self.assertFalse(old_shortcut.exists())
            self.assertTrue(expected_shortcut.exists())
            self.assertEqual(result["plan_summary"]["renamed"], 1)
            self.assertEqual(result["plan_summary"]["created"], 0)
            self.assertEqual(result["plan_summary"]["unmapped_on_disk"], 0)
            self.assertEqual(result["plan"][0]["shortcut_relpath"], "[2098]/[20980101] Mapped Work/BDRip(VCB).lnk")

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

    def test_association_matches_unmapped_shortcut_by_clean_work_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Absolute Duo" / "Absolute Duo_BDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101] Absolute Duo" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
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

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch("collection_detail.link_index._windows_shortcut_target", return_value=str(target)),
            ):
                payload = link_index_association_payload(st, resolve_targets=True)

            self.assertEqual(payload["summary"]["total_unmapped"], 1)
            self.assertEqual(payload["summary"]["exact"], 1)
            self.assertEqual(payload["summary"]["exact_auto"], 1)
            auto = payload["auto_apply_items"][0]
            self.assertEqual(auto["path"], "Absolute Duo")
            self.assertEqual(auto["press_path"], "Absolute Duo_BDRip")
            self.assertEqual(auto["target_path"], str(target))
            row = payload["rows"][0]
            self.assertEqual(row["work_name_hint"], "Absolute Duo")
            self.assertEqual(row["auto_candidate"]["name"], "Absolute Duo")
            self.assertEqual(row["auto_candidate"]["suggested_target_path"], str(target))

    def test_association_payload_returns_all_rows_without_display_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            st = _settings(db)
            rows = [
                {
                    "shortcut_relpath": f"[2099]/Work {i}/BDRip.lnk",
                    "target_path": "",
                    "target_exists": False,
                    "exact_count": 0,
                    "candidates": [],
                }
                for i in range(305)
            ]

            with (
                patch("collection_detail.link_index._load_catalog_works", return_value=[]),
                patch("collection_detail.link_index._unmapped_disk_leaves_for_works", return_value=[]),
                patch("collection_detail.link_index._association_rows_for_disk_leaves", return_value=rows),
                patch("collection_detail.link_index.collection_link_index_config_json", return_value={}),
            ):
                payload = link_index_association_payload(st)

            self.assertEqual(payload["summary"]["total_unmapped"], 305)
            self.assertEqual(len(payload["rows"]), 305)

    def test_association_requires_matching_press_format(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Rakugo" / "Rakugo_DVDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101] Rakugo" / "DVDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Rakugo", [("R2Jraw", "----")]), encoding="utf-8")
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
                payload = link_index_association_payload(st)

            self.assertEqual(payload["summary"]["exact"], 0)
            self.assertEqual(payload["summary"]["exact_auto"], 0)
            row = payload["rows"][0]
            self.assertIsNone(row["auto_candidate"])
            self.assertEqual(len(row["candidates"]), 1)
            self.assertEqual(row["candidates"][0]["match_type"], "work_exact")
            self.assertEqual(row["candidates"][0]["press_format"], "R2Jraw")

    def test_association_matches_single_press_format_without_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Air Gear" / "Air Gear_DVDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101] Air Gear" / "DVDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("BDRip", "VCB"), ("DVDRip", "CK")]), encoding="utf-8")
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
                payload = link_index_association_payload(st)
                tree_payload = collection_link_index_payload(st)

            self.assertEqual(payload["summary"]["exact_auto"], 1)
            auto = payload["auto_apply_items"][0]
            self.assertEqual(auto["path"], "Air Gear")
            self.assertEqual(auto["press_path"], "Air Gear_DVDRip")
            self.assertEqual(auto["target_path"], str(target))
            row = payload["rows"][0]
            self.assertEqual(row["auto_candidate"]["press_label"], "DVDRip-CK")
            tree_link = _find_link_by_relpath(tree_payload["tree"], "[2099]/[20990101] Air Gear/DVDRip.lnk")
            self.assertIsNotNone(tree_link)
            self.assertTrue(tree_link["db_name_matched"])
            self.assertEqual(tree_link["db_match_press"], "DVDRip-CK")

    def test_rejected_association_candidate_is_hidden_until_pair_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Air Gear" / "Air Gear_DVDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101] Air Gear" / "DVDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("DVDRip", "CK")]), encoding="utf-8")
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

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch("collection_detail.link_index._windows_shortcut_targets", return_value=target_infos),
                patch("collection_detail.link_index.feature_data_root", return_value=root / "data"),
            ):
                payload = link_index_association_payload(st)
                row = payload["rows"][0]
                cand = row["candidates"][0]
                self.assertTrue(cand["reject_key"])
                self.assertEqual(cand["press_format"], "DVDRip")
                self.assertEqual(cand["press_group"], "CK")

                rejected = reject_link_index_association_from_ui_body(
                    {
                        "reject_key": cand["reject_key"],
                        "shortcut_relpath": row["shortcut_relpath"],
                        "target_path": cand["suggested_target_path"],
                        "yaml_source_rel": cand["yaml_source_rel"],
                        "index_in_file": cand["index_in_file"],
                        "press_key": cand["press_key"],
                    },
                    settings=st,
                )

                filtered_row = rejected["association"]["rows"][0]
                self.assertEqual(filtered_row["candidates"], [])
                self.assertIsNone(filtered_row["auto_candidate"])

                fp.write_text(_catalog_yaml_with_press("Air Gear", [("DVDRip", "NEO")]), encoding="utf-8")
                changed = link_index_association_payload(st)
                changed_cand = changed["rows"][0]["candidates"][0]
                self.assertEqual(changed_cand["press_group"], "NEO")
                self.assertNotEqual(changed_cand["reject_key"], cand["reject_key"])

    def test_auto_association_requires_target_leaf_name_to_match_shortcut_work_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Air Gear+!" / "Air Gear+!_DVDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101] Air Gear" / "DVDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("DVDRip", "CK")]), encoding="utf-8")
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
                payload = link_index_association_payload(st)

            self.assertEqual(payload["summary"]["exact"], 0)
            self.assertEqual(payload["summary"]["exact_auto"], 0)
            row = payload["rows"][0]
            self.assertIsNone(row["auto_candidate"])
            self.assertEqual(row["name_hints"], ["Air Gear+!", "Air Gear"])
            self.assertEqual(row["candidates"], [])

    def test_auto_association_prefers_target_leaf_work_name_over_series_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Air Gear" / "Air Gear+!_DVDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101] Air Gear" / "DVDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(
                _catalog_yaml_with_press("Air Gear", [("DVDRip", "CK")])
                + _catalog_yaml_with_press("Air Gear+!", [("DVDRip", "CK")]),
                encoding="utf-8",
            )
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
                payload = link_index_association_payload(st)

            row = payload["rows"][0]
            self.assertEqual(row["work_name_hint"], "Air Gear+!")
            self.assertEqual(row["auto_candidate"]["name"], "Air Gear+!")
            self.assertEqual(row["auto_candidate"]["suggested_path"], "Air Gear")
            self.assertEqual(row["auto_candidate"]["suggested_press_path"], "Air Gear+!_DVDRip")
            self.assertTrue(row["auto_candidate"]["target_name_check"]["work_name_matches_target"])
            self.assertEqual(row["candidates"][0]["name"], "Air Gear+!")

    def test_association_filters_candidates_when_shortcut_date_years_differ(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Lupin" / "Lupin_BDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[198X]" / "[19890401][19890401] Lupin" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2007].yaml"
            fp.write_text(
                _catalog_yaml_with_press_dates("Lupin", [("BDRip", "JSUM")], "20070727", "20070727"),
                encoding="utf-8",
            )
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
                payload = link_index_association_payload(st)

            row = payload["rows"][0]
            self.assertIsNone(row["auto_candidate"])
            self.assertEqual(row["candidates"], [])

    def test_association_filters_candidates_without_matching_press(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Air Gear" / "Air Gear_BDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "[20990101][20990331] Air Gear" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("720p", "DMG")]), encoding="utf-8")
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
                payload = link_index_association_payload(st)

            row = payload["rows"][0]
            self.assertIsNone(row["auto_candidate"])
            self.assertEqual(len(row["candidates"]), 1)
            self.assertEqual(row["candidates"][0]["match_type"], "work_exact")
            self.assertEqual(row["candidates"][0]["press_format"], "720p")

    def test_association_exposes_same_work_links_and_all_db_press_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target_a = media / "Air Gear" / "Air Gear_X264"
            target_b = media / "Air Gear" / "Air Gear_BDRip"
            target_a.mkdir(parents=True)
            target_b.mkdir(parents=True)
            shortcut_dir = finish / "[2099]" / "[20990101][20990331] Air Gear"
            shortcut_a = shortcut_dir / "X264.lnk"
            shortcut_b = shortcut_dir / "BDRip.lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_a.write_text("", encoding="utf-8")
            shortcut_b.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("DVDRip", "----"), ("BDRip", "VCB")]), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }
            target_infos = {
                str(shortcut_a.resolve()): {
                    "target_path": str(target_a),
                    "target_resolved": True,
                    "error": "",
                },
                str(shortcut_b.resolve()): {
                    "target_path": str(target_b),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = link_index_association_payload(st)

            row = payload["rows"][0]
            self.assertEqual(row["link_option_count"], 2)
            self.assertEqual(
                {item["link_name"] for item in row["link_options"]},
                {"X264", "BDRip"},
            )
            self.assertEqual(
                {(item["press_format"], item["press_group"]) for item in row["candidates"]},
                {("DVDRip", "----"), ("BDRip", "VCB")},
            )
            self.assertTrue(all(item["match_type"] == "work_exact" for item in row["candidates"]))

    def test_association_defaults_group_match_then_remaining_single_pair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            work_name = "Honey And Clove 2"
            target_plain = media / work_name / f"{work_name}_BDRip"
            target_jsum = media / work_name / f"{work_name}_BDRip(Jsum)"
            target_plain.mkdir(parents=True)
            target_jsum.mkdir(parents=True)
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_jsum = shortcut_dir / "BDRip(Jsum).lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("", encoding="utf-8")
            shortcut_jsum.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press(work_name, [("BDRip", "JSUM"), ("BDRip", "MZ")]), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(target_plain),
                    "target_resolved": True,
                    "error": "",
                },
                str(shortcut_jsum.resolve()): {
                    "target_path": str(target_jsum),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = link_index_association_payload(st)

            rows = {row["link_name"]: row for row in payload["rows"]}
            self.assertEqual(rows["BDRip(Jsum)"]["default_candidate"]["press_group"], "JSUM")
            self.assertEqual(rows["BDRip(Jsum)"]["default_match_reason"], "press_exact")
            self.assertEqual(rows["BDRip(Jsum)"]["auto_candidate"]["press_group"], "JSUM")
            self.assertTrue(rows["BDRip(Jsum)"]["can_auto_apply"])
            self.assertEqual(rows["BDRip"]["default_candidate"]["press_group"], "MZ")
            self.assertEqual(rows["BDRip"]["default_match_reason"], "remaining_single")
            self.assertEqual(rows["BDRip"]["auto_candidate"]["press_group"], "MZ")
            self.assertTrue(rows["BDRip"]["can_auto_apply"])
            self.assertEqual(payload["summary"]["exact_auto"], 2)

    def test_association_uses_mapped_sibling_link_to_promote_remaining_single(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            work_name = "Honey And Clove 2"
            target_plain = media / work_name / f"{work_name}_BDRip"
            target_jsum = media / work_name / f"{work_name}_BDRip(Jsum)"
            target_plain.mkdir(parents=True)
            target_jsum.mkdir(parents=True)
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_jsum = shortcut_dir / "BDRip(Jsum).lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("", encoding="utf-8")
            shortcut_jsum.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(
                _catalog_yaml_with_press_paths(
                    work_name,
                    [
                        ("BDRip", "JSUM", f"{work_name}_BDRip(Jsum)"),
                        ("BDRip", "MZ", ""),
                    ],
                    work_path=work_name,
                ),
                encoding="utf-8",
            )
            st = _settings(db, fp)

            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(target_plain),
                    "target_resolved": True,
                    "error": "",
                },
                str(shortcut_jsum.resolve()): {
                    "target_path": str(target_jsum),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = link_index_association_payload(st)

            rows = {row["link_name"]: row for row in payload["rows"]}
            self.assertNotIn("BDRip(Jsum)", rows)
            self.assertEqual(rows["BDRip"]["default_candidate"]["press_group"], "MZ")
            self.assertEqual(rows["BDRip"]["default_match_reason"], "remaining_single")
            self.assertEqual(rows["BDRip"]["auto_candidate"]["press_group"], "MZ")
            self.assertTrue(rows["BDRip"]["can_auto_apply"])
            self.assertEqual(payload["summary"]["exact_auto"], 1)

    def test_association_excludes_db_press_that_already_has_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            work_name = "Honey And Clove 2"
            target_plain = media / work_name / f"{work_name}_BDRip"
            target_jsum = media / work_name / f"{work_name}_BDRip(Jsum)"
            target_plain.mkdir(parents=True)
            target_jsum.mkdir(parents=True)
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_jsum = shortcut_dir / "BDRip(Jsum).lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("", encoding="utf-8")
            shortcut_jsum.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(
                _catalog_yaml_with_press_paths(
                    work_name,
                    [
                        ("BDRip", "JSUM", "already/linked"),
                        ("BDRip", "MZ", ""),
                    ],
                ),
                encoding="utf-8",
            )
            st = _settings(db, fp)

            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(target_plain),
                    "target_resolved": True,
                    "error": "",
                },
                str(shortcut_jsum.resolve()): {
                    "target_path": str(target_jsum),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = link_index_association_payload(st)

            for row in payload["rows"]:
                self.assertEqual(
                    {(item["press_format"], item["press_group"]) for item in row["candidates"]},
                    {("BDRip", "MZ")},
                )

    def test_apply_association_writes_mapping_to_catalog_yaml(self) -> None:
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
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                base = collection_link_index_payload(st)
                work = base["works"][0]
                result = apply_link_index_associations_from_ui_body(
                    {
                        "items": [
                            {
                                "yaml_source_rel": work["yaml_source_rel"],
                                "index_in_file": work["index_in_file"],
                                "press_key": work["press"][0]["press_key"],
                                "path": "Absolute Duo",
                                "press_path": "BDRip VCB",
                            }
                        ]
                    },
                    settings=st,
                )

            saved_doc = load_yaml(fp)
            coll = saved_doc[0]["attributes"][1]["data"]
            self.assertEqual(coll["path"], "Absolute Duo")
            self.assertEqual(coll["collectioned"][0]["press_path"], "BDRip VCB")
            self.assertEqual(result["mapping_summary"]["mapped_press"], 1)

    def test_apply_association_returns_warning_when_post_write_match_still_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            st = _settings(db)
            shortcut_rel = "[2015]/[20150701][20150916] おくさまが生徒会長!/bdrip.lnk"

            with (
                patch("collection_detail.link_index._save_ui_mappings_to_catalog", return_value=[{"changes": 1}]),
                patch("collection_detail.link_index.collection_link_index_payload", return_value={"ok": True}),
                patch(
                    "collection_detail.link_index.link_index_association_payload",
                    return_value={"rows": [{"shortcut_relpath": shortcut_rel}]},
                ),
            ):
                result = apply_link_index_associations_from_ui_body(
                    {
                        "items": [
                            {
                                "shortcut_relpath": shortcut_rel,
                                "yaml_source_rel": "[JP][TVInfo][2015].yaml",
                                "index_in_file": 0,
                                "press_key": "main:0:BDRip:",
                                "path": "おくさまが生徒会長!",
                                "press_path": "BDRip",
                            }
                        ]
                    },
                    settings=st,
                )

            self.assertEqual(result["association_unresolved"], [shortcut_rel.lower()])
            self.assertIn("仍未消失", result["write_warning"])

    def test_apply_association_accepts_absolute_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            target = root / "media" / "Absolute Duo" / "BDRip VCB"
            target.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                base = collection_link_index_payload(st)
                work = base["works"][0]
                result = apply_link_index_associations_from_ui_body(
                    {
                        "items": [
                            {
                                "yaml_source_rel": work["yaml_source_rel"],
                                "index_in_file": work["index_in_file"],
                                "press_key": work["press"][0]["press_key"],
                                "target_path": str(target),
                            }
                        ]
                    },
                    settings=st,
                )

            saved_doc = load_yaml(fp)
            coll = saved_doc[0]["attributes"][1]["data"]
            self.assertEqual(Path(coll["path"]), target.parent.resolve())
            self.assertEqual(coll["collectioned"][0]["press_path"], target.name)
            self.assertEqual(result["mapping_summary"]["mapped_press"], 1)

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
