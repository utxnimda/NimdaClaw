from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from directory_organizer.strategies import (
    BaseOrganizer, DirectoryRule, GenericOrganizer, STRATEGIES,
    infer_release_metadata, list_strategies, normalize_relative_path, propose_layout,
)


def files(*paths):
    return [{"relative_path": path, "size": 1024} for path in paths]


def destinations(plan):
    return {item["source_rel"]: item["target_rel"] for item in plan["files"]}


class StrategyMetadataTests(unittest.TestCase):
    def test_existing_release_suffix_is_authoritative_title(self):
        for source, title, fmt, group in (
            ("BLEACH_DVDRip", "BLEACH", "DVDRip", ""),
            ("血界戦線 & Beyond_BDRip(VCBM)", "血界戦線 & Beyond", "BDRip", "VCBM"),
            ("IV_1080p", "IV", "1080p", ""),
            ("グリザイアの果実_BDRip(VCB)", "グリザイアの果実", "BDRip", "VCB"),
            ("Fate／Zero_BDRip(----)", "Fate／Zero", "BDRip", ""),
        ):
            with self.subTest(source=source):
                out = infer_release_metadata(source)
                self.assertEqual((out["title"], out["press_format"], out["press_group"]), (title, fmt, group))
                self.assertEqual(out["issues"], [])

    def test_little_busters_vcb_collaboration(self):
        for title, year in (("Little Busters! EX", "2014"), ("Little Busters! Refrain", "2013")):
            source = title + " " + year + " [BD 1920x1080 AVC FLAC] - mawen1250&VCB-Studio"
            plan = propose_layout(source, files("01.mkv"))
            self.assertEqual(plan["strategy_id"], "generic")
            self.assertEqual(plan["title"], title)
            self.assertEqual(plan["release_name"], title + "_BDRip(VCBM)")

    def test_bracketed_title_is_not_codec_or_group(self):
        plan = propose_layout("[VCB-Studio&Airota][Kekkai Sensen & Beyond][BDRip][Ma10p_1080p][x265_flac]", files("01.mkv"))
        self.assertEqual(plan["title"], "Kekkai Sensen & Beyond")
        self.assertEqual(plan["strategy_id"], "generic")
        self.assertEqual(plan["issues"], [])

    def test_bracketed_combo_is_warning_not_guessed_records(self):
        plan = propose_layout("[2013-14][Little Busters! Refrain+EX][BDRIP][1080P][1-21Fin+SP]", files("01.mkv"))
        self.assertEqual(plan["title"], "Little Busters! Refrain+EX")
        self.assertEqual(plan["press_format"], "BDRip")
        self.assertEqual(plan["issues"][0]["code"], "work-multiple")
        self.assertFalse(plan["issues"][0]["blocking"])

    def test_technical_only_name_needs_title(self):
        plan = propose_layout("[VCB-Studio][BDRip][Ma10p_1080p][x265_flac]", files("01.mkv"))
        self.assertEqual(plan["title"], "")
        self.assertIn("title-unresolved", [item["code"] for item in plan["issues"]])

    def test_unknown_format_requires_manual_field_and_overrides_resolve_it(self):
        plan = propose_layout("死神", files("01.mkv"))
        self.assertIn("format-unresolved", [item["code"] for item in plan["issues"]])
        fixed = propose_layout("死神", files("01.mkv"), overrides={"press_format": "DVDRip", "press_group": ""})
        self.assertEqual(fixed["issues"], [])
        self.assertEqual(fixed["release_name"], "死神_DVDRip")

    def test_absent_group_is_supported_without_issue(self):
        plan = propose_layout("BLEACH_DVDRip", files("死神 [001].mkv"))
        self.assertEqual(plan["press_group"], "")
        self.assertEqual(plan["issues"], [])

    def test_explicit_generic_strategy_does_not_assign_a_release_group(self):
        plan = propose_layout("作品_BDRip", files("01.mkv"), "generic")
        self.assertEqual(plan["strategy_id"], "generic")
        self.assertEqual(plan["press_group"], "")

    def test_removed_and_unknown_strategy_ids_are_rejected(self):
        for strategy in ("manual", "vcb", "jsum", "missing"):
            with self.subTest(strategy=strategy):
                with self.assertRaisesRegex(ValueError, "未知整理类"):
                    propose_layout("作品_BDRip", files("01.mkv"), strategy)

    def test_user_override_can_select_auto_strategy(self):
        plan = propose_layout("作品_BDRip", files("01.mkv"), overrides={"press_group": "Jsum"})
        self.assertEqual(plan["strategy_id"], "generic")
        self.assertEqual(plan["release_name"], "作品_BDRip(Jsum)")

    def test_list_strategy_choices(self):
        choices = list_strategies()
        self.assertEqual([(item["id"], item["label"]) for item in choices], [("auto", "自动"), ("generic", "通用")])
        self.assertEqual(STRATEGIES, {"generic": GenericOrganizer})
        self.assertTrue(all(item["label"] and item["description"] for item in choices))

    def test_auto_and_generic_use_the_same_rules_without_changing_group_metadata(self):
        inputs = files("01.mkv", "01.ass", "cover.jpg", "SPs/CM01.mkv")
        expected = propose_layout("Work_BDRip(VCBA)", inputs, "generic")
        for strategy in ("auto", "generic"):
            with self.subTest(strategy=strategy):
                result = propose_layout("Work_BDRip(VCBA)", inputs, strategy)
                self.assertEqual(result["strategy_id"], "generic")
                self.assertEqual(destinations(result), destinations(expected))
                self.assertEqual(result["press_group"], "VCBA")


