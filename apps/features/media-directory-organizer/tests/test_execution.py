from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer.catalog import CatalogWork, MediaCatalog, PressRecord
from media_directory_organizer.execution import (
    MediaRollbackError,
    _IncompleteFileMove,
    _link_move_file_no_replace,
    _move_file_no_replace,
    _remove_empty_descendants,
    apply_plan,
)
from media_directory_organizer.service import build_plan
from media_directory_organizer.plan_identity import stable_plan_id
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
            original_move = _move_file_no_replace
            calls = 0

            def interrupt_second_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt()
                return original_move(source, destination)

            with patch("media_directory_organizer.execution._move_file_no_replace", side_effect=interrupt_second_move):
                with self.assertRaises(KeyboardInterrupt):
                    apply_plan(plan, confirmation=plan["plan_id"])

            self.assertEqual(calls, 3)
            for move in plan["moves"]:
                self.assertEqual(Path(move["source"]).read_bytes(), b"original media")
                self.assertFalse(Path(move["target"]).exists())

    def test_change_between_moves_is_detected_and_prior_moves_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            original_move = _move_file_no_replace
            calls = 0

            def mutate_after_first_move(source, destination):
                nonlocal calls
                result = original_move(source, destination)
                calls += 1
                if calls == 1:
                    Path(plan["moves"][1]["source"]).write_bytes(b"externally updated media")
                return result

            with patch("media_directory_organizer.execution._move_file_no_replace", side_effect=mutate_after_first_move):
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

    def test_move_primitive_never_replaces_existing_file_or_moves_into_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.mkv"
            source.write_bytes(b"reviewed media")
            target = base / "target.mkv"
            target.write_bytes(b"external media")
            directory = base / "directory.mkv"
            directory.mkdir()
            for destination in (target, directory):
                with self.subTest(destination=destination), self.assertRaises(OSError):
                    _move_file_no_replace(source, destination)
                self.assertEqual(source.read_bytes(), b"reviewed media")
            self.assertEqual(target.read_bytes(), b"external media")
            self.assertEqual(list(directory.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows rename failure must not fall back to copying")
    def test_windows_rename_failure_does_not_leave_untracked_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            with patch("media_directory_organizer.execution.os.rename", side_effect=PermissionError("locked source")):
                with self.assertRaisesRegex(OSError, "locked source"):
                    apply_plan(plan, confirmation=plan["plan_id"])
            for move in plan["moves"]:
                self.assertEqual(Path(move["source"]).read_bytes(), b"original media")
                self.assertFalse(Path(move["target"]).exists())

    def test_concurrent_target_creation_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))

            def concurrent_create(source, target):
                target.write_bytes(b"created after validation")
                return _move_file_no_replace(source, target)

            with patch("media_directory_organizer.execution._move_file_no_replace", side_effect=concurrent_create):
                with self.assertRaises(OSError):
                    apply_plan(plan, confirmation=plan["plan_id"])
            self.assertEqual(Path(plan["moves"][0]["target"]).read_bytes(), b"created after validation")
            self.assertTrue(all(Path(move["source"]).exists() for move in plan["moves"]))

    def test_exclusive_link_fallback_cleans_own_link_when_source_unlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mkv"
            target = Path(temp) / "target.mkv"
            source.write_bytes(b"keep source")
            original_unlink = Path.unlink

            def deny_source_unlink(path, *args, **kwargs):
                if path == source:
                    raise PermissionError("locked source")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", deny_source_unlink):
                with self.assertRaisesRegex(PermissionError, "locked source"):
                    _link_move_file_no_replace(source, target)
            self.assertEqual(source.read_bytes(), b"keep source")
            self.assertFalse(target.exists())

    def test_exclusive_link_fallback_moves_only_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mkv"
            target = Path(temp) / "target.mkv"
            source.write_bytes(b"keep media")
            _link_move_file_no_replace(source, target)
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), b"keep media")
            directory = Path(temp) / "source-directory"
            directory.mkdir()
            with self.assertRaisesRegex(ValueError, "普通文件"):
                _link_move_file_no_replace(directory, Path(temp) / "target-directory")
            self.assertTrue(directory.is_dir())
            self.assertFalse((Path(temp) / "target-directory").exists())

    def test_exclusive_link_collision_never_removes_preexisting_same_file_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mkv"
            target = Path(temp) / "target.mkv"
            source.write_bytes(b"existing media")
            os.link(source, target)
            with self.assertRaises(FileExistsError):
                _link_move_file_no_replace(source, target)
            self.assertTrue(source.samefile(target))
            self.assertEqual(target.read_bytes(), b"existing media")

    def test_interrupt_after_os_move_keeps_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))

            def move_then_interrupt(source, target):
                _move_file_no_replace(source, target)
                raise KeyboardInterrupt()

            with patch("media_directory_organizer.execution._move_file_no_replace", side_effect=move_then_interrupt):
                with self.assertRaises(MediaRollbackError) as raised:
                    apply_plan(plan, confirmation=plan["plan_id"])
            payload = raised.exception.to_payload()
            self.assertFalse(payload["media"]["rollback_complete"])
            self.assertEqual(payload["media"]["recovery_moves"][0]["target"], plan["moves"][0]["target"])
            self.assertFalse(Path(plan["moves"][0]["source"]).exists())
            self.assertTrue(Path(plan["moves"][0]["target"]).is_file())

    def test_interrupt_after_os_link_keeps_both_files_and_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            original_link = os.link

            def link_then_interrupt(source, target, **kwargs):
                original_link(source, target, **kwargs)
                raise KeyboardInterrupt()

            with (
                patch("media_directory_organizer.execution._move_file_no_replace", side_effect=_link_move_file_no_replace),
                patch("media_directory_organizer.execution.os.link", side_effect=link_then_interrupt),
            ):
                with self.assertRaises(MediaRollbackError) as raised:
                    apply_plan(plan, confirmation=plan["plan_id"])
            payload = raised.exception.to_payload()
            self.assertFalse(payload["media"]["rollback_complete"])
            self.assertIn("KeyboardInterrupt", payload["media"]["failed_move"]["error"])
            self.assertEqual(payload["media"]["rolled_back_file_count"], 0)
            recovery = payload["media"]["recovery_moves"][0]
            self.assertEqual(recovery["target"], plan["moves"][0]["target"])
            self.assertTrue(Path(recovery["source"]).samefile(Path(recovery["target"])))

    def test_interrupt_during_rollback_reports_every_unrestored_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, source, catalog, settings, _plan = self._fixture(Path(temp))
            (source / "Sample 03.mkv").write_bytes(b"original media")
            plan = build_plan(root, catalog=catalog, settings=settings)
            calls = 0

            def fail_move_and_interrupt_one_rollback(source, target):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise PermissionError("move failed")
                if calls == 4:
                    raise KeyboardInterrupt()
                return _move_file_no_replace(source, target)

            with patch("media_directory_organizer.execution._move_file_no_replace", side_effect=fail_move_and_interrupt_one_rollback):
                with self.assertRaises(MediaRollbackError) as raised:
                    apply_plan(plan, confirmation=plan["plan_id"])
            payload = raised.exception.to_payload()
            self.assertEqual(calls, 5)
            self.assertFalse(payload["media"]["rollback_complete"])
            self.assertEqual(payload["media"]["moved_file_count"], 2)
            self.assertEqual(payload["media"]["rolled_back_file_count"], 1)
            self.assertEqual(len(payload["media"]["recovery_moves"]), 1)
            recovery = payload["media"]["recovery_moves"][0]
            self.assertEqual(recovery["target"], plan["moves"][1]["target"])
            self.assertEqual(recovery["error"], "KeyboardInterrupt")
            self.assertTrue(Path(recovery["target"]).is_file())
            self.assertFalse(Path(recovery["source"]).exists())
            self.assertTrue(Path(plan["moves"][0]["source"]).is_file())
            self.assertFalse(Path(plan["moves"][0]["target"]).exists())

    def test_exclusive_link_fallback_keeps_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mkv"
            target = Path(temp) / "target.mkv"
            source.write_bytes(b"keep source")
            original_unlink = Path.unlink

            def replace_target_before_failure(path, *args, **kwargs):
                if path == source:
                    original_unlink(target)
                    target.write_bytes(b"external replacement")
                    raise PermissionError("locked source")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", replace_target_before_failure):
                with self.assertRaises(_IncompleteFileMove):
                    _link_move_file_no_replace(source, target)
            self.assertEqual(target.read_bytes(), b"external replacement")
            self.assertEqual(source.read_bytes(), b"keep source")

    def test_exclusive_link_cleanup_failure_returns_partial_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            with (
                patch("media_directory_organizer.execution._move_file_no_replace", side_effect=_link_move_file_no_replace),
                patch.object(Path, "unlink", side_effect=PermissionError("locked files")),
            ):
                with self.assertRaises(MediaRollbackError) as raised:
                    apply_plan(plan, confirmation=plan["plan_id"])
            payload = raised.exception.to_payload()
            self.assertFalse(payload["media"]["rollback_complete"])
            self.assertEqual(payload["media"]["moved_file_count"], 0)
            self.assertEqual(payload["media"]["rolled_back_file_count"], 0)
            self.assertEqual(payload["media"]["recovery_moves"][0]["source"], plan["moves"][0]["source"])
            self.assertTrue(Path(plan["moves"][0]["source"]).is_file())
            self.assertTrue(Path(plan["moves"][0]["target"]).is_file())

    def test_destination_directory_is_created_once_for_shared_file_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
            target_parent = Path(plan["moves"][0]["target"]).parent
            self.assertEqual(target_parent, Path(plan["moves"][1]["target"]).parent)
            original_mkdir = Path.mkdir
            mkdir_calls = []

            def record_mkdir(path, *args, **kwargs):
                mkdir_calls.append(path)
                return original_mkdir(path, *args, **kwargs)

            target_parent.parent.mkdir(parents=True, exist_ok=True)
            with patch.object(Path, "mkdir", record_mkdir):
                result = apply_plan(plan, confirmation=plan["plan_id"])
            self.assertTrue(result["ok"])
            self.assertEqual(mkdir_calls.count(target_parent), 1)

    def test_cleanup_rejects_outside_directory_and_work_root(self) -> None:
        for outside_root in (True, False):
            with self.subTest(outside_root=outside_root), tempfile.TemporaryDirectory() as temp:
                root, _source, _catalog, _settings, plan = self._fixture(Path(temp))
                unreviewed = Path(temp) / "unrelated" if outside_root else root
                kept = unreviewed / "keep-empty"
                kept.mkdir(parents=True)
                plan["assignments"].append({"source_dir": str(unreviewed), "target_dir": str(root / "unused-target")})
                plan["plan_id"] = stable_plan_id(plan)
                result = apply_plan(plan, confirmation=plan["plan_id"])
                self.assertTrue(result["ok"])
                self.assertTrue(kept.is_dir())
                self.assertEqual(len(result["cleanup_warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
