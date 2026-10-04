from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from work_catalog_yaml.common.http import error_response
from work_catalog_yaml.common import operation_results
from work_catalog_yaml.common.operation_results import diagnostic_context, iter_result_details, summarize_result
from work_catalog_yaml.operation_progress import (
    OperationRegistry, execute_operation, operation_context, report_exception, report_progress,
)


class OperationContextTest(unittest.TestCase):
    def setUp(self):
        self.registry = OperationRegistry()

    def run_job(self, function, *args):
        row = self.registry.register(str(uuid4()), "/api/test-operation")
        execute_operation(self.registry, row, function, *args)
        return self.registry.snapshot(row.id)

    def test_nested_failure_preserves_object_stage_and_origin_after_context_exits(self):
        def write_shortcut():
            raise PermissionError("快捷方式目录只读")

        def worker():
            try:
                with operation_context(stage="创建快捷方式", action="增量补建", object="作品A", yaml_source_rel="2005.yaml"):
                    with operation_context(source_path="U:/作品A", target_path="E:/索引/作品A.lnk", index_in_file=0):
                        write_shortcut()
            except PermissionError as exc:
                return error_response(f"无法写入：{exc}", status_code=403)

        snapshot = self.run_job(worker)
        self.assertEqual(snapshot["status"], "failed")
        failure = snapshot["result"]["details"][0]
        self.assertEqual(failure["stage"], "创建快捷方式")
        self.assertEqual(failure["action"], "增量补建")
        self.assertEqual(failure["object"], "作品A")
        self.assertEqual(failure["source_path"], "U:/作品A")
        self.assertEqual(failure["target_path"], "E:/索引/作品A.lnk")
        self.assertEqual(failure["index_in_file"], 0)
        self.assertEqual(failure["yaml_source_rel"], "2005.yaml")
        self.assertEqual(failure["error_type"], "PermissionError")
        self.assertEqual(failure["reason"], "快捷方式目录只读")
        self.assertEqual(failure["location"]["function"], "write_shortcut")
        self.assertGreater(failure["location"]["line"], 0)
        self.assertEqual(failure["location"]["file"], __file__)
        event = next(event for event in snapshot["events"] if event["detail"] == "PermissionError")
        self.assertEqual(event["context"]["target_path"], failure["target_path"])

    def test_scope_exit_restores_parent_context_and_a_new_worker_has_no_old_identity(self):
        def worker():
            with operation_context(object="父对象", stage="外层"):
                with operation_context(object="子对象", stage="内层"):
                    report_progress("子处理", context={"actual": False, "expected": 0})
                report_progress("父处理")
            report_progress("范围外")
            return {"ok": True}

        snapshot = self.run_job(worker)
        child, parent, outside = snapshot["events"][2:5]
        self.assertEqual(child["context"]["object"], "子对象")
        self.assertIs(child["context"]["actual"], False)
        self.assertEqual(child["context"]["expected"], 0)
        self.assertEqual(parent["context"]["object"], "父对象")
        self.assertEqual(parent["context"]["stage"], "外层")
        self.assertNotIn("object", outside["context"])
        second = self.run_job(lambda: {"ok": True})
        self.assertNotIn("object", second["context"])
        self.assertNotIn("子对象", json.dumps(second, ensure_ascii=False))

    def test_worker_logs_only_top_level_operation_identity_not_request_contents(self):
        body = {"name": "作品A", "work_key": "key", "yaml_source_rel": "2005.yaml", "index_in_file": 0,
                "token": "NEVER-LOG", "rows": [{"name": "NEVER-LOG"}], "data": {"path": "NEVER-LOG"},
                "body": "NEVER-LOG", "actual": {"password": "NEVER-LOG"}}
        snapshot = self.run_job(lambda value: {"ok": False, "error": "处理失败"}, body)
        text = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("NEVER-LOG", text)
        self.assertEqual(snapshot["context"]["name"], "作品A")
        self.assertEqual(snapshot["context"]["work_key"], "key")
        self.assertEqual(snapshot["result"]["details"][0]["yaml_source_rel"], "2005.yaml")
        self.assertEqual(body["token"], "NEVER-LOG")

    def test_uncaught_failure_carries_last_reported_step_and_traceback_location(self):
        row = self.registry.register(str(uuid4()), "uncaught")

        def fail():
            report_progress("写入文件", context={"object": "对象B", "target_path": "U:/B", "code": "write"})
            raise OSError("磁盘已满")

        with self.assertRaisesRegex(OSError, "磁盘已满"):
            execute_operation(self.registry, row, fail)
        snapshot = self.registry.snapshot(row.id)
        failure = snapshot["result"]["details"][0]
        self.assertEqual(failure["stage"], "写入文件")
        self.assertEqual(failure["object"], "对象B")
        self.assertEqual(failure["location"]["function"], "fail")
        self.assertEqual(failure["reason"], "磁盘已满")
        self.assertEqual(failure["error_type"], "OSError")

    def test_caught_error_cannot_be_hidden_by_a_successful_response(self):
        def worker():
            try:
                with operation_context(stage="处理单项", object="对象C"):
                    raise ValueError("条目无效")
            except ValueError as exc:
                report_exception(exc, context={"code": "invalid-item"})
            return {"ok": True, "created": 2}

        snapshot = self.run_job(worker)
        self.assertEqual(snapshot["status"], "warning")
        failure = snapshot["result"]["details"][0]
        self.assertEqual(failure["object"], "对象C")
        self.assertEqual(failure["code"], "invalid-item")
        self.assertIn("条目无效", failure["reason"])

    def test_thread_contexts_do_not_mix_between_simultaneous_operations(self):
        barrier = threading.Barrier(2)

        def worker(name):
            with operation_context(stage="并发验证", object=name):
                barrier.wait(timeout=5)
                report_progress("读取对象")
                return {"ok": True, "warnings": [{"reason": "请核对", "name": name}]}

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda name: self.run_job(worker, name), ("对象甲", "对象乙")))
        for name, snapshot in zip(("对象甲", "对象乙"), results):
            self.assertEqual(snapshot["events"][2]["context"]["object"], name)
            self.assertEqual(snapshot["result"]["details"][0]["name"], name)


