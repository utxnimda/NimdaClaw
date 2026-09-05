"""Small synchronous facade shared by the Starlette API and desktop UI.

The browser never submits an executable move list.  Preview and apply both
rebuild a plan from the configured catalog, filesystem state and explicit
target overrides.  This keeps the destructive boundary in the backend while
leaving HTTP status/error mapping to the framework application.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from media_directory_organizer.filesystem import is_reparse_point
from typing import Any

from media_directory_organizer.catalog import (
    CatalogWork,
    MediaCatalog,
    normalized_identity,
    normalized_value,
    path_key,
)
from media_directory_organizer.landing import (
    apply_catalog_shortcut_repair,
    apply_organizer_plan_shortcuts,
    apply_work_landing_shortcut_retry,
    apply_work_landing,
    discover_catalog_work_draft,
    group_registry_for_organizer,
    organizer_transaction_lock,
    preview_catalog_shortcut_repair,
    preview_work_landing_shortcut_retry,
    preview_work_landing,
    preview_organizer_plan_shortcuts,
    shortcut_repair_descriptor_for_plan,
    shortcut_repair_descriptor_for_registration,
)
from media_directory_organizer.service import _stable_plan_id, apply_plan, build_plan
from media_directory_organizer.settings import OrganizerSettings, load_organizer_settings
from media_directory_organizer.suggestions import suggest_work_landing
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings, get_resolved_browse_settings


_PLAN_ID_RE = re.compile(r"[0-9a-f]{16}\Z")


def _body_mapping(body: Any) -> Mapping[str, Any]:
    if not isinstance(body, Mapping):
        raise ValueError("请求体必须是 JSON 对象")
    return body


def _string_mapping_from_body(
    body: Mapping[str, Any],
    field: str,
    *,
    key_label: str,
    value_label: str,
) -> dict[str, str]:
    raw = body.get(field)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{field} 必须是 JSON 对象")
    overrides: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{field} 的 {key_label} 必须是非空字符串")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key_label} {key!r} 的 {value_label} 必须是非空字符串")
        overrides[key.strip()] = value.strip()
    return overrides


def _target_overrides_from_body(body: Mapping[str, Any]) -> dict[str, str]:
    return _string_mapping_from_body(
        body,
        "target_overrides",
        key_label="source",
        value_label="目标相对目录",
    )


def _route_target_overrides_from_body(body: Mapping[str, Any]) -> dict[str, str]:
    return _string_mapping_from_body(
        body,
        "route_target_overrides",
        key_label="route_id",
        value_label="目标相对目录",
    )


def _file_work_overrides_from_body(body: Mapping[str, Any]) -> dict[str, str]:
    return _string_mapping_from_body(
        body,
        "file_work_overrides",
        key_label="源文件",
        value_label="数据库作品名",
    )


def _source_work_overrides_from_body(body: Mapping[str, Any]) -> dict[str, str]:
    raw = body.get("source_work_overrides")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("source_work_overrides 必须是 JSON 对象")
    return {
        str(source).strip(): str(value).strip()
        for source, value in raw.items()
        if isinstance(source, str)
        and source.strip()
        and isinstance(value, str)
        and value.strip()
    }


def _source_binding_path(root: Path, raw_source: Any) -> Path:
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError("来源作品绑定的一级目录必须是非空字符串")
    configured = Path(raw_source.strip()).expanduser()
    lexical = configured if configured.is_absolute() else root / configured
    try:
        source = lexical.resolve()
    except OSError as exc:
        raise ValueError(f"来源作品绑定目录无法解析：{raw_source}") from exc
    if source.parent != root or not source.is_dir():
        raise ValueError(f"来源作品绑定必须指向作品根目录下已存在的一级普通目录：{source}")
    if is_reparse_point(lexical):
        raise ValueError(f"来源作品绑定不能指向符号链接或目录联接：{lexical}")
    return source


def _catalog_work_ref(work: CatalogWork, catalog: MediaCatalog) -> dict[str, Any] | None:
    if work.source_index < 0 or not work.source_sha256:
        return None
    try:
        source = Path(work.source_file).expanduser().resolve()
        relative = source.relative_to(catalog.catalog_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if source.parent != catalog.catalog_root.resolve() or source.suffix.casefold() != ".yaml":
        return None
    return {
        "yaml_source_rel": relative,
        "index_in_file": work.source_index,
        "work_name": work.name,
        "source_sha256": work.source_sha256,
    }


def _public_binding_candidate(work: CatalogWork, catalog: MediaCatalog) -> dict[str, Any]:
    return {
        "catalog_ref": _catalog_work_ref(work, catalog),
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


def _catalog_work_from_ref(raw_ref: Any, catalog: MediaCatalog) -> CatalogWork:
    if not isinstance(raw_ref, Mapping):
        raise ValueError("catalog_ref 必须是服务端候选返回的对象")
    yaml_source_rel = str(raw_ref.get("yaml_source_rel") or "").strip().replace("\\", "/")
    work_name = str(raw_ref.get("work_name") or "").strip()
    source_sha256 = str(raw_ref.get("source_sha256") or "").strip().casefold()
    raw_index = raw_ref.get("index_in_file")
    if (
        not yaml_source_rel
        or not work_name
        or len(source_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_sha256)
        or isinstance(raw_index, bool)
    ):
        raise ValueError("catalog_ref 缺少精确数据库文件、索引、作品名或 source_sha256")
    try:
        source_index = int(raw_index)
    except (TypeError, ValueError) as exc:
        raise ValueError("catalog_ref.index_in_file 必须是非负整数") from exc
    if source_index < 0:
        raise ValueError("catalog_ref.index_in_file 必须是非负整数")
    expected_source = catalog.catalog_root.joinpath(*yaml_source_rel.split("/")).resolve()
    if (
        expected_source.parent != catalog.catalog_root.resolve()
        or expected_source.suffix.casefold() != ".yaml"
    ):
        raise ValueError("catalog_ref 越出配置的数据库目录")
    matches = [
        work
        for work in catalog.works
        if Path(work.source_file).expanduser().resolve() == expected_source
        and work.source_index == source_index
        and work.name == work_name
        and work.source_sha256.casefold() == source_sha256
    ]
    if len(matches) != 1:
        raise ValueError("catalog_ref 已失效或数据库记录发生变化，请重新选择作品")
    return matches[0]


def _raw_source_binding_entries(body: Mapping[str, Any]) -> list[tuple[str, Any, str]]:
    entries: list[tuple[str, Any, str]] = []
    seen: set[str] = set()
    for field in ("source_work_bindings", "source_work_overrides"):
        raw = body.get(field)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field} 必须是 JSON 对象")
        for raw_source, value in raw.items():
            if not isinstance(raw_source, str) or not raw_source.strip():
                raise ValueError(f"{field} 的来源目录必须是非空字符串")
            source_key = raw_source.strip().casefold()
            if source_key in seen:
                raise ValueError("同一来源目录不能同时出现在 source_work_bindings 和 source_work_overrides")
            seen.add(source_key)
            entries.append((raw_source.strip(), value, field))
    return entries


def _binding_registration_draft(
    discovery: Mapping[str, Any],
    *,
    source_name: str,
    work_name: str,
) -> dict[str, Any]:
    draft = dict(discovery.get("draft") or {})
    draft["name"] = work_name
    selected_presses = [
        dict(press)
        for press in draft.get("presses") or []
        if source_name in (press.get("source_names") or [])
    ]
    draft["presses"] = selected_presses
    return {
        "state": "catalog_work_required",
        "source_name": source_name,
        "search_query": work_name,
        "draft": draft,
        "suggest_endpoint": "/api/media-directory-organizer/landing/suggest",
        "preview_endpoint": "/api/media-directory-organizer/landing/preview",
    }


def _prepare_source_work_bindings(
    body: Mapping[str, Any],
    *,
    root: Path,
    catalog: MediaCatalog,
    settings: OrganizerSettings,
) -> dict[str, Any]:
    raw_entries = _raw_source_binding_entries(body)
    if not raw_entries:
        return {
            "present": False,
            "rows": [],
            "catalog": catalog,
            "catalog_overrides": {},
            "selected_works_by_source": {},
            "source_work_blockers": {},
            "legacy_overrides": _source_work_overrides_from_body(body),
            "source_names": _source_names_from_body(body),
            "issues": [],
        }

    discovery: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    selected_works: dict[tuple[str, int, str], CatalogWork] = {}
    binding_works: dict[str, CatalogWork] = {}
    source_work_blockers: dict[str, str] = {}
    routable_sources: list[str] = []
    issues: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for raw_source, raw_value, field in raw_entries:
        source = _source_binding_path(root, raw_source)
        source_key = path_key(source)
        if source_key in seen_paths:
            raise ValueError(f"同一来源目录重复绑定：{source}")
        seen_paths.add(source_key)
        if isinstance(raw_value, str):
            requested_mode = "manual"
            work_name = raw_value.strip()
            raw_ref = None
            draft_work = None
            raw_binding_presses: Any = None
        elif isinstance(raw_value, Mapping):
            requested_mode = str(raw_value.get("mode") or "").strip().casefold()
            raw_ref = raw_value.get("catalog_ref")
            work_name = str(raw_value.get("work_name") or "").strip()
            draft_work = raw_value.get("draft_work")
            raw_binding_presses = raw_value.get("presses")
            if raw_binding_presses is None and isinstance(draft_work, Mapping):
                raw_binding_presses = draft_work.get("presses")
            if not requested_mode:
                requested_mode = (
                    "catalog" if raw_ref is not None else "draft" if draft_work is not None else "manual"
                )
        else:
            raise ValueError(f"来源目录 {source.name!r} 的作品绑定必须是字符串或对象")

        row: dict[str, Any] = {
            "source_name": source.name,
            "source_path": str(source),
            "selected": True,
            "can_override": True,
            "requested_mode": requested_mode,
            "requested_work_name": work_name,
            "request_field": field,
            "legacy_shortcut_authoritative": False,
            "catalog_ref": None,
            "resolved_works": [],
            "candidates": [],
            "registration_required": False,
            "registration": None,
            "next_action": "none",
        }
        binding_presses: list[dict[str, str]] = []
        if raw_binding_presses is not None:
            if not isinstance(raw_binding_presses, list):
                raise ValueError(f"来源目录 {source.name!r} 的 presses 必须是数组")
            for raw_press in raw_binding_presses:
                if not isinstance(raw_press, Mapping):
                    raise ValueError(f"来源目录 {source.name!r} 的 presses 项必须是对象")
                raw_sources = raw_press.get("source_names")
                press_sources = (
                    [str(value).strip() for value in raw_sources if isinstance(value, str) and value.strip()]
                    if isinstance(raw_sources, list)
                    else []
                )
                if press_sources and source.name not in press_sources:
                    continue
                press_format = str(raw_press.get("press_format") or "").strip()
                press_group = str(raw_press.get("press_group") or "").strip()
                press_path = str(raw_press.get("press_path") or "").strip()
                if press_format or press_group or press_path:
                    binding_presses.append(
                        {
                            "press_format": press_format,
                            "press_group": press_group,
                            "press_path": press_path,
                        }
                    )
        selected_work: CatalogWork | None = None
        if requested_mode == "catalog":
            if draft_work is not None:
                raise ValueError(f"来源目录 {source.name!r} 不能同时提交 catalog_ref 和 draft_work")
            selected_work = _catalog_work_from_ref(raw_ref, catalog)
            row["state"] = "catalog_bound"
            row["requested_work_name"] = selected_work.name
            row["catalog_ref"] = _catalog_work_ref(selected_work, catalog)
        elif requested_mode == "manual":
            if raw_ref is not None or draft_work is not None:
                raise ValueError(f"来源目录 {source.name!r} 的 manual 模式只能提交 work_name")
            if not work_name:
                raise ValueError(f"来源目录 {source.name!r} 的手输作品名不能为空")
            matches = [
                work
                for work in catalog.works
                if normalized_identity(work.name) == normalized_identity(work_name)
            ]
            row["candidates"] = [_public_binding_candidate(work, catalog) for work in matches]
            if len(matches) == 1:
                selected_work = matches[0]
                row["state"] = "manual_matched"
                row["catalog_ref"] = _catalog_work_ref(selected_work, catalog)
            elif len(matches) > 1:
                row["state"] = "selection_required"
                row["next_action"] = "select_catalog"
                issues.append(
                    {
                        "code": "source-work-selection-required",
                        "message": "手输作品名匹配到多条数据库记录，必须选择精确 catalog_ref",
                        "path": str(source),
                        "work_name": work_name,
                    }
                )
            else:
                if discovery is None:
                    discovery = discover_catalog_work_draft(
                        root,
                        catalog=catalog,
                        settings=settings,
                    )
                row["state"] = "catalog_missing"
                row["registration_required"] = True
                row["registration"] = _binding_registration_draft(
                    discovery,
                    source_name=source.name,
                    work_name=work_name,
                )
                row["next_action"] = "search"
                issues.append(
                    {
                        "code": "source-work-catalog-missing",
                        "message": "手输作品名在数据库中没有精确记录；请先搜索或完整手填作品信息",
                        "path": str(source),
                        "work_name": work_name,
                    }
                )
        elif requested_mode == "draft":
            if raw_ref is not None:
                raise ValueError(f"来源目录 {source.name!r} 的 draft 模式不能提交 catalog_ref")
            if not isinstance(draft_work, Mapping):
                raise ValueError(f"来源目录 {source.name!r} 的 draft 模式缺少 draft_work")
            row["state"] = "draft_ready"
            row["requested_work_name"] = str(draft_work.get("name") or "").strip()
            row["registration_required"] = True
            row["registration"] = {
                "state": "draft_ready",
                "source_name": source.name,
                "draft": dict(draft_work),
                "preview_endpoint": "/api/media-directory-organizer/landing/preview",
            }
            row["next_action"] = "complete_manual"
            issues.append(
                {
                    "code": "source-work-draft-requires-landing",
                    "message": "新作品草稿必须进入完整落地预览后才能写数据库或移动媒体",
                    "path": str(source),
                }
            )
        elif requested_mode == "automatic":
            if raw_ref is not None or draft_work is not None or work_name:
                raise ValueError(f"来源目录 {source.name!r} 的 automatic 模式不能携带作品选择")
            row["state"] = "automatic_pending"
            routable_sources.append(source.name)
        else:
            raise ValueError(f"来源目录 {source.name!r} 的 mode 不受支持：{requested_mode}")

        if selected_work is None and requested_mode != "automatic":
            source_work_blockers[str(source)] = (
                "该来源的人工作品绑定尚未解析为唯一数据库记录；"
                "为避免继承其他作品，本次仅报告缺失/歧义，不生成媒体路由"
            )

        if selected_work is not None:
            ref_key = (selected_work.source_file, selected_work.source_index, selected_work.name)
            selected_works[ref_key] = selected_work
            binding_works[source_key] = selected_work
            routable_sources.append(source.name)
            candidate = _public_binding_candidate(selected_work, catalog)
            row["resolved_works"] = [candidate]
            if not row["candidates"]:
                row["candidates"] = [candidate]
            if not binding_presses:
                if discovery is None:
                    discovery = discover_catalog_work_draft(
                        root,
                        catalog=catalog,
                        settings=settings,
                    )
                source_discovery = next(
                    (
                        item
                        for item in discovery.get("sources") or []
                        if isinstance(item, Mapping)
                        and str(item.get("name") or "") == source.name
                    ),
                    {},
                )
                inferred_format = str(source_discovery.get("suggested_press_format") or "")
                inferred_group = str(source_discovery.get("suggested_press_group") or "")
                inferred_path = str(source_discovery.get("suggested_press_path") or "")
                if inferred_format or inferred_group:
                    binding_presses.append(
                        {
                            "press_format": inferred_format,
                            "press_group": inferred_group,
                            "press_path": inferred_path,
                        }
                    )
            row["inferred_press"] = [
                {
                    "press_format": press["press_format"],
                    "press_group": press["press_group"],
                    "suggested_press_path": press["press_path"],
                }
                for press in binding_presses
            ]
            raw_path = selected_work.path.strip()
            same_root = False
            if raw_path:
                try:
                    same_root = path_key(Path(raw_path).expanduser().resolve()) == path_key(root)
                except OSError:
                    same_root = False
            if not same_root:
                row["catalog_repair_required"] = True
                row["registration_required"] = True
                row["next_action"] = "complete_manual"
                issues.append(
                    {
                        "code": "source-work-catalog-path-repair-required",
                        "message": "所选数据库作品的 path 尚未指向当前作品根目录，必须先做完整落地预览",
                        "path": str(source),
                        "work_name": selected_work.name,
                    }
                )
            press_repair_required = False
            for inferred in binding_presses:
                inferred_format = inferred["press_format"]
                inferred_group = inferred["press_group"]
                if not inferred_format:
                    continue
                same_format = [
                    press
                    for press in selected_work.presses
                    if normalized_value(press.press_format) == normalized_value(inferred_format)
                ]
                exact_press = [
                    press
                    for press in same_format
                    if inferred_group
                    and normalized_value(press.press_group) == normalized_value(inferred_group)
                ]
                if (
                    not same_format
                    or (inferred_group and len(exact_press) != 1)
                    or (len(exact_press) == 1 and not exact_press[0].press_path.strip())
                ):
                    press_repair_required = True
                    break
            if press_repair_required:
                row["catalog_press_required"] = True
                row["catalog_repair_required"] = True
                row["registration_required"] = True
                row["next_action"] = "complete_manual"
                issues.append(
                    {
                        "code": "source-work-catalog-press-repair-required",
                        "message": "所选数据库作品缺少当前来源对应的压制记录或 press_path，必须先做完整落地预览",
                        "path": str(source),
                        "work_name": selected_work.name,
                    }
                )
        rows.append(row)

    # Exact choices are allowed to establish an in-memory family anchor for a
    # read-only preview.  The configured catalog object and YAML files remain
    # untouched; apply still rebuilds and validates the same immutable refs.
    selected_keys = set(selected_works)
    anchored_by_key = {
        key: replace(work, path=str(root))
        for key, work in selected_works.items()
    }
    retained = [
        work
        for work in catalog.works
        if (work.source_file, work.source_index, work.name) not in selected_keys
    ]
    plan_catalog = MediaCatalog(
        works=(*anchored_by_key.values(), *retained),
        catalog_root=catalog.catalog_root,
    )
    catalog_overrides = {
        str(Path(row["source_path"])): anchored_by_key[
            (work.source_file, work.source_index, work.name)
        ]
        for row in rows
        for work in [binding_works.get(path_key(Path(row["source_path"])))]
        if work is not None
    }
    explicit_sources = _source_names_from_body(body)
    return {
        "present": True,
        "rows": rows,
        "catalog": plan_catalog,
        "catalog_overrides": catalog_overrides,
        "selected_works_by_source": binding_works,
        "source_work_blockers": source_work_blockers,
        "legacy_overrides": {},
        # A binding changes authority for that source; it does not silently
        # narrow the media scope.  Only an explicit source_names array may do
        # that.  This prevents one valid binding from producing a partial,
        # executable plan while sibling sources were never reviewed.
        "source_names": explicit_sources,
        "issues": issues,
    }


def _source_press_overrides_from_body(
    body: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    raw = body.get("source_press_overrides")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("source_press_overrides 必须是 JSON 对象")

    overrides: dict[str, dict[str, str]] = {}
    for source, press in raw.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source_press_overrides 的来源目录必须是非空字符串")
        if not isinstance(press, Mapping):
            raise ValueError(f"来源目录 {source!r} 的压制信息必须是 JSON 对象")

        raw_format = press.get("press_format")
        raw_group = press.get("press_group")
        if raw_format is not None and (
            not isinstance(raw_format, str) or not raw_format.strip()
        ):
            raise ValueError(f"来源目录 {source!r} 的 press_format 必须是非空字符串")
        if raw_group is not None and (
            not isinstance(raw_group, str) or not raw_group.strip()
        ):
            raise ValueError(f"来源目录 {source!r} 的 press_group 必须是非空字符串")
        press_format = raw_format.strip() if isinstance(raw_format, str) else ""
        press_group = raw_group.strip() if isinstance(raw_group, str) else ""
        if not press_format and not press_group:
            raise ValueError(
                f"来源目录 {source!r} 至少要提供 press_format 或 press_group"
            )
        override: dict[str, str] = {}
        if press_format:
            override["press_format"] = press_format
        if press_group:
            override["press_group"] = press_group
        overrides[source.strip()] = override
    return overrides


def _source_names_from_body(body: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = body.get("source_names")
    if raw is None:
        return None
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(item, str) or not item.strip() for item in raw)
    ):
        raise ValueError("source_names 显式提交时必须是至少含一个非空字符串的数组")
    return tuple(item.strip() for item in raw)


def _root_from_body(body: Mapping[str, Any], settings: OrganizerSettings) -> str:
    raw = body.get("root")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if settings.default_work_root is not None:
            return str(settings.default_work_root)
        raise ValueError("缺少要整理的作品目录 root")
    if not isinstance(raw, str):
        raise ValueError("root 必须是非空字符串")
    return raw.strip()


def _build_from_ui_body(
    body: Mapping[str, Any],
    *,
    settings: OrganizerSettings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or load_organizer_settings()
    root = _root_from_body(body, resolved_settings)
    root_path = Path(root).expanduser().resolve()
    catalog = MediaCatalog.load(resolved_settings.catalog_root, domain="", country="")
    prepared = _prepare_source_work_bindings(
        body,
        root=root_path,
        catalog=catalog,
        settings=resolved_settings,
    )
    plan_catalog = prepared["catalog"]
    if prepared["present"] and not plan_catalog.matching_family_for_root(root_path):
        discovery = discover_catalog_work_draft(
            root_path,
            catalog=catalog,
            settings=resolved_settings,
        )
        plan: dict[str, Any] = {
            "version": 4,
            "root": str(root_path),
            "catalog_root": str(catalog.catalog_root),
            "plan_id": "",
            "ready": False,
            "registration_required": True,
            "registration": discovery,
            "family_works": [],
            "assignments": [],
            "moves": [],
            "unresolved_files": [],
            "issues": list(prepared["issues"]),
            "summary": {
                "source_directory_count": 0,
                "assignment_count": 0,
                "target_directory_count": 0,
                "file_count": 0,
                "scanned_file_count": 0,
                "unresolved_file_count": 0,
                "bytes": 0,
                "issue_count": len(prepared["issues"]),
            },
            "source_work_bindings": list(prepared["rows"]),
            "legacy_shortcut_authoritative": False,
        }
        _refresh_source_binding_summary(plan)
        return plan

    plan = build_plan(
        root,
        catalog=plan_catalog,
        settings=resolved_settings,
        source_names=prepared["source_names"],
        target_overrides=_target_overrides_from_body(body),
        route_target_overrides=_route_target_overrides_from_body(body),
        file_work_overrides=_file_work_overrides_from_body(body),
        source_work_overrides=prepared["legacy_overrides"],
        source_catalog_work_overrides=prepared["catalog_overrides"],
        source_work_blockers=prepared["source_work_blockers"],
        source_press_overrides=_source_press_overrides_from_body(body),
    )
    if prepared["present"]:
        _merge_prepared_source_bindings(plan, prepared)
    return plan


def _refresh_source_binding_summary(plan: dict[str, Any]) -> None:
    rows = [
        row
        for row in plan.get("source_work_bindings") or []
        if isinstance(row, Mapping)
    ]
    state_counts = {
        state: sum(1 for row in rows if str(row.get("state") or "") == state)
        for state in sorted({str(row.get("state") or "") for row in rows})
        if state
    }
    plan["source_work_binding_summary"] = {
        "total_count": len(rows),
        "selected_count": sum(1 for row in rows if row.get("selected") is True),
        "state_counts": state_counts,
        "registration_required_count": sum(
            1 for row in rows if row.get("registration_required") is True
        ),
    }


def _attach_shortcut_diagnostics(plan: dict[str, Any]) -> None:
    plan["legacy_shortcut_authoritative"] = False
    diagnostics: list[dict[str, Any]] = []
    for row in plan.get("shortcuts") or []:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "")
        diagnostics.append(
            {
                "source_name": "",
                "work_name": str(row.get("work_name") or ""),
                "shortcut_path": str(row.get("shortcut_path") or ""),
                "target_path": str(row.get("target_path") or ""),
                "existing_target_path": str(row.get("existing_target_path") or ""),
                "state": status,
                "message": (
                    "旧快捷方式目标与数据库目标冲突；仅作诊断，不参与作品归属"
                    if status == "conflict"
                    else "快捷方式状态仅作诊断；作品归属只取数据库 catalog_ref 或本次人工选择"
                ),
            }
        )
    plan["shortcut_diagnostics"] = diagnostics


def _merge_prepared_source_bindings(
    plan: dict[str, Any],
    prepared: Mapping[str, Any],
) -> None:
    planned_rows = {
        path_key(Path(str(row.get("source_path") or ""))): row
        for row in plan.get("source_work_bindings") or []
        if isinstance(row, dict) and str(row.get("source_path") or "").strip()
    }
    for requested in prepared.get("rows") or []:
        if not isinstance(requested, Mapping):
            continue
        source_path = Path(str(requested["source_path"]))
        key = path_key(source_path)
        row = planned_rows.get(key)
        if row is None:
            row = {
                "source_name": requested["source_name"],
                "source_path": str(source_path),
                "selected": True,
                "can_override": True,
                "inferred_press": [],
                "issues": [],
            }
            planned_rows[key] = row
        service_issues = list(row.get("issues") or [])
        service_inferred_press = list(row.get("inferred_press") or [])
        requested_inferred_press = list(requested.get("inferred_press") or [])
        automatic_service_values = (
            {
                key: row.get(key)
                for key in (
                    "state",
                    "authority",
                    "suggested_work_name",
                    "suggestion_authority",
                    "catalog_ref",
                    "resolved_works",
                    "candidates",
                    "registration_required",
                    "next_action",
                )
            }
            if str(requested.get("requested_mode") or "") == "automatic"
            else None
        )
        row.update(dict(requested))
        if automatic_service_values is not None:
            row.update(automatic_service_values)
            row["requested_mode"] = "automatic"
        row["issues"] = service_issues
        if service_inferred_press:
            merged_inferred: list[dict[str, Any]] = []
            for inferred in service_inferred_press:
                merged = dict(inferred)
                requested_match = next(
                    (
                        item
                        for item in requested_inferred_press
                        if normalized_value(str(item.get("press_format") or ""))
                        == normalized_value(str(inferred.get("press_format") or ""))
                        and normalized_value(str(item.get("press_group") or ""))
                        == normalized_value(str(inferred.get("press_group") or ""))
                    ),
                    None,
                )
                if requested_match is not None and str(
                    requested_match.get("suggested_press_path") or ""
                ).strip():
                    merged["suggested_press_path"] = str(
                        requested_match.get("suggested_press_path") or ""
                    )
                merged_inferred.append(merged)
            row["inferred_press"] = merged_inferred
        else:
            row["inferred_press"] = requested_inferred_press

    extra_issues = [dict(issue) for issue in prepared.get("issues") or []]
    assignments_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for assignment in plan.get("assignments") or []:
        if not isinstance(assignment, Mapping):
            continue
        source_dir = str(assignment.get("source_dir") or "").strip()
        if source_dir:
            assignments_by_source.setdefault(path_key(Path(source_dir)), []).append(assignment)
    selected_works = prepared.get("selected_works_by_source") or {}
    for source_key, work in selected_works.items():
        if not isinstance(work, CatalogWork):
            continue
        row = planned_rows.get(str(source_key))
        if row is None:
            continue
        source_assignments = assignments_by_source.get(str(source_key), [])
        for inferred in row.get("inferred_press") or []:
            assignment_match = next(
                (
                    assignment
                    for assignment in source_assignments
                    if normalized_value(str(assignment.get("press_format") or ""))
                    == normalized_value(str(inferred.get("press_format") or ""))
                    and normalized_value(str(assignment.get("press_group") or ""))
                    == normalized_value(str(inferred.get("press_group") or ""))
                ),
                None,
            )
            if assignment_match is not None and not str(
                inferred.get("suggested_press_path") or ""
            ).strip():
                inferred["suggested_press_path"] = str(
                    assignment_match.get("target_relpath") or ""
                )
        missing_press = bool(row.get("catalog_press_required"))
        for assignment in source_assignments:
            press_format = str(assignment.get("press_format") or "")
            press_group = str(assignment.get("press_group") or "")
            matches = [
                press
                for press in work.presses
                if normalized_value(press.press_format) == normalized_value(press_format)
                and normalized_value(press.press_group) == normalized_value(press_group)
            ]
            if len(matches) != 1 or not matches[0].press_path.strip():
                missing_press = True
                break
        if not source_assignments:
            for inferred in row.get("inferred_press") or []:
                press_format = str(inferred.get("press_format") or "")
                press_group = str(inferred.get("press_group") or "")
                if not press_format:
                    continue
                same_format = [
                    press
                    for press in work.presses
                    if normalized_value(press.press_format) == normalized_value(press_format)
                ]
                exact = [
                    press
                    for press in same_format
                    if press_group
                    and normalized_value(press.press_group) == normalized_value(press_group)
                ]
                if (
                    not same_format
                    or (press_group and len(exact) != 1)
                    or (len(exact) == 1 and not exact[0].press_path.strip())
                ):
                    missing_press = True
                    break
        if missing_press:
            row["catalog_press_required"] = True
            row["catalog_repair_required"] = True
            row["registration_required"] = True
            row["next_action"] = "complete_manual"
            if not any(
                issue.get("code") == "source-work-catalog-press-repair-required"
                and str(issue.get("path") or "") == str(row.get("source_path") or "")
                for issue in extra_issues
            ):
                extra_issues.append(
                    {
                        "code": "source-work-catalog-press-repair-required",
                        "message": "所选数据库作品缺少当前来源对应的唯一压制记录或 press_path，必须先做完整落地预览",
                        "path": str(row.get("source_path") or ""),
                        "work_name": work.name,
                    }
                )
        if row.get("catalog_repair_required"):
            existing_presses = [
                {
                    "source_names": [],
                    "press_format": press.press_format,
                    "press_group": press.press_group,
                    "press_path": press.press_path,
                }
                for press in work.presses
            ]
            editable_presses: list[dict[str, Any]] = []
            for inferred in row.get("inferred_press") or []:
                press_format = str(inferred.get("press_format") or "").strip()
                press_group = str(inferred.get("press_group") or "").strip()
                if not press_format:
                    continue
                matches = [
                    press
                    for press in existing_presses
                    if normalized_value(press["press_format"]) == normalized_value(press_format)
                    and press_group
                    and normalized_value(press["press_group"]) == normalized_value(press_group)
                ]
                if len(matches) == 1:
                    editable_presses.append(
                        {
                            **dict(matches[0]),
                            "source_names": [str(row.get("source_name") or "")],
                            "press_path": str(matches[0]["press_path"] or "")
                            or str(inferred.get("suggested_press_path") or ""),
                        }
                    )
                    continue
                editable_presses.append(
                    {
                        "source_names": [str(row.get("source_name") or "")],
                        "press_format": press_format,
                        "press_group": press_group,
                        "press_path": str(inferred.get("suggested_press_path") or ""),
                    }
                )
            row["registration"] = {
                "state": "catalog_repair_required",
                "source_name": str(row.get("source_name") or ""),
                "catalog_ref": _catalog_work_ref(work, prepared["catalog"]),
                "existing_presses": existing_presses,
                "draft": {
                    "name": work.name,
                    "date": {"start": work.start_date, "end": work.end_date},
                    "domain": work.domain,
                    "country": work.country,
                    "release_type": work.release_type,
                    "path": str(plan.get("root") or ""),
                    "presses": editable_presses,
                },
                "preview_endpoint": "/api/media-directory-organizer/landing/preview",
            }
    plan.setdefault("issues", []).extend(extra_issues)
    plan["issues"].sort(
        key=lambda row: (
            str(row.get("path") or "").casefold(),
            str(row.get("code") or ""),
            str(row.get("message") or ""),
        )
    )
    plan["source_work_bindings"] = sorted(
        planned_rows.values(),
        key=lambda row: str(row.get("source_name") or "").casefold(),
    )
    _refresh_source_binding_summary(plan)
    plan["legacy_shortcut_authoritative"] = False
    if extra_issues:
        plan["ready"] = False
    summary = plan.setdefault("summary", {})
    summary["issue_count"] = len(plan["issues"])
    if plan.get("plan_id"):
        plan["plan_id"] = _stable_plan_id(plan)


def organizer_config_payload() -> dict[str, Any]:
    """Return JSON-safe organizer settings used by the local desktop page."""
    settings = load_organizer_settings()
    return {
        "ok": True,
        "version": 4,
        "capabilities": {
            "per_file_routing": True,
            "route_target_overrides": True,
            "file_work_overrides": True,
            "source_work_overrides": True,
            "source_work_bindings": True,
            "exact_catalog_ref_binding": True,
            "mixed_catalog_landing": True,
            "source_press_overrides": True,
            "catalog_work_onboarding": True,
            "catalog_work_suggestions": True,
            "bangumi_anime_search": True,
            "scoped_shortcut_creation": True,
            "scoped_shortcut_retry": True,
            "catalog_shortcut_repair": True,
        },
        "paths": {
            "catalog_root": str(settings.catalog_root),
            "allowed_resource_roots": [str(path) for path in settings.allowed_resource_roots],
            "default_work_root": str(settings.default_work_root or ""),
        },
        "detection": {
            "format_markers": {
                name: list(markers) for name, markers in settings.format_markers.items()
            },
            "group_markers": {
                name: list(markers) for name, markers in settings.group_markers.items()
            },
        },
        "naming": {"group_suffixes": dict(settings.group_suffixes)},
        "classification": {
            "work_aliases": {
                name: list(aliases) for name, aliases in settings.work_aliases.items()
            }
        },
        "safety": {"max_files": settings.max_files},
        "metadata": {
            "bangumi": {
                "enabled_for": {"domain": "animation", "country": "japan"},
                "manual_confirmation_required": True,
            }
        },
        "group_registry": group_registry_for_organizer(),
    }


def preview_organizer_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
) -> dict[str, Any]:
    """Build a fresh, non-persistent plan for browser review."""
    payload = _body_mapping(body)
    organizer = settings or load_organizer_settings()
    root = _root_from_body(payload, organizer)
    catalog = MediaCatalog.load(organizer.catalog_root, domain="", country="")
    has_source_bindings = bool(
        payload.get("source_work_bindings") or payload.get("source_work_overrides")
    )
    if not catalog.matching_family_for_root(root) and not has_source_bindings:
        discovery = discover_catalog_work_draft(
            root,
            catalog=catalog,
            settings=organizer,
        )
        plan = {
            "version": 4,
            "root": str(discovery["root"]),
            "plan_id": "",
            "ready": False,
            "registration_required": True,
            "registration": discovery,
            "family_works": [],
            "assignments": [],
            "moves": [],
            "unresolved_files": [],
            "source_work_bindings": [
                {
                    "source_name": str(source.get("name") or ""),
                    "source_path": str(source.get("path") or ""),
                    "selected": True,
                    "can_override": True,
                    "state": "unresolved",
                    "requested_mode": "automatic",
                    "requested_work_name": "",
                    "suggested_work_name": str(
                        source.get("suggested_work_name")
                        or source.get("work_title_hint")
                        or ""
                    ),
                    "suggestion_authority": (
                        "directory_press_suffix"
                        if str(source.get("suggested_work_name") or "").strip()
                        else ""
                    ),
                    "authority": "automatic",
                    "catalog_ref": None,
                    "resolved_works": [],
                    "candidates": [
                        {
                            **dict(candidate),
                            "work": dict(candidate.get("draft_work") or {}),
                        }
                        for candidate in discovery.get("existing_candidates") or []
                        if isinstance(candidate, Mapping)
                    ],
                    "inferred_press": [
                        {
                            "press_format": str(source.get("suggested_press_format") or ""),
                            "press_group": str(source.get("suggested_press_group") or ""),
                        }
                    ],
                    "registration_required": True,
                    "registration": _binding_registration_draft(
                        discovery,
                        source_name=str(source.get("name") or ""),
                        work_name=str(
                            source.get("suggested_work_name")
                            or source.get("work_title_hint")
                            or source.get("name")
                            or Path(root).name
                        ),
                    ),
                    "next_action": "search",
                    "legacy_shortcut_authoritative": False,
                    "issues": [],
                }
                for source in discovery.get("sources") or []
                if isinstance(source, Mapping)
            ],
            "legacy_shortcut_authoritative": False,
            "issues": [
                {
                    "code": "catalog-work-not-found",
                    "message": "作品数据库中没有与该目录对应的作品，请手填作品信息后生成完整落地预览",
                    "path": str(discovery["root"]),
                }
            ],
            "summary": {
                "source_directory_count": len(discovery["sources"]),
                "assignment_count": 0,
                "target_directory_count": 0,
                "file_count": 0,
                "unresolved_file_count": 0,
                "bytes": 0,
                "issue_count": 1,
            },
        }
        _refresh_source_binding_summary(plan)
        plan["repair_required"] = True
        plan["repair"] = shortcut_repair_descriptor_for_registration(discovery)
    else:
        plan = _build_from_ui_body(payload, settings=organizer)
        plan = preview_organizer_plan_shortcuts(
            plan,
            organizer_settings=organizer,
        )
        _attach_shortcut_diagnostics(plan)
        repair = shortcut_repair_descriptor_for_plan(
            plan,
            organizer_settings=organizer,
        )
        if repair is not None:
            plan["repair_required"] = True
            plan["repair"] = repair
    plan.setdefault("shortcut_diagnostics", [])
    plan["legacy_shortcut_authoritative"] = False
    return {"ok": True, "plan": plan}


def _resolved_landing_settings(
    settings: OrganizerSettings | None,
    browse_settings: JpTvBrowseSettings | None,
) -> tuple[OrganizerSettings, JpTvBrowseSettings]:
    organizer = settings or load_organizer_settings()
    if browse_settings is not None:
        return organizer, browse_settings
    detail, _path = get_resolved_browse_settings()
    return organizer, detail


def preview_organizer_landing_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    return preview_work_landing(
        _body_mapping(body),
        organizer_settings=organizer,
        browse_settings=detail,
    )


def suggest_organizer_landing_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    bangumi_searcher: Any | None = None,
) -> dict[str, Any]:
    """Return read-only local/Bangumi candidates for a missing catalog work.

    A suggestion only contains a proposed form value.  It deliberately cannot
    write the catalog, create shortcuts or move files; those operations remain
    behind the existing landing preview and confirmation flow.
    """

    organizer = settings or load_organizer_settings()
    return suggest_work_landing(
        _body_mapping(body),
        settings=organizer,
        bangumi_searcher=bangumi_searcher,
    )


def apply_organizer_landing_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    return apply_work_landing(
        _body_mapping(body),
        organizer_settings=organizer,
        browse_settings=detail,
    )


def preview_organizer_landing_shortcuts_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    return preview_work_landing_shortcut_retry(
        _body_mapping(body),
        organizer_settings=organizer,
        browse_settings=detail,
    )


def apply_organizer_landing_shortcuts_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    return apply_work_landing_shortcut_retry(
        _body_mapping(body),
        organizer_settings=organizer,
        browse_settings=detail,
    )


def preview_organizer_catalog_shortcut_repair_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    payload = _body_mapping(body)
    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    organizer_plan = (
        _build_from_ui_body(payload, settings=organizer)
        if payload.get("include_media_move") is True
        else None
    )
    return preview_catalog_shortcut_repair(
        payload,
        organizer_settings=organizer,
        browse_settings=detail,
        organizer_plan=organizer_plan,
    )


def apply_organizer_catalog_shortcut_repair_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    payload = _body_mapping(body)
    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    if payload.get("include_media_move") is not True:
        return apply_catalog_shortcut_repair(
            payload,
            organizer_settings=organizer,
            browse_settings=detail,
        )
    # The factory is invoked only after idempotent receipt replay has been
    # checked, while repair owns both its transaction locks. A repeated success
    # therefore does not try to rescan source folders that were already moved.
    return apply_catalog_shortcut_repair(
        payload,
        organizer_settings=organizer,
        browse_settings=detail,
        organizer_plan_factory=lambda: _build_from_ui_body(
            payload,
            settings=organizer,
        ),
    )


def apply_organizer_from_ui_body(
    body: Any,
    *,
    settings: OrganizerSettings | None = None,
    browse_settings: JpTvBrowseSettings | None = None,
) -> dict[str, Any]:
    """Rebuild and apply only an explicitly acknowledged, unchanged plan."""
    payload = _body_mapping(body)
    if payload.get("acknowledge_move") is not True:
        raise ValueError("执行移动前必须明确设置 acknowledge_move=true")
    if payload.get("acknowledge_shortcuts") is not True:
        raise ValueError("创建快捷方式前必须明确设置 acknowledge_shortcuts=true")

    reviewed_plan_id = payload.get("plan_id")
    confirmation = payload.get("confirmation")
    reviewed_shortcut_plan_id = payload.get("shortcut_plan_id")
    shortcut_confirmation = payload.get("shortcut_confirmation")
    if not isinstance(reviewed_plan_id, str) or not _PLAN_ID_RE.fullmatch(reviewed_plan_id):
        raise ValueError("plan_id 必须是预览返回的完整 16 位计划 ID")
    if not isinstance(confirmation, str) or not _PLAN_ID_RE.fullmatch(confirmation):
        raise ValueError("confirmation 必须完整输入预览返回的 16 位计划 ID")
    if confirmation != reviewed_plan_id:
        raise ValueError("confirmation 与 plan_id 不一致，拒绝执行")
    if not isinstance(reviewed_shortcut_plan_id, str) or not _PLAN_ID_RE.fullmatch(
        reviewed_shortcut_plan_id
    ):
        raise ValueError("shortcut_plan_id 必须是预览返回的完整 16 位快捷方式计划 ID")
    if not isinstance(shortcut_confirmation, str) or not _PLAN_ID_RE.fullmatch(
        shortcut_confirmation
    ):
        raise ValueError("shortcut_confirmation 必须完整输入预览返回的 16 位快捷方式计划 ID")
    if shortcut_confirmation != reviewed_shortcut_plan_id:
        raise ValueError("shortcut_confirmation 与 shortcut_plan_id 不一致，拒绝执行")

    organizer, detail = _resolved_landing_settings(settings, browse_settings)
    if detail.filesystem_root is None:
        raise ValueError("collection-detail 未配置可写数据库目录")
    if detail.filesystem_root.resolve() != organizer.catalog_root.resolve():
        raise ValueError("目录整理器与 collection-detail 使用的数据库目录不一致")

    with organizer_transaction_lock():
        # Deliberately ignore every client-supplied move/shortcut row. Both
        # plans are rebuilt from the catalog and filesystem immediately before
        # the first write.
        plan = _build_from_ui_body(payload, settings=organizer)
        plan = preview_organizer_plan_shortcuts(
            plan,
            organizer_settings=organizer,
        )
        current_plan_id = str(plan.get("plan_id") or "")
        if current_plan_id != reviewed_plan_id:
            raise ValueError(
                "媒体计划已发生变化，尚未移动任何文件；请重新预览并确认完整计划 ID："
                f"{current_plan_id}"
            )
        current_shortcut_plan_id = str(plan.get("shortcut_plan_id") or "")
        if current_shortcut_plan_id != reviewed_shortcut_plan_id:
            raise ValueError(
                "快捷方式计划已发生变化，尚未移动任何文件；请重新预览并确认完整计划 ID："
                f"{current_shortcut_plan_id}"
            )
        if not plan.get("ready"):
            raise ValueError("媒体或快捷方式计划存在未决问题，尚未移动任何文件")
        execution = apply_plan(plan, confirmation=confirmation)
        try:
            shortcut_result = apply_organizer_plan_shortcuts(
                plan["shortcuts"],
                organizer_settings=organizer,
                browse_settings=detail,
            )
        except Exception as exc:
            scope = plan.get("shortcut_scope") or {}
            return {
                "ok": False,
                "state": "shortcut_pending",
                "plan_id": current_plan_id,
                "shortcut_plan_id": current_shortcut_plan_id,
                "execution": execution,
                "shortcut_error": str(exc),
                "shortcut_retry": {
                    "preview_endpoint": "/api/media-directory-organizer/landing/shortcuts/preview",
                    "apply_endpoint": "/api/media-directory-organizer/landing/shortcuts/apply",
                    "root": scope.get("root") or plan["root"],
                    "work_refs": scope.get("work_refs") or [],
                },
            }
        return {
            "ok": True,
            "state": "complete",
            "plan_id": current_plan_id,
            "shortcut_plan_id": current_shortcut_plan_id,
            "execution": execution,
            "shortcuts": shortcut_result,
        }


__all__ = [
    "apply_organizer_catalog_shortcut_repair_from_ui_body",
    "apply_organizer_landing_from_ui_body",
    "apply_organizer_landing_shortcuts_from_ui_body",
    "apply_organizer_from_ui_body",
    "organizer_config_payload",
    "suggest_organizer_landing_from_ui_body",
    "preview_organizer_landing_from_ui_body",
    "preview_organizer_catalog_shortcut_repair_from_ui_body",
    "preview_organizer_landing_shortcuts_from_ui_body",
    "preview_organizer_from_ui_body",
]
