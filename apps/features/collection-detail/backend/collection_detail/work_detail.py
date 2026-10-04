"""Read-only, source-versioned access to an entire catalog work record."""
from __future__ import annotations

from datetime import date, datetime

import math
from pathlib import Path, PureWindowsPath
import re
from typing import Any

from work_catalog_yaml.input_validation import parse_record_index
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings, jp_tv_yaml_catalog_relpath
from work_catalog_yaml.yaml_io import load_yaml_string
from collection_detail.catalog_repository import CatalogRepository


class CatalogDetailStaleError(ValueError):
    """The displayed row no longer identifies a record in the current file."""


def raw_work_records(document: Any) -> list[Any]:
    """Keep raw mappings instead of the normalized, intentionally lossy presenter model."""
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("works", "entries"):
            records = document.get(key)
            if isinstance(records, list):
                return records
    raise ValueError("YAML 根须为作品数组或含 works / entries 的对象")


def json_work_record(record: Any) -> dict[str, Any]:
    """Preserve every field, converting YAML dates to ISO text without silent omission.

    Unsupported values, non-string keys and recursive aliases produce a useful
    error rather than dropping fields or returning JSON that browsers cannot read.
    """
    if not isinstance(record, dict):
        raise ValueError("作品记录须为对象")
    ancestors: set[int] = set()

    def convert(value: Any, location: str, depth: int) -> Any:
        if depth > 80:
            raise ValueError(f"{location}：嵌套层级过深，无法显示完整 JSON")
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{location}：非有限数值无法表示为 JSON")
            return value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in ancestors:
                raise ValueError(f"{location}：循环 YAML 引用无法表示为 JSON")
            ancestors.add(identity)
            try:
                if isinstance(value, list):
                    return [convert(item, f"{location}[{i}]", depth + 1) for i, item in enumerate(value)]
                converted = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{location}：非字符串字段名 {key!r} 无法无损显示为 JSON")
                    converted[key] = convert(item, f"{location}.{key}", depth + 1)
                return converted
            finally:
                ancestors.remove(identity)
        raise ValueError(f"{location}：YAML 类型 {type(value).__name__} 无法无损显示为 JSON")

    return convert(record, "作品", 0)


def _exact_catalog_relpath(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("yaml_source_rel 须为当前 DB 列表中的相对路径")
    relative = raw.strip().replace("\\", "/")
    if (
        relative.startswith("/")
        or PureWindowsPath(relative).drive
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or any(ord(char) < 32 for char in relative)
    ):
        raise ValueError("yaml_source_rel 非法：须为当前 DB 列表中的完整相对路径")
    return relative


def work_detail_payload(body: dict[str, Any], settings: JpTvBrowseSettings) -> dict[str, Any]:
    relative = _exact_catalog_relpath(body.get("yaml_source_rel"))
    index = parse_record_index(body.get("index_in_file"))
    expected_hash = body.get("source_sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        raise ValueError("缺少有效的 source_sha256，请重新加载收集列表后查看")

    # Do not resolve arbitrary client paths or fall back to their basename.
    # Membership and containment must both be established from server settings.
    selected: tuple[str, Path] | None = None
    for allowed in settings.resolved_catalog_yaml_paths:
        candidate = Path(allowed).resolve()
        try:
            canonical_relative = jp_tv_yaml_catalog_relpath(settings, candidate)
        except (OSError, ValueError):
            continue
        if canonical_relative == relative:
            selected = (canonical_relative, candidate)
            break
    if selected is None:
        raise ValueError(f"不在当前 DB 数据列表中：{relative}")
    canonical_relative, source_path = selected
    source = CatalogRepository(settings).read_source(source_path)
    source_bytes = source.data
    actual_hash = source.source_sha256
    if expected_hash.lower() != actual_hash:
        raise CatalogDetailStaleError("作品数据库已变化，请重新加载收集列表后查看，避免显示错误的作品")
    try:
        document = load_yaml_string(source_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"作品 YAML 解析失败：{exc}") from exc
    records = raw_work_records(document)
    if index >= len(records):
        raise ValueError("index_in_file 超出作品范围，请重新加载收集列表后查看")
    return {
        "ok": True,
        "record": json_work_record(records[index]),
        "source": {
            "yaml_source_rel": canonical_relative,
            "index_in_file": index,
            "sha256": actual_hash,
        },
    }
