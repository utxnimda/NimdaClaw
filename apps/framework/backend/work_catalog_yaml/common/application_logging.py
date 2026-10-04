"""Shared application logging, appended into local-calendar-day directories."""
from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import re
import threading
from typing import Callable, TextIO

from work_catalog_yaml.common.log_files import (
    ensure_log_directory as _ensure_directory,
    ordinary_log_file as _ordinary_file,
)
from work_catalog_yaml.common.log_format import UnifiedLogFormatter

_CONFIGURE_LOCK = threading.RLock()


def _open_append(path: Path) -> TextIO:
    _ensure_directory(path.parent)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not _ordinary_file(before):
        raise OSError(f"日志文件必须是普通文件：{path}")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        after = os.fstat(descriptor)
        if not _ordinary_file(after) or (before is not None and
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise OSError("日志文件在打开期间已变化")
        return os.fdopen(descriptor, "a", encoding="utf-8", errors="backslashreplace", newline="\n")
    except BaseException:
        os.close(descriptor)
        raise


class DailyApplicationFileHandler(logging.Handler):
    """One process-owned handler; each day appends, never renames or deletes logs."""
    def __init__(self, root: Path, filename: str, *, clock: Callable[[], datetime] | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.log", filename):
            raise ValueError("日志名称必须是普通 .log 文件名")
        super().__init__()
        # Do not resolve away a junction or symbolic link before validating it.
        self.root = Path(os.path.abspath(root))
        self.filename = filename
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.stream: TextIO | None = None
        self.baseFilename = ""
        try:
            self.ensure_current()
        except BaseException:
            self.close()
            raise

    def ensure_current(self) -> Path:
        with self.lock:
            if self._closed:
                raise ValueError("日志处理器已关闭")
            path = self.root / self._clock().strftime("%Y-%m-%d") / self.filename
            if str(path) != self.baseFilename:
                stream = _open_append(path)
                previous = self.stream
                self.stream, self.baseFilename = stream, str(path)
                if previous is not None:
                    previous.close()
            return path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self.lock:
                if self._closed:
                    return
                self.ensure_current()
                assert self.stream is not None
                if not _ordinary_file(os.fstat(self.stream.fileno())):
                    raise OSError("日志文件不再是独立普通文件")
                self.stream.write(self.format(record) + "\n")
                self.stream.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        with self.lock:
            if self.stream is not None:
                self.stream.flush()

    def close(self) -> None:
        with self.lock:
            try:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
            finally:
                super().close()


def configure_application_logging(root: Path, *, filename: str = "application.log",
                                  logger: logging.Logger | None = None,
                                  clock: Callable[[], datetime] | None = None) -> DailyApplicationFileHandler:
    """Reuse one handler for this root/name, without changing unrelated handlers."""
    target = logger if logger is not None else logging.getLogger()
    absolute = Path(os.path.abspath(root))
    with _CONFIGURE_LOCK:
        target.setLevel(logging.INFO)
        for handler in target.handlers:
            if (isinstance(handler, DailyApplicationFileHandler) and not handler._closed
                    and handler.root == absolute and handler.filename == filename):
                handler.ensure_current()
                return handler
        handler = DailyApplicationFileHandler(absolute, filename, clock=clock)
        handler.setFormatter(UnifiedLogFormatter())
        target.addHandler(handler)
        return handler