class StrategyLayoutTests(unittest.TestCase):
    def test_general_resource_categories(self):
        plan = propose_layout("作品_BDRip", files("01.mkv", "ost.flac", "NCOP01.mkv", "NCED01.ass", "cover.jpg", "notes.txt", "PV.mkv"))
        self.assertEqual(destinations(plan), {
            "01.mkv": "作品_BDRip_Disc/01.mkv", "ost.flac": "作品_BDRip_CD/ost.flac", "NCOP01.mkv": "作品_BDRip_OP+ED/NCOP01.mkv",
            "NCED01.ass": "作品_BDRip_OP+ED/NCED01.ass", "cover.jpg": "作品_BDRip_Image/cover.jpg", "notes.txt": "作品_BDRip_Others/notes.txt", "PV.mkv": "作品_BDRip_Others/PV.mkv",
        })

    def test_episode_video_with_subtitles_gets_one_episode_folder(self):
        plan = propose_layout("血界戦線 & Beyond_BDRip(VCBM)", files("[VCB-Studio] Kekkai Sensen & Beyond [01][Ma10p_1080p].mkv", "Kekkai Sensen & Beyond [01].chs.ass", "Kekkai Sensen & Beyond [02].mkv"))
        targets = list(destinations(plan).values())
        self.assertTrue(targets[0].startswith("血界戦線 & Beyond_BDRip_Disc/_01/"))
        self.assertTrue(targets[1].startswith("血界戦線 & Beyond_BDRip_Disc/_01/"))
        self.assertEqual(targets[2], "血界戦線 & Beyond_BDRip_Disc/Kekkai Sensen & Beyond [02].mkv")

    def test_multiple_video_versions_are_grouped(self):
        plan = propose_layout("作品_BDRip", files("作品 [01][1080p].mkv", "作品 [01][720p].mkv"))
        self.assertTrue(all(item["target_rel"].startswith("作品_BDRip_Disc/_01/") for item in plan["files"]))

    def test_chinese_bleach_episode_is_supported(self):
        plan = propose_layout("BLEACH_DVDRip", files("死神 - 001.mkv", "死神 - 001.简体.ass", "死神 - 365.mkv", "cover.jpg"))
        self.assertEqual(plan["issues"], [])
        self.assertEqual(destinations(plan)["死神 - 001.mkv"], "BLEACH_DVDRip_Disc/_001/死神 - 001.mkv")
        self.assertEqual(destinations(plan)["死神 - 365.mkv"], "BLEACH_DVDRip_Disc/死神 - 365.mkv")

    def test_seasons_do_not_merge_same_episode_numbers(self):
        plan = propose_layout("作品_BDRip", files("S01E01.mkv", "S01E01.ass", "S02E01.mkv", "S02E01.ass", "cover.jpg"))
        self.assertEqual(destinations(plan)["S01E01.mkv"], "作品_BDRip_Disc/_S01E01/S01E01.mkv")
        self.assertEqual(destinations(plan)["S02E01.mkv"], "作品_BDRip_Disc/_S02E01/S02E01.mkv")

    def test_episode_padding_differences_still_group_together(self):
        plan = propose_layout("BLEACH_DVDRip", files("死神 [001].mkv", "死神 [1].ass", "死神 [365].mkv", "cover.jpg"))
        self.assertEqual(destinations(plan)["死神 [001].mkv"], "BLEACH_DVDRip_Disc/_001/死神 [001].mkv")
        self.assertEqual(destinations(plan)["死神 [1].ass"], "BLEACH_DVDRip_Disc/_001/死神 [1].ass")

    def test_number_inside_work_title_is_not_an_episode(self):
        for title in ("5 Centimeters Per Second", "Yamada-kun to 7-nin no Majo", "3-gatsu no Lion"):
            with self.subTest(title=title):
                plan = propose_layout(title + "_BDRip", files(title + ".mkv", title + ".ass", "cover.jpg"))
                self.assertEqual(destinations(plan)[title + ".mkv"], title + "_BDRip_Disc/" + title + ".mkv")

    def test_resolution_only_release_with_subtitles_uses_normalized_disc_root(self):
        for source in ("IV_1080p", "Overlord IV_1080p", "作品_720p"):
            with self.subTest(source=source):
                plan = propose_layout(source, files("Overlord IV - 01.mkv", "Overlord IV - 01.ass", "Overlord IV - 02.mkv"))
                self.assertTrue(all(item["target_rel"].startswith(source + "_Disc/") for item in plan["files"]))
                self.assertEqual(plan["layout_assessment"]["kind"], "unorganized")

    def test_resolution_release_with_other_assets_uses_categories(self):
        plan = propose_layout("IV_1080p", files("01.mkv", "01.ass", "cover.jpg"))
        self.assertEqual(destinations(plan)["01.mkv"], "IV_1080p_Disc/_01/01.mkv")
        self.assertEqual(destinations(plan)["cover.jpg"], "IV_1080p_Image/cover.jpg")

    def test_album_auxiliaries_remain_with_audio_album(self):
        for strategy, folder in (("auto", "[EAC] Soundtrack"), ("generic", "[Hi-Res] Soundtrack"), ("generic", "CD/Album")):
            with self.subTest(strategy=strategy, folder=folder):
                plan = propose_layout("作品_BDRip", files(folder + "/01.flac", folder + "/cover.jpg", folder + "/rip.log"), strategy)
                expected_prefix = "作品_BDRip_CD/"
                self.assertTrue(all(item["category"] == "CD" and item["target_rel"].startswith(expected_prefix) for item in plan["files"]))

    def test_auxiliary_file_title_does_not_split_single_release(self):
        plan = propose_layout("山田くんと7人の魔女_BDRip", files("[Yamada-kun to 7-nin no Majo][01].mkv", "[Other music title]/01.flac"))
        self.assertEqual(plan["title"], "山田くんと7人の魔女")
        self.assertNotIn("work-multiple", [item["code"] for item in plan["issues"]])

    def test_nested_group_labels_removed_not_filenames(self):
        plan = propose_layout("作品_BDRip(VCB)", files("Scans [VCB-Studio]/[VCB-Studio] cover.jpg", "_Disc(VCBM)/[01].mkv"))
        self.assertEqual(destinations(plan)["Scans [VCB-Studio]/[VCB-Studio] cover.jpg"], "作品_BDRip_Image/[VCB-Studio] cover.jpg")
        self.assertEqual(destinations(plan)["_Disc(VCBM)/[01].mkv"], "作品_BDRip_Disc/[01].mkv")

    def test_organized_files_are_idempotent_for_every_strategy(self):
        inputs = files("_Disc/_01/作品 [01].mkv", "_Disc/_01/作品 [01].ass", "_CD/[EAC] Album/01.flac", "_CD/[EAC] Album/cover.jpg", "_OP+ED/NCOP01.mkv", "_Image/Scans/cover.jpg", "_Others/info.txt")
        for strategy in ("auto", "generic"):
            with self.subTest(strategy=strategy):
                plan = propose_layout("作品_BDRip", inputs, strategy)
                self.assertTrue(all(item["target_rel"].startswith("作品_BDRip_") for item in plan["files"]), plan)
                second = propose_layout("作品_BDRip", files(*(item["target_rel"] for item in plan["files"])), strategy)
                self.assertTrue(all(item["source_rel"] == item["target_rel"] for item in second["files"]))

    def test_repeated_preview_of_new_plan_is_idempotent(self):
        for strategy in ("auto", "generic"):
            first = propose_layout("作品_BDRip", files("01.mkv", "01.ass", "CD/album/01.flac", "NCOPs/01.mkv", "Scans/cover.jpg"), strategy)
            second = propose_layout(first["release_name"], files(*(item["target_rel"] for item in first["files"])), strategy)
            self.assertTrue(all(item["source_rel"] == item["target_rel"] for item in second["files"]), second)

    def test_existing_resolution_structure_is_not_flattened(self):
        plan = propose_layout("IV_1080p", files("_Disc/_01/01.mkv", "_Disc/_01/01.ass"))
        self.assertEqual(destinations(plan), {"_Disc/_01/01.mkv": "IV_1080p_Disc/_01/01.mkv", "_Disc/_01/01.ass": "IV_1080p_Disc/_01/01.ass"})

    def test_explicit_file_targets_can_preserve_every_file_or_choose_new_paths(self):
        paths = ("folder/01.mkv", "image.png", "music.flac")
        for strategy in ("auto", "generic"):
            with self.subTest(strategy=strategy):
                plan = propose_layout("杂项_DVDRip", files(*paths), strategy, {"targets": {path: path for path in paths}})
                self.assertEqual(plan["issues"], [])
                self.assertEqual(destinations(plan), {path: path for path in paths})
                changed = propose_layout("杂项_DVDRip", files("folder/01.mkv"), strategy,
                                         {"release_name": "手动目录", "targets": {"folder/01.mkv": "_Disc/01.mkv"}})
                self.assertEqual(changed["release_name"], "手动目录")
                self.assertEqual(destinations(changed)["folder/01.mkv"], "_Disc/01.mkv")

    def test_subtitle_only_pair_does_not_invent_video_episode_folder(self):
        plan = propose_layout("作品_BDRip", files("01.ass", "01.srt"))
        self.assertEqual(destinations(plan)["01.ass"], "作品_BDRip_Disc/01.ass")

    def test_derived_directory_filter_is_used_by_shared_algorithm(self):
        class CustomOrganizer(GenericOrganizer):
            def filter_directory(self, parts):
                if parts and parts[0] == "CustomAlbum":
                    return DirectoryRule("CD", 1, "用户自定义专辑目录")
                return super().filter_directory(parts)
        plan = CustomOrganizer().propose("作品_BDRip", files("CustomAlbum/data.bin"))
        self.assertEqual(destinations(plan)["CustomAlbum/data.bin"], "作品_BDRip_CD/data.bin")
        self.assertTrue(issubclass(GenericOrganizer, BaseOrganizer))


