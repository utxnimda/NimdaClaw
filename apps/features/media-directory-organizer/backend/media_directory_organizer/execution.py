"""Execute reviewed media movement plans and report incomplete recovery.

Planning stays read-only in :mod:`service`; this module owns validation at
execution time, moves, rollback, and cleanup of emptied source directories.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping
from work_catalog_yaml.operation_progress import report_progress

from media_directory_organizer.catalog import path_key
from media_directory_organizer.filesystem import assert_ordinary_path, is_reparse_point
from media_directory_organizer.plan_identity import stable_plan_id


class MediaRollbackError(OSError):
    """A failed move could not be fully undone; preserve catalog and recovery data."""

    def __init__(
        self,
        message: str,
        *,
        plan_id: str = "",
        root: str = "",
        recovery_moves: Iterable[Mapping[str, str]] = (),
        moved_file_count: int = 0,
        rolled_back_file_count: int = 0,
        failed_move: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.plan_id = plan_id
        self.root = root
        self.recovery_moves = [dict(row) for row in recovery_moves]
        self.moved_file_count = moved_file_count
        self.rolled_back_file_count = rolled_back_file_count
        self.failed_move = dict(failed_move or {})
        self.catalog_recovery: dict[str, Any] | None = None
        self.operation: dict[str, str] = {}

    def to_payload(self) -> dict[str, Any]:
        message = str(self)
        if self.catalog_recovery is not None:
            message += "；媒体未能完整回滚，数据库更改已保留，请核对文件及数据库历史后恢复"
        payload: dict[str, Any] = {
            "ok": False,
            "state": "partial",
            "error": message,
            "plan_id": self.plan_id,
            "root": self.root,
            "media": {
                "ok": False,
                "rollback_complete": False,
                "moved_file_count": self.moved_file_count,
                "rolled_back_file_count": self.rolled_back_file_count,
                "recovery_moves": self.recovery_moves,
                "failed_move": self.failed_move,
            },
            "recovery_required": True,
            "retry_requires_preview": True,
            **self.operation,
        }
        if self.catalog_recovery is not None:
            payload["catalog"] = self.catalog_recovery
        return payload


class _IncompleteFileMove(OSError):
    """A failed exclusive-link move may have left a destination to reconcile."""


def _link_move_file_no_replace(source: Path, target: Path) -> None:
    """Same-volume POSIX fallback; never replace an existing destination."""
    source_metadata = source.lstat()
    if not stat.S_ISREG(source_metadata.st_mode) or is_reparse_point(source):
        raise ValueError(f"只能移动普通文件：{source}")
    link_created = False
    try:
        os.link(source, target, follow_symlinks=False)
        link_created = True
        source.unlink()
    except BaseException as exc:
        if not link_created:
            # os.link may succeed just before an interrupt is delivered. There
            # is then no reliable ownership flag, so preserve any destination
            # and expose recovery evidence instead of guessing it is ours.
            if isinstance(exc, FileExistsError):
                raise
            try:
                target.lstat()
            except FileNotFoundError:
                raise exc
            except OSError:
                pass
            raise _IncompleteFileMove(
                f"目标链接创建结果未能确认：{str(exc) or type(exc).__name__}；请核对 {source} 与 {target}"
            ) from exc
        try:
            # Only remove the link we just created, not a concurrent replacement.
            target_metadata = target.lstat()
            if (
                not stat.S_ISREG(target_metadata.st_mode)
                or is_reparse_point(target)
                or (target_metadata.st_dev, target_metadata.st_ino)
                != (source_metadata.st_dev, source_metadata.st_ino)
                or not source.samefile(target)
            ):
                raise OSError(f"移动失败后目标文件已变化，需人工核对：{target}")
            target.unlink()
        except BaseException as cleanup_exc:
            raise _IncompleteFileMove(
                f"源文件未能删除：{str(exc) or type(exc).__name__}；"
                f"新增目标链接未能确认清理：{str(cleanup_exc) or type(cleanup_exc).__name__}"
            ) from exc
        raise


def _move_file_no_replace(source: Path, target: Path) -> None:
    """Move within the reviewed volume without overwrite or copy fallback.

    Windows rename fails atomically for an existing destination or a locked
    source. shutil.move instead falls back to copying after *any* rename error;
    that can overwrite a concurrent destination or leave an untracked copy.
    POSIX rename replaces destinations, so use an exclusive hard link there.
    A cross-volume move is deliberately rejected rather than copying media.
    """
    if os.name == "nt":
        os.rename(source, target)
    else:
        _link_move_file_no_replace(source, target)


def _assert_move_unchanged(move: dict[str, Any], *, root: Path) -> tuple[Path, Path]:
    source = Path(str(move["source"]))
    target = Path(str(move["target"]))
    assert_ordinary_path(source, root=root)
    assert_ordinary_path(target, root=root)
    if not source.is_file():
        raise FileNotFoundError(f"源文件已不存在：{source}")
    stat = source.stat()
    if stat.st_size != int(move["size"]) or stat.st_mtime_ns != int(move["mtime_ns"]):
        raise ValueError(f"预览后源文件发生变化，请重新生成计划：{source}")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"目标文件已存在，禁止覆盖：{target}")
    if source.name != target.name:
        raise ValueError(f"文件名变化，拒绝执行：{source.name} -> {target.name}")
    return source, target


def _remove_empty_descendants(source_dir: Path) -> int:
    if is_reparse_point(source_dir):
        raise OSError(f"拒绝清理符号链接或目录联接：{source_dir}")
    if not source_dir.exists():
        return 0
    removed_count = 0
    descendants: list[Path] = []
    for current, dirnames, _filenames in os.walk(source_dir, followlinks=False):
        safe_names = [
            name for name in dirnames if not is_reparse_point(Path(current) / name)
        ]
        dirnames[:] = safe_names
        descendants.extend(Path(current) / name for name in safe_names)
    for directory_index, directory in enumerate(reversed(descendants)):
        if directory_index % 100 == 0:
            report_progress("检查并清理空子目录", completed=directory_index, total=len(descendants), unit="目录", detail=str(directory))
        try:
            try:
                assert_ordinary_path(directory, root=source_dir)
            except ValueError:
                continue
            directory.rmdir()
            removed_count += 1
        except OSError:
            pass
    return removed_count


def _remove_empty_source_tree(source_dir: Path) -> int:
    if not source_dir.exists():
        return 0
    removed_count = _remove_empty_descendants(source_dir)
    source_dir.rmdir()
    return removed_count + 1


def apply_plan(plan: dict[str, Any], *, confirmation: str) -> dict[str, Any]:
    """Execute a previously reviewed plan. No destination file may be overwritten."""
    plan_id = str(plan.get("plan_id") or "")
    if plan_id and stable_plan_id(plan) != plan_id:
        raise ValueError("计划内容与计划 ID 不一致，拒绝执行")
    if not plan_id or confirmation.strip() != plan_id:
        raise ValueError(f"确认码不匹配；必须完整输入本次预览的计划 ID：{plan_id}")
    if not plan.get("ready"):
        raise ValueError("计划存在问题或没有可移动文件，拒绝执行")

    root = Path(str(plan["root"]))
    moves = plan.get("moves") or []
    checked = []
    for move_index, move in enumerate(moves):
        if move_index % 100 == 0:
            report_progress("重新校验预览后的文件状态", completed=move_index, total=len(moves), unit="文件", detail=str(move["source"]))
        checked.append(_assert_move_unchanged(move, root=root))
    # Episode folders often contain several videos/subtitles. Create each
    # destination directory once instead of issuing mkdir for every file.
    report_progress("准备媒体目标目录")
    for parent in dict.fromkeys(target.parent for _, target in checked):
        assert_ordinary_path(parent, root=root)
        parent.mkdir(parents=True, exist_ok=True)

    completed: list[tuple[Path, Path]] = []
    pending_move: tuple[Path, Path] | None = None
    try:
        for move_index, move in enumerate(moves):
            if move_index % 20 == 0:
                report_progress("移动已确认的媒体文件", completed=move_index, total=len(moves), unit="文件", detail=f"{move['source']} → {move['target']}")
            source, target = _assert_move_unchanged(move, root=root)
            pending_move = (source, target)
            _move_file_no_replace(source, target)
            completed.append((source, target))
            pending_move = None
    except BaseException as exc:
        report_progress("文件移动失败，正在回滚本次已移动文件", completed=0, total=len(completed), unit="文件", detail=str(exc))
        rollback_errors: list[str] = []
        recovery_moves: list[dict[str, str]] = []
        error_detail = str(exc) or type(exc).__name__
        failed_move = {"source": str(move["source"]), "target": str(move["target"]), "error": error_detail}
        incomplete_current_move = isinstance(exc, _IncompleteFileMove)
        if pending_move is not None and pending_move not in completed:
            try:
                # An interrupt may arrive after the OS renamed the file but
                # before Python records completion. Do not claim full rollback.
                pending_source, pending_target = pending_move
                incomplete_current_move = incomplete_current_move or (
                    not pending_source.exists() and pending_target.exists()
                )
            except OSError:
                incomplete_current_move = True
        if incomplete_current_move:
            rollback_errors.append(f"本次文件移动结果未能完整确认，需人工核对：{error_detail}")
            recovery_moves.append(dict(failed_move))
        rolled_back_file_count = 0
        for rollback_index, (source, target) in enumerate(reversed(completed)):
            if rollback_index % 20 == 0:
                report_progress("回滚已移动媒体文件", completed=rollback_index, total=len(completed), unit="文件", detail=f"{target} → {source}")
            try:
                assert_ordinary_path(source, root=root)
                assert_ordinary_path(target, root=root)
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() or source.is_symlink():
                    raise FileExistsError(str(source))
                _move_file_no_replace(target, source)
                rolled_back_file_count += 1
            except BaseException as rollback_exc:
                rollback_detail = str(rollback_exc) or type(rollback_exc).__name__
                rollback_errors.append(f"{target} -> {source}: {rollback_detail}")
                recovery_moves.append({
                    "source": str(source),
                    "target": str(target),
                    "error": rollback_detail,
                })
        report_progress("媒体回滚阶段结束", completed=rolled_back_file_count, total=len(completed), unit="文件", detail=f"回滚失败 {len(rollback_errors)} 项")
        if not isinstance(exc, Exception) and not rollback_errors:
            raise
        detail = (
            f"；回滚失败：{'；'.join(rollback_errors)}"
            if rollback_errors
            else "；已回滚本次已移动文件"
        )
        if rollback_errors:
            raise MediaRollbackError(
                f"移动失败：{error_detail}{detail}",
                plan_id=plan_id,
                root=str(root),
                recovery_moves=recovery_moves,
                moved_file_count=len(completed),
                rolled_back_file_count=rolled_back_file_count,
                failed_move=failed_move,
            ) from exc
        raise OSError(f"移动失败：{error_detail}{detail}") from exc

    report_progress("媒体文件移动完成", completed=len(completed), total=len(moves), unit="文件")
    source_dirs = {
        Path(str(row["source_dir"]))
        for row in plan.get("assignments") or []
        if isinstance(row, dict) and row.get("source_dir")
    }
    preserved_target_keys = {
        path_key(Path(str(row["target_dir"])))
        for row in plan.get("assignments") or []
        if isinstance(row, dict) and row.get("target_dir")
    }
    cleanup_warnings: list[str] = []
    cleaned_directory_count = 0
    for source_dir in sorted(
        source_dirs,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        report_progress("清理整理后的空目录", detail=str(source_dir))
        try:
            assert_ordinary_path(source_dir, root=root)
            if source_dir == root:
                raise ValueError("拒绝清理作品根目录")
            if path_key(source_dir) in preserved_target_keys:
                cleaned_directory_count += _remove_empty_descendants(source_dir)
            else:
                cleaned_directory_count += _remove_empty_source_tree(source_dir)
        except (OSError, ValueError) as exc:
            cleanup_warnings.append(f"未能清理源目录 {source_dir}：{exc}")

    report_progress("空目录清理结束", completed=cleaned_directory_count, unit="目录", detail=f"保留或清理失败提醒 {len(cleanup_warnings)} 项")
    return {
        "ok": True,
        "plan_id": plan_id,
        "moved_file_count": len(completed),
        "moved_bytes": sum(int(move["size"]) for move in plan.get("moves") or []),
        "cleaned_directory_count": cleaned_directory_count,
        "target_directories": sorted(
            {
                str(row["target_dir"])
                for row in plan.get("assignments") or []
            },
            key=str.casefold,
        ),
        "cleanup_warnings": cleanup_warnings,
    }


__all__ = ["MediaRollbackError", "apply_plan"]
