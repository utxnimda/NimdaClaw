from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from catalog_library import providers
from collection_detail.catalog_edit_service import catalog_records
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


class ProviderImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db"
        self.db.mkdir()
        self.file = self.db / "[JP][TVInfo][2020].yaml"
        self.record = {"attributes": [
            {"type": "name", "data": "Local title"},
            {"type": "country", "data": "japan"},
            {"type": "date", "data": {"start": "20200101", "end": "20201231"}},
            {"type": "collection-type", "data": {"domain": "animation", "release_type": "tv",
             "path": "Z:/Keep Media", "collectioned": [{"press_format": "BDRip", "press_group": "VCB",
             "press_path": "Keep_BDRip"}], "markers": []}}],
            "metadata": {"summary": "My description", "custom": "keep"}}
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        self.settings = JpTvBrowseSettings(version=1, filesystem_root=self.db,
            resolved_default_readable=str(self.file), resolved_catalog_yaml_paths=(str(self.file),),
            enum_options={}, enum_labels={}, enum_section_labels={}, app_features=())
        self.sources = self.root / "sources"
        self.subject = {"id": 12, "type": 2, "name": "Source title", "name_cn": "中文名称",
                        "summary": "Source description", "date": "2020-02-01", "nsfw": False,
                        "images": {"large": "https://example.com/not-loaded.jpg"},
                        "infobox": [{"key": "别名", "value": [{"v": "Alias"}, {"v": "中文名称"}]}]}
        self.episode = {"id": 35, "subject_id": 12, "type": 0, "sort": 1, "ep": 1,
                        "name": "Episode title", "name_cn": "第一集", "airdate": "2020-02-01", "duration": "24m"}
        self.page = {"total": 1, "limit": 100, "offset": 0, "data": [self.episode]}

    def ref(self):
        return catalog_records(self.settings)[0]["ref"]

    def preview(self, subject=None, pages=None, episode_range=None, ref=None):
        with patch.object(providers, "_request_json", side_effect=deepcopy([subject or self.subject, *(pages or [self.page])])):
            body = {"ref": ref or self.ref(), "subject_id": 12}
            if episode_range is not None:
                body["episode_range"] = episode_range
            return providers.preview_provider_import(body, settings=self.settings, source_root=self.sources)

    def apply(self, preview, selected):
        return providers.apply_provider_import({"ref": preview["ref"], "snapshot_id": preview["snapshot_id"],
            "preview_token": preview["preview_token"], "selected_fields": selected}, settings=self.settings, source_root=self.sources)

    def test_preview_saves_raw_snapshot_only_and_no_remote_cover(self):
        before = self.file.read_bytes()
        preview = self.preview()
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(len(list(self.sources.glob("*.json"))), 1)
        raw = json.loads((self.sources / (preview["snapshot_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(raw["raw"]["subject"], self.subject)
        self.assertEqual(raw["raw"]["episode_pages"], [self.page])
        self.assertEqual(preview["subject"]["cover_url"], "")
        fields = {row["field"]: row for row in preview["diff"]}
        self.assertEqual(fields["metadata.summary"]["before"], "My description")
        self.assertEqual(fields["air_date_start"]["after"], "20200201")
        self.assertNotIn("air_date_end", fields)
        self.assertIsNone(preview["episode_range"])
        self.assertEqual(preview["episode_counts"], {"selected": 1, "total": 1})
        self.assertIn("整条目共 1", fields["metadata.episodes"]["reason"])

    def test_episode_range_validates_before_network_or_snapshot(self):
        for bounds in ({}, {"start": 1}, {"start": 1, "end": 2, "extra": 3},
                       {"start": "1", "end": 2}, {"start": True, "end": 2},
                       {"start": -1, "end": 2}, {"start": 3, "end": 2},
                       {"start": 0, "end": float("inf")}, {"start": float("nan"), "end": 2}):
            with self.subTest(bounds=bounds), patch.object(providers, "_request_json", side_effect=AssertionError("no network")), self.assertRaises(ValueError):
                providers.preview_provider_import({"ref": self.ref(), "subject_id": 12, "episode_range": bounds},
                                                  settings=self.settings, source_root=self.sources)
        self.assertFalse(self.sources.exists())

    def test_episode_range_selects_only_main_story_and_falls_back_to_sort(self):
        rows = [self.episode, {**self.episode, "id": 36, "ep": 2, "sort": 20},
                {**self.episode, "id": 37, "ep": 0, "sort": 3},
                {**self.episode, "id": 38, "ep": "invalid", "sort": 4},
                {**self.episode, "id": 39, "type": 1, "ep": 2, "sort": 2},
                {**self.episode, "id": 40, "ep": 5, "sort": 2}]
        preview = self.preview(pages=[{**self.page, "total": len(rows), "data": rows}],
                               episode_range={"start": 2.0, "end": 4.0})
        self.assertEqual(preview["episode_range"], {"start": 2, "end": 4})
        self.assertEqual(preview["episode_counts"], {"selected": 3, "total": 6})
        chapter_diff = next(row for row in preview["diff"] if row["field"] == "metadata.episodes")
        self.assertEqual([row["source_id"] for row in chapter_diff["after"]], [36, 37, 38])
        self.assertIn("匹配 3", chapter_diff["reason"])
        self.apply(preview, ["metadata.episodes"])
        record = catalog_records(self.settings)[0]["record"]
        self.assertEqual(record["source_refs"][0]["scope"], "episodes:2-4")
        self.assertEqual(record["source_refs"][0]["episode_range"], {"start": 2, "end": 4})
        self.assertEqual([row["source_id"] for row in record["metadata"]["episodes"]], [36, 37, 38])
        self.assertNotIn("ep", record["metadata"]["episodes"][2])

    def test_empty_range_cannot_clear_episodes_but_other_fields_can_be_applied(self):
        preview = self.preview(episode_range={"start": 10, "end": 12})
        self.assertEqual(preview["episode_counts"], {"selected": 0, "total": 1})
        chapter_diff = next(row for row in preview["diff"] if row["field"] == "metadata.episodes")
        self.assertFalse(chapter_diff["selectable"])
        self.assertIn("范围没有匹配章节", chapter_diff["reason"])
        with self.assertRaisesRegex(ValueError, "为空或无效"):
            self.apply(preview, ["metadata.episodes"])
        self.apply(preview, ["metadata.summary"])
        record = catalog_records(self.settings)[0]["record"]
        self.assertEqual(record["metadata"]["summary"], "Source description")
        self.assertNotIn("episodes", record["metadata"])
        self.assertEqual(record["source_refs"][0]["scope"], "episodes:10-12")

    def test_different_ranges_on_two_local_works_share_subject_without_merging(self):
        second = deepcopy(self.record)
        second["attributes"][0]["data"] = "Second local season"
        self.file.write_text(dump_yaml_string([self.record, second]), encoding="utf-8")
        pages = [{**self.page, "total": 2, "data": [self.episode, {**self.episode, "id": 36, "ep": 13, "sort": 13}]}]
        current = catalog_records(self.settings)
        first_preview = self.preview(pages=pages, episode_range={"start": 1, "end": 12}, ref=current[0]["ref"])
        second_preview = self.preview(pages=pages, episode_range={"start": 13, "end": 24}, ref=current[1]["ref"])
        self.apply(first_preview, ["metadata.episodes"])
        self.apply(second_preview, ["metadata.episodes"])
        records = catalog_records(self.settings)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["record"]["id"], records[1]["record"]["id"])
        self.assertEqual(records[0]["record"]["source_refs"][0]["scope"], "episodes:1-12")
        self.assertEqual(records[1]["record"]["source_refs"][0]["scope"], "episodes:13-24")
        self.assertEqual(records[0]["record"]["metadata"]["episodes"][0]["source_id"], 35)
        self.assertEqual(records[1]["record"]["metadata"]["episodes"][0]["source_id"], 36)
        self.assertEqual(records[0]["record"]["attributes"], self.record["attributes"])
        self.assertEqual(records[1]["record"]["attributes"], second["attributes"])

    def test_updating_source_range_does_not_remove_other_scopes(self):
        self.apply(self.preview(), [])
        self.apply(self.preview(episode_range={"start": 1, "end": 12}), [])
        self.apply(self.preview(episode_range={"start": 13, "end": 24}), [])
        self.apply(self.preview(episode_range={"start": 1.0, "end": 12.0}), [])
        self.apply(self.preview(), [])
        refs = catalog_records(self.settings)[0]["record"]["source_refs"]
        self.assertEqual(len(refs), 3)
        self.assertEqual({ref["scope"] for ref in refs}, {"", "episodes:1-12", "episodes:13-24"})

    def test_apply_range_comes_only_from_signed_preview(self):
        preview = self.preview(episode_range={"start": 1, "end": 12})
        body = {"ref": preview["ref"], "snapshot_id": preview["snapshot_id"], "preview_token": preview["preview_token"],
                "selected_fields": ["metadata.episodes"], "episode_range": {"start": 13, "end": 24}}
        with self.assertRaisesRegex(ValueError, "不支持的参数"):
            providers.apply_provider_import(body, settings=self.settings, source_root=self.sources)
        # Mutating display-only data does not alter the signed import range.
        preview["episode_range"] = {"start": 13, "end": 24}
        self.apply(preview, ["metadata.episodes"])
        self.assertEqual(catalog_records(self.settings)[0]["record"]["source_refs"][0]["scope"], "episodes:1-12")

    def test_manual_name_and_date_changes_are_flagged_in_next_diff(self):
        self.apply(self.preview(), ["name", "air_date_start"])
        record = catalog_records(self.settings)[0]["record"]
        record["attributes"][0]["data"] = "My manual title"
        record["attributes"][2]["data"]["start"] = "20200301"
        self.file.write_text(dump_yaml_string([record]), encoding="utf-8")
        preview = self.preview()
        for field in ("name", "air_date_start"):
            diff = next(row for row in preview["diff"] if row["field"] == field)
            self.assertTrue(diff["locally_modified"])
            self.assertIn("本地内容已在导入后修改，采用将替换当前内容", diff["reason"])

    def test_apply_only_selected_through_shared_writer_preserves_media_and_adds_id(self):
        preview = self.preview()
        with patch.object(providers.service, "apply_edits", wraps=providers.service.apply_edits) as writer:
            result = self.apply(preview, ["metadata.summary", "metadata.episodes"])
        writer.assert_called_once()
        self.assertTrue(result["db_committed"])
        record = load_yaml_string(self.file.read_text(encoding="utf-8"))[0]
        self.assertEqual(record["attributes"], self.record["attributes"])
        self.assertEqual(record["metadata"]["summary"], "Source description")
        self.assertEqual(record["metadata"]["custom"], "keep")
        self.assertEqual(record["metadata"]["episodes"][0]["air_date"], "20200201")
        self.assertTrue(record["id"].startswith("work_"))
        self.assertEqual(record["source_refs"][0]["external_id"], "12")
        self.assertEqual(record["source_refs"][0]["subject_id"], 12)
        self.assertNotIn("classifications", record)
        self.assertNotIn("cover_url", record["metadata"])

    def test_empty_selection_binds_source_and_adds_its_different_name_only(self):
        preview = self.preview()
        self.apply(preview, [])
        record = catalog_records(self.settings)[0]["record"]
        self.assertEqual(record["metadata"]["aliases"], ["Source title"])
        self.assertEqual(record["metadata"]["summary"], self.record["metadata"]["summary"])
        self.assertEqual(record["metadata"]["custom"], "keep")
        self.assertEqual(record["metadata"]["field_sources"]["metadata.aliases"]["external_id"], "12")
        self.assertEqual(record["attributes"], self.record["attributes"])
        self.assertEqual(len(record["source_refs"]), 1)

    def test_alias_preview_merges_local_names_and_explains_rename_variant_without_writing(self):
        self.record["metadata"]["aliases"] = ["My alias", "Alias"]
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        before = self.file.read_bytes()
        preview = self.preview()
        row = next(row for row in preview["diff"] if row["field"] == "metadata.aliases")
        self.assertEqual(preview["automatic_aliases"], ["Source title"])
        self.assertEqual(row["before"], ["My alias", "Alias"])
        self.assertEqual(row["after"], ["My alias", "Alias", "Source title", "中文名称"])
        self.assertEqual(row["after_when_name_selected"], ["My alias", "Alias", "中文名称"])
        self.assertIn("合并", row["reason"])
        self.assertEqual(self.file.read_bytes(), before)

    def test_selected_aliases_preserve_all_manual_aliases_and_match_preview(self):
        original = [f"Manual {number}" for number in range(105)]
        self.record["metadata"]["aliases"] = original + ["Ａｌｉａｓ", "source TITLE"]
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        preview = self.preview()
        row = next(row for row in preview["diff"] if row["field"] == "metadata.aliases")
        self.assertEqual(preview["automatic_aliases"], [])
        result = self.apply(preview, ["metadata.aliases"])
        actual = catalog_records(self.settings)[0]["record"]["metadata"]["aliases"]
        self.assertEqual(actual, row["after"])
        self.assertEqual(actual, original + ["Ａｌｉａｓ", "source TITLE", "中文名称"])
        self.assertEqual(result["automatic_aliases_added"], [])

    def test_same_or_empty_source_name_does_not_add_an_automatic_alias(self):
        for name in (" Local title ", "Ｌｏｃａｌ title", "local TITLE", "", "   "):
            with self.subTest(name=name):
                self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
                preview = self.preview(subject={**self.subject, "name": name})
                self.assertEqual(preview["automatic_aliases"], [])
                result = self.apply(preview, [])
                self.assertEqual(catalog_records(self.settings)[0]["record"]["metadata"], self.record["metadata"])
                self.assertEqual(result["automatic_aliases_added"], [])

    def test_selected_name_does_not_become_a_new_alias_and_rename_alias_preview_matches(self):
        for fields in (["name"], ["name", "metadata.aliases"]):
            with self.subTest(fields=fields):
                self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
                preview = self.preview()
                result = self.apply(preview, fields)
                record = catalog_records(self.settings)[0]["record"]
                self.assertEqual(record["attributes"][0]["data"], "Source title")
                self.assertEqual(result["automatic_aliases_added"], [])
                self.assertNotIn("Source title", record["metadata"].get("aliases", []))
                if "metadata.aliases" in fields:
                    row = next(row for row in preview["diff"] if row["field"] == "metadata.aliases")
                    self.assertEqual(record["metadata"]["aliases"], row["after_when_name_selected"])
                else:
                    self.assertNotIn("aliases", record["metadata"])

    def test_source_name_is_added_even_without_other_provider_aliases(self):
        preview = self.preview(subject={**self.subject, "name_cn": "", "infobox": []})
        self.assertEqual(preview["automatic_aliases"], ["Source title"])
        result = self.apply(preview, ["metadata.aliases"])
        self.assertEqual(result["automatic_aliases_added"], ["Source title"])
        self.assertEqual(catalog_records(self.settings)[0]["record"]["metadata"]["aliases"], ["Source title"])

    def test_automatic_alias_repeat_and_next_refresh_never_duplicate_or_save_twice(self):
        preview = self.preview()
        result = self.apply(preview, [])
        self.assertEqual(result["automatic_aliases_added"], ["Source title"])
        self.assertEqual(result["applied_fields"], ["metadata.aliases"])
        before = self.file.read_bytes()
        with patch.object(providers.service, "apply_edits", side_effect=AssertionError("no second write")):
            repeated = self.apply(preview, [])
        self.assertTrue(repeated["unchanged"])
        self.assertEqual(self.file.read_bytes(), before)
        newer = self.preview()
        self.assertEqual(newer["automatic_aliases"], [])
        self.apply(newer, [])
        self.assertEqual(catalog_records(self.settings)[0]["record"]["metadata"]["aliases"], ["Source title"])

    def test_old_preview_cannot_restore_automatically_added_alias_after_manual_removal(self):
        preview = self.preview()
        self.apply(preview, [])
        record = catalog_records(self.settings)[0]["record"]
        record["metadata"]["aliases"] = ["My replacement"]
        self.file.write_text(dump_yaml_string([record]), encoding="utf-8")
        before = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.apply(preview, [])
        self.assertEqual(self.file.read_bytes(), before)

    def test_manual_alias_edit_after_preview_is_not_overwritten(self):
        preview = self.preview()
        self.record["metadata"]["aliases"] = ["Edited while previewing"]
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        before = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.apply(preview, ["metadata.aliases"])
        self.assertEqual(self.file.read_bytes(), before)

    def test_alias_values_are_derived_from_snapshot_not_client_display_data(self):
        preview = self.preview()
        preview["automatic_aliases"] = ["Untrusted replacement"]
        for row in preview["diff"]:
            if row["field"] == "metadata.aliases":
                row["after"] = ["Untrusted replacement"]
                row["after_when_name_selected"] = ["Untrusted replacement"]
        self.apply(preview, [])
        self.assertEqual(catalog_records(self.settings)[0]["record"]["metadata"]["aliases"], ["Source title"])

    def test_repeat_apply_is_idempotent_without_second_write(self):
        preview = self.preview()
        self.apply(preview, ["metadata.summary"])
        before = self.file.read_bytes()
        with patch.object(providers.service, "apply_edits", side_effect=AssertionError("must not save again")):
            result = self.apply(preview, ["metadata.summary"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.file.read_bytes(), before)

    def test_stale_ref_does_not_contact_provider(self):
        ref = self.ref()
        self.record["metadata"]["summary"] = "Edited"
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        with patch.object(providers, "_request_json", side_effect=AssertionError("no request")), self.assertRaisesRegex(ValueError, "已变化"):
            providers.preview_provider_import({"ref": ref, "subject_id": 12}, settings=self.settings, source_root=self.sources)

    def test_changes_after_preview_are_not_overwritten(self):
        preview = self.preview()
        self.record["metadata"]["summary"] = "Edited after preview"
        self.file.write_text(dump_yaml_string([self.record]), encoding="utf-8")
        before = self.file.read_bytes()
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.apply(preview, ["metadata.summary"])
        self.assertEqual(self.file.read_bytes(), before)

    def test_client_record_token_or_ref_tampering_rejected(self):
        preview = self.preview()
        body = {"ref": preview["ref"], "snapshot_id": preview["snapshot_id"],
                "preview_token": preview["preview_token"], "selected_fields": []}
        for extra in ({"record": {"metadata": {"summary": "evil"}}},
                      {"ref": {**preview["ref"], "index_in_file": 1}},
                      {"snapshot_id": "../escape"}, {"preview_token": "a." + "0" * 64}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                providers.apply_provider_import({**body, **extra}, settings=self.settings, source_root=self.sources)
        self.assertEqual(catalog_records(self.settings)[0]["record"], self.record)

    def test_unknown_duplicate_or_empty_source_fields_rejected(self):
        subject = {**self.subject, "summary": "", "date": "not a date"}
        preview = self.preview(subject=subject)
        for selected in (["press_path"], ["name", "name"], ["metadata.summary"], ["air_date_start"], None):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.apply(preview, selected)

    def test_corrupt_snapshot_rejected(self):
        preview = self.preview()
        path = self.sources / (preview["snapshot_id"] + ".json")
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.apply(preview, ["name"])

    def test_incomplete_or_duplicate_pages_produce_no_snapshot(self):
        invalid_pages = [
            [{**self.page, "total": 2}, {"total": 2, "limit": 100, "offset": 1, "data": []}],
            [{**self.page, "total": 2}, {"total": 2, "limit": 100, "offset": 1, "data": [self.episode]}],
            [{**self.page, "total": 2}, {"total": 3, "limit": 100, "offset": 1, "data": []}],
            [{**self.page, "offset": 100}],
            [{**self.page, "total": providers.MAX_EPISODES + 1}],
        ]
        for pages in invalid_pages:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                self.preview(pages=pages)
            self.assertFalse(self.sources.exists())

    def test_complete_multiple_pages_and_zero_episodes(self):
        pages = [{**self.page, "total": 2}, {"total": 2, "limit": 100, "offset": 1,
                 "data": [{**self.episode, "id": 36, "sort": 2}]}]
        preview = self.preview(pages=pages)
        episodes = next(row["after"] for row in preview["diff"] if row["field"] == "metadata.episodes")
        self.assertEqual(len(episodes), 2)
        empty = self.preview(pages=[{"total": 0, "limit": 100, "offset": 0, "data": []}])
        self.assertFalse(next(row["selectable"] for row in empty["diff"] if row["field"] == "metadata.episodes"))

    def test_expired_token_and_other_catalog_rejected(self):
        preview = self.preview()
        with patch.object(providers.time, "time", return_value=10**12), self.assertRaisesRegex(ValueError, "过期"):
            self.apply(preview, [])
        with patch.object(providers, "_catalog_scope", return_value="other"), self.assertRaisesRegex(ValueError, "不一致"):
            self.apply(preview, [])

    def test_dates_preserve_unknown_and_do_not_invent_end(self):
        self.assertEqual(providers._date("2020"), "20200000")
        self.assertEqual(providers._date("2020-02"), "20200200")
        self.assertEqual(providers._date("2020-02-31"), "")
        self.assertEqual(providers._date(""), "")
        self.assertEqual(providers._date("0000-00-00"), "")

    def test_preview_network_failure_leaves_database_and_sources_untouched(self):
        before = self.file.read_bytes()
        with patch.object(providers, "_request_json", side_effect=ValueError("网络失败")), self.assertRaises(ValueError):
            providers.preview_provider_import({"ref": self.ref(), "subject_id": 12}, settings=self.settings, source_root=self.sources)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertFalse(self.sources.exists())

    def test_snapshot_reads_are_size_limited(self):
        preview = self.preview()
        with patch.object(providers, "MAX_SNAPSHOT_BYTES", 2), self.assertRaisesRegex(ValueError, "大小限制"):
            self.apply(preview, [])

    def test_source_path_links_are_rejected(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("host cannot create symbolic links")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            providers._source_root(link / "snapshots")

    def test_search_bounds_and_fixed_public_scope(self):
        with patch.object(providers, "_request_json", return_value={"total": 1, "data": [self.subject]}) as request:
            result = providers.search_provider({"query": "Demo", "type": 2})
        self.assertEqual(request.call_args.args[0], "/v0/search/subjects")
        self.assertEqual(request.call_args.kwargs["payload"]["filter"], {"type": [2], "nsfw": False})
        self.assertEqual(result["results"][0]["cover_url"], "")
        for body in ({"query": ""}, {"query": "x" * 201}, {"query": "x\n"}, {"query": "x", "type": True},
                     {"query": "x", "offset": -1}, {"query": "x", "url": "http://localhost"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                providers.search_provider(body)

    def test_malformed_local_extensions_fail_before_preview_network(self):
        cases = [{"metadata": None}, {"metadata": "legacy"}, {"metadata": {"summary": {}}},
                 {"metadata": {"field_sources": []}}, {"metadata": {"field_sources": {"name": "legacy"}}},
                 {"source_refs": None}, {"source_refs": "legacy"}, {"source_refs": [None]},
                 {"source_refs": [{"provider": "bangumi"}]}]
        for extra in cases:
            with self.subTest(extra=extra):
                self.file.write_text(dump_yaml_string([{**self.record, **extra}]), encoding="utf-8")
                before = self.file.read_bytes()
                with patch.object(providers, "_request_json", side_effect=AssertionError("must not access network")), self.assertRaisesRegex(ValueError, "本地作品扩展数据无效.*修复原记录"):
                    providers.preview_provider_import({"ref": self.ref(), "subject_id": 12}, settings=self.settings, source_root=self.sources)
                self.assertEqual(self.file.read_bytes(), before)
                self.assertFalse(self.sources.exists())

    def test_malformed_extensions_after_preview_do_not_produce_write_or_unhandled_error(self):
        preview = self.preview()
        for extra in ({"metadata": None}, {"metadata": {"field_sources": []}}, {"source_refs": ["legacy"]}):
            with self.subTest(extra=extra):
                self.file.write_text(dump_yaml_string([{**self.record, **extra}]), encoding="utf-8")
                before = self.file.read_bytes()
                with patch.object(providers.service, "apply_edits", side_effect=AssertionError("must not write")), self.assertRaisesRegex(ValueError, "本地作品扩展数据无效"):
                    self.apply(preview, ["metadata.summary"])
                self.assertEqual(self.file.read_bytes(), before)


class ProviderHttpTest(unittest.TestCase):
    def test_search_slow_stream_uses_default_total_request_deadline(self):
        clock = [100.0]

        class SlowResponse(io.BytesIO):
            headers = {}
            reads = 0

            def read1(self, _limit):
                self.reads += 1
                clock[0] += 20
                return b" "

        response = SlowResponse()
        with patch.object(providers.time, "monotonic", side_effect=lambda: clock[0]), patch.object(providers, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "总时限"):
                providers.search_provider({"query": "Demo"})
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual(response.reads, 2)

    def test_retry_after_is_honored_or_deferred_without_long_wait(self):
        self.assertEqual(providers._retry_delay("2"), 2)
        with self.assertRaisesRegex(ValueError, "稍后重试"):
            providers._retry_delay("30")
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
        with self.assertRaisesRegex(ValueError, "稍后重试"):
            providers._retry_delay(future)
        with patch.object(providers, "build_opener") as opener, patch.object(providers.time, "sleep") as sleep:
            opener.return_value.open.side_effect = HTTPError("url", 429, "", {"Retry-After": "30"}, io.BytesIO())
            with self.assertRaisesRegex(ValueError, "Retry-After"):
                providers._request_json("/v0/subjects/12")
            self.assertEqual(opener.return_value.open.call_count, 1)
            sleep.assert_not_called()

    def test_http_incomplete_read_is_controlled_and_retry_is_bounded(self):
        class IncompleteResponse(io.BytesIO):
            headers = {}

            def read(self, _limit):
                raise IncompleteRead(b"private response fragment", 20)

            read1 = read

        with patch.object(providers, "build_opener") as opener, patch.object(providers.time, "sleep"):
            opener.return_value.open.side_effect = lambda *args, **kwargs: IncompleteResponse()
            with self.assertRaisesRegex(ValueError, "连接失败或超时") as failure:
                providers._request_json("/v0/subjects/12")
            self.assertNotIn("private", str(failure.exception))
            self.assertEqual(opener.return_value.open.call_count, 2)

    def test_total_deadline_bounds_request_timeout_and_stops_new_requests(self):
        with patch.object(providers.time, "monotonic", return_value=100):
            self.assertEqual(providers._remaining(102), 2)
            with patch.object(providers, "build_opener") as opener, self.assertRaisesRegex(ValueError, "总时限"):
                providers._request_json("/v0/subjects/12", deadline=99)
            opener.return_value.open.assert_not_called()
            with patch.object(providers.time, "sleep") as sleep, self.assertRaisesRegex(ValueError, "剩余时间"):
                providers._wait_retry(3, 102)
            sleep.assert_not_called()

    def test_slow_chunk_stream_stops_at_total_deadline(self):
        with patch.object(providers.time, "monotonic", side_effect=[100, 105]), self.assertRaisesRegex(ValueError, "总时限"):
            providers._read_response(io.BytesIO(b"part of a slow body"), 102)

    def test_path_and_id_cannot_be_arbitrary_url(self):
        for path in ("https://evil.test", "/v0/subjects/../../internal", "/v0/users/me"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                providers._request_json(path)
        for value in (True, 0, -1, "https://bangumi.tv/subject/12", "12/../../", "0012"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                providers._positive_id(value)

    def test_redirects_are_refused(self):
        with self.assertRaisesRegex(ValueError, "重定向"):
            providers._NoRedirects().redirect_request(None, None, 302, "", {}, "http://127.0.0.1")

    def test_request_headers_timeout_and_response_limit(self):
        response = io.BytesIO(b'{"id":12}')
        response.headers = {"Content-Length": "9"}
        with patch.object(providers, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(providers._request_json("/v0/subjects/12"), {"id": 12})
            args = opener.return_value.open.call_args
            self.assertEqual(args.args[0].full_url, "https://api.bgm.tv/v0/subjects/12")
            self.assertEqual(args.args[0].get_header("User-agent"), providers.USER_AGENT)
            self.assertEqual(args.kwargs["timeout"], providers.REQUEST_TIMEOUT)
        for header, data in ((str(providers.MAX_RESPONSE_BYTES + 1), b"{}"), ("", b"123456")):
            response = io.BytesIO(data)
            response.headers = {"Content-Length": header}
            with patch.object(providers, "build_opener") as opener, patch.object(providers, "MAX_RESPONSE_BYTES", 5):
                opener.return_value.open.return_value = response
                with self.assertRaisesRegex(ValueError, "大小限制"):
                    providers._request_json("/v0/subjects/12")

    def test_short_content_length_body_is_not_accepted_as_complete_json(self):
        response = io.BytesIO(b"{}")
        response.headers = {"Content-Length": "30"}
        with patch.object(providers, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "未完整接收"):
                providers._request_json("/v0/subjects/12")

    def test_bounded_retry_does_not_expose_remote_error_body(self):
        with patch.object(providers, "build_opener") as opener, patch.object(providers.time, "sleep"):
            opener.return_value.open.side_effect = URLError("secret remote detail")
            with self.assertRaises(ValueError) as failure:
                providers._request_json("/v0/subjects/12")
            self.assertEqual(opener.return_value.open.call_count, 2)
            self.assertNotIn("secret", str(failure.exception))
        with patch.object(providers, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("url", 403, "secret", {}, io.BytesIO(b"secret"))
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                providers._request_json("/v0/subjects/12")
            self.assertEqual(opener.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
