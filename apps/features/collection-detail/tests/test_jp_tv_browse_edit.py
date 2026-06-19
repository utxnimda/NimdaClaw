from __future__ import annotations

import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path

from work_catalog_yaml.jp_tv.browse_save import (
    browse_apply_enum_edits_from_ui_body,
    browse_save_yaml_from_ui_body,
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
