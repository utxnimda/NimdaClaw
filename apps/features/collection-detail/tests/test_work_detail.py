from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collection_detail.work_detail import json_work_record, raw_work_records, work_detail_payload
from work_catalog_yaml.jp_tv import browse_api
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string


def _settings(root: Path, *paths: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=root,
        resolved_default_readable=str(paths[0]) if paths else None,
        resolved_catalog_yaml_paths=tuple(str(path) for path in paths),
        enum_options={},
        enum_labels={},
        enum_section_labels={},
        app_features=(),
    )


def _record(name: str = "完整作品") -> dict:
    return {
        "id": "custom-id",
        "extra": {"nested": [None, False, 3.5, {"html": "<script>not code</script>"}]},
        "attributes": [
            {"type": "date", "data": {"start": "20240101", "end": "20240331", "note": "保留日期额外信息"}},
            {
                "type": "collection-type", "description": "原始收集说明", "custom": "保留属性扩展",
                "data": {
                    "domain": "animation", "release_type": "tv", "path": "作品根目录",
                    "collectioned": [{
                        "press_format": "_BDRip", "press_group": "----", "press_path": "版本",
                        "note": "保留压制行额外字段",
                    }],
                    "markers": ["subs"], "custom_collection": {"retain": True},
                    "continuations": [{
                        "title": "附加篇", "note": "附加篇描述", "collectioned": [],
                    }],
                },
            },
            {"type": "country", "data": "japan"},
            {"type": "name", "data": name},
            {"type": "unknown-attribute", "data": {"details": [1, 2, 3]}},
        ],
    }


class WorkDetailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "db"
        self.root.mkdir()
        self.source = self.root / "catalog.yaml"
        self.record = _record()
        # Retain CRLF in the fixture to catch hashing re-encoded/read_text data.
        self.source.write_bytes(dump_yaml_string([self.record]).replace("\n", "\r\n").encode("utf-8"))
        self.settings = _settings(self.root, self.source)
        self.body = {
            "yaml_source_rel": self.source.name,
            "index_in_file": 0,
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
        }

    def _response(self, body: dict | None = None):
        with patch.object(browse_api, "get_resolved_browse_settings", return_value=(self.settings, None)):
            response = browse_api._post_collection_detail_work_detail_api.__wrapped__(body or self.body)
        return response.status_code, json.loads(response.body)

    def test_complete_original_record_is_read_only_and_preserves_extensions(self) -> None:
        before = self.source.read_bytes()
        status, payload = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(payload["record"], self.record)
        self.assertEqual(payload["source"], {
            "yaml_source_rel": self.source.name,
            "index_in_file": 0,
            "sha256": self.body["source_sha256"],
        })
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.root])
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_browse_hash_matches_exact_read_bytes_and_omits_full_db_records(self) -> None:
        payload, _ = browse_api._build_catalog_browse_payload(self.settings, [self.source])
        self.assertEqual(payload["sources_loaded"], [{
            "relpath": self.source.name, "count": 1, "sha256": self.body["source_sha256"],
        }])
        row = payload["profile_groups"][0]["rows"][0]
        self.assertNotIn("source_record", row)
        self.assertNotIn("extra", row)

    def test_changed_file_returns_conflict_before_index_can_read_a_different_work(self) -> None:
        self.source.write_text(dump_yaml_string([_record("插入在前的其他作品"), self.record]), encoding="utf-8")
        status, payload = self._response()
        self.assertEqual(status, 409)
        self.assertTrue(payload["reload_required"])
        self.assertEqual(payload["code"], "catalog-source-changed")
        self.assertIn("重新加载", payload["error"])
        self.assertNotIn("record", payload)

    def test_invalid_or_missing_hash_is_rejected(self) -> None:
        for digest in (None, "", "a" * 63, "z" * 64, 123, True):
            with self.subTest(digest=digest):
                status, _ = self._response({**self.body, "source_sha256": digest})
                self.assertEqual(status, 400)

    def test_invalid_indices_are_rejected_instead_of_coerced(self) -> None:
        for index in (None, True, False, -1, 0.0, "-1", "1.0", 1):
            with self.subTest(index=index):
                status, _ = self._response({**self.body, "index_in_file": index})
                self.assertEqual(status, 400)

    def test_traversal_absolute_and_basename_fallback_paths_are_rejected(self) -> None:
        for relative in (
            "../catalog.yaml", "nested/../catalog.yaml", "./catalog.yaml", "/catalog.yaml",
            "D:/catalog.yaml", "D:catalog.yaml", "\\\\server\\share\\catalog.yaml",
            "not-allowed/catalog.yaml", "nested//catalog.yaml", "catalog.yaml\x00",
            str(self.source),
        ):
            with self.subTest(relative=relative):
                status, _ = self._response({**self.body, "yaml_source_rel": relative})
                self.assertEqual(status, 400)

    def test_configured_nested_source_requires_its_full_relative_path(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        source = nested / "catalog.yaml"
        source.write_bytes(self.source.read_bytes())
        self.settings = _settings(self.root, source)
        status, _ = self._response()
        self.assertEqual(status, 400)
        status, payload = self._response({**self.body, "yaml_source_rel": "nested\\catalog.yaml"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"]["yaml_source_rel"], "nested/catalog.yaml")

    def test_unlisted_and_outside_root_files_cannot_be_read(self) -> None:
        outside = Path(self.temp.name) / "catalog.yaml"
        outside.write_bytes(self.source.read_bytes())
        for settings in (_settings(self.root), _settings(self.root, outside)):
            self.settings = settings
            status, _ = self._response()
            self.assertEqual(status, 400)

    def test_upload_retains_full_records_with_disambiguated_sources_and_is_readonly(self) -> None:
        other = _record("第二个上传作品")
        uploads = [("catalog.yaml", self.source.read_bytes()), ("catalog.yaml", dump_yaml_string([other]).encode("utf-8"))]
        with patch.object(browse_api, "get_resolved_browse_settings", return_value=(self.settings, None)):
            response = browse_api._post_browse_api.__wrapped__(uploads)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        rows = payload["profile_groups"][0]["rows"]
        self.assertFalse(payload["save"]["enabled"])
        self.assertEqual(rows[0]["source_record"], self.record)
        self.assertEqual(rows[1]["source_record"], other)
        self.assertNotEqual(rows[0]["yaml_source_rel"], rows[1]["yaml_source_rel"])
        self.assertEqual(payload["sources_loaded"][0]["sha256"], self.body["source_sha256"])

    def test_supported_document_roots_have_identical_record_selection(self) -> None:
        for document in ([self.record], {"works": [self.record]}, {"entries": [self.record]}):
            with self.subTest(root=type(document).__name__):
                self.assertIs(raw_work_records(document)[0], self.record)
                self.source.write_text(dump_yaml_string(document), encoding="utf-8")
                body = {**self.body, "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}
                self.assertEqual(work_detail_payload(body, self.settings)["record"], self.record)

    def test_yaml_dates_are_stable_readable_json_strings(self) -> None:
        record = {"date": date(2026, 9, 27), "timestamp": datetime(2026, 9, 27, 3, 4, tzinfo=timezone.utc)}
        self.assertEqual(json_work_record(record), {"date": "2026-09-27", "timestamp": "2026-09-27T03:04:00+00:00"})

    def test_unsupported_yaml_values_fail_explicitly_without_dropping_fields(self) -> None:
        for record in ({"extra": b"binary"}, {"extra": {1, 2}}, {"extra": float("nan")}, {1: "numeric key"}):
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "JSON"):
                json_work_record(record)
        recursive = {}
        recursive["child"] = recursive
        with self.assertRaisesRegex(ValueError, "循环"):
            json_work_record(recursive)

    def test_unsupported_upload_data_is_reported_as_a_readable_error(self) -> None:
        record = _record()
        record["extra"]["unsupported"] = float("inf")
        with patch.object(browse_api, "get_resolved_browse_settings", return_value=(self.settings, None)):
            response = browse_api._post_browse_api.__wrapped__([("catalog.yaml", dump_yaml_string([record]).encode("utf-8"))])
        self.assertEqual(response.status_code, 400)
        self.assertIn("非有限数值", json.loads(response.body)["error"])


if __name__ == "__main__":
    unittest.main()
