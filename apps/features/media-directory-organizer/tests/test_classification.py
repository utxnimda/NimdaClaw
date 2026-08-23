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
) -> ClassificationContext:
    path = Path(relative_path)
    return ClassificationContext(
        relative_path=path,
        route_relative_paths=tuple(Path(item) for item in siblings),
        work_name=work_name,
        press_format="BDRip",
        press_group=press_group,
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
            "Work Menu01.png": CATEGORY_MENU,
            "Work CM01.mkv": CATEGORY_CM,
            "Work ED2_PV.mkv": CATEGORY_PV,
            "Work Next Episode Preview.mkv": CATEGORY_PREVIEW,
            "Work [SP01].mkv": CATEGORY_SP,
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
