"""Editable collection completion records for the JP TV browse app."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.layout import feature_config_path, feature_data_root
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml

_FINISH_DIR_ENV = "JP_TV_COLLECTION_FINISH_DIR"
_INFO_PATH_ENV = "JP_TV_COLLECTION_INFO_PATH"
_RECORDS_PATH_ENV = "JP_TV_COLLECTION_RECORDS_PATH"
_DEFAULT_FINISH_DIR = r"E:\LinkVideo\[ACG] Japan\Finish"
_YEAR_NAME_RE = re.compile(r"^\[?((?:19|20)\d{2}|(?:19|20)\dX)\]?$", re.IGNORECASE)


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
    cfg_paths = _feature_config_paths()
    raw = (
        os.environ.get(_FINISH_DIR_ENV, "").strip()
        or _str_or_blank(cfg_paths.get("finish_dir"))
        or _DEFAULT_FINISH_DIR
    )
    return Path(raw).expanduser()


def collection_records_path(settings: JpTvBrowseSettings) -> Path:
    raw = os.environ.get(_INFO_PATH_ENV, "").strip() or os.environ.get(_RECORDS_PATH_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    if _use_workspace_collection_info_config(settings):
        cfg_paths = _feature_config_paths()
        cfg_db = _str_or_blank(cfg_paths.get("database_path"))
        if cfg_db:
            return Path(cfg_db).expanduser().resolve()
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
            return Path(cfg_history).expanduser().resolve()
        return (feature_data_root("collection-info") / "history").resolve()
    if settings.filesystem_root is None:
        raise ValueError("missing paths.filesystem_root")
    return (settings.filesystem_root.resolve().parent / "History" / "CollectionInfo").resolve()


def _snapshot_name(path: Path, *, now: datetime | None = None) -> str:
    dt = now or datetime.now()
    return f"{path.stem}__saved-{dt:%Y%m%d-%H%M%S}{path.suffix}"


def collection_year_key_from_dirname(name: str) -> str | None:
    m = _YEAR_NAME_RE.match(name.strip())
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
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for child in root.iterdir():
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
    if not path.is_file():
        return [_default_record()]
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
    years: list[dict[str, str]] = []
    try:
        years = scan_finish_years(finish_dir)
    except OSError as exc:
        warning = f"cannot scan finish dir: {exc}"
    allowed = {it["key"] for it in years} if years else None
    path = collection_records_path(settings)
    records = _load_records_from_path(path, allowed_years=allowed)
    return {
        "ok": True,
        "path": str(path),
        "finish_dir": str(finish_dir),
        "warning": warning,
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
    allowed_years: set[str] | None = None
    try:
        years = scan_finish_years(finish_dir)
    except OSError:
        years = []
    if years:
        allowed_years = {it["key"] for it in years}
    records = _records_from_body(body, allowed_years=allowed_years)
    target = collection_records_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    history_file = ""
    if target.is_file():
        hist_root = collection_records_history_root(settings)
        hist_root.mkdir(parents=True, exist_ok=True)
        history_file = _snapshot_name(target)
        shutil.copy2(target, hist_root / history_file)
    payload = {
        "version": 1,
        "finish_dir": str(finish_dir),
        "records": records,
    }
    target.write_text(dump_yaml_string(payload), encoding="utf-8")
    return {
        "path": str(target),
        "history_file": history_file,
        "records": records,
    }
