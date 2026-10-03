import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("korean_catalog_migration", ROOT / "scripts/repair-korean-catalog-placement.py")
migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migration
spec.loader.exec_module(migration)


def work(name="Korean Work", *, country="korea", domain="television", release_type="tv", start="2020-05-01"):
    return {"custom_work_metadata": {"preserved": [1, "two"]}, "attributes": [
        {"type": "date", "description": "do not change dates", "data": {"start": start, "end": "20200630"}},
        {"type": "collection-type", "data": {
            "domain": domain, "release_type": release_type, "path": '\u202a"G:\\Korea\\A  B"\u202c',
            "collectioned": [{"press_format": "1080p", "press_group": "", "press_path": '"\u2066A  B_1080p\u2069"', "custom": "untouched"}],
            "continuations": [{"title": "Second", "collectioned": [{"press_format": "BDRip", "press_group": "VCB", "press_path": '\u202a"Extra BDRip"\u202c'}]}],
            "markers": ["custom marker"],
        }},
        {"type": "country", "data": country},
        {"type": "name", "data": name},
        {"type": "custom-attribute", "description": "keep", "data": {"value": "exact"}},
    ]}


class KoreanCatalogMigrationTest(unittest.TestCase):
    def test_preview_then_apply_preserves_records_dates_and_exact_preimage_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, history = root / "db", root / "history"
            db.mkdir()
            jp, kr = db / "[JP][TVInfo][2026].yaml", db / "[KR][TVInfo][2020].yaml"
            korean, japanese, existing = work(), work("Keep Japan", country="japan"), work("Existing", start="20200101")
            source_document = {"extra": {"untouched": "yes"}, "works": [japanese, korean]}
            jp.write_bytes(("# Original comment\r\n" + dump_yaml_string(source_document).replace("\n", "\r\n")).encode("utf-8"))
            kr.write_text(dump_yaml_string([existing]), encoding="utf-8")
            preimages = {jp.name: jp.read_bytes(), kr.name: kr.read_bytes()}
            preview = migration.repair_catalog(db, history=history)
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["records_to_move"], 1)
            self.assertEqual(preview["path_fields_to_clean"], 3)
            self.assertEqual(preview["moves"][0]["date"], {"start": "2020-05-01", "end": "20200630"})
            self.assertFalse(history.exists())
            self.assertEqual(jp.read_bytes(), preimages[jp.name])
            self.assertEqual(kr.read_bytes(), preimages[kr.name])
            result = migration.repair_catalog(db, history=history, apply=True, expect_plan_sha256=preview["plan_sha256"])
            self.assertTrue(result["applied"])
            self.assertEqual(len(result["backups"]), 2)
            for backup in result["backups"]:
                name = Path(backup).name.split("__saved-")[0] + ".yaml"
                self.assertEqual(Path(backup).read_bytes(), preimages[name])
            self.assertEqual(load_yaml_string(jp.read_text(encoding="utf-8")), {"extra": {"untouched": "yes"}, "works": [japanese]})
            expected = copy.deepcopy(korean)
            collection = expected["attributes"][1]["data"]
            collection["path"] = "G:\\Korea\\A  B"
            collection["collectioned"][0]["press_path"] = "A  B_1080p"
            collection["continuations"][0]["collectioned"][0]["press_path"] = "Extra BDRip"
            self.assertEqual(load_yaml_string(kr.read_text(encoding="utf-8")), [existing, expected])
            self.assertEqual(migration.repair_catalog(db, history=history)["records_to_move"], 0)

    def test_only_exact_korean_television_classification_and_top_level_jp_files_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            jp = db / "[JP][TVInfo][2026].yaml"
            untouched = [work("Anime", domain="animation"), work("Drama legacy", domain="tv-drama"),
                         work("Movie", release_type="movie"), work("Japanese", country="japan")]
            jp.write_text(dump_yaml_string([*untouched, work()]), encoding="utf-8")
            nested = db / "nested"
            nested.mkdir()
            nested_file = nested / "[JP][TVInfo][2026].yaml"
            nested_file.write_text(dump_yaml_string([work("Nested")]), encoding="utf-8")
            other = db / "other.yaml"
            other.write_text(dump_yaml_string([work("Other")]), encoding="utf-8")
            result = migration.repair_catalog(db, history=root / "history", apply=True)
            self.assertEqual(result["source_files_scanned"], 1)
            self.assertEqual(result["records_to_move"], 1)
            self.assertEqual(load_yaml_string(jp.read_text(encoding="utf-8")), untouched)
            self.assertEqual(load_yaml_string(nested_file.read_text(encoding="utf-8")), [work("Nested")])
            self.assertEqual(load_yaml_string(other.read_text(encoding="utf-8")), [work("Other")])

    def test_empty_source_remains_as_an_empty_yaml_and_new_destination_is_not_backed_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            jp = db / "[JP][TVInfo][2026].yaml"
            jp.write_text(dump_yaml_string([work()]), encoding="utf-8")
            result = migration.repair_catalog(db, history=root / "history", apply=True)
            self.assertTrue(jp.is_file())
            self.assertEqual(load_yaml_string(jp.read_text(encoding="utf-8")), [])
            self.assertEqual(len(result["backups"]), 1)
            self.assertTrue((db / "[KR][TVInfo][2020].yaml").is_file())

    def test_duplicate_content_or_same_identity_blocks_the_entire_batch_without_merge(self):
        for identical in (True, False):
            with self.subTest(identical=identical), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                db, history = root / "db", root / "history"
                db.mkdir()
                jp, kr = db / "[JP][TVInfo][2026].yaml", db / "[KR][TVInfo][2020].yaml"
                existing, _ = migration._clean_record(work())
                if not identical:
                    existing["attributes"][0]["data"]["end"] = ""
                    existing["attributes"][1]["data"]["collectioned"] = []
                jp.write_text(dump_yaml_string([work(), work("Independent")]), encoding="utf-8")
                kr.write_text(dump_yaml_string([existing]), encoding="utf-8")
                originals = [jp.read_bytes(), kr.read_bytes()]
                result = migration.repair_catalog(db, history=history, apply=True)
                self.assertFalse(result["ok"])
                self.assertFalse(result["applied"])
                self.assertEqual(len(result["conflicts"]), 1)
                self.assertEqual([jp.read_bytes(), kr.read_bytes()], originals)
                self.assertFalse(history.exists())

    def test_duplicate_source_identity_is_detected_before_creating_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            for year in (2025, 2026):
                (db / f"[JP][TVInfo][{year}].yaml").write_text(dump_yaml_string([work()]), encoding="utf-8")
            result = migration.repair_catalog(db, history=root / "history", apply=True)
            self.assertEqual(len(result["conflicts"]), 1)
            self.assertFalse((db / "[KR][TVInfo][2020].yaml").exists())
            self.assertFalse((root / "history").exists())

    def test_changed_preview_hash_stops_before_backups_or_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            jp = db / "[JP][TVInfo][2026].yaml"
            jp.write_text(dump_yaml_string([work()]), encoding="utf-8")
            before = jp.read_bytes()
            with self.assertRaisesRegex(ValueError, "已确认的预览"):
                migration.repair_catalog(db, history=root / "history", apply=True, expect_plan_sha256="wrong")
            self.assertEqual(jp.read_bytes(), before)
            self.assertFalse((root / "history").exists())

    def test_external_file_change_after_planning_is_detected_by_preimage_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            jp = db / "[JP][TVInfo][2026].yaml"
            jp.write_text(dump_yaml_string([work()]), encoding="utf-8")
            external = dump_yaml_string([work("Externally edited")]).encode("utf-8")
            original_serialize = migration._serialize

            def concurrent_edit(document):
                result = original_serialize(document)
                jp.write_bytes(external)
                return result

            with patch.object(migration, "_serialize", side_effect=concurrent_edit), self.assertRaisesRegex(ValueError, "计划后发生变化"):
                migration.repair_catalog(db, history=root / "history", apply=True)
            self.assertEqual(jp.read_bytes(), external)
            self.assertFalse((db / "[KR][TVInfo][2020].yaml").exists())
            self.assertFalse((root / "history").exists())

    def test_aliases_fail_closed_and_history_cannot_be_inside_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "db"
            db.mkdir()
            jp = db / "[JP][TVInfo][2026].yaml"
            before = b"works: &works []\nextra: *works\n"
            jp.write_bytes(before)
            with self.assertRaisesRegex(ValueError, "YAML 别名"):
                migration.repair_catalog(db, history=root / "history", apply=True)
            self.assertEqual(jp.read_bytes(), before)
            self.assertFalse((root / "history").exists())
            with self.assertRaisesRegex(ValueError, "备份目录"):
                migration.repair_catalog(db, history=db / "history", apply=True)


if __name__ == "__main__":
    unittest.main()
