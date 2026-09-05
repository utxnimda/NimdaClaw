from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.web import (
    preview_organizer_from_ui_body,
    suggest_organizer_landing_from_ui_body,
)


EXACT_WORK = "Binding Exact Work"
MANUAL_WORK = "Binding Manual Work"
MISSING_WORK = "Binding New Work"
EXACT_SOURCE = "[VCB-Studio] Binding Exact Work [BDRip]"
MANUAL_SOURCE = "[JSUM] Unrecognized Existing Alias [BDRip]"
MISSING_SOURCE = "[mawen1250] Binding New Work [BDRip]"
EXACT_TARGET = "Binding Exact Work_BDRip(VCBM)"


def _work_yaml(
    *,
    name: str,
    root: Path,
    group: str,
    press_path: str = "",
    start: str = "20200101",
) -> str:
    press_path_line = f"        press_path: {press_path}\n" if press_path else ""
    return f"""\
- attributes:
  - type: date
    data: {{start: '{start}', end: ''}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      path: '{str(root).replace(chr(39), chr(39) * 2)}'
      collectioned:
      - press_format: BDRip
        press_group: {group}
{press_path_line}      markers: []
  - type: country
    data: japan
  - type: name
    data: {name}
"""


def _tree_snapshot(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return directories, files


def _write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class _BindingFixture:
    def __init__(self, base: Path, *, duplicate_manual: bool = False) -> None:
        self.base = base
        self.resource_root = base / "resources"
        self.root = self.resource_root / "Binding Family"
        self.catalog_root = base / "catalog"
        self.shortcut_root = base / "shortcuts"
        self.index_path = base / "cache" / "link-index.yaml"
        self.root.mkdir(parents=True)
        self.catalog_root.mkdir()
        self.shortcut_root.mkdir()
        self.sources = {
            "exact": self.root / EXACT_SOURCE,
            "manual": self.root / MANUAL_SOURCE,
            "missing": self.root / MISSING_SOURCE,
        }
        self.media = {
            "exact": _write_file(
                self.sources["exact"] / f"[{EXACT_WORK}][01].mkv",
                b"exact-media",
            ),
            "manual": _write_file(
                self.sources["manual"] / "[Unknown Existing Alias][01].mkv",
                b"manual-media",
            ),
            "missing": _write_file(
                self.sources["missing"] / f"[{MISSING_WORK}][01].mkv",
                b"missing-media",
            ),
        }
        entries = [
            _work_yaml(
                name=EXACT_WORK,
                root=self.root,
                group="VCB",
                press_path=EXACT_TARGET,
            ),
            _work_yaml(
                name=MANUAL_WORK,
                root=self.root,
                group="VCB",
                start="20210101",
            ),
        ]
        if duplicate_manual:
            entries.append(
                _work_yaml(
                    name=MANUAL_WORK,
                    root=self.root,
                    group="VCB",
                    start="20220101",
                )
            )
        self.catalog_file = self.catalog_root / "[JP][TVInfo][bindings].yaml"
        self.catalog_file.write_text("".join(entries), encoding="utf-8")
        self.settings = OrganizerSettings(
            catalog_root=self.catalog_root,
            allowed_resource_roots=(self.resource_root,),
            format_markers={"BDRip": ("bdrip",)},
            group_markers={
                "VCB": ("vcb-studio", "vcb"),
                "JSUM": ("jsum",),
                "MW": ("mawen1250",),
            },
            group_suffixes={"VCB": "VCBM", "JSUM": "Jsum", "MW": "MW"},
            max_files=100,
            default_work_root=self.root,
            work_aliases={},
        )


@contextmanager
def _shortcut_preview_sandbox(fixture: _BindingFixture) -> Iterator[None]:
    def shortcut_target(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "collection_detail.link_index.resource_roots",
                return_value=[fixture.resource_root],
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index.shortcut_roots",
                return_value=[fixture.shortcut_root],
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._shortcut_root_for_work",
                return_value=fixture.shortcut_root,
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._layout_levels",
                return_value=("{year_label}", "{name}"),
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._shortcut_name_template",
                return_value="{press_format}{press_group_suffix}",
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._link_index_db_path",
                return_value=fixture.index_path,
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._windows_shortcut_target",
                side_effect=shortcut_target,
            )
        )
        stack.enter_context(
            patch(
                "collection_detail.link_index._create_windows_shortcut",
                side_effect=AssertionError("a preview attempted to create a shortcut"),
            )
        )
        yield


