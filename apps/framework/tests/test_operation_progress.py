from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse

from work_catalog_yaml.api_runtime import ApiWorkQueue, _dispatch
from work_catalog_yaml.jp_tv import browse_api
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from work_catalog_yaml.operation_progress import (
    DuplicateOperationId, OperationRegistry, execute_operation, normalize_operation_id, report_progress,
)


async def request(app, path, *, method="GET", headers=(), body=b"{}"):
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"", "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"), *headers],
    }, receive, send)
    response = next(item for item in messages if item["type"] == "http.response.start")
    data = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return response["status"], json.loads(data), dict(response["headers"])


class ProgressRegistryTest(unittest.TestCase):
    def test_optional_reporter_can_be_imported_without_starlette(self):
        program = (
            "import builtins\n"
            "original_import = builtins.__import__\n"
            "def import_without_web(name, *args, **kwargs):\n"
            "    if name == 'starlette' or name.startswith('starlette.'):\n"
            "        raise ImportError('optional web dependency is unavailable')\n"
            "    return original_import(name, *args, **kwargs)\n"
            "builtins.__import__ = import_without_web\n"
            "from work_catalog_yaml.operation_progress import report_progress\n"
            "report_progress('CLI operation', completed=1, total=1)\n"
            "print('optional-reporter-ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("optional-reporter-ok", result.stdout)

    def test_progress_is_optional_and_uuid_validation_is_strict(self):
        report_progress("CLI callers are unaffected", completed=1, total=2)
        operation_id = str(uuid4())
        self.assertEqual(normalize_operation_id(operation_id.upper()), operation_id)
        for invalid in ("", "not-uuid", operation_id.replace("-", ""), "{" + operation_id + "}"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_operation_id(invalid)

    def test_events_and_finished_retention_are_bounded_without_evicting_active_jobs(self):
        registry = OperationRegistry(max_completed=2, max_events=3)
        active = registry.register(str(uuid4()), "active")
        registry.start(active)
        completed_ids = []
        for index in range(5):
            row = registry.register(str(uuid4()), f"job {index}")
            completed_ids.append(row.id)
            registry.start(row)
            registry.finish(row, "succeeded", "done")
        for index in range(10):
            registry.report(active, f"step {index}", completed=index, total=10, unit="项")
        snapshot = registry.snapshot(active.id)
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(len(snapshot["events"]), 3)
        self.assertEqual([event["seq"] for event in snapshot["events"]], [10, 11, 12])
        self.assertEqual(snapshot["last_seq"], 12)
        self.assertEqual(sum(registry.snapshot(key) is not None for key in completed_ids), 2)
        snapshot["events"][0]["message"] = "changed externally"
        self.assertNotEqual(registry.snapshot(active.id)["events"][0]["message"], "changed externally")

    def test_expiry_does_not_allow_old_context_to_mutate_a_reused_id(self):
        registry = OperationRegistry(retention_seconds=1)
        row = registry.register(str(uuid4()), "first")
        registry.start(row)
        with patch("work_catalog_yaml.operation_progress.time.monotonic", return_value=10):
            registry.finish(row, "succeeded", "done")
        with patch("work_catalog_yaml.operation_progress.time.monotonic", return_value=12):
            replacement = registry.register(row.id, "replacement")
            registry.start(replacement)
            registry.report(row, "wrong job")
            registry.finish(row, "failed", "wrong completion")
            snapshot = registry.snapshot(row.id)
        self.assertEqual(snapshot["title"], "replacement")
        self.assertEqual(snapshot["status"], "running")
        self.assertNotIn("wrong job", [event["message"] for event in snapshot["events"]])

    def test_duplicate_ids_cannot_replace_active_or_completed_work(self):
        registry = OperationRegistry()
        row = registry.register(str(uuid4()), "first")
        with self.assertRaises(DuplicateOperationId):
            registry.register(row.id.upper(), "replacement")
        registry.start(row)
        registry.finish(row, "succeeded", "done")
        with self.assertRaises(DuplicateOperationId):
            registry.register(row.id, "replacement")

    def test_worker_context_is_reset_and_counts_are_sanitized(self):
        registry = OperationRegistry()
        row = registry.register(str(uuid4()), "first")

        def service():
            report_progress("文件处理中", completed=2, total=3, unit="文件", detail="example.yaml")
            report_progress("next phase", completed=True, total=-2, unit="项")
            return {"ok": True}

        execute_operation(registry, row, service)
        report_progress("must not leak")
        snapshot = registry.snapshot(row.id)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["events"][2]["completed"], 2)
        self.assertEqual(snapshot["events"][2]["detail"], "example.yaml")
        self.assertIsNone(snapshot["completed"])
        self.assertIsNone(snapshot["total"])
        self.assertEqual(snapshot["last_seq"], 5)
        for key in ("started_at", "updated_at", "finished_at"):
            self.assertIsNotNone(datetime.fromisoformat(snapshot[key]).tzinfo)

    def test_json_status_false_payload_and_exceptions_report_failure(self):
        for response in (
            JSONResponse({"ok": False, "error": "validation failed"}),
            JSONResponse({"ok": True}, status_code=409),
            {"ok": False, "message": "service failed"},
        ):
            with self.subTest(response=response):
                registry = OperationRegistry()
                row = registry.register(str(uuid4()), "first")
                self.assertIs(execute_operation(registry, row, lambda: response), response)
                self.assertEqual(registry.snapshot(row.id)["status"], "failed")
        registry = OperationRegistry()
        row = registry.register(str(uuid4()), "exception")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            execute_operation(registry, row, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(registry.snapshot(row.id)["message"], "boom")
        report_progress("must not leak after exception")
        self.assertEqual(registry.snapshot(row.id)["last_seq"], 3)

    def test_partial_success_is_warning_without_changing_response(self):
        cases = (
            ({"ok": True, "state": "partial"}, "部分项目未完成"),
            ({"ok": True, "execution": {"partial_failure": True}}, "部分项目未完成"),
            ({"ok": True, "execution": {"partial": True}}, "部分项目未完成"),
            ({"ok": True, "file_generation": {"failed_count": 2, "failed": [{}, {}]}}, "失败 2 项"),
            ({"ok": True, "errors": ["one error"]}, "失败 1 项"),
            ({"ok": True, "failed": 3}, "失败 3 项"),
            ({"ok": True, "file_generation": {"skipped_empty_target": 2, "skipped_missing_target": 3}}, "空目标 2 项、目标不存在 3 项"),
        )
        for payload, expected_message in cases:
            with self.subTest(payload=payload):
                registry = OperationRegistry()
                row = registry.register(str(uuid4()), "partial")
                response = JSONResponse(payload)
                before = response.body
                self.assertIs(execute_operation(registry, row, lambda: response), response)
                self.assertEqual(response.body, before)
                snapshot = registry.snapshot(row.id)
                self.assertEqual(snapshot["status"], "warning")
                self.assertIsNotNone(snapshot["finished_at"])
                self.assertIn(expected_message, snapshot["message"])

    def test_zero_counts_are_success_and_explicit_failure_overrides_partial(self):
        registry = OperationRegistry()
        successful = registry.register(str(uuid4()), "success")
        execute_operation(registry, successful, lambda: {"ok": True, "failed": [], "errors": [], "failed_count": 0,
                                                       "file_generation": {"skipped_empty_target": 0, "skipped_missing_target": 0}})
        self.assertEqual(registry.snapshot(successful.id)["status"], "succeeded")
        failed = registry.register(str(uuid4()), "failed")
        execute_operation(registry, failed, lambda: JSONResponse({"ok": False, "state": "partial", "error": "rollback failed"}))
        self.assertEqual(registry.snapshot(failed.id)["status"], "failed")
        self.assertEqual(registry.snapshot(failed.id)["message"], "rollback failed")

    def test_parallel_threads_do_not_mix_operation_events(self):
        registry = OperationRegistry()
        rows = [registry.register(str(uuid4()), str(index)) for index in range(3)]
        barrier = threading.Barrier(3)

        def work(index):
            barrier.wait(timeout=2)
            for step in range(20):
                report_progress(f"worker {index}", completed=step, total=20)

        threads = [threading.Thread(target=execute_operation, args=(registry, row, work, index)) for index, row in enumerate(rows)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        for index, row in enumerate(rows):
            snapshot = registry.snapshot(row.id)
            self.assertEqual(snapshot["status"], "succeeded")
            messages = [event["message"] for event in snapshot["events"][2:-1]]
            self.assertEqual(messages, [f"worker {index}"] * 20)


class ProgressApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = build_jp_tv_browse_app()
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    def dispatch_request(self, operation_id):
        return Request({
            "type": "http", "app": self.app, "method": "POST", "scheme": "http",
            "path": "/api/test", "root_path": "", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8765"), (b"x-nimda-operation-id", operation_id.encode())],
        })

    async def test_progress_polling_is_immediate_while_disk_queue_is_blocked(self):
        started, release = threading.Event(), threading.Event()
        operation_id = str(uuid4())

        def slow(_body):
            report_progress("正在扫描目录", completed=7, total=12, unit="目录", detail="mock folder")
            started.set()
            release.wait(3)
            return JSONResponse({"ok": True})

        active = asyncio.create_task(_dispatch(self.dispatch_request(operation_id), slow, {}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            status, payload, headers = await asyncio.wait_for(request(self.app, f"/api/operations/{operation_id}"), 0.5)
            self.assertEqual(status, 200)
            self.assertIn(b"no-store", headers[b"cache-control"])
            self.assertEqual(payload["operation"]["status"], "running")
            self.assertEqual(payload["operation"]["completed"], 7)
            self.assertEqual(payload["operation"]["total"], 12)
        finally:
            release.set()
            await active
        self.assertEqual(self.app.state.operation_registry.snapshot(operation_id)["status"], "succeeded")

    async def test_queued_operation_is_registered_before_worker_starts(self):
        started, release = threading.Event(), threading.Event()

        def blocking():
            started.set()
            release.wait(3)

        active = asyncio.create_task(self.app.state.api_disk_queue.run(blocking))
        operation_id = str(uuid4())
        queued = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            queued = asyncio.create_task(_dispatch(self.dispatch_request(operation_id), lambda _body: {"ok": True}, {}))
            await asyncio.sleep(0)
            snapshot = self.app.state.operation_registry.snapshot(operation_id)
            self.assertEqual(snapshot["status"], "queued")
            self.assertIsNone(snapshot["started_at"])
        finally:
            release.set()
            await asyncio.gather(*(item for item in (active, queued) if item))
        self.assertEqual(self.app.state.operation_registry.snapshot(operation_id)["status"], "succeeded")

    async def test_disconnect_after_start_does_not_mark_running_transaction_cancelled(self):
        started, release = threading.Event(), threading.Event()
        operation_id = str(uuid4())

        def slow(_body):
            started.set()
            release.wait(3)
            report_progress("已提交事务", completed=1, total=1)
            return {"ok": True}

        active = asyncio.create_task(_dispatch(self.dispatch_request(operation_id), slow, {}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            active.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await active
            self.assertEqual(self.app.state.operation_registry.snapshot(operation_id)["status"], "running")
        finally:
            release.set()
            await self.app.state.api_disk_queue.close()
        snapshot = self.app.state.operation_registry.snapshot(operation_id)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertIn("已提交事务", [event["message"] for event in snapshot["events"]])

    async def test_disconnected_client_can_poll_partial_result_without_http_body(self):
        started, release = threading.Event(), threading.Event()
        operation_id = str(uuid4())

        def partial(_body):
            started.set()
            release.wait(3)
            return JSONResponse({"ok": True, "file_generation": {
                "created": 2, "failed_count": 1, "failed": [{"error": "failed shortcut"}],
                "skipped_empty_target": 3, "skipped_missing_target": 4,
            }})

        active = asyncio.create_task(_dispatch(self.dispatch_request(operation_id), partial, {}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            active.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await active
            self.assertEqual(self.app.state.operation_registry.snapshot(operation_id)["status"], "running")
        finally:
            release.set()
            await self.app.state.api_disk_queue.close()
        status, payload, _headers = await request(self.app, f"/api/operations/{operation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["operation"]["status"], "warning")
        self.assertIn("失败 1 项", payload["operation"]["message"])
        self.assertIn("空目标 3 项、目标不存在 4 项", payload["operation"]["message"])

    async def test_disconnect_or_shutdown_cancels_only_queued_operation(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                queue = self.app.state.api_disk_queue
                started, release = threading.Event(), threading.Event()
                operation_id = str(uuid4())
                calls = []

                def blocking():
                    started.set()
                    release.wait(3)

                active = asyncio.create_task(queue.run(blocking))
                queued = None
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    queued = asyncio.create_task(_dispatch(self.dispatch_request(operation_id), lambda _body: calls.append(1), {}))
                    await asyncio.sleep(0)
                    if shutdown:
                        queue.begin_shutdown()
                        self.assertEqual((await queued).status_code, 503)
                    else:
                        queued.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await queued
                    snapshot = self.app.state.operation_registry.snapshot(operation_id)
                    self.assertEqual(snapshot["status"], "cancelled")
                    self.assertIsNone(snapshot["started_at"])
                    self.assertEqual(calls, [])
                finally:
                    release.set()
                    await asyncio.gather(*(item for item in (active, queued) if item), return_exceptions=True)

    async def test_queue_full_and_closed_are_terminal_without_running_service(self):
        await self.app.state.api_disk_queue.close()
        self.app.state.api_disk_queue = ApiWorkQueue(max_pending=1)
        queue = self.app.state.api_disk_queue
        started, release = threading.Event(), threading.Event()
        calls = []

        def blocking():
            started.set()
            release.wait(3)

        active = asyncio.create_task(queue.run(blocking))
        full_id = str(uuid4())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            response = await _dispatch(self.dispatch_request(full_id), lambda _body: calls.append(1), {})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(self.app.state.operation_registry.snapshot(full_id)["status"], "failed")
        finally:
            release.set()
            await active
            await queue.close()
        closed_id = str(uuid4())
        response = await _dispatch(self.dispatch_request(closed_id), lambda _body: calls.append(1), {})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.app.state.operation_registry.snapshot(closed_id)["status"], "cancelled")
        self.assertEqual(calls, [])

    async def test_missing_invalid_duplicate_and_cross_origin_ids(self):
        valid_id = str(uuid4())
        status, _payload, headers = await request(self.app, f"/api/operations/{valid_id}")
        self.assertEqual(status, 404)
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertEqual((await request(self.app, "/api/operations/bad-id"))[0], 400)
        with patch.object(browse_api, "resource_libraries_cached_payload", return_value={"ok": True}) as service:
            endpoint = "/api/collection-detail/resource-libraries/cache"
            self.assertEqual((await request(self.app, endpoint, headers=[(b"x-nimda-operation-id", b"bad-id")]))[0], 400)
            service.assert_not_called()
            tracked_headers = [(b"x-nimda-operation-id", valid_id.encode())]
            self.assertEqual((await request(self.app, endpoint, headers=tracked_headers))[0], 200)
            self.assertEqual((await request(self.app, endpoint, headers=tracked_headers))[0], 409)
            service.assert_called_once()
            for source in ((b"origin", b"https://untrusted.example"), (b"sec-fetch-site", b"cross-site")):
                self.assertEqual((await request(self.app, f"/api/operations/{valid_id}", headers=[source]))[0], 403)
                self.assertEqual((await request(self.app, endpoint, headers=[source, (b"x-nimda-operation-id", str(uuid4()).encode())]))[0], 403)
            service.assert_called_once()

    async def test_untracked_requests_remain_compatible(self):
        with patch.object(browse_api, "resource_libraries_cached_payload", return_value={"ok": True, "example": 1}):
            status, payload, _headers = await request(self.app, "/api/collection-detail/resource-libraries/cache")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "example": 1})
        self.assertEqual(len(self.app.state.operation_registry._operations), 0)


if __name__ == "__main__":
    unittest.main()
