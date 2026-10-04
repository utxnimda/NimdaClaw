from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from work_catalog_yaml.operation_progress import execute_operation, report_progress


async def request(app, url, *, method="GET", headers=(), body=b""):
    parts = urlsplit(url)
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

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": parts.path, "raw_path": parts.path.encode(), "root_path": "",
               "query_string": parts.query.encode(), "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345),
               "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"), *headers]}, receive, send)
    response = next(item for item in messages if item["type"] == "http.response.start")
    content = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return response["status"], content, dict(response["headers"])


class OperationLogFileApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = build_jp_tv_browse_app(operation_log_root=self.root / "logs")
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.store = self.app.state.operation_registry.log_store
        self.row = self.app.state.operation_registry.register(str(uuid4()), "中文日志测试")

        def worker():
            report_progress("开始匹配", context={"stage": "匹配数据库", "object": "作品甲"})
            return {"ok": True, "issues": [{"message": f"条目{i}未匹配", "reason": f"匹配记录不存在{i}",
                                               "shortcut_path": f"E:/LinkVideo/{i}.lnk"} for i in range(3)]}

        execute_operation(self.app.state.operation_registry, self.row, worker)
        self.prefix = f"/api/operations/{self.row.id}/log"

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def test_plain_text_view_and_download_use_same_real_utf8_log(self):
        expected = self.store.safe_log_path(self.row.id)
        self.assertEqual(expected.suffix, ".log")
        for query, disposition in (("", b"inline"), ("?download=1", b"attachment")):
            status, content, headers = await request(self.app, self.prefix + "/file" + query)
            self.assertEqual(status, 200)
            self.assertEqual(content, expected.read_bytes())
            self.assertIn("匹配记录不存在", content.decode("utf-8"))
            self.assertNotIn('"schema":', content.decode("utf-8"))
            self.assertTrue(headers[b"content-type"].startswith(b"text/plain"))
            self.assertIn(b"charset=utf-8", headers[b"content-type"])
            self.assertTrue(headers[b"content-disposition"].startswith(disposition))
            self.assertIn(expected.name.encode(), headers[b"content-disposition"])
            self.assertIn(b"no-store", headers[b"cache-control"])
            self.assertEqual(headers[b"x-content-type-options"], b"nosniff")

    async def test_file_response_uses_safe_open_handle_and_closes_it(self):
        original = self.store.open_text_file
        handles = []

        def tracked(operation_id):
            path, handle = original(operation_id)
            handles.append(handle)
            return path, handle

        with patch.object(self.store, "open_text_file", side_effect=tracked) as opening:
            status, content, _headers = await request(self.app, self.prefix + "/file")
        self.assertEqual(status, 200)
        self.assertTrue(content)
        opening.assert_called_once_with(self.row.id)
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    async def test_hardlink_swap_before_safe_open_cannot_disclose_another_file(self):
        protected = self.root / "protected.txt"
        protected.write_text("NEVER-DISCLOSE-PRIVATE", encoding="utf-8")
        original = self.store.open_text_file

        def swapped(operation_id):
            path = self.store.safe_log_path(operation_id)
            path.unlink()  # Only this test's generated temporary log projection.
            os.link(protected, path)
            return original(operation_id)

        with patch.object(self.store, "open_text_file", side_effect=swapped):
            status, content, _headers = await request(self.app, self.prefix + "/file")
        self.assertEqual(status, 503)
        self.assertNotIn(b"NEVER-DISCLOSE-PRIVATE", content)
        self.assertEqual(protected.read_text(encoding="utf-8"), "NEVER-DISCLOSE-PRIVATE")

    async def test_search_uses_full_log_and_returns_resumable_byte_offsets(self):
        query = urlencode({"q": "匹配记录不存在", "limit": 1})
        status, content, headers = await request(self.app, self.prefix + "/search?" + query)
        self.assertEqual(status, 200)
        first = json.loads(content)["search"]
        self.assertEqual(len(first["matches"]), 1)
        self.assertIn("匹配记录不存在", first["matches"][0]["text"])
        self.assertGreater(first["matches"][0]["line"], 0)
        self.assertGreater(first["next_offset"], first["matches"][0]["offset"])
        self.assertTrue(first["path"].endswith(".log"))
        self.assertIn(b"no-store", headers[b"cache-control"])
        status, content, _ = await request(self.app, self.prefix + "/search?" + query + f"&offset={first['next_offset']}")
        self.assertEqual(status, 200)
        second = json.loads(content)["search"]
        self.assertEqual(len(second["matches"]), 1)
        self.assertGreater(second["matches"][0]["offset"], first["matches"][0]["offset"])

    async def test_search_rejects_bad_offsets_limits_queries_and_ids(self):
        for values in ({"q": "x", "offset": -1}, {"q": "x", "offset": "bad"},
                       {"q": "x", "offset": 10**15}, {"q": "x", "limit": 0}, {"q": "x", "limit": 201},
                       {"q": ""}, {"q": "x\ny"}, {"q": "x" * 513}):
            with self.subTest(values=values):
                self.assertEqual((await request(self.app, self.prefix + "/search?" + urlencode(values)))[0], 400)
        for suffix, method in (("/search?q=x", "GET"), ("/file", "GET"), ("/open", "POST")):
            self.assertEqual((await request(self.app, "/api/operations/not-a-uuid/log" + suffix, method=method))[0], 400)
            self.assertEqual((await request(self.app, f"/api/operations/{uuid4()}/log" + suffix, method=method))[0], 404)

    async def test_open_can_only_select_server_owned_uuid_log_not_client_path(self):
        expected = self.store.safe_log_path(self.row.id)
        with patch("os.startfile", create=True) as opening:
            status, content, headers = await request(self.app, self.prefix + "/open?path=C:/private.txt", method="POST",
                                                     body=b'{"path":"C:/private.txt","id":"not-a-uuid"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["path"], str(expected))
        opening.assert_called_once_with(str(expected), "open")
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertEqual((await request(self.app, self.prefix + "/open"))[0], 405)

    async def test_cross_site_open_download_and_search_are_denied_before_file_access(self):
        with patch("os.startfile", create=True) as opening, patch.object(self.store, "safe_log_path") as lookup:
            for suffix, method in (("/file", "GET"), ("/search?q=x", "GET"), ("/open", "POST")):
                for headers in ([(b"origin", b"https://evil.example")], [(b"sec-fetch-site", b"cross-site")]):
                    self.assertEqual((await request(self.app, self.prefix + suffix, method=method, headers=headers))[0], 403)
        opening.assert_not_called()
        lookup.assert_not_called()

    async def test_missing_store_and_log_access_errors_are_reported_without_opening_editor(self):
        with patch("os.startfile", create=True) as opening:
            with patch.object(self.app.state.operation_registry, "log_store", None):
                for suffix, method in (("/file", "GET"), ("/search?q=x", "GET"), ("/open", "POST")):
                    self.assertEqual((await request(self.app, self.prefix + suffix, method=method))[0], 404)
            with patch.object(self.store, "safe_log_path", side_effect=FileNotFoundError("日志已不可用")):
                status, content, _ = await request(self.app, self.prefix + "/open", method="POST")
            self.assertEqual(status, 503)
            self.assertIn("日志已不可用", json.loads(content)["error"])
        opening.assert_not_called()


if __name__ == "__main__":
    unittest.main()