class DiagnosticProjectionTest(unittest.TestCase):
    def test_batch_issue_never_inherits_another_objects_last_progress_identity(self):
        result = summarize_result({"issues": [{"name": "作品A", "reason": "待核对", "path": "A.lnk"}]},
                                  context={"stage": "扫描", "object": "作品B", "source_path": "B.lnk", "work_key": "B"})
        detail = result["details"][0]
        self.assertEqual(detail["name"], "作品A")
        self.assertEqual(detail["stage"], "扫描")
        self.assertNotIn("object", detail)
        self.assertNotIn("source_path", detail)
        self.assertNotIn("work_key", detail)

    def test_detailed_fields_are_preserved_once_and_distinct_objects_are_not_merged(self):
        shared = {"message": "路径不匹配", "reason": "DB目标与快捷方式实际指向不同", "source_path": "db.yaml",
                  "target_path": "U:/目标", "work_key": "work", "yaml_source_rel": "2005.yaml", "name": "作品A",
                  "expected": "U:/预期", "actual": "U:/实际", "location": {"file": "service.py", "line": 17, "function": "resolve"}}
        payload = {"issues": [{**shared, "press_index": 0}, {**shared, "press_index": 1}]}
        before = json.dumps(payload, ensure_ascii=False)
        result = summarize_result(payload)
        self.assertEqual(len(result["details"]), 2)
        for field, value in shared.items():
            self.assertEqual(result["details"][0][field], value)
        self.assertEqual(result["details"], list(iter_result_details(payload)))
        self.assertEqual(json.dumps(payload, ensure_ascii=False), before)

    def test_projection_whitelist_drops_nested_business_data_and_nonfinite_numbers(self):
        detail = diagnostic_context({"object": Path("demo"), "phase": "查找", "press_path": "demo_BDRip", "operation_count": 0,
                                     "token": "secret", "actual": {"rows": ["secret"]}, "expected": float("inf"),
                                     "location": {"file": "a.py", "line": 3, "function": "open", "locals": {"secret": "value"}}})
        self.assertEqual(detail["object"], "demo")
        self.assertEqual(detail["stage"], "查找")
        self.assertEqual(detail["operation_count"], 0)
        self.assertNotIn("phase", detail)
        self.assertNotIn("token", detail)
        self.assertNotIn("actual", detail)
        self.assertNotIn("expected", detail)
        self.assertNotIn("locals", detail["location"])

    def test_finish_normalizes_each_diagnostic_once_for_log_and_panel(self):
        written = []
        store = Mock()
        store.create.return_value = Path("test-operation.log")
        store.append_details.side_effect = lambda _id, details: written.extend(details)
        registry = OperationRegistry(log_store=store)
        row = registry.register(str(uuid4()), "single-pass")
        payload = {"issues": [{"reason": f"问题{i}", "name": f"作品{i}", "actual": False} for i in range(100)]}
        with patch.object(operation_results, "_diagnostic", wraps=operation_results._diagnostic) as normalize:
            execute_operation(registry, row, lambda: payload)
        self.assertEqual(normalize.call_count, 100)
        result = registry.snapshot(row.id)["result"]
        self.assertEqual(result["details"], written[:80])
        self.assertEqual(result["truncated"], 20)
        self.assertEqual(len(written), 100)

    def test_partial_log_write_failure_does_not_drop_remaining_panel_diagnostics(self):
        store = Mock()
        store.create.return_value = Path("test-operation.log")

        def fail_after_one(_id, details):
            next(details)
            raise OSError("日志磁盘不可用")

        store.append_details.side_effect = fail_after_one
        registry = OperationRegistry(log_store=store)
        row = registry.register(str(uuid4()), "failed-log")
        execute_operation(registry, row, lambda: {"issues": [{"reason": f"问题{i}"} for i in range(100)]})
        snapshot = registry.snapshot(row.id)
        self.assertEqual(len(snapshot["result"]["details"]), 80)
        self.assertEqual(snapshot["result"]["truncated"], 20)
        self.assertIn("日志磁盘不可用", snapshot["log"]["error"])


if __name__ == "__main__":
    unittest.main()