class GeneratedCategoryPrefixTests(unittest.TestCase):
    def test_nanoha_vcba_new_categories_use_work_format_without_group(self):
        title = "魔法少女リリカルなのは"
        prefix = title + "_BDRip"
        paths = ["Nanoha [01].mkv", "Nanoha [01].ass", "CDs/Album [VCBA]/01.flac",
                 "CDs/Album [VCBA]/cover.jpg", "NCOP01.mkv", "Scans [VCBA]/[VCBA] cover.jpg",
                 "Menu01.mkv", "font.ttf", "Subtitles.rar", "checksum.sfv"]
        result = propose_layout(prefix + "(VCBA)", files(*paths))
        self.assertEqual(result["strategy_id"], "generic")
        self.assertEqual(result["press_group"], "VCBA")
        self.assertEqual(result["issues"], [])
        self.assertEqual(destinations(result), {
            "Nanoha [01].mkv": prefix + "_Disc/_01/Nanoha [01].mkv",
            "Nanoha [01].ass": prefix + "_Disc/_01/Nanoha [01].ass",
            "CDs/Album [VCBA]/01.flac": prefix + "_CD/Album/01.flac",
            "CDs/Album [VCBA]/cover.jpg": prefix + "_CD/Album/cover.jpg",
            "NCOP01.mkv": prefix + "_OP+ED/NCOP01.mkv",
            "Scans [VCBA]/[VCBA] cover.jpg": prefix + "_Image/[VCBA] cover.jpg",
            "Menu01.mkv": prefix + "_Menu/Menu01.mkv",
            "font.ttf": prefix + "_Fonts/font.ttf",
            "Subtitles.rar": prefix + "_Subs/Subtitles.rar",
            "checksum.sfv": prefix + "_Others/checksum.sfv",
        })
        self.assertEqual({row["category"] for row in result["files"]}, {"Disc", "CD", "OP+ED", "Image", "Menu", "Fonts", "Subs", "Others"})
        self.assertTrue(all("VCBA" not in row["target_rel"].split("/", 1)[0] for row in result["files"]))
        second = propose_layout(result["release_name"], files(*(row["target_rel"] for row in result["files"])))
        self.assertEqual(second["issues"], [])
        self.assertTrue(all(row["source_rel"] == row["target_rel"] for row in second["files"]))

    def test_standard_target_release_name_controls_new_category_prefix(self):
        for release, prefix in (("更名作品_DVDRip(JSUM)", "更名作品_DVDRip"),
                                ("Renamed_web-dl(VCBA)", "Renamed_WEB-DL"),
                                ("IV_1080p", "IV_1080p")):
            with self.subTest(release=release):
                result = propose_layout("Original_BDRip(VCBA)", files("01.mkv", "cover.jpg"),
                                        overrides={"release_name": release, "title": "DB alias", "press_format": "BDRip"})
                self.assertEqual(result["release_name"], release)
                self.assertEqual(destinations(result), {"01.mkv": prefix + "_Disc/01.mkv", "cover.jpg": prefix + "_Image/cover.jpg"})

    def test_nonstandard_target_uses_overridden_metadata_and_canonical_format(self):
        for raw_format, canonical in (("bd-rip", "BDRip"), ("dvd", "DVDRip"), ("webdl", "WEB-DL")):
            with self.subTest(raw_format=raw_format):
                result = propose_layout("Original_BDRip(VCBA)", files("01.mkv", "song.flac", "cover.jpg"),
                                        overrides={"release_name": "手动目标", "title": "Metadata title", "press_format": raw_format, "press_group": "VCBM"})
                prefix = "Metadata title_" + canonical
                self.assertEqual(result["release_name"], "手动目标")
                self.assertEqual(destinations(result), {"01.mkv": prefix + "_Disc/01.mkv", "song.flac": prefix + "_CD/song.flac", "cover.jpg": prefix + "_Image/cover.jpg"})

    def test_metadata_only_overrides_produce_safe_prefixed_category_names(self):
        result = propose_layout("Original_BDRip(VCBA)", files("cover.jpg"),
                                overrides={"title": "Fate/Zero: Test?", "press_format": "dvd", "press_group": "Jsum"})
        self.assertEqual(destinations(result)["cover.jpg"], "Fate／Zero： Test？_DVDRip_Image/cover.jpg")
        self.assertEqual(result["issues"], [])

    def test_target_rename_normalizes_category_roots_and_keeps_resource_contents(self):
        kept = ["Old_BDRip_Disc/Old_01/Show [01].mkv", "Old_BDRip_Image/cover.jpg",
                "Old_BDRip_Music/Album/01.flac", "Old_BDRip_Booklet/Vol.1/001.webp"]
        result = propose_layout("Original_BDRip(VCBA)", files(*kept, "Show [01].ass", "new.jpg", "song.flac"),
                                overrides={"release_name": "Renamed_DVDRip(Jsum)"})
        targets = destinations(result)
        self.assertEqual({path: targets[path] for path in kept}, {path: path.replace("Old_BDRip_", "Renamed_DVDRip_", 1) for path in kept})
        self.assertEqual(targets["Show [01].ass"], "Renamed_DVDRip_Disc/Old_01/Show [01].ass")
        self.assertEqual(targets["new.jpg"], "Renamed_DVDRip_Image/new.jpg")
        self.assertEqual(targets["song.flac"], "Renamed_DVDRip_CD/song.flac")
        self.assertEqual(result["issues"], [])

    def test_explicit_file_targets_are_not_prefixed_or_normalized_into_categories(self):
        explicit = {"01.mkv": "_Disc/manual/01.mkv", "song.flac": "Music/Handpicked/song.flac", "cover.jpg": "Booklet/Printed/cover.jpg"}
        result = propose_layout("Nanoha_BDRip(VCBA)", files(*explicit, "notes.txt"), overrides={"targets": explicit})
        targets = destinations(result)
        self.assertTrue(all(targets[source] == target for source, target in explicit.items()))
        self.assertEqual(targets["notes.txt"], "Nanoha_BDRip_Others/notes.txt")
        second = propose_layout(result["release_name"], files(*targets.values()), overrides={"targets": {value: value for value in explicit.values()}})
        self.assertTrue(all(row["source_rel"] == row["target_rel"] for row in second["files"]))

    def test_custom_target_for_file_is_not_wrapped_by_default_prefix(self):
        class CustomOrganizer(GenericOrganizer):
            def target_for_file(self, relative_path, classification, *, episode_folder=""):
                return "Handpicked/" + relative_path

        result = CustomOrganizer().propose("Nanoha_BDRip", files("01.mkv", "cover.jpg"))
        self.assertEqual(destinations(result), {"01.mkv": "Handpicked/01.mkv", "cover.jpg": "Handpicked/cover.jpg"})

    def test_category_directory_override_normalizes_roots_without_flattening_contents(self):
        class CustomOrganizer(GenericOrganizer):
            def category_directory(self, classification, *, title, press_format):
                return "Custom_" + classification.category

        result = CustomOrganizer().propose("Nanoha_BDRip", files("01.mkv", "cover.jpg", "_CD/Album/01.flac", "song.flac"))
        self.assertEqual(destinations(result), {"01.mkv": "Custom_Disc/01.mkv", "cover.jpg": "Custom_Image/cover.jpg",
                         "_CD/Album/01.flac": "Custom_CD/Album/01.flac", "song.flac": "Custom_CD/song.flac"})

    def test_pure_generic_disc_and_subtitle_layout_uses_final_target_prefix(self):
        paths = ["Show [01].mkv", "Show [01].ass", "Show [02].mkv", "Show [02].srt"]
        result = propose_layout("Show_BDRip", files(*paths), overrides={"release_name": "Renamed_DVDRip"})
        self.assertEqual(destinations(result), {path: "Renamed_DVDRip_Disc/_" + ("01" if "[01]" in path else "02") + "/" + path for path in paths})
        self.assertEqual(result["layout_assessment"]["kind"], "unorganized")


