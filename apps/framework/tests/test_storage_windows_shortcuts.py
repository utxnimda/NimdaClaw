from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from work_catalog_yaml.storage import filesystem, windows_shortcuts


@unittest.skipUnless(os.name == "nt", "Windows shortcut publication semantics")
class WindowsShortcutStorageTest(unittest.TestCase):
    def test_create_never_overwrites_an_existing_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            shortcut = Path(td) / "keep.lnk"
            shortcut.write_bytes(b"keep")
            with patch.object(windows_shortcuts.subprocess, "run") as process:
                with self.assertRaises(FileExistsError):
                    windows_shortcuts.create_windows_shortcut(shortcut, Path(td))
                process.assert_not_called()
            self.assertEqual(shortcut.read_bytes(), b"keep")

    def test_create_publishes_only_complete_temporary_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            shortcut = Path(td) / "new.lnk"
            def create_temporary(*_args, **kwargs):
                temporary = Path(kwargs["env"]["NIMDA_SHORTCUT_PATH"])
                self.assertNotEqual(temporary, shortcut)
                self.assertFalse(shortcut.exists())
                temporary.write_bytes(b"completed shortcut")
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            with patch.object(windows_shortcuts.subprocess, "run", side_effect=create_temporary):
                windows_shortcuts.create_windows_shortcut(shortcut, Path(td))
            self.assertEqual(shortcut.read_bytes(), b"completed shortcut")
            self.assertEqual(list(Path(td).iterdir()), [shortcut])

    def test_create_race_preserves_another_writers_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            shortcut = Path(td) / "race.lnk"
            def racing_creator(*_args, **kwargs):
                Path(kwargs["env"]["NIMDA_SHORTCUT_PATH"]).write_bytes(b"ours")
                shortcut.write_bytes(b"other writer")
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            with patch.object(windows_shortcuts.subprocess, "run", side_effect=racing_creator):
                with self.assertRaises(FileExistsError):
                    windows_shortcuts.create_windows_shortcut(shortcut, Path(td))
            self.assertEqual(shortcut.read_bytes(), b"other writer")
            self.assertEqual(list(Path(td).iterdir()), [shortcut])

    def test_create_timeout_cleans_its_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            shortcut = Path(td) / "new.lnk"
            def time_out(*_args, **kwargs):
                Path(kwargs["env"]["NIMDA_SHORTCUT_PATH"]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired("powershell", 20)
            with patch.object(windows_shortcuts.subprocess, "run", side_effect=time_out):
                with self.assertRaises(subprocess.TimeoutExpired):
                    windows_shortcuts.create_windows_shortcut(shortcut, Path(td))
            self.assertEqual(list(Path(td).iterdir()), [])


class FilesystemStorageTest(unittest.TestCase):
    def test_ordinary_directory_rejects_reparse_parent_before_reading_children(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = root / "junction" / "Release"
            original = Path.lstat
            def metadata(path):
                if path == root / "junction":
                    return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
                if path == candidate:
                    return SimpleNamespace(st_mode=0o40755, st_file_attributes=0)
                return original(path)
            with patch.object(Path, "lstat", metadata):
                self.assertFalse(filesystem.ordinary_directory(str(candidate)))


if __name__ == "__main__":
    unittest.main()
