from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer.catalog import CatalogWork, MediaCatalog
from media_directory_organizer.classification import DEFAULT_CLASSIFIER_REGISTRY
from media_directory_organizer.service import apply_plan, build_plan
from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.web import (
    apply_organizer_from_ui_body,
    organizer_config_payload,
    preview_organizer_from_ui_body,
)


BASE_WORK = "Little Busters!"
REFRAIN_WORK = "Little Busters! Refrain"
EX_WORK = "Little Busters! EX"

BASE_JSUM_TARGET = "Little Busters!_BDRip(Jsum)"
BASE_MW_TARGET = "Little Busters!_BDRip(MW)"
REFRAIN_JSUM_TARGET = "Little Busters! Refrain_BDRip(Jsum)"
REFRAIN_VCB_TARGET = "Little Busters! Refrain_BDRip(VCBM)"
EX_JSUM_TARGET = "Little Busters! EX_BDRip(Jsum)"
EX_VCB_TARGET = "Little Busters! EX_BDRip(VCBM)"

CATALOG_YAML = """\
- attributes:
  - type: date
    data:
      start: '20121006'
      end: '20130406'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: Little Busters!_BDRip(Jsum)
      - press_format: BDRip
        press_group: MW
        press_path: Little Busters!_BDRip(MW)
      markers: []
      path: 'WORK_ROOT'
  - type: country
    data: japan
  - type: name
    data: Little Busters!
- attributes:
  - type: date
    data:
      start: '20131005'
      end: '20131228'
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: Little Busters! Refrain_BDRip(Jsum)
      - press_format: BDRip
        press_group: VCB
        press_path: Little Busters! Refrain_BDRip(VCBM)
      markers: []
      path: 'WORK_ROOT'
  - type: country
    data: japan
  - type: name
    data: Little Busters! Refrain
- attributes:
  - type: date
    data:
      start: '20140129'
      end: '20140730'
  - type: collection-type
    data:
      domain: animation
      release_type: ova
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: Little Busters! EX_BDRip(Jsum)
      - press_format: BDRip
        press_group: VCB
        press_path: Little Busters! EX_BDRip(VCBM)
      markers: []
      path: 'WORK_ROOT'
  - type: country
    data: japan
  - type: name
    data: Little Busters! EX
"""


EXPECTED_TARGETS = {
    (REFRAIN_WORK, "JSUM"): REFRAIN_JSUM_TARGET,
    (REFRAIN_WORK, "VCB"): REFRAIN_VCB_TARGET,
    (EX_WORK, "JSUM"): EX_JSUM_TARGET,
    (EX_WORK, "VCB"): EX_VCB_TARGET,
}


def _settings(catalog_root: Path, allowed_root: Path, work_root: Path) -> OrganizerSettings:
    return OrganizerSettings(
        catalog_root=catalog_root,
        allowed_resource_roots=(allowed_root,),
        format_markers={"BDRip": ("bdrip", "bd 1920x1080")},
        group_markers={
            "VCB": ("vcb-studio", "vcb"),
            "JSUM": ("jsum",),
            "MW": ("mawen1250",),
        },
        group_suffixes={"JSUM": "Jsum", "VCB": "VCBM", "MW": "MW"},
        max_files=100,
        default_work_root=work_root,
        work_aliases={
            REFRAIN_WORK: (
                "Little Busters! Refrain",
                "Little Busters! ～Refrain～",
                "リトルバスターズ！～Refrain～",
            ),
            EX_WORK: ("Little Busters! EX", "リトルバスターズ！EX"),
        },
    )


