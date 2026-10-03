"""ASGI composition: routes, static resources, local-origin checks and worker lifecycle."""
from __future__ import annotations

import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles

from work_catalog_yaml.api_runtime import api_health, api_lifespan, install_api_queues
from work_catalog_yaml.operation_progress import api_operation_progress
from work_catalog_yaml.layout import feature_frontend_root, framework_frontend_root
from work_catalog_yaml.jp_tv.browse_security import is_loopback_hostname, mutation_source_is_allowed
from work_catalog_yaml.jp_tv import browse_api as api


def _resolve_browse_static_dir() -> Path:
    raw = os.environ.get("JP_TV_BROWSE_STATIC_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if not p.is_dir():
            raise ValueError(
                f"JP_TV_BROWSE_STATIC_DIR / --browse-static 不是目录：{p}（应指向 apps/framework/frontend）"
            )
        return p
    return framework_frontend_root()


def _resolve_feature_frontend_dir(feature_id: str) -> Path:
    p = feature_frontend_root(feature_id).resolve()
    if not p.is_dir():
        raise ValueError(f"feature frontend 目录不存在：{p}")
    return p


def browse_static_root() -> Path:
    """当前实际挂载的 ``browse_static`` 目录（用于日志 / 排错）。"""
    return _resolve_browse_static_dir()


class _BrowseNoCacheStaticMiddleware(BaseHTTPMiddleware):
    """避免浏览器长期缓存旧 ``browse.js`` / ``index.html`` 导致页面行为与仓库不一致。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            return response
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


class _BrowseLocalApiSecurityMiddleware(BaseHTTPMiddleware):
    """Keep the local filesystem API local and reject cross-site mutations."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        allow_remote = os.environ.get("JP_TV_BROWSE_ALLOW_REMOTE", "").strip() == "1"
        if not allow_remote and not is_loopback_hostname(request.url.hostname):
            return JSONResponse(
                {"ok": False, "error": "仅允许通过 localhost / 环回地址访问本地应用"},
                status_code=403,
            )

        is_api_mutation = request.url.path.startswith("/api") and request.method.upper() not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }
        is_progress_request = request.url.path.startswith("/api/operations/") or (
            request.url.path.startswith("/api") and "x-nimda-operation-id" in request.headers
        )
        if is_api_mutation or is_progress_request:
            fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
            if fetch_site == "cross-site":
                return JSONResponse(
                    {"ok": False, "error": "已拒绝跨站 API 请求"},
                    status_code=403,
                )
            origin = request.headers.get("origin", "").strip()
            if not mutation_source_is_allowed(
                fetch_site=fetch_site,
                origin=origin,
                request_scheme=request.url.scheme,
                request_host=request.headers.get("host", ""),
            ):
                return JSONResponse(
                    {"ok": False, "error": "API 请求来源与当前应用不一致"},
                    status_code=403,
                )

        return await call_next(request)


