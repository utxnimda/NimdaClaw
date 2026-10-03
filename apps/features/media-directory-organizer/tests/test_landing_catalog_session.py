"""Read-only landing evidence is shared within a phase, never across writes."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer import landing
from media_directory_organizer import landing_catalog as catalog
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.yaml_io import dump_yaml_string


def _fixture(base: Path, count: int = 12):
    database = base / "catalog"
    database.mkdir()
    root = base / "Shared Work"
    root.mkdir()
    entries = [
        {"attributes": [
            {"type": "name", "data": f"Shared Work {number}" if number < 5 else f"Unrelated {number}"},
            {"type": "date", "data": {"start": "20200101", "end": ""}},
            {"type": "country", "data": "japan"},
            {"type": "collection-type", "data": {
                "domain": "animation", "release_type": "tv",
                "path": str(root if number < 5 else base / "other"),
                "collectioned": [{"press_format": "BDRip", "press_group": "VCB", "press_path": f"Work{number}_BDRip"}],
            }},
        ]}
        for number in range(count)
    ]
    source = database / "fixture.yaml"
    source.write_text(dump_yaml_string(entries), encoding="utf-8")
    reference = {
        "yaml_source_rel": source.name, "index_in_file": 0,
        "work_name": "Shared Work 0", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    return database, root, source, reference


class LandingCatalogSessionTest(unittest.TestCase):
    def test_reference_index_rejects_float_and_boolean_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database, _root, _source, reference = _fixture(Path(temp))
            for value in (False, True, 0.0, 0.5, "0.5"):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "work_ref.index_in_file 非法"):
                    catalog._record_from_work_ref({**reference, "index_in_file": value}, catalog_root=database)
            self.assertEqual(catalog._record_from_work_ref(
                {**reference, "index_in_file": "0"}, catalog_root=database,
            )["work"]["name"], reference["work_name"])

    def test_cached_assignment_still_validates_every_name_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database, _root, _source, reference = _fixture(Path(temp))
            session = catalog.CatalogReadSession()
            cache = {}
            expected = catalog._catalog_record_for_assignment(
                {"catalog_ref": reference}, catalog_root=database, cache=cache, session=session,
            )
            for changed in ({"work_name": "Wrong Work"}, {"source_sha256": "0" * 64}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    catalog._catalog_record_for_assignment(
                        {"catalog_ref": {**reference, **changed}}, catalog_root=database,
                        cache=cache, session=session,
                    )
            self.assertIs(catalog._catalog_record_for_assignment(
                {"catalog_ref": reference}, catalog_root=database, cache=cache, session=session,
            ), expected)

    def test_plain_legacy_cache_cannot_bypass_catalog_reference_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database, _root, _source, reference = _fixture(Path(temp))
            cache = {}
            catalog._catalog_record_for_assignment({"catalog_ref": reference}, catalog_root=database, cache=cache)
            with self.assertRaises(ValueError):
                catalog._catalog_record_for_assignment(
                    {"catalog_ref": {**reference, "work_name": "Wrong Work"}},
                    catalog_root=database, cache=cache,
                )

    def test_record_hash_is_bound_to_its_bytes_and_new_phase_rechecks_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database, _root, source, reference = _fixture(Path(temp))
            old = catalog._record_from_work_ref(reference, catalog_root=database)
            source.write_text(source.read_text(encoding="utf-8").replace("Work0_BDRip", "Work0_NewRip"), encoding="utf-8")

            self.assertEqual(catalog._catalog_ref_for_record(old)["source_sha256"], reference["source_sha256"])
            with self.assertRaisesRegex(ValueError, "已经变化"):
                catalog._record_from_work_ref(reference, catalog_root=database)
            current = catalog._record_from_work_ref({**reference, "source_sha256": ""}, catalog_root=database)
            self.assertEqual(current["presses"][0]["press_path"], "Work0_NewRip")
            self.assertNotEqual(catalog._catalog_ref_for_record(current)["source_sha256"], reference["source_sha256"])

    def test_candidate_queries_read_once_and_build_only_matching_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database, root, source, _reference = _fixture(Path(temp), count=300)
            session = catalog.CatalogReadSession()
            original_read = Path.read_bytes
            reads = []

            def read(path):
                if path == source:
                    reads.append(path)
                return original_read(path)

            with patch.object(Path, "read_bytes", new=read), patch.object(
                catalog, "_catalog_entry_record", wraps=catalog._catalog_entry_record,
            ) as build:
                first = catalog._all_catalog_records_for_root(catalog_root=database, root=root, session=session)
                second = catalog._catalog_records_matching_repair_draft(
                    {"name": root.name}, catalog_root=database, root=root, session=session,
                )
                third = catalog._registration_existing_catalog_candidates(
                    root, sources=[], catalog_root=database, session=session,
                )
            self.assertEqual([len(first), len(second), len(third)], [5, 5, 5])
            self.assertEqual(build.call_count, 5)
            self.assertEqual(len(reads), 1)
            self.assertEqual([row["work"]["name"] for row in first], [f"Shared Work {number}" for number in range(5)])

    def test_shortcut_preview_reuses_assignment_and_root_catalog_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database, root, source, reference = _fixture(Path(temp))
            settings = OrganizerSettings(database, (root.parent,), {}, {}, {})
            plan = {
                "root": str(root), "moves": [], "issues": [], "ready": True,
                "assignments": [
                    {"catalog_ref": {**reference, "index_in_file": number, "work_name": f"Shared Work {number}"},
                     "catalog_file": str(source), "work_name": f"Shared Work {number}",
                     "press_format": "BDRip", "press_group": "VCB",
                     "target_dir": str(root / f"Work{number}_BDRip"), "file_count": 1}
                    for number in range(5)
                ],
            }
            original_read = Path.read_bytes
            reads = []

            def read(path):
                if path == source:
                    reads.append(path)
                return original_read(path)

            with patch.object(Path, "read_bytes", new=read), patch.object(
                landing, "preview_scoped_shortcuts_for_work", return_value=[],
            ):
                result = landing.preview_organizer_plan_shortcuts(plan, organizer_settings=settings)
            self.assertEqual(result["shortcut_issues"], [])
            self.assertEqual(len(result["shortcut_scope"]["work_refs"]), 5)
            self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    unittest.main()