class StrategySafetyTests(unittest.TestCase):
    def test_planning_is_pure_and_does_not_mutate_inputs(self):
        source = files("01.mkv", "01.ass")
        overrides = {"title": "作品", "press_format": "BDRip", "files": [{"source_rel": "01.ass", "target_rel": "Subs/01.ass"}]}
        before = deepcopy((source, overrides))
        with patch("builtins.open", side_effect=AssertionError("strategy must not perform I/O")):
            result = propose_layout("作品_BDRip", source, overrides=overrides)
        self.assertEqual((source, overrides), before)
        self.assertEqual(destinations(result)["01.ass"], "Subs/01.ass")

    def test_unsafe_sources_are_blocked(self):
        for bad in ("../x.mkv", "/x.mkv", "C:/x.mkv", "\\\\server\\x.mkv", "a//b.mkv", "a/./b.mkv", "a/CON.txt", "a/b.mkv:evil", "a./b.mkv", "a /b.mkv"):
            with self.subTest(path=bad):
                result = propose_layout("作品_BDRip", files(bad))
                self.assertEqual(result["files"], [])
                self.assertTrue(result["issues"][0]["blocking"])
                self.assertEqual(result["issues"][0]["code"], "unsafe-source-path")

    def test_unsafe_manual_target_is_not_returned_as_executable_target(self):
        result = propose_layout("作品_BDRip", files("01.mkv"), overrides={"targets": {"01.mkv": "../outside.mkv"}})
        self.assertEqual(result["files"][0]["target_rel"], "01.mkv")
        self.assertEqual(result["issues"][0]["code"], "unsafe-target-path")
        self.assertTrue(result["issues"][0]["blocking"])

    def test_case_insensitive_target_conflicts_are_blocking(self):
        result = propose_layout("作品_BDRip", files("A/01.mkv", "B/01.mkv"), overrides={"targets": {"A/01.mkv": "_Disc/01.mkv", "B/01.mkv": "_disc/01.MKV"}})
        self.assertEqual(result["issues"][0]["code"], "target-conflict")
        self.assertTrue(result["issues"][0]["blocking"])

    def test_duplicate_source_does_not_create_double_operation(self):
        result = propose_layout("作品_BDRip", files("01.mkv", "01.MKV"))
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["issues"][0]["code"], "duplicate-source")

    def test_windows_relative_separators_are_normalized(self):
        self.assertEqual(normalize_relative_path("_Disc\\_01\\01.mkv"), "_Disc/_01/01.mkv")

    def test_generated_release_name_uses_safe_windows_punctuation(self):
        result = propose_layout("作品_BDRip", files("01.mkv"), overrides={"title": "Fate/Zero: Test?"})
        self.assertEqual(result["release_name"], "Fate／Zero： Test？_BDRip")

    def test_empty_manual_metadata_remains_unresolved_in_automatic_mode(self):
        result = propose_layout("作品_BDRip", files("01.mkv"), overrides={"title": "", "press_format": ""})
        self.assertEqual({item["code"] for item in result["issues"]}, {"title-unresolved", "format-unresolved"})


