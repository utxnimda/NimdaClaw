"""Audit existing catalog/index/shortcut bindings; explicitly apply safe missing paths.

No media is moved and no shortcut is created, deleted or rewritten. Existing
nonempty bindings are never replaced. A reviewed plan hash, whole-file backups,
the catalog lock and compare-and-swap writes protect a repair batch.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
from io import StringIO
import json
from pathlib import Path
import stat
import sys
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/framework/backend"))

from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent
from work_catalog_yaml.jp_tv.browse_settings import load_jp_tv_browse_settings
from work_catalog_yaml.jp_tv.validate import load_jp_tv_entries_from_yaml
from work_catalog_yaml.layout import ensure_feature_backend_paths
from work_catalog_yaml.persistence import FileWrite, commit_file_writes, history_snapshot_name
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string

ensure_feature_backend_paths()
from collection_detail import link_index as links
from collection_detail.catalog_bindings import plan_catalog_bindings
from collection_detail.save import catalog_write_transaction, history_catalog_root


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ordinary(path: Path, *, directory: bool = False) -> bool:
    try:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return False
        return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    except OSError:
        return False


def parser() -> YAML:
    result = YAML(typ="rt")
    result.preserve_quotes = True
    result.allow_unicode = True
    result.width = 1_000_000
    return result


def read_document(path: Path) -> tuple[bytes, Any]:
    if not ordinary(path):
        raise ValueError(f"数据文件必须为已存在的普通文件：{path}")
    before = path.read_bytes()
    text = before.decode("utf-8")
    yaml = parser()
    if any(isinstance(event, AliasEvent) for event in yaml.parse(text)):
        raise ValueError(f"存在 YAML 别名，不能保证仅修改指定绑定：{path}")
    return before, yaml.load(text)


def serialize(document: Any, before: bytes) -> bytes:
    yaml = parser()
    if b"\r\n" in before:
        yaml.line_break = "\r\n"
    stream = StringIO()
    yaml.dump(document, stream)
    data = stream.getvalue().encode("utf-8")
    if yaml.load(data.decode("utf-8")) != document:
        raise ValueError("YAML 写回校验失败：标量或扩展字段发生变化")
    return data


def _path_key(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").casefold()


def fill_mapping(document: Any, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    works = links._works_list_mut(document)
    index = mapping["index_in_file"]
    if type(index) is not int or not 0 <= index < len(works):
        raise ValueError("作品记录位置无效")
    work = works[index]
    collections = [a for a in work.get("attributes", []) if isinstance(a, dict) and a.get("type") == "collection-type"]
    if len(collections) != 1 or not isinstance(collections[0].get("data"), dict):
        raise ValueError("作品必须恰有一个有效 collection-type 属性")
    collection = collections[0]["data"]
    changes: list[dict[str, Any]] = []

    def fill(container: dict, key: str, value: str, field: str) -> None:
        previous = container.get(key)
        if previous is not None and (not isinstance(previous, str) or previous.strip()):
            if _path_key(previous) != _path_key(value):
                raise ValueError(f"拒绝覆盖已有非空绑定：{field}")
            return
        container[key] = value
        changes.append({"field": field, "before": previous, "after": value})

    fill(collection, "path", mapping["path"], "path")
    press_rows = links._raw_press_rows(collection)
    for press in mapping.get("press", []):
        position = links._press_key_position(press["press_key"])
        if position is None or not 0 <= position < len(press_rows):
            raise ValueError("压制记录位置无效")
        fill(press_rows[position], "press_path", press["press_path"], f"press[{position}].press_path")
    return changes


def shortcut_evidence(items: list[dict], shortcut_roots: list[Path]) -> tuple[dict, list[dict], list[dict]]:
    """Read only the exact shortcut paths retained by the index; never scan media."""
    roots = [root.resolve() for root in shortcut_roots]
    paths: dict[str, Path] = {}
    by_entry: dict[str, str] = {}
    issues: list[dict] = []
    for item in items:
        raw = str(item.get("shortcut_path") or "")
        entry = str(item.get("entry_key") or "")
        if not raw or not entry:
            continue
        path = Path(raw)
        if not path.is_absolute() or path.suffix.lower() != ".lnk":
            issues.append({"code": "invalid-shortcut-path", "entry_key": entry, "path": raw})
            continue
        try:
            if not path.exists():
                continue
            resolved = path.resolve()
        except OSError as exc:
            issues.append({"code": "shortcut-inaccessible", "entry_key": entry, "path": raw, "message": str(exc)})
            continue
        root = next((root for root in roots if resolved.is_relative_to(root)), None)
        if root is None or not ordinary(path):
            issues.append({"code": "unsafe-shortcut-path", "entry_key": entry, "path": raw})
            continue
        current = path.parent
        safe = True
        while current != root and current != current.parent:
            if not ordinary(current, directory=True):
                safe = False
                break
            current = current.parent
        if not safe:
            issues.append({"code": "unsafe-shortcut-parent", "entry_key": entry, "path": raw})
            continue
        paths[str(resolved)] = resolved
        by_entry[entry] = str(resolved)
    targets: dict[str, dict] = {}
    snapshot: list[dict] = []
    path_list = sorted(paths.values(), key=lambda value: str(value).casefold())
    for offset in range(0, len(path_list), 400):
        batch = path_list[offset:offset + 400]
        print(f"只读解析快捷方式 {offset + 1}-{offset + len(batch)} / {len(path_list)}", file=sys.stderr, flush=True)
        before_stats = {str(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in batch}
        result = links._windows_shortcut_targets(batch)
        for path in batch:
            key = str(path)
            after = (path.stat().st_mtime_ns, path.stat().st_size)
            if after != before_stats[key]:
                raise ValueError(f"快捷方式读取过程中变化，请重新扫描：{path}")
            info = result.get(key) or {}
            targets[key] = info
            snapshot.append({"path": key, "mtime_ns": after[0], "size": after[1],
                             "target_path": str(info.get("target_path") or ""),
                             "resolved": bool(info.get("target_resolved"))})
    evidence: dict[str, dict] = {}
    for entry, path in by_entry.items():
        info = targets.get(path) or {}
        # An existing but unreadable shortcut is not evidence that can safely
        # be ignored in favour of an old cached target.
        evidence[entry] = {"target_path": str(info.get("target_path") or ""), "shortcut_path": path}
        if not info.get("target_resolved"):
            issues.append({"code": "shortcut-unreadable", "entry_key": entry, "path": path,
                           "message": str(info.get("error") or "无法读取实际快捷方式目标")})
    return evidence, snapshot, issues


def repair(settings: Any, *, apply: bool = False, expect_plan_sha256: str | None = None) -> dict:
    if settings.filesystem_root is None:
        raise ValueError("未配置作品数据库目录")
    if apply and not expect_plan_sha256:
        raise ValueError("执行修复必须提供已审预览的 --expect-plan-sha256")
    with catalog_write_transaction(settings.filesystem_root):
        paths = links._catalog_yaml_paths(settings)
        documents = {}
        for number, path in enumerate(paths):
            if number % 12 == 0:
                print(f"读取作品数据库 {number + 1}-{min(number + 12, len(paths))} / {len(paths)}", file=sys.stderr, flush=True)
            documents[str(path)] = read_document(path)
        index_path = links._link_index_db_path()
        if not ordinary(index_path):
            raise ValueError(f"索引必须为已存在的普通文件：{index_path}")
        index_before = index_path.read_bytes()
        # The generated index has no user-authored layout to retain. Use the
        # regular cached safe loader rather than materializing a large RT AST.
        index_document = load_yaml_string(index_before.decode("utf-8"))
        if not isinstance(index_document, dict) or not isinstance(index_document.get("items"), list):
            raise ValueError("索引数据库格式无效")
        if _path_key(index_document.get("catalog_root")) != _path_key(settings.filesystem_root.resolve()):
            raise ValueError("索引不属于当前作品数据库，拒绝自动修复")
        works = links._load_catalog_works(settings)
        items = [dict(item) for item in index_document["items"] if isinstance(item, dict)]
        resource_roots = links.resource_roots()
        evidence, shortcut_snapshot, evidence_issues = shortcut_evidence(items, links.shortcut_roots())
        plan = plan_catalog_bindings(works, items, resource_roots=resource_roots, shortcut_targets=evidence)
        # Any invalid/unreadable shortcut explicitly blocks repair of its work,
        # even when an index-only candidate would otherwise look plausible.
        bad_entries = {issue["entry_key"] for issue in evidence_issues}
        bad_works = {(item.get("yaml_source_rel"), item.get("index_in_file")) for item in items if item.get("entry_key") in bad_entries}
        bad_works.update((issue.get("yaml_source_rel"), issue.get("index_in_file"))
                         for issue in plan["issues"] if issue.get("blocking"))
        mappings = [mapping for mapping in plan["mappings"] if (mapping["yaml_source_rel"], mapping["index_in_file"]) not in bad_works]
        by_relative = {links._catalog_relpath(settings, path): path for path in paths}
        work_names = {(work["yaml_source_rel"], work["index_in_file"]): work["name"] for work in works}
        changes: list[dict] = []
        staged: list[FileWrite] = []
        modified: set[str] = set()
        for mapping in mappings:
            path = by_relative[mapping["yaml_source_rel"]]
            before, document = documents[str(path)]
            field_changes = fill_mapping(document, mapping)
            if field_changes:
                modified.add(str(path))
                changes.append({**mapping, "name": work_names[(mapping["yaml_source_rel"], mapping["index_in_file"])],
                                "changes": field_changes})
        for path_key in sorted(modified):
            before, document = documents[path_key]
            after = serialize(document, before)
            load_jp_tv_entries_from_yaml(load_yaml_string(after.decode("utf-8")))
            staged.append(FileWrite(Path(path_key), after, before))
        virtual = links._merge_ui_mappings(works, links._ui_mapping_items({"works": mappings}))
        # Rebuild only the derived index, without heuristic resource candidates.
        # Existing complete catalog paths are authoritative; old index targets
        # are retained solely for unresolved records and are not written to YAML.
        previous_items = links._matching_previous_index_items(
            virtual, index_document, catalog_root=links._settings_catalog_root_key(settings))
        derived = links._index_entries_from_works(virtual, previous_items=previous_items,
                    use_resource_index=False, prefer_catalog_target=True)
        index_after_document = copy.deepcopy(index_document)
        index_after_document["items"] = derived
        index_after_document["source"] = "collection-detail catalog yaml"
        material = {"sources": {path: digest(before) for path, (before, _) in sorted(documents.items())},
                    "index_sha256": digest(index_before), "mappings": mappings,
                    "resource_roots": [str(root) for root in resource_roots], "shortcuts": shortcut_snapshot,
                    "derived_items": derived}
        plan_sha256 = digest(json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        if expect_plan_sha256 and plan_sha256 != expect_plan_sha256:
            raise ValueError("数据库、快捷方式或修复计划已变化，未写入；请重新预览")
        result = {"ok": True, "applied": False, "plan_sha256": plan_sha256,
                  "files_scanned": len(paths), "works_scanned": len(works), "index_items": len(items),
                  "shortcuts_verified": len(shortcut_snapshot), "files_changed": len(staged),
                  "works_changed": len(changes), "fields_changed": sum(len(item["changes"]) for item in changes),
                  "press_bindings_added": sum(len(item["press"]) for item in changes),
                  "summary": plan["summary"], "changes": changes,
                  "issues": plan["issues"] + evidence_issues, "backups": [], "files": []}
        if apply:
            for path, (before, _) in documents.items():
                if Path(path).read_bytes() != before:
                    raise ValueError(f"数据库在扫描后发生变化：{path}")
            if index_path.read_bytes() != index_before:
                raise ValueError("索引在扫描后发生变化")
            for snapshot in shortcut_snapshot:
                info = Path(snapshot["path"]).stat()
                if (info.st_mtime_ns, info.st_size) != (snapshot["mtime_ns"], snapshot["size"]):
                    raise ValueError("快捷方式在扫描后发生变化，未写入")
            index_after_document["generated_at"] = datetime.now().isoformat(timespec="seconds")
            index_after = dump_yaml_string(index_after_document).encode("utf-8")
            staged.append(FileWrite(index_path, index_after, index_before))
            history = history_catalog_root(settings) / "binding-repair"
            committed = []
            for write in staged:
                backup = history / history_snapshot_name(write.target)
                result["backups"].append(str(backup))
                result["files"].append({"path": str(write.target), "before_sha256": digest(write.previous),
                                        "after_sha256": digest(write.content), "backup": str(backup)})
                committed.append(FileWrite(write.target, write.content, write.previous, backup))
            commit_file_writes(committed)
            result["applied"] = True
        return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--apply", action="store_true")
    args_parser.add_argument("--expect-plan-sha256")
    args = args_parser.parse_args()
    result = repair(load_jp_tv_browse_settings(), apply=args.apply, expect_plan_sha256=args.expect_plan_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
