from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from work_catalog_yaml.common.http import error_response


class CommonHttpTest(unittest.TestCase):
    def test_explicit_diagnostics_are_sanitized_without_losing_the_original_error(self):
        context = {"stage": "写入", "object": "作品A", "target_path": "U:/A", "token": "NEVER-LOG", "rows": []}
        with patch("work_catalog_yaml.operation_progress.report_exception") as report:
            try:
                raise OSError("磁盘只读")
            except OSError as exc:
                original = exc
                response = error_response("写入失败", status_code=500, context=context)
        payload = json.loads(response.body)
        self.assertEqual(payload["context"], {"stage": "写入", "object": "作品A", "target_path": "U:/A"})
        self.assertEqual(payload["error"], "写入失败")
        report.assert_called_once_with(original, context=context)

    def test_validation_failure_preserves_contract_without_fake_exception(self):
        with patch("work_catalog_yaml.operation_progress.report_exception") as report:
            response = error_response("请选择文件", status_code=400)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"ok": False, "error": "请选择文件"})
        report.assert_not_called()

    def test_caught_error_keeps_specific_status_code_and_optional_response_fields(self):
        original = PermissionError("文件只读")
        with patch("work_catalog_yaml.operation_progress.report_exception") as report:
            try:
                raise original
            except PermissionError as exception:
                response = error_response(exception, status_code=403, code="write-denied", reload_required=True)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.body), {
            "ok": False, "error": "文件只读", "code": "write-denied", "reload_required": True,
        })
        report.assert_called_once_with(original)

    def test_public_message_does_not_replace_the_original_exception(self):
        original = OSError("具体的磁盘错误")
        with patch("work_catalog_yaml.operation_progress.report_exception") as report:
            try:
                raise original
            except OSError:
                response = error_response("保存失败，请查看处理详情", status_code=500, ok=True)
        self.assertFalse(json.loads(response.body)["ok"])
        self.assertEqual(json.loads(response.body)["error"], "保存失败，请查看处理详情")
        report.assert_called_once_with(original)


if __name__ == "__main__":
    unittest.main()
