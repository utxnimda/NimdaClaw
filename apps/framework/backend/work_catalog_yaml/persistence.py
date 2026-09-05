"""Atomic local-file writes shared by editable YAML features.

Callers hold ``directory_write_transaction`` across read/modify/commit. A batch
is preflighted in full and rolls back completed replacements on errors or interruption;
each individual replacement is atomic, not the entire multi-file batch.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence


_WRITE_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


def _file_lock(directory: Path, filename: str, timeout_seconds: float) -> BinaryIO:
    path = directory / filename
    stream = path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return stream
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"数据正被另一个进程写入，等待文件锁超时：{path}") from exc
                time.sleep(0.05)
    except BaseException:
        stream.close()
        raise


@contextmanager
def directory_write_transaction(
    directory: Path,
    *,
    lock_filename: str = ".nimda-catalog.lock",
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Reentrant thread/process lock for cooperating writers in one directory."""
    root = directory.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"数据目录不存在：{root}")
    if Path(lock_filename).name != lock_filename or lock_filename in {"", ".", ".."}:
        raise ValueError("lock_filename must be a plain filename")
    key = (os.path.normcase(str(root)), lock_filename)
    with _WRITE_LOCK:
        states = getattr(_LOCK_STATE, "roots", None)
        if states is None:
            states = {}
            _LOCK_STATE.roots = states
        if key not in states:
            states[key] = {"depth": 0, "stream": _file_lock(root, lock_filename, timeout_seconds)}
        state = states[key]
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1
            if state["depth"] == 0:
                states.pop(key)
                stream = state["stream"]
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()


def history_snapshot_name(path: Path, *, now: datetime | None = None) -> str:
    stamp = f"{now or datetime.now():%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}"
    return f"{path.stem}__saved-{stamp}{path.suffix}"


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Replace one file after its complete contents have reached a sibling temp file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_file():
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class FileWrite:
    target: Path
    content: bytes
    previous: bytes | None
    history_path: Path | None = None


class PersistenceRollbackError(OSError):
    """A failed batch could not fully restore its original files."""


def _current_bytes(target: Path) -> bytes | None:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"写入目标不是普通文件：{target}")
    return target.read_bytes() if target.is_file() else None


def commit_file_writes(writes: Sequence[FileWrite]) -> None:
    """Preflight all inputs; roll back our replacements without clobbering later writes."""
    keys: set[str] = set()
    for write in writes:
        key = os.path.normcase(str(write.target.resolve()))
        if key in keys:
            raise ValueError(f"批次包含重复写入目标：{write.target}")
        keys.add(key)
        if _current_bytes(write.target) != write.previous:
            raise ValueError(f"数据在写入前发生变化，请重新加载：{write.target}")
    # Finish every recovery snapshot before replacing any live file.
    for write in writes:
        if write.history_path is not None and write.previous is not None:
            write.history_path.parent.mkdir(parents=True, exist_ok=True)
            with write.history_path.open("xb") as stream:
                stream.write(write.previous)
                stream.flush()
                os.fsync(stream.fileno())
    completed: list[FileWrite] = []
    try:
        for write in writes:
            if _current_bytes(write.target) != write.previous:
                raise ValueError(f"数据在写入前发生变化，请重新加载：{write.target}")
            atomic_write_bytes(write.target, write.content)
            completed.append(write)
    except BaseException as failure:
        rollback_errors: list[str] = []
        for write in reversed(completed):
            try:
                if _current_bytes(write.target) != write.content:
                    raise ValueError("存在后续改动，拒绝覆盖")
                if write.previous is None:
                    write.target.unlink()
                else:
                    atomic_write_bytes(write.target, write.previous)
            except Exception as exc:
                rollback_errors.append(f"{write.target}: {exc}")
        if rollback_errors:
            raise PersistenceRollbackError("保存失败且部分文件无法自动恢复：" + "; ".join(rollback_errors)) from failure
        raise
