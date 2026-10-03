"""Path validation shared by catalog export commands."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath


_COPIED_PATH_MARKS = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"


def normalize_copied_path(value: object) -> str:
    """Remove Explorer copy wrappers without changing the actual path contents.

    Directional marks occasionally surround Windows copied paths; only strip
    these at the boundaries, together with paired quotation marks. Interior
    spaces, separators, and Unicode filename characters remain unchanged.
    """
    if not isinstance(value, str):
        return ""
    result = value
    while True:
        cleaned = result.strip().strip(_COPIED_PATH_MARKS).strip()
        if len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        if cleaned == result:
            return cleaned
        result = cleaned


def resolve_output_path(base: str | Path, *relative_paths: str) -> Path:
    """Resolve a file under base, rejecting absolute paths and path escapes.

    Windows drive, UNC and alternate-stream syntax is rejected on every host,
    so an exported catalog has the same meaning on Windows and other systems.
    Existing symbolic links/junctions must also resolve inside the output root.
    """
    root = Path(base).resolve()
    destination = root
    for raw in relative_paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("输出相对路径不能为空")
        normalized = raw.replace("\\", "/")
        windows_path = PureWindowsPath(normalized)
        if windows_path.drive or windows_path.root or ":" in normalized or "\0" in normalized:
            raise ValueError(f"输出路径必须是相对路径：{raw}")
        parts = normalized.split("/")
        if ".." in parts:
            raise ValueError(f"输出路径不能包含 ..：{raw}")
        destination = destination.joinpath(*(part for part in parts if part not in {"", "."}))
    destination = destination.resolve()
    if destination == root or not destination.is_relative_to(root):
        raise ValueError(f"输出路径必须位于输出目录内：{destination}")
    return destination
