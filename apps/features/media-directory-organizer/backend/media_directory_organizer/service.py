"""Build read-only media plans and expose the compatible execution API."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from work_catalog_yaml.operation_progress import report_progress
from work_catalog_yaml.paths import normalize_copied_path

from media_directory_organizer.catalog import (
    CatalogMatchIndex,
    CatalogWork,
    MediaCatalog,
    PressRecord,
    normalize_press_group,
    normalized_identity,
    normalized_press_group,
    normalized_value,
    path_key,
)
from media_directory_organizer.classification import (
    CATEGORY_DISC,
    CATEGORY_OTHERS,
    ClassificationContext,
    ClassifierRegistry,
    DEFAULT_CLASSIFIER_REGISTRY,
    LAYOUT_CATEGORIES,
    LayoutDecision,
    canonical_resolution_episode_subdirectories,
    disc_version_subdirectories,
    is_resolution_press_format,
    is_vcb_family_group,
)
from media_directory_organizer.inference import parse_press_directory_name
from media_directory_organizer.filesystem import (
    assert_ordinary_path,
    contains_regular_file,
    is_reparse_point,
)
from media_directory_organizer.settings import OrganizerSettings
# Preserve service imports used by the CLI, web layer, and older integrations.
from media_directory_organizer.execution import (
    MediaRollbackError,
    _assert_move_unchanged,
    _remove_empty_descendants,
    _remove_empty_source_tree,
    apply_plan,
)
from media_directory_organizer.plan_identity import stable_plan_id as _stable_plan_id


_INVALID_WINDOWS_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DISC_VIDEO_EXTENSIONS = frozenset(
    {
        ".avi",
        ".m2ts",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".rmvb",
        ".ts",
        ".webm",
        ".wmv",
    }
)
_CLASSIFIER_FINGERPRINT = "virtual-folder-filter-layout-v10-empty-group-20261001"
_TRAILING_GROUP_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\((?P<group>[^()]*)\)$")
_BRACKET_CONTENT_RE = re.compile(r"\[([^\[\]\r\n]+)\]")
_BRACKET_YEAR_OR_DATE_RE = re.compile(
    r"(?:19|20)\d{2}(?:\s*[-~]\s*\d{2,4})?|\d{6,8}",
    re.IGNORECASE,
)
_BRACKET_RESOLUTION_RE = re.compile(
    r"(?:\d{3,4}[pi]|\d{3,4}\s*[x×]\s*\d{3,4}|[248]k)",
    re.IGNORECASE,
)
_BRACKET_EPISODE_RE = re.compile(
    r"(?:ep(?:isodes?)?\s*)?\d{1,4}(?:v\d+)?"
    r"(?:\s*[-~]\s*\d{1,4})?(?:\s*(?:fin|end))?"
    r"(?:\s*\+\s*(?:sp|ova|oad|ncop|nced|menu)(?:x\d+)?)*",
    re.IGNORECASE,
)
_BRACKET_CATALOG_CODE_RE = re.compile(r"[A-Z]{2,10}(?:-\d{2,8}){1,3}")
_BRACKET_TECH_TOKEN_RE = re.compile(
    r"(?:"
    r"\d+|\d+(?:bit|bits?|ch)|\d{3,4}[pi]|\d{3,4}x\d{3,4}|"
    r"bd|bdrip|bdmv|blu(?:ray|-ray)|dvd|dvdrip|web|webdl|webrip|remux|"
    r"h26[45]|x26[45]|avc|hevc|av1|mpeg\d*|vc-?1|"
    r"ma\d+p|hi\d+p|\d+bit|hdr\d*|sdr|"
    r"flac(?:x\d+)?|aac(?:x\d+)?|ac3|eac3|dts(?:hd)?|truehd|pcm|wav(?:x\d+)?|"
    r"mkv|mp4|mka|ass|srt|ssa|vtt|sup|pgs|eac|cue|log|iso|mds|mdf|nrg|"
    r"ttf|otf|woff2?|"
    r"jpe?g|png|tiff?|webp|bmp|gif|"
    r"chs|cht|sc|tc|gb|big5|zh|zho|hans|hant|jpn|japanese|eng|english|"
    r"fin|end|sp|ova|oad|ncop|nced|menus?|fonts?|scans?|logos?|pv|cm|"
    r"trailers?|previews?|cds?|audio|music|ost|soundtracks?|commentar(?:y|ies)|"
    r"character|songs?|images?|pictures?|booklets?|covers?|"
    r"bonus|extras?|crc32|md5|sha1|sha256|sha512|xxh3|"
    r"v\d+|vol\d+"
    r")",
    re.IGNORECASE,
)
_BRACKET_TECH_PHRASE_RE = re.compile(
    r"(?:raws?|fansubs?|subs?|subtitle|studio|team|字幕|压制|"
    r"producer\s+logo|通常盤|限定盤|初回盤|特装盤|"
    r"special\s+edition|limited\s+edition)",
    re.IGNORECASE,
)
_OUTSIDE_ANCILLARY_LABEL_RE = re.compile(
    r"(?:op(?:\s*\+\s*ed)?|ed|ost|opening|ending|soundtracks?|"
    r"character\s+songs?|insert\s+songs?|music|audio|commentar(?:y|ies)|"
    r"fonts?|scans?|images?|booklets?|covers?)\b",
    re.IGNORECASE,
)


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
    if work.source_index >= 0:
        return f"{work.source_file}\0#{work.source_index}\0{work.name}"
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
        normalized_press_group(item.press_group)
        for item in same_format
    }
    if len(distinct_groups) > 1 and normalize_press_group(press.press_group):
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
    *,
    inner_relative_path: Path | None = None,
    flatten_disc_root: bool = False,
) -> Path:
    inner_path = inner_relative_path or decision.relative_path
    if flatten_disc_root and decision.category == CATEGORY_DISC:
        layout = inner_path
    else:
        category_dir = f"{_category_stem(suggested_relpath, press, settings)}_{decision.category}"
        layout = Path(category_dir) / inner_path
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
        if is_reparse_point(candidate):
            return "layout-reparse-point", "分类目标路径经过符号链接或目录联接", candidate
        if not candidate.is_dir():
            return "layout-parent-not-directory", "分类目标的父路径已被普通文件占用", candidate
    return None


def _manual_target(root: Path, raw_value: str) -> tuple[Path, str]:
    value = str(raw_value).strip()
    if not value:
        raise ValueError("人工目标目录不能为空")
    configured = Path(value).expanduser()
    lexical = configured if configured.is_absolute() else root / configured
    assert_ordinary_path(lexical, root=root)
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


def _scan_files(
    source: Path,
    *,
    excluded_directories: Iterable[Path] = (),
) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    issues: list[dict[str, str]] = []
    report_progress("扫描待整理文件", completed=0, unit="文件", detail=str(source))
    if is_reparse_point(source):
        return [], [_issue("reparse-point", "拒绝移动符号链接或目录联接", source)]
    excluded_keys = {path_key(path) for path in excluded_directories}
    scanned_directories = 0

    def record_walk_error(error: OSError) -> None:
        issues.append(
            _issue(
                "scan-failed",
                f"无法完整扫描待整理目录：{error}",
                getattr(error, "filename", None) or source,
            )
        )

    for current, dirnames, filenames in os.walk(
        source,
        topdown=True,
        followlinks=False,
        onerror=record_walk_error,
    ):
        current_path = Path(current)
        scanned_directories += 1
        if scanned_directories % 100 == 0:
            report_progress("遍历待整理子目录", completed=scanned_directories, unit="目录", detail=str(current_path))
        safe_dirnames: list[str] = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            if path_key(child) in excluded_keys:
                continue
            if is_reparse_point(child):
                issues.append(_issue("reparse-point", "拒绝移动符号链接或目录联接", child))
            else:
                safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames
        for filename in sorted(filenames):
            child = current_path / filename
            if is_reparse_point(child):
                issues.append(_issue("symlink-file", "拒绝移动符号链接文件", child))
                continue
            if child.is_file():
                files.append(child)
                if len(files) % 100 == 0:
                    report_progress("扫描待整理文件", completed=len(files), unit="文件", detail=str(child))
    report_progress("来源目录文件扫描完成", completed=len(files), unit="文件", detail=str(source))
    return files, issues


def _catalog_ref_for_work(
    work: CatalogWork,
    *,
    catalog_root: Path,
) -> dict[str, Any] | None:
    if work.source_index < 0 or not work.source_sha256:
        return None
    try:
        source = Path(work.source_file).expanduser().resolve()
        relative = source.relative_to(catalog_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if source.parent != catalog_root.resolve() or source.suffix.casefold() != ".yaml":
        return None
    return {
        "yaml_source_rel": relative,
        "index_in_file": work.source_index,
        "work_name": work.name,
        "source_sha256": work.source_sha256,
    }


def _public_catalog_work(
    work: CatalogWork,
    *,
    catalog_root: Path,
) -> dict[str, Any]:
    return {
        "catalog_ref": _catalog_ref_for_work(work, catalog_root=catalog_root),
        "work": {
            "name": work.name,
            "date": {"start": work.start_date, "end": work.end_date},
            "domain": work.domain,
            "country": work.country,
            "release_type": work.release_type,
            "path": work.path,
            "presses": [
                {
                    "press_format": press.press_format,
                    "press_group": press.press_group,
                    "press_path": press.press_path,
                }
                for press in work.presses
            ],
        },
    }


def _catalog_target_rows(
    work_root: Path,
    family: Iterable[CatalogWork],
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


def _equivalent_shared_database_target_rows(
    rows: Iterable[Mapping[str, Any]],
) -> bool:
    """Return whether several catalog rows describe one physical encode.

    Split-cour database works may intentionally share one ``press_path``.  It
    is safe to collapse only the physical format/group binding; work ownership
    remains unresolved and is still selected by the normal work-routing
    rules.  Repeated rows from the same work are treated as a catalog conflict.
    """

    selected = tuple(rows)
    if len(selected) < 2 or any(
        row.get("authority") != "database_press_path" for row in selected
    ):
        return False
    signatures = {
        (
            normalized_value(str(row["press"].press_format)),
            normalized_press_group(str(row["press"].press_group)),
        )
        for row in selected
        if isinstance(row.get("press"), PressRecord)
    }
    work_keys = {
        _work_key(row["work"])
        for row in selected
        if isinstance(row.get("work"), CatalogWork)
    }
    return (
        len(signatures) == 1
        and len(work_keys) == len(selected)
        and bool(next(iter(signatures), ("",))[0])
    )


def _generated_category_children(
    directory: Path,
    target_rows: Iterable[Mapping[str, Any]],
    settings: OrganizerSettings,
) -> tuple[Path, ...]:
    """Return already-canonical category roots below one press directory."""

    expected_names: set[str] = set()
    for row in target_rows:
        press = row.get("press")
        if not isinstance(press, PressRecord):
            continue
        stem = _category_stem(str(row.get("relpath") or ""), press, settings)
        expected_names.update(
            normalized_value(f"{stem}_{category}") for category in LAYOUT_CATEGORIES
        )
    result: list[Path] = []
    for child in directory.iterdir():
        if not child.is_dir():
            continue
        if is_reparse_point(child):
            continue
        if normalized_value(child.name) in expected_names:
            result.append(child)
    return tuple(sorted(result, key=lambda path: path.name.casefold()))


def _directories_contain_regular_file(directories: Iterable[Path]) -> bool:
    """Return whether known category trees contain at least one ordinary file."""

    return any(contains_regular_file(directory) for directory in directories)


def _resolution_flat_press_is_settled(
    directory: Path,
    target_rows: tuple[Mapping[str, Any], ...],
    settings: OrganizerSettings,
    classifier_registry: ClassifierRegistry,
) -> bool:
    """Recognize flat or canonical episode resolution layouts without extras."""

    if not target_rows:
        return False
    first = target_rows[0]
    work = first.get("work")
    press = first.get("press")
    if not isinstance(work, CatalogWork) or not isinstance(press, PressRecord):
        return False
    if not all(
        isinstance(row.get("press"), PressRecord)
        and is_resolution_press_format(row["press"].press_format)
        for row in target_rows
    ):
        return False

    try:
        children = tuple(directory.iterdir())
        if not children:
            return False
        files: list[Path] = []
        for child in children:
            if is_reparse_point(child):
                return False
            if child.is_file():
                files.append(child)
                continue
            if not child.is_dir():
                return False
            episode_files = tuple(child.iterdir())
            if not episode_files or any(
                is_reparse_point(item) or not item.is_file() for item in episode_files
            ):
                return False
            files.extend(episode_files)
        relative_paths = tuple(child.relative_to(directory) for child in files)
        decisions = {
            path_key(child): classifier_registry.classify(
                ClassificationContext(
                    relative_path=child.relative_to(directory),
                    route_relative_paths=relative_paths,
                    work_name=work.name,
                    press_format=press.press_format,
                    press_group=press.press_group,
                    release_type=work.release_type,
                    source_dir_name=directory.name,
                    target_dir_name=directory.name,
                )
            )
            for child in files
        }
        if any(decision.category != CATEGORY_DISC for decision in decisions.values()):
            return False
        layout_items = tuple(
            (
                path_key(child),
                path_key(directory),
                child.relative_to(directory),
                decisions[path_key(child)],
            )
            for child in files
        )
        directory_stem = _category_stem(str(first.get("relpath") or ""), press, settings)
        preserved_directories = canonical_resolution_episode_subdirectories(
            layout_items,
            directory_stem=directory_stem,
            press_format=press.press_format,
        )
        if any(
            child.parent != directory and path_key(child) not in preserved_directories
            for child in files
        ):
            return False
        episode_directories = disc_version_subdirectories(
            layout_items,
            directory_stem=directory_stem,
            release_type=work.release_type,
            press_format=press.press_format,
        )
        return all(
            normalized_value(str(child.relative_to(directory).parent))
            == normalized_value(str(episode_directories[path_key(child)]))
            for child in files
            if path_key(child) in episode_directories
        )
    except (OSError, TypeError, ValueError):
        return False


def _press_directory_scan_state(
    directory: Path,
    target_rows: Iterable[Mapping[str, Any]],
    settings: OrganizerSettings,
    classifier_registry: ClassifierRegistry,
) -> tuple[bool, tuple[Path, ...], bool]:
    """Return whether a press directory is settled and safe subtrees to skip.

    A database or derived press target is not automatically considered
    organized just because its first-level name matches.  Root files and
    legacy containers such as ``CDs``/``Scans`` still need classification.
    Canonical category directories are skipped so a partial retry never nests
    an already-organized subtree a second time.
    """

    rows = tuple(target_rows)
    children = tuple(directory.iterdir())
    has_database_target = any(
        str(row.get("authority") or "") == "database_press_path"
        for row in rows
    )
    if not children:
        # An empty synthesized target is merely a possible destination for an
        # incoming release.  An explicit database press_path, however, is also
        # shortcut authority and must contain real media before it is settled.
        return not has_database_target, (), False
    if _resolution_flat_press_is_settled(
        directory,
        rows,
        settings,
        classifier_registry,
    ):
        return True, (), True
    canonical = _generated_category_children(directory, rows, settings)
    canonical_has_files = _directories_contain_regular_file(canonical)
    canonical_keys = {path_key(path) for path in canonical}
    unsettled = [child for child in children if path_key(child) not in canonical_keys]
    return (
        not unsettled
        and bool(canonical)
        and (canonical_has_files or not has_database_target),
        canonical,
        canonical_has_files,
    )


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


def _matching_works(
    candidates: Iterable[CatalogWork], settings: OrganizerSettings,
) -> CatalogMatchIndex:
    if isinstance(candidates, CatalogMatchIndex):
        return candidates
    return CatalogMatchIndex(candidates, settings.work_aliases)


def _exact_work_matches(
    value: str,
    candidates: Iterable[CatalogWork],
    settings: OrganizerSettings,
) -> tuple[CatalogWork, ...]:
    """Return only full normalized name/alias matches.

    A title extracted from ``<work>_<format>`` is complete work-name evidence;
    substring matching here could incorrectly collapse an unregistered sequel
    into a shorter existing title.
    """

    wanted = normalized_identity(value)
    if not wanted:
        return ()
    works = _matching_works(candidates, settings)
    matched = [work for work, _alias in works.exact_matches(wanted)]
    matched.sort(
        key=lambda work: (work.name.casefold(), work.source_file.casefold(), work.source_index)
    )
    return tuple(matched)


def _catalog_press_formats(catalog: MediaCatalog) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            press.press_format
            for work in catalog.works
            for press in work.presses
            if press.press_format.strip()
        )
    )


def _preferred_work_matches(
    matches: Iterable[CatalogWork],
    *,
    root_identity: str,
) -> tuple[CatalogWork, ...]:
    unique: dict[str, CatalogWork] = {_work_key(work): work for work in matches}
    matched_specific = [
        work for work in unique.values() if not root_identity or work.identity != root_identity
    ]
    matched_base = [
        work for work in unique.values() if root_identity and work.identity == root_identity
    ]
    chosen = matched_specific or matched_base
    chosen.sort(key=lambda work: (work.name.casefold(), work.source_file.casefold()))
    return tuple(chosen)


def _relaxed_work_matches(
    value: str,
    candidates: Iterable[CatalogWork],
    *,
    root_identity: str,
    settings: OrganizerSettings,
) -> tuple[tuple[CatalogWork, ...], dict[str, str]]:
    works = _matching_works(candidates, settings)
    value_identity = normalized_identity(value)
    matched_specific: list[CatalogWork] = []
    matched_base: list[CatalogWork] = []
    evidence: dict[str, str] = {}
    for work in works:
        signals: list[str] = []
        for alias, identity in works.aliases_for(work):
            if identity and (
                _boundary_contains(value, alias) if len(identity) <= 3
                else identity in value_identity
            ):
                signals.append(alias)
        work_identity = works.identity_for(work)
        is_base = bool(root_identity and work_identity == root_identity)
        if (
            not is_base
            and root_identity
            and work_identity.startswith(root_identity)
            and len(work_identity) > len(root_identity)
        ):
            tail = work_identity[len(root_identity) :]
            if _alias_matches(value, tail, short_boundary=len(tail) <= 3):
                signals.append(tail)
        if not signals:
            continue
        evidence[_work_key(work)] = "、".join(dict.fromkeys(signals))
        (matched_base if is_base else matched_specific).append(work)
    chosen = matched_specific or matched_base
    chosen.sort(key=lambda work: (work.name.casefold(), work.source_file.casefold()))
    return tuple(chosen), evidence


def _bracket_segments(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    result: list[str] = []
    seen: set[str] = set()
    for matched in _BRACKET_CONTENT_RE.finditer(normalized):
        segment = matched.group(1).strip()
        key = normalized_value(segment)
        if not segment or key in seen:
            continue
        seen.add(key)
        result.append(segment)
    return tuple(result)


def _exact_bracket_work_matches(
    segment: str,
    candidates: Iterable[CatalogWork],
    settings: OrganizerSettings,
) -> tuple[list[CatalogWork], dict[str, str]]:
    identity = normalized_identity(segment)
    matches: list[CatalogWork] = []
    evidence: dict[str, str] = {}
    if not identity:
        return matches, evidence
    works = _matching_works(candidates, settings)
    for work, alias in works.exact_matches(identity):
        matches.append(work)
        evidence[_work_key(work)] = f"方括号完整匹配：{alias}"
    return matches, evidence


def _is_technical_bracket_segment(
    segment: str,
    candidates: Iterable[CatalogWork],
    settings: OrganizerSettings,
) -> bool:
    """Return whether a bracket is release metadata rather than a work title.

    Exact catalog work/alias matching is intentionally performed before this
    filter, so numeric or acronym titles remain matchable when the database has
    evidence for them.
    """

    value = unicodedata.normalize("NFKC", segment).strip()
    folded = value.casefold()
    compact = re.sub(r"\s+", "", folded)
    metadata_compact = re.sub(r"[^a-z0-9]+", "", folded)
    if not compact:
        return True
    if _BRACKET_YEAR_OR_DATE_RE.fullmatch(compact):
        return True
    if _BRACKET_RESOLUTION_RE.fullmatch(compact):
        return True
    if _BRACKET_EPISODE_RE.fullmatch(compact):
        return True
    if re.fullmatch(r"(?:s\d{1,2})?(?:e|ep)\d{1,4}(?:v\d+)?", compact):
        return True
    if re.fullmatch(
        r"(?:(?:menu|sp|ova|oad|ncop|nced|pv|cm|vol|volume)\d*){1,4}",
        metadata_compact,
    ):
        return True
    if re.fullmatch(r"(?:ova|oad)\d+(?:comic|bd|dvd)?\d*", metadata_compact):
        return True
    if re.fullmatch(r"\d+(?:bit)?\d+(?:khz|hz)", metadata_compact):
        return True
    if _BRACKET_CATALOG_CODE_RE.fullmatch(value):
        return True
    if normalized_value(value) in _matching_works(candidates, settings).press_values:
        return True
    if _detect_markers(value, settings.format_markers):
        return True
    if _detect_markers(value, settings.group_markers):
        return True
    if _BRACKET_TECH_PHRASE_RE.search(value):
        return True
    tokens = re.findall(r"[a-z]+\d*[a-z]*|\d+x\d+|\d+[a-z]*", folded)
    return bool(tokens) and all(_BRACKET_TECH_TOKEN_RE.fullmatch(token) for token in tokens)


def _outside_title_extension_hints(
    value: str,
    match: CatalogWork,
    candidates: Iterable[CatalogWork],
    settings: OrganizerSettings,
) -> tuple[str, ...]:
    """Return title-like text which strictly extends a matched work alias.

    Release names often put the group/format in brackets and the work title in
    plain text.  A relaxed alias match is safe for ``Known Work - 01.mkv`` but
    not for ``Known Work & Beyond - 01.mkv``: the latter likely names a sequel
    or special which may not exist in the catalog yet.
    """

    works = _matching_works(candidates, settings)
    result: list[str] = []
    for component in re.split(r"[\\/]", unicodedata.normalize("NFKC", value)):
        component = component.strip()
        if not component:
            continue
        folded = component.casefold()
        identity_chars: list[str] = []
        identity_positions: list[int] = []
        for index, char in enumerate(folded):
            if not char.isalnum():
                continue
            identity_chars.append(char)
            identity_positions.append(index)
        component_identity = "".join(identity_chars)
        if not component_identity:
            continue

        has_safe_alias = False
        has_extended_alias = False
        numbered_extension_hint = ""
        for alias, alias_identity in works.aliases_for(match):
            if len(alias_identity) < 5:
                continue
            offset = 0
            while True:
                position = component_identity.find(alias_identity, offset)
                if position < 0:
                    break
                raw_start = identity_positions[position]
                raw_end = identity_positions[position + len(alias_identity) - 1] + 1
                extras = (folded[:raw_start], folded[raw_end:])
                right_extra = extras[1].lstrip()
                numbered_match = re.match(
                    r"\s*([2-9])(?=$|\s|[._-])",
                    extras[1],
                )
                ancillary_tail = bool(
                    _OUTSIDE_ANCILLARY_LABEL_RE.match(right_extra.lstrip(" -_."))
                )
                # ``Known Work 2 - 01`` contains both a sequel number and an
                # episode number.  A one-digit number directly following the
                # alias is title evidence; ``Known Work - 01`` keeps the
                # separator before its episode number and remains technical.
                numbered_title_extension = numbered_match is not None
                if numbered_match is not None and not numbered_extension_hint:
                    numbered_extension_hint = (
                        component[:raw_end].rstrip()
                        + " "
                        + numbered_match.group(1)
                    ).strip()
                substantive = numbered_title_extension or any(
                    normalized_identity(extra)
                    and not _is_technical_bracket_segment(extra, works, settings)
                    for extra_index, extra in enumerate(extras)
                    if not (extra_index == 1 and ancillary_tail)
                )
                if substantive:
                    has_extended_alias = True
                else:
                    # A longer registered alias may fully explain a component
                    # which also contains a shorter alias.  That exact evidence
                    # must win over the shorter alias's apparent extension.
                    has_safe_alias = True
                offset = position + 1

        if not has_extended_alias or has_safe_alias:
            continue

        # Remove trailing episode/codec/container/checksum tokens so every
        # episode of one unknown related title contributes the same hint.
        display = numbered_extension_hint or component
        if not numbered_extension_hint:
            token_matches = list(re.finditer(r"[^\W_]+", display, re.UNICODE))
            cut = len(display)
            while token_matches:
                token = token_matches[-1]
                if not _BRACKET_TECH_TOKEN_RE.fullmatch(token.group(0)):
                    break
                cut = token.start()
                token_matches.pop()
            if cut < len(display):
                display = display[:cut].rstrip(" ._-+[]()")
        display = display.strip(" ._-") or component
        if display not in result:
            result.append(display)
    return tuple(result)


def _match_works(
    value: str,
    candidates: Iterable[CatalogWork],
    *,
    root_identity: str,
    settings: OrganizerSettings,
) -> tuple[tuple[CatalogWork, ...], dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """Match works and return structured bracket-title evidence.

    A complete bracket-to-name/alias match outranks substring matching.  A
    title-like bracket that only partially matches one catalog work is reported
    as unmatched so the caller can distinguish two cases: a source directory
    that already identifies one work may safely inherit that work, while a
    source without a unique owner remains unresolved.  A bracket that clearly
    contains multiple catalog works remains an intentional ambiguous match.
    """

    works = _matching_works(candidates, settings)
    hints: list[str] = []
    recognized_hints: list[str] = []
    unmatched_hints: list[str] = []
    bracket_matches: list[CatalogWork] = []
    bracket_evidence: dict[str, str] = {}
    for segment in _bracket_segments(value):
        exact, exact_evidence = _exact_bracket_work_matches(segment, works, settings)
        if exact:
            hints.append(segment)
            recognized_hints.append(segment)
            bracket_matches.extend(exact)
            bracket_evidence.update(exact_evidence)
            continue
        if _is_technical_bracket_segment(segment, works, settings):
            continue
        hints.append(segment)
        relaxed, relaxed_evidence = _relaxed_work_matches(
            segment,
            works,
            root_identity=root_identity,
            settings=settings,
        )
        if len(relaxed) > 1:
            recognized_hints.append(segment)
            bracket_matches.extend(relaxed)
            for work in relaxed:
                signal = relaxed_evidence.get(_work_key(work), work.name)
                bracket_evidence[_work_key(work)] = f"方括号包含多个作品：{signal}"
        else:
            unmatched_hints.append(segment)

    if bracket_matches:
        chosen = _preferred_work_matches(bracket_matches, root_identity=root_identity)
        return chosen, bracket_evidence, tuple(recognized_hints), ()
    outside_brackets = _BRACKET_CONTENT_RE.sub(
        " ", unicodedata.normalize("NFKC", value)
    )
    outside_matches, outside_evidence = _relaxed_work_matches(
        outside_brackets,
        works,
        root_identity=root_identity,
        settings=settings,
    )
    if outside_matches:
        outside_extension_hints: tuple[str, ...] = ()
        if len(outside_matches) == 1:
            outside_extension_hints = _outside_title_extension_hints(
                outside_brackets,
                outside_matches[0],
                works,
                settings,
            )
        combined_unmatched = tuple(
            dict.fromkeys((*unmatched_hints, *outside_extension_hints))
        )
        if outside_extension_hints or (
            unmatched_hints
            and _unmatched_hints_extend_known_work(unmatched_hints, works, settings)
        ):
            combined_hints = tuple(dict.fromkeys((*hints, *outside_extension_hints)))
            return (), {}, combined_hints, combined_unmatched
        # Release groups are commonly placed in the first bracket while the
        # actual title follows it (``[UnknownGroup] Known Title``).  A known
        # database title outside brackets is stronger than an unknown bracket
        # and prevents the group label from becoming a false work hint.
        return outside_matches, outside_evidence, (), ()
    if unmatched_hints:
        # Do not let a shorter alias substring override the full, unknown title
        # contained in brackets.  The caller will expose the hints for manual
        # catalog registration/selection.
        return (), {}, tuple(hints), tuple(unmatched_hints)
    matches, evidence = _relaxed_work_matches(
        value,
        works,
        root_identity=root_identity,
        settings=settings,
    )
    return matches, evidence, tuple(hints), tuple(unmatched_hints)


def _family_with_direct_directory_evidence(
    anchored_family: Iterable[CatalogWork],
    *,
    catalog: MediaCatalog,
    direct_directories: Iterable[Path],
    root_identity: str,
    settings: OrganizerSettings,
) -> tuple[CatalogWork, ...]:
    """Extend an anchored root family with unique first-level work evidence.

    A physical root may be named after only one title while holding other
    independent titles in the same series (for example Grisaia's 果実, 迷宮
    and 楽園).  A full database title/alias in a first-level release directory
    is stronger evidence than the physical root's name.  Ambiguous matches are
    deliberately ignored and remain available for manual resolution later.
    """

    combined: list[CatalogWork] = []
    seen: set[str] = set()

    def append(work: CatalogWork) -> None:
        key = _work_key(work)
        if key in seen:
            return
        seen.add(key)
        combined.append(work)

    catalog_press_formats = _catalog_press_formats(catalog)
    catalog_works = _matching_works(catalog.works, settings)
    for work in anchored_family:
        append(work)
    for directory in sorted(direct_directories, key=lambda path: path.name.casefold()):
        parsed = parse_press_directory_name(
            directory.name,
            settings=settings,
            press_formats=catalog_press_formats,
        )
        if parsed is not None:
            exact_matches = _exact_work_matches(
                parsed["work_name"],
                catalog_works,
                settings,
            )
            if len(exact_matches) == 1:
                append(exact_matches[0])
            # A canonical suffix makes the complete prefix authoritative.
            # When that exact title is absent or duplicated, do not fall back
            # to a shorter substring match; the source binding UI must ask.
            continue
        matches, _evidence, _hints, _unmatched = _match_works(
            directory.name,
            catalog_works,
            root_identity=root_identity,
            settings=settings,
        )
        if len(matches) != 1 or not matches[0].presses:
            continue
        append(matches[0])
    return tuple(combined)


def _unmatched_hints_extend_known_work(
    hints: Iterable[str],
    candidates: Iterable[CatalogWork],
    settings: OrganizerSettings,
) -> bool:
    """Return whether an unknown title looks like a distinct related work.

    A romanized title can be completely different from the catalog's Japanese
    name and is therefore safe to treat as a release label when the catalog has
    only one possible work.  A longer title which contains a known name/alias
    (for example ``Kekkai Sensen & Beyond`` versus ``Kekkai Sensen``) is a much
    stronger signal for a sequel/special and must remain a directory-level
    ambiguity instead of silently collapsing into the base work.
    """

    known_identities = _matching_works(candidates, settings).known_identities
    for hint in hints:
        hint_identity = normalized_identity(hint)
        if not hint_identity:
            continue
        if any(
            known in hint_identity and len(hint_identity) > len(known)
            for known in known_identities
        ):
            return True
    return False


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
            normalized_press_group(press_group),
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
    work_name_hints: tuple[str, ...]
    unmatched_work_name_hints: tuple[str, ...]
    suggested_work_name: str
    directory_name_authoritative: bool
    directory_catalog_matches: tuple[CatalogWork, ...]
    group_selected: bool = False


@dataclass(frozen=True)
class _SourcePressOverride:
    source: Path
    press_format: str = ""
    press_group: str = ""
    press_group_selected: bool = False


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


@dataclass
class _PendingWorkRoute:
    """A file's preliminary work match before Disc ownership is applied."""

    source_scan: _SourceScan
    source_file: Path
    relative_path: Path
    work: CatalogWork | None
    work_reason: str
    work_stage: str
    candidate_names: list[str]
    file_work_name_hints: tuple[str, ...]
    file_unmatched_work_name_hints: tuple[str, ...]
    manual: bool = False
    disc_evidence: bool = False


