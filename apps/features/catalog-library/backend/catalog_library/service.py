"""Read local records and delegate every work mutation to the shared catalog writer.

Media directories and external providers are deliberately absent from this layer.
Classification definitions have their own versioned, atomic storage; membership
lives only in authoritative work records, not in a second membership database.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any
import unicodedata

from collection_detail.catalog_edit_service import catalog_records, preview_catalog_edits, apply_catalog_edits
from collection_detail.catalog_repository import CatalogRepository
from work_catalog_yaml.layout import feature_data_root
from work_catalog_yaml.input_validation import parse_record_index
from work_catalog_yaml.persistence import FileWrite, commit_file_writes, directory_write_transaction, history_snapshot_name
from .model import CLASSIFICATION_ID, WORK_ID, SCHEMA_VERSION, classification_definition, new_work_id, validate_extensions
from .assets import cover_url

_CACHE: OrderedDict[tuple, list[dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = threading.RLock()


def _records(settings) -> list[dict[str, Any]]:
    def source_key():
        # Refresh membership as well as contents: another app instance can
        # create/remove a year file while the complete snapshot is being read.
        return tuple((str(path), hashlib.sha256(path.read_bytes()).hexdigest())
                     for path in CatalogRepository(settings).catalog_paths())
    key = source_key()
    # Enum labels are part of the projection too. Never cache mutable source
    # bytes based on path alone, or keep failed/partial loads in the cache.
    key += ((json.dumps({name: dict(labels) for name, labels in settings.enum_labels.items()}, ensure_ascii=False, sort_keys=True),),)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    records = catalog_records(settings)
    known = set()
    for item in records:
        identity = item["record"].get("id")
        if identity:
            if not isinstance(identity, str) or not WORK_ID.fullmatch(identity):
                raise ValueError(f"作品稳定 ID 无效：{item['work_key']}")
            if identity in known:
                raise ValueError(f"数据库存在重复作品稳定 ID：{identity}")
            known.add(identity)
    # A concurrent writer between fingerprinting and parsing must not cause a
    # newer parsed snapshot to be cached under an older content version.
    if source_key() == key[:-1]:
        with _CACHE_LOCK:
            _CACHE[key] = records
            while len(_CACHE) > 4:
                _CACHE.popitem(last=False)
    return records


def _data_root(data_root=None) -> Path:
    root = Path(data_root) if data_root is not None else feature_data_root("catalog-library")
    return root.expanduser().absolute()


def _safe_directory(path: Path) -> Path:
    """Reject link/junction escapes before either reading or writing library data."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError(f"作品库数据目录不允许符号链接或联接：{candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"作品库数据目录不是普通目录：{candidate}")
    return path


