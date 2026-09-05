"""Bounded content-based reuse of successfully parsed YAML documents.

The retained document is private: every cache consumer receives a deep copy.
Keys are complete source strings, not paths or timestamps, so a same-size edit
with a preserved mtime cannot reuse stale data. Disk reads remain the caller's
responsibility. Memory accounting estimates retained Python objects, not RSS.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import sys
import threading
from typing import Any, Callable


def _retained_size(source: str, document: Any, limit: int) -> int:
    seen: set[int] = set()
    pending = [source, document]
    size = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(value)
        if size > limit:
            return size
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
    return size


class YamlParseCache:
    def __init__(self, *, max_entries: int = 128, max_bytes: int = 32 * 1024 * 1024) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("cache bounds cannot be negative")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._documents: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._lock = threading.RLock()
        self._bytes = 0
        self._hits = 0
        self._misses = 0

    def parse(self, source: str, loader: Callable[[], Any]) -> Any:
        with self._lock:
            entry = self._documents.get(source)
            if entry is not None:
                self._documents.move_to_end(source)
                self._hits += 1
                document = entry[0]
            else:
                self._misses += 1
        if entry is not None:
            return deepcopy(document)

        # Parsing can be slow; do not block unrelated cache hits behind it.
        # Exceptions are deliberately not retained.
        document = loader()
        size = _retained_size(source, document, self.max_bytes)
        if not self.max_entries or size > self.max_bytes:
            return document
        with self._lock:
            # Another worker may have parsed the same immutable content.
            entry = self._documents.get(source)
            if entry is None:
                while self._documents and (
                    len(self._documents) >= self.max_entries or self._bytes + size > self.max_bytes
                ):
                    _, (_, discarded_size) = self._documents.popitem(last=False)
                    self._bytes -= discarded_size
                self._documents[source] = (document, size)
                self._bytes += size
            else:
                document = entry[0]
                self._documents.move_to_end(source)
        return deepcopy(document)

    def clear(self) -> None:
        with self._lock:
            self._documents.clear()
            self._bytes = 0
            self._hits = 0
            self._misses = 0

    def info(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._documents),
                "estimated_bytes": self._bytes,
                "hits": self._hits,
                "misses": self._misses,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
            }
