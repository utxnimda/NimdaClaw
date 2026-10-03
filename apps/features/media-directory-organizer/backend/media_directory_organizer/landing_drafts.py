"""Source-title suggestions and validated catalog drafts for landing previews.

This module prepares data only. Catalog writes, media moves, shortcut creation,
and transaction sequencing remain in the landing module.
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from collection_detail.save import _strict_new_work_patch
from media_directory_organizer.catalog import (
    CatalogWork,
    MediaCatalog,
    PressRecord,
    normalized_identity,
    normalize_press_group,
    normalized_press_group,
    normalized_value,
    path_key,
)
from media_directory_organizer.inference import (
    parse_press_directory_name,
    suggest_press_paths,
)
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.media_groups import (
    media_group_classifier_family,
    media_group_code_known,
)


_SOURCE_TITLE_BRACKET_RE = re.compile(r"[\[\u3010\uff3b]([^\]\u3011\uff3d]+)[\]\u3011\uff3d]")
_SOURCE_TITLE_TECH_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:bd(?:rip)?|blu-?ray|dvd(?:rip)?|web-?dl|webrip|"
    r"1080p?|2160p?|720p?|4k|ma\d+p|x26[45]|h26[45]|avc|hevc|flac|aac|"
    r"hi10p|10bit|8bit|sp|ova|oad|ncop|nced|fin)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_SOURCE_COMBINED_TITLE_RE = re.compile(r"\S\s*[+＋]\s*\S")
_SOURCE_GROUP_SPLIT_RE = re.compile(r"\s*(?:&|＆|\+|＋|×|/)\s*")
_RESOLUTION_ONLY_PRESS_FORMATS = frozenset({"720p", "1080p", "2160p", "4k"})


def _source_title_is_technical(value: str) -> bool:
    """Conservatively reject release metadata when discovering sibling works."""

    text = str(value or "").strip()
    compact = re.sub(r"\s+", "", text).casefold()
    if not compact:
        return True
    if re.fullmatch(r"(?:19|20)\d{2}(?:[-_.]?\d{2}){0,2}", compact):
        return True
    if re.fullmatch(r"\d{1,4}(?:[-~]\d{1,4})?(?:fin)?(?:\+?[a-z]+)*", compact):
        return True
    return _SOURCE_TITLE_TECH_RE.search(text) is not None


def _source_group_tokens(source: Mapping[str, Any]) -> set[str]:
    tokens = {
        normalized_identity(str(value))
        for value in source.get("group_candidates") or []
        if str(value).strip()
    }
    for detail in source.get("group_candidate_details") or []:
        if not isinstance(detail, Mapping):
            continue
        for value in [detail.get("value"), *(detail.get("evidence") or [])]:
            identity = normalized_identity(str(value or ""))
            if identity:
                tokens.add(identity)
    return tokens


def _source_title_is_group(value: str, group_tokens: set[str]) -> bool:
    identity = normalized_identity(value)
    if identity and identity in group_tokens:
        return True
    parts = [part for part in _SOURCE_GROUP_SPLIT_RE.split(value) if part.strip()]
    return len(parts) > 1 and all(
        normalized_identity(part) in group_tokens for part in parts
    )


def _source_work_title(
    source_name: str,
    *,
    root_name: str,
    settings: OrganizerSettings,
    group_tokens: set[str] | None = None,
) -> str:
    """Extract only strong title-shaped evidence from one release directory.

    This intentionally handles the two common layouts used by the collection:
    ``[year][Title][format]`` and ``[group] Title [format]``.  Returning an
    empty string is safer than inventing a work split from a codec/group token.
    """

    value = str(source_name or "").strip()
    parsed_press_directory = parse_press_directory_name(
        value,
        settings=settings,
    )
    if parsed_press_directory is not None:
        return parsed_press_directory["work_name"]
    known_groups = group_tokens or set()
    candidates = [
        segment.strip()
        for segment in _SOURCE_TITLE_BRACKET_RE.findall(value)
        if segment.strip()
        and not _source_title_is_technical(segment)
        and not _source_title_is_group(segment, known_groups)
    ]
    if len(candidates) > 1:
        return ""
    if candidates:
        candidate = candidates[0]
        if normalized_identity(candidate) == normalized_identity(root_name):
            return root_name
        return candidate

    outside = _SOURCE_TITLE_BRACKET_RE.sub(" ", value)
    outside = re.sub(r"\s+", " ", outside).strip(" ._-+")
    if (
        outside
        and not _source_title_is_technical(outside)
        and not _SOURCE_COMBINED_TITLE_RE.search(outside)
    ):
        return outside
    return ""


def _registration_work_drafts(
    root: Path,
    sources: list[dict[str, Any]],
    *,
    defaults: Mapping[str, str],
    settings: OrganizerSettings,
) -> list[dict[str, Any]]:
    """Split a missing catalog root only when every source has one strong title."""

    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    has_ambiguous_or_combined_title = False
    for source in sources:
        title = _source_work_title(
            str(source.get("name") or ""),
            root_name=root.name,
            settings=settings,
            group_tokens=_source_group_tokens(source),
        )
        is_combined = bool(title and _SOURCE_COMBINED_TITLE_RE.search(title))
        source["work_title_hint"] = title
        source["work_title_hint_kind"] = (
            "combined" if is_combined else "single" if title else "ambiguous"
        )
        # A combined bracket such as ``[Refrain+EX]`` is valuable title
        # evidence, but it is not a safe single database work name.  Keep the
        # raw hint for manual review while withholding an automatic suggestion.
        source["suggested_work_name"] = "" if is_combined else title
        identity = normalized_identity(title)
        if not identity or is_combined:
            has_ambiguous_or_combined_title = True
            continue
        bucket = grouped.setdefault(identity, {"name": title, "sources": []})
        if normalized_identity(root.name) == identity:
            bucket["name"] = root.name
        bucket["sources"].append(source)
    if has_ambiguous_or_combined_title or len(grouped) < 2:
        return []

    drafts: list[dict[str, Any]] = []
    for bucket in grouped.values():
        work_name = str(bucket["name"])
        work_sources = list(bucket["sources"])
        strong_formats = {
            normalized_value(str(source.get("suggested_press_format") or ""))
            for source in work_sources
            if str(source.get("suggested_press_format") or "").strip()
            and normalized_value(str(source.get("suggested_press_format") or ""))
            not in _RESOLUTION_ONLY_PRESS_FORMATS
        }
        if strong_formats == {"bdrip"}:
            for source in work_sources:
                source_format = normalized_value(
                    str(source.get("suggested_press_format") or "")
                )
                source_group = str(source.get("suggested_press_group") or "").strip()
                if (
                    source_format in _RESOLUTION_ONLY_PRESS_FORMATS
                    and media_group_classifier_family(source_group) == "VCB"
                ):
                    source["suggested_press_format"] = "BDRip"
                    source["needs_confirmation"] = True
                    reasons = list(source.get("suggestion_reasons") or [])
                    reasons.append(
                        "同作品其他来源明确为 BDRip；当前 VCB 系来源仅识别到分辨率，暂继承 BDRip，需人工确认"
                    )
                    source["suggestion_reasons"] = reasons
        presses = suggest_press_paths(
            work_name,
            [
                {
                    "source_names": [source["name"]],
                    "press_format": source["suggested_press_format"],
                    "press_group": source["suggested_press_group"],
                    "press_group_confirmed": bool(source["suggested_press_group"]),
                    "press_path": "",
                    "suggestion_confidence": source["suggestion_confidence"],
                    "suggestion_reasons": source["suggestion_reasons"],
                    "needs_confirmation": source["needs_confirmation"],
                }
                for source in work_sources
            ],
            settings=settings,
            root_name=root.name,
            preserve_manual=False,
        )
        presses_by_source = {
            str(press.get("source_names", [""])[0]): press
            for press in presses
            if isinstance(press, Mapping)
            and isinstance(press.get("source_names"), list)
            and press.get("source_names")
        }
        for source in work_sources:
            press = presses_by_source.get(str(source.get("name") or ""))
            if press is not None:
                source["suggested_press_path"] = str(
                    press.get("suggested_press_path") or press.get("press_path") or ""
                )
        drafts.append(
            {
                "name": work_name,
                "date": {"start": "", "end": ""},
                "domain": defaults["domain"],
                "country": defaults["country"],
                "release_type": defaults["release_type"],
                "path": str(root),
                "presses": presses,
            }
        )
    return drafts


def _most_common(values: list[str], fallback: str) -> str:
    cleaned = [value for value in values if value]
    if not cleaned:
        return fallback
    return Counter(cleaned).most_common(1)[0][0]


def _scope_defaults(root: Path, catalog: MediaCatalog) -> dict[str, str]:
    siblings = []
    for work in catalog.works:
        if not work.path:
            continue
        try:
            work_path = Path(work.path).expanduser().resolve()
        except OSError:
            continue
        if work_path.parent == root.parent:
            siblings.append(work)
    return {
        "domain": _most_common([work.domain for work in siblings], "animation"),
        "country": _most_common([work.country for work in siblings], "japan"),
        "release_type": _most_common([work.release_type for work in siblings], "tv"),
    }


def _candidate_values(rows: list[Any]) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = str(row.get("value") or "") if isinstance(row, Mapping) else str(row)
        if value.strip() and value not in values:
            values.append(value)
    return values


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


def _source_names(raw: Any, *, index: int) -> list[str]:
    values = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip()
        if name not in result:
            result.append(name)
    if not result:
        raise ValueError(f"压制记录 {index + 1} 至少需要选择一个来源目录")
    return result


def _normalize_draft(
    raw: Any,
    *,
    root: Path,
    require_all_sources: bool = True,
) -> tuple[dict[str, Any], dict[str, dict[str, str]], list[str]]:
    if not isinstance(raw, Mapping):
        raise ValueError("draft_work 必须是对象")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("新增作品名不能为空")
    date = raw.get("date") if isinstance(raw.get("date"), Mapping) else {}
    domain = str(raw.get("domain") or "").strip()
    country = str(raw.get("country") or "").strip().casefold()
    release_type = str(raw.get("release_type") or "").strip()
    presses_raw = raw.get("presses")
    if not isinstance(presses_raw, list) or not presses_raw:
        raise ValueError("新增作品至少需要一个压制记录")

    direct_dirs = {
        child.name: child.resolve()
        for child in root.iterdir()
        if child.is_dir()
    }
    source_press_overrides: dict[str, dict[str, str]] = {}
    selected_sources: list[str] = []
    catalog_presses: list[dict[str, str]] = []
    press_paths_by_pair: dict[tuple[str, str], str] = {}
    for index, raw_press in enumerate(presses_raw):
        if not isinstance(raw_press, Mapping):
            raise ValueError(f"压制记录 {index + 1} 必须是对象")
        press_format = str(raw_press.get("press_format") or "").strip()
        if "press_group" not in raw_press or not isinstance(raw_press["press_group"], str):
            raise ValueError(f"压制记录 {index + 1} 必须选择压制组（可以选择无组）")
        if raw_press.get("press_group_confirmed") is False:
            raise ValueError(f"压制记录 {index + 1} 尚未确认压制组（可以选择无组）")
        press_group = normalize_press_group(raw_press["press_group"]).upper()
        press_path = str(raw_press.get("press_path") or "").strip().replace("\\", "/").strip("/")
        if not press_format or not press_path:
            raise ValueError(f"压制记录 {index + 1} 必须填写格式和目标目录")
        if press_group and not media_group_code_known(press_group):
            raise ValueError(f"压制记录 {index + 1} 使用了未登记的组简称：{press_group}")
        pair = (press_format.casefold(), normalized_press_group(press_group))
        existing_press_path = press_paths_by_pair.get(pair)
        if existing_press_path is not None and existing_press_path.casefold() != press_path.casefold():
            raise ValueError(
                f"同一格式/组不能对应两个不同目标目录：{press_format}/{press_group}"
            )
        names = _source_names(raw_press.get("source_names", raw_press.get("source_name")), index=index)
        for source_name in names:
            source_path = direct_dirs.get(source_name)
            if source_path is None:
                raise ValueError(f"压制记录 {index + 1} 的来源一级目录不存在：{source_name}")
            if source_name in selected_sources:
                raise ValueError(f"来源一级目录不能分配给多个压制记录：{source_name}")
            selected_sources.append(source_name)
            source_press_overrides[str(source_path)] = {
                "press_format": press_format,
                "press_group": press_group,
            }
        if existing_press_path is None:
            press_paths_by_pair[pair] = press_path
            catalog_presses.append(
                {
                    "press_format": press_format,
                    "press_group": press_group,
                    "press_path": press_path,
                }
            )

    unassigned_sources = sorted(set(direct_dirs) - set(selected_sources), key=str.casefold)
    if require_all_sources and unassigned_sources:
        raise ValueError(
            "每个来源一级目录都必须且只能归入一个压制记录，尚未分配："
            + " / ".join(unassigned_sources)
        )

    patch = {
        "name": name,
        "date": {
            "start": str(date.get("start") or "").strip(),
            "end": str(date.get("end") or "").strip(),
        },
        "domain": domain,
        "country": country,
        "release_type": release_type,
        "path": str(root),
        "markers": [],
        "collectioned_ordered": catalog_presses,
    }
    return patch, source_press_overrides, selected_sources


def _normalize_draft_works(
    raw: Any,
    *,
    root: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[str], dict[str, str]]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("draft_works 至少需要两个作品草稿")

    patches: list[dict[str, Any]] = []
    source_press_overrides: dict[str, dict[str, str]] = {}
    source_work_names: dict[str, str] = {}
    selected_sources: list[str] = []
    work_identities: set[str] = set()
    target_paths: dict[str, str] = {}
    for index, raw_work in enumerate(raw):
        patch, press_overrides, work_sources = _normalize_draft(
            raw_work,
            root=root,
            require_all_sources=False,
        )
        identity = normalized_identity(patch["name"])
        if not identity or identity in work_identities:
            raise ValueError(f"draft_works[{index}] 的作品名为空或与另一作品重复")
        work_identities.add(identity)
        for row in patch["collectioned_ordered"]:
            relative = str(row["press_path"]).replace("\\", "/").strip("/")
            key = relative.casefold()
            previous = target_paths.get(key)
            if previous is not None:
                raise ValueError(
                    "同一作品根目录内，不同作品/压制不能共享 press_path："
                    f"{relative}（{previous} / {patch['name']}）"
                )
            target_paths[key] = str(patch["name"])
        for source_name in work_sources:
            if source_name in source_work_names:
                raise ValueError(
                    f"来源一级目录不能分配给多个作品：{source_name}"
                )
            source_work_names[source_name] = str(patch["name"])
            selected_sources.append(source_name)
        source_press_overrides.update(press_overrides)
        patches.append(patch)

    direct_sources = {
        child.name
        for child in root.iterdir()
        if child.is_dir()
    }
    missing = sorted(direct_sources - set(selected_sources), key=str.casefold)
    if missing:
        raise ValueError(
            "每个来源一级目录都必须且只能归入一个作品，尚未分配："
            + " / ".join(missing)
        )
    return patches, source_press_overrides, selected_sources, source_work_names


def _draft_catalog_work(
    patch: dict[str, Any],
    catalog_change: dict[str, Any],
) -> CatalogWork:
    presses = tuple(
        PressRecord(
            press_format=str(row["press_format"]),
            press_group=str(row["press_group"]),
            press_path=str(row["press_path"]),
        )
        for row in patch["collectioned_ordered"]
    )
    return CatalogWork(
        name=str(patch["name"]),
        path=str(patch["path"]),
        domain=str(patch["domain"]),
        country=str(patch["country"]),
        release_type=str(patch["release_type"]),
        presses=presses,
        source_file=str(catalog_change["target"]),
        source_index=int(catalog_change.get("index_in_file", -1)),
        source_sha256=str(catalog_change.get("before_sha256") or ""),
        start_date=str((patch.get("date") or {}).get("start") or ""),
        end_date=str((patch.get("date") or {}).get("end") or ""),
    )


def _retry_draft_patch(raw: Any, *, root: Path) -> dict[str, Any]:
    """Convert either the UI draft or the normalized landing patch without scanning sources."""

    if not isinstance(raw, Mapping):
        raise ValueError("draft_work must be an object")
    raw_presses = raw.get("collectioned_ordered")
    if raw_presses is None:
        raw_presses = raw.get("presses")
    if not isinstance(raw_presses, list) or not raw_presses:
        raise ValueError("draft_work must contain at least one press record")
    presses: list[dict[str, str]] = []
    for index, raw_press in enumerate(raw_presses):
        if not isinstance(raw_press, Mapping):
            raise ValueError(f"press record {index + 1} must be an object")
        presses.append(
            {
                "press_format": str(raw_press.get("press_format") or "").strip(),
                "press_group": str(raw_press.get("press_group") or "").strip(),
                "press_path": str(raw_press.get("press_path") or "").strip(),
            }
        )
    date = raw.get("date") if isinstance(raw.get("date"), Mapping) else {}
    raw_path = str(raw.get("path") or root).strip()
    try:
        draft_path = Path(raw_path).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"invalid draft work path: {raw_path}") from exc
    if draft_path != root:
        raise ValueError("draft_work.path no longer matches the requested work root")
    return {
        "name": str(raw.get("name") or "").strip(),
        "date": {
            "start": str(date.get("start") or "").strip(),
            "end": str(date.get("end") or "").strip(),
        },
        "domain": str(raw.get("domain") or "").strip(),
        "country": str(raw.get("country") or "").strip(),
        "release_type": str(raw.get("release_type") or "").strip(),
        "path": str(root),
        "markers": list(raw.get("markers")) if isinstance(raw.get("markers"), list) else [],
        "collectioned_ordered": presses,
    }


def _repair_press_path(raw: Any, *, index: int) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"修复作品压制记录 {index + 1} 的 press_path 必须是字符串")
    candidate = raw.strip().replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        raise ValueError(f"修复作品压制记录 {index + 1} 的目标目录必须是相对路径")
    parts = candidate.strip("/").split("/")
    if not candidate or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"修复作品压制记录 {index + 1} 的目标目录包含非法路径片段")
    return "/".join(parts)


def _selected_catalog_repair_patch(raw: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    """Validate an existing-row repair without rejecting legacy catalog vocabulary.

    Existing records may predate the media-group registry and may contain repeated
    format/group pairs in continuation blocks.  The selected-record flow therefore
    preserves row identity metadata and validates only values needed for a safe path
    repair.  New rows are still registry-validated later when they are resolved.
    """

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("修复作品名不能为空")
    date = raw.get("date") if isinstance(raw.get("date"), Mapping) else {}
    start = str(date.get("start") or "").strip()
    end = str(date.get("end") or "").strip()
    iso_date = re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
    if iso_date.fullmatch(start) is None:
        raise ValueError("修复作品开始日期必须为 YYYY-MM-DD")
    if end and iso_date.fullmatch(end) is None:
        raise ValueError("修复作品结束日期必须为 YYYY-MM-DD 或留空")
    if end and end < start:
        raise ValueError("修复作品结束日期不能早于开始日期")
    for label in ("domain", "country", "release_type"):
        if not str(raw.get(label) or "").strip():
            raise ValueError(f"修复作品 {label} 不能为空")
    presses_raw = raw.get("collectioned_ordered")
    if presses_raw is None:
        presses_raw = raw.get("presses")
    if not isinstance(presses_raw, list) or not presses_raw:
        raise ValueError("修复作品至少需要一个压制记录")
    presses: list[dict[str, Any]] = []
    requested_press_keys: set[str] = set()
    for index, raw_press in enumerate(presses_raw):
        if not isinstance(raw_press, Mapping):
            raise ValueError(f"修复作品压制记录 {index + 1} 必须是对象")
        press_format = str(raw_press.get("press_format") or "").strip()
        press_group = str(raw_press.get("press_group") or "").strip()
        if not press_format:
            raise ValueError(f"修复作品压制记录 {index + 1} 必须填写格式")
        if "press_group" not in raw_press or not isinstance(raw_press["press_group"], str):
            raise ValueError(f"修复作品压制记录 {index + 1} 必须选择压制组（可以选择无组）")
        if raw_press.get("press_group_confirmed") is False:
            raise ValueError(f"修复作品压制记录 {index + 1} 尚未确认压制组（可以选择无组）")
        row: dict[str, Any] = {
            "press_format": press_format,
            "press_group": press_group,
            "press_path": _repair_press_path(raw_press.get("press_path"), index=index),
        }
        press_key = str(raw_press.get("press_key") or "").strip()
        if press_key:
            if press_key in requested_press_keys:
                raise ValueError(f"修复作品重复选择同一数据库压制记录：{press_key}")
            requested_press_keys.add(press_key)
            row["press_key"] = press_key
        if str(raw_press.get("segment") or "").strip():
            row["segment"] = str(raw_press.get("segment") or "").strip()
            row["continuation_index"] = raw_press.get("continuation_index")
            row["continuation_title"] = str(raw_press.get("continuation_title") or "")
        presses.append(row)
    return {
        "name": name,
        "date": {"start": start, "end": end},
        "domain": str(raw.get("domain") or "").strip(),
        "country": str(raw.get("country") or "").strip().casefold(),
        "release_type": str(raw.get("release_type") or "").strip(),
        "path": str(root),
        "markers": list(raw.get("markers") or []) if isinstance(raw.get("markers"), list) else [],
        "collectioned_ordered": presses,
    }


def _repair_draft_patch(
    raw: Any,
    *,
    root: Path,
    selected_catalog: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("draft_work 必须是对象")
    raw_path = str(raw.get("path") or "").strip()
    if not raw_path:
        raise ValueError("修复作品必须明确填写 path")
    try:
        draft_root = Path(raw_path).expanduser().resolve()
    except OSError as exc:
        raise ValueError(f"修复作品 path 无法解析：{raw_path}") from exc
    if path_key(draft_root) != path_key(root):
        raise ValueError("修复作品 path 必须与本次作品根目录完全一致")
    if selected_catalog:
        return _selected_catalog_repair_patch(raw, root=root)
    presses = raw.get("collectioned_ordered")
    if presses is None:
        presses = raw.get("presses")
    patch = {
        "name": raw.get("name"),
        "date": raw.get("date"),
        "domain": raw.get("domain"),
        "country": raw.get("country"),
        "release_type": raw.get("release_type"),
        "path": str(root),
        "markers": list(raw.get("markers") or []) if isinstance(raw.get("markers"), list) else [],
        "collectioned_ordered": presses,
    }
    return _strict_new_work_patch(patch)
