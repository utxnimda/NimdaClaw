"""Plan missing catalog path bindings from strictly identified index records.

This module performs metadata reads only. It never scans shortcut directories,
modifies a catalog, creates shortcuts, or persists caches. Callers own preview,
confirmation, concurrency checks and the final catalog transaction.
"""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from typing import Any, Iterable, Mapping
import unicodedata

from work_catalog_yaml.paths import normalize_copied_path


_WORK_FIELDS = ("name", "country", "domain", "release_type", "begin_date", "end_date")
_PRESS_DIRECTORY_SUFFIX = re.compile(
    r"(?:^|[_\s])(?:BDRip|DVDRip|DVD|BD|BDMV|WEBRip|WEB-DL|HDTV|\d{3,4}[pi])(?:\([^()]*\))?$", re.IGNORECASE
)
_EPISODE_RANGE = re.compile(r"(?<!\d)(\d{1,3})\s*[-~〜–—]\s*(\d{1,3})\s*(?:話|集)?\s*[\])]?$")
_PURE_PRESS_DIRECTORY = re.compile(
    r"_?(?:BDRip|DVDRip|DVD|BD|BDMV|WEBRip|WEB-DL|HDTV|\d{3,4}[pi])(?:\([^()]*\))?(?:\s+Ver\.?\s*\d+)?$",
    re.IGNORECASE,
)
_EXPLICIT_TITLE_YEAR = re.compile(
    r"(?:[\s_]+(?P<plain>(?:19|20)\d{2})|\s*\((?P<paren>(?:19|20)\d{2})\)|\s*\[(?P<bracket>(?:19|20)\d{2})\])$"
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _relative(value: Any) -> str:
    value = normalize_copied_path(value).replace("\\", "/")
    if not value or PureWindowsPath(value).drive or value.startswith("/") or ":" in value:
        raise ValueError("必须为非空相对路径")
    if any(part in {"", ".", ".."} for part in value.split("/")) or any(ord(c) < 32 for c in value):
        raise ValueError("包含非法相对路径片段")
    return value


def _reference(work: Mapping[str, Any]) -> tuple[str, int]:
    relative = _relative(work.get("yaml_source_rel"))
    index = work.get("index_in_file")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("作品 index_in_file 必须为非负整数")
    return relative, index


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _title_identity(raw: Any) -> tuple[str, tuple[int, int] | None]:
    title = unicodedata.normalize("NFKC", _text(raw)).casefold()
    interval = None
    match = _EPISODE_RANGE.search(title)
    if match is not None:
        interval = (int(match.group(1)), int(match.group(2)))
        if interval[0] <= interval[1]:
            title = title[:match.start()].rstrip(" ([")
        else:
            interval = None
    # Keep digits, season numbers and years; no substring or fuzzy alias match.
    return "".join(char for char in title if char.isalnum()), interval


def split_explicit_title_year(title: Any) -> tuple[str, str]:
    """Split only an explicitly separated trailing release-year qualifier.

    This syntax helper does not decide whether a year belongs to the title.
    Callers must first compare the complete candidate with the catalog title;
    for example, the ``1999`` in a catalog title ``Space 1999`` stays literal.
    """
    normalized = unicodedata.normalize("NFKC", _text(title))
    match = _EXPLICIT_TITLE_YEAR.search(normalized)
    if match is None:
        return normalized, ""
    base = normalized[:match.start()].rstrip()
    if not base:
        return normalized, ""
    return base, next(value for value in match.groupdict().values() if value is not None)


def _directory_work_title(target: Path, press_format: str) -> str:
    name = unicodedata.normalize("NFKC", target.name)
    if _PURE_PRESS_DIRECTORY.fullmatch(name):
        return unicodedata.normalize("NFKC", target.parent.name)
    exact_suffix = re.compile(r"_" + re.escape(press_format) + r"(?:\([^()]*\))?$", re.IGNORECASE) if press_format else None
    suffix = exact_suffix.search(name) if exact_suffix is not None else None
    suffix = suffix or _PRESS_DIRECTORY_SUFFIX.search(name)
    if suffix is not None:
        name = name[:suffix.start()]
    return name


def catalog_work_matches_target_name(
    work: Mapping[str, Any], target: str | Path, press_format: str = "",
) -> bool:
    """Check a target's work name, allowing only a matching extra release year.

    Literal title digits and years are retained. A candidate's *additional*
    trailing year is accepted only when its remaining complete title matches
    and the qualifier equals the catalog's known broadcast year. This is not
    a fuzzy/substring matcher and does not weaken shared-target safeguards.
    """
    catalog_base = _title_identity(work.get("name"))[0]
    if not catalog_base:
        return False
    candidate = _directory_work_title(Path(target), press_format)
    if _title_identity(candidate)[0] == catalog_base:
        return True
    candidate_base, year = split_explicit_title_year(candidate)
    return bool(year and year == _broadcast_year(work) and _title_identity(candidate_base)[0] == catalog_base)


def _broadcast_year(work: Mapping[str, Any]) -> str:
    value = _text(work.get("begin_date"))
    return value[:4] if len(value) >= 4 and value[:4].isdigit() else ""


class _DirectoryProblem(ValueError):
    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


class _DirectoryInspector:
    def __init__(self) -> None:
        self.checked: dict[Path, _DirectoryProblem | None] = {}

    def ordinary(self, raw: Any) -> Path:
        value = normalize_copied_path(raw)
        path = Path(value)
        if not value or not path.is_absolute() or ".." in path.parts:
            raise _DirectoryProblem("目录必须为不含 .. 的绝对路径")
        # Do not resolve links before checking: even a junction pointing back
        # inside the allowed root is not an ordinary catalog binding.
        for current in (path, *path.parents):
            if current not in self.checked:
                problem = None
                try:
                    metadata = current.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or (
                        getattr(metadata, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    ):
                        problem = _DirectoryProblem(f"目录经过符号链接或重解析点：{current}")
                    elif not stat.S_ISDIR(metadata.st_mode):
                        problem = _DirectoryProblem(f"路径不是普通目录：{current}")
                except FileNotFoundError:
                    problem = _DirectoryProblem(f"目录不存在：{current}", missing=True)
                except OSError as exc:
                    problem = _DirectoryProblem(f"无法安全核对目录 {current}：{exc}")
                self.checked[current] = problem
            if self.checked[current] is not None:
                raise self.checked[current]  # type: ignore[misc]
        return path

    def inside(self, raw: Any, roots: list[Path]) -> Path:
        path = self.ordinary(raw)
        if not any(path != root and path.is_relative_to(root) for root in roots):
            raise _DirectoryProblem("目录必须位于配置的资源根目录之下，不能等于资源根目录")
        return path


def plan_catalog_bindings(
    works: Iterable[dict[str, Any]],
    index_items: Iterable[dict[str, Any]],
    *,
    resource_roots: Iterable[str | Path],
    shortcut_targets: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return ``{mappings, issues, summary}`` without modifying any input.

    ``works`` has the shape of ``link_index._load_catalog_works``. Mappings are
    accepted by the existing explicit catalog-mapping writer; their ``press``
    list includes only newly filled press_path fields. Issues marked blocking
    represent conflicts/unsafe evidence, whereas absent index targets are
    unresolved records that remain unchanged.
    """
    work_list = list(works)
    issues: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    inspector = _DirectoryInspector()
    counts = {"work_count": len(work_list), "press_count": 0, "mapped_work_count": 0,
              "mapped_press_count": 0, "unchanged_press_count": 0}

    def issue(code: str, message: str, work: Mapping[str, Any] | None = None,
              press: Mapping[str, Any] | None = None, *, blocking: bool = True,
              target_path: str = "") -> None:
        work = work or {}
        press = press or {}
        relative = _text(work.get("yaml_source_rel"))
        index = work.get("index_in_file")
        issues.append({"code": code, "message": message, "error": message,
                       "yaml_source_rel": relative, "index_in_file": index,
                       "work_key": f"{relative}#{index}" if relative else "",
                       "name": _text(work.get("name")), "press_key": _text(press.get("press_key")),
                       "target_path": target_path, "blocking": blocking})

    roots: list[Path] = []
    for raw_root in resource_roots:
        try:
            root = inspector.ordinary(str(raw_root))
            if root not in roots:
                roots.append(root)
        except _DirectoryProblem as exc:
            issue("resource-root-unsafe", str(exc), blocking=not exc.missing, target_path=str(raw_root))
    if not roots:
        issue("resource-roots-empty", "没有可用的普通资源根目录，不能推断数据库绑定", blocking=False)

    by_ref: dict[tuple[str, int], list[dict[str, Any]]] = {}
    shared_targets: dict[str, list[dict[str, Any]]] = {}
    for item in index_items:
        if not isinstance(item, dict):
            continue
        try:
            by_ref.setdefault(_reference(item), []).append(item)
            raw_target = normalize_copied_path(item.get("target_path"))
            if raw_target and Path(raw_target).is_absolute():
                shared_targets.setdefault(_path_key(Path(raw_target)), []).append(item)
        except ValueError:
            continue  # Invalid external index identities cannot match a work.
    work_ref_counts: dict[tuple[str, int], int] = {}
    for work in work_list:
        if isinstance(work, dict):
            try:
                ref = _reference(work)
                work_ref_counts[ref] = work_ref_counts.get(ref, 0) + 1
            except ValueError:
                pass

    for work in work_list:
        if not isinstance(work, dict):
            issue("work-invalid", "作品记录必须为对象")
            continue
        try:
            ref = _reference(work)
        except ValueError as exc:
            issue("work-identity-invalid", str(exc), work)
            continue
        if work_ref_counts[ref] != 1:
            issue("work-identity-ambiguous", "同一个数据库作品引用出现多次，拒绝自动绑定", work)
            continue
        presses = work.get("press")
        if not isinstance(presses, list) or not presses:
            issue("press-empty", "作品没有可绑定的压制记录", work, blocking=False)
            continue
        counts["press_count"] += len(presses)
        if any(field not in work or not isinstance(work[field], str) for field in _WORK_FIELDS):
            issue("work-identity-incomplete", "作品身份字段不完整，无法严格核对索引", work)
            continue
        if any(not _text(work.get(field)) for field in _WORK_FIELDS[:4]):
            issue("work-identity-incomplete", "作品名称、国家或类型为空，拒绝推断索引关联", work)
            continue
        root_raw = normalize_copied_path(work.get("path"))
        work_root = None
        if root_raw:
            try:
                work_root = inspector.inside(root_raw, roots)
            except _DirectoryProblem as exc:
                issue("catalog-work-path-invalid", f"已有作品 path 不自动覆盖：{exc}", work, target_path=root_raw)
                continue

        targets: list[tuple[dict[str, Any], Path, str]] = []
        all_resolved = True
        seen_press_keys: set[str] = set()
        key_counts = Counter(_text(press.get("press_key")) for press in presses if isinstance(press, dict))
        duplicate_press_keys = {key for key, count in key_counts.items() if count > 1}
        for press in presses:
            if not isinstance(press, dict):
                issue("press-invalid", "压制记录必须为对象", work)
                all_resolved = False
                continue
            press_key = _text(press.get("press_key"))
            if not press_key or press_key in duplicate_press_keys or press_key in seen_press_keys:
                issue("press-identity-ambiguous", "压制 press_key 为空或重复，拒绝自动绑定", work, press)
                all_resolved = False
                continue
            seen_press_keys.add(press_key)
            existing_subdir = normalize_copied_path(press.get("press_path"))
            existing_target = None
            if existing_subdir:
                try:
                    existing_subdir = _relative(existing_subdir)
                    if work_root is not None:
                        existing_target = inspector.inside(str(work_root / existing_subdir), roots)
                        if not existing_target.is_relative_to(work_root) or existing_target == work_root:
                            raise ValueError("压制目录不在作品目录内")
                        counts["unchanged_press_count"] += 1
                except ValueError as exc:
                    issue("catalog-press-path-invalid", f"已有 press_path 不自动覆盖：{exc}", work, press)
                    all_resolved = False
                    continue

            candidates = [item for item in by_ref.get(ref, []) if item.get("press_key") == press_key]
            if len(candidates) != 1:
                if not candidates and existing_target is not None:
                    targets.append((press, existing_target, existing_subdir))
                    continue
                issue("index-missing" if not candidates else "index-ambiguous",
                      "没有唯一的索引压制记录，暂不补齐绑定", work, press, blocking=bool(candidates))
                all_resolved = False
                continue
            indexed = candidates[0]
            identity_ok = all(
                field in indexed and isinstance(indexed[field], str) and _text(indexed[field]) == _text(work[field])
                for field in _WORK_FIELDS
            )
            identity_ok = identity_ok and all(
                field in press and field in indexed and isinstance(press[field], str)
                and isinstance(indexed[field], str) and press[field] == indexed[field]
                for field in ("press_format", "press_group")
            )
            if not identity_ok:
                issue("index-identity-mismatch", "索引的作品身份、日期或压制字段与当前数据库不一致，拒绝错位绑定", work, press)
                all_resolved = False
                continue
            if indexed.get("target_source") == "resource_format_fallback":
                issue("index-target-format-only", "索引仅按压制格式匹配，尚未确认压制组，不能自动补齐数据库", work, press,
                      blocking=False, target_path=_text(indexed.get("target_path")))
                all_resolved = False
                continue
            target_raw = normalize_copied_path(indexed.get("target_path"))
            if not target_raw:
                if existing_target is not None:
                    targets.append((press, existing_target, existing_subdir))
                    continue
                issue("index-target-empty", "索引尚无目标目录，暂不补齐绑定", work, press, blocking=False)
                all_resolved = False
                continue
            try:
                target = inspector.inside(target_raw, roots)
            except _DirectoryProblem as exc:
                issue("index-target-missing" if exc.missing else "index-target-unsafe", str(exc), work, press,
                      blocking=not exc.missing, target_path=target_raw)
                all_resolved = False
                continue
            actual = shortcut_targets.get(_text(indexed.get("entry_key"))) if shortcut_targets is not None else None
            if actual is not None:
                try:
                    if not isinstance(actual, dict) or not normalize_copied_path(actual.get("target_path")):
                        raise _DirectoryProblem("实际快捷方式没有可验证的目标目录")
                    actual_target = inspector.inside(actual.get("target_path"), roots)
                    if _path_key(actual_target) != _path_key(target):
                        raise _DirectoryProblem("实际快捷方式目标与索引 target_path 不一致")
                except _DirectoryProblem as exc:
                    issue("shortcut-target-conflict", str(exc), work, press, target_path=target_raw)
                    all_resolved = False
                    continue
            if existing_target is not None and _path_key(existing_target) != _path_key(target):
                issue("catalog-target-conflict", "已有有效数据库绑定与索引目标不同，保留数据库绑定并要求人工确认", work, press,
                      target_path=target_raw)
                all_resolved = False
                continue
            if existing_target is None:
                work_base, work_interval = _title_identity(work.get("name"))
                same_target_items = shared_targets.get(_path_key(target), [])
                press_collision = any(
                    _reference(other) == ref and other.get("press_key") != press_key
                    and (other.get("press_format"), other.get("press_group"))
                    != (press.get("press_format"), press.get("press_group"))
                    for other in same_target_items
                )
                if press_collision:
                    issue("shared-press-target-needs-confirmation",
                          "同一目标同时关联不同压制格式/组，尚无完整数据库绑定证明多版本共存，需要人工确认",
                          work, press, target_path=str(target))
                    all_resolved = False
                    continue
                other_bindings = [other for other in same_target_items if _reference(other) != ref]
                shared_problem = False
                for other in other_bindings:
                    other_base, other_interval = _title_identity(other.get("name"))
                    different_years = _broadcast_year(work) != _broadcast_year(other)
                    disjoint_ranges = bool(work_interval and other_interval and (
                        work_interval[1] < other_interval[0] or other_interval[1] < work_interval[0]
                    ))
                    if work_base != other_base or (different_years and not disjoint_ranges):
                        issue("shared-target-needs-confirmation",
                              f"目标目录还关联其他作品/开播年份，需要人工确认：{_text(other.get('name'))} ({_broadcast_year(other)})",
                              work, press, target_path=str(target))
                        shared_problem = True
                        break
                if shared_problem:
                    all_resolved = False
                    continue
                if not catalog_work_matches_target_name(work, target, _text(press.get("press_format"))):
                    issue("target-name-needs-confirmation", "目标目录的作品名与数据库作品名不一致，不能自动推断作品根目录",
                          work, press, target_path=str(target))
                    all_resolved = False
                    continue
            targets.append((press, target, existing_subdir))

        if work_root is None:
            if not targets or not all_resolved:
                issue("work-root-unresolved", "压制目标未全部确认，不能用部分目标猜测作品根目录", work, blocking=False)
                continue
            try:
                # Use the common parent, never a press directory itself, even
                # for duplicate/shared targets or nested press directories.
                if len({_path_key(target.parent) for _, target, _ in targets}) != 1:
                    raise ValueError("压制目标位于不同父目录，不能自动将上级集合目录认定为作品根目录")
                inferred = os.path.commonpath([str(target.parent) for _, target, _ in targets])
                work_root = inspector.inside(inferred, roots)
                if _PRESS_DIRECTORY_SUFFIX.search(work_root.name):
                    raise ValueError("共同父目录看起来仍是压制目录，不能将其猜作作品根目录")
            except (ValueError, OSError) as exc:
                issue("work-root-unsafe", f"无法安全推断共同作品目录：{exc}", work)
                continue

        additions: list[dict[str, str]] = []
        inconsistent_existing = False
        for press, target, existing_subdir in targets:
            if target == work_root or not target.is_relative_to(work_root):
                issue("target-outside-work", "索引目标不在已有/推断的作品目录下，不能补齐 press_path", work, press,
                      target_path=str(target))
                inconsistent_existing = True
                continue
            relative = target.relative_to(work_root).as_posix()
            if existing_subdir:
                if _path_key(work_root / existing_subdir) != _path_key(target):
                    issue("catalog-press-path-conflict", "已有非空 press_path 与推断的目录不一致，不自动覆盖", work, press,
                          target_path=str(target))
                    inconsistent_existing = True
                continue
            additions.append({"press_key": _text(press["press_key"]), "press_path": relative})
        if not root_raw and inconsistent_existing:
            continue  # A new root affects all existing relative press paths.
        if additions or not root_raw:
            mappings.append({"yaml_source_rel": ref[0], "index_in_file": ref[1],
                             "path": root_raw or str(work_root), "press": additions})
            counts["mapped_work_count"] += 1
            counts["mapped_press_count"] += len(additions)

    return {"mappings": mappings, "issues": issues,
            "summary": {**counts, "issue_count": len(issues),
                        "blocking_issue_count": sum(bool(item["blocking"]) for item in issues)}}
