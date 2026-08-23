"""Build shortcut-index views from collection-detail catalog YAML."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import base64
import hashlib
import json
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ruamel.yaml import YAML

from work_catalog_yaml.jp_tv.browse_settings import (
    JpTvBrowseSettings,
    resolve_safe_yaml_under_root,
)
from work_catalog_yaml.jp_tv.validate import (
    TV_JP_PRESS_FORMAT_KEY,
    TV_JP_PRESS_GROUP_KEY,
    TV_JP_PRESS_PATH_KEY,
    entry_air_dates,
    entry_collection_type_data,
    entry_country_slug,
    entry_display_name,
    entry_domain_slug,
    entry_release_type_slug,
    jp_tv_press_pair_from_row,
    load_jp_tv_entries_from_yaml,
)
from work_catalog_yaml.layout import feature_config_path, feature_data_root
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml, load_yaml_string

from collection_detail.payload import build_collectioned_ordered
from collection_detail.save import (
    _assert_save_target_allowed,
    _works_list_mut,
    history_catalog_root,
    history_snapshot_name,
)


_INVALID_NAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEFAULT_LEVELS = ("{year_label}", "{date_range_label} {name}")
_DEFAULT_SHORTCUT_NAME = "{press_format}{press_group_suffix}"
_DEFAULT_LEGACY_MEDIA_ROOT = "E:/LinkVideo/[ACG] Japan"
_DEFAULT_SHORTCUT_ROOT = "E:/LinkVideo/[ACG] Japan/Finish"
_SHORTCUT_SCAN_CACHE: dict[str, Any] = {"signature": None, "leaves": []}
_LINK_INDEX_LITE_PAYLOAD_CACHE: dict[str, Any] = {"signature": None, "payload": None}
_FEATURE_CONFIG_CACHE: dict[str, Any] = {"signature": None, "data": None}
_RESOURCE_SCAN_CACHE_FILENAME = "resource-library-scan-cache.yaml"
_RESOURCE_SCAN_LEGACY_JSON_FILENAME = "resource-library-scan-cache.json"
_RESOURCE_SCAN_NODE_DIRNAME = "resource-library-scan-cache"
_RESOURCE_SCAN_METRICS_VERSION = 2
_SHORTCUT_SCAN_CACHE_FILENAME = "link-index-shortcut-scan-cache.yaml"
_LINK_INDEX_DB_FILENAME = "link-index.yaml"
_PRESS_ALIAS_TOKENS = ("VCB", "CK")


def _str_or_blank(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _invalidate_feature_config_cache() -> None:
    _FEATURE_CONFIG_CACHE["signature"] = None
    _FEATURE_CONFIG_CACHE["data"] = None


def _feature_config() -> dict[str, Any]:
    p = feature_config_path("collection-detail")
    try:
        st = p.stat()
    except OSError:
        _invalidate_feature_config_cache()
        return {}
    signature = (str(p.resolve()), int(st.st_mtime_ns), int(st.st_size))
    if _FEATURE_CONFIG_CACHE.get("signature") == signature:
        cached = _FEATURE_CONFIG_CACHE.get("data")
        return cast(dict[str, Any], cached) if isinstance(cached, dict) else {}
    raw = load_yaml(p)
    data = raw if isinstance(raw, dict) else {}
    _FEATURE_CONFIG_CACHE["signature"] = signature
    _FEATURE_CONFIG_CACHE["data"] = data
    return data


def _paths_config() -> dict[str, Any]:
    paths = _feature_config().get("paths")
    return paths if isinstance(paths, dict) else {}


def _link_index_config() -> dict[str, Any]:
    raw = _feature_config().get("link_index")
    return raw if isinstance(raw, dict) else {}


def _path_from_config(key: str, fallback: Path | str) -> Path:
    raw = _str_or_blank(_paths_config().get(key))
    return Path(raw or fallback).expanduser()


def _legacy_media_root() -> Path:
    """Resolve the pre-resource-library root used by older relative catalog paths."""
    return _path_from_config("media_root", _DEFAULT_LEGACY_MEDIA_ROOT).resolve()


def resource_roots() -> list[Path]:
    raw = _paths_config().get("resource_roots")
    values: list[str] = []
    if isinstance(raw, list):
        values = [_str_or_blank(x) for x in raw]
    elif isinstance(raw, str):
        values = [_str_or_blank(raw)]
    out: list[Path] = []
    seen: set[str] = set()
    for item in values:
        if not item:
            continue
        p = Path(item).expanduser().resolve()
        key = str(p).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    if out:
        return out
    return [_legacy_media_root()]


def _resource_roots_explicitly_configured() -> bool:
    raw = _paths_config().get("resource_roots")
    if isinstance(raw, str):
        return bool(_str_or_blank(raw))
    if isinstance(raw, list):
        return any(bool(_str_or_blank(x)) for x in raw)
    return False


def _normal_resource_excludes(raw: Any) -> list[str]:
    if raw is None:
        return []
    values: list[str] = []
    if isinstance(raw, str):
        text = raw.replace("；", ";").replace("，", ",")
        values = [x.strip() for part in text.splitlines() for x in part.replace(";", ",").split(",")]
    elif isinstance(raw, list):
        values = [_str_or_blank(x) for x in raw]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item or "").strip().strip("/\\")
        if not value:
            continue
        key = value.replace("\\", "/").casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _resource_root_config_key(root: Path | str) -> str:
    try:
        return str(Path(str(root)).expanduser().resolve()).casefold()
    except (OSError, ValueError):
        return str(root).casefold()


def resource_excludes() -> dict[str, list[str]]:
    raw = _paths_config().get("resource_excludes")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        excludes = _normal_resource_excludes(value)
        if excludes:
            out[_resource_root_config_key(str(key))] = excludes
    return out


def resource_excludes_for_root(root: Path | str) -> list[str]:
    return list(resource_excludes().get(_resource_root_config_key(root), []))


def shortcut_root() -> Path:
    return _path_from_config("shortcut_root", _DEFAULT_SHORTCUT_ROOT).resolve()


def _shortcut_root_profiles() -> list[dict[str, Any]]:
    raw = _paths_config().get("shortcut_roots")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        root_s = _str_or_blank(item.get("root") or item.get("path"))
        match = item.get("match")
        if not root_s or not isinstance(match, dict):
            continue
        normal_match = {
            key: _str_or_blank(match.get(key))
            for key in ("domain", "country", "release_type")
            if _str_or_blank(match.get(key))
        }
        if not normal_match:
            continue
        out.append(
            {
                "root": Path(root_s).expanduser().resolve(),
                "match": normal_match,
            }
        )
    return out


def shortcut_roots() -> list[Path]:
    out = [shortcut_root(), *[cast(Path, item["root"]) for item in _shortcut_root_profiles()]]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in out:
        key = _path_compare_key(root)
        if key and key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _shortcut_root_for_work(work: dict[str, Any]) -> Path:
    for profile in _shortcut_root_profiles():
        match = cast(dict[str, str], profile["match"])
        if all(_str_or_blank(work.get(key)) == expected for key, expected in match.items()):
            return cast(Path, profile["root"])
    return shortcut_root()


def _layout_levels() -> tuple[str, ...]:
    raw = _link_index_config().get("layout_levels")
    if isinstance(raw, list):
        out = tuple(_str_or_blank(x) for x in raw if _str_or_blank(x))
        if out:
            return out
    return _DEFAULT_LEVELS


def _shortcut_name_template() -> str:
    return _str_or_blank(_link_index_config().get("shortcut_name")) or _DEFAULT_SHORTCUT_NAME


def _scan_max_depth() -> int:
    raw = _link_index_config().get("scan_max_depth")
    if isinstance(raw, int) and raw >= 0:
        return min(raw, 8)
    return 4


def _scan_max_dirs() -> int:
    raw = _link_index_config().get("scan_max_dirs")
    if isinstance(raw, int) and raw > 0:
        return min(raw, 10000)
    return 2000


def _resource_scan_max_dirs() -> int:
    raw = _link_index_config().get("resource_scan_max_dirs")
    if isinstance(raw, int) and raw > 0:
        return min(raw, 200000)
    return 50000


def _resource_scan_max_depth() -> int:
    raw = _link_index_config().get("resource_scan_max_depth")
    if isinstance(raw, int) and raw >= 0:
        return min(raw, 12)
    return 1


def collection_link_index_config_json() -> dict[str, Any]:
    roots = resource_roots()
    shortcut_profiles = _shortcut_root_profiles()
    return {
        "resource_roots": [str(p) for p in roots],
        "resource_excludes": {str(p): resource_excludes_for_root(p) for p in roots},
        "shortcut_root": str(shortcut_root()),
        "shortcut_roots": [str(p) for p in shortcut_roots()],
        "shortcut_root_profiles": [
            {"root": str(item["root"]), "match": dict(cast(dict[str, str], item["match"]))}
            for item in shortcut_profiles
        ],
        "index_db_path": str(_link_index_db_path()),
        "layout_levels": list(_layout_levels()),
        "shortcut_name": _shortcut_name_template(),
        "resource_scan_max_dirs": _resource_scan_max_dirs(),
        "resource_scan_max_depth": _resource_scan_max_depth(),
        "storage": "catalog_yaml",
        "path_field": "attributes/data/path",
        "press_path_field": "attributes/data/collectioned/*/press_path",
    }


def _resource_scan_cache_path() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _RESOURCE_SCAN_CACHE_FILENAME).resolve()


def _shortcut_scan_cache_path() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _SHORTCUT_SCAN_CACHE_FILENAME).resolve()


def _link_index_db_path() -> Path:
    return (feature_data_root("collection-detail") / "db" / "index" / _LINK_INDEX_DB_FILENAME).resolve()


def _resource_scan_legacy_json_path() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _RESOURCE_SCAN_LEGACY_JSON_FILENAME).resolve()


def _resource_scan_node_dir() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _RESOURCE_SCAN_NODE_DIRNAME).resolve()


def _resource_scan_node_path(relpath: str) -> Path:
    digest = hashlib.sha1(str(relpath or "").encode("utf-8")).hexdigest()
    return (_resource_scan_node_dir() / digest[:2] / f"{digest[2:]}.yaml").resolve()


def _empty_resource_tree() -> dict[str, Any]:
    return {"type": "folder", "name": "资源库", "relpath": "", "path": "", "children": [], "children_loaded": True}


def _resource_scan_cache_empty() -> dict[str, Any]:
    return {
        "ok": True,
        "cached": False,
        "metrics_version": _RESOURCE_SCAN_METRICS_VERSION,
        "config": collection_link_index_config_json(),
        "summary": {
            "root_count": 0,
            "existing_root_count": 0,
            "series_count": 0,
            "item_count": 0,
            "dir_count": 0,
            "file_count": 0,
            "size": 0,
            "direct_child_count": 0,
            "total_child_count": 0,
            "truncated": False,
            "max_dirs": _resource_scan_max_dirs(),
        },
        "roots": [],
        "items": [],
        "tree": _empty_resource_tree(),
        "scanned_at": "",
        "cache_path": str(_resource_scan_cache_path()),
        "node_cache_dir": str(_resource_scan_node_dir()),
    }


def _resource_node_summary(node: dict[str, Any]) -> dict[str, Any]:
    children = [child for child in node.get("children", []) or [] if isinstance(child, dict)]
    files = [item for item in node.get("files", []) or [] if isinstance(item, dict)]
    out = {k: v for k, v in node.items() if k not in {"children", "files"}}
    out["children"] = []
    out["files"] = []
    out["children_loaded"] = False
    out["has_children"] = bool(children)
    out["child_count"] = len(children)
    out["direct_file_count"] = len(files)
    out.setdefault("direct_child_count", len(children) + len(files))
    out.setdefault(
        "total_child_count",
        int(out.get("dir_count") or 0) + int(out.get("file_count") or 0),
    )
    out.setdefault("size", 0)
    out.setdefault("mtime", 0)
    return out


def _resource_node_cache_payload(node: dict[str, Any]) -> dict[str, Any]:
    children = [child for child in node.get("children", []) or [] if isinstance(child, dict)]
    files = [item for item in node.get("files", []) or [] if isinstance(item, dict)]
    out = {k: v for k, v in node.items() if k not in {"children", "files"}}
    out["children"] = [_resource_node_summary(child) for child in children]
    out["files"] = files
    out["children_loaded"] = True
    out["has_children"] = bool(children)
    out["child_count"] = len(children)
    out["direct_file_count"] = len(files)
    out.setdefault("direct_child_count", len(children) + len(files))
    out.setdefault(
        "total_child_count",
        int(out.get("dir_count") or 0) + int(out.get("file_count") or 0),
    )
    out.setdefault("size", 0)
    out.setdefault("mtime", 0)
    return {
        "ok": True,
        "metrics_version": _RESOURCE_SCAN_METRICS_VERSION,
        "relpath": str(out.get("relpath") or ""),
        "node": out,
    }


def _resource_parent_relpath(relpath: str) -> str:
    rel = str(relpath or "")
    if not rel:
        return ""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _resource_entry_search_text(entry: dict[str, Any]) -> str:
    return "\n".join(
        str(entry.get(key) or "").casefold()
        for key in ("name", "relpath", "path", "error", "series_name", "work_name", "press_info")
    )


def _resource_entry_matches(entry: dict[str, Any], needle: str) -> bool:
    return bool(needle) and needle in _resource_entry_search_text(entry)


def _resource_search_tree_from_entries(entries: list[dict[str, Any]], query: str) -> tuple[dict[str, Any], dict[str, int]]:
    needle = query.casefold()
    folders = {str(item.get("relpath") or ""): item for item in entries if item.get("type") == "folder"}
    folder_keep: set[str] = {""}
    matched_folder_rels: set[str] = set()
    matched_files: list[dict[str, Any]] = []
    for entry in entries:
        if not _resource_entry_matches(entry, needle):
            continue
        if entry.get("type") == "folder":
            relpath = str(entry.get("relpath") or "")
            matched_folder_rels.add(relpath)
            cur = relpath
            while True:
                folder_keep.add(cur)
                if not cur:
                    break
                cur = str(folders.get(cur, {}).get("parent_relpath") or _resource_parent_relpath(cur))
        elif entry.get("type") == "file":
            matched_files.append(entry)
            cur = str(entry.get("parent_relpath") or _resource_parent_relpath(str(entry.get("relpath") or "")))
            while True:
                folder_keep.add(cur)
                if not cur:
                    break
                cur = str(folders.get(cur, {}).get("parent_relpath") or _resource_parent_relpath(cur))

    def folder_node(relpath: str) -> dict[str, Any]:
        raw = folders.get(relpath) or {"type": "folder", "name": "资源库", "relpath": relpath, "path": ""}
        return {
            "type": "folder",
            "name": str(raw.get("name") or ("资源库" if not relpath else relpath.rsplit("/", 1)[-1])),
            "relpath": relpath,
            "path": str(raw.get("path") or ""),
            "exists": bool(raw.get("exists", True)),
            "error": str(raw.get("error") or ""),
            "size": int(raw.get("size") or 0),
            "mtime": int(raw.get("mtime") or 0),
            "children": [],
            "files": [],
            "children_loaded": True,
            "has_children": False,
            "_resource_search_match": relpath in matched_folder_rels,
        }

    nodes = {relpath: folder_node(relpath) for relpath in folder_keep}
    for file_entry in matched_files:
        parent = str(file_entry.get("parent_relpath") or _resource_parent_relpath(str(file_entry.get("relpath") or "")))
        if parent not in nodes:
            nodes[parent] = folder_node(parent)
        nodes[parent]["files"].append(
            {
                "type": "file",
                "name": str(file_entry.get("name") or ""),
                "relpath": str(file_entry.get("relpath") or ""),
                "path": str(file_entry.get("path") or ""),
                "size": int(file_entry.get("size") or 0),
                "mtime": int(file_entry.get("mtime") or 0),
            }
        )

    rels_by_depth = sorted((rel for rel in nodes if rel), key=lambda x: (x.count("/"), x.casefold()))
    for relpath in rels_by_depth:
        parent = str(folders.get(relpath, {}).get("parent_relpath") or _resource_parent_relpath(relpath))
        if parent not in nodes:
            continue
        nodes[parent]["children"].append(nodes[relpath])

    def sort_and_count(node: dict[str, Any]) -> tuple[int, int, int]:
        node["children"].sort(key=lambda x: str(x.get("name") or "").casefold())
        node["files"].sort(key=lambda x: str(x.get("name") or "").casefold())
        dir_count = 0
        file_count = len(node["files"])
        size = sum(int(item.get("size") or 0) for item in node["files"])
        for child in node["children"]:
            child_dirs, child_files, child_size = sort_and_count(child)
            dir_count += 1 + child_dirs
            file_count += child_files
            size += child_size
        node["child_count"] = len(node["children"])
        node["direct_file_count"] = len(node["files"])
        node["direct_child_count"] = len(node["children"]) + len(node["files"])
        node["dir_count"] = dir_count
        node["file_count"] = file_count
        node["total_child_count"] = dir_count + file_count
        node["has_children"] = bool(node["children"] or node["files"])
        if not node.get("_resource_search_match") or not node.get("relpath"):
            node["size"] = size
        return dir_count, file_count, int(node.get("size") or 0)

    root = nodes.get("") or folder_node("")
    sort_and_count(root)
    return root, {
        "matched_folder_count": len(matched_folder_rels),
        "matched_file_count": len(matched_files),
        "matched_count": len(matched_folder_rels) + len(matched_files),
        "shown_folder_count": max(0, len(nodes) - 1),
        "shown_file_count": len(matched_files),
    }


def _resource_search_entries_from_main_cache(cache: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {
            "type": "folder",
            "name": "资源库",
            "relpath": "",
            "parent_relpath": "",
            "path": "",
            "size": int(((cache.get("summary") if isinstance(cache.get("summary"), dict) else {}) or {}).get("size") or 0),
        }
    ]
    roots = cache.get("roots") if isinstance(cache.get("roots"), list) else []
    for root_idx, root in enumerate(roots):
        if not isinstance(root, dict):
            continue
        root_rel = f"root:{root_idx}"
        root_entry = {
            "type": "folder",
            "name": str(root.get("root") or f"resource-root-{root_idx + 1}"),
            "relpath": root_rel,
            "parent_relpath": "",
            "path": str(root.get("root") or ""),
            "exists": bool(root.get("exists")),
            "error": str(root.get("error") or ""),
            "size": int(root.get("size") or 0),
            "mtime": int(root.get("mtime") or 0),
            "direct_child_count": int(root.get("direct_child_count") or 0),
            "total_child_count": int(root.get("total_child_count") or 0),
            "dir_count": int(root.get("dir_count") or 0),
            "file_count": int(root.get("file_count") or 0),
            "child_count": int(root.get("series_count") or 0),
            "direct_file_count": 0,
            "has_children": True,
        }
        entries.append(root_entry)
        series_rows = root.get("series") if isinstance(root.get("series"), list) else []
        for series in series_rows:
            if not isinstance(series, dict):
                continue
            series_name = str(series.get("name") or "")
            series_rel = f"{root_rel}/{series.get('relpath') or series_name}"
            child_rows = series.get("children") if isinstance(series.get("children"), list) else []
            entries.append(
                {
                    "type": "folder",
                    "name": series_name,
                    "relpath": series_rel,
                    "parent_relpath": root_rel,
                    "path": str(series.get("path") or ""),
                    "exists": True,
                    "error": str(series.get("error") or ""),
                    "size": int(series.get("size") or 0),
                    "mtime": int(series.get("mtime") or 0),
                    "direct_child_count": len(child_rows),
                    "total_child_count": len(child_rows),
                    "dir_count": len(child_rows),
                    "file_count": 0,
                    "child_count": len(child_rows),
                    "direct_file_count": 0,
                    "has_children": bool(child_rows),
                    "series_name": series_name,
                }
            )
            for child in child_rows:
                if not isinstance(child, dict):
                    continue
                child_name = str(child.get("name") or "")
                child_rel_raw = str(child.get("relpath") or "")
                child_rel = f"{root_rel}/{child_rel_raw}" if child_rel_raw else f"{series_rel}/{child_name}"
                entries.append(
                    {
                        "type": "folder",
                        "name": child_name,
                        "relpath": child_rel,
                        "parent_relpath": series_rel,
                        "path": str(child.get("path") or ""),
                        "exists": True,
                        "error": str(child.get("error") or ""),
                        "size": int(child.get("size") or 0),
                        "mtime": int(child.get("mtime") or 0),
                        "direct_child_count": 0,
                        "total_child_count": 0,
                        "dir_count": 0,
                        "file_count": 0,
                        "child_count": 0,
                        "direct_file_count": 0,
                        "has_children": False,
                        "series_name": str(child.get("series_name") or series_name),
                        "work_name": str(child.get("work_name") or ""),
                        "press_info": str(child.get("press_info") or ""),
                    }
                )
    return entries


def _write_resource_node_cache(node: dict[str, Any]) -> None:
    relpath = str(node.get("relpath") or "")
    path = _resource_scan_node_path(relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    if node.get("children_loaded") is False:
        return
    else:
        payload = _resource_node_cache_payload(node)
    path.write_text(dump_yaml_string(payload), encoding="utf-8")
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            _write_resource_node_cache(child)


def _load_resource_scan_cache() -> dict[str, Any]:
    path = _resource_scan_cache_path()
    if not path.is_file():
        out = _resource_scan_cache_empty()
        legacy = _resource_scan_legacy_json_path()
        if legacy.is_file():
            out["legacy_cache_path"] = str(legacy)
            out["cache_note"] = "检测到旧 JSON 缓存，请重新扫描生成 YAML 缓存。"
        return out
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        return _resource_scan_cache_empty()
    if int(raw.get("metrics_version") or 0) != _RESOURCE_SCAN_METRICS_VERSION:
        out = _resource_scan_cache_empty()
        out["stale_cache_path"] = str(path)
        out["cache_note"] = "资源库目录大小统计已更新，请重新扫描资源库。"
        return out
    raw["ok"] = True
    raw["cached"] = True
    raw["metrics_version"] = _RESOURCE_SCAN_METRICS_VERSION
    raw["cache_path"] = str(path)
    raw["node_cache_dir"] = str(_resource_scan_node_dir())
    raw["config"] = collection_link_index_config_json()
    raw.setdefault("summary", _resource_scan_cache_empty()["summary"])
    raw.setdefault("roots", [])
    raw.setdefault("items", [])
    raw.setdefault("tree", _empty_resource_tree())
    raw.setdefault("scanned_at", "")
    return raw


def _save_resource_scan_cache(payload: dict[str, Any]) -> None:
    path = _resource_scan_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    node_dir = _resource_scan_node_dir()
    if node_dir.exists():
        shutil.rmtree(node_dir)
    node_dir.mkdir(parents=True, exist_ok=True)

    cache_payload = dict(payload)
    cache_payload["ok"] = True
    cache_payload["cached"] = True
    cache_payload["metrics_version"] = _RESOURCE_SCAN_METRICS_VERSION
    cache_payload["cache_path"] = str(path)
    cache_payload["node_cache_dir"] = str(node_dir)
    tree = cache_payload.get("tree") if isinstance(cache_payload.get("tree"), dict) else _empty_resource_tree()
    _write_resource_node_cache(tree)
    cache_payload["tree"] = _resource_node_cache_payload(tree)["node"]
    path.write_text(dump_yaml_string(cache_payload), encoding="utf-8")

    legacy = _resource_scan_legacy_json_path()
    if legacy.is_file():
        legacy.unlink()


def resource_libraries_node_payload(relpath: str) -> dict[str, Any]:
    rel = str(relpath or "")
    path = _resource_scan_node_path(rel)
    if not path.is_file():
        live_node = _resource_live_node_from_relpath(rel)
        if live_node is not None:
            _write_resource_node_cache(live_node)
            return {"ok": True, "cached": False, "relpath": rel, "node": live_node}
        raise FileNotFoundError(rel or "资源库根目录")
    raw = load_yaml(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("node"), dict):
        raise ValueError("资源库目录缓存损坏，请重新扫描。")
    if int(raw.get("metrics_version") or 0) != _RESOURCE_SCAN_METRICS_VERSION:
        live_node = _resource_live_node_from_relpath(rel)
        if live_node is not None:
            _write_resource_node_cache(live_node)
            return {"ok": True, "cached": False, "relpath": rel, "node": live_node}
        raise ValueError("resource library node cache is stale; please rescan")
    node = raw["node"]
    if str(node.get("relpath") or "") != rel:
        raise ValueError("资源库目录缓存索引不一致，请重新扫描。")
    if node.get("children_loaded") is False and node.get("path"):
        node = _resource_live_node_for_relpath(node)
        _write_resource_node_cache(node)
    return {"ok": True, "cached": True, "relpath": rel, "node": node}


def _yaml_roundtrip() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.allow_unicode = True
    y.width = 10_000_000
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def _normal_resource_root_entries(raw: Any) -> tuple[list[str], dict[str, list[str]]]:
    if not isinstance(raw, list):
        raise ValueError("roots 必须为数组")
    roots: list[str] = []
    excludes: dict[str, list[str]] = {}
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        if isinstance(item, dict):
            value = _str_or_blank(item.get("path") or item.get("root") or item.get("value"))
            exclude_values = _normal_resource_excludes(item.get("excludes"))
        else:
            value = _str_or_blank(item)
            exclude_values = []
        if not value:
            continue
        try:
            path = Path(value).expanduser().resolve()
        except OSError as exc:
            raise ValueError(f"roots[{idx}] 不是有效目录路径：{value}") from exc
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        root_str = str(path)
        roots.append(root_str)
        if exclude_values:
            excludes[root_str] = exclude_values
    return roots, excludes


def save_resource_library_roots_from_ui_body(body: dict[str, Any]) -> dict[str, Any]:
    roots, excludes = _normal_resource_root_entries(body.get("roots"))
    cfg_path = feature_config_path("collection-detail")
    y = _yaml_roundtrip()
    if cfg_path.is_file():
        with cfg_path.open(encoding="utf-8") as fp:
            doc = y.load(fp) or {}
    else:
        doc = {"version": 1}
    if not isinstance(doc, dict):
        raise ValueError(f"collection-detail 配置必须为对象：{cfg_path}")
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        paths = {}
        doc["paths"] = paths
    paths["resource_roots"] = roots
    if excludes:
        paths["resource_excludes"] = excludes
    else:
        paths.pop("resource_excludes", None)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg_path.open("w", encoding="utf-8") as fp:
        y.dump(doc, fp)
    _invalidate_feature_config_cache()
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None
    return {"config": collection_link_index_config_json(), "config_path": str(cfg_path.resolve())}


def _split_resource_leaf_name(name: str) -> tuple[str, str]:
    raw = str(name or "").strip()
    if not raw:
        return "", ""
    for sep in ("_", "＿"):
        if sep in raw:
            left, right = raw.rsplit(sep, 1)
            return left.strip() or raw, right.strip()
    return raw, ""


def _resource_file_entry(root: Path, path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        size = int(st.st_size)
        mtime = int(st.st_mtime)
    except OSError:
        size = 0
        mtime = 0
    try:
        relpath = path.relative_to(root).as_posix()
    except ValueError:
        relpath = path.name
    return {
        "type": "file",
        "name": path.name,
        "relpath": relpath,
        "path": str(path),
        "size": size,
        "mtime": mtime,
    }


def _resource_dir_stat(path: Path) -> tuple[bool, int]:
    try:
        st = path.stat()
        return path.is_dir(), int(st.st_mtime)
    except OSError:
        return False, 0


def _resource_dir_metric_summary(
    root: Path,
    path: Path,
    excludes: list[str],
    shortcut_root_key: str,
) -> dict[str, int | bool]:
    size = 0
    direct_child_count = 0
    total_child_count = 0
    dir_count = 0
    file_count = 0
    stack = [path]
    first = True
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        if first:
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir and (
                    (shortcut_root_key and _path_compare_key(entry) == shortcut_root_key)
                    or _is_resource_scan_excluded(root, entry, excludes)
                ):
                    continue
                direct_child_count += 1
            first = False
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if shortcut_root_key and _path_compare_key(entry) == shortcut_root_key:
                    continue
                if _is_resource_scan_excluded(root, entry, excludes):
                    continue
                dir_count += 1
                total_child_count += 1
                stack.append(entry)
                continue
            try:
                size += int(entry.stat().st_size)
            except OSError:
                pass
            file_count += 1
            total_child_count += 1
    return {
        "size": size,
        "direct_child_count": direct_child_count,
        "total_child_count": total_child_count,
        "dir_count": dir_count,
        "file_count": file_count,
        "has_children": direct_child_count > 0,
    }


def _resource_shallow_dir_node(
    root: Path,
    path: Path,
    relpath: str,
    excludes: list[str],
    shortcut_root_key: str,
    root_name: str | None = None,
) -> dict[str, Any]:
    exists, mtime = _resource_dir_stat(path)
    metrics = (
        _resource_dir_metric_summary(root, path, excludes, shortcut_root_key)
        if exists
        else {
            "size": 0,
            "direct_child_count": 0,
            "total_child_count": 0,
            "dir_count": 0,
            "file_count": 0,
            "has_children": False,
        }
    )
    return {
        "type": "folder",
        "name": root_name or path.name,
        "relpath": relpath,
        "path": str(path),
        "exists": exists,
        "error": "" if exists else "directory missing",
        "children": [],
        "files": [],
        "children_loaded": False,
        "has_children": bool(metrics.get("has_children")),
        "size": int(metrics.get("size") or 0),
        "mtime": mtime,
        "direct_child_count": int(metrics.get("direct_child_count") or 0),
        "total_child_count": int(metrics.get("total_child_count") or 0),
        "dir_count": int(metrics.get("dir_count") or 0),
        "file_count": int(metrics.get("file_count") or 0),
        "series_count": int(metrics.get("dir_count") or 0),
        "item_count": int(metrics.get("file_count") or 0),
    }


def _resource_dir_node(
    root: Path,
    path: Path,
    relpath: str,
    excludes: list[str],
    shortcut_root_key: str,
    counters: dict[str, Any],
    max_dirs: int,
    root_name: str | None = None,
    depth: int = 0,
    max_depth: int | None = None,
) -> dict[str, Any]:
    exists, dir_mtime = _resource_dir_stat(path)
    node: dict[str, Any] = {
        "type": "folder",
        "name": root_name or path.name,
        "relpath": relpath,
        "path": str(path),
        "exists": exists,
        "error": "",
        "children": [],
        "files": [],
        "children_loaded": True,
        "size": 0,
        "mtime": dir_mtime,
        "direct_child_count": 0,
        "total_child_count": 0,
        "dir_count": 0,
        "file_count": 0,
        "series_count": 0,
        "item_count": 0,
    }
    if not node["exists"]:
        node["error"] = "目录不存在"
        return node
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
    except OSError as exc:
        node["error"] = str(exc)
        return node
    for entry in entries:
        if entry.is_dir():
            if shortcut_root_key and _path_compare_key(entry) == shortcut_root_key:
                continue
            if _is_resource_scan_excluded(root, entry, excludes):
                continue
            if counters["dirs"] >= max_dirs:
                counters["truncated"] = True
                continue
            counters["dirs"] += 1
            try:
                root_rel = relpath.split("/", 1)[0] if relpath.startswith("root:") else relpath
                child_rel = f"{root_rel}/{entry.relative_to(root).as_posix()}" if root_rel else entry.relative_to(root).as_posix()
            except ValueError:
                child_rel = f"{relpath}/{entry.name}"
            if max_depth is not None and depth >= max_depth:
                child = _resource_shallow_dir_node(root, entry, child_rel, excludes, shortcut_root_key)
            else:
                child = _resource_dir_node(
                    root,
                    entry,
                    child_rel,
                    excludes,
                    shortcut_root_key,
                    counters,
                    max_dirs,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            node["children"].append(child)
            node["dir_count"] += 1 + int(child.get("dir_count") or 0)
            node["file_count"] += int(child.get("file_count") or 0)
            node["size"] += int(child.get("size") or 0)
            node["direct_child_count"] += 1
            node["total_child_count"] += 1 + int(
                child.get("total_child_count")
                if child.get("total_child_count") is not None
                else int(child.get("dir_count") or 0) + int(child.get("file_count") or 0)
            )
        else:
            file_item = _resource_file_entry(root, entry)
            node["files"].append(file_item)
            counters["files"] += 1
            node["file_count"] += 1
            node["size"] += int(file_item.get("size") or 0)
            node["direct_child_count"] += 1
            node["total_child_count"] += 1
    node["series_count"] = int(node["dir_count"] or 0)
    node["item_count"] = int(node["file_count"] or 0)
    return node


def _resource_root_for_relpath(relpath: str) -> Path | None:
    m = re.match(r"^root:(\d+)(?:/|$)", str(relpath or ""))
    if not m:
        return None
    idx = int(m.group(1))
    roots = resource_roots()
    return roots[idx] if 0 <= idx < len(roots) else None


def _resource_path_for_relpath(relpath: str) -> Path | None:
    rel = str(relpath or "").replace("\\", "/").strip("/")
    root = _resource_root_for_relpath(rel)
    if root is None:
        return None
    rest = rel.split("/", 1)[1] if "/" in rel else ""
    try:
        root_r = root.resolve()
        path = (root_r / rest).resolve() if rest else root_r
        path.relative_to(root_r)
        return path
    except (OSError, ValueError):
        return None


def _resource_live_node_for_relpath(node: dict[str, Any]) -> dict[str, Any]:
    relpath = str(node.get("relpath") or "")
    root = _resource_root_for_relpath(relpath)
    if root is None:
        return node
    path_s = _str_or_blank(node.get("path"))
    if not path_s:
        return node
    path = Path(path_s).expanduser()
    try:
        path_r = path.resolve()
        path_r.relative_to(root.resolve())
    except (OSError, ValueError):
        return node
    try:
        shortcut_root_key = _path_compare_key(shortcut_root())
    except (OSError, ValueError):
        shortcut_root_key = ""
    counters: dict[str, Any] = {"dirs": 0, "files": 0, "truncated": False}
    return _resource_dir_node(
        root,
        path_r,
        relpath,
        resource_excludes_for_root(root),
        shortcut_root_key,
        counters,
        _resource_scan_max_dirs(),
        root_name=str(node.get("name") or path_r.name),
        depth=0,
        max_depth=0,
    )


def _resource_live_node_from_relpath(relpath: str) -> dict[str, Any] | None:
    rel = str(relpath or "").replace("\\", "/").strip("/")
    root = _resource_root_for_relpath(rel)
    path = _resource_path_for_relpath(rel)
    if root is None or path is None:
        return None
    root_name = str(root) if rel == rel.split("/", 1)[0] else path.name
    try:
        shortcut_root_key = _path_compare_key(shortcut_root())
    except (OSError, ValueError):
        shortcut_root_key = ""
    counters: dict[str, Any] = {"dirs": 0, "files": 0, "truncated": False}
    return _resource_dir_node(
        root,
        path,
        rel,
        resource_excludes_for_root(root),
        shortcut_root_key,
        counters,
        _resource_scan_max_dirs(),
        root_name=root_name,
        depth=0,
        max_depth=0,
    )


def resource_libraries_cached_payload() -> dict[str, Any]:
    return _load_resource_scan_cache()


def resource_libraries_search_payload(query: str) -> dict[str, Any]:
    q = str(query or "").strip()
    cache = _load_resource_scan_cache()
    if not q:
        return cache
    if not cache.get("cached"):
        return cache
    entries = _resource_search_entries_from_main_cache(cache)
    tree, counts = _resource_search_tree_from_entries(entries, q)
    summary = dict(cache.get("summary") if isinstance(cache.get("summary"), dict) else {})
    summary.update(
        {
            "search_query": q,
            "search_entry_count": len(entries),
            "search_matched_count": counts["matched_count"],
            "search_matched_folder_count": counts["matched_folder_count"],
            "search_matched_file_count": counts["matched_file_count"],
            "search_shown_folder_count": counts["shown_folder_count"],
            "search_shown_file_count": counts["shown_file_count"],
            "direct_child_count": int(tree.get("direct_child_count") or 0),
            "total_child_count": int(tree.get("total_child_count") or 0),
            "dir_count": int(tree.get("dir_count") or 0),
            "file_count": int(tree.get("file_count") or 0),
            "size": int(tree.get("size") or 0),
        }
    )
    return {
        "ok": True,
        "cached": True,
        "search": True,
        "query": q,
        "config": collection_link_index_config_json(),
        "summary": summary,
        "roots": cache.get("roots") if isinstance(cache.get("roots"), list) else [],
        "items": [],
        "tree": tree,
        "scanned_at": str(cache.get("scanned_at") or ""),
        "cache_path": str(cache.get("cache_path") or _resource_scan_cache_path()),
        "node_cache_dir": str(cache.get("node_cache_dir") or _resource_scan_node_dir()),
        "search_scope": "resource-series-press-directory",
    }


def _is_resource_scan_excluded(root: Path, path: Path, excludes: list[str]) -> bool:
    if not excludes:
        return False
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    candidates = {
        path.name.casefold(),
        rel.casefold(),
        rel.replace("/", "\\").casefold(),
    }
    for item in excludes:
        key = str(item or "").strip().strip("/\\")
        if not key:
            continue
        key_casefold = key.casefold()
        if key_casefold in candidates or key.replace("\\", "/").casefold() in candidates:
            return True
        if key_casefold == "$recycle" and any(candidate.startswith("$recycle") for candidate in candidates):
            return True
    return False


def scan_resource_libraries_payload() -> dict[str, Any]:
    max_dirs = _resource_scan_max_dirs()
    max_depth = _resource_scan_max_depth()
    try:
        shortcut_root_key = _path_compare_key(shortcut_root())
    except (OSError, ValueError):
        shortcut_root_key = ""
    total_seen = 0
    roots_out: list[dict[str, Any]] = []
    flat_items: list[dict[str, Any]] = []
    tree = _empty_resource_tree()
    truncated = False
    for root_idx, root in enumerate(resource_roots()):
        root_excludes = resource_excludes_for_root(root)
        root_rel = f"root:{root_idx}"
        try:
            root_mtime = int(root.stat().st_mtime)
        except OSError:
            root_mtime = 0
        root_item: dict[str, Any] = {
            "root": str(root),
            "excludes": root_excludes,
            "exists": root.is_dir(),
            "series_count": 0,
            "item_count": 0,
            "file_count": 0,
            "dir_count": 0,
            "size": 0,
            "mtime": root_mtime,
            "direct_child_count": 0,
            "total_child_count": 0,
            "series": [],
            "error": "",
        }
        roots_out.append(root_item)
        if not root_item["exists"]:
            root_item["error"] = "目录不存在"
            tree["children"].append(
                {
                    "type": "folder",
                    "name": str(root),
                    "relpath": root_rel,
                    "path": str(root),
                    "exists": False,
                    "error": root_item["error"],
                    "children": [],
                    "files": [],
                    "size": 0,
                    "mtime": root_mtime,
                    "direct_child_count": 0,
                    "total_child_count": 0,
                    "dir_count": 0,
                    "file_count": 0,
                }
            )
            continue
        if shortcut_root_key and _path_compare_key(root) == shortcut_root_key:
            root_item["error"] = "已跳过索引输出目录"
            tree["children"].append(
                {
                    "type": "folder",
                    "name": str(root),
                    "relpath": root_rel,
                    "path": str(root),
                    "exists": True,
                    "error": root_item["error"],
                    "children": [],
                    "files": [],
                    "size": 0,
                    "mtime": root_mtime,
                    "direct_child_count": 0,
                    "total_child_count": 0,
                    "dir_count": 0,
                    "file_count": 0,
                }
            )
            continue
        counters: dict[str, Any] = {"dirs": 0, "files": 0, "truncated": False}
        root_node = _resource_dir_node(
            root,
            root,
            root_rel,
            root_excludes,
            shortcut_root_key,
            counters,
            max_dirs,
            str(root),
            depth=0,
            max_depth=max_depth,
        )
        tree["children"].append(root_node)
        root_item["dir_count"] = int(root_node.get("dir_count") or 0)
        root_item["file_count"] = int(root_node.get("file_count") or 0)
        root_item["size"] = int(root_node.get("size") or 0)
        root_item["mtime"] = int(root_node.get("mtime") or 0)
        root_item["direct_child_count"] = int(root_node.get("direct_child_count") or 0)
        root_item["total_child_count"] = int(root_node.get("total_child_count") or 0)
        root_item["series_count"] = len(root_node.get("children") or [])
        for series_node in root_node.get("children") or []:
            if not isinstance(series_node, dict):
                continue
            children: list[dict[str, Any]] = []
            for child in series_node.get("children") or []:
                if not isinstance(child, dict):
                    continue
                work_name, press_info = _split_resource_leaf_name(str(child.get("name") or ""))
                child_rel = str(child.get("relpath") or "")
                relpath = child_rel.split("/", 1)[1] if child_rel.startswith(root_rel + "/") else child_rel
                item = {
                    "root": str(root),
                    "series_name": str(series_node.get("name") or ""),
                    "name": str(child.get("name") or ""),
                    "work_name": work_name,
                    "press_info": press_info,
                    "relpath": relpath,
                    "path": str(child.get("path") or ""),
                }
                children.append(item)
                flat_items.append(item)
            series_rel = str(series_node.get("relpath") or "")
            root_item["series"].append(
                {
                    "name": str(series_node.get("name") or ""),
                    "path": str(series_node.get("path") or ""),
                    "relpath": series_rel.split("/", 1)[1] if series_rel.startswith(root_rel + "/") else series_rel,
                    "children": children,
                    "error": str(series_node.get("error") or ""),
                }
            )
            root_item["item_count"] += len(children)
        total_seen += counters["dirs"]
        if counters.get("truncated"):
            truncated = True
            root_item["error"] = (root_item.get("error") or "") + ("；" if root_item.get("error") else "") + "扫描数量达到上限"
        if truncated:
            break
    tree["size"] = sum(int(item.get("size") or 0) for item in roots_out)
    tree["direct_child_count"] = len(tree.get("children") or [])
    tree["total_child_count"] = sum(int(item.get("total_child_count") or 0) for item in roots_out)
    tree["dir_count"] = sum(int(item.get("dir_count") or 0) for item in roots_out)
    tree["file_count"] = sum(int(item.get("file_count") or 0) for item in roots_out)
    tree["series_count"] = sum(int(item.get("series_count") or 0) for item in roots_out)
    tree["item_count"] = len(flat_items)
    payload = {
        "ok": True,
        "cached": False,
        "config": collection_link_index_config_json(),
        "summary": {
            "root_count": len(roots_out),
            "existing_root_count": sum(1 for item in roots_out if item.get("exists")),
            "series_count": sum(int(item.get("series_count") or 0) for item in roots_out),
            "item_count": len(flat_items),
            "dir_count": sum(int(item.get("dir_count") or 0) for item in roots_out),
            "file_count": sum(int(item.get("file_count") or 0) for item in roots_out),
            "size": sum(int(item.get("size") or 0) for item in roots_out),
            "direct_child_count": sum(int(item.get("direct_child_count") or 0) for item in roots_out),
            "total_child_count": sum(int(item.get("total_child_count") or 0) for item in roots_out),
            "truncated": truncated,
            "max_dirs": max_dirs,
            "max_depth": max_depth,
        },
        "roots": roots_out,
        "items": flat_items,
        "tree": tree,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "cache_path": str(_resource_scan_cache_path()),
    }
    _save_resource_scan_cache(payload)
    return _load_resource_scan_cache()


def _safe_name(raw: Any, fallback: str = "_") -> str:
    s = str(raw if raw is not None else "").strip()
    if not s:
        s = fallback
    s = _INVALID_NAME_CHARS_RE.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or fallback


def _template_text(template: str, ctx: dict[str, Any]) -> str:
    out = str(template or "")
    for key, value in ctx.items():
        out = out.replace("{" + key + "}", str(value))
    return _safe_name(out)


def _year_from_entry(entry: Any, yaml_rel: str) -> str:
    try:
        start, _end = entry_air_dates(entry)
    except ValueError:
        start = ""
    m = re.search(r"(19|20)\d{2}", str(start))
    if m:
        return m.group(0)
    m = re.search(r"\[(?:JP|CN|US)?[^\]]*\]\[.*?\]\[((?:19|20)\d{2})\]", yaml_rel)
    if m:
        return m.group(1)
    m = re.search(r"(19|20)\d{2}", yaml_rel)
    return m.group(0) if m else ""


def _air_date_parts(entry: Any) -> tuple[str, str]:
    try:
        start, end = entry_air_dates(entry)
    except ValueError:
        return "", ""
    return _str_or_blank(start), _str_or_blank(end)


def _enum_display(settings: JpTvBrowseSettings, enum_key: str, raw: str) -> str:
    labels = settings.enum_labels.get(enum_key, {})
    return labels.get(raw, raw)


def _work_key(yaml_rel: str, index_in_file: int) -> str:
    return f"{yaml_rel}#{int(index_in_file)}"


def _press_key(position: int, row: dict[str, Any]) -> str:
    fm = _str_or_blank(row.get(TV_JP_PRESS_FORMAT_KEY))
    gp = _str_or_blank(row.get(TV_JP_PRESS_GROUP_KEY))
    seg = _str_or_blank(row.get("segment")) or "main"
    cont = row.get("continuation_index")
    cont_s = "" if cont is None else str(cont)
    return f"{position}:{seg}:{cont_s}:{fm}:{gp}"


def _press_key_position(press_key: str) -> int | None:
    head = str(press_key or "").split(":", 1)[0]
    try:
        pos = int(head)
    except (TypeError, ValueError):
        return None
    return pos if pos >= 0 else None


def _press_label(row: dict[str, Any]) -> str:
    fm = _str_or_blank(row.get(TV_JP_PRESS_FORMAT_KEY))
    gp = _str_or_blank(row.get(TV_JP_PRESS_GROUP_KEY))
    if fm and gp:
        return f"{fm}-{gp}"
    return fm or gp or "press"


def _catalog_relpath(settings: JpTvBrowseSettings, yaml_abs: Path) -> str:
    if settings.filesystem_root is None:
        return yaml_abs.name
    try:
        return yaml_abs.resolve().relative_to(settings.filesystem_root.resolve()).as_posix()
    except ValueError:
        return yaml_abs.name


def _catalog_yaml_paths(settings: JpTvBrowseSettings) -> list[Path]:
    if settings.filesystem_root is not None:
        root = settings.filesystem_root.resolve()
        if root.is_dir():
            return [
                p.resolve()
                for p in sorted(root.glob("*.yaml"))
                if p.is_file() and not p.name.startswith(".") and p.name != _LINK_INDEX_DB_FILENAME
            ]
    return [Path(abs_s).resolve() for abs_s in settings.resolved_catalog_yaml_paths]


def _clean_rel_path(raw: Any, *, label: str) -> str:
    if not isinstance(raw, str):
        return ""
    s = raw.strip().replace("\\", "/")
    s = re.sub(r"/+", "/", s).strip("/")
    if not s:
        return ""
    if re.match(r"^[A-Za-z]:", s) or s.startswith("/"):
        raise ValueError(f"{label} 必须为相对路径")
    parts = s.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} 包含非法路径片段")
    return s


def _clean_work_path(raw: Any, *, label: str = "path") -> str:
    if not isinstance(raw, str):
        return ""
    raw_s = raw.strip()
    if not raw_s:
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", raw_s) or raw_s.startswith(("/", "\\")):
        try:
            return str(Path(raw_s).expanduser().resolve())
        except OSError:
            return str(Path(raw_s).expanduser())
    return _clean_rel_path(raw_s, label=label)


def _load_catalog_works(settings: JpTvBrowseSettings) -> list[dict[str, Any]]:
    works_out: list[dict[str, Any]] = []
    for fp in _catalog_yaml_paths(settings):
        if not fp.is_file():
            continue
        yaml_rel = _catalog_relpath(settings, fp)
        raw_text = fp.read_text(encoding="utf-8")
        entries = load_jp_tv_entries_from_yaml(load_yaml_string(raw_text))
        for idx, entry in enumerate(entries):
            name = entry_display_name(entry)
            domain = entry_domain_slug(entry)
            country = entry_country_slug(entry)
            release_type = entry_release_type_slug(entry)
            year = _year_from_entry(entry, yaml_rel)
            begin_date, end_date = _air_date_parts(entry)
            coll = entry_collection_type_data(entry)
            work_path = _str_or_blank(coll.get("path")).replace("\\", "/")
            press_rows: list[dict[str, Any]] = []
            for pos, row in enumerate(build_collectioned_ordered(coll)):
                fm = _str_or_blank(row.get(TV_JP_PRESS_FORMAT_KEY))
                gp = _str_or_blank(row.get(TV_JP_PRESS_GROUP_KEY))
                if not fm and not gp:
                    continue
                press_rows.append(
                    {
                        "press_key": _press_key(pos, row),
                        "press_format": fm,
                        "press_group": gp,
                        "press_path": _str_or_blank(row.get(TV_JP_PRESS_PATH_KEY)).replace("\\", "/"),
                        "label": _press_label(row),
                        "segment": row.get("segment") or "main",
                        "continuation_index": row.get("continuation_index"),
                        "continuation_title": row.get("continuation_title") or "",
                    },
                )
            works_out.append(
                {
                    "work_key": _work_key(yaml_rel, idx),
                    "yaml_source_rel": yaml_rel,
                    "index_in_file": idx,
                    "name": name,
                    "path": work_path,
                    "year": year,
                    "year_label": f"[{year}]" if year else "",
                    "begin_date": begin_date,
                    "end_date": end_date,
                    "date_range_label": (
                        f"[{begin_date}][{end_date}]"
                        if begin_date and end_date
                        else f"[{begin_date or end_date}]" if begin_date or end_date else ""
                    ),
                    "domain": domain,
                    "domain_label": _enum_display(settings, "domain", domain),
                    "country": country,
                    "country_label": _enum_display(settings, "country", country),
                    "release_type": release_type,
                    "release_type_label": _enum_display(settings, "release_type", release_type),
                    "press": press_rows,
                },
            )
    return works_out


def _normalize_ui_work(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    yaml_rel = _str_or_blank(raw.get("yaml_source_rel")).replace("\\", "/")
    try:
        index = int(raw.get("index_in_file"))
    except (TypeError, ValueError):
        return None
    if not yaml_rel or index < 0:
        return None
    path_s = _clean_work_path(raw.get("path", raw.get("media_dir")), label="path")
    press_in = raw.get("press")
    old_press_in = raw.get("press_targets")
    press_raw = press_in if isinstance(press_in, list) else old_press_in
    press: list[dict[str, str]] = []
    for item in press_raw if isinstance(press_raw, list) else []:
        if not isinstance(item, dict):
            continue
        pk = _str_or_blank(item.get("press_key"))
        ppath_raw = item.get(TV_JP_PRESS_PATH_KEY, item.get("target_subdir"))
        ppath = _clean_rel_path(ppath_raw, label="press_path")
        if pk or ppath:
            press.append({"press_key": pk, TV_JP_PRESS_PATH_KEY: ppath})
    return {
        "work_key": _work_key(yaml_rel, index),
        "yaml_source_rel": yaml_rel,
        "index_in_file": index,
        "path": path_s,
        "press": press,
    }


def _ui_mapping_items(body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("works"), list):
        return []
    return [x for x in (_normalize_ui_work(item) for item in body["works"]) if x is not None]


def _merge_ui_mappings(works: list[dict[str, Any]], mapping_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {str(item.get("work_key")): item for item in mapping_items}
    out: list[dict[str, Any]] = []
    for work in works:
        m = mapping.get(str(work.get("work_key")))
        if not m:
            out.append(work)
            continue
        press_paths = {
            str(item.get("press_key")): str(item.get(TV_JP_PRESS_PATH_KEY) or "")
            for item in m.get("press", [])
            if isinstance(item, dict)
        }
        next_press: list[dict[str, Any]] = []
        for press in work.get("press", []):
            if not isinstance(press, dict):
                continue
            pk = str(press.get("press_key") or "")
            next_press.append(
                {
                    **press,
                    TV_JP_PRESS_PATH_KEY: press_paths.get(pk, str(press.get(TV_JP_PRESS_PATH_KEY) or "")),
                }
            )
        out.append({**work, "path": str(m.get("path") or ""), "press": next_press})
    return out


def _path_under(root: Path, raw: str, *, label: str) -> Path:
    s = _clean_rel_path(raw, label=label)
    if not s:
        raise ValueError(f"{label} 不能为空")
    root_r = root.resolve()
    cand = (root_r / s).resolve()
    cand.relative_to(root_r)
    return cand


def _target_for(media_root_p: Path, work_path_s: str, press_path_s: str) -> Path:
    if re.match(r"^[A-Za-z]:[\\/]", str(work_path_s or "")) or str(work_path_s or "").startswith(("/", "\\")):
        work_dir = Path(str(work_path_s)).expanduser().resolve()
    else:
        work_dir = _path_under(media_root_p, work_path_s, label="path")
    sub = _clean_rel_path(press_path_s, label="press_path")
    if not sub:
        raise ValueError("press_path 不能为空")
    target = (work_dir / sub).resolve()
    target.relative_to(work_dir)
    return target


def _plan_context(work: dict[str, Any], press: dict[str, Any]) -> dict[str, str]:
    ctx = {k: str(v or "") for k, v in work.items() if not isinstance(v, list)}
    ctx.update({k: str(v or "") for k, v in press.items() if not isinstance(v, list)})
    ctx["press_label"] = str(press.get("label") or "")
    group = _str_or_blank(press.get(TV_JP_PRESS_GROUP_KEY))
    ctx["press_group_suffix"] = "" if not group or group == "----" else f"({group})"
    return ctx


def _link_index_db_empty() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": "",
        "source": "collection-detail catalog yaml",
        "layout_levels": list(_layout_levels()),
        "shortcut_name": _shortcut_name_template(),
        "items": [],
    }


def _load_link_index_db() -> dict[str, Any]:
    path = _link_index_db_path()
    if not path.is_file():
        return _link_index_db_empty()
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        return _link_index_db_empty()
    raw.setdefault("version", 1)
    raw.setdefault("generated_at", "")
    raw.setdefault("source", "collection-detail catalog yaml")
    raw.setdefault("layout_levels", list(_layout_levels()))
    raw.setdefault("shortcut_name", _shortcut_name_template())
    raw.setdefault("items", [])
    return raw


def _save_link_index_db(payload: dict[str, Any]) -> None:
    path = _link_index_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml_string(payload), encoding="utf-8")


def _index_entry_key(work: dict[str, Any], press: dict[str, Any]) -> str:
    raw = {
        "yaml_source_rel": _str_or_blank(work.get("yaml_source_rel")).replace("\\", "/"),
        "index_in_file": str(work.get("index_in_file") if work.get("index_in_file") is not None else ""),
        "press_key": _str_or_blank(press.get("press_key")),
    }
    return hashlib.sha1(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _index_relpath_for(work: dict[str, Any], press: dict[str, Any]) -> tuple[str, list[str]]:
    ctx = _plan_context(work, press)
    parts = [_template_text(level, ctx) for level in _layout_levels()]
    filename = _template_text(_shortcut_name_template(), ctx)
    if not filename.lower().endswith(".lnk"):
        filename += ".lnk"
    parts.append(filename)
    clean_parts = [part for part in parts if part]
    return "/".join(clean_parts), clean_parts


def _catalog_target_for_work_press(work: dict[str, Any], press: dict[str, Any]) -> tuple[str, bool, str]:
    work_path_s = _str_or_blank(work.get("path"))
    press_path_s = _str_or_blank(press.get(TV_JP_PRESS_PATH_KEY))
    if not work_path_s or not press_path_s:
        return "", False, ""
    try:
        target = _target_for(_legacy_media_root(), work_path_s, press_path_s)
    except (OSError, ValueError) as exc:
        return "", False, str(exc)
    try:
        exists = target.is_dir()
    except OSError:
        exists = False
    return str(target), exists, ""


def _resource_candidate_for_work_press(
    work: dict[str, Any],
    press: dict[str, Any],
    resource_items: list[dict[str, Any]],
    resource_index: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    work_keys = {_strict_name_key(work.get("name") or "")}
    work_keys = {key for key in work_keys if key}
    if not work_keys:
        return None
    press_probe = {
        "press_format": press.get(TV_JP_PRESS_FORMAT_KEY) or press.get("press_format") or "",
        "press_group": press.get(TV_JP_PRESS_GROUP_KEY) or press.get("press_group") or "",
    }
    exact_press_keys = _candidate_press_resource_match_keys(press_probe)
    format_key = _press_component_key(press_probe.get("press_format") or "")
    candidates: list[dict[str, Any]] = []
    fallback_candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    resource_pool: list[dict[str, Any]] = []
    if resource_index is not None:
        seen_pool: set[int] = set()
        for work_key in work_keys:
            for resource in resource_index.get(work_key, []):
                ident = id(resource)
                if ident in seen_pool:
                    continue
                seen_pool.add(ident)
                resource_pool.append(resource)
    else:
        resource_pool = resource_items
    for resource in resource_pool:
        if not isinstance(resource, dict):
            continue
        if resource_index is None and not (_resource_work_match_keys(resource) & work_keys):
            continue
        path_s = _str_or_blank(resource.get("path"))
        if not path_s:
            continue
        try:
            path = Path(path_s).expanduser().resolve()
            exists = path.is_dir()
        except OSError:
            path = Path(path_s).expanduser()
            exists = False
        if not exists:
            continue
        target_key = _path_compare_key(path)
        if target_key in seen:
            continue
        resource_press_raw = resource.get("press_info") or resource.get("name") or ""
        resource_press_keys = {_press_component_key(resource_press_raw)} | _press_alias_match_keys(resource_press_raw)
        resource_press_keys = {key for key in resource_press_keys if key}
        base = {
            "target_path": str(path),
            "suggested_path": str(path.parent),
            "suggested_press_path": path.name,
            "resource_root": resource.get("root") or "",
            "resource_relpath": resource.get("relpath") or "",
            "series_name": resource.get("series_name") or "",
            "resource_name": resource.get("name") or "",
            "work_name": resource.get("work_name") or "",
            "press_info": resource.get("press_info") or "",
        }
        if exact_press_keys and (resource_press_keys & exact_press_keys):
            seen.add(target_key)
            candidates.append({**base, "match_source": "resource_exact_press", "score": 200})
            continue
        if format_key and (
            _press_component_key(resource.get("press_info") or "") == format_key
            or format_key in str(_press_component_key(resource.get("press_info") or "")).split()
        ):
            fallback_candidates.append({**base, "match_source": "resource_format_fallback", "score": 100})
    pool = candidates if candidates else fallback_candidates
    if not pool:
        return None
    pool.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("target_path") or "").casefold()))
    best_score = int(pool[0].get("score") or 0)
    top = [item for item in pool if int(item.get("score") or 0) == best_score]
    if len(top) != 1:
        return None
    return dict(top[0])


def _resource_items_by_work_key(resource_items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for resource in resource_items:
        if not isinstance(resource, dict):
            continue
        for key in _resource_work_match_keys(resource):
            if key:
                out.setdefault(key, []).append(resource)
    return out


def _index_entry_from_work_press(
    work: dict[str, Any],
    press: dict[str, Any],
    resource_items: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
    resource_index: dict[str, list[dict[str, Any]]] | None = None,
    prefer_previous_target: bool = False,
) -> dict[str, Any]:
    relpath, parts = _index_relpath_for(work, press)
    work_shortcut_root = _shortcut_root_for_work(work)
    try:
        planned_shortcut_path = str((work_shortcut_root / relpath).resolve())
    except OSError:
        planned_shortcut_path = str(work_shortcut_root / relpath)
    catalog_target, catalog_exists, catalog_error = _catalog_target_for_work_press(work, press)
    resource_candidate = _resource_candidate_for_work_press(work, press, resource_items, resource_index)
    previous_target = _str_or_blank((previous or {}).get("target_path"))
    target_path = ""
    target_source = ""
    if prefer_previous_target and previous_target:
        target_path = previous_target
        target_source = _str_or_blank((previous or {}).get("target_source")) or "index_db_previous"
    elif resource_candidate:
        target_path = _str_or_blank(resource_candidate.get("target_path"))
        target_source = str(resource_candidate.get("match_source") or "resource")
    elif catalog_target:
        target_path = catalog_target
        target_source = "catalog"
    elif previous_target:
        target_path = previous_target
        target_source = "index_db_previous"
    target_exists = False
    if target_path:
        try:
            target_exists = Path(target_path).expanduser().is_dir()
        except OSError:
            target_exists = False
    status = "ready" if target_exists else "missing_target"
    if catalog_error and not target_path:
        status = "invalid_path"
    entry: dict[str, Any] = {
        "entry_key": _index_entry_key(work, press),
        "status": status,
        "source": "index_db",
        "work_key": work.get("work_key") or "",
        "yaml_source_rel": work.get("yaml_source_rel") or "",
        "index_in_file": work.get("index_in_file"),
        "name": work.get("name") or "",
        "domain": work.get("domain") or "",
        "country": work.get("country") or "",
        "release_type": work.get("release_type") or "",
        "year": work.get("year") or "",
        "begin_date": work.get("begin_date") or "",
        "end_date": work.get("end_date") or "",
        "date_range_label": work.get("date_range_label") or "",
        "press_key": press.get("press_key") or "",
        "press_label": press.get("label") or "",
        "press_format": press.get(TV_JP_PRESS_FORMAT_KEY) or "",
        "press_group": press.get(TV_JP_PRESS_GROUP_KEY) or "",
        "press_path": _str_or_blank(press.get(TV_JP_PRESS_PATH_KEY)),
        "work_path": _str_or_blank(work.get("path")),
        "target_path": target_path,
        "target_source": target_source,
        "target_exists": target_exists,
        "shortcut_path": planned_shortcut_path,
        "shortcut_root": str(work_shortcut_root),
        "shortcut_relpath": relpath,
        "shortcut_parts": parts,
        "shortcut_exists": False,
        "shortcut_target_path": target_path,
        "shortcut_target_exists": target_exists,
        "matched_shortcut_path": "",
        "matched_shortcut_relpath": "",
        "link_exists": target_exists,
        "db_linked": target_exists,
    }
    if catalog_error:
        entry["error"] = catalog_error
    if resource_candidate:
        entry["resource_candidate"] = resource_candidate
        if previous_target and _path_compare_key(previous_target) != _path_compare_key(resource_candidate.get("target_path")):
            entry["target_fix"] = resource_candidate
            entry["status"] = "missing_target" if not target_exists else "ready"
    return entry


def _index_entries_from_works(
    works: list[dict[str, Any]],
    *,
    previous_items: list[dict[str, Any]] | None = None,
    use_resource_index: bool = False,
    prefer_previous_target: bool = False,
) -> list[dict[str, Any]]:
    resource_items = _resource_fix_items_from_cache() if use_resource_index and _resource_roots_explicitly_configured() else []
    resource_index = _resource_items_by_work_key(resource_items) if resource_items else None
    previous_by_key = {
        str(item.get("entry_key") or ""): item
        for item in previous_items or []
        if isinstance(item, dict) and item.get("entry_key")
    }
    entries: list[dict[str, Any]] = []
    for work in works:
        for press in work.get("press", []) or []:
            if not isinstance(press, dict):
                continue
            key = _index_entry_key(work, press)
            entries.append(
                _index_entry_from_work_press(
                    work,
                    press,
                    resource_items,
                    previous=previous_by_key.get(key),
                    resource_index=resource_index,
                    prefer_previous_target=prefer_previous_target,
                )
            )
    return entries


def _save_index_entries_from_works(works: list[dict[str, Any]], *, catalog_root: str = "") -> dict[str, Any]:
    previous = _load_link_index_db()
    entries = _index_entries_from_works(
        works,
        previous_items=[item for item in previous.get("items", []) if isinstance(item, dict)]
        if isinstance(previous.get("items"), list)
        else [],
        use_resource_index=True,
    )
    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "collection-detail catalog yaml",
        "catalog_root": catalog_root,
        "layout_levels": list(_layout_levels()),
        "shortcut_name": _shortcut_name_template(),
        "items": entries,
    }
    _save_link_index_db(payload)
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None
    return payload


def _index_db_items_for_payload(works: list[dict[str, Any]], *, catalog_root: str = "") -> tuple[list[dict[str, Any]], bool]:
    db = _load_link_index_db()
    raw_items = db.get("items")
    db_catalog_root = _str_or_blank(db.get("catalog_root"))
    can_use_db = bool(catalog_root and db_catalog_root and db_catalog_root == catalog_root)
    if can_use_db and isinstance(raw_items, list) and raw_items:
        return _refreshed_index_db_display_items(raw_items), True
    return _index_entries_from_works(works), False


def _refreshed_index_db_display_items(raw_items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return out
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        target_s = _str_or_blank(item.get("target_path"))
        target_exists = False
        if target_s:
            try:
                target_exists = Path(target_s).expanduser().is_dir()
            except OSError:
                target_exists = False
        item["target_exists"] = target_exists
        item["shortcut_target_path"] = target_s
        item["shortcut_target_exists"] = target_exists
        item["link_exists"] = target_exists
        item["db_linked"] = target_exists
        item["status"] = "ready" if target_exists else "missing_target"
        out.append(item)
    return out


def _settings_catalog_root_key(settings: JpTvBrowseSettings) -> str:
    if settings.filesystem_root is None:
        return ""
    try:
        return str(settings.filesystem_root.resolve())
    except OSError:
        return str(settings.filesystem_root)


def _path_compare_key(raw: Any) -> str:
    s = str(raw) if isinstance(raw, Path) else _str_or_blank(raw)
    if not s:
        return ""
    try:
        return os.path.normcase(str(Path(s).expanduser().resolve()))
    except OSError:
        return os.path.normcase(str(Path(s).expanduser()))


def _planned_relpath_keys(plan: list[dict[str, Any]]) -> set[str]:
    return {
        _path_compare_key(item.get("shortcut_path"))
        for item in plan
        if _path_compare_key(item.get("shortcut_path"))
    }


def _planned_target_keys(plan: list[dict[str, Any]]) -> set[str]:
    return {
        key
        for key in (_path_compare_key(item.get("target_path")) for item in plan)
        if key
    }


def _disk_leaf_is_unmapped(
    item: dict[str, Any],
    *,
    planned_relpaths: set[str],
    planned_targets: set[str],
) -> bool:
    shortcut_key = _path_compare_key(item.get("shortcut_path") or item.get("path"))
    if shortcut_key and shortcut_key in planned_relpaths:
        return False
    target_key = _path_compare_key(item.get("target_path"))
    if bool(item.get("target_exists")) and target_key and target_key in planned_targets:
        return False
    return True


def _mapping_summary(works: list[dict[str, Any]]) -> dict[str, int]:
    total_press = 0
    mapped_press = 0
    unconfigured_work_path = 0
    unconfigured_press_path = 0
    for work in works:
        work_path_s = _str_or_blank(work.get("path"))
        press_rows = [p for p in work.get("press", []) if isinstance(p, dict)]
        if press_rows and not work_path_s:
            unconfigured_work_path += len(press_rows)
        for press in press_rows:
            total_press += 1
            press_path_s = _str_or_blank(press.get(TV_JP_PRESS_PATH_KEY))
            if work_path_s and press_path_s:
                mapped_press += 1
            elif not press_path_s:
                unconfigured_press_path += 1
    return {
        "total_press": total_press,
        "mapped_press": mapped_press,
        "unconfigured_press": total_press - mapped_press,
        "unconfigured_work_path": unconfigured_work_path,
        "unconfigured_press_path": unconfigured_press_path,
    }


def _plan_summary(plan: list[dict[str, Any]]) -> dict[str, int]:
    out = {
        "total": len(plan),
        "ready": 0,
        "missing_target": 0,
        "target_fixable": 0,
        "duplicate_shortcut": 0,
        "invalid_path": 0,
        "shortcut_exists": 0,
        "unmapped_on_disk": 0,
        "empty_target_path": 0,
        "created": 0,
        "renamed": 0,
        "skipped": 0,
        "failed": 0,
    }
    for item in plan:
        status = str(item.get("status") or "")
        if status in out:
            out[status] += 1
        if not _str_or_blank(item.get("target_path")):
            out["empty_target_path"] += 1
        if item.get("target_fix"):
            out["target_fixable"] += 1
        if item.get("created"):
            out["created"] += 1
        if item.get("renamed"):
            out["renamed"] += 1
        if item.get("skipped"):
            out["skipped"] += 1
        if item.get("error") and status not in {"invalid_path"}:
            out["failed"] += 1
    return out


def _copy_shortcut_leaves(leaves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in leaves]


def _shortcut_scan_signature_payload(signature: tuple[Any, ...]) -> dict[str, Any]:
    root = str(signature[0]) if signature else ""
    files = signature[1] if len(signature) > 1 and isinstance(signature[1], tuple) else ()
    return {
        "root": root,
        "files": [
            {"relpath": str(rel), "mtime_ns": int(mtime_ns), "size": int(size)}
            for rel, mtime_ns, size in files
        ],
    }


def _shortcut_scan_signature_from_payload(raw: Any) -> tuple[Any, ...] | None:
    if not isinstance(raw, dict):
        return None
    root = _str_or_blank(raw.get("root"))
    files = raw.get("files")
    if not root or not isinstance(files, list):
        return None
    out: list[tuple[str, int, int]] = []
    for item in files:
        if not isinstance(item, dict):
            return None
        rel = _str_or_blank(item.get("relpath"))
        if not rel:
            return None
        try:
            mtime_ns = int(item.get("mtime_ns"))
            size = int(item.get("size"))
        except (TypeError, ValueError):
            return None
        out.append((rel, mtime_ns, size))
    return (root, tuple(sorted(out)))


def _shortcut_leaf_cache_payload(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "type",
        "name",
        "relpath",
        "path",
        "shortcut_path",
        "shortcut_root",
        "shortcut_relpath",
        "target_path",
        "target_exists",
        "target_resolved",
        "target_error",
        "shortcut_exists",
    ):
        if key in item:
            out[key] = item[key]
    parts = item.get("shortcut_parts")
    out["shortcut_parts"] = [str(part) for part in parts] if isinstance(parts, list) else []
    return out


def _shortcut_leaf_from_cache(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    rel = _str_or_blank(raw.get("shortcut_relpath") or raw.get("relpath")).replace("\\", "/")
    path = _str_or_blank(raw.get("shortcut_path") or raw.get("path"))
    name = _str_or_blank(raw.get("name"))
    if not rel or not path or not name:
        return None
    parts = raw.get("shortcut_parts")
    return {
        "type": _str_or_blank(raw.get("type")) or "disk_link",
        "name": name,
        "relpath": rel,
        "path": path,
        "shortcut_path": path,
        "shortcut_root": _str_or_blank(raw.get("shortcut_root")),
        "shortcut_relpath": rel,
        "shortcut_parts": [str(part) for part in parts] if isinstance(parts, list) else rel.split("/"),
        "shortcut_exists": bool(raw.get("shortcut_exists", True)),
        "target_path": _str_or_blank(raw.get("target_path")),
        "target_exists": bool(raw.get("target_exists")),
        "target_resolved": bool(raw.get("target_resolved")),
        "target_error": _str_or_blank(raw.get("target_error")),
    }


def _load_shortcut_scan_cache(signature: tuple[Any, ...]) -> list[dict[str, Any]] | None:
    path = _shortcut_scan_cache_path()
    if not path.is_file():
        return None
    try:
        raw = load_yaml(path)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    cached_signature = _shortcut_scan_signature_from_payload(raw.get("signature"))
    if cached_signature != signature:
        return None
    leaves = raw.get("leaves")
    if not isinstance(leaves, list):
        return None
    out: list[dict[str, Any]] = []
    for item in leaves:
        leaf = _shortcut_leaf_from_cache(item)
        if leaf is None:
            return None
        out.append(leaf)
    return out


def _save_shortcut_scan_cache(signature: tuple[Any, ...], leaves: list[dict[str, Any]]) -> None:
    path = _shortcut_scan_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "cached": True,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "signature": _shortcut_scan_signature_payload(signature),
        "leaves": [_shortcut_leaf_cache_payload(item) for item in leaves],
    }
    path.write_text(dump_yaml_string(payload), encoding="utf-8")


def _scan_shortcut_leaves(*, refresh_targets: bool = False) -> list[dict[str, Any]]:
    max_dirs = _scan_max_dirs()
    max_depth = max(_scan_max_depth(), len(_layout_levels()) + 2)
    leaves: list[dict[str, Any]] = []
    shortcut_paths: list[Path] = []
    signature_parts: list[tuple[str, int, int]] = []
    seen_dirs = 0
    scanned_roots: list[str] = []
    for root_index, root in enumerate(shortcut_roots()):
        try:
            if not root.is_dir():
                continue
            root_resolved = root.resolve()
        except OSError:
            continue
        scanned_roots.append(str(root_resolved))
        stack: list[tuple[Path, int]] = [(root_resolved, 0)]
        while stack and seen_dirs < max_dirs:
            cur, depth = stack.pop()
            seen_dirs += 1
            try:
                children = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                continue
            for child in reversed(children):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    if depth < max_depth:
                        stack.append((child, depth + 1))
                    continue
                if child.is_file() and child.suffix.lower() == ".lnk":
                    try:
                        shortcut_abs = child.resolve()
                        rel = shortcut_abs.relative_to(root_resolved).as_posix()
                        st = shortcut_abs.stat()
                    except ValueError:
                        continue
                    except OSError:
                        continue
                    shortcut_paths.append(shortcut_abs)
                    signature_parts.append((f"{root_index}:{rel}", int(st.st_mtime_ns), int(st.st_size)))
                    leaves.append(
                        {
                            "type": "disk_link",
                            "name": child.name,
                            "relpath": rel,
                            "path": str(shortcut_abs),
                            "shortcut_path": str(shortcut_abs),
                            "shortcut_root": str(root_resolved),
                            "shortcut_relpath": rel,
                            "shortcut_parts": rel.split("/"),
                            "shortcut_exists": True,
                        }
                    )
    signature = ("|".join(scanned_roots), tuple(sorted(signature_parts)))
    if not refresh_targets and _SHORTCUT_SCAN_CACHE.get("signature") == signature:
        return _copy_shortcut_leaves(cast(list[dict[str, Any]], _SHORTCUT_SCAN_CACHE.get("leaves") or []))
    if not refresh_targets:
        cached_leaves = _load_shortcut_scan_cache(signature)
        if cached_leaves is not None:
            _SHORTCUT_SCAN_CACHE["signature"] = signature
            _SHORTCUT_SCAN_CACHE["leaves"] = _copy_shortcut_leaves(cached_leaves)
            return cached_leaves
    target_infos = _windows_shortcut_targets(shortcut_paths)
    for item in leaves:
        shortcut_s = str(item.get("shortcut_path") or "")
        info = target_infos.get(shortcut_s) or {}
        target_s = str(info.get("target_path") or "")
        target = Path(target_s).expanduser().resolve() if target_s else None
        item["target_path"] = str(target) if target is not None else ""
        item["target_exists"] = bool(target and target.exists())
        item["target_resolved"] = bool(info.get("target_resolved"))
        item["target_error"] = str(info.get("error") or "")
    _SHORTCUT_SCAN_CACHE["signature"] = signature
    _SHORTCUT_SCAN_CACHE["leaves"] = _copy_shortcut_leaves(leaves)
    try:
        _save_shortcut_scan_cache(signature, leaves)
    except OSError:
        pass
    return leaves


def _append_assoc(node: dict[str, Any], assoc: dict[str, Any]) -> None:
    key = f"{assoc.get('work_key')}::{assoc.get('press_key', '')}"
    seen = node.setdefault("_assoc_seen", set())
    if key in seen:
        return
    seen.add(key)
    node.setdefault("associated", []).append(assoc)


def _folder_child(parent: dict[str, Any], name: str, relpath: str, abs_path: str) -> dict[str, Any]:
    by_name = parent.setdefault("_children_by_name", {})
    folder_key = f"folder:{name}"
    if folder_key not in by_name:
        child = {
            "type": "folder",
            "name": name,
            "relpath": relpath,
            "path": abs_path,
            "associated": [],
            "children": [],
        }
        by_name[folder_key] = child
        parent.setdefault("children", []).append(child)
    return cast(dict[str, Any], by_name[folder_key])


def _strip_tree_internal(node: dict[str, Any]) -> dict[str, Any]:
    children = [_strip_tree_internal(child) for child in node.get("children", []) if isinstance(child, dict)]
    children.sort(key=lambda x: (0 if x.get("type") == "folder" else 1, str(x.get("name") or "").lower()))
    out = {k: v for k, v in node.items() if not k.startswith("_") and k != "children"}
    out["children"] = children
    return out


def _build_tree(
    plan: list[dict[str, Any]],
    disk_leaves: list[dict[str, Any]] | None = None,
    disk_matches: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    configured_roots = shortcut_roots()
    multi_root = len(configured_roots) > 1
    default_root = shortcut_root()
    root: dict[str, Any] = {
        "type": "root",
        "name": "快捷方式索引" if multi_root else (default_root.name or str(default_root)),
        "relpath": "",
        "path": "" if multi_root else str(default_root),
        "associated": [],
        "children": [],
    }
    root_nodes: dict[str, dict[str, Any]] = {}

    def tree_root_for(raw_root: Any) -> tuple[Path, dict[str, Any]]:
        sr = Path(_str_or_blank(raw_root) or str(default_root)).expanduser().resolve()
        if not multi_root:
            return sr, root
        key = _path_compare_key(sr)
        node = root_nodes.get(key)
        if node is None:
            node = {
                "type": "folder",
                "name": str(sr),
                "relpath": f"root:{len(root_nodes)}",
                "path": str(sr),
                "associated": [],
                "children": [],
            }
            root_nodes[key] = node
            root["children"].append(node)
        return sr, node

    planned_relpaths = _planned_relpath_keys(plan)
    planned_targets = _planned_target_keys(plan)
    for item in plan:
        parts = item.get("shortcut_parts")
        if not isinstance(parts, list) or not parts:
            continue
        assoc = {
            "work_key": item.get("work_key") or "",
            "entry_key": item.get("entry_key") or "",
            "yaml_source_rel": item.get("yaml_source_rel") or "",
            "index_in_file": item.get("index_in_file"),
            "name": item.get("name") or "",
            "year": item.get("year") or "",
            "press_key": item.get("press_key") or "",
            "press_label": item.get("press_label") or "",
        }
        _append_assoc(root, assoc)
        sr, cur = tree_root_for(item.get("shortcut_root"))
        if cur is not root:
            _append_assoc(cur, assoc)
        rel_parts: list[str] = []
        for part in [str(x) for x in parts[:-1]]:
            rel_parts.append(part)
            rel = "/".join(rel_parts)
            folder = _folder_child(cur, part, rel, str((sr / rel).resolve()))
            _append_assoc(folder, assoc)
            cur = folder
        link_name = str(parts[-1])
        rel = "/".join([str(x) for x in parts])
        link_node = {
            "type": "link",
            "name": link_name,
            "relpath": rel,
            "path": item.get("shortcut_path") or "",
            "shortcut_path": item.get("shortcut_path") or "",
            "target_path": item.get("target_path") or "",
            "shortcut_target_path": item.get("shortcut_target_path") or "",
            "matched_shortcut_path": item.get("matched_shortcut_path") or "",
            "matched_shortcut_relpath": item.get("matched_shortcut_relpath") or "",
            "shortcut_target_exists": bool(item.get("shortcut_target_exists")),
            "target_resolved": bool(item.get("target_resolved")),
            "target_error": str(item.get("target_error") or ""),
            "open_path": item.get("target_path") or "",
            "source": item.get("source") or "",
            "status": item.get("status") or "",
            "db_associated": True,
            "db_linked": bool(item.get("db_linked")),
            "link_exists": bool(item.get("link_exists")),
            "target_exists": bool(item.get("target_exists")),
            "shortcut_exists": bool(item.get("shortcut_exists")),
            "work_key": item.get("work_key") or "",
            "yaml_source_rel": item.get("yaml_source_rel") or "",
            "index_in_file": item.get("index_in_file"),
            "press_key": item.get("press_key") or "",
            "press_format": item.get("press_format") or "",
            "press_group": item.get("press_group") or "",
            "associated": [assoc],
            "children": [],
        }
        if isinstance(item.get("target_fix"), dict):
            link_node["target_fix"] = dict(cast(dict[str, Any], item["target_fix"]))
        if not link_node["shortcut_exists"] and item.get("source") != "index_db":
            link_node["warnings"] = ["索引链接尚未生成"]
        if not link_node["target_exists"]:
            link_node.setdefault("warnings", []).append("目标目录不存在")
        if link_node["shortcut_exists"] and not link_node["shortcut_target_exists"]:
            link_node.setdefault("warnings", []).append("lnk 实际指向目录不存在")
        if link_node["shortcut_exists"] and not link_node["db_linked"]:
            link_node.setdefault("warnings", []).append("lnk 指向与 DB 目标路径不一致")
        cur.setdefault("children", []).append(link_node)
    for item in disk_leaves or []:
        rel_l = str(item.get("shortcut_relpath") or "").lower()
        if not rel_l or not _disk_leaf_is_unmapped(
            item,
            planned_relpaths=planned_relpaths,
            planned_targets=planned_targets,
        ):
            continue
        parts = item.get("shortcut_parts")
        if not isinstance(parts, list) or not parts:
            continue
        sr, cur = tree_root_for(item.get("shortcut_root"))
        rel_parts = []
        for part in [str(x) for x in parts[:-1]]:
            rel_parts.append(part)
            rel = "/".join(rel_parts)
            cur = _folder_child(cur, part, rel, str((sr / rel).resolve()))
        rel = str(item.get("shortcut_relpath") or "")
        match = (disk_matches or {}).get(rel.lower()) or {}
        candidates_raw = match.get("candidates") if isinstance(match, dict) else None
        candidates = [c for c in candidates_raw if isinstance(c, dict)] if isinstance(candidates_raw, list) else []
        matched_candidates = [c for c in candidates if c.get("can_apply")]
        best = matched_candidates[0] if matched_candidates else {}
        cur.setdefault("children", []).append(
            {
                "type": "link",
                "name": str(parts[-1]),
                "relpath": rel,
                "path": str(item.get("shortcut_path") or ""),
                "shortcut_path": str(item.get("shortcut_path") or ""),
                "target_path": str(item.get("target_path") or ""),
                "shortcut_target_path": str(item.get("target_path") or ""),
                "open_path": str(item.get("shortcut_path") or ""),
                "status": "unmapped_on_disk",
                "db_associated": False,
                "db_linked": False,
                "db_name_matched": bool(best),
                "db_match_type": str(best.get("match_type") or ""),
                "db_match_name": str(best.get("name") or ""),
                "db_match_press": str(best.get("press_label") or ""),
                "link_exists": bool(item.get("target_exists")),
                "target_exists": bool(item.get("target_exists")),
                "target_resolved": bool(item.get("target_resolved")),
                "target_error": str(item.get("target_error") or ""),
                "shortcut_exists": True,
                "associated": [],
                "warnings": ["DB 数据中没有找到关联"],
                "children": [],
            }
        )
        disk_node = cur.setdefault("children", [])[-1]
        if not disk_node.get("target_exists"):
            disk_node.setdefault("warnings", []).append("lnk 实际指向目录不存在")
        if isinstance(item.get("target_fix"), dict):
            disk_node["target_fix"] = dict(cast(dict[str, Any], item["target_fix"]))
            disk_node.setdefault("warnings", []).append("资源库目录中找到可修复的真实目录")
        if best:
            disk_node["warnings"] = [
                f"DB 名称+压制匹配：{best.get('name') or ''} / {best.get('press_label') or ''}，尚未写入索引关联"
            ]
        elif candidates:
            disk_node["warnings"] = ["DB 作品名有候选，但压制格式或压制组未匹配"]
    return _strip_tree_internal(root)


def _slim_tree_for_index_browser(node: dict[str, Any]) -> dict[str, Any]:
    node_type = str(node.get("type") or "folder")
    out: dict[str, Any] = {
        "type": node_type,
        "name": str(node.get("name") or ""),
        "relpath": str(node.get("relpath") or ""),
        "children": [
            _slim_tree_for_index_browser(child)
            for child in node.get("children", [])
            if isinstance(child, dict)
        ],
    }
    if node_type == "link":
        for key in (
            "status",
            "source",
            "shortcut_path",
            "target_path",
            "shortcut_target_path",
            "matched_shortcut_path",
            "matched_shortcut_relpath",
            "target_resolved",
            "target_error",
            "shortcut_target_exists",
            "shortcut_exists",
            "target_exists",
            "link_exists",
            "db_associated",
            "db_linked",
            "db_name_matched",
            "target_fix",
            "entry_key",
            "yaml_source_rel",
            "index_in_file",
            "press_format",
            "press_group",
        ):
            if key in node:
                out[key] = node[key]
        return out
    if "path" in node:
        out["path"] = str(node.get("path") or "")
    return out


def _strict_name_key(raw: Any) -> str:
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    return re.sub(r"\s+", " ", s)


def _press_component_key(raw: Any) -> str:
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s


def _press_alias_match_keys(raw: Any) -> set[str]:
    key = _press_component_key(raw)
    if not key:
        return set()
    compact = re.sub(r"[^a-z0-9]+", "", key)
    out: set[str] = set()
    for token in _PRESS_ALIAS_TOKENS:
        alias = _press_component_key(token)
        if not alias:
            continue
        boundary = re.search(rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)", key)
        if boundary or compact.startswith(alias) or compact.endswith(alias):
            out.add(alias)
    return out


def _press_group_match_key(raw: Any) -> str:
    key = _press_component_key(raw)
    return "" if key in {"", "----"} else key


def _resource_fix_items_from_cache() -> list[dict[str, Any]]:
    try:
        payload = _load_resource_scan_cache()
    except (OSError, ValueError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _candidate_press_resource_match_keys(candidate: dict[str, Any] | None) -> set[str]:
    if not isinstance(candidate, dict):
        return set()
    fmt = _str_or_blank(candidate.get("press_format"))
    group = _str_or_blank(candidate.get("press_group"))
    if not fmt:
        return set()
    keys = _press_alias_match_keys(fmt) | _press_alias_match_keys(group)
    group_key = _press_group_match_key(group)
    if not group_key:
        keys.add(_press_component_key(fmt))
        return {key for key in keys if key}
    variants = [
        f"{fmt}({group})",
        f"{fmt}-{group}",
        f"{fmt}_{group}",
        f"{fmt} {group}",
        f"{fmt}/{group}",
    ]
    keys.update(_press_component_key(value) for value in variants if _press_component_key(value))
    return {key for key in keys if key}


def _resource_name_without_press_suffix(resource_name: Any, press_info: Any) -> str:
    name = str(resource_name or "").strip()
    press = str(press_info or "").strip()
    if not name or not press:
        return ""
    folded = name.casefold()
    for sep in ("_", "-", " "):
        marker = f"{sep}{press}".casefold()
        if folded.endswith(marker):
            return name[: -len(marker)].strip()
    return ""


def _resource_work_match_keys(resource: dict[str, Any]) -> set[str]:
    values = [
        resource.get("work_name") or "",
        _resource_name_without_press_suffix(resource.get("name"), resource.get("press_info")),
    ]
    return {key for key in (_strict_name_key(value) for value in values) if key}


def _link_index_lite_payload_signature(settings: JpTvBrowseSettings) -> tuple[Any, ...]:
    files = []
    for fp in _catalog_yaml_paths(settings):
        try:
            st = fp.stat()
        except OSError:
            files.append((str(fp), None, None))
            continue
        files.append((str(fp), st.st_mtime_ns, st.st_size))
    index_db = _link_index_db_path()
    try:
        index_st = index_db.stat()
        index_sig: tuple[Any, ...] = (str(index_db), index_st.st_mtime_ns, index_st.st_size)
    except OSError:
        index_sig = (str(index_db), None, None)
    config_s = json.dumps(collection_link_index_config_json(), ensure_ascii=False, sort_keys=True)
    return (tuple(files), index_sig, config_s, _SHORTCUT_SCAN_CACHE.get("signature"))


def _cached_link_index_lite_payload(settings: JpTvBrowseSettings) -> dict[str, Any] | None:
    signature = _link_index_lite_payload_signature(settings)
    if _LINK_INDEX_LITE_PAYLOAD_CACHE.get("signature") != signature:
        return None
    payload = _LINK_INDEX_LITE_PAYLOAD_CACHE.get("payload")
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def _cache_link_index_lite_payload(settings: JpTvBrowseSettings, payload: dict[str, Any]) -> None:
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = _link_index_lite_payload_signature(settings)
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = payload


def validate_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    resource_payload: dict[str, Any] | None = None
    if body.get("refresh_resources", True) is not False:
        resource_payload = scan_resource_libraries_payload()
    works = _load_catalog_works(settings)
    db = _load_link_index_db()
    raw_items = db.get("items")
    previous_items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    validation_plan = _index_entries_from_works(
        works,
        previous_items=previous_items,
        use_resource_index=True,
        prefer_previous_target=True,
    )
    payload = _payload_from_works(
        works,
        refresh_links=True,
        catalog_root=_settings_catalog_root_key(settings),
        plan_override=validation_plan,
    )
    summary = payload.get("plan_summary") if isinstance(payload.get("plan_summary"), dict) else {}
    payload["validation"] = {
        "resource_scan_summary": (resource_payload or {}).get("summary") if isinstance(resource_payload, dict) else None,
        "mismatch_count": int(summary.get("target_fixable") or 0),
        "index_db_path": str(_link_index_db_path()),
    }
    return payload


def _is_index_db_fix_request(raw_items: list[Any]) -> bool:
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        shortcut_s = _str_or_blank(raw.get("shortcut_path") or raw.get("path"))
        if _str_or_blank(raw.get("source")) == "index_db":
            return True
        if not shortcut_s and _str_or_blank(raw.get("shortcut_relpath")):
            return True
        if shortcut_s.startswith("indexdb://"):
            return True
    return False


def _apply_link_index_db_target_fixes(
    raw_items: list[Any],
    *,
    settings: JpTvBrowseSettings,
    refresh_payload: bool,
) -> dict[str, Any]:
    db = _load_link_index_db()
    items = db.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("索引 DB 为空，请先重新生成索引。")
    by_rel: dict[str, dict[str, Any]] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        rel = _str_or_blank(item.get("shortcut_relpath") or item.get("relpath")).replace("\\", "/").lower()
        key = _str_or_blank(item.get("entry_key"))
        if rel:
            by_rel[rel] = item
        if key:
            by_key[key] = item
    fixes: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{idx}] 必须为对象")
        rel = _str_or_blank(raw.get("shortcut_relpath") or raw.get("relpath")).replace("\\", "/").lower()
        key = _str_or_blank(raw.get("entry_key"))
        target_s = _str_or_blank(raw.get("target_path") or raw.get("fix_target_path"))
        if not target_s:
            raise ValueError(f"items[{idx}] 缺少 target_path")
        target = Path(target_s).expanduser()
        try:
            target = target.resolve()
        except OSError:
            pass
        if not target.is_dir():
            raise ValueError(f"items[{idx}].target_path 不是存在的资源目录")
        item = by_key.get(key) if key else None
        if item is None and rel:
            item = by_rel.get(rel)
        if item is None:
            raise ValueError(f"items[{idx}] 没有找到对应的索引 DB 项")
        item["target_path"] = str(target)
        item["shortcut_target_path"] = str(target)
        item["target_exists"] = True
        item["shortcut_target_exists"] = True
        item["link_exists"] = True
        item["db_linked"] = True
        item["target_source"] = "manual_fix"
        item["status"] = "ready"
        item.pop("target_fix", None)
        fixes.append(
            {
                "shortcut_relpath": _str_or_blank(item.get("shortcut_relpath") or item.get("relpath")),
                "target_path": str(target),
            }
        )
    db["items"] = items
    db["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_link_index_db(db)
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None
    if not refresh_payload:
        return {"fixes": fixes}
    payload = collection_link_index_payload(settings, refresh_links=True)
    payload["fixes"] = fixes
    return payload


def apply_link_index_target_fixes_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    if not _is_index_db_fix_request(raw_items):
        raise ValueError("link index target fixes only support index DB items")
    return _apply_link_index_db_target_fixes(
        raw_items,
        settings=settings,
        refresh_payload=body.get("refresh_payload") is not False,
    )


def _raw_collection_data(work: Any) -> dict[str, Any]:
    if not isinstance(work, dict):
        raise ValueError("作品必须为对象")
    attrs = work.get("attributes")
    if not isinstance(attrs, list):
        raise ValueError("作品缺少 attributes")
    for attr in attrs:
        if isinstance(attr, dict) and attr.get("type") == "collection-type":
            data = attr.get("data")
            if not isinstance(data, dict):
                data = {}
                attr["data"] = data
            return cast(dict[str, Any], data)
    data: dict[str, Any] = {}
    attrs.append({"type": "collection-type", "data": data})
    return data


def _raw_press_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    main = data.get("collectioned")
    if isinstance(main, list):
        for row in main:
            if isinstance(row, dict) and jp_tv_press_pair_from_row(row):
                rows.append(cast(dict[str, Any], row))
    conts = data.get("continuations")
    if isinstance(conts, list):
        for blk in conts:
            if not isinstance(blk, dict):
                continue
            cel = blk.get("collectioned")
            if not isinstance(cel, list):
                continue
            for row in cel:
                if isinstance(row, dict) and jp_tv_press_pair_from_row(row):
                    rows.append(cast(dict[str, Any], row))
    return rows


def _set_mapping_on_raw_work(work: Any, mapping: dict[str, Any]) -> bool:
    data = _raw_collection_data(work)
    changed = False
    next_path = _clean_work_path(mapping.get("path"), label="path")
    old_path = _str_or_blank(data.get("path")).replace("\\", "/")
    if next_path:
        if old_path != next_path:
            data["path"] = next_path
            changed = True
    elif "path" in data:
        data.pop("path", None)
        changed = True

    row_refs = _raw_press_rows(data)
    for pmap in mapping.get("press", []):
        if not isinstance(pmap, dict):
            continue
        pos = _press_key_position(str(pmap.get("press_key") or ""))
        if pos is None:
            continue
        if pos >= len(row_refs):
            raise ValueError(f"press_key 越界：{pmap.get('press_key')}")
        row = row_refs[pos]
        next_press_path = _clean_rel_path(pmap.get(TV_JP_PRESS_PATH_KEY), label="press_path")
        old_press_path = _str_or_blank(row.get(TV_JP_PRESS_PATH_KEY)).replace("\\", "/")
        if next_press_path:
            if old_press_path != next_press_path:
                row[TV_JP_PRESS_PATH_KEY] = next_press_path
                changed = True
        elif TV_JP_PRESS_PATH_KEY in row:
            row.pop(TV_JP_PRESS_PATH_KEY, None)
            changed = True
    return changed


def _save_ui_mappings_to_catalog(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> list[dict[str, Any]]:
    mappings = _ui_mapping_items(body)
    if not mappings:
        raise ValueError("没有可保存的索引关联")
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，不能写回索引关联")

    by_rel: dict[str, list[dict[str, Any]]] = {}
    for item in mappings:
        by_rel.setdefault(str(item["yaml_source_rel"]), []).append(item)

    writes: list[dict[str, Any]] = []
    hist_root = history_catalog_root(settings)
    for rel_s in sorted(by_rel.keys()):
        target = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s).expanduser().resolve()
        _assert_save_target_allowed(target, settings)
        raw_text = target.read_text(encoding="utf-8")
        doc = load_yaml_string(raw_text)
        works = _works_list_mut(doc)
        changed = 0
        for mapping in by_rel[rel_s]:
            idx = int(mapping["index_in_file"])
            if idx < 0 or idx >= len(works):
                raise ValueError(f"{rel_s}: index_in_file 越界：{idx}")
            if _set_mapping_on_raw_work(works[idx], mapping):
                changed += 1
        if changed <= 0:
            continue
        new_text = dump_yaml_string(doc)
        try:
            load_jp_tv_entries_from_yaml(load_yaml_string(new_text))
        except Exception as exc:
            raise ValueError(f"{rel_s} 写回索引关联后的 YAML 校验失败：{exc}") from exc
        hist_root.mkdir(parents=True, exist_ok=True)
        hist_name = history_snapshot_name(target)
        shutil.copy2(target, hist_root / hist_name)
        target.write_text(new_text, encoding="utf-8")
        writes.append({"path": str(target), "history_file": hist_name, "changes": changed})
    return writes


def _create_windows_shortcut(shortcut_path: Path, target_path: Path) -> None:
    if os.name != "nt":
        raise OSError(".lnk generation is only supported on Windows")
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    powershell_exe = os.environ.get(
        "SystemRoot",
        r"C:\Windows",
    ) + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
    script = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$shortcutPath = $env:NIMDA_SHORTCUT_PATH\n"
        "$targetPath = $env:NIMDA_TARGET_PATH\n"
        "$typeDefinition = @'\n"
        "using System;\n"
        "using System.Text;\n"
        "using System.Runtime.InteropServices;\n"
        "namespace NimdaShortcut {\n"
        "  [ComImport, Guid(\"00021401-0000-0000-C000-000000000046\")]\n"
        "  public class ShellLink {}\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"000214F9-0000-0000-C000-000000000046\")]\n"
        "  public interface IShellLinkW {\n"
        "    [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cchMaxPath, IntPtr pfd, uint fFlags);\n"
        "    [PreserveSig] int GetIDList(out IntPtr ppidl);\n"
        "    [PreserveSig] int SetIDList(IntPtr pidl);\n"
        "    [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cchMaxName);\n"
        "    [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);\n"
        "    [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cchMaxPath);\n"
        "    [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);\n"
        "    [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cchMaxPath);\n"
        "    [PreserveSig] int SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);\n"
        "    [PreserveSig] int GetHotkey(out short pwHotkey);\n"
        "    [PreserveSig] int SetHotkey(short wHotkey);\n"
        "    [PreserveSig] int GetShowCmd(out int piShowCmd);\n"
        "    [PreserveSig] int SetShowCmd(int iShowCmd);\n"
        "    [PreserveSig] int GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cchIconPath, out int piIcon);\n"
        "    [PreserveSig] int SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);\n"
        "    [PreserveSig] int SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);\n"
        "    [PreserveSig] int Resolve(IntPtr hwnd, uint fFlags);\n"
        "    [PreserveSig] int SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);\n"
        "  }\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"0000010B-0000-0000-C000-000000000046\")]\n"
        "  public interface IPersistFile {\n"
        "    void GetClassID(out Guid pClassID);\n"
        "    [PreserveSig] int IsDirty();\n"
        "    [PreserveSig] int Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);\n"
        "    [PreserveSig] int Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);\n"
        "    [PreserveSig] int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);\n"
        "    [PreserveSig] int GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);\n"
        "  }\n"
        "  public static class ShortcutWriter {\n"
        "    public static void Save(string shortcutPath, string targetPath) {\n"
        "      IShellLinkW shellLink = (IShellLinkW)new ShellLink();\n"
        "      int hr = shellLink.SetPath(targetPath);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      hr = shellLink.SetWorkingDirectory(targetPath);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      IPersistFile persist = (IPersistFile)shellLink;\n"
        "      hr = persist.Save(shortcutPath, true);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "    }\n"
        "  }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $typeDefinition\n"
        "$shortcutPath = [System.IO.Path]::GetFullPath($shortcutPath)\n"
        "$targetPath = [System.IO.Path]::GetFullPath($targetPath)\n"
        "[NimdaShortcut.ShortcutWriter]::Save($shortcutPath, $targetPath)\n"
    )
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        [
            powershell_exe,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        env={
            **os.environ.copy(),
            "NIMDA_SHORTCUT_PATH": str(shortcut_path),
            "NIMDA_TARGET_PATH": str(target_path),
        },
        **kwargs,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if not detail:
            detail = f"PowerShell exited with code {proc.returncode}"
        raise OSError(f"写入 .lnk 失败：{detail}")


_SCOPED_SHORTCUT_LOCK = threading.RLock()


def _scoped_work_context(work: dict[str, Any]) -> dict[str, Any]:
    date = work.get("date") if isinstance(work.get("date"), dict) else {}
    begin_date = _str_or_blank(date.get("start") or work.get("begin_date"))
    end_date = _str_or_blank(date.get("end") or work.get("end_date"))
    year_match = re.search(r"(?:19|20)\d{2}", begin_date)
    year = year_match.group(0) if year_match else ""
    return {
        "work_key": work.get("work_key") or "",
        "yaml_source_rel": work.get("yaml_source_rel") or "",
        "index_in_file": work.get("index_in_file"),
        "name": work.get("name") or "",
        "path": work.get("path") or "",
        "domain": work.get("domain") or "",
        "country": work.get("country") or "",
        "release_type": work.get("release_type") or "",
        "year": year,
        "year_label": f"[{year}]" if year else "",
        "begin_date": begin_date,
        "end_date": end_date,
        "date_range_label": (
            f"[{begin_date}][{end_date}]"
            if begin_date and end_date
            else f"[{begin_date or end_date}]"
            if begin_date or end_date
            else ""
        ),
    }


def preview_scoped_shortcuts_for_work(
    work: dict[str, Any],
    presses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Plan shortcuts for one work without clearing or writing shortcut roots."""

    work_ctx = _scoped_work_context(work)
    if not _str_or_blank(work_ctx.get("name")):
        raise ValueError("scoped shortcut work name cannot be empty")
    planned: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw_press in enumerate(presses):
        if not isinstance(raw_press, dict):
            raise ValueError(f"scoped shortcut press {index + 1} must be an object")
        press_format = _str_or_blank(raw_press.get(TV_JP_PRESS_FORMAT_KEY))
        press_group = _str_or_blank(raw_press.get(TV_JP_PRESS_GROUP_KEY))
        press_path = _str_or_blank(raw_press.get(TV_JP_PRESS_PATH_KEY)).replace("\\", "/")
        target_s = _str_or_blank(raw_press.get("target_path"))
        if not press_format or not press_group or not press_path or not target_s:
            raise ValueError(
                f"scoped shortcut press {index + 1} requires format, group, press_path and target_path"
            )
        target = Path(target_s).expanduser().resolve()
        if not _path_under_any_root(target, resource_roots()):
            raise ValueError(f"shortcut target is outside configured resource roots: {target}")
        press = {
            "press_key": raw_press.get("press_key") or f"{index}:main::{press_format}:{press_group}",
            "label": raw_press.get("label") or f"{press_format}-{press_group}",
            TV_JP_PRESS_FORMAT_KEY: press_format,
            TV_JP_PRESS_GROUP_KEY: press_group,
            TV_JP_PRESS_PATH_KEY: press_path,
        }
        relpath, parts = _index_relpath_for(work_ctx, press)
        item_root = _shortcut_root_for_work(work_ctx)
        shortcut_path = _safe_shortcut_path_from_rel(item_root, relpath)
        shortcut_key = _path_compare_key(shortcut_path)
        if shortcut_key in seen_paths:
            raise ValueError(f"multiple presses resolve to the same shortcut path: {shortcut_path}")
        seen_paths.add(shortcut_key)
        status = "planned"
        existing_target = ""
        if shortcut_path.exists():
            if not shortcut_path.is_file():
                status = "conflict"
            else:
                existing_target = _windows_shortcut_target(shortcut_path)
                status = (
                    "already_exists"
                    if existing_target and _path_compare_key(existing_target) == _path_compare_key(target)
                    else "conflict"
                )
        entry = _index_entry_from_work_press(work_ctx, press, [])
        entry.update(
            {
                "target_path": str(target),
                "target_source": "media_directory_organizer",
                "target_exists": target.is_dir(),
                "shortcut_path": str(shortcut_path),
                "shortcut_root": str(item_root),
                "shortcut_relpath": relpath,
                "shortcut_parts": parts,
                "shortcut_target_path": str(target),
                "shortcut_exists": status == "already_exists",
                "matched_shortcut_path": str(shortcut_path) if status == "already_exists" else "",
                "matched_shortcut_relpath": relpath if status == "already_exists" else "",
            }
        )
        planned.append(
            {
                "status": status,
                "work_name": str(work_ctx["name"]),
                "press_format": press_format,
                "press_group": press_group,
                "press_path": press_path,
                "target_path": str(target),
                "target_exists_before_move": target.is_dir(),
                "shortcut_root": str(item_root),
                "shortcut_relpath": relpath,
                "shortcut_path": str(shortcut_path),
                "existing_target_path": existing_target,
                "index_entry": entry,
            }
        )
    return planned


