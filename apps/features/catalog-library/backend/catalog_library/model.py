"""Pure validation for optional, backward-compatible work-library extensions."""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1
WORK_ID = re.compile(r"work_[0-9a-f]{32}\Z")
CLASSIFICATION_ID = re.compile(r"classification_[0-9a-f]{32}\Z")
CLASSIFICATION_TYPES = frozenset({"series", "topic", "universe"})
COVER_MIMES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def validate_cover_reference(cover):
    """Only local content identities can become a browser image source."""
    if not isinstance(cover, dict) or not isinstance(cover.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", cover["sha256"]):
        raise ValueError("metadata.cover 缺少有效本地内容 SHA-256")
    extension = cover.get("extension")
    if not isinstance(extension, str) or extension not in COVER_MIMES or cover.get("mime_type") != COVER_MIMES[extension]:
        raise ValueError("metadata.cover 图片类型无效")
    if any(type(cover.get(key)) is not int or not 0 < cover[key] <= 12000 for key in ("width", "height")) or cover["width"] * cover["height"] > 40_000_000:
        raise ValueError("metadata.cover 图片尺寸无效")
    if "source_url" in cover and (not isinstance(cover["source_url"], str) or len(cover["source_url"]) > 2048):
        raise ValueError("metadata.cover 来源地址格式无效")
    return cover


def new_work_id() -> str:
    return "work_" + uuid4().hex


def attribute(record: dict[str, Any], kind: str, default: Any = None) -> Any:
    return next((item.get("data", default) for item in record.get("attributes", [])
                 if isinstance(item, dict) and item.get("type") == kind), default)


def validate_extensions(record: dict[str, Any]) -> dict[str, Any]:
    """Do not invent missing metadata or interpret continuations as sequels."""
    result = deepcopy(record)
    if "id" in result and (not isinstance(result["id"], str) or not WORK_ID.fullmatch(result["id"])):
        raise ValueError("作品 id 必须为 work_ 加 32 位小写十六进制稳定标识")
    if "schema_version" in result and (type(result["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION):
        raise ValueError("不支持的作品 schema_version")
    if "classifications" in result:
        values = result["classifications"]
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError("classifications 必须是数组，最多 200 项")
        seen = set()
        for item in values:
            if not isinstance(item, dict) or item.get("type") not in CLASSIFICATION_TYPES:
                raise ValueError("分类 type 必须为 series、topic 或 universe")
            identity = item.get("value_id")
            if not isinstance(identity, str) or not CLASSIFICATION_ID.fullmatch(identity):
                raise ValueError("分类 value_id 无效")
            if identity in seen:
                raise ValueError("同一作品不能重复关联相同分类")
            seen.add(identity)
            if "order" in item and (type(item["order"]) not in {int, float} or not math.isfinite(item["order"])):
                raise ValueError("分类 order 必须为有限数值")
    if "metadata" in result:
        metadata = result["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须为对象")
        if "summary" in metadata and not isinstance(metadata["summary"], str):
            raise ValueError("metadata.summary 必须为字符串")
        if "cover" in metadata:
            validate_cover_reference(metadata["cover"])
        if "aliases" in metadata and (not isinstance(metadata["aliases"], list) or
                                      any(not isinstance(alias, str) for alias in metadata["aliases"])):
            raise ValueError("metadata.aliases 必须为字符串数组")
        if "episodes" in metadata and (not isinstance(metadata["episodes"], list) or
                                       any(not isinstance(episode, dict) for episode in metadata["episodes"])):
            raise ValueError("metadata.episodes 必须为对象数组")
    if "source_refs" in result:
        refs = result["source_refs"]
        if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
            raise ValueError("source_refs 必须为对象数组")
        identities = set()
        for ref in refs:
            provider, external_id = ref.get("provider"), ref.get("external_id")
            if not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider):
                raise ValueError("来源 provider 无效")
            if type(external_id) not in {str, int} or not str(external_id).strip():
                raise ValueError("来源 external_id 不能为空")
            identity = (provider, str(external_id), str(ref.get("scope", "")))
            if identity in identities:
                raise ValueError("不能重复关联相同来源和范围")
            identities.add(identity)
    return result


def classification_definition(raw: Any, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("分类定义必须为对象")
    kind = raw.get("type", existing.get("type") if existing else "series")
    if kind not in CLASSIFICATION_TYPES:
        raise ValueError("分类 type 必须为 series、topic 或 universe")
    if existing and kind != existing["type"]:
        raise ValueError("已有分类不能修改 type，请新增分类")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValueError("分类名称必须为 1～200 字符")
    aliases = raw.get("aliases", existing.get("aliases", []) if existing else [])
    if not isinstance(aliases, list) or len(aliases) > 100 or any(not isinstance(alias, str) or len(alias) > 200 for alias in aliases):
        raise ValueError("分类 aliases 必须为字符串数组，最多 100 项")
    return {"id": existing["id"] if existing else "classification_" + uuid4().hex,
            "type": kind, "name": name.strip(), "aliases": list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip()))}
