"""Read-only suggestions for onboarding a work that is absent from the catalog.

The functions in this module only inspect the selected work root and return
proposed form values.  They intentionally have no catalog-save, shortcut or
file-move dependency; the existing landing preview remains the write boundary.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from media_directory_organizer.bangumi import BangumiLookupError, search_bangumi_anime
from media_directory_organizer.inference import infer_source_press, suggest_press_paths
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.media_groups import load_media_group_registry


_BANGUMI_LIMIT = 6


def _confidence_percent(value: Any) -> int:
    labels = {"none": 0, "low": 35, "medium": 65, "high": 95}
    if isinstance(value, str) and value.strip().casefold() in labels:
        return labels[value.strip().casefold()]
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 <= numeric <= 1:
        numeric *= 100
    return max(0, min(100, round(numeric)))


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validated_root(raw: Any, settings: OrganizerSettings) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("root 必须是非空字符串")
    root = Path(raw.strip()).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"作品根目录不存在：{root}")
    if settings.allowed_resource_roots and not any(
        _path_under(root, allowed) for allowed in settings.allowed_resource_roots
    ):
        raise ValueError(f"作品根目录不在配置的资源库范围内：{root}")
    return root


def _clean_query(raw: Any, *, fallback: str) -> str:
    if raw is None:
        raw = fallback
    if not isinstance(raw, str):
        raise ValueError("query 必须是字符串")
    query = " ".join(unicodedata.normalize("NFKC", raw).split())
    if not query:
        query = " ".join(unicodedata.normalize("NFKC", fallback).split())
    if not query:
        raise ValueError("智能识别搜索词不能为空")
    if len(query) > 200:
        raise ValueError("智能识别搜索词不能超过 200 个字符")
    return query


def _source_names(raw: Any, *, index: int, root: Path) -> list[str]:
    values = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip()
        source = (root / name).resolve()
        if source.parent != root or not source.is_dir():
            raise ValueError(f"压制记录 {index + 1} 的来源一级目录不存在：{name}")
        if name not in result:
            result.append(name)
    if not result:
        raise ValueError(f"压制记录 {index + 1} 至少需要一个来源一级目录")
    return result


def _draft_mapping(raw: Any, *, root: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("draft_work 必须是 JSON 对象")
    date = raw.get("date") if isinstance(raw.get("date"), Mapping) else {}
    raw_presses = raw.get("presses")
    if raw_presses is None:
        raw_presses = raw.get("collectioned_ordered")
    if not isinstance(raw_presses, list) or not raw_presses:
        raise ValueError("draft_work 至少需要一条压制记录")
    presses: list[dict[str, Any]] = []
    for index, raw_press in enumerate(raw_presses):
        if not isinstance(raw_press, Mapping):
            raise ValueError(f"压制记录 {index + 1} 必须是 JSON 对象")
        presses.append(
            {
                **deepcopy(dict(raw_press)),
                "source_names": _source_names(
                    raw_press.get("source_names", raw_press.get("source_name")),
                    index=index,
                    root=root,
                ),
                "press_format": str(raw_press.get("press_format") or "").strip(),
                "press_group": str(raw_press.get("press_group") or "").strip().upper(),
                "press_path": str(raw_press.get("press_path") or "").strip(),
            }
        )
    return {
        **deepcopy(dict(raw)),
        "name": str(raw.get("name") or root.name).strip() or root.name,
        "date": {
            "start": str(date.get("start") or "").strip(),
            "end": str(date.get("end") or "").strip(),
        },
        "domain": str(raw.get("domain") or "").strip(),
        "country": str(raw.get("country") or "").strip().casefold(),
        "release_type": str(raw.get("release_type") or "").strip(),
        "path": str(root),
        "presses": presses,
    }


def _merge_inference(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-source evidence without inventing a value on disagreement."""

    formats = {
        str(row.get("suggested_press_format") or "").strip()
        for row in rows
        if str(row.get("suggested_press_format") or "").strip()
    }
    groups = {
        str(row.get("suggested_press_group") or "").strip().upper()
        for row in rows
        if str(row.get("suggested_press_group") or "").strip()
    }
    reasons: list[str] = []
    for row in rows:
        for reason in row.get("reasons") or []:
            text = str(reason).strip()
            if text and text not in reasons:
                reasons.append(text)
    confidences = [_confidence_percent(row.get("confidence")) for row in rows]
    return {
        "suggested_press_format": next(iter(formats)) if len(formats) == 1 else "",
        "suggested_press_group": next(iter(groups)) if len(groups) == 1 else "",
        "confidence": min(confidences) if confidences else 0,
        "reasons": reasons,
        "needs_confirmation": any(bool(row.get("needs_confirmation")) for row in rows)
        or len(formats) != 1
        or len(groups) != 1,
        "source_inferences": rows,
    }


