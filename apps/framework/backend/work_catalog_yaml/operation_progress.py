"""Bounded live operation progress with optional durable per-operation logs.

Timestamps are UTC ISO 8601 strings. Progress follows the worker transaction,
not the HTTP connection: disconnecting a client does not stop a running job.
"""
from __future__ import annotations

import contextvars
import copy
import json
import threading
import time
import traceback
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, TypeVar
from uuid import UUID

from work_catalog_yaml.common.operation_logs import OperationLogStore, DuplicateLogId
from work_catalog_yaml.common.operation_results import ResultProjection, diagnostic_context

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import JSONResponse


T = TypeVar("T")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "warning"})
_current_operation: contextvars.ContextVar[tuple["OperationRegistry", "_Operation"] | None] = (
    contextvars.ContextVar("nimda_operation_progress", default=None)
)
_current_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("nimda_operation_context", default={})


@contextmanager
def operation_context(**fields: Any):
    """Bind safe object/stage identifiers; preserve the innermost failing scope."""
    scope = {**_current_context.get(), **diagnostic_context(fields)}
    token = _current_context.set(scope)
    try:
        yield
    except BaseException as exc:
        try:
            inherited = diagnostic_context(getattr(exc, "_nimda_operation_context", None))
            exc._nimda_operation_context = {**scope, **inherited}
        except (AttributeError, TypeError):
            pass
        raise
    finally:
        _current_context.reset(token)


def _exception_diagnostic(exc: BaseException, context: dict[str, Any]) -> dict[str, Any]:
    detail = {**context, **diagnostic_context(getattr(exc, "_nimda_operation_context", None)),
              "level": "error", "error_type": type(exc).__name__, "message": _text(exc, 2048), "reason": _text(exc, 2048)}
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        frame = frames[-1]
        detail["location"] = {"file": frame.filename, "line": frame.lineno, "function": frame.name}
    return detail


def _worker_context(function: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    # Only top-level operation identity fields. Never traverse uploaded bytes,
    # edited rows, catalogs, provider tokens or arbitrary request containers.
    keys = {"path", "source_path", "target_path", "shortcut_path", "work_key", "press_key", "press_path",
            "yaml_source_rel", "index_in_file", "record_index", "press_index", "name", "root_path", "relpath"}
    context = {"action": getattr(function, "__qualname__", getattr(function, "__name__", "执行服务"))}
    for value in ((args[0] if args else None), kwargs):
        if isinstance(value, dict):
            context.update(diagnostic_context({key: value[key] for key in keys.intersection(value)}))
    code = getattr(function, "__code__", None)
    if code is not None:
        context["location"] = {"file": code.co_filename, "line": code.co_firstlineno, "function": code.co_name}
    return diagnostic_context(context)


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
    result: dict[str, Any] | None = None
    log: dict[str, Any] = field(default_factory=lambda: {"path": "", "available": False, "error": ""})
    source: str = "server"
    path: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    base_context: dict[str, Any] = field(default_factory=dict)
    exception_details: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=8))