def refresh_link_index_db_from_catalog(settings: JpTvBrowseSettings) -> dict[str, Any]:
    """Refresh the YAML index DB only; this never removes or writes .lnk files."""

    works = _load_catalog_works(settings)
    previous = _load_link_index_db()
    previous_items = previous.get("items") if isinstance(previous.get("items"), list) else []
    entries = _index_entries_from_works(
        works,
        previous_items=[item for item in previous_items if isinstance(item, dict)],
        use_resource_index=False,
    )
    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "collection-detail catalog yaml",
        "catalog_root": _settings_catalog_root_key(settings),
        "layout_levels": list(_layout_levels()),
        "shortcut_name": _shortcut_name_template(),
        "items": entries,
    }
    _save_link_index_db(payload)
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None
    return {
        "path": str(_link_index_db_path()),
        "generated_at": payload["generated_at"],
        "item_count": len(entries),
    }


def apply_scoped_shortcuts_for_work(
    items: list[dict[str, Any]],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    """Create only the reviewed work's missing shortcuts and refresh index DB."""

    with _SCOPED_SHORTCUT_LOCK:
        prepared: list[tuple[Path, Path, dict[str, Any]]] = []
        already_exists = 0
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"scoped shortcut item {index + 1} must be an object")
            item_root = Path(_str_or_blank(item.get("shortcut_root"))).expanduser().resolve()
            if not any(_path_compare_key(item_root) == _path_compare_key(root) for root in shortcut_roots()):
                raise ValueError(f"scoped shortcut root is not configured: {item_root}")
            shortcut_path = _safe_shortcut_path_from_rel(item_root, item.get("shortcut_relpath"))
            if _path_compare_key(shortcut_path) != _path_compare_key(item.get("shortcut_path")):
                raise ValueError("scoped shortcut path no longer matches the reviewed plan")
            target = Path(_str_or_blank(item.get("target_path"))).expanduser().resolve()
            if not target.is_dir() or not _path_under_any_root(target, resource_roots()):
                raise FileNotFoundError(f"shortcut target directory is missing or outside resource roots: {target}")
            if shortcut_path.exists():
                existing_target = _windows_shortcut_target(shortcut_path) if shortcut_path.is_file() else ""
                if existing_target and _path_compare_key(existing_target) == _path_compare_key(target):
                    already_exists += 1
                    continue
                raise FileExistsError(f"shortcut path already exists with another target: {shortcut_path}")
            prepared.append((shortcut_path, target, item))

        created: list[Path] = []
        index_path = _link_index_db_path()
        previous_index = index_path.read_bytes() if index_path.is_file() else None
        try:
            for shortcut_path, target, _item in prepared:
                _create_windows_shortcut(shortcut_path, target)
                created.append(shortcut_path)
            index_result = refresh_link_index_db_from_catalog(settings)
        except BaseException:
            for shortcut_path in reversed(created):
                shortcut_path.unlink(missing_ok=True)
            if previous_index is None:
                index_path.unlink(missing_ok=True)
            else:
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_bytes(previous_index)
            raise
        return {
            "ok": True,
            "planned_count": len(items),
            "created_count": len(created),
            "already_exists_count": already_exists,
            "shortcut_paths": [str(path) for path in created],
            "index_db": index_result,
        }