def _local_proposed_work(
    draft: dict[str, Any],
    *,
    root: Path,
    settings: OrganizerSettings,
) -> tuple[dict[str, Any], list[str], int]:
    presses: list[dict[str, Any]] = []
    warnings: list[str] = []
    confidence_values: list[int] = []
    group_registry = load_media_group_registry()
    for index, raw_press in enumerate(draft["presses"]):
        source_inferences = [
            infer_source_press(
                source_name,
                settings=settings,
                group_registry=group_registry,
            )
            for source_name in raw_press["source_names"]
        ]
        inferred = _merge_inference(source_inferences)
        row = deepcopy(raw_press)
        if not row["press_format"] and inferred["suggested_press_format"]:
            row["press_format"] = inferred["suggested_press_format"]
        if not row["press_group"] and inferred["suggested_press_group"]:
            row["press_group"] = inferred["suggested_press_group"]
        row.update(inferred)
        # The editable values are the proposal; suggested_* keeps the evidence
        # visible even when a user has already entered an override.
        row["suggested_press_format"] = inferred["suggested_press_format"]
        row["suggested_press_group"] = inferred["suggested_press_group"]
        presses.append(row)
        confidence_values.append(int(inferred["confidence"]))
        if not row["press_format"]:
            warnings.append(f"压制记录 {index + 1} 未能唯一识别格式，请手动确认")
        if not row["press_group"]:
            warnings.append(f"压制记录 {index + 1} 未能唯一识别压制/字幕组，请手动确认")
        elif inferred["needs_confirmation"]:
            warnings.append(f"压制记录 {index + 1} 的组别是中等置信建议，请核对")

    presses = suggest_press_paths(
        str(draft["name"]),
        presses,
        settings=settings,
        root_name=root.name,
        preserve_manual=True,
    )
    proposed = {
        **deepcopy(draft),
        "path": str(root),
        "presses": presses,
    }
    confidence = round(sum(confidence_values) / len(confidence_values)) if confidence_values else 0
    return proposed, warnings, confidence


