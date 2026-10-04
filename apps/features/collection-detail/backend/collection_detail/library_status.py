"""Read-only page aggregation: saved DB records, disk facts and cached trees."""
from __future__ import annotations

from typing import Any

from collection_detail.catalog_inventory import CatalogInventoryRepository
from collection_detail import link_index
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.storage.disk_inventory import inventory_path_key, lexical_path


def _count(raw: Any) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return 0


def _observed_unbound_directories(cache: dict[str, Any], repository: CatalogInventoryRepository,
                                  snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not cache.get("cached"):
        return []
    bound = set()
    for work in snapshot["relationships"]["works"]:
        if work.get("resolved_path"):
            bound.add(inventory_path_key(work["resolved_path"]))
    observed = []
    seen = set()
    configured_roots = {inventory_path_key(root) for root in repository.roots}
    for raw_root in cache.get("roots", []) or []:
        if not isinstance(raw_root, dict):
            continue
        try:
            root = lexical_path(raw_root.get("root") or "")
            if inventory_path_key(root) not in configured_roots:
                continue
        except (TypeError, ValueError, OSError):
            continue
        for series in raw_root.get("series", []) or []:
            if not isinstance(series, dict):
                continue
            try:
                path = lexical_path(series.get("path") or "")
                key = inventory_path_key(path)
                if path.parent != root or key in bound or key in seen:
                    continue
            except (TypeError, ValueError, OSError):
                continue
            seen.add(key)
            fact = repository.directory_state(path)
            state = "observed-unbound" if fact["state"] == "both" else (
                "stale-observation" if fact["state"] == "db-only" else fact["state"]
            )
            observed.append({
                **fact, "state": state, "path": str(path), "name": str(series.get("name") or path.name),
                "relationship": "no-saved-path-binding", "name_matching": "not_checked", "source": "saved-resource-cache",
            })
    return observed


def library_status_payload(settings: JpTvBrowseSettings) -> dict[str, Any]:
    repository = CatalogInventoryRepository(
        settings, resource_roots=link_index.resource_roots(resolve_links=False),
        default_media_root=link_index._legacy_media_root(resolve_links=False),
    )
    snapshot = repository.snapshot()
    cache = link_index.resource_libraries_cached_payload()
    summary = cache.get("summary") if isinstance(cache.get("summary"), dict) else {}
    observed = _observed_unbound_directories(cache, repository, snapshot)
    return {
        "ok": True, "read_only": True, **snapshot,
        "resource_tree": {
            "cached": bool(cache.get("cached")), "scanned_at": str(cache.get("scanned_at") or ""),
            "directory_count": _count(summary.get("dir_count")), "file_count": _count(summary.get("file_count")),
            "truncated": bool(summary.get("truncated")), "count_source": "saved-resource-cache",
            "note": "目录树统计是缓存中的物理目录和文件数，不是数据库作品数；未扫描或离线不代表作品不存在。",
        },
        "observed_unbound_directories": observed,
        "observed_unbound_directory_count": len(observed),
        "observed_unbound_note": "仅展示已保存扫描缓存中未被作品 path 绑定的一级目录；不等于没有同名数据库记录，未进行名称匹配或自动入库。",
    }