def _apply_disc_work_inheritance(
    pending_routes: list[_PendingWorkRoute],
    classifier_registry: ClassifierRegistry,
) -> None:
    """Make ancillary files inherit the nearest unambiguous Disc owner.

    Work-title matches in music, scans, fonts and other extras are not proof
    that a release source actually contains another catalog work.  Only a
    video which the layout classifier considers ``Disc`` becomes an ownership
    anchor.  A subtree containing multiple known owners (or an unresolved Disc
    video) remains split/ambiguous and therefore keeps the normal per-file
    routing result.
    """

    by_source: dict[str, list[_PendingWorkRoute]] = {}
    for pending in pending_routes:
        by_source.setdefault(path_key(pending.source_scan.source), []).append(pending)

    unresolved_owner = "\0unresolved-disc-owner"
    for source_routes in by_source.values():
        route_relative_paths = tuple(
            pending.relative_path for pending in source_routes
        )
        evidence_by_subtree: dict[tuple[str, ...], set[str]] = {}
        evidence_path: dict[tuple[tuple[str, ...], str], Path] = {}
        works_by_key: dict[str, CatalogWork] = {}

        for pending in source_routes:
            try:
                decision = classifier_registry.classify(
                    ClassificationContext(
                        relative_path=pending.relative_path,
                        route_relative_paths=route_relative_paths,
                        work_name=pending.work.name if pending.work is not None else "",
                        press_format=pending.source_scan.press_format,
                        press_group=pending.source_scan.explicit_group,
                        release_type=(
                            pending.work.release_type
                            if pending.work is not None
                            else (
                                pending.source_scan.source_matches[0].release_type
                                if len(pending.source_scan.source_matches) == 1
                                else ""
                            )
                        ),
                        source_dir_name=pending.source_scan.source.name,
                    )
                )
            except (TypeError, ValueError):
                # The normal layout pass reports classifier failures with full
                # route context.  A failed probe must only disable inheritance.
                continue
            if (
                decision.category != CATEGORY_DISC
                or pending.relative_path.suffix.casefold() not in _DISC_VIDEO_EXTENSIONS
            ):
                continue
            pending.disc_evidence = True
            owner_key = unresolved_owner
            if pending.work is not None:
                owner_key = _work_key(pending.work)
                works_by_key[owner_key] = pending.work
            # Every ancestor subtree, including the source root (), receives
            # this Disc anchor.  This supports a uniquely owned nested season
            # while retaining multiple works in a combined release root.
            directory_parts = pending.relative_path.parts[:-1]
            for depth in range(len(directory_parts) + 1):
                subtree = directory_parts[:depth]
                evidence_by_subtree.setdefault(subtree, set()).add(owner_key)
                evidence_path.setdefault((subtree, owner_key), pending.relative_path)

        for pending in source_routes:
            if pending.manual or pending.disc_evidence:
                continue
            directory_parts = pending.relative_path.parts[:-1]
            owner_key: str | None = None
            owner_subtree: tuple[str, ...] = ()
            evidence_blocked = False
            for depth in range(len(directory_parts), -1, -1):
                subtree = directory_parts[:depth]
                owners = evidence_by_subtree.get(subtree)
                if not owners:
                    continue
                # Ownership evidence only grows toward the root.  Once the
                # nearest evidence is ambiguous, a broader subtree cannot make
                # it safer to infer an owner.
                if unresolved_owner in owners or len(owners) != 1:
                    evidence_blocked = True
                    break
                owner_key = next(iter(owners))
                owner_subtree = subtree
                break
            owner: CatalogWork | None = None
            anchor: Path | None = None
            stage = ""
            scope = ""
            if owner_key is not None:
                owner = works_by_key[owner_key]
                anchor = evidence_path[(owner_subtree, owner_key)]
                scope = "当前子树" if owner_subtree else "当前来源目录"
                stage = "disc-subtree-owner" if owner_subtree else "disc-source-owner"
            elif not evidence_blocked and len(pending.source_scan.source_matches) == 1:
                # A source explicitly naming one catalog work is itself the
                # current-work boundary for CD/image-only releases.  An actual
                # unknown or different Disc anchor above prevents this fallback.
                owner = pending.source_scan.source_matches[0]
                scope = "当前来源目录"
                stage = "source-current-owner"
            if owner is None:
                continue
            if pending.work is not None and _work_key(pending.work) == _work_key(owner):
                # Keep a stronger directory/Disc ownership explanation when a
                # weak source-default assignment already chose the same work.
                if pending.work_stage != "source-default":
                    continue
            pending.work = owner
            if anchor is None:
                pending.work_reason = (
                    f"{scope}明确属于 {owner.name}，且没有其他作品的 Disc 正片资源，"
                    "其他资源继承该作品"
                )
            else:
                pending.work_reason = (
                    f"{scope}只有 {owner.name} 的 Disc 正片资源，其他资源继承该作品：{anchor}"
                )
            pending.work_stage = stage


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