def _fingerprint(root: Path, draft: Mapping[str, Any], query: str) -> str:
    stable = {
        "version": 1,
        "root": str(root),
        "draft_work": draft,
        "query": query,
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _candidate_id(fingerprint: str, source: str, identity: str) -> str:
    return hashlib.sha256(f"{fingerprint}\0{source}\0{identity}".encode("utf-8")).hexdigest()[:16]


def _release_type_for_platform(platform: Any) -> str:
    value = unicodedata.normalize("NFKC", str(platform or "")).strip().casefold()
    if value in {"tv", "テレビ"}:
        return "tv"
    if value in {"ova", "oad"}:
        return "ova"
    if value in {"movie", "劇場版", "剧场版"}:
        return "movie"
    return ""


def _bangumi_proposed_work(
    local: dict[str, Any],
    metadata: Mapping[str, Any],
    *,
    root: Path,
    settings: OrganizerSettings,
) -> tuple[dict[str, Any], list[str]]:
    proposed = deepcopy(local)
    name = str(metadata.get("name") or metadata.get("name_cn") or "").strip()
    if name:
        proposed["name"] = name
    proposed["domain"] = "animation"
    proposed["country"] = "japan"
    date = proposed.get("date") if isinstance(proposed.get("date"), dict) else {}
    start = str(metadata.get("date") or metadata.get("start_date") or "").strip()
    end = str(metadata.get("end_date") or "").strip()
    proposed["date"] = {
        "start": start or str(date.get("start") or "").strip(),
        "end": end or str(date.get("end") or "").strip(),
    }
    warnings: list[str] = []
    mapped_release_type = _release_type_for_platform(metadata.get("platform"))
    if mapped_release_type:
        proposed["release_type"] = mapped_release_type
    elif str(metadata.get("platform") or "").strip():
        warnings.append(
            f"Bangumi 平台 {metadata.get('platform')} 未自动映射，请手动确认发行类型"
        )
    # Filesystem naming remains a local decision.  Re-run the local path helper
    # so auto-suggested rows remain coherent while manual overrides survive.
    proposed["presses"] = suggest_press_paths(
        str(proposed["name"]),
        list(proposed["presses"]),
        settings=settings,
        root_name=root.name,
        preserve_manual=True,
    )
    return proposed, warnings


def suggest_work_landing(
    body: Mapping[str, Any],
    *,
    settings: OrganizerSettings,
    bangumi_searcher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build local and optional Bangumi candidates without mutating anything."""

    root = _validated_root(body.get("root"), settings)
    draft = _draft_mapping(body.get("draft_work"), root=root)
    query = _clean_query(body.get("query"), fallback=str(draft["name"] or root.name))
    include_bangumi = body.get("include_bangumi", True)
    if not isinstance(include_bangumi, bool):
        raise ValueError("include_bangumi 必须是布尔值")

    fingerprint = _fingerprint(root, draft, query)
    local, local_warnings, local_confidence = _local_proposed_work(
        draft,
        root=root,
        settings=settings,
    )
    local_reasons = ["根据来源一级目录识别压制格式和组别", "按现有数据库规则推导 press_path"]
    candidates: list[dict[str, Any]] = [
        {
            "candidate_id": _candidate_id(fingerprint, "local", str(root)),
            "input_fingerprint": fingerprint,
            "source": "local",
            "confidence": local_confidence,
            "reasons": local_reasons,
            "proposed_work": local,
            "proposed_presses": deepcopy(local["presses"]),
            "warnings": local_warnings,
        }
    ]
    warnings = list(local_warnings)
    provider_error = ""
    search_url = f"https://bangumi.tv/subject_search/{quote(query, safe='')}?cat=2"
    eligible = (
        str(draft.get("domain") or "").strip().casefold() == "animation"
        and str(draft.get("country") or "").strip().casefold() == "japan"
    )
    if include_bangumi and eligible:
        searcher = bangumi_searcher or search_bangumi_anime
        try:
            result = searcher(query, limit=_BANGUMI_LIMIT)
            if isinstance(result, Mapping):
                external_url = str(result.get("search_url") or "").strip()
                if external_url.startswith("https://bangumi.tv/subject_search/"):
                    search_url = external_url
                metadata_candidates = result.get("candidates")
            else:
                metadata_candidates = []
            if not isinstance(metadata_candidates, list):
                metadata_candidates = []
            for metadata in metadata_candidates:
                if not isinstance(metadata, Mapping):
                    continue
                subject_id = metadata.get("id", metadata.get("subject_id"))
                try:
                    subject_id = int(subject_id)
                except (TypeError, ValueError):
                    continue
                if subject_id <= 0:
                    continue
                proposed, candidate_warnings = _bangumi_proposed_work(
                    local,
                    metadata,
                    root=root,
                    settings=settings,
                )
                subject_url = f"https://bangumi.tv/subject/{subject_id}"
                confidence = _confidence_percent(metadata.get("confidence"))
                reasons = [
                    str(item).strip()
                    for item in metadata.get("reasons") or []
                    if str(item).strip()
                ]
                candidates.append(
                    {
                        "candidate_id": _candidate_id(fingerprint, "bangumi", str(subject_id)),
                        "input_fingerprint": fingerprint,
                        "source": "bangumi",
                        "confidence": confidence,
                        "reasons": reasons,
                        "subject_id": subject_id,
                        "subject_url": subject_url,
                        "name": str(metadata.get("name") or ""),
                        "name_cn": str(metadata.get("name_cn") or ""),
                        "start_date": str(
                            metadata.get("date") or metadata.get("start_date") or ""
                        ),
                        "end_date": str(metadata.get("end_date") or ""),
                        "platform": str(metadata.get("platform") or ""),
                        "eps": metadata.get("eps"),
                        "total_episodes": metadata.get("total_episodes"),
                        "score": metadata.get("score"),
                        "rank": metadata.get("rank"),
                        "summary": str(metadata.get("summary") or "")[:800],
                        "proposed_work": proposed,
                        "proposed_presses": deepcopy(proposed["presses"]),
                        "warnings": candidate_warnings,
                    }
                )
        except BangumiLookupError as exc:
            provider_error = str(exc)
        except (OSError, TimeoutError):
            provider_error = "Bangumi 暂时不可用；本地推导仍可使用，也可以稍后重试"
    elif include_bangumi and not eligible:
        warnings.append("Bangumi 动画匹配仅在“动画 / 日本”作品下启用")

    return {
        "ok": True,
        "version": 1,
        "input_fingerprint": fingerprint,
        "query": query,
        "search_url": search_url,
        "candidates": candidates,
        "warnings": warnings,
        "provider_error": provider_error,
        "writes_performed": False,
        "requires_manual_confirmation": True,
    }


__all__ = ["suggest_work_landing"]
