"""Pure resource-tree summaries and cached search projection.

This module deliberately does not read configuration, touch disk, or own caches.
"""
from __future__ import annotations

from typing import Any


RESOURCE_SCAN_METRICS_VERSION = 2


def resource_node_summary(node: dict[str, Any]) -> dict[str, Any]:
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


def resource_node_cache_payload(node: dict[str, Any]) -> dict[str, Any]:
    children = [child for child in node.get("children", []) or [] if isinstance(child, dict)]
    files = [item for item in node.get("files", []) or [] if isinstance(item, dict)]
    out = {k: v for k, v in node.items() if k not in {"children", "files"}}
    out["children"] = [resource_node_summary(child) for child in children]
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
        "metrics_version": RESOURCE_SCAN_METRICS_VERSION,
        "relpath": str(out.get("relpath") or ""),
        "node": out,
    }


def resource_parent_relpath(relpath: str) -> str:
    rel = str(relpath or "")
    if not rel:
        return ""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def resource_entry_search_text(entry: dict[str, Any]) -> str:
    return "\n".join(
        str(entry.get(key) or "").casefold()
        for key in ("name", "relpath", "path", "error", "series_name", "work_name", "press_info")
    )


def resource_entry_matches(entry: dict[str, Any], needle: str) -> bool:
    return bool(needle) and needle in resource_entry_search_text(entry)


def resource_search_tree_from_entries(entries: list[dict[str, Any]], query: str) -> tuple[dict[str, Any], dict[str, int]]:
    needle = query.casefold()
    folders = {str(item.get("relpath") or ""): item for item in entries if item.get("type") == "folder"}
    folder_keep: set[str] = {""}
    matched_folder_rels: set[str] = set()
    matched_files: list[dict[str, Any]] = []
    for entry in entries:
        if not resource_entry_matches(entry, needle):
            continue
        if entry.get("type") == "folder":
            relpath = str(entry.get("relpath") or "")
            matched_folder_rels.add(relpath)
            cur = relpath
            while True:
                folder_keep.add(cur)
                if not cur:
                    break
                cur = str(folders.get(cur, {}).get("parent_relpath") or resource_parent_relpath(cur))
        elif entry.get("type") == "file":
            matched_files.append(entry)
            cur = str(entry.get("parent_relpath") or resource_parent_relpath(str(entry.get("relpath") or "")))
            while True:
                folder_keep.add(cur)
                if not cur:
                    break
                cur = str(folders.get(cur, {}).get("parent_relpath") or resource_parent_relpath(cur))

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
        parent = str(file_entry.get("parent_relpath") or resource_parent_relpath(str(file_entry.get("relpath") or "")))
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
        parent = str(folders.get(relpath, {}).get("parent_relpath") or resource_parent_relpath(relpath))
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


def resource_search_entries_from_main_cache(cache: dict[str, Any]) -> list[dict[str, Any]]:
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