class ExistingLayoutTests(unittest.TestCase):
    def test_legacy_nanoha_supported_roots_normalize_and_unsupported_folders_enter_others(self):
        prefix = "魔法少女リリカルなのは Movie_BDRip"
        paths = [
            f"{prefix}_Disc/{prefix}_1st/movie.mkv", f"{prefix}_Disc/{prefix}_1st/movie.ass",
            f"{prefix}_Booklet/{prefix}_Booklet_1st/cover.webp", f"{prefix}_Music/Album/01.flac",
            f"{prefix}_Music/Album/Scans/cover.jpg", f"{prefix}_OP+ED/NCOP01.mkv",
            *[f"{prefix}_{category}/content.bin" for category in ("CM", "EV", "Fonts", "Menu", "Other", "PV", "SP", "Interview", "SpecialTalk", "Visual Fan Book", "Subs", "IV", "Story")],
        ]
        expected = {}
        for path in paths:
            root, rest = path.split("/", 1)
            category = root[len(prefix) + 1:]
            if category in {"Disc", "Booklet", "Music", "OP+ED", "CM", "Fonts", "Menu", "Subs"}:
                expected[path] = path
            elif category == "Other":
                expected[path] = prefix + "_Others/" + rest
            elif category == "Visual Fan Book":
                expected[path] = prefix + "_Booklet/" + rest
            else:
                expected[path] = prefix + "_Others/" + path
        for strategy in ("auto", "generic"):
            with self.subTest(strategy=strategy):
                result = propose_layout(prefix, files(*paths), strategy)
                self.assertEqual(destinations(result), expected)
                self.assertEqual(result["layout_assessment"]["preserved_files"], sum(source == target for source, target in expected.items()))
                second = propose_layout(prefix, files(*expected.values()), strategy)
                self.assertTrue(all(row["source_rel"] == row["target_rel"] for row in second["files"]))

    def test_vivid_legacy_decimal_episode_directories_do_not_get_new_wrappers(self):
        for release in ("ViVid Strike!_1080p", "ViVid Strike!_BDRip"):
            paths = [f"{release}_Disc/{release}_{episode}/[SumiSora][ViVid_Strike][{episode}].{ext}"
                     for episode in ("01", "05.5", "05.75") for ext in ("mkv", "sc.ass", "tc.ass")]
            paths += [f"{release}_Other/OPMV.mkv", f"{release}_Booklet/Vol.1/001.webp"]
            result = propose_layout(release, files(*paths))
            self.assertEqual(destinations(result), {path: path.replace(release + "_Other/", release + "_Others/") for path in paths})

    def test_plain_category_roots_gain_the_standard_prefix_without_extra_wrappers(self):
        paths = ["Disc/01/01.mkv", "Disc/01/01.ass", "CD/Album/01.flac", "Image/cover.jpg",
                 "Booklet/Vol.1/001.webp", "Menu/01.mkv", "Others/info.txt"]
        result = propose_layout("作品_BDRip", files(*paths))
        self.assertEqual(destinations(result), {path: "作品_BDRip_" + path for path in paths})
        self.assertEqual(result["layout_assessment"]["kind"], "unorganized")

    def test_legacy_prefix_uses_new_title_without_changing_internal_title_folders(self):
        paths = ["Old Name_DVDRip_Disc/Old Name_01/01.avi", "Old Name_DVDRip_Image/cover.jpg"]
        result = propose_layout("新作品名_DVDRip", files(*paths))
        self.assertEqual(destinations(result), {path: path.replace("Old Name_DVDRip_", "新作品名_DVDRip_", 1) for path in paths})

    def test_existing_episode_paths_are_reused_beneath_normalized_category_roots(self):
        kept = ["Old_BDRip_Disc/Old_BDRip_01/[01].mkv", "Old_BDRip_Booklet/001.webp", "Old_BDRip_Other/readme.txt"]
        result = propose_layout("New_BDRip", files(*kept, "[01].ass", "new.webp", "info.txt"))
        targets = destinations(result)
        self.assertEqual({path: targets[path] for path in kept}, {path: path.replace("Old_BDRip_", "New_BDRip_", 1).replace("_Other/", "_Others/") for path in kept})
        self.assertEqual(targets["[01].ass"], "New_BDRip_Disc/Old_BDRip_01/[01].ass")
        self.assertEqual(targets["new.webp"], "New_BDRip_Image/new.webp")
        self.assertEqual(targets["info.txt"], "New_BDRip_Others/info.txt")
        self.assertEqual(result["layout_assessment"]["preserved_files"], 0)
        self.assertEqual(result["layout_assessment"]["suggested_moves"], 6)

    def test_generic_video_subtitle_layouts_always_use_categories_for_common_formats(self):
        for fmt in ("BDRip", "DVDRip", "WEB-DL", "WEBRip", "1080p", "720p"):
            with self.subTest(fmt=fmt):
                paths = ["作品 [01].mkv", "作品 [01].chs.ass", "作品 [01].cht.ass", "作品 [02].mkv"]
                result = propose_layout("作品_" + fmt, files(*paths))
                self.assertEqual(destinations(result), {path: "作品_" + fmt + "_Disc/" + ("_01/" if "[01]" in path else "") + path for path in paths})
                self.assertEqual(result["layout_assessment"]["kind"], "unorganized")

    def test_known_release_suffix_does_not_hide_unorganized_mixed_assets(self):
        result = propose_layout("作品_BDRip", files("01.mkv", "cover.jpg", "track.flac", "info.txt"))
        self.assertEqual(result["layout_assessment"]["kind"], "unorganized")
        self.assertEqual(result["layout_assessment"]["suggested_moves"], 4)
        self.assertEqual(destinations(result)["01.mkv"], "作品_BDRip_Disc/01.mkv")

    def test_multiple_video_versions_still_need_episode_group_even_for_resolution_release(self):
        for fmt in ("BDRip", "1080p"):
            result = propose_layout("作品_" + fmt, files("作品 [01][v1].mkv", "作品 [01][v2].mkv", "作品 [01].ass"))
            self.assertEqual(result["layout_assessment"]["kind"], "unorganized")
            self.assertTrue(all(item["target_rel"].startswith("作品_" + fmt + "_Disc/_01/") for item in result["files"]))

    def test_vcb_collaboration_abbreviations_use_common_rules_without_losing_group(self):
        for group in ("VCB", "VCBA", "VCBS", "VCBM", "VCB-Studio"):
            with self.subTest(group=group):
                result = propose_layout("作品_BDRip(" + group + ")", files("01.mkv", "CDs/[EAC] Album/01.flac", "Scans/box.webp", "SPs/NCOP.mkv", "checksum.sfv"))
                self.assertEqual(result["strategy_id"], "generic")
                self.assertEqual(result["press_group"], group)
                self.assertEqual(result["layout_assessment"]["kind"], "unorganized")
                self.assertEqual(destinations(result)["01.mkv"], "作品_BDRip_Disc/01.mkv")
                self.assertEqual(destinations(result)["SPs/NCOP.mkv"], "作品_BDRip_OP+ED/NCOP.mkv")

    def test_group_metadata_does_not_disable_common_episode_grouping(self):
        for group in ("VCB", "Jsum"):
            result = propose_layout("作品_BDRip(" + group + ")", files("01.mkv", "01.ass"))
            self.assertEqual(destinations(result)["01.mkv"], "作品_BDRip_Disc/_01/01.mkv")
            self.assertEqual(result["layout_assessment"]["kind"], "unorganized")

    def test_bracketed_vcb_collaboration_keeps_its_specific_group_code(self):
        result = propose_layout("[VCBA][作品][BDRip]", files("01.mkv", "Scans/cover.webp"))
        self.assertEqual(result["strategy_id"], "generic")
        self.assertEqual(result["press_group"], "VCBA")
        self.assertEqual(result["title"], "作品")

    def test_multiple_legacy_category_roots_merge_into_one_standard_root_when_paths_do_not_conflict(self):
        paths = ["A_BDRip_Disc/01.mkv", "B_BDRip_Disc/02.mkv", "03.mkv"]
        result = propose_layout("作品_BDRip", files(*paths))
        self.assertEqual(destinations(result), {paths[0]: "作品_BDRip_Disc/01.mkv", paths[1]: "作品_BDRip_Disc/02.mkv", paths[2]: "作品_BDRip_Disc/03.mkv"})
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["layout_assessment"]["kind"], "unorganized")
        fixed = propose_layout("作品_BDRip", files(*paths), overrides={"targets": {"03.mkv": "A_BDRip_Disc/03.mkv"}})
        self.assertFalse(any(item["code"] == "existing-category-ambiguous" for item in fixed["issues"]))
        self.assertEqual(destinations(fixed)["03.mkv"], "A_BDRip_Disc/03.mkv")

    def test_multiple_existing_episode_versions_require_manual_subtitle_choice(self):
        paths = ["Disc/version-a/[01].mkv", "Disc/version-b/[01].mkv", "[01].ass"]
        result = propose_layout("作品_BDRip", files(*paths))
        self.assertEqual(destinations(result)["[01].ass"], "[01].ass")
        self.assertTrue(any(item["code"] == "subtitle-video-ambiguous" and item["blocking"] for item in result["issues"]))
        fixed = propose_layout("作品_BDRip", files(*paths), overrides={"targets": {"[01].ass": "Disc/version-b/[01].ass"}})
        self.assertFalse(any(item["code"] == "subtitle-video-ambiguous" for item in fixed["issues"]))

    def test_existing_layout_still_allows_explicit_manual_target_changes(self):
        path = "作品_BDRip_Disc/01.mkv"
        result = propose_layout("作品_BDRip", files(path), overrides={"targets": {path: "_Disc/01.mkv"}})
        self.assertEqual(destinations(result)[path], "_Disc/01.mkv")
        self.assertEqual(result["layout_assessment"]["suggested_moves"], 1)