def build_jp_tv_browse_app() -> Starlette:
    static_dir = str(_resolve_browse_static_dir())
    collection_detail_frontend = str(_resolve_feature_frontend_dir("collection-detail"))
    collection_info_frontend = str(_resolve_feature_frontend_dir("collection-info"))
    media_directory_organizer_frontend = str(
        _resolve_feature_frontend_dir("media-directory-organizer")
    )
    jp_tv_browse_api = Router(
        routes=[
            Route("/health", endpoint=api_health, methods=["GET"]),
            Route("/operations/{operation_id}", endpoint=api_operation_progress, methods=["GET"]),
            Route("/config", endpoint=api._get_config_api, methods=["GET"]),
            Route("/browse/default", endpoint=api._get_browse_default_api, methods=["GET"]),
            Route("/browse/catalog", endpoint=api._post_browse_catalog_api, methods=["POST"]),
            Route("/browse/save", endpoint=api._post_browse_save_api, methods=["POST"]),
            Route(
                "/collection-detail/work/detail",
                endpoint=api._post_collection_detail_work_detail_api,
                methods=["POST"],
            ),
            Route("/config/enum-edits", endpoint=api._post_config_enum_edits_api, methods=["POST"]),
            Route("/collection-info", endpoint=api._get_collection_records_api, methods=["GET"]),
            Route("/collection-info", endpoint=api._post_collection_records_api, methods=["POST"]),
            Route("/collection-records", endpoint=api._get_collection_records_api, methods=["GET"]),
            Route("/collection-records", endpoint=api._post_collection_records_api, methods=["POST"]),
            Route(
                "/collection-detail/link-index",
                endpoint=api._get_collection_detail_link_index_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/link-index/preview",
                endpoint=api._post_collection_detail_link_index_preview_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/save",
                endpoint=api._post_collection_detail_link_index_save_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/generate",
                endpoint=api._post_collection_detail_link_index_generate_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/generate-files",
                endpoint=api._post_collection_detail_link_index_generate_files_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/validate",
                endpoint=api._post_collection_detail_link_index_validate_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/open",
                endpoint=api._post_collection_detail_link_index_open_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/press/open",
                endpoint=api._post_collection_detail_press_open_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/resolve",
                endpoint=api._post_collection_detail_link_index_resolve_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/fixes/apply",
                endpoint=api._post_collection_detail_link_index_fixes_apply_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/resource-libraries/config",
                endpoint=api._post_collection_detail_resource_libraries_config_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/resource-libraries/scan",
                endpoint=api._post_collection_detail_resource_libraries_scan_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/resource-libraries/cache",
                endpoint=api._get_collection_detail_resource_libraries_cache_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/resource-libraries/node",
                endpoint=api._get_collection_detail_resource_libraries_node_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/resource-libraries/search",
                endpoint=api._get_collection_detail_resource_libraries_search_api,
                methods=["GET"],
            ),
            Route(
                "/media-directory-organizer/config",
                endpoint=api._get_media_directory_organizer_config_api,
                methods=["GET"],
            ),
            Route(
                "/media-directory-organizer/preview",
                endpoint=api._post_media_directory_organizer_preview_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/apply",
                endpoint=api._post_media_directory_organizer_apply_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/suggest",
                endpoint=api._post_media_directory_organizer_landing_suggest_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/preview",
                endpoint=api._post_media_directory_organizer_landing_preview_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/apply",
                endpoint=api._post_media_directory_organizer_landing_apply_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/shortcuts/preview",
                endpoint=api._post_media_directory_organizer_landing_shortcuts_preview_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/shortcuts/apply",
                endpoint=api._post_media_directory_organizer_landing_shortcuts_apply_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/repair/preview",
                endpoint=api._post_media_directory_organizer_catalog_shortcut_repair_preview_api,
                methods=["POST"],
            ),
            Route(
                "/media-directory-organizer/landing/repair/apply",
                endpoint=api._post_media_directory_organizer_catalog_shortcut_repair_apply_api,
                methods=["POST"],
            ),
            Route("/browse", endpoint=api._post_browse_api, methods=["POST"]),
        ],
    )
    routes = [
        Mount("/api", app=jp_tv_browse_api),
        Mount(
            "/features/collection-detail",
            app=StaticFiles(directory=collection_detail_frontend),
            name="collection_detail_frontend",
        ),
        Mount(
            "/features/collection-info",
            app=StaticFiles(directory=collection_info_frontend),
            name="collection_info_frontend",
        ),
        Mount(
            "/features/media-directory-organizer",
            app=StaticFiles(directory=media_directory_organizer_frontend),
            name="media_directory_organizer_frontend",
        ),
        Mount(
            "/",
            app=StaticFiles(directory=static_dir, html=True),
            name="jp_tv_browse_static",
        ),
    ]
    app = Starlette(
        lifespan=api_lifespan,
        routes=routes,
        middleware=[
            Middleware(_BrowseLocalApiSecurityMiddleware),
            Middleware(_BrowseNoCacheStaticMiddleware),
        ],
    )
    install_api_queues(app)
    return app


app = build_jp_tv_browse_app()
