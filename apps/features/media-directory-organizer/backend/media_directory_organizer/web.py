"""Small synchronous facade shared by the Starlette API and desktop UI.

The browser never submits an executable move list.  Preview and apply both
rebuild a plan from the configured catalog, filesystem state and explicit
target overrides.  This keeps the destructive boundary in the backend while
leaving HTTP status/error mapping to the framework application.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from media_directory_organizer.catalog import MediaCatalog
from media_directory_organizer.landing import (
    apply_work_landing_shortcut_retry,
    apply_work_landing,
    discover_catalog_work_draft,
    group_registry_for_organizer,
    preview_work_landing_shortcut_retry,
    preview_work_landing,
)
from media_directory_organizer.service import apply_plan, build_plan
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
    catalog = MediaCatalog.load(resolved_settings.catalog_root, domain="", country="")
    return build_plan(
        root,
        catalog=catalog,
        settings=resolved_settings,
        source_names=_source_names_from_body(body),
        target_overrides=_target_overrides_from_body(body),
        route_target_overrides=_route_target_overrides_from_body(body),
        file_work_overrides=_file_work_overrides_from_body(body),
    )


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
            "catalog_work_onboarding": True,
            "catalog_work_suggestions": True,
            "bangumi_anime_search": True,
            "scoped_shortcut_creation": True,
            "scoped_shortcut_retry": True,
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


def preview_organizer_from_ui_body(body: Any) -> dict[str, Any]:
    """Build a fresh, non-persistent plan for browser review."""
    payload = _body_mapping(body)
    settings = load_organizer_settings()
    root = _root_from_body(payload, settings)
    catalog = MediaCatalog.load(settings.catalog_root, domain="", country="")
    if not catalog.matching_family_for_root(root):
        discovery = discover_catalog_work_draft(
            root,
            catalog=catalog,
            settings=settings,
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
    else:
        plan = _build_from_ui_body(payload, settings=settings)
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


def apply_organizer_from_ui_body(body: Any) -> dict[str, Any]:
    """Rebuild and apply only an explicitly acknowledged, unchanged plan."""
    payload = _body_mapping(body)
    if payload.get("acknowledge_move") is not True:
        raise ValueError("执行移动前必须明确设置 acknowledge_move=true")

    reviewed_plan_id = payload.get("plan_id")
    confirmation = payload.get("confirmation")
    if not isinstance(reviewed_plan_id, str) or not _PLAN_ID_RE.fullmatch(reviewed_plan_id):
        raise ValueError("plan_id 必须是预览返回的完整 16 位计划 ID")
    if not isinstance(confirmation, str) or not _PLAN_ID_RE.fullmatch(confirmation):
        raise ValueError("confirmation 必须完整输入预览返回的 16 位计划 ID")
    if confirmation != reviewed_plan_id:
        raise ValueError("confirmation 与 plan_id 不一致，拒绝执行")

    # Deliberately ignore any client-supplied assignments/moves/ready fields.
    # The filesystem and catalog are scanned again immediately before apply.
    plan = _build_from_ui_body(payload)
    current_plan_id = str(plan.get("plan_id") or "")
    if current_plan_id != reviewed_plan_id:
        raise ValueError(
            "计划已发生变化，尚未移动任何文件；请重新预览并确认完整计划 ID："
            f"{current_plan_id}"
        )
    execution = apply_plan(plan, confirmation=confirmation)
    return {"ok": True, "plan_id": current_plan_id, "execution": execution}


__all__ = [
    "apply_organizer_landing_from_ui_body",
    "apply_organizer_landing_shortcuts_from_ui_body",
    "apply_organizer_from_ui_body",
    "organizer_config_payload",
    "suggest_organizer_landing_from_ui_body",
    "preview_organizer_landing_from_ui_body",
    "preview_organizer_landing_shortcuts_from_ui_body",
    "preview_organizer_from_ui_body",
]
