"""Desktop-owned native dialogs; HTTP callers cannot create arbitrary UI code."""
from __future__ import annotations

from threading import Lock

_picker = None
_lock = Lock()


def set_directory_picker(picker) -> None:
    global _picker
    _picker = picker


def choose_directory(initial: str = "") -> str:
    if _picker is None:
        raise ValueError("系统目录选择仅在桌面应用中可用；浏览器模式请手动输入路径")
    if not _lock.acquire(blocking=False):
        raise ValueError("已有目录选择窗口打开，请先完成或取消选择")
    try:
        result = _picker(initial)
        return str(result or "")
    finally:
        _lock.release()
