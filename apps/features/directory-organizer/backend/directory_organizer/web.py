"""HTTP adapters for the independent organizer; all work uses the shared queue."""
from starlette.responses import JSONResponse

from work_catalog_yaml.api_runtime import json_endpoint
from work_catalog_yaml.common.http import error_response
from work_catalog_yaml.common.native_dialogs import choose_directory
from work_catalog_yaml.jp_tv.browse_settings import get_resolved_browse_settings
from .service import OrganizerService

service = OrganizerService()


def _call(action, body):
    try:
        settings, _ = get_resolved_browse_settings()
        result = getattr(service, action)(body, settings=settings)
        return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, RuntimeError) as exc:
        return error_response(str(exc), status_code=400 if isinstance(exc, ValueError) else 500)


@json_endpoint
def scan(body):
    return _call("scan", body)


@json_endpoint
def preview(body):
    response = _call("preview", body)
    if response.status_code == 200:
        import json
        payload = json.loads(response.body)
        return JSONResponse({"ok": True, "plan": {key: value for key, value in payload.items() if key != "ok"}}, headers={"Cache-Control": "no-store"})
    return response


@json_endpoint
def execute(body):
    return _call("execute", body)


@json_endpoint
def catalog_search(body):
    return _call("search_catalog", body)


@json_endpoint
def shortcut_action(body):
    return _call("shortcut_action", body)


@json_endpoint
def choose_folder(body):
    try:
        initial = body.get("root", "")
        if not isinstance(initial, str) or len(initial) > 4096:
            raise ValueError("初始目录路径无效")
        path = choose_directory(initial)
        return JSONResponse({"ok": True, "path": path, "cancelled": not bool(path)})
    except (ValueError, OSError, RuntimeError) as exc:
        return error_response(str(exc))
