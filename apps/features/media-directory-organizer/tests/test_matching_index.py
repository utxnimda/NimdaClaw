"""Matching behavior and operation-count regressions; no catalog/media I/O.

Run with ``--benchmark`` for the 300-work, 100-alias, five-query scenario.
Before indexing, the same scenario took 0.259 seconds and normalized identities
422,805 times on the development machine (September 2026).
"""
from __future__ import annotations

import cProfile
import json
import statistics
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer import catalog as catalog_module
from media_directory_organizer import service
from media_directory_organizer.catalog import CatalogMatchIndex, CatalogWork, MediaCatalog, PressRecord
from media_directory_organizer.settings import OrganizerSettings


def fixture():
    root = Path.cwd() / "__synthetic_organizer_benchmark__"
    works = tuple(
        CatalogWork(
            name=f"Work{number:05}", path="", domain="animation", country="japan",
            release_type="tv", presses=(PressRecord("BDRip", "VCB"),),
            source_file=str(root / "db.yaml"), source_index=number,
        )
        for number in range(300)
    )
    settings = OrganizerSettings(
        root, (), {"BDRip": ("bdrip",)}, {"VCB": ("vcb",)}, {},
        work_aliases={f"Work{number:05}": (f"Alias{number:05}",) for number in range(100)},
    )
    return MediaCatalog(works, root), settings


def scenario(catalog, settings):
    family = service._family_with_direct_directory_evidence(
        catalog.works[:1], catalog=catalog,
        direct_directories=(catalog.catalog_root / f"Work{number:05}_BDRip" for number in range(90, 94)),
        root_identity="work00000", settings=settings,
    )
    matched = service._match_works(
        "[VCB][Alias00099][1080p]", catalog.works,
        root_identity="work00000", settings=settings,
    )
    return len(family), [work.name for work in matched[0]]


class MatchingIndexTest(unittest.TestCase):
    def test_normalized_alias_collisions_preserve_records_and_first_evidence(self) -> None:
        catalog, settings = fixture()
        first = replace(catalog.works[0], name="Known Work", source_index=1)
        second = replace(first, source_index=2)
        settings = replace(settings, work_aliases={
            "Ｋｎｏｗｎ Ｗｏｒｋ": ("First Alias", "Ｆｉｒｓｔ Ａｌｉａｓ"),
            "known work": ("Other Alias",),
        })
        indexed = CatalogMatchIndex((first, second), settings.work_aliases)

        self.assertEqual(service._exact_work_matches("FIRST ALIAS", indexed, settings), (first, second))
        self.assertEqual(service._exact_work_matches("Other Alias", indexed, settings), (first, second))
        matched, evidence = service._exact_bracket_work_matches("first alias", indexed, settings)
        self.assertEqual(matched, [first, second])
        self.assertEqual(set(evidence.values()), {"方括号完整匹配：First Alias"})

    def test_new_matching_scope_observes_alias_edits_without_polluting_previous_scope(self) -> None:
        catalog, settings = fixture()
        work = catalog.works[0]
        previous = CatalogMatchIndex((work,), settings.work_aliases)
        settings.work_aliases[work.name] = ("Replacement Alias",)
        current = CatalogMatchIndex((work,), settings.work_aliases)

        self.assertEqual(service._exact_work_matches("Alias00000", previous, settings), (work,))
        self.assertEqual(service._exact_work_matches("Alias00000", current, settings), ())
        self.assertEqual(service._exact_work_matches("Replacement Alias", current, settings), (work,))
        changed_work = replace(work, name="Changed Work")
        self.assertEqual(service._exact_work_matches("Work00000", (changed_work,), settings), ())

    def test_matching_normalization_is_linear_in_catalog_and_alias_rows(self) -> None:
        catalog, settings = fixture()
        normalize = catalog_module.normalized_identity
        with patch.object(catalog_module, "normalized_identity", wraps=normalize) as catalog_calls, patch.object(
            service, "normalized_identity", wraps=normalize,
        ) as service_calls:
            self.assertEqual(scenario(catalog, settings), (5, ["Work00099"]))
        # The old per-work scan performed 422,805 normalizations here. Allow
        # room for evidence processing while rejecting a candidates × aliases
        # regression independently of CPU speed and CI timing.
        self.assertLess(catalog_calls.call_count + service_calls.call_count, 4000)

    def test_repeated_file_matches_reuse_normalized_candidate_evidence(self) -> None:
        catalog, settings = fixture()
        indexed = CatalogMatchIndex(catalog.works, settings.work_aliases)
        with patch.object(catalog_module, "normalized_identity", wraps=catalog_module.normalized_identity) as normalize:
            for episode in range(1, 101):
                matched = service._match_works(
                    f"[VCB][Alias00099][{episode:02}][1080p].mkv", indexed,
                    root_identity="work00000", settings=settings,
                )
                self.assertEqual(matched[0], (catalog.works[99],))
        self.assertLess(normalize.call_count, 500)


def benchmark() -> None:
    catalog, settings = fixture()
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        result = scenario(catalog, settings)
        samples.append(time.perf_counter() - start)
    profile = cProfile.Profile()
    profile.runcall(scenario, catalog, settings)
    normalizations = sum(
        entry.callcount for entry in profile.getstats()
        if getattr(entry.code, "co_name", "") == "normalized_identity"
    )
    print(json.dumps({
        "works": 300, "alias_rows": 100, "directory_queries": 4, "bracket_queries": 1,
        "result": result, "median_seconds": statistics.median(samples),
        "identity_normalizations": normalizations, "writes_performed": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        benchmark()
    else:
        unittest.main()
