from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
import os
from unittest.mock import patch

from catalog_library import service
from collection_detail import save
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


def work(name, start="20111001", path="Z:/Offline/Shared"):
    return {"attributes": [{"type": "name", "data": name}, {"type": "country", "data": "japan"},
            {"type": "date", "data": {"start": start, "end": ""}},
            {"type": "collection-type", "data": {"domain": "animation", "release_type": "tv", "path": path,
             "collectioned": [{"press_format": "BDRip", "press_group": "", "press_path": "Shared_BDRip"}]}}]}


class CatalogLibraryServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.source = self.db / "[JP][TVInfo][2011].yaml"
        self.raw = [work("Fate/Zero I"), work("Fate/Zero II", "20120401"), work("Unknown", "")]
        self.source.write_text(dump_yaml_string(self.raw), encoding="utf-8")
        self.library = self.root / "library"
        self.settings = JpTvBrowseSettings(version=1, filesystem_root=self.db, resolved_default_readable=str(self.source),
            resolved_catalog_yaml_paths=(str(self.source),), enum_options={}, enum_labels=MappingProxyType({"domain": MappingProxyType({"animation": "动画"})}),
            enum_section_labels={}, app_features=())
        with service._CACHE_LOCK:
            service._CACHE.clear()

    def call(self, name, body=None):
        return getattr(service, name)(body, settings=self.settings, data_root=self.library)

    def records(self):
        return load_yaml_string(self.source.read_text(encoding="utf-8"))

    def test_browse_and_detail_are_read_only_offline_and_keep_same_path_records(self):
        before = self.source.read_bytes()
        paths = set(self.root.rglob("*"))
        page = self.call("browse", {"page_size": 2})
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["legacy_count"], 3)
        self.assertEqual(page["items"][0]["name"], "Fate/Zero II")
        self.assertEqual(page["items"][0]["cover_url"], "")
        self.assertNotIn("record", page["items"][0])
        self.assertNotIn("press", page["items"][0])
        detail = self.call("detail", {"id": page["items"][0]["id"], "ref": page["items"][0]["ref"]})
        self.assertEqual(detail["resources"][0]["press_path"], "Shared_BDRip")
        self.assertEqual(detail["chapters"], [])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(set(self.root.rglob("*")), paths)

    def test_browse_caches_parse_but_invalidates_when_source_changes(self):
        with patch.object(service, "catalog_records", wraps=service.catalog_records) as reader:
            self.call("browse")
            self.call("browse", {"page": 2})
            self.assertEqual(reader.call_count, 1)
            self.raw.append(work("New"))
            self.source.write_text(dump_yaml_string(self.raw), encoding="utf-8")
            self.assertEqual(self.call("browse")["total"], 4)
            self.assertEqual(reader.call_count, 2)

    def test_query_and_pagination(self):
        self.assertEqual(self.call("browse", {"search": "fate/zero", "year": "2012"})["total"], 1)
        page = self.call("browse", {"page_size": 1, "page": 100, "sort": "name"})
        self.assertEqual(page["page"], 3)
        self.assertEqual(page["items"][0]["name"], "Unknown")
        for body in ({"page": True}, {"page_size": 0}, {"page_size": 101}, {"sort": "disk"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.call("browse", body)

    def test_equal_length_same_mtime_external_change_invalidates_cache(self):
        self.call("browse")
        timestamp = self.source.stat()
        self.source.write_bytes(self.source.read_bytes().replace(b"Unknown", b"Changed"))
        os.utime(self.source, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
        self.assertEqual(self.call("browse", {"search": "Changed"})["total"], 1)

    def test_catalog_membership_change_during_read_is_not_cached_under_old_paths(self):
        extra = self.db / "[JP][TVInfo][2012].yaml"
        original_reader = service.catalog_records

        def add_year_during_read(settings):
            extra.write_text(dump_yaml_string([work("Concurrent new file", "20120101")]), encoding="utf-8")
            return original_reader(settings)

        with patch.object(service, "catalog_records", side_effect=add_year_during_read):
            self.assertEqual(self.call("browse")["total"], 4)
        # Reverting the additional file must never retrieve its rows from a
        # cache entry fingerprinted before that file existed.
        extra.unlink()
        self.assertEqual(self.call("browse")["total"], 3)
        self.assertEqual(self.call("browse", {"search": "Concurrent new file"})["total"], 0)

    def test_legacy_identity_needs_current_version_and_rejects_boolean_index(self):
        item = self.call("browse")["items"][0]
        with self.assertRaises(ValueError):
            self.call("detail", {"id": item["id"]})
        with self.assertRaises(ValueError):
            self.call("detail", {"ref": {**item["ref"], "index_in_file": True}})
        self.source.write_text(dump_yaml_string(list(reversed(self.raw))), encoding="utf-8")
        first = self.call("browse")["items"][0]
        # Pick a moved record, not the middle record which happens to stay put.
        with self.assertRaises(ValueError):
            self.call("detail", {"ref": {**item["ref"], "index_in_file": 0}})

    def test_empty_identity_selection_does_not_hide_remaining_legacy_records(self):
        self.assertEqual(self.call("preview_identity_initialization", {"refs": []})["remaining"], 3)

    def test_nonstandard_legacy_extensions_have_safe_views_and_unchanged_raw_records(self):
        self.raw[0].update({"metadata": {"aliases": [1, "Alias"], "summary": {"custom": "old"},
                                       "episodes": [None, {"title": "Valid chapter"}]},
                            "source_refs": None,
                            "classifications": [{"type": "series", "value_id": "classification_" + "b" * 32, "order": "first"}]})
        self.raw[1].update({"metadata": None, "source_refs": [1, {"provider": "custom", "external_id": "1"}]})
        self.raw[2]["classifications"] = None
        self.source.write_text(dump_yaml_string(self.raw), encoding="utf-8")
        before = self.source.read_bytes()
        page = self.call("browse", {"search": "Alias", "sort": "series_order", "classification": "classification_" + "b" * 32})
        self.assertEqual(page["total"], 1)
        item = page["items"][0]
        self.assertEqual(item["aliases"], ["Alias"])
        self.assertEqual(item["summary"], "")
        self.assertEqual(item["source_count"], 0)
        self.assertNotIn("order", item["classifications"][0])
        self.assertTrue(item["warnings"])
        detail = self.call("detail", {"ref": item["ref"]})
        self.assertEqual(detail["record"], self.raw[0])
        self.assertEqual(detail["chapters"], [{"title": "Valid chapter"}])
        self.assertEqual(detail["sources"], [])
        self.assertTrue(detail["warnings"])
        self.assertEqual(self.call("browse")["total"], 3)
        self.assertEqual(self.source.read_bytes(), before)

    def test_classification_directory_junctions_are_rejected_before_read_or_write(self):
        # Mock reparse detection so this safety test also runs on hosts without
        # permission to create Windows symlinks/junctions.
        with patch.object(Path, "is_junction", lambda path: path == self.library / "db", create=True):
            with self.assertRaisesRegex(ValueError, "符号链接或联接"):
                self.call("classifications_payload")
            with self.assertRaisesRegex(ValueError, "符号链接或联接"):
                self.call("save_classification", {"revision": "", "name": "Unsafe"})
        revision = self.call("classifications_payload")["revision"]
        with patch.object(Path, "is_junction", lambda path: path == self.library / "history", create=True):
            with self.assertRaisesRegex(ValueError, "符号链接或联接"):
                self.call("save_classification", {"revision": revision, "name": "Unsafe"})
        self.assertFalse(self.library.exists())

    def test_classification_parent_regular_file_is_rejected(self):
        self.library.mkdir()
        (self.library / "db").write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不是普通目录"):
            self.call("classifications_payload")

    def test_unknown_dates_do_not_use_storage_year_as_actual_date(self):
        page = self.call("browse", {"sort": "date_asc"})
        unknown = page["items"][-1]
        self.assertEqual(unknown["name"], "Unknown")
        self.assertEqual(unknown["begin_date"], "")
        self.assertEqual(unknown["year"], "2011")

    def test_explicit_identity_preview_then_shared_apply_with_history(self):
        before = self.source.read_bytes()
        paths = set(self.root.rglob("*"))
        preview = self.call("preview_identity_initialization")
        self.assertEqual(len(preview["edits"]), 3)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(set(self.root.rglob("*")), paths)
        with patch.object(service, "apply_catalog_edits", wraps=service.apply_catalog_edits) as writer:
            result = self.call("apply_edits", {"edits": preview["edits"]})
            writer.assert_called_once()
        self.assertTrue(result["db_committed"])
        self.assertEqual([item["id"] for item in self.records()], [item["record"]["id"] for item in preview["edits"]])
        self.assertEqual(self.call("browse")["legacy_count"], 0)
        self.assertEqual(self.call("preview_identity_initialization")["edits"], [])
        self.assertTrue(list((self.root / "History").glob("*.yaml")))

    def test_stable_id_survives_reorder_and_raw_legacy_editor_preserves_extensions(self):
        preview = self.call("preview_identity_initialization")
        self.call("apply_edits", {"edits": preview["edits"]})
        current = self.records()
        identity = current[0]["id"]
        current.reverse()
        self.source.write_text(dump_yaml_string(current), encoding="utf-8")
        detail = self.call("detail", {"id": identity})
        self.assertEqual(detail["item"]["name"], "Fate/Zero I")
        raw = deepcopy(detail["record"])
        raw.pop("id")
        raw.pop("schema_version")
        normalized = save.normalize_raw_record(raw, previous=detail["record"])
        self.assertEqual(normalized["id"], identity)
        with self.assertRaisesRegex(ValueError, "不允许更改"):
            save.normalize_raw_record({**raw, "id": "work_" + "f" * 32}, previous=detail["record"])

    def test_old_table_and_raw_saves_preserve_new_metadata_fields(self):
        extended = deepcopy(self.raw[0])
        extended.update({"id": "work_" + "a" * 32, "schema_version": 1,
                         "metadata": {"summary": "User-written", "aliases": ["Zero"]},
                         "source_refs": [{"provider": "bangumi", "external_id": "100"}],
                         "classifications": [{"type": "series", "value_id": "classification_" + "b" * 32}]})
        self.source.write_text(dump_yaml_string([extended, *self.raw[1:]]), encoding="utf-8")
        detail = self.call("detail", {"id": extended["id"]})
        save.browse_save_yaml_from_ui_body({"rows": [{**detail["ref"], "name": "Table edit"}]}, settings=self.settings)
        after = self.records()[0]
        for key in ("id", "schema_version", "metadata", "source_refs", "classifications"):
            self.assertEqual(after[key], extended[key])
        current = self.call("detail", {"id": extended["id"]})
        raw = deepcopy(current["record"])
        for key in ("id", "schema_version", "metadata", "source_refs", "classifications"):
            raw.pop(key)
        raw["attributes"][0]["data"] = "Raw edit"
        save.browse_save_yaml_from_ui_body({"rows": [{**current["ref"], "raw_record": raw}]}, settings=self.settings)
        after = self.records()[0]
        for key in ("id", "schema_version", "metadata", "source_refs", "classifications"):
            self.assertEqual(after[key], extended[key])

    def test_classification_lifecycle_and_membership_are_separate(self):
        original = self.call("classifications_payload")
        created = self.call("save_classification", {"revision": original["revision"], "name": "Fate/Zero"})
        classification = created["item"]
        refs = [item["ref"] for item in self.call("browse", {"search": "Fate/Zero"})["items"]]
        preview = self.call("preview_classification_edits", {"refs": refs, "value_id": classification["id"], "action": "assign", "order": 1})
        self.call("apply_edits", {"edits": preview["edits"]})
        grouped = self.call("browse", {"classification": classification["id"], "sort": "series_order"})
        self.assertEqual(grouped["total"], 2)
        before = self.source.read_bytes()
        renamed = self.call("save_classification", {"revision": created["revision"], "id": classification["id"], "name": "Fate Zero"})
        self.assertEqual(renamed["item"]["id"], classification["id"])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(list((self.library / "history").glob("*.json")))
        preview = self.call("preview_classification_edits", {"refs": [grouped["items"][0]["ref"]], "value_id": classification["id"], "action": "remove"})
        self.call("apply_edits", {"edits": preview["edits"]})
        self.assertEqual(self.call("browse", {"classification": classification["id"]})["total"], 1)
        self.assertEqual(self.call("browse")["total"], 3)

    def test_classification_revision_and_duplicate_name_are_checked(self):
        revision = self.call("classifications_payload")["revision"]
        created = self.call("save_classification", {"revision": revision, "name": "Series"})
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.call("save_classification", {"revision": revision, "name": "Another"})
        with self.assertRaisesRegex(ValueError, "同名"):
            self.call("save_classification", {"revision": created["revision"], "name": " SERIES "})

    def test_stale_preview_and_duplicate_identity_are_rejected(self):
        preview = self.call("preview_identity_initialization")
        self.raw[0]["attributes"][0]["data"] = "Changed"
        self.source.write_text(dump_yaml_string(self.raw), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "变化"):
            self.call("apply_edits", {"edits": preview["edits"]})
        preview = self.call("preview_identity_initialization")
        preview["edits"][1]["record"]["id"] = preview["edits"][0]["record"]["id"]
        with self.assertRaisesRegex(ValueError, "已被其他"):
            self.call("apply_edits", {"edits": preview["edits"]})

    def test_legacy_new_row_gets_id_and_cannot_clone_an_existing_id(self):
        save.browse_save_yaml_from_ui_body({"new_rows": [{"raw_record": work("New")} ]}, settings=self.settings)
        new = self.records()[-1]
        self.assertRegex(new["id"], r"^work_[0-9a-f]{32}$")
        self.assertEqual(new["schema_version"], 1)
        with self.assertRaisesRegex(ValueError, "重复"):
            save.browse_save_yaml_from_ui_body({"new_rows": [{"raw_record": new}]}, settings=self.settings)


if __name__ == "__main__":
    unittest.main()
