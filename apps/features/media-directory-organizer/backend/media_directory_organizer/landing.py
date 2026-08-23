"""Preview-first onboarding for a catalog work, media layout and shortcuts."""
from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from collection_detail.link_index import (
    apply_scoped_shortcuts_for_work,
    preview_scoped_shortcuts_for_work,
)
from collection_detail.save import (
    append_catalog_work_from_preview,
    preview_catalog_work_append,
    rollback_catalog_work_append,
)
from media_directory_organizer.catalog import CatalogWork, MediaCatalog, PressRecord, normalized_identity
from media_directory_organizer.inference import infer_source_press, suggest_press_paths
from media_directory_organizer.service import _scan_files, apply_plan, build_plan
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import (
    entry_air_dates,
    entry_collection_type_data,
    entry_country_slug,
    entry_display_name,
    entry_domain_slug,
    entry_release_type_slug,
)
from work_catalog_yaml.media_groups import (
    load_media_group_registry,
    media_group_code_known,
    media_group_registry_api_payload,
)


_LANDING_LOCK = threading.RLock()
_PLAN_ID_LENGTH = 16


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validated_root(raw: Any, settings: OrganizerSettings) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("root 不能为空")
    root = Path(raw.strip()).expanduser().resolve()
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
        "sources": sources,
    }


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
        press_group = str(raw_press.get("press_group") or "").strip().upper()
        press_path = str(raw_press.get("press_path") or "").strip().replace("\\", "/").strip("/")
        if not press_format or not press_group or not press_path:
            raise ValueError(f"压制记录 {index + 1} 必须填写格式、组简称和目标目录")
        if not media_group_code_known(press_group):
            raise ValueError(f"压制记录 {index + 1} 使用了未登记的组简称：{press_group}")
        pair = (press_format.casefold(), press_group.casefold())
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
    if unassigned_sources:
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
    )


def _shortcut_presses(
    patch: dict[str, Any],
    catalog_change: dict[str, Any],
    organizer_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    targets: dict[tuple[str, str], str] = {}
    for assignment in organizer_plan.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        key = (
            str(assignment.get("press_format") or "").casefold(),
            str(assignment.get("press_group") or "").casefold(),
        )
        target = str(assignment.get("target_dir") or "")
        if key in targets and targets[key].casefold() != target.casefold():
            raise ValueError("同一压制记录被规划到多个目标目录，无法生成唯一快捷方式")
        targets[key] = target
    result: list[dict[str, Any]] = []
    for index, row in enumerate(patch["collectioned_ordered"]):
        key = (str(row["press_format"]).casefold(), str(row["press_group"]).casefold())
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
    return {key: value for key, value in change.items() if not str(key).startswith("_") and key != "patch"}


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


def _date_identity(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _press_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("press_format") or "").strip().casefold(),
        str(row.get("press_group") or "").strip().casefold(),
        str(row.get("press_path") or "").strip().replace("\\", "/").strip("/").casefold(),
    )


def _assert_retry_catalog_work_exact(
    patch: dict[str, Any],
    catalog_change: dict[str, Any],
    *,
    catalog: MediaCatalog,
    root: Path,
) -> None:
    """Require one database work and an exact semantic match with the reviewed draft."""

    matches = [
        work
        for work in catalog.works
        if normalized_identity(work.name) == normalized_identity(str(patch["name"]))
        and work.path
        and Path(work.path).expanduser().resolve() == root
    ]
    if len(matches) != 1:
        raise ValueError(
            "shortcut retry requires exactly one existing database work matching name and path"
        )
    target = Path(str(catalog_change["target"])).expanduser().resolve()
    if Path(matches[0].source_file).resolve() != target:
        raise ValueError("database work reference is ambiguous; shortcut retry was refused")
    entries = load_jp_tv_yaml_file(target)
    index = int(catalog_change["index_in_file"])
    if index < 0 or index >= len(entries):
        raise ValueError("database work index changed; shortcut retry was refused")
    entry = entries[index]
    data = entry_collection_type_data(entry)
    begin_date, end_date = entry_air_dates(entry)
    actual_markers = [
        str(value).strip()
        for value in data.get("markers", [])
        if isinstance(value, str) and value.strip()
    ]
    expected_markers = [
        str(value).strip()
        for value in patch.get("markers", [])
        if isinstance(value, str) and value.strip()
    ]
    actual_presses = data.get("collectioned") if isinstance(data.get("collectioned"), list) else []
    expected_presses = patch["collectioned_ordered"]
    fields_match = (
        entry_display_name(entry).strip() == str(patch["name"]).strip()
        and _date_identity(begin_date) == _date_identity(patch["date"]["start"])
        and _date_identity(end_date) == _date_identity(patch["date"]["end"])
        and entry_domain_slug(entry).strip().casefold() == str(patch["domain"]).strip().casefold()
        and entry_country_slug(entry).strip().casefold() == str(patch["country"]).strip().casefold()
        and entry_release_type_slug(entry).strip().casefold()
        == str(patch["release_type"]).strip().casefold()
        and str(data.get("path") or "").strip()
        and Path(str(data.get("path"))).expanduser().resolve() == root
        and actual_markers == expected_markers
        and len(actual_presses) == len(expected_presses)
        and [_press_identity(row) for row in actual_presses if isinstance(row, Mapping)]
        == [_press_identity(row) for row in expected_presses]
    )
    if not fields_match:
        raise ValueError(
            "database work no longer exactly matches draft_work; shortcut retry was refused"
        )


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
        "version": 1,
        "root": payload["root"],
        "draft_work": payload["draft_work"],
        "catalog_change": payload["catalog_change"],
        "organizer_plan_id": payload["organizer_plan"]["plan_id"],
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


def preview_work_landing_shortcut_retry(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    """Rebuild a scoped shortcut-only plan for an already-landed catalog work."""

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
        return {
            "ok": True,
            "state": "complete",
            "retry_plan_id": reviewed,
            "draft_work": current["draft_work"],
            "shortcuts": shortcut_result,
        }


def preview_work_landing(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    detail_settings = _browse_settings_for_catalog(organizer_settings, browse_settings)
    root = _validated_root(body.get("root"), organizer_settings)
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


def apply_work_landing(
    body: Mapping[str, Any],
    *,
    organizer_settings: OrganizerSettings,
    browse_settings: JpTvBrowseSettings,
) -> dict[str, Any]:
    if body.get("acknowledge_catalog_write") is not True:
        raise ValueError("必须明确确认新增作品数据库记录")
    if body.get("acknowledge_move") is not True:
        raise ValueError("必须明确确认执行媒体移动")
    if body.get("acknowledge_shortcuts") is not True:
        raise ValueError("必须明确确认新增快捷方式")
    reviewed = str(body.get("landing_plan_id") or "")
    confirmation = str(body.get("confirmation") or "")
    if len(reviewed) != _PLAN_ID_LENGTH or reviewed != confirmation:
        raise ValueError("完整落地确认 ID 不匹配")

    with _LANDING_LOCK:
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
        except BaseException:
            rollback_catalog_work_append(receipt)
            raise
        try:
            shortcut_result = apply_scoped_shortcuts_for_work(
                current["shortcuts"],
                settings=browse_settings,
            )
        except (OSError, ValueError, FileExistsError, FileNotFoundError) as exc:
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
    "apply_work_landing_shortcut_retry",
    "apply_work_landing",
    "discover_catalog_work_draft",
    "group_registry_for_organizer",
    "preview_work_landing_shortcut_retry",
    "preview_work_landing",
]
