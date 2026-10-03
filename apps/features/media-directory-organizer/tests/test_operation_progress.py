from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from collection_detail import link_index
from collection_info import service as collection_info
from media_directory_organizer import bangumi, execution, service
from media_directory_organizer.catalog import CatalogWork, MediaCatalog, PressRecord
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.operation_progress import OperationRegistry, execute_operation


class FeatureOperationProgressTest(unittest.TestCase):
    def _fixture(self, base: Path, count: int = 2):
        root = base / "Sample"
        source = root / "Sample [BDRip][VCB]"
        source.mkdir(parents=True)
        for episode in range(1, count + 1):
            (source / f"Sample {episode:03}.mkv").write_bytes(b"fixture")
        catalog = MediaCatalog(
            works=(CatalogWork(
                name="Sample", path=str(root), domain="animation", country="japan",
                release_type="tv", presses=(PressRecord("BDRip", "VCB", "Sample_BDRip"),),
                source_file=str(base / "db.yaml"),
            ),),
            catalog_root=base,
        )
        settings = OrganizerSettings(
            catalog_root=base, allowed_resource_roots=(base,),
            format_markers={"BDRip": ("bdrip",)}, group_markers={"VCB": ("vcb",)},
            group_suffixes={},
        )
        return root, source, catalog, settings

    def _run(self, function, *args, **kwargs):
        registry = OperationRegistry()
        operation = registry.register(str(uuid4()), "隔离进度测试")
        result = execute_operation(registry, operation, function, *args, **kwargs)
        snapshot = registry.snapshot(operation.id)
        self.assertEqual(snapshot["status"], "succeeded")
        return result, snapshot["events"]

    def test_preview_reports_real_scan_count_without_mutating_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source, catalog, settings = self._fixture(Path(temporary), count=201)
            with patch.object(service, "report_progress") as reporter:
                plan = service.build_plan(root, catalog=catalog, settings=settings)
            self.assertEqual(len(list(source.iterdir())), 201)
            self.assertEqual(plan["summary"]["scanned_file_count"], 201)
            scans = [call for call in reporter.call_args_list if call.args[0] == "扫描待整理文件"]
            self.assertEqual([call.kwargs["completed"] for call in scans], [0, 100, 200])
            last = reporter.call_args_list[-1]
            self.assertEqual(last.kwargs["completed"], 201)
            self.assertIn("尚未移动", last.args[0])

    def test_confirmed_move_reports_validation_move_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source, catalog, settings = self._fixture(Path(temporary))
            plan = service.build_plan(root, catalog=catalog, settings=settings)
            result, events = self._run(execution.apply_plan, plan, confirmation=plan["plan_id"])
            self.assertEqual(result["moved_file_count"], 2)
            self.assertFalse(source.exists())
            messages = [event["message"] for event in events]
            for message in ("重新校验预览后的文件状态", "移动已确认的媒体文件", "媒体文件移动完成", "空目录清理结束"):
                self.assertIn(message, messages)
            done = next(event for event in events if event["message"] == "媒体文件移动完成")
            self.assertEqual((done["completed"], done["total"]), (2, 2))

    def test_move_failure_reports_rollback_and_preserves_original_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source, catalog, settings = self._fixture(Path(temporary))
            plan = service.build_plan(root, catalog=catalog, settings=settings)
            original_move = execution._move_file_no_replace
            calls = 0

            def fail_second(source_path, target_path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated failure")
                original_move(source_path, target_path)

            registry = OperationRegistry()
            operation = registry.register(str(uuid4()), "回滚测试")
            with patch.object(execution, "_move_file_no_replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "simulated failure"):
                    execute_operation(registry, operation, execution.apply_plan, plan, confirmation=plan["plan_id"])
            snapshot = registry.snapshot(operation.id)
            self.assertEqual(snapshot["status"], "failed")
            rollback = next(event for event in snapshot["events"] if event["message"] == "媒体回滚阶段结束")
            self.assertEqual((rollback["completed"], rollback["total"]), (1, 1))
            self.assertEqual(len(list(source.iterdir())), 2)
            self.assertTrue(all(not Path(move["target"]).exists() for move in plan["moves"]))

    def test_progress_does_not_bypass_move_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, source, catalog, settings = self._fixture(Path(temporary))
            plan = service.build_plan(root, catalog=catalog, settings=settings)
            with patch.object(execution, "report_progress") as reporter:
                with self.assertRaisesRegex(ValueError, "确认码不匹配"):
                    execution.apply_plan(plan, confirmation="wrong")
            reporter.assert_not_called()
            self.assertEqual(len(list(source.iterdir())), 2)

    def test_resource_scan_reports_actual_root_file_totals_and_cache_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            media = base / "media"
            (media / "Series" / "BDRip").mkdir(parents=True)
            (media / "Series" / "BDRip" / "01.mkv").write_bytes(b"fixture")
            config = {"paths": {"resource_roots": [str(media)], "shortcut_root": str(base / "shortcuts")}}
            with patch.object(link_index, "_feature_config", return_value=config), patch.object(link_index, "feature_data_root", return_value=base / "data"):
                result, events = self._run(link_index.scan_resource_libraries_payload)
            self.assertEqual(result["summary"]["file_count"], 1)
            root_done = next(event for event in events if event["message"] == "资源根目录扫描完成")
            self.assertEqual((root_done["completed"], root_done["total"]), (1, 1))
            self.assertIn("1 个文件", root_done["detail"])
            self.assertTrue(any(event["message"] == "资源库扫描缓存已保存" for event in events))

    def test_bangumi_reports_external_wait_and_candidate_parsing(self):
        with patch.object(bangumi, "_read_payload", return_value={"data": [], "total": 0}):
            result, events = self._run(bangumi.search_bangumi_anime, "Fixture")
        self.assertEqual(result["candidates"], [])
        messages = [event["message"] for event in events]
        self.assertIn("正在请求 Bangumi 作品搜索", messages)
        self.assertIn("解析 Bangumi 返回结果并计算匹配度", messages)
        self.assertIn("Bangumi 候选已生成，等待手动确认", messages)

    def test_collection_year_scan_reports_path_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "[2026]").mkdir()
            result, events = self._run(collection_info.scan_finish_years, base)
            self.assertEqual(result[0]["key"], "2026")
            self.assertTrue(any(event["detail"] == str(base) for event in events))
            self.assertEqual(len(list(base.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
