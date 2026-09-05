from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer.catalog import CatalogWork, MediaCatalog, PressRecord
from media_directory_organizer.execution import (
    _remove_empty_descendants,
    apply_plan,
)
from media_directory_organizer.service import build_plan
from media_directory_organizer.settings import OrganizerSettings


class OrganizerExecutionTest(unittest.TestCase):
    def _fixture(self, base: Path):
        root = base / "Sample"
        source = root / "Sample [BDRip][VCB]"
        source.mkdir(parents=True)
        for episode in (1, 2):
            (source / f"Sample {episode:02}.mkv").write_bytes(b"original media")
        catalog = MediaCatalog(
            works=(CatalogWork(
                name="Sample", path=str(root), domain="animation", country="japan",
                release_type="tv", presses=(PressRecord("BDRip", "VCB", "Sample_BDRip"),),
                source_file=str(base / "db.yaml"),
            ),),
            catalog_root=base,
        )
        settings = OrganizerSettings(
            catalog_root=base, allowed_resource_roots=(base,),
            format_markers={"BDRip": ("bdrip",)}, group_markers={"VCB": ("vcb",)},
            group_suffixes={},
        )
        plan = build_plan(root, catalog=catalog, settings=settings)
        self.assertTrue(plan["ready"], plan["issues"])
        self.assertEqual(len(plan["moves"]), 2)
        return root, source, catalog, settings, plan

    def _directory_link(self, path: Path, target: Path) -> None:
        if os.name == "nt":
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
                check=False, capture_output=True, text=True,
            )
            if created.returncode:
                self.skipTest(f"Directory junction unavailable: {created.stderr}")
        else:
            try:
                path.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlink unavailable: {exc}")

    def test_preview_rejects_top_level_source_junction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, source, catalog, settings, _plan = self._fixture(base)
            outside = base / "original-source"
            source.rename(outside)
            self._directory_link(source, outside)

            plan = build_plan(root, catalog=catalog, settings=settings)

            self.assertFalse(plan["ready"])
            self.assertEqual(plan["moves"], [])
            self.assertIn("reparse-point", {issue["code"] for issue in plan["issues"]})
            self.assertEqual(len(list(outside.iterdir())), 2)

    def test_apply_rejects_source_replaced_by_junction_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            _root, source, _catalog, _settings, plan = self._fixture(base)
            outside = base / "original-source"
            source.rename(outside)
            self._directory_link(source, outside)

            with self.assertRaisesRegex(ValueError, "符号链接或目录联接"):
                apply_plan(plan, confirmation=plan["plan_id"])

            self.assertEqual(len(list(outside.iterdir())), 2)
            self.assertTrue(all(not Path(move["target"]).exists() for move in plan["moves"]))

    def test_apply_rejects_target_parent_junction_created_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            _root, _source, _catalog, _settings, plan = self._fixture(base)
            destination_parent = Path(plan["moves"][0]["target"]).parent
            destination_parent.parent.mkdir(parents=True)
            outside = base / "unreviewed-target"
            outside.mkdir()
            self._directory_link(destination_parent, outside)

            with self.assertRaisesRegex(ValueError, "符号链接或目录联接"):
                apply_plan(plan, confirmation=plan["plan_id"])

            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue(all(Path(move["source"]).is_file() for move in plan["moves"]))

    def test_interrupt_after_first_move_rolls_media_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            original_move = shutil.move
            calls = 0

            def interrupt_second_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt()
                return original_move(source, destination)

            with patch("media_directory_organizer.execution.shutil.move", side_effect=interrupt_second_move):
                with self.assertRaises(KeyboardInterrupt):
                    apply_plan(plan, confirmation=plan["plan_id"])

            self.assertEqual(calls, 3)
            for move in plan["moves"]:
                self.assertEqual(Path(move["source"]).read_bytes(), b"original media")
                self.assertFalse(Path(move["target"]).exists())

    def test_change_between_moves_is_detected_and_prior_moves_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            original_move = shutil.move
            calls = 0

            def mutate_after_first_move(source, destination):
                nonlocal calls
                result = original_move(source, destination)
                calls += 1
                if calls == 1:
                    Path(plan["moves"][1]["source"]).write_bytes(b"externally updated media")
                return result

            with patch("media_directory_organizer.execution.shutil.move", side_effect=mutate_after_first_move):
                with self.assertRaisesRegex(OSError, "源文件发生变化"):
                    apply_plan(plan, confirmation=plan["plan_id"])

            self.assertEqual(Path(plan["moves"][0]["source"]).read_bytes(), b"original media")
            self.assertEqual(Path(plan["moves"][1]["source"]).read_bytes(), b"externally updated media")
            self.assertTrue(all(not Path(move["target"]).exists() for move in plan["moves"]))

    def test_empty_directory_cleanup_does_not_traverse_junctions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            (source / "ordinary-empty").mkdir(parents=True)
            outside = base / "unrelated"
            (outside / "keep-empty").mkdir(parents=True)
            self._directory_link(source / "linked", outside)

            self.assertEqual(_remove_empty_descendants(source), 1)

            self.assertTrue((outside / "keep-empty").is_dir())
            self.assertTrue((source / "linked").exists())


if __name__ == "__main__":
    unittest.main()