class OperationRegistry:
    def __init__(self, *, max_completed: int = 128, max_events: int = 200, retention_seconds: float = 1800,
                 log_store: OperationLogStore | None = None) -> None:
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
        self.log_store = log_store

    def _snapshot(self, row: _Operation, *, include_events: bool = True) -> dict[str, Any]:
        snapshot = {
            "id": row.id, "title": row.title, "status": row.status,
            "started_at": row.started_at, "updated_at": row.updated_at,
            "finished_at": row.finished_at, "message": row.message,
            "completed": row.completed, "total": row.total, "unit": row.unit,
            "last_seq": row.last_seq, "result": row.result, "source": row.source,
            "path": row.path, "context": row.context,
        }
        if include_events:
            snapshot.update(events=list(row.events), log=row.log)
        return copy.deepcopy(snapshot)

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

    def _append(self, row: _Operation, message: str, *, detail: str = "", exception_traceback: str = "",
                context: dict[str, Any] | None = None) -> None:
        row.updated_at = _now()
        row.message = _text(message, 512)
        row.last_seq += 1
        row.events.append({
            "seq": row.last_seq, "at": row.updated_at, "message": row.message,
            "detail": _text(detail, 2048), "completed": row.completed,
            "total": row.total, "unit": row.unit,
            "context": copy.deepcopy(diagnostic_context(row.context if context is None else context)),
        })
        if self.log_store is not None and row.log["available"] and not row.log["error"]:
            try:
                self.log_store.append(row.id, self._snapshot(row, include_events=False), row.events[-1], traceback=exception_traceback)
            except (OSError, ValueError) as exc:
                row.log["error"] = f"持久日志写入失败，仅保留内存进度：{_text(exc, 512)}"

    def register(self, operation_id: str, title: str, *, source: str = "server", path: str = "") -> _Operation:
        operation_id = normalize_operation_id(operation_id)
        with self._lock:
            self._prune()
            if operation_id in self._operations:
                raise DuplicateOperationId("操作 ID 已存在，请使用新的操作 ID 重试")
            row = _Operation(operation_id, _text(title, 128), deque(maxlen=self._max_events))
            row.source = source
            row.path = _text(path or (title if title.startswith("/api/") else ""), 512).split("?", 1)[0]
            if self.log_store is not None:
                try:
                    path = self.log_store.create(operation_id)
                    row.log = {"path": str(path), "available": True, "error": ""}
                except DuplicateLogId:
                    raise DuplicateOperationId("操作 ID 已存在，请使用新的操作 ID 重试") from None
                except (OSError, ValueError) as exc:
                    row.log["error"] = f"持久日志不可用，仅保留内存进度：{_text(exc, 512)}"
            self._operations[operation_id] = row
            self._append(row, row.message)
            return row

    def snapshot(self, operation_id: str) -> dict[str, Any] | None:
        operation_id = normalize_operation_id(operation_id)
        with self._lock:
            self._prune()
            row = self._operations.get(operation_id)
            if row is None:
                return self.log_store.snapshot(operation_id, max_events=self._max_events) if self.log_store else None
            return self._snapshot(row)

    def history(self, *, limit: int = 30, before: str = "") -> tuple[list[dict[str, Any]], str | None]:
        with self._lock:
            self._prune()
            if self.log_store is not None:
                snapshots, cursor = self.log_store.history(limit=limit, before=before)
                snapshots = [self._snapshot(self._operations[item["id"]]) if item["id"] in self._operations else item for item in snapshots]
                if not before:
                    known = {item["id"] for item in snapshots}
                    snapshots.extend(self._snapshot(row) for row in self._operations.values() if row.id not in known and not row.log["available"])
                    snapshots.sort(key=lambda row: row["updated_at"], reverse=True)
                return snapshots[:limit], cursor
            if not 1 <= limit <= 100:
                raise ValueError("limit 必须在 1 到 100 之间")
            return [self._snapshot(row) for row in sorted(self._operations.values(), key=lambda row: row.updated_at, reverse=True)[:limit]], None

    def start(self, row: _Operation, *, context: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._operations.get(row.id) is not row or row.status != "queued":
                return
            row.status = "running"
            row.base_context = diagnostic_context(context)
            row.context = dict(row.base_context)
            row.started_at = _now()
            self._append(row, "开始处理")

    def report(self, row: _Operation, message: str, *, completed: int | None = None,
               total: int | None = None, unit: str = "", detail: str = "", context: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._operations.get(row.id) is not row or row.status != "running":
                return
            row.completed, row.total, row.unit = _count(completed), _count(total), _text(unit, 32)
            row.context = {**row.base_context, "stage": _text(message, 512), **diagnostic_context(context)}
            self._append(row, message, detail=detail)

    def finish(self, row: _Operation, status: str, message: str, *, result: Any = None, exception_traceback: str = "",
               context: dict[str, Any] | None = None) -> None:
        if status not in _TERMINAL:
            raise ValueError("invalid final operation status")
        with self._lock:
            if self._operations.get(row.id) is not row or row.status in _TERMINAL:
                return
            row.status = status
            row.finished_at = _now()
            row.finished_clock = time.monotonic()
            row.context = {**row.context, **diagnostic_context(context)}
            projection = ResultProjection(result, message=message, context=row.context, extra_details=row.exception_details)
            details = projection.iter_details()
            if self.log_store is not None and row.log["available"] and not row.log["error"]:
                try:
                    self.log_store.append_details(row.id, details)
                except (OSError, ValueError) as exc:
                    row.log["error"] = f"持久日志写入失败，仅保留内存结果：{_text(exc, 512)}"
            # Also complete the bounded panel summary after a log write failed.
            for _ in details:
                pass
            row.result = projection.result
            self._append(row, message, exception_traceback=exception_traceback)
            self._prune()

    def exception(self, row: _Operation, exc: BaseException, *, context: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._operations.get(row.id) is row and row.status == "running":
                diagnostic = _exception_diagnostic(exc, {**row.context, **diagnostic_context(context)})
                row.exception_details.append(diagnostic)
                row.context = diagnostic_context(diagnostic)
                self._append(row, f"处理异常：{_text(exc, 512)}", detail=type(exc).__name__,
                             exception_traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def report_progress(message: str, *, completed: int | None = None, total: int | None = None,
                    unit: str = "", detail: str = "", context: dict[str, Any] | None = None) -> None:
    """Emit one service milestone; CLI/untracked callers need no special setup."""
    current = _current_operation.get()
    if current is not None:
        registry, operation = current
        registry.report(operation, message, completed=completed, total=total, unit=unit, detail=detail,
                        context={**_current_context.get(), **diagnostic_context(context)})


def report_exception(exc: BaseException, *, context: dict[str, Any] | None = None) -> None:
    """Preserve caught feature exceptions before an HTTP adapter handles them."""
    current = _current_operation.get()
    if current is not None:
        registry, operation = current
        registry.exception(operation, exc, context={**_current_context.get(), **diagnostic_context(context)})


def _response_warning(payload: Any) -> str:
    """Retain explicit partial outcomes even if the HTTP response is lost.

    Inspect only response objects, not arbitrary records in data arrays. Arrays
    explicitly named errors/failed are counts; never copy their full contents
    into the bounded progress log.
    """
    partial = False
    failures = empty_targets = missing_targets = diagnostics = 0

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
        nonlocal partial, failures, empty_targets, missing_targets, diagnostics
        if not isinstance(value, dict) or depth > 3:
            return
        partial = partial or value.get("state") == "partial" or value.get("partial") is True or value.get("partial_failure") is True
        # Most APIs expose both a failed_count and failed detail list; count the
        # same outcome once instead of adding duplicate representations.
        failures += max(positive_count(value.get("failed_count")), error_count(value.get("failed")), error_count(value.get("errors")))
        empty_targets += positive_count(value.get("skipped_empty_target"))
        missing_targets += positive_count(value.get("skipped_missing_target"))
        diagnostics += max(error_count(value.get("unmapped_shortcuts")), error_count(value.get("issues")), error_count(value.get("warnings")))
        for key, child in value.items():
            if key not in {"errors", "failed"}:
                inspect(child, depth + 1)

    inspect(payload)
    if not (partial or failures or empty_targets or missing_targets or diagnostics):
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
    if diagnostics:
        messages.append(f"有 {diagnostics} 项需要关注")
    return "处理结束：" + "；".join(messages) + "。具体结果和问题请查看处理详情及日志。"


def execute_operation(registry: OperationRegistry, operation: _Operation,
                      function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    # CLI feature modules also import report_progress. Keep web dependencies
    # optional until an HTTP worker or endpoint actually needs them.
    from starlette.responses import JSONResponse

    registry.start(operation, context=_worker_context(function, args, kwargs))
    token = _current_operation.set((registry, operation))
    context_token = _current_context.set({})
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
        if not failed and not warning and operation.exception_details:
            warning = "处理结束，但有已捕获的处理异常；请查看具体对象、步骤及原因。"
        registry.finish(operation, "failed" if failed else "warning" if warning else "succeeded", warning or message, result=payload)
        return result
    except BaseException as exc:
        diagnostic = _exception_diagnostic(exc, {**operation.context, **_current_context.get()})
        operation.exception_details.append(diagnostic)
        registry.finish(operation, "failed", _text(exc, 512) or "处理异常",
                        result={"error": _text(exc, 2048)}, exception_traceback=traceback.format_exc(),
                        context=diagnostic_context(diagnostic))
        raise
    finally:
        _current_context.reset(context_token)
        _current_operation.reset(token)


async def api_operation_progress(request: Request) -> JSONResponse:
    import asyncio
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        operation = await asyncio.to_thread(request.app.state.operation_registry.snapshot, request.path_params["operation_id"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"无法读取操作日志：{exc}"}, status_code=503, headers=headers)
    if operation is None:
        return JSONResponse({"ok": False, "error": "未找到该操作，可能尚未开始或日志不可用"}, status_code=404, headers=headers)
    return JSONResponse({"ok": True, "operation": operation}, headers=headers)


async def api_operation_history(request: Request) -> JSONResponse:
    import asyncio
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        operations, cursor = await asyncio.to_thread(request.app.state.operation_registry.history,
            limit=int(request.query_params.get("limit", "30")), before=request.query_params.get("before", ""))
        return JSONResponse({"ok": True, "operations": operations, "next_cursor": cursor}, headers=headers)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"无法读取操作历史：{exc}"}, status_code=503, headers=headers)


async def api_operation_log(request: Request) -> JSONResponse:
    import asyncio
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        operation_id = normalize_operation_id(request.path_params["operation_id"])
        store = request.app.state.operation_registry.log_store
        result = await asyncio.to_thread(store.read, operation_id,
            offset=int(request.query_params.get("offset", "0")), limit=int(request.query_params.get("limit", "65536"))) if store else None
        if result is None:
            return JSONResponse({"ok": False, "error": "未找到该操作的持久日志"}, status_code=404, headers=headers)
        return JSONResponse({"ok": True, "log": result}, headers=headers)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"无法读取日志文件：{exc}"}, status_code=503, headers=headers)


async def api_operation_log_search(request: Request) -> JSONResponse:
    import asyncio
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        operation_id = normalize_operation_id(request.path_params["operation_id"])
        store = request.app.state.operation_registry.log_store
        result = await asyncio.to_thread(store.search, operation_id, request.query_params.get("q", ""),
            offset=int(request.query_params.get("offset", "0")), limit=int(request.query_params.get("limit", "50"))) if store else None
        if result is None:
            return JSONResponse({"ok": False, "error": "未找到该操作的日志文件"}, status_code=404, headers=headers)
        return JSONResponse({"ok": True, "search": result}, headers=headers)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"搜索日志文件失败：{exc}"}, status_code=503, headers=headers)


