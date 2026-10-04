from __future__ import annotations

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from catalog_library import web
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from test_api_runtime import request


class CatalogLibraryApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_browse_uses_shared_disk_queue_and_existing_settings(self):
        configured = object()
        received = []

        def browse(body, *, settings):
            received.append((body, settings, threading.current_thread().name))
            return {"items": [], "total": 0, "page": 1}

        app = build_jp_tv_browse_app()
        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(configured, None)), patch.object(
                web.service, "browse", side_effect=browse,
            ):
                status, body = await request(app, "/api/catalog-library/browse", method="POST", body=b'{"page":1}')
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "items": [], "total": 0, "page": 1})
        self.assertEqual(received[0][:2], ({"page": 1}, configured))
        self.assertTrue(received[0][2].startswith("nimda-disk"))

    async def test_classification_get_never_calls_writer(self):
        app = build_jp_tv_browse_app()
        configured = object()
        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(configured, None)), patch.object(
                web.service, "classifications_payload", return_value={"items": [], "revision": "initial"},
            ) as reader, patch.object(web.service, "save_classification") as writer:
                status, body = await request(app, "/api/catalog-library/classifications")
                self.assertEqual(status, 200)
                self.assertEqual(body["revision"], "initial")
                reader.assert_called_once_with(settings=configured)
                writer.assert_not_called()

    async def test_new_mutations_keep_local_origin_protection(self):
        app = build_jp_tv_browse_app()
        async with app.router.lifespan_context(app):
            with patch.object(web.service, "apply_edits") as writer:
                status, body = await request(
                    app, "/api/catalog-library/edits/apply", method="POST", body=b'{}',
                    headers=[(b"origin", b"https://example.com"), (b"sec-fetch-site", b"cross-site")],
                )
                self.assertEqual(status, 403)
                self.assertFalse(body["ok"])
                writer.assert_not_called()

    async def test_validation_failures_are_reported_in_operation_details(self):
        app = build_jp_tv_browse_app()
        operation_id = "1261d30e-627f-43e8-b6e5-e7c1f8bfb343"
        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(object(), None)), patch.object(
                web.service, "detail", side_effect=ValueError("作品记录已变化，请重新预览"),
            ):
                status, body = await request(
                    app, "/api/catalog-library/detail", method="POST", body=b'{}',
                    headers=[(b"x-nimda-operation-id", operation_id.encode())],
                )
            self.assertEqual(status, 400)
            self.assertIn("作品记录已变化", body["error"])
            status, progress = await request(app, "/api/operations/" + operation_id)
            self.assertEqual(status, 200)
            self.assertIn("作品记录已变化", json.dumps(progress, ensure_ascii=False))

    async def test_provider_network_work_does_not_block_local_browse(self):
        app = build_jp_tv_browse_app()
        started, release = threading.Event(), threading.Event()
        threads = []

        def search(body, *, settings):
            threads.append(threading.current_thread().name)
            started.set()
            release.wait(5)
            return {"results": [], "total": 0}

        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(object(), None)), patch.object(
                web.providers, "search_provider", side_effect=search,
            ), patch.object(web.service, "browse", return_value={"items": [], "total": 0}):
                pending = asyncio.create_task(request(
                    app, "/api/catalog-library/provider/search", method="POST", body=b'{"query":"test"}',
                ))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    status, body = await asyncio.wait_for(request(
                        app, "/api/catalog-library/browse", method="POST", body=b'{}',
                    ), timeout=2)
                    self.assertEqual(status, 200)
                    self.assertEqual(body["items"], [])
                finally:
                    release.set()
                    status, _ = await pending
                self.assertEqual(status, 200)
        self.assertTrue(threads[0].startswith("nimda-provider"))

    async def test_provider_apply_uses_disk_queue_and_never_searches(self):
        app = build_jp_tv_browse_app()
        threads = []

        def apply(body, *, settings):
            threads.append(threading.current_thread().name)
            return {"applied": True}

        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(object(), None)), patch.object(
                web.providers, "apply_provider_import", side_effect=apply,
            ), patch.object(web.providers, "search_provider") as search:
                status, _ = await request(app, "/api/catalog-library/provider/apply", method="POST", body=b'{}')
                self.assertEqual(status, 200)
                search.assert_not_called()
        self.assertTrue(threads[0].startswith("nimda-disk"))

    async def test_cover_preview_uses_network_queue_without_applying_records(self):
        app = build_jp_tv_browse_app()
        threads = []

        def preview(body, *, settings):
            threads.append(threading.current_thread().name)
            return {"diff": [{"field": "metadata.cover"}], "warnings": []}

        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(object(), None)), patch.object(
                web.providers, "preview_provider_cover", side_effect=preview,
            ), patch.object(web.service, "apply_edits") as writer:
                status, result = await request(app, "/api/catalog-library/provider/cover-preview", method="POST", body=b'{}')
                self.assertEqual(status, 200)
                self.assertEqual(result["diff"][0]["field"], "metadata.cover")
                writer.assert_not_called()
        self.assertTrue(threads[0].startswith("nimda-provider"))

    async def test_cover_assets_are_local_disk_reads_with_no_provider_work(self):
        app = build_jp_tv_browse_app()
        filename = "a" * 64 + ".png"
        threads = []

        def read_asset(name):
            self.assertEqual(name, filename)
            threads.append(threading.current_thread().name)
            return b"fixture-image", "image/png"

        async with app.router.lifespan_context(app):
            with patch.object(web.assets, "read_asset", side_effect=read_asset), patch.object(
                web.providers, "preview_provider_cover",
            ) as fetch:
                status, body = await request(app, "/api/catalog-library/assets/" + filename)
                self.assertEqual(status, 200)
                self.assertEqual(body, "fixture-image")
                status, body = await request(app, "/api/catalog-library/assets/" + filename, headers=[
                    (b"if-none-match", ('"' + "a" * 64 + '"').encode()),
                ])
                self.assertEqual(status, 304)
                self.assertEqual(body, "")
                status, _ = await request(app, "/api/catalog-library/assets/" + filename, method="HEAD")
                self.assertEqual(status, 200)
                status, _ = await request(app, "/api/catalog-library/assets/" + filename, method="POST")
                self.assertEqual(status, 405)
                fetch.assert_not_called()
        self.assertTrue(all(thread.startswith("nimda-disk") for thread in threads))

    async def test_missing_cover_is_404_and_cover_preview_keeps_origin_protection(self):
        app = build_jp_tv_browse_app()
        async with app.router.lifespan_context(app):
            with patch.object(web.assets, "read_asset", side_effect=ValueError("封面已不存在")):
                status, _ = await request(app, "/api/catalog-library/assets/invalid.png")
                self.assertEqual(status, 404)
            with patch.object(web.providers, "preview_provider_cover") as fetch:
                status, _ = await request(
                    app, "/api/catalog-library/provider/cover-preview", method="POST", body=b'{}',
                    headers=[(b"origin", b"https://example.com"), (b"sec-fetch-site", b"cross-site")],
                )
                self.assertEqual(status, 403)
                fetch.assert_not_called()

    async def test_routes_reject_invalid_json_and_get_writes(self):
        app = build_jp_tv_browse_app()
        async with app.router.lifespan_context(app):
            status, body = await request(app, "/api/catalog-library/edits/apply", method="POST", body=b'[]')
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])
            for endpoint in ("edits/apply", "identities/apply", "provider/apply", "provider/cover-preview", "classification-edits/preview"):
                with self.subTest(endpoint=endpoint):
                    status, _ = await request(app, "/api/catalog-library/" + endpoint)
                    self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
