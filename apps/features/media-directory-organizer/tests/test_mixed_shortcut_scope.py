from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer.settings import OrganizerSettings
from media_directory_organizer.web import preview_organizer_from_ui_body


def _write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fixture(
    temp: str,
    *,
    missing_unplanned_target: bool,
) -> tuple[Path, OrganizerSettings, Path, Path, dict[tuple[str, str], Path]]:
    base = Path(temp)
    root = base / "Shared Shortcut Root"
    root.mkdir()
    catalog_root = base / "db"
    catalog_root.mkdir()
    targets = {
        ("Shared Alpha", "JSUM"): root / "Shared Alpha_BDRip(Jsum)",
        ("Shared Alpha", "VCB"): root / "Shared Alpha_BDRip(VCBM)",
        ("Shared Beta", "JSUM"): root / "Shared Beta_BDRip(Jsum)",
        ("Shared Beta", "VCB"): root / "Shared Beta_BDRip(VCBM)",
    }
    for (work_name, press_group), target in targets.items():
        if (work_name, press_group) == ("Shared Beta", "VCB"):
            continue
        if missing_unplanned_target and (work_name, press_group) == (
            "Shared Alpha",
            "VCB",
        ):
            continue
        _write_file(
            target / f"{work_name}_BDRip_Disc" / "01.mkv",
            f"{work_name}-{press_group}".encode("utf-8"),
        )

    pending_media = _write_file(
        root / "[Shared Beta][BDRip][VCB]" / "[Shared Beta][01].mkv",
        b"pending-beta-vcb",
    )
    root_yaml = str(root).replace("'", "''")
    catalog_file = catalog_root / "[JP][TVInfo][shared-shortcuts].yaml"
    catalog_file.write_text(
        "".join(
            f"""\
- attributes:
  - type: date
    data: {{start: '20240101', end: '20240301'}}
  - type: collection-type
    data:
      domain: animation
      release_type: tv
      collectioned:
      - press_format: BDRip
        press_group: JSUM
        press_path: {work_name}_BDRip(Jsum)
      - press_format: BDRip
        press_group: VCB
        press_path: {work_name}_BDRip(VCBM)
      markers: []
      path: '{root_yaml}'
  - type: country
    data: japan
  - type: name
    data: {work_name}
"""
            for work_name in ("Shared Alpha", "Shared Beta")
        ),
        encoding="utf-8",
    )
    settings = OrganizerSettings(
        catalog_root=catalog_root,
        allowed_resource_roots=(base,),
        format_markers={"BDRip": ("bdrip",)},
        group_markers={"JSUM": ("jsum",), "VCB": ("vcb",)},
        group_suffixes={"JSUM": "Jsum", "VCB": "VCBM"},
        max_files=100,
        default_work_root=root,
        work_aliases={},
    )
    return root, settings, pending_media, catalog_file, targets


class MixedShortcutScopeTest(unittest.TestCase):
    def test_all_database_presses_are_scoped_but_only_planned_target_may_be_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as shortcut_temp, ExitStack() as stack:
            shortcut_root = Path(shortcut_temp) / "shortcuts"
            shortcut_root.mkdir()
            stack.enter_context(
                patch(
                    "collection_detail.link_index.resource_roots",
                    return_value=[Path(tempfile.gettempdir())],
                )
            )
            stack.enter_context(
                patch(
                    "collection_detail.link_index._shortcut_root_for_work",
                    return_value=shortcut_root,
                )
            )
            stack.enter_context(
                patch(
                    "collection_detail.link_index._layout_levels",
                    return_value=("{name}",),
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
                    "collection_detail.link_index._windows_shortcut_target",
                    return_value="",
                )
            )

            for missing_unplanned_target in (False, True):
                with self.subTest(
                    missing_unplanned_target=missing_unplanned_target
                ), tempfile.TemporaryDirectory() as temp:
                    root, settings, pending_media, catalog_file, targets = _fixture(
                        temp,
                        missing_unplanned_target=missing_unplanned_target,
                    )
                    catalog_before = catalog_file.read_bytes()

                    plan = preview_organizer_from_ui_body(
                        {"root": str(root)},
                        settings=settings,
                    )["plan"]

                    self.assertEqual(len(plan["assignments"]), 1)
                    self.assertEqual(
                        (
                            plan["assignments"][0]["work_name"],
                            plan["assignments"][0]["press_group"],
                        ),
                        ("Shared Beta", "VCB"),
                    )
                    self.assertEqual(plan["shortcut_summary"]["total_count"], 4)
                    self.assertEqual(
                        len(plan["shortcut_scope"]["work_refs"]),
                        2,
                    )
                    self.assertEqual(
                        {
                            (row["work_name"], row["press_group"])
                            for row in plan["shortcuts"]
                        },
                        set(targets),
                    )
                    self.assertEqual(
                        plan["shortcut_summary"]["missing_target_count"],
                        2 if missing_unplanned_target else 1,
                    )
                    missing_issues = [
                        issue
                        for issue in plan["shortcut_issues"]
                        if issue["code"] == "shortcut-target-missing"
                    ]
                    if missing_unplanned_target:
                        self.assertFalse(plan["ready"])
                        self.assertEqual(len(missing_issues), 1)
                        self.assertEqual(
                            Path(missing_issues[0]["path"]),
                            targets[("Shared Alpha", "VCB")],
                        )
                    else:
                        self.assertTrue(plan["ready"], plan["issues"])
                        self.assertEqual(missing_issues, [])
                    self.assertNotIn(
                        str(targets[("Shared Beta", "VCB")]),
                        {issue.get("path") for issue in missing_issues},
                    )
                    self.assertTrue(pending_media.is_file())
                    self.assertEqual(catalog_file.read_bytes(), catalog_before)
                    self.assertEqual(list(shortcut_root.rglob("*.lnk")), [])


if __name__ == "__main__":
    unittest.main()
