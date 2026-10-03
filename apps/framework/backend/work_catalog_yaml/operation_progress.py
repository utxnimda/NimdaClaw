"""Bounded, process-local operation progress independent of filesystem queues.

Timestamps are UTC ISO 8601 strings. Progress follows the worker transaction,
not the HTTP connection: disconnecting a client does not stop a running job.
"""
from __future__ import annotations

import contextvars
import copy
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, TypeVar
from uuid import UUID

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import JSONResponse


T = TypeVar("T")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "warning"})
_current_operation: contextvars.ContextVar[tuple["OperationRegistry", "_Operation"] | None] = (
    contextvars.ContextVar("nimda_operation_progress", default=None)
)


class DuplicateOperationId(ValueError):
    pass


def normalize_operation_id(value: str) -> str:
    raw = str(value).strip()
    try:
        normalized = str(UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("操作 ID 必须是有效 UUID") from None
    if raw.casefold() != normalized:
        raise ValueError("操作 ID 必须是带横线的 UUID")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _count(value: int | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(eq=False)
class _Operation:
    id: str
    title: str
    events: deque[dict[str, Any]]
    status: str = "queued"
    started_at: str | None = None
    updated_at: str = field(default_factory=_now)
    finished_at: str | None = None
    message: str = "等待处理队列"
    completed: int | None = None
    total: int | None = None
    unit: str = ""
    last_seq: int = 0
    finished_clock: float | None = None


class OperationRegistry:
    def __init__(self, *, max_completed: int = 128, max_events: int = 200, retention_seconds: float = 1800) -> None:
        for value in (max_completed, max_events):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("progress limits must be positive integers")
        if isinstance(retention_seconds, bool) or retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self._max_completed = max_completed
        self._max_events = max_events
        self._retention_seconds = retention_seconds
        self._lock = threading.RLock()
        self._operations: dict[str, _Operation] = {}

    def _prune(self) -> None:
        # Never evict an active operation, including jobs with no recent events.
        now = time.monotonic()
        finished = sorted(
            (row for row in self._operations.values() if row.finished_clock is not None),
            key=lambda row: row.finished_clock,
        )
        excess = max(0, len(finished) - self._max_completed)
        for index, row in enumerate(finished):
            if index < excess or now - row.finished_clock > self._retention_seconds:
                self._operations.pop(row.id, None)

    def _append(self, row: _Operation, message: str, *, detail: str = "") -> None:
        row.updated_at = _now()
        row.message = _text(message, 512)
        row.last_seq += 1
        row.events.append({
            "seq": row.last_seq, "at": row.updated_at, "message": row.message,
            "detail": _text(detail, 2048), "completed": row.completed,
            "total": row.total, "unit": row.unit,
        })

    def register(self, operation_id: str, title: str) -> _Operation:
        operation_id = normalize_operation_id(operation_id)
        with self._lock:
            self._prune()
            if operation_id in self._operations:
                raise DuplicateOperationId("操作 ID 已存在，请使用新的操作 ID 重试")
            row = _Operation(operation_id, _text(title, 128), deque(maxlen=self._max_events))
            self._operations[operation_id] = row
            self._append(row, row.message)
            return row

    def snapshot(self, operation_id: str) -> dict[str, Any] | None:
        operation_id = normalize_operation_id(operation_id)
        with self._lock:
            self._prune()
            row = self._operations.get(operation_id)
            if row is None:
                return None
            return copy.deepcopy({
                "id": row.id, "title": row.title, "status": row.status,
                "started_at": row.started_at, "updated_at": row.updated_at,
                "finished_at": row.finished_at, "message": row.message,
                "completed": row.completed, "total": row.total, "unit": row.unit,
                "events": list(row.events), "last_seq": row.last_seq,
            })

    def start(self, row: _Operation) -> None:
        with self._lock:
            if self._operations.get(row.id) is not row or row.status != "queued":
                return
            row.status = "running"
            row.started_at = _now()
            self._append(row, "开始处理")

    def report(self, row: _Operation, message: str, *, completed: int | None = None,
               total: int | None = None, unit: str = "", detail: str = "") -> None:
        with self._lock:
            if self._operations.get(row.id) is not row or row.status != "running":
                return
            row.completed, row.total, row.unit = _count(completed), _count(total), _text(unit, 32)
            self._append(row, message, detail=detail)

    def finish(self, row: _Operation, status: str, message: str) -> None:
        if status not in _TERMINAL:
            raise ValueError("invalid final operation status")
        with self._lock:
            if self._operations.get(row.id) is not row or row.status in _TERMINAL:
                return
            row.status = status
            row.finished_at = _now()
            row.finished_clock = time.monotonic()
            self._append(row, message)
            self._prune()


def report_progress(message: str, *, completed: int | None = None, total: int | None = None,
                    unit: str = "", detail: str = "") -> None:
    """Emit one service milestone; CLI/untracked callers need no special setup."""
    current = _current_operation.get()
    if current is not None:
        registry, operation = current
        registry.report(operation, message, completed=completed, total=total, unit=unit, detail=detail)


def _response_warning(payload: Any) -> str:
    """Retain explicit partial outcomes even if the HTTP response is lost.

    Inspect only response objects, not arbitrary records in data arrays. Arrays
    explicitly named errors/failed are counts; never copy their full contents
    into the bounded progress log.
    """
    partial = False
    failures = empty_targets = missing_targets = 0

    def positive_count(value: Any) -> int:
        try:
            return max(0, int(value)) if isinstance(value, (int, float, str)) else 0
        except (ValueError, OverflowError):
            return 0

    def error_count(value: Any) -> int:
        if isinstance(value, (list, dict)):
            return len(value)
        return positive_count(value)

    def inspect(value: Any, depth: int = 0) -> None:
        nonlocal partial, failures, empty_targets, missing_targets
        if not isinstance(value, dict) or depth > 3:
            return
        partial = partial or value.get("state") == "partial" or value.get("partial") is True or value.get("partial_failure") is True
        # Most APIs expose both a failed_count and failed detail list; count the
        # same outcome once instead of adding duplicate representations.
        failures += max(positive_count(value.get("failed_count")), error_count(value.get("failed")), error_count(value.get("errors")))
        empty_targets += positive_count(value.get("skipped_empty_target"))
        missing_targets += positive_count(value.get("skipped_missing_target"))
        for key, child in value.items():
            if key not in {"errors", "failed"}:
                inspect(child, depth + 1)

    inspect(payload)
    if not (partial or failures or empty_targets or missing_targets):
        return ""
    messages = []
    if partial or failures:
        messages.append("部分项目未完成" + (f"（失败 {failures} 项）" if failures else ""))
    skipped = []
    if empty_targets:
        skipped.append(f"空目标 {empty_targets} 项")
    if missing_targets:
        skipped.append(f"目标不存在 {missing_targets} 项")
    if skipped:
        messages.append("已跳过：" + "、".join(skipped))
    return "处理结束：" + "；".join(messages) + "。请查看原页面结果。"


def execute_operation(registry: OperationRegistry, operation: _Operation,
                      function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    # CLI feature modules also import report_progress. Keep web dependencies
    # optional until an HTTP worker or endpoint actually needs them.
    from starlette.responses import JSONResponse

    registry.start(operation)
    token = _current_operation.set((registry, operation))
    try:
        result = function(*args, **kwargs)
        payload: Any = result
        failed = False
        if isinstance(result, JSONResponse):
            failed = result.status_code >= 400
            try:
                payload = json.loads(result.body)
            except (ValueError, UnicodeError):
                payload = None
        if isinstance(payload, dict):
            failed = failed or payload.get("ok") is False
        message = "处理完成"
        if failed:
            message = _text(payload.get("error") or payload.get("message"), 512) if isinstance(payload, dict) else ""
            message = message or "处理失败，请查看操作结果"
        warning = "" if failed else _response_warning(payload)
        registry.finish(operation, "failed" if failed else "warning" if warning else "succeeded", warning or message)
        return result
    except BaseException as exc:
        registry.finish(operation, "failed", _text(exc, 512) or "处理异常")
        raise
    finally:
        _current_operation.reset(token)


async def api_operation_progress(request: Request) -> JSONResponse:
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        operation = request.app.state.operation_registry.snapshot(request.path_params["operation_id"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    if operation is None:
        return JSONResponse({"ok": False, "error": "未找到该操作，可能尚未开始或记录已过期"}, status_code=404, headers=headers)
    return JSONResponse({"ok": True, "operation": operation}, headers=headers)
