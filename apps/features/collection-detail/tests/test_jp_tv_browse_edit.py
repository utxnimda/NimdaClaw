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

from work_catalog_yaml import persistence

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
from work_catalog_yaml.yaml_io import load_yaml_string


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
                    ("media-directory-organizer", "目录整理", 30),
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

    def test_save_body_appends_new_row_to_current_year_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "DB"
            db.mkdir()
            current_catalog = db / "[JP][TVInfo][2026].yaml"

            writes = browse_save_yaml_from_ui_body(
                {
                    "new_rows": [
                        {
                            "domain": "animation",
                            "release_type": "tv",
                            "country": "japan",
                            "name": "新增行",
                            # 新增行内容日期不参与目标文件选择。
                            "date": {"start": "20990401", "end": "20990630"},
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
