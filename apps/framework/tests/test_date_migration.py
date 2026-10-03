import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("date_migration", ROOT / "scripts/normalize-catalog-dates.py")
migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migration
spec.loader.exec_module(migration)


class DateMigrationTest(unittest.TestCase):
    def test_aliases_fail_closed_before_backups_or_writes(self):
        original = ("metadata: &shared {start: '2024-01-02', end: '2024-03-04'}\n"
                    "works:\n- attributes:\n  - type: date\n    data: *shared\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "alias.yaml"
            path.write_bytes(original.encode())
            with self.assertRaisesRegex(ValueError, "YAML 别名"):
                migration.normalize_catalog(root, history=root / "history", apply=True)
            self.assertEqual(path.read_bytes(), original.encode())
            self.assertFalse((root / "history").exists())

    def test_preview_apply_backup_and_idempotence_preserve_other_text(self):
        original = ("# user comment\r\nworks:\r\n- attributes:\r\n"
                    "  - type: date\r\n    data: {start: '2024-01-02', end: 20240304}\r\n"
                    "  - type: name\r\n    data: My Work # title\r\n")
        expected = original.replace("'2024-01-02'", "'20240102'").replace("end: 20240304", "end: '20240304'")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            path = db / "test.yaml"
            path.write_bytes(original.encode())
            preview = migration.normalize_catalog(db, history=root / "history")
            self.assertEqual(preview["fields_changed"], 2)
            self.assertEqual(path.read_bytes(), original.encode())
            self.assertFalse((root / "history").exists())
            applied = migration.normalize_catalog(db, history=root / "history", apply=True)
            self.assertEqual(path.read_bytes(), expected.encode())
            self.assertEqual(Path(applied["backups"][0]).read_bytes(), original.encode())
            self.assertEqual(migration.normalize_catalog(db, history=root / "history")["fields_changed"], 0)

    def test_bad_dates_unknown_parts_and_reversed_ranges_are_not_guessed(self):
        original = ("- attributes:\n  - type: date\n    data: {start: '20070101', end: '20074020'}\n"
                    "- attributes:\n  - type: date\n    data: {start: '20070102', end: '20070101'}\n"
                    "- attributes:\n  - type: date\n    data: {start: '20070000', end: 'XXXXXXXX'}\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "test.yaml"
            path.write_text(original, encoding="utf-8")
            result = migration.normalize_catalog(root, history=root / "history", apply=True)
            self.assertEqual(result["fields_changed"], 0)
            self.assertEqual(len(result["issues"]), 4)
            self.assertEqual(result["backups"], [])
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
