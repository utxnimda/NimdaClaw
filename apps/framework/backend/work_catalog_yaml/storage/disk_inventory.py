"""Memoized, read-only directory probes; no scans, link resolution or writes."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any


def lexical_path(raw: str | Path) -> Path:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise ValueError("目录必须是没有上级跳转的绝对路径")
    return Path(os.path.abspath(path))


def inventory_path_key(raw: str | Path) -> str:
    return os.path.normcase(str(lexical_path(raw)))


class DiskInventory:
    """One-request snapshot of physical facts, including unavailable roots.

    lstat is used from the filesystem anchor down to the requested directory so
    a junction/symlink is rejected before any of its children can be followed.
    Missing and inaccessible directories are deliberately distinct states.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, Any]] = {}
        self._probes: dict[str, dict[str, Any]] = {}

    def _node(self, path: Path) -> dict[str, Any]:
        key = inventory_path_key(path)
        if key in self._nodes:
            return self._nodes[key]
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                result = {"state": "unsafe", "reason": "symlink-or-reparse-point"}
            elif not stat.S_ISDIR(metadata.st_mode):
                result = {"state": "unsafe", "reason": "not-an-ordinary-directory"}
            else:
                result = {"state": "present", "reason": "ordinary-directory"}
        except FileNotFoundError:
            result = {"state": "missing", "reason": "directory-missing"}
        except (OSError, ValueError) as error:
            result = {"state": "unavailable", "reason": "directory-unavailable", "error": str(error)}
        self._nodes[key] = result
        return result

    def probe(self, raw: str | Path) -> dict[str, Any]:
        try:
            path = lexical_path(raw)
            key = inventory_path_key(path)
        except (OSError, TypeError, ValueError) as error:
            return {"path": str(raw), "state": "unsafe", "reason": "invalid-path", "error": str(error)}
        if key not in self._probes:
            result: dict[str, Any] = {"path": str(path), "state": "present", "reason": "ordinary-directory"}
            for current in (*reversed(path.parents), path):
                node = self._node(current)
                if node["state"] != "present":
                    result.update(node)
                    result["observed_path"] = str(current)
                    break
            self._probes[key] = result
        return dict(self._probes[key])