def _classification_state(data_root=None) -> tuple[Path, bytes | None, dict]:
    path = _safe_directory(_data_root(data_root) / "db") / "classifications.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("分类字典不是普通文件")
    content = path.read_bytes() if path.is_file() else None
    state = json.loads(content) if content is not None else {"schema_version": 1, "items": []}
    if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("items"), list):
        raise ValueError("分类字典格式或版本无效")
    identities = set()
    for item in state["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not CLASSIFICATION_ID.fullmatch(item["id"]):
            raise ValueError("分类字典中存在无效 ID")
        if item["id"] in identities:
            raise ValueError("分类字典中存在重复 ID")
        identities.add(item["id"])
        classification_definition(item, existing=item)
    return path, content, state


def classifications_payload(body=None, *, settings=None, data_root=None) -> dict:
    _path, content, state = _classification_state(data_root)
    return {"ok": True, "items": deepcopy(state["items"]), "revision": hashlib.sha256(content or b"").hexdigest()}


def save_classification(body, *, settings=None, data_root=None) -> dict:
    if not isinstance(body, dict):
        raise ValueError("分类保存参数必须为对象")
    path, _content, _state = _classification_state(data_root)
    history_root = _safe_directory(_data_root(data_root) / "history")
    path.parent.mkdir(parents=True, exist_ok=True)
    with directory_write_transaction(path.parent):
        path, content, state = _classification_state(data_root)
        if body.get("revision") != hashlib.sha256(content or b"").hexdigest():
            raise ValueError("分类字典已变化，请重新加载后保存")
        existing = next((item for item in state["items"] if item["id"] == body.get("id")), None)
        if body.get("id") is not None and existing is None:
            raise ValueError("指定分类不存在")
        item = classification_definition(body, existing=existing)
        if any(other["id"] != item["id"] and other["type"] == item["type"] and _normal(other["name"]) == _normal(item["name"])
               for other in state["items"]):
            raise ValueError("相同类型中已存在同名分类")
        updated = deepcopy(state)
        if existing:
            updated["items"] = [item if other["id"] == item["id"] else other for other in updated["items"]]
        else:
            updated["items"].append(item)
        payload = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if payload != content:
            history = history_root / history_snapshot_name(path) if content is not None else None
            commit_file_writes([FileWrite(path, payload, content, history)])
    return {**classifications_payload(data_root=data_root), "item": item}


def _normal(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _memberships(record, definitions, warnings=None) -> list[dict]:
    values = record.get("classifications", [])
    warnings = [] if warnings is None else warnings
    if not isinstance(values, list):
        warnings.append("旧分类格式不标准，已忽略展示；原始记录保留。")
        return []
    result = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("value_id"), str):
            warnings.append("旧分类项缺少标识，已忽略展示；原始记录保留。")
            continue
        projection = {**deepcopy(item), "id": item["value_id"], "name": definitions.get(item["value_id"], {}).get("name", item["value_id"]),
                      "kind": item.get("type") if isinstance(item.get("type"), str) else "series", "missing": item["value_id"] not in definitions}
        order = projection.get("order")
        if "order" in projection and not (type(order) is int or (type(order) is float and math.isfinite(order))):
            projection.pop("order")
            warnings.append("旧分类顺序无效，已按未排序展示；原始记录保留。")
        result.append(projection)
    return result


def _extension_projection(record, *, include_chapters=True) -> tuple[dict, list, list[str]]:
    """Old extensions were free-form. Degrade views without rewriting records."""
    warnings = []
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        warnings.append("旧资料格式不标准，已忽略展示；原始记录保留。")
    metadata = dict(metadata)
    fields = [("summary", str, ""), ("aliases", list, [])]
    if include_chapters:
        fields.append(("episodes", list, []))
    for key, expected, default in fields:
        if key not in metadata:
            continue
        value = metadata[key]
        if not isinstance(value, expected):
            metadata[key] = default
            warnings.append(f"旧资料 {key} 格式不标准，已忽略展示；原始记录保留。")
        elif key in {"aliases", "episodes"}:
            wanted = str if key == "aliases" else dict
            metadata[key] = [item for item in value if isinstance(item, wanted)]
            if len(metadata[key]) != len(value):
                warnings.append(f"旧资料 {key} 含无效项，已忽略展示；原始记录保留。")
    sources = record.get("source_refs", [])
    if not isinstance(sources, list):
        sources = []
        warnings.append("旧来源关联格式不标准，已忽略展示；原始记录保留。")
    elif any(not isinstance(item, dict) for item in sources):
        sources = [item for item in sources if isinstance(item, dict)]
        warnings.append("旧来源关联含无效项，已忽略展示；原始记录保留。")
    return metadata, deepcopy(sources), warnings


def _item(item, definitions) -> dict:
    record = item["record"]
    metadata, sources, warnings = _extension_projection(record, include_chapters=False)
    memberships = _memberships(record, definitions, warnings)
    return {key: deepcopy(value) for key, value in item.items() if key not in {"record", "press", "record_sha256"}} | {
        "id": record.get("id") or "legacy:" + item["work_key"], "stable_id": bool(record.get("id")),
        "summary": str(metadata.get("summary", ""))[:600], "aliases": deepcopy(metadata.get("aliases", [])),
        # No remote image URL may escape into the offline browsing surface.
        "cover_url": cover_url(metadata.get("cover")), "press_count": len(item.get("press", [])),
        "classifications": memberships, "source_count": len(sources), "warnings": list(dict.fromkeys(warnings)),
    }


def _number(raw, default, maximum, label):
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{label} 必须为正整数")
    try:
        value = int(raw)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} 必须为正整数") from exc
    if str(value) != str(raw) or not 1 <= value <= maximum:
        raise ValueError(f"{label} 必须在 1～{maximum} 之间")
    return value


