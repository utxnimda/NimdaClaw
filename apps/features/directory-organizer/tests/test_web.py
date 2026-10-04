from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from directory_organizer import web
from directory_organizer.service import OrganizerService, shortcuts
from work_catalog_yaml.common import native_dialogs
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string


async def request(app, action, body=None, *, raw=None, headers=(), method="POST"):
    """In-process ASGI request: no listener, browser or native window is opened."""
    messages = []
    sent = False
    payload = raw if raw is not None else json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    path = "/api/directory-organizer/" + action

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
               "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345),
               "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json"), *headers]}, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], content, dict(start["headers"])


class OrganizerWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.db = self.base / "db"
        self.db.mkdir()
        self.root = self.base / "media"
        self.child = self.root / "Demo_BDRip(Jsum)"
        self.child.mkdir(parents=True)
        (self.child / "Demo 01.mkv").write_bytes(b"temporary-fixture-video")
        self.source = self.db / "[JP][TVInfo][2005].yaml"
        record = {"external": {"keep": "full record"}, "attributes": [
            {"type": "name", "data": "Demo"}, {"type": "country", "data": "japan"},
            {"type": "date", "data": {"start": "20050101", "end": "20050331"}},
            {"type": "collection-type", "data": {"domain": "animation", "release_type": "tv", "path": str(self.root),
                "markers": [], "collectioned": [{"press_format": "BDRip", "press_group": "Jsum", "press_path": self.child.name}]}},
        ]}
        self.source.write_text(dump_yaml_string([record]), encoding="utf-8")
        self.settings = JpTvBrowseSettings(version=1, filesystem_root=self.db, resolved_default_readable=str(self.source),
            resolved_catalog_yaml_paths=(str(self.source),), enum_options={}, enum_labels={}, enum_section_labels={}, app_features=())
        self.service = OrganizerService(journal_root=self.base / "receipts")
        for replacement in (patch.object(web, "service", self.service),
                            patch.object(web, "get_resolved_browse_settings", return_value=(self.settings, None))):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.app = build_jp_tv_browse_app()
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.temporary.cleanup()

    async def test_scan_and_preview_are_read_only_and_use_expected_envelopes(self):
        before = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        operation_id = str(uuid4())
        status, content, headers = await request(self.app, "scan", {"root": str(self.root)},
            headers=[(b"x-nimda-operation-id", operation_id.encode())])
        scan = json.loads(content)
        self.assertEqual(status, 200)
        self.assertTrue(scan["ok"])
        self.assertEqual([item["name"] for item in scan["children"]], [self.child.name])
        self.assertTrue(scan["strategies"])
        self.assertEqual(headers[b"x-nimda-operation-id"].decode(), operation_id)
        self.assertEqual(self.app.state.operation_registry.snapshot(operation_id)["status"], "succeeded")
        self.assertIn(b"no-store", headers[b"cache-control"])
        status, content, _headers = await request(self.app, "preview", {"root": str(self.root), "child": self.child.name})
        preview = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(set(preview), {"ok", "plan"})
        self.assertTrue(preview["plan"]["can_execute"], preview)
        self.assertEqual(preview["plan"]["child"], self.child.name)
        self.assertFalse(any(key.startswith("_") for key in preview["plan"]))
        after = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.base / "receipts").exists())

    async def test_catalog_search_returns_complete_raw_record_and_source_versions(self):
        status, content, _headers = await request(self.app, "catalog", {"query": "Demo", "limit": 10})
        data = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["records"][0]["record"]["external"], {"keep": "full record"})
        self.assertEqual(len(data["records"][0]["ref"]["source_sha256"]), 64)
        self.assertEqual(len(data["records"][0]["ref"]["record_sha256"]), 64)
        self.assertTrue(data["records"][0]["press"][0]["press_key"])

    async def test_execute_requires_explicit_confirmation_before_any_media_change(self):
        before = self.source.read_bytes()
        status, content, _headers = await request(self.app, "execute", {"plan_ids": [str(uuid4())]})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(content)["ok"])
        self.assertIn("确认", json.loads(content)["error"])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual((self.child / "Demo 01.mkv").read_bytes(), b"temporary-fixture-video")
        self.assertFalse((self.base / "receipts").exists())

    async def test_execute_adapter_passes_the_confirmed_batch_without_reinterpreting_it(self):
        body = {"plan_ids": [str(uuid4()), str(uuid4())], "confirm": True, "generate_shortcuts": True}
        result = {"results": [{"status": "succeeded", "child": self.child.name}], "partial": False, "failed_count": 0}
        with patch.object(self.service, "execute", return_value=result) as execute:
            status, content, _headers = await request(self.app, "execute", body)
        execute.assert_called_once_with(body, settings=self.settings)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {"ok": True, **result})

    async def test_shortcut_preview_and_apply_envelopes_match_frontend_contract(self):
        refs = [{"work_key": "catalog#0", "press_key": "0:main::BDRip:Jsum"}]
        plan = {"plan_id": "fixture-plan", "creatable": 1, "conflicts": []}
        with patch.object(shortcuts, "preview_shortcuts", return_value=plan) as preview:
            status, content, _headers = await request(self.app, "shortcuts", {"refs": refs, "preview": True})
        preview.assert_called_once_with(refs, settings=self.settings)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {"ok": True, "shortcut_preview": plan})
        with patch.object(shortcuts, "apply_shortcuts", return_value={"created": 1, "failed_count": 0}) as apply:
            rejected, _content, _headers = await request(self.app, "shortcuts", {"refs": refs, "plan_id": "fixture-plan"})
            self.assertEqual(rejected, 400)
            apply.assert_not_called()
            status, content, _headers = await request(self.app, "shortcuts", {"refs": refs, "plan_id": "fixture-plan", "confirm": True})
        apply.assert_called_once_with(refs, "fixture-plan", settings=self.settings)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["created"], 1)

    async def test_errors_are_json_and_recorded_in_operation_details(self):
        for error, expected in ((ValueError("bad path fixture"), 400), (OSError("read denied fixture"), 500)):
            operation_id = str(uuid4())
            with self.subTest(error=error), patch.object(self.service, "scan", side_effect=error):
                status, content, _headers = await request(self.app, "scan", {}, headers=[(b"x-nimda-operation-id", operation_id.encode())])
            self.assertEqual(status, expected)
            self.assertEqual(json.loads(content)["error"], str(error))
            operation = self.app.state.operation_registry.snapshot(operation_id)
            self.assertEqual(operation["status"], "failed")
            self.assertIn(str(error), json.dumps(operation, ensure_ascii=False))

    async def test_non_object_body_and_cross_origin_requests_never_reach_service(self):
        with patch.object(self.service, "scan") as scan:
            for payload in (b"[]", b"null", b"invalid json"):
                status, _content, _headers = await request(self.app, "scan", raw=payload)
                self.assertEqual(status, 400)
            status, _content, _headers = await request(self.app, "scan", {}, headers=[(b"origin", b"https://foreign.invalid")])
            self.assertEqual(status, 403)
            scan.assert_not_called()

    async def test_chooser_selection_cancellation_and_invalid_initial_values(self):
        with patch.object(web, "choose_directory", return_value=str(self.root)) as picker:
            status, content, _headers = await request(self.app, "choose-directory", {"root": str(self.root)})
        picker.assert_called_once_with(str(self.root))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {"ok": True, "path": str(self.root), "cancelled": False})
        with patch.object(web, "choose_directory", return_value=""):
            status, content, _headers = await request(self.app, "choose-directory", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(content)["cancelled"])
        with patch.object(web, "choose_directory") as picker:
            for initial in (None, [], "x" * 4097):
                status, _content, _headers = await request(self.app, "choose-directory", {"root": initial})
                self.assertEqual(status, 400)
            status, _content, _headers = await request(self.app, "choose-directory", {}, headers=[(b"sec-fetch-site", b"cross-site")])
            self.assertEqual(status, 403)
            picker.assert_not_called()

    async def test_browser_mode_without_native_picker_returns_actionable_error(self):
        with patch.object(native_dialogs, "_picker", None):
            status, content, _headers = await request(self.app, "choose-directory", {})
        self.assertEqual(status, 400)
        self.assertIn("手动输入", json.loads(content)["error"])


