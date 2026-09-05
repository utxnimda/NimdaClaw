from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from starlette.datastructures import UploadFile

from work_catalog_yaml.api_runtime import ApiWorkQueue, WorkQueueClosed
from work_catalog_yaml.jp_tv import browse_api
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from media_directory_organizer.service import MediaRollbackError


async def request(app, path: str, *, body: bytes = b"", method: str = "GET", content_type: str = "application/json"):
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

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"", "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", content_type.encode())],
    }
    await app(scope, receive, send)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    data = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return status, json.loads(data)


class ApiQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_a_queued_request_never_starts_its_operation(self) -> None:
        queue = ApiWorkQueue()
        started, release = threading.Event(), threading.Event()
        calls = []

        def first():
            started.set()
            release.wait(3)
            calls.append("first")

        active = asyncio.create_task(queue.run(first))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        queued = asyncio.create_task(queue.run(lambda: calls.append("cancelled")))
        await asyncio.sleep(0)
        queued.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await queued
        finally:
            release.set()
            await active
            await queue.close()
        self.assertEqual(calls, ["first"])

    async def test_cancelled_running_operation_finishes_and_shutdown_waits(self) -> None:
        queue = ApiWorkQueue()
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        def write():
            started.set()
            release.wait(3)
            completed.set()

        active = asyncio.create_task(queue.run(write))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        closing = asyncio.create_task(queue.close())
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(closing.done())
            self.assertFalse(completed.is_set())
        finally:
            release.set()
            await closing
        self.assertTrue(completed.is_set())
        with self.assertRaises(WorkQueueClosed):
            await queue.run(lambda: None)

    async def test_shutdown_discards_queued_work_but_keeps_started_work(self) -> None:
        queue = ApiWorkQueue()
        started, release = threading.Event(), threading.Event()
        calls = []

        def first():
            started.set()
            release.wait(3)
            calls.append("finished")

        active = asyncio.create_task(queue.run(first))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        queued = asyncio.create_task(queue.run(lambda: calls.append("must not run")))
        await asyncio.sleep(0)
        queue.begin_shutdown()
        try:
            with self.assertRaises(WorkQueueClosed):
                await queued
        finally:
            release.set()
            await active
            await queue.close()
        self.assertEqual(calls, ["finished"])


class ApiSchedulingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = build_jp_tv_browse_app()
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def test_disk_requests_are_serial_but_health_responds_during_slow_work(self) -> None:
        started, release = threading.Event(), threading.Event()
        calls = []
        main_thread = threading.get_ident()
        threads = []

        def execute(body):
            threads.append(threading.get_ident())
            calls.append(body["id"])
            if body["id"] == 1:
                started.set()
                release.wait(3)
            return {"ok": True, "id": body["id"]}

        with patch.object(browse_api, "apply_organizer_from_ui_body", side_effect=execute):
            first = asyncio.create_task(request(self.app, "/api/media-directory-organizer/apply", body=b'{"id":1}', method="POST"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            second = asyncio.create_task(request(self.app, "/api/media-directory-organizer/apply", body=b'{"id":2}', method="POST"))
            try:
                for _ in range(30):
                    if self.app.state.api_disk_queue.snapshot()["queued"]:
                        break
                    await asyncio.sleep(0.005)
                status, health = await asyncio.wait_for(request(self.app, "/api/health"), 0.5)
                self.assertEqual(status, 200)
                self.assertEqual(health["state"], "busy")
                self.assertEqual(health["disk"], {"closing": False, "running": 1, "queued": 1})
                self.assertEqual(calls, [1])
            finally:
                release.set()
                outcomes = await asyncio.gather(first, second)
        self.assertEqual(calls, [1, 2])
        self.assertTrue(all(thread != main_thread for thread in threads))
        self.assertEqual([outcome[0] for outcome in outcomes], [200, 200])

    async def test_invalid_json_and_closed_queue_do_not_call_service(self) -> None:
        with patch.object(browse_api, "apply_organizer_from_ui_body") as execute:
            for body in (b"broken", b"[]", b"null"):
                status, payload = await request(self.app, "/api/media-directory-organizer/apply", body=body, method="POST")
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])
            self.app.state.api_disk_queue.begin_shutdown()
            status, payload = await request(self.app, "/api/media-directory-organizer/apply", body=b"{}", method="POST")
            self.assertEqual(status, 503)
            execute.assert_not_called()

    async def test_shutdown_returns_503_to_already_queued_http_request(self) -> None:
        started, release = threading.Event(), threading.Event()
        calls = []

        def execute(body):
            calls.append(body["id"])
            started.set()
            release.wait(3)
            return {"ok": True}

        with patch.object(browse_api, "apply_organizer_from_ui_body", side_effect=execute):
            first = asyncio.create_task(request(self.app, "/api/media-directory-organizer/apply", body=b'{"id":1}', method="POST"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            second = asyncio.create_task(request(self.app, "/api/media-directory-organizer/apply", body=b'{"id":2}', method="POST"))
            try:
                for _ in range(30):
                    if self.app.state.api_disk_queue.snapshot()["queued"]:
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(self.app.state.api_disk_queue.snapshot()["queued"], 1)
                self.app.state.api_disk_queue.begin_shutdown()
                status, payload = await asyncio.wait_for(second, 0.5)
                self.assertEqual(status, 503)
                self.assertFalse(payload["ok"])
                self.assertEqual(calls, [1])
                self.assertFalse(first.done())
            finally:
                release.set()
                await asyncio.gather(first, second)
        self.assertEqual(calls, [1])

    async def test_slow_provider_does_not_block_disk_queue(self) -> None:
        started, release = threading.Event(), threading.Event()

        def suggest(_body):
            started.set()
            release.wait(3)
            return {"ok": True}

        with patch.object(browse_api, "suggest_organizer_landing_from_ui_body", side_effect=suggest), patch.object(
            browse_api, "organizer_config_payload", return_value={"ok": True}
        ):
            pending = asyncio.create_task(request(self.app, "/api/media-directory-organizer/landing/suggest", body=b"{}", method="POST"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            try:
                status, payload = await asyncio.wait_for(request(self.app, "/api/media-directory-organizer/config"), 0.5)
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                self.assertFalse(pending.done())
            finally:
                release.set()
                await pending

    async def test_partial_recovery_contract_survives_worker_boundary(self) -> None:
        error = MediaRollbackError("恢复失败", root="work", recovery_moves=[{"source": "old/a", "target": "new/a"}])
        with patch.object(browse_api, "apply_organizer_from_ui_body", side_effect=error):
            status, payload = await request(self.app, "/api/media-directory-organizer/apply", body=b"{}", method="POST")
        self.assertEqual(status, 409)
        self.assertEqual(payload["state"], "partial")
        self.assertEqual(payload["media"]["recovery_moves"][0]["target"], "new/a")

    async def test_uploaded_file_is_closed_before_invalid_yaml_response(self) -> None:
        closed = []
        original_close = UploadFile.close

        async def record_close(upload):
            await original_close(upload)
            closed.append(upload.file.closed)

        body = b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="bad.yaml"\r\nContent-Type: text/yaml\r\n\r\n\xff\r\n--boundary--\r\n'
        with patch.object(UploadFile, "close", record_close), patch.object(browse_api, "get_resolved_browse_settings", return_value=(None, None)):
            status, payload = await request(self.app, "/api/browse", body=body, method="POST", content_type="multipart/form-data; boundary=boundary")
        self.assertEqual(status, 400)
        self.assertIn("UTF-8", payload["error"])
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