def _binding_rows(plan: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(row["source_name"]): row
        for row in plan.get("source_work_bindings", [])
    }


class SourceWorkBindingsPreviewTest(unittest.TestCase):
    def test_three_sources_report_catalog_manual_and_missing_states_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _BindingFixture(Path(temp))

            with _shortcut_preview_sandbox(fixture):
                initial_before = _tree_snapshot(fixture.base)
                initial = preview_organizer_from_ui_body(
                    {
                        "root": str(fixture.root),
                        "source_names": [EXACT_SOURCE],
                    },
                    settings=fixture.settings,
                )["plan"]
                self.assertEqual(_tree_snapshot(fixture.base), initial_before)
                exact_row = _binding_rows(initial)[EXACT_SOURCE]
                exact_ref = exact_row["catalog_ref"]
                self.assertIsInstance(exact_ref, dict)

                exact_shortcut = next(
                    row
                    for row in initial["shortcuts"]
                    if row["work_name"] == EXACT_WORK
                )
                wrong_target = fixture.resource_root / "Wrong Legacy Target"
                wrong_target.mkdir()
                wrong_shortcut = Path(exact_shortcut["shortcut_path"])
                wrong_shortcut.parent.mkdir(parents=True, exist_ok=True)
                wrong_shortcut.write_text(str(wrong_target), encoding="utf-8")
                request = {
                    "root": str(fixture.root),
                    "source_work_bindings": {
                        str(fixture.sources["exact"]): {
                            "mode": "catalog",
                            "catalog_ref": exact_ref,
                        },
                        MANUAL_SOURCE: {
                            "mode": "manual",
                            "work_name": MANUAL_WORK,
                        },
                        MISSING_SOURCE: {
                            "mode": "manual",
                            "work_name": MISSING_WORK,
                        },
                    },
                }
                reviewed_before = _tree_snapshot(fixture.base)
                reviewed = preview_organizer_from_ui_body(
                    request,
                    settings=fixture.settings,
                )["plan"]

                self.assertEqual(_tree_snapshot(fixture.base), reviewed_before)
                self.assertFalse(reviewed["ready"])
                rows = _binding_rows(reviewed)
                self.assertEqual(rows[EXACT_SOURCE]["state"], "catalog_bound")
                self.assertEqual(rows[MANUAL_SOURCE]["state"], "manual_matched")
                self.assertEqual(rows[MISSING_SOURCE]["state"], "catalog_missing")
                self.assertEqual(rows[EXACT_SOURCE]["catalog_ref"], exact_ref)
                self.assertFalse(reviewed["legacy_shortcut_authoritative"])
                self.assertEqual(
                    reviewed["source_work_binding_summary"]["state_counts"],
                    {
                        "catalog_bound": 1,
                        "catalog_missing": 1,
                        "manual_matched": 1,
                    },
                )
                self.assertTrue(rows[MANUAL_SOURCE]["catalog_repair_required"])
                self.assertTrue(rows[MANUAL_SOURCE]["registration_required"])
                self.assertEqual(rows[MANUAL_SOURCE]["next_action"], "complete_manual")
                repair_presses = rows[MANUAL_SOURCE]["registration"]["draft"]["presses"]
                self.assertEqual(
                    {
                        (row["press_format"], row["press_group"])
                        for row in repair_presses
                    },
                    {("BDRip", "JSUM")},
                )
                self.assertEqual(
                    [
                        (row["press_format"], row["press_group"], row["press_path"])
                        for row in rows[MANUAL_SOURCE]["registration"]["existing_presses"]
                    ],
                    [("BDRip", "VCB", "")],
                )
                self.assertTrue(rows[MISSING_SOURCE]["registration_required"])
                self.assertEqual(rows[MISSING_SOURCE]["next_action"], "search")
                self.assertEqual(
                    rows[MISSING_SOURCE]["registration"]["draft"]["name"],
                    MISSING_WORK,
                )
                issue_codes = {issue["code"] for issue in reviewed["issues"]}
                self.assertIn("source-work-catalog-press-repair-required", issue_codes)
                self.assertIn("source-work-catalog-missing", issue_codes)
                self.assertIn("shortcut-conflict", issue_codes)
                self.assertGreaterEqual(reviewed["shortcut_summary"]["conflict_count"], 1)
                conflict = next(
                    row
                    for row in reviewed["shortcuts"]
                    if row["shortcut_path"] == str(wrong_shortcut)
                )
                self.assertEqual(conflict["status"], "conflict")
                self.assertEqual(conflict["existing_target_path"], str(wrong_target))
                self.assertEqual(wrong_shortcut.read_text(encoding="utf-8"), str(wrong_target))

                search_calls: list[tuple[str, int]] = []

                def fake_search(query: str, *, limit: int) -> dict[str, object]:
                    search_calls.append((query, limit))
                    return {
                        "query": query,
                        "search_url": "https://bangumi.tv/subject_search/Binding%20New%20Work?cat=2",
                        "total": 1,
                        "candidates": [
                            {
                                "id": 999001,
                                "name": MISSING_WORK,
                                "name_cn": "绑定新作品",
                                "date": "2023-01-01",
                                "end_date": "2023-03-01",
                                "platform": "TV",
                                "eps": 12,
                                "total_episodes": 12,
                                "confidence": 99,
                                "reasons": ["测试精确候选"],
                            }
                        ],
                    }

                search_before = _tree_snapshot(fixture.base)
                suggestion = suggest_organizer_landing_from_ui_body(
                    {
                        "root": str(fixture.root),
                        "draft_work": rows[MISSING_SOURCE]["registration"]["draft"],
                        "query": MISSING_WORK,
                        "include_bangumi": True,
                    },
                    settings=fixture.settings,
                    bangumi_searcher=fake_search,
                )
                self.assertEqual(_tree_snapshot(fixture.base), search_before)
                self.assertEqual(search_calls, [(MISSING_WORK, 6)])
                self.assertFalse(suggestion["writes_performed"])
                self.assertTrue(suggestion["requires_manual_confirmation"])
                self.assertEqual(
                    {candidate["source"] for candidate in suggestion["candidates"]},
                    {"local", "bangumi"},
                )

                completed_draft = dict(rows[MISSING_SOURCE]["registration"]["draft"])
                completed_draft["date"] = {
                    "start": "2023-01-01",
                    "end": "2023-03-01",
                }
                draft_request = {
                    **request,
                    "source_work_bindings": {
                        **request["source_work_bindings"],
                        MISSING_SOURCE: {
                            "mode": "draft",
                            "draft_work": completed_draft,
                        },
                    },
                }
                draft_before = _tree_snapshot(fixture.base)
                draft_plan = preview_organizer_from_ui_body(
                    draft_request,
                    settings=fixture.settings,
                )["plan"]
                self.assertEqual(_tree_snapshot(fixture.base), draft_before)
                draft_row = _binding_rows(draft_plan)[MISSING_SOURCE]
                self.assertEqual(draft_row["state"], "draft_ready")
                self.assertTrue(draft_row["registration_required"])
                self.assertEqual(draft_row["next_action"], "complete_manual")
                self.assertFalse(draft_plan["ready"])

    def test_automatic_binding_second_pass_preserves_service_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _BindingFixture(Path(temp))
            with _shortcut_preview_sandbox(fixture):
                before = _tree_snapshot(fixture.base)
                reviewed = preview_organizer_from_ui_body(
                    {
                        "root": str(fixture.root),
                        "source_work_bindings": {
                            source.name: {"mode": "automatic"}
                            for source in fixture.sources.values()
                        },
                    },
                    settings=fixture.settings,
                )["plan"]
                self.assertEqual(_tree_snapshot(fixture.base), before)
                row = _binding_rows(reviewed)[EXACT_SOURCE]
                self.assertEqual(row["requested_mode"], "automatic")
                self.assertEqual(row["state"], "automatic_matched")
                self.assertIsNotNone(row["catalog_ref"])
                self.assertEqual(len(row["resolved_works"]), 1)
                self.assertGreaterEqual(len(row["candidates"]), 1)

    def test_binding_selection_ref_validation_and_direct_child_checks_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = _BindingFixture(Path(temp), duplicate_manual=True)
            with _shortcut_preview_sandbox(fixture):
                before = _tree_snapshot(fixture.base)
                multiple = preview_organizer_from_ui_body(
                    {
                        "root": str(fixture.root),
                        "source_work_bindings": {
                            MANUAL_SOURCE: {
                                "mode": "manual",
                                "work_name": MANUAL_WORK,
                            }
                        },
                    },
                    settings=fixture.settings,
                )["plan"]
                self.assertEqual(_tree_snapshot(fixture.base), before)
                multiple_row = _binding_rows(multiple)[MANUAL_SOURCE]
                self.assertEqual(multiple_row["state"], "selection_required")
                self.assertEqual(len(multiple_row["candidates"]), 2)
                self.assertFalse(multiple["ready"])

                selected_routes: list[str] = []
                selected_indexes: list[int] = []
                for candidate in multiple_row["candidates"]:
                    selected_ref = candidate["catalog_ref"]
                    exact_duplicate = preview_organizer_from_ui_body(
                        {
                            "root": str(fixture.root),
                            "source_names": [MANUAL_SOURCE],
                            "source_work_bindings": {
                                MANUAL_SOURCE: {
                                    "mode": "catalog",
                                    "catalog_ref": selected_ref,
                                }
                            },
                            "source_press_overrides": {
                                MANUAL_SOURCE: {
                                    "press_format": "BDRip",
                                    "press_group": "VCB",
                                }
                            },
                        },
                        settings=fixture.settings,
                    )["plan"]
                    selected_row = _binding_rows(exact_duplicate)[MANUAL_SOURCE]
                    self.assertEqual(selected_row["catalog_ref"], selected_ref)
                    self.assertEqual(len(exact_duplicate["assignments"]), 1)
                    assignment = exact_duplicate["assignments"][0]
                    self.assertEqual(assignment["catalog_ref"], selected_ref)
                    selected_routes.append(assignment["route_id"])
                    selected_indexes.append(int(selected_ref["index_in_file"]))
                self.assertEqual(len(set(selected_indexes)), 2)
                self.assertEqual(len(set(selected_routes)), 2)

                exact_initial = preview_organizer_from_ui_body(
                    {
                        "root": str(fixture.root),
                        "source_names": [EXACT_SOURCE],
                    },
                    settings=fixture.settings,
                )["plan"]
                exact_ref = _binding_rows(exact_initial)[EXACT_SOURCE]["catalog_ref"]
                forged_ref = {**exact_ref, "source_sha256": "0" * 64}
                forged_before = _tree_snapshot(fixture.base)
                with self.assertRaisesRegex(ValueError, "catalog_ref"):
                    preview_organizer_from_ui_body(
                        {
                            "root": str(fixture.root),
                            "source_work_bindings": {
                                EXACT_SOURCE: {
                                    "mode": "catalog",
                                    "catalog_ref": forged_ref,
                                }
                            },
                        },
                        settings=fixture.settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), forged_before)

                nested = fixture.sources["exact"] / "Nested"
                nested.mkdir()
                nested_before = _tree_snapshot(fixture.base)
                with self.assertRaisesRegex(ValueError, "一级"):
                    preview_organizer_from_ui_body(
                        {
                            "root": str(fixture.root),
                            "source_work_bindings": {
                                str(nested): {
                                    "mode": "catalog",
                                    "catalog_ref": exact_ref,
                                }
                            },
                        },
                        settings=fixture.settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), nested_before)

                fixture.catalog_file.write_bytes(
                    fixture.catalog_file.read_bytes() + b"\n"
                )
                stale_before = _tree_snapshot(fixture.base)
                with self.assertRaisesRegex(ValueError, "catalog_ref"):
                    preview_organizer_from_ui_body(
                        {
                            "root": str(fixture.root),
                            "source_work_bindings": {
                                EXACT_SOURCE: {
                                    "mode": "catalog",
                                    "catalog_ref": exact_ref,
                                }
                            },
                        },
                        settings=fixture.settings,
                    )
                self.assertEqual(_tree_snapshot(fixture.base), stale_before)


if __name__ == "__main__":
    unittest.main()
