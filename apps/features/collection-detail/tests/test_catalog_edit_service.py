from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collection_detail import catalog_edit_service as service
from collection_detail import save
from collection_detail.payload import build_collectioned_ordered
from collection_detail.work_detail import raw_work_records
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string
from test_jp_tv_link_index import _settings, _catalog_yaml_mapped


def attribute(record: dict, kind: str) -> dict:
    return next(item for item in record["attributes"] if item["type"] == kind)


class CatalogEditServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.source = self.db / "[JP][TVInfo][2098].yaml"
        self.records = load_yaml_string(_catalog_yaml_mapped("First", (self.root / "future").as_posix(), "First_BDRip"))
        self.records += load_yaml_string(_catalog_yaml_mapped("Second", (self.root / "second").as_posix(), "Second_BDRip"))
        self.records[0]["external"] = {"id": 9, "tags": ["keep", "metadata"]}
        attribute(self.records[0], "date")["data"]["precision"] = "day"
        collection = attribute(self.records[0], "collection-type")
        collection["description"] = "keep-description"
        collection["data"]["extension"] = {"keep": True}
        collection["data"]["collectioned"][0]["codec"] = "AVC"
        self.source.write_text(dump_yaml_string({"works": self.records, "schema_note": "keep-root"}), encoding="utf-8")
        self.settings = _settings(self.db, self.source)

    def edit(self, index: int = 0) -> dict:
        item = service.catalog_records(self.settings)[index]
        return {"ref": item["ref"], "record": item["record"]}

    def disk_records(self) -> list:
        return raw_work_records(load_yaml_string(self.source.read_text(encoding="utf-8")))

    def test_read_preserves_complete_record_and_versions(self) -> None:
        item = service.catalog_records(self.settings)[0]
        self.assertEqual(item["record"], self.records[0])
        self.assertEqual(len(item["ref"]["source_sha256"]), 64)
        self.assertEqual(item["ref"]["record_sha256"], save.catalog_record_sha256(item["record"]))
        self.assertEqual(item["work_key"], self.source.name + "#0")
        self.assertTrue(item["press"][0]["press_key"])

    def test_record_directory_reuses_legacy_root_for_relative_db_paths(self) -> None:
        legacy = self.root / "legacy-media"
        record = self.edit()["record"]
        attribute(record, "collection-type")["data"]["path"] = "Family/Demo"
        record["path"] = "unrelated-top-level-extension"
        with patch("collection_detail.link_index._legacy_media_root", return_value=legacy) as root:
            self.assertEqual(service.resolve_record_directory(record), legacy / "Family" / "Demo")
            self.assertEqual(service.resolve_record_directory({"record": record}, "Demo_BDRip"),
                             legacy / "Family" / "Demo" / "Demo_BDRip")
            self.assertEqual(service.resolve_record_directory({"path": "Family/Demo"}, "Demo_BDRip"),
                             legacy / "Family" / "Demo" / "Demo_BDRip")
            self.assertTrue(all(call.kwargs == {"resolve_links": False} for call in root.call_args_list))
        self.assertFalse(legacy.exists())

    def test_record_directory_keeps_absolute_roots_without_resolving_links(self) -> None:
        absolute = self.root / "lexical-link-parent" / "Demo"
        with patch("collection_detail.link_index._legacy_media_root", return_value=self.root / "unused"), \
             patch.object(Path, "resolve", side_effect=AssertionError("Must not follow symbolic links")):
            self.assertEqual(service.resolve_record_directory({"path": str(absolute)}), absolute)
            self.assertEqual(service.resolve_record_directory({"path": str(absolute)}, "BDRip"), absolute / "BDRip")
        self.assertFalse(absolute.exists())

    def test_record_directory_rejects_traversal_missing_root_and_absolute_press_path(self) -> None:
        with patch("collection_detail.link_index._legacy_media_root", return_value=self.root / "legacy"):
            for record, press in (({"path": "../escape"}, None), ({"path": "Demo"}, "../escape"),
                                  ({"path": "Demo"}, str(self.root / "absolute")), ({}, None)):
                with self.subTest(record=record, press=press), self.assertRaises(ValueError):
                    service.resolve_record_directory(record, press)

    def test_preview_is_read_only_and_accepts_future_target_directory(self) -> None:
        edit = self.edit()
        attribute(edit["record"], "collection-type")["data"]["collectioned"][0]["press_path"] = "Renamed_BDRip"
        before = self.source.read_bytes()
        files = set(self.root.rglob("*"))
        result = service.preview_catalog_edits([edit], settings=self.settings)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["changes"][0]["action"], "update")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(set(self.root.rglob("*")), files)

    def test_apply_uses_original_saver_and_preserves_all_extensions(self) -> None:
        edit = self.edit()
        attribute(edit["record"], "name")["data"] = "Updated"
        edit["record"]["external"]["id"] = 10
        with patch.object(service, "browse_save_yaml_from_ui_body", wraps=save.browse_save_yaml_from_ui_body) as writer:
            result = service.apply_catalog_edits([edit], settings=self.settings)
        writer.assert_called_once()
        self.assertTrue(result["db_committed"])
        self.assertEqual(result["records"][0]["name"], "Updated")
        record = self.disk_records()[0]
        self.assertEqual(record["external"], {"id": 10, "tags": ["keep", "metadata"]})
        self.assertEqual(attribute(record, "collection-type")["data"]["collectioned"][0]["codec"], "AVC")
        self.assertEqual(attribute(record, "date")["data"]["precision"], "day")
        self.assertTrue(result["writes"][0]["history_name"])
        self.assertEqual(load_yaml_string(self.source.read_text(encoding="utf-8"))["schema_note"], "keep-root")

    def test_create_uses_server_country_year_and_compact_dates(self) -> None:
        record = self.edit()["record"]
        attribute(record, "name")["data"] = "New Work"
        attribute(record, "date")["data"].update({"start": "2098-02-01", "end": "2098-03-01"})
        result = service.apply_catalog_edits([{"ref": None, "record": record}], settings=self.settings)
        created = result["records"][0]
        self.assertEqual(created["ref"]["yaml_source_rel"], self.source.name)
        self.assertEqual(created["ref"]["index_in_file"], 2)
        self.assertEqual(attribute(created["record"], "date")["data"]["start"], "20980201")
        self.assertEqual(created["record"]["external"]["id"], 9)

    def test_new_raw_record_preview_and_apply_reuse_the_collection_save_pipeline(self) -> None:
        record = self.edit()["record"]
        attribute(record, "name")["data"] = "New through shared writer"
        attribute(record, "date")["data"].update({"start": "2098-02-01", "end": "2098-03-01"})
        edit = {"ref": None, "record": record}
        before = self.source.read_bytes()
        paths_before = set(self.root.rglob("*"))
        with patch.object(service, "preview_save_yaml_from_ui_body", wraps=save.preview_save_yaml_from_ui_body) as validator, \
             patch.object(save, "_browse_save_yaml_from_ui_body_unlocked", wraps=save._browse_save_yaml_from_ui_body_unlocked) as pipeline, \
             patch.object(save, "catalog_write_transaction", side_effect=AssertionError("Preview must not acquire a write lock")), \
             patch.object(save, "commit_file_writes", side_effect=AssertionError("Preview must not write")):
            preview = service.preview_catalog_edits([edit], settings=self.settings)
        self.assertEqual(preview["issues"], [])
        validator.assert_called_once()
        pipeline.assert_called_once()
        self.assertTrue(pipeline.call_args.kwargs["dry_run"])
        preview_body = validator.call_args.args[0]
        self.assertEqual(preview_body["rows"], [])
        self.assertEqual(len(preview_body["new_rows"]), 1)
        self.assertEqual(set(preview_body["new_rows"][0]), {"raw_record"})
        self.assertEqual(attribute(preview_body["new_rows"][0]["raw_record"], "date")["data"]["start"], "20980201")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(set(self.root.rglob("*")), paths_before)

        with patch.object(service, "browse_save_yaml_from_ui_body", wraps=save.browse_save_yaml_from_ui_body) as writer, \
             patch.object(save, "_browse_save_yaml_from_ui_body_unlocked", wraps=save._browse_save_yaml_from_ui_body_unlocked) as pipeline, \
             patch.object(save, "catalog_write_transaction", wraps=save.catalog_write_transaction) as lock, \
             patch.object(save, "commit_file_writes", wraps=save.commit_file_writes) as commit:
            applied = service.apply_catalog_edits([edit], settings=self.settings)
        writer.assert_called_once_with(preview_body, settings=self.settings)
        pipeline.assert_called_once()
        self.assertFalse(pipeline.call_args.kwargs.get("dry_run", False))
        lock.assert_called_once_with(self.db)
        commit.assert_called_once()
        staged = commit.call_args.args[0]
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].previous, before)
        self.assertEqual(staged[0].history_path.read_bytes(), before)
        self.assertTrue(applied["db_committed"])
        self.assertEqual(applied["records"][0]["ref"]["index_in_file"], 2)
        self.assertEqual(attribute(record, "date")["data"]["start"], "2098-02-01")

    def test_flat_and_raw_new_records_have_equivalent_storage_and_archiving(self) -> None:
        for country, code in (("japan", "JP"), ("korea", "KR")):
            for existing in (False, True):
                with self.subTest(country=country, existing=existing):
                    # Both input forms use identical nonexistent media paths;
                    # only isolated catalog YAMLs and their backups may change.
                    media_root = self.root / "not-created-media" / "Shared work"
                    flat = {"name": "Shared work", "country": country,
                            "date": {"start": "2097-04-03", "end": "2097-06-26"},
                            "domain": "animation", "release_type": "tv", "path": str(media_root),
                            "markers": ["fixture"], "yaml_source_rel": "../untrusted-client.yaml",
                            "collectioned_ordered": [
                                {"press_format": " BDRip ", "press_group": "", "press_path": "Shared\\BDRip"},
                                {"segment": "continuation", "continuation_index": 0, "continuation_title": "Extras",
                                 "press_format": " 1080p ", "press_group": " VCB ", "press_path": "Shared\\Web"}]}
                    raw = {"attributes": [
                        {"type": "date", "data": copy.deepcopy(flat["date"])},
                        {"type": "collection-type", "data": {
                            "domain": "animation", "release_type": "tv", "path": str(media_root),
                            "markers": ["fixture"],
                            "collectioned": [{"press_format": " BDRip ", "press_group": "", "press_path": "Shared\\BDRip"}],
                            "continuations": [{"title": "Extras", "collectioned": [
                                {"press_format": " 1080p ", "press_group": " VCB ", "press_path": "Shared\\Web"}]}]}},
                        {"type": "country", "data": country}, {"type": "name", "data": "Shared work"}]}
                    stored = []
                    for mode in ("flat", "raw"):
                        db = self.root / f"{country}-{existing}-{mode}" / "db"
                        db.mkdir(parents=True)
                        target = db / f"[{code}][TVInfo][2097].yaml"
                        if existing:
                            target.write_text("[]\n", encoding="utf-8")
                        settings = _settings(db)
                        if mode == "flat":
                            writes = save.browse_save_yaml_from_ui_body({"new_rows": [copy.deepcopy(flat)]}, settings=settings)
                        else:
                            result = service.apply_catalog_edits([{"ref": None, "record": copy.deepcopy(raw)}], settings=settings)
                            self.assertTrue(result["db_committed"])
                            self.assertEqual(result["issues"], [])
                            self.assertEqual(result["records"][0]["ref"]["yaml_source_rel"], target.name)
                            writes = [(Path(item["path"]), item["history_name"]) for item in result["writes"]]
                        self.assertEqual([path for path, _history in writes], [target])
                        self.assertEqual(bool(writes[0][1]), existing)
                        if existing:
                            self.assertEqual((save.history_catalog_root(settings) / writes[0][1]).read_text(encoding="utf-8"), "[]\n")
                        self.assertEqual(sorted(path.name for path in db.glob("*.yaml")), [target.name])
                        stored.append(raw_work_records(load_yaml_string(target.read_text(encoding="utf-8"))))
                    self.assertEqual(stored[0], stored[1])
                    self.assertEqual(attribute(stored[0][0], "date")["data"], {"start": "20970403", "end": "20970626"})
                    self.assertEqual(attribute(stored[0][0], "collection-type")["data"]["path"], media_root.as_posix())
                    self.assertFalse(media_root.exists())

    def test_new_record_shared_preview_validator_rejects_invalid_country_without_writes(self) -> None:
        record = self.edit()["record"]
        attribute(record, "country")["data"] = "unsupported-country"
        before = self.source.read_bytes()
        paths_before = set(self.root.rglob("*"))
        with patch.object(service, "preview_save_yaml_from_ui_body", wraps=save.preview_save_yaml_from_ui_body) as validator, \
             patch.object(save, "commit_file_writes", side_effect=AssertionError("Invalid records must not write")):
            preview = service.preview_catalog_edits([{"ref": None, "record": record}], settings=self.settings)
            self.assertTrue(preview["issues"][0]["blocking"])
            self.assertIn("不支持国家", preview["issues"][0]["message"])
            validator.assert_called_once()
            self.assertEqual(set(self.root.rglob("*")), paths_before)
            with self.assertRaisesRegex(ValueError, "不支持国家"):
                save.browse_save_yaml_from_ui_body(validator.call_args.args[0], settings=self.settings)
        self.assertEqual(self.source.read_bytes(), before)
        # The actual save entry acquires its shared lock even when validation
        # fails; no catalog or recovery snapshot should have been created.
        self.assertEqual(set(self.root.rglob("*")) - {self.db / ".nimda-catalog.lock"}, paths_before)

    def test_neighbor_change_can_rebind_but_target_change_is_rejected(self) -> None:
        first = self.edit(0)
        second = self.edit(1)
        attribute(first["record"], "name")["data"] = "First changed"
        service.apply_catalog_edits([first], settings=self.settings)
        attribute(second["record"], "name")["data"] = "Second changed"
        self.assertEqual(service.preview_catalog_edits([second], settings=self.settings)["issues"], [])
        service.apply_catalog_edits([second], settings=self.settings)
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "已变化"):
            service.apply_catalog_edits([first], settings=self.settings)
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_version_and_duplicate_record_ref_rejected(self) -> None:
        edit = self.edit()
        duplicate = service.preview_catalog_edits([edit, copy.deepcopy(edit)], settings=self.settings)
        self.assertTrue(duplicate["issues"][0]["blocking"])
        edit["ref"].pop("source_sha256")
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            service.apply_catalog_edits([edit], settings=self.settings)

    def test_unchanged_historical_date_allowed_changed_invalid_date_rejected(self) -> None:
        docs = self.disk_records()
        attribute(docs[0], "date")["data"]["start"] = "20980231"
        self.source.write_text(dump_yaml_string(docs), encoding="utf-8")
        edit = self.edit()
        attribute(edit["record"], "name")["data"] = "Rename only"
        result = service.preview_catalog_edits([edit], settings=self.settings)
        self.assertEqual(result["issues"], [])
        service.apply_catalog_edits([edit], settings=self.settings)
        edit = self.edit()
        attribute(edit["record"], "date")["data"]["start"] = "20980230"
        self.assertTrue(service.preview_catalog_edits([edit], settings=self.settings)["issues"])

    def test_post_commit_refresh_failure_is_marked_committed(self) -> None:
        current = service.catalog_records(self.settings)
        edit = {"ref": current[0]["ref"], "record": copy.deepcopy(current[0]["record"])}
        attribute(edit["record"], "name")["data"] = "Saved before refresh failed"
        with patch.object(service, "catalog_records", side_effect=[current, OSError("refresh fixture error")]):
            result = service.apply_catalog_edits([edit], settings=self.settings)
        self.assertTrue(result["db_committed"])
        self.assertEqual(result["issues"][0]["code"], "catalog-post-commit-refresh-failed")
        self.assertEqual(attribute(self.disk_records()[0], "name")["data"], "Saved before refresh failed")

    def test_table_patch_does_not_erase_unedited_extensions(self) -> None:
        current = self.edit()
        collection = attribute(current["record"], "collection-type")["data"]
        save.browse_save_yaml_from_ui_body({"rows": [{**current["ref"], "name": "Table edit",
            "date": {"start": "2098-01-01", "end": "2098-03-31"}, "domain": collection["domain"],
            "release_type": collection["release_type"], "path": collection["path"], "markers": [],
            "collectioned_ordered": [{"press_format": "BDRip", "press_group": "", "press_path": "First_BDRip"}]}]}, settings=self.settings)
        record = self.disk_records()[0]
        self.assertEqual(record["external"]["id"], 9)
        data = attribute(record, "collection-type")["data"]
        self.assertEqual(data["extension"], {"keep": True})
        self.assertEqual(data["collectioned"][0]["codec"], "AVC")
        self.assertEqual(attribute(record, "date")["data"]["precision"], "day")

    def test_unsafe_press_path_and_non_json_keys_rejected(self) -> None:
        edit = self.edit()
        attribute(edit["record"], "collection-type")["data"]["collectioned"][0]["press_path"] = "../escape"
        self.assertTrue(service.preview_catalog_edits([edit], settings=self.settings)["issues"])
        edit = self.edit()
        edit["record"][1] = "not a string key"
        self.assertTrue(service.preview_catalog_edits([edit], settings=self.settings)["issues"])

    def test_table_delete_and_edit_keeps_the_correct_row_and_continuation_metadata(self) -> None:
        docs = self.disk_records()
        collection = attribute(docs[0], "collection-type")["data"]
        collection["collectioned"].append({"press_format": "1080p", "press_group": "", "codec": "HEVC"})
        collection["continuations"] = [
            {"title": "Drop", "custom": "drop", "collectioned": [{"press_format": "DVDRip", "press_group": "", "codec": "DROP"}]},
            {"title": "Keep", "custom": "keep", "collectioned": [{"press_format": "BDRip", "press_group": "VCB", "codec": "KEEP"}]},
        ]
        self.source.write_text(dump_yaml_string(docs), encoding="utf-8")
        ordered = build_collectioned_ordered(collection)
        ordered = [ordered[1], ordered[3]]
        ordered[0]["press_format"] = "720p"
        ordered[1]["press_group"] = ""
        edit = self.edit()
        save.browse_save_yaml_from_ui_body({"rows": [{**edit["ref"], "domain": collection["domain"],
            "release_type": collection["release_type"], "path": collection["path"],
            "markers": [], "collectioned_ordered": ordered}]}, settings=self.settings)
        saved = attribute(self.disk_records()[0], "collection-type")["data"]
        self.assertEqual(saved["collectioned"][0]["codec"], "HEVC")
        self.assertEqual(saved["continuations"][0]["custom"], "keep")
        self.assertEqual(saved["continuations"][0]["collectioned"][0]["codec"], "KEEP")
        self.assertNotIn("_source_press_index", saved["collectioned"][0])

    def test_regular_save_and_delete_reject_stale_file_version(self) -> None:
        edit = self.edit()
        self.source.write_text(self.source.read_text(encoding="utf-8") + "\n# Other editor\n", encoding="utf-8")
        reference = {key: value for key, value in edit["ref"].items() if key != "record_sha256"}
        before = self.source.read_bytes()
        for body in ({"rows": [{**reference, "name": "Stale"}]}, {"deleted_rows": [reference]}):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "已变化"):
                save.browse_save_yaml_from_ui_body(body, settings=self.settings)
        self.assertEqual(self.source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
