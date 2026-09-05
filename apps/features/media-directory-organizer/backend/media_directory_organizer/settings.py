"""Configuration loader for the media-directory-organizer feature."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from work_catalog_yaml.layout import feature_config_path, feature_data_root, resolve_workspace_path
from work_catalog_yaml.yaml_io import load_yaml


_DEFAULT_FORMAT_MARKERS: dict[str, tuple[str, ...]] = {
    "BDRip": ("bdrip", "blu-ray", "bluray", "bd 1920x1080", "bd 1280x720"),
    "DVDRip": ("dvdrip", "dvd rip"),
    "1080p": ("1080p",),
    "720p": ("720p",),
    "2160p": ("2160p", "uhd"),
}

_DEFAULT_GROUP_MARKERS: dict[str, tuple[str, ...]] = {
    "VCB": ("vcb-studio", "vcb studio", "vcb"),
    "JSUM": ("jsum",),
    "MW": ("mawen1250", "mawen"),
    "CKCS": ("ckcs",),
    "CK": ("ck",),
}

_DEFAULT_GROUP_SUFFIXES: dict[str, str] = {
    "JSUM": "Jsum",
    "VCB": "VCB",
    "MW": "MW",
    "CKCS": "CKCS",
}


def _string(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _mapping_of_markers(raw: Any, fallback: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    out = dict(fallback)
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        name = str(key).strip()
        if not name or not isinstance(value, list):
            continue
        markers = tuple(str(item).strip() for item in value if str(item).strip())
        if markers:
            out[name] = markers
    return out


@dataclass(frozen=True)
class OrganizerSettings:
    catalog_root: Path
    allowed_resource_roots: tuple[Path, ...]
    format_markers: dict[str, tuple[str, ...]]
    group_markers: dict[str, tuple[str, ...]]
    group_suffixes: dict[str, str]
    max_files: int = 20000
    default_work_root: Path | None = None
    work_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)


def load_organizer_settings(config_path: str | Path | None = None) -> OrganizerSettings:
    path = Path(config_path).expanduser().resolve() if config_path else feature_config_path(
        "media-directory-organizer"
    )
    raw = load_yaml(path) if path.is_file() else {}
    if not isinstance(raw, dict):
        raw = {}
    paths = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    catalog_raw = _string(paths.get("catalog_root"))
    catalog_root = (
        resolve_workspace_path(catalog_raw)
        if catalog_raw
        else (feature_data_root("collection-detail") / "db").resolve()
    )

    allowed_raw = paths.get("allowed_resource_roots")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        detail_cfg = feature_config_path("collection-detail")
        detail_raw = load_yaml(detail_cfg) if detail_cfg.is_file() else {}
        detail_paths = detail_raw.get("paths") if isinstance(detail_raw, dict) else {}
        allowed_raw = detail_paths.get("resource_roots") if isinstance(detail_paths, dict) else []
    allowed = tuple(
        resolve_workspace_path(item.strip())
        for item in (allowed_raw if isinstance(allowed_raw, list) else [])
        if isinstance(item, str) and item.strip()
    )
    default_root_raw = _string(paths.get("default_work_root"))
    default_work_root = (
        resolve_workspace_path(default_root_raw)
        if default_root_raw
        else None
    )

    detection = raw.get("detection") if isinstance(raw.get("detection"), dict) else {}
    naming = raw.get("naming") if isinstance(raw.get("naming"), dict) else {}
    suffixes = dict(_DEFAULT_GROUP_SUFFIXES)
    custom_suffixes = naming.get("group_suffixes")
    if isinstance(custom_suffixes, dict):
        suffixes.update(
            {
                str(key).strip(): str(value).strip()
                for key, value in custom_suffixes.items()
                if str(key).strip() and str(value).strip()
            }
        )
    safety = raw.get("safety") if isinstance(raw.get("safety"), dict) else {}
    classification = (
        raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    )
    work_aliases = _mapping_of_markers(classification.get("work_aliases"), {})
    try:
        max_files = max(1, int(safety.get("max_files", 20000)))
    except (TypeError, ValueError):
        max_files = 20000
    return OrganizerSettings(
        catalog_root=catalog_root,
        allowed_resource_roots=allowed,
        format_markers=_mapping_of_markers(detection.get("format_markers"), _DEFAULT_FORMAT_MARKERS),
        group_markers=_mapping_of_markers(detection.get("group_markers"), _DEFAULT_GROUP_MARKERS),
        group_suffixes=suffixes,
        max_files=max_files,
        default_work_root=default_work_root,
        work_aliases=work_aliases,
    )
