"""本地浏览 JP TV 作品 YAML：`starlette` + `uvicorn`。

启动：`work-catalog jp-tv browse [--config 路径]`（需 ``pip install 'work-catalog-yaml[web]'``）。

配置：工程内 ``config/framework/app.yaml`` 与 ``config/features/*/config.yaml``。
保存备份在 ``data/features/<feature-id>/history``。

静态页默认来自 ``apps/framework/frontend``，页签前端来自 ``apps/features/*/frontend``。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, Router
from starlette.staticfiles import StaticFiles

from work_catalog_yaml.layout import (
    ensure_feature_backend_paths,
    feature_frontend_root,
    framework_frontend_root,
)

ensure_feature_backend_paths()

from collection_info.service import (
    collection_records_payload,
    save_collection_records_from_ui_body,
)
from collection_detail.payload import build_jp_tv_browse_payload_single_group_order
from collection_detail.save import (
    annotate_save_capabilities,
    browse_apply_enum_edits_from_ui_body,
    browse_save_yaml_from_ui_body,
    history_catalog_root,
)
from collection_detail.link_index import (
    apply_link_index_associations_from_ui_body,
    apply_link_index_target_fixes_from_ui_body,
    collection_link_index_config_json,
    collection_link_index_payload,
    generate_link_index_from_ui_body,
    link_index_association_payload,
    open_link_index_path_from_ui_body,
    preview_link_index_from_ui_body,
    reject_link_index_association_from_ui_body,
    resource_libraries_node_payload,
    resolve_link_index_path_from_ui_body,
    resource_libraries_cached_payload,
    resource_libraries_search_payload,
    save_resource_library_roots_from_ui_body,
    save_link_index_from_ui_body,
    scan_resource_libraries_payload,
)
from work_catalog_yaml.jp_tv.browse_settings import (
    JpTvBrowseSettings,
    browse_config_candidates_hmsg,
    get_resolved_browse_settings,
    jp_tv_browse_app_config_json,
    jp_tv_browse_merged_enum_bundle_for_api,
    jp_tv_yaml_catalog_relpath,
)
from work_catalog_yaml.jp_tv.validate import load_jp_tv_entries_from_yaml
from work_catalog_yaml.yaml_io import load_yaml_string


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


def _normalized_catalog_rel_key(raw: str) -> str:
    return raw.replace("\\", "/").strip().lstrip("/")


def _canonical_catalog_yaml_entries(
    settings: JpTvBrowseSettings,
) -> list[tuple[str, Path]]:
    """与查找表一致的 (规范化键, 绝对路径) 列表；供 /api/config 与 POST catalog 共用。"""
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for abs_ps in settings.resolved_catalog_yaml_paths:
        fp = Path(abs_ps).resolve()
        if not fp.is_file():
            continue
        try:
            rk = jp_tv_yaml_catalog_relpath(settings, fp)
        except (OSError, ValueError):
            rk = fp.name
        k = _normalized_catalog_rel_key(rk)
        if k in seen:
            continue
        seen.add(k)
        out.append((k, fp))
    return out


def _catalog_relpath_lookup(settings: JpTvBrowseSettings) -> dict[str, Path]:
    return {k: fp for k, fp in _canonical_catalog_yaml_entries(settings)}


def _resolve_path_in_catalog_lookup(lk: dict[str, Path], raw: str) -> Path | None:
    """将客户端提交的 ``raw`` 解析为 DB 数据文件（兼容路径大小写、仅文件名等）。"""
    nk = _normalized_catalog_rel_key(raw)
    hit = lk.get(nk)
    if hit is not None:
        return hit
    for k, fp in lk.items():
        if k.lower() == nk.lower():
            return fp
    base = Path(nk.replace("\\", "/")).name
    if base and base != nk:
        bh = lk.get(base)
        if bh is not None:
            return bh
        for k, fp in lk.items():
            if k.lower() == base.lower():
                return fp
    return None


def _build_catalog_browse_payload(
    settings: JpTvBrowseSettings,
    abs_paths_ordered: list[Path],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    agg_entries: list[Any] = []
    row_meta_accum: list[tuple[str | None, int]] = []
    sources_loaded: list[dict[str, int | str]] = []
    resolved_files: list[Path] = []

    for abs_s in abs_paths_ordered:
        fp = Path(abs_s).resolve()
        if not fp.is_file():
            raise OSError(f"数据文件不存在：{fp.name}")
        raw = fp.read_text(encoding="utf-8")
        doc = load_yaml_string(raw)
        works = load_jp_tv_entries_from_yaml(doc)
        try:
            yrel = jp_tv_yaml_catalog_relpath(settings, fp)
        except (OSError, ValueError):
            yrel = fp.name
        sources_loaded.append({"relpath": yrel, "count": len(works)})
        for i, ent in enumerate(works):
            agg_entries.append(ent)
            row_meta_accum.append((yrel, i))
        resolved_files.append(fp)

    fname_disp = (
        resolved_files[0].name
        if len(resolved_files) == 1
        else f"DB 聚合 {len(resolved_files)} 个 YAML"
    )
    payload = build_jp_tv_browse_payload_single_group_order(
        agg_entries,
        row_meta=row_meta_accum,
        filename=fname_disp,
    )
    if payload.get("ok"):
        payload["sources_loaded"] = sources_loaded
    ann = tuple(str(p.resolve()) for p in resolved_files)
    return payload, ann


class _BrowseNoCacheStaticMiddleware(BaseHTTPMiddleware):
    """避免浏览器长期缓存旧 ``browse.js`` / ``index.html`` 导致页面行为与仓库不一致。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            return response
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response