class AuthoritativeCategoryTests(unittest.TestCase):
    def test_cm_video_subtitles_and_explicit_directories_use_the_common_cm_root(self):
        paths = ["CM01.mkv", "CM01.ass", "Old_BDRip_CM/CM02.mkv", "SPs/CM/CM03.mkv", "PV01.mkv"]
        result = propose_layout("Work_BDRip", files(*paths))
        self.assertEqual(destinations(result), {
            paths[0]: "Work_BDRip_CM/CM01.mkv", paths[1]: "Work_BDRip_CM/CM01.ass",
            paths[2]: "Work_BDRip_CM/CM02.mkv", paths[3]: "Work_BDRip_CM/CM/CM03.mkv",
            paths[4]: "Work_BDRip_Others/PV01.mkv",
        })
        self.assertEqual(result["issues"], [])

    def test_menu_fonts_subtitle_archives_and_other_have_explicit_categories(self):
        plan = propose_layout("作品_BDRip", files("Menu01.mkv", "[Menu02].bmp", "font.ttf", "Fonts.7z", "字幕.zip", "Show.ass.rar", "info.bin"))
        self.assertEqual(destinations(plan), {"Menu01.mkv": "作品_BDRip_Menu/Menu01.mkv", "[Menu02].bmp": "作品_BDRip_Menu/[Menu02].bmp",
                         "font.ttf": "作品_BDRip_Fonts/font.ttf", "Fonts.7z": "作品_BDRip_Fonts/Fonts.7z", "字幕.zip": "作品_BDRip_Subs/字幕.zip",
                         "Show.ass.rar": "作品_BDRip_Subs/Show.ass.rar", "info.bin": "作品_BDRip_Others/info.bin"})

    def test_explicit_subtitle_bundle_beats_ancillary_font_changed_label(self):
        path = "[FLsnow][Mahoushoujo_Ririkaru_Nanoha][Subtitles][BDRIP][v3.1][CHT_Font_Changed_For_Windows_10].rar"
        result = propose_layout("作品_BDRip", files(path))
        self.assertEqual(destinations(result)[path], "作品_BDRip_Subs/" + path)
        self.assertEqual(result["files"][0]["category"], "Subs")

    def test_op_ed_video_and_subtitles_but_standalone_audio_is_cd(self):
        paths = ["NCOP01.mkv", "NCED01.ass", "Opening.mp4", "Ending.mp4", "NCOP01.flac", "NCED01.mka", "NCOPs/song.flac"]
        result = propose_layout("作品_BDRip", files(*paths), "generic")
        rows = {item["source_rel"]: item for item in result["files"]}
        for path in paths[:4]:
            self.assertEqual(rows[path]["category"], "OP+ED")
        for path in paths[4:6]:
            self.assertEqual(rows[path]["category"], "CD")
        self.assertEqual(rows[paths[6]]["category"], "OP+ED", "an explicit resource directory owns all its contents")

    def test_chinese_and_japanese_opening_ending_synonyms_are_recognized(self):
        paths = ["片头.mkv", "片尾.mkv", "片頭.mkv", "オープニング.mkv", "エンディング.mkv", "開場動畫.mkv", "开场动画.mkv", "結束動畫.mkv"]
        result = propose_layout("作品_BDRip", files(*paths, "片頭.flac", "菜單.mkv"))
        rows = {item["source_rel"]: item for item in result["files"]}
        for path in paths:
            self.assertEqual(rows[path]["category"], "OP+ED", path)
        self.assertEqual(rows["片頭.flac"]["category"], "CD")
        self.assertEqual(rows["菜單.mkv"]["category"], "Menu")

    def test_music_and_booklet_are_manual_and_not_destinations_for_new_files(self):
        kept = ["旧_BDRip_Music/album/01.tta", "旧_BDRip_Music/album/01.cue", "旧_BDRip_Music/album/cover.jpg",
                "旧_BDRip_Music/album/rip.log", "旧_BDRip_Booklet/Vol.1/001.webp", "旧_BDRip_Booklet/Subs/01.ass"]
        result = propose_layout("作品_BDRip", files(*kept, "song.flac", "new.jpg"))
        targets = destinations(result)
        self.assertEqual({path: targets[path] for path in kept}, {path: path.replace("旧_BDRip_", "作品_BDRip_", 1) for path in kept})
        self.assertEqual(targets["song.flac"], "作品_BDRip_CD/song.flac")
        self.assertEqual(targets["new.jpg"], "作品_BDRip_Image/new.jpg")
        self.assertEqual(result["issues"], [])

    def test_explicit_manual_containers_take_precedence_at_any_depth(self):
        paths = ["Scans/旧_Booklet/Vol.1/cover.jpg", "CDs/旧_Music/01.flac", "CDs/旧_Music/Subs/01.ass"]
        result = propose_layout("作品_BDRip(VCBA)", files(*paths))
        self.assertEqual(destinations(result), {
            paths[0]: "作品_BDRip_Booklet/Vol.1/cover.jpg", paths[1]: "作品_BDRip_Music/01.flac", paths[2]: "作品_BDRip_Music/Subs/01.ass"})
        self.assertEqual(result["issues"], [])

    def test_bare_booklet_inside_publisher_album_stays_with_whole_album(self):
        paths = ["CDs/[EAC] Album/01.flac", "CDs/[EAC] Album/Booklet/cover.jpg", "CDs/[EAC] Album/Booklet/readme.log"]
        result = propose_layout("作品_BDRip(VCBA)", files(*paths))
        targets = destinations(result)
        self.assertTrue(all(targets[path] == "作品_BDRip_CD/" + path[len("CDs/"):] for path in paths))
        self.assertTrue(all(item["category"] == "CD" for item in result["files"]))

    def test_direct_bare_music_and_booklet_remain_manual(self):
        paths = ["Music/01.flac", "Music/cover.jpg", "Booklet/001.webp", "Booklet/01.ass"]
        result = propose_layout("作品_BDRip", files(*paths, "new.flac", "new.webp"))
        self.assertEqual({path: destinations(result)[path] for path in paths}, {path: "作品_BDRip_" + path for path in paths})
        self.assertEqual(destinations(result)["new.flac"], "作品_BDRip_CD/new.flac")
        self.assertEqual(destinations(result)["new.webp"], "作品_BDRip_Image/new.webp")

    def test_legacy_and_new_other_resources_share_standard_others_root(self):
        old = propose_layout("作品_BDRip", files("_Others/keep.txt", "new.txt"))
        self.assertEqual(destinations(old)["_Others/keep.txt"], "作品_BDRip_Others/keep.txt")
        self.assertEqual(destinations(old)["new.txt"], "作品_BDRip_Others/new.txt")
        new = propose_layout("作品_BDRip", files("new.txt"))
        self.assertEqual(destinations(new)["new.txt"], "作品_BDRip_Others/new.txt")

    def test_external_audio_follows_existing_episode_with_unpacked_subtitles(self):
        paths = ["_Disc/_01/Show [01].mkv", "_Disc/_01/Show [01].chs.ass", "Show [01].JPN.flac", "01.mka"]
        result = propose_layout("Show_BDRip", files(*paths))
        targets = destinations(result)
        self.assertEqual(targets[paths[0]], "Show_BDRip" + paths[0])
        self.assertEqual(targets[paths[1]], "Show_BDRip" + paths[1])
        self.assertEqual(targets["Show [01].JPN.flac"], "Show_BDRip_Disc/_01/Show [01].JPN.flac")
        self.assertEqual(targets["01.mka"], "Show_BDRip_Disc/_01/01.mka")
        self.assertEqual(result["issues"], [])

    def test_external_audio_follows_video_new_target_not_original_root(self):
        result = propose_layout("Show_BDRip(VCB)", files("Show [01].mkv", "Show [01].flac", "Show [01].ass"))
        self.assertTrue(all(item["target_rel"].startswith("Show_BDRip_Disc/_01/") for item in result["files"]))
        self.assertTrue(all(item["category"] == "Disc" for item in result["files"]))

    def test_numeric_album_tracks_and_unrelated_song_do_not_become_episodes(self):
        paths = ["Show [01].mkv", "CDs/Album/01.flac", "CDs/Album/01.cue", "Song [01].flac", "01 - Intro.flac", "OST [01].mka"]
        result = propose_layout("Show_BDRip", files(*paths))
        for item in result["files"][1:]:
            self.assertEqual(item["category"], "CD", item)
            self.assertTrue(item["target_rel"].startswith("Show_BDRip_CD/"), item)

    def test_existing_subs_unpacked_files_follow_actual_episode_but_archive_stays(self):
        paths = ["作品_BDRip_Disc/作品_01/Show [01].mkv", "作品_BDRip_Subs/Show [01].ass", "作品_BDRip_Subs/01.srt", "作品_BDRip_Subs/Subtitles.rar"]
        result = propose_layout("作品_BDRip", files(*paths))
        targets = destinations(result)
        self.assertEqual(targets[paths[1]], "作品_BDRip_Disc/作品_01/Show [01].ass")
        self.assertEqual(targets[paths[2]], "作品_BDRip_Disc/作品_01/01.srt")
        self.assertEqual(targets[paths[3]], paths[3])
        self.assertEqual(result["issues"], [])

    def test_existing_subs_without_video_is_blocked_until_manual_target(self):
        path = "_Subs/Show [01].ass"
        result = propose_layout("Show_BDRip", files(path))
        self.assertEqual(destinations(result)[path], path)
        self.assertTrue(any(item["code"] == "subtitle-video-unresolved" and item["blocking"] for item in result["issues"]))
        fixed = propose_layout("Show_BDRip", files(path), overrides={"targets": {path: "_Disc/_01/Show [01].ass"}})
        self.assertFalse(any(item["code"] == "subtitle-video-unresolved" for item in fixed["issues"]))

    def test_existing_subs_multiple_episode_directories_require_choice(self):
        paths = ["_Disc/version-a/Show [01].mkv", "_Disc/version-b/Show [01].mkv", "_Subs/Show [01].ass"]
        result = propose_layout("Show_BDRip", files(*paths))
        self.assertEqual(destinations(result)[paths[2]], paths[2])
        self.assertTrue(any(item["code"] == "subtitle-video-ambiguous" and item["blocking"] for item in result["issues"]))

    def test_audio_multiple_episode_directories_require_choice(self):
        paths = ["_Disc/version-a/Show [01].mkv", "_Disc/version-b/Show [01].mkv", "01.mka"]
        result = propose_layout("Show_BDRip", files(*paths))
        self.assertEqual(destinations(result)["01.mka"], "01.mka")
        self.assertTrue(any(item["code"] == "audio-video-ambiguous" and item["blocking"] for item in result["issues"]))

    def test_companion_checks_final_manual_video_destinations(self):
        paths = ["Show [01][1080p].mkv", "Show [01][720p].mkv", "_Subs/Show [01].ass", "01.mka"]
        targets = {paths[0]: "version-a/Show [01].mkv", paths[1]: "version-b/Show [01].mkv"}
        result = propose_layout("Show_BDRip(VCB)", files(*paths), overrides={"targets": targets})
        self.assertEqual(destinations(result)[paths[2]], paths[2])
        self.assertEqual(destinations(result)[paths[3]], paths[3])
        self.assertEqual({item["code"] for item in result["issues"]}, {"subtitle-video-ambiguous", "audio-video-ambiguous"})

    def test_multiple_versions_planned_into_one_episode_share_companions(self):
        paths = ["Show [01][1080p].mkv", "Show [01][720p].mkv", "_Subs/Show [01].ass", "01.mka"]
        result = propose_layout("Show_BDRip(VCB)", files(*paths))
        self.assertEqual(result["issues"], [])
        self.assertTrue(all(item["target_rel"].startswith("Show_BDRip_Disc/_01/") for item in result["files"]))

    def test_simple_generic_release_with_external_audio_uses_one_disc_episode_folder(self):
        paths = ["Show [01].mkv", "Show [01].ass", "Show [01].mka"]
        result = propose_layout("Show_BDRip", files(*paths))
        self.assertEqual(destinations(result), {path: "Show_BDRip_Disc/_01/" + path for path in paths})
        self.assertEqual(result["layout_assessment"]["kind"], "unorganized")

    def test_explicit_target_can_keep_unmatched_unpacked_subtitle_in_place(self):
        path = "_Subs/Show [01].ass"
        for strategy in ("auto", "generic"):
            with self.subTest(strategy=strategy):
                result = propose_layout("Show_BDRip", files(path), strategy, {"targets": {path: path}})
                self.assertEqual(destinations(result)[path], path)
                self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main()
