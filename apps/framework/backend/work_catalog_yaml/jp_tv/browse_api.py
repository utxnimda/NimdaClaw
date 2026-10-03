"""Synchronous browse response builders, independent of request/worker scheduling."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse
from work_catalog_yaml.api_runtime import json_endpoint, query_endpoint, upload_endpoint
from work_catalog_yaml.operation_progress import report_progress
from work_catalog_yaml.layout import ensure_feature_backend_paths

ensure_feature_backend_paths()

from collection_info.service import (
    collection_records_payload,
    save_collection_records_from_ui_body,
)
from collection_detail.payload import build_jp_tv_browse_payload_single_group_order
from collection_detail.work_detail import (
    CatalogDetailStaleError,
    json_work_record,
    raw_work_records,
    work_detail_payload,
)
from collection_detail.save import (
    CatalogRollbackConflictError,
    annotate_save_capabilities,
    browse_apply_enum_edits_from_ui_body,
    browse_save_yaml_from_ui_body,
    history_catalog_root,
)
from collection_detail.link_index import (
    apply_link_index_target_fixes_from_ui_body,
    collection_link_index_config_json,
    collection_link_index_payload,
    generate_link_index_files_from_ui_body,
    generate_link_index_from_ui_body,
    open_collection_press_path_from_ui_body,
    open_link_index_path_from_ui_body,
    preview_link_index_from_ui_body,
    resource_libraries_node_payload,
    resolve_link_index_path_from_ui_body,
    resource_libraries_cached_payload,
    resource_libraries_search_payload,
    save_resource_library_roots_from_ui_body,
    save_link_index_from_ui_body,
    scan_resource_libraries_payload,
    validate_link_index_from_ui_body,
)
from media_directory_organizer.web import (
    apply_organizer_catalog_shortcut_repair_from_ui_body,
    apply_organizer_landing_from_ui_body,
    apply_organizer_landing_shortcuts_from_ui_body,
    apply_organizer_from_ui_body,
    organizer_config_payload,
    preview_organizer_catalog_shortcut_repair_from_ui_body,
    preview_organizer_landing_from_ui_body,
    preview_organizer_landing_shortcuts_from_ui_body,
    preview_organizer_from_ui_body,
    suggest_organizer_landing_from_ui_body,
)
from media_directory_organizer.service import MediaRollbackError
from work_catalog_yaml.media_groups import media_group_registry_api_payload
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

    for file_number, abs_s in enumerate(abs_paths_ordered):
        fp = Path(abs_s).resolve()
        report_progress("读取作品数据库", completed=file_number, total=len(abs_paths_ordered), unit="文件", detail=fp.name)
        if not fp.is_file():
            raise OSError(f"数据文件不存在：{fp.name}")
        raw_bytes = fp.read_bytes()
        doc = load_yaml_string(raw_bytes.decode("utf-8"))
        works = load_jp_tv_entries_from_yaml(doc)
        try:
            yrel = jp_tv_yaml_catalog_relpath(settings, fp)
        except (OSError, ValueError):
            yrel = fp.name
        sources_loaded.append({"relpath": yrel, "count": len(works), "sha256": hashlib.sha256(raw_bytes).hexdigest()})
        for i, ent in enumerate(works):
            agg_entries.append(ent)
            row_meta_accum.append((yrel, i))
        resolved_files.append(fp)

    report_progress("汇总作品列表", completed=len(abs_paths_ordered), total=len(abs_paths_ordered), unit="文件", detail=f"共 {len(agg_entries)} 部作品")

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


@query_endpoint
def _get_config_api(query: dict[str, str]) -> JSONResponse:
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
    group_registry = media_group_registry_api_payload()
    registered_codes = [str(code) for code in group_registry.get("press_group_codes", [])]
    existing_group_codes = list(eo_json.get("press_group") or [])
    eo_json["press_group"] = list(dict.fromkeys([*existing_group_codes, *registered_codes]))
    group_labels = dict(el_json.get("press_group") or {})
    for option in group_registry.get("options", []):
        if isinstance(option, dict) and option.get("code"):
            group_labels[str(option["code"])] = str(option.get("label") or option["code"])
    el_json["press_group"] = group_labels
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
            "group_registry": group_registry,
            "app": jp_tv_browse_app_config_json(st),
        }
    )


@query_endpoint
def _get_browse_default_api(query: dict[str, str]) -> JSONResponse:
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


@json_endpoint
def _post_browse_catalog_api(body: dict[str, Any]) -> JSONResponse:
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


@upload_endpoint
def _post_browse_api(uploads: list[tuple[str, bytes]]) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    agg_entries: list[Any] = []
    row_meta_accum: list[tuple[str | None, int]] = []
    sources_loaded: list[dict[str, int | str]] = []
    uploaded_records: dict[tuple[str, int], dict[str, Any]] = {}
    yrel_count: dict[str, int] = {}

    def disambig_relpath(raw_name: str) -> str:
        base = Path(str(raw_name or "").strip() or "unnamed.yaml").name
        n = yrel_count.get(base, 0) + 1
        yrel_count[base] = n
        if n == 1:
            return base
        stem, suf = Path(base).stem, Path(base).suffix
        return f"{stem}__{n}{suf}"

    for file_number, (fname, raw_bytes) in enumerate(uploads):
        report_progress("解析上传的作品数据", completed=file_number, total=len(uploads), unit="文件", detail=fname)
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
            for index, record in enumerate(raw_work_records(doc)):
                uploaded_records[(yrel, index)] = json_work_record(record)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": f"{yrel}：{e}"}, status_code=400)
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"{yrel}：YAML / 条目解析失败：{e}"},
                status_code=400,
            )
        sources_loaded.append({"relpath": yrel, "count": len(works), "sha256": hashlib.sha256(raw_bytes).hexdigest()})
        for i, ent in enumerate(works):
            agg_entries.append(ent)
            row_meta_accum.append((yrel, i))

    fname_disp = (
        Path(uploads[0][0]).name
        if len(uploads) == 1
        else f"上传聚合 {len(uploads)} 个 YAML"
    )
    try:
        report_progress("组装上传作品列表", completed=len(uploads), total=len(uploads), unit="文件", detail=f"共 {len(agg_entries)} 部作品")
        payload = build_jp_tv_browse_payload_single_group_order(
            agg_entries,
            row_meta=row_meta_accum,
            filename=fname_disp,
        )
        if payload.get("ok"):
            payload["sources_loaded"] = sources_loaded
            for group in payload.get("profile_groups", []):
                for row in group.get("rows", []):
                    row["source_record"] = uploaded_records[(row["yaml_source_rel"], row["index_in_file"])]
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"组装浏览数据失败：{e}"}, status_code=400)

    annotate_save_capabilities(payload, yaml_disk_abs=None, settings=st)
    return JSONResponse(payload)


@json_endpoint
def _post_collection_detail_work_detail_api(body: dict[str, Any]) -> JSONResponse:
    try:
        settings, _cfg_used = get_resolved_browse_settings()
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    try:
        return JSONResponse(work_detail_payload(body, settings))
    except CatalogDetailStaleError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "code": "catalog-source-changed", "reload_required": True},
            status_code=409,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"无法读取作品数据：{exc}"}, status_code=400)


@json_endpoint
def _post_browse_save_api(body: dict[str, Any]) -> JSONResponse:
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


@json_endpoint
def _post_config_enum_edits_api(body: dict[str, Any]) -> JSONResponse:
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


@query_endpoint
def _get_collection_records_api(query: dict[str, str]) -> JSONResponse:
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


@json_endpoint
def _post_collection_records_api(body: dict[str, Any]) -> JSONResponse:
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


@query_endpoint
def _get_collection_detail_link_index_api(query: dict[str, str]) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        refresh_links = str(query.get("refresh_links") or "").lower() in {"1", "true", "yes"}
        lite = str(query.get("lite") or "").lower() in {"1", "true", "yes"}
        return JSONResponse(collection_link_index_payload(st, refresh_links=refresh_links, lite=lite))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read link index failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_link_index_preview_api(body: dict[str, Any]) -> JSONResponse:
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


@json_endpoint
def _post_collection_detail_link_index_save_api(body: dict[str, Any]) -> JSONResponse:
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


@json_endpoint
def _post_collection_detail_link_index_generate_api(body: dict[str, Any]) -> JSONResponse:
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


@json_endpoint
def _post_collection_detail_link_index_generate_files_api(body: dict[str, Any]) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **generate_link_index_files_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"generate link index files denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"generate link index files failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_link_index_validate_api(body: dict[str, Any]) -> JSONResponse:
    try:
        st, _cfg_used = get_resolved_browse_settings()
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    try:
        return JSONResponse({"ok": True, **validate_link_index_from_ui_body(body, settings=st)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"validate link index denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"validate link index failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_link_index_open_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **open_link_index_path_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"路径不存在：{e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"open path failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_press_open_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **open_collection_press_path_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"路径不存在：{e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"open path failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_link_index_resolve_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **resolve_link_index_path_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"路径不存在：{e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"resolve link failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_link_index_fixes_apply_api(body: dict[str, Any]) -> JSONResponse:
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


@json_endpoint
def _post_collection_detail_resource_libraries_config_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse({"ok": True, **save_resource_library_roots_from_ui_body(body)})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": f"write resource library config denied: {e}"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"write resource library config failed: {e}"}, status_code=500)


@json_endpoint
def _post_collection_detail_resource_libraries_scan_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(scan_resource_libraries_payload())
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"scan resource libraries failed: {e}"}, status_code=500)


@query_endpoint
def _get_collection_detail_resource_libraries_cache_api(query: dict[str, str]) -> JSONResponse:
    try:
        return JSONResponse(resource_libraries_cached_payload())
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read resource library cache failed: {e}"}, status_code=500)


@query_endpoint
def _get_collection_detail_resource_libraries_node_api(query: dict[str, str]) -> JSONResponse:
    relpath = query.get("relpath", "")
    try:
        return JSONResponse(resource_libraries_node_payload(relpath))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"resource library node cache missing: {e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"read resource library node failed: {e}"}, status_code=500)


@query_endpoint
def _get_collection_detail_resource_libraries_search_api(query: dict[str, str]) -> JSONResponse:
    query = query.get("q", "")
    try:
        return JSONResponse(resource_libraries_search_payload(query))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"resource library search cache missing: {e}"}, status_code=404)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"search resource libraries failed: {e}"}, status_code=500)


@query_endpoint
def _get_media_directory_organizer_config_api(query: dict[str, str]) -> JSONResponse:
    try:
        return JSONResponse(organizer_config_payload())
    except (ValueError, OSError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@json_endpoint
def _post_media_directory_organizer_preview_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(preview_organizer_from_ui_body(body))
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"目录预览失败：{exc}"}, status_code=500)


@json_endpoint
def _post_media_directory_organizer_apply_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(apply_organizer_from_ui_body(body))
    except MediaRollbackError as exc:
        return JSONResponse(exc.to_payload(), status_code=409)
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": f"目录整理无权限：{exc}"}, status_code=403)
    except FileExistsError as exc:
        return JSONResponse({"ok": False, "error": f"目标文件冲突：{exc}"}, status_code=409)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"目录整理执行失败：{exc}"}, status_code=500)


@json_endpoint
def _post_media_directory_organizer_landing_preview_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(preview_organizer_landing_from_ui_body(body))
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"完整落地预览失败：{exc}"}, status_code=500)


@json_endpoint(network=True)
def _post_media_directory_organizer_landing_suggest_api(body: dict[str, Any]) -> JSONResponse:
    try:
        payload = suggest_organizer_landing_from_ui_body(body)
        return JSONResponse(payload)
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"智能识别失败：{exc}"}, status_code=500)


@json_endpoint
def _post_media_directory_organizer_landing_apply_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(apply_organizer_landing_from_ui_body(body))
    except MediaRollbackError as exc:
        return JSONResponse(exc.to_payload(), status_code=409)
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": f"完整落地无权限：{exc}"}, status_code=403)
    except FileExistsError as exc:
        return JSONResponse({"ok": False, "error": f"完整落地目标冲突：{exc}"}, status_code=409)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"完整落地失败：{exc}"}, status_code=500)


@json_endpoint
def _post_media_directory_organizer_landing_shortcuts_preview_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(preview_organizer_landing_shortcuts_from_ui_body(body))
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"快捷方式重试预览失败：{exc}"},
            status_code=500,
        )


@json_endpoint
def _post_media_directory_organizer_landing_shortcuts_apply_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(apply_organizer_landing_shortcuts_from_ui_body(body))
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except PermissionError as exc:
        return JSONResponse(
            {"ok": False, "error": f"快捷方式重试无权限：{exc}"},
            status_code=403,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"ok": False, "error": f"快捷方式路径冲突：{exc}"},
            status_code=409,
        )
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"快捷方式重试失败：{exc}"},
            status_code=500,
        )


@json_endpoint
def _post_media_directory_organizer_catalog_shortcut_repair_preview_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(preview_organizer_catalog_shortcut_repair_from_ui_body(body))
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"数据库/快捷方式修复预览失败：{exc}"},
            status_code=500,
        )


@json_endpoint
def _post_media_directory_organizer_catalog_shortcut_repair_apply_api(body: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(apply_organizer_catalog_shortcut_repair_from_ui_body(body))
    except MediaRollbackError as exc:
        return JSONResponse(exc.to_payload(), status_code=409)
    except CatalogRollbackConflictError as exc:
        return JSONResponse(
            {
                "ok": False,
                "state": "partial",
                "error": (
                    "快捷方式创建失败；数据库存在后续改动，"
                    f"这些改动已保留且未回滚：{exc}"
                ),
                "catalog": {
                    "state": "preserved",
                    "later_changes_preserved": True,
                    "rolled_back": False,
                },
            },
            status_code=409,
        )
    except (TypeError, ValueError, FileNotFoundError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except PermissionError as exc:
        return JSONResponse(
            {"ok": False, "error": f"数据库/快捷方式修复无权限：{exc}"},
            status_code=403,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"ok": False, "error": f"数据库/快捷方式修复冲突：{exc}"},
            status_code=409,
        )
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"数据库/快捷方式修复失败：{exc}"},
            status_code=500,
        )
