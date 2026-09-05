"""Path validation shared by catalog export commands."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath


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
