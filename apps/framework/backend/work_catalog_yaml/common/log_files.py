"""Filesystem safety shared by append-only application and operation logs."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from work_catalog_yaml.storage.filesystem import ordinary_directory

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def ordinary_log_file(metadata: os.stat_result) -> bool:
    return (stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
            and not (getattr(metadata, "st_file_attributes", 0) & _REPARSE)
            and metadata.st_nlink == 1)


def ensure_log_directory(path: Path) -> None:
    """Create missing components only beneath a verified ordinary path chain."""
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        if current.parent == current:
            raise OSError(f"日志根目录不可访问：{current}")
        current = current.parent
    if not ordinary_directory(str(current)):
        raise OSError(f"日志目录不是普通目录：{current}")
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        if not ordinary_directory(str(directory)):
            raise OSError(f"日志目录不是普通目录：{directory}")
