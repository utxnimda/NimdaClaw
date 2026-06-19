from __future__ import annotations

import unittest

from work_catalog_yaml.jp_tv.browse_payload import build_jp_tv_browse_payload_single_group_order
from work_catalog_yaml.jp_tv.parse import parse_jp_tv_txt_content
from work_catalog_yaml.jp_tv.render import render_jp_tv_text
from work_catalog_yaml.jp_tv.validate import (
    entry_collection_type_data,
    entry_display_name,
    jp_tv_works_to_plain_list,
)


class JpTvParseRenderTest(unittest.TestCase):
    def test_parse_legacy_fixed_width_line(self) -> None:
        entries = parse_jp_tv_txt_content(
            "[20050105][20060629]                 DVDRip           魔法先生ネギま！\n"
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entry_display_name(entries[0]), "魔法先生ネギま！")
        coll = entry_collection_type_data(entries[0])
        self.assertEqual(
            coll["collectioned"],
            [{"press_format": "DVDRip", "press_group": ""}],
        )

    def test_parse_legacy_fixed_width_short_format_column(self) -> None:
        entries = parse_jp_tv_txt_content(
            "[20060109][20060626] 480p                             まほらば\n"
        )

        self.assertEqual(entry_display_name(entries[0]), "まほらば")
        coll = entry_collection_type_data(entries[0])
        self.assertEqual(
            coll["collectioned"],
            [{"press_format": "480p", "press_group": ""}],
        )

    def test_parse_bracket_pipe_line_still_strips_decoration(self) -> None:
        entries = parse_jp_tv_txt_content(
            "[20050107][20050708][-BDRip][JSUM] |ああっ女神さまっ\n"
        )

        self.assertEqual(entry_display_name(entries[0]), "ああっ女神さまっ")
        coll = entry_collection_type_data(entries[0])
        self.assertEqual(
            coll["collectioned"],
            [{"press_format": "BDRip", "press_group": "JSUM"}],
        )

    def test_render_collect_markers(self) -> None:
        entries = parse_jp_tv_txt_content(
            "[20150101][20150301][BDRip][JSUM][____MS] |作品标题\n"
        )

        rendered = render_jp_tv_text(entries)

        self.assertIn("[____MS]", rendered)
        self.assertIn("作品标题", rendered)

    def test_parse_and_render_preserve_continuation_rows(self) -> None:
        entries = parse_jp_tv_txt_content(
            "[20130106][20130331][--720p][CKCS][-BDRip][VCB] | Love Live!\n"
            "                    [-BDRip][JSUM]\n"
        )

        coll = entry_collection_type_data(entries[0])
        self.assertEqual(
            coll["collectioned"],
            [
                {"press_format": "720p", "press_group": "CKCS"},
                {"press_format": "BDRip", "press_group": "VCB"},
            ],
        )
        self.assertEqual(
            coll["continuations"],
            [
                {
                    "collectioned": [
                        {"press_format": "BDRip", "press_group": "JSUM"},
                    ],
                },
            ],
        )

        plain = jp_tv_works_to_plain_list(entries)
        coll_plain = plain[0]["attributes"][1]["data"]
        self.assertEqual(coll_plain["continuations"], coll["continuations"])

        rendered_lines = render_jp_tv_text(entries).splitlines()
        self.assertEqual(len(rendered_lines), 2)
        self.assertIn("[BDRip][JSUM]", rendered_lines[1])

        payload = build_jp_tv_browse_payload_single_group_order(entries)
        ordered = payload["profile_groups"][0]["rows"][0]["collectioned_ordered"]
        self.assertEqual(ordered[2]["segment"], "continuation")
        self.assertEqual(ordered[2]["continuation_index"], 0)


if __name__ == "__main__":
    unittest.main()