def _parse_source_work_overrides(
    root: Path,
    raw_overrides: Mapping[str, str] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, tuple[Path, str]]:
    """Parse one work choice that applies to an entire first-level source."""

    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("source_work_overrides 必须是来源目录到数据库作品名的对象")
    parsed: dict[str, tuple[Path, str]] = {}
    for raw_source, raw_work in (raw_overrides or {}).items():
        if not isinstance(raw_source, str) or not isinstance(raw_work, str):
            issues.append(
                _issue(
                    "source-work-override-invalid-type",
                    "整目录作品修正的来源目录和作品名必须是字符串",
                )
            )
            continue
        source_value = raw_source.strip()
        work_name = raw_work.strip()
        if not source_value or not work_name:
            issues.append(
                _issue(
                    "source-work-override-empty",
                    "整目录作品修正不能包含空目录或空作品名",
                )
            )
            continue
        configured = Path(source_value).expanduser()
        source_path = (
            configured.resolve()
            if configured.is_absolute()
            else (root / configured).resolve()
        )
        if not _path_under(source_path, root) or source_path.parent != root:
            issues.append(
                _issue(
                    "source-work-override-outside-root",
                    "整目录作品修正只能指向作品根目录下的一级来源目录",
                    source_path,
                )
            )
            continue
        key = path_key(source_path)
        if key in parsed:
            issues.append(
                _issue(
                    "source-work-override-duplicate",
                    "同一来源目录配置了多个整目录作品修正",
                    source_path,
                )
            )
            continue
        parsed[key] = (source_path, work_name)
    return parsed