async def _get_config_api(_: Request) -> JSONResponse:
    try:
        st, cfg_path = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse(
            {
                "error": str(e),
                "help": browse_config_candidates_hmsg(),
                "config_parse_error": True,
                "config_used": None,
                "paths": {
                    "filesystem_root": "",
                    "catalog_yaml_relpaths": [],
                    "history_root": "",
                },
                "link_index": {},
                "default_load": {
                    "enabled": False,
                    "resolved_path": "",
                    "catalog_yaml_count": 0,
                    "multi_file": False,
                },
                "enum_options": {},
                "enum_labels": {},
                "enum_section_labels": {},
                "app": {"features": []},
            },
            status_code=500,
        )
    eo_json, el_json, esl_json = jp_tv_browse_merged_enum_bundle_for_api(st)
    cat_rels = [k for k, _ in _canonical_catalog_yaml_entries(st)]
    nl = len(cat_rels)
    hist_root_s = ""
    try:
        if st.filesystem_root is not None:
            hist_root_s = str(history_catalog_root(st))
    except (OSError, ValueError):
        hist_root_s = ""
    return JSONResponse(
        {
            "help": browse_config_candidates_hmsg(),
            "config_used": str(cfg_path) if cfg_path else None,
            "paths": {
                "filesystem_root": str(st.filesystem_root) if st.filesystem_root else "",
                "catalog_yaml_relpaths": cat_rels,
                "history_root": hist_root_s,
            },
            "link_index": collection_link_index_config_json(),
            "default_load": {
                "enabled": st.default_load_enabled(),
                "resolved_path": st.resolved_default_readable or "",
                "catalog_yaml_count": nl,
                "multi_file": nl > 1,
            },
            "enum_options": eo_json,
            "enum_labels": el_json,
            "enum_section_labels": esl_json,
            "app": jp_tv_browse_app_config_json(st),
        }
    )


async def _get_browse_default_api(_: Request) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if not st.default_load_enabled():
        return JSONResponse(
            {
                "ok": False,
                "error": "未配置可用的数据：请在配置中设置 paths.filesystem_root（数据 DB 目录），"
                "且该目录下需存在至少一个 ``*.yaml``。",
            },
            status_code=400,
        )
    abs_paths = [Path(abs_s) for abs_s in st.resolved_catalog_yaml_paths]
    try:
        payload, _ann = _build_catalog_browse_payload(st, abs_paths)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"无法读取数据：{e}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"解析失败：{e}"}, status_code=400)

    if not payload.get("ok"):
        return JSONResponse(payload, status_code=400)

    annotate_save_capabilities(
        payload,
        yaml_disk_abs=None,
        settings=st,
        catalog_default=True,
        catalog_disk_abs_paths=None,
    )
    return JSONResponse(payload)


