from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_directory_organizer.catalog import (
    CatalogWork, MediaCatalog, PressRecord, normalize_press_group,
)
from media_directory_organizer.inference import suggest_press_paths
from media_directory_organizer.service import apply_plan, build_plan
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.yaml_io import dump_yaml_string


class EmptyPressGroupOrganizerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "Bleach"
        self.root.mkdir()
        self.catalog_root = self.base / "catalog"
        self.catalog_root.mkdir()
        self.settings = OrganizerSettings(
            catalog_root=self.catalog_root, allowed_resource_roots=(self.base,),
            format_markers={"DVDRip": ("dvdrip",), "BDRip": ("bdrip",)},
            group_markers={"VCB": ("vcb",)}, group_suffixes={"VCB": "VCB"},
            max_files=1000, default_work_root=self.root,
        )

    def work(self, *presses: PressRecord, name: str = "Bleach", index: int = 0) -> CatalogWork:
        return CatalogWork(
            name=name, path=str(self.root), domain="animation", country="japan",
            release_type="tv", presses=presses, source_file=str(self.catalog_root / "fixture.yaml"),
            source_index=index,
        )

    def preview(self, work: CatalogWork, **kwargs):
        catalog = MediaCatalog(works=(work,), catalog_root=self.catalog_root)
        return build_plan(self.root, catalog=catalog, settings=self.settings, **kwargs)

    def source(self, name: str = "download DVDRip", count: int = 1) -> Path:
        source = self.root / name
        source.mkdir()
        for number in range(1, count + 1):
            (source / f"BLEACH 死神 {number:03}.mkv").write_bytes(b"synthetic-media")
        return source

    def test_catalog_keeps_blank_and_legacy_groups_without_rewriting_them(self) -> None:
        rows = [{"press_format": "DVDRip", "press_group": group} for group in ("", "---", "----")]
        rows.append({"press_format": "", "press_group": "VCB"})
        entry = {"attributes": [
            {"type": "date", "data": {"start": "20041005", "end": "20120327"}},
            {"type": "name", "data": "Bleach"},
            {"type": "country", "data": "japan"},
            {"type": "collection-type", "data": {
                "domain": "animation", "release_type": "tv", "path": str(self.root),
                "collectioned": rows,
            }},
        ]}
        yaml_path = self.catalog_root / "fixture.yaml"
        yaml_path.write_text(dump_yaml_string([entry]), encoding="utf-8")
        before = yaml_path.read_bytes()
        loaded = MediaCatalog.load(self.catalog_root).works[0]
        self.assertEqual([press.press_group for press in loaded.presses], ["", "---", "----"])
        self.assertEqual(yaml_path.read_bytes(), before)

    def test_empty_group_selection_is_exact_while_none_does_not_filter(self) -> None:
        work = self.work(PressRecord("DVDRip", "----"), PressRecord("DVDRip", "VCB"))
        self.assertEqual(len(work.presses_for("DVDRip")), 2)
        self.assertEqual(work.presses_for("DVDRip", ""), (work.presses[0],))
        self.assertEqual(work.presses_for("DVDRip", "---"), (work.presses[0],))
        self.assertEqual(work.presses_for("DVDRip", "vcb"), (work.presses[1],))
        self.assertEqual(normalize_press_group("－－－－"), "")

    def test_bleach_exact_catalog_binding_with_empty_group_routes_365_files(self) -> None:
        source = self.source("BLEACH_DVDRip", count=365)
        work = self.work(PressRecord("DVDRip", "", "BLEACH_DVDRip"))
        plan = self.preview(work, source_catalog_work_overrides={str(source): work})
        self.assertTrue(plan["ready"], plan["issues"])
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["unresolved_files"], [])
        self.assertEqual(len(plan["moves"]), 365)
        self.assertEqual(plan["assignments"][0]["press_group"], "")
        self.assertTrue(plan["source_work_bindings"][0]["inferred_press"][0]["press_group_confirmed"])
        self.assertEqual(plan["assignments"][0]["target_relpath"], "BLEACH_DVDRip")
        self.assertEqual(plan["assignments"][0]["classifier_ids"], ["fallback-layout"])
        self.assertEqual(len(list(source.glob("*.mkv"))), 365)
        apply_plan(plan, confirmation=plan["plan_id"])
        settled = self.preview(work)
        self.assertEqual(settled["moves"], [])
        self.assertEqual(settled["issues"], [])

    def test_automatic_group_is_ambiguous_but_explicit_empty_resolves_exact_row(self) -> None:
        source = self.source()
        work = self.work(PressRecord("DVDRip", ""), PressRecord("DVDRip", "VCB"))
        automatic = self.preview(work)
        self.assertEqual([issue["code"] for issue in automatic["issues"]], ["group-unresolved"])
        self.assertEqual(set(automatic["issues"][0]["press_groups"]), {"", "VCB"})
        self.assertFalse(automatic["source_work_bindings"][0]["inferred_press"][0]["press_group_confirmed"])
        omitted = self.preview(work, source_press_overrides={str(source): {"press_format": "DVDRip"}})
        self.assertEqual([issue["code"] for issue in omitted["issues"]], ["group-unresolved"])
        explicit = self.preview(work, source_press_overrides={str(source): {"press_group": ""}})
        self.assertTrue(explicit["ready"], explicit["issues"])
        self.assertEqual(explicit["assignments"][0]["target_relpath"], "Bleach_DVDRip")
        self.assertTrue(explicit["source_work_bindings"][0]["inferred_press"][0]["press_group_confirmed"])
        real = self.preview(work, source_press_overrides={str(source): {"press_group": "VCB"}})
        self.assertTrue(real["ready"], real["issues"])
        self.assertEqual(real["assignments"][0]["target_relpath"], "Bleach_DVDRip(VCB)")

    def test_explicit_empty_never_inherits_a_real_group_record(self) -> None:
        source = self.source()
        work = self.work(PressRecord("DVDRip", "VCB"))
        plan = self.preview(work, source_press_overrides={str(source): {"press_group": ""}})
        self.assertEqual([issue["code"] for issue in plan["issues"]], ["group-unresolved"])
        self.assertEqual(plan["moves"], [])
        self.assertIn("----", plan["issues"][0]["message"])

    def test_legacy_empty_group_keeps_existing_authoritative_press_path(self) -> None:
        self.source("download DVDRip")
        for group in ("", "---", "----"):
            with self.subTest(group=group):
                work = self.work(PressRecord("DVDRip", group, "Archived_DVD"))
                plan = self.preview(work)
                self.assertTrue(plan["ready"], plan["issues"])
                self.assertEqual(plan["assignments"][0]["target_relpath"], "Archived_DVD")
                self.assertEqual(plan["assignments"][0]["press_group"], group)
                self.assertNotIn("()", plan["moves"][0]["target"])

    def test_duplicate_empty_rows_remain_a_database_ambiguity(self) -> None:
        self.source()
        work = self.work(PressRecord("DVDRip", ""), PressRecord("DVDRip", "----"))
        plan = self.preview(work)
        self.assertEqual([issue["code"] for issue in plan["issues"]], ["press-row-ambiguous"])
        self.assertEqual(plan["moves"], [])

    def test_unsupported_exact_source_binding_reports_one_folder_issue(self) -> None:
        source = self.source("BLEACH_DVDRip", count=365)
        selected = self.work(PressRecord("BDRip", "VCB"))
        other = self.work(PressRecord("DVDRip", ""), name="Bleach Movie", index=1)
        catalog = MediaCatalog(works=(selected, other), catalog_root=self.catalog_root)
        plan = build_plan(self.root, catalog=catalog, settings=self.settings,
                          source_catalog_work_overrides={str(source): selected})
        self.assertEqual(len(plan["issues"]), 1, plan["issues"])
        self.assertEqual(plan["issues"][0]["code"], "work-ambiguous")
        self.assertEqual(plan["issues"][0]["path"], str(source))
        self.assertEqual(plan["unresolved_files"], [])
        self.assertEqual(plan["moves"], [])

    def test_group_path_suggestions_count_empty_as_a_distinct_group(self) -> None:
        rows = suggest_press_paths("Bleach", [
            {"press_format": "DVDRip", "press_group": "----"},
            {"press_format": "DVDRip", "press_group": "VCB"},
        ], settings=self.settings)
        self.assertEqual([row["press_path"] for row in rows], ["Bleach_DVDRip", "Bleach_DVDRip(VCB)"])
        self.assertEqual(rows[0]["press_group"], "----")

    def test_null_group_is_not_an_explicit_empty_choice(self) -> None:
        source = self.source()
        work = self.work(PressRecord("DVDRip", ""))
        plan = self.preview(work, source_press_overrides={str(source): {"press_group": None}})
        self.assertIn("source-press-override-invalid-type", [issue["code"] for issue in plan["issues"]])


if __name__ == "__main__":
    unittest.main()
