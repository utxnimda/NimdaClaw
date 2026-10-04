"""Shared, source-versioned full catalog editing for every feature.

This layer plans edits; the collection-list saver remains the single writer.
Filesystem paths in a draft need not exist yet: the organizer moves them only
after confirmation, and then persists the already validated record here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from collection_detail.catalog_repository import CatalogRepository, work_key
from collection_detail.save import (
    browse_save_yaml_from_ui_body, catalog_record_sha256, catalog_relpath_for_new_work,
    catalog_write_transaction, normalize_raw_record, preview_save_yaml_from_ui_body,
)
from collection_detail.work_detail import json_work_record, raw_work_records
from work_catalog_yaml.input_validation import parse_record_index
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.validate import entry_air_dates, entry_country_slug, load_jp_tv_entries_from_yaml
from work_catalog_yaml.operation_progress import report_exception
from work_catalog_yaml.yaml_io import load_yaml_string


def resolve_record_directory(record: dict[str, Any], press_path: str | None = None) -> Path:
    """Resolve authoritative absolute/legacy-relative DB bindings lexically.

    Accept either a catalog_records projection or its complete raw record.
    This is a path calculation, not a filesystem safety/existence assertion:
    callers still inspect ordinary directories immediately before disk access.
    """
    from collection_detail.catalog_repository import resolve_catalog_directory
    from collection_detail.link_index import _legacy_media_root

    if not isinstance(record, dict):
        raise ValueError("作品记录必须为对象")
    raw = record.get("record", record)
    path = record.get("path")
    # A raw record's similarly named extension field is not its work binding.
    # When complete source data is available, collection-type is authoritative.
    if isinstance(raw, dict) and "attributes" in raw:
        path = ""
        attributes = raw.get("attributes", [])
        if isinstance(attributes, list):
            for attribute in attributes:
                if isinstance(attribute, dict) and attribute.get("type") == "collection-type":
                    data = attribute.get("data")
                    path = data.get("path", "") if isinstance(data, dict) else ""
                    break
    return resolve_catalog_directory(_legacy_media_root(resolve_links=False), path, press_path, resolve_links=False)


def catalog_records(settings: JpTvBrowseSettings) -> list[dict[str, Any]]:
    """Return a coherent snapshot with complete raw records and stable identities."""
    repository = CatalogRepository(settings)
    sources = [repository.read_source(path) for path in repository.catalog_paths()]
    projections = {item["work_key"]: item for item in repository.load_works(
        catalog_overrides={source.path: source.data for source in sources})}
    result = []
    for source in sources:
        for index, raw in enumerate(raw_work_records(load_yaml_string(source.data.decode("utf-8")))):
            record = json_work_record(raw)
            key = work_key(source.relative_path, index)
            ref = {"yaml_source_rel": source.relative_path, "index_in_file": index,
                   "source_sha256": source.source_sha256, "record_sha256": catalog_record_sha256(record)}
            result.append({**projections[key], "ref": ref, "record": record,
                           "record_sha256": ref["record_sha256"]})
    return result


def _prepare_edits(edits: Any, current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(edits, list) or len(edits) > 200:
        raise ValueError("edits 必须为数组，单次最多 200 条作品记录")
    by_ref = {(item["ref"]["yaml_source_rel"], item["ref"]["index_in_file"]): item for item in current}
    normalized = []
    changes = []
    rows = []
    new_rows = []
    seen = set()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edits[{index}] 必须为对象")
        ref = edit.get("ref")
        previous = None
        normalized_ref = None
        if ref is not None:
            if not isinstance(ref, dict):
                raise ValueError(f"edits[{index}].ref 必须为对象或 null")
            relative = ref.get("yaml_source_rel")
            position = parse_record_index(ref.get("index_in_file"))
            if not isinstance(relative, str):
                raise ValueError("ref.yaml_source_rel 必须为当前作品数据库中的完整相对路径")
            identity = (relative, position)
            if identity in seen:
                raise ValueError(f"同一批次重复修改作品记录：{relative}#{position}")
            seen.add(identity)
            existing = by_ref.get(identity)
            if existing is None:
                raise ValueError(f"作品记录不在当前数据库中：{relative}#{position}")
            version = existing["ref"]
            expected = ref.get("source_sha256")
            if not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise ValueError("ref 缺少有效 source_sha256，请重新加载或预览")
            if ref.get("record_sha256") is not None and ref["record_sha256"] != version["record_sha256"]:
                raise ValueError(f"作品记录已变化，请重新预览：{relative}#{position}")
            if expected != version["source_sha256"] and ref.get("record_sha256") != version["record_sha256"]:
                raise ValueError(f"数据库文件已变化，请重新预览：{relative}")
            previous = existing["record"]
            normalized_ref = dict(version)
        try:
            record = normalize_raw_record(edit.get("record"), previous=previous)
        except (ValueError, TypeError) as exc:
            identity_text = f"{normalized_ref['yaml_source_rel']}#{normalized_ref['index_in_file']}" if normalized_ref else "新增作品"
            raise ValueError(f"edits[{index}]（{identity_text}）：{exc}") from exc
        normalized.append({"ref": normalized_ref, "record": record})
        action = "create" if ref is None else "unchanged" if record == previous else "update"
        changes.append({"action": action, "ref": normalized_ref, "before": previous, "after": record})
        if action == "create":
            new_rows.append({"raw_record": record})
        elif action == "update":
            rows.append({**normalized_ref, "raw_record": record})
    return normalized, changes, {"rows": rows, "new_rows": new_rows}


def preview_catalog_edits(edits: Any, *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    """No locks, directories, backups or data writes are produced by a preview."""
    try:
        normalized, changes, body = _prepare_edits(edits, catalog_records(settings))
        if body["rows"] or body["new_rows"]:
            preview_save_yaml_from_ui_body(body, settings=settings)
        return {"edits": normalized, "changes": changes, "issues": []}
    except (OSError, ValueError, TypeError) as exc:
        return {"edits": [], "changes": [], "issues": [{"code": "catalog-edit-invalid", "blocking": True,
                "stage": "数据库修改预检", "action": "校验完整作品记录与源版本", "message": str(exc),
                "reason": str(exc), "error_type": type(exc).__name__}]}


def apply_catalog_edits(edits: Any, *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    """Commit via the original saver; post-commit refresh cannot imply rollback."""
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，禁止写盘")
    committed = False
    writes: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    try:
        with catalog_write_transaction(settings.filesystem_root):
            current = catalog_records(settings)
            normalized, changes, body = _prepare_edits(edits, current)
            wanted = []
            counts: dict[str, int] = {}
            for item in current:
                relative = item["ref"]["yaml_source_rel"]
                counts[relative] = max(counts.get(relative, 0), item["ref"]["index_in_file"] + 1)
            for edit in normalized:
                if edit["ref"] is not None:
                    wanted.append((edit["ref"]["yaml_source_rel"], edit["ref"]["index_in_file"]))
                else:
                    entry = load_jp_tv_entries_from_yaml([edit["record"]])[0]
                    relative = catalog_relpath_for_new_work(entry_country_slug(entry), entry_air_dates(entry)[0])
                    position = counts.get(relative, 0)
                    wanted.append((relative, position))
                    counts[relative] = position + 1
            if body["rows"] or body["new_rows"]:
                saved = browse_save_yaml_from_ui_body(body, settings=settings)
                committed = True
                writes = [{"path": str(path), "history_name": history} for path, history in saved]
            else:
                committed = True
            after = {(item["ref"]["yaml_source_rel"], item["ref"]["index_in_file"]): item for item in catalog_records(settings)}
            records = [after[key] for key in wanted]
            return {"db_committed": True, "records": records, "writes": writes, "issues": [], "changes": changes}
    except Exception as exc:
        if not committed:
            # A storage rollback conflict is not a clean failure; callers must
            # not roll media back over a possibly committed DB without inspection.
            from work_catalog_yaml.persistence import PersistenceRollbackError
            if isinstance(exc, PersistenceRollbackError):
                exc.db_committed = None
            raise
        report_exception(exc, context={"stage": "数据库保存后刷新", "action": "读取已提交记录", "path": str(settings.filesystem_root)})
        return {"db_committed": True, "records": records, "writes": writes,
                "issues": [{"code": "catalog-post-commit-refresh-failed", "level": "warning", "blocking": False,
                            "message": "数据库已保存，但刷新结果失败；请重新读取后处理快捷方式", "reason": str(exc)}]}
