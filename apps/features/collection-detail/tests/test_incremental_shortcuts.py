from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from collection_detail import link_index
from collection_detail.catalog_repository import CatalogRepository
from work_catalog_yaml.yaml_io import load_yaml, dump_yaml_string, load_yaml_string
from work_catalog_yaml.operation_progress import OperationRegistry, execute_operation
from test_jp_tv_link_index import _settings, _catalog_yaml_mapped, _catalog_yaml_with_press


class IncrementalShortcutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.media = self.root / "media"
        self.target = self.media / "Bound" / "BDRip"
        self.target.mkdir(parents=True)
        self.finish = self.root / "finish"
        self.finish.mkdir()
        self.keep = self.finish / "user-file.txt"
        self.keep.write_bytes(b"keep")
        self.source = self.db / "[JP][TVInfo][2098].yaml"
        docs = load_yaml_string(_catalog_yaml_mapped("Bound", self.target.parent.as_posix(), "BDRip"))
        docs += load_yaml_string(_catalog_yaml_mapped("Missing", (self.media / "Missing").as_posix(), "BDRip"))
        docs += load_yaml_string(_catalog_yaml_with_press("Unbound", [("BDRip", "VCB")]))
        self.source.write_text(dump_yaml_string(docs), encoding="utf-8")
        self.settings = _settings(self.db, self.source)
        config = {"paths": {"media_root": str(self.media), "resource_roots": [str(self.media)],
                            "shortcut_root": str(self.finish)}}
        for target, value in (("_feature_config", config), ("_link_index_db_path", self.root / "index.yaml"),
                              ("feature_data_root", self.root / "feature")):
            mock = patch.object(link_index, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        reader = patch.object(link_index, "_windows_shortcut_targets", side_effect=lambda paths: {
            str(path.resolve()): {"target_path": path.read_text(encoding="utf-8")} for path in paths
        })
        reader.start()
        self.addCleanup(reader.stop)

    def preview(self) -> dict:
        return link_index.generate_link_index_files_from_ui_body(
            {"preview": True, "incremental": True}, settings=self.settings,
        )["file_generation"]

    def apply(self, plan: dict) -> dict:
        return link_index.generate_link_index_files_from_ui_body({
            "incremental": True, "confirm_incremental": True, "plan_id": plan["plan_id"],
        }, settings=self.settings)["file_generation"]

    @staticmethod
    def create(path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(target), encoding="utf-8")

    def test_incremental_creates_only_saved_existing_binding_and_is_idempotent(self) -> None:
        before = self.source.read_bytes()
        with patch.object(link_index, "_create_windows_shortcut", side_effect=self.create) as create:
            plan = self.preview()
            self.assertEqual(plan["creatable"], 1)
            self.assertEqual(plan["skipped_unbound_count"], 1)
            self.assertEqual(plan["skipped_missing_target"], 1)
            result = self.apply(plan)
            self.assertEqual(result["created"], 1)
            self.assertEqual(result["removed_count"], 0)
            self.assertEqual(result["catalog_writes"], [])
            repeat = self.preview()
            self.assertEqual(repeat["creatable"], 0)
            self.assertEqual(repeat["already_exists_count"], 1)
            self.assertEqual(self.apply(repeat)["created"], 0)
            self.assertEqual(create.call_count, 1)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.keep.read_bytes(), b"keep")

    def test_incremental_rejects_existing_other_target_without_writes(self) -> None:
        plan = self.preview()
        shortcut = Path(next(row["shortcut_path"] for row in plan["items"] if row["status"] == "planned"))
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_text(str(self.root / "other-target"), encoding="utf-8")
        before = shortcut.read_bytes()
        with patch.object(link_index, "_create_windows_shortcut") as create:
            conflict = self.preview()
            self.assertEqual(conflict["conflict_count"], 1)
            issue = next(item for item in conflict["issues"] if item["code"] == "shortcut-existing-conflict")
            self.assertIn(str(self.root / "other-target"), issue["reason"])
            self.assertIn(str(self.target), issue["reason"])
            self.assertEqual(issue["shortcut_path"], str(shortcut))
            self.assertEqual(issue["yaml_source_rel"], self.source.name)
            self.assertEqual(issue["index_in_file"], 0)
            with self.assertRaisesRegex(ValueError, "冲突"):
                self.apply(conflict)
            create.assert_not_called()
        self.assertEqual(shortcut.read_bytes(), before)
        self.assertFalse((self.root / "index.yaml").exists())

    def test_skipped_binding_issues_identify_each_work_record_and_missing_field(self) -> None:
        plan = self.preview()
        missing = next(issue for issue in plan["issues"] if issue["code"] == "missing-disk-directory")
        unbound = next(issue for issue in plan["issues"] if issue["code"] == "missing-catalog-binding")
        self.assertEqual(missing["yaml_source_rel"], self.source.name)
        self.assertEqual(missing["index_in_file"], 1)
        self.assertEqual(missing["name"], "Missing")
        self.assertEqual(missing["target_path"], str(self.media / "Missing" / "BDRip"))
        self.assertTrue(missing["press_key"])
        self.assertIn("path", unbound["reason"])
        self.assertEqual(unbound["index_in_file"], 2)
        self.assertTrue(unbound["skipped"])
        self.assertFalse((self.root / "index.yaml").exists())

    def test_create_failure_has_object_identity_cause_and_stage_in_operation_details(self) -> None:
        plan = self.preview()
        registry = OperationRegistry()
        operation = registry.register(str(uuid4()), "补建快捷方式")
        source_before = self.source.read_bytes()
        with patch.object(link_index, "_create_windows_shortcut", side_effect=PermissionError("fixture-denied")):
            result = execute_operation(registry, operation, self.apply, plan)
        failure = result["failed"][0]
        self.assertEqual(failure["yaml_source_rel"], self.source.name)
        self.assertEqual(failure["index_in_file"], 0)
        self.assertEqual(failure["object"], "Bound")
        self.assertEqual(failure["error_type"], "PermissionError")
        self.assertEqual(failure["reason"], "fixture-denied")
        self.assertEqual(failure["target_path"], str(self.target))
        details = registry.snapshot(operation.id)["result"]["details"]
        self.assertTrue(any(item.get("error_type") == "PermissionError" and item.get("stage") == "增量创建快捷方式" for item in details))
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertFalse(list(self.finish.rglob("*.lnk")))

    def test_creation_reports_all_failures_beyond_old_fifty_item_cap(self) -> None:
        items = [{"shortcut_relpath": f"work-{i}.lnk", "shortcut_path": str(self.finish / f"work-{i}.lnk"),
                  "target_path": str(self.target), "target_exists": True,
                  "work_key": f"work-{i}", "press_key": "p", "name": f"Work {i}",
                  "yaml_source_rel": self.source.name, "index_in_file": i} for i in range(51)]
        bindings = {"issues": [], "summary": {}, "source_versions": []}
        with patch.object(link_index, "_catalog_binding_generation_context", return_value=([], items, False, bindings)), \
             patch.object(link_index, "_save_index_entries"), \
             patch.object(link_index, "_create_windows_shortcut", side_effect=OSError("cannot create")):
            plan = self.preview()
            result = self.apply(plan)
        self.assertEqual(result["failed_count"], 51)
        self.assertEqual(len(result["failed"]), 51)
        self.assertEqual(result["failed"][-1]["index_in_file"], 50)

    def test_incremental_rejects_stale_db_byte_snapshot(self) -> None:
        plan = self.preview()
        doc = load_yaml(self.source)
        doc[0]["custom"] = "changed only a non-target field"
        self.source.write_text(dump_yaml_string(doc), encoding="utf-8")
        with patch.object(link_index, "_create_windows_shortcut") as create:
            with self.assertRaisesRegex(ValueError, "重新预检"):
                self.apply(plan)
            create.assert_not_called()
        self.assertFalse((self.root / "index.yaml").exists())

    def test_incremental_rejects_stale_shortcut_file_state(self) -> None:
        plan = self.preview()
        shortcut = Path(next(row["shortcut_path"] for row in plan["items"] if row["status"] == "planned"))
        self.create(shortcut, self.target)
        current = self.preview()
        shortcut.write_text(str(self.target) + "\n", encoding="utf-8")
        with patch.object(link_index, "_windows_shortcut_targets", return_value={
            str(shortcut.resolve()): {"target_path": str(self.target)},
        }), patch.object(link_index, "_create_windows_shortcut") as create:
            with self.assertRaisesRegex(ValueError, "重新预检"):
                self.apply(current)
            create.assert_not_called()
        self.assertFalse((self.root / "index.yaml").exists())

    def test_replace_mode_rejects_db_expansion_since_preview_before_any_clear(self) -> None:
        self.source.write_text(
            _catalog_yaml_mapped("Bound", self.target.parent.as_posix(), "BDRip"), encoding="utf-8",
        )
        plan = link_index.generate_link_index_files_from_ui_body(
            {"preview": True}, settings=self.settings,
        )["file_generation"]
        second = self.media / "Second" / "BDRip"
        second.mkdir(parents=True)
        doc = load_yaml(self.source)
        doc += load_yaml_string(_catalog_yaml_mapped("Second", second.parent.as_posix(), "BDRip"))
        self.source.write_text(dump_yaml_string(doc), encoding="utf-8")
        with patch.object(link_index, "_clear_directory_contents") as clear, patch.object(
            link_index, "_save_index_entries",
        ) as index_write, patch.object(link_index, "_create_windows_shortcut") as create:
            with self.assertRaisesRegex(ValueError, "重新预检"):
                link_index.generate_link_index_files_from_ui_body({
                    "plan_id": plan["plan_id"], "confirm_clear": True, "confirm_clear_twice": True,
                }, settings=self.settings)
            clear.assert_not_called()
            index_write.assert_not_called()
            create.assert_not_called()
        self.assertEqual(self.keep.read_bytes(), b"keep")

    def test_replace_mode_never_reloads_a_larger_db_after_preflight(self) -> None:
        self.source.write_text(
            _catalog_yaml_mapped("Bound", self.target.parent.as_posix(), "BDRip"), encoding="utf-8",
        )
        plan = link_index.generate_link_index_files_from_ui_body(
            {"preview": True}, settings=self.settings,
        )["file_generation"]
        second = self.media / "Second" / "BDRip"
        second.mkdir(parents=True)
        original_entries = link_index._shortcut_root_direct_entries
        scans = 0

        def external_edit_after_preflight(root):
            nonlocal scans
            scans += 1
            if scans == 2:
                doc = load_yaml(self.source)
                doc += load_yaml_string(_catalog_yaml_mapped("Second", second.parent.as_posix(), "BDRip"))
                self.source.write_text(dump_yaml_string(doc), encoding="utf-8")
            return original_entries(root)

        with patch.object(link_index, "_shortcut_root_direct_entries", side_effect=external_edit_after_preflight), patch.object(
            CatalogRepository, "load_works", autospec=True, side_effect=CatalogRepository.load_works,
        ) as db_load, patch.object(link_index, "_create_windows_shortcut", side_effect=self.create) as create:
            result = link_index.generate_link_index_files_from_ui_body({
                "plan_id": plan["plan_id"], "confirm_clear": True, "confirm_clear_twice": True,
            }, settings=self.settings)
            self.assertEqual(result["file_generation"]["created"], 1)
            create.assert_called_once()
            self.assertEqual(create.call_args.args[1], self.target)
            self.assertEqual(db_load.call_count, 1, "Only the reviewed snapshot may reach execution")
        self.assertEqual(len(load_yaml(self.source)), 2, "The external edit occurred")
        self.assertEqual(len(load_yaml(self.root / "index.yaml")["items"]), 1)
        self.assertEqual(len(result["works"]), 1, "The result reports the executed snapshot")
        self.assertFalse(any("Second" in str(path) for path in self.finish.rglob("*.lnk")))

    def test_catalog_snapshot_does_not_admit_a_new_file_added_while_reading_sources(self) -> None:
        self.source.write_text(
            _catalog_yaml_mapped("Bound", self.target.parent.as_posix(), "BDRip"), encoding="utf-8",
        )
        second = self.media / "Second" / "BDRip"
        second.mkdir(parents=True)
        next_year = self.db / "[JP][TVInfo][2099].yaml"
        original_read = CatalogRepository.read_source

        def read_then_add_year(repository, path):
            source = original_read(repository, path)
            if not next_year.exists():
                next_year.write_text(
                    _catalog_yaml_mapped("Second", second.parent.as_posix(), "BDRip"), encoding="utf-8",
                )
            return source

        with patch.object(CatalogRepository, "read_source", autospec=True, side_effect=read_then_add_year):
            works, items, _, bindings = link_index._catalog_binding_generation_context(self.settings)
        self.assertTrue(next_year.exists())
        self.assertEqual([work["name"] for work in works], ["Bound"])
        self.assertEqual(len(items), 1)
        self.assertEqual([source["path"] for source in bindings["source_versions"]], [self.source.name])
        fresh_works, _, _, fresh_bindings = link_index._catalog_binding_generation_context(self.settings)
        self.assertEqual({work["name"] for work in fresh_works}, {"Bound", "Second"})
        self.assertEqual({source["path"] for source in fresh_bindings["source_versions"]},
                         {self.source.name, next_year.name})

    def test_replace_mode_blocks_missing_bindings_before_clearing_outputs(self) -> None:
        with patch.object(link_index, "_clear_directory_contents") as clear:
            with self.assertRaisesRegex(ValueError, "path / press_path"):
                link_index.generate_link_index_files_from_ui_body(
                    {"confirm_clear": True, "confirm_clear_twice": True}, settings=self.settings,
                )
            clear.assert_not_called()
        self.assertEqual(self.keep.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
