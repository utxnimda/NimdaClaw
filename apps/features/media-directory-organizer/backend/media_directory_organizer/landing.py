"""Preview-first onboarding for a catalog work, media layout and shortcuts."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from collection_detail.link_index import (
    apply_scoped_shortcuts_for_work,
    preview_scoped_shortcuts_for_work,
    shortcut_target_matches,
)
from collection_detail.save import (
    CatalogMutationReceipt,
    CatalogRollbackConflictError,
    _file_sha256,
    _new_work_from_row_patch,
    _strict_new_work_patch,
    _works_list_mut,
    apply_catalog_yaml_mutation,
    append_catalog_work_from_preview,
    catalog_write_transaction,
    preview_catalog_work_append,
    rollback_catalog_yaml_mutation,
)
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
from media_directory_organizer.inference import infer_source_press, suggest_press_paths
from media_directory_organizer.filesystem import (
    contains_regular_file as _directory_contains_regular_file,
    is_reparse_point as _is_reparse_point,
)
from media_directory_organizer.service import (
    MediaRollbackError,
    _scan_files,
    _stable_plan_id,
    apply_plan,
    build_plan,
)
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import load_jp_tv_entries_from_yaml
from work_catalog_yaml.media_groups import (
    load_media_group_registry,
    media_group_code_known,
    media_group_registry_api_payload,
)
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string
from work_catalog_yaml.operation_progress import report_progress
from work_catalog_yaml.paths import normalize_copied_path


# Preserve the existing landing import surface while keeping data preparation separate.
from media_directory_organizer.landing_drafts import (
    _source_title_is_technical,
    _source_group_tokens,
    _source_title_is_group,
    _source_work_title,
    _registration_work_drafts,
    _most_common,
    _scope_defaults,
    _candidate_values,
    _confidence_percent,
    _source_names,
    _normalize_draft,
    _normalize_draft_works,
    _draft_catalog_work,
    _retry_draft_patch,
    _repair_press_path,
    _selected_catalog_repair_patch,
    _repair_draft_patch,
)
from media_directory_organizer.landing_catalog import (
    CatalogReadSession,
    _catalog_source,
    _press_key_for_row,
    _catalog_entry_record,
    _catalog_record_for_assignment,
    _press_ref,
    _work_ref,
    _date_identity,
    _press_identity,
    _assert_retry_catalog_work_exact,
    _record_from_work_ref,
    _all_catalog_records_for_root,
    _iso_repair_date,
    _catalog_ref_for_record,
    _repair_draft_for_record,
    _catalog_records_matching_repair_draft,
    _public_repair_candidate,
    _registration_existing_catalog_candidates,
    _record_name_matches_patch,
    _record_path_matches_root,
)


_LANDING_LOCK = threading.RLock()
_PLAN_ID_LENGTH = 16
_REPAIR_OPERATION_RECEIPT_LIMIT = 128
_REPAIR_OPERATION_RECEIPTS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_REPAIR_PREVIEW_ENDPOINT = "/api/media-directory-organizer/landing/repair/preview"
_REPAIR_APPLY_ENDPOINT = "/api/media-directory-organizer/landing/repair/apply"
_REPAIRABLE_SHORTCUT_CODES = frozenset(
    {
        "catalog-work-not-found",
        "shortcut-catalog-work-invalid",
        "shortcut-catalog-path-missing",
        "shortcut-catalog-path-invalid",
        "shortcut-catalog-path-mismatch",
        "shortcut-catalog-press-not-found",
        "shortcut-catalog-press-empty",
        "shortcut-catalog-press-invalid",
        "shortcut-press-path-missing",
        "shortcut-database-target-mismatch",
    }
)


def organizer_transaction_lock() -> Any:
    """Serialize catalog/media/shortcut transactions across organizer flows."""

    return _LANDING_LOCK


def _preserve_catalog_after_media_failure(
    error: MediaRollbackError,
    *,
    changes: Any,
    receipts: list[CatalogMutationReceipt],
    operation_key: str,
    operation_id: str,
) -> None:
    """Keep committed metadata when media recovery needs manual intervention."""

    error.catalog_recovery = {
        "state": "preserved",
        "rolled_back": False,
        "changes": changes,
        "recovery_files": [
            {
                "target": str(receipt.target),
                "target_existed": receipt.target_existed,
                "history_path": str(receipt.history_path) if receipt.history_path else None,
                "written_sha256": receipt.written_sha256,
            }
            for receipt in receipts
        ],
    }
    error.operation[operation_key] = operation_id


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validated_root(raw: Any, settings: OrganizerSettings) -> Path:
    root_text = normalize_copied_path(raw)
    if not root_text:
        raise ValueError("root 不能为空")
    root = Path(root_text).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"作品目录不存在：{root}")
    if settings.allowed_resource_roots and not any(
        _path_under(root, allowed) for allowed in settings.allowed_resource_roots
    ):
        raise ValueError(f"作品目录不在配置的资源库范围内：{root}")
    return root


def _browse_settings_for_catalog(
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> JpTvBrowseSettings:
    if browse_settings.filesystem_root is None:
        raise ValueError("collection-detail 未配置可写数据库目录")
    if browse_settings.filesystem_root.resolve() != organizer_settings.catalog_root.resolve():
        raise ValueError("目录整理器与 collection-detail 使用的数据库目录不一致")
    return browse_settings


def discover_catalog_work_draft(
    root: str | Path,
    *,
    catalog: MediaCatalog,
    settings: OrganizerSettings,
) -> dict[str, Any]:
    root_path = _validated_root(str(root), settings)
    defaults = _scope_defaults(root_path, catalog)
    group_registry = load_media_group_registry()
    sources: list[dict[str, Any]] = []
    for source in sorted(
        (child for child in root_path.iterdir() if child.is_dir()),
        key=lambda path: path.name.casefold(),
    ):
        inference = infer_source_press(
            source.name,
            settings=settings,
            group_registry=group_registry,
        )
        format_details = list(inference.get("format_candidates") or [])
        group_details = list(inference.get("group_candidates") or [])
        formats = _candidate_values(format_details)
        groups = _candidate_values(group_details)
        sources.append(
            {
                "name": source.name,
                "path": str(source),
                "format_candidates": formats,
                "group_candidates": groups,
                "format_candidate_details": format_details,
                "group_candidate_details": group_details,
                "suggested_press_format": str(
                    inference.get("suggested_press_format") or ""
                ),
                "suggested_press_group": str(
                    inference.get("suggested_press_group") or ""
                ),
                "suggestion_confidence": _confidence_percent(inference.get("confidence")),
                "suggestion_reasons": list(inference.get("reasons") or []),
                "needs_confirmation": bool(inference.get("needs_confirmation", True)),
            }
        )
    inferred_presses = suggest_press_paths(
        root_path.name,
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
            for source in sources
        ],
        settings=settings,
        root_name=root_path.name,
        preserve_manual=False,
    )
    for source, press in zip(sources, inferred_presses):
        source["suggested_press_path"] = str(press.get("suggested_press_path") or "")
    related = catalog.matching_family_for_root(root_path)
    work_drafts = _registration_work_drafts(
        root_path,
        sources,
        defaults=defaults,
        settings=settings,
    )
    existing_candidates = _registration_existing_catalog_candidates(
        root_path,
        sources=sources,
        catalog_root=settings.catalog_root.resolve(),
    )
    return {
        "state": "catalog_work_required" if not related else "catalog_work_optional",
        "registration_required": not related,
        "root": str(root_path),
        "related_works": [work.name for work in related],
        "draft": {
            "name": root_path.name,
            "date": {"start": "", "end": ""},
            "domain": defaults["domain"],
            "country": defaults["country"],
            "release_type": defaults["release_type"],
            "path": str(root_path),
            "presses": inferred_presses,
        },
        "work_drafts": work_drafts,
        "existing_candidates": existing_candidates,
        "shared_target_supported": len(existing_candidates) >= 2,
        "sources": sources,
    }


def _file_work_overrides(
    root: Path,
    source_names: list[str],
    work_name: str,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for name in source_names:
        source = (root / name).resolve()
        if source.parent != root:
            raise ValueError(f"来源目录不是作品根目录的直接子目录：{source}")
        files, _issues = _scan_files(source)
        for path in files:
            overrides[str(path)] = work_name
    return overrides


def _multi_file_work_overrides(
    root: Path,
    source_work_names: Mapping[str, str],
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for source_name, work_name in source_work_names.items():
        source = (root / source_name).resolve()
        if source.parent != root:
            raise ValueError(f"来源目录不是作品根目录的直接子目录：{source}")
        files, _issues = _scan_files(source)
        for path in files:
            overrides[str(path)] = str(work_name)
    return overrides


def _shortcut_presses(
    patch: dict[str, Any],
    catalog_change: dict[str, Any],
    organizer_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    targets: dict[tuple[str, str], str] = {}
    wanted_work = normalized_identity(str(patch.get("name") or ""))
    wanted_source = str(catalog_change.get("yaml_source_rel") or "")
    wanted_index = int(catalog_change.get("index_in_file", -1))
    for assignment in organizer_plan.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        assignment_ref = assignment.get("catalog_ref")
        exact_match = bool(
            isinstance(assignment_ref, Mapping)
            and str(assignment_ref.get("yaml_source_rel") or "") == wanted_source
            and int(assignment_ref.get("index_in_file", -1)) == wanted_index
        )
        if not exact_match and (
            isinstance(assignment_ref, Mapping)
            or normalized_identity(str(assignment.get("work_name") or "")) != wanted_work
        ):
            continue
        key = (
            str(assignment.get("press_format") or "").casefold(),
            normalized_press_group(str(assignment.get("press_group") or "")),
        )
        target = str(assignment.get("target_dir") or "")
        if key in targets and targets[key].casefold() != target.casefold():
            raise ValueError("同一压制记录被规划到多个目标目录，无法生成唯一快捷方式")
        targets[key] = target
    result: list[dict[str, Any]] = []
    for index, row in enumerate(patch["collectioned_ordered"]):
        key = (str(row["press_format"]).casefold(), normalized_press_group(str(row["press_group"])))
        target = targets.get(key)
        if not target:
            raise ValueError(
                f"压制记录没有对应的整理目标：{row['press_format']}/{row['press_group']}"
            )
        result.append(
            {
                **row,
                "press_key": f"{index}:main::{row['press_format']}:{row['press_group']}",
                "target_path": target,
            }
        )
    return result


def _public_catalog_change(change: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in change.items()
        if not str(key).startswith("_")
        and key not in {"patch", "record", "kind", "key"}
    }


def _shortcut_issue(
    code: str,
    message: str,
    path: str | Path = "",
    **details: Any,
) -> dict[str, str]:
    issue = {"code": code, "message": message, "path": str(path) if path else ""}
    issue.update(
        {
            key: str(value)
            for key, value in details.items()
            if value is not None and str(value) != ""
        }
    )
    return issue


class _UnsafeTargetPathError(ValueError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"press_path 经过符号链接或目录联接：{path}")


def _target_from_press_path(root: Path, press_path: str) -> Path:
    normalized = str(press_path).strip().replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"press_path 必须是作品根目录内的相对路径：{normalized}")
    relative = normalized.strip("/")
    if not relative:
        raise ValueError("press_path 不能为空")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"press_path 包含非法路径段：{relative}")
    # Inspect every lexical component before resolve(). Otherwise a symlink or
    # Windows junction is dereferenced first and the later is_symlink check sees
    # only the ordinary destination directory.
    lexical_target = root
    for part in parts:
        lexical_target = lexical_target / part
        if _is_reparse_point(lexical_target):
            raise _UnsafeTargetPathError(lexical_target)
    target = lexical_target.resolve()
    if not _path_under(target, root):
        raise ValueError(f"press_path 越出作品根目录：{relative}")
    return target


def _shortcut_plan_id(payload: Mapping[str, Any]) -> str:
    stable = {
        "version": 2,
        "root": str(payload.get("root") or ""),
        "work_refs": payload.get("work_refs") or [],
        "issues": payload.get("issues") or [],
        "shortcuts": [
            {
                "status": row.get("status"),
                "target_path": row.get("target_path"),
                "target_exists_before_move": row.get("target_exists_before_move"),
                "shortcut_path": row.get("shortcut_path"),
                "existing_target_path": row.get("existing_target_path"),
            }
            for row in payload.get("shortcuts") or []
            if isinstance(row, Mapping)
        ],
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]


def _preview_shortcut_records(
    records: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    root: Path,
    require_existing_targets: bool,
    allowed_missing_target_keys: set[str] | None = None,
    initial_issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    issues = list(initial_issues or [])
    allowed_missing = allowed_missing_target_keys or set()
    shortcuts: list[dict[str, Any]] = []
    work_refs: list[dict[str, Any]] = []
    seen_shortcuts: dict[str, str] = {}
    for record, selected_rows in sorted(
        records,
        key=lambda item: (
            str(item[0]["work"]["yaml_source_rel"]).casefold(),
            int(item[0]["work"]["index_in_file"]),
        ),
    ):
        rows = sorted(selected_rows, key=lambda row: int(row["position"]))
        work_refs.append(_work_ref(record, rows))
        presses: list[dict[str, Any]] = []
        for row in rows:
            try:
                target = _target_from_press_path(root, str(row.get("press_path") or ""))
            except ValueError as exc:
                issues.append(
                    _shortcut_issue(
                        "shortcut-invalid-press-path",
                        str(exc),
                        record["source"],
                        work_name=record["work"]["name"],
                        press_format=row.get("press_format"),
                        press_group=row.get("press_group"),
                    )
                )
                continue
            presses.append({**_press_ref(row), "target_path": str(target)})
        if not presses:
            continue
        try:
            planned = preview_scoped_shortcuts_for_work(record["work"], presses)
        except (OSError, ValueError) as exc:
            issues.append(
                _shortcut_issue(
                    "shortcut-preview-failed",
                    f"无法生成作品快捷方式计划：{exc}",
                    record["source"],
                    work_name=record["work"]["name"],
                )
            )
            continue
        for row in planned:
            key = path_key(str(row["shortcut_path"]))
            previous = seen_shortcuts.get(key)
            if previous is not None:
                issues.append(
                    _shortcut_issue(
                        "shortcut-path-collision",
                        f"多个数据库压制记录生成同一个快捷方式；另一个目标为：{previous}",
                        row["shortcut_path"],
                        work_name=row.get("work_name"),
                    )
                )
                continue
            seen_shortcuts[key] = str(row["target_path"])
            shortcuts.append(row)
            if row.get("status") == "conflict":
                issues.append(
                    _shortcut_issue(
                        "shortcut-conflict",
                        "快捷方式路径已经存在，但没有指向数据库权威目标目录",
                        row["shortcut_path"],
                        work_name=row.get("work_name"),
                    )
                )
            if (
                require_existing_targets
                and not row.get("target_exists_before_move")
                and path_key(str(row["target_path"])) not in allowed_missing
            ):
                issues.append(
                    _shortcut_issue(
                        "shortcut-target-missing",
                        "数据库 press_path 对应的整理目标目录不存在",
                        row["target_path"],
                        work_name=row.get("work_name"),
                    )
                )
    payload: dict[str, Any] = {
        "root": str(root),
        "work_refs": work_refs,
        "shortcuts": shortcuts,
        "issues": issues,
    }
    payload["shortcut_summary"] = {
        "total_count": len(shortcuts),
        "planned_count": sum(1 for row in shortcuts if row.get("status") == "planned"),
        "already_exists_count": sum(
            1 for row in shortcuts if row.get("status") == "already_exists"
        ),
        "conflict_count": sum(1 for row in shortcuts if row.get("status") == "conflict"),
        "missing_target_count": sum(
            1 for row in shortcuts if not row.get("target_exists_before_move")
        ),
        "missing_catalog_path_count": sum(
            1 for issue in issues if issue.get("code") == "shortcut-catalog-path-missing"
        ),
        "missing_press_path_count": sum(
            1 for issue in issues if issue.get("code") == "shortcut-press-path-missing"
        ),
        "issue_count": len(issues),
    }
    payload["ready"] = bool(shortcuts) and not issues
    payload["shortcut_plan_id"] = _shortcut_plan_id(payload)
    return payload


def preview_organizer_plan_shortcuts(
    organizer_plan: dict[str, Any],
    *,
    organizer_settings: OrganizerSettings,
) -> dict[str, Any]:
    """Attach a DB-authoritative, multi-work scoped shortcut plan."""

    root = Path(str(organizer_plan.get("root") or "")).expanduser().resolve()
    catalog_root = organizer_settings.catalog_root.resolve()
    catalog_session = CatalogReadSession()
    assignments = [
        assignment
        for assignment in organizer_plan.get("assignments") or []
        if isinstance(assignment, Mapping)
    ]
    settled_media_scan = bool(
        not assignments
        and not (organizer_plan.get("moves") or [])
        and not (organizer_plan.get("unresolved_files") or [])
        and not (organizer_plan.get("issues") or [])
    )
    if settled_media_scan:
        shortcut_plan = _preview_catalog_root_shortcuts(
            root=root,
            catalog_root=catalog_root,
            session=catalog_session,
        )
        organizer_plan["shortcuts"] = shortcut_plan["shortcuts"]
        organizer_plan["shortcut_summary"] = shortcut_plan["shortcut_summary"]
        organizer_plan["shortcut_plan_id"] = shortcut_plan["shortcut_plan_id"]
        organizer_plan["shortcut_scope"] = {
            "root": str(root),
            "work_refs": shortcut_plan["work_refs"],
        }
        organizer_plan["shortcut_issues"] = shortcut_plan["issues"]
        if int(shortcut_plan.get("catalog_record_count") or 0) > 0:
            organizer_plan["media_state"] = "already_organized"
            summary = shortcut_plan["shortcut_summary"]
            total_count = int(summary.get("total_count") or 0)
            already_exists_count = int(summary.get("already_exists_count") or 0)
            planned_count = int(summary.get("planned_count") or 0)
            organizer_plan["shortcut_state"] = (
                "blocked"
                if shortcut_plan["issues"]
                else "complete"
                if total_count > 0
                and planned_count == 0
                and already_exists_count == total_count
                else "pending"
                if planned_count > 0
                else "blocked"
            )
        else:
            organizer_plan["media_state"] = "empty"
            organizer_plan["shortcut_state"] = "blocked"
        if shortcut_plan["issues"]:
            existing_issues = organizer_plan.setdefault("issues", [])
            existing_issues.extend(shortcut_plan["issues"])
            summary = organizer_plan.setdefault("summary", {})
            summary["issue_count"] = len(existing_issues)
            organizer_plan["plan_id"] = _stable_plan_id(organizer_plan)
        # There is intentionally no executable media plan after all source
        # folders have already been landed. Missing shortcuts use the existing
        # shortcut-only retry endpoint instead of attempting a second move.
        organizer_plan["ready"] = False
        return organizer_plan

    cache: dict[tuple[str, str], dict[str, Any]] = {}
    selected: dict[tuple[str, int], tuple[dict[str, Any], dict[int, dict[str, Any]]]] = {}
    allowed_missing_target_keys: set[str] = set()
    issues: list[dict[str, str]] = []
    issue_keys: set[tuple[str, str, str, str, str, str]] = set()

    def add_issue(issue: dict[str, str]) -> None:
        key = (
            issue.get("code", ""),
            issue.get("path", ""),
            issue.get("message", ""),
            issue.get("work_name", ""),
            issue.get("press_format", ""),
            issue.get("press_group", ""),
        )
        if key not in issue_keys:
            issue_keys.add(key)
            issues.append(issue)

    for assignment in assignments:
        try:
            record = _catalog_record_for_assignment(
                assignment,
                catalog_root=catalog_root,
                cache=cache,
                session=catalog_session,
            )
        except (OSError, ValueError) as exc:
            add_issue(
                _shortcut_issue(
                    "shortcut-catalog-work-invalid",
                    f"无法从数据库精确复核作品：{exc}",
                    assignment.get("catalog_file") or "",
                    work_name=assignment.get("work_name"),
                )
            )
            continue
        work = record["work"]
        raw_work_path = str(work.get("path") or "").strip()
        if not raw_work_path:
            add_issue(
                _shortcut_issue(
                    "shortcut-catalog-path-missing",
                    "数据库作品缺少权威 path，不能规划快捷方式",
                    record["source"],
                    work_name=work["name"],
                )
            )
            continue
        try:
            database_root = Path(raw_work_path).expanduser().resolve()
        except OSError as exc:
            add_issue(
                _shortcut_issue(
                    "shortcut-catalog-path-invalid",
                    f"数据库作品 path 无法解析：{exc}",
                    raw_work_path,
                    work_name=work["name"],
                )
            )
            continue
        if path_key(database_root) != path_key(root):
            add_issue(
                _shortcut_issue(
                    "shortcut-catalog-path-mismatch",
                    "数据库作品 path 与本次整理根目录不一致，不能规划快捷方式",
                    raw_work_path,
                    work_name=work["name"],
                )
            )
            continue
        press_format = normalized_value(str(assignment.get("press_format") or ""))
        press_group = normalized_press_group(str(assignment.get("press_group") or ""))
        candidates = [
            row
            for row in record["presses"]
            if normalized_value(str(row.get("press_format") or "")) == press_format
            and normalized_press_group(str(row.get("press_group") or "")) == press_group
        ]
        if not candidates:
            add_issue(
                _shortcut_issue(
                    "shortcut-catalog-press-not-found",
                    "数据库中找不到本次整理使用的格式/压制组记录",
                    record["source"],
                    work_name=work["name"],
                    press_format=assignment.get("press_format"),
                    press_group=assignment.get("press_group"),
                )
            )
            continue
        populated = [row for row in candidates if str(row.get("press_path") or "").strip()]
        if len(populated) != len(candidates):
            add_issue(
                _shortcut_issue(
                    "shortcut-press-path-missing",
                    "数据库压制记录缺少权威 press_path，不能规划快捷方式",
                    record["source"],
                    work_name=work["name"],
                    press_format=assignment.get("press_format"),
                    press_group=assignment.get("press_group"),
                )
            )
            continue
        assignment_target = Path(str(assignment.get("target_dir") or "")).expanduser().resolve()
        exact = []
        for row in populated:
            try:
                if path_key(_target_from_press_path(root, str(row["press_path"]))) == path_key(
                    assignment_target
                ):
                    exact.append(row)
            except ValueError:
                continue
        if len(exact) != 1:
            add_issue(
                _shortcut_issue(
                    "shortcut-database-target-mismatch",
                    "整理目标与数据库权威 press_path 不一致或不能唯一匹配",
                    assignment_target,
                    work_name=work["name"],
                    press_format=assignment.get("press_format"),
                    press_group=assignment.get("press_group"),
                )
            )
            continue
        selected_row = exact[0]
        record_key = (str(work["yaml_source_rel"]), int(work["index_in_file"]))
        record_bucket = selected.setdefault(record_key, (record, {}))
        existing = record_bucket[1].get(int(selected_row["position"]))
        if existing is not None and _press_identity(existing) != _press_identity(selected_row):
            add_issue(
                _shortcut_issue(
                    "shortcut-press-ambiguous",
                    "同一数据库压制记录被解析为不同目标，不能规划快捷方式",
                    record["source"],
                    work_name=work["name"],
                )
            )
            continue
        record_bucket[1][int(selected_row["position"])] = selected_row
        if int(assignment.get("file_count") or 0) > 0:
            allowed_missing_target_keys.add(path_key(assignment_target))

    assignment_records = [
        (record, list(rows.values()))
        for record, rows in (value for value in selected.values())
        if rows
    ]
    catalog_records, catalog_issues, _catalog_record_count = _catalog_root_shortcut_records(
        root=root,
        catalog_root=catalog_root,
        session=catalog_session,
    )
    # An active plan is normally assignment-scoped.  Once a sibling press has
    # already landed under the same physical root, shortcut completion must
    # cover every exact-root database press without pretending that unrelated
    # missing targets will be created by this media plan.
    mixed_root_scope = _has_landed_press_outside_plan(
        catalog_records,
        root=root,
        planned_target_keys=allowed_missing_target_keys,
    )
    if mixed_root_scope:
        records = catalog_records
        for issue in catalog_issues:
            add_issue(issue)
    else:
        records = assignment_records
    if not records and not issues and not (organizer_plan.get("issues") or []):
        issues.append(
            _shortcut_issue(
                "shortcut-scope-empty",
                "本次整理计划没有可由数据库权威 path/press_path 复核的压制记录",
                root,
            )
        )
    shortcut_plan = _preview_shortcut_records(
        records,
        root=root,
        require_existing_targets=mixed_root_scope,
        allowed_missing_target_keys=(
            allowed_missing_target_keys if mixed_root_scope else None
        ),
        initial_issues=issues,
    )
    organizer_plan["shortcuts"] = shortcut_plan["shortcuts"]
    organizer_plan["shortcut_summary"] = shortcut_plan["shortcut_summary"]
    organizer_plan["shortcut_plan_id"] = shortcut_plan["shortcut_plan_id"]
    organizer_plan["shortcut_scope"] = {
        "root": str(root),
        "work_refs": shortcut_plan["work_refs"],
    }
    organizer_plan["shortcut_issues"] = shortcut_plan["issues"]
    media_ready = bool(organizer_plan.get("ready"))
    if shortcut_plan["issues"]:
        existing_issues = organizer_plan.setdefault("issues", [])
        existing_issues.extend(shortcut_plan["issues"])
        summary = organizer_plan.setdefault("summary", {})
        summary["issue_count"] = len(existing_issues)
        organizer_plan["plan_id"] = _stable_plan_id(organizer_plan)
    organizer_plan["ready"] = media_ready and bool(shortcut_plan["ready"])
    return organizer_plan


def apply_organizer_plan_shortcuts(
    shortcuts: list[dict[str, Any]],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    """Apply only a freshly rebuilt scoped shortcut plan."""

    _browse_settings_for_catalog(organizer_settings, browse_settings)
    return apply_scoped_shortcuts_for_work(shortcuts, settings=browse_settings)


def _retry_shortcut_rows(
    patch: dict[str, Any],
    catalog_change: dict[str, Any],
    *,
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    work = {
        **patch,
        "yaml_source_rel": catalog_change["yaml_source_rel"],
        "index_in_file": catalog_change["index_in_file"],
        "work_key": f"{catalog_change['yaml_source_rel']}#{catalog_change['index_in_file']}",
    }
    presses: list[dict[str, Any]] = []
    for index, row in enumerate(patch["collectioned_ordered"]):
        relative = str(row["press_path"]).replace("\\", "/").strip("/")
        target = root.joinpath(*relative.split("/")).resolve()
        if not _path_under(target, root):
            raise ValueError(f"press_path resolves outside the work root: {relative}")
        presses.append(
            {
                **row,
                "press_key": f"{index}:main::{row['press_format']}:{row['press_group']}",
                "target_path": str(target),
            }
        )
    return work, presses


def _catalog_root_shortcut_records(
    *,
    root: Path,
    catalog_root: Path,
    session: CatalogReadSession | None = None,
) -> tuple[
    list[tuple[dict[str, Any], list[dict[str, Any]]]],
    list[dict[str, str]],
    int,
]:
    """Return every valid press row whose database path is this exact root."""

    issues: list[dict[str, str]] = []
    records: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    catalog_records = _all_catalog_records_for_root(
        catalog_root=catalog_root,
        root=root,
        session=session,
    )
    if not catalog_records:
        issues.append(
            _shortcut_issue(
                "shortcut-catalog-path-missing",
                "数据库中没有 path 精确等于当前作品根目录的作品，无法检查已整理目录的快捷方式",
                root,
            )
        )
    for record in catalog_records:
        selected_rows: list[dict[str, Any]] = []
        presses = [row for row in record.get("presses") or [] if isinstance(row, Mapping)]
        if not presses:
            issues.append(
                _shortcut_issue(
                    "shortcut-catalog-press-empty",
                    "数据库作品没有任何压制记录，无法检查快捷方式",
                    record["source"],
                    work_name=record["work"]["name"],
                )
            )
            continue
        for row in presses:
            if not str(row.get("press_format") or "").strip():
                issues.append(
                    _shortcut_issue(
                        "shortcut-catalog-press-invalid",
                        "数据库压制记录缺少格式，无法检查快捷方式",
                        record["source"],
                        work_name=record["work"]["name"],
                    )
                )
                continue
            if not str(row.get("press_path") or "").strip():
                issues.append(
                    _shortcut_issue(
                        "shortcut-press-path-missing",
                        "数据库压制记录缺少权威 press_path，无法检查已整理目标",
                        record["source"],
                        work_name=record["work"]["name"],
                        press_format=row.get("press_format"),
                        press_group=row.get("press_group"),
                    )
                )
                continue
            selected_rows.append(dict(row))
        if selected_rows:
            records.append((record, selected_rows))
    return records, issues, len(catalog_records)


def _has_landed_press_outside_plan(
    records: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    root: Path,
    planned_target_keys: set[str],
) -> bool:
    for _record, rows in records:
        for row in rows:
            try:
                target = _target_from_press_path(root, str(row.get("press_path") or ""))
            except ValueError:
                continue
            if path_key(target) in planned_target_keys:
                continue
            if _directory_contains_regular_file(target):
                return True
    return False


def _preview_catalog_root_shortcuts(
    *,
    root: Path,
    catalog_root: Path,
    session: CatalogReadSession | None = None,
) -> dict[str, Any]:
    """Inspect shortcuts for an exact catalog root after media is already landed."""

    records, issues, catalog_record_count = _catalog_root_shortcut_records(
        root=root,
        catalog_root=catalog_root,
        session=session,
    )
    payload = _preview_shortcut_records(
        records,
        root=root,
        require_existing_targets=True,
        initial_issues=issues,
    )
    payload["catalog_record_count"] = catalog_record_count
    return payload


def _repair_endpoint_descriptor(
    *,
    root: Path,
    reason_codes: list[str],
    draft_work: Mapping[str, Any] | None = None,
    catalog_ref: Mapping[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    independent_append_allowed: bool = False,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "state": "repair_required",
        "root": str(root),
        "preview_endpoint": _REPAIR_PREVIEW_ENDPOINT,
        "apply_endpoint": _REPAIR_APPLY_ENDPOINT,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "candidates": candidates or [],
        "independent_append_allowed": bool(independent_append_allowed),
        "shared_target_supported": len(candidates or []) >= 2,
    }
    if draft_work is not None:
        descriptor["draft_work"] = dict(draft_work)
    if catalog_ref is not None:
        descriptor["catalog_ref"] = dict(catalog_ref)
    return descriptor


def shortcut_repair_descriptor_for_registration(
    discovery: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(str(discovery.get("root") or "")).expanduser().resolve()
    draft = discovery.get("draft")
    candidates = [
        dict(candidate)
        for candidate in discovery.get("existing_candidates") or []
        if isinstance(candidate, Mapping)
    ]
    return _repair_endpoint_descriptor(
        root=root,
        reason_codes=["catalog-work-not-found"],
        draft_work=draft if isinstance(draft, Mapping) else None,
        candidates=candidates,
    )


def shortcut_repair_descriptor_for_plan(
    organizer_plan: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
) -> dict[str, Any] | None:
    issues = organizer_plan.get("shortcut_issues") or organizer_plan.get("issues") or []
    reason_codes = [
        str(issue.get("code") or "")
        for issue in issues
        if isinstance(issue, Mapping)
        and str(issue.get("code") or "") in _REPAIRABLE_SHORTCUT_CODES
    ]
    if not reason_codes:
        return None
    repair_work_names = {
        str(issue.get("work_name") or "").strip()
        for issue in issues
        if isinstance(issue, Mapping)
        and str(issue.get("code") or "") in _REPAIRABLE_SHORTCUT_CODES
        and str(issue.get("work_name") or "").strip()
    }
    root = Path(str(organizer_plan.get("root") or "")).expanduser().resolve()
    assignments = [
        assignment
        for assignment in organizer_plan.get("assignments") or []
        if isinstance(assignment, Mapping)
    ]
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    catalog_session = CatalogReadSession()
    grouped: dict[tuple[str, int], tuple[dict[str, Any], list[Mapping[str, Any]]]] = {}
    for assignment in assignments:
        try:
            record = _catalog_record_for_assignment(
                assignment,
                catalog_root=organizer_settings.catalog_root.resolve(),
                cache=cache,
                session=catalog_session,
            )
        except (OSError, ValueError):
            continue
        work = record["work"]
        if repair_work_names and str(work.get("name") or "") not in repair_work_names:
            continue
        key = (str(work["yaml_source_rel"]), int(work["index_in_file"]))
        grouped.setdefault(key, (record, []))[1].append(assignment)
    # Once media has already landed there are intentionally no assignments left.
    # Keep the repair flow usable by offering only records whose authoritative
    # catalog path exactly matches this root; never infer candidates by name.
    if not grouped and organizer_plan.get("media_state") == "already_organized":
        for record in _all_catalog_records_for_root(
            catalog_root=organizer_settings.catalog_root.resolve(),
            root=root,
            session=catalog_session,
        ):
            work = record["work"]
            key = (str(work["yaml_source_rel"]), int(work["index_in_file"]))
            grouped[key] = (record, [])
    candidates = [
        {
            "catalog_ref": _catalog_ref_for_record(record),
            "draft_work": _repair_draft_for_record(record, root=root, assignments=rows),
        }
        for record, rows in grouped.values()
    ]
    first = candidates[0] if len(candidates) == 1 else {}
    return _repair_endpoint_descriptor(
        root=root,
        reason_codes=reason_codes,
        draft_work=first.get("draft_work") if first else None,
        catalog_ref=first.get("catalog_ref") if first else None,
        candidates=candidates,
    )


def _merged_update_patch(
    record: Mapping[str, Any],
    requested: Mapping[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    work = record["work"]
    if normalized_identity(str(work.get("name") or "")) != normalized_identity(
        str(requested.get("name") or "")
    ):
        raise ValueError("catalog_ref 对应作品与修复作品名不一致")
    existing_pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    existing_by_key: dict[str, dict[str, Any]] = {}
    for raw in record.get("presses") or []:
        if not isinstance(raw, Mapping):
            continue
        row = {
            "press_key": str(raw.get("press_key") or ""),
            "position": int(raw.get("position") or 0),
            "press_format": str(raw.get("press_format") or "").strip(),
            "press_group": str(raw.get("press_group") or "").strip(),
            "press_path": str(raw.get("press_path") or "").strip(),
            "segment": str(raw.get("segment") or "main"),
            "continuation_index": raw.get("continuation_index"),
            "continuation_title": str(raw.get("continuation_title") or ""),
        }
        key = (normalized_value(row["press_format"]), normalized_press_group(row["press_group"]))
        existing_pairs.setdefault(key, []).append(row)
        existing_by_key[row["press_key"]] = row
    resolved: list[dict[str, Any]] = []
    consumed_existing_keys: set[str] = set()
    appended_pairs: set[tuple[str, str]] = set()
    next_position = len(existing_by_key)
    for requested_row in requested.get("collectioned_ordered") or []:
        key = (
            normalized_value(str(requested_row.get("press_format") or "")),
            normalized_press_group(str(requested_row.get("press_group") or "")),
        )
        requested_key = str(requested_row.get("press_key") or "").strip()
        matching: list[dict[str, Any]] = []
        if requested_key:
            selected = existing_by_key.get(requested_key)
            if selected is None:
                raise ValueError(f"数据库压制记录已经变化：{requested_key}")
            selected_pair = (
                normalized_value(selected["press_format"]),
                normalized_press_group(selected["press_group"]),
            )
            if selected_pair != key:
                raise ValueError("选择已有数据库记录时只能修正 path/press_path，不能改格式或组简称")
            matching = [selected]
        else:
            matching = existing_pairs.get(key, [])
            if len(matching) > 1:
                raise ValueError(
                    "数据库中同一格式/组存在多条压制记录；请使用服务端候选携带的 press_key 明确选择："
                    f"{requested_row.get('press_format')}/{requested_row.get('press_group')}"
                )
        if matching:
            selected = matching[0]
            selected_key = str(selected.get("press_key") or "")
            if selected_key in consumed_existing_keys:
                raise ValueError(
                    "修复作品重复选择同一数据库压制记录："
                    f"{selected_key}"
                )
            consumed_existing_keys.add(selected_key)
            resolved.append(
                {
                    **selected,
                    "press_path": str(requested_row.get("press_path") or ""),
                }
            )
            continue
        if key in appended_pairs:
            raise ValueError(
                f"修复作品存在重复新增压制记录：{requested_row.get('press_format')}/"
                f"{requested_row.get('press_group')}"
            )
        press_group = normalize_press_group(str(requested_row.get("press_group") or ""))
        if normalize_press_group(press_group) and not media_group_code_known(press_group):
            raise ValueError(
                "新增到已有作品的压制记录使用了未登记的组简称："
                f"{press_group}；历史值仅可原样保留，不能作为新记录添加"
            )
        press_format = str(requested_row.get("press_format") or "").strip()
        row = {
                "press_key": f"{next_position}:main::{press_format}:{press_group}",
                "position": next_position,
                "press_format": str(requested_row.get("press_format") or ""),
                "press_group": press_group,
                "press_path": str(requested_row.get("press_path") or ""),
                "segment": "main",
            }
        next_position += 1
        appended_pairs.add(key)
        resolved.append(row)
    return {
        "name": str(work.get("name") or ""),
        "date": {
            "start": _iso_repair_date((work.get("date") or {}).get("start")),
            "end": _iso_repair_date((work.get("date") or {}).get("end")),
        },
        "domain": str(work.get("domain") or ""),
        "country": str(work.get("country") or ""),
        "release_type": str(work.get("release_type") or ""),
        "path": str(root),
        "markers": list(work.get("markers") or []),
        "collectioned_ordered": resolved,
    }


def _raw_collection_data_mut(work: Any) -> dict[str, Any]:
    if not isinstance(work, dict):
        raise ValueError("数据库作品必须是对象")
    attributes = work.get("attributes")
    if not isinstance(attributes, list):
        raise ValueError("数据库作品缺少 attributes")
    for attribute in attributes:
        if isinstance(attribute, dict) and attribute.get("type") == "collection-type":
            data = attribute.get("data")
            if isinstance(data, dict):
                return data
    raise ValueError("数据库作品缺少 collection-type.data")


def _raw_press_row_slots(data: Mapping[str, Any]) -> list[tuple[list[Any], int]]:
    slots: list[tuple[list[Any], int]] = []

    def append_rows(raw: Any) -> None:
        if not isinstance(raw, list):
            return
        for index, row in enumerate(raw):
            if isinstance(row, Mapping):
                if str(row.get("press_format") or "").strip() or str(
                    row.get("press_group") or ""
                ).strip():
                    slots.append((raw, index))
            elif isinstance(row, list) and len(row) == 2:
                slots.append((raw, index))

    append_rows(data.get("collectioned"))
    continuations = data.get("continuations")
    if isinstance(continuations, list):
        for block in continuations:
            if isinstance(block, Mapping):
                append_rows(block.get("collectioned"))
    return slots


def _apply_selected_path_repair_to_work(
    work: Any,
    record: Mapping[str, Any],
    patch: Mapping[str, Any],
    *,
    root: Path,
) -> None:
    """Patch only work path and selected press_path values in the raw YAML tree."""

    data = _raw_collection_data_mut(work)
    data["path"] = str(root).replace("\\", "/")
    slots = _raw_press_row_slots(data)
    original_count = len(record.get("presses") or [])
    main = data.get("collectioned")
    if not isinstance(main, list):
        main = []
        data["collectioned"] = main
    for row in patch.get("collectioned_ordered") or []:
        position = int(row.get("position") or 0)
        press_path = str(row.get("press_path") or "")
        if position < original_count:
            if position >= len(slots):
                raise ValueError(f"数据库压制记录位置已经变化：{row.get('press_key')}")
            container, index = slots[position]
            raw_row = container[index]
            if isinstance(raw_row, dict):
                raw_row["press_path"] = press_path
            else:
                container[index] = {
                    "press_format": str(row.get("press_format") or ""),
                    "press_group": str(row.get("press_group") or ""),
                    "press_path": press_path,
                }
        else:
            main.append(
                {
                    "press_format": str(row.get("press_format") or ""),
                    "press_group": str(row.get("press_group") or ""),
                    "press_path": press_path,
                }
            )


def _preview_catalog_repair_change(
    patch: dict[str, Any],
    *,
    catalog_ref: Any,
    catalog_intent: str,
    root: Path,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
    session: CatalogReadSession | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    catalog_root = organizer_settings.catalog_root.resolve()
    reader = session or CatalogReadSession()
    if catalog_ref is None:
        matches = _catalog_records_matching_repair_draft(
            patch,
            catalog_root=catalog_root,
            root=root,
            session=reader,
        )
        same_name_matches = [record for record in matches if _record_name_matches_patch(record, patch)]
        exact_identity_matches = [
            record
            for record in same_name_matches
            if _record_path_matches_root(record, root)
        ]
        independent_allowed = bool(matches) and not same_name_matches
        if catalog_intent and catalog_intent != "append_independent":
            raise ValueError(f"不支持的 catalog_intent：{catalog_intent}")
        if catalog_intent == "append_independent" and not independent_allowed:
            raise ValueError(
                "只有候选作品名不同（可共享 family root）时才能明确新增为独立作品；"
                "数据库已有规范化同名作品，必须选择并修正现有记录"
            )
        try:
            preview = preview_catalog_work_append(
                patch,
                settings=browse_settings,
                allow_shared_path_with_different_name=(
                    catalog_intent == "append_independent"
                ),
            )
        except ValueError:
            if not matches:
                raise
            preview = None
        if preview is not None and preview.get("action") == "already_exists":
            if len(exact_identity_matches) == 1:
                return preview, exact_identity_matches[0], []
            preview = None
        if matches and catalog_intent != "append_independent":
            return (
                {
                    "action": "selection_required",
                    "target": "",
                    "yaml_source_rel": "",
                    "index_in_file": -1,
                    "before_sha256": "",
                    "after_sha256": "",
                    "independent_append_allowed": independent_allowed,
                },
                None,
                [_public_repair_candidate(record, root=root) for record in matches],
            )
        if preview is None:
            raise ValueError("数据库作品信息与修复草稿不一致；请先明确选择要更新的记录")
        return preview, None, []
    if not isinstance(catalog_ref, Mapping):
        raise ValueError("catalog_ref 必须是服务端预览返回的作品引用")
    record = _record_from_work_ref(catalog_ref, catalog_root=catalog_root, session=reader)
    merged = _merged_update_patch(record, patch, root=root)
    patch.clear()
    patch.update(merged)
    source = Path(record["source"]).resolve()
    previous = reader.content(source)
    doc = load_yaml_string(previous.decode("utf-8"))
    works = _works_list_mut(doc)
    index = int(record["work"]["index_in_file"])
    if index < 0 or index >= len(works):
        raise ValueError("catalog_ref 对应数据库索引已变化")
    _apply_selected_path_repair_to_work(works[index], record, merged, root=root)
    after_text = dump_yaml_string(doc)
    load_jp_tv_entries_from_yaml(load_yaml_string(after_text))
    before_sha = _file_sha256(previous)
    after_sha = _file_sha256(after_text.encode("utf-8"))
    action = "already_exists" if before_sha == after_sha else "update"
    return (
        {
            "action": action,
            "target": str(source),
            "yaml_source_rel": str(record["work"]["yaml_source_rel"]),
            "index_in_file": index,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "patch": patch,
            "_after_text": after_text,
        },
        record,
        [],
    )


def _repair_plan_id(payload: Mapping[str, Any]) -> str:
    catalog = payload.get("catalog_change") or {}
    organizer_plan = payload.get("organizer_plan") or {}
    stable = {
        "version": 2,
        "root": payload.get("root"),
        "media_move_planned": bool(payload.get("media_move_planned")),
        "organizer_plan_id": organizer_plan.get("plan_id"),
        "draft_work": payload.get("draft_work"),
        "catalog_ref": payload.get("catalog_ref"),
        "catalog_intent": payload.get("catalog_intent"),
        "catalog_change": {
            "action": catalog.get("action"),
            "yaml_source_rel": catalog.get("yaml_source_rel"),
            "index_in_file": catalog.get("index_in_file"),
            "before_sha256": catalog.get("before_sha256"),
            "after_sha256": catalog.get("after_sha256"),
        },
        "issues": payload.get("issues") or [],
        "shortcuts": [
            {
                "status": row.get("status"),
                "target_path": row.get("target_path"),
                "target_exists_before_move": row.get("target_exists_before_move"),
                "shortcut_path": row.get("shortcut_path"),
                "existing_target_path": row.get("existing_target_path"),
            }
            for row in payload.get("shortcuts") or []
            if isinstance(row, Mapping)
        ],
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]


def _planned_repair_targets(
    organizer_plan: Mapping[str, Any],
    *,
    root: Path,
    record: Mapping[str, Any] | None,
    catalog_root: Path,
    session: CatalogReadSession | None = None,
) -> tuple[set[tuple[str, str, str]], list[dict[str, str]]]:
    """Prove which missing repair targets will be created by this exact media plan."""

    issues: list[dict[str, str]] = []
    try:
        plan_root = Path(str(organizer_plan.get("root") or "")).expanduser().resolve()
    except OSError:
        plan_root = Path()
    if path_key(plan_root) != path_key(root):
        issues.append(
            _shortcut_issue(
                "repair-media-root-mismatch",
                "数据库修复根目录与媒体整理计划不一致",
                plan_root,
            )
        )
        return set(), issues
    if not organizer_plan.get("ready"):
        issues.append(
            _shortcut_issue(
                "repair-media-plan-not-ready",
                "媒体整理计划仍有未决文件或冲突，不能与数据库修复一起执行",
                root,
            )
        )
    if record is None:
        issues.append(
            _shortcut_issue(
                "repair-media-catalog-selection-required",
                "必须先明确选择本次媒体整理对应的现有数据库作品，才能串联执行",
                root,
            )
        )
        return set(), issues

    wanted_key = (
        str(record["work"]["yaml_source_rel"]),
        int(record["work"]["index_in_file"]),
    )
    assignments = [
        row
        for row in organizer_plan.get("assignments") or []
        if isinstance(row, Mapping)
    ]
    moves = [row for row in organizer_plan.get("moves") or [] if isinstance(row, Mapping)]
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    reader = session or CatalogReadSession()
    selected: list[Mapping[str, Any]] = []
    unbound_count = 0
    for assignment in assignments:
        try:
            assignment_record = _catalog_record_for_assignment(
                assignment,
                catalog_root=catalog_root,
                cache=cache,
                session=reader,
            )
        except (OSError, ValueError):
            unbound_count += 1
            continue
        assignment_key = (
            str(assignment_record["work"]["yaml_source_rel"]),
            int(assignment_record["work"]["index_in_file"]),
        )
        if assignment_key == wanted_key:
            selected.append(assignment)
        else:
            unbound_count += 1
    if unbound_count:
        issues.append(
            _shortcut_issue(
                "repair-media-plan-multiple-works",
                "当前媒体计划还包含其他数据库作品；请先缩小到单个作品后再串联修复",
                root,
            )
        )
    if not selected:
        issues.append(
            _shortcut_issue(
                "repair-media-plan-unbound",
                "当前媒体整理计划没有可与所选数据库作品精确绑定的任务",
                root,
            )
        )
        return set(), issues

    planned: set[tuple[str, str, str]] = set()
    for assignment in selected:
        relpath = str(assignment.get("target_relpath") or "").strip()
        try:
            target = _target_from_press_path(root, relpath)
        except (OSError, ValueError) as exc:
            issues.append(
                _shortcut_issue(
                    "repair-media-target-unsafe",
                    f"媒体整理目标不是作品根目录内的安全路径：{exc}",
                    assignment.get("target_dir") or relpath,
                )
            )
            continue
        try:
            assignment_target = Path(
                str(assignment.get("target_dir") or "")
            ).expanduser().resolve()
        except OSError:
            assignment_target = Path()
        if path_key(target) != path_key(assignment_target):
            issues.append(
                _shortcut_issue(
                    "repair-media-target-mismatch",
                    "媒体整理任务的目标相对路径与绝对路径不一致",
                    assignment.get("target_dir") or relpath,
                )
            )
            continue
        creates_target = False
        for move in moves:
            try:
                move_target = Path(str(move.get("target") or "")).expanduser().resolve()
            except OSError:
                continue
            if move_target != target and _path_under(move_target, target):
                creates_target = True
                break
        if not creates_target:
            issues.append(
                _shortcut_issue(
                    "repair-media-target-without-move",
                    "press_path 尚不存在，且当前媒体计划没有任何文件会写入该目标目录",
                    target,
                    work_name=assignment.get("work_name"),
                    press_format=assignment.get("press_format"),
                    press_group=assignment.get("press_group"),
                )
            )
            continue
        planned.add(
            (
                normalized_value(str(assignment.get("press_format") or "")),
                normalized_press_group(str(assignment.get("press_group") or "")),
                path_key(target),
            )
        )
    return planned, issues


def preview_catalog_shortcut_repair(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
    organizer_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview a standalone repair or a repair bound to one reviewed media plan."""

    _browse_settings_for_catalog(organizer_settings, browse_settings)
    root = _validated_root(body.get("root"), organizer_settings)
    raw_catalog_ref = body.get("catalog_ref")
    catalog_intent = str(body.get("catalog_intent") or "").strip()
    catalog_session = CatalogReadSession()
    patch = _repair_draft_patch(
        body.get("draft_work"),
        root=root,
        selected_catalog=isinstance(raw_catalog_ref, Mapping),
    )
    catalog_change, record, candidates = _preview_catalog_repair_change(
        patch,
        catalog_ref=raw_catalog_ref,
        catalog_intent=catalog_intent,
        root=root,
        organizer_settings=organizer_settings,
        browse_settings=browse_settings,
        session=catalog_session,
    )
    if record is not None:
        existing_work = record["work"]
        patch = {
            **patch,
            "name": str(existing_work.get("name") or ""),
            "date": {
                "start": _iso_repair_date((existing_work.get("date") or {}).get("start")),
                "end": _iso_repair_date((existing_work.get("date") or {}).get("end")),
            },
            "domain": str(existing_work.get("domain") or ""),
            "country": str(existing_work.get("country") or ""),
            "release_type": str(existing_work.get("release_type") or ""),
            "path": str(root),
            "markers": list(existing_work.get("markers") or []),
        }
    media_move_planned = organizer_plan is not None
    issues: list[dict[str, str]] = []
    if catalog_change.get("action") == "selection_required":
        issues.append(
            _shortcut_issue(
                "repair-catalog-selection-required",
                "数据库中已有同名、同 path 或名称前缀候选；默认必须明确选择现有记录。"
                "若确认只是共享 family root/前缀误命中，可显式选择‘新增为独立作品’",
                root,
            )
        )
    planned_targets: set[tuple[str, str, str]] = set()
    if media_move_planned:
        planned_targets, media_issues = _planned_repair_targets(
            organizer_plan,
            root=root,
            record=record,
            catalog_root=organizer_settings.catalog_root.resolve(),
            session=catalog_session,
        )
        issues.extend(media_issues)

    presses: list[dict[str, Any]] = []
    shortcut_press_pairs: set[tuple[str, str]] = set()
    draft_target_keys: set[tuple[str, str, str]] = set()
    for row in patch["collectioned_ordered"]:
        try:
            target = _target_from_press_path(root, str(row.get("press_path") or ""))
        except _UnsafeTargetPathError as exc:
            target = exc.path
            issues.append(
                _shortcut_issue(
                    "repair-target-unsafe",
                    "press_path 经过符号链接或目录联接；修复流程拒绝解引用后创建快捷方式",
                    target,
                    press_format=row.get("press_format"),
                    press_group=row.get("press_group"),
                )
            )
        target_safe = target.is_dir() and not _is_reparse_point(target)
        target_key = (
            normalized_value(str(row.get("press_format") or "")),
            normalized_press_group(str(row.get("press_group") or "")),
            path_key(target),
        )
        draft_target_keys.add(target_key)
        target_will_be_created = media_move_planned and target_key in planned_targets
        if not media_move_planned and not target_safe:
            issues.append(
                _shortcut_issue(
                    "repair-target-missing",
                    (
                        "press_path 必须对应作品根目录内已经存在的普通目录；修复流程不会移动媒体"
                    ),
                    target,
                    press_format=row.get("press_format"),
                    press_group=row.get("press_group"),
                )
            )
        pair = (
            normalized_value(str(row.get("press_format") or "")),
            normalized_press_group(str(row.get("press_group") or "")),
        )
        include_shortcut = not media_move_planned or target_will_be_created
        if include_shortcut and pair not in shortcut_press_pairs:
            shortcut_press_pairs.add(pair)
            presses.append({**row, "target_path": str(target)})
    if media_move_planned:
        for missing_key in sorted(planned_targets - draft_target_keys):
            issues.append(
                _shortcut_issue(
                    "repair-media-target-unregistered",
                    "媒体整理目标没有对应的数据库 press_path，不能串联执行",
                    missing_key[2],
                )
            )
    work_ref = record["work"] if record is not None else {
        "yaml_source_rel": catalog_change.get("yaml_source_rel") or "",
        "index_in_file": int(catalog_change.get("index_in_file") or 0),
        "work_key": (
            f"{catalog_change.get('yaml_source_rel') or ''}#"
            f"{int(catalog_change.get('index_in_file') or 0)}"
        ),
    }
    shortcut_work = {
        **patch,
        "yaml_source_rel": str(work_ref.get("yaml_source_rel") or ""),
        "index_in_file": int(work_ref.get("index_in_file") or 0),
        "work_key": str(work_ref.get("work_key") or ""),
    }
    shortcuts: list[dict[str, Any]] = []
    target_unsafe = any(issue.get("code") == "repair-target-unsafe" for issue in issues)
    if not candidates and not target_unsafe:
        try:
            shortcuts = preview_scoped_shortcuts_for_work(shortcut_work, presses)
        except (OSError, ValueError) as exc:
            issues.append(
                _shortcut_issue(
                    "repair-shortcut-preview-failed",
                    f"无法生成快捷方式修复计划：{exc}",
                    root,
                )
            )
    for row in shortcuts:
        if row.get("status") == "conflict":
            issues.append(
                _shortcut_issue(
                    "shortcut-conflict",
                    "快捷方式路径已存在但目标不同，修复流程不会覆盖",
                    row.get("shortcut_path") or "",
                )
            )
    summary = {
        "total_count": len(shortcuts),
        "planned_count": sum(1 for row in shortcuts if row.get("status") == "planned"),
        "already_exists_count": sum(
            1 for row in shortcuts if row.get("status") == "already_exists"
        ),
        "conflict_count": sum(1 for row in shortcuts if row.get("status") == "conflict"),
        "missing_target_count": sum(
            1 for issue in issues if issue.get("code") == "repair-target-missing"
        ),
        "planned_target_creation_count": sum(
            1 for row in shortcuts if not row.get("target_exists_before_move")
        ) if media_move_planned else 0,
        "deduplicated_press_count": max(
            0, len(patch["collectioned_ordered"]) - len(presses)
        ),
        "issue_count": len(issues),
    }
    public_draft = {
        **patch,
        "presses": list(patch["collectioned_ordered"]),
    }
    repair_reason_codes = [str(issue.get("code") or "") for issue in issues]
    normalized_catalog_ref = _catalog_ref_for_record(record) if record is not None else None
    payload: dict[str, Any] = {
        "ok": True,
        "state": (
            "catalog_media_shortcut_repair_preview"
            if media_move_planned
            else "catalog_shortcut_repair_preview"
        ),
        "repair_required": True,
        "root": str(root),
        "media_move_planned": media_move_planned,
        "draft_work": public_draft,
        "catalog_ref": normalized_catalog_ref,
        "catalog_intent": catalog_intent,
        "independent_append_allowed": bool(
            catalog_change.get("independent_append_allowed")
        ),
        "catalog_change": _public_catalog_change(catalog_change),
        "repair_candidates": candidates,
        "shortcuts": shortcuts,
        "shortcut_summary": summary,
        "issues": issues,
        "ready": bool(shortcuts) and not issues,
        "repair": _repair_endpoint_descriptor(
            root=root,
            reason_codes=repair_reason_codes,
            draft_work=public_draft,
            catalog_ref=normalized_catalog_ref,
            candidates=candidates,
            independent_append_allowed=bool(
                catalog_change.get("independent_append_allowed")
            ),
        ),
    }
    if media_move_planned:
        payload["organizer_plan"] = deepcopy(dict(organizer_plan))
        payload["media_summary"] = dict(organizer_plan.get("summary") or {})
    payload["repair_plan_id"] = _repair_plan_id(payload)
    return payload


