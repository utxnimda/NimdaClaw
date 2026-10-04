from __future__ import annotations

import logging
import unittest

from work_catalog_yaml.common.log_format import UnifiedLogFormatter, format_log_lines, format_operation_entry


class LogFormatTest(unittest.TestCase):
    def test_every_multiline_message_and_traceback_line_has_timestamp_and_level(self):
        entry = {"operation": {"id": "test", "title": "测试", "status": "failed", "updated_at": "2026-10-03T01:02:03.004Z"},
                 "event": {"message": "处理失败\n第二行", "detail": "其他信息"}, "traceback": "Traceback:\n  中文堆栈\nValueError: 错误"}
        lines = format_operation_entry(entry).splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertRegex(line, r"^\[2026-10-03 \d{2}:02:03\.004\]\[ERROR\] ")

    def test_all_diagnostic_context_fields_are_readable_not_json(self):
        context = {"stage": "扫描", "action": "匹配", "object": "压制目录", "source_path": "源目录", "target_path": "目标目录",
                   "name": "作品", "work_key": "work-1", "press_key": "press-1", "press_path": "_BDRip",
                   "yaml_source_rel": "2005.yaml", "index_in_file": 0, "expected": True, "actual": False,
                   "error_type": "ValueError", "reason": "目标错误", "location": {"file": "scan.py", "line": 42, "function": "scan"}}
        event = {"event": {"message": "发现问题", "context": context}, "operation": {"status": "warning"}}
        text = format_operation_entry(event)
        for expected in ("[WARN]", "阶段：扫描", "动作：匹配", "压制记录标识：press-1", "文件内记录索引：0", "预期：是", "实际：否",
                         "数据库文件：2005.yaml", "代码位置：文件：scan.py，行：42，函数：scan", "原因：目标错误"):
            self.assertIn(expected, text)
        self.assertNotIn('"stage":', text)
        detail = format_operation_entry({"kind": "result_detail", "detail": {"level": "warning", "message": "明细", **context}})
        self.assertIn("来源路径：源目录", detail)
        self.assertIn("目标路径：目标目录", detail)

    def test_application_formatter_uses_same_format_and_normalizes_warning(self):
        record = logging.LogRecord("nimda.example", logging.WARNING, "example.py", 2, "第一行\n第二行", (), None)
        output = UnifiedLogFormatter().format(record)
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("][WARN] " in line for line in lines))
        self.assertIn("nimda.example: 第一行", lines[0])
        self.assertNotIn("WARNING", output)

    def test_zero_and_false_are_not_dropped(self):
        text = format_operation_entry({"kind": "result_detail", "detail": {"message": "检查", "actual": 0, "expected": False}})
        self.assertIn("实际：0", text)
        self.assertIn("预期：否", text)
        self.assertEqual(len(format_log_lines("a\nb").splitlines()), 2)
        self.assertIn("][DEBUG]", format_log_lines("debug", level="debug"))
        self.assertIn("][TRACE]", format_log_lines("trace", level="TRACE"))


if __name__ == "__main__":
    unittest.main()
