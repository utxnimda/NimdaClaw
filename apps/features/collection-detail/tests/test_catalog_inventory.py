from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collection_detail.catalog_inventory import CatalogInventoryRepository
from collection_detail.library_status import library_status_payload
from collection_detail import link_index
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string


def settings(db: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1, filesystem_root=db, resolved_default_readable=None,
        resolved_catalog_yaml_paths=(), enum_options={}, enum_labels={},
        enum_section_labels={}, app_features=(),
    )


def work(name: str, path: Path | str, press_path: str = "Release_BDRip") -> dict:
    return {"attributes": [
        {"type": "date", "data": {"start": "20990101", "end": "20990331"}},
        {"type": "collection-type", "data": {
            "domain": "animation", "release_type": "tv", "path": str(path).replace("\\", "/"),
            "collectioned": [{"press_format": "BDRip", "press_group": "", "press_path": press_path}],
            "markers": [],
        }},
        {"type": "country", "data": "japan"},
        {"type": "name", "data": name},
    ]}


class InventoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / "db"
        self.db.mkdir()
        self.root = self.base / "media"
        self.root.mkdir()
        self.both = self.root / "Both"
        self.both.mkdir()
        (self.both / "Release_BDRip").mkdir()
        self.source = self.db / "catalog.yaml"

    def save_fixture(self, entries):
        self.source.write_text(dump_yaml_string(entries), encoding="utf-8")

    def repository(self, disk=None):
        return CatalogInventoryRepository(settings(self.db), resource_roots=[self.root], default_media_root=self.root, disk=disk)

    def test_both_db_only_unbound_and_shared_directory_counts_remain_separate(self):
        self.save_fixture([
            work("Season 1", self.both), work("Missing", self.root / "Missing"),
            work("Unbound", "", ""), work("Season 2", self.both),
        ])
        before = self.source.read_bytes()
        original_scandir = os.scandir

        def scandir(path):
            candidate = Path(path)
            if candidate == self.root or self.root in candidate.parents:
                raise AssertionError("must not scan media")
            return original_scandir(path)

        with patch("os.scandir", side_effect=scandir), patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")):
            snapshot = self.repository().snapshot()
        self.assertEqual(snapshot["database"], {"work_record_count": 4, "press_record_count": 4})
        counts = snapshot["relationships"]["counts"]
        self.assertEqual((counts["both"], counts["db-only"], counts["unbound"]), (2, 1, 1))
        works = snapshot["relationships"]["works"]
        self.assertEqual(works[0]["resolved_path"], works[3]["resolved_path"])
        self.assertNotEqual(works[0]["work_key"], works[3]["work_key"])
        self.assertEqual([row["press"][0]["state"] for row in works], ["both", "db-only", "unbound", "both"])
        self.assertEqual(self.source.read_bytes(), before)

    def test_offline_root_never_becomes_db_only(self):
        self.save_fixture([work("Present binding", self.both), work("Missing binding", self.root / "Missing"), work("Unbound", "", "")])
        original = Path.lstat
        for failure in (FileNotFoundError("drive unavailable"), PermissionError("access denied")):
            def lstat(path, *args, **kwargs):
                if path == self.root:
                    raise failure
                return original(path, *args, **kwargs)

            with self.subTest(failure=failure), patch.object(Path, "lstat", autospec=True, side_effect=lstat):
                snapshot = self.repository().snapshot()
            self.assertEqual(snapshot["relationships"]["counts"]["offline"], 2)
            self.assertEqual(snapshot["relationships"]["counts"]["db-only"], 0)
            self.assertEqual(snapshot["relationships"]["counts"]["unbound"], 1)
            self.assertFalse(snapshot["roots"][0]["online"])

    def test_invalid_and_unsafe_bindings_do_not_probe_outside_resources(self):
        unsafe = self.root / "Link"
        outside = self.base / "Outside"
        self.save_fixture([
            work("Bad press", self.both), work("Outside", outside),
            work("Linked", unsafe), work("Absolute press", self.both),
        ])
        repository = self.repository()
        saved = repository.catalog.load_works()
        # Canonical YAML validation already rejects these values. Recheck the
        # DTO boundary too so an adapter cannot smuggle unsafe paths through.
        saved[0]["press"][0]["press_path"] = "../escape"
        saved[3]["press"][0]["press_path"] = str(outside)
        original = Path.lstat
        touched = []

        def lstat(path, *args, **kwargs):
            touched.append(path)
            if path == unsafe:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if path == outside:
                raise AssertionError("outside binding must not be probed")
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", autospec=True, side_effect=lstat), patch.object(repository.catalog, "load_works", return_value=saved):
            snapshot = repository.snapshot()
        works = snapshot["relationships"]["works"]
        self.assertEqual(works[0]["press"][0]["state"], "invalid")
        self.assertEqual(works[1]["state"], "invalid")
        self.assertEqual(works[2]["state"], "unsafe")
        self.assertEqual(works[2]["press"][0]["state"], "unsafe")
        self.assertEqual(works[3]["press"][0]["state"], "invalid")
        self.assertNotIn(outside, touched)

    def test_reparse_resource_root_is_unsafe_without_touching_bound_children(self):
        self.save_fixture([work("Known", self.both)])
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == self.root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if path == self.both:
                raise AssertionError("reparse root children must not be followed")
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", autospec=True, side_effect=lstat):
            snapshot = self.repository().snapshot()
        self.assertEqual(snapshot["relationships"]["counts"]["unsafe"], 1)
        self.assertEqual(snapshot["relationships"]["counts"]["db-only"], 0)
        self.assertEqual(snapshot["roots"][0]["state"], "unsafe")

    def test_page_reads_saved_cache_only_and_never_creates_records_or_shortcuts(self):
        disk_only = self.root / "Disk only"
        disk_only.mkdir()
        same_name = self.root / "Known but unbound"
        same_name.mkdir()
        self.save_fixture([work("Known", self.both), work("Known but unbound", "", "")])
        before = self.source.read_bytes()
        cache = {"cached": True, "scanned_at": "saved-time", "summary": {"dir_count": 100, "file_count": 200, "item_count": 999}, "roots": [{
            "root": str(self.root), "series": [
                {"path": str(self.both)}, {"path": str(disk_only)}, {"path": str(same_name)},
                {"path": str(self.root / "Stale")}, {"path": str(self.base / "Outside")},
                {"path": str(self.both / "Release_BDRip")},
            ],
        }]}
        with ExitStack() as stack:
            roots_read = stack.enter_context(patch.object(link_index, "resource_roots", return_value=[self.root]))
            stack.enter_context(patch.object(link_index, "_legacy_media_root", return_value=self.root))
            cache_read = stack.enter_context(patch.object(link_index, "resource_libraries_cached_payload", return_value=cache))
            for name in ("scan_resource_libraries_payload", "_save_resource_scan_cache", "_create_windows_shortcut", "_save_link_index_db"):
                stack.enter_context(patch.object(link_index, name, side_effect=AssertionError("must not mutate or scan")))
            payload = library_status_payload(settings(self.db))
        cache_read.assert_called_once_with()
        roots_read.assert_called_once_with(resolve_links=False)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["database"]["work_record_count"], 2)
        self.assertEqual(payload["resource_tree"]["directory_count"], 100)
        self.assertEqual(payload["resource_tree"]["file_count"], 200)
        observed = payload["observed_unbound_directories"]
        self.assertEqual(len(observed), 3)
        self.assertEqual([entry["state"] for entry in observed], ["observed-unbound", "observed-unbound", "stale-observation"])
        self.assertTrue(all(entry["name_matching"] == "not_checked" for entry in observed))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.db.iterdir()), ["catalog.yaml"])

    def test_missing_scan_cache_is_not_interpreted_as_missing_media(self):
        self.save_fixture([work("Known", self.both)])
        with patch.object(link_index, "resource_roots", return_value=[self.root]), patch.object(link_index, "_legacy_media_root", return_value=self.root), patch.object(link_index, "resource_libraries_cached_payload", return_value={"cached": False, "roots": []}):
            payload = library_status_payload(settings(self.db))
        self.assertEqual(payload["relationships"]["counts"]["both"], 1)
        self.assertFalse(payload["resource_tree"]["cached"])
        self.assertEqual(payload["observed_unbound_directories"], [])


if __name__ == "__main__":
    unittest.main()