async def api_operation_log_file(request: Request):
    import asyncio
    from urllib.parse import quote
    from starlette.background import BackgroundTask
    from starlette.responses import StreamingResponse, JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0", "X-Content-Type-Options": "nosniff"}
    try:
        operation_id = normalize_operation_id(request.path_params["operation_id"])
        store = request.app.state.operation_registry.log_store
        opened = await asyncio.to_thread(store.open_text_file, operation_id) if store else None
        if opened is None:
            return JSONResponse({"ok": False, "error": "未找到该操作的日志文件"}, status_code=404, headers=headers)
        path, stream = opened
        disposition = "attachment" if request.query_params.get("download") == "1" else "inline"
        headers["Content-Disposition"] = f"{disposition}; filename*=utf-8''{quote(path.name)}"

        def chunks():
            try:
                while block := stream.read(65536):
                    yield block
            finally:
                stream.close()

        # Stream the already validated descriptor, never reopen its path after
        # validation. Background cleanup also covers early client disconnects.
        return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8", headers=headers,
            background=BackgroundTask(stream.close))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"打开日志文件失败：{exc}"}, status_code=503, headers=headers)


async def api_operation_log_open(request: Request) -> JSONResponse:
    import asyncio
    import os
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        operation_id = normalize_operation_id(request.path_params["operation_id"])
        store = request.app.state.operation_registry.log_store
        path = await asyncio.to_thread(store.safe_log_path, operation_id) if store else None
        if path is None:
            return JSONResponse({"ok": False, "error": "未找到该操作的日志文件"}, status_code=404, headers=headers)
        if not hasattr(os, "startfile"):
            raise OSError("当前系统不支持直接打开；请使用下载日志或面板内搜索")
        # Only a validated, server-owned .log is opened; no client-supplied path.
        await asyncio.to_thread(os.startfile, str(path), "open")
        return JSONResponse({"ok": True, "path": str(path), "message": "已请求系统打开日志文件，可使用 Ctrl+F 搜索"}, headers=headers)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"打开日志文件失败：{exc}"}, status_code=503, headers=headers)


