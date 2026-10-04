from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from directory_organizer.execution import MediaExecution
from directory_organizer.snapshots import existing_directory, signature, snapshot


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def execution(self, count=2, *, label="release"):
        source = self.base / (label + "-source")
        source.mkdir()
        for index in range(count):
            (source / f"episode-{index:04}.mkv").write_bytes(f"fixture-{index}".encode())
        facts = snapshot(source)
        target = self.base / (label + "-target")
        plan = {"id": str(uuid4()), "source_path": str(source), "target_path": str(target), "_snapshot": facts,
                "_moves": [{"source": str(source / item["relative_path"]),
                            "target": str(target / "_Disc" / item["relative_path"]), "signature": item["signature"]}
                           for item in facts["files"]]}
        return MediaExecution(plan, self.base / "receipts"), source, target

    @staticmethod
    def rows(media):
        return [json.loads(line) for line in media.journal.read_text(encoding="utf-8").splitlines()]

    def assert_restored(self, source, count):
        for index in range(count):
            self.assertEqual((source / f"episode-{index:04}.mkv").read_bytes(), f"fixture-{index}".encode())

    def test_run_emits_durable_intent_then_completion_without_growing_move_arrays(self):
        media, source, target = self.execution(3)
        original_move = MediaExecution.move_exclusive
        observed = []

        def checked_move(before, after):
            event = self.rows(media)[-1]
            self.assertEqual(event["event"], "move-intent")
            self.assertEqual(event["file"]["source"], str(before))
            self.assertEqual(event["file"]["target"], str(after))
            self.assertTrue(before.is_file())
            self.assertFalse(after.exists())
            observed.append(event["index"])
            original_move(before, after)

        with patch.object(MediaExecution, "move_exclusive", side_effect=checked_move):
            media.run()
        rows = self.rows(media)
        self.assertEqual(media.journal.suffix, ".jsonl")
        self.assertEqual(observed, [0, 1, 2])
        self.assertEqual([row["event"] for row in rows], ["execution-start", "move-intent", "move-complete", "move-intent", "move-complete", "move-intent", "move-complete", "media-complete"])
        self.assertEqual([row["sequence"] for row in rows], list(range(1, 9)))
        self.assertTrue(all("moves" not in row for row in rows))
        self.assertEqual(rows[-1]["moved_count"], 3)
        self.assertEqual(list(source.iterdir()), [])
        self.assertEqual(len(list((target / "_Disc").iterdir())), 3)

    def test_append_preserves_existing_bytes_and_log_size_grows_linearly(self):
        sizes = []
        for count in (8, 16, 32):
            media, _, _ = self.execution(count, label=f"scale-{count:02}")
            with patch("directory_organizer.execution.os.fsync"):
                media.run()
            data = media.journal.read_bytes()
            self.assertEqual(len(data.splitlines()), 2 * count + 2)
            sizes.append(len(data))
            with patch("directory_organizer.execution.os.fsync"):
                media.save_receipt("test-checkpoint")
            self.assertTrue(media.journal.read_bytes().startswith(data))
        self.assertLess(sizes[1], sizes[0] * 2.2)
        self.assertLess(sizes[2], sizes[1] * 2.2)

    def test_database_details_are_written_once_and_complete_cleans_empty_sources(self):
        media, source, target = self.execution()
        media.run()
        database = {"db_committed": True, "records": [{"name": "fixture", "extended": {"keep": [1, 2, 3]}}]}
        self.assertEqual(media.complete(database), [])
        rows = self.rows(media)
        self.assertEqual([row["database"] for row in rows if "database" in row], [database])
        self.assertEqual(rows[-1]["event"], "execution-complete")
        self.assertEqual(rows[-1]["state"], "complete")
        self.assertFalse(source.exists())
        self.assertTrue(target.is_dir())

    def test_no_media_moves_preserve_preexisting_empty_classification_directories(self):
        media, source, _target = self.execution(1)
        empty_cd = source / "_CD"
        empty_image = source / "_Image" / "Scans"
        empty_cd.mkdir()
        empty_image.mkdir(parents=True)
        media.plan["_snapshot"] = snapshot(source)
        media.plan["_moves"] = []
        media.plan["target_path"] = str(source)
        media.run()
        self.assertEqual(media.complete({"db_committed": True}), [])
        self.assertEqual(media.moved, [])
        self.assertTrue(empty_cd.is_dir())
        self.assertTrue(empty_image.is_dir())
        self.assertEqual((source / "episode-0000.mkv").read_bytes(), b"fixture-0")
        self.assertFalse(any(row["event"] == "empty-directory-removed" for row in self.rows(media)))
        self.assertEqual(self.rows(media)[-1]["state"], "complete")

    def test_partial_move_cleans_only_vacated_source_parents_and_preserves_unrelated_empty_folders(self):
        media, source, target = self.execution(2)
        moved_parent = source / "incoming" / "episodes"
        moved_parent.mkdir(parents=True)
        moved_file = moved_parent / "episode-0000.mkv"
        (source / moved_file.name).rename(moved_file)
        unrelated_empty = [source / "_Image", source / "incoming" / "_CD"]
        for directory in unrelated_empty:
            directory.mkdir()
        facts = snapshot(source)
        media.plan["_snapshot"] = facts
        media.plan["_moves"] = [{"source": str(moved_file), "target": str(target / "_Disc" / moved_file.name),
                                  "signature": signature(moved_file)}]
        media.run()
        self.assertEqual(media.complete({"db_committed": True}), [])
        self.assertFalse(moved_parent.exists())
        self.assertTrue(all(directory.is_dir() for directory in unrelated_empty))
        self.assertTrue((source / "incoming").is_dir())
        self.assertEqual((source / "episode-0001.mkv").read_bytes(), b"fixture-1")
        self.assertEqual((target / "_Disc" / moved_file.name).read_bytes(), b"fixture-0")
        removed = [row["path"] for row in self.rows(media) if row["event"] == "empty-directory-removed"]
        self.assertEqual(removed, [str(moved_parent)])

    def test_failed_intent_does_not_move_resource(self):
        media, source, _ = self.execution()
        original_save = media.save_receipt

        def failing(event="checkpoint", **details):
            if event == "move-intent":
                raise OSError("fixture intent journal denied")
            return original_save(event, **details)

        with patch.object(media, "save_receipt", side_effect=failing), self.assertRaisesRegex(OSError, "intent journal"):
            media.run()
        self.assertEqual(media.moved, [])
        self.assert_restored(source, 2)
        self.assertEqual(media.rollback(), [])
        self.assertEqual(self.rows(media)[-1]["state"], "rolled-back")

    def test_second_file_failure_logs_first_file_rollback(self):
        media, source, target = self.execution()
        original_move = MediaExecution.move_exclusive
        calls = 0

        def failing(before, after):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("fixture move denied")
            return original_move(before, after)

        with patch.object(MediaExecution, "move_exclusive", side_effect=failing):
            with self.assertRaisesRegex(PermissionError, "move denied"):
                media.run()
            failures = media.rollback()
        self.assertEqual(failures, [])
        self.assert_restored(source, 2)
        self.assertFalse(target.exists())
        events = self.rows(media)
        self.assertEqual(sum(row["event"] == "rollback-intent" for row in events), 1)
        self.assertEqual(sum(row["event"] == "rollback-complete" for row in events), 1)
        self.assertEqual(events[-1]["state"], "rolled-back")
        self.assertEqual(events[-1]["rollback_count"], 1)

    def test_journal_fsync_failure_after_move_never_prevents_media_rollback(self):
        media, source, _ = self.execution()
        calls = 0
        original_fsync = os.fsync

        def failing(descriptor):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("fixture log disk unavailable")
            return original_fsync(descriptor)

        with patch("directory_organizer.execution.os.fsync", side_effect=failing):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                media.run()
            failures = media.rollback()
        self.assert_restored(source, 2)
        self.assertEqual(sum(row.get("code") == "rollback-journal-failed" for row in failures), 1)
        self.assertFalse(any(row.get("stage") == "回滚媒体移动" for row in failures))

    def test_invalid_created_directory_does_not_hide_original_failure_or_stop_receipt(self):
        media, source, _ = self.execution()
        media.run()
        replaced = self.base / "replaced-empty-directory"
        replaced.mkdir()
        media.created.append(replaced)

        def checked(raw):
            if Path(raw) == replaced:
                raise ValueError("fixture created directory became a junction")
            return existing_directory(raw)

        with patch("directory_organizer.execution.existing_directory", side_effect=checked):
            failures = media.rollback()
        self.assert_restored(source, 2)
        self.assertTrue(replaced.is_dir())
        self.assertTrue(any("became a junction" in row["message"] for row in failures))
        self.assertEqual(self.rows(media)[-1]["event"], "rollback-finished")
        self.assertEqual(self.rows(media)[-1]["state"], "rollback-incomplete")

    def test_changed_target_is_preserved_and_failure_is_journaled(self):
        media, source, target = self.execution()
        media.run()
        changed = target / "_Disc" / "episode-0001.mkv"
        changed.write_bytes(b"external modification must survive")
        failures = media.rollback()
        self.assertTrue(failures)
        self.assertEqual(changed.read_bytes(), b"external modification must survive")
        self.assertEqual((source / "episode-0000.mkv").read_bytes(), b"fixture-0")
        self.assertTrue(any(row["event"] == "rollback-file-failed" for row in self.rows(media)))

    def test_target_modified_between_move_and_stat_is_not_adopted_for_rollback(self):
        media, source, target = self.execution(1)
        original_move = MediaExecution.move_exclusive

        def externally_changed(before, after):
            original_move(before, after)
            after.write_bytes(b"external content written immediately after rename")

        with patch.object(MediaExecution, "move_exclusive", side_effect=externally_changed):
            with self.assertRaisesRegex(ValueError, "移动后目标身份或内容元数据发生变化"):
                media.run()
        self.assertEqual(media.moved[0][2], media.plan["_moves"][0]["signature"])
        failures = media.rollback()
        self.assertTrue(failures)
        self.assertEqual((target / "_Disc" / "episode-0000.mkv").read_bytes(), b"external content written immediately after rename")
        self.assertFalse((source / "episode-0000.mkv").exists())
        self.assertFalse(any(row["event"] == "move-complete" for row in self.rows(media)))

    def test_only_ctime_change_is_allowed_and_original_identity_drives_rollback(self):
        media, source, target = self.execution(1)

        def changed_ctime(path):
            value = signature(path)
            if target in path.parents:
                value[2] += 1000
            return value

        with patch("directory_organizer.execution.signature", side_effect=changed_ctime):
            media.run()
            self.assertEqual(media.moved[0][2], media.plan["_moves"][0]["signature"])
            self.assertEqual(media.rollback(), [])
        self.assert_restored(source, 1)

    def test_existing_journal_is_never_overwritten_or_resumed_implicitly(self):
        media, source, target = self.execution()
        media.journal.parent.mkdir()
        media.journal.write_bytes(b"previous recovery receipt\n")
        with self.assertRaises(FileExistsError):
            media.run()
        self.assertEqual(media.journal.read_bytes(), b"previous recovery receipt\n")
        self.assert_restored(source, 2)
        self.assertFalse(target.exists())

    def test_replaced_journal_identity_is_rejected_without_foreign_write(self):
        media, _, _ = self.execution()
        media.save_receipt("initial")
        media.journal.rename(media.journal.with_suffix(".previous"))
        media.journal.write_bytes(b"unrelated replacement\n")
        with self.assertRaisesRegex(OSError, "身份已变化"):
            media.save_receipt("second")
        self.assertEqual(media.journal.read_bytes(), b"unrelated replacement\n")

    def test_hardlinked_journal_is_rejected(self):
        media, _, _ = self.execution()
        media.save_receipt("initial")
        original = media.journal.read_bytes()
        os.link(media.journal, self.base / "other-receipt-link")
        with self.assertRaisesRegex(OSError, "普通独占文件"):
            media.save_receipt("second")
        self.assertEqual(media.journal.read_bytes(), original)

    def test_post_commit_journal_error_is_warning_and_does_not_restore_media(self):
        media, source, target = self.execution()
        media.run()
        with patch.object(media, "save_receipt", side_effect=ValueError("fixture unsafe receipt path")):
            warnings = media.complete({"db_committed": True})
        self.assertTrue(warnings)
        self.assertTrue((target / "_Disc" / "episode-0000.mkv").is_file())
        self.assertFalse((source / "episode-0000.mkv").exists())


if __name__ == "__main__":
    unittest.main()
