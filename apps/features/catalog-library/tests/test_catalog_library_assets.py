from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import ssl
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zlib

from PIL import Image, ImageFile

from catalog_library import assets, providers, service, web
from catalog_library.model import validate_extensions
from catalog_library.network import failure_reason
from collection_detail.catalog_edit_service import catalog_records
from work_catalog_yaml.yaml_io import dump_yaml_string
import test_catalog_library_providers as provider_fixtures


SOURCE = "https://lain.bgm.tv/pic/cover/l/12/34/12_Abc.jpg"


def encoded_image(kind="PNG", width=1, height=1, *, frames=1):
    stream = io.BytesIO()
    images = [Image.new("RGB", (width, height), (index * 30 % 255, 50, 70)) for index in range(frames)]
    try:
        if frames == 1:
            images[0].save(stream, format=kind)
        else:
            images[0].save(stream, format=kind, save_all=True, append_images=images[1:], duration=50, loop=0)
        return stream.getvalue()
    finally:
        for image in images:
            image.close()


def png(width=1, height=1):
    return encoded_image("PNG", width, height)


def png_header_only(width=1, height=1):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IEND", b"")


def header_only_images():
    jpeg = b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x02\x00\x03\x01\xff\xd9"
    gif = b"GIF89a\x03\x00\x02\x00\x00\x00\x00;"
    block = b"\x00\x00\x00\x00\x02\x00\x00\x01\x00\x00"
    webp = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10) + block
    return ((jpeg, "image/jpeg"), (gif, "image/gif"), (webp, "image/webp"), (png_header_only(), "image/png"))


class AssetSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "assets"
        self.data = png()
        self.subject = {"id": 12, "images": {"large": SOURCE}}

    def acquire(self):
        with patch.object(assets, "_download", return_value=(self.data, "image/png")):
            return assets.acquire_cover(self.subject, user_agent="test", root=self.root)

    def response(self, data=None, **headers):
        response = io.BytesIO(self.data if data is None else data)
        response.headers = {"Content-Type": "image/png", **headers}
        return response

    def test_official_cdn_and_resize_urls_only(self):
        for url in (SOURCE, SOURCE.replace("/pic/", "/r/400/pic/"), SOURCE.replace("/pic/", "/r/400x600/pic/")):
            self.assertEqual(assets.validate_source_url(url), url)
        for url in ("http://lain.bgm.tv/pic/cover/l/x.jpg", SOURCE.replace("lain.bgm.tv", "evil.lain.bgm.tv"),
                    SOURCE.replace("lain.bgm.tv", "127.0.0.1"), SOURCE + "?url=evil", SOURCE + "#x",
                    SOURCE.replace("/pic/", "/../pic/"), SOURCE.replace("/pic/", "/%70ic/"),
                    SOURCE.replace("https://", "https://user@"), SOURCE.replace("lain.bgm.tv", "lain.bgm.tv:8080"),
                    "file:///tmp/a.png", "data:image/png;base64,x", SOURCE.replace("cover", "user"), " " + SOURCE):
            with self.subTest(url=url), self.assertRaises(ValueError), patch.object(assets, "build_opener") as opener:
                assets._download(url, user_agent="test")
            opener.assert_not_called()

    def test_redirect_refused(self):
        with self.assertRaisesRegex(ValueError, "重定向"):
            assets._NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.test/a.png")

    def test_image_signatures_dimensions_and_mime(self):
        self.assertEqual(assets.inspect_image(self.data, "image/png"), {"extension": "png", "mime_type": "image/png", "width": 1, "height": 1})
        for kind, mime in (("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp"), ("PNG", "image/png")):
            with self.subTest(mime=mime):
                data = encoded_image(kind, 3, 2)
                info = assets.inspect_image(data, mime)
                self.assertEqual((info["width"], info["height"]), (3, 2))
        for data, mime in ((b"<html>bad</html>", "image/jpeg"), (b"<svg></svg>", "image/svg+xml"),
                           (self.data, "image/jpeg"), (self.data[:40], "image/png"), (png_header_only(12001), "image/png"),
                           (png_header_only(10000, 10000), "image/png"), (png_header_only(0), "image/png")):
            with self.subTest(mime=mime, size=len(data)), self.assertRaises(ValueError):
                assets.inspect_image(data, mime)

    def test_all_header_only_images_are_rejected_without_creating_assets(self):
        for data, mime in header_only_images():
            with self.subTest(mime=mime), patch.object(assets, "_download", return_value=(data, mime)), self.assertRaises(ValueError):
                assets.acquire_cover(self.subject, user_agent="test", root=self.root)
        self.assertFalse(self.root.exists())

    def test_real_images_with_truncated_pixel_data_are_rejected(self):
        for kind, mime in (("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp"), ("PNG", "image/png")):
            data = encoded_image(kind, 20, 20)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                assets.inspect_image(data[:len(data) // 2], mime)

    def test_dimensions_checked_before_decode_and_frames_are_bounded(self):
        data = png(3, 2)
        with patch.object(assets, "MAX_DIMENSION", 2), patch.object(Image.Image, "load", side_effect=AssertionError("must not decode")), self.assertRaisesRegex(ValueError, "尺寸"):
            assets.inspect_image(data, "image/png")
        animation = encoded_image("GIF", 3, 2, frames=3)
        self.assertEqual(assets.inspect_image(animation, "image/gif")["width"], 3)
        with patch.object(assets, "MAX_FRAMES", 2), self.assertRaisesRegex(ValueError, "帧数"):
            assets.inspect_image(animation, "image/gif")
        with patch.object(assets, "MAX_DECODED_PIXELS", 8), self.assertRaisesRegex(ValueError, "累计"):
            assets.inspect_image(animation, "image/gif")

    def test_missing_dependency_and_unsafe_decoder_setting_fail_helpfully(self):
        with patch.dict("sys.modules", {"PIL": None}), self.assertRaisesRegex(RuntimeError, "Pillow"):
            assets.inspect_image(self.data, "image/png")
        with patch.object(ImageFile, "LOAD_TRUNCATED_IMAGES", True), self.assertRaisesRegex(RuntimeError, "截断"):
            assets.inspect_image(self.data, "image/png")

    def test_content_addressed_asset_is_reused_without_overwrite(self):
        first = self.acquire()
        self.assertEqual(first["sha256"], hashlib.sha256(self.data).hexdigest())
        with patch.object(assets, "atomic_write_bytes", side_effect=AssertionError("must reuse")):
            second = self.acquire()
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.root.glob("*"))), 1)
        self.assertEqual(assets.cover_url(first), f"/api/catalog-library/assets/{first['sha256']}.png")

    def test_offline_read_does_not_contact_provider_or_create_folders(self):
        cover = self.acquire()
        paths = set(self.root.rglob("*"))
        with patch.object(assets, "build_opener", side_effect=AssertionError("no network")):
            self.assertEqual(assets.read_asset(f"{cover['sha256']}.png", root=self.root), (self.data, "image/png"))
        self.assertEqual(set(self.root.rglob("*")), paths)
        absent = self.root / "missing"
        with self.assertRaises(ValueError):
            assets.read_asset("a" * 64 + ".png", root=absent)
        self.assertFalse(absent.exists())

    def test_asset_paths_traversal_and_nonimage_formats_rejected(self):
        for name in ("../secret.txt", "a" * 64 + ".svg", "A" * 64 + ".jpg", "a" * 64 + ".jpg:stream",
                     "/" + "a" * 64 + ".jpg", "\\" + "a" * 64 + ".jpg", "x.png"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.read_asset(name, root=self.root)

    def test_corrupted_content_address_rejected_and_not_overwritten(self):
        cover = self.acquire()
        path = self.root / (cover["sha256"] + ".png")
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            assets.read_asset(path.name, root=self.root)
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.acquire()
        self.assertEqual(path.read_bytes(), b"corrupt")

    def test_symlinks_rejected_for_file_and_parent(self):
        real = Path(self.temp.name) / "real"
        real.mkdir()
        linked = Path(self.temp.name) / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("host cannot create symbolic links")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            assets.asset_root(linked / "assets")
        self.root.mkdir()
        (real / "cover").write_bytes(self.data)
        name = hashlib.sha256(self.data).hexdigest() + ".png"
        (self.root / name).symlink_to(real / "cover")
        with self.assertRaises(ValueError):
            assets.read_asset(name, root=self.root)

    def test_download_headers_timeout_and_size(self):
        with patch.object(assets, "build_opener") as opener:
            opener.return_value.open.return_value = self.response(**{"Content-Length": str(len(self.data))})
            data, mime = assets._download(SOURCE, user_agent="test-agent")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("User-agent"), "test-agent")
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
            self.assertLessEqual(opener.return_value.open.call_args.kwargs["timeout"], assets.SOCKET_TIMEOUT)
            self.assertEqual((data, mime), (self.data, "image/png"))
        for headers in ({"Content-Type": "text/html"}, {"Content-Type": "image/svg+xml"},
                        {"Content-Encoding": "gzip"}, {"Content-Length": "9999999999"}, {"Content-Length": "bad"},
                        {"Content-Length": "1"}):
            with self.subTest(headers=headers), patch.object(assets, "build_opener") as opener, self.assertRaises(ValueError):
                opener.return_value.open.return_value = self.response(**headers)
                assets._download(SOURCE, user_agent="test")

    def test_chunked_response_limit_and_deadline(self):
        with patch.object(assets, "build_opener") as opener, patch.object(assets, "MAX_IMAGE_BYTES", 3), self.assertRaisesRegex(ValueError, "大小限制"):
            opener.return_value.open.return_value = self.response()
            assets._download(SOURCE, user_agent="test")
        with patch.object(assets.time, "monotonic", return_value=100), patch.object(assets, "build_opener") as opener, self.assertRaisesRegex(ValueError, "总时限"):
            assets._download(SOURCE, user_agent="test", deadline=99)
        opener.return_value.open.assert_not_called()

    def test_network_failure_is_sanitized_and_creates_no_assets(self):
        with patch.object(assets, "build_opener") as opener, self.assertRaisesRegex(ValueError, "连接失败") as failure:
            opener.return_value.open.side_effect = URLError("private detail")
            assets.acquire_cover(self.subject, user_agent="test", root=self.root)
        self.assertNotIn("private", str(failure.exception))
        self.assertFalse(self.root.exists())

    def test_network_diagnostics_distinguish_tls_timeout_dns_and_refusal(self):
        for error, expected in ((ssl.SSLEOFError("private URL credentials"), "TLS"),
                                (TimeoutError("private URL credentials"), "超时"),
                                (socket.gaierror("private URL credentials"), "域名解析"),
                                (ConnectionRefusedError("private URL credentials"), "连接被拒绝")):
            with self.subTest(error=type(error).__name__):
                self.assertIn(expected, failure_reason(URLError(error)))
                self.assertNotIn("private", failure_reason(URLError(error)))
                with patch.object(assets, "build_opener") as opener, self.assertRaises(ValueError) as failure:
                    opener.return_value.open.side_effect = URLError(error)
                    assets._download(SOURCE, user_agent="test")
                self.assertIn(expected, str(failure.exception))
                self.assertNotIn("credentials", str(failure.exception))

    def test_cover_reference_never_accepts_url_as_browser_source(self):
        for cover in (None, SOURCE, {"url": SOURCE}, {"sha256": "../secret", "extension": "png"}):
            self.assertEqual(assets.cover_url(cover), "")
            with self.assertRaises(ValueError):
                validate_extensions({"metadata": {"cover": cover}})