async def _post_browse_catalog_api(request: Request) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if not st.default_load_enabled():
        return JSONResponse(
            {
                "ok": False,
                "error": "未配置可用的数据：请在配置中设置 paths.filesystem_root（数据 DB 目录），"
                "且该目录下需存在至少一个 ``*.yaml``。",
            },
            status_code=400,
        )
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体须为 JSON，且含 paths 数组"}, status_code=400)

    paths = body.get("paths")
    if not isinstance(paths, list) or len(paths) == 0:
        return JSONResponse(
            {"ok": False, "error": "paths 须为非空的字符串数组（数据相对路径，与配置中列出的一致）"},
            status_code=400,
        )

    lk = _catalog_relpath_lookup(st)
    chosen: list[Path] = []
    seen_canon: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            return JSONResponse(
                {"ok": False, "error": "paths 每项须为非空字符串"},
                status_code=400,
            )
        fp = _resolve_path_in_catalog_lookup(lk, raw)
        if fp is None:
            return JSONResponse(
                {"ok": False, "error": f"不在当前 DB 数据列表中：{raw}"},
                status_code=400,
            )
        canon = str(fp.resolve())
        if canon in seen_canon:
            continue
        seen_canon.add(canon)
        chosen.append(fp)

    if not chosen:
        return JSONResponse({"ok": False, "error": "未选中任何有效数据文件"}, status_code=400)

    try:
        payload, abs_ann = _build_catalog_browse_payload(st, chosen)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"无法读取数据：{e}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"解析失败：{e}"}, status_code=400)

    if not payload.get("ok"):
        return JSONResponse(payload, status_code=400)

    annotate_save_capabilities(
        payload,
        yaml_disk_abs=None,
        settings=st,
        catalog_default=True,
        catalog_disk_abs_paths=abs_ann,
    )
    return JSONResponse(payload)


async def _post_browse_api(request: Request) -> JSONResponse:
    try:
        form = await request.form()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"无法解析 multipart：{e}"}, status_code=400)

    parts: list[Any] = list(form.getlist("file"))
    if not parts:
        single = form.get("file")
        if single is not None:
            parts = [single]

    uploads: list[Any] = []
    for p in parts:
        if p is None:
            continue
        if hasattr(p, "read") and callable(getattr(p, "read", None)):
            uploads.append(p)

    if not uploads:
        return JSONResponse(
            {"ok": False, "error": "请选择上传字段 file（.yaml），可一次上传多个"},
            status_code=400,
        )

    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    agg_entries: list[Any] = []
    row_meta_accum: list[tuple[str | None, int]] = []
    sources_loaded: list[dict[str, int | str]] = []
    yrel_count: dict[str, int] = {}

    def disambig_relpath(raw_name: str) -> str:
        base = Path(str(raw_name or "").strip() or "unnamed.yaml").name
        n = yrel_count.get(base, 0) + 1
        yrel_count[base] = n
        if n == 1:
            return base
        stem, suf = Path(base).stem, Path(base).suffix
        return f"{stem}__{n}{suf}"

    for up in uploads:
        fname = getattr(up, "filename", None) or ""
        raw_bytes = await up.read()
        if not raw_bytes.strip():
            return JSONResponse(
                {"ok": False, "error": f"空文件：{fname or '（未命名）'}"},
                status_code=400,
            )
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return JSONResponse(
                {"ok": False, "error": f"{fname or '文件'} 须为 UTF-8 文本：{e}"},
                status_code=400,
            )
        yrel = disambig_relpath(fname)
        try:
            doc = load_yaml_string(text)
            works = load_jp_tv_entries_from_yaml(doc)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": f"{yrel}：{e}"}, status_code=400)
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"{yrel}：YAML / 条目解析失败：{e}"},
                status_code=400,
            )
        sources_loaded.append({"relpath": yrel, "count": len(works)})
        for i, ent in enumerate(works):
            agg_entries.append(ent)
            row_meta_accum.append((yrel, i))

    fname_disp = (
        Path(getattr(uploads[0], "filename", "") or "").name
        if len(uploads) == 1
        else f"上传聚合 {len(uploads)} 个 YAML"
    )
    try:
        payload = build_jp_tv_browse_payload_single_group_order(
            agg_entries,
            row_meta=row_meta_accum,
            filename=fname_disp,
        )
        if payload.get("ok"):
            payload["sources_loaded"] = sources_loaded
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"组装浏览数据失败：{e}"}, status_code=400)

    annotate_save_capabilities(payload, yaml_disk_abs=None, settings=st)
    return JSONResponse(payload)


