from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collection_detail import link_index
from collection_detail.catalog_repository import CatalogRepository
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string


def record(name: str, target: Path | None, *, country: str = "japan") -> dict:
    return {"attributes": [
        {"type": "date", "data": {"start": "20990101", "end": "20990331"}},
        {"type": "collection-type", "data": {
            "domain": "animation", "release_type": "tv",
            "path": str(target.parent).replace("\\", "/") if target is not None else "",
            "collectioned": [{"press_format": "BDRip", "press_group": "",
                              "press_path": target.name if target is not None else ""}],
            "markers": [],
        }},
        {"type": "country", "data": country},
        {"type": "name", "data": name},
    ]}


def link_nodes(tree: dict) -> list[dict]:
    result = [tree] if tree.get("type") == "link" else []
    for child in tree.get("children", []):
        result.extend(link_nodes(child))
    return result


def path_key(raw: str | Path) -> str:
    return str(Path(raw).absolute()).casefold()


class IndexDisplayProjectionTest(unittest.TestCase):
    """The index browser joins saved records and observations, never writes them."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / "db"
        self.media = self.base / "media"
        self.finish = self.base / "finish"
        self.korea_finish = self.base / "korea-finish"
        for directory in (self.db, self.media, self.finish, self.korea_finish):
            directory.mkdir()
        self.source = self.db / "catalog.yaml"
        self.index = self.base / "derived" / "db" / "index" / "link-index.yaml"
        self.settings = JpTvBrowseSettings(
            version=1, filesystem_root=self.db,
            resolved_default_readable=str(self.source),
            resolved_catalog_yaml_paths=(str(self.source),),
            enum_options={}, enum_labels={}, enum_section_labels={}, app_features=(),
        )
        self.config = {
            "paths": {
                "media_root": str(self.media), "resource_roots": [str(self.media)],
                "shortcut_root": str(self.finish),
                "shortcut_roots": [{"root": str(self.korea_finish), "match": {"country": "korea"}}],
            },
            "link_index": {"layout_levels": ["{name}"], "shortcut_name": "{press_format}"},
        }
        self.target_infos: dict[str, dict] = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(link_index, "_feature_config", return_value=self.config))
        stack.enter_context(patch.object(link_index, "feature_data_root", return_value=self.base / "derived"))
        stack.enter_context(patch.object(link_index, "_link_index_db_path", return_value=self.index))
        self.reader = stack.enter_context(patch.object(
            link_index, "_windows_shortcut_targets", side_effect=self.read_shortcut_targets,
        ))
        cache = link_index._SHORTCUT_SCAN_CACHE
        previous = dict(cache)
        self.addCleanup(lambda: (cache.clear(), cache.update(previous)))
        cache["signature"] = None
        cache["leaves"] = []

    def read_shortcut_targets(self, paths: list[Path]) -> dict:
        return {str(path): self.target_infos[str(path)] for path in paths if str(path) in self.target_infos}

    def save_records(self, records: list[dict]) -> None:
        self.source.write_text(dump_yaml_string(records), encoding="utf-8")

    def target(self, name: str) -> Path:
        target = self.media / name / "Release_BDRip"
        target.mkdir(parents=True)
        return target

    def shortcut(self, name: str, target: Path, *, root: Path | None = None) -> Path:
        shortcut = (root or self.finish) / name / "BDRip.lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"mock shortcut; COM reading is intercepted")
        self.target_infos[str(shortcut.absolute())] = {
            "target_path": str(target), "target_resolved": True, "error": "",
        }
        return shortcut

    def payload(self, *, refresh: bool = False) -> dict:
        return link_index.collection_link_index_payload(self.settings, lite=True, refresh_links=refresh)

    def database_node(self, payload: dict, shortcut: Path) -> dict:
        matches = [node for node in link_nodes(payload["tree"])
                   if node.get("db_associated") and path_key(node["shortcut_path"]) == path_key(shortcut)]
        self.assertEqual(len(matches), 1, str(shortcut))
        return matches[0]

    def test_fresh_saved_catalog_add_delete_and_rename_override_old_derived_index(self) -> None:
        original, removed = self.target("Original"), self.target("Removed")
        self.save_records([record("Original", original), record("Removed", removed)])
        old_works = CatalogRepository(self.settings).load_works()
        old_items = link_index._index_entries_from_works(old_works, prefer_catalog_target=True)
        self.index.parent.mkdir(parents=True)
        self.index.write_text(dump_yaml_string(link_index._index_payload(
            old_items, catalog_root=link_index._settings_catalog_root_key(self.settings),
        )), encoding="utf-8")
        self.assertEqual(len(link_nodes(self.payload()["tree"])), 2)

        added = self.target("Added")
        self.save_records([record("Renamed", original), record("Added", added)])
        source_before, index_before = self.source.read_bytes(), self.index.read_bytes()
        payload = self.payload()
        names = {node["relpath"].split("/")[0] for node in link_nodes(payload["tree"]) if node.get("db_associated")}
        self.assertEqual(names, {"Renamed", "Added"})
        self.assertEqual(payload["mapping_summary"]["total_press"], 2)
        self.assertEqual((self.source.read_bytes(), self.index.read_bytes()), (source_before, index_before))

    def test_exact_shortcut_join_keeps_database_only_and_physical_only_nodes(self) -> None:
        present, database_only, orphan_target = self.target("Present"), self.target("DatabaseOnly"), self.target("Orphan")
        self.save_records([record("Present", present), record("DatabaseOnly", database_only), record("Unbound", None)])
        present_shortcut = self.shortcut("Present", present)
        orphan_shortcut = self.shortcut("Loose", orphan_target)
        payload = self.payload()
        present_node = self.database_node(payload, present_shortcut)
        self.assertTrue(present_node["shortcut_exists"])
        self.assertTrue(present_node["db_linked"])
        missing_node = self.database_node(payload, self.finish / "DatabaseOnly" / "BDRip.lnk")
        self.assertTrue(missing_node["target_exists"])
        self.assertFalse(missing_node["shortcut_exists"])
        self.assertFalse(missing_node["db_linked"])
        unbound_node = self.database_node(payload, self.finish / "Unbound" / "BDRip.lnk")
        self.assertFalse(unbound_node["target_path"])
        physical = [node for node in link_nodes(payload["tree"]) if not node.get("db_associated")]
        self.assertEqual([path_key(node["shortcut_path"]) for node in physical], [path_key(orphan_shortcut)])
        self.assertEqual(payload["disk_summary"]["shortcut_leaves"], 2)
        self.assertEqual(payload["disk_summary"]["unmapped_on_disk"], 1)
        self.assertEqual(payload["unmapped_shortcuts"], [{
            "shortcut_path": str(orphan_shortcut), "shortcut_root": str(self.finish),
            "shortcut_relpath": "Loose/BDRip.lnk", "shortcut_exists": True,
            "target_path": str(orphan_target), "target_exists": True,
            "target_resolved": True, "target_error": "",
        }])
        self.assertEqual(len(link_nodes(payload["tree"])), 4)

    def test_unmapped_details_keep_missing_and_unresolved_targets_separate(self) -> None:
        self.save_records([])
        missing = self.shortcut("Missing", self.media / "Missing")
        unresolved = self.shortcut("Unresolved", self.media / "Unknown")
        self.target_infos[str(unresolved.absolute())] = {
            "target_path": "", "target_resolved": False, "error": "COM target read failed",
        }
        payload = self.payload()
        details = {path_key(item["shortcut_path"]): item for item in payload["unmapped_shortcuts"]}
        self.assertTrue(details[path_key(missing)]["target_resolved"])
        self.assertFalse(details[path_key(missing)]["target_exists"])
        self.assertEqual(details[path_key(missing)]["target_path"], str(self.media / "Missing"))
        self.assertFalse(details[path_key(unresolved)]["target_resolved"])
        self.assertFalse(details[path_key(unresolved)]["target_exists"])
        self.assertEqual(details[path_key(unresolved)]["target_error"], "COM target read failed")

    def test_unmapped_detail_list_is_complete_in_lite_and_full_responses(self) -> None:
        leaves = [{
            "shortcut_path": str(self.finish / f"{index:04}.lnk"),
            "shortcut_root": str(self.finish), "shortcut_relpath": f"{index:04}.lnk",
            "shortcut_parts": [f"{index:04}.lnk"], "shortcut_exists": True,
            "target_path": "", "target_resolved": False, "target_exists": False,
        } for index in reversed(range(301))]
        with patch.object(link_index, "_scan_shortcut_leaves", return_value=leaves):
            for lite in (False, True):
                with self.subTest(lite=lite):
                    payload = link_index._payload_from_works([], lite=lite)
                    self.assertEqual(len(payload["unmapped_shortcuts"]), 301)
                    self.assertEqual(payload["plan_summary"]["unmapped_on_disk"], 301)
                    self.assertEqual(payload["unmapped_shortcuts"][0]["shortcut_relpath"], "0000.lnk")
                    self.assertEqual(payload["unmapped_shortcuts"][-1]["shortcut_relpath"], "0300.lnk")

    def test_wrong_actual_target_does_not_replace_authoritative_database_binding(self) -> None:
        expected, wrong = self.target("Expected"), self.target("Wrong")
        self.save_records([record("Expected", expected)])
        shortcut = self.shortcut("Expected", wrong)
        payload = self.payload()
        node = self.database_node(payload, shortcut)
        self.assertEqual(path_key(node["target_path"]), path_key(expected))
        self.assertEqual(path_key(node["shortcut_target_path"]), path_key(wrong))
        self.assertTrue(node["shortcut_exists"])
        self.assertTrue(node["shortcut_target_exists"])
        self.assertFalse(node["db_linked"])
        self.assertNotEqual(node["status"], "ready")
        issue = next(item for item in payload["issues"] if item["code"] == "target_mismatch")
        self.assertEqual(issue["shortcut_path"], str(shortcut))
        self.assertEqual(issue["target_path"], str(expected))
        self.assertEqual(issue["yaml_source_rel"], self.source.name)
        self.assertEqual(issue["index_in_file"], 0)
        self.assertIn(str(wrong), issue["reason"])

    def test_resource_scan_missing_root_has_exact_warning_path_and_reason(self) -> None:
        missing = self.base / "missing-media"
        self.config["paths"]["resource_roots"] = [str(missing)]
        captured = {}
        def save(payload):
            captured.update(payload)
        with patch.object(link_index, "_save_resource_scan_cache", side_effect=save), \
             patch.object(link_index, "_load_resource_scan_cache", side_effect=lambda: captured):
            payload = link_index.scan_resource_libraries_payload()
        self.assertEqual(payload["issues"][0]["source_path"], str(missing))
        self.assertEqual(payload["issues"][0]["reason"], "目录不存在")
        self.assertEqual(payload["summary"]["existing_root_count"], 0)

    def test_same_relative_path_in_two_roots_never_cross_associates(self) -> None:
        japan, korea = self.target("Japan"), self.target("Korea")
        self.save_records([record("Shared Title", japan), record("Shared Title", korea, country="korea")])
        japan_shortcut = self.shortcut("Shared Title", korea)
        payload = self.payload()
        japan_node = self.database_node(payload, japan_shortcut)
        korea_node = self.database_node(payload, self.korea_finish / "Shared Title" / "BDRip.lnk")
        self.assertTrue(japan_node["shortcut_exists"])
        self.assertFalse(japan_node["db_linked"])
        self.assertFalse(korea_node["shortcut_exists"])
        self.assertFalse(korea_node["db_linked"])
        self.assertEqual(path_key(japan_node["target_path"]), path_key(japan))
        self.assertEqual(path_key(korea_node["target_path"]), path_key(korea))

    def test_shared_target_does_not_hide_extra_alias_or_fake_second_season_link(self) -> None:
        shared = self.target("SharedRelease")
        self.save_records([record("Season 1", shared), record("Season 2", shared)])
        first = self.shortcut("Season 1", shared)
        alias = self.shortcut("Extra Alias", shared)
        payload = self.payload()
        self.assertTrue(self.database_node(payload, first)["db_linked"])
        second = self.database_node(payload, self.finish / "Season 2" / "BDRip.lnk")
        self.assertFalse(second["shortcut_exists"])
        self.assertFalse(second["db_linked"])
        physical = [node for node in link_nodes(payload["tree"]) if not node.get("db_associated")]
        self.assertEqual([path_key(node["shortcut_path"]) for node in physical], [path_key(alias)])
        self.assertEqual(len(link_nodes(payload["tree"])), 3)
        self.assertEqual(payload["mapping_summary"]["total_press"], 2)

    def test_zero_database_records_still_displays_real_shortcuts(self) -> None:
        self.save_records([])
        orphan = self.shortcut("Orphan", self.target("Orphan"))
        payload = self.payload()
        nodes = link_nodes(payload["tree"])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(path_key(nodes[0]["shortcut_path"]), path_key(orphan))
        self.assertFalse(nodes[0]["db_associated"])
        self.assertEqual(payload["mapping_summary"]["total_press"], 0)

    def test_observed_target_never_becomes_a_saved_binding_for_generation(self) -> None:
        target = self.target("ObservedOnly")
        self.save_records([record("ObservedOnly", None)])
        shortcut = self.shortcut("ObservedOnly", target)
        source_before = self.source.read_bytes()
        node = self.database_node(self.payload(), shortcut)
        self.assertTrue(node["shortcut_exists"])
        self.assertFalse(node["target_path"])
        self.assertEqual(node["status"], "missing_catalog_binding")
        self.assertNotIn("lnk 指向与 DB 目标路径不一致", node.get("warnings", []))
        self.assertEqual(path_key(node["shortcut_target_path"]), path_key(target))
        with patch.object(link_index, "_create_windows_shortcut", side_effect=AssertionError("not authorized to create")):
            generated = link_index.generate_link_index_from_ui_body({"preview": True}, settings=self.settings)
            preview = link_index.generate_link_index_files_from_ui_body({"preview": True}, settings=self.settings)
        self.assertEqual(len(generated["generated"]), 1)
        self.assertFalse(generated["generated"][0]["target_path"])
        issues = generated["catalog_bindings"]["issues"]
        self.assertTrue(any(issue["code"] == "missing-catalog-binding" and issue["blocking"] for issue in issues))
        self.assertGreater(preview["file_generation"]["blocked_count"], 0)
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertFalse(self.index.exists())

    def test_unbound_record_without_shortcut_is_not_a_missing_disk_target(self) -> None:
        self.save_records([record("Unbound", None)])
        payload = self.payload()
        node = self.database_node(payload, self.finish / "Unbound" / "BDRip.lnk")
        self.assertEqual(node["status"], "missing_catalog_binding")
        self.assertEqual(payload["plan_summary"]["missing_catalog_binding"], 1)
        self.assertEqual(payload["plan_summary"]["missing_target"], 0)

    def test_summary_counts_shortcut_observations_separately_from_db_status(self) -> None:
        summary = link_index._plan_summary([
            {"status": "ready", "target_path": "present", "shortcut_exists": True},
            {"status": "shortcut_exists", "target_path": "present", "shortcut_exists": True},
        ])
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["shortcut_exists"], 2)

    def test_normal_and_forced_display_reads_never_persist_database_or_caches(self) -> None:
        target = self.target("ReadOnly")
        self.save_records([record("ReadOnly", target)])
        self.shortcut("ReadOnly", target)
        before = {path.relative_to(self.base).as_posix(): path.read_bytes()
                  for path in self.base.rglob("*") if path.is_file()}
        with ExitStack() as stack:
            for name in ("_save_shortcut_scan_cache", "_save_link_index_db", "atomic_write_bytes",
                         "commit_file_writes", "_create_windows_shortcut", "write_node_cache", "publish_resource_snapshot"):
                stack.enter_context(patch.object(link_index, name, side_effect=AssertionError(f"display must not call {name}")))
            stack.enter_context(patch.object(Path, "write_bytes", side_effect=AssertionError("display must not write bytes")))
            stack.enter_context(patch.object(Path, "write_text", side_effect=AssertionError("display must not write text")))
            stack.enter_context(patch.object(Path, "mkdir", side_effect=AssertionError("display must not create directories")))
            for refresh in (False, True):
                with self.subTest(refresh=refresh):
                    payload = self.payload(refresh=refresh)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["disk_summary"]["shortcut_leaves"], 1)
        after = {path.relative_to(self.base).as_posix(): path.read_bytes()
                 for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
