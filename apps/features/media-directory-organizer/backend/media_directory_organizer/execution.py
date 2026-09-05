"""Execute reviewed media movement plans and report incomplete recovery.

Planning stays read-only in :mod:`service`; this module owns validation at
execution time, moves, rollback, and cleanup of emptied source directories.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

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
    for directory in reversed(descendants):
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
    checked = [_assert_move_unchanged(move, root=root) for move in moves]
    for _, target in checked:
        target.parent.mkdir(parents=True, exist_ok=True)

    completed: list[tuple[Path, Path]] = []
    try:
        for move in moves:
            source, target = _assert_move_unchanged(move, root=root)
            shutil.move(str(source), str(target))
            completed.append((source, target))
    except BaseException as exc:
        rollback_errors: list[str] = []
        recovery_moves: list[dict[str, str]] = []
        failed_move = {"source": str(move["source"]), "target": str(move["target"]), "error": str(exc)}
        for source, target in reversed(completed):
            try:
                assert_ordinary_path(source, root=root)
                assert_ordinary_path(target, root=root)
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() or source.is_symlink():
                    raise FileExistsError(str(source))
                shutil.move(str(target), str(source))
            except Exception as rollback_exc:
                rollback_errors.append(f"{target} -> {source}: {rollback_exc}")
                recovery_moves.append({
                    "source": str(source),
                    "target": str(target),
                    "error": str(rollback_exc),
                })
        if not isinstance(exc, Exception) and not rollback_errors:
            raise
        detail = (
            f"；回滚失败：{'；'.join(rollback_errors)}"
            if rollback_errors
            else "；已回滚本次已移动文件"
        )
        if rollback_errors:
            raise MediaRollbackError(
                f"移动失败：{exc}{detail}",
                plan_id=plan_id,
                root=str(root),
                recovery_moves=recovery_moves,
                moved_file_count=len(completed),
                rolled_back_file_count=len(completed) - len(recovery_moves),
                failed_move=failed_move,
            ) from exc
        raise OSError(f"移动失败：{exc}{detail}") from exc

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
        try:
            if path_key(source_dir) in preserved_target_keys:
                cleaned_directory_count += _remove_empty_descendants(source_dir)
            else:
                cleaned_directory_count += _remove_empty_source_tree(source_dir)
        except OSError as exc:
            cleanup_warnings.append(f"未能清理源目录 {source_dir}：{exc}")

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
