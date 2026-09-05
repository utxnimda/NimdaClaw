from __future__ import annotations

import unittest
from pathlib import Path

from media_directory_organizer.catalog import PressRecord
from media_directory_organizer.classification import (
    CATEGORY_CD,
    CATEGORY_CM,
    CATEGORY_DISC,
    CATEGORY_FONTS,
    CATEGORY_IMAGE,
    CATEGORY_LIVE,
    CATEGORY_MENU,
    CATEGORY_MV,
    CATEGORY_OP_ED,
    CATEGORY_OTHERS,
    CATEGORY_PREVIEW,
    CATEGORY_PV,
    CATEGORY_SP,
    CATEGORY_SUBS,
    ClassificationContext,
    DEFAULT_CLASSIFIER_REGISTRY,
    FolderFilterResult,
    GenericClassifier,
    JsumClassifier,
    VcbClassifier,
    disc_version_subdirectories,
    is_resolution_press_format,
)
from media_directory_organizer.service import (
    _category_stem,
    _groups_matching_explicit,
)
from media_directory_organizer.settings import OrganizerSettings


def _context(
    relative_path: str | Path,
    *,
    siblings: tuple[str | Path, ...] = (),
    press_group: str = "----",
    work_name: str = "Work",
    release_type: str = "tv",
    press_format: str = "BDRip",
) -> ClassificationContext:
    path = Path(relative_path)
    return ClassificationContext(
        relative_path=path,
        route_relative_paths=tuple(Path(item) for item in siblings),
        work_name=work_name,
        press_format=press_format,
        press_group=press_group,
        release_type=release_type,
        source_dir_name="Source",
        target_dir_name="Work_BDRip(Group)",
    )


class ClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.generic = GenericClassifier()

    def test_every_category_is_an_overridable_folder_filter(self) -> None:
        expected_methods = {
            "filter_disc",
            "filter_cd",
            "filter_op_ed",
            "filter_image",
            "filter_menu",
            "filter_cm",
            "filter_pv",
            "filter_preview",
            "filter_sp",
            "filter_fonts",
            "filter_subs",
            "filter_mv",
            "filter_live",
            "filter_others",
        }
        self.assertTrue(issubclass(JsumClassifier, GenericClassifier))
        self.assertTrue(issubclass(VcbClassifier, GenericClassifier))
        self.assertTrue(all(callable(getattr(self.generic, name)) for name in expected_methods))

    def test_one_virtual_filter_can_be_overridden_for_one_work(self) -> None:
        class SpecialJsum(JsumClassifier):
            def filter_cd(self, context: ClassificationContext) -> FolderFilterResult | None:
                if context.work_name == "Special Work" and context.relative_path.suffix == ".bin":
                    return self._result(context, "cd-special-work", "单作品自定义 CD 规则")
                return super().filter_cd(context)

        classifier = SpecialJsum()
        special = classifier.classify(
            _context("Custom Assets/bundle.bin", work_name="Special Work", press_group="JSUM")
        )
        ordinary = classifier.classify(
            _context("Custom Assets/bundle.bin", work_name="Ordinary Work", press_group="JSUM")
        )

        self.assertEqual(special.category, CATEGORY_CD)
        self.assertEqual(special.rule_id, "jsum-cd-special-work")
        self.assertEqual(ordinary.category, CATEGORY_OTHERS)

    def test_generic_common_category_matrix(self) -> None:
        cases = {
            "Work - 01.mkv": CATEGORY_DISC,
            "Work OST.flac": CATEGORY_CD,
            "[EAC] Work OP Song (wav+png).rar": CATEGORY_CD,
            "Work オリジナルサウンドトラック (wav+jpg).rar": CATEGORY_CD,
            "Work NCOP01.mkv": CATEGORY_OP_ED,
            "Work NCOP01v2.mkv": CATEGORY_OP_ED,
            "Work Menu01.png": CATEGORY_MENU,
            "Work CM01.mkv": CATEGORY_CM,
            "Work CM01v2.mkv": CATEGORY_CM,
            "Work ED2_PV.mkv": CATEGORY_PV,
            "Work PV01v2.mkv": CATEGORY_PV,
            "Work Next Episode Preview.mkv": CATEGORY_PREVIEW,
            "Work [SP01].mkv": CATEGORY_SP,
            "Work SP01v2.mkv": CATEGORY_SP,
            "Work OVA01v2.mkv": CATEGORY_SP,
            "Work CD01v2.mkv": CATEGORY_CD,
            "Work [Tokuten BD][BDMV].rar": CATEGORY_SP,
            "Work Promotion Movie.mkv": CATEGORY_PV,
            "Work [Booklet].rar": CATEGORY_IMAGE,
            "Work font.ttf": CATEGORY_FONTS,
            "Work [Subtitles].zip": CATEGORY_SUBS,
            "Work [MV01].mkv": CATEGORY_MV,
            "Work [LIVE].mkv": CATEGORY_LIVE,
            "Work [Logo].mkv": CATEGORY_OTHERS,
            "unknown.bin": CATEGORY_OTHERS,
        }
        for relative_path, expected in cases.items():
            with self.subTest(relative_path=relative_path):
                decision = self.generic.classify(_context(relative_path))
                self.assertEqual(decision.category, expected)
                self.assertEqual(decision.relative_path.name, Path(relative_path).name)

    def test_explicit_source_folder_outranks_extensions_and_preserves_inner_tree(self) -> None:
        cases = (
            (Path("CDs/Disc 1/cover.jpg"), CATEGORY_CD, Path("Disc 1/cover.jpg")),
            (Path("SPs/Menu/Menu01.png"), CATEGORY_MENU, Path("Menu01.png")),
            (Path("BD Scans/Booklet/001.jpg"), CATEGORY_IMAGE, Path("Booklet/001.jpg")),
            (Path("Disc/episode.ass"), CATEGORY_DISC, Path("episode.ass")),
            (Path("Work_BDRip_Other/Logo.mkv"), CATEGORY_OTHERS, Path("Logo.mkv")),
            (Path("Work_BDRip_Others/Info.txt"), CATEGORY_OTHERS, Path("Info.txt")),
        )
        for source, category, inner in cases:
            with self.subTest(source=source):
                decision = self.generic.classify(_context(source))
                self.assertEqual(decision.category, category)
                self.assertEqual(decision.relative_path, inner)

    def test_ova_and_oad_labels_are_specials_only_outside_their_own_release_type(self) -> None:
        for release_type in ("tv", "movie", ""):
            with self.subTest(release_type=release_type):
                self.assertEqual(
                    self.generic.classify(
                        _context("Work OVA01.mkv", release_type=release_type)
                    ).category,
                    CATEGORY_SP,
                )
                self.assertEqual(
                    self.generic.classify(
                        _context("OAD/Work 01.mkv", release_type=release_type)
                    ).category,
                    CATEGORY_SP,
                )

        self.assertEqual(
            self.generic.classify(
                _context("Work OVA01.mkv", release_type="ova")
            ).category,
            CATEGORY_DISC,
        )
        self.assertEqual(
            self.generic.classify(
                _context("OVA/Work 01.mkv", release_type="ova")
            ).category,
            CATEGORY_DISC,
        )
        self.assertEqual(
            self.generic.classify(
                _context("Work OAD01.mkv", release_type="oad")
            ).category,
            CATEGORY_DISC,
        )

    def test_route_siblings_keep_sidecars_with_disc(self) -> None:
        paths = (
            Path("Episode 01.mkv"),
            Path("Episode 01.sc.ass"),
            Path("Episode 01.mka"),
            Path("Episode 01.flac"),
        )
        for source in paths:
            with self.subTest(source=source):
                decision = self.generic.classify(_context(source, siblings=paths))
                self.assertEqual(decision.category, CATEGORY_DISC)

        standalone_sub = self.generic.classify(_context("Episode 02.ass"))
        standalone_audio = self.generic.classify(_context("Track 01.flac"))
        self.assertEqual(standalone_sub.category, CATEGORY_SUBS)
        self.assertEqual(standalone_audio.category, CATEGORY_CD)

    def test_disc_multi_version_episode_directory_keeps_sidecars_with_versions(self) -> None:
        paths = (
            Path("Main/Work - 01 [TV][1080p].mkv"),
            Path("Main/Work - 01 [TV][1080p].sc.ass"),
            Path("Main/Work - 01 [OA][720p].mkv"),
            Path("Main/Work - 01 [OA][720p].mkv.sha256"),
            Path("Main/Work - 02 [TV][1080p].mkv"),
            Path("Main/Work SP01.mkv"),
            Path("Main/Work NCOP01.mkv"),
            Path("Main/Work CD01.mkv"),
        )
        decisions = {
            str(path): self.generic.classify(_context(path, siblings=paths))
            for path in paths
        }

        self.assertEqual(decisions[str(paths[1])].category, CATEGORY_DISC)
        self.assertEqual(decisions[str(paths[3])].category, CATEGORY_DISC)
        self.assertEqual(decisions[str(paths[5])].category, CATEGORY_SP)
        self.assertEqual(decisions[str(paths[6])].category, CATEGORY_OP_ED)
        self.assertEqual(decisions[str(paths[7])].category, CATEGORY_CD)

        planned = disc_version_subdirectories(
            (
                (str(index), "release-a", path, decisions[str(path)])
                for index, path in enumerate(paths)
            ),
            directory_stem="Work_BDRip",
        )

        expected_directory = Path("Work_BDRip_01")
        self.assertEqual(
            planned,
            {
                "0": expected_directory,
                "1": expected_directory,
                "2": expected_directory,
                "3": expected_directory,
            },
        )

    def test_disc_single_video_with_matching_subtitles_gets_episode_directory(self) -> None:
        paths = (
            Path(
                "[Nekomoe kissaten&VCB-Studio] Kekkai Sensen & Beyond "
                "[01][Ma10p_1080p][x265_flac_aac].mkv"
            ),
            Path(
                "[Nekomoe kissaten&VCB-Studio] Kekkai Sensen & Beyond "
                "[01][Ma10p_1080p][x265_flac_aac].sc.ass"
            ),
            Path(
                "[Nekomoe kissaten&VCB-Studio] Kekkai Sensen & Beyond "
                "[01][Ma10p_1080p][x265_flac_aac].tc.ass"
            ),
            Path(
                "[Nekomoe kissaten&VCB-Studio] Kekkai Sensen & Beyond "
                "[02][Ma10p_1080p][x265_flac_aac].mkv"
            ),
        )
        vcb = VcbClassifier()
        decisions = {
            str(path): vcb.classify(_context(path, siblings=paths))
            for path in paths
        }

        planned = disc_version_subdirectories(
            (
                (str(index), "vcbm-release", path, decisions[str(path)])
                for index, path in enumerate(paths)
            ),
            directory_stem="血界戦線 & Beyond_BDRip",
            release_type="tv",
        )

        expected_directory = Path("血界戦線 & Beyond_BDRip_01")
        self.assertEqual(
            planned,
            {
                "0": expected_directory,
                "1": expected_directory,
                "2": expected_directory,
            },
        )

    def test_resolution_release_single_video_and_subtitles_stay_flat(self) -> None:
        paths = (
            Path(
                "Overlord IV_1080p_01/"
                "[Sakurato] Overlord IV [01][HEVC-10bit 1080P@60FPS AAC][CHS&CHT].mkv"
            ),
            Path(
                "Overlord IV_1080p_01/"
                "[Sakurato] Overlord IV [01][HEVC-10bit 1080P AAC][CHS&CHT]_Subtitles03.ass"
            ),
            Path(
                "Overlord IV_1080p_01/"
                "[Sakurato] Overlord IV [01][HEVC-10bit 1080P AAC][CHS&CHT]_Subtitles04.ass"
            ),
        )
        decisions = {
            str(path): self.generic.classify(
                _context(
                    path,
                    siblings=paths,
                    work_name="Overlord IV",
                    press_format="1080p",
                )
            )
            for path in paths
        }

        self.assertTrue(is_resolution_press_format("1080p"))
        self.assertTrue(is_resolution_press_format("1920x1080"))
        self.assertFalse(is_resolution_press_format("BDRip"))
        self.assertEqual(
            {decision.category for decision in decisions.values()},
            {CATEGORY_DISC},
        )
        self.assertEqual(
            disc_version_subdirectories(
                (
                    (str(index), "sakurato-release", path, decisions[str(path)])
                    for index, path in enumerate(paths)
                ),
                directory_stem="Overlord IV_1080p",
                release_type="tv",
                press_format="1080p",
            ),
            {},
        )

    def test_resolution_release_real_video_versions_still_get_episode_directory(self) -> None:
        paths = (
            Path("Work [01][TV][1080p].mkv"),
            Path("Work [01][OA][720p].mkv"),
            Path("Work [01][TV][1080p].sc.ass"),
        )
        decisions = {
            str(path): self.generic.classify(
                _context(path, siblings=paths, press_format="1080p")
            )
            for path in paths
        }

        self.assertEqual(
            disc_version_subdirectories(
                (
                    (str(index), "resolution-release", path, decisions[str(path)])
                    for index, path in enumerate(paths)
                ),
                directory_stem="Work_1080p",
                release_type="tv",
                press_format="1080p",
            ),
            {
                "0": Path("Work_1080p_01"),
                "1": Path("Work_1080p_01"),
                "2": Path("Work_1080p_01"),
            },
        )

    def test_disc_single_video_with_matching_audio_gets_episode_directory(self) -> None:
        paths = (Path("Work - 02.mkv"), Path("Work - 02.flac"))
        decisions = [self.generic.classify(_context(path, siblings=paths)) for path in paths]

        planned = disc_version_subdirectories(
            (
                (str(index), "release-a", path, decision)
                for index, (path, decision) in enumerate(zip(paths, decisions))
            ),
            directory_stem="Work_BDRip",
        )

        self.assertEqual(
            planned,
            {
                "0": Path("Work_BDRip_02"),
                "1": Path("Work_BDRip_02"),
            },
        )

    def test_disc_sidecar_from_another_source_scope_does_not_trigger_bundle(self) -> None:
        paths = (Path("Work - 01.mkv"), Path("Work - 01.sc.ass"))
        decisions = [self.generic.classify(_context(path, siblings=paths)) for path in paths]

        planned = disc_version_subdirectories(
            (
                ("video", "release-a", paths[0], decisions[0]),
                ("subtitle", "release-b", paths[1], decisions[1]),
            ),
            directory_stem="Work_BDRip",
        )

        self.assertEqual(planned, {})

    def test_disc_single_version_and_unrelated_same_number_stay_flat(self) -> None:
        paths = (
            Path("Work - 01.mkv"),
            Path("Work - 02 [TV].mkv"),
            Path("Recap - 02 [OA].mkv"),
        )
        decisions = [self.generic.classify(_context(path, siblings=paths)) for path in paths]

        planned = disc_version_subdirectories(
            (
                (str(index), "release-a", path, decision)
                for index, (path, decision) in enumerate(zip(paths, decisions))
            ),
            directory_stem="Work_BDRip",
        )

        self.assertEqual(planned, {})

    def test_real_world_version_labels_share_stable_episode_key(self) -> None:
        frontier = (
            Path("[VCB] Macross Frontier [01][1080p].mkv"),
            Path("[VCB] Macross Frontier [01 (Deculture Ver.)][BD].mkv"),
            Path("[VCB] Macross Frontier [01 (Yack Deculture Ver.)][CHT].mkv"),
        )
        frontier_decisions = [
            self.generic.classify(_context(path, siblings=frontier)) for path in frontier
        ]
        frontier_plan = disc_version_subdirectories(
            (
                (str(index), "frontier", path, decision)
                for index, (path, decision) in enumerate(zip(frontier, frontier_decisions))
            ),
            directory_stem="Macross Frontier_BDRip",
            release_type="tv",
        )
        self.assertEqual(
            set(frontier_plan.values()),
            {Path("Macross Frontier_BDRip_01")},
        )
        self.assertEqual(set(frontier_plan), {"0", "1", "2"})

        delta = (
            Path("[VCB] Macross Delta [0.89][1080p].mkv"),
            Path("[VCB] Macross Delta [01+][BD].mkv"),
        )
        delta_decisions = [
            self.generic.classify(_context(path, siblings=delta)) for path in delta
        ]
        delta_plan = disc_version_subdirectories(
            (
                (str(index), "delta", path, decision)
                for index, (path, decision) in enumerate(zip(delta, delta_decisions))
            ),
            directory_stem="Macross Delta_BDRip",
            release_type="tv",
        )
        self.assertEqual(
            delta_plan,
            {
                "0": Path("Macross Delta_BDRip_01"),
                "1": Path("Macross Delta_BDRip_01"),
            },
        )

        for fractional in ("0.1", "0.5", "0.99"):
            with self.subTest(fractional=fractional):
                unverified = (
                    Path(f"[VCB] Macross Delta [{fractional}][1080p].mkv"),
                    Path("[VCB] Macross Delta [01][BD].mkv"),
                )
                unverified_decisions = [
                    self.generic.classify(_context(path, siblings=unverified))
                    for path in unverified
                ]
                self.assertEqual(
                    disc_version_subdirectories(
                        (
                            (str(index), "delta", path, decision)
                            for index, (path, decision) in enumerate(
                                zip(unverified, unverified_decisions)
                            )
                        ),
                        directory_stem="Macross Delta_BDRip",
                        release_type="tv",
                    ),
                    {},
                )

    def test_empty_episode_core_cannot_bridge_conflicting_titles(self) -> None:
        paths = (
            Path("Alpha [01].mkv"),
            Path("[01v2].mkv"),
            Path("Beta [01].mkv"),
        )
        decisions = [self.generic.classify(_context(path, siblings=paths)) for path in paths]

        planned = disc_version_subdirectories(
            (
                (str(index), "release", path, decision)
                for index, (path, decision) in enumerate(zip(paths, decisions))
            ),
            directory_stem="Work_BDRip",
            release_type="tv",
        )

        self.assertEqual(planned, {})

    def test_movie_and_distinct_seasons_do_not_gain_numeric_directory(self) -> None:
        paths = (Path("Film [01].mkv"), Path("Film [01v2].mkv"))
        decisions = [self.generic.classify(_context(path, siblings=paths)) for path in paths]
        movie_plan = disc_version_subdirectories(
            (
                (str(index), "movie", path, decision)
                for index, (path, decision) in enumerate(zip(paths, decisions))
            ),
            directory_stem="Film_BDRip",
            release_type="movie",
        )
        self.assertEqual(movie_plan, {})

        seasons = (Path("Work S01E01.mkv"), Path("Work S02E01.mkv"))
        season_decisions = [
            self.generic.classify(_context(path, siblings=seasons)) for path in seasons
        ]
        season_plan = disc_version_subdirectories(
            (
                (str(index), "series", path, decision)
                for index, (path, decision) in enumerate(zip(seasons, season_decisions))
            ),
            directory_stem="Work_BDRip",
            release_type="tv",
        )
        self.assertEqual(season_plan, {})

    def test_numbered_commentary_video_is_disc_version_but_interview_and_extras_are_not(self) -> None:
        paths = (
            Path("Work - 04.mkv"),
            Path("Work - 04 commentary.mkv"),
            Path("Work - 04 commentary.sc.ass"),
            Path("Commentary.mka"),
            Path("Work - 04 interview commentary.mkv"),
            Path("Work SP04 commentary.mkv"),
            Path("Work NCOP04 commentary.mkv"),
        )
        decisions = {
            str(path): self.generic.classify(_context(path, siblings=paths))
            for path in paths
        }

        self.assertEqual(decisions[str(paths[0])].category, CATEGORY_DISC)
        self.assertEqual(decisions[str(paths[1])].category, CATEGORY_DISC)
        self.assertEqual(decisions[str(paths[2])].category, CATEGORY_DISC)
        self.assertEqual(decisions[str(paths[3])].category, CATEGORY_OTHERS)
        self.assertEqual(decisions[str(paths[4])].category, CATEGORY_OTHERS)
        self.assertEqual(decisions[str(paths[5])].category, CATEGORY_SP)
        self.assertEqual(decisions[str(paths[6])].category, CATEGORY_OP_ED)

        planned = disc_version_subdirectories(
            (
                (str(index), "release-a", path, decisions[str(path)])
                for index, path in enumerate(paths)
            ),
            directory_stem="Work_BDRip",
        )
        self.assertEqual(
            planned,
            {
                "0": Path("Work_BDRip_04"),
                "1": Path("Work_BDRip_04"),
                "2": Path("Work_BDRip_04"),
            },
        )

    def test_roman_four_in_work_title_is_not_an_interview_marker(self) -> None:
        self.assertEqual(
            self.generic.classify(_context("Overlord IV [01].mkv")).category,
            CATEGORY_DISC,
        )
        for interview_name in ("Bonus [IV].mkv", "Bonus (IV).mkv", "Bonus IV01.mkv"):
            with self.subTest(interview_name=interview_name):
                self.assertEqual(
                    self.generic.classify(_context(interview_name)).category,
                    CATEGORY_OTHERS,
                )

    def test_disc_image_bundle_stays_together_in_cd(self) -> None:
        paths = (Path("Album.ccd"), Path("Album.img"), Path("Album.sub"))
        for source in paths:
            with self.subTest(source=source):
                decision = self.generic.classify(_context(source, siblings=paths))
                self.assertEqual(decision.category, CATEGORY_CD)

    def test_jsum_and_vcb_specific_rules(self) -> None:
        jsum = JsumClassifier()
        vcb = VcbClassifier()

        self.assertEqual(jsum.classify(_context("[Logo].mkv", press_group="JSUM")).category, CATEGORY_OTHERS)
        self.assertEqual(jsum.classify(_context("[SPCD01].rar", press_group="JSUM")).category, CATEGORY_CD)
        self.assertEqual(vcb.classify(_context("Work 13 (NC Ver.).mkv", press_group="VCB")).category, CATEGORY_OP_ED)
        self.assertEqual(
            vcb.classify(_context("bd-scans/Booklet/001.png", press_group="VCBP")).relative_path,
            Path("Booklet/001.png"),
        )

    def test_every_vcb_family_code_uses_one_classifier(self) -> None:
        codes = (
            "VCB",
            "VCBA",
            "VCBB",
            "VCBC",
            "VCBD",
            "VCBE",
            "VCBF",
            "VCBJ",
            "VCBK",
            "VCBL",
            "VCBM",
            "VCBO",
            "VCBP",
            "VCBS",
            "VCBT",
            "VCBV",
            "VCBX",
            "VCBY",
            "VCBZ",
            "MAFV",
            "VA",
            "VAF",
            "VAL",
            "VBD",
            "VKM",
            "VMK",
            "VPM",
            "VTL",
            "VCDM",
            "VCUR",
            " vcbq ",
        )
        classifiers = [DEFAULT_CLASSIFIER_REGISTRY.classifier_for(code) for code in codes]
        self.assertTrue(all(isinstance(classifier, VcbClassifier) for classifier in classifiers))
        self.assertEqual(len({id(classifier) for classifier in classifiers}), 1)
        self.assertIsNone(DEFAULT_CLASSIFIER_REGISTRY.classifier_for("MYVCB"))

    def test_vcb_source_marker_keeps_real_database_group_identity(self) -> None:
        unique = [PressRecord("BDRip", "VCBS", "Work_BDRip(VCBS)")]
        multiple = [
            PressRecord("BDRip", "VCBS", "Work_BDRip(VCBS)"),
            PressRecord("BDRip", "VCBP", "Work_BDRip(VCBP)"),
        ]
        exact_first = [PressRecord("BDRip", "VCB"), *multiple]

        self.assertEqual([row.press_group for row in _groups_matching_explicit(unique, "VCB")], ["VCBS"])
        self.assertEqual(
            {row.press_group for row in _groups_matching_explicit(multiple, "VCB")},
            {"VCBS", "VCBP"},
        )
        self.assertEqual([row.press_group for row in _groups_matching_explicit(exact_first, "VCB")], ["VCB"])

    def test_category_stem_removes_only_verified_group_suffix(self) -> None:
        settings = OrganizerSettings(
            catalog_root=Path("db"),
            allowed_resource_roots=(Path("media"),),
            format_markers={},
            group_markers={},
            group_suffixes={"VCB": "VCBM", "JSUM": "Jsum"},
        )
        self.assertEqual(
            _category_stem("Work (TV)_BDRip(VCBM)", PressRecord("BDRip", "VCB"), settings),
            "Work (TV)_BDRip",
        )
        self.assertEqual(
            _category_stem("Work_BDRip(Jsum)", PressRecord("BDRip", "JSUM"), settings),
            "Work_BDRip",
        )
        self.assertEqual(
            _category_stem("Work (TV)", PressRecord("BDRip", "VCB"), settings),
            "Work (TV)",
        )

    def test_invalid_filter_result_is_rejected(self) -> None:
        class UnsafeClassifier(GenericClassifier):
            def filter_cd(self, context: ClassificationContext) -> FolderFilterResult | None:
                return FolderFilterResult(Path("../escape.bin"), "unsafe", "unsafe")

        with self.assertRaisesRegex(ValueError, "安全的相对路径"):
            UnsafeClassifier().classify(_context("file.bin"))


if __name__ == "__main__":
    unittest.main()
