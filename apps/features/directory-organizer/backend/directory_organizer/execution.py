"""Exclusive same-volume moves with append-only recovery events and rollback.

Each file intent is flushed before the move.  Completed events and bounded
summary records keep journal serialization and writes linear in file count;
the in-memory move list is retained only for immediate safe rollback.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from work_catalog_yaml.common.log_files import ensure_log_directory, ordinary_log_file
from work_catalog_yaml.operation_progress import report_exception, report_progress
from .snapshots import existing_directory, path_key, safe_target, signature, snapshot


def _same_file_identity(before, after):
    # Rename (and POSIX link/unlink) can change ctime, but must not change the
    # payload metadata, volume or file identity from the confirmed snapshot.
    return all(before[index] == after[index] for index in (0, 1, 3, 4))


class MediaExecution:
    def __init__(self, plan: dict, journal_root: Path):
        self.plan = plan
        self.source = Path(plan["source_path"])
        self.journal = journal_root / (plan["id"] + ".jsonl")
        self.moved: list[tuple[Path, Path, list[int]]] = []
        self.created: list[Path] = []
        self.receipt = {"plan_id": plan["id"], "source": str(self.source), "target": plan["target_path"],
                        "state": "prepared", "database": None, "rollback_count": 0}
        self._journal_identity: tuple[int, int] | None = None
        self._journal_sequence = 0
        self._journal_failure = ""

    def save_receipt(self, event="checkpoint", **details):
        """Append one durable event; never rewrite previous file/DB details.

        An existing journal is not silently resumed by a new execution object.
        File identity and ordinary-file checks also prevent replacing a receipt
        with a link between events.  A failed write poisons this journal so a
        partial last line cannot be followed by apparently valid new events.
        """
        if self._journal_failure:
            raise OSError("整理收据此前写入失败，未继续追加：" + self._journal_failure)
        row = {"version": 1, "sequence": self._journal_sequence + 1,
               "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "event": event, "plan_id": self.plan["id"], "state": self.receipt["state"],
               "source": str(self.source), "target": self.plan["target_path"],
               "planned_moves": len(self.plan["_moves"]), "moved_count": len(self.moved),
               "rollback_count": self.receipt["rollback_count"], "created_directory_count": len(self.created),
               **details}
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = None
        try:
            ensure_log_directory(self.journal.parent)
            initial = self._journal_identity is None
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            if initial:
                flags |= os.O_CREAT | os.O_EXCL
            elif not ordinary_log_file(self.journal.lstat()):
                raise OSError("整理收据不再是普通独占文件：" + str(self.journal))
            descriptor = os.open(self.journal, flags, 0o600)
            opened, visible = os.fstat(descriptor), self.journal.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if not ordinary_log_file(opened) or not ordinary_log_file(visible) or identity != (visible.st_dev, visible.st_ino):
                raise OSError("整理收据文件被链接或替换：" + str(self.journal))
            if not initial and identity != self._journal_identity:
                raise OSError("整理收据身份已变化，拒绝追加：" + str(self.journal))
            self._journal_identity = identity
            with os.fdopen(descriptor, "ab") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._journal_sequence += 1
        except (OSError, ValueError) as exc:
            self._journal_failure = str(exc)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _recovery_event(self, failures: list[dict], event: str, **details):
        """A journal failure must never prevent restoring an already moved file."""
        try:
            self.save_receipt(event, **details)
        except (OSError, ValueError) as exc:
            if not any(item.get("code") == "rollback-journal-failed" for item in failures):
                failures.append({"code": "rollback-journal-failed", "stage": "保存回滚收据",
                                 "message": f"回滚收据保存失败：{exc}", "path": str(self.journal), "level": "error"})
                report_exception(exc, context={"stage": "保存回滚收据", "path": str(self.journal)})

    def mkdir(self, path: Path):
        safe_target(path)
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            existing_directory(str(directory.parent))
            directory.mkdir(exist_ok=False)
            self.created.append(directory)

    @staticmethod
    def move_exclusive(source: Path, target: Path):
        existing_directory(str(source.parent))
        existing_directory(str(target.parent))
        if os.path.lexists(target):
            raise FileExistsError(f"目标已存在，未覆盖：{target}")
        if os.name == "nt":
            os.rename(source, target)  # Windows rename never overwrites a destination.
        else:
            # Same-volume exclusive publish for fixture/Linux use; unlike rename,
            # link fails atomically if another writer wins the target pathname.
            os.link(source, target, follow_symlinks=False)
            try:
                source.unlink()
            except BaseException:
                target.unlink()
                raise

    def run(self):
        current = snapshot(self.source)
        if current["fingerprint"] != self.plan["_snapshot"]["fingerprint"]:
            raise ValueError("来源目录已变化；请重新预览，尚未移动文件")
        safe_target(Path(self.plan["target_path"]))
        self.save_receipt("execution-start", source_fingerprint=current["fingerprint"])
        self.mkdir(Path(self.plan["target_path"]))
        moves = self.plan["_moves"]
        for index, move in enumerate(moves):
            source, target = Path(move["source"]), Path(move["target"])
            if signature(source) != move["signature"]:
                raise ValueError(f"来源文件已变化：{source}")
            self.mkdir(target.parent)
            self.receipt["state"] = "moving"
            pending = {"source": str(source), "target": str(target), "signature": move["signature"]}
            self.save_receipt("move-intent", index=index, file=pending)
            report_progress("移动已确认的资源", completed=index, total=len(moves), unit="文件",
                            context={"stage": "移动媒体", "source_path": str(source), "target_path": str(target)})
            self.move_exclusive(source, target)
            # Keep the pre-move signature authoritative for rollback.  The
            # destination may be replaced/edited immediately after rename;
            # adopting its new identity would authorize restoring foreign data.
            self.moved.append((source, target, move["signature"]))
            moved_signature = signature(target)
            if not _same_file_identity(move["signature"], moved_signature):
                raise ValueError(f"资源移动后目标身份或内容元数据发生变化，停止处理并保留现场：{target}")
            self.save_receipt("move-complete", index=index,
                              file={"source": str(source), "target": str(target), "signature": moved_signature})
        self.receipt["state"] = "media-complete"
        self.save_receipt("media-complete")

    def rollback(self) -> list[dict]:
        failures = []
        self.receipt["state"] = "rolling-back"
        self._recovery_event(failures, "rollback-start")
        for index, (source, target, expected) in enumerate(reversed(self.moved)):
            try:
                actual = signature(target)
                if not _same_file_identity(expected, actual):
                    raise ValueError("移动后的目标文件被外部修改，拒绝覆盖式回滚")
                self.mkdir(source.parent)
                self._recovery_event(failures, "rollback-intent", index=index,
                                     file={"source": str(target), "target": str(source), "signature": expected})
                self.move_exclusive(target, source)
                self.receipt["rollback_count"] += 1
                self._recovery_event(failures, "rollback-complete", index=index,
                                     file={"source": str(target), "target": str(source)})
            except Exception as exc:
                context = {"stage": "回滚媒体移动", "source_path": str(target), "target_path": str(source)}
                report_exception(exc, context=context)
                failures.append({**context, "message": str(exc), "level": "error"})
                self._recovery_event(failures, "rollback-file-failed", index=index, error=str(exc),
                                     file={"source": str(target), "target": str(source)})
        for directory in reversed(self.created):
            try:
                if directory.exists() and existing_directory(str(directory)):
                    directory.rmdir()  # Empty directories only; never recursively delete.
                    self._recovery_event(failures, "rollback-directory-removed", path=str(directory))
            except (OSError, ValueError) as exc:
                # Non-empty created folders may contain newly added user files;
                # preserve them.  Unsafe/replaced paths are explicitly reported.
                if isinstance(exc, ValueError):
                    context = {"stage": "回滚后清理空目录", "path": str(directory)}
                    report_exception(exc, context=context)
                    failures.append({**context, "message": str(exc), "level": "error"})
                    self._recovery_event(failures, "rollback-directory-failed", path=str(directory), error=str(exc))
        self.receipt["state"] = "rollback-incomplete" if failures else "rolled-back"
        self._recovery_event(failures, "rollback-finished", errors=failures.copy())
        return failures

    def complete(self, database: dict) -> list[dict]:
        self.receipt["database"] = database
        self.receipt["state"] = "database-complete"
        warnings = []
        # A receipt failure after DB commit must never trigger media rollback.
        try:
            self.save_receipt("database-committed", database=database)
        except (OSError, ValueError) as exc:
            warnings.append({"message": f"数据库已保存，但收据更新失败：{exc}", "path": str(self.journal), "level": "warning"})
        # Cleanup is a consequence of an actual move, not an independent scan
        # of every empty folder.  Pre-existing empty classification folders are
        # user data too, especially when this execution only repairs the DB.
        directories: set[Path] = set()
        source_root = Path(os.path.abspath(self.source))
        for moved_source, _target, _signature in self.moved:
            directory = Path(os.path.abspath(moved_source.parent))
            try:
                directory.relative_to(source_root)
            except ValueError:
                continue  # Never clean any ancestor outside the selected source.
            while True:
                directories.add(directory)
                if directory == source_root:
                    break
                directory = directory.parent
        target_root = Path(os.path.abspath(self.plan["target_path"]))
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            if path_key(directory) == path_key(target_root) or directory in target_root.parents:
                continue
            if not directory.exists():
                continue
            try:
                existing_directory(str(directory))
                directory.rmdir()
                try:
                    self.save_receipt("empty-directory-removed", path=str(directory))
                except (OSError, ValueError) as exc:
                    if not any(item.get("code") == "cleanup-journal-failed" for item in warnings):
                        warnings.append({"code": "cleanup-journal-failed", "message": f"空目录已清理，但收据记录失败：{exc}",
                                         "path": str(self.journal), "level": "warning"})
                report_progress("清理整理后空目录", context={"stage": "清理空目录", "path": str(directory)})
            except OSError as exc:
                # Non-empty means unrelated/new files appeared: always preserve.
                try:
                    empty = not any(directory.iterdir())
                except OSError:
                    empty = True
                if empty:
                    warnings.append({"message": f"空目录未能删除：{exc}", "path": str(directory), "level": "warning"})
            except ValueError as exc:
                warnings.append({"message": str(exc), "path": str(directory), "level": "warning"})
        self.receipt["state"] = "complete"
        try:
            self.save_receipt("execution-complete", warning_count=len(warnings))
        except (OSError, ValueError) as exc:
            if not any(item.get("path") == str(self.journal) for item in warnings):
                warnings.append({"message": f"处理已完成，但最终收据记录失败：{exc}", "path": str(self.journal), "level": "warning"})
        return warnings