def _payload_from_works(
    works: list[dict[str, Any]],
    *,
    refresh_links: bool = False,
    lite: bool = False,
    catalog_root: str = "",
    plan_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if plan_override is not None:
        plan = plan_override
        index_db_exists = True
    else:
        plan, index_db_exists = _index_db_items_for_payload(works, catalog_root=catalog_root)
    plan_summary = _plan_summary(plan)
    plan_summary["unmapped_on_disk"] = 0
    tree = _build_tree(plan, [], {})
    payload: dict[str, Any] = {
        "ok": True,
        "config": collection_link_index_config_json(),
        "plan_summary": plan_summary,
        "mapping_summary": _mapping_summary(works),
        "disk_summary": {
            "shortcut_leaves": 0,
            "unmapped_on_disk": 0,
            "db_match_cached": False,
            "index_db_exists": index_db_exists,
            "index_db_path": str(_link_index_db_path()),
        },
        "tree": _slim_tree_for_index_browser(tree) if lite else tree,
    }
    if lite:
        return payload
    payload.update(
        {
            "works": works,
            "plan": plan[:300],
        }
    )
    return payload


def _lite_payload_from_index_db(settings: JpTvBrowseSettings) -> dict[str, Any] | None:
    catalog_root = _settings_catalog_root_key(settings)
    if not catalog_root:
        return None
    db = _load_link_index_db()
    if _str_or_blank(db.get("catalog_root")) != catalog_root:
        return None
    items = _refreshed_index_db_display_items(db.get("items"))
    if not items:
        return None
    payload = _payload_from_works(
        [],
        lite=True,
        catalog_root=catalog_root,
        plan_override=items,
    )
    payload["mapping_summary"] = {
        "total_press": len(items),
        "mapped_press": sum(1 for item in items if _str_or_blank(item.get("target_path"))),
        "unconfigured_press": sum(1 for item in items if not _str_or_blank(item.get("target_path"))),
        "unconfigured_work_path": 0,
        "unconfigured_press_path": 0,
    }
    return payload


def collection_link_index_payload(
    settings: JpTvBrowseSettings,
    *,
    refresh_links: bool = False,
    lite: bool = False,
) -> dict[str, Any]:
    if lite and not refresh_links:
        fast_payload = _lite_payload_from_index_db(settings)
        if fast_payload is not None:
            return fast_payload
        cached = _cached_link_index_lite_payload(settings)
        if cached is not None:
            return cached
    payload = _payload_from_works(
        _load_catalog_works(settings),
        refresh_links=refresh_links,
        lite=lite,
        catalog_root=_settings_catalog_root_key(settings),
    )
    if lite:
        _cache_link_index_lite_payload(settings, payload)
    return payload


def preview_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    works = _merge_ui_mappings(_load_catalog_works(settings), _ui_mapping_items(body))
    return _payload_from_works(works, catalog_root=_settings_catalog_root_key(settings))


def save_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    writes = _save_ui_mappings_to_catalog(body, settings=settings)
    payload = collection_link_index_payload(settings)
    payload["writes"] = writes
    return payload


def generate_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    works = _load_catalog_works(settings)
    if body.get("refresh_resources") is True:
        scan_resource_libraries_payload()
    saved = _save_index_entries_from_works(works, catalog_root=_settings_catalog_root_key(settings))
    payload = _payload_from_works(works, refresh_links=True, catalog_root=_settings_catalog_root_key(settings))
    payload["generated"] = saved.get("items", [])
    payload["index_db"] = {
        "path": str(_link_index_db_path()),
        "generated_at": saved.get("generated_at") or "",
        "item_count": len(saved.get("items") or []),
    }
    return payload


def _shortcut_root_direct_entries(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"索引输出路径不是目录：{root}")
    return list(root.iterdir())


def _safe_shortcut_path_from_rel(root: Path, relpath: Any) -> Path:
    rel = _str_or_blank(relpath).replace("\\", "/").strip("/")
    if not rel:
        raise ValueError("索引项缺少相对路径")
    rel_path = Path(rel)
    if rel_path.is_absolute() or any(part in {"", ".", ".."} for part in rel_path.parts):
        raise ValueError(f"索引项路径非法：{rel}")
    target = (root / rel_path).resolve()
    target.relative_to(root)
    if target.suffix.lower() != ".lnk":
        target = target.with_suffix(target.suffix + ".lnk") if target.suffix else target.with_suffix(".lnk")
    return target


def _clear_directory_contents(root: Path) -> int:
    removed = 0
    root.mkdir(parents=True, exist_ok=True)
    for child in list(root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


def _backup_shortcut_root(root: Path, backup_dir_raw: Any) -> str:
    backup_parent_s = _str_or_blank(backup_dir_raw)
    if not backup_parent_s:
        return ""
    backup_parent = Path(backup_parent_s).expanduser().resolve()
    root_resolved = root.resolve()
    try:
        backup_parent.relative_to(root_resolved)
        raise ValueError("备份目录不能位于索引输出目录内部")
    except ValueError as exc:
        if "备份目录" in str(exc):
            raise
    if _path_compare_key(backup_parent) == _path_compare_key(root_resolved):
        raise ValueError("备份目录不能等于索引输出目录")
    backup_parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = root_resolved.name or "index-output"
    backup_path = backup_parent / f"{base_name}-{stamp}"
    suffix = 1
    while backup_path.exists():
        suffix += 1
        backup_path = backup_parent / f"{base_name}-{stamp}-{suffix}"
    shutil.copytree(root_resolved, backup_path)
    return str(backup_path)


def _link_index_file_generation_plan(settings: JpTvBrowseSettings) -> tuple[list[dict[str, Any]], bool]:
    works = _load_catalog_works(settings)
    return _index_db_items_for_payload(works, catalog_root=_settings_catalog_root_key(settings))


def _shortcut_root_for_index_item(item: dict[str, Any]) -> Path:
    configured = _str_or_blank(item.get("shortcut_root"))
    return Path(configured).expanduser().resolve() if configured else _shortcut_root_for_work(item)


def _link_index_file_generation_preview(items: list[dict[str, Any]]) -> dict[str, Any]:
    roots_in_plan: list[Path] = []
    seen_roots: set[str] = set()
    for item in items:
        root = _shortcut_root_for_index_item(item)
        key = _path_compare_key(root)
        if key and key not in seen_roots:
            seen_roots.add(key)
            roots_in_plan.append(root)
    if not roots_in_plan:
        roots_in_plan = [shortcut_root()]
    root_rows: list[dict[str, Any]] = []
    all_entries: list[Path] = []
    for root in roots_in_plan:
        entries = _shortcut_root_direct_entries(root)
        all_entries.extend(entries)
        root_rows.append(
            {
                "output_root": str(root),
                "root_exists": root.exists(),
                "root_non_empty": bool(entries),
                "existing_count": len(entries),
                "existing_sample": [entry.name for entry in entries[:20]],
                "planned": sum(
                    1 for item in items if _path_compare_key(_shortcut_root_for_index_item(item)) == _path_compare_key(root)
                ),
            }
        )
    creatable = 0
    skipped_empty_target = 0
    skipped_missing_target = 0
    for item in items:
        target_s = _str_or_blank(item.get("target_path"))
        if not target_s:
            skipped_empty_target += 1
            continue
        if "target_exists" in item:
            target_exists = bool(item.get("target_exists"))
        else:
            try:
                target_exists = Path(target_s).expanduser().is_dir()
            except OSError:
                target_exists = False
        if not target_exists:
            skipped_missing_target += 1
            continue
        creatable += 1
    return {
        "output_root": str(roots_in_plan[0]),
        "output_roots": [str(root) for root in roots_in_plan],
        "roots": root_rows,
        "root_exists": all(root.exists() for root in roots_in_plan),
        "root_non_empty": bool(all_entries),
        "existing_count": len(all_entries),
        "existing_sample": [str(entry) for entry in all_entries[:20]],
        "total": len(items),
        "creatable": creatable,
        "skipped_empty_target": skipped_empty_target,
        "skipped_missing_target": skipped_missing_target,
    }


def generate_link_index_files_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    items, index_db_exists = _link_index_file_generation_plan(settings)
    preview = _link_index_file_generation_preview(items)
    preview["index_db_exists"] = index_db_exists
    if body.get("preview") is True:
        return {
            "config": collection_link_index_config_json(),
            "file_generation": preview,
        }
    roots = [Path(value).expanduser().resolve() for value in preview["output_roots"]]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    if preview["root_non_empty"] and (
        body.get("confirm_clear") is not True or body.get("confirm_clear_twice") is not True
    ):
        raise ValueError("索引输出目录非空，生成前必须完成两次确认")
    backup_paths: list[str] = []
    removed_count = 0
    for root in roots:
        entries = _shortcut_root_direct_entries(root)
        if entries and _str_or_blank(body.get("backup_dir")):
            backup_paths.append(_backup_shortcut_root(root, body.get("backup_dir")))
        if entries:
            removed_count += _clear_directory_contents(root)
    created = 0
    skipped_empty_target = 0
    skipped_missing_target = 0
    failed: list[dict[str, Any]] = []
    for item in items:
        rel = _str_or_blank(item.get("shortcut_relpath") or item.get("relpath"))
        target_s = _str_or_blank(item.get("target_path"))
        if not target_s:
            skipped_empty_target += 1
            continue
        try:
            target_path = Path(target_s).expanduser().resolve()
        except OSError:
            target_path = Path(target_s).expanduser()
        if not target_path.is_dir():
            skipped_missing_target += 1
            continue
        try:
            item_root = _shortcut_root_for_index_item(item)
            shortcut_path = _safe_shortcut_path_from_rel(item_root, rel)
            _create_windows_shortcut(shortcut_path, target_path)
            created += 1
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            failed.append(
                {
                    "shortcut_relpath": rel,
                    "target_path": target_s,
                    "error": str(exc),
                }
            )
    payload = collection_link_index_payload(settings)
    payload["file_generation"] = {
        **preview,
        "output_root": str(roots[0]),
        "output_roots": [str(root) for root in roots],
        "backup_path": backup_paths[0] if len(backup_paths) == 1 else "",
        "backup_paths": backup_paths,
        "removed_count": removed_count,
        "created": created,
        "skipped_empty_target": skipped_empty_target,
        "skipped_missing_target": skipped_missing_target,
        "failed_count": len(failed),
        "failed": failed[:50],
    }
    return payload


def _path_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _windows_shortcut_target(shortcut_path: Path) -> str:
    if os.name != "nt" or shortcut_path.suffix.lower() != ".lnk":
        return ""
    info = _windows_shortcut_targets([shortcut_path]).get(str(shortcut_path.expanduser().resolve())) or {}
    return str(info.get("target_path") or "")


def _windows_shortcut_targets(shortcut_paths: list[Path]) -> dict[str, dict[str, Any]]:
    paths = [p for p in shortcut_paths if p.suffix.lower() == ".lnk"]
    if os.name != "nt" or not paths:
        return {}
    powershell_exe = os.environ.get(
        "SystemRoot",
        r"C:\Windows",
    ) + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
    script = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$raw = [Console]::In.ReadToEnd()\n"
        "$paths = $raw | ConvertFrom-Json\n"
        "$typeDefinition = @'\n"
        "using System;\n"
        "using System.Text;\n"
        "using System.Runtime.InteropServices;\n"
        "namespace NimdaShortcutReader {\n"
        "  [ComImport, Guid(\"00021401-0000-0000-C000-000000000046\")]\n"
        "  public class ShellLink {}\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"000214F9-0000-0000-C000-000000000046\")]\n"
        "  public interface IShellLinkW {\n"
        "    [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cchMaxPath, IntPtr pfd, uint fFlags);\n"
        "    [PreserveSig] int GetIDList(out IntPtr ppidl);\n"
        "    [PreserveSig] int SetIDList(IntPtr pidl);\n"
        "    [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cchMaxName);\n"
        "    [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);\n"
        "    [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cchMaxPath);\n"
        "    [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);\n"
        "    [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cchMaxPath);\n"
        "    [PreserveSig] int SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);\n"
        "    [PreserveSig] int GetHotkey(out short pwHotkey);\n"
        "    [PreserveSig] int SetHotkey(short wHotkey);\n"
        "    [PreserveSig] int GetShowCmd(out int piShowCmd);\n"
        "    [PreserveSig] int SetShowCmd(int iShowCmd);\n"
        "    [PreserveSig] int GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cchIconPath, out int piIcon);\n"
        "    [PreserveSig] int SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);\n"
        "    [PreserveSig] int SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);\n"
        "    [PreserveSig] int Resolve(IntPtr hwnd, uint fFlags);\n"
        "    [PreserveSig] int SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);\n"
        "  }\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"0000010B-0000-0000-C000-000000000046\")]\n"
        "  public interface IPersistFile {\n"
        "    void GetClassID(out Guid pClassID);\n"
        "    [PreserveSig] int IsDirty();\n"
        "    [PreserveSig] int Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);\n"
        "    [PreserveSig] int Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);\n"
        "    [PreserveSig] int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);\n"
        "    [PreserveSig] int GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);\n"
        "  }\n"
        "  public static class ShortcutReader {\n"
        "    public static string Read(string shortcutPath) {\n"
        "      IShellLinkW shellLink = (IShellLinkW)new ShellLink();\n"
        "      IPersistFile persist = (IPersistFile)shellLink;\n"
        "      int hr = persist.Load(shortcutPath, 0);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      StringBuilder path = new StringBuilder(32768);\n"
        "      hr = shellLink.GetPath(path, path.Capacity, IntPtr.Zero, 0);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      return path.ToString();\n"
        "    }\n"
        "  }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $typeDefinition\n"
        "$rows = foreach ($shortcutPath in $paths) {\n"
        "  try {\n"
        "    $target = [NimdaShortcutReader.ShortcutReader]::Read([string]$shortcutPath)\n"
        "    [pscustomobject]@{ path = [string]$shortcutPath; target = [string]$target; error = '' }\n"
        "  } catch {\n"
        "    [pscustomobject]@{ path = [string]$shortcutPath; target = ''; error = [string]$_.Exception.Message }\n"
        "  }\n"
        "}\n"
        "$json = $rows | ConvertTo-Json -Compress\n"
        "[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))\n"
    )
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        [powershell_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        input=json.dumps([str(p) for p in paths], ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        **kwargs,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        decoded_stdout = base64.b64decode(proc.stdout.strip()).decode("utf-8")
        raw_rows = json.loads(decoded_stdout)
    except (ValueError, json.JSONDecodeError):
        return {}
    rows = raw_rows if isinstance(raw_rows, list) else [raw_rows]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        path_s = str(row.get("path") or "")
        if not path_s:
            continue
        out[str(Path(path_s).expanduser().resolve())] = {
            "target_path": str(row.get("target") or ""),
            "target_resolved": bool(row.get("target")),
            "error": str(row.get("error") or ""),
        }
    return out


def resolve_link_index_path_from_ui_body(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path 不能为空")
    p = Path(raw.strip()).expanduser().resolve()
    roots = [*resource_roots(), *shortcut_roots()]
    if not _path_under_any_root(p, roots):
        raise ValueError("只能解析资源库目录或索引输出目录下的路径")
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.is_file() and p.suffix.lower() == ".lnk":
        target_s = _windows_shortcut_target(p)
        if not target_s:
            raise FileNotFoundError(f"无法解析快捷方式目标：{p}")
        target = Path(target_s).expanduser().resolve()
        return {
            "source_path": str(p),
            "target_path": str(target),
            "target_exists": target.exists(),
            "open_path": str(target if target.is_dir() else target.parent),
            "resolved_from_shortcut": True,
        }
    return {
        "source_path": str(p),
        "target_path": str(p),
        "target_exists": p.exists(),
        "open_path": str(p if p.is_dir() else p.parent),
        "resolved_from_shortcut": False,
    }


def open_link_index_path_from_ui_body(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path 不能为空")
    p = Path(raw.strip()).expanduser().resolve()
    roots = [*resource_roots(), *shortcut_roots()]
    if not _path_under_any_root(p, roots):
        raise ValueError("只能打开资源库目录或索引输出目录下的路径")
    if not p.exists():
        raise FileNotFoundError(str(p))
    open_path = p
    resolved_from_shortcut = False
    if p.is_file() and p.suffix.lower() == ".lnk":
        target_s = _windows_shortcut_target(p)
        if not target_s:
            raise FileNotFoundError(f"无法解析快捷方式目标：{p}")
        target = Path(target_s).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(str(target))
        open_path = target if target.is_dir() else target.parent
        resolved_from_shortcut = True
    elif p.is_file():
        open_path = p.parent
    if os.name == "nt":
        os.startfile(str(open_path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(open_path)])
    return {"path": str(open_path), "source_path": str(p), "resolved_from_shortcut": resolved_from_shortcut}


def open_collection_press_path_from_ui_body(body: dict[str, Any]) -> dict[str, Any]:
    work_raw = body.get("path", body.get("work_path"))
    press_raw = body.get("press_path")
    if not isinstance(work_raw, str) or not work_raw.strip():
        raise ValueError("作品父路径不能为空")
    if not isinstance(press_raw, str) or not press_raw.strip():
        raise ValueError("压制路径不能为空")
    target = _target_for(_legacy_media_root(), work_raw.strip(), press_raw.strip())
    roots = resource_roots()
    if not _path_under_any_root(target, roots):
        raise ValueError("只能打开资源库目录下的连接路径")
    if not target.exists():
        raise FileNotFoundError(str(target))
    open_path = target if target.is_dir() else target.parent
    if os.name == "nt":
        os.startfile(str(open_path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(open_path)])
    return {
        "path": str(open_path),
        "target_path": str(target),
        "source_path": str(target),
        "resolved_from_shortcut": False,
    }
