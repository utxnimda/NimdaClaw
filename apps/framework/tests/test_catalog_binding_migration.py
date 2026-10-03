from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("repair_catalog_bindings", ROOT / "scripts/repair-catalog-bindings.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class CatalogBindingMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.media = self.root / "media"
        self.target = self.media / "Air" / "Air_BDRip"
        self.target.mkdir(parents=True)
        self.source = self.db / "2005.yaml"
        self.source.write_text("""# Keep this comment and extra attributes.
- attributes:
  - type: name
    data: Air
  - type: country
    data: japan
  - type: date
    data: {start: '20050106', end: '20050331'}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: ''
        custom_press: [false, 0, null]
      markers: []
      custom: keep
  unknown: preserve
""", encoding="utf-8")
        self.settings = JpTvBrowseSettings(version=1, filesystem_root=self.db,
            resolved_default_readable=str(self.source), resolved_catalog_yaml_paths=(str(self.source),),
            enum_options={}, enum_labels={}, enum_section_labels={}, app_features=())
        self.index = self.db / "index" / "link-index.yaml"
        self.index.parent.mkdir()
        self.history = self.root / "history"
        self.config = {"paths": {"resource_roots": [str(self.media)], "shortcut_root": str(self.root / "shortcuts")}}
        for name, value in (("_feature_config", self.config), ("_link_index_db_path", self.index)):
            mocked = patch.object(migration.links, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(migration, "history_catalog_root", return_value=self.history)
        mocked.start()
        self.addCleanup(mocked.stop)
        works = migration.links._load_catalog_works(self.settings)
        item = migration.links._index_entry_from_work_press(works[0], works[0]["press"][0], [])
        item.update(target_path=str(self.target), target_source="index_db_previous")
        self.index.write_text(dump_yaml_string({"version": 1, "catalog_root": str(self.db), "items": [item]}), encoding="utf-8")

    def test_preview_is_read_only_and_apply_preserves_unknown_fields_and_backups(self):
        before, index_before = self.source.read_bytes(), self.index.read_bytes()
        plan = migration.repair(self.settings)
        self.assertEqual(plan["works_changed"], 1)
        self.assertEqual(plan["press_bindings_added"], 1)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.index.read_bytes(), index_before)
        self.assertFalse(self.history.exists())
        result = migration.repair(self.settings, apply=True, expect_plan_sha256=plan["plan_sha256"])
        self.assertTrue(result["applied"])
        self.assertEqual(len(result["backups"]), 2)
        backups = [Path(path).read_bytes() for path in result["backups"]]
        self.assertIn(before, backups)
        self.assertIn(index_before, backups)
        text = self.source.read_text(encoding="utf-8")
        self.assertIn("# Keep this comment", text)
        document = load_yaml_string(text)
        collection = document[0]["attributes"][3]["data"]
        self.assertEqual(collection["custom"], "keep")
        self.assertEqual(collection["collectioned"][0]["custom_press"], [False, 0, None])
        self.assertEqual(Path(collection["path"]), self.target.parent)
        self.assertEqual(collection["collectioned"][0]["press_path"], self.target.name)
        self.assertEqual(document[0]["unknown"], "preserve")
        index = load_yaml_string(self.index.read_text(encoding="utf-8"))
        self.assertEqual(Path(index["items"][0]["target_path"]), self.target)
        self.assertEqual(index["items"][0]["target_source"], "catalog")
        self.assertEqual(migration.repair(self.settings)["works_changed"], 0)

    def test_wrong_or_missing_confirmation_hash_never_writes(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "expect-plan-sha256"):
            migration.repair(self.settings, apply=True)
        with self.assertRaisesRegex(ValueError, "已变化"):
            migration.repair(self.settings, apply=True, expect_plan_sha256="0" * 64)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.history.exists())

    def test_source_change_after_preview_rejects_apply(self):
        plan = migration.repair(self.settings)
        self.source.write_text(self.source.read_text(encoding="utf-8") + "# concurrent user edit\n", encoding="utf-8")
        edited = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "已变化"):
            migration.repair(self.settings, apply=True, expect_plan_sha256=plan["plan_sha256"])
        self.assertEqual(self.source.read_bytes(), edited)
        self.assertFalse(self.history.exists())

    def test_existing_unreadable_shortcut_blocks_the_work(self):
        item = load_yaml_string(self.index.read_text(encoding="utf-8"))["items"][0]
        issue = {"code": "shortcut-unreadable", "entry_key": item["entry_key"]}
        with patch.object(migration, "shortcut_evidence", return_value=({}, [], [issue])):
            plan = migration.repair(self.settings)
        self.assertEqual(plan["works_changed"], 0)
        self.assertIn(issue, plan["issues"])

    def test_conflict_on_one_press_blocks_partial_work_migration(self):
        original = migration.plan_catalog_bindings
        def with_conflict(*args, **kwargs):
            plan = original(*args, **kwargs)
            plan["issues"].append({"code": "test-conflict", "blocking": True,
                                  "yaml_source_rel": "2005.yaml", "index_in_file": 0})
            return plan
        with patch.object(migration, "plan_catalog_bindings", side_effect=with_conflict):
            self.assertEqual(migration.repair(self.settings)["works_changed"], 0)

    def test_fill_mapping_never_replaces_nonempty_existing_bindings(self):
        document = load_yaml_string(self.source.read_text(encoding="utf-8"))
        document[0]["attributes"][3]["data"]["path"] = "different"
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            migration.fill_mapping(document, {"index_in_file": 0, "path": str(self.target.parent), "press": []})

    def test_stale_identity_is_not_relabelled_as_current_index_evidence(self):
        index = load_yaml_string(self.index.read_text(encoding="utf-8"))
        index["items"][0]["name"] = "Other work"
        self.index.write_text(dump_yaml_string(index), encoding="utf-8")
        before = self.source.read_bytes()
        plan = migration.repair(self.settings)
        self.assertEqual(plan["works_changed"], 0)
        migration.repair(self.settings, apply=True, expect_plan_sha256=plan["plan_sha256"])
        self.assertEqual(self.source.read_bytes(), before)
        current = load_yaml_string(self.index.read_text(encoding="utf-8"))
        self.assertEqual(current["items"][0]["target_path"], "")

    def test_mismatched_press_identity_cannot_become_trusted_after_repair(self):
        index = load_yaml_string(self.index.read_text(encoding="utf-8"))
        index["items"][0]["press_group"] = "Different group"
        self.index.write_text(dump_yaml_string(index), encoding="utf-8")
        before = self.source.read_bytes()
        plan = migration.repair(self.settings)
        self.assertEqual(plan["works_changed"], 0)
        migration.repair(self.settings, apply=True, expect_plan_sha256=plan["plan_sha256"])
        self.assertEqual(self.source.read_bytes(), before)
        current = load_yaml_string(self.index.read_text(encoding="utf-8"))
        self.assertEqual(current["items"][0]["target_path"], "")
        self.assertEqual(migration.repair(self.settings)["works_changed"], 0)

    def test_duplicate_index_evidence_cannot_become_trusted_after_repair(self):
        index = load_yaml_string(self.index.read_text(encoding="utf-8"))
        index["items"].append(dict(index["items"][0]))
        self.index.write_text(dump_yaml_string(index), encoding="utf-8")
        before = self.source.read_bytes()
        plan = migration.repair(self.settings)
        self.assertEqual(plan["works_changed"], 0)
        migration.repair(self.settings, apply=True, expect_plan_sha256=plan["plan_sha256"])
        self.assertEqual(self.source.read_bytes(), before)
        current = load_yaml_string(self.index.read_text(encoding="utf-8"))
        self.assertEqual(current["items"][0]["target_path"], "")
        self.assertEqual(migration.repair(self.settings)["works_changed"], 0)

    def test_commit_failure_rolls_back_catalog_and_keeps_recovery_snapshots(self):
        from work_catalog_yaml import persistence
        plan = migration.repair(self.settings)
        before, index_before = self.source.read_bytes(), self.index.read_bytes()
        original = persistence.atomic_write_bytes
        def failing(path, data):
            if path == self.index:
                raise OSError("simulated index write failure")
            return original(path, data)
        with patch.object(persistence, "atomic_write_bytes", side_effect=failing):
            with self.assertRaisesRegex(OSError, "simulated"):
                migration.repair(self.settings, apply=True, expect_plan_sha256=plan["plan_sha256"])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.index.read_bytes(), index_before)
        self.assertEqual(len(list(self.history.rglob("*.yaml"))), 2)


if __name__ == "__main__":
    unittest.main()
