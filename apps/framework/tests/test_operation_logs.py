from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from work_catalog_yaml.common.operation_logs import OperationLogStore
from work_catalog_yaml.common.operation_results import summarize_result
from work_catalog_yaml.operation_progress import DuplicateOperationId, OperationRegistry, execute_operation, report_exception, report_progress
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from work_catalog_yaml.jp_tv import browse_api


async def request(app, url, *, method="GET", body=b"", headers=()):
    parts = urlsplit(url)
    sent = False
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": parts.path, "raw_path": parts.path.encode(), "root_path": "",
               "query_string": parts.query.encode(), "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345),
               "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"), *headers]}, receive, send)
    response = next(item for item in messages if item["type"] == "http.response.start")
    content = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return response["status"], json.loads(content), dict(response["headers"])


class OperationLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "logs"
        self.store = OperationLogStore(self.root)
        self.registry = OperationRegistry(log_store=self.store, max_completed=1, max_events=3)

    def run_job(self, payload=None):
        row = self.registry.register(str(uuid4()), "测试处理")
        execute_operation(self.registry, row, lambda: payload or {"ok": True})
        return row

    def test_log_records_survive_eviction_and_restart(self):
        first = self.run_job({"ok": True, "summary": {"created": 2}})
        second = self.run_job()
        self.assertNotIn(first.id, self.registry._operations)
        self.assertEqual(self.registry.snapshot(first.id)["status"], "succeeded")
        restarted = OperationRegistry(log_store=OperationLogStore(self.root))
        restored = restarted.snapshot(first.id)
        self.assertEqual(restored["title"], "测试处理")
        self.assertEqual(restored["result"]["counters"], [{"label": "汇总·已创建", "value": 2}])
        self.assertTrue(restored["log"]["available"])
        self.assertEqual(len(restored["events"]), 3)
        with self.assertRaises(DuplicateOperationId):
            restarted.register(first.id, "must not overwrite")
        history, cursor = restarted.history(limit=1)
        self.assertEqual(history[0]["id"], second.id)
        older, done = restarted.history(limit=1, before=cursor)
        self.assertEqual(older[0]["id"], first.id)
        self.assertIsNone(done)

    def test_incomplete_previous_session_does_not_pretend_success(self):
        row = self.registry.register(str(uuid4()), "interrupted")
        self.registry.start(row)
        self.assertEqual(self.store.snapshot(row.id)["status"], "running")
        restored = OperationLogStore(self.root).snapshot(row.id)
        self.assertTrue(restored["interrupted"])
        self.assertEqual(restored["status"], "warning")
        self.assertIn("核对", restored["message"])

    def test_full_diagnostics_persist_beyond_bounded_summary_without_business_data(self):
        errors = [{"message": f"bad-{i}", "path": f"test-{i}.lnk", "token": "NEVER-LOG"} for i in range(1005)]
        row = self.run_job({"ok": True, "errors": errors, "rows": [{"secret": "NEVER-LOG"}],
                            "data": {"token": "NEVER-LOG"}, "tree": {"path": "NEVER-LOG"}})
        snapshot = self.registry.snapshot(row.id)
        self.assertEqual(len(snapshot["result"]["details"]), 80)
        self.assertEqual(snapshot["result"]["truncated"], 925)
        content = self.store.find(row.id).read_text(encoding="utf-8")
        entries = [json.loads(line) for line in content.splitlines()]
        details = [entry["detail"] for entry in entries if entry.get("kind") == "result_detail"]
        self.assertEqual(len(details), 1005)
        self.assertEqual(details[-1]["message"], "bad-1004")
        self.assertNotIn("NEVER-LOG", content)
        human = Path(snapshot["log"]["path"]).read_text(encoding="utf-8")
        self.assertIn("bad-1004", human)
        self.assertNotIn("NEVER-LOG", human)
        self.assertTrue(snapshot["log"]["path"].endswith(".log"))
        self.assertEqual(OperationLogStore(self.root).snapshot(row.id)["status"], "warning")

    def test_caught_and_uncaught_exceptions_include_traceback(self):
        caught = self.registry.register(str(uuid4()), "caught")

        def worker():
            try:
                raise OSError("write denied")
            except OSError as exc:
                report_exception(exc)
                return {"ok": False, "error": str(exc)}

        execute_operation(self.registry, caught, worker)
        text = self.store.read(caught.id)["content"]
        self.assertIn("Traceback", text)
        self.assertIn("OSError: write denied", text)
        row = self.registry.register(str(uuid4()), "uncaught")
        with self.assertRaises(RuntimeError):
            execute_operation(self.registry, row, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertIn("RuntimeError: boom", self.store.read(row.id)["content"])

    def test_interrupted_large_diagnostic_stream_remains_discoverable(self):
        row = self.registry.register(str(uuid4()), "large interrupted")
        self.registry.start(row)
        self.store.append_details(row.id, ({"level": "error", "message": "x" * 1024} for _ in range(1500)))
        restored = OperationLogStore(self.root).snapshot(row.id)
        self.assertEqual(restored["id"], row.id)
        self.assertTrue(restored["interrupted"])

    def test_write_failure_degrades_without_breaking_service(self):
        row = self.registry.register(str(uuid4()), "failed log")
        with patch.object(self.store, "append", side_effect=OSError("disk full")):
            result = execute_operation(self.registry, row, lambda: {"ok": True})
        self.assertTrue(result["ok"])
        snapshot = self.registry.snapshot(row.id)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertIn("disk full", snapshot["log"]["error"])
        self.assertTrue(snapshot["log"]["available"])

    def test_create_failure_is_visible_and_keeps_memory_bounded(self):
        with patch.object(self.store, "create", side_effect=PermissionError("denied")):
            first = self.run_job()
            second = self.run_job()
        self.assertNotIn(first.id, self.registry._operations)
        snapshot = self.registry.snapshot(second.id)
        self.assertFalse(snapshot["log"]["available"])
        self.assertIn("denied", snapshot["log"]["error"])

    def test_chunked_utf8_reads_and_invalid_offsets(self):
        row = self.run_job({"ok": False, "error": "中文错误"})
        chunks, offset = [], 0
        while True:
            part = self.store.read(row.id, offset=offset, limit=17)
            chunks.append(part["content"])
            offset = part["next_offset"]
            if part["eof"]:
                break
        self.assertEqual("".join(chunks), self.store.safe_log_path(row.id).read_text(encoding="utf-8"))
        for params in ({"offset": -1}, {"offset": offset + 1}, {"limit": 0}, {"limit": 262145}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.store.read(row.id, **params)
        with self.assertRaises(ValueError):
            self.store.history(before="../../private")

    def test_root_and_file_reparse_chains_are_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            self.root.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("host cannot create symlinks")
        with self.assertRaises(OSError):
            self.store.create(str(uuid4()))
        self.assertEqual(list(outside.iterdir()), [])

    def test_hardlinked_log_is_not_read_or_overwritten(self):
        row = self.run_job()
        log = self.store.find(row.id)
        alias = Path(self.temp.name) / "alias.jsonl"
        try:
            os.link(log, alias)
        except OSError:
            self.skipTest("host cannot create hardlinks")
        with self.assertRaises(OSError):
            self.store.read(row.id)
        with self.assertRaises(OSError):
            self.store.append(row.id, {}, {})

    def test_projection_preserves_index_targets_and_chinese_counters(self):
        result = summarize_result({"plan_summary": {"ready": 3, "unmapped_on_disk": 2},
            "unmapped_shortcuts": [{"shortcut_path": "a.lnk", "target_path": "D:/a", "target_exists": False, "target_error": "不存在"}]})
        self.assertEqual(result["details"][0]["path"], "a.lnk")
        self.assertIn("D:/a", result["details"][0]["message"])
        self.assertIn("目标不存在", result["details"][0]["message"])
        self.assertEqual({item["label"] for item in result["counters"]}, {"计划·可处理", "计划·未关联快捷方式"})

    def test_registration_and_lookup_do_not_rescan_complete_history(self):
        first = self.run_job()
        with patch.object(self.store, "_files", side_effect=AssertionError("must not scan historical files")):
            for _ in range(12):
                self.run_job()
        restarted = OperationLogStore(self.root)
        with patch.object(restarted, "_files", side_effect=AssertionError("must use persistent UUID index")):
            self.assertEqual(restarted.snapshot(first.id)["id"], first.id)
            OperationRegistry(log_store=restarted).register(str(uuid4()), "new after restart")
        for _ in range(300):
            self.store._remember(str(uuid4()), self.root / "unused")
        self.assertLessEqual(len(self.store._paths), 256)

    def test_first_history_page_does_not_scan_older_dates(self):
        for _ in range(3):
            self.run_job()
        old = self.root / "2000-01-01"
        old.mkdir()
        original = os.scandir
        def check(path):
            self.assertNotEqual(Path(path), old)
            return original(path)
        with patch("work_catalog_yaml.common.operation_logs.os.scandir", side_effect=check):
            rows, cursor = self.store.history(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(cursor)

    def test_legacy_jsonl_gets_real_readable_log_without_changing_sidecar(self):
        row = self.run_job({"ok": False, "error": "中文错误"})
        machine = self.store.find(row.id)
        original = machine.read_bytes()
        readable = self.store.safe_log_path(row.id)
        readable.unlink()  # Only this test's generated temporary projection.
        restarted = OperationLogStore(self.root)
        result = restarted.read(row.id)
        self.assertEqual(result["path"], str(readable))
        self.assertTrue(readable.is_file())
        self.assertRegex(result["content"], r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\]\[ERROR\]")
        self.assertIn("中文错误", result["content"])
        self.assertNotIn('"schema":', result["content"])
        self.assertEqual(machine.read_bytes(), original)
        self.assertEqual(restarted.snapshot(row.id)["log"]["path"], str(readable))

    def test_readable_hardlink_is_rejected_before_regeneration(self):
        row = self.run_job()
        readable = self.store.safe_log_path(row.id)
        alias = Path(self.temp.name) / "readable-alias.log"
        os.link(readable, alias)
        original = alias.read_bytes()
        with self.assertRaises(OSError):
            OperationLogStore(self.root).safe_log_path(row.id)
        self.assertEqual(alias.read_bytes(), original)

    def test_full_file_search_literal_unicode_pages_and_true_line_numbers(self):
        row = self.run_job()
        self.store.append_details(row.id, ({"level": "info", "message": f"中文 Needle [a.*] {index}"} for index in range(135)))
        first_read = self.store.read(row.id, limit=100)
        self.assertNotIn("Needle", first_read["content"])
        found, offset = [], 0
        while True:
            result = self.store.search(row.id, "中文 needle [a.*]", offset=offset, limit=17)
            found.extend(result["matches"])
            self.assertGreaterEqual(result["next_offset"], offset)
            offset = result["next_offset"]
            if result["eof"]:
                break
        self.assertEqual(len(found), 135)
        lines = self.store.safe_log_path(row.id).read_text(encoding="utf-8").splitlines()
        for match in found:
            self.assertEqual(match["text"], lines[match["line"] - 1])
        self.assertEqual(self.store.search(row.id, "[a.+]")["matches"], [])
        for query in ("", "\n", "x" * 513):
            with self.assertRaises(ValueError):
                self.store.search(row.id, query)

    def test_search_unicode_chunk_boundary_and_very_long_line_is_one_match(self):
        from work_catalog_yaml.common.log_format import format_log_lines
        row = self.run_job()
        prefix_bytes = len(format_log_lines("", at="2026-10-03T00:00:00Z").encode("utf-8")) - 1
        text = "a" * (65536 - prefix_bytes - 2) + "猫咪needle" + "b" * 90000 + "猫咪needle"
        self.store.append_details(row.id, [{"level": "info", "message": text}])
        result = self.store.search(row.id, "猫咪needle", limit=1)
        self.assertEqual(len(result["matches"]), 1)
        self.assertIn("猫咪needle", result["matches"][0]["text"])
        if not result["eof"]:
            rest = self.store.search(row.id, "猫咪needle", offset=result["next_offset"], limit=1)
            self.assertEqual(rest["matches"], [])

    def test_search_scan_budget_can_continue_beyond_eight_megabytes(self):
        row = self.run_job()
        self.store.append_details(row.id, ({"level": "info", "message": "x" * 10000} for _ in range(900)))
        self.store.append_details(row.id, [{"level": "info", "message": "最后的匹配"}])
        first = self.store.search(row.id, "最后的匹配")
        self.assertFalse(first["eof"])
        self.assertEqual(first["matches"], [])
        self.assertLessEqual(first["next_offset"], 8 * 1024 * 1024 + 3)
        second = self.store.search(row.id, "最后的匹配", offset=first["next_offset"])
        self.assertTrue(second["eof"])
        self.assertEqual(len(second["matches"]), 1)

    def test_unicode_casefold_expansion_keeps_match_in_visible_excerpt(self):
        row = self.run_job()
        self.store.append_details(row.id, [{"level": "info", "message": "ß" * 10000 + "Straße 中文目标"}])
        result = self.store.search(row.id, "STRASSE 中文目标")
        self.assertEqual(len(result["matches"]), 1)
        self.assertIn("Straße 中文目标", result["matches"][0]["text"])

    def test_open_text_file_returns_verified_utf8_log_handle(self):
        row = self.run_job()
        path, stream = self.store.open_text_file(row.id)
        try:
            self.assertEqual(path.suffix, ".log")
            self.assertEqual(stream.tell(), 0)
            self.assertIn("测试处理", stream.read().decode("utf-8"))
        finally:
            stream.close()

    def test_readable_projection_recovers_interrupted_dual_write(self):
        row = self.run_job()
        machine = self.store.find(row.id)
        payload = {"kind": "result_detail", "at": "2026-10-03T00:00:00Z", "detail": {"level": "error", "message": "遗漏的人类可读记录"}}
        with self.store._open(machine, append=True) as stream:
            stream.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        result = self.store.read(row.id)
        self.assertIn("遗漏的人类可读记录", result["content"])


class OperationLogApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "logs"
        self.app = build_jp_tv_browse_app(operation_log_root=self.root)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.temp.cleanup()

    async def test_invalid_json_has_durable_operation_without_logging_body(self):
        operation_id = str(uuid4())
        status, _, headers = await request(self.app, "/api/browse/save", method="POST", body=b"SECRET-BROKEN",
            headers=[(b"x-nimda-operation-id", operation_id.encode())])
        self.assertEqual(status, 400)
        self.assertEqual(headers[b"x-nimda-operation-id"].decode(), operation_id)
        status, payload, _ = await request(self.app, f"/api/operations/{operation_id}/log")
        self.assertEqual(status, 200)
        self.assertNotIn("SECRET-BROKEN", payload["log"]["content"])
        self.assertIn("请求体须为 JSON 对象", payload["log"]["content"])

    async def test_history_log_and_security_contracts(self):
        row = self.app.state.operation_registry.register(str(uuid4()), "sample")
        execute_operation(self.app.state.operation_registry, row, lambda: {"ok": True})
        status, payload, headers = await request(self.app, "/api/operations?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["operations"][0]["id"], row.id)
        self.assertIn(b"no-store", headers[b"cache-control"])
        for path in ("/api/operations", f"/api/operations/{row.id}/log"):
            self.assertEqual((await request(self.app, path, headers=[(b"origin", b"https://evil.example")]))[0], 403)
        for path in ("/api/operations?limit=0", "/api/operations?before=../escape", f"/api/operations/{row.id}/log?offset=-1"):
            self.assertEqual((await request(self.app, path))[0], 400)
        self.assertEqual((await request(self.app, f"/api/operations/{uuid4()}/log"))[0], 404)

    async def test_client_results_are_persisted_and_cannot_overwrite_server(self):
        operation_id = str(uuid4())
        body = {"id": operation_id, "title": "客户端校验", "path": "/api/test", "status": "failed", "message": "目录未填写",
                "details": "2026-10-03T10:00:00Z 验证开始\n2026-10-03T10:00:01Z 目录未填写"}
        status, payload, _ = await request(self.app, "/api/operations/client-events", method="POST", body=json.dumps(body).encode())
        self.assertEqual(status, 200)
        self.assertEqual(payload["operation"]["source"], "client")
        self.assertEqual(payload["operation"]["path"], "/api/test")
        self.assertTrue(payload["operation"]["log"]["available"])
        self.assertIn("验证开始", self.app.state.operation_registry.log_store.read(operation_id)["content"])
        self.assertEqual((await request(self.app, "/api/operations/client-events", method="POST", body=json.dumps(body).encode()))[0], 409)
        for field, value in (("path", "../../private"), ("path", "/api/test?token=secret"), ("status", "running"), ("details", "x" * 16385)):
            invalid = {**body, "id": str(uuid4()), field: value}
            self.assertEqual((await request(self.app, "/api/operations/client-events", method="POST", body=json.dumps(invalid).encode()))[0], 400)

    async def test_untracked_post_is_logged_and_caught_exception_keeps_stack(self):
        with patch.object(browse_api, "get_resolved_browse_settings", return_value=(None, None)), patch.object(
            browse_api, "save_link_index_from_ui_body", side_effect=PermissionError("denied")):
            status, _, headers = await request(self.app, "/api/collection-detail/link-index/save", method="POST", body=b"{}")
        self.assertEqual(status, 403)
        operation_id = headers[b"x-nimda-operation-id"].decode()
        snapshot = self.app.state.operation_registry.snapshot(operation_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("PermissionError: denied", self.app.state.operation_registry.log_store.read(operation_id)["content"])

    async def test_slow_log_registration_does_not_block_health(self):
        started, release = threading.Event(), threading.Event()
        store = self.app.state.operation_registry.log_store
        original = store.create
        def slow(operation_id):
            started.set()
            release.wait(3)
            return original(operation_id)
        with patch.object(store, "create", side_effect=slow):
            active = asyncio.create_task(request(self.app, "/api/browse/save", method="POST", body=b"broken"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertEqual((await asyncio.wait_for(request(self.app, "/api/health"), 0.5))[0], 200)
            finally:
                release.set()
                self.assertEqual((await active)[0], 400)


if __name__ == "__main__":
    unittest.main()