def _write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class MediaDirectoryOrganizerTest(unittest.TestCase):
    def test_catalog_prefix_similarity_without_exact_anchor_requires_registration(self) -> None:
        catalog = MediaCatalog(
            works=(
                CatalogWork(
                    name="Air Gear",
                    path="",
                    domain="animation",
                    country="japan",
                    release_type="tv",
                    presses=(),
                    source_file="catalog.yaml",
                ),
            ),
            catalog_root=Path("catalog"),
        )

        self.assertEqual(catalog.matching_family_for_root(Path("Air")), ())
        self.assertEqual(
            [work.name for work in catalog.matching_family_for_root(Path("Air Gear"))],
            ["Air Gear"],
        )

    def _fixture(
        self, temp: str
    ) -> tuple[
        Path,
        MediaCatalog,
        OrganizerSettings,
        dict[str, Path],
        dict[str, Path],
    ]:
        base = Path(temp)
        root = base / BASE_WORK
        root.mkdir()
        catalog_root = base / "db"
        catalog_root.mkdir()
        yaml_text = CATALOG_YAML.replace("WORK_ROOT", str(root).replace("'", "''"))
        (catalog_root / "[JP][TVInfo][test].yaml").write_text(yaml_text, encoding="utf-8")

        # Already-organized base-work targets must not be scanned as input.
        (root / BASE_JSUM_TARGET).mkdir()
        (root / BASE_MW_TARGET).mkdir()

        sources = {
            "jsum": root / "[2013-14][Little Busters! Refrain+EX][BDRip][JSUM]",
            "ex_vcb": root / "Little Busters! EX 2014 [BD 1920x1080] - VCB-Studio",
            "refrain_vcb": root
            / "Little Busters! Refrain 2013 [BD 1920x1080] - VCB-Studio",
        }

        files = {
            # One JSUM source intentionally mixes two works. The shared Logo is
            # the sole ambiguous file until an explicit per-file override is supplied.
            "jsum_ex": _write_file(
                sources["jsum"] / "EX" / "[Little Busters! EX][01].mkv", b"jsum-ex"
            ),
            "jsum_refrain": _write_file(
                sources["jsum"]
                / "Refrain"
                / "[Little Busters! Refrain][01].mkv",
                b"jsum-refrain",
            ),
            "jsum_refrain_ja": _write_file(
                sources["jsum"]
                / "Japanese"
                / "リトルバスターズ！～Refrain～ [02].mkv",
                b"jsum-refrain-ja",
            ),
            "shared_logo": _write_file(
                sources["jsum"]
                / "Shared"
                / "[Little Busters! Refrain+EX][Logo].mkv",
                b"shared-logo",
            ),
            # Both VCB sources contain a top-level main episode and nested trees.
            "ex_vcb_main": _write_file(
                sources["ex_vcb"] / "Little Busters! EX - 01.mkv", b"ex-main"
            ),
            "ex_vcb_audio": _write_file(
                sources["ex_vcb"] / "Little Busters! EX OST.flac", b"ex-audio"
            ),
            "ex_vcb_scan": _write_file(
                sources["ex_vcb"]
                / "bd-scans"
                / "Booklet"
                / "Little Busters! EX 001.png",
                b"ex-scan",
            ),
            "ex_vcb_fallback": _write_file(
                sources["ex_vcb"]
                / "Extras"
                / "Little Busters! EX font.ttf",
                b"ex-font",
            ),
            "refrain_vcb_main": _write_file(
                sources["refrain_vcb"] / "Little Busters! Refrain - 01.mkv",
                b"refrain-main",
            ),
            "refrain_vcb_special": _write_file(
                sources["refrain_vcb"] / "Little Busters! Refrain NCOP.mkv",
                b"refrain-ncop",
            ),
            "refrain_vcb_cd": _write_file(
                sources["refrain_vcb"]
                / "CDs"
                / "Disc 1"
                / "Little Busters! Refrain track.flac",
                b"refrain-cd",
            ),
            "refrain_vcb_sp": _write_file(
                sources["refrain_vcb"]
                / "SPs"
                / "Menu"
                / "Little Busters! Refrain Menu.mkv",
                b"refrain-sp",
            ),
        }

        settings = _settings(catalog_root, base, root)
        return root, MediaCatalog.load(catalog_root), settings, sources, files

    @staticmethod
    def _logo_override(files: dict[str, Path]) -> dict[str, str]:
        return {str(files["shared_logo"]): REFRAIN_WORK}

    def _resolved_plan(
        self,
        root: Path,
        catalog: MediaCatalog,
        settings: OrganizerSettings,
        files: dict[str, Path],
        **kwargs: object,
    ) -> dict[str, object]:
        return build_plan(
            root,
            catalog=catalog,
            settings=settings,
            file_work_overrides=self._logo_override(files),
            **kwargs,
        )

    def test_initial_preview_has_four_routes_and_only_shared_logo_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)

            plan = build_plan(root, catalog=catalog, settings=settings)
            repeated = build_plan(root, catalog=catalog, settings=settings)

            self.assertFalse(plan["ready"])
            self.assertEqual(plan["plan_id"], repeated["plan_id"])
            self.assertEqual(plan["version"], 4)
            self.assertEqual(plan["family_works"], [BASE_WORK, REFRAIN_WORK, EX_WORK])
            self.assertEqual(plan["summary"]["route_count"], 4)
            self.assertEqual(plan["summary"]["assignment_count"], 4)
            self.assertEqual(plan["summary"]["scanned_file_count"], len(files))
            self.assertEqual(plan["summary"]["classified_file_count"], len(files) - 1)
            self.assertEqual(plan["summary"]["unresolved_file_count"], 1)
            self.assertEqual({issue["code"] for issue in plan["issues"]}, {"work-ambiguous"})
            self.assertEqual(len(plan["unresolved_files"]), 1)
            self.assertEqual(plan["unresolved_files"][0]["source"], str(files["shared_logo"]))
            self.assertEqual(
                set(plan["unresolved_files"][0]["candidates"]),
                {REFRAIN_WORK, EX_WORK},
            )
            self.assertEqual(
                {(row["work_name"], row["press_group"]) for row in plan["assignments"]},
                set(EXPECTED_TARGETS),
            )

    def test_file_override_resolves_every_file_with_exact_targets_and_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)

            plan = self._resolved_plan(root, catalog, settings, files)

            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["issues"], [])
            self.assertEqual(plan["unresolved_files"], [])
            self.assertEqual(plan["summary"]["route_count"], 4)
            self.assertEqual(plan["summary"]["file_count"], len(files))
            self.assertEqual(plan["summary"]["fallback_file_count"], 0)
            self.assertEqual(plan["summary"]["others_file_count"], 1)
            self.assertEqual(plan["summary"]["database_target_count"], 4)

            assignments = {
                (row["work_name"], row["press_group"]): row
                for row in plan["assignments"]
            }
            self.assertEqual(set(assignments), set(EXPECTED_TARGETS))
            for key, expected_target in EXPECTED_TARGETS.items():
                self.assertEqual(assignments[key]["target_relpath"], expected_target)
                self.assertEqual(assignments[key]["target_authority"], "database_press_path")

            moves = plan["moves"]
            move_sources = [row["source"] for row in moves]
            move_targets = [row["target"] for row in moves]
            self.assertEqual(set(move_sources), {str(path) for path in files.values()})
            self.assertEqual(len(move_sources), len(set(move_sources)))
            self.assertEqual(len(move_targets), len(set(move_targets)))
            self.assertTrue(
                all(Path(row["source"]).name == Path(row["target"]).name for row in moves)
            )

            jsum_routes = {
                row["route_id"] for row in plan["assignments"] if row["press_group"] == "JSUM"
            }
            jsum_moves = [row for row in moves if row["route_id"] in jsum_routes]
            self.assertEqual(len(jsum_moves), 4)
            for move in jsum_moves:
                self.assertEqual(move["classifier_id"], "jsum-layout")
                self.assertNotIn("(Jsum)", Path(move["target_relative_path"]).parts[0])
            self.assertEqual(
                {move["layout_category"] for move in jsum_moves},
                {"Disc", "Others"},
            )

            moves_by_source = {row["source"]: row for row in moves}
            expected_vcb_layouts = {
                "ex_vcb_main": Path("Little Busters! EX_BDRip_Disc") / files["ex_vcb_main"].name,
                "ex_vcb_audio": Path("Little Busters! EX_BDRip_CD") / files["ex_vcb_audio"].name,
                "ex_vcb_scan": Path("Little Busters! EX_BDRip_Image") / "Booklet" / files["ex_vcb_scan"].name,
                "ex_vcb_fallback": Path("Little Busters! EX_BDRip_Fonts") / files["ex_vcb_fallback"].name,
                "refrain_vcb_main": Path("Little Busters! Refrain_BDRip_Disc") / files["refrain_vcb_main"].name,
                "refrain_vcb_special": Path("Little Busters! Refrain_BDRip_OP+ED") / files["refrain_vcb_special"].name,
                "refrain_vcb_cd": Path("Little Busters! Refrain_BDRip_CD") / "Disc 1" / files["refrain_vcb_cd"].name,
                "refrain_vcb_sp": Path("Little Busters! Refrain_BDRip_Menu") / files["refrain_vcb_sp"].name,
            }
            for file_key, expected_relative in expected_vcb_layouts.items():
                self.assertEqual(
                    Path(moves_by_source[str(files[file_key])]["target_relative_path"]),
                    expected_relative,
                )

            fonts_move = moves_by_source[str(files["ex_vcb_fallback"])]
            self.assertEqual(fonts_move["classifier_id"], "vcb-layout")
            self.assertEqual(fonts_move["layout_stage"], "press-group")
            self.assertEqual(fonts_move["layout_category"], "Fonts")
            logo_move = moves_by_source[str(files["shared_logo"])]
            self.assertEqual(logo_move["work_name"], REFRAIN_WORK)
            self.assertEqual(logo_move["classification_stage"], "manual")

    def test_generic_fallback_classifies_unknown_group_into_others(self) -> None:
        relative = Path("Unknown Tree") / "bundle.bin"

        decision = DEFAULT_CLASSIFIER_REGISTRY.classify("UNKNOWN-GROUP", relative)

        self.assertEqual(decision.relative_path, relative)
        self.assertEqual(decision.classifier_id, "fallback-layout")
        self.assertEqual(decision.rule_id, "generic-others-fallback")
        self.assertEqual(decision.stage, "fallback")
        self.assertEqual(decision.category, "Others")

    def test_route_override_changes_only_the_selected_route_and_plan_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            original = self._resolved_plan(root, catalog, settings, files)
            selected = next(
                row
                for row in original["assignments"]
                if row["work_name"] == EX_WORK and row["press_group"] == "JSUM"
            )

            revised = self._resolved_plan(
                root,
                catalog,
                settings,
                files,
                route_target_overrides={selected["route_id"]: "Manual EX JSUM"},
            )

            self.assertTrue(revised["ready"], revised["issues"])
            self.assertNotEqual(original["plan_id"], revised["plan_id"])
            original_by_route = {row["route_id"]: row for row in original["assignments"]}
            revised_by_route = {row["route_id"]: row for row in revised["assignments"]}
            self.assertEqual(set(original_by_route), set(revised_by_route))
            for route_id, original_assignment in original_by_route.items():
                revised_assignment = revised_by_route[route_id]
                if route_id == selected["route_id"]:
                    self.assertEqual(revised_assignment["target_relpath"], "Manual EX JSUM")
                    self.assertEqual(revised_assignment["target_authority"], "user_override")
                else:
                    self.assertEqual(
                        revised_assignment["target_relpath"],
                        original_assignment["target_relpath"],
                    )
                    self.assertEqual(
                        revised_assignment["target_authority"],
                        original_assignment["target_authority"],
                    )
            self.assertEqual(revised["summary"]["manual_target_count"], 1)

    def test_legacy_source_override_is_blocked_when_source_has_multiple_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, sources, files = self._fixture(temp)

            plan = self._resolved_plan(
                root,
                catalog,
                settings,
                files,
                target_overrides={str(sources["jsum"]): "Unsafe Legacy Target"},
            )

            self.assertFalse(plan["ready"])
            self.assertEqual(
                {issue["code"] for issue in plan["issues"]},
                {"legacy-override-ambiguous"},
            )
            self.assertFalse(
                any(row["source_dir"] == str(sources["jsum"]) for row in plan["assignments"])
            )
            self.assertFalse(
                any(Path(row["source"]).is_relative_to(sources["jsum"]) for row in plan["moves"])
            )

    def test_existing_destination_blocks_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            collision = (
                root
                / REFRAIN_VCB_TARGET
                / "Little Busters! Refrain_BDRip_OP+ED"
                / files["refrain_vcb_special"].name
            )
            _write_file(collision, b"existing")

            plan = self._resolved_plan(root, catalog, settings, files)

            self.assertFalse(plan["ready"])
            self.assertIn("destination-exists", {issue["code"] for issue in plan["issues"]})

    def test_apply_rejects_tampered_plan_and_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, _sources, files = self._fixture(temp)
            plan = self._resolved_plan(root, catalog, settings, files)

            with self.assertRaisesRegex(ValueError, "确认码不匹配"):
                apply_plan(plan, confirmation="wrong")

            original_source = Path(plan["moves"][0]["source"])
            plan["moves"][0]["target"] = str(root / "tampered" / original_source.name)
            with self.assertRaisesRegex(ValueError, "计划内容与计划 ID 不一致"):
                apply_plan(plan, confirmation=plan["plan_id"])
            self.assertTrue(original_source.is_file())

    def test_apply_moves_every_file_without_renaming_and_cleans_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, catalog, settings, sources, files = self._fixture(temp)
            plan = self._resolved_plan(root, catalog, settings, files)

            result = apply_plan(plan, confirmation=plan["plan_id"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["moved_file_count"], len(files))
            self.assertTrue(
                (
                    root
                    / REFRAIN_JSUM_TARGET
                    / "Little Busters! Refrain_BDRip_Others"
                    / "Shared"
                    / files["shared_logo"].name
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / EX_VCB_TARGET
                    / "Little Busters! EX_BDRip_CD"
                    / files["ex_vcb_audio"].name
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / EX_VCB_TARGET
                    / "Little Busters! EX_BDRip_Image"
                    / "Booklet"
                    / files["ex_vcb_scan"].name
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / REFRAIN_VCB_TARGET
                    / "Little Busters! Refrain_BDRip_OP+ED"
                    / files["refrain_vcb_special"].name
                ).is_file()
            )
            self.assertTrue(all(not source.exists() for source in sources.values()))

    def test_web_config_preview_and_explicit_empty_selection_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, _sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                config = organizer_config_payload()
                preview = preview_organizer_from_ui_body(request)
                with self.assertRaisesRegex(ValueError, "source_names"):
                    preview_organizer_from_ui_body({"root": str(root), "source_names": []})

            self.assertTrue(config["ok"])
            self.assertEqual(config["version"], 4)
            self.assertTrue(config["capabilities"]["per_file_routing"])
            self.assertTrue(config["capabilities"]["route_target_overrides"])
            self.assertTrue(config["capabilities"]["file_work_overrides"])
            self.assertEqual(config["paths"]["catalog_root"], str(settings.catalog_root))
            self.assertEqual(config["paths"]["default_work_root"], str(root))
            self.assertIn(REFRAIN_WORK, config["classification"]["work_aliases"])
            self.assertTrue(preview["ok"])
            self.assertTrue(preview["plan"]["ready"], preview["plan"]["issues"])

    def test_web_apply_rebuild_detects_changes_before_any_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                preview = preview_organizer_from_ui_body(request)["plan"]
                with self.assertRaisesRegex(ValueError, "acknowledge_move"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": preview["plan_id"],
                        }
                    )
                with self.assertRaisesRegex(ValueError, "confirmation 与 plan_id 不一致"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": "0" * 16,
                            "acknowledge_move": True,
                        }
                    )

                files["ex_vcb_main"].write_bytes(b"changed-after-preview")
                with self.assertRaisesRegex(ValueError, "计划已发生变化"):
                    apply_organizer_from_ui_body(
                        {
                            **request,
                            "plan_id": preview["plan_id"],
                            "confirmation": preview["plan_id"],
                            "acknowledge_move": True,
                            "moves": [
                                {
                                    "source": "C:\\not-trusted",
                                    "target": "C:\\also-not-trusted",
                                }
                            ],
                        }
                    )

            self.assertTrue(files["ex_vcb_main"].is_file())
            self.assertTrue(all(source.exists() for source in sources.values()))
            self.assertFalse((root / EX_VCB_TARGET).exists())

    def test_web_apply_uses_fresh_server_plan_and_ignores_client_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _catalog, settings, sources, files = self._fixture(temp)
            request = {
                "root": str(root),
                "file_work_overrides": self._logo_override(files),
            }
            with patch(
                "media_directory_organizer.web.load_organizer_settings",
                return_value=settings,
            ):
                plan = preview_organizer_from_ui_body(request)["plan"]
                result = apply_organizer_from_ui_body(
                    {
                        **request,
                        "plan_id": plan["plan_id"],
                        "confirmation": plan["plan_id"],
                        "acknowledge_move": True,
                        "moves": [
                            {
                                "source": "C:\\not-trusted",
                                "target": "C:\\also-not-trusted",
                            }
                        ],
                    }
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["plan_id"], plan["plan_id"])
            self.assertEqual(result["execution"]["moved_file_count"], len(files))
            self.assertTrue(
                (
                    root
                    / REFRAIN_JSUM_TARGET
                    / "Little Busters! Refrain_BDRip_Others"
                    / "Shared"
                    / files["shared_logo"].name
                ).is_file()
            )
            self.assertTrue(all(not source.exists() for source in sources.values()))


if __name__ == "__main__":
    unittest.main()
