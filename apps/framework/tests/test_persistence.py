from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from work_catalog_yaml import persistence
from work_catalog_yaml.persistence import FileWrite, atomic_write_bytes, commit_file_writes, history_snapshot_name


class PersistenceTest(unittest.TestCase):
    def test_replace_failure_preserves_original_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "records.yaml"
            target.write_bytes(b"original")
            with patch.object(Path, "replace", side_effect=OSError("replace denied")):
                with self.assertRaisesRegex(OSError, "replace denied"):
                    atomic_write_bytes(target, b"replacement")
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(root.iterdir()), [target])

    def test_batch_checks_every_source_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first, second = root / "first.yaml", root / "second.yaml"
            first.write_bytes(b"first")
            second.write_bytes(b"later edit")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                commit_file_writes([FileWrite(first, b"new first", b"first"), FileWrite(second, b"new second", b"second")])
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"later edit")

    def test_later_replace_failure_rolls_back_existing_and_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first, added, last = root / "first.yaml", root / "added.yaml", root / "last.yaml"
            first.write_bytes(b"first")
            last.write_bytes(b"last")

            def fail_last(target: Path, data: bytes) -> None:
                if target == last:
                    raise OSError("disk failure")
                atomic_write_bytes(target, data)

            with patch.object(persistence, "atomic_write_bytes", side_effect=fail_last):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    commit_file_writes([
                        FileWrite(first, b"new first", b"first"),
                        FileWrite(added, b"new added", None),
                        FileWrite(last, b"new last", b"last"),
                    ])
            self.assertEqual(first.read_bytes(), b"first")
            self.assertFalse(added.exists())
            self.assertEqual(last.read_bytes(), b"last")

    def test_rollback_does_not_clobber_another_writer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first, last = root / "first.yaml", root / "last.yaml"
            first.write_bytes(b"first")
            last.write_bytes(b"last")

            def concurrent_edit(target: Path, data: bytes) -> None:
                if target == last:
                    first.write_bytes(b"concurrent edit")
                    raise OSError("disk failure")
                atomic_write_bytes(target, data)

            with patch.object(persistence, "atomic_write_bytes", side_effect=concurrent_edit):
                with self.assertRaisesRegex(persistence.PersistenceRollbackError, "无法自动恢复") as raised:
                    commit_file_writes([FileWrite(first, b"new first", b"first"), FileWrite(last, b"new last", b"last")])
            self.assertEqual(first.read_bytes(), b"concurrent edit")
            self.assertIsInstance(raised.exception, OSError)
            self.assertIn(str(first), str(raised.exception))
            self.assertIn("拒绝覆盖", str(raised.exception))

    def test_snapshot_names_are_unique_even_at_identical_time(self) -> None:
        target = Path("records.yaml")
        now = datetime(2026, 9, 5)
        self.assertEqual(len({history_snapshot_name(target, now=now) for _ in range(100)}), 100)


if __name__ == "__main__":
    unittest.main()
