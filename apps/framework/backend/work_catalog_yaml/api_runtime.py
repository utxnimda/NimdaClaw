"""Owned worker queues and request adapters for the local filesystem API.

Disk operations run serially, preserving read/modify/write ordering without
blocking the ASGI loop. Cancelling a queued request cancels its work; work that
has already started finishes, and application shutdown waits for it.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial, wraps
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse

from work_catalog_yaml.operation_progress import (
    DuplicateOperationId, OperationRegistry, execute_operation, normalize_operation_id,
)
from work_catalog_yaml.common.operation_logs import OperationLogStore


T = TypeVar("T")


class WorkQueueClosed(RuntimeError):
    pass


class WorkQueueBusy(RuntimeError):
    pass


class ApiWorkQueue:
    def __init__(self, *, workers: int = 1, name: str = "nimda-disk", max_pending: int = 64) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._max_pending = max_pending
        self._lock = threading.RLock()
        self._futures: set[Future[Any]] = set()
        self._closing = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "closing": self._closing,
                "running": sum(future.running() for future in self._futures),
                "queued": sum(not future.running() and not future.done() for future in self._futures),
            }

    def _completed(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    async def run(self, function: Callable[..., T], *args: Any,
                  _on_cancelled: Callable[[], None] | None = None, **kwargs: Any) -> T:
        with self._lock:
            if self._closing:
                raise WorkQueueClosed("应用正在关闭，尚未开始的操作已取消")
            if len(self._futures) >= self._max_pending:
                raise WorkQueueBusy("待处理操作过多，请等待当前操作完成后重试")
            context = contextvars.copy_context()
            future = self._executor.submit(context.run, partial(function, *args, **kwargs))
            self._futures.add(future)
            future.add_done_callback(self._completed)
            if _on_cancelled is not None:
                future.add_done_callback(lambda result: _on_cancelled() if result.cancelled() else None)
        wrapped = asyncio.wrap_future(future)
        # A disconnected client cannot receive a later error, but the result
        # must still be observed to avoid unhandled-future warnings.
        wrapped.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            if future.cancelled() and self.snapshot()["closing"]:
                raise WorkQueueClosed("应用正在关闭，尚未开始的操作已取消") from None
            future.cancel()  # False once running: finish the existing transaction.
            raise

    def begin_shutdown(self) -> None:
        with self._lock:
            self._closing = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def close(self) -> None:
        self.begin_shutdown()
        with self._lock:
            pending = tuple(self._futures)
        if pending:
            await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)

    def wait_for_running(self) -> None:
        """Desktop shutdown can wait even if the ASGI loop was interrupted."""
        self.begin_shutdown()
        with self._lock:
            pending = tuple(self._futures)
        for future in pending:
            try:
                future.result()
            except BaseException:
                pass  # The request/recovery handler owns the operation outcome.


def install_api_queues(app: Any, *, operation_log_root: Path | None = None) -> None:
    app.state.api_disk_queue = ApiWorkQueue()
    app.state.api_network_queue = ApiWorkQueue(workers=2, name="nimda-provider")
    if not isinstance(getattr(app.state, "operation_registry", None), OperationRegistry):
        app.state.operation_registry = OperationRegistry(log_store=OperationLogStore(operation_log_root) if operation_log_root is not None else None)


def api_work_queues(app: Any) -> tuple[ApiWorkQueue, ...]:
    state = getattr(app, "state", None)
    return tuple(
        queue for name in ("api_disk_queue", "api_network_queue")
        if isinstance(queue := getattr(state, name, None), ApiWorkQueue)
    )


@asynccontextmanager
async def api_lifespan(app: Any):
    if not api_work_queues(app) or any(queue.snapshot()["closing"] for queue in api_work_queues(app)):
        install_api_queues(app)
    try:
        yield
    finally:
        queues = api_work_queues(app)
        for queue in queues:
            queue.begin_shutdown()
        await asyncio.gather(*(queue.close() for queue in queues))


async def api_health(request: Request) -> JSONResponse:
    disk, network = api_work_queues(request.app)
    state = {"disk": disk.snapshot(), "network": network.snapshot()}
    closing = any(row["closing"] for row in state.values())
    busy = any(row["running"] or row["queued"] for row in state.values())
    return JSONResponse({"ok": not closing, "state": "stopping" if closing else "busy" if busy else "ready", **state})


async def _dispatch(request: Request, function: Callable[..., T], value: Any, *, network: bool = False) -> T | JSONResponse:
    queue = request.app.state.api_network_queue if network else request.app.state.api_disk_queue
    registry = request.app.state.operation_registry
    operation = None
    raw_operation_id = request.headers.get("x-nimda-operation-id")
    if raw_operation_id is not None or request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        try:
            operation_id = normalize_operation_id(raw_operation_id) if raw_operation_id is not None else str(uuid4())
            operation = await _register_operation(registry, operation_id, request.url.path)
        except DuplicateOperationId as exc:
            return JSONResponse({"ok": False, "error": str(exc), "code": "operation-id-exists"}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "code": "invalid-operation-id"}, status_code=400)
    try:
        if operation is not None:
            result = await queue.run(
                execute_operation, registry, operation, function, value,
                _on_cancelled=lambda: registry.finish(operation, "cancelled", "尚未开始的操作已取消"),
            )
            if isinstance(result, JSONResponse):
                result.headers["X-Nimda-Operation-Id"] = operation.id
            return result
        return await queue.run(function, value)
    except WorkQueueClosed as exc:
        if operation is not None:
            registry.finish(operation, "cancelled", str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except WorkQueueBusy as exc:
        if operation is not None:
            registry.finish(operation, "failed", str(exc))
        return JSONResponse(
            {"ok": False, "error": str(exc), "code": "api-queue-full"},
            status_code=503,
            headers={"Retry-After": "1"},
        )


def query_endpoint(function: Callable[..., T]) -> Callable[..., Any]:
    @wraps(function)
    async def endpoint(request: Request):
        return await _dispatch(request, function, dict(request.query_params))
    return endpoint


async def _register_operation(registry: OperationRegistry, operation_id: str, title: str):
    if registry.log_store is None:
        return registry.register(operation_id, title)
    pending = asyncio.create_task(asyncio.to_thread(registry.register, operation_id, title))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        row = await pending
        await asyncio.to_thread(registry.finish, row, "cancelled", "请求在进入处理队列前已取消")
        raise


async def _invalid_request(request: Request, message: str) -> JSONResponse:
    """Record parser failures before a worker exists without retaining the body."""
    registry = request.app.state.operation_registry
    raw_id = request.headers.get("x-nimda-operation-id")
    try:
        operation_id = normalize_operation_id(raw_id) if raw_id is not None else str(uuid4())
        operation = await _register_operation(registry, operation_id, request.url.path)
    except (DuplicateOperationId, ValueError):
        # Invalid/reused supplied IDs must never overwrite another operation.
        operation = await _register_operation(registry, str(uuid4()), request.url.path)
    def record_failure():
        registry.start(operation)
        registry.finish(operation, "failed", message, result={"error": message})
    if registry.log_store is not None:
        await asyncio.to_thread(record_failure)
    else:
        record_failure()
    return JSONResponse({"ok": False, "error": message}, status_code=400, headers={"X-Nimda-Operation-Id": operation.id})


def json_endpoint(function: Callable[..., T] | None = None, *, network: bool = False):
    def decorate(handler: Callable[..., T]):
        @wraps(handler)
        async def endpoint(request: Request):
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeError):
                return await _invalid_request(request, "请求体须为 JSON 对象")
            return await _dispatch(request, handler, body, network=network)
        return endpoint
    return decorate(function) if function is not None else decorate


def upload_endpoint(function: Callable[..., T]) -> Callable[..., Any]:
    @wraps(function)
    async def endpoint(request: Request):
        try:
            async with request.form() as form:
                uploads = [
                    (str(part.filename or ""), await part.read())
                    for part in form.getlist("file")
                    if hasattr(part, "filename") and callable(getattr(part, "read", None))
                ]
        except Exception as exc:
            return await _invalid_request(request, f"无法解析 multipart：{exc}")
        if not uploads:
            return await _invalid_request(request, "请选择上传字段 file（.yaml），可一次上传多个")
        return await _dispatch(request, function, uploads)
    return endpoint
