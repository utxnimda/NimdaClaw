"""Build and execute explicit, collision-safe file movement plans."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from media_directory_organizer.catalog import (
    CatalogWork,
    MediaCatalog,
    PressRecord,
    normalized_identity,
    normalized_value,
    path_key,
)
from media_directory_organizer.classification import (
    CATEGORY_OTHERS,
    ClassificationContext,
    ClassifierRegistry,
    DEFAULT_CLASSIFIER_REGISTRY,
    LayoutDecision,
    is_vcb_family_group,
)
from media_directory_organizer.settings import OrganizerSettings


_INVALID_WINDOWS_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PLACEHOLDER_GROUPS = {"", "----", "---", "--", "-"}
_CLASSIFIER_FINGERPRINT = "virtual-folder-filter-layout-v4-20260823"
_TRAILING_GROUP_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\((?P<group>[^()]*)\)$")


def _detect_markers(value: str, markers: dict[str, tuple[str, ...]]) -> list[str]:
    haystack = unicodedata.normalize("NFKC", value).casefold()
    scores: dict[str, int] = {}
    for canonical, aliases in markers.items():
        matched_lengths = [
            len(unicodedata.normalize("NFKC", alias))
            for alias in aliases
            if unicodedata.normalize("NFKC", alias).casefold() in haystack
        ]
        if matched_lengths:
            scores[canonical] = max(matched_lengths)
    if not scores:
        return []
    strongest = max(scores.values())
    return sorted(
        (canonical for canonical, score in scores.items() if score == strongest),
        key=str.casefold,
    )


def _same_value(left: str, right: str) -> bool:
    return normalized_value(left) == normalized_value(right)


def _work_key(work: CatalogWork) -> str:
    return f"{work.source_file}\0{work.name}"


def _group_suffix(group: str, settings: OrganizerSettings) -> str:
    for configured, suffix in settings.group_suffixes.items():
        if _same_value(configured, group):
            return suffix
    return group.strip()


def _synthesized_press_path(
    work: CatalogWork,
    press: PressRecord,
    settings: OrganizerSettings,
) -> str:
    same_format = [item for item in work.presses if _same_value(item.press_format, press.press_format)]
    base = f"{work.name}_{press.press_format}"
    distinct_groups = {
        normalized_value(item.press_group)
        for item in same_format
        if item.press_group.strip() not in _PLACEHOLDER_GROUPS
    }
    if len(distinct_groups) > 1 and press.press_group.strip() not in _PLACEHOLDER_GROUPS:
        base += f"({_group_suffix(press.press_group, settings)})"
    return base


def _target_relpath(work: CatalogWork, press: PressRecord, settings: OrganizerSettings) -> tuple[str, str]:
    if press.press_path:
        return press.press_path.replace("/", os.sep).replace("\\", os.sep), "database_press_path"
    return _synthesized_press_path(work, press, settings), "derived_from_catalog"


def _category_stem(
    suggested_relpath: str,
    press: PressRecord,
    settings: OrganizerSettings,
) -> str:
    """Return the press basename with only a verified group suffix removed."""

    name = Path(suggested_relpath).name
    matched = _TRAILING_GROUP_SUFFIX_RE.fullmatch(name)
    if matched is None:
        return name
    suffix = matched.group("group").strip()
    known_suffixes = {
        normalized_identity(press.press_group),
        normalized_identity(_group_suffix(press.press_group, settings)),
    }
    suffix_identity = normalized_identity(suffix)
    if suffix_identity in known_suffixes or (
        is_vcb_family_group(press.press_group) and is_vcb_family_group(suffix)
    ):
        base = matched.group("base").rstrip()
        if base:
            return base
    return name


def _category_layout_path(
    suggested_relpath: str,
    press: PressRecord,
    settings: OrganizerSettings,
    decision: LayoutDecision,
) -> Path:
    category_dir = f"{_category_stem(suggested_relpath, press, settings)}_{decision.category}"
    layout = Path(category_dir) / decision.relative_path
    if not _validate_relpath(str(layout)):
        raise ValueError(f"分类器生成的目标相对路径非法：{layout}")
    return layout


def _validate_relpath(value: str) -> bool:
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    return all(not _INVALID_WINDOWS_NAME_RE.search(part) for part in path.parts)


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _destination_parent_issue(target: Path, destination: Path) -> tuple[str, str, Path] | None:
    """Reject existing non-directory or reparse components below a press target."""

    try:
        relative_parent = destination.parent.relative_to(target)
    except ValueError:
        return "unsafe-layout-target", "分类目标不位于压制目标目录内", destination
    current = target
    for component in (Path(), *relative_parent.parents[::-1], relative_parent):
        candidate = current if component == Path() else target / component
        if not candidate.exists() and not candidate.is_symlink():
            continue
        is_junction = bool(getattr(candidate, "is_junction", lambda: False)())
        if candidate.is_symlink() or is_junction:
            return "layout-reparse-point", "分类目标路径经过符号链接或目录联接", candidate
        if not candidate.is_dir():
            return "layout-parent-not-directory", "分类目标的父路径已被普通文件占用", candidate
    return None


def _manual_target(root: Path, raw_value: str) -> tuple[Path, str]:
    value = str(raw_value).strip()
    if not value:
        raise ValueError("人工目标目录不能为空")
    configured = Path(value).expanduser()
    target = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
    if not _path_under(target, root) or path_key(target) == path_key(root):
        raise ValueError("人工目标目录必须位于作品根目录内，且不能等于作品根目录")
    relpath = str(target.relative_to(root.resolve()))
    if not _validate_relpath(relpath):
        raise ValueError(f"人工目标相对目录非法：{relpath}")
    return target, relpath


def _presses_for_format(work: CatalogWork, press_format: str) -> list[PressRecord]:
    return [press for press in work.presses if _same_value(press.press_format, press_format)]


def _issue(
    code: str,
    message: str,
    path: str | Path = "",
    **details: str,
) -> dict[str, str]:
    row = {"code": code, "message": message, "path": str(path) if path else ""}
    row.update({key: str(value) for key, value in details.items() if value != ""})
    return row


def _scan_files(source: Path) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    issues: list[dict[str, str]] = []
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_dirnames: list[str] = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            is_junction = bool(getattr(child, "is_junction", lambda: False)())
            if child.is_symlink() or is_junction:
                issues.append(_issue("reparse-point", "拒绝移动符号链接或目录联接", child))
            else:
                safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames
        for filename in sorted(filenames):
            child = current_path / filename
            if child.is_symlink():
                issues.append(_issue("symlink-file", "拒绝移动符号链接文件", child))
                continue
            if child.is_file():
                files.append(child)
    return files, issues


def _stable_plan_id(payload: dict[str, Any]) -> str:
    stable = {
        "version": payload.get("version"),
        "root": payload["root"],
        "catalog_root": payload["catalog_root"],
        "classifier_fingerprint": payload.get("classifier_fingerprint"),
        "family_works": payload.get("family_works") or [],
        "assignments": payload["assignments"],
        "moves": payload["moves"],
        "unresolved_files": payload.get("unresolved_files") or [],
        "issues": payload["issues"],
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _catalog_target_rows(
    work_root: Path,
    family: tuple[CatalogWork, ...],
    settings: OrganizerSettings,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for work in family:
        for press in work.presses:
            relpath, authority = _target_relpath(work, press, settings)
            rows.append(
                {
                    "work": work,
                    "press": press,
                    "relpath": relpath,
                    "target": work_root / relpath,
                    "authority": authority,
                }
            )
    return rows


def _boundary_contains(value: str, token: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    escaped = re.escape(unicodedata.normalize("NFKC", token).casefold())
    return bool(re.search(rf"(?<![0-9a-z]){escaped}(?![0-9a-z])", normalized))


def _alias_matches(value: str, alias: str, *, short_boundary: bool = False) -> bool:
    identity = normalized_identity(alias)
    if not identity:
        return False
    if short_boundary or len(identity) <= 3:
        return _boundary_contains(value, alias)
    return identity in normalized_identity(value)


def _aliases_for_work(work: CatalogWork, settings: OrganizerSettings) -> tuple[str, ...]:
    aliases: list[str] = [work.name]
    for configured_name, configured_aliases in settings.work_aliases.items():
        if normalized_identity(configured_name) == work.identity:
            aliases.extend(configured_aliases)
    return tuple(dict.fromkeys(alias for alias in aliases if alias.strip()))


def _match_works(
    value: str,
    candidates: Iterable[CatalogWork],
    *,
    root_identity: str,
    settings: OrganizerSettings,
) -> tuple[tuple[CatalogWork, ...], dict[str, str]]:
    works = tuple(candidates)
    matched_specific: list[CatalogWork] = []
    matched_base: list[CatalogWork] = []
    evidence: dict[str, str] = {}
    for work in works:
        signals: list[str] = []
        for alias in _aliases_for_work(work, settings):
            if _alias_matches(value, alias):
                signals.append(alias)
        is_base = bool(root_identity and work.identity == root_identity)
        if (
            not is_base
            and root_identity
            and work.identity.startswith(root_identity)
            and len(work.identity) > len(root_identity)
        ):
            tail = work.identity[len(root_identity) :]
            if _alias_matches(value, tail, short_boundary=len(tail) <= 3):
                signals.append(tail)
        if not signals:
            continue
        evidence[_work_key(work)] = "、".join(dict.fromkeys(signals))
        (matched_base if is_base else matched_specific).append(work)
    chosen = matched_specific or matched_base
    chosen.sort(key=lambda work: (work.name.casefold(), work.source_file.casefold()))
    return tuple(chosen), evidence


def _work_from_override(
    raw_name: str,
    candidates: Iterable[CatalogWork],
) -> tuple[CatalogWork | None, str]:
    exact = [work for work in candidates if work.name == raw_name]
    if len(exact) == 1:
        return exact[0], "人工逐文件指定数据库作品"
    normalized = [
        work for work in candidates if normalized_identity(work.name) == normalized_identity(raw_name)
    ]
    if len(normalized) == 1:
        return normalized[0], "人工逐文件指定数据库作品（规范化名称匹配）"
    if not normalized and not exact:
        return None, f"人工指定的作品不在当前数据库系列中：{raw_name}"
    return None, f"人工指定的作品名无法唯一定位数据库记录：{raw_name}"


def _route_id(
    root: Path,
    source: Path,
    work: CatalogWork,
    press_format: str,
    press_group: str,
) -> str:
    raw = "\0".join(
        (
            path_key(root),
            path_key(source),
            _work_key(work),
            normalized_value(press_format),
            normalized_value(press_group),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class _SourceScan:
    source: Path
    files: tuple[Path, ...]
    press_format: str
    explicit_group: str
    group_reason: str
    source_matches: tuple[CatalogWork, ...]
    source_evidence: dict[str, str]


@dataclass
class _RoutedFile:
    source_scan: _SourceScan
    source_file: Path
    relative_path: Path
    work: CatalogWork
    work_reason: str
    work_stage: str
    group: str = ""
    group_reason: str = ""


def _parse_source_overrides(
    root: Path,
    raw_overrides: Mapping[str, str] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, str]:
    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("target_overrides 必须是 source 到目标相对目录的对象")
    parsed: dict[str, str] = {}
    for raw_source, raw_target in (raw_overrides or {}).items():
        if not isinstance(raw_source, str) or not isinstance(raw_target, str):
            issues.append(_issue("override-invalid-type", "人工目标修正的 source 和 target 必须是字符串"))
            continue
        source_value = raw_source.strip()
        if not source_value:
            issues.append(_issue("override-source-empty", "人工目标修正缺少源目录"))
            continue
        configured = Path(source_value).expanduser()
        source_path = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
        source_key = path_key(source_path)
        if source_key in parsed:
            issues.append(_issue("override-source-duplicate", "同一源目录配置了多个人工目标", source_path))
            continue
        parsed[source_key] = raw_target
    return parsed


def _parse_route_overrides(
    raw_overrides: Mapping[str, str] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, str]:
    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("route_target_overrides 必须是 route_id 到目标相对目录的对象")
    parsed: dict[str, str] = {}
    for raw_route, raw_target in (raw_overrides or {}).items():
        if not isinstance(raw_route, str) or not isinstance(raw_target, str):
            issues.append(_issue("route-override-invalid-type", "路由目标修正的 route_id 和 target 必须是字符串"))
            continue
        route = raw_route.strip()
        if not route:
            issues.append(_issue("route-override-id-empty", "路由目标修正缺少 route_id"))
            continue
        parsed[route] = raw_target
    return parsed


def _parse_file_work_overrides(
    root: Path,
    raw_overrides: Mapping[str, str] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, tuple[Path, str]]:
    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("file_work_overrides 必须是源文件到数据库作品名的对象")
    parsed: dict[str, tuple[Path, str]] = {}
    for raw_source, raw_work in (raw_overrides or {}).items():
        if not isinstance(raw_source, str) or not isinstance(raw_work, str):
            issues.append(_issue("file-work-override-invalid-type", "逐文件作品修正的源文件和作品名必须是字符串"))
            continue
        source_value = raw_source.strip()
        work_name = raw_work.strip()
        if not source_value or not work_name:
            issues.append(_issue("file-work-override-empty", "逐文件作品修正不能包含空路径或空作品名"))
            continue
        configured = Path(source_value).expanduser()
        source_path = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
        if not _path_under(source_path, root):
            issues.append(_issue("file-work-override-outside-root", "逐文件作品修正越出作品根目录", source_path))
            continue
        parsed[path_key(source_path)] = (source_path, work_name)
    return parsed


def _parse_source_press_overrides(
    root: Path,
    raw_overrides: Mapping[str, Any] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, tuple[Path, str, str]]:
    """Parse folder-level format/group choices used by new-work onboarding."""

    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("source_press_overrides 必须是来源目录到压制信息的对象")
    parsed: dict[str, tuple[Path, str, str]] = {}
    for raw_source, raw_press in (raw_overrides or {}).items():
        if not isinstance(raw_source, str) or not isinstance(raw_press, Mapping):
            issues.append(
                _issue("source-press-override-invalid-type", "来源压制修正必须包含目录、格式和组简称")
            )
            continue
        source_value = raw_source.strip()
        press_format = str(raw_press.get("press_format") or "").strip()
        press_group = str(raw_press.get("press_group") or "").strip()
        if not source_value or not press_format or not press_group:
            issues.append(
                _issue("source-press-override-empty", "来源压制修正不能包含空目录、空格式或空组简称")
            )
            continue
        configured = Path(source_value).expanduser()
        source_path = configured.resolve() if configured.is_absolute() else (root / configured).resolve()
        if not _path_under(source_path, root) or source_path.parent != root:
            issues.append(
                _issue("source-press-override-outside-root", "来源压制修正只能指向作品根目录下的一级目录", source_path)
            )
            continue
        key = path_key(source_path)
        if key in parsed:
            issues.append(
                _issue("source-press-override-duplicate", "同一来源目录配置了多个压制修正", source_path)
            )
            continue
        parsed[key] = (source_path, press_format, press_group)
    return parsed


def _groups_matching_explicit(
    presses: Iterable[PressRecord],
    explicit_group: str,
) -> list[PressRecord]:
    """Prefer an exact real group, then a unique compatible VCB-family group."""

    candidates = list(presses)
    exact = [press for press in candidates if _same_value(press.press_group, explicit_group)]
    if exact:
        return exact
    if is_vcb_family_group(explicit_group):
        return [press for press in candidates if is_vcb_family_group(press.press_group)]
    return []


def _resolve_group(
    routed: _RoutedFile,
    explicit_group_usage: Mapping[tuple[str, str], set[str]],
) -> tuple[str | None, str]:
    work = routed.work
    press_format = routed.source_scan.press_format
    available = {
        normalized_value(press.press_group): press.press_group
        for press in _presses_for_format(work, press_format)
    }
    explicit = routed.source_scan.explicit_group
    if explicit:
        matches = _groups_matching_explicit(
            _presses_for_format(work, press_format),
            explicit,
        )
        if len(matches) == 1:
            matched = matches[0].press_group
            reason = routed.source_scan.group_reason
            if not _same_value(matched, explicit):
                reason += f"；数据库唯一 VCB 系真实组码为 {matched}"
            return matched, reason
        if len(matches) > 1:
            return None, (
                f"来源只识别到 VCB 系列，但数据库作品 {work.name} 的 {press_format} "
                "存在多个 VCB 系压制组："
                + ", ".join(sorted({press.press_group for press in matches}, key=str.casefold))
            )
        return None, (
            f"数据库作品 {work.name} 的 {press_format} 没有压制组 {explicit}；"
            f"候选为：{', '.join(sorted(available.values(), key=str.casefold)) or '无'}"
        )
    if len(available) == 1:
        return next(iter(available.values())), "数据库中该作品/格式只有一个压制组"
    used = explicit_group_usage.get((_work_key(work), normalized_value(press_format)), set())
    remaining = [group for key, group in available.items() if key not in used]
    if len(remaining) == 1:
        return remaining[0], "结合其他来源目录的明确组标记，匹配数据库中唯一未命中的压制组"
    return None, (
        "无法唯一确定压制组；数据库候选为："
        + (", ".join(sorted(available.values(), key=str.casefold)) or "无")
    )


def build_plan(
    work_root: str | Path,
    *,
    catalog: MediaCatalog,
    settings: OrganizerSettings,
    source_names: Iterable[str] | None = None,
    target_overrides: Mapping[str, str] | None = None,
    route_target_overrides: Mapping[str, str] | None = None,
    file_work_overrides: Mapping[str, str] | None = None,
    source_press_overrides: Mapping[str, Any] | None = None,
    classifier_registry: ClassifierRegistry = DEFAULT_CLASSIFIER_REGISTRY,
) -> dict[str, Any]:
    """Analyze a work directory without changing it and return a serializable plan."""
    root = Path(work_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"作品目录不存在：{root}")
    if settings.allowed_resource_roots and not any(
        _path_under(root, allowed) for allowed in settings.allowed_resource_roots
    ):
        raise ValueError(f"作品目录不在配置的资源库范围内：{root}")

    family = catalog.family_for_root(root)
    target_rows = _catalog_target_rows(root, family, settings)
    issues: list[dict[str, str]] = []
    for row in target_rows:
        if not _validate_relpath(str(row["relpath"])):
            issues.append(
                _issue(
                    "invalid-target",
                    f"数据库或推导出的目标相对目录非法：{row['relpath']}",
                )
            )

    selected = {str(name) for name in source_names or [] if str(name)}
    direct_dirs = sorted(
        (child for child in root.iterdir() if child.is_dir()),
        key=lambda path: path.name.casefold(),
    )
    expected_paths = {path_key(Path(row["target"]).resolve()) for row in target_rows}
    sources = [child for child in direct_dirs if path_key(child) not in expected_paths]
    if selected:
        missing = sorted(selected - {source.name for source in sources})
        for name in missing:
            issues.append(
                _issue(
                    "source-not-found",
                    "指定的待整理一级子目录不存在或已经符合规则",
                    root / name,
                )
            )
        sources = [source for source in sources if source.name in selected]

    source_overrides = _parse_source_overrides(root, target_overrides, issues=issues)
    route_overrides = _parse_route_overrides(route_target_overrides, issues=issues)
    work_overrides = _parse_file_work_overrides(root, file_work_overrides, issues=issues)
    press_overrides = _parse_source_press_overrides(root, source_press_overrides, issues=issues)
    source_keys = {path_key(source) for source in sources}
    for source_key in sorted(set(source_overrides) - source_keys):
        issues.append(
            _issue(
                "override-source-not-found",
                "人工目标修正对应的源目录不存在、未被选中或已经符合规则",
                source_key,
            )
        )
    for source_key in sorted(set(press_overrides) - source_keys):
        source_path, _format, _group = press_overrides[source_key]
        issues.append(
            _issue(
                "source-press-override-not-found",
                "来源压制修正对应的一级目录不存在、未被选中或已经符合目标规则",
                source_path,
            )
        )

    root_identity = normalized_identity(root.name)
    family_formats = {
        normalized_value(press.press_format)
        for work in family
        for press in work.presses
    }
    source_scans: list[_SourceScan] = []
    all_scanned_files: set[str] = set()
    for source in sources:
        files, scan_issues = _scan_files(source)
        issues.extend(scan_issues)
        all_scanned_files.update(path_key(path) for path in files)
        if not files:
            issues.append(_issue("empty-source", "待整理目录中没有可移动文件", source))
            continue
        manual_press = press_overrides.get(path_key(source))
        formats = (
            [manual_press[1]]
            if manual_press is not None
            else [
                press_format
                for press_format in _detect_markers(source.name, settings.format_markers)
                if normalized_value(press_format) in family_formats
            ]
        )
        if manual_press is not None and normalized_value(formats[0]) not in family_formats:
            issues.append(
                _issue(
                    "source-press-format-not-in-catalog",
                    f"人工选择的压制格式不在作品数据库中：{formats[0]}",
                    source,
                )
            )
            continue
        if len(formats) != 1:
            issues.append(
                _issue(
                    "format-ambiguous",
                    "无法唯一识别压制格式"
                    if not formats
                    else f"识别到多个压制格式：{', '.join(formats)}",
                    source,
                )
            )
            continue
        groups = (
            [manual_press[2]]
            if manual_press is not None
            else _detect_markers(source.name, settings.group_markers)
        )
        if len(groups) > 1:
            issues.append(
                _issue(
                    "group-ambiguous",
                    f"识别到多个同等强度的压制组：{', '.join(groups)}",
                    source,
                )
            )
            continue
        explicit_group = groups[0] if groups else ""
        candidates = tuple(
            work for work in family if _presses_for_format(work, formats[0])
        )
        source_matches, source_evidence = _match_works(
            source.name,
            candidates,
            root_identity=root_identity,
            settings=settings,
        )
        source_scans.append(
            _SourceScan(
                source=source,
                files=tuple(files),
                press_format=formats[0],
                explicit_group=explicit_group,
                group_reason=(
                    "来源目录名中的压制组标记"
                    if explicit_group
                    else "来源目录未明确标记压制组"
                ),
                source_matches=source_matches,
                source_evidence=source_evidence,
            )
        )

    if len(all_scanned_files) > settings.max_files:
        issues.append(
            _issue(
                "file-limit",
                f"扫描文件数超过安全上限 {settings.max_files}",
                root,
            )
        )

    unresolved_files: list[dict[str, Any]] = []
    routed_files: list[_RoutedFile] = []
    used_work_override_keys: set[str] = set()
    for scan in source_scans:
        candidates = tuple(
            work for work in family if _presses_for_format(work, scan.press_format)
        )
        source_default = scan.source_matches[0] if len(scan.source_matches) == 1 else None
        for source_file in scan.files:
            relative = source_file.relative_to(scan.source)
            source_file_key = path_key(source_file)
            manual = work_overrides.get(source_file_key)
            work: CatalogWork | None = None
            work_reason = ""
            work_stage = ""
            candidate_names: list[str] = []
            if manual is not None:
                used_work_override_keys.add(source_file_key)
                work, work_reason = _work_from_override(manual[1], candidates)
                work_stage = "manual"
                if work is None:
                    candidate_names = [candidate.name for candidate in candidates]
            else:
                file_matches, file_evidence = _match_works(
                    str(relative),
                    candidates,
                    root_identity=root_identity,
                    settings=settings,
                )
                base_only_file_match = bool(
                    file_matches
                    and all(match.identity == root_identity for match in file_matches)
                )
                source_is_specific = bool(
                    source_default is not None
                    and source_default.identity != root_identity
                )
                if base_only_file_match and source_is_specific:
                    work = source_default
                    work_reason = (
                        "文件只命中系列基础名，继承来源目录中更具体的作品："
                        + scan.source_evidence.get(_work_key(work), work.name)
                    )
                    work_stage = "source-specific-over-base"
                elif len(file_matches) == 1:
                    work = file_matches[0]
                    work_reason = (
                        "文件相对路径命中作品别名："
                        + file_evidence.get(_work_key(work), work.name)
                    )
                    work_stage = "filename"
                elif len(file_matches) > 1:
                    candidate_names = [candidate.name for candidate in file_matches]
                    work_reason = "文件名同时命中多个作品"
                elif source_default is not None:
                    work = source_default
                    work_reason = (
                        "文件名未包含作品标记，继承来源目录的唯一作品："
                        + scan.source_evidence.get(_work_key(work), work.name)
                    )
                    work_stage = "source-default"
                elif len(candidates) == 1:
                    work = candidates[0]
                    work_reason = "该系列和压制格式在数据库中只有一个候选作品"
                    work_stage = "catalog-singleton"
                else:
                    candidate_names = [
                        candidate.name for candidate in (scan.source_matches or candidates)
                    ]
                    work_reason = (
                        "来源目录含多个作品且该文件名不能唯一确定归属"
                        if len(scan.source_matches) > 1
                        else "文件名和来源目录都不能唯一确定数据库作品"
                    )
            if work is None:
                unresolved = {
                    "source": str(source_file),
                    "source_dir": str(scan.source),
                    "source_relpath": str(relative),
                    "reason": work_reason,
                    "candidates": sorted(dict.fromkeys(candidate_names), key=str.casefold),
                }
                unresolved_files.append(unresolved)
                issues.append(
                    _issue(
                        "work-ambiguous",
                        work_reason,
                        source_file,
                        candidates=" / ".join(unresolved["candidates"]),
                    )
                )
                continue
            routed_files.append(
                _RoutedFile(
                    source_scan=scan,
                    source_file=source_file,
                    relative_path=relative,
                    work=work,
                    work_reason=work_reason,
                    work_stage=work_stage,
                )
            )

    for unused_key in sorted(set(work_overrides) - used_work_override_keys):
        source_path, _ = work_overrides[unused_key]
        issues.append(
            _issue(
                "file-work-override-not-found",
                "逐文件作品修正对应的源文件不存在、未被选中或不可扫描",
                source_path,
            )
        )

    explicit_group_usage: dict[tuple[str, str], set[str]] = {}
    for routed in routed_files:
        explicit = routed.source_scan.explicit_group
        if not explicit:
            continue
        matching = _groups_matching_explicit(
            _presses_for_format(routed.work, routed.source_scan.press_format),
            explicit,
        )
        if len(matching) == 1:
            key = (_work_key(routed.work), normalized_value(routed.source_scan.press_format))
            explicit_group_usage.setdefault(key, set()).add(normalized_value(matching[0].press_group))

    group_cache: dict[tuple[str, str, str], tuple[str | None, str]] = {}
    group_issue_keys: set[tuple[str, str, str]] = set()
    grouped_files: list[_RoutedFile] = []
    for routed in routed_files:
        cache_key = (
            path_key(routed.source_scan.source),
            _work_key(routed.work),
            normalized_value(routed.source_scan.press_format),
        )
        if cache_key not in group_cache:
            group_cache[cache_key] = _resolve_group(routed, explicit_group_usage)
        group, reason = group_cache[cache_key]
        if group is None:
            if cache_key not in group_issue_keys:
                group_issue_keys.add(cache_key)
                issues.append(
                    _issue(
                        "group-unresolved",
                        reason,
                        routed.source_scan.source,
                        work_name=routed.work.name,
                    )
                )
            continue
        routed.group = group
        routed.group_reason = reason
        grouped_files.append(routed)

    route_buckets: dict[tuple[str, str, str, str], list[_RoutedFile]] = {}
    for routed in grouped_files:
        key = (
            path_key(routed.source_scan.source),
            _work_key(routed.work),
            normalized_value(routed.source_scan.press_format),
            normalized_value(routed.group),
        )
        route_buckets.setdefault(key, []).append(routed)

    route_metadata: list[dict[str, Any]] = []
    source_route_counts: dict[str, int] = {}
    press_issue_keys: set[tuple[str, str, str, str]] = set()
    for bucket_key, bucket_files in route_buckets.items():
        sample = bucket_files[0]
        matching = sample.work.presses_for(sample.source_scan.press_format, sample.group)
        if len(matching) != 1:
            if bucket_key not in press_issue_keys:
                press_issue_keys.add(bucket_key)
                issues.append(
                    _issue(
                        "press-row-ambiguous",
                        "数据库中无法唯一定位对应的 collectioned 压制记录",
                        sample.source_scan.source,
                        work_name=sample.work.name,
                        press_group=sample.group,
                    )
                )
            continue
        press = matching[0]
        route = _route_id(
            root,
            sample.source_scan.source,
            sample.work,
            press.press_format,
            press.press_group,
        )
        source_key = path_key(sample.source_scan.source)
        source_route_counts[source_key] = source_route_counts.get(source_key, 0) + 1
        suggested_relpath, suggested_authority = _target_relpath(sample.work, press, settings)
        route_metadata.append(
            {
                "route_id": route,
                "source_key": source_key,
                "source": sample.source_scan.source,
                "work": sample.work,
                "press": press,
                "files": bucket_files,
                "suggested_relpath": suggested_relpath,
                "suggested_authority": suggested_authority,
            }
        )

    known_route_ids = {row["route_id"] for row in route_metadata}
    for unknown_route in sorted(set(route_overrides) - known_route_ids):
        issues.append(
            _issue(
                "route-override-not-found",
                "路由目标修正对应的 route_id 已不存在，请基于最新预览重新修改",
                route_id=unknown_route,
            )
        )

    assignments: list[dict[str, Any]] = []
    moves: list[dict[str, Any]] = []
    destination_sources: dict[str, str] = {}
    total_bytes = 0
    fallback_file_count = 0
    others_file_count = 0
    planned_category_directories: set[str] = set()
    for metadata in route_metadata:
        route = str(metadata["route_id"])
        source = Path(metadata["source"])
        work = metadata["work"]
        press = metadata["press"]
        suggested_relpath = str(metadata["suggested_relpath"])
        override_value = route_overrides.get(route)
        if override_value is None and path_key(source) in source_overrides:
            if source_route_counts[path_key(source)] != 1:
                issues.append(
                    _issue(
                        "legacy-override-ambiguous",
                        "该来源目录已拆成多个作品路由，旧 source 级目标修正不能安全套用；请按 route_id 分别修正",
                        source,
                    )
                )
                continue
            override_value = source_overrides[path_key(source)]
        if override_value is not None:
            try:
                target, relpath = _manual_target(root, override_value)
            except ValueError as exc:
                issues.append(
                    _issue(
                        "invalid-target-override",
                        str(exc),
                        source,
                        route_id=route,
                    )
                )
                continue
            authority = "user_override"
        else:
            relpath = suggested_relpath
            authority = str(metadata["suggested_authority"])
            if not _validate_relpath(relpath):
                issues.append(
                    _issue(
                        "invalid-target",
                        f"数据库或推导出的目标相对目录非法：{relpath}",
                        source,
                        route_id=route,
                    )
                )
                continue
            target = (root / relpath).resolve()
        if any(
            path_key(target) == path_key(candidate)
            or _path_under(target, candidate)
            for candidate in sources
        ):
            issues.append(
                _issue(
                    "unsafe-target",
                    "目标目录不能等于或位于任一待整理源目录内",
                    target,
                    route_id=route,
                )
            )
            continue
        if not _path_under(target, root) or path_key(target) == path_key(source):
            issues.append(
                _issue(
                    "unsafe-target",
                    "目标目录越界或与源目录相同",
                    target,
                    route_id=route,
                )
            )
            continue

        assignment_moves: list[dict[str, Any]] = []
        assignment_bytes = 0
        classifier_ids: set[str] = set()
        classifier_rules: set[str] = set()
        layout_categories: set[str] = set()
        category_directories: set[str] = set()
        work_stages: set[str] = set()
        work_reasons: set[str] = set()
        route_relative_paths = tuple(
            routed_file.relative_path for routed_file in metadata["files"]
        )
        for routed in metadata["files"]:
            try:
                decision: LayoutDecision = classifier_registry.classify(
                    ClassificationContext(
                        relative_path=routed.relative_path,
                        route_relative_paths=route_relative_paths,
                        work_name=work.name,
                        press_format=press.press_format,
                        press_group=press.press_group,
                        source_dir_name=source.name,
                        target_dir_name=target.name,
                    )
                )
                layout_relative = _category_layout_path(
                    suggested_relpath,
                    press,
                    settings,
                    decision,
                )
            except (TypeError, ValueError) as exc:
                issues.append(
                    _issue(
                        "layout-classification-failed",
                        str(exc),
                        routed.source_file,
                        route_id=route,
                    )
                )
                continue
            destination = target / layout_relative
            if not _path_under(destination, target):
                issues.append(
                    _issue(
                        "unsafe-layout-target",
                        "分类器生成的目标路径越出压制目标目录",
                        destination,
                        route_id=route,
                    )
                )
                continue
            parent_issue = _destination_parent_issue(target, destination)
            if parent_issue is not None:
                code, message, issue_path = parent_issue
                issues.append(_issue(code, message, issue_path, route_id=route))
                continue
            if routed.source_file.name != destination.name:
                issues.append(
                    _issue(
                        "filename-change",
                        "目标文件名发生变化，已拒绝",
                        routed.source_file,
                        route_id=route,
                    )
                )
                continue
            try:
                stat = routed.source_file.stat()
            except OSError as exc:
                issues.append(
                    _issue(
                        "stat-failed",
                        f"无法读取文件状态：{exc}",
                        routed.source_file,
                        route_id=route,
                    )
                )
                continue
            destination_key = path_key(destination)
            if destination.exists():
                issues.append(
                    _issue(
                        "destination-exists",
                        "目标文件已存在，禁止覆盖",
                        destination,
                        route_id=route,
                    )
                )
                continue
            previous = destination_sources.get(destination_key)
            if previous:
                issues.append(
                    _issue(
                        "planned-collision",
                        f"多个源文件将写入同一目标；另一个源文件为：{previous}",
                        destination,
                        route_id=route,
                    )
                )
                continue
            destination_sources[destination_key] = str(routed.source_file)
            move = {
                "route_id": route,
                "source": str(routed.source_file),
                "target": str(destination),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "source_dir": str(source),
                "source_relpath": str(routed.relative_path),
                "target_relative_path": str(layout_relative),
                "destination_relpath": str(destination.relative_to(root)),
                "work_name": work.name,
                "classification_stage": routed.work_stage,
                "classification_reason": routed.work_reason,
                "classifier_id": decision.classifier_id,
                "classification_rule": decision.rule_id,
                "layout_stage": decision.stage,
                "layout_reason": decision.reason,
                "layout_category": decision.category,
                "layout_inner_path": str(decision.relative_path),
                "category_dir": str(target / layout_relative.parts[0]),
                "category_relpath": str((target / layout_relative.parts[0]).relative_to(root)),
            }
            assignment_moves.append(move)
            assignment_bytes += stat.st_size
            classifier_ids.add(decision.classifier_id)
            classifier_rules.add(decision.rule_id)
            layout_categories.add(decision.category)
            category_directory = str(target / layout_relative.parts[0])
            category_directories.add(category_directory)
            planned_category_directories.add(category_directory)
            work_stages.add(routed.work_stage)
            work_reasons.add(routed.work_reason)
            if decision.stage == "fallback":
                fallback_file_count += 1
            if decision.category == CATEGORY_OTHERS:
                others_file_count += 1

        moves.extend(assignment_moves)
        total_bytes += assignment_bytes
        assignments.append(
            {
                "route_id": route,
                "assignment_id": route,
                "source_dir": str(source),
                "target_dir": str(target),
                "target_relpath": relpath,
                "suggested_target_relpath": suggested_relpath,
                "work_name": work.name,
                "work_path": work.path,
                "domain": work.domain,
                "country": work.country,
                "release_type": work.release_type,
                "press_format": press.press_format,
                "press_group": press.press_group,
                "target_authority": authority,
                "work_match_reason": "；".join(sorted(work_reasons)),
                "group_match_reason": metadata["files"][0].group_reason,
                "classification_mode": "per_file",
                "classifier_ids": sorted(classifier_ids),
                "classification_rules": sorted(classifier_rules),
                "categories": sorted(layout_categories, key=str.casefold),
                "category_directories": sorted(category_directories, key=str.casefold),
                "category_count": len(category_directories),
                "routing_basis": sorted(work_stages),
                "file_count": len(assignment_moves),
                "bytes": assignment_bytes,
                "catalog_file": work.source_file,
            }
        )

    root_files = [
        child for child in root.iterdir() if child.is_file() and not child.is_symlink()
    ]
    if root_files:
        issues.append(
            _issue(
                "root-files",
                f"作品根目录含 {len(root_files)} 个直属文件；本插件只整理一级子目录，未规划这些文件",
                root,
            )
        )

    issues.sort(
        key=lambda row: (
            row.get("path", "").casefold(),
            row["code"],
            row["message"],
        )
    )
    unresolved_files.sort(key=lambda row: row["source"].casefold())
    assignments.sort(
        key=lambda row: (
            row["source_dir"].casefold(),
            row["work_name"].casefold(),
            row["route_id"],
        )
    )
    moves.sort(key=lambda row: row["source"].casefold())
    scanned_file_count = len(all_scanned_files)
    payload: dict[str, Any] = {
        "version": 4,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "catalog_root": str(catalog.catalog_root),
        "classifier_fingerprint": _CLASSIFIER_FINGERPRINT,
        "family_works": [work.name for work in family],
        "assignments": assignments,
        "moves": moves,
        "unresolved_files": unresolved_files,
        "issues": issues,
        "ready": (
            bool(moves)
            and not issues
            and len(moves) == scanned_file_count
        ),
        "summary": {
            "source_directory_count": len(
                {row["source_dir"] for row in assignments}
            ),
            "assignment_count": len(assignments),
            "route_count": len(assignments),
            "target_directory_count": len(
                {row["target_dir"] for row in assignments}
            ),
            "press_target_directory_count": len(
                {row["target_dir"] for row in assignments}
            ),
            "category_directory_count": len(planned_category_directories),
            "file_count": len(moves),
            "scanned_file_count": scanned_file_count,
            "classified_file_count": len(moves),
            "fallback_file_count": fallback_file_count,
            "others_file_count": others_file_count,
            "unresolved_file_count": len(unresolved_files),
            "bytes": total_bytes,
            "issue_count": len(issues),
            "database_target_count": sum(
                1
                for row in assignments
                if row["target_authority"] == "database_press_path"
            ),
            "derived_target_count": sum(
                1
                for row in assignments
                if row["target_authority"] == "derived_from_catalog"
            ),
            "manual_target_count": sum(
                1
                for row in assignments
                if row["target_authority"] == "user_override"
            ),
        },
    }
    payload["plan_id"] = _stable_plan_id(payload)
    return payload


def _assert_move_unchanged(move: dict[str, Any]) -> tuple[Path, Path]:
    source = Path(str(move["source"]))
    target = Path(str(move["target"]))
    if not source.is_file():
        raise FileNotFoundError(f"源文件已不存在：{source}")
    stat = source.stat()
    if stat.st_size != int(move["size"]) or stat.st_mtime_ns != int(move["mtime_ns"]):
        raise ValueError(f"预览后源文件发生变化，请重新生成计划：{source}")
    if target.exists():
        raise FileExistsError(f"目标文件已存在，禁止覆盖：{target}")
    if source.name != target.name:
        raise ValueError(f"文件名变化，拒绝执行：{source.name} -> {target.name}")
    return source, target


def _remove_empty_source_tree(source_dir: Path) -> None:
    if not source_dir.exists():
        return
    descendants = sorted(
        (path for path in source_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in descendants:
        try:
            directory.rmdir()
        except OSError:
            pass
    source_dir.rmdir()


def apply_plan(plan: dict[str, Any], *, confirmation: str) -> dict[str, Any]:
    """Execute a previously reviewed plan. No destination file may be overwritten."""
    plan_id = str(plan.get("plan_id") or "")
    if plan_id and _stable_plan_id(plan) != plan_id:
        raise ValueError("计划内容与计划 ID 不一致，拒绝执行")
    if not plan_id or confirmation.strip() != plan_id:
        raise ValueError(f"确认码不匹配；必须完整输入本次预览的计划 ID：{plan_id}")
    if not plan.get("ready"):
        raise ValueError("计划存在问题或没有可移动文件，拒绝执行")

    checked = [_assert_move_unchanged(move) for move in plan.get("moves") or []]
    for _, target in checked:
        target.parent.mkdir(parents=True, exist_ok=True)

    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in checked:
            if target.exists():
                raise FileExistsError(f"执行期间目标文件出现，禁止覆盖：{target}")
            shutil.move(str(source), str(target))
            completed.append((source, target))
    except Exception as exc:
        rollback_errors: list[str] = []
        for source, target in reversed(completed):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    raise FileExistsError(str(source))
                shutil.move(str(target), str(source))
            except Exception as rollback_exc:  # pragma: no cover
                rollback_errors.append(f"{target} -> {source}: {rollback_exc}")
        detail = (
            f"；回滚失败：{'；'.join(rollback_errors)}"
            if rollback_errors
            else "；已回滚本次已移动文件"
        )
        raise OSError(f"移动失败：{exc}{detail}") from exc

    source_dirs = {
        Path(str(row["source_dir"]))
        for row in plan.get("assignments") or []
        if isinstance(row, dict) and row.get("source_dir")
    }
    cleanup_warnings: list[str] = []
    for source_dir in sorted(
        source_dirs,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            _remove_empty_source_tree(source_dir)
        except OSError as exc:
            cleanup_warnings.append(f"未能清理源目录 {source_dir}：{exc}")

    return {
        "ok": True,
        "plan_id": plan_id,
        "moved_file_count": len(completed),
        "moved_bytes": sum(int(move["size"]) for move in plan.get("moves") or []),
        "target_directories": sorted(
            {
                str(row["target_dir"])
                for row in plan.get("assignments") or []
            },
            key=str.casefold,
        ),
        "cleanup_warnings": cleanup_warnings,
    }
