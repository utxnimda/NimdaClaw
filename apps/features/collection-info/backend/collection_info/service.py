"""Editable collection completion records for the JP TV browse app."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.layout import feature_config_path, feature_data_root, resolve_workspace_path
from work_catalog_yaml.persistence import FileWrite, commit_file_writes, directory_write_transaction, history_snapshot_name
from work_catalog_yaml.operation_progress import operation_context, report_exception, report_progress
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml

_FINISH_DIR_ENV = "JP_TV_COLLECTION_FINISH_DIR"
_INFO_PATH_ENV = "JP_TV_COLLECTION_INFO_PATH"
_RECORDS_PATH_ENV = "JP_TV_COLLECTION_RECORDS_PATH"
_DEFAULT_FINISH_DIR = r"E:\LinkVideo\[ACG] Japan\Finish"
_YEAR_NAME_RE = re.compile(r"((?:19|20)\d{2}|(?:19|20)\dX)", re.IGNORECASE)


def _str_or_blank(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _feature_config_paths() -> dict[str, Any]:
    cfg = feature_config_path("collection-info")
    if not cfg.is_file():
        return {}
    raw = load_yaml(cfg)
    if not isinstance(raw, dict):
        return {}
    paths = raw.get("paths")
    return paths if isinstance(paths, dict) else {}


def _use_workspace_collection_info_config(settings: JpTvBrowseSettings) -> bool:
    if settings.filesystem_root is None:
        return True
    feature_db = (feature_data_root("collection-detail") / "db").resolve()
    try:
        settings.filesystem_root.resolve().relative_to(feature_db)
        return True
    except ValueError:
        return False


def collection_finish_dir() -> Path:
    override = os.environ.get(_FINISH_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    cfg_paths = _feature_config_paths()
    raw = _str_or_blank(cfg_paths.get("finish_dir")) or _DEFAULT_FINISH_DIR
    return resolve_workspace_path(raw)


def collection_records_path(settings: JpTvBrowseSettings) -> Path:
    raw = os.environ.get(_INFO_PATH_ENV, "").strip() or os.environ.get(_RECORDS_PATH_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    if _use_workspace_collection_info_config(settings):
        cfg_paths = _feature_config_paths()
        cfg_db = _str_or_blank(cfg_paths.get("database_path"))
        if cfg_db:
            return resolve_workspace_path(cfg_db)
        modern = feature_data_root("collection-info") / "db" / "collection-info.yaml"
    else:
        modern = settings.filesystem_root.resolve().parent / "CollectionInfo" / "collection-info.yaml"
    data_root = settings.filesystem_root.resolve().parent if settings.filesystem_root is not None else Path()
    legacy = data_root / "CollectionInfo" / "collection-info.yaml"
    older = data_root / "CollectionRecords" / "collection-records.yaml"
    if not modern.is_file() and legacy.is_file():
        return legacy.resolve()
    if not modern.is_file() and older.is_file():
        return older.resolve()
    return modern.resolve()


def collection_records_history_root(settings: JpTvBrowseSettings) -> Path:
    if _use_workspace_collection_info_config(settings):
        cfg_paths = _feature_config_paths()
        cfg_history = _str_or_blank(cfg_paths.get("history_root"))
        if cfg_history:
            return resolve_workspace_path(cfg_history)
        return (feature_data_root("collection-info") / "history").resolve()
    if settings.filesystem_root is None:
        raise ValueError("missing paths.filesystem_root")
    return (settings.filesystem_root.resolve().parent / "History" / "CollectionInfo").resolve()


def collection_year_key_from_dirname(name: str) -> str | None:
    candidate = name.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    m = _YEAR_NAME_RE.fullmatch(candidate)
    if not m:
        return None
    return m.group(1).upper()


def _year_sort_key(key: str) -> tuple[int, str]:
    k = key.upper()
    if k.endswith("X"):
        return (int(k[:3]) * 10, k)
    return (int(k), k)


def scan_finish_years(finish_dir: str | Path) -> list[dict[str, str]]:
    root = Path(finish_dir).expanduser()
    report_progress("扫描收集完成年份目录", detail=str(root))
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for child_index, child in enumerate(root.iterdir()):
        if child_index % 100 == 0:
            report_progress("读取收集年份目录", completed=child_index, unit="目录项", detail=str(child))
        if not child.is_dir():
            continue
        key = collection_year_key_from_dirname(child.name)
        if key is None or key in seen:
            continue
        seen.add(key)
        entries.append({"key": key, "label": child.name, "path_name": child.name})
    entries.sort(key=lambda it: _year_sort_key(it["key"]))
    return entries


def _default_record() -> dict[str, Any]:
    return {
        "domain": "animation",
        "country": "japan",
        "release_type": "tv",
        "completed_years": [],
    }


def _clean_years(raw: Any, *, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = collection_year_key_from_dirname(str(item).strip())
        if not s or s in seen:
            continue
        if allowed is not None and s not in allowed:
            continue
        seen.add(s)
        out.append(s)
    out.sort(key=_year_sort_key)
    return out


def _clean_scalar(raw: Any, fallback: str) -> str:
    s = raw.strip() if isinstance(raw, str) else ""
    return s or fallback


def normalize_collection_record(
    raw: Any,
    *,
    allowed_years: set[str] | None = None,
) -> dict[str, Any]:
    base = _default_record()
    if not isinstance(raw, dict):
        raw = {}
    years_raw = raw.get("completed_years")
    if years_raw is None:
        years_raw = raw.get("finished_years")
    if years_raw is None:
        years_raw = raw.get("years")
    return {
        "domain": _clean_scalar(raw.get("domain"), str(base["domain"])),
        "country": _clean_scalar(raw.get("country"), str(base["country"])),
        "release_type": _clean_scalar(raw.get("release_type"), str(base["release_type"])),
        "completed_years": _clean_years(years_raw, allowed=allowed_years),
    }


def _load_records_from_path(path: Path, *, allowed_years: set[str] | None) -> list[dict[str, Any]]:
    report_progress("读取收集情况数据库", detail=str(path), context={"stage": "读取收集情况", "action": "读取数据库文件", "source_path": str(path)})
    if not path.is_file():
        return [_default_record()]
    with operation_context(stage="读取收集情况", action="解析收集情况YAML", source_path=str(path)):
        raw = load_yaml(path)
    records_raw: Any
    if isinstance(raw, dict):
        records_raw = raw.get("records")
    else:
        records_raw = raw
    if not isinstance(records_raw, list):
        return [_default_record()]
    records = [
        normalize_collection_record(item, allowed_years=allowed_years)
        for item in records_raw
    ]
    return records or [_default_record()]


def collection_records_payload(settings: JpTvBrowseSettings) -> dict[str, Any]:
    finish_dir = collection_finish_dir()
    warning = ""
    issues: list[dict[str, Any]] = []
    years: list[dict[str, str]] = []
    try:
        with operation_context(stage="读取收集情况", action="扫描完成年份目录", source_path=str(finish_dir)):
            years = scan_finish_years(finish_dir)
    except OSError as exc:
        warning = f"cannot scan finish dir: {exc}"
        context = {"stage": "读取收集情况", "action": "扫描完成年份目录", "source_path": str(finish_dir)}
        report_exception(exc, context=context)
        issues.append({**context, "path": str(finish_dir), "code": "finish-directory-unavailable",
                       "message": "无法读取完成年份目录；仍保留并显示数据库中已有收集记录",
                       "reason": str(exc), "error_type": type(exc).__name__})
    path = collection_records_path(settings)
    # Directory availability is a UI hint, not authority over saved history.
    # A disconnected disk or removed year folder must not silently erase data.
    records = _load_records_from_path(path, allowed_years=None)
    report_progress("收集情况读取完成", completed=len(records), total=len(records), unit="记录")
    return {
        "ok": True,
        "path": str(path),
        "finish_dir": str(finish_dir),
        "warning": warning,
        "issues": issues,
        "years": years,
        "records": records,
    }


def _records_from_body(body: dict[str, Any], *, allowed_years: set[str] | None) -> list[dict[str, Any]]:
    raw = body.get("records")
    if raw is None and "record" in body:
        raw = [body.get("record")]
    if not isinstance(raw, list):
        raise ValueError("records must be a list")
    records = [
        normalize_collection_record(item, allowed_years=allowed_years)
        for item in raw
        if isinstance(item, dict)
    ]
    if not records:
        raise ValueError("records must contain at least one record")
    return records


def save_collection_records_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    finish_dir = collection_finish_dir()
    target = collection_records_path(settings)
    with operation_context(stage="保存收集情况", action="校验记录", target_path=str(target)):
        records = _records_from_body(body, allowed_years=None)
    with operation_context(stage="保存收集情况", action="准备数据库目录", target_path=str(target.parent)):
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "finish_dir": str(finish_dir),
        "records": records,
    }
    report_progress("等待收集情况写入锁", detail=str(target), context={"stage": "保存收集情况", "action": "等待写入锁", "target_path": str(target)})
    with directory_write_transaction(target.parent, lock_filename=".nimda-collection-info.lock"):
        with operation_context(stage="保存收集情况", action="读取修改前快照", source_path=str(target)):
            previous = target.read_bytes() if target.is_file() else None
        history_file = history_snapshot_name(target) if previous is not None else ""
        history_path = collection_records_history_root(settings) / history_file if history_file else None
        report_progress("备份并保存收集情况", detail=str(target), context={"stage": "保存收集情况", "action": "写入历史备份与数据库", "target_path": str(target), "history_path": str(history_path or "")})
        with operation_context(stage="保存收集情况", action="原子保存，失败则回滚", target_path=str(target), history_path=str(history_path or "")):
            commit_file_writes([FileWrite(target, dump_yaml_string(payload).encode("utf-8"), previous, history_path)])
    report_progress("收集情况保存完成", completed=len(records), total=len(records), unit="记录")
    return {
        "path": str(target),
        "history_file": history_file,
        "records": records,
    }