def _parse_source_catalog_work_overrides(
    root: Path,
    raw_overrides: Mapping[str, CatalogWork] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, tuple[Path, CatalogWork]]:
    """Parse server-resolved, exact catalog choices for first-level sources.

    This parameter is deliberately internal to the backend.  HTTP callers send
    an immutable catalog reference; the web facade validates that reference
    against the configured catalog before passing the matching ``CatalogWork``
    here.  A plain work name remains supported through
    ``source_work_overrides`` for backwards compatibility.
    """

    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("source_catalog_work_overrides 必须是来源目录到数据库作品的对象")
    parsed: dict[str, tuple[Path, CatalogWork]] = {}
    for raw_source, raw_work in (raw_overrides or {}).items():
        if not isinstance(raw_source, str) or not isinstance(raw_work, CatalogWork):
            issues.append(
                _issue(
                    "source-catalog-work-override-invalid-type",
                    "精确整目录作品修正必须由服务端解析为数据库作品",
                )
            )
            continue
        source_value = raw_source.strip()
        if not source_value:
            issues.append(
                _issue(
                    "source-catalog-work-override-empty",
                    "精确整目录作品修正缺少来源目录",
                )
            )
            continue
        configured = Path(source_value).expanduser()
        source_path = (
            configured.resolve()
            if configured.is_absolute()
            else (root / configured).resolve()
        )
        if not _path_under(source_path, root) or source_path.parent != root:
            issues.append(
                _issue(
                    "source-catalog-work-override-outside-root",
                    "精确整目录作品修正只能指向作品根目录下的一级来源目录",
                    source_path,
                )
            )
            continue
        key = path_key(source_path)
        if key in parsed:
            issues.append(
                _issue(
                    "source-catalog-work-override-duplicate",
                    "同一来源目录配置了多个精确数据库作品",
                    source_path,
                )
            )
            continue
        parsed[key] = (source_path, raw_work)
    return parsed


def _same_catalog_record(left: CatalogWork, right: CatalogWork) -> bool:
    if left.source_index >= 0 and right.source_index >= 0:
        return (
            path_key(left.source_file) == path_key(right.source_file)
            and left.source_index == right.source_index
            and left.name == right.name
        )
    return _work_key(left) == _work_key(right)


def _work_from_exact_catalog_choice(
    selected: CatalogWork,
    candidates: Iterable[CatalogWork],
) -> tuple[CatalogWork | None, str]:
    matches = [work for work in candidates if _same_catalog_record(work, selected)]
    if len(matches) == 1:
        return matches[0], "人工按 catalog_ref 精确指定整个来源目录"
    if not matches:
        return None, f"精确选择的数据库作品不支持当前来源压制格式：{selected.name}"
    return None, f"catalog_ref 在当前目录系列中无法唯一定位：{selected.name}"


