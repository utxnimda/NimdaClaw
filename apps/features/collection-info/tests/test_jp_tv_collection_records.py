from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from collection_info import service
from work_catalog_yaml import persistence
from work_catalog_yaml.operation_progress import OperationRegistry, execute_operation
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.collection_records import (
    collection_records_payload,
    save_collection_records_from_ui_body,
    scan_finish_years,
)
from work_catalog_yaml.yaml_io import load_yaml_string


def _settings(db: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=db,
        resolved_default_readable=None,
        resolved_catalog_yaml_paths=(),
        enum_options={},
        enum_labels={},
        enum_section_labels={},
        app_features=(),
    )


class JpTvCollectionRecordsTest(unittest.TestCase):
    def test_offline_year_scan_reports_stage_path_and_original_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            finish = root / "OfflineFinish"
            registry = OperationRegistry()
            operation = registry.register(str(uuid4()), "读取收集情况")
            with patch.dict(os.environ, {"JP_TV_COLLECTION_FINISH_DIR": str(finish)}), \
                 patch.object(service, "scan_finish_years", side_effect=PermissionError("fixture denied")):
                result = execute_operation(registry, operation, collection_records_payload, _settings(db))
            issue = result["issues"][0]
            self.assertEqual(issue["source_path"], str(finish))
            self.assertEqual(issue["reason"], "fixture denied")
            self.assertEqual(issue["action"], "扫描完成年份目录")
            details = registry.snapshot(operation.id)["result"]["details"]
            self.assertTrue(any(item.get("error_type") == "PermissionError" and item.get("source_path") == str(finish) for item in details))
            self.assertEqual(registry.snapshot(operation.id)["status"], "warning")

    def test_save_failure_records_exact_database_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            registry = OperationRegistry()
            operation = registry.register(str(uuid4()), "保存收集情况")
            with patch.object(service, "commit_file_writes", side_effect=OSError("fixture disk full")):
                with self.assertRaisesRegex(OSError, "fixture disk full"):
                    execute_operation(registry, operation, save_collection_records_from_ui_body,
                                      {"record": {"completed_years": ["2024"]}}, settings=_settings(db))
            details = registry.snapshot(operation.id)["result"]["details"]
            failure = next(item for item in details if item.get("error_type") == "OSError")
            self.assertEqual(failure["target_path"], str(service.collection_records_path(_settings(db))))
            self.assertEqual(failure["action"], "原子保存，失败则回滚")
            self.assertFalse(service.collection_records_path(_settings(db)).exists())

    def test_year_names_require_paired_brackets(self) -> None:
        for raw in ("[2024", "2024]", "[[2024]]", "2024\nextra"):
            with self.subTest(raw=raw):
                self.assertIsNone(service.collection_year_key_from_dirname(raw))
        self.assertEqual(service.collection_year_key_from_dirname("[199x]"), "199X")
        self.assertEqual(service.collection_year_key_from_dirname(" 2024 "), "2024")

    def test_quick_successive_saves_keep_distinct_recovery_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, finish = root / "DB", root / "Finish"
            db.mkdir()
            finish.mkdir()
            with patch.dict(os.environ, {"JP_TV_COLLECTION_FINISH_DIR": str(finish)}):
                first = save_collection_records_from_ui_body({"record": {"completed_years": ["2024"]}}, settings=_settings(db))
                target = Path(first["path"])
                first_bytes = target.read_bytes()
                second = save_collection_records_from_ui_body({"record": {"completed_years": ["2025"]}}, settings=_settings(db))
                second_bytes = target.read_bytes()
                third = save_collection_records_from_ui_body({"record": {"completed_years": ["2026"]}}, settings=_settings(db))
            history = service.collection_records_history_root(_settings(db))
            self.assertNotEqual(second["history_file"], third["history_file"])
            self.assertEqual((history / second["history_file"]).read_bytes(), first_bytes)
            self.assertEqual((history / third["history_file"]).read_bytes(), second_bytes)

    def test_failed_replace_keeps_collection_records_readable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, finish = root / "DB", root / "Finish"
            db.mkdir()
            finish.mkdir()
            with patch.dict(os.environ, {"JP_TV_COLLECTION_FINISH_DIR": str(finish)}):
                result = save_collection_records_from_ui_body({"record": {"completed_years": ["2024"]}}, settings=_settings(db))
                target = Path(result["path"])
                original = target.read_bytes()
                with patch.object(persistence, "atomic_write_bytes", side_effect=OSError("disk failure")):
                    with self.assertRaisesRegex(OSError, "disk failure"):
                        save_collection_records_from_ui_body({"record": {"completed_years": ["2025"]}}, settings=_settings(db))
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(collection_records_payload(_settings(db))["records"][0]["completed_years"], ["2024"])

    def test_relative_feature_paths_are_resolved_under_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (
                patch.dict(os.environ, {"JP_TV_COLLECTION_INFO_PATH": "", "JP_TV_COLLECTION_RECORDS_PATH": "", "JP_TV_COLLECTION_FINISH_DIR": ""}),
                patch("work_catalog_yaml.layout.workspace_root", return_value=root),
                patch.object(service, "_use_workspace_collection_info_config", return_value=True),
                patch.object(service, "_feature_config_paths", return_value={"database_path": "data/records.yaml", "history_root": "data/history", "finish_dir": "media/Finish"}),
            ):
                self.assertEqual(service.collection_records_path(_settings(root / "DB")), root / "data" / "records.yaml")
                self.assertEqual(service.collection_records_history_root(_settings(root / "DB")), root / "data" / "history")
                self.assertEqual(service.collection_finish_dir(), root / "media" / "Finish")

    def test_scan_finish_years_uses_finish_directory_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("[198X]", "[1990]", "[197X]", "notes"):
                (root / name).mkdir()
            (root / "[2025]").write_text("not a directory", encoding="utf-8")

            years = scan_finish_years(root)

            self.assertEqual([it["key"] for it in years], ["197X", "198X", "1990"])
            self.assertEqual([it["label"] for it in years], ["[197X]", "[198X]", "[1990]"])

    def test_save_and_load_collection_records_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td)
            db = data_root / "DB"
            finish = data_root / "Finish"
            db.mkdir()
            finish.mkdir()
            for name in ("[2025]", "[2024]"):
                (finish / name).mkdir()

            with patch.dict(os.environ, {"JP_TV_COLLECTION_FINISH_DIR": str(finish)}):
                result = save_collection_records_from_ui_body(
                    {
                        "records": [
                            {
                                "domain": "animation",
                                "country": "japan",
                                "release_type": "tv",
                                "completed_years": ["2025", "[2024]", "2023"],
                            },
                        ],
                    },
                    settings=_settings(db),
                )

                target = data_root / "CollectionInfo" / "collection-info.yaml"
                self.assertEqual(Path(result["path"]), target.resolve())
                raw = load_yaml_string(target.read_text(encoding="utf-8"))
                self.assertEqual(raw["records"][0]["completed_years"], ["2023", "2024", "2025"])

                payload = collection_records_payload(_settings(db))
                self.assertEqual(payload["records"][0]["completed_years"], ["2023", "2024", "2025"])

    def test_saved_years_survive_partial_and_offline_finish_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, finish = root / "DB", root / "Finish"
            db.mkdir()
            (finish / "[2024]").mkdir(parents=True)
            with patch.dict(os.environ, {"JP_TV_COLLECTION_FINISH_DIR": str(finish)}):
                result = save_collection_records_from_ui_body(
                    {"record": {"completed_years": ["2023", "2024", "invalid", "2023"]}},
                    settings=_settings(db),
                )
                self.assertEqual(result["records"][0]["completed_years"], ["2023", "2024"])
                for failure in (None, OSError("disk offline")):
                    with self.subTest(failure=failure), patch.object(
                        service, "scan_finish_years", side_effect=failure,
                        return_value=[{"key": "2024", "label": "[2024]", "path_name": "[2024]"}],
                    ):
                        loaded = collection_records_payload(_settings(db))
                        self.assertEqual(loaded["records"][0]["completed_years"], ["2023", "2024"])
                        saved = save_collection_records_from_ui_body({"records": loaded["records"]}, settings=_settings(db))
                        self.assertEqual(saved["records"], loaded["records"])

    def test_save_does_not_scan_finish_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            with patch.object(service, "scan_finish_years", side_effect=AssertionError("unnecessary disk scan")):
                result = save_collection_records_from_ui_body(
                    {"record": {"completed_years": ["2020"]}}, settings=_settings(db),
                )
            self.assertEqual(result["records"][0]["completed_years"], ["2020"])


if __name__ == "__main__":
    unittest.main()
