"""Collection-completion HTTP API owned by the collection-info feature."""
from __future__ import annotations

from typing import Any
from starlette.responses import JSONResponse
from work_catalog_yaml.common.http import error_response
from work_catalog_yaml.api_runtime import json_endpoint, query_endpoint
from work_catalog_yaml.jp_tv.browse_settings import get_resolved_browse_settings

from collection_info.service import (
    collection_records_payload,
    save_collection_records_from_ui_body,
)

@query_endpoint
def _get_collection_records_api(query: dict[str, str]) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return error_response(str(e), status_code=500)
    try:
        return JSONResponse(collection_records_payload(st))
    except ValueError as e:
        return error_response(str(e), status_code=400)
    except OSError as e:
        return error_response(f"read collection records failed: {e}", status_code=500)


@json_endpoint
def _post_collection_records_api(body: dict[str, Any]) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return error_response(str(e), status_code=500)
    try:
        result = save_collection_records_from_ui_body(body, settings=st)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    except PermissionError as e:
        return error_response(f"write collection records denied: {e}", status_code=403)
    except OSError as e:
        return error_response(f"write collection records failed: {e}", status_code=500)
    return JSONResponse({"ok": True, **result})
