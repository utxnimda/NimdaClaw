"""Build shortcut-index views from collection-detail catalog YAML."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import difflib
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
_DEFAULT_MEDIA_ROOT = "E:/LinkVideo/[ACG] Japan"
_DEFAULT_SHORTCUT_ROOT = "E:/LinkVideo/[ACG] Japan/Finish"
_SHORTCUT_SCAN_CACHE: dict[str, Any] = {"signature": None, "leaves": []}
_DISK_ASSOC_CACHE: dict[str, Any] = {"signature": None, "rows": []}
_LINK_INDEX_LITE_PAYLOAD_CACHE: dict[str, Any] = {"signature": None, "payload": None}
_ASSOCIATION_REJECTS_FILENAME = "link-index-rejected-associations.json"
_RESOURCE_SCAN_CACHE_FILENAME = "resource-library-scan-cache.yaml"
_RESOURCE_SCAN_LEGACY_JSON_FILENAME = "resource-library-scan-cache.json"
_RESOURCE_SCAN_NODE_DIRNAME = "resource-library-scan-cache"


def _str_or_blank(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _feature_config() -> dict[str, Any]:
    p = feature_config_path("collection-detail")
    if not p.is_file():
        return {}
    raw = load_yaml(p)
    return raw if isinstance(raw, dict) else {}


def _paths_config() -> dict[str, Any]:
    paths = _feature_config().get("paths")
    return paths if isinstance(paths, dict) else {}


def _link_index_config() -> dict[str, Any]:
    raw = _feature_config().get("link_index")
    return raw if isinstance(raw, dict) else {}


def _path_from_config(key: str, fallback: Path | str) -> Path:
    raw = _str_or_blank(_paths_config().get(key))
    return Path(raw or fallback).expanduser()


def media_root() -> Path:
    return _path_from_config("media_root", _DEFAULT_MEDIA_ROOT).resolve()


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
    return [media_root()]


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


def _layout_levels() -> tuple[str, ...]:
    raw = _link_index_config().get("layout_levels")
    if isinstance(raw, list):
        out = tuple(_str_or_blank(x) for x in raw if _str_or_blank(x))
        if out:
            return out
    return _DEFAULT_LEVELS


def _shortcut_name_template() -> str:
    return _str_or_blank(_link_index_config().get("shortcut_name")) or _DEFAULT_SHORTCUT_NAME


def _overwrite_shortcuts_default() -> bool:
    raw = _link_index_config().get("overwrite_shortcuts")
    return bool(raw) if isinstance(raw, bool) else True


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


def collection_link_index_config_json() -> dict[str, Any]:
    roots = resource_roots()
    return {
        "media_root": str(media_root()),
        "resource_roots": [str(p) for p in roots],
        "resource_excludes": {str(p): resource_excludes_for_root(p) for p in roots},
        "shortcut_root": str(shortcut_root()),
        "layout_levels": list(_layout_levels()),
        "shortcut_name": _shortcut_name_template(),
        "overwrite_shortcuts": _overwrite_shortcuts_default(),
        "resource_scan_max_dirs": _resource_scan_max_dirs(),
        "storage": "catalog_yaml",
        "path_field": "attributes/data/path",
        "press_path_field": "attributes/data/collectioned/*/press_path",
    }


def _association_rejects_path() -> Path:
    return (feature_data_root("collection-detail") / "db" / _ASSOCIATION_REJECTS_FILENAME).resolve()


def _resource_scan_cache_path() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _RESOURCE_SCAN_CACHE_FILENAME).resolve()


def _resource_scan_legacy_json_path() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _RESOURCE_SCAN_LEGACY_JSON_FILENAME).resolve()


def _resource_scan_node_dir() -> Path:
    return (feature_data_root("collection-detail") / "cache" / _RESOURCE_SCAN_NODE_DIRNAME).resolve()


def _resource_scan_node_path(relpath: str) -> Path:
    digest = hashlib.sha1(str(relpath or "").encode("utf-8")).hexdigest()
    return (_resource_scan_node_dir() / digest[:2] / f"{digest[2:]}.yaml").resolve()


def _load_association_rejects() -> dict[str, Any]:
    path = _association_rejects_path()
    if not path.is_file():
        return {"version": 1, "items": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        items = raw.get("items")
        if isinstance(items, dict):
            return {"version": 1, "items": {str(k): v for k, v in items.items() if str(k)}}
        if isinstance(items, list):
            return {"version": 1, "items": {str(x): {"key": str(x)} for x in items if str(x)}}
    if isinstance(raw, list):
        return {"version": 1, "items": {str(x): {"key": str(x)} for x in raw if str(x)}}
    return {"version": 1, "items": {}}


def _save_association_rejects(store: dict[str, Any]) -> None:
    path = _association_rejects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    items = store.get("items")
    clean_items = items if isinstance(items, dict) else {}
    payload = {"version": 1, "items": clean_items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _empty_resource_tree() -> dict[str, Any]:
    return {"type": "folder", "name": "资源库", "relpath": "", "path": "", "children": []}


def _empty_resource_tree() -> dict[str, Any]:
    return {"type": "folder", "name": "资源库", "relpath": "", "path": "", "children": [], "children_loaded": True}


def _resource_scan_cache_empty() -> dict[str, Any]:
    return {
        "ok": True,
        "cached": False,
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
    return {"ok": True, "relpath": str(out.get("relpath") or ""), "node": out}


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
    path.write_text(dump_yaml_string(_resource_node_cache_payload(node)), encoding="utf-8")
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
    raw["ok"] = True
    raw["cached"] = True
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
        raise FileNotFoundError(rel or "资源库根目录")
    raw = load_yaml(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("node"), dict):
        raise ValueError("资源库目录缓存损坏，请重新扫描。")
    node = raw["node"]
    if str(node.get("relpath") or "") != rel:
        raise ValueError("资源库目录缓存索引不一致，请重新扫描。")
    return {"ok": True, "cached": True, "relpath": rel, "node": node}


def _association_reject_keys() -> set[str]:
    return set((_load_association_rejects().get("items") or {}).keys())


def _association_reject_identity(item: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "shortcut_relpath": _str_or_blank(item.get("shortcut_relpath") or item.get("relpath")).replace("\\", "/"),
        "shortcut_path": _str_or_blank(item.get("shortcut_path") or item.get("path")),
        "link_name": _without_lnk_suffix(item.get("name") or ""),
        "target_path": _str_or_blank(cand.get("suggested_target_path") or item.get("target_path")),
        "yaml_source_rel": _str_or_blank(cand.get("yaml_source_rel")).replace("\\", "/"),
        "index_in_file": str(cand.get("index_in_file") if cand.get("index_in_file") is not None else ""),
        "name": _str_or_blank(cand.get("name")),
        "begin_date": _str_or_blank(cand.get("begin_date")),
        "end_date": _str_or_blank(cand.get("end_date")),
        "press_key": _str_or_blank(cand.get("press_key")),
        "press_format": _str_or_blank(cand.get("press_format")),
        "press_group": _str_or_blank(cand.get("press_group")),
        "suggested_path": _str_or_blank(cand.get("suggested_path")).replace("\\", "/"),
        "suggested_press_path": _str_or_blank(cand.get("suggested_press_path")).replace("\\", "/"),
    }


def _association_reject_key(item: dict[str, Any], cand: dict[str, Any]) -> str:
    raw = json.dumps(_association_reject_identity(item, cand), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _yaml_roundtrip() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.allow_unicode = True
    y.width = 10_000_000
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def _normal_resource_root_strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("roots 必须为数组")
    out: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        value = _str_or_blank(item)
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
        out.append(str(path))
    return out


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


def _resource_dir_node(
    root: Path,
    path: Path,
    relpath: str,
    excludes: list[str],
    shortcut_root_key: str,
    counters: dict[str, Any],
    max_dirs: int,
    root_name: str | None = None,
) -> dict[str, Any]:
    try:
        dir_stat = path.stat()
        dir_mtime = int(dir_stat.st_mtime)
    except OSError:
        dir_mtime = 0
    node: dict[str, Any] = {
        "type": "folder",
        "name": root_name or path.name,
        "relpath": relpath,
        "path": str(path),
        "exists": path.is_dir(),
        "error": "",
        "children": [],
        "files": [],
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
            child = _resource_dir_node(root, entry, child_rel, excludes, shortcut_root_key, counters, max_dirs)
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


def _resource_tree_from_roots(roots_out: list[dict[str, Any]]) -> dict[str, Any]:
    tree = _empty_resource_tree()
    root_children: list[dict[str, Any]] = []
    for root_idx, root in enumerate(roots_out):
        root_path = str(root.get("root") or "")
        root_rel = f"root:{root_idx}"
        root_node: dict[str, Any] = {
            "type": "folder",
            "name": root_path or f"resource-root-{root_idx + 1}",
            "relpath": root_rel,
            "path": root_path,
            "series_count": int(root.get("series_count") or 0),
            "item_count": int(root.get("item_count") or 0),
            "exists": bool(root.get("exists")),
            "error": str(root.get("error") or ""),
            "children": [],
        }
        series_rows = root.get("series") if isinstance(root.get("series"), list) else []
        for series in series_rows:
            if not isinstance(series, dict):
                continue
            series_name = str(series.get("name") or "")
            series_rel = f"{root_rel}/{series.get('relpath') or series_name}"
            child_rows = series.get("children") if isinstance(series.get("children"), list) else []
            series_node: dict[str, Any] = {
                "type": "folder",
                "name": series_name,
                "relpath": series_rel,
                "path": str(series.get("path") or ""),
                "series_count": 0,
                "item_count": len(child_rows),
                "exists": True,
                "error": str(series.get("error") or ""),
                "children": [],
            }
            for child in child_rows:
                if not isinstance(child, dict):
                    continue
                child_name = str(child.get("name") or "")
                series_node["children"].append(
                    {
                        "type": "resource",
                        "name": child_name,
                        "relpath": f"{root_rel}/{child.get('relpath') or (series_name + '/' + child_name)}",
                        "path": str(child.get("path") or ""),
                        "series_name": str(child.get("series_name") or series_name),
                        "work_name": str(child.get("work_name") or ""),
                        "press_info": str(child.get("press_info") or ""),
                        "children": [],
                    }
                )
            root_node["children"].append(series_node)
        root_children.append(root_node)
    tree["children"] = root_children
    return tree


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
        root_node = _resource_dir_node(root, root, root_rel, root_excludes, shortcut_root_key, counters, max_dirs, str(root))
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
                if p.is_file() and not p.name.startswith(".")
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


def _split_target_path_for_ui_mapping(raw: Any, *, label: str = "target_path") -> tuple[str, str]:
    target_s = _str_or_blank(raw)
    if not target_s:
        return "", ""
    if not (re.match(r"^[A-Za-z]:[\\/]", target_s) or target_s.startswith(("/", "\\"))):
        raise ValueError(f"{label} 必须为实际绝对路径")
    target = Path(target_s).expanduser()
    try:
        target = target.resolve()
    except OSError:
        pass
    leaf = target.name
    if not leaf:
        raise ValueError(f"{label} 必须指向具体资源目录")
    return _clean_work_path(str(target.parent), label=f"{label}.path"), _clean_rel_path(
        leaf,
        label=f"{label}.press_path",
    )


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


def _relative_dirs(root: Path) -> list[str]:
    root = root.resolve()
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    max_depth = _scan_max_depth()
    max_dirs = _scan_max_dirs()
    out: list[str] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(out) < max_dirs:
        cur, depth = stack.pop()
        if depth > 0:
            try:
                out.append(cur.relative_to(root).as_posix())
            except ValueError:
                continue
        if depth >= max_depth:
            continue
        try:
            children = sorted([p for p in cur.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in reversed(children):
            if child.name.startswith("."):
                continue
            stack.append((child, depth + 1))
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


def _shortcut_for(shortcut_root_p: Path, levels: tuple[str, ...], name_tpl: str, ctx: dict[str, str]) -> Path:
    parts = [_template_text(level, ctx) for level in levels]
    filename = _template_text(name_tpl, ctx)
    if not filename.lower().endswith(".lnk"):
        filename += ".lnk"
    return shortcut_root_p.joinpath(*parts, filename).resolve()


def _work_assoc(work: dict[str, Any], press: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "work_key": work.get("work_key") or "",
        "yaml_source_rel": work.get("yaml_source_rel") or "",
        "index_in_file": work.get("index_in_file"),
        "name": work.get("name") or "",
        "year": work.get("year") or "",
    }
    if press is not None:
        out.update(
            {
                "press_key": press.get("press_key") or "",
                "press_label": press.get("label") or "",
            }
        )
    return out


def _build_plan_from_works(works: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mr = media_root()
    sr = shortcut_root()
    levels = _layout_levels()
    name_tpl = _shortcut_name_template()
    plan: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for work in works:
        work_path_s = _str_or_blank(work.get("path"))
        if not work_path_s:
            continue
        for press in work.get("press", []):
            if not isinstance(press, dict):
                continue
            press_path_s = _str_or_blank(press.get(TV_JP_PRESS_PATH_KEY))
            if not press_path_s:
                continue
            try:
                target = _target_for(mr, work_path_s, press_path_s)
                shortcut = _shortcut_for(sr, levels, name_tpl, _plan_context(work, press))
                shortcut.relative_to(sr)
                shortcut_s = str(shortcut)
                target_s = str(target)
                status = "ready"
                if not target.exists():
                    status = "missing_target"
                prev = seen.get(shortcut_s.lower())
                if prev is not None and prev != target_s:
                    status = "duplicate_shortcut"
                seen.setdefault(shortcut_s.lower(), target_s)
                shortcut_rel = shortcut.relative_to(sr).as_posix()
                plan.append(
                    {
                        "status": status,
                        "work_key": work["work_key"],
                        "yaml_source_rel": work.get("yaml_source_rel") or "",
                        "index_in_file": work.get("index_in_file"),
                        "name": work.get("name") or "",
                        "year": work.get("year") or "",
                        "begin_date": work.get("begin_date") or "",
                        "end_date": work.get("end_date") or "",
                        "date_range_label": work.get("date_range_label") or "",
                        "press_key": press.get("press_key") or "",
                        "press_label": press.get("label") or "",
                        "press_format": press.get(TV_JP_PRESS_FORMAT_KEY) or "",
                        "press_group": press.get(TV_JP_PRESS_GROUP_KEY) or "",
                        "press_path": press_path_s,
                        "work_path": work_path_s,
                        "target_path": target_s,
                        "target_exists": target.exists(),
                        "shortcut_path": shortcut_s,
                        "shortcut_relpath": shortcut_rel,
                        "shortcut_parts": shortcut_rel.split("/"),
                        "shortcut_exists": shortcut.exists(),
                    },
                )
            except (OSError, ValueError) as exc:
                plan.append(
                    {
                        "status": "invalid_path",
                        "work_key": work.get("work_key") or "",
                        "yaml_source_rel": work.get("yaml_source_rel") or "",
                        "index_in_file": work.get("index_in_file"),
                        "name": work.get("name") or "",
                        "year": work.get("year") or "",
                        "begin_date": work.get("begin_date") or "",
                        "end_date": work.get("end_date") or "",
                        "date_range_label": work.get("date_range_label") or "",
                        "press_key": press.get("press_key") or "",
                        "press_label": press.get("label") or "",
                        "press_format": press.get(TV_JP_PRESS_FORMAT_KEY) or "",
                        "press_group": press.get(TV_JP_PRESS_GROUP_KEY) or "",
                        "press_path": press_path_s,
                        "work_path": work_path_s,
                        "error": str(exc),
                    },
                )
    return plan


def _path_compare_key(raw: Any) -> str:
    s = str(raw) if isinstance(raw, Path) else _str_or_blank(raw)
    if not s:
        return ""
    try:
        return os.path.normcase(str(Path(s).expanduser().resolve()))
    except OSError:
        return os.path.normcase(str(Path(s).expanduser()))


def _annotate_plan_shortcut_targets(plan: list[dict[str, Any]], disk_leaves: list[dict[str, Any]]) -> None:
    disk_by_shortcut: dict[str, dict[str, Any]] = {}
    disk_by_target: dict[str, dict[str, Any]] = {}
    for disk_item in disk_leaves:
        shortcut_s = _str_or_blank(disk_item.get("shortcut_path"))
        if shortcut_s:
            disk_by_shortcut[_path_compare_key(shortcut_s)] = disk_item
        target_s = _str_or_blank(disk_item.get("target_path"))
        if bool(disk_item.get("target_exists")) and target_s:
            disk_by_target.setdefault(_path_compare_key(target_s), disk_item)
    for item in plan:
        shortcut_s = _str_or_blank(item.get("shortcut_path"))
        target_key = _path_compare_key(item.get("target_path"))
        disk_item: dict[str, Any] = {}
        if not shortcut_s:
            disk_item = disk_by_target.get(target_key) or {}
        else:
            disk_item = disk_by_shortcut.get(_path_compare_key(shortcut_s)) or disk_by_target.get(target_key) or {}
        actual_s = _str_or_blank(disk_item.get("target_path"))
        item["matched_shortcut_path"] = _str_or_blank(disk_item.get("shortcut_path"))
        item["matched_shortcut_relpath"] = _str_or_blank(disk_item.get("shortcut_relpath"))
        item["shortcut_target_path"] = actual_s
        item["shortcut_target_exists"] = bool(disk_item.get("target_exists"))
        item["db_linked"] = bool(
            item.get("shortcut_target_exists")
            and actual_s
            and _path_compare_key(actual_s) == _path_compare_key(item.get("target_path"))
        )
        item["link_exists"] = bool(item.get("shortcut_target_exists"))
        if item["db_linked"] and item["matched_shortcut_path"]:
            planned_shortcut = Path(str(item.get("shortcut_path") or ""))
            matched_shortcut = Path(str(item["matched_shortcut_path"]))
            if planned_shortcut.name:
                try:
                    normalized_shortcut = (matched_shortcut.parent / planned_shortcut.name).resolve()
                    normalized_shortcut.relative_to(shortcut_root())
                    item["configured_shortcut_path"] = str(planned_shortcut)
                    item["configured_shortcut_relpath"] = str(item.get("shortcut_relpath") or "")
                    item["shortcut_path"] = str(normalized_shortcut)
                    normalized_rel = normalized_shortcut.relative_to(shortcut_root()).as_posix()
                    item["shortcut_relpath"] = normalized_rel
                    item["shortcut_parts"] = normalized_rel.split("/")
                    item["shortcut_exists"] = normalized_shortcut.exists()
                except (OSError, ValueError):
                    pass


def _planned_relpath_keys(plan: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("shortcut_relpath") or "").lower()
        for item in plan
        if item.get("shortcut_relpath")
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
    rel = str(item.get("shortcut_relpath") or "").lower()
    if rel and rel in planned_relpaths:
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
        "created": 0,
        "renamed": 0,
        "skipped": 0,
        "failed": 0,
    }
    for item in plan:
        status = str(item.get("status") or "")
        if status in out:
            out[status] += 1
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


def _scan_shortcut_leaves(*, refresh_targets: bool = False) -> list[dict[str, Any]]:
    root = shortcut_root()
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    max_dirs = _scan_max_dirs()
    max_depth = max(_scan_max_depth(), len(_layout_levels()) + 2)
    leaves: list[dict[str, Any]] = []
    shortcut_paths: list[Path] = []
    signature_parts: list[tuple[str, int, int]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    seen_dirs = 0
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
                    rel = shortcut_abs.relative_to(root.resolve()).as_posix()
                    st = shortcut_abs.stat()
                except ValueError:
                    continue
                except OSError:
                    continue
                shortcut_paths.append(shortcut_abs)
                signature_parts.append((rel, int(st.st_mtime_ns), int(st.st_size)))
                leaves.append(
                    {
                        "type": "disk_link",
                        "name": child.name,
                        "relpath": rel,
                        "path": str(shortcut_abs),
                        "shortcut_path": str(shortcut_abs),
                        "shortcut_relpath": rel,
                        "shortcut_parts": rel.split("/"),
                        "shortcut_exists": True,
                    }
                )
    signature = (str(root.resolve()), tuple(sorted(signature_parts)))
    if not refresh_targets and _SHORTCUT_SCAN_CACHE.get("signature") == signature:
        return _copy_shortcut_leaves(cast(list[dict[str, Any]], _SHORTCUT_SCAN_CACHE.get("leaves") or []))
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
    sr = shortcut_root()
    root: dict[str, Any] = {
        "type": "root",
        "name": sr.name or str(sr),
        "relpath": "",
        "path": str(sr),
        "associated": [],
        "children": [],
    }
    planned_relpaths = _planned_relpath_keys(plan)
    planned_targets = _planned_target_keys(plan)
    for item in plan:
        parts = item.get("shortcut_parts")
        if not isinstance(parts, list) or not parts:
            continue
        assoc = {
            "work_key": item.get("work_key") or "",
            "yaml_source_rel": item.get("yaml_source_rel") or "",
            "index_in_file": item.get("index_in_file"),
            "name": item.get("name") or "",
            "year": item.get("year") or "",
            "press_key": item.get("press_key") or "",
            "press_label": item.get("press_label") or "",
        }
        _append_assoc(root, assoc)
        cur = root
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
            "associated": [assoc],
            "children": [],
        }
        if isinstance(item.get("target_fix"), dict):
            link_node["target_fix"] = dict(cast(dict[str, Any], item["target_fix"]))
        if not link_node["shortcut_exists"]:
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
        cur = root
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
            "yaml_source_rel",
            "index_in_file",
        ):
            if key in node:
                out[key] = node[key]
        return out
    if "path" in node:
        out["path"] = str(node.get("path") or "")
    return out


def _without_lnk_suffix(raw: Any) -> str:
    return re.sub(r"\.lnk$", "", str(raw or "").strip(), flags=re.IGNORECASE)


def _strip_leading_date_prefixes(raw: str) -> str:
    s = str(raw or "").strip()
    while True:
        next_s = re.sub(r"^\s*[\[\(【]\s*((?:19|20)\d{2}(?:\d{4})?)\s*[\]\)】]\s*", "", s)
        if next_s != s:
            s = next_s
            continue
        next_s = re.sub(r"^\s*((?:19|20)\d{6})[\s._-]+", "", s)
        if next_s == s:
            return s.strip()
        s = next_s


def _name_match_key(raw: Any) -> str:
    s = _strip_leading_date_prefixes(_without_lnk_suffix(raw))
    s = unicodedata.normalize("NFKC", s)
    chars = [ch.casefold() if ch.isalnum() else " " for ch in s]
    return " ".join("".join(chars).split())


def _match_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = difflib.SequenceMatcher(a=left, b=right).ratio()
    lt = set(left.split())
    rt = set(right.split())
    overlap = len(lt & rt)
    token_score = (2 * overlap / (len(lt) + len(rt))) if lt and rt else 0.0
    contains_score = 0.0
    shorter = left if len(left) < len(right) else right
    longer = right if shorter == left else left
    if len(shorter) >= 4 and shorter in longer:
        contains_score = min(0.9, 0.62 + len(shorter) / max(len(longer), 1) * 0.25)
    return max(ratio, token_score, contains_score)


def _match_tokens(key: str) -> set[str]:
    out: set[str] = set()
    for token in str(key or "").split():
        if not token:
            continue
        if token.isascii() and len(token) <= 1:
            continue
        out.add(token)
    return out


def _association_match_context(works: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    by_exact: dict[str, list[dict[str, Any]]] = {}
    by_strict_exact: dict[str, list[dict[str, Any]]] = {}
    by_token: dict[str, list[dict[str, Any]]] = {}
    by_year: dict[str, list[dict[str, Any]]] = {}
    for work in works:
        name_key = _name_match_key(work.get("name") or "")
        strict_name_key = _strict_name_key(work.get("name") or "")
        if not name_key:
            continue
        record = {
            "work": work,
            "work_key": work.get("work_key") or "",
            "name_key": name_key,
            "strict_name_key": strict_name_key,
            "tokens": _match_tokens(name_key),
            "year": str(work.get("year") or ""),
        }
        records.append(record)
        by_exact.setdefault(name_key, []).append(record)
        if strict_name_key:
            by_strict_exact.setdefault(strict_name_key, []).append(record)
        if record["year"]:
            by_year.setdefault(str(record["year"]), []).append(record)
        for token in record["tokens"]:
            by_token.setdefault(str(token), []).append(record)
    return {
        "records": records,
        "by_exact": by_exact,
        "by_strict_exact": by_strict_exact,
        "by_token": by_token,
        "by_year": by_year,
    }


def _shortcut_year_hint(item: dict[str, Any]) -> str:
    hay = " ".join(str(x or "") for x in item.get("shortcut_parts", []) if isinstance(x, str))
    m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:\d{4})?(?:[^\d]|$)", hay)
    return m.group(1) if m else ""


def _shortcut_date_range_hint(item: dict[str, Any]) -> tuple[str, str]:
    parts = item.get("shortcut_parts")
    hay_parts = [str(x or "") for x in parts if isinstance(x, str)] if isinstance(parts, list) else []
    hay_parts.append(str(item.get("shortcut_relpath") or item.get("relpath") or ""))
    for hay in hay_parts:
        dates = re.findall(r"[\[\(【]\s*((?:19|20)\d{6})\s*[\]\)】]", hay)
        if dates:
            return dates[0], dates[1] if len(dates) > 1 else dates[0]
    return "", ""


def _date_year(raw: Any) -> str:
    s = str(raw or "").strip()
    return s[:4] if re.match(r"^(?:19|20)\d{2}", s) else ""


def _shortcut_work_dates_compatible(item: dict[str, Any], work: dict[str, Any]) -> bool:
    shortcut_begin, shortcut_end = _shortcut_date_range_hint(item)
    work_begin = str(work.get("begin_date") or "")
    work_end = str(work.get("end_date") or "")
    shortcut_begin_year = _date_year(shortcut_begin)
    shortcut_end_year = _date_year(shortcut_end)
    work_begin_year = _date_year(work_begin)
    work_end_year = _date_year(work_end)
    if shortcut_begin_year and work_begin_year and shortcut_begin_year != work_begin_year:
        return False
    if shortcut_end_year and work_end_year and shortcut_end_year != work_end_year:
        return False
    return True


def _shortcut_work_name_hint(item: dict[str, Any]) -> str:
    parts = item.get("shortcut_parts")
    if isinstance(parts, list) and len(parts) >= 2:
        return _strip_leading_date_prefixes(str(parts[-2] or ""))
    return _strip_leading_date_prefixes(_without_lnk_suffix(item.get("name") or ""))


def _target_rel_under_media_root(target: Path | None) -> str:
    if target is None:
        return ""
    try:
        return target.resolve().relative_to(media_root().resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _shortcut_target_info(item: dict[str, Any], *, resolve_target: bool = False) -> dict[str, Any]:
    raw = str(item.get("shortcut_path") or item.get("path") or "")
    if not raw:
        return {
            "target_path": "",
            "target_exists": False,
            "target_relpath": "",
            "target_under_media_root": False,
            "target_resolved": False,
            "error": "shortcut_path is empty",
        }
    existing_target_s = _str_or_blank(item.get("target_path"))
    if existing_target_s:
        try:
            existing_target = Path(existing_target_s).expanduser().resolve()
        except OSError:
            existing_target = Path(existing_target_s).expanduser()
        target_rel = _target_rel_under_media_root(existing_target)
        return {
            "target_path": str(existing_target),
            "target_exists": bool(item.get("target_exists")),
            "target_relpath": target_rel,
            "target_under_media_root": bool(target_rel),
            "target_resolved": True,
            "error": _str_or_blank(item.get("target_error")),
        }
    if not resolve_target:
        return {
            "target_path": "",
            "target_exists": None,
            "target_relpath": "",
            "target_under_media_root": False,
            "target_resolved": False,
            "error": "",
        }
    shortcut = Path(raw).expanduser().resolve()
    target_s = ""
    error = ""
    try:
        target_s = _windows_shortcut_target(shortcut)
    except (OSError, subprocess.SubprocessError) as exc:
        error = str(exc)
    target = Path(target_s).expanduser().resolve() if target_s else None
    target_rel = _target_rel_under_media_root(target)
    return {
        "target_path": str(target) if target is not None else "",
        "target_exists": bool(target and target.exists()),
        "target_relpath": target_rel,
        "target_under_media_root": bool(target_rel),
        "target_resolved": True,
        "error": error if not target_s else "",
    }


def _target_name_hints(item: dict[str, Any], target_info: dict[str, Any]) -> list[str]:
    target_relpath = str(target_info.get("target_relpath") or "")
    target_base, _target_leaf, target_suffix = _target_leaf_work_base(item, None, target_info)
    hints = []
    if target_base and target_suffix:
        hints.append(target_base)
    hints.append(_shortcut_work_name_hint(item))
    if target_relpath:
        parts = [part for part in target_relpath.split("/") if part]
        if len(parts) >= 2:
            hints.append(parts[-2])
        elif parts:
            hints.append(parts[-1])
    out: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        h = _strip_leading_date_prefixes(hint)
        k = _strict_name_key(h)
        if h and k not in seen:
            seen.add(k)
            out.append(h)
    return out


def _suggest_paths_for_candidate(
    item: dict[str, Any],
    work: dict[str, Any],
    target_info: dict[str, Any],
) -> tuple[str, str, str]:
    target_rel = str(target_info.get("target_relpath") or "").strip("/")
    existing_work_path = _str_or_blank(work.get("path")).replace("\\", "/").strip("/")
    target_abs = _str_or_blank(target_info.get("target_path"))
    if target_rel:
        if existing_work_path and (
            target_rel == existing_work_path or target_rel.startswith(existing_work_path + "/")
        ):
            press_path = target_rel[len(existing_work_path) :].strip("/")
            if press_path:
                return existing_work_path, press_path, "target_existing_work_path"
        parts = [part for part in target_rel.split("/") if part]
        if len(parts) >= 2:
            return "/".join(parts[:-1]), parts[-1], "target_relpath"
        if parts:
            return parts[0], _without_lnk_suffix(item.get("name")), "target_relpath_single"
    if target_abs:
        try:
            target_path = Path(target_abs).expanduser().resolve()
        except OSError:
            target_path = Path(target_abs).expanduser()
        if target_path.name:
            return str(target_path.parent), target_path.name, "target_absolute_path"
    fallback_work_path = existing_work_path or _shortcut_work_name_hint(item)
    fallback_press_path = _without_lnk_suffix(item.get("name"))
    return fallback_work_path, fallback_press_path, "shortcut_name"


def _suggested_target_path_for_candidate(
    work_path: str,
    press_path: str,
    target_info: dict[str, Any],
) -> str:
    target_abs = _str_or_blank(target_info.get("target_path"))
    if target_abs:
        return target_abs
    if not work_path or not press_path:
        return ""
    try:
        return str(_target_for(media_root(), work_path, press_path))
    except (OSError, ValueError):
        return ""


def _strict_name_key(raw: Any) -> str:
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    return re.sub(r"\s+", " ", s)


def _target_leaf_work_base(
    item: dict[str, Any],
    press: dict[str, Any] | None,
    target_info: dict[str, Any],
) -> tuple[str, str, str]:
    target_s = _str_or_blank(target_info.get("target_path"))
    if not target_s:
        return "", "", ""
    try:
        leaf = Path(target_s).expanduser().name
    except OSError:
        leaf = Path(target_s).name
    if not leaf:
        return "", "", ""
    suffixes = [
        _without_lnk_suffix(item.get("name")),
    ]
    if isinstance(press, dict):
        fmt = _str_or_blank(press.get(TV_JP_PRESS_FORMAT_KEY))
        grp = _str_or_blank(press.get(TV_JP_PRESS_GROUP_KEY))
        label = _str_or_blank(press.get("label"))
        suffixes.extend([fmt, label])
        if fmt and grp:
            suffixes.append(f"{fmt}-{grp}")
    seen: set[str] = set()
    for suffix in sorted((x for x in suffixes if x), key=len, reverse=True):
        suffix_key = _press_component_key(suffix)
        if suffix_key in seen:
            continue
        seen.add(suffix_key)
        marker = "_" + suffix
        marker_key = _press_component_key(marker)
        if _press_component_key(leaf).endswith(marker_key):
            return leaf[: -len(marker)], leaf, suffix
    return leaf, leaf, ""


def _target_leaf_matches_shortcut_work(
    item: dict[str, Any],
    press: dict[str, Any] | None,
    target_info: dict[str, Any],
) -> tuple[bool, str, str, str]:
    work_name = _shortcut_work_name_hint(item)
    base, leaf, suffix = _target_leaf_work_base(item, press, target_info)
    return (
        bool(work_name and base and _strict_name_key(work_name) == _strict_name_key(base)),
        work_name,
        base,
        suffix or leaf,
    )


def _press_component_key(raw: Any) -> str:
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s


def _resource_fix_items_from_cache() -> list[dict[str, Any]]:
    try:
        payload = _load_resource_scan_cache()
    except (OSError, ValueError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _shortcut_press_match_keys(item: dict[str, Any]) -> set[str]:
    values: list[str] = []
    parts = item.get("shortcut_parts")
    if isinstance(parts, list) and parts:
        values.append(_without_lnk_suffix(parts[-1]))
    values.append(_without_lnk_suffix(item.get("name") or ""))
    keys: set[str] = set()
    for value in values:
        raw = _without_lnk_suffix(value)
        if not raw:
            continue
        variants = [raw]
        m = re.match(r"^(.+?)\((.+?)\)$", raw)
        if m:
            left = m.group(1).strip()
            right = m.group(2).strip()
            variants.extend([left, f"{left}-{right}", f"{left}_{right}", f"{left} {right}", f"{left}/{right}"])
        for variant in variants:
            keys.add(_press_component_key(variant))
        leaf = Path(raw.replace("\\", "/")).name
        if leaf:
            keys.add(_press_component_key(leaf))
    return {key for key in keys if key}


def _shortcut_work_match_keys(item: dict[str, Any]) -> set[str]:
    values = [_shortcut_work_name_hint(item)]
    target_s = _str_or_blank(item.get("target_path"))
    if target_s:
        try:
            target = Path(target_s).expanduser()
            values.append(target.parent.name)
        except OSError:
            pass
    keys = {_strict_name_key(value) for value in values if _strict_name_key(value)}
    return {key for key in keys if key}


def _actual_shortcut_item_for_fix(item: dict[str, Any]) -> dict[str, Any] | None:
    shortcut_s = _str_or_blank(item.get("matched_shortcut_path") or item.get("shortcut_path") or item.get("path"))
    if not shortcut_s:
        return None
    actual_exists = item.get("shortcut_target_exists") if "shortcut_target_exists" in item else item.get("target_exists")
    if actual_exists is True:
        return None
    if not bool(item.get("shortcut_exists") or item.get("matched_shortcut_path")):
        return None
    try:
        shortcut = Path(shortcut_s).expanduser().resolve()
    except OSError:
        shortcut = Path(shortcut_s).expanduser()
    parts = item.get("shortcut_parts")
    if not isinstance(parts, list) or not parts:
        try:
            parts = shortcut.relative_to(shortcut_root()).as_posix().split("/")
        except (OSError, ValueError):
            parts = [shortcut.name]
    out = dict(item)
    out["shortcut_path"] = str(shortcut)
    out["path"] = str(shortcut)
    out["name"] = shortcut.name
    out["shortcut_parts"] = parts
    out["target_path"] = _str_or_blank(item.get("shortcut_target_path") or item.get("target_path"))
    out["target_exists"] = bool(actual_exists)
    return out


def _resource_fix_candidate_for_shortcut_item(
    item: dict[str, Any],
    resource_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    shortcut_item = _actual_shortcut_item_for_fix(item)
    if shortcut_item is None:
        return None
    work_keys = _shortcut_work_match_keys(shortcut_item)
    press_keys = _shortcut_press_match_keys(shortcut_item)
    if not work_keys or not press_keys:
        return None
    candidates: list[dict[str, Any]] = []
    for resource in resource_items:
        resource_work_key = _strict_name_key(resource.get("work_name") or "")
        if resource_work_key not in work_keys:
            continue
        resource_press_key = _press_component_key(resource.get("press_info") or "")
        if resource_press_key not in press_keys:
            continue
        path_s = _str_or_blank(resource.get("path"))
        if not path_s:
            continue
        try:
            resource_path = Path(path_s).expanduser().resolve()
        except OSError:
            resource_path = Path(path_s).expanduser()
        try:
            resource_exists = resource_path.is_dir()
        except OSError:
            resource_exists = False
        if not resource_exists:
            continue
        series_key = _strict_name_key(resource.get("series_name") or "")
        score = 100 + (10 if series_key in work_keys else 0)
        candidates.append(
            {
                "target_path": str(resource_path),
                "suggested_path": str(resource_path.parent),
                "suggested_press_path": resource_path.name,
                "resource_root": resource.get("root") or "",
                "resource_relpath": resource.get("relpath") or "",
                "series_name": resource.get("series_name") or "",
                "resource_name": resource.get("name") or "",
                "work_name": resource.get("work_name") or "",
                "press_info": resource.get("press_info") or "",
                "score": score,
                "reason": "资源库目录中找到同作品名、同压制信息的真实目录",
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: (
            -int(c.get("score") or 0),
            str(c.get("target_path") or "").casefold(),
        )
    )
    best_score = int(candidates[0].get("score") or 0)
    top = [cand for cand in candidates if int(cand.get("score") or 0) == best_score]
    if len(top) != 1:
        return None
    result = dict(top[0])
    result["candidate_count"] = len(candidates)
    return result


def _annotate_plan_resource_fixes(plan: list[dict[str, Any]]) -> None:
    resource_items = _resource_fix_items_from_cache()
    if not resource_items:
        return
    for item in plan:
        fix = _resource_fix_candidate_for_shortcut_item(item, resource_items)
        if fix:
            item["target_fix"] = fix


def _annotate_shortcut_resource_fixes(items: list[dict[str, Any]]) -> None:
    resource_items = _resource_fix_items_from_cache()
    if not resource_items:
        return
    for item in items:
        fix = _resource_fix_candidate_for_shortcut_item(item, resource_items)
        if fix:
            item["target_fix"] = fix


def _link_press_hint(item: dict[str, Any], work: dict[str, Any]) -> tuple[str, str]:
    leaf = _press_component_key(_without_lnk_suffix(item.get("name") or ""))
    if not leaf:
        return "", ""
    format_keys = [
        _press_component_key(press.get(TV_JP_PRESS_FORMAT_KEY))
        for press in work.get("press", [])
        if isinstance(press, dict)
    ]
    for fmt in sorted({x for x in format_keys if x}, key=len, reverse=True):
        if leaf == fmt:
            return fmt, ""
        m = re.match(r"^(.+?)\((.+?)\)$", leaf)
        if m and m.group(1).strip() == fmt:
            return fmt, m.group(2).strip()
        for sep in ("-", "_", " ", "/", "／"):
            prefix = fmt + sep
            if leaf.startswith(prefix):
                return fmt, leaf[len(prefix) :].strip()
    parts = re.split(r"[\s_-]+", leaf, maxsplit=1)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""


def _press_score(leaf_name: str, press: dict[str, Any]) -> float:
    needle = _name_match_key(leaf_name)
    if not needle:
        return 0.0
    values = [
        press.get("label") or "",
        press.get(TV_JP_PRESS_FORMAT_KEY) or "",
        press.get(TV_JP_PRESS_GROUP_KEY) or "",
        f"{press.get(TV_JP_PRESS_FORMAT_KEY) or ''}-{press.get(TV_JP_PRESS_GROUP_KEY) or ''}",
    ]
    return max(_match_similarity(needle, _name_match_key(v)) for v in values)


def _best_press_for_link(item: dict[str, Any], work: dict[str, Any]) -> dict[str, Any] | None:
    press_rows = [p for p in work.get("press", []) if isinstance(p, dict)]
    if not press_rows:
        return None
    link_format, link_group = _link_press_hint(item, work)
    if not link_format:
        return None
    format_matches = [
        press
        for press in press_rows
        if _press_component_key(press.get(TV_JP_PRESS_FORMAT_KEY)) == link_format
    ]
    if len(format_matches) == 1:
        return format_matches[0]
    if len(format_matches) <= 1 or not link_group:
        return None
    group_matches = [
        press
        for press in format_matches
        if _press_component_key(press.get(TV_JP_PRESS_GROUP_KEY)) == link_group
    ]
    return group_matches[0] if len(group_matches) == 1 else None


def _press_options_for_work(work: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for press in work.get("press", []):
        if not isinstance(press, dict):
            continue
        out.append(
            {
                "press_key": press.get("press_key") or "",
                "press_label": press.get("label") or "",
                "press_format": press.get(TV_JP_PRESS_FORMAT_KEY) or "",
                "press_group": press.get(TV_JP_PRESS_GROUP_KEY) or "",
                "press_path": press.get(TV_JP_PRESS_PATH_KEY) or "",
            }
        )
    return out


def _association_candidate(
    item: dict[str, Any],
    work: dict[str, Any],
    target_info: dict[str, Any],
    *,
    score: float,
    exact: bool,
    press_override: dict[str, Any] | None = None,
    match_type: str | None = None,
) -> dict[str, Any]:
    press = press_override if isinstance(press_override, dict) else _best_press_for_link(item, work)
    work_path, press_path, source = _suggest_paths_for_candidate(item, work, target_info)
    suggested_target_path = _suggested_target_path_for_candidate(work_path, press_path, target_info)
    link_target_exists = target_info.get("target_exists") is True
    target_name_matches, shortcut_work_name, target_work_base, target_suffix = _target_leaf_matches_shortcut_work(
        item,
        press,
        target_info,
    )
    work_name_matches_target = bool(
        target_work_base
        and _strict_name_key(work.get("name") or "") == _strict_name_key(target_work_base)
    )
    strict_exact = bool(exact and (target_name_matches or work_name_matches_target))
    reason = ""
    if (
        press
        and link_target_exists
        and target_info.get("target_resolved")
        and not (target_name_matches or work_name_matches_target)
    ):
        reason = (
            "目标目录名未完全匹配：索引作品名「"
            + shortcut_work_name
            + "」，目标目录去掉 _压制格式 后为「"
            + target_work_base
            + "」"
        )
    elif not press:
        reason = "作品没有可关联的压制项"
    elif not target_info.get("target_resolved"):
        reason = "未解析快捷方式目标，需人工确认相对路径"
    elif not target_info.get("target_under_media_root"):
        reason = "快捷方式目标不在当前媒体根目录下，需人工确认相对路径"
    if not press and [p for p in work.get("press", []) if isinstance(p, dict)]:
        reason = "压制格式未匹配；同格式多项时需要压制组也匹配"
    if not link_target_exists:
        reason = "快捷方式目标目录不存在，不能作为实际资源目录关联"
    result = {
        "work_key": work.get("work_key") or "",
        "yaml_source_rel": work.get("yaml_source_rel") or "",
        "index_in_file": work.get("index_in_file"),
        "name": work.get("name") or "",
        "year": work.get("year") or "",
        "begin_date": work.get("begin_date") or "",
        "end_date": work.get("end_date") or "",
        "date_range_label": work.get("date_range_label") or "",
        "path": work.get("path") or "",
        "score": round(float(score), 4),
        "match_type": match_type or ("exact" if strict_exact else "candidate"),
        "press_key": press.get("press_key") if press else "",
        "press_label": press.get("label") if press else "",
        "press_path": press.get(TV_JP_PRESS_PATH_KEY) if press else "",
        "press_options": _press_options_for_work(work),
        "suggested_path": work_path,
        "suggested_press_path": press_path,
        "suggested_target_path": suggested_target_path,
        "suggested_path_source": source,
        "target_name_matched": target_name_matches,
        "target_name_check": {
            "shortcut_work_name": shortcut_work_name,
            "target_work_base": target_work_base,
            "stripped_suffix": target_suffix,
            "work_name_matches_target": work_name_matches_target,
        },
        "can_apply": bool(link_target_exists and press and suggested_target_path),
        "can_auto_apply": bool(link_target_exists and strict_exact and press and suggested_target_path),
        "reason": reason,
    }
    if press:
        result["press_format"] = press.get(TV_JP_PRESS_FORMAT_KEY) or ""
        result["press_group"] = press.get(TV_JP_PRESS_GROUP_KEY) or ""
    else:
        result["press_format"] = ""
        result["press_group"] = ""
    result["reject_key"] = _association_reject_key(item, result)
    return result


def _association_shortcut_parent_relpath(item: dict[str, Any]) -> str:
    rel = _str_or_blank(item.get("shortcut_relpath") or item.get("relpath")).replace("\\", "/")
    if not rel:
        return ""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _association_link_option(item: dict[str, Any], *, resolve_target: bool = False) -> dict[str, Any]:
    target_info = _shortcut_target_info(item, resolve_target=resolve_target)
    rel = _str_or_blank(item.get("shortcut_relpath") or item.get("relpath")).replace("\\", "/")
    return {
        "shortcut_relpath": rel,
        "shortcut_path": _str_or_blank(item.get("shortcut_path") or item.get("path")),
        "link_name": _without_lnk_suffix(item.get("name") or Path(rel).name),
        "display": rel[:-4] if rel.lower().endswith(".lnk") else rel,
        "target_path": target_info.get("target_path") or "",
        "target_exists": target_info.get("target_exists"),
        "target_relpath": target_info.get("target_relpath") or "",
        "target_under_media_root": bool(target_info.get("target_under_media_root")),
        "target_resolved": bool(target_info.get("target_resolved")),
        "target_error": target_info.get("error") or "",
    }


def _association_link_options_for_item(
    item: dict[str, Any],
    links_by_parent: dict[str, list[dict[str, Any]]] | None,
    *,
    resolve_targets: bool = False,
) -> list[dict[str, Any]]:
    parent = _association_shortcut_parent_relpath(item)
    rows = list((links_by_parent or {}).get(parent.lower(), [])) if parent else [item]
    if not rows:
        rows = [item]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        option = _association_link_option(row, resolve_target=resolve_targets)
        rel_key = _str_or_blank(option.get("shortcut_relpath")).casefold()
        path_key = _str_or_blank(option.get("shortcut_path")).casefold()
        key = rel_key or path_key
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(option)
    current_rel = _str_or_blank(item.get("shortcut_relpath") or item.get("relpath")).replace("\\", "/").casefold()
    out.sort(
        key=lambda opt: (
            0 if _str_or_blank(opt.get("shortcut_relpath")).casefold() == current_rel else 1,
            str(opt.get("display") or opt.get("shortcut_relpath") or "").casefold(),
        )
    )
    return out


def _association_exact_work_records_for_item(
    item: dict[str, Any],
    target_info: dict[str, Any],
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    target_work_base, _target_leaf, _target_suffix = _target_leaf_work_base(item, None, target_info)
    work_name = target_work_base or _shortcut_work_name_hint(item)
    strict_key = _strict_name_key(work_name)
    if not strict_key:
        return []
    by_strict_exact = cast(dict[str, list[dict[str, Any]]], ctx.get("by_strict_exact") or {})
    records = by_strict_exact.get(strict_key, [])
    return [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("work"), dict)
        and _shortcut_work_dates_compatible(item, cast(dict[str, Any], record.get("work") or {}))
    ]


def _association_candidates_for_exact_work_group(
    item: dict[str, Any],
    target_info: dict[str, Any],
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in _association_exact_work_records_for_item(item, target_info, ctx):
        work = cast(dict[str, Any], record.get("work") or {})
        for press in work.get("press", []) or []:
            if not isinstance(press, dict) or not _str_or_blank(press.get("press_key")):
                continue
            key = "::".join(
                [
                    _str_or_blank(work.get("yaml_source_rel")).replace("\\", "/"),
                    str(work.get("index_in_file")),
                    _str_or_blank(press.get("press_key")),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _association_candidate(
                    item,
                    work,
                    target_info,
                    score=1.0,
                    exact=True,
                    press_override=press,
                    match_type="work_exact",
                )
            )
    candidates.sort(
        key=lambda c: (
            str(c.get("name") or "").casefold(),
            str(c.get("begin_date") or c.get("year") or ""),
            str(c.get("press_format") or "").casefold(),
            str(c.get("press_group") or "").casefold(),
        )
    )
    return candidates


def _association_candidate_key(cand: dict[str, Any]) -> str:
    return "::".join(
        [
            _str_or_blank(cand.get("yaml_source_rel")).replace("\\", "/").casefold(),
            str(cand.get("index_in_file")),
            _str_or_blank(cand.get("press_key")).casefold(),
        ]
    )


def _association_candidate_is_unlinked(cand: dict[str, Any]) -> bool:
    return not _str_or_blank(cand.get("press_path"))


def _association_link_key(link: dict[str, Any]) -> str:
    rel = _str_or_blank(link.get("shortcut_relpath")).replace("\\", "/").casefold()
    if rel:
        return rel
    return _str_or_blank(link.get("shortcut_path")).casefold()


def _press_group_match_key(raw: Any) -> str:
    key = _press_component_key(raw)
    return "" if key in {"", "----"} else key


def _candidate_press_match_pair(cand: dict[str, Any]) -> tuple[str, str]:
    return (
        _press_component_key(cand.get("press_format")),
        _press_group_match_key(cand.get("press_group")),
    )


def _link_option_press_match_pair(link: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[str, str]:
    press_rows = [
        {
            TV_JP_PRESS_FORMAT_KEY: cand.get("press_format") or "",
            TV_JP_PRESS_GROUP_KEY: cand.get("press_group") or "",
        }
        for cand in candidates
    ]
    item = {"name": (link.get("link_name") or Path(str(link.get("display") or "")).name or "") + ".lnk"}
    fmt, group = _link_press_hint(item, {"press": press_rows})
    return fmt, _press_group_match_key(group)


def _association_default_candidates_by_link(
    link_options: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    pairing_candidates: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    link_by_key = {_association_link_key(link): link for link in link_options if _association_link_key(link)}
    cand_by_key = {_association_candidate_key(cand): cand for cand in candidates if _association_candidate_key(cand)}
    pair_cand_by_key = {
        _association_candidate_key(cand): cand
        for cand in (pairing_candidates if pairing_candidates is not None else candidates)
        if _association_candidate_key(cand)
    }
    remaining_links = set(link_by_key)
    remaining_cands = set(pair_cand_by_key)
    pair_by_link: dict[str, str] = {}
    reason_by_link: dict[str, str] = {}
    edges: list[tuple[str, str]] = []
    cand_pairs = {key: _candidate_press_match_pair(cand) for key, cand in pair_cand_by_key.items()}
    for link_key, link in link_by_key.items():
        link_pair = _link_option_press_match_pair(link, candidates)
        if not link_pair[0]:
            continue
        for cand_key, cand_pair in cand_pairs.items():
            if link_pair == cand_pair:
                edges.append((link_key, cand_key))
    while True:
        active_edges = [
            (link_key, cand_key)
            for link_key, cand_key in edges
            if link_key in remaining_links and cand_key in remaining_cands
        ]
        link_degree: dict[str, int] = {}
        cand_degree: dict[str, int] = {}
        for link_key, cand_key in active_edges:
            link_degree[link_key] = link_degree.get(link_key, 0) + 1
            cand_degree[cand_key] = cand_degree.get(cand_key, 0) + 1
        unique_edges = [
            (link_key, cand_key)
            for link_key, cand_key in active_edges
            if link_degree.get(link_key) == 1 and cand_degree.get(cand_key) == 1
        ]
        if not unique_edges:
            break
        for link_key, cand_key in unique_edges:
            if link_key not in remaining_links or cand_key not in remaining_cands:
                continue
            pair_by_link[link_key] = cand_key
            reason_by_link[link_key] = "press_exact"
            remaining_links.remove(link_key)
            remaining_cands.remove(cand_key)
    if pair_by_link and len(remaining_links) == 1 and len(remaining_cands) == 1:
        link_key = next(iter(remaining_links))
        cand_key = next(iter(remaining_cands))
        pair_by_link[link_key] = cand_key
        reason_by_link[link_key] = "remaining_single"
    out: dict[str, dict[str, Any]] = {}
    out_reasons: dict[str, str] = {}
    for link_key, cand_key in pair_by_link.items():
        cand = cand_by_key.get(cand_key)
        if not cand:
            continue
        out[link_key] = cand
        out_reasons[link_key] = reason_by_link.get(link_key, "")
    return out, out_reasons


def _association_row_for_unmapped_link(
    item: dict[str, Any],
    works: list[dict[str, Any]],
    match_ctx: dict[str, Any] | None = None,
    *,
    resolve_target: bool = False,
    links_by_parent: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    target_info = _shortcut_target_info(item, resolve_target=resolve_target)
    link_options = _association_link_options_for_item(
        item,
        links_by_parent,
        resolve_targets=resolve_target,
    )
    hints = _target_name_hints(item, target_info)
    hint_keys = [_name_match_key(hint) for hint in hints]
    strict_hint_keys = [_strict_name_key(hint) for hint in hints]
    year_hint = _shortcut_year_hint(item)
    ctx = match_ctx or _association_match_context(works)
    by_exact = cast(dict[str, list[dict[str, Any]]], ctx.get("by_exact") or {})
    by_strict_exact = cast(dict[str, list[dict[str, Any]]], ctx.get("by_strict_exact") or {})
    by_token = cast(dict[str, list[dict[str, Any]]], ctx.get("by_token") or {})
    by_year = cast(dict[str, list[dict[str, Any]]], ctx.get("by_year") or {})
    pool_by_key: dict[str, dict[str, Any]] = {}
    strict_exact_keys: set[str] = set()
    loose_exact_keys: set[str] = set()
    target_base_hint, _target_leaf_hint, target_base_suffix = _target_leaf_work_base(item, None, target_info)
    target_base_key = _strict_name_key(target_base_hint)
    for strict_hint_key in strict_hint_keys:
        for record in by_strict_exact.get(strict_hint_key, []):
            wk = str(record.get("work_key") or "")
            if wk:
                pool_by_key[wk] = record
                strict_exact_keys.add(wk)
    for hint_key in hint_keys:
        for record in by_exact.get(hint_key, []):
            wk = str(record.get("work_key") or "")
            if wk:
                pool_by_key.setdefault(wk, record)
                loose_exact_keys.add(wk)
        for token in _match_tokens(hint_key):
            for record in by_token.get(token, []):
                wk = str(record.get("work_key") or "")
                if wk:
                    pool_by_key.setdefault(wk, record)
    if year_hint:
        same_year = {
            str(record.get("work_key") or ""): record
            for record in by_year.get(year_hint, [])
            if str(record.get("work_key") or "")
        }
        narrowed = {
            wk: record
            for wk, record in pool_by_key.items()
            if wk in same_year or wk in strict_exact_keys or wk in loose_exact_keys
        }
        if narrowed:
            pool_by_key = narrowed
    candidates: list[dict[str, Any]] = []
    for record in pool_by_key.values():
        work = cast(dict[str, Any], record.get("work") or {})
        name_key = str(record.get("name_key") or "")
        if not name_key:
            continue
        if not _shortcut_work_dates_compatible(item, work):
            continue
        wk = str(record.get("work_key") or "")
        exact = wk in strict_exact_keys
        loose_exact = wk in loose_exact_keys
        if (
            target_base_suffix
            and target_base_key
            and _strict_name_key(work.get("name") or "") != target_base_key
        ):
            continue
        score = 1.0 if exact else (0.98 if loose_exact else max((_match_similarity(hk, name_key) for hk in hint_keys), default=0.0))
        if year_hint and str(work.get("year") or "") == year_hint:
            score = min(1.0, score + 0.08)
        if exact or loose_exact or score >= 0.42:
            candidates.append(
                _association_candidate(item, work, target_info, score=score, exact=exact),
            )
    original_exact_candidates = [
        cand
        for cand in candidates
        if cand.get("press_key") and cand.get("match_type") == "exact"
    ]
    pairing_candidates = [cand for cand in candidates if cand.get("press_key")]
    candidates = [
        cand
        for cand in candidates
        if cand.get("press_key") and _association_candidate_is_unlinked(cand)
    ]
    original_exact_candidates = [
        cand
        for cand in original_exact_candidates
        if _association_candidate_is_unlinked(cand)
    ]
    if not original_exact_candidates:
        work_group_candidates_all = [
            cand for cand in _association_candidates_for_exact_work_group(item, target_info, ctx) if cand.get("press_key")
        ]
        work_group_candidates = [
            cand
            for cand in work_group_candidates_all
            if cand.get("press_key") and _association_candidate_is_unlinked(cand)
        ]
        if work_group_candidates:
            candidates = work_group_candidates
            pairing_candidates = work_group_candidates_all
    candidates.sort(
        key=lambda c: (
            0 if c.get("match_type") == "exact" else 1,
            -float(c.get("score") or 0),
            str(c.get("name") or ""),
        )
    )
    if not any(cand.get("match_type") == "work_exact" for cand in candidates):
        candidates = candidates[:12]
    default_candidates_by_link, default_reasons_by_link = _association_default_candidates_by_link(
        link_options,
        candidates,
        pairing_candidates,
    )
    current_link_key = _association_link_key(link_options[0]) if link_options else ""
    default_candidate = default_candidates_by_link.get(current_link_key)
    default_reason = default_reasons_by_link.get(current_link_key, "")
    exact_candidates = [c for c in candidates if c.get("match_type") == "exact"]
    auto_candidate = exact_candidates[0] if len(exact_candidates) == 1 and exact_candidates[0].get("can_auto_apply") else None
    if auto_candidate is None and default_candidate and default_candidate.get("can_auto_apply"):
        auto_candidate = default_candidate
        exact_candidates = [default_candidate]
    return {
        "id": item.get("shortcut_relpath") or item.get("relpath") or item.get("shortcut_path") or "",
        "shortcut_relpath": item.get("shortcut_relpath") or item.get("relpath") or "",
        "shortcut_path": item.get("shortcut_path") or item.get("path") or "",
        "link_name": _without_lnk_suffix(item.get("name") or ""),
        "work_name_hint": hints[0] if hints else "",
        "name_hints": hints,
        "year_hint": year_hint,
        "target_path": target_info.get("target_path") or "",
        "target_exists": target_info.get("target_exists"),
        "target_relpath": target_info.get("target_relpath") or "",
        "target_under_media_root": bool(target_info.get("target_under_media_root")),
        "target_resolved": bool(target_info.get("target_resolved")),
        "target_error": target_info.get("error") or "",
        "link_options": link_options,
        "link_option_count": len(link_options),
        "work_group_relpath": _association_shortcut_parent_relpath(item),
        "exact_count": len(exact_candidates),
        "candidates": candidates,
        "default_candidate": default_candidate,
        "default_match_reason": default_reason,
        "auto_candidate": auto_candidate,
        "can_auto_apply": bool(auto_candidate),
    }


def _association_row_without_rejected(row: dict[str, Any], rejected_keys: set[str]) -> dict[str, Any]:
    if not rejected_keys:
        return row
    candidates = [
        cand
        for cand in (row.get("candidates") if isinstance(row.get("candidates"), list) else [])
        if isinstance(cand, dict) and str(cand.get("reject_key") or "") not in rejected_keys
    ]
    if len(candidates) == len(row.get("candidates") or []):
        return row
    link_options = [item for item in (row.get("link_options") if isinstance(row.get("link_options"), list) else []) if isinstance(item, dict)]
    default_candidates_by_link, default_reasons_by_link = _association_default_candidates_by_link(link_options, candidates)
    current_link_key = _association_link_key(link_options[0]) if link_options else ""
    default_candidate = default_candidates_by_link.get(current_link_key)
    default_reason = default_reasons_by_link.get(current_link_key, "")
    exact_candidates = [c for c in candidates if c.get("match_type") == "exact"]
    auto_candidate = exact_candidates[0] if len(exact_candidates) == 1 and exact_candidates[0].get("can_auto_apply") else None
    if auto_candidate is None and default_candidate and default_candidate.get("can_auto_apply"):
        auto_candidate = default_candidate
        exact_candidates = [default_candidate]
    next_row = dict(row)
    next_row["candidates"] = candidates
    next_row["exact_count"] = len(exact_candidates)
    next_row["default_candidate"] = default_candidate
    next_row["default_match_reason"] = default_reason
    next_row["auto_candidate"] = auto_candidate
    next_row["can_auto_apply"] = bool(auto_candidate)
    return next_row


def _unmapped_disk_leaves_for_works(works: list[dict[str, Any]], *, refresh_targets: bool = False) -> list[dict[str, Any]]:
    return _unmapped_disk_leaves_from_scan(works, _scan_shortcut_leaves(refresh_targets=refresh_targets))


def _unmapped_disk_leaves_from_scan(
    works: list[dict[str, Any]],
    shortcut_leaves: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan = _build_plan_from_works(works)
    planned_relpaths = _planned_relpath_keys(plan)
    planned_targets = _planned_target_keys(plan)
    return [
        item
        for item in shortcut_leaves
        if _disk_leaf_is_unmapped(item, planned_relpaths=planned_relpaths, planned_targets=planned_targets)
    ]


def _association_rows_for_disk_leaves(
    works: list[dict[str, Any]],
    disk_leaves: list[dict[str, Any]],
    *,
    resolve_targets: bool = False,
    rejected_keys: set[str] | None = None,
    link_leaves: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    match_ctx = _association_match_context(works)
    links_by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in link_leaves if link_leaves is not None else disk_leaves:
        parent = _association_shortcut_parent_relpath(item).lower()
        if parent:
            links_by_parent.setdefault(parent, []).append(item)
    for rows in links_by_parent.values():
        rows.sort(
            key=lambda x: str(x.get("shortcut_relpath") or x.get("relpath") or x.get("shortcut_path") or "").casefold()
        )
    rows = [
        _association_row_for_unmapped_link(
            item,
            works,
            match_ctx,
            resolve_target=resolve_targets,
            links_by_parent=links_by_parent,
        )
        for item in disk_leaves
    ]
    if rejected_keys:
        rows = [_association_row_without_rejected(row, rejected_keys) for row in rows]
    return rows


def _disk_assoc_signature(
    works: list[dict[str, Any]],
    disk_leaves: list[dict[str, Any]],
    rejected_keys: set[str] | None = None,
) -> tuple[Any, ...]:
    work_sig = []
    for work in works:
        press_sig = []
        for press in work.get("press", []):
            if not isinstance(press, dict):
                continue
            press_sig.append(
                (
                    str(press.get("press_key") or ""),
                    str(press.get(TV_JP_PRESS_FORMAT_KEY) or ""),
                    str(press.get(TV_JP_PRESS_GROUP_KEY) or ""),
                    str(press.get(TV_JP_PRESS_PATH_KEY) or ""),
                )
            )
        work_sig.append(
            (
                str(work.get("work_key") or ""),
                str(work.get("yaml_source_rel") or ""),
                str(work.get("index_in_file") or ""),
                str(work.get("name") or ""),
                str(work.get("year") or ""),
                str(work.get("path") or ""),
                tuple(sorted(press_sig)),
            )
        )
    disk_sig = [
        (
            str(item.get("shortcut_relpath") or item.get("relpath") or ""),
            str(item.get("target_path") or ""),
            bool(item.get("target_exists")),
        )
        for item in disk_leaves
    ]
    return (tuple(sorted(work_sig)), tuple(sorted(disk_sig)), tuple(sorted(rejected_keys or ())))


def _cache_disk_association_rows(
    works: list[dict[str, Any]],
    disk_leaves: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    rejected_keys: set[str] | None = None,
) -> None:
    if len(rows) != len(disk_leaves):
        return
    _DISK_ASSOC_CACHE["signature"] = _disk_assoc_signature(works, disk_leaves, rejected_keys)
    _DISK_ASSOC_CACHE["rows"] = list(rows)


def _cached_disk_association_rows(
    works: list[dict[str, Any]],
    disk_leaves: list[dict[str, Any]],
    rejected_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    signature = _disk_assoc_signature(works, disk_leaves, rejected_keys)
    if _DISK_ASSOC_CACHE.get("signature") != signature:
        return []
    return list(cast(list[dict[str, Any]], _DISK_ASSOC_CACHE.get("rows") or []))


def _link_index_lite_payload_signature(settings: JpTvBrowseSettings) -> tuple[Any, ...]:
    files = []
    for fp in _catalog_yaml_paths(settings):
        try:
            st = fp.stat()
        except OSError:
            files.append((str(fp), None, None))
            continue
        files.append((str(fp), st.st_mtime_ns, st.st_size))
    config_s = json.dumps(collection_link_index_config_json(), ensure_ascii=False, sort_keys=True)
    return (tuple(files), config_s, _SHORTCUT_SCAN_CACHE.get("signature"))


def _cached_link_index_lite_payload(settings: JpTvBrowseSettings) -> dict[str, Any] | None:
    signature = _link_index_lite_payload_signature(settings)
    if _LINK_INDEX_LITE_PAYLOAD_CACHE.get("signature") != signature:
        return None
    payload = _LINK_INDEX_LITE_PAYLOAD_CACHE.get("payload")
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def _cache_link_index_lite_payload(settings: JpTvBrowseSettings, payload: dict[str, Any]) -> None:
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = _link_index_lite_payload_signature(settings)
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = payload


def link_index_association_payload(
    settings: JpTvBrowseSettings,
    *,
    resolve_targets: bool = False,
    refresh_links: bool = False,
) -> dict[str, Any]:
    works = _load_catalog_works(settings)
    all_disk_leaves = _scan_shortcut_leaves(refresh_targets=refresh_links)
    disk_leaves = _unmapped_disk_leaves_from_scan(works, all_disk_leaves)
    rejected_keys = _association_reject_keys()
    rows = _association_rows_for_disk_leaves(
        works,
        disk_leaves,
        resolve_targets=resolve_targets,
        rejected_keys=rejected_keys,
        link_leaves=all_disk_leaves,
    )
    _cache_disk_association_rows(works, disk_leaves, rows, rejected_keys=rejected_keys)
    auto_items: list[dict[str, Any]] = []
    for row in rows:
        cand = row.get("auto_candidate")
        if not isinstance(cand, dict):
            continue
        auto_items.append(
            {
                "shortcut_relpath": row.get("shortcut_relpath") or "",
                "yaml_source_rel": cand.get("yaml_source_rel") or "",
                "index_in_file": cand.get("index_in_file"),
                "press_key": cand.get("press_key") or "",
                "path": cand.get("suggested_path") or "",
                "press_path": cand.get("suggested_press_path") or "",
                "target_path": cand.get("suggested_target_path") or "",
            }
        )
    summary = {
        "total_unmapped": len(rows),
        "exact": sum(1 for row in rows if row.get("target_exists") is True and int(row.get("exact_count") or 0) == 1),
        "exact_auto": len(auto_items),
        "ambiguous_exact": sum(1 for row in rows if row.get("target_exists") is True and int(row.get("exact_count") or 0) > 1),
        "with_candidates": sum(1 for row in rows if row.get("candidates")),
        "without_candidates": sum(1 for row in rows if not row.get("candidates")),
        "target_outside_media_root": sum(
            1 for row in rows if row.get("target_path") and not row.get("target_under_media_root")
        ),
        "targets_resolved": sum(1 for row in rows if row.get("target_resolved")),
    }
    return {
        "ok": True,
        "config": collection_link_index_config_json(),
        "summary": summary,
        "auto_apply_items": auto_items,
        "rows": rows,
    }


def reject_link_index_association_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    reject_key = _str_or_blank(body.get("reject_key"))
    if not reject_key:
        raise ValueError("reject_key 不能为空")
    store = _load_association_rejects()
    items = store.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        store["items"] = items
    items[reject_key] = {
        "key": reject_key,
        "shortcut_relpath": _str_or_blank(body.get("shortcut_relpath")).replace("\\", "/"),
        "target_path": _str_or_blank(body.get("target_path")),
        "yaml_source_rel": _str_or_blank(body.get("yaml_source_rel")).replace("\\", "/"),
        "index_in_file": body.get("index_in_file"),
        "press_key": _str_or_blank(body.get("press_key")),
    }
    _save_association_rejects(store)
    _DISK_ASSOC_CACHE["signature"] = None
    _DISK_ASSOC_CACHE["rows"] = []
    return {
        "rejected_key": reject_key,
        "association": link_index_association_payload(settings),
    }


def apply_link_index_associations_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 必须为非空数组")
    by_work: dict[str, dict[str, Any]] = {}
    requested_shortcut_relpaths: set[str] = set()
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{idx}] 必须为对象")
        shortcut_rel = _str_or_blank(raw.get("shortcut_relpath")).replace("\\", "/").lower()
        if shortcut_rel:
            requested_shortcut_relpaths.add(shortcut_rel)
        yaml_rel = _str_or_blank(raw.get("yaml_source_rel")).replace("\\", "/")
        try:
            index_in_file = int(raw.get("index_in_file"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"items[{idx}].index_in_file 非法") from exc
        press_key = _str_or_blank(raw.get("press_key"))
        if _str_or_blank(raw.get("target_path")):
            path_s, press_path_s = _split_target_path_for_ui_mapping(
                raw.get("target_path"),
                label=f"items[{idx}].target_path",
            )
        else:
            path_s = _clean_work_path(raw.get("path"), label=f"items[{idx}].path")
            press_path_s = _clean_rel_path(raw.get("press_path"), label=f"items[{idx}].press_path")
        if not yaml_rel or index_in_file < 0:
            raise ValueError(f"items[{idx}] 缺少有效作品定位")
        if not press_key:
            raise ValueError(f"items[{idx}] 缺少 press_key")
        if not path_s or not press_path_s:
            raise ValueError(f"items[{idx}] target_path / path / press_path 不能为空")
        key = _work_key(yaml_rel, index_in_file)
        mapping = by_work.setdefault(
            key,
            {
                "yaml_source_rel": yaml_rel,
                "index_in_file": index_in_file,
                "path": path_s,
                "press": [],
            },
        )
        if mapping["path"] != path_s:
            raise ValueError(f"{yaml_rel}#{index_in_file} 收到多个不同 path")
        mapping["press"].append({"press_key": press_key, TV_JP_PRESS_PATH_KEY: press_path_s})
    writes = _save_ui_mappings_to_catalog({"works": list(by_work.values())}, settings=settings)
    payload = collection_link_index_payload(settings)
    payload["writes"] = writes
    association = link_index_association_payload(settings)
    payload["association"] = association
    if requested_shortcut_relpaths:
        remaining = {
            _str_or_blank(row.get("shortcut_relpath")).replace("\\", "/").lower()
            for row in association.get("rows", [])
            if isinstance(row, dict)
        }
        unresolved = sorted(x for x in requested_shortcut_relpaths if x in remaining)
        if unresolved:
            payload["association_unresolved"] = unresolved
            payload["write_warning"] = (
                "关联已写入，但部分索引项重新匹配后仍未消失，请检查路径、压制项或是否存在重复快捷方式："
                + "，".join(unresolved[:5])
                + (" ..." if len(unresolved) > 5 else "")
            )
    if not writes and not payload.get("write_warning"):
        payload["write_warning"] = "没有检测到 YAML 实际变更，可能已经写入过。"
    return payload


def apply_link_index_target_fixes_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 必须为非空数组")
    by_work: dict[str, dict[str, Any]] = {}
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{idx}] 必须为对象")
        yaml_rel = _str_or_blank(raw.get("yaml_source_rel")).replace("\\", "/")
        try:
            index_in_file = int(raw.get("index_in_file"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"items[{idx}].index_in_file 非法") from exc
        press_key = _str_or_blank(raw.get("press_key"))
        target_path_raw = raw.get("target_path", raw.get("fix_target_path"))
        path_s, press_path_s = _split_target_path_for_ui_mapping(
            target_path_raw,
            label=f"items[{idx}].target_path",
        )
        target_path = Path(_str_or_blank(target_path_raw)).expanduser()
        try:
            target_path = target_path.resolve()
        except OSError:
            pass
        if not target_path.is_dir():
            raise ValueError(f"items[{idx}].target_path 不是存在的资源目录")
        if not yaml_rel or index_in_file < 0:
            raise ValueError(f"items[{idx}] 缺少有效作品定位")
        if not press_key:
            raise ValueError(f"items[{idx}] 缺少 press_key")
        key = _work_key(yaml_rel, index_in_file)
        mapping = by_work.setdefault(
            key,
            {
                "yaml_source_rel": yaml_rel,
                "index_in_file": index_in_file,
                "path": path_s,
                "press": [],
            },
        )
        if mapping["path"] != path_s:
            raise ValueError(f"{yaml_rel}#{index_in_file} 收到多个不同 path")
        mapping["press"].append({"press_key": press_key, TV_JP_PRESS_PATH_KEY: press_path_s})
    writes = _save_ui_mappings_to_catalog({"works": list(by_work.values())}, settings=settings)
    _DISK_ASSOC_CACHE["signature"] = None
    _DISK_ASSOC_CACHE["rows"] = []
    payload = collection_link_index_payload(settings, refresh_links=True)
    payload["writes"] = writes
    return payload


def _shortcut_fix_item_from_ui(raw: dict[str, Any], idx: int) -> dict[str, Any]:
    sr = shortcut_root()
    shortcut_s = _str_or_blank(raw.get("shortcut_path") or raw.get("path"))
    shortcut_rel = _str_or_blank(raw.get("shortcut_relpath")).replace("\\", "/")
    if shortcut_rel and not shortcut_s:
        shortcut_s = str((sr / shortcut_rel).resolve())
    if not shortcut_s:
        raise ValueError(f"items[{idx}] 缺少 shortcut_path")
    shortcut = Path(shortcut_s).expanduser().resolve()
    shortcut.relative_to(sr)
    if shortcut.suffix.lower() != ".lnk":
        raise ValueError(f"items[{idx}].shortcut_path 必须是 .lnk")
    if not shortcut.is_file():
        raise FileNotFoundError(str(shortcut))
    rel = shortcut.relative_to(sr).as_posix()
    return {
        "type": "disk_link",
        "name": shortcut.name,
        "path": str(shortcut),
        "shortcut_path": str(shortcut),
        "shortcut_relpath": rel,
        "shortcut_parts": rel.split("/"),
        "shortcut_exists": True,
        "target_path": _str_or_blank(raw.get("current_target_path") or raw.get("old_target_path")),
        "target_exists": False,
    }


def apply_link_index_target_fixes_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 必须为非空数组")
    resource_items = _resource_fix_items_from_cache()
    if not resource_items:
        raise ValueError("资源库目录缓存为空，请先扫描资源库")
    fixes: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{idx}] 必须为对象")
        shortcut_item = _shortcut_fix_item_from_ui(raw, idx)
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
        candidate = _resource_fix_candidate_for_shortcut_item(shortcut_item, resource_items)
        if not candidate:
            raise ValueError(f"items[{idx}] 没有找到可修复候选")
        if _path_compare_key(candidate.get("target_path")) != _path_compare_key(target):
            raise ValueError(f"items[{idx}].target_path 与资源库候选不一致")
        _create_windows_shortcut(Path(str(shortcut_item["shortcut_path"])), target)
        fixes.append(
            {
                "shortcut_path": str(shortcut_item["shortcut_path"]),
                "shortcut_relpath": str(shortcut_item["shortcut_relpath"]),
                "target_path": str(target),
            }
        )
    _SHORTCUT_SCAN_CACHE["signature"] = None
    _SHORTCUT_SCAN_CACHE["leaves"] = []
    _LINK_INDEX_LITE_PAYLOAD_CACHE["signature"] = None
    _LINK_INDEX_LITE_PAYLOAD_CACHE["payload"] = None
    if body.get("refresh_payload") is False:
        return {"fixes": fixes}
    payload = collection_link_index_payload(settings, refresh_links=True)
    payload["fixes"] = fixes
    return payload


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


def _rename_shortcut_within_root(source_path: Path, target_path: Path) -> None:
    sr = shortcut_root()
    source = source_path.expanduser().resolve()
    target = target_path.expanduser().resolve()
    source.relative_to(sr)
    target.relative_to(sr)
    if source.suffix.lower() != ".lnk" or target.suffix.lower() != ".lnk":
        raise ValueError("只能规范化 .lnk 快捷方式")
    if _path_compare_key(source) == _path_compare_key(target):
        return
    if not source.is_file():
        raise FileNotFoundError(str(source))
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _payload_from_works(
    works: list[dict[str, Any]],
    *,
    refresh_links: bool = False,
    lite: bool = False,
) -> dict[str, Any]:
    plan = _build_plan_from_works(works)
    disk_leaves = _scan_shortcut_leaves(refresh_targets=refresh_links)
    _annotate_shortcut_resource_fixes(disk_leaves)
    _annotate_plan_shortcut_targets(plan, disk_leaves)
    _annotate_plan_resource_fixes(plan)
    planned_relpaths = _planned_relpath_keys(plan)
    planned_targets = _planned_target_keys(plan)
    unmapped_disk = [
        item
        for item in disk_leaves
        if _disk_leaf_is_unmapped(item, planned_relpaths=planned_relpaths, planned_targets=planned_targets)
    ]
    disk_match_rows = _cached_disk_association_rows(works, unmapped_disk)
    disk_matches = {
        str(row.get("shortcut_relpath") or "").lower(): row
        for row in disk_match_rows
        if row.get("shortcut_relpath")
    }
    plan_summary = _plan_summary(plan)
    plan_summary["unmapped_on_disk"] = len(unmapped_disk)
    plan_summary["target_fixable"] += sum(1 for item in unmapped_disk if item.get("target_fix"))
    tree = _build_tree(plan, disk_leaves, disk_matches)
    payload: dict[str, Any] = {
        "ok": True,
        "config": collection_link_index_config_json(),
        "plan_summary": plan_summary,
        "mapping_summary": _mapping_summary(works),
        "disk_summary": {
            "shortcut_leaves": len(disk_leaves),
            "unmapped_on_disk": len(unmapped_disk),
            "db_match_cached": bool(disk_match_rows),
        },
        "tree": _slim_tree_for_index_browser(tree) if lite else tree,
    }
    if lite:
        return payload
    dirs_warning = ""
    try:
        media_dirs = _relative_dirs(media_root())
    except OSError as exc:
        media_dirs = []
        dirs_warning = str(exc)
    payload.update(
        {
            "media_dirs": media_dirs,
            "media_dirs_warning": dirs_warning,
            "works": works,
            "plan": plan[:300],
        }
    )
    return payload


def collection_link_index_payload(
    settings: JpTvBrowseSettings,
    *,
    refresh_links: bool = False,
    lite: bool = False,
) -> dict[str, Any]:
    if lite and not refresh_links:
        cached = _cached_link_index_lite_payload(settings)
        if cached is not None:
            return cached
    payload = _payload_from_works(_load_catalog_works(settings), refresh_links=refresh_links, lite=lite)
    if lite:
        _cache_link_index_lite_payload(settings, payload)
    return payload


def preview_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    works = _merge_ui_mappings(_load_catalog_works(settings), _ui_mapping_items(body))
    return _payload_from_works(works)


def save_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    writes = _save_ui_mappings_to_catalog(body, settings=settings)
    payload = collection_link_index_payload(settings)
    payload["writes"] = writes
    return payload


def generate_link_index_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    works = _load_catalog_works(settings)
    overwrite = body.get("overwrite_shortcuts")
    overwrite_bool = bool(overwrite) if isinstance(overwrite, bool) else _overwrite_shortcuts_default()
    plan = _build_plan_from_works(works)
    disk_leaves = _scan_shortcut_leaves(refresh_targets=True)
    _annotate_shortcut_resource_fixes(disk_leaves)
    _annotate_plan_shortcut_targets(plan, disk_leaves)
    _annotate_plan_resource_fixes(plan)
    out_plan: list[dict[str, Any]] = []
    for item in plan:
        next_item = dict(item)
        if item.get("status") != "ready":
            next_item["skipped"] = True
            out_plan.append(next_item)
            continue
        shortcut = Path(str(item.get("shortcut_path") or ""))
        target = Path(str(item.get("target_path") or ""))
        matched_shortcut_s = _str_or_blank(item.get("matched_shortcut_path"))
        if (
            item.get("db_linked")
            and matched_shortcut_s
            and _path_compare_key(matched_shortcut_s) != _path_compare_key(shortcut)
        ):
            if shortcut.exists():
                next_item["status"] = "shortcut_exists"
                next_item["skipped"] = True
                next_item["rename_skipped"] = True
                out_plan.append(next_item)
                continue
            try:
                _rename_shortcut_within_root(Path(matched_shortcut_s), shortcut)
                next_item["renamed"] = True
                next_item["shortcut_exists"] = True
                next_item["matched_shortcut_path"] = str(shortcut)
                try:
                    next_item["matched_shortcut_relpath"] = shortcut.resolve().relative_to(shortcut_root()).as_posix()
                except (OSError, ValueError):
                    next_item["matched_shortcut_relpath"] = next_item.get("shortcut_relpath") or ""
            except (OSError, ValueError) as exc:
                next_item["status"] = "failed"
                next_item["error"] = str(exc)
            out_plan.append(next_item)
            continue
        if shortcut.exists() and not overwrite_bool:
            next_item["status"] = "shortcut_exists"
            next_item["skipped"] = True
            out_plan.append(next_item)
            continue
        try:
            _create_windows_shortcut(shortcut, target)
            next_item["created"] = True
        except (OSError, subprocess.CalledProcessError) as exc:
            next_item["status"] = "failed"
            next_item["error"] = str(exc)
        out_plan.append(next_item)
    disk_leaves = _scan_shortcut_leaves(refresh_targets=True)
    _annotate_shortcut_resource_fixes(disk_leaves)
    _annotate_plan_shortcut_targets(out_plan, disk_leaves)
    _annotate_plan_resource_fixes(out_plan)
    planned_relpaths = _planned_relpath_keys(out_plan)
    planned_targets = _planned_target_keys(out_plan)
    unmapped_disk = [
        item
        for item in disk_leaves
        if _disk_leaf_is_unmapped(item, planned_relpaths=planned_relpaths, planned_targets=planned_targets)
    ]
    disk_match_rows = _cached_disk_association_rows(works, unmapped_disk)
    disk_matches = {
        str(row.get("shortcut_relpath") or "").lower(): row
        for row in disk_match_rows
        if row.get("shortcut_relpath")
    }
    plan_summary = _plan_summary(out_plan)
    plan_summary["unmapped_on_disk"] = len(unmapped_disk)
    plan_summary["target_fixable"] += sum(1 for item in unmapped_disk if item.get("target_fix"))
    return {
        "config": collection_link_index_config_json(),
        "works": works,
        "plan": out_plan[:300],
        "plan_summary": plan_summary,
        "mapping_summary": _mapping_summary(works),
        "tree": _build_tree(out_plan, disk_leaves, disk_matches),
        "writes": [],
    }


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
        "$rows | ConvertTo-Json -Compress\n"
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
        raw_rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
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
    roots = [media_root(), shortcut_root()]
    if not _path_under_any_root(p, roots):
        raise ValueError("只能解析媒体根目录或索引输出目录下的路径")
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
    roots = [media_root(), shortcut_root()]
    if not _path_under_any_root(p, roots):
        raise ValueError("只能打开媒体根目录或索引输出目录下的路径")
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
