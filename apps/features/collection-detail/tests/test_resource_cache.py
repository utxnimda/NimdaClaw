from __future__ import annotations

import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from collection_detail import link_index, resource_cache
from collection_detail.resource_tree import resource_node_cache_payload, resource_node_summary, resource_search_tree_from_entries
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml


class ResourceCacheTest(unittest.TestCase):
    def test_summary_preserves_lazy_counts_and_is_idempotent(self) -> None:
        node = {
            "relpath": "root:0/Series", "children_loaded": False,
            "children": [], "files": [], "has_children": True,
            "child_count": 3, "direct_file_count": 2,
            "direct_child_count": 5, "total_child_count": 20, "size": 1024,
        }
        summary = resource_node_summary(node)
        self.assertTrue(summary["has_children"])
        self.assertEqual(summary["child_count"], 3)
        self.assertEqual(summary["direct_file_count"], 2)
        self.assertEqual(resource_node_summary(summary), summary)
        cached = resource_node_cache_payload(node)["node"]
        self.assertFalse(cached["children_loaded"])
        self.assertEqual(cached["direct_child_count"], 5)

    def test_file_only_directory_remains_expandable_in_summary_and_cache(self) -> None:
        node = {"relpath": "root:0/Series/Disc", "children": [], "files": [{"name": "ep01.mkv"}], "children_loaded": True}
        summary = resource_node_summary(node)
        self.assertTrue(summary["has_children"])
        self.assertEqual(summary["direct_file_count"], 1)
        cached = resource_node_cache_payload({"children": [node], "files": []})["node"]
        self.assertTrue(cached["children"][0]["has_children"])

    def test_legacy_file_only_summary_is_repaired_without_rescanning(self) -> None:
        first = link_index.scan_resource_libraries_payload()
        node_path = resource_cache.node_cache_path(Path(first["node_cache_dir"]).parent, "root:0/Series", first["generation"])
        cached = load_yaml(node_path)
        cached["node"]["children"][0]["has_children"] = False
        node_path.write_text(dump_yaml_string(cached), encoding="utf-8")
        before = node_path.read_bytes()
        with patch.object(link_index, "_resource_live_node_from_relpath", side_effect=AssertionError("unexpected disk scan")):
            node = link_index.resource_libraries_node_payload("root:0/Series")
        self.assertTrue(node["cached"])
        self.assertTrue(node["node"]["children"][0]["has_children"])
        self.assertEqual(node_path.read_bytes(), before)

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.media = self.base / "media"
        (self.media / "Series" / "Work_BDRip").mkdir(parents=True)
        (self.media / "Series" / "Work_BDRip" / "ep01.mkv").write_bytes(b"video")
        self.config = {"paths": {"resource_roots": [str(self.media)], "shortcut_root": str(self.base / "shortcuts")}}
        self.stack.enter_context(patch.object(link_index, "_feature_config", return_value=self.config))
        self.stack.enter_context(patch.object(link_index, "feature_data_root", return_value=self.base / "data"))

    def test_failed_node_or_manifest_write_preserves_previous_snapshot(self) -> None:
        first = link_index.scan_resource_libraries_payload()
        manifest = Path(first["cache_path"])
        previous = manifest.read_bytes()
        node_paths = sorted(Path(first["node_cache_dir"]).rglob("*.yaml"))
        old_nodes = {path: path.read_bytes() for path in node_paths}
        original_write = resource_cache.atomic_write_bytes
        for fail_manifest in (False, True):
            with self.subTest(fail_manifest=fail_manifest):
                node_count = 0

                def fail_write(path: Path, content: bytes) -> None:
                    nonlocal node_count
                    if path != manifest:
                        node_count += 1
                    if (fail_manifest and path == manifest) or (not fail_manifest and node_count == 2):
                        raise OSError("simulated full disk")
                    original_write(path, content)

                with patch.object(resource_cache, "atomic_write_bytes", side_effect=fail_write):
                    with self.assertRaisesRegex(OSError, "simulated full disk"):
                        link_index.scan_resource_libraries_payload()
                self.assertEqual(manifest.read_bytes(), previous)
                self.assertTrue(all(path.read_bytes() == value for path, value in old_nodes.items()))
                self.assertEqual(link_index.resource_libraries_cached_payload()["generation"], first["generation"])
                node = link_index.resource_libraries_node_payload("root:0")["node"]
                self.assertEqual(node["children"][0]["name"], "Series")

    def test_interruption_after_manifest_replace_does_not_delete_published_nodes(self) -> None:
        first = link_index.scan_resource_libraries_payload()
        original_write = resource_cache.atomic_write_bytes

        def interrupt_after_replace(path: Path, content: bytes) -> None:
            original_write(path, content)
            if path == Path(first["cache_path"]):
                raise KeyboardInterrupt("after publish")

        with patch.object(resource_cache, "atomic_write_bytes", side_effect=interrupt_after_replace):
            with self.assertRaises(KeyboardInterrupt):
                link_index.scan_resource_libraries_payload()
        cached = link_index.resource_libraries_cached_payload()
        self.assertTrue(cached["cached"])
        self.assertNotEqual(cached["generation"], first["generation"])
        self.assertEqual(link_index.resource_libraries_node_payload("root:0")["node"]["children"][0]["name"], "Series")

    def test_root_reordering_invalidates_manifest_and_nodes(self) -> None:
        second_root = self.base / "second"
        (second_root / "Other").mkdir(parents=True)
        self.config["paths"]["resource_roots"].append(str(second_root))
        link_index.scan_resource_libraries_payload()
        self.config["paths"]["resource_roots"].reverse()
        cache = link_index.resource_libraries_cached_payload()
        self.assertFalse(cache["cached"])
        self.assertEqual(cache["items"], [])
        current_node = link_index.resource_libraries_node_payload("root:0")
        self.assertFalse(current_node["cached"])
        self.assertEqual(current_node["node"]["path"], str(second_root))
        self.assertEqual(current_node["node"]["children"][0]["name"], "Other")

    def test_changed_excludes_reject_stale_node_even_when_requested_directly(self) -> None:
        link_index.scan_resource_libraries_payload()
        link_index.resource_libraries_node_payload("root:0/Series/Work_BDRip")
        self.config["paths"]["resource_excludes"] = {str(self.media): ["Series"]}
        self.assertFalse(link_index.resource_libraries_cached_payload()["cached"])
        with self.assertRaises(FileNotFoundError):
            link_index.resource_libraries_node_payload("root:0/Series/Work_BDRip")

    def test_config_change_during_scan_preserves_previous_snapshot(self) -> None:
        first = link_index.scan_resource_libraries_payload()
        previous = Path(first["cache_path"]).read_bytes()
        original_scan = link_index._resource_dir_node

        def change_config_after_scan(*args, **kwargs):
            node = original_scan(*args, **kwargs)
            self.config["paths"]["resource_excludes"] = {str(self.media): ["Series"]}
            return node

        with patch.object(link_index, "_resource_dir_node", side_effect=change_config_after_scan):
            with self.assertRaisesRegex(ValueError, "扫描期间资源库配置已改变"):
                link_index.scan_resource_libraries_payload()
        self.assertEqual(Path(first["cache_path"]).read_bytes(), previous)
        self.assertTrue(Path(first["node_cache_dir"]).is_dir())

    def test_direct_alias_requests_validate_lexical_path_before_resolving(self) -> None:
        alias = self.media / "Alias"
        target = self.media / "Series"
        alias.mkdir()
        original_resolve = Path.resolve

        def resolve_alias(path: Path, *args, **kwargs) -> Path:
            try:
                rest = path.relative_to(alias)
            except ValueError:
                return original_resolve(path, *args, **kwargs)
            return original_resolve(target / rest, *args, **kwargs)

        for reason in ("symlink", "junction", "excluded"):
            with self.subTest(reason=reason), ExitStack() as patches:
                self.config["paths"]["resource_excludes"] = {str(self.media): ["Alias"]} if reason == "excluded" else {}
                patches.enter_context(patch.object(Path, "resolve", new=resolve_alias))
                patches.enter_context(patch.object(Path, "is_symlink", new=lambda path: reason == "symlink" and path == alias))
                patches.enter_context(patch.object(Path, "is_junction", new=lambda path: reason == "junction" and path == alias, create=True))
                for relpath in ("root:0/Alias", "root:0/Alias/Work_BDRip"):
                    self.assertIsNone(link_index._resource_path_for_relpath(relpath))
                    with self.assertRaises(FileNotFoundError):
                        link_index.resource_libraries_node_payload(relpath)

    def test_previously_cached_directory_cannot_be_reopened_after_becoming_link(self) -> None:
        link_index.scan_resource_libraries_payload()
        link_index.resource_libraries_node_payload("root:0/Series")
        with patch.object(Path, "is_symlink", new=lambda path: path == self.media / "Series"):
            with self.assertRaises(FileNotFoundError):
                link_index.resource_libraries_node_payload("root:0/Series")

    def test_parent_path_components_cannot_bypass_excludes(self) -> None:
        self.config["paths"]["resource_excludes"] = {str(self.media): ["Excluded"]}
        self.assertIsNone(link_index._resource_path_for_relpath("root:0/Excluded/../Series"))

    def test_successful_rescan_prunes_only_previous_generation(self) -> None:
        first = link_index.scan_resource_libraries_payload()
        node_root = Path(first["node_cache_dir"]).parent
        unrelated = node_root / "notes"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
        another_scan = node_root / ("a" * 32)
        another_scan.mkdir()
        second = link_index.scan_resource_libraries_payload()
        self.assertFalse(Path(first["node_cache_dir"]).exists())
        self.assertTrue(Path(second["node_cache_dir"]).is_dir())
        self.assertEqual((unrelated / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertTrue(another_scan.is_dir())

    def test_corrupt_manifest_can_be_rebuilt(self) -> None:
        first = link_index.scan_resource_libraries_payload()
        Path(first["cache_path"]).write_text("broken: [unclosed", encoding="utf-8")
        self.assertFalse(link_index.resource_libraries_cached_payload()["cached"])
        second = link_index.scan_resource_libraries_payload()
        self.assertTrue(second["cached"])
        self.assertEqual(link_index.resource_libraries_node_payload("root:0")["node"]["children"][0]["name"], "Series")

    def test_scan_does_not_follow_directory_links_for_deep_metrics(self) -> None:
        linked = self.media / "Series" / "Work_BDRip" / "Linked"
        linked.mkdir()
        (linked / "outside.mkv").write_bytes(b"outside resource")
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == linked):
            payload = link_index.scan_resource_libraries_payload()
        self.assertEqual(payload["summary"]["size"], 5)
        self.assertEqual(payload["summary"]["file_count"], 1)

    def test_warm_node_read_builds_current_config_once(self) -> None:
        link_index.scan_resource_libraries_payload()
        with patch.object(link_index, "collection_link_index_config_json", wraps=link_index.collection_link_index_config_json) as projections:
            node = link_index.resource_libraries_node_payload("root:0")
        self.assertTrue(node["cached"])
        self.assertEqual(projections.call_count, 1)

    def test_scan_reuses_enumerated_file_metadata_and_refreshes_on_next_scan(self) -> None:
        video = self.media / "Series" / "Work_BDRip" / "ep01.mkv"
        original_stat = Path.stat
        file_stat_paths = []

        def count_file_stats(path: Path, *args, **kwargs):
            if path == video:
                file_stat_paths.append(path)
            return original_stat(path, *args, **kwargs)

        for max_depth in (0, 12):
            with self.subTest(max_depth=max_depth), patch.object(Path, "stat", new=count_file_stats):
                first = link_index._resource_dir_node(self.media, self.media, "root:0", [], "", {"dirs": 0, "files": 0, "truncated": False}, 50000, max_depth=max_depth)
                self.assertEqual(first["size"], len(video.read_bytes()))
                self.assertEqual(first["file_count"], 1)
        self.assertEqual(file_stat_paths, [])
        video.write_bytes(b"changed-size")
        refreshed = link_index._resource_dir_node(self.media, self.media, "root:0", [], "", {"dirs": 0, "files": 0, "truncated": False}, 50000, max_depth=0)
        self.assertEqual(refreshed["size"], 12)

    def test_pure_search_preserves_parent_tree_without_mutating_cache(self) -> None:
        entries = [
            {"type": "folder", "relpath": "", "name": "Library"},
            {"type": "folder", "relpath": "root:0", "name": "Disk"},
            {"type": "folder", "relpath": "root:0/Series", "name": "Series", "parent_relpath": "root:0"},
            {"type": "file", "relpath": "root:0/Series/episode.MKV", "name": "episode.MKV", "size": 12},
        ]
        previous = copy.deepcopy(entries)
        tree, counts = resource_search_tree_from_entries(entries, "EPISODE")
        self.assertEqual(counts["matched_file_count"], 1)
        self.assertEqual(counts["matched_folder_count"], 0)
        self.assertEqual(tree["size"], 12)
        self.assertEqual(tree["children"][0]["children"][0]["files"][0]["name"], "episode.MKV")
        self.assertEqual(entries, previous)


if __name__ == "__main__":
    unittest.main()
