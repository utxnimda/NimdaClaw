"""Small, defensive Bangumi anime-search client.

The organizer uses this module only to prepare suggestions for manual review.
It deliberately has no catalog or filesystem write capability.
"""
from __future__ import annotations

import json
import math
import re
import socket
import unicodedata
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from work_catalog_yaml.operation_progress import report_progress


_API_ORIGIN = "https://api.bgm.tv"
_API_PATH = "/v0/search/subjects"
_SUBJECT_ORIGIN = "https://bangumi.tv"
_USER_AGENT = (
    "utxnimda/NimdaClaw/1.0.0 (https://github.com/utxnimda/NimdaClaw)"
)
_DEFAULT_TIMEOUT = 6.0
_MAX_QUERY_LENGTH = 200
_MAX_LIMIT = 10
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
_SEPARATED_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})(?:日)?(?!\d)"
)
_END_DATE_KEYS = {
    "放送结束",
    "放送結束",
    "放送終了",
    "播放结束",
    "播放結束",
    "播放完结",
    "播放完結",
}
_ALIAS_KEYS = {
    "alias",
    "aliases",
    "别名",
    "別名",
    "日文名",
    "英文名",
    "罗马字",
    "羅馬字",
}


class BangumiLookupError(RuntimeError):
    """A stable, user-presentable failure from the Bangumi boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "unavailable",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _normalized_query(query: Any) -> str:
    if not isinstance(query, str):
        raise ValueError("Bangumi 搜索关键词必须是字符串")
    normalized = unicodedata.normalize("NFKC", query)
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError("Bangumi 搜索关键词不能为空")
    if len(normalized) > _MAX_QUERY_LENGTH:
        raise ValueError("Bangumi 搜索关键词不能超过 200 个字符")
    return normalized


def _validated_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Bangumi 搜索数量必须是正整数")
    return min(limit, _MAX_LIMIT)


def _validated_timeout(timeout: Any) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("Bangumi 请求超时必须是正数")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Bangumi 请求超时必须是正数")
    return value


def _identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    result = float(value)
    return result if math.isfinite(result) else 0.0


def _iter_infobox_values(value: Any, *, depth: int = 0):
    if depth > 4:
        return
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            yield cleaned
        return
    if isinstance(value, Mapping):
        if "v" in value:
            yield from _iter_infobox_values(value.get("v"), depth=depth + 1)
        elif "value" in value:
            yield from _iter_infobox_values(value.get("value"), depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_infobox_values(item, depth=depth + 1)


def _infobox_rows(raw: Any):
    if not isinstance(raw, list):
        return
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        key = _string(item.get("key"))
        if key:
            yield key, item.get("value")


def _aliases_from_infobox(raw: Any) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for key, value in _infobox_rows(raw):
        if _identity(key) not in {_identity(item) for item in _ALIAS_KEYS}:
            continue
        for alias in _iter_infobox_values(value):
            identity = _identity(alias)
            if identity and identity not in seen:
                seen.add(identity)
                aliases.append(alias)
    return aliases


def _date_from_text(value: Any) -> str:
    text = _string(value)
    if not text:
        return ""
    matched = _SEPARATED_DATE_RE.search(text) or _COMPACT_DATE_RE.search(text)
    if matched is None:
        return ""
    try:
        parsed = date(*(int(part) for part in matched.groups()))
    except ValueError:
        return ""
    return parsed.isoformat()


def _end_date_from_infobox(raw: Any) -> str:
    normalized_keys = {_identity(item) for item in _END_DATE_KEYS}
    for key, value in _infobox_rows(raw):
        if _identity(key) not in normalized_keys:
            continue
        for candidate in _iter_infobox_values(value):
            parsed = _date_from_text(candidate)
            if parsed:
                return parsed
    return ""


def _query_year(query: str) -> str:
    matched = _YEAR_RE.search(query)
    return matched.group(1) if matched else ""


def _confidence(
    query: str,
    *,
    name: str,
    name_cn: str,
    aliases: list[str],
    subject_date: str,
) -> tuple[float, list[str], float]:
    query_year = _query_year(query)
    query_variants = [query]
    if query_year:
        without_year = " ".join(_YEAR_RE.sub(" ", query).split())
        if without_year:
            query_variants.append(without_year)
    query_identities = [identity for value in query_variants if (identity := _identity(value))]

    names: list[tuple[str, str]] = []
    if name:
        names.append(("原名", name))
    if name_cn:
        names.append(("中文名", name_cn))
    names.extend(("别名", alias) for alias in aliases)

    best_ratio = 0.0
    best_kind = "名称"
    best_relation = "similar"
    for kind, candidate in names:
        candidate_identity = _identity(candidate)
        if not candidate_identity:
            continue
        for query_identity in query_identities:
            if query_identity == candidate_identity:
                ratio = 1.0
                relation = "exact"
            elif query_identity in candidate_identity or candidate_identity in query_identity:
                coverage = min(len(query_identity), len(candidate_identity)) / max(
                    len(query_identity), len(candidate_identity)
                )
                ratio = 0.90 + 0.09 * coverage
                relation = "contains"
            else:
                ratio = SequenceMatcher(
                    None,
                    query_identity,
                    candidate_identity,
                    autojunk=False,
                ).ratio()
                relation = "similar"
            if ratio > best_ratio:
                best_ratio = ratio
                best_kind = kind
                best_relation = relation

    if best_relation == "exact":
        score = 0.92
        reasons = [f"{best_kind}完全匹配"]
    elif best_relation == "contains":
        score = 0.72 + 0.18 * best_ratio
        reasons = [f"{best_kind}包含匹配"]
    elif best_ratio > 0:
        score = 0.72 * best_ratio
        reasons = [f"名称相似度 {round(best_ratio * 100)}%"]
    else:
        score = 0.0
        reasons = ["未找到可比较名称"]

    subject_year = subject_date[:4] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", subject_date) else ""
    if query_year and subject_year:
        if query_year == subject_year:
            score += 0.07
            reasons.append("年份匹配")
        else:
            score -= 0.10
            reasons.append("年份与查询不一致")

    raw_score = max(0.0, min(1.0, score))
    return round(raw_score, 3), reasons, raw_score


def _candidate(raw: Any, *, query: str, source_index: int) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    subject_id = raw.get("id")
    subject_type = raw.get("type")
    if (
        isinstance(subject_id, bool)
        or not isinstance(subject_id, int)
        or subject_id <= 0
        or isinstance(subject_type, bool)
        or subject_type != 2
    ):
        return None

    name = _string(raw.get("name"))
    name_cn = _string(raw.get("name_cn"))
    infobox = raw.get("infobox")
    aliases = _aliases_from_infobox(infobox)
    raw_date = _string(raw.get("date"))
    normalized_date = _date_from_text(raw_date) or raw_date
    rating = raw.get("rating") if isinstance(raw.get("rating"), Mapping) else {}
    confidence, reasons, raw_confidence = _confidence(
        query,
        name=name,
        name_cn=name_cn,
        aliases=aliases,
        subject_date=normalized_date,
    )
    return {
        "id": subject_id,
        "name": name,
        "name_cn": name_cn,
        "aliases": aliases,
        "date": normalized_date,
        "platform": _string(raw.get("platform")),
        "end_date": _end_date_from_infobox(infobox),
        "eps": _nonnegative_int(raw.get("eps")),
        "total_episodes": _nonnegative_int(raw.get("total_episodes")),
        "score": _finite_number(rating.get("score")),
        "rank": _nonnegative_int(rating.get("rank")),
        "summary": _string(raw.get("summary")),
        "subject_url": f"{_SUBJECT_ORIGIN}/subject/{subject_id}",
        "confidence": confidence,
        "reasons": reasons,
        "_raw_confidence": raw_confidence,
        "_source_index": source_index,
    }


def _retry_after_seconds(headers: Any) -> int | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if raw is None:
        return None
    value = str(raw).strip()
    if value.isdecimal():
        return max(0, int(value))
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds())
        return max(0, seconds)
    except (TypeError, ValueError, OverflowError):
        return None


def _http_failure(status: int, headers: Any = None) -> BangumiLookupError:
    if status == 429:
        return BangumiLookupError(
            "Bangumi 请求过于频繁，请稍后重试",
            code="rate_limited",
            retry_after=_retry_after_seconds(headers),
        )
    if 300 <= status < 400:
        return BangumiLookupError(
            "Bangumi 请求发生重定向，已拒绝继续",
            code="redirect_refused",
        )
    if 400 <= status < 500:
        return BangumiLookupError(
            "Bangumi 拒绝了搜索请求",
            code="request_rejected",
        )
    if status >= 500:
        return BangumiLookupError(
            "Bangumi 服务暂时不可用",
            code="unavailable",
        )
    return BangumiLookupError("Bangumi 搜索请求失败", code="request_failed")


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        code = getcode()
        if isinstance(code, int):
            return code
    return 200


def _validate_final_url(response: Any) -> None:
    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return
    final_url = geturl()
    if not isinstance(final_url, str) or not final_url:
        return
    try:
        parsed = urlsplit(final_url)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if (
        parsed is None
        or parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "api.bgm.tv"
        or port not in {None, 443}
    ):
        raise BangumiLookupError(
            "Bangumi 请求发生跨主机重定向，已拒绝继续",
            code="redirect_refused",
        )


def _open(request: Request, *, timeout: float, opener: Any) -> Any:
    if opener is None:
        return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)
    open_method = getattr(opener, "open", None)
    if callable(open_method):
        return open_method(request, timeout=timeout)
    if isinstance(opener, Callable):
        return opener(request, timeout=timeout)
    raise ValueError("opener 必须可调用或提供 open 方法")


def _read_payload(request: Request, *, timeout: float, opener: Any) -> Mapping[str, Any]:
    response = None
    try:
        response = _open(request, timeout=timeout, opener=opener)
    except HTTPError as exc:
        failure = _http_failure(int(exc.code), exc.headers)
        try:
            exc.close()
        except Exception:
            pass
        raise failure from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise BangumiLookupError("无法连接 Bangumi，请稍后重试", code="unavailable") from None
    except BangumiLookupError:
        raise
    except Exception:
        raise BangumiLookupError("无法连接 Bangumi，请稍后重试", code="unavailable") from None

    try:
        status = _response_status(response)
        if status < 200 or status >= 300:
            raise _http_failure(status, getattr(response, "headers", None))
        _validate_final_url(response)
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if not isinstance(raw, bytes):
            raise BangumiLookupError(
                "Bangumi 返回了无法识别的数据",
                code="invalid_response",
            )
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise BangumiLookupError(
                "Bangumi 返回的数据过大，已停止读取",
                code="response_too_large",
            )
    except BangumiLookupError:
        raise
    except (TimeoutError, socket.timeout, OSError):
        raise BangumiLookupError("无法连接 Bangumi，请稍后重试", code="unavailable") from None
    except Exception:
        raise BangumiLookupError(
            "Bangumi 返回了无法识别的数据",
            code="invalid_response",
        ) from None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BangumiLookupError(
            "Bangumi 返回了无法识别的数据",
            code="invalid_response",
        ) from None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise BangumiLookupError(
            "Bangumi 返回了无法识别的数据",
            code="invalid_response",
        )
    return payload


def search_bangumi_anime(
    query: str,
    *,
    limit: int = 6,
    timeout: float = _DEFAULT_TIMEOUT,
    opener: Any = None,
) -> dict[str, Any]:
    """Search Bangumi anime and return manually reviewable candidates.

    The result is a suggestion list only.  This function never selects a
    candidate, writes the catalog, or touches the filesystem.
    """
    normalized_query = _normalized_query(query)
    page_limit = _validated_limit(limit)
    request_timeout = _validated_timeout(timeout)
    api_url = f"{_API_ORIGIN}{_API_PATH}?limit={page_limit}&offset=0"
    body = json.dumps(
        {
            "keyword": normalized_query,
            "sort": "match",
            "filter": {"type": [2], "nsfw": False},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    report_progress("正在请求 Bangumi 作品搜索", detail=normalized_query)
    payload = _read_payload(request, timeout=request_timeout, opener=opener)
    report_progress("解析 Bangumi 返回结果并计算匹配度")
    data = payload["data"]
    candidates = [
        candidate
        for index, item in enumerate(data[:page_limit])
        if (candidate := _candidate(item, query=normalized_query, source_index=index))
        is not None
    ]
    candidates.sort(key=lambda item: -float(item["_raw_confidence"]))
    for candidate in candidates:
        candidate.pop("_raw_confidence", None)
        candidate.pop("_source_index", None)

    total_raw = payload.get("total")
    total = (
        total_raw
        if isinstance(total_raw, int) and not isinstance(total_raw, bool) and total_raw >= 0
        else len(candidates)
    )
    search_url = (
        f"{_SUBJECT_ORIGIN}/subject_search/{quote(normalized_query, safe='')}?cat=2"
    )
    report_progress("Bangumi 候选已生成，等待手动确认", completed=len(candidates), unit="候选作品")
    return {
        "query": normalized_query,
        "search_url": search_url,
        "total": total,
        "candidates": candidates,
    }


__all__ = ["BangumiLookupError", "search_bangumi_anime"]