def _apply_catalog_repair_change(
    current: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> tuple[dict[str, Any], CatalogMutationReceipt | None]:
    change = current["catalog_change"]
    action = str(change.get("action") or "")
    if action == "append":
        return append_catalog_work_from_preview(
            current["draft_work"],
            settings=browse_settings,
            expected_before_sha256=str(change["before_sha256"]),
            allow_shared_path_with_different_name=(
                str(current.get("catalog_intent") or "") == "append_independent"
            ),
        )
    if action == "already_exists":
        return dict(change), None
    if action != "update":
        raise ValueError("当前数据库修复计划尚未明确选择可写作品")
    root = Path(str(current["root"])).expanduser().resolve()
    patch = _repair_draft_patch(current["draft_work"], root=root, selected_catalog=True)
    internal_change, _record, candidates = _preview_catalog_repair_change(
        patch,
        catalog_ref=current.get("catalog_ref"),
        catalog_intent=str(current.get("catalog_intent") or ""),
        root=root,
        organizer_settings=organizer_settings,
        browse_settings=browse_settings,
    )
    if candidates or internal_change.get("action") != "update":
        raise ValueError("数据库修复目标在确认前发生变化，尚未写入")
    change = internal_change
    if browse_settings.filesystem_root is None:
        raise ValueError("collection-detail 未配置可写数据库目录")
    target = Path(str(change["target"])).expanduser().resolve()
    target.relative_to(browse_settings.filesystem_root.resolve())
    if not target.is_file() or target.parent != browse_settings.filesystem_root.resolve():
        raise ValueError("数据库修复目标不是配置目录内的 YAML 文件")
    expected = str(change["before_sha256"])
    after_bytes = str(change["_after_text"]).encode("utf-8")
    receipt = apply_catalog_yaml_mutation(
        target=target,
        after_bytes=after_bytes,
        settings=browse_settings,
        expected_before_sha256=expected,
        work_ref={
            "yaml_source_rel": str(change["yaml_source_rel"]),
            "index_in_file": int(change["index_in_file"]),
        },
    )
    return _public_catalog_change(dict(change)), receipt


def _repair_request_fingerprint(body: Mapping[str, Any]) -> str:
    canonical = {
        "root": body.get("root"),
        "draft_work": body.get("draft_work"),
        "catalog_ref": body.get("catalog_ref"),
        "catalog_intent": body.get("catalog_intent"),
        "include_media_move": body.get("include_media_move") is True,
        "source_names": body.get("source_names"),
        "target_overrides": body.get("target_overrides"),
        "route_target_overrides": body.get("route_target_overrides"),
        "file_work_overrides": body.get("file_work_overrides"),
        "source_work_overrides": body.get("source_work_overrides"),
        "source_press_overrides": body.get("source_press_overrides"),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _repair_shortcut_scope_after_catalog(
    current: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
) -> dict[str, Any]:
    """Build an exact retry scope from the catalog row written by this repair."""

    change = current.get("catalog_change") or {}
    raw_ref = {
        "yaml_source_rel": str(change.get("yaml_source_rel") or ""),
        "index_in_file": int(change.get("index_in_file") or 0),
        "work_name": str((current.get("draft_work") or {}).get("name") or ""),
    }
    record = _record_from_work_ref(
        raw_ref,
        catalog_root=organizer_settings.catalog_root.resolve(),
    )
    rows = record.get("presses") or []
    shortcut_identities = {
        _press_identity(row)
        for row in current.get("shortcuts") or []
        if isinstance(row, Mapping)
    }
    selected: list[dict[str, Any]] = []
    seen_positions: set[int] = set()
    for index, requested in enumerate(
        (current.get("draft_work") or {}).get("collectioned_ordered") or []
    ):
        if not isinstance(requested, Mapping):
            continue
        if _press_identity(requested) not in shortcut_identities:
            continue
        try:
            position = int(requested.get("position", index))
        except (TypeError, ValueError):
            position = index
        if position in seen_positions or position < 0 or position >= len(rows):
            raise ValueError("数据库修复完成后无法精确重建快捷方式重试范围")
        candidate = rows[position]
        if _press_identity(candidate) != _press_identity(requested):
            raise ValueError("数据库修复完成后的压制记录与已确认计划不一致")
        seen_positions.add(position)
        selected.append(candidate)
    if not selected:
        raise ValueError("数据库修复完成后没有可供快捷方式重试的压制记录")
    return {
        "root": str(current.get("root") or ""),
        "work_refs": [_work_ref(record, selected)],
    }


def _remember_repair_operation(
    repair_plan_id: str,
    *,
    fingerprint: str,
    result: Mapping[str, Any],
    catalog_result: Mapping[str, Any],
    shortcuts: list[dict[str, Any]],
) -> None:
    catalog_target = Path(str(catalog_result.get("target") or "")).expanduser()
    if not catalog_target.is_file():
        raise OSError("修复完成后无法读取目标数据库文件，未记录可重放回执")
    final_catalog_sha256 = _file_sha256(catalog_target.read_bytes())
    _REPAIR_OPERATION_RECEIPTS[repair_plan_id] = {
        "fingerprint": fingerprint,
        "result": deepcopy(dict(result)),
        "catalog_target": str(catalog_target),
        "catalog_sha256": final_catalog_sha256,
        "shortcuts": tuple(
            (
                str(item.get("shortcut_path") or ""),
                str(item.get("target_path") or ""),
            )
            for item in shortcuts
        ),
    }
    _REPAIR_OPERATION_RECEIPTS.move_to_end(repair_plan_id)
    while len(_REPAIR_OPERATION_RECEIPTS) > _REPAIR_OPERATION_RECEIPT_LIMIT:
        _REPAIR_OPERATION_RECEIPTS.popitem(last=False)


def _verify_repair_receipt_final_state(
    receipt: Mapping[str, Any],
    *,
    browse_settings: JpTvBrowseSettings,
) -> tuple[tuple[str, str], ...]:
    if browse_settings.filesystem_root is None:
        raise ValueError("collection-detail 未配置可写数据库目录")
    catalog_root = browse_settings.filesystem_root.resolve()
    target = Path(str(receipt.get("catalog_target") or "")).expanduser().resolve()
    expected_sha256 = str(receipt.get("catalog_sha256") or "")
    if (
        target.parent != catalog_root
        or target.suffix.casefold() != ".yaml"
        or not target.is_file()
        or not expected_sha256
    ):
        raise ValueError("上次修复的数据库最终状态已不存在，拒绝按旧 repair_plan_id 重放")
    if _file_sha256(target.read_bytes()) != expected_sha256:
        raise ValueError("上次修复后数据库又发生变化，拒绝按旧 repair_plan_id 重放")

    expected_shortcuts: list[tuple[str, str]] = []
    for raw in receipt.get("shortcuts") or ():
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            raise ValueError("上次修复的快捷方式回执已损坏，拒绝按旧 repair_plan_id 重放")
        shortcut_path, target_path = str(raw[0]), str(raw[1])
        if not shortcut_path or not target_path or not shortcut_target_matches(
            shortcut_path,
            target_path,
        ):
            raise ValueError("上次修复的快捷方式最终状态已变化，拒绝按旧 repair_plan_id 重放")
        expected_shortcuts.append((shortcut_path, target_path))
    return tuple(expected_shortcuts)


def _replay_repair_operation(
    receipt: Mapping[str, Any],
    *,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    expected_shortcuts = _verify_repair_receipt_final_state(
        receipt,
        browse_settings=browse_settings,
    )

    replayed = deepcopy(dict(receipt["result"]))
    replayed["replayed"] = True
    replayed["current_repair_plan_id"] = replayed.get("repair_plan_id")
    shortcut_result = dict(replayed.get("shortcuts") or {})
    shortcut_result.update(
        {
            "planned_count": len(expected_shortcuts),
            "created_count": 0,
            "already_exists_count": len(expected_shortcuts),
            "shortcut_paths": [],
            "index_db": None,
        }
    )
    replayed["shortcuts"] = shortcut_result
    return replayed


def apply_catalog_shortcut_repair(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
    organizer_plan: Mapping[str, Any] | None = None,
    organizer_plan_factory: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply a standalone repair or one transactionally bound to media moves."""
    report_progress("复核数据库、媒体与快捷方式串联修复确认")

    if body.get("acknowledge_catalog_write") is not True:
        raise ValueError("数据库修复前必须明确设置 acknowledge_catalog_write=true")
    if body.get("acknowledge_shortcuts") is not True:
        raise ValueError("创建快捷方式前必须明确设置 acknowledge_shortcuts=true")
    media_move_planned = organizer_plan is not None or organizer_plan_factory is not None
    if media_move_planned and body.get("acknowledge_move") is not True:
        raise ValueError("串联数据库修复与媒体整理前必须明确设置 acknowledge_move=true")
    reviewed = str(body.get("repair_plan_id") or "")
    confirmation = str(body.get("confirmation") or "")
    if (
        len(reviewed) != _PLAN_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in reviewed)
        or reviewed != confirmation
    ):
        raise ValueError("数据库/快捷方式修复确认必须完整匹配 16 位 repair_plan_id")
    with _LANDING_LOCK:
        if browse_settings.filesystem_root is None:
            raise ValueError("collection-detail 未配置可写数据库目录")
        # Rebuild and validate the reviewed plan while holding the same cross-process
        # transaction as the eventual write. This also closes the already_exists
        # TOCTOU window: a cooperating editor cannot change the selected catalog row
        # between validation and shortcut creation. Receipt CAS still protects
        # against older/uncooperative writers that ignore this lock.
        with catalog_write_transaction(browse_settings.filesystem_root):
            fingerprint = _repair_request_fingerprint(body)
            operation_receipt = _REPAIR_OPERATION_RECEIPTS.get(reviewed)
            if (
                operation_receipt is not None
                and operation_receipt.get("fingerprint") != fingerprint
            ):
                raise ValueError("repair_plan_id 已用于另一份修复请求，拒绝重放")
            if operation_receipt is not None:
                return _replay_repair_operation(
                    operation_receipt,
                    browse_settings=browse_settings,
                )
            if organizer_plan is None and organizer_plan_factory is not None:
                organizer_plan = organizer_plan_factory()
            current = preview_catalog_shortcut_repair(
                body,
                organizer_settings=organizer_settings,
                browse_settings=browse_settings,
                organizer_plan=organizer_plan,
            )
            if current["repair_plan_id"] != reviewed:
                raise ValueError(
                    "数据库或快捷方式修复计划已发生变化，尚未写入；请重新预览："
                    f"{current['repair_plan_id']}"
                )
            if not current["ready"]:
                raise ValueError("数据库/快捷方式修复计划仍有未决问题，尚未写入")
            report_progress("串联修复：写入已确认的数据库更改")
            catalog_result, receipt = _apply_catalog_repair_change(
                current,
                organizer_settings=organizer_settings,
                browse_settings=browse_settings,
            )
            media_result: dict[str, Any]
            if media_move_planned:
                try:
                    report_progress("串联修复：执行已确认的媒体整理")
                    media_result = apply_plan(
                        current["organizer_plan"],
                        confirmation=str(current["organizer_plan"]["plan_id"]),
                    )
                except MediaRollbackError as exc:
                    _preserve_catalog_after_media_failure(
                        exc, changes=catalog_result, receipts=[receipt],
                        operation_key="repair_plan_id", operation_id=reviewed,
                    )
                    raise
                except BaseException:
                    rollback_catalog_yaml_mutation(receipt)
                    raise
            else:
                media_result = {
                    "moved_file_count": 0,
                    "moved_bytes": 0,
                    "skipped": True,
                }
            shortcut_scope = (
                _repair_shortcut_scope_after_catalog(
                    current,
                    organizer_settings=organizer_settings,
                )
                if media_move_planned
                else None
            )
            try:
                report_progress("串联修复：检查和补建快捷方式")
                shortcut_result = apply_scoped_shortcuts_for_work(
                    current["shortcuts"],
                    settings=browse_settings,
                )
            except BaseException as shortcut_exc:
                report_progress("快捷方式阶段失败，检查恢复或重试状态", detail=str(shortcut_exc))
                if media_move_planned:
                    if isinstance(shortcut_exc, Exception):
                        return {
                            "ok": False,
                            "state": "shortcut_pending",
                            "repair_plan_id": reviewed,
                            "catalog": catalog_result,
                            "media": media_result,
                            "shortcut_error": str(shortcut_exc),
                            "shortcut_retry": {
                                "preview_endpoint": (
                                    "/api/media-directory-organizer/landing/shortcuts/preview"
                                ),
                                "apply_endpoint": (
                                    "/api/media-directory-organizer/landing/shortcuts/apply"
                                ),
                                **dict(shortcut_scope or {}),
                            },
                        }
                    raise
                try:
                    rollback_catalog_yaml_mutation(receipt)
                except CatalogRollbackConflictError as rollback_exc:
                    raise CatalogRollbackConflictError(
                        f"快捷方式创建失败，且数据库已有后续改动，未自动回滚：{rollback_exc}"
                    ) from shortcut_exc
                raise
            result = {
                "ok": True,
                "state": "complete",
                "repair_plan_id": reviewed,
                "catalog": catalog_result,
                "shortcuts": shortcut_result,
                "media": media_result,
            }
            _remember_repair_operation(
                reviewed,
                fingerprint=fingerprint,
                result=result,
                catalog_result=catalog_result,
                shortcuts=current["shortcuts"],
            )
            return result


def _preview_existing_catalog_shortcut_retry(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    _browse_settings_for_catalog(organizer_settings, browse_settings)
    root = _validated_root(body.get("root"), organizer_settings)
    catalog_root = organizer_settings.catalog_root.resolve()
    catalog_session = CatalogReadSession()
    issues: list[dict[str, str]] = []
    records: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    normalized_refs: list[dict[str, Any]] = []
    repair_source_records: list[dict[str, Any]] = []
    explicit_refs = body.get("work_refs") if "work_refs" in body else None

    if explicit_refs is None:
        catalog_records = _all_catalog_records_for_root(
            catalog_root=catalog_root, root=root, session=catalog_session,
        )
        repair_source_records = list(catalog_records)
        if not catalog_records:
            issues.append(
                _shortcut_issue(
                    "shortcut-catalog-path-missing",
                    "数据库中没有权威 path 精确等于该作品根目录的作品，不能从磁盘目录名反推",
                    root,
                )
            )
        for record in catalog_records:
            selected_rows: list[dict[str, Any]] = []
            for row in record["presses"]:
                if not row.get("press_format"):
                    issues.append(
                        _shortcut_issue(
                            "shortcut-catalog-press-invalid",
                            "数据库压制记录缺少格式，不能规划快捷方式",
                            record["source"],
                            work_name=record["work"]["name"],
                        )
                    )
                    continue
                if not row.get("press_path"):
                    issues.append(
                        _shortcut_issue(
                            "shortcut-press-path-missing",
                            "数据库压制记录缺少权威 press_path，不能规划快捷方式",
                            record["source"],
                            work_name=record["work"]["name"],
                            press_format=row.get("press_format"),
                            press_group=row.get("press_group"),
                        )
                    )
                    continue
                selected_rows.append(row)
            normalized_refs.append(_work_ref(record, list(record["presses"])))
            if not record["presses"]:
                issues.append(
                    _shortcut_issue(
                        "shortcut-catalog-press-empty",
                        "数据库作品没有压制记录，不能规划快捷方式",
                        record["source"],
                        work_name=record["work"]["name"],
                    )
                )
            if selected_rows:
                records.append((record, selected_rows))
    else:
        if not isinstance(explicit_refs, list) or not explicit_refs:
            raise ValueError("work_refs 显式提交时必须是至少含一个作品引用的数组")
        seen_refs: set[tuple[str, int]] = set()
        for index, raw_ref in enumerate(explicit_refs):
            if not isinstance(raw_ref, Mapping):
                raise ValueError(f"work_refs[{index}] 必须是对象")
            try:
                record = _record_from_work_ref(raw_ref, catalog_root=catalog_root, session=catalog_session)
            except (OSError, ValueError) as exc:
                issues.append(
                    _shortcut_issue(
                        "shortcut-work-ref-invalid",
                        f"无法从数据库精确复核作品引用：{exc}",
                        raw_ref.get("yaml_source_rel") or "",
                    )
                )
                continue
            repair_source_records.append(record)
            work_key = (
                str(record["work"]["yaml_source_rel"]),
                int(record["work"]["index_in_file"]),
            )
            if work_key in seen_refs:
                issues.append(
                    _shortcut_issue(
                        "shortcut-work-ref-duplicate",
                        "同一数据库作品被重复提交",
                        record["source"],
                        work_name=record["work"]["name"],
                    )
                )
                continue
            seen_refs.add(work_key)
            raw_work_path = str(record["work"].get("path") or "").strip()
            path_valid = True
            if not raw_work_path:
                path_valid = False
                issues.append(
                    _shortcut_issue(
                        "shortcut-catalog-path-missing",
                        "数据库作品缺少权威 path，不能规划快捷方式",
                        record["source"],
                        work_name=record["work"]["name"],
                    )
                )
            else:
                try:
                    path_valid = path_key(Path(raw_work_path).expanduser().resolve()) == path_key(root)
                except OSError:
                    path_valid = False
                if not path_valid:
                    issues.append(
                        _shortcut_issue(
                            "shortcut-catalog-path-mismatch",
                            "数据库作品 path 与快捷方式修复根目录不一致",
                            raw_work_path,
                            work_name=record["work"]["name"],
                        )
                    )
            ref_presses = raw_ref.get("presses")
            if not isinstance(ref_presses, list) or not ref_presses:
                issues.append(
                    _shortcut_issue(
                        "shortcut-work-ref-presses-empty",
                        "作品引用没有任何待修复的数据库压制记录",
                        record["source"],
                        work_name=record["work"]["name"],
                    )
                )
                normalized_refs.append(_work_ref(record, []))
                continue
            selected_rows = []
            for press_index, raw_press in enumerate(ref_presses):
                if not isinstance(raw_press, Mapping):
                    issues.append(
                        _shortcut_issue(
                            "shortcut-press-ref-invalid",
                            f"presses[{press_index}] 必须是对象",
                            record["source"],
                            work_name=record["work"]["name"],
                        )
                    )
                    continue
                press_key = str(raw_press.get("press_key") or "")
                try:
                    position = int(press_key.split(":", 1)[0])
                except (TypeError, ValueError):
                    position = -1
                candidate = next(
                    (
                        row
                        for row in record["presses"]
                        if int(row["position"]) == position
                    ),
                    None,
                )
                if (
                    candidate is None
                    or candidate["press_key"] != press_key
                    or _press_identity(candidate) != _press_identity(raw_press)
                ):
                    issues.append(
                        _shortcut_issue(
                            "shortcut-press-ref-changed",
                            "数据库压制记录已经变化，必须重新预览快捷方式计划",
                            record["source"],
                            work_name=record["work"]["name"],
                        )
                    )
                    continue
                if not candidate.get("press_path"):
                    issues.append(
                        _shortcut_issue(
                            "shortcut-press-path-missing",
                            "数据库压制记录缺少权威 press_path，不能规划快捷方式",
                            record["source"],
                            work_name=record["work"]["name"],
                            press_format=candidate.get("press_format"),
                            press_group=candidate.get("press_group"),
                        )
                    )
                    continue
                if any(int(row["position"]) == int(candidate["position"]) for row in selected_rows):
                    issues.append(
                        _shortcut_issue(
                            "shortcut-press-ref-duplicate",
                            "同一数据库压制记录被重复提交",
                            record["source"],
                            work_name=record["work"]["name"],
                        )
                    )
                    continue
                selected_rows.append(candidate)
            normalized_refs.append(_work_ref(record, selected_rows))
            if path_valid and selected_rows:
                records.append((record, selected_rows))

    shortcut_plan = _preview_shortcut_records(
        records,
        root=root,
        require_existing_targets=True,
        initial_issues=issues,
    )
    shortcut_plan["work_refs"] = normalized_refs
    shortcut_plan["shortcut_plan_id"] = _shortcut_plan_id(shortcut_plan)
    shortcut_plan["ready"] = bool(shortcut_plan["shortcuts"]) and not shortcut_plan["issues"]
    response: dict[str, Any] = {
        "ok": True,
        "state": "shortcut_retry_preview",
        "root": str(root),
        "work_refs": normalized_refs,
        "shortcuts": shortcut_plan["shortcuts"],
        "shortcut_summary": shortcut_plan["shortcut_summary"],
        "issues": shortcut_plan["issues"],
        "ready": shortcut_plan["ready"],
        "shortcut_plan_id": shortcut_plan["shortcut_plan_id"],
        "retry_plan_id": shortcut_plan["shortcut_plan_id"],
    }
    repair_reason_codes = [
        str(issue.get("code") or "")
        for issue in shortcut_plan["issues"]
        if isinstance(issue, Mapping)
        and str(issue.get("code") or "") in _REPAIRABLE_SHORTCUT_CODES
    ]
    if repair_reason_codes:
        candidates = [
            _public_repair_candidate(record, root=root)
            for record in {
                (
                    str(record["work"]["yaml_source_rel"]),
                    int(record["work"]["index_in_file"]),
                ): record
                for record in repair_source_records
            }.values()
        ]
        if candidates:
            first = candidates[0] if len(candidates) == 1 else {}
            repair = _repair_endpoint_descriptor(
                root=root,
                reason_codes=repair_reason_codes,
                draft_work=first.get("draft_work") if first else None,
                catalog_ref=first.get("catalog_ref") if first else None,
                candidates=candidates,
            )
        else:
            catalog = MediaCatalog.load(catalog_root, domain="", country="")
            discovery = discover_catalog_work_draft(
                root,
                catalog=catalog,
                settings=organizer_settings,
            )
            repair = shortcut_repair_descriptor_for_registration(discovery)
            repair["reason_codes"] = list(dict.fromkeys(repair_reason_codes))
        response["repair_required"] = True
        response["repair"] = repair
    return response


def _shortcut_retry_plan_id(payload: dict[str, Any]) -> str:
    stable = {
        "version": 1,
        "root": payload["root"],
        "draft_work": payload["draft_work"],
        "catalog_ref": {
            "yaml_source_rel": payload["catalog_change"]["yaml_source_rel"],
            "index_in_file": payload["catalog_change"]["index_in_file"],
            "sha256": payload["catalog_change"]["before_sha256"],
        },
        "shortcuts": [
            {
                "status": row["status"],
                "target_path": row["target_path"],
                "target_exists": row["target_exists_before_move"],
                "shortcut_path": row["shortcut_path"],
                "existing_target_path": row["existing_target_path"],
            }
            for row in payload["shortcuts"]
        ],
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]


def _landing_plan_id(payload: dict[str, Any]) -> str:
    stable = {
        "version": 3,
        "root": payload["root"],
        "draft_work": payload.get("draft_work"),
        "draft_works": payload.get("draft_works") or [],
        "source_work_bindings": payload.get("source_work_bindings") or {},
        "shared_target_bindings": payload.get("shared_target_bindings") or [],
        "catalog_change": payload.get("catalog_change"),
        "catalog_changes": payload.get("catalog_changes") or [],
        "organizer_plan_id": payload["organizer_plan"]["plan_id"],
        "issues": payload.get("issues") or [],
        "media_no_move": bool(payload.get("media_no_move")),
        "shortcuts": [
            {
                "status": row["status"],
                "target_path": row["target_path"],
                "shortcut_path": row["shortcut_path"],
                "existing_target_path": row["existing_target_path"],
            }
            for row in payload["shortcuts"]
        ],
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:_PLAN_ID_LENGTH]


def _preview_catalog_work_append_batch(
    patches: list[dict[str, Any]],
    *,
    settings: JpTvBrowseSettings,
) -> list[dict[str, Any]]:
    """Preview several shared-root appends, composing same-year YAML in memory."""

    changes = [
        preview_catalog_work_append(
            patch,
            settings=settings,
            allow_shared_path_with_different_name=True,
        )
        for patch in patches
    ]
    append_groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for change in changes:
        if change.get("action") == "append":
            append_groups.setdefault(path_key(change["target"]), []).append(change)

    for grouped in append_groups.values():
        target = Path(str(grouped[0]["target"])).expanduser().resolve()
        previous = target.read_bytes() if target.is_file() else b""
        doc = load_yaml_string(previous.decode("utf-8")) if previous else []
        works = _works_list_mut(doc)
        for change in grouped:
            change["index_in_file"] = len(works)
            works.append(_new_work_from_row_patch(change["patch"]))
        after_text = dump_yaml_string(doc)
        load_jp_tv_entries_from_yaml(load_yaml_string(after_text))
        before_sha256 = _file_sha256(previous)
        after_sha256 = _file_sha256(after_text.encode("utf-8"))
        for change in grouped:
            change["before_sha256"] = before_sha256
            change["after_sha256"] = after_sha256
            change["_after_text"] = after_text
    return changes


def _apply_catalog_work_append_batch(
    patches: list[dict[str, Any]],
    *,
    settings: JpTvBrowseSettings,
) -> tuple[list[dict[str, Any]], list[CatalogMutationReceipt]]:
    changes = _preview_catalog_work_append_batch(patches, settings=settings)
    append_groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for change in changes:
        if change.get("action") == "append":
            append_groups.setdefault(path_key(change["target"]), []).append(change)
    receipts: list[CatalogMutationReceipt] = []
    try:
        for grouped in append_groups.values():
            first = grouped[0]
            receipt = apply_catalog_yaml_mutation(
                target=Path(str(first["target"])),
                after_bytes=str(first["_after_text"]).encode("utf-8"),
                settings=settings,
                expected_before_sha256=str(first["before_sha256"]),
                work_ref={
                    "work_refs": [
                        {
                            "yaml_source_rel": str(change["yaml_source_rel"]),
                            "index_in_file": int(change["index_in_file"]),
                        }
                        for change in grouped
                    ]
                },
            )
            receipts.append(receipt)
    except BaseException:
        for receipt in reversed(receipts):
            rollback_catalog_yaml_mutation(receipt)
        raise
    return [_public_catalog_change(change) for change in changes], receipts


def _rollback_catalog_receipts(receipts: list[CatalogMutationReceipt]) -> None:
    for receipt_index, receipt in enumerate(reversed(receipts)):
        report_progress("回滚本次数据库更改", completed=receipt_index, total=len(receipts), unit="数据文件", detail=str(receipt.target))
        rollback_catalog_yaml_mutation(receipt)


def _catalog_work_refs_from_changes(
    changes: list[Mapping[str, Any]],
    patches: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build an exact shortcut retry scope from the catalog rows just committed."""

    if len(changes) != len(patches):
        raise ValueError("批量作品写入结果与已审核作品数量不一致")
    refs: list[dict[str, Any]] = []
    entry_cache: dict[str, list[Any]] = {}
    for change, patch in zip(changes, patches):
        source = Path(str(change.get("target") or "")).expanduser().resolve()
        yaml_source_rel = str(change.get("yaml_source_rel") or "").strip()
        if not source.is_file() or not yaml_source_rel:
            raise ValueError("批量作品写入后无法复核数据库文件")
        entries = entry_cache.get(str(source))
        if entries is None:
            entries = load_jp_tv_yaml_file(source)
            entry_cache[str(source)] = entries
        index = int(change.get("index_in_file", -1))
        if index < 0 or index >= len(entries):
            raise ValueError(f"批量作品写入后的数据库索引无效：{yaml_source_rel}#{index}")
        record = _catalog_entry_record(
            source,
            yaml_source_rel,
            index,
            entries[index],
        )
        if str(record["work"].get("name") or "") != str(patch.get("name") or ""):
            raise ValueError(f"批量作品写入后的作品名不一致：{yaml_source_rel}#{index}")
        selected_rows: list[dict[str, Any]] = []
        for reviewed_press in patch.get("collectioned_ordered") or []:
            if not isinstance(reviewed_press, Mapping):
                raise ValueError("已审核作品包含无效压制记录")
            matches = [
                row
                for row in record["presses"]
                if _press_identity(row) == _press_identity(reviewed_press)
            ]
            if len(matches) != 1:
                raise ValueError(
                    "批量作品写入后无法唯一复核已审核压制记录："
                    f"{record['work']['name']} / "
                    f"{reviewed_press.get('press_format')} / "
                    f"{reviewed_press.get('press_group')}"
                )
            selected_rows.append(matches[0])
        if not selected_rows:
            raise ValueError(f"批量作品没有可供快捷方式重试的压制记录：{record['work']['name']}")
        refs.append(_work_ref(record, selected_rows))
    return refs


def _normalize_shared_target_bindings(
    raw: Any,
    *,
    root: Path,
    catalog_root: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, str]],
    list[str],
    dict[str, str],
]:
    """Resolve explicit shared physical targets to exact existing catalog rows.

    A member with no ``source_names`` is a shortcut/catalog projection only.
    Sources owned by other members still create the one shared physical target.
    """

    if not isinstance(raw, list) or not raw:
        raise ValueError("shared_target_bindings 必须是至少含一项的数组")
    direct_dirs = {
        child.name: child.resolve()
        for child in root.iterdir()
        if child.is_dir()
    }
    normalized_bindings: list[dict[str, Any]] = []
    selected_sources: list[str] = []
    source_work_names: dict[str, str] = {}
    source_binding_indexes: dict[str, int] = {}
    source_press_overrides: dict[str, dict[str, str]] = {}
    selected_press_refs: set[tuple[str, int, str]] = set()
    target_bindings: dict[str, int] = {}
    records_by_key: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()
    catalog_session = CatalogReadSession()

    for binding_index, raw_binding in enumerate(raw):
        if not isinstance(raw_binding, Mapping):
            raise ValueError(f"shared_target_bindings[{binding_index}] 必须是对象")
        press_format = str(raw_binding.get("press_format") or "").strip()
        press_group = str(raw_binding.get("press_group") or "").strip()
        press_path = _repair_press_path(
            raw_binding.get("press_path"),
            index=binding_index,
        )
        if not press_format:
            raise ValueError(
                f"shared_target_bindings[{binding_index}] 必须填写格式"
            )
        if "press_group" not in raw_binding or not isinstance(raw_binding["press_group"], str):
            raise ValueError(f"shared_target_bindings[{binding_index}] 必须选择压制组（可以选择无组）")
        if raw_binding.get("press_group_confirmed") is False:
            raise ValueError(f"shared_target_bindings[{binding_index}] 尚未确认压制组（可以选择无组）")
        target = _target_from_press_path(root, press_path)
        target_key = path_key(target)
        if target_key in target_bindings:
            raise ValueError("同一个实体 press_path 只能声明一个共享目标绑定")
        target_bindings[target_key] = binding_index

        raw_members = raw_binding.get("members")
        if not isinstance(raw_members, list) or len(raw_members) < 2:
            raise ValueError(
                f"shared_target_bindings[{binding_index}] 至少需要两个数据库作品成员"
            )
        normalized_members: list[dict[str, Any]] = []
        member_work_keys: set[tuple[str, int]] = set()
        binding_source_count = 0
        for member_index, raw_member in enumerate(raw_members):
            if not isinstance(raw_member, Mapping):
                raise ValueError(
                    f"shared_target_bindings[{binding_index}].members[{member_index}] 必须是对象"
                )
            raw_ref = raw_member.get("catalog_ref")
            if not isinstance(raw_ref, Mapping):
                raise ValueError("共享目标成员必须携带服务端候选中的 catalog_ref")
            source_sha256 = str(raw_ref.get("source_sha256") or "").strip().casefold()
            if (
                len(source_sha256) != 64
                or any(character not in "0123456789abcdef" for character in source_sha256)
            ):
                raise ValueError("共享目标成员必须原样提交候选中的 source_sha256")
            record = _record_from_work_ref(raw_ref, catalog_root=catalog_root, session=catalog_session)
            work = record["work"]
            existing_work_path = str(work.get("path") or "").strip()
            if existing_work_path:
                try:
                    same_root = path_key(Path(existing_work_path).expanduser().resolve()) == path_key(root)
                except OSError:
                    same_root = False
                if not same_root:
                    raise ValueError(
                        "共享目标成员已经绑定到另一个作品根目录，拒绝覆盖："
                        f"{work['name']} / {existing_work_path}"
                    )
            work_key = (
                str(work["yaml_source_rel"]),
                int(work["index_in_file"]),
            )
            if work_key in member_work_keys:
                raise ValueError("同一个共享目标不能重复绑定同一数据库作品")
            member_work_keys.add(work_key)
            press_key = str(raw_member.get("press_key") or "").strip()
            matches = [
                row
                for row in record.get("presses") or []
                if str(row.get("press_key") or "") == press_key
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"共享目标成员的数据库压制记录已经变化：{work['name']} / {press_key}"
                )
            selected_row = matches[0]
            if (
                normalized_value(str(selected_row.get("press_format") or ""))
                != normalized_value(press_format)
                or normalized_press_group(str(selected_row.get("press_group") or ""))
                != normalized_press_group(press_group)
            ):
                raise ValueError(
                    "共享目标的格式/组必须与每个成员所选数据库压制记录完全一致"
                )
            existing_press_path = str(selected_row.get("press_path") or "").strip()
            if (
                existing_press_path
                and existing_press_path.replace("\\", "/").strip("/").casefold()
                != press_path.casefold()
            ):
                raise ValueError(
                    "共享目标成员的数据库 press_path 已指向其他实体目录，拒绝覆盖："
                    f"{work['name']} / {existing_press_path}"
                )
            press_ref_key = (work_key[0], work_key[1], press_key)
            if press_ref_key in selected_press_refs:
                raise ValueError("同一数据库压制记录不能加入多个共享目标")
            selected_press_refs.add(press_ref_key)

            source_names_raw = raw_member.get("source_names")
            if source_names_raw is None:
                source_names_raw = []
            if not isinstance(source_names_raw, list):
                raise ValueError("共享目标成员 source_names 必须是数组")
            member_sources: list[str] = []
            for raw_name in source_names_raw:
                name = str(raw_name or "").strip()
                if not name:
                    continue
                source = direct_dirs.get(name)
                if source is None:
                    raise ValueError(f"共享目标成员的来源一级目录不存在：{name}")
                if name in source_work_names:
                    raise ValueError(f"来源一级目录不能重复分配：{name}")
                member_sources.append(name)
                selected_sources.append(name)
                source_binding_indexes[name] = binding_index
                source_work_names[name] = str(work["name"])
                source_press_overrides[str(source)] = {
                    "press_format": press_format,
                    "press_group": press_group,
                }
            binding_source_count += len(member_sources)

            refreshed_ref = _catalog_ref_for_record(record)
            normalized_members.append(
                {
                    "catalog_ref": refreshed_ref,
                    "work_name": str(work["name"]),
                    "press_key": press_key,
                    "source_names": member_sources,
                }
            )
            bucket = records_by_key.setdefault(
                work_key,
                {"record": record, "selected_rows": []},
            )
            bucket["selected_rows"].append(
                {
                    **dict(selected_row),
                    "press_path": press_path,
                }
            )
        if binding_source_count == 0 and not target.is_dir():
            raise ValueError(
                "共享目标没有任何媒体来源，且实体目标目录尚不存在"
            )
        normalized_bindings.append(
            {
                "press_format": press_format,
                "press_group": press_group,
                "press_path": press_path,
                "target_path": str(target),
                "members": normalized_members,
            }
        )

    misassigned_target_sources = [
        name
        for name in selected_sources
        if path_key(direct_dirs[name]) in target_bindings
        and target_bindings[path_key(direct_dirs[name])]
        != source_binding_indexes[name]
    ]
    if misassigned_target_sources:
        raise ValueError(
            "一个共享实体目标不能作为另一个共享绑定的待整理来源："
            + " / ".join(sorted(misassigned_target_sources, key=str.casefold))
        )
    # Existing physical targets must remain in the server-side scan even when
    # the UI correctly treats them as already-landed targets rather than
    # incoming sources.  Canonical targets are skipped without a move;
    # targets containing loose files still enter normal fail-closed routing.
    for name, source in direct_dirs.items():
        if path_key(source) in target_bindings and name not in selected_sources:
            selected_sources.append(name)
    incoming_direct_dirs = {
        name
        for name, source in direct_dirs.items()
        if path_key(source) not in target_bindings
    }
    missing_sources = sorted(
        incoming_direct_dirs - set(selected_sources),
        key=str.casefold,
    )
    if missing_sources:
        raise ValueError(
            "每个待整理一级来源目录都必须且只能分配一次，尚未分配："
            + " / ".join(missing_sources)
        )

    records: list[dict[str, Any]] = []
    patches: list[dict[str, Any]] = []
    for bucket in records_by_key.values():
        record = bucket["record"]
        requested = {
            "name": str(record["work"]["name"]),
            "collectioned_ordered": list(bucket["selected_rows"]),
        }
        patches.append(_merged_update_patch(record, requested, root=root))
        records.append(record)
    return (
        normalized_bindings,
        records,
        patches,
        source_press_overrides,
        selected_sources,
        source_work_names,
    )


def _preview_catalog_shared_update_batch(
    records: list[Mapping[str, Any]],
    patches: list[Mapping[str, Any]],
    *,
    root: Path,
) -> list[dict[str, Any]]:
    if len(records) != len(patches):
        raise ValueError("共享目标数据库记录与修复数据数量不一致")
    grouped: OrderedDict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = (
        OrderedDict()
    )
    for record, patch in zip(records, patches):
        grouped.setdefault(path_key(record["source"]), []).append((record, patch))

    changes: list[dict[str, Any]] = []
    for rows in grouped.values():
        source = Path(str(rows[0][0]["source"])).resolve()
        previous = source.read_bytes()
        doc = load_yaml_string(previous.decode("utf-8"))
        works = _works_list_mut(doc)
        seen_indexes: set[int] = set()
        for record, patch in rows:
            index = int(record["work"]["index_in_file"])
            if index in seen_indexes or index < 0 or index >= len(works):
                raise ValueError("共享目标包含重复或已经变化的数据库作品索引")
            seen_indexes.add(index)
            _apply_selected_path_repair_to_work(
                works[index],
                record,
                patch,
                root=root,
            )
        after_text = dump_yaml_string(doc)
        load_jp_tv_entries_from_yaml(load_yaml_string(after_text))
        before_sha = _file_sha256(previous)
        after_sha = _file_sha256(after_text.encode("utf-8"))
        action = "already_exists" if before_sha == after_sha else "update"
        for record, patch in rows:
            changes.append(
                {
                    "action": action,
                    "target": str(source),
                    "yaml_source_rel": str(record["work"]["yaml_source_rel"]),
                    "index_in_file": int(record["work"]["index_in_file"]),
                    "before_sha256": before_sha,
                    "after_sha256": after_sha,
                    "patch": dict(patch),
                    "_after_text": after_text,
                }
            )
    order = {
        (
            str(record["work"]["yaml_source_rel"]),
            int(record["work"]["index_in_file"]),
        ): index
        for index, record in enumerate(records)
    }
    changes.sort(
        key=lambda change: order[
            (str(change["yaml_source_rel"]), int(change["index_in_file"]))
        ]
    )
    return changes


def _apply_catalog_shared_update_batch(
    records: list[Mapping[str, Any]],
    patches: list[Mapping[str, Any]],
    *,
    root: Path,
    settings: JpTvBrowseSettings,
) -> tuple[list[dict[str, Any]], list[CatalogMutationReceipt]]:
    changes = _preview_catalog_shared_update_batch(records, patches, root=root)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for change in changes:
        grouped.setdefault(path_key(change["target"]), []).append(change)
    receipts: list[CatalogMutationReceipt] = []
    try:
        for rows in grouped.values():
            first = rows[0]
            if str(first["before_sha256"]) == str(first["after_sha256"]):
                continue
            receipt = apply_catalog_yaml_mutation(
                target=Path(str(first["target"])),
                after_bytes=str(first["_after_text"]).encode("utf-8"),
                settings=settings,
                expected_before_sha256=str(first["before_sha256"]),
                work_ref={
                    "work_refs": [
                        {
                            "yaml_source_rel": str(change["yaml_source_rel"]),
                            "index_in_file": int(change["index_in_file"]),
                        }
                        for change in rows
                    ]
                },
            )
            receipts.append(receipt)
    except BaseException:
        _rollback_catalog_receipts(receipts)
        raise
    return [_public_catalog_change(change) for change in changes], receipts


def _catalog_with_shared_repairs(
    catalog: MediaCatalog,
    records: list[Mapping[str, Any]],
    patches: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> MediaCatalog:
    replaced = {
        (
            path_key(record["source"]),
            int(record["work"]["index_in_file"]),
        )
        for record in records
    }
    retained = tuple(
        work
        for work in catalog.works
        if (path_key(work.source_file), work.source_index) not in replaced
    )
    repaired = tuple(
        _draft_catalog_work(patch, change)
        for patch, change in zip(patches, changes)
    )
    return MediaCatalog(works=(*retained, *repaired), catalog_root=catalog.catalog_root)


def _shared_target_shortcuts(
    bindings: list[Mapping[str, Any]],
    records: list[Mapping[str, Any]],
    patches: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    target_by_press_ref: dict[tuple[str, int, str], str] = {}
    for binding in bindings:
        target = str(binding.get("target_path") or "")
        for member in binding.get("members") or []:
            catalog_ref = member.get("catalog_ref") or {}
            target_by_press_ref[
                (
                    str(catalog_ref.get("yaml_source_rel") or ""),
                    int(catalog_ref.get("index_in_file") or 0),
                    str(member.get("press_key") or ""),
                )
            ] = target

    shortcuts: list[dict[str, Any]] = []
    seen_shortcuts: set[str] = set()
    for record, patch in zip(records, patches):
        work = record["work"]
        shortcut_work = {
            **dict(patch),
            "yaml_source_rel": str(work["yaml_source_rel"]),
            "index_in_file": int(work["index_in_file"]),
            "work_key": str(work["work_key"]),
        }
        presses: list[dict[str, Any]] = []
        for row in patch.get("collectioned_ordered") or []:
            ref_key = (
                str(work["yaml_source_rel"]),
                int(work["index_in_file"]),
                str(row.get("press_key") or ""),
            )
            target = target_by_press_ref.get(ref_key)
            if not target:
                raise ValueError("共享目标成员无法精确映射到快捷方式目标")
            presses.append({**dict(row), "target_path": target})
        for shortcut in preview_scoped_shortcuts_for_work(shortcut_work, presses):
            shortcut_key = path_key(shortcut["shortcut_path"])
            if shortcut_key in seen_shortcuts:
                raise ValueError("多个数据库作品生成了同一个快捷方式路径")
            seen_shortcuts.add(shortcut_key)
            shortcuts.append(shortcut)
    return shortcuts


def _preview_shared_target_landing(
    body: Mapping[str, Any],
    *,
    root: Path,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    (
        bindings,
        records,
        patches,
        source_press_overrides,
        source_names,
        source_work_names,
    ) = _normalize_shared_target_bindings(
        body.get("shared_target_bindings"),
        root=root,
        catalog_root=organizer_settings.catalog_root.resolve(),
    )
    catalog_changes = _preview_catalog_shared_update_batch(
        records,
        patches,
        root=root,
    )
    catalog = MediaCatalog.load(organizer_settings.catalog_root, domain="", country="")
    plan_catalog = _catalog_with_shared_repairs(catalog, records, patches, catalog_changes)
    organizer_plan = build_plan(
        root,
        catalog=plan_catalog,
        settings=organizer_settings,
        source_names=source_names,
        source_work_overrides=source_work_names,
        source_press_overrides=source_press_overrides,
    )
    shortcuts = _shared_target_shortcuts(bindings, records, patches)
    shortcut_conflicts = [row for row in shortcuts if row.get("status") == "conflict"]
    assignment_targets = {
        path_key(str(row.get("target_dir") or ""))
        for row in organizer_plan.get("assignments") or []
        if isinstance(row, Mapping) and str(row.get("target_dir") or "").strip()
    }
    issues: list[dict[str, str]] = []
    for binding in bindings:
        target = Path(str(binding["target_path"]))
        target_is_planned = path_key(target) in assignment_targets
        if not target_is_planned and not target.is_dir():
            issues.append(
                _shortcut_issue(
                    "shared-target-without-media-route",
                    "共享实体目标尚不存在，且媒体计划不会创建该目录",
                    target,
                )
            )
        elif not target_is_planned and not _directory_contains_regular_file(target):
            issues.append(
                _shortcut_issue(
                    "shared-target-empty",
                    "共享实体目标中没有普通媒体文件，拒绝写入数据库或创建快捷方式",
                    target,
                )
            )
    if body.get("acknowledge_shared_targets") is not True:
        issues.append(
            _shortcut_issue(
                "shared-target-confirmation-required",
                "必须明确确认多个数据库作品共享同一个实体媒体目录",
                root,
            )
        )
    for shortcut in shortcut_conflicts:
        issues.append(
            _shortcut_issue(
                "shortcut-conflict",
                "快捷方式路径已存在但目标不同，拒绝覆盖",
                shortcut.get("shortcut_path") or "",
            )
        )
    shared_no_move_candidate = bool(
        not organizer_plan.get("ready")
        and not (organizer_plan.get("moves") or [])
        and not (organizer_plan.get("issues") or [])
        and not (organizer_plan.get("unresolved_files") or [])
        and int((organizer_plan.get("summary") or {}).get("scanned_file_count") or 0)
        == 0
        and all(Path(str(binding["target_path"])).is_dir() for binding in bindings)
    )
    shared_no_move_ready = shared_no_move_candidate and all(
        _directory_contains_regular_file(Path(str(binding["target_path"])))
        for binding in bindings
    )
    if shared_no_move_ready:
        # ``build_plan`` normally reserves ready=true for a non-empty move
        # set.  Shared landing also has a safe zero-move transaction: repair
        # exact DB rows and create their shortcuts when every physical target
        # is already canonical and present.  The plan is still rebuilt under
        # the landing lock before apply, and apply_plan verifies its plan ID.
        organizer_plan["ready"] = True
        organizer_plan["shared_no_move"] = True
    payload: dict[str, Any] = {
        "ok": True,
        "state": "shared_target_landing_preview",
        "root": str(root),
        "shared_target_bindings": bindings,
        "catalog_changes": [_public_catalog_change(change) for change in catalog_changes],
        "organizer_plan": organizer_plan,
        "shortcuts": shortcuts,
        "issues": issues,
        "shared_target_summary": {
            "binding_count": len(bindings),
            "database_record_count": len(records),
            "physical_target_count": len(
                {path_key(binding["target_path"]) for binding in bindings}
            ),
            "shortcut_count": len(shortcuts),
        },
        "shortcut_summary": {
            "total_count": len(shortcuts),
            "planned_count": sum(1 for row in shortcuts if row.get("status") == "planned"),
            "already_exists_count": sum(
                1 for row in shortcuts if row.get("status") == "already_exists"
            ),
            "conflict_count": len(shortcut_conflicts),
        },
        "ready": bool(organizer_plan.get("ready")) and bool(shortcuts) and not issues,
    }
    payload["landing_plan_id"] = _landing_plan_id(payload)
    return payload


def _mixed_binding_source(root: Path, raw_source: Any) -> Path:
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError("source_work_bindings 的来源目录必须是非空字符串")
    configured = Path(raw_source.strip()).expanduser()
    lexical = configured if configured.is_absolute() else root / configured
    source = lexical.resolve()
    if source.parent != root or not source.is_dir():
        raise ValueError(f"来源作品绑定必须指向作品根目录下已存在的一级目录：{source}")
    if _is_reparse_point(lexical):
        raise ValueError(f"来源作品绑定不能指向符号链接或目录联接：{lexical}")
    return source


def _mixed_binding_press(
    raw_binding: Mapping[str, Any],
    *,
    source_name: str,
    index: int,
) -> dict[str, Any]:
    raw_presses = raw_binding.get("presses")
    draft = raw_binding.get("draft_work")
    if raw_presses is None and isinstance(draft, Mapping):
        raw_presses = draft.get("presses")
    if not isinstance(raw_presses, list):
        raise ValueError(f"来源绑定 {source_name!r} 必须提供 presses 数组")
    matches: list[Mapping[str, Any]] = []
    for raw_press in raw_presses:
        if not isinstance(raw_press, Mapping):
            raise ValueError(f"来源绑定 {source_name!r} 的 presses 项必须是对象")
        raw_sources = raw_press.get("source_names")
        names = (
            [
                str(value).strip()
                for value in raw_sources
                if isinstance(value, str) and value.strip()
            ]
            if isinstance(raw_sources, list)
            else []
        )
        if source_name in names:
            matches.append(raw_press)
    if len(matches) != 1:
        raise ValueError(
            f"来源绑定 {source_name!r} 必须且只能有一条适用于该来源的压制记录"
        )
    raw_press = matches[0]
    press_format = str(raw_press.get("press_format") or "").strip()
    press_group = str(raw_press.get("press_group") or "").strip()
    press_path = _repair_press_path(raw_press.get("press_path"), index=index)
    if not press_format:
        raise ValueError(f"来源绑定 {source_name!r} 必须填写 press_format")
    if "press_group" not in raw_press or not isinstance(raw_press["press_group"], str):
        raise ValueError(f"来源绑定 {source_name!r} 必须选择 press_group（可以选择无组）")
    if raw_press.get("press_group_confirmed") is False:
        raise ValueError(f"来源绑定 {source_name!r} 尚未确认 press_group（可以选择无组）")
    result: dict[str, Any] = {
        "source_names": [source_name],
        "press_format": press_format,
        "press_group": press_group,
        "press_path": press_path,
    }
    press_key = str(raw_press.get("press_key") or "").strip()
    if press_key:
        result["press_key"] = press_key
    return result


def _normalize_mixed_source_bindings(
    raw: Any,
    *,
    root: Path,
    catalog_root: Path,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("source_work_bindings 必须是至少含一项的对象")
    normalized: OrderedDict[str, dict[str, Any]] = OrderedDict()
    source_names: list[str] = []
    source_press_overrides: dict[str, dict[str, str]] = {}
    source_work_keys: dict[str, str] = {}
    existing_groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    draft_groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    target_owners: dict[str, str] = {}
    catalog_session = CatalogReadSession()

    for index, (raw_source, raw_binding) in enumerate(raw.items()):
        source = _mixed_binding_source(root, raw_source)
        if source.name in normalized:
            raise ValueError(f"来源一级目录重复绑定：{source.name}")
        if not isinstance(raw_binding, Mapping):
            raise ValueError(f"来源绑定 {source.name!r} 必须是对象")
        mode = str(raw_binding.get("mode") or "").strip().casefold()
        if mode not in {"catalog", "draft"}:
            raise ValueError(
                f"完整落地只接受 catalog 或 draft 来源绑定：{source.name} / {mode or '-'}"
            )
        press = _mixed_binding_press(
            raw_binding,
            source_name=source.name,
            index=index,
        )
        target = _target_from_press_path(root, str(press["press_path"]))
        source_names.append(source.name)
        source_press_overrides[str(source)] = {
            "press_format": str(press["press_format"]),
            "press_group": str(press["press_group"]),
        }

        if mode == "catalog":
            if raw_binding.get("draft_work") is not None:
                raise ValueError(f"来源绑定 {source.name!r} 不能同时提交 catalog_ref 和 draft_work")
            raw_ref = raw_binding.get("catalog_ref")
            if not isinstance(raw_ref, Mapping):
                raise ValueError(f"来源绑定 {source.name!r} 缺少 catalog_ref")
            source_sha256 = str(raw_ref.get("source_sha256") or "").strip().casefold()
            if (
                len(source_sha256) != 64
                or any(character not in "0123456789abcdef" for character in source_sha256)
            ):
                raise ValueError("catalog 来源绑定必须原样提交服务端候选中的 source_sha256")
            record = _record_from_work_ref(raw_ref, catalog_root=catalog_root, session=catalog_session)
            work = record["work"]
            group_key = (
                f"catalog:{work['yaml_source_rel']}#{int(work['index_in_file'])}"
            )
            group = existing_groups.setdefault(
                group_key,
                {"record": record, "presses": [], "sources": []},
            )
            normalized_ref = _catalog_ref_for_record(record)
            work_name = str(work["name"])
            normalized_binding = {
                "mode": "catalog",
                "catalog_ref": normalized_ref,
                "work_name": work_name,
                "presses": [dict(press)],
            }
        else:
            if raw_binding.get("catalog_ref") is not None:
                raise ValueError(f"来源绑定 {source.name!r} 的 draft 模式不能提交 catalog_ref")
            raw_draft = raw_binding.get("draft_work")
            if not isinstance(raw_draft, Mapping):
                raise ValueError(f"来源绑定 {source.name!r} 缺少 draft_work")
            work_name = str(raw_draft.get("name") or "").strip()
            identity = normalized_identity(work_name)
            if not identity:
                raise ValueError(f"来源绑定 {source.name!r} 的新增作品名不能为空")
            group_key = f"draft:{identity}"
            metadata = {
                "name": work_name,
                "date": dict(raw_draft.get("date") or {}),
                "domain": str(raw_draft.get("domain") or "").strip(),
                "country": str(raw_draft.get("country") or "").strip(),
                "release_type": str(raw_draft.get("release_type") or "").strip(),
                "path": str(root),
                "markers": list(raw_draft.get("markers") or [])
                if isinstance(raw_draft.get("markers"), list)
                else [],
            }
            group = draft_groups.get(group_key)
            if group is None:
                group = {"metadata": metadata, "presses": [], "sources": []}
                draft_groups[group_key] = group
            elif group["metadata"] != metadata:
                raise ValueError(f"同一新增作品在多个来源绑定中的元数据不一致：{work_name}")
            normalized_binding = {
                "mode": "draft",
                "draft_work": {**metadata, "presses": [dict(press)]},
                "work_name": work_name,
                "presses": [dict(press)],
            }

        target_key = path_key(target)
        owner = target_owners.get(target_key)
        if owner is not None and owner != group_key:
            raise ValueError(
                "不同作品不能通过普通来源绑定共享同一个 press_path；请使用 shared_target_bindings："
                + str(press["press_path"])
            )
        target_owners[target_key] = group_key
        pair = (
            normalized_value(str(press["press_format"])),
            normalized_press_group(str(press["press_group"])),
        )
        duplicate = next(
            (
                row
                for row in group["presses"]
                if (
                    normalized_value(str(row["press_format"])),
                    normalized_press_group(str(row["press_group"])),
                )
                == pair
            ),
            None,
        )
        if duplicate is not None:
            if normalized_value(str(duplicate["press_path"])) != normalized_value(
                str(press["press_path"])
            ):
                raise ValueError(
                    f"同一作品的相同格式/组不能绑定多个 press_path：{work_name} / "
                    f"{press['press_format']} / {press['press_group']}"
                )
            duplicate["source_names"] = list(
                dict.fromkeys([*duplicate.get("source_names", []), source.name])
            )
        else:
            group["presses"].append(dict(press))
        group["sources"].append(source.name)
        source_work_keys[str(source)] = group_key
        normalized[source.name] = normalized_binding

    direct_sources = {
        child.name
        for child in root.iterdir()
        if child.is_dir()
    }
    missing_sources = sorted(direct_sources - set(normalized), key=str.casefold)
    if missing_sources:
        raise ValueError(
            "混合来源完整落地必须覆盖每个一级目录，尚未绑定："
            + " / ".join(missing_sources)
        )

    existing_records: list[dict[str, Any]] = []
    existing_patches: list[dict[str, Any]] = []
    existing_keys: list[str] = []
    for group_key, group in existing_groups.items():
        record = group["record"]
        patch = _merged_update_patch(
            record,
            {
                "name": str(record["work"]["name"]),
                "collectioned_ordered": group["presses"],
            },
            root=root,
        )
        existing_keys.append(group_key)
        existing_records.append(record)
        existing_patches.append(patch)

    draft_patches: list[dict[str, Any]] = []
    draft_keys: list[str] = []
    for group_key, group in draft_groups.items():
        patch = _strict_new_work_patch(
            {
                **group["metadata"],
                "collectioned_ordered": group["presses"],
            }
        )
        draft_keys.append(group_key)
        draft_patches.append(patch)

    return {
        "bindings": dict(normalized),
        "source_names": source_names,
        "source_press_overrides": source_press_overrides,
        "source_work_keys": source_work_keys,
        "existing_keys": existing_keys,
        "existing_records": existing_records,
        "existing_patches": existing_patches,
        "draft_keys": draft_keys,
        "draft_patches": draft_patches,
    }


def _preview_mixed_catalog_changes(
    normalized: Mapping[str, Any],
    *,
    settings: JpTvBrowseSettings,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for key, record, patch in zip(
        normalized["existing_keys"],
        normalized["existing_records"],
        normalized["existing_patches"],
    ):
        operations.append(
            {
                "key": key,
                "kind": "existing",
                "target": Path(str(record["source"])).resolve(),
                "yaml_source_rel": str(record["work"]["yaml_source_rel"]),
                "index_in_file": int(record["work"]["index_in_file"]),
                "record": record,
                "patch": patch,
            }
        )
    for key, patch in zip(normalized["draft_keys"], normalized["draft_patches"]):
        preview = preview_catalog_work_append(
            patch,
            settings=settings,
            allow_shared_path_with_different_name=True,
        )
        if preview.get("action") != "append":
            raise ValueError(
                f"新增作品已存在于数据库；请改用精确 catalog_ref：{patch['name']}"
            )
        operations.append(
            {
                "key": key,
                "kind": "draft",
                "target": Path(str(preview["target"])).resolve(),
                "yaml_source_rel": str(preview["yaml_source_rel"]),
                "index_in_file": -1,
                "record": None,
                "patch": patch,
            }
        )

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for operation in operations:
        grouped.setdefault(path_key(operation["target"]), []).append(operation)
    for grouped_operations in grouped.values():
        target = grouped_operations[0]["target"]
        previous = target.read_bytes() if target.is_file() else b""
        doc = load_yaml_string(previous.decode("utf-8")) if previous else []
        works = _works_list_mut(doc)
        used_existing_indexes: set[int] = set()
        for operation in grouped_operations:
            if operation["kind"] == "existing":
                index = int(operation["index_in_file"])
                if index in used_existing_indexes or index < 0 or index >= len(works):
                    raise ValueError("混合落地中的数据库作品索引重复或已经变化")
                used_existing_indexes.add(index)
                _apply_selected_path_repair_to_work(
                    works[index],
                    operation["record"],
                    operation["patch"],
                    root=Path(str(operation["patch"]["path"])).resolve(),
                )
            else:
                operation["index_in_file"] = len(works)
                works.append(_new_work_from_row_patch(operation["patch"]))
        after_text = dump_yaml_string(doc)
        load_jp_tv_entries_from_yaml(load_yaml_string(after_text))
        before_sha = _file_sha256(previous)
        after_sha = _file_sha256(after_text.encode("utf-8"))
        for operation in grouped_operations:
            operation["before_sha256"] = before_sha
            operation["after_sha256"] = after_sha
            operation["_after_text"] = after_text
            operation["action"] = (
                "append"
                if operation["kind"] == "draft"
                else "already_exists"
                if before_sha == after_sha
                else "update"
            )
    return operations


def _existing_catalog_work_after_patch(
    record: Mapping[str, Any],
    patch: Mapping[str, Any],
    change: Mapping[str, Any],
) -> CatalogWork:
    presses = [
        PressRecord(
            press_format=str(row.get("press_format") or ""),
            press_group=str(row.get("press_group") or ""),
            press_path=str(row.get("press_path") or ""),
        )
        for row in record.get("presses") or []
        if isinstance(row, Mapping)
    ]
    for row in patch.get("collectioned_ordered") or []:
        position = int(row.get("position", len(presses)))
        value = PressRecord(
            press_format=str(row.get("press_format") or ""),
            press_group=str(row.get("press_group") or ""),
            press_path=str(row.get("press_path") or ""),
        )
        if 0 <= position < len(presses):
            presses[position] = value
        else:
            presses.append(value)
    work = record["work"]
    return CatalogWork(
        name=str(work["name"]),
        path=str(patch["path"]),
        domain=str(work["domain"]),
        country=str(work["country"]),
        release_type=str(work["release_type"]),
        presses=tuple(presses),
        source_file=str(record["source"]),
        source_index=int(work["index_in_file"]),
        source_sha256=str(change.get("before_sha256") or ""),
        start_date=str((work.get("date") or {}).get("start") or ""),
        end_date=str((work.get("date") or {}).get("end") or ""),
    )


def _apply_mixed_catalog_changes(
    changes: list[Mapping[str, Any]],
    *,
    settings: JpTvBrowseSettings,
) -> tuple[list[dict[str, Any]], list[CatalogMutationReceipt]]:
    grouped: OrderedDict[str, list[Mapping[str, Any]]] = OrderedDict()
    for change in changes:
        grouped.setdefault(path_key(change["target"]), []).append(change)
    receipts: list[CatalogMutationReceipt] = []
    try:
        for rows in grouped.values():
            first = rows[0]
            if str(first["before_sha256"]) == str(first["after_sha256"]):
                continue
            receipt = apply_catalog_yaml_mutation(
                target=Path(str(first["target"])),
                after_bytes=str(first["_after_text"]).encode("utf-8"),
                settings=settings,
                expected_before_sha256=str(first["before_sha256"]),
                work_ref={
                    "work_refs": [
                        {
                            "yaml_source_rel": str(change["yaml_source_rel"]),
                            "index_in_file": int(change["index_in_file"]),
                        }
                        for change in rows
                    ]
                },
            )
            receipts.append(receipt)
    except BaseException:
        _rollback_catalog_receipts(receipts)
        raise
    return [_public_catalog_change(dict(change)) for change in changes], receipts


def _preview_mixed_source_landing(
    body: Mapping[str, Any],
    *,
    root: Path,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    normalized = _normalize_mixed_source_bindings(
        body.get("source_work_bindings"),
        root=root,
        catalog_root=organizer_settings.catalog_root.resolve(),
    )
    changes = _preview_mixed_catalog_changes(normalized, settings=browse_settings)
    change_by_key = {str(change["key"]): change for change in changes}
    catalog = MediaCatalog.load(organizer_settings.catalog_root, domain="", country="")
    selected_existing = {
        (
            path_key(record["source"]),
            int(record["work"]["index_in_file"]),
        )
        for record in normalized["existing_records"]
    }
    retained = [
        work
        for work in catalog.works
        if (path_key(work.source_file), work.source_index) not in selected_existing
    ]
    works_by_key: dict[str, CatalogWork] = {}
    for key, record, patch in zip(
        normalized["existing_keys"],
        normalized["existing_records"],
        normalized["existing_patches"],
    ):
        works_by_key[key] = _existing_catalog_work_after_patch(
            record,
            patch,
            change_by_key[key],
        )
    for key, patch in zip(normalized["draft_keys"], normalized["draft_patches"]):
        works_by_key[key] = _draft_catalog_work(patch, change_by_key[key])
    plan_catalog = MediaCatalog(
        works=(*works_by_key.values(), *retained),
        catalog_root=catalog.catalog_root,
    )
    source_catalog_overrides = {
        source: works_by_key[key]
        for source, key in normalized["source_work_keys"].items()
    }
    organizer_plan = build_plan(
        root,
        catalog=plan_catalog,
        settings=organizer_settings,
        source_names=normalized["source_names"],
        source_catalog_work_overrides=source_catalog_overrides,
        source_press_overrides=normalized["source_press_overrides"],
    )

    shortcuts: list[dict[str, Any]] = []
    seen_shortcuts: set[str] = set()
    all_patches = [*normalized["existing_patches"], *normalized["draft_patches"]]
    all_keys = [*normalized["existing_keys"], *normalized["draft_keys"]]
    target_paths: dict[str, Path] = {}
    for key, patch in zip(all_keys, all_patches):
        change = change_by_key[key]
        shortcut_work = {
            **dict(patch),
            "yaml_source_rel": str(change["yaml_source_rel"]),
            "index_in_file": int(change["index_in_file"]),
            "work_key": f"{change['yaml_source_rel']}#{change['index_in_file']}",
        }
        press_rows: list[dict[str, Any]] = []
        for index, row in enumerate(patch.get("collectioned_ordered") or []):
            target = _target_from_press_path(root, str(row.get("press_path") or ""))
            target_paths[path_key(target)] = target
            press_rows.append(
                {
                    **dict(row),
                    "press_key": str(row.get("press_key") or "")
                    or f"{index}:main::{row.get('press_format')}:{row.get('press_group')}",
                    "target_path": str(target),
                }
            )
        for shortcut in preview_scoped_shortcuts_for_work(shortcut_work, press_rows):
            shortcut_key = path_key(shortcut["shortcut_path"])
            if shortcut_key in seen_shortcuts:
                raise ValueError("混合来源落地会生成重复快捷方式路径")
            seen_shortcuts.add(shortcut_key)
            shortcuts.append(shortcut)

    assignment_targets = {
        path_key(str(row.get("target_dir") or ""))
        for row in organizer_plan.get("assignments") or []
        if isinstance(row, Mapping) and str(row.get("target_dir") or "").strip()
    }
    issues: list[dict[str, str]] = []
    for target_key, target in sorted(
        target_paths.items(),
        key=lambda item: str(item[1]).casefold(),
    ):
        if target_key in assignment_targets:
            continue
        if not target.is_dir():
            issues.append(
                _shortcut_issue(
                    "mixed-target-without-media-route",
                    "压制目标尚不存在，且媒体计划不会创建该目录",
                    target,
                )
            )
        elif not _directory_contains_regular_file(target):
            issues.append(
                _shortcut_issue(
                    "mixed-target-empty",
                    "零移动压制目标中没有普通媒体文件，拒绝写数据库或创建快捷方式",
                    target,
                )
            )
    shortcut_conflicts = [row for row in shortcuts if row.get("status") == "conflict"]
    for shortcut in shortcut_conflicts:
        issues.append(
            _shortcut_issue(
                "shortcut-conflict",
                "快捷方式路径已存在但目标不同，拒绝覆盖",
                shortcut.get("shortcut_path") or "",
            )
        )
    no_move_ready = bool(
        not organizer_plan.get("moves")
        and not organizer_plan.get("issues")
        and not organizer_plan.get("unresolved_files")
        and not issues
        and target_paths
    )
    if no_move_ready:
        organizer_plan["ready"] = True
        organizer_plan["mixed_no_move"] = True
    media_ready = bool(organizer_plan.get("ready")) or no_move_ready
    payload: dict[str, Any] = {
        "ok": True,
        "state": "mixed_source_landing_preview",
        "root": str(root),
        "source_work_bindings": normalized["bindings"],
        "source_work_binding_summary": {
            "total_count": len(normalized["bindings"]),
            "catalog_count": sum(
                1
                for binding in normalized["bindings"].values()
                if binding.get("mode") == "catalog"
            ),
            "draft_count": sum(
                1
                for binding in normalized["bindings"].values()
                if binding.get("mode") == "draft"
            ),
            "existing_work_count": len(normalized["existing_keys"]),
            "new_work_count": len(normalized["draft_keys"]),
            "press_count": sum(
                len(binding.get("presses") or [])
                for binding in normalized["bindings"].values()
            ),
        },
        "catalog_changes": [_public_catalog_change(dict(change)) for change in changes],
        "organizer_plan": organizer_plan,
        "shortcuts": shortcuts,
        "issues": issues,
        "media_no_move": no_move_ready,
        "shortcut_summary": {
            "total_count": len(shortcuts),
            "planned_count": sum(1 for row in shortcuts if row.get("status") == "planned"),
            "already_exists_count": sum(
                1 for row in shortcuts if row.get("status") == "already_exists"
            ),
            "conflict_count": len(shortcut_conflicts),
        },
        "ready": media_ready and bool(shortcuts) and not issues,
    }
    payload["landing_plan_id"] = _landing_plan_id(payload)
    return payload


def _preview_multi_work_landing(
    body: Mapping[str, Any],
    *,
    root: Path,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    patches, source_press_overrides, source_names, source_work_names = (
        _normalize_draft_works(body.get("draft_works"), root=root)
    )
    catalog_changes = _preview_catalog_work_append_batch(
        patches,
        settings=browse_settings,
    )
    catalog = MediaCatalog.load(organizer_settings.catalog_root, domain="", country="")
    draft_works: list[CatalogWork] = []
    for patch, change in zip(patches, catalog_changes):
        already_present = any(
            normalized_identity(work.name) == normalized_identity(str(patch["name"]))
            and work.path
            and Path(work.path).expanduser().resolve() == root
            for work in catalog.works
        )
        if not already_present:
            draft_works.append(_draft_catalog_work(patch, change))
    plan_catalog = catalog.with_works(draft_works) if draft_works else catalog
    organizer_plan = build_plan(
        root,
        catalog=plan_catalog,
        settings=organizer_settings,
        source_names=source_names,
        file_work_overrides=_multi_file_work_overrides(root, source_work_names),
        source_press_overrides=source_press_overrides,
    )

    shortcuts: list[dict[str, Any]] = []
    seen_shortcuts: set[str] = set()
    for patch, change in zip(patches, catalog_changes):
        shortcut_work = {
            **patch,
            "yaml_source_rel": change["yaml_source_rel"],
            "index_in_file": change["index_in_file"],
            "work_key": f"{change['yaml_source_rel']}#{change['index_in_file']}",
        }
        rows = _shortcut_presses(patch, change, organizer_plan)
        for shortcut in preview_scoped_shortcuts_for_work(shortcut_work, rows):
            key = path_key(shortcut["shortcut_path"])
            if key in seen_shortcuts:
                raise ValueError("多个作品生成了同一个快捷方式路径，拒绝合并多作品落地计划")
            seen_shortcuts.add(key)
            shortcuts.append(shortcut)
    shortcut_conflicts = [row for row in shortcuts if row["status"] == "conflict"]
    payload: dict[str, Any] = {
        "ok": True,
        "state": "multi_work_landing_preview",
        "root": str(root),
        "draft_works": patches,
        "source_press_overrides": source_press_overrides,
        "catalog_changes": [_public_catalog_change(change) for change in catalog_changes],
        "organizer_plan": organizer_plan,
        "shortcuts": shortcuts,
        "shortcut_summary": {
            "total_count": len(shortcuts),
            "planned_count": sum(1 for row in shortcuts if row["status"] == "planned"),
            "already_exists_count": sum(
                1 for row in shortcuts if row["status"] == "already_exists"
            ),
            "conflict_count": len(shortcut_conflicts),
        },
        "ready": bool(organizer_plan.get("ready")) and bool(shortcuts) and not shortcut_conflicts,
    }
    payload["landing_plan_id"] = _landing_plan_id(payload)
    return payload


def preview_work_landing_shortcut_retry(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    """Rebuild a scoped shortcut-only plan for an already-landed catalog work."""

    if "work_refs" in body or "draft_work" not in body:
        return _preview_existing_catalog_shortcut_retry(
            body,
            organizer_settings=organizer_settings,
            browse_settings=browse_settings,
        )

    detail_settings = _browse_settings_for_catalog(organizer_settings, browse_settings)
    root = _validated_root(body.get("root"), organizer_settings)
    requested_patch = _retry_draft_patch(body.get("draft_work"), root=root)
    catalog_change = preview_catalog_work_append(requested_patch, settings=detail_settings)
    if catalog_change.get("action") != "already_exists":
        raise ValueError(
            "shortcut retry is only allowed after the work already exists in the database"
        )
    patch = dict(catalog_change["patch"])
    catalog = MediaCatalog.load(organizer_settings.catalog_root, domain="", country="")
    _assert_retry_catalog_work_exact(
        patch,
        catalog_change,
        catalog=catalog,
        root=root,
    )
    shortcut_work, shortcut_presses = _retry_shortcut_rows(
        patch,
        catalog_change,
        root=root,
    )
    shortcuts = preview_scoped_shortcuts_for_work(shortcut_work, shortcut_presses)
    conflicts = [row for row in shortcuts if row["status"] == "conflict"]
    missing_targets = [row for row in shortcuts if not row["target_exists_before_move"]]
    payload: dict[str, Any] = {
        "ok": True,
        "state": "shortcut_retry_preview",
        "root": str(root),
        "draft_work": patch,
        "catalog_change": _public_catalog_change(catalog_change),
        "shortcuts": shortcuts,
        "shortcut_summary": {
            "total_count": len(shortcuts),
            "planned_count": sum(1 for row in shortcuts if row["status"] == "planned"),
            "already_exists_count": sum(
                1 for row in shortcuts if row["status"] == "already_exists"
            ),
            "conflict_count": len(conflicts),
            "missing_target_count": len(missing_targets),
        },
        "ready": bool(shortcuts) and not conflicts and not missing_targets,
    }
    payload["retry_plan_id"] = _shortcut_retry_plan_id(payload)
    return payload


def apply_work_landing_shortcut_retry(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    """Apply only the reviewed work's missing shortcuts; never move media."""

    if body.get("acknowledge_shortcuts") is not True:
        raise ValueError("shortcut retry requires acknowledge_shortcuts=true")
    reviewed = str(body.get("retry_plan_id") or "")
    confirmation = str(body.get("confirmation") or "")
    if (
        len(reviewed) != _PLAN_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in reviewed)
        or len(confirmation) != _PLAN_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in confirmation)
        or reviewed != confirmation
    ):
        raise ValueError("shortcut retry confirmation must exactly match the 16-character plan ID")

    with _LANDING_LOCK:
        current = preview_work_landing_shortcut_retry(
            body,
            organizer_settings=organizer_settings,
            browse_settings=browse_settings,
        )
        if current["retry_plan_id"] != reviewed:
            raise ValueError(
                "shortcut retry plan changed; no shortcut was written. Preview again with ID "
                f"{current['retry_plan_id']}"
            )
        if not current["ready"]:
            raise ValueError(
                "shortcut retry still has a conflict or missing target directory; no shortcut was written"
            )
        shortcut_result = apply_scoped_shortcuts_for_work(
            current["shortcuts"],
            settings=browse_settings,
        )
        result = {
            "ok": True,
            "state": "complete",
            "retry_plan_id": reviewed,
            "shortcuts": shortcut_result,
        }
        if "draft_work" in current:
            result["draft_work"] = current["draft_work"]
        if "work_refs" in current:
            result["work_refs"] = current["work_refs"]
        return result


def preview_work_landing(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    report_progress("预览数据库登记、媒体归类与快捷方式完整流程")
    detail_settings = _browse_settings_for_catalog(organizer_settings, browse_settings)
    root = _validated_root(body.get("root"), organizer_settings)
    if isinstance(body.get("source_work_bindings"), Mapping):
        return _preview_mixed_source_landing(
            body,
            root=root,
            organizer_settings=organizer_settings,
            browse_settings=detail_settings,
        )
    if isinstance(body.get("shared_target_bindings"), list):
        return _preview_shared_target_landing(
            body,
            root=root,
            organizer_settings=organizer_settings,
            browse_settings=detail_settings,
        )
    if isinstance(body.get("draft_works"), list):
        return _preview_multi_work_landing(
            body,
            root=root,
            organizer_settings=organizer_settings,
            browse_settings=detail_settings,
        )
    patch, source_press_overrides, source_names = _normalize_draft(
        body.get("draft_work"),
        root=root,
    )
    catalog_change = preview_catalog_work_append(patch, settings=detail_settings)
    catalog = MediaCatalog.load(organizer_settings.catalog_root, domain="", country="")
    draft = _draft_catalog_work(patch, catalog_change)
    existing_matches = [
        work
        for work in catalog.works
        if normalized_identity(work.name) == normalized_identity(draft.name)
        and work.path
        and Path(work.path).expanduser().resolve() == root
    ]
    plan_catalog = catalog if existing_matches else catalog.with_works([draft])
    work_overrides = _file_work_overrides(root, source_names, draft.name)
    organizer_plan = build_plan(
        root,
        catalog=plan_catalog,
        settings=organizer_settings,
        source_names=source_names,
        file_work_overrides=work_overrides,
        source_press_overrides=source_press_overrides,
    )
    shortcut_work = {
        **patch,
        "yaml_source_rel": catalog_change["yaml_source_rel"],
        "index_in_file": catalog_change["index_in_file"],
        "work_key": f"{catalog_change['yaml_source_rel']}#{catalog_change['index_in_file']}",
    }
    shortcut_rows = _shortcut_presses(patch, catalog_change, organizer_plan)
    shortcuts = preview_scoped_shortcuts_for_work(shortcut_work, shortcut_rows)
    shortcut_conflicts = [row for row in shortcuts if row["status"] == "conflict"]
    payload: dict[str, Any] = {
        "ok": True,
        "state": "landing_preview",
        "root": str(root),
        "draft_work": patch,
        "source_press_overrides": source_press_overrides,
        "catalog_change": _public_catalog_change(catalog_change),
        "organizer_plan": organizer_plan,
        "shortcuts": shortcuts,
        "shortcut_summary": {
            "planned_count": len(shortcuts),
            "already_exists_count": sum(1 for row in shortcuts if row["status"] == "already_exists"),
            "conflict_count": len(shortcut_conflicts),
        },
        "ready": bool(organizer_plan.get("ready")) and not shortcut_conflicts,
    }
    payload["landing_plan_id"] = _landing_plan_id(payload)
    return payload


def _apply_multi_work_landing(
    current: Mapping[str, Any],
    *,
    reviewed: str,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    report_progress("完整落地：批量登记作品数据库")
    catalog_results, receipts = _apply_catalog_work_append_batch(
        [dict(patch) for patch in current.get("draft_works") or []],
        settings=browse_settings,
    )
    try:
        work_refs = _catalog_work_refs_from_changes(
            catalog_results,
            [dict(patch) for patch in current.get("draft_works") or []],
        )
        media_result = apply_plan(
            dict(current["organizer_plan"]),
            confirmation=str(current["organizer_plan"]["plan_id"]),
        )
    except MediaRollbackError as exc:
        _preserve_catalog_after_media_failure(
            exc, changes=catalog_results, receipts=receipts,
            operation_key="landing_plan_id", operation_id=reviewed,
        )
        raise
    except BaseException:
        _rollback_catalog_receipts(receipts)
        raise
    try:
        shortcut_result = apply_scoped_shortcuts_for_work(
            list(current["shortcuts"]),
            settings=browse_settings,
        )
    except Exception as exc:
        return {
            "ok": False,
            "state": "shortcut_pending",
            "landing_plan_id": reviewed,
            "root": current["root"],
            "draft_works": current["draft_works"],
            "catalog": catalog_results,
            "media": media_result,
            "shortcut_error": str(exc),
            "shortcut_retry": {
                "preview_endpoint": "/api/media-directory-organizer/landing/shortcuts/preview",
                "apply_endpoint": "/api/media-directory-organizer/landing/shortcuts/apply",
                "root": current["root"],
                "work_refs": work_refs,
                "acknowledge_shortcuts": True,
            },
        }
    return {
        "ok": True,
        "state": "complete",
        "landing_plan_id": reviewed,
        "catalog": catalog_results,
        "media": media_result,
        "shortcuts": shortcut_result,
    }


def _apply_shared_target_landing(
    current: Mapping[str, Any],
    *,
    reviewed: str,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    report_progress("完整落地：校验并登记共享媒体目录")
    root = Path(str(current["root"])).expanduser().resolve()
    (
        _bindings,
        records,
        patches,
        _source_press_overrides,
        _source_names,
        _source_work_names,
    ) = _normalize_shared_target_bindings(
        current.get("shared_target_bindings"),
        root=root,
        catalog_root=organizer_settings.catalog_root.resolve(),
    )
    catalog_results, receipts = _apply_catalog_shared_update_batch(
        records,
        patches,
        root=root,
        settings=browse_settings,
    )
    try:
        work_refs = _catalog_work_refs_from_changes(catalog_results, patches)
        media_result = apply_plan(
            dict(current["organizer_plan"]),
            confirmation=str(current["organizer_plan"]["plan_id"]),
        )
    except MediaRollbackError as exc:
        _preserve_catalog_after_media_failure(
            exc, changes=catalog_results, receipts=receipts,
            operation_key="landing_plan_id", operation_id=reviewed,
        )
        raise
    except BaseException:
        _rollback_catalog_receipts(receipts)
        raise
    try:
        shortcut_result = apply_scoped_shortcuts_for_work(
            list(current["shortcuts"]),
            settings=browse_settings,
        )
    except Exception as exc:
        return {
            "ok": False,
            "state": "shortcut_pending",
            "landing_plan_id": reviewed,
            "root": current["root"],
            "shared_target_bindings": current["shared_target_bindings"],
            "catalog": catalog_results,
            "media": media_result,
            "shortcut_error": str(exc),
            "shortcut_retry": {
                "preview_endpoint": "/api/media-directory-organizer/landing/shortcuts/preview",
                "apply_endpoint": "/api/media-directory-organizer/landing/shortcuts/apply",
                "root": current["root"],
                "work_refs": work_refs,
                "acknowledge_shortcuts": True,
            },
        }
    return {
        "ok": True,
        "state": "complete",
        "landing_plan_id": reviewed,
        "shared_target_bindings": current["shared_target_bindings"],
        "catalog": catalog_results,
        "media": media_result,
        "shortcuts": shortcut_result,
    }


def _apply_mixed_source_landing(
    current: Mapping[str, Any],
    *,
    reviewed: str,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    report_progress("完整落地：校验已有作品与新增作品混合登记")
    root = Path(str(current["root"])).expanduser().resolve()
    normalized = _normalize_mixed_source_bindings(
        current["source_work_bindings"],
        root=root,
        catalog_root=organizer_settings.catalog_root.resolve(),
    )
    changes = _preview_mixed_catalog_changes(normalized, settings=browse_settings)
    catalog_results, receipts = _apply_mixed_catalog_changes(
        changes,
        settings=browse_settings,
    )
    patches = [*normalized["existing_patches"], *normalized["draft_patches"]]
    try:
        work_refs = _catalog_work_refs_from_changes(catalog_results, patches)
        if current.get("media_no_move"):
            organizer_plan = current["organizer_plan"]
            media_result = {
                "ok": True,
                "plan_id": str(organizer_plan.get("plan_id") or ""),
                "moved_file_count": 0,
                "moved_bytes": 0,
                "cleaned_directory_count": 0,
                "target_directories": sorted(
                    {
                        str(row.get("target_dir") or "")
                        for row in organizer_plan.get("assignments") or []
                        if isinstance(row, Mapping)
                        and str(row.get("target_dir") or "").strip()
                    },
                    key=str.casefold,
                ),
                "cleanup_warnings": [],
                "already_canonical": True,
            }
        else:
            media_result = apply_plan(
                dict(current["organizer_plan"]),
                confirmation=str(current["organizer_plan"]["plan_id"]),
            )
    except MediaRollbackError as exc:
        _preserve_catalog_after_media_failure(
            exc, changes=catalog_results, receipts=receipts,
            operation_key="landing_plan_id", operation_id=reviewed,
        )
        raise
    except BaseException:
        _rollback_catalog_receipts(receipts)
        raise
    try:
        shortcut_result = apply_scoped_shortcuts_for_work(
            list(current["shortcuts"]),
            settings=browse_settings,
        )
    except Exception as exc:
        return {
            "ok": False,
            "state": "shortcut_pending",
            "landing_plan_id": reviewed,
            "root": current["root"],
            "source_work_bindings": current["source_work_bindings"],
            "source_work_binding_summary": current.get("source_work_binding_summary") or {},
            "catalog": catalog_results,
            "media": media_result,
            "shortcut_error": str(exc),
            "shortcut_retry": {
                "preview_endpoint": "/api/media-directory-organizer/landing/shortcuts/preview",
                "apply_endpoint": "/api/media-directory-organizer/landing/shortcuts/apply",
                "root": current["root"],
                "work_refs": work_refs,
                "acknowledge_shortcuts": True,
            },
        }
    return {
        "ok": True,
        "state": "complete",
        "landing_plan_id": reviewed,
        "source_work_bindings": current["source_work_bindings"],
        "source_work_binding_summary": current.get("source_work_binding_summary") or {},
        "catalog": catalog_results,
        "media": media_result,
        "shortcuts": shortcut_result,
    }


def apply_work_landing(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    report_progress("复核完整落地确认与最新计划")
    if body.get("acknowledge_catalog_write") is not True:
        raise ValueError("必须明确确认新增作品数据库记录")
    if body.get("acknowledge_move") is not True:
        raise ValueError("必须明确确认执行媒体移动")
    if body.get("acknowledge_shortcuts") is not True:
        raise ValueError("必须明确确认新增快捷方式")
    if (
        isinstance(body.get("shared_target_bindings"), list)
        and body.get("acknowledge_shared_targets") is not True
    ):
        raise ValueError("必须明确确认多个数据库作品共享同一个实体媒体目录")
    reviewed = str(body.get("landing_plan_id") or "")
    confirmation = str(body.get("confirmation") or "")
    if len(reviewed) != _PLAN_ID_LENGTH or reviewed != confirmation:
        raise ValueError("完整落地确认 ID 不匹配")

    with _LANDING_LOCK:
        if isinstance(body.get("source_work_bindings"), Mapping):
            if browse_settings.filesystem_root is None:
                raise ValueError("collection-detail 未配置可写数据库目录")
            with catalog_write_transaction(browse_settings.filesystem_root):
                current = preview_work_landing(
                    body,
                    organizer_settings=organizer_settings,
                    browse_settings=browse_settings,
                )
                if current["landing_plan_id"] != reviewed:
                    raise ValueError(
                        "混合来源完整落地计划已经变化，尚未写入或移动；请重新预览："
                        f"{current['landing_plan_id']}"
                    )
                if not current["ready"]:
                    raise ValueError("混合来源完整落地计划仍有未决问题，不能执行")
                return _apply_mixed_source_landing(
                    current,
                    reviewed=reviewed,
                    organizer_settings=organizer_settings,
                    browse_settings=browse_settings,
                )
        if isinstance(body.get("shared_target_bindings"), list):
            if browse_settings.filesystem_root is None:
                raise ValueError("collection-detail 未配置可写数据库目录")
            with catalog_write_transaction(browse_settings.filesystem_root):
                current = preview_work_landing(
                    body,
                    organizer_settings=organizer_settings,
                    browse_settings=browse_settings,
                )
                if current["landing_plan_id"] != reviewed:
                    raise ValueError(
                        "共享实体目录落地计划已经变化，尚未写入或移动；请重新预览："
                        f"{current['landing_plan_id']}"
                    )
                if not current["ready"]:
                    raise ValueError("共享实体目录落地计划仍有未决问题，不能执行")
                return _apply_shared_target_landing(
                    current,
                    reviewed=reviewed,
                    organizer_settings=organizer_settings,
                    browse_settings=browse_settings,
                )
        if isinstance(body.get("draft_works"), list):
            if browse_settings.filesystem_root is None:
                raise ValueError("collection-detail 未配置可写数据库目录")
            with catalog_write_transaction(browse_settings.filesystem_root):
                current = preview_work_landing(
                    body,
                    organizer_settings=organizer_settings,
                    browse_settings=browse_settings,
                )
                if current["landing_plan_id"] != reviewed:
                    raise ValueError(
                        "完整落地计划已经变化，尚未写入或移动；请重新预览："
                        f"{current['landing_plan_id']}"
                    )
                if not current["ready"]:
                    raise ValueError("当前完整落地计划仍有未决问题，不能执行")
                return _apply_multi_work_landing(
                    current,
                    reviewed=reviewed,
                    browse_settings=browse_settings,
                )
        current = preview_work_landing(
            body,
            organizer_settings=organizer_settings,
            browse_settings=browse_settings,
        )
        if current["landing_plan_id"] != reviewed:
            raise ValueError(
                f"完整落地计划已经变化，尚未写入或移动；请重新预览：{current['landing_plan_id']}"
            )
        if not current["ready"]:
            raise ValueError("当前完整落地计划仍有未决问题，不能执行")
        report_progress("完整落地：登记作品数据库")
        catalog_result, receipt = append_catalog_work_from_preview(
            current["draft_work"],
            settings=browse_settings,
            expected_before_sha256=current["catalog_change"]["before_sha256"],
        )
        try:
            media_result = apply_plan(
                current["organizer_plan"],
                confirmation=current["organizer_plan"]["plan_id"],
            )
        except MediaRollbackError as exc:
            _preserve_catalog_after_media_failure(
                exc, changes=catalog_result, receipts=[receipt],
                operation_key="landing_plan_id", operation_id=reviewed,
            )
            raise
        except BaseException:
            report_progress("媒体整理失败，回滚本次数据库登记")
            rollback_catalog_yaml_mutation(receipt)
            raise
        try:
            shortcut_result = apply_scoped_shortcuts_for_work(
                current["shortcuts"],
                settings=browse_settings,
            )
        except Exception as exc:
            return {
                "ok": False,
                "state": "shortcut_pending",
                "landing_plan_id": reviewed,
                "root": current["root"],
                "draft_work": current["draft_work"],
                "catalog": catalog_result,
                "media": media_result,
                "shortcut_error": str(exc),
                "shortcut_retry": {
                    "preview_endpoint": "/api/media-directory-organizer/landing/shortcuts/preview",
                    "apply_endpoint": "/api/media-directory-organizer/landing/shortcuts/apply",
                    "root": current["root"],
                    "draft_work": current["draft_work"],
                    "acknowledge_shortcuts": True,
                },
            }
        return {
            "ok": True,
            "state": "complete",
            "landing_plan_id": reviewed,
            "catalog": catalog_result,
            "media": media_result,
            "shortcuts": shortcut_result,
        }


def group_registry_for_organizer() -> dict[str, Any]:
    return media_group_registry_api_payload()


__all__ = [
    "apply_catalog_shortcut_repair",
    "apply_organizer_plan_shortcuts",
    "apply_work_landing_shortcut_retry",
    "apply_work_landing",
    "discover_catalog_work_draft",
    "group_registry_for_organizer",
    "organizer_transaction_lock",
    "preview_catalog_shortcut_repair",
    "preview_work_landing_shortcut_retry",
    "shortcut_repair_descriptor_for_plan",
    "shortcut_repair_descriptor_for_registration",
    "preview_work_landing",
    "preview_organizer_plan_shortcuts",
]
