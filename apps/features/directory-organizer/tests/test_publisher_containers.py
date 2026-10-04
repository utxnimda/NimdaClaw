from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from directory_organizer.strategies import GenericOrganizer, propose_layout


def files(*paths):
    return [{"relative_path": path, "size": 1024} for path in paths]


def destinations(plan):
    return {row["source_rel"]: row["target_rel"] for row in plan["files"]}


STRATEGIES = (
    ("auto", "Work_BDRip"),
    ("auto", "Work_BDRip(VCBA)"),
    ("auto", "Work_BDRip(Jsum)"),
    ("generic", "Work_BDRip"),
)
DVD_STRATEGIES = ("auto", "generic")


class PublisherContainerTests(unittest.TestCase):
    def test_singular_sp_and_plural_sps_expose_dvdrip_resources_for_all_strategies(self):
        resources = {"Menu01.mkv": ("Menu", "Menu01.mkv"),
                     "CM01.mkv": ("CM", "CM01.mkv"),
                     "NCOP01.mkv": ("OP+ED", "NCOP01.mkv"),
                     "NCED01.mkv": ("OP+ED", "NCED01.mkv"),
                     "Menu/01.mkv": ("Menu", "Menu/01.mkv"),
                     "Unknown/01.mkv": ("Others", "Unknown/01.mkv"),
                     "Unknown/cover.jpg": ("Others", "Unknown/cover.jpg")}
        for wrapper in ("sp", "SP", "Sp", "sps"):
            for strategy in DVD_STRATEGIES:
                with self.subTest(wrapper=wrapper, strategy=strategy):
                    expected = {wrapper + "/" + path: "Work_DVDRip_" + category + "/" + target
                                for path, (category, target) in resources.items()}
                    result = propose_layout("Work_DVDRip", files(*expected), strategy)
                    self.assertEqual(destinations(result), expected)
                    self.assertEqual(result["issues"], [])

    def test_dvdrip_sp_fixture_classifies_all_48_resources_without_file_access(self):
        names = {"CM": ["CM" + str(number) for number in range(1, 8)],
                 "OP+ED": ["NCOP" + str(number).zfill(2) for number in range(1, 16)]
                          + ["NCED" + str(number).zfill(2) for number in range(1, 8)],
                 "Others": ["EDMV", "Preview01", "Preview01v2"]
                           + ["SP" + str(number).zfill(2) for number in range(1, 14)]
                           + ["Trailer" + str(number) for number in range(1, 4)]}
        expected = {"sp/" + name + ".mkv": "Gun x Sword_DVDRip_" + category + "/" + name + ".mkv"
                    for category, stems in names.items() for name in stems}
        source = files(*expected)
        original = deepcopy(source)
        self.assertEqual(len(source), 48)
        for strategy in DVD_STRATEGIES:
            with self.subTest(strategy=strategy):
                with patch("builtins.open", side_effect=AssertionError("publisher planning must not access real files")):
                    result = propose_layout("Gun x Sword_DVDRip", source, strategy)
                self.assertEqual(source, original)
                self.assertEqual(destinations(result), expected)
                self.assertEqual({category: sum(row["category"] == category for row in result["files"])
                                  for category in names}, {"CM": 7, "OP+ED": 22, "Others": 19})
                self.assertEqual(result["issues"], [])

    def test_singular_sp_does_not_unpack_inside_independent_resource_directories(self):
        expected = {"Unknown/sp/Menu01.mkv": "Work_DVDRip_Others/Unknown/sp/Menu01.mkv",
                    "sp/Set/sp/Menu02.mkv": "Work_DVDRip_Others/Set/sp/Menu02.mkv",
                    "sp/Set/cover.jpg": "Work_DVDRip_Others/Set/cover.jpg"}
        for strategy in DVD_STRATEGIES:
            with self.subTest(strategy=strategy):
                result = propose_layout("Work_DVDRip", files(*expected), strategy)
                self.assertEqual(destinations(result), expected)
                self.assertTrue(all(row["category"] == "Others" for row in result["files"]))
                self.assertEqual(result["issues"], [])

    def test_singular_sp_explicit_targets_keep_requested_paths(self):
        targets = {"sp/Menu01.mkv": "_Menu/sp/Manual/Menu01.mkv",
                   "sp/Preview01v2.mkv": "Custom/Preview01v2.mkv",
                   "SP/Unknown/cover.jpg": "Booklet/SP/cover.jpg"}
        for strategy in DVD_STRATEGIES:
            with self.subTest(strategy=strategy):
                result = propose_layout("Work_DVDRip", files(*targets), strategy, {"targets": targets})
                self.assertEqual(destinations(result), targets)
                self.assertEqual(result["issues"], [])

    def test_singular_sp_target_collisions_remain_blocking_and_can_be_overridden(self):
        paths = ["sp/Menu01.mkv", "menu01.MKV"]
        for strategy in DVD_STRATEGIES:
            with self.subTest(strategy=strategy):
                result = propose_layout("Work_DVDRip", files(*paths), strategy)
                self.assertEqual(len(result["files"]), 2)
                self.assertEqual(len({row["target_rel"].casefold() for row in result["files"]}), 1)
                self.assertTrue(any(issue["code"] == "target-conflict" and issue["blocking"] for issue in result["issues"]))
                override = {paths[1]: "_Menu/manual/Menu02.mkv"}
                fixed = propose_layout("Work_DVDRip", files(*paths), strategy, {"targets": override})
                self.assertFalse(any(issue["code"] == "target-conflict" for issue in fixed["issues"]))
                self.assertEqual(destinations(fixed)[paths[1]], override[paths[1]])

    def test_singular_sp_generated_dvdrip_layout_is_idempotent(self):
        paths = ["sp/Menu01.mkv", "sp/Menu/01.mkv", "sp/CM01.mkv", "sp/NCOP01.mkv",
                 "sp/Preview01.mkv", "sp/Preview01v2.mkv", "sp/Trailer1.mkv",
                 "sp/Unknown/cover.jpg", "sp/Unknown/01.mkv"]
        for strategy in DVD_STRATEGIES:
            with self.subTest(strategy=strategy):
                result = propose_layout("Work_DVDRip", files(*paths), strategy)
                self.assertEqual(result["issues"], [])
                for _ in range(2):
                    previous = [row["target_rel"] for row in result["files"]]
                    result = propose_layout(result["release_name"], files(*previous), strategy)
                    self.assertEqual(destinations(result), {path: path for path in previous})
                    self.assertEqual(result["issues"], [])

    def test_publisher_detection_is_a_shared_overridable_rule(self):
        class CustomOrganizer(GenericOrganizer):
            def publisher_prefix_length(self, parts):
                if parts and parts[0] == "PublisherBundle":
                    return 1
                return super().publisher_prefix_length(parts)

        source = files("PublisherBundle/Menu/01.mkv", "PublisherBundle/Menu02.mkv")
        result = CustomOrganizer().propose("Work_BDRip", source)
        self.assertEqual(destinations(result), {
            "PublisherBundle/Menu/01.mkv": "Work_BDRip_Menu/Menu/01.mkv",
            "PublisherBundle/Menu02.mkv": "Work_BDRip_Menu/Menu02.mkv",
        })

    def test_unknown_folders_are_not_removed_and_inner_wrappers_are_not_recursively_stripped(self):
        result = propose_layout("Work_BDRip(VCBA)", files("MyAssets/Menu01.mkv", "SPs/Set/SPs/Menu02.mkv"))
        self.assertEqual(destinations(result), {
            "MyAssets/Menu01.mkv": "Work_BDRip_Others/MyAssets/Menu01.mkv",
            "SPs/Set/SPs/Menu02.mkv": "Work_BDRip_Others/Set/SPs/Menu02.mkv",
        })

    def test_resolution_release_with_publisher_wrapper_is_not_an_existing_flat_layout(self):
        result = propose_layout("Work_1080p", files("SPs/01.mkv"))
        self.assertEqual(destinations(result), {"SPs/01.mkv": "Work_1080p_Disc/01.mkv"})
        self.assertEqual(result["layout_assessment"]["kind"], "unorganized")

    def test_unknown_independent_folder_is_one_others_resource_not_split_by_descendant_extensions(self):
        paths = ["MyAssets/01.mkv", "MyAssets/cover.jpg", "MyAssets/CDs/Album/01.flac", "MyAssets/Menu/Menu01.mkv"]
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*paths), strategy)
                self.assertEqual(destinations(result), {path: "Work_BDRip_Others/" + path for path in paths})
                self.assertTrue(all(row["category"] == "Others" for row in result["files"]))
                self.assertEqual(result["issues"], [])

    def test_group_labels_on_wrappers_and_underscored_op_ed_aliases_remain_supported(self):
        result = propose_layout("Work_BDRip(Jsum)", files("SPs [VCB-Studio]/_NCOPs/01.mkv", "_NCEDs/02.mkv"))
        self.assertEqual(destinations(result), {
            "SPs [VCB-Studio]/_NCOPs/01.mkv": "Work_BDRip_OP+ED/01.mkv",
            "_NCEDs/02.mkv": "Work_BDRip_OP+ED/02.mkv",
        })

    def test_top_level_publisher_wrappers_are_replaced_by_new_categories(self):
        expected = {
            "SPs/Menu01.mkv": "Work_BDRip_Menu/Menu01.mkv",
            "SPs/NCOP.mkv": "Work_BDRip_OP+ED/NCOP.mkv",
            "SPs/PV.mkv": "Work_BDRip_Others/PV.mkv",
            "Scans/cover.jpg": "Work_BDRip_Image/cover.jpg",
            "CDs/Album/01.flac": "Work_BDRip_CD/Album/01.flac",
        }
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*expected), strategy)
                self.assertEqual(destinations(result), expected)
                self.assertEqual(result["issues"], [])

    def test_original_resource_subfolders_keep_their_names_and_depth(self):
        expected = {
            "SPs/MenuSet/Menu01.mkv": "Work_BDRip_Others/MenuSet/Menu01.mkv",
            "SPs/Menu/01.mkv": "Work_BDRip_Menu/Menu/01.mkv",
            "SPs/NCOPs/01.mkv": "Work_BDRip_OP+ED/NCOPs/01.mkv",
            "Scans/Vol.1/Book/001.jpg": "Work_BDRip_Image/Vol.1/Book/001.jpg",
        }
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*expected), strategy)
                self.assertEqual(destinations(result), expected)
                self.assertEqual(result["issues"], [])

    def test_album_booklets_and_auxiliaries_stay_with_the_album(self):
        paths = ["CDs/Album/01.flac", "CDs/Album/01.cue", "CDs/Album/rip.log",
                 "CDs/Album/Booklet/Vol.1/cover.jpg", "CDs/Album/Booklet/readme.txt"]
        expected = {path: "Work_BDRip_CD/" + path[len("CDs/"):] for path in paths}
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*paths), strategy)
                self.assertEqual(destinations(result), expected)
                self.assertTrue(all(row["category"] == "CD" for row in result["files"]))
                self.assertEqual(result["issues"], [])

    def test_publisher_names_are_removed_without_renaming_files(self):
        paths = ["SPs/[VCBA] Menu01.mkv", "Scans/[VCB-Studio] cover.jpg", "CDs/Album/[Jsum] track.flac"]
        result = propose_layout("Work_BDRip(VCBA)", files(*paths))
        self.assertEqual(destinations(result), {
            paths[0]: "Work_BDRip_Menu/[VCBA] Menu01.mkv",
            paths[1]: "Work_BDRip_Image/[VCB-Studio] cover.jpg",
            paths[2]: "Work_BDRip_CD/Album/[Jsum] track.flac",
        })

    def test_existing_top_level_categories_normalize_roots_but_keep_all_internal_paths(self):
        paths = ["作品_BDRip_Menu/MenuSet/01.mkv", "_Menu/keep/Menu02.mkv",
                 "作品_BDRip_Image/Scans/cover.jpg", "_CD/Album/01.flac",
                 "Music/Album/01.flac", "Booklet/Vol.1/cover.jpg"]
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*paths), strategy)
                self.assertEqual(destinations(result), {
                    paths[0]: "Work_BDRip_Menu/MenuSet/01.mkv", paths[1]: "Work_BDRip_Menu/keep/Menu02.mkv",
                    paths[2]: "Work_BDRip_Image/Scans/cover.jpg", paths[3]: "Work_BDRip_CD/Album/01.flac",
                    paths[4]: "Work_BDRip_Music/Album/01.flac", paths[5]: "Work_BDRip_Booklet/Vol.1/cover.jpg",
                })
                self.assertEqual(result["issues"], [])

    def test_single_existing_category_is_reused_without_publisher_wrapper(self):
        for root in ("作品_BDRip_Menu", "_Menu", "Menu"):
            for strategy, release in STRATEGIES:
                with self.subTest(root=root, strategy=strategy, release=release):
                    existing = root + "/Keep/Menu00.mkv"
                    result = propose_layout(release, files(existing, "SPs/Menu01.mkv"), strategy)
                    self.assertEqual(destinations(result), {existing: "Work_BDRip_Menu/Keep/Menu00.mkv", "SPs/Menu01.mkv": "Work_BDRip_Menu/Menu01.mkv"})
                    self.assertEqual(result["issues"], [])

    def test_pv_and_legacy_other_use_others_while_cm_has_its_own_category(self):
        existing = "Old_BDRip_Other/keep.txt"
        result = propose_layout("Work_BDRip(VCBA)", files(existing, "SPs/PV/01.mkv", "SPs/CM.mkv"))
        self.assertEqual(destinations(result), {
            existing: "Work_BDRip_Others/keep.txt",
            "SPs/PV/01.mkv": "Work_BDRip_Others/PV/01.mkv",
            "SPs/CM.mkv": "Work_BDRip_CM/CM.mkv",
        })
        self.assertEqual(result["issues"], [])

    def test_nested_explicit_manual_music_and_booklet_override_publisher_rules(self):
        kept = ["SPs/User_Music/Album/02.flac", "Scans/作品_BDRip_Booklet/Vol.1/cover.jpg",
                "CDs/作品_BDRip_Music/Album/01.flac", "CDs/作品_BDRip_Music/Album/Subs/01.ass"]
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*kept, "Scans/new.jpg", "CDs/NewAlbum/new.flac"), strategy)
                targets = destinations(result)
                self.assertEqual({path: targets[path] for path in kept}, {
                    kept[0]: "Work_BDRip_Music/Album/02.flac", kept[1]: "Work_BDRip_Booklet/Vol.1/cover.jpg",
                    kept[2]: "Work_BDRip_Music/Album/01.flac", kept[3]: "Work_BDRip_Music/Album/Subs/01.ass",
                })
                self.assertEqual(targets["Scans/new.jpg"], "Work_BDRip_Image/new.jpg")
                self.assertEqual(targets["CDs/NewAlbum/new.flac"], "Work_BDRip_CD/NewAlbum/new.flac")
                self.assertEqual(result["issues"], [])

    def test_explicit_identity_targets_can_keep_publisher_wrappers(self):
        paths = ["SPs/Menu01.mkv", "SPs/NCOPs/01.mkv", "Scans/Vol.1/001.jpg", "CDs/Album/Booklet/cover.jpg"]
        targets = {path: path for path in paths}
        for strategy in ("auto", "generic"):
            with self.subTest(strategy=strategy):
                result = propose_layout("Work_BDRip", files(*paths), strategy, {"targets": targets})
                self.assertEqual(destinations(result), targets)
                self.assertEqual(result["issues"], [])

    def test_explicit_targets_win_even_when_they_retain_publisher_names(self):
        targets = {"SPs/Menu01.mkv": "_Menu/SPs/Manual/Menu01.mkv",
                   "Scans/cover.jpg": "Booklet/Scans/cover.jpg",
                   "CDs/Album/01.flac": "Music/CDs/Album/01.flac"}
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*targets), strategy, {"targets": targets})
                self.assertEqual(destinations(result), targets)
                self.assertEqual(result["issues"], [])

    def test_removing_a_wrapper_still_detects_case_insensitive_target_collisions(self):
        paths = ["SPs/Menu01.mkv", "menu01.MKV"]
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*paths), strategy)
                self.assertEqual(len(result["files"]), 2)
                self.assertEqual(len({row["target_rel"].casefold() for row in result["files"]}), 1)
                self.assertTrue(any(issue["code"] == "target-conflict" and issue["blocking"] for issue in result["issues"]))
                fixed = propose_layout(release, files(*paths), strategy, {"targets": {paths[1]: "_Menu/manual/Menu02.mkv"}})
                self.assertFalse(any(issue["code"] == "target-conflict" for issue in fixed["issues"]))
                self.assertEqual(destinations(fixed)[paths[1]], "_Menu/manual/Menu02.mkv")

    def test_generated_layout_is_idempotent_on_second_and_third_preview(self):
        paths = ["SPs/Menu01.mkv", "SPs/Menu/01.mkv", "SPs/NCOPs/01.mkv",
                 "Scans/Vol.1/cover.jpg", "CDs/Album/01.flac", "CDs/Album/Booklet/cover.jpg"]
        for strategy, release in STRATEGIES:
            with self.subTest(strategy=strategy, release=release):
                result = propose_layout(release, files(*paths), strategy)
                for _ in range(2):
                    previous = [row["target_rel"] for row in result["files"]]
                    result = propose_layout(result["release_name"], files(*previous), strategy)
                    self.assertEqual(destinations(result), {path: path for path in previous})
                    self.assertEqual(result["issues"], [])

    def test_planning_only_uses_supplied_facts_and_does_not_mutate_them(self):
        source = files("SPs/Menu01.mkv", "Scans/cover.jpg", "CDs/Album/01.flac")
        overrides = {"release_name": "Renamed_DVDRip(VCBA)", "targets": {"Scans/cover.jpg": "Booklet/custom.jpg"}}
        before = deepcopy((source, overrides))
        with patch("builtins.open", side_effect=AssertionError("publisher planning must not perform file I/O")):
            result = propose_layout("Work_BDRip(VCBA)", source, overrides=overrides)
        self.assertEqual((source, overrides), before)
        self.assertEqual(destinations(result), {"SPs/Menu01.mkv": "Renamed_DVDRip_Menu/Menu01.mkv",
                         "Scans/cover.jpg": "Booklet/custom.jpg", "CDs/Album/01.flac": "Renamed_DVDRip_CD/Album/01.flac"})


if __name__ == "__main__":
    unittest.main()
