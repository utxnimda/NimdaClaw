"""Exact-record incremental shortcut service shared by indexes and organizers."""
from __future__ import annotations

from typing import Any

from collection_detail.link_index import generate_link_index_files_from_ui_body
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings


def preview_shortcuts(refs: list[dict[str, str]], *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    """Return the existing file_generation plan, never creating or replacing files."""
    return generate_link_index_files_from_ui_body({"incremental": True, "preview": True, "scope": refs}, settings=settings)["file_generation"]


def apply_shortcuts(refs: list[dict[str, str]], plan_id: str, *, settings: JpTvBrowseSettings) -> dict[str, Any]:
    """Use the confirmed DB-authoritative plan; conflicts are never overwritten."""
    return generate_link_index_files_from_ui_body({"incremental": True, "confirm_incremental": True,
        "scope": refs, "plan_id": plan_id}, settings=settings)["file_generation"]
