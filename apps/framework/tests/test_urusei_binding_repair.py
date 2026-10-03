from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("repair_urusei_bindings", ROOT / "scripts/repair-urusei-bindings.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class UruseiBindingRepairTest(unittest.TestCase):
    """All DB/index/shortcut fixtures live in a temporary directory; no COM is used."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.media_parent = self.root / "media"
        self.media = self.media_parent / "うる星やつら"
        self.targets = {
            (1981, "VCB"): self.media / "うる星やつら_BDRip",
            (1981, "DBDR"): self.media / "うる星やつら_BDRip(DBDR)",
            (2022, "JSUM"): self.media / "うる星やつら 2022_BDRip",
            (2022, "DBD"): self.media / "うる星やつら 2022_BDRip(DBDR)",
        }
        for target in self.targets.values():
            target.mkdir(parents=True)
        self.sources = {}
        for year, start, end, groups in (
            (1981, "19811014", "19860319", ("VCB", "DBDR")),
            (2022, "20221013", "20230323", ("JSUM", "DBD")),
        ):
            source = self.db / f"[JP][TVInfo][{year}].yaml"
            record = {"attributes": [
                {"type": "date", "data": {"start": start, "end": end}},
                {"type": "collection-type", "data": {
                    "domain": "animation", "release_type": "tv",
                    "collectioned": [{"press_format": "BDRip", "press_group": group,
                                       "custom_press": [False, 0, None]} for group in groups],
                    "markers": [], "custom_collection": {"keep": True},
                }},
                {"type": "country", "data": "japan"},
                {"type": "name", "data": "うる星やつら"},
                {"type": "unknown", "data": {"unchanged": "extension"}},
            ], "record_note": "preserve"}
            source.write_text("# Keep the user-authored comment.\n" + dump_yaml_string([record]), encoding="utf-8")
            self.sources[year] = source
        self.settings = JpTvBrowseSettings(
            version=1, filesystem_root=self.db,
            resolved_default_readable=str(self.sources[1981]),
            resolved_catalog_yaml_paths=tuple(str(path) for path in self.sources.values()),
            enum_options={}, enum_labels={}, enum_section_labels={}, app_features=(),
        )
        self.index = self.db / "index" / "link-index.yaml"
        self.index.parent.mkdir()
        self.history = self.root / "history"
        self.shortcut_root = self.root / "shortcuts"
        self.config = {"paths": {"resource_roots": [str(self.media_parent)], "shortcut_root": str(self.shortcut_root)}}
        for owner, name, value in (
            (migration.links, "_feature_config", self.config),
            (migration.links, "_link_index_db_path", self.index),
            (migration, "history_catalog_root", self.history),
            (migration.migration, "load_jp_tv_browse_settings", self.settings),
        ):
            mocked = patch.object(owner, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

        def read_shortcuts(paths):
            return {str(path): {"target_path": json.loads(Path(path).read_bytes())["target"],
                                "target_resolved": True} for path in paths}

        def retarget(previous, target):
            metadata = json.loads(previous)
            metadata["target"] = str(target)
            return json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")

        for name, implementation in (("_read_shortcut_targets", read_shortcuts), ("_retarget_shortcut_bytes", retarget)):
            mocked = patch.object(migration, name, side_effect=implementation)
            mocked.start()
            self.addCleanup(mocked.stop)

        self.shortcuts = {}
        items = []
        for work in migration.links._load_catalog_works(self.settings):
            year = int(work["begin_date"][:4])
            for press in work["press"]:
                group = press["press_group"]
                target = self.targets[(year, group)] if year == 1981 else self.targets[(1981, "VCB")]
                item = migration.links._index_entry_from_work_press(work, press, [])
                item.update(target_path=str(target), target_source="index_db_previous",
                            target_exists=True, shortcut_target_path=str(target),
                            shortcut_target_exists=True, shortcut_exists=True)
                shortcut = Path(item["shortcut_path"])
                shortcut.parent.mkdir(parents=True, exist_ok=True)
                shortcut.write_bytes(json.dumps({
                    "target": str(target), "arguments": "--keep-this", "working_directory": "keep-working-directory",
                    "icon": "keep-icon,1", "description": "user-description", "hotkey": "Ctrl+Alt+U",
                }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                self.shortcuts[(year, group)] = shortcut
                items.append(item)
        self.unrelated = {"entry_key": "unrelated-index-entry", "work_key": "other.yaml#9",
                          "name": "Do not change this item", "target_path": "unrelated-target",
                          "target_source": "manual_fix", "custom": [False, 0, None, ""]}
        items.append(copy.deepcopy(self.unrelated))
        self.index.write_text(dump_yaml_string({
            "version": 1, "catalog_root": migration.links._settings_catalog_root_key(self.settings), "generated_at": "2000-01-01T00:00:00",
            "custom_index_metadata": {"preserve": True}, "items": items,
        }), encoding="utf-8")
        self.files = [*self.sources.values(), self.index, *self.shortcuts.values()]

    def snapshot(self):
        return {path: path.read_bytes() for path in self.files}

    def preview(self):
        return migration.repair(self.settings, media_root=self.media)

    def apply(self, plan):
        return migration.repair(self.settings, media_root=self.media, apply=True,
                                expect_plan_sha256=plan["plan_sha256"])

    def collection(self, year):
        work = load_yaml_string(self.sources[year].read_text(encoding="utf-8"))[0]
        return next(attr["data"] for attr in work["attributes"] if attr["type"] == "collection-type")

    def edit_collection(self, year, edit):
        document = load_yaml_string(self.sources[year].read_text(encoding="utf-8"))
        coll = next(attr["data"] for attr in document[0]["attributes"] if attr["type"] == "collection-type")
        edit(coll)
        self.sources[year].write_text(dump_yaml_string(document), encoding="utf-8")

    def test_preview_is_read_only_and_missing_or_wrong_hash_never_writes(self):
        before = self.snapshot()
        plan = self.preview()
        self.assertFalse(plan["applied"])
        self.assertEqual(len(plan["plan_sha256"]), 64)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())
        with self.assertRaises(ValueError):
            migration.repair(self.settings, media_root=self.media, apply=True)
        with self.assertRaises(ValueError):
            migration.repair(self.settings, media_root=self.media, apply=True, expect_plan_sha256="0" * 64)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_apply_changes_only_two_shortcuts_two_catalogs_and_index_with_five_backups(self):
        before = self.snapshot()
        plan = self.preview()
        result = self.apply(plan)
        self.assertTrue(result["applied"])
        expected_changes = {self.sources[1981], self.sources[2022], self.index,
                            self.shortcuts[(2022, "JSUM")], self.shortcuts[(2022, "DBD")]}
        changed = {path for path, previous in before.items() if path.read_bytes() != previous}
        self.assertEqual(changed, expected_changes)
        self.assertEqual(len(result["backups"]), 5)
        backups = [Path(path).read_bytes() for path in result["backups"]]
        self.assertCountEqual(backups, [before[path] for path in expected_changes])
        for year in (1981, 2022):
            self.assertIn("# Keep the user-authored comment.", self.sources[year].read_text(encoding="utf-8"))
            document = load_yaml_string(self.sources[year].read_text(encoding="utf-8"))
            coll = self.collection(year)
            self.assertEqual(Path(coll["path"]), self.media)
            self.assertEqual(coll["custom_collection"], {"keep": True})
            self.assertEqual(document[0]["record_note"], "preserve")
            self.assertEqual(document[0]["attributes"][-1], {"type": "unknown", "data": {"unchanged": "extension"}})
            for press in coll["collectioned"]:
                self.assertEqual(press["press_path"], self.targets[(year, press["press_group"])].name)
                self.assertEqual(press["custom_press"], [False, 0, None])
        index = load_yaml_string(self.index.read_text(encoding="utf-8"))
        self.assertEqual(index["items"][-1], self.unrelated)
        self.assertEqual(index["custom_index_metadata"], {"preserve": True})
        for item in index["items"][:-1]:
            year = int(item["begin_date"][:4])
            self.assertEqual(Path(item["target_path"]), self.targets[(year, item["press_group"])])
            self.assertEqual(item["target_source"], "catalog")
        for (year, group), shortcut in self.shortcuts.items():
            metadata = json.loads(shortcut.read_bytes())
            self.assertEqual(Path(metadata["target"]), self.targets[(year, group)])
            previous_metadata = json.loads(before[shortcut])
            self.assertEqual({key: value for key, value in metadata.items() if key != "target"},
                             {key: value for key, value in previous_metadata.items() if key != "target"})

    def test_source_change_after_preview_rejects_apply(self):
        plan = self.preview()
        self.sources[2022].write_bytes(self.sources[2022].read_bytes() + b"# concurrent edit\n")
        edited = self.snapshot()
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertEqual(self.snapshot(), edited)
        self.assertFalse(self.history.exists())

    def test_shortcut_change_after_preview_rejects_apply(self):
        plan = self.preview()
        shortcut = self.shortcuts[(2022, "JSUM")]
        metadata = json.loads(shortcut.read_bytes())
        metadata["description"] = "concurrent user shortcut edit"
        shortcut.write_bytes(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        edited = self.snapshot()
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertEqual(self.snapshot(), edited)
        self.assertFalse(self.history.exists())

    def test_missing_media_directory_rejects_without_writing(self):
        self.targets[(2022, "DBD")].rmdir()
        before = self.snapshot()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.preview()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_nonempty_existing_work_root_is_never_overwritten(self):
        other = self.media_parent / "other-existing-work"
        other.mkdir()
        self.edit_collection(2022, lambda coll: coll.update(path=str(other)))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_nonempty_existing_press_path_conflict_is_never_overwritten(self):
        def conflict(coll):
            coll["path"] = str(self.media)
            coll["collectioned"][0]["press_path"] = self.targets[(1981, "VCB")].name
        self.edit_collection(2022, conflict)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_unexpected_press_identity_is_rejected(self):
        self.edit_collection(2022, lambda coll: coll["collectioned"][1].update(press_group="UNRELATED"))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_commit_failure_restores_all_original_files_and_preserves_backups(self):
        from work_catalog_yaml import persistence
        plan = self.preview()
        before = self.snapshot()
        original = persistence.atomic_write_bytes

        def failing(path, data):
            if Path(path) == self.index:
                raise OSError("simulated urusei index write failure")
            return original(path, data)

        with patch.object(persistence, "atomic_write_bytes", side_effect=failing):
            with self.assertRaisesRegex(OSError, "simulated urusei"):
                self.apply(plan)
        self.assertEqual(self.snapshot(), before)
        backups = [path.read_bytes() for path in self.history.rglob("*") if path.is_file()]
        for path in (self.sources[1981], self.sources[2022], self.index,
                     self.shortcuts[(2022, "JSUM")], self.shortcuts[(2022, "DBD")]):
            self.assertIn(before[path], backups)

    def test_second_shortcut_preparation_failure_never_writes_formal_files(self):
        plan = self.preview()
        before = self.snapshot()
        original = migration._retarget_shortcut_bytes
        prepared_targets = []

        def failing(previous, target):
            prepared_targets.append(Path(target))
            if Path(target) == self.targets[(2022, "DBD")]:
                raise OSError("simulated second shortcut preparation failure")
            return original(previous, target)

        with patch.object(migration, "_retarget_shortcut_bytes", side_effect=failing):
            with self.assertRaisesRegex(OSError, "second shortcut preparation"):
                self.apply(plan)
        self.assertEqual(prepared_targets, [self.targets[(2022, "JSUM")], self.targets[(2022, "DBD")]])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.history.exists())

    def test_second_shortcut_commit_failure_rolls_back_catalog_index_and_first_shortcut(self):
        from work_catalog_yaml import persistence
        plan = self.preview()
        before = self.snapshot()
        original = persistence.atomic_write_bytes
        first_shortcut = self.shortcuts[(2022, "JSUM")]
        second_shortcut = self.shortcuts[(2022, "DBD")]
        successful_writes = []

        def failing(path, data):
            path = Path(path)
            if path == second_shortcut:
                self.assertNotEqual(first_shortcut.read_bytes(), before[first_shortcut])
                self.assertNotEqual(self.index.read_bytes(), before[self.index])
                for source in self.sources.values():
                    self.assertNotEqual(source.read_bytes(), before[source])
                raise OSError("simulated second shortcut commit failure")
            result = original(path, data)
            successful_writes.append((path, data))
            return result

        with patch.object(persistence, "atomic_write_bytes", side_effect=failing):
            with self.assertRaisesRegex(OSError, "second shortcut commit"):
                self.apply(plan)
        self.assertEqual(self.snapshot(), before)
        first_writes = [data for path, data in successful_writes if path == first_shortcut]
        self.assertEqual(len(first_writes), 2)
        self.assertNotEqual(first_writes[0], before[first_shortcut])
        self.assertEqual(first_writes[1], before[first_shortcut])
        backups = [path.read_bytes() for path in self.history.rglob("*") if path.is_file()]
        self.assertCountEqual(backups, [before[path] for path in (
            self.sources[1981], self.sources[2022], self.index, first_shortcut, second_shortcut,
        )])


if __name__ == "__main__":
    unittest.main()
