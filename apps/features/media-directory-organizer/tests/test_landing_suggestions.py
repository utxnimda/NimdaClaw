from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_directory_organizer.catalog import MediaCatalog
from media_directory_organizer.landing import discover_catalog_work_draft
from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.suggestions import suggest_work_landing


SOURCE_NAME = "[2017][Fate Apocrypha][BDRIP][1080P][1-25Fin+SP]"


def _settings(catalog_root: Path, resource_root: Path) -> OrganizerSettings:
    return OrganizerSettings(
        catalog_root=catalog_root,
        allowed_resource_roots=(resource_root,),
        format_markers={
            "BDRip": ("bdrip", "blu-ray", "bluray"),
            "DVDRip": ("dvdrip",),
            "1080p": ("1080p",),
            "720p": ("720p",),
        },
        group_markers={
            "VCB": ("vcb-studio", "vcb"),
            "JSUM": ("jsum",),
            "MW": ("mawen1250",),
        },
        group_suffixes={"VCB": "VCB", "JSUM": "Jsum", "MW": "MW"},
        max_files=100,
        default_work_root=None,
        work_aliases={},
    )


def _draft(root: Path, *, country: str = "japan") -> dict[str, object]:
    return {
        "name": root.name,
        "date": {"start": "", "end": ""},
        "domain": "animation" if country == "japan" else "tv-drama",
        "country": country,
        "release_type": "tv",
        "path": str(root),
        "presses": [
            {
                "source_names": [SOURCE_NAME],
                "press_format": "",
                "press_group": "",
                "press_path": "",
            }
        ],
    }


class LandingSuggestionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.catalog_root = self.base / "catalog"
        self.resource_root = self.base / "resources"
        self.work_root = self.resource_root / "Fate／Apocrypha"
        self.source_root = self.work_root / SOURCE_NAME
        self.catalog_root.mkdir()
        self.source_root.mkdir(parents=True)
        self.settings = _settings(self.catalog_root, self.resource_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _snapshot(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        directories = tuple(
            sorted(path.relative_to(self.base).as_posix() for path in self.base.rglob("*") if path.is_dir())
        )
        files = tuple(
            sorted(path.relative_to(self.base).as_posix() for path in self.base.rglob("*") if path.is_file())
        )
        return directories, files

    def test_discovery_prefers_bdrip_over_resolution_and_suggests_safe_target(self) -> None:
        catalog = MediaCatalog(works=(), catalog_root=self.catalog_root)
        result = discover_catalog_work_draft(
            self.work_root,
            catalog=catalog,
            settings=self.settings,
        )

        press = result["draft"]["presses"][0]
        self.assertEqual(press["press_format"], "BDRip")
        self.assertEqual(press["press_path"], "Fate／Apocrypha_BDRip")
        self.assertEqual(result["sources"][0]["suggested_press_format"], "BDRip")

    def test_local_and_bangumi_candidates_are_read_only_and_require_manual_choice(self) -> None:
        calls: list[tuple[str, int]] = []

        def fake_search(query: str, *, limit: int) -> dict[str, object]:
            calls.append((query, limit))
            return {
                "query": query,
                "search_url": "https://bangumi.tv/subject_search/Fate%2FApocrypha?cat=2",
                "total": 1,
                "candidates": [
                    {
                        "id": 204855,
                        "name": "Fate/Apocrypha",
                        "name_cn": "命运／外典",
                        "date": "2017-07-01",
                        "end_date": "2017-12-30",
                        "platform": "TV",
                        "eps": 25,
                        "total_episodes": 25,
                        "score": 6.4,
                        "rank": 7000,
                        "summary": "test summary",
                        "confidence": 98,
                        "reasons": ["原名完全匹配"],
                    }
                ],
            }

        before = self._snapshot()
        result = suggest_work_landing(
            {
                "root": str(self.work_root),
                "draft_work": _draft(self.work_root),
                "query": "Fate／Apocrypha",
                "include_bangumi": True,
            },
            settings=self.settings,
            bangumi_searcher=fake_search,
        )
        after = self._snapshot()

        self.assertEqual(before, after)
        self.assertEqual(calls, [("Fate/Apocrypha", 6)])
        self.assertFalse(result["writes_performed"])
        self.assertTrue(result["requires_manual_confirmation"])
        self.assertEqual([row["source"] for row in result["candidates"]], ["local", "bangumi"])
        local_press = result["candidates"][0]["proposed_work"]["presses"][0]
        self.assertEqual(local_press["press_format"], "BDRip")
        self.assertEqual(local_press["press_path"], "Fate／Apocrypha_BDRip")
        bangumi = result["candidates"][1]
        self.assertEqual(bangumi["subject_url"], "https://bangumi.tv/subject/204855")
        self.assertEqual(bangumi["proposed_work"]["name"], "Fate/Apocrypha")
        self.assertEqual(bangumi["proposed_work"]["date"]["start"], "2017-07-01")
        self.assertEqual(bangumi["proposed_work"]["date"]["end"], "2017-12-30")
        self.assertEqual(bangumi["proposed_work"]["presses"][0]["press_path"], "Fate／Apocrypha_BDRip")
        self.assertEqual(bangumi["input_fingerprint"], result["input_fingerprint"])

    def test_provider_failure_keeps_local_candidate(self) -> None:
        def offline(_query: str, *, limit: int) -> dict[str, object]:
            self.assertEqual(limit, 6)
            raise OSError("offline")

        result = suggest_work_landing(
            {
                "root": str(self.work_root),
                "draft_work": _draft(self.work_root),
                "include_bangumi": True,
            },
            settings=self.settings,
            bangumi_searcher=offline,
        )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["source"], "local")
        self.assertIn("Bangumi", result["provider_error"])

    def test_non_japanese_animation_does_not_call_bangumi(self) -> None:
        def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("Bangumi must not be called")

        result = suggest_work_landing(
            {
                "root": str(self.work_root),
                "draft_work": _draft(self.work_root, country="korea"),
                "include_bangumi": True,
            },
            settings=self.settings,
            bangumi_searcher=forbidden,
        )
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(any("仅在" in warning for warning in result["warnings"]))

    def test_manual_press_path_is_preserved(self) -> None:
        draft = _draft(self.work_root)
        draft["presses"][0].update(
            {
                "press_format": "BDRip",
                "press_group": "VCB",
                "press_path": "人工确认的目标目录",
            }
        )
        result = suggest_work_landing(
            {
                "root": str(self.work_root),
                "draft_work": draft,
                "include_bangumi": False,
            },
            settings=self.settings,
        )
        press = result["candidates"][0]["proposed_work"]["presses"][0]
        self.assertEqual(press["press_path"], "人工确认的目标目录")
        self.assertEqual(press["suggested_press_path"], "Fate／Apocrypha_BDRip")


if __name__ == "__main__":
    unittest.main()
