from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from directory_organizer.category_rules import CategoryMatch, CommonCategoryClassifier, ResourceContext
from directory_organizer.strategies import BaseOrganizer, GenericOrganizer


class CommonCategoryClassifierTests(unittest.TestCase):
    def test_default_registry_has_one_hook_per_category_and_others_last(self):
        classifier = CommonCategoryClassifier()
        self.assertEqual(classifier.category_names, ("Music", "Booklet", "CD", "Subs", "Fonts", "Menu", "CM", "OP+ED", "Image", "Disc", "Others"))
        self.assertIsInstance(classifier.category_names, tuple)
        self.assertEqual(len(set(classifier.category_names)), 11)

    def test_every_default_hook_classifies_its_resource(self):
        cases = (("music", "Music", "Music", True), ("booklet", "Booklet", "Booklet", True),
                 ("cd", "CD", "track.flac", False), ("subs", "Subs", "Subtitles.rar", False),
                 ("fonts", "Fonts", "font.ttf", False), ("menu", "Menu", "Menu01.mkv", False),
                 ("cm", "CM", "CM01.mkv", False), ("op_ed", "OP+ED", "NCOP01.mkv", False),
                 ("image", "Image", "cover.jpg", False), ("disc", "Disc", "episode.mkv", False),
                 ("others", "Others", "readme.txt", False))
        classifier = CommonCategoryClassifier()
        for hook, category, path, is_directory in cases:
            with self.subTest(category=category):
                context = ResourceContext(path, is_directory=is_directory)
                direct = getattr(classifier, "filter_" + hook)(context)
                self.assertIsInstance(direct, CategoryMatch)
                self.assertEqual(direct.category, category)
                self.assertTrue(direct.reason)
                self.assertEqual(classifier.classify_resource(context).category, category)

    def test_standard_roots_are_consumed_but_independent_resource_folders_are_kept(self):
        classifier = CommonCategoryClassifier()
        for path in ("Menu", "_Menu", "Old_BDRip_Menu"):
            with self.subTest(path=path):
                match = classifier.classify_resource(ResourceContext(path, is_directory=True))
                self.assertEqual(match.category, "Menu")
                self.assertFalse(match.keep_directory)
        nested = classifier.classify_resource(ResourceContext("Menu", is_directory=True, inside_publisher=True))
        self.assertEqual(nested.category, "Menu")
        self.assertTrue(nested.keep_directory)

    def test_publisher_wrappers_and_legacy_other_roots_do_not_become_extra_layers(self):
        classifier = CommonCategoryClassifier()
        for path, category in (("CDs", "CD"), ("Scans", "Image"), ("Pictures", "Image"), ("Other", "Others"), ("Others", "Others"), ("Old_BDRip_Other", "Others")):
            for inside in (False, True):
                with self.subTest(path=path, inside_publisher=inside):
                    match = classifier.classify_resource(ResourceContext(path, is_directory=True, inside_publisher=inside))
                    self.assertEqual(match.category, category)
                    self.assertFalse(match.keep_directory)

    def test_music_and_booklet_protect_contents_without_matching_loose_files(self):
        classifier = CommonCategoryClassifier()
        for category in ("Music", "Booklet"):
            for name in (category, "_" + category, "Old_BDRip_" + category):
                with self.subTest(name=name):
                    match = classifier.classify_resource(ResourceContext(name, is_directory=True))
                    self.assertEqual(match.category, category)
                    self.assertTrue(match.protect_contents)
                    self.assertFalse(match.keep_directory)
        self.assertIsNone(classifier.filter_music(ResourceContext("Music.flac")))
        self.assertIsNone(classifier.filter_booklet(ResourceContext("Booklet.jpg")))

    def test_explicit_standard_category_suffix_wins_over_keywords_in_the_work_title(self):
        classifier = CommonCategoryClassifier()
        for path, category in (("OST_BDRip_Menu", "Menu"), ("Album_BDRip_Disc", "Disc"),
                               ("[EAC] Work_BDRip_Image", "Image"), ("Soundtrack_BDRip_Booklet", "Booklet")):
            with self.subTest(path=path):
                match = classifier.classify_resource(ResourceContext(path, is_directory=True))
                self.assertEqual(match.category, category)
                self.assertFalse(match.keep_directory)
                self.assertEqual(match.protect_contents, category == "Booklet")
        self.assertEqual(classifier.classify_resource(ResourceContext("Album", is_directory=True)).category, "CD")

    def test_custom_formal_category_uses_longest_registered_suffix(self):
        classifier = CommonCategoryClassifier()
        classifier.register_category("Extra", lambda context: None)
        classifier.register_category("Foo_Extra", lambda context: None)
        match = classifier.classify_resource(ResourceContext("Work_BDRip_Foo_Extra", is_directory=True))
        self.assertEqual(match.category, "Foo_Extra")
        self.assertFalse(match.keep_directory)

    def test_unknown_and_unsupported_independent_folders_are_whole_others(self):
        classifier = CommonCategoryClassifier()
        for name in ("Unknown Package", "PV", "Interview", "Old_BDRip_PV", "Extras"):
            with self.subTest(name=name):
                match = classifier.classify_resource(ResourceContext(name, is_directory=True))
                self.assertEqual(match.category, "Others")
                self.assertTrue(match.keep_directory)

    def test_cm_has_its_own_rule_and_does_not_consume_unrelated_video(self):
        classifier = CommonCategoryClassifier()
        for path, directory in (("CM", True), ("Work_BDRip_CM", True), ("CM01.mkv", False), ("CM01.ass", False)):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_resource(ResourceContext(path, is_directory=directory)).category, "CM")
        self.assertEqual(classifier.classify_resource(ResourceContext("PV01.mkv")).category, "Others")
        self.assertEqual(classifier.classify_resource(ResourceContext("episode.mkv")).category, "Disc")

    def test_filename_priority_distinguishes_subtitle_archives_fonts_and_audio(self):
        classifier = CommonCategoryClassifier()
        cases = {"Subtitles_Font_Changed.rar": "Subs", "Fonts.7z": "Fonts", "Menu01.bmp": "Menu",
                 "NCOP01.flac": "CD", "NCED01.ass": "OP+ED", "01.mka": "CD"}
        for path, category in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, category)

    def test_roman_numeral_in_work_title_does_not_turn_episodes_into_bonus_resources(self):
        classifier = CommonCategoryClassifier()
        for path in ("Overlord IV - 01.mkv", "Overlord IV - 01.ass"):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, "Disc")

    def test_numbered_preview_trailer_teaser_and_commercial_resources_stay_others(self):
        classifier = CommonCategoryClassifier()
        stems = ("Preview01", "Preview01v2", "Trailer1", "Trailer03v2", "Teaser01",
                 "Teaser1v2", "Commercial2", "Commercial02v2", "EDMV", "SP01")
        for stem in stems:
            for extension in ("mkv", "ass"):
                for filename in (stem + "." + extension,
                                 "[Gun x Sword][" + stem + "][DVDRIP]." + extension):
                    with self.subTest(filename=filename):
                        self.assertEqual(classifier.classify_resource(ResourceContext(filename)).category, "Others")

    def test_logo_and_producer_logo_metadata_marks_video_and_subtitles_as_others(self):
        classifier = CommonCategoryClassifier()
        for label in ("Logo", "Producer Logo"):
            for extension in ("mkv", "ass"):
                path = "[Gun x Sword][" + label + "][BDRIP][1080P][H264_FLAC]." + extension
                with self.subTest(path=path):
                    self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, "Others")

    def test_logo_labels_accept_case_separator_and_bracket_variations(self):
        classifier = CommonCategoryClassifier()
        stems = ("[Gun x Sword][lOgO][BDRIP]", "[Gun x Sword][pRoDuCeR_lOgO][BDRIP]",
                 "[Gun x Sword][Producer-Logo][BDRIP]", "[Gun x Sword][Producer  Logo][BDRIP]",
                 "【Gun x Sword】【Logo】【BDRIP】", "(Gun x Sword)(Producer Logo)(BDRIP)",
                 "[VCB-Studio][Gun x Sword][Logo][BDRIP]", "[Gun x Sword][Logo][01][BDRIP]")
        for stem in stems:
            for extension in ("MKV", "ASS"):
                path = stem + "." + extension
                with self.subTest(path=path):
                    self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, "Others")

    def test_bare_logo_resource_names_are_others(self):
        classifier = CommonCategoryClassifier()
        for stem in ("Logo", "Producer Logo", "producer_logo", "PRODUCER-LOGO", "[VCB-Studio]Logo"):
            for extension in ("mkv", "ass"):
                path = stem + "." + extension
                with self.subTest(path=path):
                    self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, "Others")

    def test_similar_work_titles_are_not_mistaken_for_logo_resource_labels(self):
        classifier = CommonCategoryClassifier()
        stems = ("Log Horizon - 01", "Logos - 01", "[Logo Adventure][01][BDRIP]",
                 "[VCB-Studio][Logo][01][BDRIP]", "[JSUM][Logo][E01][BDRIP]",
                 "[Logo][Episode 01][BDRIP]")
        for stem in stems:
            for extension in ("mkv", "ass"):
                path = stem + "." + extension
                with self.subTest(path=path):
                    self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, "Disc")

    def test_loose_logo_resources_plan_into_others_without_creating_disc_moves(self):
        paths = ["[Gun x Sword][" + label + "][BDRIP][1080P][H264_FLAC]." + extension
                 for label in ("Logo", "Producer Logo") for extension in ("mkv", "ass")]
        source = [{"relative_path": path, "size": 1024} for path in paths]
        before = deepcopy(source)
        with patch("builtins.open", side_effect=AssertionError("Logo planning must not access real files")):
            result = GenericOrganizer().propose("Gun x Sword_BDRip", source)
        self.assertEqual(source, before)
        self.assertEqual({row["source_rel"]: row["target_rel"] for row in result["files"]},
                         {path: "Gun x Sword_BDRip_Others/" + path for path in paths})
        self.assertTrue(all(row["category"] == "Others" for row in result["files"]))
        self.assertEqual(result["issues"], [])

    def test_existing_logo_resources_in_others_stay_there_on_repeated_preview(self):
        paths = ["Gun x Sword_BDRip_Others/[Gun x Sword][" + label + "][BDRIP][1080P][H264_FLAC]." + extension
                 for label in ("Logo", "Producer Logo") for extension in ("mkv", "ass")]
        for _ in range(2):
            with patch("builtins.open", side_effect=AssertionError("Logo planning must not access real files")):
                result = GenericOrganizer().propose("Gun x Sword_BDRip", [{"relative_path": path, "size": 1024} for path in paths])
            self.assertEqual({row["source_rel"]: row["target_rel"] for row in result["files"]}, {path: path for path in paths})
            self.assertTrue(all(row["category"] == "Others" for row in result["files"]))
            self.assertEqual(result["issues"], [])
            paths = [row["target_rel"] for row in result["files"]]

    def test_explicit_episode_number_keeps_regular_and_logo_named_works_in_disc(self):
        classifier = CommonCategoryClassifier()
        for release, stem in (("Gun x Sword_BDRip", "[Gun x Sword][01][BDRIP][1080P][H264_FLAC]"),
                              ("Logo_BDRip", "[Logo][01][BDRIP]")):
            with self.subTest(release=release):
                paths = [stem + ".mkv", stem + ".ass"]
                for path in paths:
                    self.assertEqual(classifier.classify_resource(ResourceContext(path)).category, "Disc")
                result = GenericOrganizer().propose(release, [{"relative_path": path} for path in paths])
                self.assertTrue(all(row["category"] == "Disc" and row["target_rel"].startswith(release + "_Disc/") for row in result["files"]))
                self.assertEqual(result["issues"], [])

    def test_ordered_registry_stops_at_first_match(self):
        calls = []
        classifier = CommonCategoryClassifier()
        classifier.register_category("First", lambda context: (calls.append("first"), CategoryMatch("First", "first match"))[1], before="Music")
        classifier.register_category("Second", lambda context: (calls.append("second"), CategoryMatch("Second", "must not run"))[1], before="Others")
        self.assertEqual(classifier.classify_resource(ResourceContext("anything.bin")).category, "First")
        self.assertEqual(calls, ["first"])
        self.assertEqual(classifier.category_names[-1], "Others")

    def test_subclass_hook_override_is_used_by_the_default_registry(self):
        class Override(CommonCategoryClassifier):
            def filter_image(self, context):
                if context.relative_path.endswith(".customimage"):
                    return CategoryMatch("Image", "subclass image", keep_directory=False)
                return super().filter_image(context)

        classifier = Override()
        self.assertEqual(classifier.classify_resource(ResourceContext("cover.customimage")).category, "Image")
        self.assertEqual(classifier.classify_resource(ResourceContext("cover.jpg")).category, "Image")

    def test_registration_can_replace_a_rule_without_duplicating_its_category(self):
        classifier = CommonCategoryClassifier()
        classifier.register_category("Image", lambda context: CategoryMatch("Image", "replacement") if context.relative_path == "custom.bin" else None)
        self.assertEqual(classifier.category_names.count("Image"), 1)
        self.assertEqual(classifier.category_names[-1], "Others")
        self.assertEqual(classifier.classify_resource(ResourceContext("custom.bin")).category, "Image")

    def test_registration_rejects_unsafe_names_unknown_anchors_and_mismatched_results(self):
        classifier = CommonCategoryClassifier()
        for name in ("", "../Extra", "Extra/Sub", "Extra:Data", "CON", "Extra."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    classifier.register_category(name, lambda context: None)
        with self.assertRaises(ValueError):
            classifier.register_category("Extra", lambda context: None, before="Missing")
        classifier.register_category("Extra", lambda context: CategoryMatch("Image", "wrong category"))
        with self.assertRaises(ValueError):
            classifier.classify_resource(ResourceContext("anything.bin"))

    def test_unmatched_directory_stays_whole_even_with_nested_known_types(self):
        paths = ["Unknown/Old_Music/01.flac", "Unknown/Old_Booklet/cover.jpg", "SPs/Unknown/_Disc/01.mkv"]
        result = GenericOrganizer().propose("Work_BDRip", [{"relative_path": path} for path in paths])
        self.assertEqual({row["source_rel"]: row["target_rel"] for row in result["files"]}, {
            paths[0]: "Work_BDRip_Others/Unknown/Old_Music/01.flac",
            paths[1]: "Work_BDRip_Others/Unknown/Old_Booklet/cover.jpg",
            paths[2]: "Work_BDRip_Others/Unknown/_Disc/01.mkv",
        })
        self.assertTrue(all(row["category"] == "Others" for row in result["files"]))

    def test_new_cm_rule_can_claim_loose_resources_from_old_other_fallback(self):
        paths = ["Old_BDRip_Other/CM01.mkv", "_Others/PV01.mkv", "Work_BDRip_Others/Unknown/CM02.mkv"]
        result = GenericOrganizer().propose("Work_BDRip", [{"relative_path": path} for path in paths])
        self.assertEqual({row["source_rel"]: row["target_rel"] for row in result["files"]}, {
            paths[0]: "Work_BDRip_CM/CM01.mkv",
            paths[1]: "Work_BDRip_Others/PV01.mkv",
            paths[2]: "Work_BDRip_Others/Unknown/CM02.mkv",
        })

    def test_registered_custom_type_reaches_the_real_organizer_category_root(self):
        class ExtraOrganizer(GenericOrganizer):
            def __init__(self):
                super().__init__()
                self.register_category("Extra", self.filter_extra, before="Others")

            def filter_extra(self, context):
                if not context.is_directory and context.relative_path.endswith(".extra"):
                    return CategoryMatch("Extra", "custom extra resource", keep_directory=False)
                return None

        self.assertTrue(issubclass(BaseOrganizer, CommonCategoryClassifier))
        organizer = ExtraOrganizer()
        source = [{"relative_path": "bonus.extra", "size": 1024}]
        before = deepcopy(source)
        with patch("builtins.open", side_effect=AssertionError("classification must not access the filesystem")):
            result = organizer.propose("Work_BDRip", source)
        self.assertEqual(source, before)
        self.assertEqual(result["files"][0]["category"], "Extra")
        self.assertEqual(result["files"][0]["target_rel"], "Work_BDRip_Extra/bonus.extra")
        self.assertEqual(result["issues"], [])
        second = organizer.propose("Work_BDRip", [{"relative_path": result["files"][0]["target_rel"], "size": 1024}])
        self.assertEqual(second["files"][0]["target_rel"], "Work_BDRip_Extra/bonus.extra")
        self.assertEqual(second["files"][0]["category"], "Extra")
        for root in ("Work_BDRip_Extra", "_Extra"):
            match = organizer.classify_resource(ResourceContext(root, is_directory=True))
            self.assertEqual(match.category, "Extra")
            self.assertFalse(match.keep_directory)
        self.assertEqual(organizer.classify_resource(ResourceContext("Extra", is_directory=True)).category, "Others")


if __name__ == "__main__":
    unittest.main()