def browse(body=None, *, settings, data_root=None) -> dict:
    body = body or {}
    if not isinstance(body, dict):
        raise ValueError("查询参数必须为对象")
    definitions_payload = classifications_payload(data_root=data_root)
    definitions = {item["id"]: item for item in definitions_payload["items"]}
    records = _records(settings)
    items = [_item(item, definitions) for item in records]
    domain_counts = Counter(item["domain"] for item in items)
    year_counts = Counter(item["year"] for item in items if item["year"])
    classification_counts = Counter(tag["value_id"] for item in items for tag in item["classifications"])
    labels = {item["domain"]: item["domain_label"] for item in items}
    facets = {"domains": [{"value": key, "label": labels.get(key, key), "count": count} for key, count in sorted(domain_counts.items())],
              "years": [{"value": key, "count": count} for key, count in sorted(year_counts.items(), reverse=True)],
              "classifications": [{**item, "kind": item["type"], "count": classification_counts[item["id"]]} for item in definitions.values()]}
    query = _normal(body.get("search", body.get("query", "")))
    classification = body.get("classification", body.get("classification_id", ""))
    filtered = []
    for item in items:
        if body.get("domain") and body["domain"] != item["domain"]:
            continue
        if body.get("year") and str(body["year"]) != item["year"]:
            continue
        if classification and not any(tag["value_id"] == classification for tag in item["classifications"]):
            continue
        searchable = " ".join([item["name"], item["summary"], *item["aliases"], *(tag["name"] for tag in item["classifications"])])
        if query and query not in _normal(searchable):
            continue
        filtered.append(item)
    sort = body.get("sort", "date_desc")
    if sort not in {"name", "name_asc", "date_desc", "date_asc", "series_order"}:
        raise ValueError("不支持的排序方式")
    if sort in {"name", "name_asc"}:
        filtered.sort(key=lambda item: (_normal(item["name"]), item["id"]))
    elif sort == "series_order":
        filtered.sort(key=lambda item: (next((tag.get("order", float("inf")) for tag in item["classifications"] if tag["value_id"] == classification), float("inf")), _normal(item["name"])))
    else:
        # Unknown dates always last; the storage-file year is never invented as
        # the actual release date. Unknown-date rows are deterministically named.
        known = [item for item in filtered if item["begin_date"]]
        unknown = [item for item in filtered if not item["begin_date"]]
        known.sort(key=lambda item: (item["begin_date"], _normal(item["name"])), reverse=sort == "date_desc")
        filtered = known + sorted(unknown, key=lambda item: _normal(item["name"]))
    page_size = _number(body.get("page_size"), 24, 100, "page_size")
    page = _number(body.get("page"), 1, 1000000, "page")
    page = min(page, max(1, (len(filtered) + page_size - 1) // page_size))
    start = (page - 1) * page_size
    return {"ok": True, "items": filtered[start:start + page_size], "total": len(filtered), "page": page, "page_size": page_size,
            "facets": facets, "classification_revision": definitions_payload["revision"],
            "legacy_count": sum(not item["stable_id"] for item in items)}


def _resolve(body, records):
    identity = body.get("id")
    if isinstance(identity, str) and WORK_ID.fullmatch(identity):
        matches = [item for item in records if identity == (item["record"].get("id") or "legacy:" + item["work_key"])]
    else:
        ref = body.get("ref")
        if not isinstance(ref, dict):
            raise ValueError("旧作品需要完整版本 ref；只有已初始化的稳定 id 才能独立查询")
        index = parse_record_index(ref.get("index_in_file"))
        expected = ref.get("source_sha256")
        if not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("作品 ref 缺少有效 source_sha256，请重新加载")
        matches = [item for item in records if item["ref"]["yaml_source_rel"] == ref.get("yaml_source_rel") and item["ref"]["index_in_file"] == index]
        if matches:
            actual = matches[0]["ref"]
            if ((ref.get("record_sha256") is not None and actual["record_sha256"] != ref["record_sha256"]) or
                (expected != actual["source_sha256"] and ref.get("record_sha256") != actual["record_sha256"])):
                raise ValueError("作品引用已变化，请重新加载")
            if identity is not None and identity != "legacy:" + matches[0]["work_key"]:
                raise ValueError("旧作品 id 与 ref 不一致")
    if len(matches) != 1:
        raise ValueError("作品不存在或引用不唯一，请重新加载")
    return matches[0]


def detail(body, *, settings, data_root=None) -> dict:
    if not isinstance(body, dict):
        raise ValueError("详情参数必须为对象")
    item = _resolve(body, _records(settings))
    definitions = {definition["id"]: definition for definition in classifications_payload(data_root=data_root)["items"]}
    record = deepcopy(item["record"])
    metadata, sources, warnings = _extension_projection(record)
    memberships = _memberships(record, definitions, warnings)
    return {"ok": True, "item": _item(item, definitions), "record": record, "ref": deepcopy(item["ref"]),
            "metadata": metadata, "resources": deepcopy(item.get("press", [])), "chapters": metadata.get("episodes", []),
            "classifications": memberships, "sources": sources, "warnings": list(dict.fromkeys(warnings))}


def _validated_edits(body, *, settings, data_root=None):
    if not isinstance(body, dict) or not isinstance(body.get("edits"), list) or not 1 <= len(body["edits"]) <= 200:
        raise ValueError("edits 必须包含 1～200 条作品修改")
    definitions = {item["id"]: item for item in classifications_payload(data_root=data_root)["items"]}
    current = _records(settings)
    by_ref = {(item["ref"]["yaml_source_rel"], item["ref"]["index_in_file"]): item for item in current}
    owners = {item["record"]["id"]: (item["ref"]["yaml_source_rel"], item["ref"]["index_in_file"]) for item in current if item["record"].get("id")}
    edits = deepcopy(body["edits"])
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or not isinstance(edit.get("record"), dict):
            raise ValueError("每条修改必须包含完整 record")
        ref = edit.get("ref")
        key = (ref.get("yaml_source_rel"), ref.get("index_in_file")) if isinstance(ref, dict) else ("new", index)
        previous = by_ref.get(key, {}).get("record", {})
        record = edit["record"]
        if previous.get("id") and record.get("id", previous["id"]) != previous["id"]:
            raise ValueError("已有作品稳定 ID 不允许更改")
        record.setdefault("id", previous.get("id") or new_work_id())
        record.setdefault("schema_version", SCHEMA_VERSION)
        record = validate_extensions(record)
        if record["id"] in owners and owners[record["id"]] != key:
            raise ValueError("作品稳定 ID 已被其他记录使用")
        owners[record["id"]] = key
        for tag in record.get("classifications", []):
            definition = definitions.get(tag["value_id"])
            if not definition or definition["type"] != tag["type"]:
                raise ValueError("作品关联了不存在或类型不一致的分类")
        edit["record"] = record
    return edits


def preview_edits(body, *, settings, data_root=None) -> dict:
    edits = _validated_edits(body, settings=settings, data_root=data_root)
    result = preview_catalog_edits(edits, settings=settings)
    return {"ok": not result["issues"], **result}


def apply_edits(body, *, settings, data_root=None) -> dict:
    # Hold the existing catalog lock during validation and the existing shared
    # writer. Other old/new pages therefore cannot bypass ID uniqueness checks.
    from collection_detail.save import catalog_write_transaction
    if settings.filesystem_root is None:
        raise ValueError("未配置作品数据库目录")
    with catalog_write_transaction(settings.filesystem_root):
        edits = _validated_edits(body, settings=settings, data_root=data_root)
        result = apply_catalog_edits(edits, settings=settings)
    with _CACHE_LOCK:
        _CACHE.clear()
    return {"ok": True, **result}


def preview_identity_initialization(body=None, *, settings, data_root=None) -> dict:
    body = body or {}
    records = _records(settings)
    refs = body.get("refs")
    if refs is not None and (not isinstance(refs, list) or len(refs) > 200):
        raise ValueError("refs 必须为最多 200 项的数组")
    selected = [_resolve({"ref": ref}, records) for ref in refs] if refs is not None else [item for item in records if not item["record"].get("id")][:200]
    edits = [{"ref": deepcopy(item["ref"]), "record": deepcopy(item["record"])} for item in selected if not item["record"].get("id")]
    if not edits:
        return {"ok": True, "edits": [], "changes": [], "issues": [], "remaining": sum(not item["record"].get("id") for item in records)}
    result = preview_edits({"edits": edits}, settings=settings, data_root=data_root)
    result["remaining"] = max(0, sum(not item["record"].get("id") for item in records) - len(edits))
    return result


def preview_classification_edits(body, *, settings, data_root=None) -> dict:
    if not isinstance(body, dict) or body.get("action") not in {"assign", "remove"}:
        raise ValueError("分类操作 action 必须为 assign 或 remove")
    refs = body.get("refs")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 200:
        raise ValueError("refs 必须包含 1～200 条作品引用")
    definitions = {item["id"]: item for item in classifications_payload(data_root=data_root)["items"]}
    definition = definitions.get(body.get("value_id"))
    if not definition:
        raise ValueError("分类不存在，请重新加载")
    records = _records(settings)
    edits = []
    for ref in refs:
        item = _resolve({"ref": ref}, records)
        record = deepcopy(item["record"])
        tags = [tag for tag in record.get("classifications", []) if tag["value_id"] != definition["id"]]
        if body["action"] == "assign":
            tag = {"type": definition["type"], "value_id": definition["id"]}
            old = next((tag for tag in record.get("classifications", []) if tag["value_id"] == definition["id"]), {})
            if "order" in body:
                if body["order"] is not None:
                    tag["order"] = body["order"]
            elif "order" in old:
                tag["order"] = old["order"]
            tags.append(tag)
        record["classifications"] = tags
        edits.append({"ref": deepcopy(item["ref"]), "record": record})
    return preview_edits({"edits": edits}, settings=settings, data_root=data_root)
