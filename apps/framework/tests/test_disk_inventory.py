from __future__ import annotations

from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from work_catalog_yaml.storage.disk_inventory import DiskInventory


class DiskInventoryTest(unittest.TestCase):
    def test_present_missing_and_non_directory_are_distinct_and_memoized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "ordinary"
            child.mkdir()
            file = root / "file.txt"
            file.write_text("data", encoding="utf-8")
            inventory = DiskInventory()
            original = Path.lstat
            with patch.object(Path, "lstat", autospec=True, side_effect=lambda path, *args, **kwargs: original(path, *args, **kwargs)) as probe:
                with patch("os.scandir", side_effect=AssertionError("must not scan")):
                    self.assertEqual(inventory.probe(child)["state"], "present")
                    count = probe.call_count
                    repeated = inventory.probe(child)
                    self.assertEqual(probe.call_count, count)
                    repeated["state"] = "changed"
                    self.assertEqual(inventory.probe(child)["state"], "present")
                    self.assertEqual(inventory.probe(root / "missing")["state"], "missing")
                    self.assertEqual(inventory.probe(file)["state"], "unsafe")

    def test_permission_failure_is_unavailable_not_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inaccessible"
            original = Path.lstat

            def lstat(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("access denied")
                return original(path, *args, **kwargs)

            with patch.object(Path, "lstat", autospec=True, side_effect=lstat):
                result = DiskInventory().probe(target)
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["reason"], "directory-unavailable")

    def test_links_and_reparse_points_are_rejected_before_descendant_access(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "unsafe"
            child = link / "child"
            original = Path.lstat
            for metadata in (
                SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0),
                SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400),
            ):
                with self.subTest(metadata=metadata):
                    touched = []

                    def lstat(path, *args, **kwargs):
                        touched.append(path)
                        if path == link:
                            return metadata
                        if path == child:
                            raise AssertionError("unsafe link must not be followed")
                        return original(path, *args, **kwargs)

                    with patch.object(Path, "lstat", autospec=True, side_effect=lstat):
                        result = DiskInventory().probe(child)
                    self.assertEqual(result["state"], "unsafe")
                    self.assertEqual(result["observed_path"], str(link))
                    self.assertNotIn(child, touched)

    def test_relative_and_traversal_paths_do_not_touch_the_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in ("relative/path", Path(directory) / ".." / "escape"):
                with self.subTest(path=path), patch.object(Path, "lstat", side_effect=AssertionError("invalid path must not be probed")):
                    self.assertEqual(DiskInventory().probe(path)["state"], "unsafe")


if __name__ == "__main__":
    unittest.main()
