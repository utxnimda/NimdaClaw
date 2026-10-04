"""Human-readable operation logs backed by structured recovery sidecars.

All names are server-generated, all reads are ID-based, and link/reparse chains
are rejected. Listing and reads are bounded; no permanent in-memory log index.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime
import heapq
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, BinaryIO, Iterator
from uuid import UUID, uuid4

from work_catalog_yaml.storage.filesystem import ordinary_directory
from work_catalog_yaml.common.log_files import (
    ensure_log_directory as _ensure_directory,
    ordinary_log_file as _ordinary_file,
)
from work_catalog_yaml.common.log_format import format_operation_entry

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_NAME = re.compile(r"\d{6}-\d{6}_[0-9a-f-]{36}\.jsonl\Z")
_CURSOR = re.compile(r"\d{4}-\d{2}-\d{2}/\d{6}-\d{6}_[0-9a-f-]{36}\.jsonl\Z")
_TAIL_BYTES = 1024 * 1024


class DuplicateLogId(ValueError):
    pass


def _uuid(raw: str) -> str:
    normalized = str(UUID(raw))
    if raw != normalized:
        raise ValueError("操作 ID 必须是有效 UUID")
    return normalized


class OperationLogStore:
    def __init__(self, root: Path) -> None:
        # Do not resolve: retain the original chain so junctions cannot vanish.
        self.root = Path(os.path.abspath(root))
        self.session_id = str(uuid4())
        self._lock = threading.RLock()
        self._paths: OrderedDict[str, Path] = OrderedDict()
        self._index_ready = False
        self._text_states: OrderedDict[Path, tuple[int, int, int, int]] = OrderedDict()
        self._search_states: OrderedDict[tuple[str, str, int], dict[str, Any]] = OrderedDict()

    def _index_path(self, operation_id: str) -> Path:
        return self.root / ".lookup" / operation_id[:2] / f"{operation_id}.json"

    def _read_index(self, operation_id: str) -> Path | None:
        index = self._index_path(operation_id)
        if not index.exists() and not index.is_symlink():
            return None
        with self._open_regular(index) as stream:
            payload = json.loads(stream.read(1024))
        relative = payload.get("path") if isinstance(payload, dict) else None
        if not isinstance(relative, str) or not _CURSOR.fullmatch(relative) or not relative.endswith(f"_{operation_id}.jsonl"):
            raise ValueError("操作日志索引无效")
        return self._remember(operation_id, self.root.joinpath(*relative.split("/")))

    def _write_index(self, operation_id: str, path: Path) -> None:
        index = self._index_path(operation_id)
        self._ensure_directory(index.parent)
        encoded = json.dumps({"path": path.relative_to(self.root).as_posix()}, separators=(",", ":")).encode("utf-8")
        descriptor = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def _complete_index(self) -> bool:
        if self._index_ready:
            return True
        marker = self.root / ".lookup" / ".complete"
        if not marker.exists() and not marker.is_symlink():
            return False
        with self._open_regular(marker) as stream:
            self._index_ready = stream.read(32) == b"nimda-operation-index-v1\n"
        if not self._index_ready:
            raise ValueError("操作日志索引完成标记无效")
        return self._index_ready

    def _ensure_index(self) -> None:
        if self._complete_index():
            return
        self._ensure_directory(self.root / ".lookup")
        # One recoverable migration for older logs. The completion marker is
        # written only after every existing log has its UUID-sharded pointer.
        for path in self._files():
            operation_id = path.name[14:-6]
            if self._read_index(operation_id) is None:
                self._write_index(operation_id, path)
        marker = self.root / ".lookup" / ".complete"
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"nimda-operation-index-v1\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._index_ready = True

    def _remember(self, operation_id: str, path: Path) -> Path:
        self._paths[operation_id] = path
        self._paths.move_to_end(operation_id)
        while len(self._paths) > 256:
            self._paths.popitem(last=False)
        return path

    def _ensure_directory(self, path: Path) -> None:
        _ensure_directory(path)

    def _files(self) -> Iterator[Path]:
        if not self.root.exists() and not self.root.is_symlink():
            return
        if not ordinary_directory(str(self.root)):
            raise OSError("日志根目录含符号链接或重解析点")
        with os.scandir(self.root) as dates:
            for date in dates:
                if not _DATE.fullmatch(date.name):
                    continue
                directory = Path(date.path)
                if not ordinary_directory(str(directory)):
                    continue
                with os.scandir(directory) as files:
                    for entry in files:
                        if _NAME.fullmatch(entry.name):
                            path = Path(entry.path)
                            # Windows DirEntry.stat may expose st_nlink=0; a
                            # fresh lstat provides the real hardlink metadata.
                            if _ordinary_file(path.lstat()):
                                yield path

    def find(self, operation_id: str) -> Path | None:
        operation_id = _uuid(operation_id)
        with self._lock:
            cached = self._paths.get(operation_id)
            if cached is not None:
                return cached
            indexed = self._read_index(operation_id)
            if indexed is not None:
                return indexed
            if self._complete_index():
                return None
            suffix = f"_{operation_id}.jsonl"
            for path in self._files():
                if path.name.endswith(suffix):
                    return self._remember(operation_id, path)
        return None

    def create(self, operation_id: str) -> Path:
        operation_id = _uuid(operation_id)
        with self._lock:
            self._ensure_index()
            if self.find(operation_id) is not None:
                raise DuplicateLogId("操作日志已存在")
            now = datetime.now().astimezone()
            directory = self.root / now.strftime("%Y-%m-%d")
            self._ensure_directory(directory)
            path = directory / f"{now:%H%M%S}-{now.microsecond:06d}_{operation_id}.jsonl"
            # Reserve the index first: crashes can leave a reserved unused ID,
            # but can never leave a successfully written log undiscoverable.
            try:
                self._write_index(operation_id, path)
            except FileExistsError:
                raise DuplicateLogId("操作日志已存在") from None
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            os.close(descriptor)
            self._remember(operation_id, path)
            return self._ensure_text_current(path)

    def _remember_text_state(self, machine: Path, readable: Path) -> None:
        source, target = machine.lstat(), readable.lstat()
        self._text_states[machine] = (source.st_size, source.st_mtime_ns, target.st_size, target.st_mtime_ns)
        self._text_states.move_to_end(machine)
        while len(self._text_states) > 128:
            self._text_states.popitem(last=False)

    def _ensure_text_current(self, machine: Path) -> Path:
        readable = machine.with_suffix(".log")
        with self._open(machine) as source:
            metadata = os.fstat(source.fileno())
            try:
                with self._open_regular(readable) as target:
                    target_metadata = os.fstat(target.fileno())
                state = (metadata.st_size, metadata.st_mtime_ns, target_metadata.st_size, target_metadata.st_mtime_ns)
                if self._text_states.get(machine) == state:
                    return readable
            except FileNotFoundError:
                pass
            # First access after restart (or an interrupted dual write) rebuilds
            # only the readable projection. Original JSONL data stays untouched.
            temporary = readable.with_name(f"{readable.name}.{uuid4()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    for line in source:
                        try:
                            payload = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(payload, dict):
                            output.write(format_operation_entry(payload).encode("utf-8"))
                    output.flush()
                    os.fsync(output.fileno())
                if not ordinary_directory(str(readable.parent)):
                    raise OSError("日志目录含符号链接或重解析点")
                if readable.exists() and not _ordinary_file(readable.lstat()):
                    raise OSError("日志文件必须是普通文件")
                os.replace(temporary, readable)
            finally:
                if temporary.exists():
                    temporary.unlink()
        self._remember_text_state(machine, readable)
        return readable

    def safe_log_path(self, operation_id: str) -> Path | None:
        """Return only a verified UUID-owned .log, materializing legacy logs."""
        with self._lock:
            machine = self.find(operation_id)
            if machine is None:
                return None
            readable = self._ensure_text_current(machine)
            with self._open_regular(readable):
                pass
            return readable

    def open_text_file(self, operation_id: str) -> tuple[Path, BinaryIO] | None:
        """Return an already-validated .log handle; the caller must close it."""
        with self._lock:
            readable = self.safe_log_path(operation_id)
            return None if readable is None else (readable, self._open_regular(readable))

    def _open(self, path: Path, *, append: bool = False):
        if path.parent.parent != self.root or not _DATE.fullmatch(path.parent.name) or not _NAME.fullmatch(path.name):
            raise ValueError("无效日志文件")
        return self._open_regular(path, append=append)

    def _open_regular(self, path: Path, *, append: bool = False):
        if not path.is_relative_to(self.root):
            raise ValueError("日志路径超出根目录")
        if not ordinary_directory(str(path.parent)):
            raise OSError("日志目录含符号链接或重解析点")
        before = path.lstat()
        if not _ordinary_file(before):
            raise OSError("日志文件必须是普通文件")
        flags = (os.O_WRONLY | os.O_APPEND) if append else os.O_RDONLY
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        after = os.fstat(descriptor)
        if not _ordinary_file(after) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(descriptor)
            raise OSError("日志文件在读取期间已变化")
        return os.fdopen(descriptor, "ab" if append else "rb")

    def append(self, operation_id: str, snapshot: dict[str, Any], event: dict[str, Any], *, traceback: str = "") -> None:
        with self._lock:
            path = self.find(operation_id)
            if path is None:
                raise FileNotFoundError("未找到操作日志")
            readable = self._ensure_text_current(path)
            payload = {"schema": 1, "session_id": self.session_id, "operation": snapshot, "event": event}
            if traceback:
                payload["traceback"] = traceback[-32768:]
            encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            with self._open(path, append=True) as stream:
                stream.write(encoded)
                stream.flush()
                if snapshot.get("finished_at"):
                    os.fsync(stream.fileno())
            with self._open_regular(readable, append=True) as stream:
                stream.write(format_operation_entry(payload).encode("utf-8"))
                stream.flush()
                if snapshot.get("finished_at"):
                    os.fsync(stream.fileno())
            self._remember_text_state(path, readable)

    def append_details(self, operation_id: str, details) -> None:
        """Stream full diagnostics without multiplying the bounded snapshot."""
        with self._lock:
            path = self.find(operation_id)
            if path is None:
                raise FileNotFoundError("未找到操作日志")
            readable = self._ensure_text_current(path)
            at = datetime.now().astimezone().isoformat(timespec="milliseconds")
            with self._open(path, append=True) as stream, self._open_regular(readable, append=True) as human:
                for item in details:
                    payload = {"schema": 1, "at": at, "operation_id": operation_id, "kind": "result_detail", "detail": item}
                    stream.write((json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
                    human.write(format_operation_entry(payload).encode("utf-8"))
                stream.flush()
                human.flush()
            self._remember_text_state(path, readable)

    def _snapshot(self, path: Path, *, max_events: int = 200) -> dict[str, Any] | None:
        with self._open(path) as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size - _TAIL_BYTES)
            stream.seek(start)
            content = stream.read(_TAIL_BYTES)
        lines = content.splitlines()
        if start and lines:
            lines = lines[1:]
        latest = None
        events: deque[dict[str, Any]] = deque(maxlen=max_events)
        for line in lines:
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # An interrupted final write must not hide older events.
            if not isinstance(item, dict) or not isinstance(item.get("operation"), dict):
                continue
            latest = item
            if isinstance(item.get("event"), dict):
                events.append(item["event"])
        if latest is None and start:
            # A crash may interrupt a large diagnostic stream before its final
            # snapshot. Recover the initial operation rather than hiding it.
            with self._open(path) as stream:
                initial = stream.read(65536)
            for line in initial.splitlines():
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(item, dict) and isinstance(item.get("operation"), dict):
                    latest = item
                    if isinstance(item.get("event"), dict):
                        events.append(item["event"])
        if latest is None:
            return None
        snapshot = latest["operation"]
        expected_id = path.name[14:-6]
        if snapshot.get("id") != expected_id:
            raise ValueError("日志 ID 与文件不一致")
        snapshot["events"] = list(events)
        snapshot["log"] = {"path": str(path.with_suffix(".log")), "machine_path": str(path), "available": True, "error": ""}
        if snapshot.get("status") in {"queued", "running"} and latest.get("session_id") != self.session_id:
            snapshot["status"] = "warning"
            snapshot["message"] = "上次运行未记录完成状态；应用可能中断，请核对实际处理结果。"
            snapshot["interrupted"] = True
        return snapshot

    def snapshot(self, operation_id: str, *, max_events: int = 200) -> dict[str, Any] | None:
        with self._lock:
            path = self.find(operation_id)
            return None if path is None else self._snapshot(path, max_events=max_events)

    def history(self, *, limit: int = 30, before: str = "") -> tuple[list[dict[str, Any]], str | None]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if before and not _CURSOR.fullmatch(before):
            raise ValueError("无效历史分页标记")
        with self._lock:
            candidates = self._history_candidates(limit + 1, before)
            snapshots = []
            for path in candidates[:limit]:
                try:
                    snapshot = self._snapshot(path, max_events=12)
                except (OSError, ValueError):
                    continue
                if snapshot is not None:
                    snapshots.append(snapshot)
            cursor = candidates[limit - 1].relative_to(self.root).as_posix() if len(candidates) > limit else None
            return snapshots, cursor

    def _history_candidates(self, limit: int, before: str) -> list[Path]:
        if not self.root.exists() and not self.root.is_symlink():
            return []
        if not ordinary_directory(str(self.root)):
            raise OSError("日志根目录含符号链接或重解析点")
        with os.scandir(self.root) as dates:
            names = sorted((entry.name for entry in dates if _DATE.fullmatch(entry.name)), reverse=True)
        result = []
        for name in names:
            if before and name > before[:10]:
                continue
            directory = self.root / name
            if not ordinary_directory(str(directory)):
                continue
            def candidates():
                with os.scandir(directory) as files:
                    for entry in files:
                        relative = f"{name}/{entry.name}"
                        if _NAME.fullmatch(entry.name) and (not before or relative < before):
                            path = Path(entry.path)
                            if _ordinary_file(path.lstat()):
                                yield path
            result.extend(heapq.nlargest(limit - len(result), candidates(), key=lambda path: path.name))
            if len(result) >= limit:
                break
        return result

    def read(self, operation_id: str, *, offset: int = 0, limit: int = 65536) -> dict[str, Any] | None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset 必须是非负整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 262144:
            raise ValueError("limit 必须在 1 到 262144 之间")
        with self._lock:
            path = self.safe_log_path(operation_id)
            if path is None:
                return None
            with self._open_regular(path) as stream:
                size = os.fstat(stream.fileno()).st_size
                if offset > size:
                    raise ValueError("offset 超出日志文件大小")
                stream.seek(offset)
                raw = stream.read(limit)
                # Keep complete UTF-8 characters when a page ends mid-character.
                if offset + len(raw) < size:
                    for _ in range(3):
                        try:
                            raw.decode("utf-8")
                            break
                        except UnicodeDecodeError as exc:
                            if exc.reason != "unexpected end of data":
                                break
                            raw += stream.read(1)
            return {"path": str(path), "content": raw.decode("utf-8", errors="replace"),
                    "next_offset": offset + len(raw), "eof": offset + len(raw) >= size, "total_bytes": size}

    def search(self, operation_id: str, query: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any] | None:
        """Search the complete UTF-8 file incrementally, not just a UI page.

        Matching is literal Unicode case-insensitive text. Each result is one
        physical line, with bounded snippets. Cursors retain partial long-line
        state in a bounded cache and never load the complete file into memory.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 512 or "\n" in query or "\r" in query:
            raise ValueError("搜索词须为 1 到 512 字符的单行文本")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset 必须是非负整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        operation_id = _uuid(operation_id)
        needle = query.casefold()
        with self._lock:
            path = self.safe_log_path(operation_id)
            if path is None:
                return None
            matches = []
            truncated = False
            with self._open_regular(path) as stream:
                metadata = os.fstat(stream.fileno())
                size = metadata.st_size
                if offset > size:
                    raise ValueError("offset 超出日志文件大小")
                identity = (metadata.st_dev, metadata.st_ino)
                cached = self._search_states.get((operation_id, needle, offset))
                if cached is not None and cached["identity"] == identity:
                    state = dict(cached)
                else:
                    state = {"identity": identity, "line": 1, "line_start": 0, "matched": False, "tail": ""}
                    # A caller-supplied random byte offset still receives true
                    # line numbers. Normal page cursors use the cached state.
                    remaining = offset
                    while remaining:
                        block = stream.read(min(65536, remaining))
                        if not block:
                            break
                        state["line"] += block.count(b"\n")
                        last_newline = block.rfind(b"\n")
                        if last_newline >= 0:
                            state["line_start"] = stream.tell() - len(block) + last_newline + 1
                        remaining -= len(block)
                stream.seek(offset)
                scanned = 0
                while stream.tell() < size and scanned < 8 * 1024 * 1024 and len(matches) < limit:
                    raw = stream.readline(min(65536, size - stream.tell(), 8 * 1024 * 1024 - scanned))
                    if not raw:
                        break
                    # Finish an incomplete UTF-8 codepoint at chunk boundaries.
                    for _ in range(3):
                        try:
                            decoded = raw.decode("utf-8")
                            break
                        except UnicodeDecodeError as exc:
                            if exc.reason != "unexpected end of data" or stream.tell() >= size:
                                decoded = raw.decode("utf-8", errors="replace")
                                break
                            raw += stream.read(1)
                    else:
                        decoded = raw.decode("utf-8", errors="replace")
                    scanned += len(raw)
                    text = state["tail"] + decoded.rstrip("\r\n")
                    folded = text.casefold()
                    match_at = folded.find(needle) if not state["matched"] else -1
                    if match_at >= 0 and not state["matched"]:
                        if len(folded) != len(text):
                            folded_position = 0
                            for text_position, character in enumerate(text):
                                if folded_position >= match_at:
                                    match_at = text_position
                                    break
                                folded_position += len(character.casefold())
                        start = max(0, match_at - 256)
                        snippet = text[start:start + 4096]
                        shortened = start > 0 or len(text) > start + 4096
                        matches.append({"line": state["line"], "offset": state["line_start"], "text": snippet,
                                        "truncated": shortened})
                        state["matched"] = True
                        truncated = truncated or shortened
                    if raw.endswith(b"\n"):
                        state.update(line=state["line"] + 1, line_start=stream.tell(), matched=False, tail="")
                    else:
                        state["tail"] = text[-max(1, len(needle)):]
                        truncated = truncated or len(raw) >= 65536
                next_offset = stream.tell()
                key = (operation_id, needle, next_offset)
                self._search_states[key] = state
                self._search_states.move_to_end(key)
                while len(self._search_states) > 256:
                    self._search_states.popitem(last=False)
            return {"path": str(path), "query": query, "matches": matches, "next_offset": next_offset,
                    "eof": next_offset >= size, "total_bytes": size, "truncated": truncated}
