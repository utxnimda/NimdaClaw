"""Pure inference helpers for catalog-onboarding suggestions.

The functions in this module only turn already supplied names and registry
data into suggestions.  They neither inspect the filesystem nor persist a
decision.  In particular, a medium-confidence layout signature is evidence
for the review UI, not permission to move files.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


_MEDIA_FORMAT_IDENTITIES = {"bdrip", "dvdrip"}
_RESOLUTION_FORMAT_RE = re.compile(r"^(?:2160|1080|720|576|480|456|432|384|360)p$")
_PLACEHOLDER_GROUPS = {"", "----", "---", "--", "-"}
_UPLOADER_MARKER_IDENTITIES = {"mawen", "mawen1250"}
_CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
_INVALID_WINDOWS_TRANSLATION = str.maketrans(
    {
        "<": "＜",
        ">": "＞",
        ":": "：",
        '"': "＂",
        "/": "／",
        "\\": "＼",
        "|": "｜",
        "?": "？",
        "*": "＊",
    }
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_JSUM_SOURCE_SIGNATURE_RE = re.compile(
    r"^\s*"
    r"\[(?:19|20)\d{2}(?:-\d{2})?(?:\s+movie)?\]"
    r"\[[^\]]+\]"
    r"\[bd[ ._-]*rip\]"
    r"\[(?:2160|1080|720)p[^\]]*\]"
    r"\[[^\]]*(?:fin|sp)[^\]]*\]",
    re.IGNORECASE,
)


def _normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _identity(value: Any) -> str:
    return "".join(character for character in _normalized_text(value) if character.isalnum())


def _settings_mapping(settings: Any, field: str) -> dict[str, tuple[str, ...]]:
    raw = settings.get(field) if isinstance(settings, Mapping) else getattr(settings, field, None)
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for key, values in raw.items():
        name = str(key).strip()
        if not name:
            continue
        if isinstance(values, str):
            aliases = (values.strip(),) if values.strip() else ()
        elif isinstance(values, Iterable):
            aliases = tuple(str(value).strip() for value in values if str(value).strip())
        else:
            aliases = ()
        if aliases:
            result[name] = aliases
    return result


def _phrase_matches(haystack: str, raw_token: Any) -> bool:
    """Match a marker while keeping short ASCII codes on token boundaries."""

    token = _normalized_text(raw_token)
    if not token:
        return False
    prefix = r"(?<![0-9a-z])" if token[0].isascii() and token[0].isalnum() else ""
    suffix = r"(?![0-9a-z])" if token[-1].isascii() and token[-1].isalnum() else ""
    return re.search(prefix + re.escape(token) + suffix, haystack, re.IGNORECASE) is not None


def _confidence_for_score(score: int) -> str:
    if score >= 400:
        return "high"
    if score >= 240:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _add_candidate(
    candidates: dict[str, dict[str, Any]],
    value: str,
    *,
    score: int,
    evidence: str,
    kind: str,
) -> None:
    raw_value = str(value or "").strip()
    canonical = raw_value if kind.endswith("format") else raw_value.upper()
    if not canonical:
        return
    key = _normalized_text(canonical)
    row = candidates.get(key)
    if row is None:
        row = {
            "value": canonical,
            "score": score,
            "confidence": _confidence_for_score(score),
            "kind": kind,
            "evidence": [],
        }
        candidates[key] = row
    elif score > int(row["score"]):
        row["score"] = score
        row["confidence"] = _confidence_for_score(score)
        row["kind"] = kind
    if evidence and evidence not in row["evidence"]:
        row["evidence"].append(evidence)


def _ranked_candidates(candidates: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = sorted(
        (dict(row) for row in candidates.values()),
        key=lambda row: (-int(row["score"]), str(row["value"]).casefold()),
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _infer_format(source_name: str, settings: Any) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    haystack = _normalized_text(source_name)
    candidates: dict[str, dict[str, Any]] = {}
    for canonical, aliases in _settings_mapping(settings, "format_markers").items():
        matched = [alias for alias in aliases if _phrase_matches(haystack, alias)]
        if not matched:
            continue
        identity = _identity(canonical)
        if identity in _MEDIA_FORMAT_IDENTITIES:
            score = 520 + max(len(_identity(alias)) for alias in matched)
            kind = "media-format"
        elif _RESOLUTION_FORMAT_RE.fullmatch(identity):
            score = 320 + max(len(_identity(alias)) for alias in matched)
            kind = "resolution-format"
        else:
            score = 260 + max(len(_identity(alias)) for alias in matched)
            kind = "other-format"
        _add_candidate(
            candidates,
            canonical,
            score=score,
            evidence=" / ".join(matched),
            kind=kind,
        )

    ranked = _ranked_candidates(candidates)
    media = [row for row in ranked if row["kind"] == "media-format"]
    reasons: list[str] = []
    if len(media) > 1:
        reasons.append(
            "同时识别到多个介质格式："
            + " / ".join(str(row["value"]) for row in media)
            + "；不能安全自动选择。"
        )
        return ranked, "", "none", reasons
    if len(media) == 1:
        chosen = media[0]
        resolutions = [row for row in ranked if row["kind"] == "resolution-format"]
        reason = f"识别到明确介质格式 {chosen['value']}"
        if resolutions:
            reason += "，其语义优先于分辨率标记 " + " / ".join(
                str(row["value"]) for row in resolutions
            )
        reasons.append(reason + "。")
        return ranked, str(chosen["value"]), "high", reasons
    if len(ranked) == 1:
        chosen = ranked[0]
        reasons.append(f"仅识别到一个格式候选 {chosen['value']}。")
        return ranked, str(chosen["value"]), str(chosen["confidence"]), reasons
    if len(ranked) > 1:
        reasons.append(
            "同时识别到多个非介质格式候选："
            + " / ".join(str(row["value"]) for row in ranked)
            + "；需要人工选择。"
        )
        return ranked, "", "none", reasons
    reasons.append("来源目录名中没有识别到压制格式。")
    return ranked, "", "none", reasons


def _registry_group_rows(group_registry: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(group_registry, Mapping):
        return [], []
    atomic: dict[str, dict[str, Any]] = {}
    combinations: dict[str, dict[str, Any]] = {}

    def absorb(raw: Any, *, default_kind: str) -> None:
        if not isinstance(raw, Mapping):
            return
        code = str(raw.get("code") or "").strip().upper()
        if not code:
            return
        members = [
            str(member).strip().upper()
            for member in raw.get("members", [])
            if str(member).strip()
        ] if isinstance(raw.get("members"), list) else []
        kind = str(raw.get("kind") or default_kind).strip().casefold()
        target = combinations if kind == "combination" or len(members) > 1 else atomic
        row = target.setdefault(
            code,
            {"code": code, "kind": "combination" if target is combinations else kind, "members": []},
        )
        for field in ("names", "aliases"):
            values = raw.get(field)
            if isinstance(values, list):
                saved = row.setdefault(field, [])
                for value in values:
                    text = str(value).strip()
                    if text and text not in saved:
                        saved.append(text)
        for member in members:
            if member not in row["members"]:
                row["members"].append(member)

    for section, kind in (
        ("release_groups", "release"),
        ("translation_groups", "translation"),
        ("catalog_only_groups", "catalog-only"),
        ("combinations", "combination"),
    ):
        rows = group_registry.get(section)
        if isinstance(rows, list):
            for raw in rows:
                absorb(raw, default_kind=kind)
    options = group_registry.get("options")
    if isinstance(options, list):
        for raw in options:
            absorb(raw, default_kind=str(raw.get("kind") or "") if isinstance(raw, Mapping) else "")
    return list(atomic.values()), list(combinations.values())


def _group_marker_score(code: str, marker: str, *, registry_name: bool = False) -> int:
    identity = _identity(marker)
    if identity in _UPLOADER_MARKER_IDENTITIES:
        return 120
    if identity in {"vcbstudio", "jsum"}:
        return 560
    if identity == _identity(code):
        return 480
    return (430 if registry_name else 400) + min(len(identity), 40)


def _infer_group(
    source_name: str,
    settings: Any,
    group_registry: Any,
) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    haystack = _normalized_text(source_name)
    candidates: dict[str, dict[str, Any]] = {}
    for canonical, aliases in _settings_mapping(settings, "group_markers").items():
        for alias in aliases:
            if not _phrase_matches(haystack, alias):
                continue
            score = _group_marker_score(canonical, alias)
            kind = "uploader" if _identity(alias) in _UPLOADER_MARKER_IDENTITIES else "group"
            _add_candidate(
                candidates,
                canonical,
                score=score,
                evidence=alias,
                kind=kind,
            )

    atomic_rows, combination_rows = _registry_group_rows(group_registry)
    for row in atomic_rows:
        code = str(row["code"])
        # ``catalog-only`` entries were learned from historic catalog values.
        # Their short codes are valid manual choices, but are not trustworthy
        # source-name markers: titles such as "Fate Zero" would otherwise be
        # misclassified as the legacy group ``ZERO``.  A curated name/alias is
        # still usable when one is added to the registry explicitly.
        if row.get("kind") != "catalog-only" and _phrase_matches(haystack, code):
            _add_candidate(candidates, code, score=480, evidence=code, kind="group")
        for field in ("names", "aliases"):
            for marker in row.get(field, []):
                if _phrase_matches(haystack, marker):
                    score = _group_marker_score(code, marker, registry_name=True)
                    kind = "uploader" if _identity(marker) in _UPLOADER_MARKER_IDENTITIES else "group"
                    _add_candidate(
                        candidates,
                        code,
                        score=score,
                        evidence=str(marker),
                        kind=kind,
                    )

    directly_matched_combinations: set[str] = set()
    for row in combination_rows:
        code = str(row["code"])
        if _phrase_matches(haystack, code):
            directly_matched_combinations.add(code)
            _add_candidate(
                candidates,
                code,
                score=680,
                evidence=f"组合代码 {code}",
                kind="combination",
            )

    strong_atomic_codes = {
        str(row["value"]).upper()
        for row in candidates.values()
        if row["kind"] == "group" and int(row["score"]) >= 300
    }
    for row in combination_rows:
        code = str(row["code"])
        members = {str(member).upper() for member in row.get("members", []) if str(member).strip()}
        if len(members) >= 2 and members == strong_atomic_codes:
            _add_candidate(
                candidates,
                code,
                score=640,
                evidence="精确命中组合成员 " + " + ".join(sorted(members)),
                kind="combination",
            )

    explicit_groups = [
        row
        for row in candidates.values()
        if row["kind"] in {"group", "combination"} and int(row["score"]) >= 300
    ]
    if not explicit_groups and _JSUM_SOURCE_SIGNATURE_RE.search(haystack):
        _add_candidate(
            candidates,
            "JSUM",
            score=280,
            evidence="[年份][标题][BDRIP][分辨率][集数Fin/SP] 目录结构",
            kind="layout-signature",
        )

    ranked = _ranked_candidates(candidates)
    combinations = [row for row in ranked if row["kind"] == "combination" and int(row["score"]) >= 600]
    reasons: list[str] = []
    if len(combinations) == 1:
        chosen = combinations[0]
        reasons.append(f"压制组精确命中注册表组合 {chosen['value']}。")
        return ranked, str(chosen["value"]), "high", reasons
    if len(combinations) > 1:
        reasons.append("同时命中多个注册表组合，需要人工选择。")
        return ranked, "", "none", reasons

    strong = [row for row in ranked if row["kind"] == "group" and int(row["score"]) >= 300]
    if len(strong) == 1:
        chosen = strong[0]
        weak_uploaders = [row for row in ranked if row["kind"] == "uploader"]
        reason = f"识别到明确压制组标记 {chosen['value']}"
        if weak_uploaders:
            reason += "；上传者标记 " + " / ".join(
                str(row["value"]) for row in weak_uploaders
            ) + " 不与明确发行组同级竞争"
        reasons.append(reason + "。")
        return ranked, str(chosen["value"]), "high", reasons
    if len(strong) > 1:
        reasons.append(
            "同时识别到多个明确压制组："
            + " / ".join(str(row["value"]) for row in strong)
            + "；注册表中没有唯一的精确组合。"
        )
        return ranked, "", "none", reasons

    signatures = [row for row in ranked if row["kind"] == "layout-signature"]
    if len(signatures) == 1:
        chosen = signatures[0]
        reasons.append(
            f"目录结构符合 {chosen['value']} 的常见发布签名；这是中等置信建议，必须人工确认。"
        )
        return ranked, str(chosen["value"]), "medium", reasons
    weak = [row for row in ranked if row["kind"] == "uploader"]
    if weak:
        reasons.append("只识别到上传者标记，不能据此确定压制组。")
    else:
        reasons.append("来源目录名中没有识别到压制组。")
    return ranked, "", "none", reasons


def infer_source_press(
    source_name: str,
    *,
    settings: Any,
    group_registry: Any = None,
) -> dict[str, Any]:
    """Return ranked, explainable format/group suggestions for one source name."""

    name = str(source_name or "").strip()
    format_candidates, press_format, format_confidence, format_reasons = _infer_format(
        name, settings
    )
    group_candidates, press_group, group_confidence, group_reasons = _infer_group(
        name, settings, group_registry
    )
    overall_score = min(
        _CONFIDENCE_ORDER[format_confidence],
        _CONFIDENCE_ORDER[group_confidence],
    )
    confidence = next(
        label for label, score in _CONFIDENCE_ORDER.items() if score == overall_score
    )
    return {
        "source_name": name,
        "format_candidates": format_candidates,
        "group_candidates": group_candidates,
        "suggested_press_format": press_format,
        "suggested_press_group": press_group,
        "format_confidence": format_confidence,
        "group_confidence": group_confidence,
        "confidence": confidence,
        "reasons": [*format_reasons, *group_reasons],
        "needs_confirmation": (
            not press_format
            or not press_group
            or format_confidence != "high"
            or group_confidence != "high"
        ),
    }


def safe_windows_component(name: Any) -> str:
    """Return one Windows-safe path component while preserving readable titles."""

    translated = "".join(
        " " if ord(character) < 32 else character.translate(_INVALID_WINDOWS_TRANSLATION)
        for character in str(name or "")
    )
    translated = translated.strip().rstrip(". ")
    if not translated:
        return ""
    base = translated.split(".", 1)[0].strip().upper()
    if base in _WINDOWS_RESERVED_NAMES or translated in {".", ".."}:
        translated = "_" + translated
    return translated


def _path_compare_value(value: Any) -> str:
    return _normalized_text(value).replace("\\", "/").strip("/")


def _group_suffix(group: str, settings: Any) -> str:
    raw = settings.get("group_suffixes") if isinstance(settings, Mapping) else getattr(
        settings, "group_suffixes", None
    )
    if isinstance(raw, Mapping):
        wanted = _normalized_text(group)
        for configured, suffix in raw.items():
            if _normalized_text(configured) == wanted and str(suffix).strip():
                return str(suffix).strip()
    return group.strip()


def parse_press_directory_name(
    source_name: Any,
    *,
    settings: Any,
    press_formats: Iterable[Any] = (),
) -> dict[str, str] | None:
    """Parse a canonical ``<work>_<format>[(group)]`` directory name.

    The separator and format must be at the very end of the first-level
    directory name (apart from one optional parenthesized group).  This makes
    the prefix safe to use as strong work-name evidence while deliberately
    rejecting generated category names such as ``Work_BDRip_CD``.  The
    original work-name spelling, including embedded underscores, is kept.

    Configured format aliases are accepted for incoming directories, while
    ``press_formats`` lets callers add canonical formats already present in
    the catalog even when the local marker configuration has not learned them
    yet.
    """

    value = str(source_name or "").strip()
    if not value:
        return None

    configured = _settings_mapping(settings, "format_markers")
    tokens: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(canonical: Any, token: Any) -> None:
        canonical_value = str(canonical or "").strip()
        token_value = str(token or "").strip()
        identity = _normalized_text(token_value)
        if not canonical_value or not identity or identity in seen:
            return
        seen.add(identity)
        tokens.append((canonical_value, token_value))

    for canonical, aliases in configured.items():
        add(canonical, canonical)
        for alias in aliases:
            add(canonical, alias)
    for press_format in press_formats:
        add(press_format, press_format)

    # Prefer the longest terminal token when one configured alias contains
    # another (for example ``web`` and ``web-rip``).
    tokens.sort(key=lambda item: len(item[1]), reverse=True)
    for canonical, token in tokens:
        matched = re.fullmatch(
            rf"(?P<work>.+)_{re.escape(token)}"
            r"(?:\s*\((?P<group>[^()]+)\))?",
            value,
            flags=re.IGNORECASE,
        )
        if matched is None:
            continue
        work_name = matched.group("work").strip()
        if not work_name or work_name.endswith(("_", ".")):
            return None
        return {
            "work_name": work_name,
            "press_format": canonical,
            "press_group": str(matched.group("group") or "").strip(),
            "authority": "directory_press_suffix",
        }
    return None


def suggest_press_paths(
    work_name: str,
    presses: Iterable[Mapping[str, Any]],
    *,
    settings: Any,
    root_name: str = "",
    preserve_manual: bool = True,
) -> list[dict[str, Any]]:
    """Suggest compatible ``press_path`` values without mutating input rows.

    A non-empty current path is considered manual when it differs from the
    row's previous ``suggested_press_path``.  Such a value remains authoritative
    by default while the newly calculated suggestion is still returned.
    """

    source_rows: list[Mapping[str, Any]] = []
    for index, press in enumerate(presses):
        if not isinstance(press, Mapping):
            raise TypeError(f"presses[{index}] must be a mapping")
        source_rows.append(press)

    work_value = str(work_name or "").strip()
    root_value = str(root_name or "").strip()
    if root_value and work_value and _identity(root_value) == _identity(work_value):
        stem_source = root_value
    else:
        stem_source = work_value or root_value
    stem = safe_windows_component(stem_source)

    groups_by_format: dict[str, set[str]] = {}
    for press in source_rows:
        press_format = str(press.get("press_format") or "").strip()
        press_group = str(press.get("press_group") or "").strip()
        if not press_format or press_group.upper() in _PLACEHOLDER_GROUPS:
            continue
        groups_by_format.setdefault(_normalized_text(press_format), set()).add(
            _normalized_text(press_group)
        )

    result: list[dict[str, Any]] = []
    for press in source_rows:
        row = dict(press)
        if isinstance(press.get("source_names"), list):
            row["source_names"] = list(press["source_names"])
        press_format = str(press.get("press_format") or "").strip()
        press_group = str(press.get("press_group") or "").strip()
        suggestion = ""
        if stem and press_format:
            suggestion = f"{stem}_{safe_windows_component(press_format)}"
            distinct_groups = groups_by_format.get(_normalized_text(press_format), set())
            if len(distinct_groups) > 1 and press_group.upper() not in _PLACEHOLDER_GROUPS:
                suffix = safe_windows_component(_group_suffix(press_group, settings))
                if suffix:
                    suggestion += f"({suffix})"

        existing = str(press.get("press_path") or "").strip()
        previous_suggestion = str(press.get("suggested_press_path") or "").strip()
        is_manual = bool(
            existing
            and (
                not previous_suggestion
                or _path_compare_value(existing) != _path_compare_value(previous_suggestion)
            )
        )
        preserved = bool(preserve_manual and is_manual)
        row["suggested_press_path"] = suggestion
        row["press_path"] = existing if preserved else suggestion
        row["press_path_is_manual"] = is_manual
        row["press_path_preserved"] = preserved
        result.append(row)
    return result


__all__ = [
    "infer_source_press",
    "parse_press_directory_name",
    "safe_windows_component",
    "suggest_press_paths",
]