def _parse_source_press_overrides(
    root: Path,
    raw_overrides: Mapping[str, Any] | None,
    *,
    issues: list[dict[str, str]],
) -> dict[str, _SourcePressOverride]:
    """Parse optional folder-level format/group choices.

    A format choice and a group choice are deliberately independent.  This lets
    the UI resolve an unknown format first, rebuild the read-only plan, and only
    then ask for a group when the selected format has more than one catalog row.
    """

    if raw_overrides is not None and not isinstance(raw_overrides, Mapping):
        raise ValueError("source_press_overrides 必须是来源目录到压制信息的对象")
    parsed: dict[str, _SourcePressOverride] = {}
    for raw_source, raw_press in (raw_overrides or {}).items():
        if not isinstance(raw_source, str) or not isinstance(raw_press, Mapping):
            issues.append(
                _issue("source-press-override-invalid-type", "来源压制修正必须包含目录和压制信息")
            )
            continue
        source_value = raw_source.strip()
        raw_format = raw_press.get("press_format")
        raw_group = raw_press.get("press_group")
        if (
            not source_value
            or (raw_format is not None and not isinstance(raw_format, str))
            or ("press_group" in raw_press and not isinstance(raw_group, str))
        ):
            issues.append(
                _issue("source-press-override-invalid-type", "来源压制修正的目录、格式和组简称必须是字符串")
            )
            continue
        press_format = (raw_format or "").strip()
        press_group = normalize_press_group(raw_group)
        press_group_selected = "press_group" in raw_press
        if not press_format and not press_group_selected:
            issues.append(
                _issue("source-press-override-empty", "来源压制修正至少要选择压制格式或压制组")
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
        parsed[key] = _SourcePressOverride(
            source=source_path,
            press_format=press_format,
            press_group=press_group,
            press_group_selected=press_group_selected,
        )
    return parsed


def _groups_matching_explicit(
    presses: Iterable[PressRecord],
    explicit_group: str,
) -> list[PressRecord]:
    """Prefer an exact real group, then a unique compatible VCB-family group."""

    candidates = list(presses)
    exact = [
        press for press in candidates
        if normalized_press_group(press.press_group) == normalized_press_group(explicit_group)
    ]
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
        normalized_press_group(press.press_group): press.press_group
        for press in _presses_for_format(work, press_format)
    }
    explicit = routed.source_scan.explicit_group
    if explicit or routed.source_scan.group_selected:
        matches = _groups_matching_explicit(
            _presses_for_format(work, press_format),
            explicit,
        )
        if len(matches) == 1:
            matched = matches[0].press_group
            reason = routed.source_scan.group_reason
            if normalized_press_group(matched) != normalized_press_group(explicit):
                reason += f"；数据库唯一 VCB 系真实组码为 {matched}"
            return matched, reason
        if len(matches) > 1:
            return None, (
                f"数据库作品 {work.name} 的 {press_format} 对应多个压制记录："
                + ", ".join(sorted({normalize_press_group(press.press_group) or '----（无）' for press in matches}, key=str.casefold))
            )
        return None, (
            f"数据库作品 {work.name} 的 {press_format} 没有压制组 {explicit or '----（无）'}；"
            f"候选为：{', '.join(sorted((normalize_press_group(value) or '----（无）' for value in available.values()), key=str.casefold)) or '无记录'}"
        )
    if len(available) == 1:
        return next(iter(available.values())), "数据库中该作品/格式只有一个压制组"
    used = explicit_group_usage.get((_work_key(work), normalized_value(press_format)), set())
    remaining = [group for key, group in available.items() if key not in used]
    if len(remaining) == 1:
        return remaining[0], "结合其他来源目录的明确组标记，匹配数据库中唯一未命中的压制组"
    return None, (
        "无法唯一确定压制组；数据库候选为："
        + (", ".join(sorted((normalize_press_group(value) or '----（无）' for value in available.values()), key=str.casefold)) or "无记录")
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
    source_work_overrides: Mapping[str, str] | None = None,
    source_catalog_work_overrides: Mapping[str, CatalogWork] | None = None,
    source_work_blockers: Mapping[str, str] | None = None,
    source_press_overrides: Mapping[str, Any] | None = None,
    classifier_registry: ClassifierRegistry = DEFAULT_CLASSIFIER_REGISTRY,
) -> dict[str, Any]:
    """Analyze a work directory without changing it and return a serializable plan."""
    root_text = normalize_copied_path(str(work_root)) if isinstance(work_root, (str, Path)) else ""
    if not root_text:
        raise ValueError("root 必须是非空路径")
    root = Path(root_text).expanduser().resolve()
    report_progress("分析作品目录及数据库匹配", detail=str(root))
    if not root.is_dir():
        raise FileNotFoundError(f"作品目录不存在：{root}")
    if settings.allowed_resource_roots and not any(
        _path_under(root, allowed) for allowed in settings.allowed_resource_roots
    ):
        raise ValueError(f"作品目录不在配置的资源库范围内：{root}")

    direct_dirs = sorted(
        (child for child in root.iterdir() if child.is_dir()),
        key=lambda path: path.name.casefold(),
    )
    root_identity = normalized_identity(root.name)
    family = _family_with_direct_directory_evidence(
        catalog.family_for_root(root),
        catalog=catalog,
        direct_directories=direct_dirs,
        root_identity=root_identity,
        settings=settings,
    )
    family = _matching_works(family, settings)
    candidates_by_format: dict[str, CatalogMatchIndex] = {}

    def matching_candidates(press_format: str) -> CatalogMatchIndex:
        key = normalized_value(press_format)
        if key not in candidates_by_format:
            candidates_by_format[key] = _matching_works(
                (work for work in family if _presses_for_format(work, press_format)),
                settings,
            )
        return candidates_by_format[key]

    catalog_press_formats = _catalog_press_formats(catalog)
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
    target_rows_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in target_rows:
        target_rows_by_path.setdefault(path_key(Path(row["target"]).resolve()), []).append(row)
    ambiguous_database_target_keys: set[str] = set()
    for target_key, rows in target_rows_by_path.items():
        database_rows = [
            row for row in rows if row.get("authority") == "database_press_path"
        ]
        if len(database_rows) <= 1 or _equivalent_shared_database_target_rows(
            database_rows
        ):
            continue
        ambiguous_database_target_keys.add(target_key)
        issues.append(
            _issue(
                "database-press-target-ambiguous",
                "同一个实体目录被不等价的数据库 press_path 记录占用，必须先修复数据库映射",
                database_rows[0]["target"],
                candidates=" / ".join(
                    sorted(
                        {
                            str(row["work"].name)
                            + " / "
                            + str(row["press"].press_format)
                            + " / "
                            + str(row["press"].press_group)
                            for row in database_rows
                        },
                        key=str.casefold,
                    )
                ),
            )
        )
    incoming_unmatched_has_files = _directories_contain_regular_file(
        child
        for child in direct_dirs
        if path_key(child) not in target_rows_by_path
    )
    sources: list[Path] = []
    settled_source_keys: set[str] = set()
    settled_source_names: set[str] = set()
    source_scan_exclusions: dict[str, tuple[Path, ...]] = {}
    source_has_canonical_files: dict[str, bool] = {}
    source_target_rows: dict[str, tuple[dict[str, Any], ...]] = {}
    for child in direct_dirs:
        if is_reparse_point(child):
            sources.append(child)
            continue
        matching_targets = target_rows_by_path.get(path_key(child), [])
        if not matching_targets:
            sources.append(child)
            continue
        settled, canonical_children, canonical_has_files = _press_directory_scan_state(
            child,
            matching_targets,
            settings,
            classifier_registry,
        )
        if (
            not settled
            and incoming_unmatched_has_files
            and not canonical_has_files
            and not any(child.iterdir())
        ):
            # During a migration, an empty database target can already exist
            # purely as the destination for another populated incoming
            # directory.  It is not itself a source and must not block those
            # moves; root-only empty targets remain fail-closed.
            settled = True
        if settled:
            settled_source_keys.add(path_key(child))
            settled_source_names.add(child.name)
            continue
        sources.append(child)
        source_scan_exclusions[path_key(child)] = canonical_children
        source_has_canonical_files[path_key(child)] = canonical_has_files
        source_target_rows[path_key(child)] = tuple(matching_targets)
    if selected:
        missing = sorted(
            selected - {source.name for source in sources} - settled_source_names
        )
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
    source_work_choices = _parse_source_work_overrides(
        root,
        source_work_overrides,
        issues=issues,
    )
    source_catalog_work_choices = _parse_source_catalog_work_overrides(
        root,
        source_catalog_work_overrides,
        issues=issues,
    )
    source_work_blocker_choices = _parse_source_work_overrides(
        root,
        source_work_blockers,
        issues=issues,
    )
    for source_key in sorted(set(source_work_choices) & set(source_catalog_work_choices)):
        source_path, _work = source_catalog_work_choices[source_key]
        issues.append(
            _issue(
                "source-work-override-conflict",
                "同一来源目录不能同时提交作品名修正和精确 catalog_ref 修正",
                source_path,
            )
        )
    for source_key in sorted(
        set(source_work_blocker_choices)
        & (set(source_work_choices) | set(source_catalog_work_choices))
    ):
        source_path, _reason = source_work_blocker_choices[source_key]
        issues.append(
            _issue(
                "source-work-blocker-conflict",
                "同一来源目录不能同时提交可执行作品绑定和未决绑定状态",
                source_path,
            )
        )
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
        if source_key in settled_source_keys:
            continue
        source_path = press_overrides[source_key].source
        issues.append(
            _issue(
                "source-press-override-not-found",
                "来源压制修正对应的一级目录不存在、未被选中或已经符合目标规则",
                source_path,
            )
        )
    for source_key in sorted(set(source_work_choices) - source_keys):
        if source_key in settled_source_keys:
            continue
        source_path, _work_name = source_work_choices[source_key]
        issues.append(
            _issue(
                "source-work-override-not-found",
                "整目录作品修正对应的一级来源目录不存在、未被选中或已经符合目标规则",
                source_path,
            )
        )
    for source_key in sorted(set(source_catalog_work_choices) - source_keys):
        if source_key in settled_source_keys:
            continue
        source_path, _work = source_catalog_work_choices[source_key]
        issues.append(
            _issue(
                "source-catalog-work-override-not-found",
                "精确整目录作品修正对应的一级来源目录不存在、未被选中或已经符合目标规则",
                source_path,
            )
        )
    for source_key in sorted(set(source_work_blocker_choices) - source_keys):
        if source_key in settled_source_keys:
            continue
        source_path, _reason = source_work_blocker_choices[source_key]
        issues.append(
            _issue(
                "source-work-blocker-not-found",
                "未决作品绑定对应的一级来源目录不存在、未被选中或已经符合目标规则",
                source_path,
            )
        )
    for file_path, _work_name in work_overrides.values():
        try:
            relative_parts = file_path.relative_to(root).parts
        except ValueError:
            continue
        if len(relative_parts) < 2:
            continue
        source_key = path_key(root / relative_parts[0])
        if (
            source_key not in source_work_choices
            and source_key not in source_catalog_work_choices
            and source_key not in source_work_blocker_choices
        ):
            continue
        issues.append(
            _issue(
                "source-file-work-override-conflict",
                "同一来源目录不能同时提交整目录作品修正和逐文件作品修正；请先确认单作品或多作品",
                file_path,
            )
        )

    family_formats = {
        normalized_value(press.press_format)
        for work in family
        for press in work.presses
    }
    source_scans: list[_SourceScan] = []
    all_scanned_files: set[str] = set()
    preserved_file_keys: set[str] = set()
    for source_index, source in enumerate(sources):
        report_progress("分析来源压制目录", completed=source_index, total=len(sources), unit="目录", detail=str(source))
        files, scan_issues = _scan_files(
            source,
            excluded_directories=source_scan_exclusions.get(path_key(source), ()),
        )
        issues.extend(scan_issues)
        all_scanned_files.update(path_key(path) for path in files)
        if not files:
            if source_has_canonical_files.get(path_key(source), False):
                # This is an existing press target whose canonical category
                # subtrees were deliberately excluded from the retry scan.
                # Empty legacy containers left beside those subtrees contain
                # no movable media and therefore mean "settled", not an empty
                # incoming source.  Any scan safety issues remain in `issues`.
                settled_source_keys.add(path_key(source))
                settled_source_names.add(source.name)
                continue
            if scan_issues:
                continue
            issues.append(_issue("empty-source", "待整理目录中没有可移动文件", source))
            continue
        database_bindings = [
            row
            for row in source_target_rows.get(path_key(source), ())
            if row.get("authority") == "database_press_path"
        ]
        shared_database_bindings: tuple[dict[str, Any], ...] = ()
        if len(database_bindings) > 1:
            if _equivalent_shared_database_target_rows(database_bindings):
                shared_database_bindings = tuple(database_bindings)
            elif path_key(source) not in ambiguous_database_target_keys:
                issues.append(
                    _issue(
                        "database-press-target-ambiguous",
                        "同一个一级目录被不等价的数据库 press_path 记录占用，必须先修复数据库映射",
                        source,
                        candidates=" / ".join(
                            sorted(
                                {
                                    str(row["work"].name)
                                    + " / "
                                    + str(row["press"].press_format)
                                    + " / "
                                    + str(row["press"].press_group)
                                    for row in database_bindings
                                },
                                key=str.casefold,
                            )
                        ),
                    )
                )
            if not shared_database_bindings:
                continue
        database_binding = database_bindings[0] if len(database_bindings) == 1 else None
        physical_database_binding = (
            database_binding
            if database_binding is not None
            else shared_database_bindings[0]
            if shared_database_bindings
            else None
        )
        parsed_press_directory = (
            parse_press_directory_name(
                source.name,
                settings=settings,
                press_formats=catalog_press_formats,
            )
            if physical_database_binding is None
            else None
        )
        parsed_press_format = (
            str(parsed_press_directory.get("press_format") or "")
            if parsed_press_directory is not None
            else ""
        )
        manual_press = press_overrides.get(path_key(source))
        formats = (
            [manual_press.press_format]
            if manual_press is not None and manual_press.press_format
            else [physical_database_binding["press"].press_format]
            if physical_database_binding is not None
            else [parsed_press_format]
            if parsed_press_format
            else [
                press_format
                for press_format in _detect_markers(source.name, settings.format_markers)
                if normalized_value(press_format) in family_formats
            ]
        )
        if (
            manual_press is not None
            and manual_press.press_format
            and normalized_value(formats[0]) not in family_formats
            and not (
                parsed_press_format
                and normalized_value(formats[0]) == normalized_value(parsed_press_format)
            )
        ):
            issues.append(
                _issue(
                    "source-press-format-not-in-catalog",
                    f"人工选择的压制格式不在作品数据库中：{formats[0]}",
                    source,
                )
            )
            continue
        if len(formats) != 1:
            available_formats: dict[str, str] = {}
            for work in family:
                for press in work.presses:
                    available_formats.setdefault(
                        normalized_value(press.press_format), press.press_format
                    )
            issue: dict[str, Any] = _issue(
                "format-ambiguous",
                "无法唯一识别压制格式"
                if not formats
                else f"识别到多个压制格式：{', '.join(formats)}",
                source,
                source_key=str(source),
            )
            issue["press_formats"] = sorted(
                available_formats.values(), key=str.casefold
            )
            issue["detected_press_formats"] = list(formats)
            issues.append(issue)
            continue
        parsed_group = (
            str(parsed_press_directory.get("press_group") or "")
            if parsed_press_directory is not None
            else ""
        )
        parsed_group_matches = (
            _detect_markers(parsed_group, settings.group_markers)
            if parsed_group
            else []
        )
        if parsed_group and not parsed_group_matches:
            parsed_group_matches = [
                configured
                for configured, suffix in settings.group_suffixes.items()
                if normalized_value(parsed_group)
                in {normalized_value(configured), normalized_value(suffix)}
            ]
        groups = (
            [manual_press.press_group]
            if manual_press is not None and manual_press.press_group_selected
            else [physical_database_binding["press"].press_group]
            if physical_database_binding is not None
            else parsed_group_matches
            if parsed_group_matches
            else _detect_markers(source.name, settings.group_markers)
        )
        if len(groups) > 1:
            available_groups: dict[str, str] = {}
            for work in family:
                for press in _presses_for_format(work, formats[0]):
                    available_groups.setdefault(
                        normalized_press_group(press.press_group), press.press_group
                    )
            issue = _issue(
                "group-ambiguous",
                f"识别到多个同等强度的压制组：{', '.join(groups)}",
                source,
                source_key=str(source),
                press_format=formats[0],
            )
            issue["press_groups"] = sorted(
                available_groups.values(), key=str.casefold
            )
            issue["detected_press_groups"] = list(groups)
            issues.append(issue)
            continue
        explicit_group = groups[0] if groups else ""
        manual_group = bool(
            manual_press is not None and manual_press.press_group_selected
        )
        candidates = matching_candidates(formats[0])
        directory_catalog_matches: tuple[CatalogWork, ...] = ()
        suggested_work_name = ""
        directory_name_authoritative = False
        if database_binding is not None:
            bound_work = database_binding["work"]
            source_matches = (bound_work,)
            source_evidence = {
                _work_key(bound_work): "数据库 press_path 精确匹配当前一级目录"
            }
            source_work_name_hints = ()
            source_unmatched_work_name_hints = ()
        elif shared_database_bindings:
            (
                source_matches,
                source_evidence,
                source_work_name_hints,
                source_unmatched_work_name_hints,
            ) = _match_works(
                source.name,
                candidates,
                root_identity=root_identity,
                settings=settings,
            )
        elif parsed_press_directory is not None:
            suggested_work_name = str(parsed_press_directory["work_name"])
            directory_name_authoritative = True
            directory_catalog_matches = _exact_work_matches(
                suggested_work_name,
                family,
                settings,
            )
            source_matches = tuple(
                work
                for work in directory_catalog_matches
                if _presses_for_format(work, formats[0])
            )
            source_evidence = {
                _work_key(work): (
                    "一级目录采用规范压制命名；压制后缀前的完整名称精确匹配数据库："
                    + suggested_work_name
                )
                for work in source_matches
            }
            source_work_name_hints = (suggested_work_name,)
            source_unmatched_work_name_hints = (
                () if directory_catalog_matches else (suggested_work_name,)
            )
        else:
            (
                source_matches,
                source_evidence,
                source_work_name_hints,
                source_unmatched_work_name_hints,
            ) = _match_works(
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
                    "人工选择的压制组"
                    if manual_group
                    else "数据库 press_path 精确匹配的压制组"
                    if database_binding is not None
                    else "多个作品共享的数据库 press_path 精确匹配压制组"
                    if shared_database_bindings
                    else (
                        "来源目录名中的压制组标记"
                        if explicit_group
                        else "来源目录未明确标记压制组"
                    )
                ),
                source_matches=source_matches,
                source_evidence=source_evidence,
                work_name_hints=source_work_name_hints,
                unmatched_work_name_hints=source_unmatched_work_name_hints,
                suggested_work_name=suggested_work_name,
                directory_name_authoritative=directory_name_authoritative,
                directory_catalog_matches=directory_catalog_matches,
                group_selected=bool(manual_group or physical_database_binding is not None),
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
    pending_routes: list[_PendingWorkRoute] = []
    used_work_override_keys: set[str] = set()
    used_source_work_override_keys: set[str] = set()
    used_source_catalog_work_override_keys: set[str] = set()
    used_source_work_blocker_keys: set[str] = set()
    for scan in source_scans:
        report_progress("识别文件对应作品", completed=0, total=len(scan.files), unit="文件", detail=str(scan.source))
        candidates = matching_candidates(scan.press_format)
        source_default = scan.source_matches[0] if len(scan.source_matches) == 1 else None
        file_match_cache: dict[
            str,
            tuple[
                tuple[CatalogWork, ...],
                dict[str, str],
                tuple[str, ...],
                tuple[str, ...],
            ],
        ] = {}
        primary_unknown_titles: set[str] = set()
        for file_index, source_file in enumerate(scan.files):
            if file_index % 100 == 0:
                report_progress("识别文件对应作品", completed=file_index, total=len(scan.files), unit="文件", detail=str(source_file))
            relative = source_file.relative_to(scan.source)
            matched = _match_works(
                str(relative),
                candidates,
                root_identity=root_identity,
                settings=settings,
            )
            file_match_cache[path_key(source_file)] = matched
            unmatched = matched[3]
            if unmatched:
                primary = normalized_identity(unmatched[0])
                if primary:
                    primary_unknown_titles.add(primary)
        source_has_multiple_unknown_titles = len(primary_unknown_titles) > 1
        source_key = path_key(scan.source)
        source_manual = source_work_choices.get(source_key)
        source_catalog_manual = source_catalog_work_choices.get(source_key)
        source_blocker = source_work_blocker_choices.get(source_key)
        source_manual_work: CatalogWork | None = None
        source_manual_reason = ""
        if source_catalog_manual is not None:
            used_source_catalog_work_override_keys.add(source_key)
            source_manual_work, source_manual_reason = _work_from_exact_catalog_choice(
                source_catalog_manual[1],
                candidates,
            )
        elif source_manual is not None:
            used_source_work_override_keys.add(source_key)
            source_manual_work, source_manual_reason = _work_from_override(
                source_manual[1],
                candidates,
            )
            source_manual_reason = source_manual_reason.replace(
                "人工逐文件指定",
                "人工指定整个来源目录",
            )
        elif source_blocker is not None:
            used_source_work_blocker_keys.add(source_key)
            source_manual_work = None
            source_manual_reason = source_blocker[1]
        has_explicit_file_work_choice = any(
            _path_under(file_path, scan.source)
            for file_path, _work_name in work_overrides.values()
        )
        if (
            (source_manual is not None or source_catalog_manual is not None)
            and source_manual_work is None
            and not has_explicit_file_work_choice
        ):
            # A rejected folder binding is one decision for the whole source,
            # rather than hundreds of unrelated file decisions.
            issues.append(
                _issue(
                    "work-ambiguous",
                    source_manual_reason,
                    scan.source,
                    source_key=str(scan.source),
                    press_format=scan.press_format,
                    candidates=" / ".join(candidate.name for candidate in candidates),
                )
            )
            continue
        if (
            scan.directory_name_authoritative
            and source_manual is None
            and source_catalog_manual is None
            and not has_explicit_file_work_choice
            and len(scan.source_matches) != 1
        ):
            if source_blocker is None:
                if not scan.directory_catalog_matches:
                    issues.append(
                        _issue(
                            "source-work-catalog-missing",
                            "规范压制目录已给出完整作品名，但数据库没有精确记录；"
                            "请先查找或手动补全作品信息："
                            + scan.suggested_work_name,
                            scan.source,
                            suggested_work_name=scan.suggested_work_name,
                        )
                    )
                elif len(scan.directory_catalog_matches) > 1:
                    issues.append(
                        _issue(
                            "source-work-selection-required",
                            "规范压制目录名精确匹配到多条数据库记录，必须选择具体记录："
                            + scan.suggested_work_name,
                            scan.source,
                        )
                    )
                else:
                    issues.append(
                        _issue(
                            "source-work-catalog-press-required",
                            "数据库作品已由规范目录名精确确定，但缺少当前压制格式记录；"
                            "请先补全压制信息："
                            + scan.press_format,
                            scan.source,
                            suggested_work_name=scan.suggested_work_name,
                        )
                    )
            # The source-level directory name is the authority.  Do not emit
            # one unresolved row per file or silently inherit another family
            # member while this single binding is awaiting confirmation.
            continue
        for source_file in scan.files:
            relative = source_file.relative_to(scan.source)
            source_file_key = path_key(source_file)
            manual = work_overrides.get(source_file_key)
            work: CatalogWork | None = None
            work_reason = ""
            work_stage = ""
            candidate_names: list[str] = []
            file_work_name_hints: tuple[str, ...] = ()
            file_unmatched_work_name_hints: tuple[str, ...] = ()
            if manual is not None:
                used_work_override_keys.add(source_file_key)
                work, work_reason = _work_from_override(manual[1], candidates)
                work_stage = "manual"
                if work is None:
                    candidate_names = [candidate.name for candidate in candidates]
            elif (
                source_manual is not None
                or source_catalog_manual is not None
                or source_blocker is not None
            ):
                work = source_manual_work
                work_reason = source_manual_reason
                work_stage = "manual-source"
                if work is None:
                    candidate_names = [candidate.name for candidate in candidates]
            else:
                (
                    file_matches,
                    file_evidence,
                    file_work_name_hints,
                    file_unmatched_work_name_hints,
                ) = file_match_cache[source_file_key]
                base_only_file_match = bool(
                    file_matches
                    and all(match.identity == root_identity for match in file_matches)
                )
                source_is_specific = bool(
                    source_default is not None
                    and source_default.identity != root_identity
                )
                if scan.directory_name_authoritative and source_default is not None:
                    work = source_default
                    work_reason = (
                        "一级目录采用规范压制命名，压制后缀前的完整作品名作为整目录归属："
                        + scan.suggested_work_name
                    )
                    work_stage = "directory-press-name"
                elif base_only_file_match and source_is_specific:
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
                elif source_is_specific:
                    # A first-level directory that uniquely names a non-root
                    # catalog work is strong ownership evidence.  Registered
                    # work matches above still override it, but unrelated
                    # release/CD title brackets must not turn every file into
                    # a per-file ambiguity.
                    work = source_default
                    source_signal = scan.source_evidence.get(_work_key(work), work.name)
                    work_reason = (
                        "文件未命中其他数据库作品，继承一级目录唯一明确的作品："
                        + source_signal
                    )
                    if file_unmatched_work_name_hints:
                        work_reason += "；未登记标题仅作提示：" + " / ".join(
                            file_unmatched_work_name_hints
                        )
                    work_stage = "source-default"
                elif source_default is not None and not source_has_multiple_unknown_titles:
                    unmatched_hints = tuple(
                        dict.fromkeys(
                            (
                                *scan.unmatched_work_name_hints,
                                *file_unmatched_work_name_hints,
                            )
                        )
                    )
                    if file_unmatched_work_name_hints and _unmatched_hints_extend_known_work(
                        unmatched_hints,
                        candidates,
                        settings,
                    ):
                        candidate_names = [candidate.name for candidate in candidates]
                        work_reason = (
                            "文件方括号中的作品名候选可能是来源目录基础作品的独立续作或特别篇，"
                            "需要按来源目录确认单作品或多作品："
                            + " / ".join(unmatched_hints)
                        )
                    else:
                        work = source_default
                        source_signal = scan.source_evidence.get(_work_key(work), work.name)
                        if file_unmatched_work_name_hints:
                            work_reason = (
                                "文件方括号中的作品名候选未匹配数据库，但来源目录只包含唯一作品，"
                                "直接继承该作品："
                                + source_signal
                                + "；未匹配候选："
                                + " / ".join(file_unmatched_work_name_hints)
                            )
                        else:
                            work_reason = (
                                "文件名未包含作品标记，继承来源目录的唯一作品："
                                + source_signal
                            )
                        work_stage = "source-default"
                elif file_unmatched_work_name_hints:
                    unmatched_hints = tuple(
                        dict.fromkeys(
                            (
                                *scan.unmatched_work_name_hints,
                                *file_unmatched_work_name_hints,
                            )
                        )
                    )
                    if (
                        len(candidates) == 1
                        and not source_has_multiple_unknown_titles
                        and not _unmatched_hints_extend_known_work(
                            unmatched_hints,
                            candidates,
                            settings,
                        )
                    ):
                        work = candidates[0]
                        work_reason = (
                            "方括号作品名尚未登记为数据库别名，但当前系列和压制格式只有一个作品，"
                            "且没有检测到其他已登记作品，按单作品目录直接继承："
                            + work.name
                            + "；候选仅作提示："
                            + " / ".join(unmatched_hints)
                        )
                        work_stage = "catalog-singleton-unregistered-alias"
                    else:
                        candidate_names = [candidate.name for candidate in candidates]
                        work_reason = (
                            "同一来源目录检测到多个未登记作品名候选，需要先确认该目录是单作品还是多作品："
                            if source_has_multiple_unknown_titles
                            else "文件方括号中的作品名候选未匹配数据库，且来源目录不能唯一确定作品："
                        ) + " / ".join(file_unmatched_work_name_hints)
                elif len(candidates) == 1:
                    unmatched_hints = tuple(
                        dict.fromkeys(
                            (
                                *scan.unmatched_work_name_hints,
                                *file_unmatched_work_name_hints,
                            )
                        )
                    )
                    if source_has_multiple_unknown_titles or (
                        unmatched_hints
                        and _unmatched_hints_extend_known_work(
                            unmatched_hints,
                            candidates,
                            settings,
                        )
                    ):
                        candidate_names = [candidates[0].name]
                        work_reason = (
                            "同一来源目录检测到多个未登记作品名候选，"
                            if source_has_multiple_unknown_titles
                            else "方括号中的作品名候选可能是数据库基础作品的独立续作或特别篇，"
                        ) + "需要按来源目录确认单作品或多作品：" + " / ".join(
                            unmatched_hints
                        )
                    else:
                        work = candidates[0]
                        if unmatched_hints:
                            work_reason = (
                                "方括号作品名尚未登记为数据库别名，但当前系列和压制格式只有一个作品，"
                                "按单作品目录直接继承："
                                + work.name
                                + "；候选仅作提示："
                                + " / ".join(unmatched_hints)
                            )
                            work_stage = "catalog-singleton-unregistered-alias"
                        else:
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
            pending_routes.append(
                _PendingWorkRoute(
                    source_scan=scan,
                    source_file=source_file,
                    relative_path=relative,
                    work=work,
                    work_reason=work_reason,
                    work_stage=work_stage,
                    candidate_names=candidate_names,
                    file_work_name_hints=file_work_name_hints,
                    file_unmatched_work_name_hints=file_unmatched_work_name_hints,
                    manual=(
                        manual is not None
                        or source_manual is not None
                        or source_catalog_manual is not None
                        or source_blocker is not None
                    ),
                )
            )

    _apply_disc_work_inheritance(pending_routes, classifier_registry)
    for pending in pending_routes:
        if pending.work is None:
            scan = pending.source_scan
            work_name_hints = list(
                dict.fromkeys((*scan.work_name_hints, *pending.file_work_name_hints))
            )
            unmatched_work_name_hints = list(
                dict.fromkeys(
                    (
                        *scan.unmatched_work_name_hints,
                        *pending.file_unmatched_work_name_hints,
                    )
                )
            )
            unresolved = {
                "source": str(pending.source_file),
                "source_dir": str(scan.source),
                "source_relpath": str(pending.relative_path),
                "reason": pending.work_reason,
                "candidates": sorted(
                    dict.fromkeys(pending.candidate_names), key=str.casefold
                ),
                "work_name_hints": work_name_hints,
                "unmatched_work_name_hints": unmatched_work_name_hints,
            }
            unresolved_files.append(unresolved)
            issue: dict[str, Any] = _issue(
                "work-unresolved" if unmatched_work_name_hints else "work-ambiguous",
                pending.work_reason,
                pending.source_file,
                candidates=" / ".join(unresolved["candidates"]),
            )
            issue["work_name_hints"] = work_name_hints
            issue["unmatched_work_name_hints"] = unmatched_work_name_hints
            issues.append(issue)
            continue
        routed_files.append(
            _RoutedFile(
                source_scan=pending.source_scan,
                source_file=pending.source_file,
                relative_path=pending.relative_path,
                work=pending.work,
                work_reason=pending.work_reason,
                work_stage=pending.work_stage,
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
    for unused_key in sorted(
        set(source_work_choices) - used_source_work_override_keys
    ):
        source_path, _work_name = source_work_choices[unused_key]
        if unused_key not in source_keys:
            continue
        issues.append(
            _issue(
                "source-work-override-not-scanned",
                "整目录作品修正对应的来源目录未能进入扫描计划",
                source_path,
            )
        )
    for unused_key in sorted(
        set(source_catalog_work_choices) - used_source_catalog_work_override_keys
    ):
        source_path, _work = source_catalog_work_choices[unused_key]
        if unused_key not in source_keys:
            continue
        issues.append(
            _issue(
                "source-catalog-work-override-not-scanned",
                "精确整目录作品修正对应的来源目录未能进入扫描计划",
                source_path,
            )
        )
    for unused_key in sorted(
        set(source_work_blocker_choices) - used_source_work_blocker_keys
    ):
        source_path, _reason = source_work_blocker_choices[unused_key]
        if unused_key not in source_keys:
            continue
        issues.append(
            _issue(
                "source-work-blocker-not-scanned",
                "未决作品绑定对应的来源目录未能进入扫描计划",
                source_path,
            )
        )

    report_progress("应用压制组分类规则并规划目标目录", completed=0, total=len(routed_files), unit="文件")
    explicit_group_usage: dict[tuple[str, str], set[str]] = {}
    for routed in routed_files:
        explicit = routed.source_scan.explicit_group
        if not explicit and not routed.source_scan.group_selected:
            continue
        matching = _groups_matching_explicit(
            _presses_for_format(routed.work, routed.source_scan.press_format),
            explicit,
        )
        if len(matching) == 1:
            key = (_work_key(routed.work), normalized_value(routed.source_scan.press_format))
            explicit_group_usage.setdefault(key, set()).add(normalized_press_group(matching[0].press_group))

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
                available_groups: dict[str, str] = {}
                for press in _presses_for_format(
                    routed.work, routed.source_scan.press_format
                ):
                    available_groups.setdefault(
                        normalized_press_group(press.press_group), press.press_group
                    )
                issue: dict[str, Any] = _issue(
                    "group-unresolved",
                    reason,
                    routed.source_scan.source,
                    work_name=routed.work.name,
                    press_format=routed.source_scan.press_format,
                    source_key=str(routed.source_scan.source),
                )
                issue["press_groups"] = sorted(
                    available_groups.values(), key=str.casefold
                )
                issues.append(issue)
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
            normalized_press_group(routed.group),
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
            lexical_target = root / relpath
            try:
                assert_ordinary_path(lexical_target, root=root)
            except ValueError as exc:
                issues.append(_issue("unsafe-target", str(exc), lexical_target, route_id=route))
                continue
            target = lexical_target.resolve()
        target_key = path_key(target)
        source_key = path_key(source)
        in_place = target_key == source_key
        if any(
            not (path_key(candidate) == source_key and in_place)
            and (
                target_key == path_key(candidate)
                or _path_under(target, candidate)
            )
            for candidate in sources
        ):
            issues.append(
                _issue(
                    "unsafe-target",
                    "目标目录不能等于或位于其他待整理源目录内",
                    target,
                    route_id=route,
                )
            )
            continue
        if not _path_under(target, root):
            issues.append(
                _issue(
                    "unsafe-target",
                    "目标目录越出作品根目录",
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
        classified_decisions: dict[str, LayoutDecision] = {}
        for file_index, routed in enumerate(metadata["files"]):
            if file_index % 100 == 0:
                report_progress("应用文件类型分类规则", completed=file_index, total=len(metadata["files"]), unit="文件", detail=str(routed.source_file))
            try:
                decision: LayoutDecision = classifier_registry.classify(
                    ClassificationContext(
                        relative_path=routed.relative_path,
                        route_relative_paths=route_relative_paths,
                        work_name=work.name,
                        press_format=press.press_format,
                        press_group=press.press_group,
                        release_type=work.release_type,
                        source_dir_name=source.name,
                        target_dir_name=target.name,
                    )
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
            classified_decisions[path_key(routed.source_file)] = decision

        try:
            resolution_disc_only = (
                is_resolution_press_format(press.press_format)
                and len(classified_decisions) == len(metadata["files"])
                and bool(classified_decisions)
                and all(
                    decision.category == CATEGORY_DISC
                    for decision in classified_decisions.values()
                )
            )
            disc_layout_items = tuple(
                (
                    path_key(routed.source_file),
                    path_key(routed.source_scan.source),
                    routed.relative_path,
                    classified_decisions[path_key(routed.source_file)],
                )
                for routed in metadata["files"]
                if path_key(routed.source_file) in classified_decisions
            )
            preserved_episode_directories = (
                canonical_resolution_episode_subdirectories(
                    disc_layout_items,
                    directory_stem=_category_stem(suggested_relpath, press, settings),
                    press_format=press.press_format,
                )
                if resolution_disc_only and in_place
                else {}
            )
            disc_version_directories = disc_version_subdirectories(
                disc_layout_items,
                directory_stem=_category_stem(suggested_relpath, press, settings),
                release_type=work.release_type,
                press_format=press.press_format,
            )
        except (TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "layout-classification-failed",
                    str(exc),
                    source,
                    route_id=route,
                )
            )
            continue

        for file_index, routed in enumerate(metadata["files"]):
            if file_index % 100 == 0:
                report_progress("规划文件目标位置并检查冲突", completed=file_index, total=len(metadata["files"]), unit="文件", detail=str(routed.source_file))
            routed_key = path_key(routed.source_file)
            decision = classified_decisions.get(routed_key)
            if decision is None:
                continue
            episode_directory = disc_version_directories.get(routed_key)
            layout_inner_path = decision.relative_path
            if episode_directory is not None:
                if (
                    not layout_inner_path.parts
                    or normalized_value(layout_inner_path.parts[0])
                    != normalized_value(episode_directory.name)
                ):
                    layout_inner_path = episode_directory / layout_inner_path.name
            elif routed_key in preserved_episode_directories:
                layout_inner_path = (
                    preserved_episode_directories[routed_key] / layout_inner_path.name
                )
            elif resolution_disc_only:
                # Resolution releases commonly contain only one encoded video
                # plus subtitles per episode. Unrecognized source containers
                # remain packaging; verified existing episode layouts above
                # are deliberately preserved instead of being flattened.
                layout_inner_path = Path(layout_inner_path.name)
            try:
                layout_relative = _category_layout_path(
                    suggested_relpath,
                    press,
                    settings,
                    decision,
                    inner_relative_path=layout_inner_path,
                    flatten_disc_root=resolution_disc_only,
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
            if (
                destination_key == routed_key
                and resolution_disc_only
                and (
                    routed_key in preserved_episode_directories
                    or len(routed.relative_path.parts) == 1
                )
            ):
                # Verified episodes and flat root files can share a partially
                # settled release with raw packaging. Neither is a move or a
                # collision when it already occupies its planned destination.
                preserved_file_keys.add(routed_key)
                continue
            if destination.exists() or destination.is_symlink():
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
            category_root = (
                target
                if resolution_disc_only
                else target / layout_relative.parts[0]
            )
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
                "layout_inner_path": str(layout_inner_path),
                "disc_version_directory": (
                    str(episode_directory) if episode_directory is not None else ""
                ),
                "category_dir": str(category_root),
                "category_relpath": str(category_root.relative_to(root)),
            }
            assignment_moves.append(move)
            assignment_bytes += stat.st_size
            classifier_ids.add(decision.classifier_id)
            classifier_rules.add(decision.rule_id)
            layout_categories.add(decision.category)
            category_directory = str(category_root)
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
                "in_place": in_place,
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
                "catalog_ref": _catalog_ref_for_work(
                    work,
                    catalog_root=catalog.catalog_root,
                ),
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

    scans_by_source = {path_key(scan.source): scan for scan in source_scans}
    assignments_by_source: dict[str, list[dict[str, Any]]] = {}
    for assignment in assignments:
        assignments_by_source.setdefault(
            path_key(Path(str(assignment["source_dir"]))),
            [],
        ).append(assignment)

    def source_issues(source: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for issue in issues:
            raw_path = str(issue.get("path") or "").strip()
            if not raw_path:
                continue
            try:
                issue_path = Path(raw_path).expanduser().resolve()
            except OSError:
                continue
            if issue_path == source or _path_under(issue_path, source):
                rows.append(dict(issue))
        return rows

    source_work_bindings: list[dict[str, Any]] = []
    for source in direct_dirs:
        source_key = path_key(source)
        scan = scans_by_source.get(source_key)
        source_assignments = assignments_by_source.get(source_key, [])
        source_assignment_keys = {
            (
                str((row.get("catalog_ref") or {}).get("yaml_source_rel") or ""),
                int((row.get("catalog_ref") or {}).get("index_in_file", -1)),
                str(row.get("work_name") or ""),
            )
            for row in source_assignments
            if isinstance(row.get("catalog_ref"), Mapping)
        }
        resolved_works = [
            work
            for work in family
            if (
                str(
                    (
                        _catalog_ref_for_work(work, catalog_root=catalog.catalog_root)
                        or {}
                    ).get("yaml_source_rel")
                    or ""
                ),
                work.source_index,
                work.name,
            )
            in source_assignment_keys
        ]
        if (
            scan is not None
            and scan.directory_name_authoritative
            and len(scan.directory_catalog_matches) == 1
            and not resolved_works
        ):
            resolved_works = [scan.directory_catalog_matches[0]]
        if source_key in settled_source_keys and not resolved_works:
            target_works = [
                row["work"]
                for row in target_rows_by_path.get(source_key, [])
                if isinstance(row.get("work"), CatalogWork)
            ]
            resolved_works = list(
                {
                    (work.source_file, work.source_index, work.name): work
                    for work in target_works
                }.values()
            )

        if scan is not None and scan.directory_name_authoritative:
            candidate_works = list(scan.directory_catalog_matches)
        elif scan is not None:
            candidate_works = [
                work
                for work in family
                if _presses_for_format(work, scan.press_format)
            ]
        else:
            candidate_works = list(family)
        candidate_works = list(
            {
                (work.source_file, work.source_index, work.name): work
                for work in candidate_works
            }.values()
        )
        candidates = [
            _public_catalog_work(work, catalog_root=catalog.catalog_root)
            for work in sorted(
                candidate_works,
                key=lambda item: (
                    item.name.casefold(),
                    item.source_file.casefold(),
                    item.source_index,
                ),
            )
        ]
        resolved = [
            _public_catalog_work(work, catalog_root=catalog.catalog_root)
            for work in sorted(
                resolved_works,
                key=lambda item: (
                    item.name.casefold(),
                    item.source_file.casefold(),
                    item.source_index,
                ),
            )
        ]
        exact_choice = source_catalog_work_choices.get(source_key)
        name_choice = source_work_choices.get(source_key)
        selected_for_plan = not selected or source.name in selected
        if exact_choice is not None:
            state = "catalog_bound"
            requested_mode = "catalog"
            requested_work_name = exact_choice[1].name
            authority = "user_catalog_ref"
            selected_catalog_ref = _catalog_ref_for_work(
                exact_choice[1],
                catalog_root=catalog.catalog_root,
            )
        elif name_choice is not None:
            requested_mode = "manual"
            requested_work_name = name_choice[1]
            authority = "user_manual_name"
            selected_catalog_ref = (
                resolved[0].get("catalog_ref") if len(resolved) == 1 else None
            )
            state = "manual_matched" if len(resolved) == 1 else "manual_unresolved"
        elif source_key in settled_source_keys:
            state = "settled"
            requested_mode = "automatic"
            requested_work_name = ""
            authority = "database_press_path"
            selected_catalog_ref = (
                resolved[0].get("catalog_ref") if len(resolved) == 1 else None
            )
        elif not selected_for_plan:
            state = "not_selected"
            requested_mode = "automatic"
            requested_work_name = ""
            authority = "automatic"
            selected_catalog_ref = None
        elif len(resolved) == 1:
            state = "automatic_matched"
            requested_mode = "automatic"
            requested_work_name = ""
            authority = "automatic"
            selected_catalog_ref = resolved[0].get("catalog_ref")
        elif len(resolved) > 1:
            state = "automatic_multiple"
            requested_mode = "automatic"
            requested_work_name = ""
            authority = "automatic"
            selected_catalog_ref = None
        else:
            state = "unresolved"
            requested_mode = "automatic"
            requested_work_name = ""
            authority = "automatic"
            selected_catalog_ref = None

        suggested_work_name = (
            scan.suggested_work_name
            if scan is not None and scan.directory_name_authoritative
            else ""
        )
        catalog_press_required = bool(
            scan is not None
            and scan.directory_name_authoritative
            and len(scan.directory_catalog_matches) == 1
            and not _presses_for_format(
                scan.directory_catalog_matches[0],
                scan.press_format,
            )
        )

        inferred_press_paths = {
            (
                str(row.get("press_format") or ""),
                str(row.get("press_group") or ""),
            ): str(
                row.get("target_relpath")
                or row.get("suggested_target_relpath")
                or ""
            )
            for row in source_assignments
        }
        inferred_presses = set(inferred_press_paths)
        if not inferred_presses and scan is not None:
            inferred_presses.add((scan.press_format, scan.explicit_group))
        source_work_bindings.append(
            {
                "source_name": source.name,
                "source_path": str(source),
                "selected": selected_for_plan,
                "can_override": True,
                "state": state,
                "requested_mode": requested_mode,
                "requested_work_name": requested_work_name,
                "suggested_work_name": suggested_work_name,
                "suggestion_authority": (
                    "directory_press_suffix" if suggested_work_name else ""
                ),
                "authority": (
                    "directory_press_suffix"
                    if requested_mode == "automatic"
                    and suggested_work_name
                    and len(resolved) == 1
                    else authority
                ),
                "catalog_ref": selected_catalog_ref,
                "resolved_works": resolved,
                "candidates": candidates,
                "catalog_press_required": catalog_press_required,
                "catalog_repair_required": catalog_press_required,
                "registration_required": bool(
                    catalog_press_required
                    or (
                        state in {"unresolved", "manual_unresolved"}
                        and not candidates
                    )
                ),
                "next_action": (
                    "complete_manual"
                    if catalog_press_required
                    else
                    "select_catalog"
                    if state in {"automatic_multiple", "manual_unresolved"}
                    and candidates
                    else "search"
                    if state in {"unresolved", "manual_unresolved"}
                    and not candidates
                    else "none"
                ),
                "legacy_shortcut_authoritative": False,
                "inferred_press": [
                    {
                        "press_format": press_format,
                        "press_group": press_group,
                        "press_group_confirmed": bool(
                            (press_format, press_group) in inferred_press_paths
                            or (scan is not None and scan.group_selected)
                            or normalize_press_group(press_group)
                        ),
                        "suggested_press_path": inferred_press_paths.get(
                            (press_format, press_group),
                            "",
                        ),
                    }
                    for press_format, press_group in sorted(
                        inferred_presses,
                        key=lambda pair: (pair[0].casefold(), pair[1].casefold()),
                    )
                ],
                "issues": source_issues(source),
            }
        )

    binding_state_counts = {
        state: sum(1 for row in source_work_bindings if row["state"] == state)
        for state in sorted({str(row["state"]) for row in source_work_bindings})
    }
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
        "source_work_bindings": source_work_bindings,
        "source_work_binding_summary": {
            "total_count": len(source_work_bindings),
            "selected_count": sum(
                1 for row in source_work_bindings if row["selected"]
            ),
            "state_counts": binding_state_counts,
            "registration_required_count": sum(
                1
                for row in source_work_bindings
                if row.get("registration_required") is True
            ),
        },
        "legacy_shortcut_authoritative": False,
        "issues": issues,
        "ready": (
            bool(moves)
            and not issues
            and len(moves) + len(preserved_file_keys) == scanned_file_count
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
            "classified_file_count": len(moves) + len(preserved_file_keys),
            "preserved_file_count": len(preserved_file_keys),
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
    report_progress("媒体整理预览已生成，尚未移动文件", completed=scanned_file_count, total=scanned_file_count, unit="文件", detail=f"计划移动 {len(moves)}，待处理问题 {len(issues)}")
    return payload
