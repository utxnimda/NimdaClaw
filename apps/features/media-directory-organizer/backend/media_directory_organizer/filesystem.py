"""Filesystem boundary checks shared by previews and media execution."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def is_reparse_point(path: Path) -> bool:
    """Recognize symlinks and Windows reparse points without following them."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def assert_ordinary_path(path: Path, *, root: Path) -> None:
    """Reject paths escaping the reviewed root or traversing a link/junction."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"文件路径不位于作品根目录内：{path}") from exc
    if ".." in relative.parts:
        raise ValueError(f"文件路径不位于作品根目录内：{path}")
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if is_reparse_point(current):
            raise ValueError(f"文件路径经过符号链接或目录联接，请重新预览：{current}")
        if current != path and current.exists() and not current.is_dir():
            raise ValueError(f"文件路径的父目录已被普通文件占用：{current}")


def contains_regular_file(directory: Path) -> bool:
    """Look for media without traversing directory links or counting linked files."""

    try:
        if not directory.is_dir() or is_reparse_point(directory):
            return False
    except OSError:
        return False
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        current_path = Path(current)
        safe_names: list[str] = []
        for name in dirnames:
            try:
                if not is_reparse_point(current_path / name):
                    safe_names.append(name)
            except OSError:
                continue
        dirnames[:] = safe_names
        for name in filenames:
            try:
                child = current_path / name
                if child.is_file() and not is_reparse_point(child):
                    return True
            except OSError:
                continue
    return False
