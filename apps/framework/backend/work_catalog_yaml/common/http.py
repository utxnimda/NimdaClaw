"""Shared JSON failure responses with operation-scoped exception diagnostics."""
from __future__ import annotations

import sys
from typing import Any

from starlette.responses import JSONResponse


def error_response(message: Any, *, status_code: int = 400, context: dict[str, Any] | None = None, **details: Any) -> JSONResponse:
    """Preserve the public error contract and the current exception traceback.

    Only the active exception is reported, never request bodies or frame locals.
    Validation failures outside an exception handler still produce a plain JSON
    response and receive their operation outcome from the request adapter.
    """
    exception = sys.exc_info()[1]  # Keep the supported Python 3.10 runtime.
    if exception is not None:
        # Delayed import keeps common HTTP helpers independent from API startup.
        from work_catalog_yaml.operation_progress import report_exception

        if context is None:
            report_exception(exception)
        else:
            report_exception(exception, context=context)
    if context is not None:
        from work_catalog_yaml.common.operation_results import diagnostic_context

        details["context"] = diagnostic_context(context)
    return JSONResponse({**details, "ok": False, "error": str(message)}, status_code=status_code)
