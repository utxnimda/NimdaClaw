"""Read-only filesystem facts shared by catalog and disk projections."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any


def path_compare_key(raw: Any) -> str:
    s = str(raw) if isinstance(raw, Path) else (raw.strip() if isinstance(raw, str) else "")
    if not s:
        return ""
    try:
        return os.path.normcase(str(Path(s).expanduser().resolve()))
    except OSError:
        return os.path.normcase(str(Path(s).expanduser()))


def path_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def ordinary_directory(raw: str, *, cache: dict[Path, bool] | None = None) -> bool:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        return False
    try:
        # Reject an unsafe ancestor before probing any child through it.
        for current in reversed((path, *path.parents)):
            ordinary = cache.get(current) if cache is not None else None
            if ordinary is None:
                metadata = current.lstat()
                ordinary = bool(stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and not (
                    getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ))
                if cache is not None:
                    cache[current] = ordinary
            if not ordinary:
                return False
    except OSError:
        return False
    return True