async def _post_browse_save_api(request: Request) -> JSONResponse:
    body: dict
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体须为 JSON 对象"}, status_code=400)

    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        batch = browse_save_yaml_from_ui_body(body, settings=st)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"无权限写入：{e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"写盘失败：{e}"}, status_code=500)

    hist_root_s = str(history_catalog_root(st))
    writes_out = [{"path": str(p), "history_file": h} for p, h in batch]
    first = batch[0] if batch else None
    return JSONResponse(
        {
            "ok": True,
            "writes": writes_out,
            "saved_path": str(first[0]) if first else "",
            "history_file": first[1] if first else "",
            "history_dirs": [hist_root_s],
            "history_dir": hist_root_s,
        },
    )


async def _post_config_enum_edits_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体须为 JSON 对象"}, status_code=400)

    try:
        st, cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        result = browse_apply_enum_edits_from_ui_body(
            body,
            settings=st,
            config_path=cfg_used,
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"无权限写入：{e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"写盘失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, **result})


async def _get_collection_records_api(_: Request) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse(collection_records_payload(st))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read collection records failed: {e}"}, status_code=500)


async def _post_collection_records_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)

    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        result = save_collection_records_from_ui_body(body, settings=st)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write collection records denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write collection records failed: {e}"}, status_code=500)
    return JSONResponse({"ok": True, **result})


async def _get_collection_detail_link_index_api(request: Request) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        refresh_links = str(request.query_params.get("refresh_links") or "").lower() in {"1", "true", "yes"}
        lite = str(request.query_params.get("lite") or "").lower() in {"1", "true", "yes"}
        return JSONResponse(collection_link_index_payload(st, refresh_links=refresh_links, lite=lite))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read link index failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_preview_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **preview_link_index_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"preview link index failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_save_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **save_link_index_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write link index denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write link index failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_generate_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **generate_link_index_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write shortcut denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write shortcut failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_open_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        return JSONResponse({"ok": True, **open_link_index_path_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"路径不存在：{e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"open path failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_resolve_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        return JSONResponse({"ok": True, **resolve_link_index_path_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"路径不存在：{e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"resolve link failed: {e}"}, status_code=500)


async def _get_collection_detail_link_index_associations_api(request: Request) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        resolve_targets = str(request.query_params.get("resolve_targets") or "").lower() in {"1", "true", "yes"}
        refresh_links = str(request.query_params.get("refresh_links") or "").lower() in {"1", "true", "yes"}
        return JSONResponse(
            link_index_association_payload(st, resolve_targets=resolve_targets, refresh_links=refresh_links)
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read link associations failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_associations_apply_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **apply_link_index_associations_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write link associations denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write link associations failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_associations_reject_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **reject_link_index_association_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write link association reject denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write link association reject failed: {e}"}, status_code=500)


async def _post_collection_detail_link_index_fixes_apply_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **apply_link_index_target_fixes_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write link target fixes denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write link target fixes failed: {e}"}, status_code=500)


async def _post_collection_detail_resource_libraries_config_api(request: Request) -> JSONResponse:
    try:
        body_any = await request.json()
        if not isinstance(body_any, dict):
            raise TypeError()
        body = body_any
    except Exception:
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)
    try:
        return JSONResponse({"ok": True, **save_resource_library_roots_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write resource library config denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write resource library config failed: {e}"}, status_code=500)


async def _get_collection_detail_resource_libraries_scan_api(_: Request) -> JSONResponse:
    try:
        return JSONResponse(scan_resource_libraries_payload())
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"scan resource libraries failed: {e}"}, status_code=500)


