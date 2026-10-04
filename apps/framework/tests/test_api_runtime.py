from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from starlette.datastructures import UploadFile
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from work_catalog_yaml.api_runtime import (
    ApiWorkQueue, WorkQueueBusy, WorkQueueClosed, install_api_queues, json_endpoint,
)
from work_catalog_yaml.jp_tv import browse_api
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app


async def request(app, path: str, *, body: bytes = b"", method: str = "GET", content_type: str = "application/json", headers=()):
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
        "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", content_type.encode()), *headers],
    }
    await app(scope, receive, send)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    data = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        payload = data.decode("utf-8")
    return status, payload


class ApiQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_queue_capacity_is_bounded_and_cancelled_slots_can_be_reused(self) -> None:
        queue = ApiWorkQueue(max_pending=2)
        started, release = threading.Event(), threading.Event()
        calls = []

        def first():
            started.set()
            release.wait(3)

        active = asyncio.create_task(queue.run(first))
        queued = replacement = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            queued = asyncio.create_task(queue.run(lambda: calls.append("cancelled")))
            await asyncio.sleep(0)
            with self.assertRaises(WorkQueueBusy):
                await queue.run(lambda: calls.append("overflow"))
            self.assertEqual(queue.snapshot()["queued"], 1)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            replacement = asyncio.create_task(queue.run(lambda: calls.append("replacement")))
            await asyncio.sleep(0)
        finally:
            release.set()
            await asyncio.gather(*(task for task in (active, queued, replacement) if task), return_exceptions=True)
            await queue.close()
        self.assertEqual(calls, ["replacement"])

    def test_invalid_capacity_is_rejected(self) -> None:
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                ApiWorkQueue(max_pending=capacity)

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
        settings_patch = patch.object(browse_api, "get_resolved_browse_settings", return_value=(None, None))
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.app = build_jp_tv_browse_app()
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def test_scan_requires_post_and_rejects_cross_site_before_disk_work(self) -> None:
        path = "/api/collection-detail/resource-libraries/scan"
        with patch.object(browse_api, "scan_resource_libraries_payload", return_value={"ok": True}) as scan:
            for method in ("GET", "HEAD"):
                status, _payload = await request(self.app, path, method=method)
                self.assertEqual(status, 405)
            for headers in (
                [(b"sec-fetch-site", b"cross-site")],
                [(b"origin", b"https://untrusted.example")],
            ):
                status, _payload = await request(self.app, path, method="POST", body=b"{}", headers=headers)
                self.assertEqual(status, 403)
            scan.assert_not_called()
            status, payload = await request(
                self.app, path, method="POST", body=b"{}", headers=[(b"sec-fetch-site", b"same-origin")],
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            scan.assert_called_once_with()

    async def test_saturated_queue_returns_503_without_running_rejected_operation(self) -> None:
        await self.app.state.api_disk_queue.close()
        self.app.state.api_disk_queue = ApiWorkQueue(max_pending=1)
        started, release = threading.Event(), threading.Event()
        calls = []

        def execute(body, *, settings):
            calls.append(body["id"])
            started.set()
            release.wait(3)
            return {"ok": True}

        with patch.object(browse_api, "save_link_index_from_ui_body", side_effect=execute):
            active = asyncio.create_task(request(self.app, "/api/collection-detail/link-index/save", method="POST", body=b'{"id":1}'))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                status, payload = await request(self.app, "/api/collection-detail/link-index/save", method="POST", body=b'{"id":2}')
                self.assertEqual(status, 503)
                self.assertEqual(payload["code"], "api-queue-full")
                self.assertEqual(calls, [1])
                self.assertEqual((await request(self.app, "/api/health"))[0], 200)
            finally:
                release.set()
                await active
            status, _payload = await request(self.app, "/api/collection-detail/link-index/save", method="POST", body=b'{"id":3}')
            self.assertEqual(status, 200)
        self.assertEqual(calls, [1, 3])

    async def test_disk_requests_are_serial_but_health_responds_during_slow_work(self) -> None:
        started, release = threading.Event(), threading.Event()
        calls = []
        main_thread = threading.get_ident()
        threads = []

        def execute(body, *, settings):
            threads.append(threading.get_ident())
            calls.append(body["id"])
            if body["id"] == 1:
                started.set()
                release.wait(3)
            return {"ok": True, "id": body["id"]}

        with patch.object(browse_api, "save_link_index_from_ui_body", side_effect=execute):
            first = asyncio.create_task(request(self.app, "/api/collection-detail/link-index/save", body=b'{"id":1}', method="POST"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            second = asyncio.create_task(request(self.app, "/api/collection-detail/link-index/save", body=b'{"id":2}', method="POST"))
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
        with patch.object(browse_api, "save_link_index_from_ui_body") as execute:
            for body in (b"broken", b"[]", b"null"):
                status, payload = await request(self.app, "/api/collection-detail/link-index/save", body=body, method="POST")
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])
            self.app.state.api_disk_queue.begin_shutdown()
            status, payload = await request(self.app, "/api/collection-detail/link-index/save", body=b"{}", method="POST")
            self.assertEqual(status, 503)
            execute.assert_not_called()

    async def test_shutdown_returns_503_to_already_queued_http_request(self) -> None:
        started, release = threading.Event(), threading.Event()
        calls = []

        def execute(body, *, settings):
            calls.append(body["id"])
            started.set()
            release.wait(3)
            return {"ok": True}

        with patch.object(browse_api, "save_link_index_from_ui_body", side_effect=execute):
            first = asyncio.create_task(request(self.app, "/api/collection-detail/link-index/save", body=b'{"id":1}', method="POST"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            second = asyncio.create_task(request(self.app, "/api/collection-detail/link-index/save", body=b'{"id":2}', method="POST"))
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
            return JSONResponse({"ok": True})

        # The network adapter is framework infrastructure, not a reason to keep
        # an obsolete feature or to add a production endpoint for testing.
        provider_app = Starlette(routes=[
            Route("/provider", endpoint=json_endpoint(network=True)(suggest), methods=["POST"]),
            Route("/disk", endpoint=json_endpoint(lambda _body: JSONResponse({"ok": True})), methods=["POST"]),
        ])
        install_api_queues(provider_app)
        pending = None
        try:
            pending = asyncio.create_task(request(provider_app, "/provider", body=b"{}", method="POST"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            status, payload = await asyncio.wait_for(request(provider_app, "/disk", body=b"{}", method="POST"), 0.5)
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertFalse(pending.done())
        finally:
            release.set()
            if pending is not None:
                await pending
            await asyncio.gather(provider_app.state.api_disk_queue.close(), provider_app.state.api_network_queue.close())

    async def test_save_error_contract_survives_worker_boundary(self) -> None:
        with patch.object(browse_api, "save_link_index_from_ui_body", side_effect=PermissionError("保存被拒绝")):
            status, payload = await request(self.app, "/api/collection-detail/link-index/save", body=b"{}", method="POST")
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        self.assertIn("保存被拒绝", payload["error"])

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
