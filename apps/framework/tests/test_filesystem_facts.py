from __future__ import annotations

from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from work_catalog_yaml.storage.filesystem import ordinary_directory


class FilesystemFactTest(unittest.TestCase):
    def test_reparse_ancestor_is_rejected_before_child_probe(self):
        ancestor = Path(__file__).absolute().parent / "unsafe-parent"
        leaf = ancestor / "unvisited-child"
        probed = []

        def metadata(path):
            probed.append(path)
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=0x400 if path == ancestor else 0,
            )

        with patch.object(Path, "lstat", metadata):
            self.assertFalse(ordinary_directory(str(leaf)))
        self.assertIn(ancestor, probed)
        self.assertNotIn(leaf, probed)
        self.assertEqual(probed[0], Path(leaf.anchor))


if __name__ == "__main__":
    unittest.main()
