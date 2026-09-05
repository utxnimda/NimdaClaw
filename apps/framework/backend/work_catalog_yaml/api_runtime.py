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
from typing import Any, Callable, TypeVar

from starlette.requests import Request
from starlette.responses import JSONResponse


T = TypeVar("T")


class WorkQueueClosed(RuntimeError):
    pass


class ApiWorkQueue:
    def __init__(self, *, workers: int = 1, name: str = "nimda-disk") -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
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

    async def run(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            if self._closing:
                raise WorkQueueClosed("应用正在关闭，尚未开始的操作已取消")
            context = contextvars.copy_context()
            future = self._executor.submit(context.run, partial(function, *args, **kwargs))
            self._futures.add(future)
            future.add_done_callback(self._completed)
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


def install_api_queues(app: Any) -> None:
    app.state.api_disk_queue = ApiWorkQueue()
    app.state.api_network_queue = ApiWorkQueue(workers=2, name="nimda-provider")


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
    try:
        return await queue.run(function, value)
    except WorkQueueClosed as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


def query_endpoint(function: Callable[..., T]) -> Callable[..., Any]:
    @wraps(function)
    async def endpoint(request: Request):
        return await _dispatch(request, function, dict(request.query_params))
    return endpoint


def json_endpoint(function: Callable[..., T] | None = None, *, network: bool = False):
    def decorate(handler: Callable[..., T]):
        @wraps(handler)
        async def endpoint(request: Request):
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeError):
                return JSONResponse({"ok": False, "error": "请求体须为 JSON 对象"}, status_code=400)
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
            return JSONResponse({"ok": False, "error": f"无法解析 multipart：{exc}"}, status_code=400)
        if not uploads:
            return JSONResponse({"ok": False, "error": "请选择上传字段 file（.yaml），可一次上传多个"}, status_code=400)
        return await _dispatch(request, function, uploads)
    return endpoint
