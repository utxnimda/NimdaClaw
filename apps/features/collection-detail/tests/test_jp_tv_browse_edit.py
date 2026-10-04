from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from collection_detail import save as catalog_save
from work_catalog_yaml import persistence
from work_catalog_yaml.operation_progress import OperationRegistry, execute_operation
from work_catalog_yaml.paths import normalize_copied_path

from work_catalog_yaml.jp_tv.browse_save import (
    CatalogRollbackConflictError,
    apply_catalog_yaml_mutation,
    browse_apply_enum_edits_from_ui_body,
    browse_save_yaml_from_ui_body,
    catalog_write_transaction,
    rollback_catalog_yaml_mutation,
)
from work_catalog_yaml.jp_tv.browse_settings import (
    JpTvBrowseSettings,
    load_jp_tv_browse_settings,
)
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


def _settings(db: Path, *paths: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=db,
        resolved_default_readable=str(paths[0]) if paths else None,
        resolved_catalog_yaml_paths=tuple(str(p) for p in paths),
        enum_options={},
        enum_labels={},
        enum_section_labels={},
        app_features=(),
    )


def _catalog_yaml(*names: str, press_format: str = "A") -> str:
    rows = []
    for ix, name in enumerate(names, start=1):
        rows.append(
            f"""
            - attributes:
                - type: date
                  data:
                    start: "2099{ix:02d}01"
                    end: "2099{ix:02d}28"
                - type: collection-type
                  data:
                    domain: animation
                    release_type: tv
                    collectioned:
                    - press_format: "{press_format}"
                      press_group: "G1"
                    markers: []
                - type: country
                  data: japan
                - type: name
                  data: "{name}"
            """,
        )
    return textwrap.dedent("\n".join(rows)).lstrip()


