"""Join saved catalog bindings with explicit, read-only filesystem facts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from collection_detail.catalog_repository import CatalogRepository, resolve_catalog_directory
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.storage.disk_inventory import DiskInventory, inventory_path_key, lexical_path


RELATIONSHIP_STATES = {
    "both": "数据库已绑定，目录存在",
    "db-only": "数据库已绑定，资源根在线但目录不存在",
    "unbound": "数据库尚未填写目录绑定，不能判断磁盘是否缺失",
    "offline": "资源根或目标不可访问，不能判断数据是否缺失",
    "unsafe": "路径包含链接、重解析点或非普通目录",
    "invalid": "绑定路径非法或不属于配置的资源根",
}


class CatalogInventoryRepository:
    def __init__(self, settings: JpTvBrowseSettings, *, resource_roots: list[Path],
                 default_media_root: Path, disk: DiskInventory | None = None) -> None:
        self.catalog = CatalogRepository(settings)
        self.default_media_root = default_media_root
        self.disk = disk if disk is not None else DiskInventory()
        self.roots: list[Path] = []
        seen: set[str] = set()
        for raw in resource_roots:
            try:
                root = lexical_path(raw)
                key = inventory_path_key(root)
            except (OSError, ValueError):
                continue
            if key not in seen:
                seen.add(key)
                self.roots.append(root)

    def root_for(self, path: Path) -> Path | None:
        candidates = []
        for root in self.roots:
            try:
                path.relative_to(root)
                candidates.append(root)
            except ValueError:
                pass
        return max(candidates, key=lambda root: len(root.parts)) if candidates else None

    def directory_state(self, path: Path) -> dict[str, Any]:
        root = self.root_for(path)
        if root is None:
            return {"state": "invalid", "reason": "outside-configured-resource-roots", "resolved_path": str(path)}
        root_fact = self.disk.probe(root)
        if root_fact["state"] != "present":
            return {
                "state": "unsafe" if root_fact["state"] == "unsafe" else "offline",
                "reason": root_fact["reason"], "root": str(root), "resolved_path": str(path),
            }
        fact = self.disk.probe(path)
        return {
            "state": {"present": "both", "missing": "db-only", "unavailable": "offline", "unsafe": "unsafe"}[fact["state"]],
            "reason": fact["reason"], "root": str(root), "resolved_path": str(path),
        }

    def _binding(self, work_path: str, press_path: str | None = None) -> dict[str, Any]:
        if not work_path or (press_path is not None and not press_path):
            return {"state": "unbound", "reason": "work-path-unset" if not work_path else "press-path-unset", "resolved_path": ""}
        try:
            path = resolve_catalog_directory(self.default_media_root, work_path, press_path, resolve_links=False)
            return self.directory_state(path)
        except ValueError as error:
            return {"state": "invalid", "reason": "invalid-catalog-binding", "error": str(error), "resolved_path": ""}
        except OSError as error:
            return {"state": "offline", "reason": "binding-unavailable", "error": str(error), "resolved_path": ""}

    def snapshot(self) -> dict[str, Any]:
        saved_works = self.catalog.load_works()
        works = []
        counts = dict.fromkeys(RELATIONSHIP_STATES, 0)
        press_counts = dict.fromkeys(RELATIONSHIP_STATES, 0)
        press_record_count = 0
        for work in saved_works:
            binding = self._binding(str(work.get("path") or ""))
            presses = []
            for press in work.get("press", []):
                state = self._binding(str(work.get("path") or ""), str(press.get("press_path") or ""))
                presses.append({**press, **state})
                press_counts[state["state"]] += 1
                press_record_count += 1
            works.append({**work, **binding, "press": presses})
            counts[binding["state"]] += 1
        roots = []
        for root in self.roots:
            fact = self.disk.probe(root)
            roots.append({**fact, "online": fact["state"] == "present"})
        return {
            "database": {"work_record_count": len(saved_works), "press_record_count": press_record_count},
            "relationships": {"states": dict(RELATIONSHIP_STATES), "counts": counts, "press_counts": press_counts, "works": works},
            "roots": roots,
        }