async def api_client_operation(request: Request) -> JSONResponse:
    import asyncio
    from starlette.responses import JSONResponse

    headers = {"Cache-Control": "no-store, max-age=0"}
    try:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65536:
                raise ValueError("客户端日志最多 64 KiB")
        body = json.loads(content)
        if not isinstance(body, dict):
            raise ValueError("客户端日志必须是 JSON 对象")
        operation_id = normalize_operation_id(body.get("id", ""))
        if body.get("status") not in {"failed", "warning", "succeeded"}:
            raise ValueError("客户端日志状态无效")
        for key, limit in (("title", 128), ("message", 2048), ("path", 512)):
            if not isinstance(body.get(key, ""), str) or len(body.get(key, "")) > limit:
                raise ValueError(f"客户端日志 {key} 字段过长或无效")
        path = body.get("path", "")
        if path and (not path.startswith("/api/") or "?" in path or "#" in path or "\n" in path):
            raise ValueError("客户端日志 path 必须是无参数 API 路径")
        details = body.get("details", [])
        if isinstance(details, str):
            if len(details) > 16384:
                raise ValueError("客户端日志 details 最多 16384 字符")
        elif not isinstance(details, list) or len(details) > 100:
            raise ValueError("客户端日志 details 最多 100 项")

        def record() -> dict[str, Any]:
            registry = request.app.state.operation_registry
            row = registry.register(operation_id, body.get("title") or path or "客户端处理", source="client", path=path)
            registry.start(row)
            if isinstance(details, str):
                for start in range(0, len(details), 2048):
                    registry.report(row, "客户端详细记录", detail=details[start:start + 2048])
                result_details = [{"message": line} for line in details.splitlines()[:100]]
            else:
                result_details = details
            registry.finish(row, body["status"], body.get("message") or "客户端处理完成", result={"details": result_details})
            return registry.snapshot(row.id)

        operation = await asyncio.to_thread(record)
        return JSONResponse({"ok": True, "operation": operation}, headers=headers)
    except DuplicateOperationId as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409, headers=headers)
    except (ValueError, UnicodeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400, headers=headers)
