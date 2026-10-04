from __future__ import annotations

import json
import unittest

from work_catalog_yaml.common.operation_results import iter_result_details, summarize_result


class UnreadableRecords(list):
    def __iter__(self):
        raise AssertionError("DB records must not be inspected")

    def __getitem__(self, key):
        raise AssertionError("DB records must not be inspected")


class OrganizerResultProjectionTests(unittest.TestCase):
    def test_preview_projects_counts_and_issues_not_raw_draft(self):
        result = summarize_result({"plan": {"child": "A", "source_path": "S:/A", "target_path": "S:/NewA", "can_execute": False,
            "counts": {"files": 12, "moves": 10, "db_records": 2, "db_changes": 1, "blocking_issues": 1},
            "issues": [{"code": "name-required", "message": "作品名不能为空", "level": "error", "blocking": True}],
            "draft": {"records": [{"error": "PRIVATE_DRAFT"}]}, "files": [{"reason": "PRIVATE_FILES"}]}})
        self.assertIn("待移动 10", result["details"][0]["message"])
        issue = next(row for row in result["details"] if row.get("code") == "name-required")
        self.assertEqual((issue["object"], issue["source_path"], issue["level"]), ("A", "S:/A", "error"))
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_batch_preserves_exact_child_context_and_db_summary_without_reading_records(self):
        result = summarize_result({"results": [
            {"plan_id": "a", "child": "A", "status": "warning", "message": "媒体与DB完成", "moved": 7, "receipt_path": "logs/a.json",
             "database": {"db_committed": True, "records": UnreadableRecords([{"error": "PRIVATE_RECORD"}]),
                "writes": [{"path": "catalog/a.yaml", "record": "PRIVATE_WRITE"}],
                "issues": [{"code": "refresh", "message": "刷新失败", "reason": "Access denied", "level": "warning"}]},
             "issues": [{"code": "empty-cleanup", "message": "空目录清理失败", "path": "S:/A/old", "reason": "仍有文件"}]},
            {"plan_id": "b", "child": "B", "status": "failed", "message": "媒体移动失败", "db_committed": False,
             "receipt_path": "logs/b.json", "issues": [{"message": "回滚目标被占用", "level": "error"}]},
            {"plan_id": "c", "child": "C", "status": "skipped", "message": "前一目录失败，未执行"}
        ]}, context={"object": "WRONG_LAST_CHILD", "source_path": "S:/WRONG"})
        header = next(row for row in result["details"] if row.get("object") == "A" and row.get("stage") == "目录整理结果")
        for value in ("已移动 7 个文件", "DB 已提交", "返回作品记录 1 项", "写入数据库文件 1 项"):
            self.assertIn(value, header["message"])
        self.assertEqual(header["receipt_path"], "logs/a.json")
        self.assertIs(header["db_committed"], True)
        self.assertEqual(header["db_record_count"], 1)
        self.assertTrue(any(row.get("object") == "A" and row.get("path") == "catalog/a.yaml" for row in result["details"]))
        self.assertEqual(next(row for row in result["details"] if row.get("code") == "refresh")["object"], "A")
        self.assertEqual(next(row for row in result["details"] if row.get("code") == "empty-cleanup")["reason"], "仍有文件")
        self.assertTrue(any(row.get("object") == "B" and row["level"] == "error" and row.get("receipt_path") == "logs/b.json" for row in result["details"]))
        self.assertTrue(any(row.get("object") == "C" and row["level"] == "warning" for row in result["details"]))
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertNotIn("WRONG", json.dumps(result))

    def test_shortcut_preview_and_direct_result_remain_safe_preflight_entries(self):
        preview = {"incremental": True, "plan_id": "shortcut", "total": 2, "creatable": 1, "already_exists_count": 0, "conflict_count": 1,
            "conflicts": [{"code": "shortcut-existing-conflict", "error": "目标不一致", "shortcut_path": "links/a.lnk", "target_path": "S:/A"}],
            "items": [{"status": "conflict", "shortcut_path": "links/a.lnk", "target_path": "S:/A", "actual_target": "S:/Other", "file_sha256": "PRIVATE_HASH", "record": "PRIVATE_RECORD"}],
            "catalog_bindings": {"records": [{"message": "PRIVATE_BINDING"}]}}
        for payload in ({"shortcut_preview": preview}, {"results": [{"plan_id": "a", "child": "A", "status": "succeeded", "message": "完成", "shortcut_preview": preview}]}, {**preview, "created": 1, "failed_count": 0}):
            with self.subTest(payload=list(payload)):
                result = summarize_result(payload)
                self.assertTrue(any("冲突 1" in row["message"] for row in result["details"]))
                self.assertTrue(any(row.get("code") == "shortcut-existing-conflict" and "目标不一致" in row["message"] for row in result["details"]))
                item = next(row for row in result["details"] if row["message"].startswith("快捷方式预检条目"))
                self.assertEqual((item["actual_target_path"], item["status"]), ("S:/Other", "conflict"))
                self.assertNotIn("PRIVATE_", json.dumps(result))
                if payload.get("created"):
                    self.assertTrue(any(row.get("stage") == "快捷方式结果" and "已创建 1" in row["message"] for row in result["details"]))

    def test_explicit_limits_apply_to_durable_details_and_panel(self):
        payload = {"results": [{"plan_id": str(i), "child": f"child-{i}", "status": "succeeded", "message": f"child-{i}"} for i in range(80)]}
        details = list(iter_result_details(payload))
        self.assertEqual(len(details), 65)
        self.assertIn("未展开其余 16 个目录", details[-1]["message"])
        self.assertFalse(any(row.get("object") == "child-64" for row in details))
        payload = {"plan": {"child": "A", "counts": {}, "issues": [{"message": f"issue-{i}"} for i in range(300)]}}
        details = list(iter_result_details(payload))
        self.assertEqual(len(details), 202)
        self.assertIn("未展开其余 100 项", details[-1]["message"])
        result = summarize_result(payload)
        self.assertEqual(len(result["details"]), 80)
        self.assertEqual(result["truncated"], 122)

    def test_unrelated_payloads_and_malformed_values_are_not_recursed(self):
        for payload in (
            {"results": [{"message": "PRIVATE_GENERIC"}], "plan": {"issues": [{"message": "PRIVATE_PLAN"}]}, "database": {"records": [{"error": "PRIVATE_DB"}]}},
            {"results": [None, [], {"child": "A", "plan_id": "x", "status": {}}], "plan": [], "shortcut_preview": []},
        ):
            self.assertEqual(summarize_result(payload)["details"], [])

    def test_existing_feature_projections_remain_compatible(self):
        result = summarize_result({"file_generation": {"created": 2, "failed": [{"error": "创建失败", "shortcut_path": "links/b.lnk"}]}, "writes": [{"path": "db.yaml"}]})
        self.assertTrue(any(row["label"] == "文件生成·已创建" and row["value"] == 2 for row in result["counters"]))
        self.assertTrue(any(row.get("path") == "links/b.lnk" and row["level"] == "error" for row in result["details"]))
        self.assertTrue(any(row.get("path") == "db.yaml" for row in result["details"]))

    def test_unknown_db_protection_flag_is_not_presented_as_proven_save(self):
        result = summarize_result({"results": [{"plan_id": "a", "child": "A", "status": "warning", "db_committed": True,
            "message": "DB 写入状态无法确认，未自动回滚媒体", "receipt_path": "logs/a.json"}]})
        row = result["details"][0]
        self.assertIn("无法确认", row["message"])
        self.assertNotIn("db_committed", row)
        self.assertEqual(row["receipt_path"], "logs/a.json")


if __name__ == "__main__":
    unittest.main()