class JpTvBrowseEditTest(unittest.TestCase):
    def test_invalid_row_save_records_exact_file_record_and_stage_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            source = db / "works.yaml"
            source.write_text(_catalog_yaml("Fixture"), encoding="utf-8")
            before = source.read_bytes()
            registry = OperationRegistry()
            operation = registry.register(str(uuid4()), "保存作品数据库")
            body = {"rows": [{"yaml_source_rel": source.name, "index_in_file": 7, "name": "Fixture"}]}
            with self.assertRaisesRegex(ValueError, "index_in_file 越界"):
                execute_operation(registry, operation, browse_save_yaml_from_ui_body, body, settings=_settings(db, source))
            details = registry.snapshot(operation.id)["result"]["details"]
            failure = next(item for item in details if item.get("error_type") == "ValueError")
            self.assertEqual(failure["yaml_source_rel"], source.name)
            self.assertEqual(failure["index_in_file"], 7)
            self.assertEqual(failure["source_path"], str(source))
            self.assertEqual(failure["action"], "校验并更新作品记录")
            self.assertTrue(failure["location"]["line"])
            self.assertEqual(source.read_bytes(), before)

    def test_save_empty_press_group_preserves_record_fields_and_exact_history(self) -> None:
        for empty_group in ("", "----"):
            with self.subTest(press_group=empty_group), tempfile.TemporaryDirectory() as td:
                db = Path(td) / "DB"
                db.mkdir()
                source = db / "works.yaml"
                fixture = load_yaml_string(_catalog_yaml("Fixture", press_format="BDRip"))
                fixture[0]["note"] = "Unrelated work metadata"
                coll_attr = fixture[0]["attributes"][1]
                coll_attr["description"] = "Collection description"
                coll = coll_attr["data"]
                coll["path"] = "Fixture"
                coll["markers"] = ["subs"]
                coll["collectioned"][0]["press_path"] = "Fixture_BDRip"
                coll["continuations"] = [{
                    "title": "Bonus",
                    "collectioned": [{
                        "press_format": "1080p", "press_group": "G2", "press_path": "Bonus_1080p",
                    }],
                }]
                source.write_text(dump_yaml_string(fixture), encoding="utf-8")
                original = source.read_bytes()

                writes = browse_save_yaml_from_ui_body({"rows": [{
                    "yaml_source_rel": source.name,
                    "index_in_file": 0,
                    "domain": "animation", "release_type": "tv",
                    "path": "Fixture", "markers": ["subs"],
                    "collectioned_ordered": [
                        {
                            "press_format": "BDRip", "press_group": empty_group,
                            "press_path": "Fixture_BDRip", "segment": "main",
                        },
                        {
                            "press_format": "1080p", "press_group": "G2", "press_path": "Bonus_1080p",
                            "segment": "continuation", "continuation_index": 0, "continuation_title": "Bonus",
                        },
                    ],
                }]}, settings=_settings(db, source))

                coll["collectioned"][0]["press_group"] = empty_group
                self.assertEqual(load_yaml_string(source.read_text(encoding="utf-8")), fixture)
                self.assertEqual(len(writes), 1)
                self.assertEqual(writes[0][0], source.resolve())
                self.assertEqual((db.parent / "History" / writes[0][1]).read_bytes(), original)

    def test_save_date_edits_use_compact_storage_and_keep_exact_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            source = db / "works.yaml"
            source.write_text(_catalog_yaml("Fixture"), encoding="utf-8")
            original = source.read_bytes()
            writes = browse_save_yaml_from_ui_body({"rows": [{
                "yaml_source_rel": source.name,
                "index_in_file": 0,
                "date": {"start": "2024-02-29", "end": "2024/3/9"},
            }]}, settings=_settings(db, source))
            dates = load_yaml_string(source.read_text(encoding="utf-8"))[0]["attributes"][0]["data"]
            self.assertEqual(dates, {"start": "20240229", "end": "20240309"})
            self.assertEqual((db.parent / "History" / writes[0][1]).read_bytes(), original)

    def test_invalid_calendar_date_never_reaches_catalog_or_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            source = db / "works.yaml"
            source.write_text(_catalog_yaml("Fixture"), encoding="utf-8")
            original = source.read_bytes()
            for invalid in ("2023-02-29", "2024-04-31", "20241301", "2024-01-32"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    browse_save_yaml_from_ui_body({"rows": [{
                        "yaml_source_rel": source.name,
                        "index_in_file": 0,
                        "date": {"start": invalid, "end": ""},
                    }]}, settings=_settings(db, source))
                self.assertEqual(source.read_bytes(), original)
            self.assertFalse((db.parent / "History").exists())

    def test_unchanged_legacy_calendar_error_does_not_block_full_table_save(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            source = db / "works.yaml"
            source.write_text(
                _catalog_yaml("Legacy", "Other").replace("20990101", "20074020"),
                encoding="utf-8",
            )
            browse_save_yaml_from_ui_body({"rows": [
                {
                    "yaml_source_rel": source.name, "index_in_file": 0,
                    "name": "Legacy renamed",
                    "date": {"start": "2007-40-20", "end": "2099-01-28"},
                },
                {
                    "yaml_source_rel": source.name, "index_in_file": 1,
                    "name": "Other renamed",
                    "date": {"start": "2099-02-01", "end": "2099-02-28"},
                },
            ]}, settings=_settings(db, source))
            raw = load_yaml_string(source.read_text(encoding="utf-8"))
            self.assertEqual(raw[0]["attributes"][0]["data"]["start"], "20074020")
            self.assertEqual(raw[0]["attributes"][3]["data"], "Legacy renamed")
            self.assertEqual(raw[1]["attributes"][0]["data"]["start"], "20990201")
            self.assertEqual(raw[1]["attributes"][3]["data"], "Other renamed")

    def test_new_work_dates_preserve_unknown_components_without_guessing(self) -> None:
        for start, end, expected_start, expected_end in (
            ("2020-00-00", "", "20200000", ""),
            ("2020-XX-XX", "20200000", "2020XXXX", "20200000"),
            ("20240229", "2024-03-01", "20240229", "20240301"),
        ):
            with self.subTest(start=start):
                work = catalog_save._new_work_from_row_patch({
                    "domain": "animation", "release_type": "tv", "country": "japan",
                    "name": "Fixture", "date": {"start": start, "end": end},
                })
                self.assertEqual(work["attributes"][0]["data"], {
                    "start": expected_start, "end": expected_end,
                })

    def test_landing_append_keeps_iso_draft_but_writes_compact_dates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            draft = {
                "name": "Fixture", "domain": "animation", "release_type": "tv", "country": "japan",
                "date": {"start": "2024-02-29", "end": "2024-03-31"},
                "path": str(root / "Media"),
                "collectioned_ordered": [{
                    "press_format": "BDRip", "press_group": "VCB", "press_path": "Fixture_BDRip",
                }],
            }
            with patch.object(catalog_save, "media_group_code_known", return_value=True):
                preview = catalog_save.preview_catalog_work_append(draft, settings=_settings(db))
                self.assertEqual(preview["patch"]["date"], draft["date"])
                self.assertFalse(Path(preview["target"]).exists())
                _, receipt = catalog_save.append_catalog_work_from_preview(
                    draft, settings=_settings(db), expected_before_sha256=preview["before_sha256"],
                )
            self.assertIsNotNone(receipt)
            raw = load_yaml_string(Path(preview["target"]).read_text(encoding="utf-8"))
            self.assertEqual(raw[0]["attributes"][0]["data"], {
                "start": "20240229", "end": "20240331",
            })

    def test_landing_append_rejects_impossible_iso_date_before_preview(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            with self.assertRaisesRegex(ValueError, "日历日期"):
                catalog_save.preview_catalog_work_append({
                    "name": "Fixture", "date": {"start": "2024-02-30", "end": ""},
                }, settings=_settings(db))
            self.assertEqual(list(db.iterdir()), [])

    def test_invalid_row_index_cannot_edit_or_delete_a_different_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            source = db / "works.yaml"
            source.write_text(_catalog_yaml("First", "Second"), encoding="utf-8")
            original = source.read_bytes()
            for action in ("rows", "deleted_rows"):
                for index in (True, False, 0.5, 1.9, "1.0"):
                    with self.subTest(action=action, index=index), self.assertRaisesRegex(ValueError, "index_in_file 非法"):
                        browse_save_yaml_from_ui_body({action: [{
                            "yaml_source_rel": source.name, "index_in_file": index, "name": "Wrong",
                        }]}, settings=_settings(db, source))
                    self.assertEqual(source.read_bytes(), original)
            self.assertFalse((db.parent / "History").exists())

    def test_existing_work_preview_hash_uses_the_parsed_snapshot_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            source = db / "works.yaml"
            draft = {
                "name": "Fixture", "domain": "animation", "release_type": "tv", "country": "japan",
                "date": {"start": "2020-01-01", "end": ""}, "path": str(root / "Media"),
                "collectioned_ordered": [{"press_format": "BDRip", "press_group": "VCB", "press_path": "Fixture_BDRip"}],
            }
            source.write_text(dump_yaml_string([catalog_save._new_work_from_row_patch(draft)]), encoding="utf-8")
            original = source.read_bytes()
            original_read = Path.read_bytes
            reads = []

            def replace_after_read(path: Path) -> bytes:
                content = original_read(path)
                if path == source:
                    reads.append(path)
                    source.write_bytes(original.replace(b"Fixture", b"Changed"))
                return content

            with patch.object(Path, "read_bytes", new=replace_after_read), patch.object(catalog_save, "media_group_code_known", return_value=True):
                preview = catalog_save.preview_catalog_work_append(draft, settings=_settings(db, source))
            self.assertEqual(preview["action"], "already_exists")
            self.assertEqual(preview["before_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(preview["before_sha256"], preview["after_sha256"])
            self.assertEqual(len(reads), 1)

    def test_invalid_later_catalog_does_not_partially_save_earlier_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            first, second = db / "a.yaml", db / "b.yaml"
            first.write_text(_catalog_yaml("Original"), encoding="utf-8")
            second.write_text(_catalog_yaml("Other"), encoding="utf-8")
            before = first.read_bytes()
            with self.assertRaisesRegex(ValueError, "越界"):
                browse_save_yaml_from_ui_body({"rows": [
                    {"yaml_source_rel": "a.yaml", "index_in_file": 0, "name": "Changed"},
                    {"yaml_source_rel": "b.yaml", "index_in_file": 99, "name": "Invalid"},
                ]}, settings=_settings(db, first, second))
            self.assertEqual(first.read_bytes(), before)
            self.assertFalse((db.parent / "History").exists())

    def test_enum_config_write_failure_restores_renamed_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            catalog, config = db / "works.yaml", root / "config.yaml"
            catalog.write_text(_catalog_yaml("Original"), encoding="utf-8")
            config.write_text("enum:\n- name: press_format\n  values: [A]\n", encoding="utf-8")
            original_catalog, original_config = catalog.read_bytes(), config.read_bytes()
            original_write = persistence.atomic_write_bytes

            def fail_config(target: Path, content: bytes) -> None:
                if target == config:
                    raise OSError("configuration write failed")
                original_write(target, content)

            with patch.object(persistence, "atomic_write_bytes", side_effect=fail_config):
                with self.assertRaisesRegex(OSError, "configuration write failed"):
                    browse_apply_enum_edits_from_ui_body({"edits": [
                        {"enum_key": "press_format", "action": "rename", "value": "A", "new_value": "B"},
                    ]}, settings=_settings(db, catalog), config_path=config)
            self.assertEqual(catalog.read_bytes(), original_catalog)
            self.assertEqual(config.read_bytes(), original_config)

    def test_catalog_mutation_rollback_refuses_to_overwrite_later_ui_save(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            catalog = db / "[JP][TVInfo][2099].yaml"
            before = _catalog_yaml("Before")
            repair = _catalog_yaml("Repair")
            catalog.write_text(before, encoding="utf-8")
            settings = _settings(db, catalog)

            receipt = apply_catalog_yaml_mutation(
                target=catalog,
                after_bytes=repair.encode("utf-8"),
                settings=settings,
                expected_before_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest(),
                work_ref={"yaml_source_rel": catalog.name, "index_in_file": 0},
            )
            browse_save_yaml_from_ui_body(
                {
                    "rows": [
                        {
                            "yaml_source_rel": catalog.name,
                            "index_in_file": 0,
                            "name": "Later UI Save",
                        }
                    ]
                },
                settings=settings,
            )

            with self.assertRaisesRegex(CatalogRollbackConflictError, "拒绝自动回滚"):
                rollback_catalog_yaml_mutation(receipt)
            self.assertIn("Later UI Save", catalog.read_text(encoding="utf-8"))

    def test_catalog_transaction_uses_cross_process_root_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            code = textwrap.dedent(
                f"""
                import time
                from pathlib import Path
                from work_catalog_yaml.jp_tv.browse_save import catalog_write_transaction
                with catalog_write_transaction(Path({json.dumps(str(db))}), timeout_seconds=2):
                    print('locked', flush=True)
                    time.sleep(1.0)
                """
            )
            child = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ.copy(),
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(TimeoutError, "文件锁超时"):
                    with catalog_write_transaction(db, timeout_seconds=0.1):
                        self.fail("a second process must not enter the catalog transaction")
            finally:
                try:
                    _stdout, stderr = child.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    _stdout, stderr = child.communicate()
                self.assertEqual(child.returncode, 0, stderr)

    def test_app_features_are_loaded_from_framework_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            cfg = root / "jp-tv-browse.config.yaml"
            cfg.write_text(
                textwrap.dedent(
                    f"""
                    version: 1
                    app:
                      features:
                        - id: collection-detail
                          label: 作品数据
                          order: 20
                        - id: collection-info
                          label: 收集情况
                          order: 10
                    paths:
                      filesystem_root: "{db.as_posix()}"
                    """,
                ).lstrip(),
                encoding="utf-8",
            )

            st = load_jp_tv_browse_settings(cfg)

            self.assertEqual(
                [(item["id"], item["label"], item["order"]) for item in st.app_features],
                [
                    ("collection-detail", "作品数据", 10),
                    ("collection-info", "收集情况", 20),
                    ("directory-organizer", "目录整理", 30),
                ],
            )

    def test_save_body_can_delete_one_catalog_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            catalog = db / "[JP][TVInfo][2099].yaml"
            catalog.write_text(_catalog_yaml("第一行", "第二行"), encoding="utf-8")

            writes = browse_save_yaml_from_ui_body(
                {
                    "deleted_rows": [
                        {
                            "yaml_source_rel": "[JP][TVInfo][2099].yaml",
                            "index_in_file": 0,
                        },
                    ],
                },
                settings=_settings(db, catalog),
            )

            self.assertEqual(len(writes), 1)
            raw = load_yaml_string(catalog.read_text(encoding="utf-8"))
            self.assertEqual(len(raw), 1)
            attrs = raw[0]["attributes"]
            self.assertEqual(attrs[3]["data"], "第二行")
            self.assertTrue((root / "History").is_dir())

    def test_save_body_appends_new_row_to_broadcast_year_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            current_catalog = db / "[JP][TVInfo][2099].yaml"

            writes = browse_save_yaml_from_ui_body(
                {
                    "new_rows": [
                        {
                            "domain": "animation",
                            "release_type": "tv",
                            "country": "japan",
                            "name": "新增行",
                            # 服务端根据开播年选择文件，而不是操作当天的年份。
                            "date": {"start": "2099-04-01", "end": "2099/6/30"},
                            "markers": ["subs"],
                            "collectioned_ordered": [
                                {"press_format": "BDRip", "press_group": "VCB"},
                            ],
                        },
                    ],
                },
                settings=_settings(db),
                now=datetime(2026, 6, 14, 9, 30, 0),
            )

            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][0], current_catalog.resolve())
            self.assertEqual(writes[0][1], "")
            raw = load_yaml_string(current_catalog.read_text(encoding="utf-8"))
            self.assertEqual(len(raw), 1)
            attrs = raw[0]["attributes"]
            self.assertEqual(attrs[0]["data"], {"start": "20990401", "end": "20990630"})
            self.assertEqual(attrs[1]["data"]["collectioned"][0]["press_format"], "BDRip")
            self.assertEqual(attrs[1]["data"]["markers"], ["subs"])
            self.assertEqual(attrs[3]["data"], "新增行")

    def test_new_rows_route_by_country_and_ignore_forged_client_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            original = db / "[JP][TVInfo][2026].yaml"
            original.write_text(_catalog_yaml("已有作品"), encoding="utf-8")
            previous = original.read_bytes()
            rows = [
                {
                    "domain": "animation" if country == "japan" else "tv-drama",
                    "release_type": "tv", "country": country, "name": country,
                    "date": {"start": "20200102", "end": "2020-03-04"},
                    "yaml_source_rel": "../../outside.yaml", "collectioned_ordered": [],
                }
                for country in ("japan", "korea", "china", "usa", "uk")
            ]
            writes = browse_save_yaml_from_ui_body(
                {"path": str(original), "new_rows": rows}, settings=_settings(db, original),
                now=datetime(2026, 9, 21),
            )
            self.assertEqual({target.name for target, _history in writes}, {
                f"[{code}][TVInfo][2020].yaml" for code in ("JP", "KR", "CN", "US", "UK")
            })
            self.assertEqual(original.read_bytes(), previous)
            self.assertFalse((Path(td) / "outside.yaml").exists())
            korean = load_yaml_string((db / "[KR][TVInfo][2020].yaml").read_text(encoding="utf-8"))[0]
            self.assertEqual(korean["attributes"][2]["data"], "korea")
            self.assertEqual(korean["attributes"][0]["data"], {"start": "20200102", "end": "20200304"})

    def test_new_work_routing_uses_known_year_and_current_year_only_when_unknown(self) -> None:
        for start, expected_year in (
            ("20200102", "2020"), ("2020-00-00", "2020"), ("2020-XX-XX", "2020"),
            ("", "2026"), (None, "2026"), ("00000000", "2026"), ("20XX-01-01", "2026"),
        ):
            with self.subTest(start=start):
                self.assertEqual(
                    catalog_save.catalog_relpath_for_new_work("korea", start, now=datetime(2026, 9, 21)),
                    f"[KR][TVInfo][{expected_year}].yaml",
                )
        for start in ("20230229", "2020-13-01", "2020", "2020-01-50"):
            with self.subTest(start=start), self.assertRaises(ValueError):
                catalog_save.catalog_relpath_for_new_work("korea", start)
        with self.assertRaisesRegex(ValueError, "国家代码"):
            catalog_save.catalog_relpath_for_new_work("../../outside", "20200102")

    def test_invalid_new_work_date_or_country_never_writes_part_of_batch(self) -> None:
        for invalid in ({"country": "unsupported"}, {"date": {"start": "20230229", "end": ""}}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as td:
                db = Path(td) / "DB"
                db.mkdir()
                valid = {"name": "有效", "country": "korea", "domain": "tv-drama", "release_type": "tv",
                         "date": {"start": "20200102", "end": ""}, "collectioned_ordered": []}
                with self.assertRaises(ValueError):
                    browse_save_yaml_from_ui_body({"new_rows": [valid, {**valid, **invalid}]}, settings=_settings(db))
                self.assertEqual(list(db.glob("*.yaml")), [])
                self.assertFalse((Path(td) / "History").exists())

    def test_copied_paths_remove_directional_wrappers_but_preserve_contents(self) -> None:
        copied = ' \u202a"G:\\Video\\电视剧\\拥抱  太阳"\u202c '
        self.assertEqual(normalize_copied_path(copied), "G:\\Video\\电视剧\\拥抱  太阳")
        self.assertEqual(normalize_copied_path('"\u2066G:\\Video\\A B\u2069"'), "G:\\Video\\A B")
        self.assertEqual(normalize_copied_path("\ufeff\u200e\\\\server\\share\\A B\u200f"), "\\\\server\\share\\A B")
        # Do not rewrite a real interior Unicode filename character.
        self.assertEqual(normalize_copied_path("G:\\A\u202a B"), "G:\\A\u202a B")
        self.assertEqual(normalize_copied_path(None), "")

    def test_new_and_existing_catalog_paths_are_cleaned_when_saved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            catalog = db / "[KR][TVInfo][2020].yaml"
            row = {
                "domain": "tv-drama", "release_type": "tv", "country": "korea", "name": "韩剧",
                "date": {"start": "2020-01-02", "end": ""},
                "path": '\u202a"G:\\Video\\电视剧\\拥抱  太阳"',
                "collectioned_ordered": [{"press_format": "1080p", "press_group": "",
                                          "press_path": '"\u2066拥抱  太阳_1080p\u2069"'}],
            }
            browse_save_yaml_from_ui_body({"new_rows": [row]}, settings=_settings(db))
            data = load_yaml_string(catalog.read_text(encoding="utf-8"))[0]["attributes"][1]["data"]
            self.assertEqual(data["path"], "G:/Video/电视剧/拥抱  太阳")
            self.assertEqual(data["collectioned"][0]["press_path"], "拥抱  太阳_1080p")
            row.update({"yaml_source_rel": catalog.name, "index_in_file": 0,
                        "path": '"\u202a\\\\server\\share\\拥抱  太阳\u202c"'})
            browse_save_yaml_from_ui_body({"rows": [row]}, settings=_settings(db, catalog))
            data = load_yaml_string(catalog.read_text(encoding="utf-8"))[0]["attributes"][1]["data"]
            self.assertEqual(data["path"], "//server/share/拥抱  太阳")

    def test_catalog_save_reports_validation_and_commit_phases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "DB"
            db.mkdir()
            row = {"name": "新作品", "country": "korea", "domain": "tv-drama", "release_type": "tv",
                   "date": {"start": "", "end": ""}, "collectioned_ordered": []}
            with patch.object(catalog_save, "report_progress") as report:
                browse_save_yaml_from_ui_body({"new_rows": [row]}, settings=_settings(db), now=datetime(2026, 9, 21))
            messages = [call.args[0] for call in report.call_args_list]
            self.assertEqual(messages[0], "等待数据库写入锁")
            self.assertIn("校验并准备数据库文件", messages)
            self.assertIn("备份并原子写入数据库", messages)
            self.assertEqual(messages[-1], "数据库写入完成")
            self.assertEqual(report.call_args.kwargs, {"completed": 1, "total": 1, "unit": "文件"})

    def test_enum_rename_updates_config_and_catalog_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            cfg_dir = root / "Config"
            db.mkdir()
            cfg_dir.mkdir()
            catalog = db / "[JP][TVInfo][2099].yaml"
            catalog.write_text(
                textwrap.dedent(
                    """
                    - attributes:
                        - type: date
                          data:
                            start: "20990101"
                            end: "20990331"
                        - type: collection-type
                          data:
                            domain: animation
                            release_type: tv
                            collectioned:
                            - press_format: "A"
                              press_group: "G1"
                            continuations:
                            - collectioned:
                              - press_format: "A"
                                press_group: "G1"
                            markers: []
                        - type: country
                          data: japan
                        - type: name
                          data: "测试作品"
                    """,
                ).lstrip(),
                encoding="utf-8",
            )
            cfg = cfg_dir / "jp-tv-browse.config.yaml"
            cfg.write_text(
                textwrap.dedent(
                    f"""
                    paths:
                      filesystem_root: "{db.as_posix()}"
                    enum:
                    - name: press_format
                      values:
                      - A
                      - C
                    - name: press_group
                      values:
                      - G1
                    """,
                ).lstrip(),
                encoding="utf-8",
            )

            result = browse_apply_enum_edits_from_ui_body(
                {
                    "edits": [
                        {
                            "enum_key": "press_format",
                            "action": "rename",
                            "value": "A",
                            "new_value": "B",
                        },
                    ],
                },
                settings=_settings(db, catalog),
                config_path=cfg,
            )

            self.assertTrue(result["config_changed"])
            self.assertEqual(result["data_writes"][0]["changes"], 2)
            raw_cfg = load_yaml_string(cfg.read_text(encoding="utf-8"))
            fmt_values = raw_cfg["enum"][0]["values"]
            self.assertEqual(fmt_values, ["B", "C"])

            raw_data = load_yaml_string(catalog.read_text(encoding="utf-8"))
            coll = raw_data[0]["attributes"][1]["data"]
            self.assertEqual(coll["collectioned"][0]["press_format"], "B")
            self.assertEqual(
                coll["continuations"][0]["collectioned"][0]["press_format"],
                "B",
            )


if __name__ == "__main__":
    unittest.main()