async def _get_collection_detail_resource_libraries_cache_api(_: Request) -> JSONResponse:
    try:
        return JSONResponse(resource_libraries_cached_payload())
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read resource library cache failed: {e}"}, status_code=500)


async def _get_collection_detail_resource_libraries_node_api(request: Request) -> JSONResponse:
    relpath = request.query_params.get("relpath", "")
    try:
        return JSONResponse(resource_libraries_node_payload(relpath))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"resource library node cache missing: {e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read resource library node failed: {e}"}, status_code=500)


async def _get_collection_detail_resource_libraries_search_api(request: Request) -> JSONResponse:
    query = request.query_params.get("q", "")
    try:
        return JSONResponse(resource_libraries_search_payload(query))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"resource library search cache missing: {e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"search resource libraries failed: {e}"}, status_code=500)


def build_jp_tv_browse_app() -> Starlette:
    static_dir = str(_resolve_browse_static_dir())
    collection_detail_frontend = str(_resolve_feature_frontend_dir("collection-detail"))
    collection_info_frontend = str(_resolve_feature_frontend_dir("collection-info"))
    jp_tv_browse_api = Router(
        routes=[
            Route("/config", endpoint=_get_config_api, methods=["GET"]),
            Route("/browse/default", endpoint=_get_browse_default_api, methods=["GET"]),
            Route("/browse/catalog", endpoint=_post_browse_catalog_api, methods=["POST"]),
            Route("/browse/save", endpoint=_post_browse_save_api, methods=["POST"]),
            Route("/config/enum-edits", endpoint=_post_config_enum_edits_api, methods=["POST"]),
            Route("/collection-info", endpoint=_get_collection_records_api, methods=["GET"]),
            Route("/collection-info", endpoint=_post_collection_records_api, methods=["POST"]),
            Route("/collection-records", endpoint=_get_collection_records_api, methods=["GET"]),
            Route("/collection-records", endpoint=_post_collection_records_api, methods=["POST"]),
            Route(
                "/collection-detail/link-index",
                endpoint=_get_collection_detail_link_index_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/link-index/preview",
                endpoint=_post_collection_detail_link_index_preview_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/save",
                endpoint=_post_collection_detail_link_index_save_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/generate",
                endpoint=_post_collection_detail_link_index_generate_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/open",
                endpoint=_post_collection_detail_link_index_open_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/resolve",
                endpoint=_post_collection_detail_link_index_resolve_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/associations",
                endpoint=_get_collection_detail_link_index_associations_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/link-index/associations/apply",
                endpoint=_post_collection_detail_link_index_associations_apply_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/associations/reject",
                endpoint=_post_collection_detail_link_index_associations_reject_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/link-index/fixes/apply",
                endpoint=_post_collection_detail_link_index_fixes_apply_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/resource-libraries/config",
                endpoint=_post_collection_detail_resource_libraries_config_api,
                methods=["POST"],
            ),
            Route(
                "/collection-detail/resource-libraries/scan",
                endpoint=_get_collection_detail_resource_libraries_scan_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/resource-libraries/cache",
                endpoint=_get_collection_detail_resource_libraries_cache_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/resource-libraries/node",
                endpoint=_get_collection_detail_resource_libraries_node_api,
                methods=["GET"],
            ),
            Route(
                "/collection-detail/resource-libraries/search",
                endpoint=_get_collection_detail_resource_libraries_search_api,
                methods=["GET"],
            ),
            Route("/browse", endpoint=_post_browse_api, methods=["POST"]),
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
            "/",
            app=StaticFiles(directory=static_dir, html=True),
            name="jp_tv_browse_static",
        ),
    ]
    return Starlette(
        routes=routes,
        middleware=[
            Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
            Middleware(_BrowseNoCacheStaticMiddleware),
        ],
    )


app = build_jp_tv_browse_app()