class NativeDirectoryPickerTest(unittest.TestCase):
    def setUp(self):
        self.original = native_dialogs._picker
        self.addCleanup(native_dialogs.set_directory_picker, self.original)
        native_dialogs.set_directory_picker(None)

    def test_missing_picker_is_not_a_fake_success(self):
        with self.assertRaisesRegex(ValueError, "桌面应用"):
            native_dialogs.choose_directory()

    def test_picker_receives_initial_path_and_returns_selection_or_cancellation(self):
        picker = Mock(side_effect=[Path("fixture-selected"), None])
        native_dialogs.set_directory_picker(picker)
        self.assertEqual(native_dialogs.choose_directory("fixture-initial"), "fixture-selected")
        self.assertEqual(native_dialogs.choose_directory(), "")
        self.assertEqual(picker.call_args_list[0].args, ("fixture-initial",))

    def test_busy_picker_is_rejected_and_lock_is_released_after_completion(self):
        def nested(_initial):
            with self.assertRaisesRegex(ValueError, "已有目录选择窗口"):
                native_dialogs.choose_directory("second")
            return "first-selection"
        native_dialogs.set_directory_picker(nested)
        self.assertEqual(native_dialogs.choose_directory(), "first-selection")
        native_dialogs.set_directory_picker(lambda _initial: "next-selection")
        self.assertEqual(native_dialogs.choose_directory(), "next-selection")

    def test_picker_failure_does_not_leave_a_permanent_busy_lock(self):
        native_dialogs.set_directory_picker(Mock(side_effect=OSError("fixture chooser unavailable")))
        with self.assertRaisesRegex(OSError, "fixture chooser"):
            native_dialogs.choose_directory()
        native_dialogs.set_directory_picker(lambda _initial: "recovered")
        self.assertEqual(native_dialogs.choose_directory(), "recovered")


if __name__ == "__main__":
    unittest.main()
