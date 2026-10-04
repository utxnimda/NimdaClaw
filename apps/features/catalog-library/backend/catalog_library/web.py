"""Thin HTTP composition for the local catalog and explicit provider imports."""
from __future__ import annotations

from starlette.responses import JSONResponse, Response

from work_catalog_yaml.api_runtime import json_endpoint, query_endpoint
from work_catalog_yaml.common.http import error_response
from work_catalog_yaml.jp_tv.browse_settings import get_resolved_browse_settings
from . import assets, providers, service


def _call(handler, body):
    try:
        settings, _ = get_resolved_browse_settings()
        result = handler(body, settings=settings)
        return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, RuntimeError) as exc:
        return error_response(
            str(exc), status_code=400 if isinstance(exc, ValueError) else 500,
            context={"stage": "作品库", "action": getattr(handler, "__name__", "读取或修改作品数据")},
        )


@json_endpoint
def browse(body):
    return _call(service.browse, body)


@json_endpoint
def detail(body):
    return _call(service.detail, body)


@query_endpoint
def classifications(body):
    return _call(lambda _body, **kwargs: service.classifications_payload(**kwargs), body)


@json_endpoint
def save_classification(body):
    return _call(service.save_classification, body)


@json_endpoint
def classification_preview(body):
    return _call(service.preview_classification_edits, body)


@json_endpoint
def identities_preview(body):
    return _call(service.preview_identity_initialization, body)


@json_endpoint
def edits_preview(body):
    return _call(service.preview_edits, body)


@json_endpoint
def edits_apply(body):
    return _call(service.apply_edits, body)


@json_endpoint(network=True)
def provider_search(body):
    return _call(providers.search_provider, body)


@json_endpoint(network=True)
def provider_preview(body):
    return _call(providers.preview_provider_import, body)


@json_endpoint(network=True)
def provider_cover_preview(body):
    return _call(providers.preview_provider_cover, body)


@json_endpoint
def provider_apply(body):
    return _call(providers.apply_provider_import, body)


async def asset(request):
    """Never serves arbitrary files or performs network I/O on an image GET."""
    filename = request.path_params.get("filename", "")
    try:
        data, mime = await request.app.state.api_disk_queue.run(assets.read_asset, filename)
    except (ValueError, OSError, RuntimeError):
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    etag = '"' + filename.split(".", 1)[0] + '"'
    headers = {"Cache-Control": "private, max-age=31536000, immutable", "ETag": etag,
               "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(data))
    return Response(b"" if request.method == "HEAD" else data, media_type=mime, headers=headers)