class CoverImportTest(unittest.TestCase):
    setUp = provider_fixtures.ProviderImportTest.setUp
    ref = provider_fixtures.ProviderImportTest.ref

    def preview(self, *, cover_only=False):
        subject = {**self.subject, "images": {"large": SOURCE}}
        responses = [subject] if cover_only else [subject, self.page]
        handler = providers.preview_provider_cover if cover_only else providers.preview_provider_import
        with patch.object(providers, "_request_json", side_effect=deepcopy(responses)) as requests, patch.object(assets, "_download", return_value=(png(), "image/png")):
            result = handler({"ref": self.ref(), "subject_id": 12}, settings=self.settings, source_root=self.sources, assets_root=self.root / "assets")
        if cover_only:
            self.assertEqual(requests.call_count, 1)
        return result

    def apply(self, preview, fields=None):
        return providers.apply_provider_import({"ref": preview["ref"], "snapshot_id": preview["snapshot_id"], "preview_token": preview["preview_token"],
            "selected_fields": ["metadata.cover"] if fields is None else fields}, settings=self.settings, source_root=self.sources, assets_root=self.root / "assets")

    def test_cover_preview_is_local_optional_and_applies_through_shared_writer(self):
        before = self.file.read_bytes()
        preview = self.preview()
        row = next(row for row in preview["diff"] if row["field"] == "metadata.cover")
        self.assertTrue(row["selectable"])
        self.assertTrue(row["after_url"].startswith("/api/catalog-library/assets/"))
        self.assertEqual(preview["subject"]["cover_url"], row["after_url"])
        self.assertEqual(before, self.file.read_bytes())
        with patch.object(providers.service, "apply_edits", wraps=providers.service.apply_edits) as writer:
            self.apply(preview)
        writer.assert_called_once()
        record = catalog_records(self.settings)[0]["record"]
        self.assertEqual(record["metadata"]["cover"], row["after"])
        self.assertEqual(record["attributes"], self.record["attributes"])
        with patch.object(assets, "build_opener", side_effect=AssertionError("offline")):
            page = service.browse(settings=self.settings, data_root=self.root / "library")
            self.assertEqual(page["items"][0]["cover_url"], row["after_url"])
            detail = service.detail({"id": page["items"][0]["id"]}, settings=self.settings, data_root=self.root / "library")
            self.assertEqual(detail["item"]["cover_url"], row["after_url"])

    def test_unselected_cover_never_enters_authoritative_record(self):
        self.apply(self.preview(), ["metadata.summary"])
        self.assertNotIn("cover", catalog_records(self.settings)[0]["record"]["metadata"])

    def test_cover_only_requires_bound_source_without_network(self):
        with patch.object(providers, "_request_json", side_effect=AssertionError("no network")), self.assertRaisesRegex(ValueError, "已关联"):
            providers.preview_provider_cover({"ref": self.ref(), "subject_id": 12}, settings=self.settings, source_root=self.sources)
        with patch.object(providers, "_request_json", side_effect=AssertionError("no network")), self.assertRaisesRegex(ValueError, "唯一"):
            providers.preview_provider_cover({"ref": self.ref()}, settings=self.settings, source_root=self.sources)

    def test_cover_only_updates_no_other_fields_or_existing_range_snapshot(self):
        self.record["metadata"]["aliases"] = ["My manual alias"]
        self.record["metadata"]["field_sources"] = {"metadata.aliases": {"provider": "manual", "note": "keep"}}
        self.record["source_refs"] = [{"provider": "bangumi", "external_id": "12", "scope": "episodes:13-24", "episode_range": {"start": 13, "end": 24},
            "snapshot_id": "b" * 64, "fields": ["metadata.episodes"]}]
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        preview = self.preview(cover_only=True)
        self.assertTrue(preview["cover_only"])
        self.assertEqual(preview["automatic_aliases"], [])
        self.assertEqual([row["field"] for row in preview["diff"]], ["metadata.cover"])
        self.apply(preview)
        record = catalog_records(self.settings)[0]["record"]
        self.assertEqual(record["metadata"]["summary"], "My description")
        self.assertEqual(record["metadata"]["aliases"], ["My manual alias"])
        self.assertEqual(record["metadata"]["field_sources"]["metadata.aliases"], {"provider": "manual", "note": "keep"})
        self.assertNotIn("episodes", record["metadata"])
        self.assertEqual(len(record["source_refs"]), 1)
        self.assertEqual(record["source_refs"][0]["snapshot_id"], "b" * 64)
        self.assertEqual(record["source_refs"][0]["scope"], "episodes:13-24")
        self.assertEqual(record["source_refs"][0]["cover_snapshot_id"], preview["snapshot_id"])
        before = self.file.read_bytes()
        self.assertTrue(self.apply(preview)["unchanged"])
        self.assertEqual(self.file.read_bytes(), before)

    def test_cover_only_cannot_adopt_unpreviewed_fields_or_empty_selection(self):
        self.apply(self.preview(), [])
        preview = self.preview(cover_only=True)
        for fields in ([], ["name"], ["metadata.cover", "metadata.summary"]):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "只能"):
                self.apply(preview, fields)

    def test_missing_asset_after_preview_does_not_write_cover_reference(self):
        preview = self.preview()
        before = self.file.read_bytes()
        with patch.object(assets, "read_asset", side_effect=ValueError("封面资源不存在")), self.assertRaisesRegex(ValueError, "不存在"):
            self.apply(preview)
        self.assertEqual(self.file.read_bytes(), before)

    def test_download_failure_preserves_existing_cover_and_other_fields_can_apply(self):
        self.apply(self.preview())
        before = catalog_records(self.settings)[0]["record"]["metadata"]["cover"]
        with patch.object(providers, "_request_json", side_effect=deepcopy([self.subject, self.page])), patch.object(assets, "acquire_cover", side_effect=ValueError("超时")):
            preview = providers.preview_provider_import({"ref": self.ref(), "subject_id": 12}, settings=self.settings, source_root=self.sources, assets_root=self.root / "assets")
        row = next(row for row in preview["diff"] if row["field"] == "metadata.cover")
        self.assertFalse(row["selectable"])
        self.assertIn("超时", " ".join(preview["warnings"]))
        with self.assertRaisesRegex(ValueError, "为空或无效"):
            self.apply(preview)
        self.apply(preview, ["metadata.summary"])
        self.assertEqual(catalog_records(self.settings)[0]["record"]["metadata"]["cover"], before)

    def test_corrupted_cover_snapshot_is_rejected(self):
        preview = self.preview()
        path = self.sources / (preview["snapshot_id"] + ".json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["cover"]["sha256"] = "a" * 64
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.apply(preview)

    def test_header_only_downloads_cannot_become_snapshot_cover_or_replace_existing(self):
        self.apply(self.preview())
        previous_cover = deepcopy(catalog_records(self.settings)[0]["record"]["metadata"]["cover"])
        for data, mime in header_only_images():
            with self.subTest(mime=mime), patch.object(providers, "_request_json", side_effect=deepcopy([self.subject, self.page])), patch.object(assets, "_download", return_value=(data, mime)):
                preview = providers.preview_provider_import({"ref": self.ref(), "subject_id": 12}, settings=self.settings,
                    source_root=self.sources, assets_root=self.root / "assets")
            snapshot = json.loads((self.sources / (preview["snapshot_id"] + ".json")).read_text(encoding="utf-8"))
            self.assertNotIn("cover", snapshot)
            row = next(row for row in preview["diff"] if row["field"] == "metadata.cover")
            self.assertFalse(row["selectable"])
            with self.assertRaisesRegex(ValueError, "为空或无效"):
                self.apply(preview)
            self.apply(preview, ["metadata.summary"])
            self.assertEqual(catalog_records(self.settings)[0]["record"]["metadata"]["cover"], previous_cover)


class AssetResponseTest(unittest.IsolatedAsyncioTestCase):
    async def test_offline_response_head_etag_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = png()
            digest = hashlib.sha256(data).hexdigest()
            filename = digest + ".png"
            (root / filename).write_bytes(data)

            class Queue:
                async def run(self, handler, name):
                    return handler(name, root=root)

            request = SimpleNamespace(path_params={"filename": filename}, method="GET", headers={},
                                      app=SimpleNamespace(state=SimpleNamespace(api_disk_queue=Queue())))
            with patch.object(assets, "build_opener", side_effect=AssertionError("GET must be offline")):
                response = await web.asset(request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.body, data)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn("immutable", response.headers["cache-control"])
                self.assertIn("sandbox", response.headers["content-security-policy"])
                self.assertEqual(response.headers["etag"], '"' + digest + '"')
                request.method = "HEAD"
                response = await web.asset(request)
                self.assertEqual(response.body, b"")
                self.assertEqual(response.headers["content-length"], str(len(data)))
                request.method = "GET"
                request.headers = {"if-none-match": response.headers["etag"]}
                response = await web.asset(request)
                self.assertEqual(response.status_code, 304)
                self.assertEqual(response.body, b"")
                (root / filename).write_bytes(b"corrupt")
                response = await web.asset(request)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
