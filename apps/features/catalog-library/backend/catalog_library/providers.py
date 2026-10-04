"""Explicit, local-first imports from public metadata providers.

Only Bangumi is implemented initially. Raw snapshots are not browsing records:
an HMAC-bound preview and explicitly selected fields must pass the shared work
writer before any source value appears in the normal library UI.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import socket
import ssl
import time
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from collection_detail.catalog_edit_service import catalog_records
from work_catalog_yaml.jp_tv.dates import normalize_air_date
from work_catalog_yaml.layout import feature_data_root
from work_catalog_yaml.operation_progress import operation_context, report_progress
from work_catalog_yaml.persistence import atomic_write_bytes

from . import assets, service
from .model import attribute, validate_extensions
from .network import failure_reason

API_ROOT = "https://api.bgm.tv"
USER_AGENT = "utxnimda/NimdaClaw/1.0 (https://github.com/utxnimda/NimdaClaw)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_EPISODES = 2000
PAGE_SIZE = 100
REQUEST_TIMEOUT = 12
REQUEST_TOTAL_TIMEOUT = 30
PREVIEW_TIMEOUT = 90
PREVIEW_LIFETIME = 3600
_SIGNING_KEY = secrets.token_bytes(32)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {
    "name": "作品名称", "metadata.summary": "简介", "metadata.aliases": "别名",
    "air_date_start": "开播 / 发行日期", "metadata.episodes": "章节 / 曲目",
    "metadata.cover": "作品封面",
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_constant(_value):
    raise ValueError("JSON 中包含非有限数字")


def _load_json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("来源响应不是有效 JSON；未修改作品数据") from None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Bangumi API 返回重定向，已拒绝访问其他地址")


def _positive_id(value: Any, label: str = "subject_id") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value)):
        raise ValueError(f"{label} 必须是正整数 ID，不接受网址")
    return int(value)


def _remaining(deadline: float | None) -> float:
    remaining = REQUEST_TIMEOUT if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("来源同步超过总时限，请稍后重试；未保存部分章节或修改作品数据")
    return min(REQUEST_TIMEOUT, remaining)


def _retry_delay(header: Any) -> float:
    """Honor a short Retry-After; defer long waits to an explicit later retry."""
    if not header:
        return 0.25
    try:
        raw = str(header).strip()
        if raw.isdecimal():
            delay = float(raw)
        else:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = (parsed - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return 0.25
    if not math.isfinite(delay) or delay > 3:
        raise ValueError("Bangumi 要求稍后重试（Retry-After），本次不继续请求；作品数据未修改")
    return max(0.25, delay)


def _wait_retry(delay: float, deadline: float | None) -> None:
    if deadline is not None and deadline - time.monotonic() <= delay:
        raise ValueError("来源同步剩余时间不足以重试；请稍后重试，作品数据未修改")
    time.sleep(delay)


def _read_response(response, deadline: float | None) -> bytes:
    # HTTPResponse.read1 performs at most one underlying socket read. Checking
    # between chunks prevents a peer dripping bytes from bypassing the overall
    # deadline indefinitely (socket reads still have REQUEST_TIMEOUT as a cap).
    read = getattr(response, "read1", response.read)
    chunks, size = [], 0
    while True:
        _remaining(deadline)
        chunk = read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
        _remaining(deadline)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("Bangumi API 响应超出安全大小限制")
        chunks.append(chunk)


def _request_json(path: str, *, query: dict | None = None, payload: dict | None = None, deadline: float | None = None):
    """No user-controlled host, redirects, cookies or account credentials."""
    if not re.fullmatch(r"/v0/(?:search/subjects|subjects/[1-9][0-9]{0,9}|episodes)", path):
        raise ValueError("不支持的 Bangumi API 路径")
    # Search and future single-request callers must not silently opt out of a
    # wall-clock budget merely because they have no multi-page preview deadline.
    if deadline is None:
        deadline = time.monotonic() + REQUEST_TOTAL_TIMEOUT
    url = API_ROOT + path + ("?" + urlencode(query) if query else "")
    request = Request(url, data=_json_bytes(payload) if payload is not None else None,
                      headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                               "Content-Type": "application/json", "Accept-Encoding": "identity"},
                      method="POST" if payload is not None else "GET")
    for attempt in range(2):
        with operation_context(stage="Bangumi 来源请求", action=path):
            try:
                with build_opener(_NoRedirects()).open(request, timeout=_remaining(deadline)) as response:
                    length = response.headers.get("Content-Length")
                    if length and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                        raise ValueError("Bangumi API 响应超出安全大小限制")
                    data = _read_response(response, deadline)
                    if length and len(data) != int(length):
                        raise ValueError("Bangumi API 响应未完整接收；未修改作品数据")
                    return _load_json(data)
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                exc.close()
                if attempt == 0 and (status == 429 or 500 <= status <= 599):
                    delay = _retry_delay(retry_after)
                    report_progress(f"Bangumi 暂不可用（HTTP {status}），重试 1 次")
                    _wait_retry(delay, deadline)
                    continue
                raise ValueError(f"Bangumi 请求失败（HTTP {status}）；未修改作品数据") from None
            except (URLError, TimeoutError, socket.timeout, socket.gaierror, ssl.SSLError, ConnectionError, HTTPException) as exc:
                reason = failure_reason(exc)
                if attempt == 0:
                    report_progress(f"Bangumi 网络请求失败：{reason}；重试 1 次")
                    _wait_retry(0.25, deadline)
                    continue
                raise ValueError(f"Bangumi 连接失败或超时：{reason}；请检查网络，作品数据未修改") from None


def _body(raw: Any, allowed: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("来源操作参数必须为对象")
    if set(raw) - allowed:
        raise ValueError("来源操作包含不支持的参数；请重新预览后确认")
    if raw.get("provider", "bangumi") != "bangumi":
        raise ValueError("当前仅支持 Bangumi 来源")
    return raw


def _text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Bangumi 字符串字段类型或长度不符合预期")
    return value.strip()


def _subject_summary(subject: dict) -> dict:
    return {"id": _positive_id(subject.get("id")), "name": _text(subject.get("name"), 1000),
            "name_cn": _text(subject.get("name_cn"), 1000), "type": subject.get("type"),
            "date": _text(subject.get("date"), 40), "cover_url": ""}


def search_provider(body: Any, *, settings=None) -> dict:
    body = _body(body, {"provider", "query", "type", "offset"})
    query = body.get("query")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or any(ord(char) < 32 for char in query):
        raise ValueError("搜索词必须为 1～200 字符，不能包含控制字符")
    offset = body.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= 1000:
        raise ValueError("搜索 offset 必须为 0～1000 的整数")
    payload = {"keyword": query.strip(), "sort": "match", "filter": {"nsfw": False}}
    if body.get("type") is not None:
        if type(body["type"]) is not int or body["type"] not in {1, 2, 3, 4, 6}:
            raise ValueError("Bangumi 作品类型无效")
        payload["filter"]["type"] = [body["type"]]
    report_progress("正在搜索 Bangumi 公开作品")
    result = _request_json("/v0/search/subjects", query={"limit": 20, "offset": offset}, payload=payload)
    if not isinstance(result, dict) or not isinstance(result.get("data"), list) or len(result["data"]) > 20:
        raise ValueError("Bangumi 搜索响应结构异常")
    if any(not isinstance(item, dict) for item in result["data"]):
        raise ValueError("Bangumi 搜索响应包含无效条目")
    rows = [_subject_summary(item) for item in result["data"] if not item.get("nsfw")]
    total = result.get("total", len(rows))
    if type(total) is not int or total < 0:
        raise ValueError("Bangumi 搜索总数无效")
    report_progress(f"搜索完成，当前页 {len(rows)} 条候选；尚未绑定作品")
    return {"provider": "bangumi", "results": rows, "total": total, "limit": 20, "offset": offset}


def _catalog_item(ref: Any, settings) -> dict:
    if not isinstance(ref, dict) or not isinstance(ref.get("yaml_source_rel"), str) or type(ref.get("index_in_file")) is not int:
        raise ValueError("缺少有效作品 ref，请重新打开作品详情")
    if not _HASH.fullmatch(str(ref.get("source_sha256", ""))) or not _HASH.fullmatch(str(ref.get("record_sha256", ""))):
        raise ValueError("作品 ref 缺少源文件与记录版本，请重新加载")
    for item in catalog_records(settings):
        if (item["ref"]["yaml_source_rel"], item["ref"]["index_in_file"]) == (ref["yaml_source_rel"], ref["index_in_file"]):
            _validate_local_extensions(item["record"], item.get("work_key", ""))
            return item
    raise ValueError("作品记录已不存在，请重新加载；未修改作品数据")


def _validate_local_extensions(record: dict, work_key: str) -> None:
    """Reject malformed legacy extensions explicitly, never silently replace them."""
    try:
        validate_extensions(record)
        sources = record.get("metadata", {}).get("field_sources", {})
        if not isinstance(sources, dict) or any(not isinstance(value, dict) for value in sources.values()):
            raise ValueError("metadata.field_sources 必须是字段名到来源对象的映射")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"本地作品扩展数据无效（{work_key}）：{exc}；请先在作品数据中修复原记录，再同步来源") from None


def _check_version(item: dict, ref: dict) -> None:
    if item["ref"]["record_sha256"] != ref["record_sha256"]:
        raise ValueError("作品记录已变化，请重新预览来源差异后确认")


def _fetch_snapshot(subject_id: int, *, cover_only=False, deadline=None) -> dict:
    deadline = deadline if deadline is not None else time.monotonic() + PREVIEW_TIMEOUT
    report_progress(f"正在获取 Bangumi 作品 {subject_id}")
    subject = _request_json(f"/v0/subjects/{subject_id}", deadline=deadline)
    if not isinstance(subject, dict) or subject.get("id") != subject_id or subject.get("nsfw"):
        raise ValueError("来源作品不存在、ID 不一致或不属于首期公开作品范围")
    _subject_summary(subject)
    if cover_only:
        return {"schema_version": 1, "provider": "bangumi", "subject_id": subject_id,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "raw": {"subject": subject, "episode_pages": []}}
    pages, seen, total, offset = [], set(), None, 0
    for _ in range(MAX_EPISODES // PAGE_SIZE + 1):
        _remaining(deadline)
        page = _request_json("/v0/episodes", query={"subject_id": subject_id, "limit": PAGE_SIZE, "offset": offset}, deadline=deadline)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ValueError("章节响应结构异常；没有保存部分章节")
        page_total = page.get("total")
        if type(page_total) is not int or not 0 <= page_total <= MAX_EPISODES:
            raise ValueError(f"章节总数无效或超出 {MAX_EPISODES} 项上限；没有保存部分章节")
        if total is not None and total != page_total:
            raise ValueError("获取期间章节总数变化，请重试；没有保存部分章节")
        total = page_total
        if page.get("offset") != offset or type(page.get("limit")) is not int or not 1 <= page["limit"] <= PAGE_SIZE:
            raise ValueError("章节分页信息不一致；没有保存部分章节")
        rows = page["data"]
        if len(rows) > page["limit"] or offset + len(rows) > total or (not rows and offset < total):
            raise ValueError("章节分页不完整；没有保存部分章节")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("章节包含无效记录")
            identity = _positive_id(row.get("id"), "episode_id")
            if identity in seen or row.get("subject_id", subject_id) != subject_id:
                raise ValueError("章节重复或属于其他作品；没有保存部分章节")
            seen.add(identity)
        pages.append(page)
        offset += len(rows)
        report_progress(f"已读取章节 {offset}/{total}", completed=offset, total=total, unit="章节")
        if offset == total:
            break
    if total is None or len(seen) != total:
        raise ValueError("章节未完整读取；没有保存部分章节")
    return {"schema_version": 1, "provider": "bangumi", "subject_id": subject_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "raw": {"subject": subject, "episode_pages": pages}}


def _source_root(source_root=None) -> Path:
    root = Path(source_root) if source_root is not None else feature_data_root("catalog-library") / "sources" / "bangumi"
    # Do not follow an existing link/junction into unrelated data while saving.
    for candidate in (root, *root.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise ValueError("来源快照目录不允许符号链接或联接")
    return root.absolute()


def _save_snapshot(snapshot: dict, source_root=None) -> tuple[str, str]:
    data = _json_bytes(snapshot)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError("来源快照超过安全大小限制；未修改作品数据")
    digest = hashlib.sha256(data).hexdigest()
    path = _source_root(source_root) / (digest + ".json")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("来源快照目标不是普通文件")
    if path.exists():
        with path.open("rb") as stream:
            previous = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(previous) > MAX_SNAPSHOT_BYTES or hashlib.sha256(previous).hexdigest() != digest:
            raise ValueError("同名来源快照校验失败，禁止覆盖")
    else:
        atomic_write_bytes(path, data)
    return digest, digest


def _read_snapshot(snapshot_id: Any, expected_hash: Any, source_root=None) -> dict:
    if not isinstance(snapshot_id, str) or not _HASH.fullmatch(snapshot_id) or expected_hash != snapshot_id:
        raise ValueError("来源快照标识无效，请重新预览")
    path = _source_root(source_root) / (snapshot_id + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("来源快照不存在、不是普通文件或超过大小限制")
    with path.open("rb") as stream:
        data = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("来源快照已变化，校验失败；请重新预览")
    snapshot = _load_json(data)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1 or snapshot.get("provider") != "bangumi":
        raise ValueError("来源快照结构或版本无效")
    return snapshot


def _date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    # Partial year/month preserve unknown components rather than invent a day.
    raw = value.strip()
    if re.fullmatch(r"\d{4}", raw):
        raw += "0000"
    elif re.fullmatch(r"\d{4}-\d{2}", raw):
        raw = raw.replace("-", "") + "00"
    try:
        normalized = normalize_air_date(raw, validate_calendar=True)
        return normalized if normalized.replace("0", "").replace("X", "") else ""
    except ValueError:
        return ""


def _aliases(subject: dict) -> list[str]:
    result = [_text(subject.get("name_cn"), 1000)]
    infobox = subject.get("infobox", [])
    if not isinstance(infobox, list):
        raise ValueError("来源 infobox 格式异常")
    for item in infobox:
        if not isinstance(item, dict) or item.get("key") not in {"别名", "中文名", "英文名"}:
            continue
        value = item.get("value")
        values = value if isinstance(value, list) else [value]
        for entry in values:
            raw = entry.get("v") if isinstance(entry, dict) else entry
            if isinstance(raw, str):
                result.append(_text(raw, 1000))
    return list(dict.fromkeys(value for value in result if value and value != subject.get("name")))[:100]


def _finite_number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _episode(row: dict) -> dict:
    result = {"id": "bangumi:" + str(_positive_id(row.get("id"), "episode_id")),
              "source_id": row["id"], "title": _text(row.get("name"), 1000),
              "name_cn": _text(row.get("name_cn"), 1000), "air_date": _date(row.get("airdate")),
              "duration": _text(row.get("duration"), 200)}
    for key in ("type", "sort", "ep", "disc", "duration_seconds"):
        value = row.get(key)
        if value is not None:
            if not _finite_number(value):
                if key in {"ep", "sort"}:
                    # An absent/invalid ep may legitimately use sort instead.
                    # Do not persist malformed provider numerals locally.
                    continue
                raise ValueError("来源章节数字字段无效")
            result[key] = value
    return result


def _episode_range(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError("episode_range 必须同时填写 start 和 end")
    for number in value.values():
        if not _finite_number(number) or number < 0:
            raise ValueError("章节范围必须为非负有限数值")
    if value["start"] > value["end"]:
        raise ValueError("章节范围的起始集不能大于结束集")
    return {key: int(number) if isinstance(number, float) and number.is_integer() else number
            for key, number in value.items()}


def _source_scope(episode_range: dict | None) -> str:
    return f"episodes:{episode_range['start']}-{episode_range['end']}" if episode_range is not None else ""


def _episode_number(row: dict) -> int | float | None:
    ep = row.get("ep")
    if _finite_number(ep) and ep > 0:
        return ep
    order = row.get("sort")
    return order if _finite_number(order) and order >= 0 else None


def _episode_rows(snapshot: dict, episode_range: dict | None = None) -> list[dict]:
    rows = [row for page in snapshot["raw"]["episode_pages"] for row in page["data"]]
    if episode_range is None:
        return rows
    return [row for row in rows if type(row.get("type")) is int and row["type"] == 0 and
            (number := _episode_number(row)) is not None and episode_range["start"] <= number <= episode_range["end"]]


def _candidates(snapshot: dict, episode_range: dict | None = None) -> dict:
    subject = snapshot["raw"]["subject"]
    return {"name": _text(subject.get("name"), 1000),
            "metadata.summary": _text(subject.get("summary"), 100000),
            "metadata.aliases": _aliases(subject), "air_date_start": _date(subject.get("date")),
            "metadata.episodes": [_episode(row) for row in _episode_rows(snapshot, episode_range)],
            "metadata.cover": deepcopy(snapshot.get("cover"))}


def _alias_key(value: str) -> str:
    # Only presentation variants are equivalent; punctuation, season numbers
    # and qualifiers remain significant. Never fuzzy-match distinct works.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _merge_aliases(existing: list[str], incoming: list[str], primary: str) -> list[str]:
    result, seen = [], set()
    for value in existing:
        key = _alias_key(value)
        if key and key not in seen:
            result.append(value)
            seen.add(key)
    primary_key = _alias_key(primary)
    for value in incoming:
        value = value.strip()
        key = _alias_key(value)
        if key and key != primary_key and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _alias_plan(record: dict, candidates: dict, *, rename: bool = False) -> dict:
    """One additive alias projection shared by preview and confirmed imports."""
    primary = candidates["name"] if rename else _field(record, "name")
    existing = _field(record, "metadata.aliases") or []
    automatic_after = _merge_aliases(existing, [candidates["name"]], primary)
    existing_keys = {_alias_key(value) for value in existing}
    return {
        "automatic": [value for value in automatic_after if _alias_key(value) not in existing_keys],
        "automatic_after": automatic_after,
        "merged_after": _merge_aliases(automatic_after, candidates["metadata.aliases"], primary),
    }


def _field(record: dict, name: str):
    if name == "name":
        return attribute(record, "name", "")
    if name == "air_date_start":
        return attribute(record, "date", {}).get("start", "")
    return record.get("metadata", {}).get(name.split(".", 1)[1])


def _set_field(record: dict, name: str, value):
    if name.startswith("metadata."):
        record.setdefault("metadata", {})[name.split(".", 1)[1]] = deepcopy(value)
    else:
        kind = "name" if name == "name" else "date"
        attr = next((item for item in record["attributes"] if item.get("type") == kind), None)
        if attr is None:
            attr = {"type": kind, "data": "" if kind == "name" else {"start": "", "end": ""}}
            record["attributes"].append(attr)
        if kind == "name":
            attr["data"] = value
        else:
            attr["data"]["start"] = value


def _diff(record: dict, candidates: dict, *, episode_range: dict | None = None, episode_total: int = 0, cover_only=False) -> list[dict]:
    count = len(candidates["metadata.episodes"])
    chapter_reason = (f"整条目共 {episode_total} 个章节，将采用全部章节（含正片与特别篇等）；"
                      "确认后替换本地章节列表，请核对是否为同一播出阶段。") if episode_range is None else (
        f"第 {episode_range['start']}～{episode_range['end']} 集范围内匹配 {count} 个正片章节，来源整条目共 {episode_total} 个章节；"
        + ("确认后替换本地章节列表，请核对范围。" if count else "范围没有匹配章节，不允许清空本地章节；仍可关联来源或采用其他字段。"))
    rows = [{"field": field, "label": label, "before": deepcopy(_field(record, field)),
             "after": deepcopy(candidates[field]), "changed": _field(record, field) != candidates[field],
             "selectable": bool(candidates[field]),
             "reason": chapter_reason if field == "metadata.episodes" else
                       "来源为空或日期无效，不允许清空本地数据" if not candidates[field] else
                       "修改后可能影响快捷方式命名；不会自动修改目录或快捷方式" if field in {"name", "air_date_start"} else ""}
            for field, label in _FIELDS.items() if not cover_only or field == "metadata.cover"]
    sources = record.get("metadata", {}).get("field_sources", {})
    for row in rows:
        if row["field"] == "metadata.cover":
            row["before_url"] = assets.cover_url(row["before"])
            row["after_url"] = assets.cover_url(row["after"])
            row["reason"] = "确认后使用已下载到本地的封面；离线浏览不会访问 Bangumi。" if row["selectable"] else "封面获取失败或来源无封面，保留原有封面。"
        if row["field"] == "metadata.aliases":
            row["reason"] = "与已有别名合并并去重，不删除手动别名；不同的来源作品名在确认来源时自动追加。"
        if episode_range is not None and row["field"] == "air_date_start":
            row["reason"] = "此日期来自完整来源条目，不按章节范围推断；" + row["reason"]
        source = sources.get(row["field"], {}) if isinstance(sources, dict) else {}
        original_hash = source.get("imported_value_sha256") if isinstance(source, dict) else None
        if isinstance(original_hash, str) and _HASH.fullmatch(original_hash) and hashlib.sha256(_json_bytes(row["before"])).hexdigest() != original_hash:
            row["locally_modified"] = True
            row["reason"] = ("本地别名已在导入后修改，将保留并合并；" if row["field"] == "metadata.aliases" else
                             "本地内容已在导入后修改，采用将替换当前内容；") + row["reason"]
        else:
            row["locally_modified"] = False
    return rows


def _catalog_scope(settings) -> str:
    if settings.filesystem_root is None:
        raise ValueError("未配置作品数据库目录")
    return hashlib.sha256(str(Path(settings.filesystem_root).resolve()).encode("utf-8")).hexdigest()


def _sign(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(_json_bytes(payload)).decode("ascii").rstrip("=")
    signature = hmac.new(_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _verify_token(token: Any) -> dict:
    if not isinstance(token, str) or len(token) > 8192 or token.count(".") != 1:
        raise ValueError("来源预览凭据无效，请重新预览")
    encoded, signature = token.split(".")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or not _HASH.fullmatch(signature):
        raise ValueError("来源预览凭据格式无效")
    expected = hmac.new(_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("来源预览凭据已失效或被修改，请重新预览")
    payload = _load_json(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if not isinstance(payload, dict) or type(payload.get("expires")) is not int or payload["expires"] < time.time():
        raise ValueError("来源预览已过期，请重新预览")
    return payload


def preview_provider_import(body: Any, *, settings, source_root=None, assets_root=None) -> dict:
    body = _body(body, {"provider", "ref", "subject_id", "episode_range"})
    episode_range = _episode_range(body.get("episode_range"))
    item = _catalog_item(body.get("ref"), settings)
    _check_version(item, body["ref"])
    subject_id = _positive_id(body.get("subject_id"))
    return _preview_import(item, subject_id, episode_range, settings=settings, source_root=source_root, assets_root=assets_root)


def preview_provider_cover(body: Any, *, settings, source_root=None, assets_root=None) -> dict:
    body = _body(body, {"provider", "ref", "subject_id"})
    item = _catalog_item(body.get("ref"), settings)
    _check_version(item, body["ref"])
    bindings = {str(ref.get("external_id")) for ref in item["record"].get("source_refs", []) if ref.get("provider") == "bangumi"}
    if "subject_id" in body:
        subject_id = _positive_id(body["subject_id"])
        if str(subject_id) not in bindings:
            raise ValueError("更新封面必须选择此作品已关联的 Bangumi 条目；请先搜索并确认来源")
    else:
        if len(bindings) != 1:
            raise ValueError("作品没有唯一的 Bangumi 来源，请先关联或选择具体来源")
        subject_id = _positive_id(next(iter(bindings)))
    return _preview_import(item, subject_id, None, settings=settings, source_root=source_root, assets_root=assets_root, cover_only=True)


def _preview_import(item, subject_id, episode_range, *, settings, source_root, assets_root, cover_only=False):
    with operation_context(stage="来源同步预览", action="下载并保存公开作品快照", name=f"Bangumi {subject_id}"):
        deadline = time.monotonic() + PREVIEW_TIMEOUT
        snapshot = _fetch_snapshot(subject_id, cover_only=cover_only, deadline=deadline)
        warnings = [] if cover_only else ["完结日期不根据章节最大日期推断；系列分类不自动导入。"]
        try:
            report_progress(f"正在下载 Bangumi 作品 {subject_id} 的封面")
            snapshot["cover"] = assets.acquire_cover(snapshot["raw"]["subject"], user_agent=USER_AGENT, root=assets_root, deadline=deadline)
            report_progress("封面已保存到本地，等待勾选确认；原有封面尚未改变")
        except (ValueError, OSError, RuntimeError) as exc:
            warning = f"Bangumi {subject_id} 封面获取未完成：{exc}；保留原有封面，其他字段仍可单独同步。"
            warnings.append(warning)
            report_progress(warning, detail="封面下载警告", context={"stage": "Bangumi 封面下载", "name": str(subject_id)})
        candidates = _candidates(snapshot, episode_range)
        alias_plan = {"automatic": []}
        renamed_aliases = []
        if not cover_only:
            alias_plan = _alias_plan(item["record"], candidates)
            renamed_aliases = _alias_plan(item["record"], candidates, rename=True)["merged_after"]
            candidates["metadata.aliases"] = alias_plan["merged_after"]
        episode_total = len(_episode_rows(snapshot))
        diff = _diff(item["record"], candidates, episode_range=episode_range, episode_total=episode_total, cover_only=cover_only)
        for row in diff:
            if row["field"] == "metadata.aliases":
                row["after_when_name_selected"] = renamed_aliases
        snapshot_id, digest = _save_snapshot(snapshot, source_root)
        payload = {"schema_version": 1, "ref": item["ref"], "work_id": item["record"].get("id", ""),
                   "scope": _catalog_scope(settings), "snapshot_id": snapshot_id, "snapshot_sha256": digest,
                   "subject_id": subject_id, "episode_range": episode_range, "cover_only": cover_only, "expires": int(time.time()) + PREVIEW_LIFETIME}
        report_progress("来源快照已保存，等待确认采用字段；正式作品数据未修改")
        return {"provider": "bangumi", "ref": deepcopy(item["ref"]), "work_id": payload["work_id"],
                "snapshot_id": snapshot_id, "snapshot_sha256": digest, "preview_token": _sign(payload),
                "fetched_at": snapshot["fetched_at"], "subject": {**_subject_summary(snapshot["raw"]["subject"]), "cover_url": assets.cover_url(snapshot.get("cover"))},
                "episode_range": episode_range, "episode_counts": {"selected": len(candidates["metadata.episodes"]), "total": episode_total},
                "cover_only": cover_only, "diff": diff, "warnings": warnings,
                "automatic_aliases": [] if cover_only else alias_plan["automatic"]}


def apply_provider_import(body: Any, *, settings, source_root=None, assets_root=None) -> dict:
    body = _body(body, {"provider", "ref", "snapshot_id", "preview_token", "selected_fields"})
    payload = _verify_token(body.get("preview_token"))
    if body.get("ref") != payload["ref"] or body.get("snapshot_id") != payload["snapshot_id"] or payload["scope"] != _catalog_scope(settings):
        raise ValueError("来源快照、作品或数据库与预览不一致，请重新预览")
    selected = body.get("selected_fields")
    if not isinstance(selected, list) or any(not isinstance(field, str) or field not in _FIELDS for field in selected):
        raise ValueError("selected_fields 必须为已预览字段的数组")
    if len(selected) != len(set(selected)):
        raise ValueError("selected_fields 不能包含重复字段")
    cover_only = payload.get("cover_only", False)
    if cover_only and selected != ["metadata.cover"]:
        raise ValueError("更新封面预览只能确认采用作品封面，请勾选封面后确认")
    snapshot = _read_snapshot(payload["snapshot_id"], payload["snapshot_sha256"], source_root)
    if snapshot.get("subject_id") != payload["subject_id"]:
        raise ValueError("来源作品 ID 与预览不一致")
    episode_range = _episode_range(payload.get("episode_range"))
    source_scope = _source_scope(episode_range)
    candidates = _candidates(snapshot, episode_range)
    item = _catalog_item(payload["ref"], settings)
    if payload["work_id"] and item["record"].get("id") != payload["work_id"]:
        raise ValueError("作品身份已变化，请重新预览")
    applied_fields = list(selected)
    automatic_aliases = []
    if not cover_only:
        alias_plan = _alias_plan(item["record"], candidates, rename="name" in selected)
        automatic_aliases = alias_plan["automatic"]
        candidates["metadata.aliases"] = alias_plan["merged_after"] if "metadata.aliases" in selected else alias_plan["automatic_after"]
        if automatic_aliases and "metadata.aliases" not in applied_fields:
            applied_fields.append("metadata.aliases")
    if any(not candidates[field] for field in selected):
        raise ValueError("所选来源字段为空或无效，不能清空本地数据")
    if "metadata.cover" in selected:
        cover = candidates["metadata.cover"]
        assets.read_asset(f"{cover['sha256']}.{cover['extension']}", root=assets_root)
    import_id = hashlib.sha256(_json_bytes({"snapshot": payload["snapshot_id"], "ref": payload["ref"],
                                         "fields": sorted(selected), "episode_range": episode_range, "cover_only": cover_only})).hexdigest()
    refs = item["record"].get("source_refs", [])
    if any(ref.get("cover_import_id" if cover_only else "import_id") == import_id for ref in refs) and all(_field(item["record"], field) == candidates[field] for field in applied_fields):
        return {"ok": True, "db_committed": True, "records": [item], "writes": [], "issues": [],
                "changes": [], "applied_fields": applied_fields, "automatic_aliases_added": [], "unchanged": True}
    _check_version(item, payload["ref"])
    record = deepcopy(item["record"])
    for field in applied_fields:
        _set_field(record, field, candidates[field])
        record.setdefault("metadata", {}).setdefault("field_sources", {})[field] = {
            "provider": "bangumi", "external_id": str(snapshot["subject_id"]), "snapshot_id": payload["snapshot_id"],
            "scope": source_scope, "episode_range": deepcopy(episode_range),
            "imported_value_sha256": hashlib.sha256(_json_bytes(candidates[field])).hexdigest()}
    matching = next((ref for ref in refs if ref.get("provider") == "bangumi" and
                     str(ref.get("external_id")) == str(snapshot["subject_id"]) and (ref.get("scope") or "") == source_scope), {})
    replacement = {**deepcopy(matching), "provider": "bangumi", "external_id": str(snapshot["subject_id"]), "subject_id": snapshot["subject_id"],
                   "snapshot_id": payload["snapshot_id"], "snapshot_sha256": payload["snapshot_sha256"],
                   "scope": source_scope, "episode_range": deepcopy(episode_range),
                   "fetched_at": snapshot["fetched_at"], "fields": sorted(applied_fields), "import_id": import_id}
    record["source_refs"] = [ref for ref in refs if not (ref.get("provider") == "bangumi" and
        str(ref.get("external_id")) == str(snapshot["subject_id"]) and (ref.get("scope") or "") == source_scope)] + [replacement]
    if cover_only:
        # A cover refresh does not replace the full subject/episode snapshot or
        # create an unscoped association alongside existing split-season scopes.
        matching = next((ref for ref in refs if ref.get("provider") == "bangumi" and str(ref.get("external_id")) == str(snapshot["subject_id"])), None)
        if matching is None:
            raise ValueError("Bangumi 来源关联已变化，请重新预览封面")
        record["source_refs"] = [{**deepcopy(ref), "cover_snapshot_id": payload["snapshot_id"],
            "cover_snapshot_sha256": payload["snapshot_sha256"], "cover_fetched_at": snapshot["fetched_at"],
            "cover_import_id": import_id} if ref is matching else deepcopy(ref) for ref in refs]
    with operation_context(stage="来源确认保存", action="通过统一作品写入服务采用字段", name=item.get("name", "")):
        report_progress(f"正在采用 {len(applied_fields)} 个字段并保存来源关联；不会操作媒体文件")
        if automatic_aliases:
            report_progress("确认来源时自动追加作品名别名：" + "、".join(automatic_aliases))
        result = service.apply_edits({"edits": [{"ref": item["ref"], "record": record}]}, settings=settings)
        if result.get("db_committed"):
            report_progress("来源关联及已选字段已保存到本地正式作品数据")
        result["applied_fields"] = applied_fields
        result["automatic_aliases_added"] = automatic_aliases if result.get("db_committed") else []
        result["shortcut_review_recommended"] = bool(set(selected) & {"name", "air_date_start"})
        return result
