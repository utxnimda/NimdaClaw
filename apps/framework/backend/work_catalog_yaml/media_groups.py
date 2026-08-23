"""Shared media release/translation group registry.

The historic collection catalog stores a single ``press_group`` code.  That
code can name a release group, a translation group, or an abbreviation for a
combination of up to four groups.  This module keeps that compatible storage
shape while exposing the normalized relationships to every feature.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from work_catalog_yaml.layout import feature_data_root, workspace_root
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml


_NOTE_RELATIVE_PATH = Path("data/source/Animation/Note.h")
_REGISTRY_RELATIVE_PATH = Path("db/groups.yaml")
_CODE_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_DEFINITION_RE = re.compile(r"^([A-Z0-9]+)(?:\s*-\s*(.*))?$")
_COMBINATION_RE = re.compile(r"^([A-Z0-9]+)\s*=\s*(.+)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_PLACEHOLDER_CODES = {"", "----"}
_MEMBER_CORRECTIONS = {
    "TGTDS": "TGTD",  # Note.h defines TGTD, but FLTG historically says TGTDS.
    "VCP": "VCB",  # Note.h defines VCB, but VCBE historically says VCP.
}
_CATALOG_RELEASE_GROUPS = {
    "JSUM": {
        "names": ["Jsum"],
        "aliases": ["JSUM"],
        "source": "collection-detail catalog",
    }
}


def default_media_group_note_path() -> Path:
    return (workspace_root() / _NOTE_RELATIVE_PATH).resolve()


def default_media_group_registry_path() -> Path:
    return (feature_data_root("media-group-registry") / _REGISTRY_RELATIVE_PATH).resolve()


def default_collection_catalog_root() -> Path:
    return (feature_data_root("collection-detail") / "db").resolve()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _append_unique(values: list[str], value: Any) -> None:
    text = _clean_text(value)
    if text and text not in values:
        values.append(text)


def _definition_row(code: str, kind: str) -> dict[str, Any]:
    return {
        "code": code,
        "kind": kind,
        "names": [],
        "aliases": [],
        "sources": [],
    }


def _parse_note(note_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    text = note_path.read_text(encoding="utf-8")
    definitions: dict[str, dict[str, Any]] = {}
    combinations: list[dict[str, Any]] = []
    in_definitions = False
    in_combinations = False
    release_section = False

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped == "//BD组代号":
            in_definitions = True
            continue
        if stripped.startswith("//顺序为"):
            in_definitions = False
            continue
        if stripped.startswith("//--观望"):
            in_combinations = False
            continue
        if not stripped.startswith("//"):
            continue
        content = stripped[2:].strip()
        combo_match = _COMBINATION_RE.fullmatch(content)
        if combo_match:
            in_combinations = True
            code = combo_match.group(1).strip().upper()
            raw_members = [part.strip().upper() for part in combo_match.group(2).split("+")]
            members = [_MEMBER_CORRECTIONS.get(member, member) for member in raw_members if member]
            corrections = [
                {"from": before, "to": after}
                for before, after in zip(raw_members, members)
                if before != after
            ]
            combinations.append(
                {
                    "code": code,
                    "kind": "combination",
                    "members": members,
                    "raw_members": raw_members,
                    "corrections": corrections,
                    "sources": [
                        {
                            "file": _NOTE_RELATIVE_PATH.as_posix(),
                            "line": line_number,
                            "raw": raw_line.strip(),
                        }
                    ],
                }
            )
            continue
        if in_combinations or not in_definitions or not content:
            continue

        main, _separator, alias = content.partition("//")
        definition_match = _DEFINITION_RE.fullmatch(main.strip())
        # HSUB is the one historic definition without a dash.  Keep accepting
        # that source form, but retain its exact line in provenance.
        if definition_match is None:
            loose = main.strip().split(None, 1)
            if loose and _CODE_RE.fullmatch(loose[0].upper()):
                definition_match = _DEFINITION_RE.fullmatch(
                    loose[0].upper() + (" - " + loose[1] if len(loose) > 1 else "")
                )
        if definition_match is None:
            continue
        code = definition_match.group(1).strip().upper()
        if code == "VCB":
            release_section = True
        kind = "release" if release_section else "translation"
        row = definitions.setdefault(code, _definition_row(code, kind))
        row["sources"].append(
            {
                "file": _NOTE_RELATIVE_PATH.as_posix(),
                "line": line_number,
                "raw": raw_line.strip(),
            }
        )
        name = _clean_text(definition_match.group(2))
        if name:
            _append_unique(row["names"], name)
        if alias:
            _append_unique(row["aliases"], alias)

    translation = [row for row in definitions.values() if row["kind"] == "translation"]
    release = [row for row in definitions.values() if row["kind"] == "release"]
    return translation, release, combinations


def _walk_press_group_codes(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        raw = value.get("press_group")
        if isinstance(raw, str):
            code = raw.strip().upper()
            if code not in _PLACEHOLDER_CODES:
                yield code
        for child in value.values():
            yield from _walk_press_group_codes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_press_group_codes(child)


def observed_catalog_group_codes(catalog_root: str | Path | None = None) -> tuple[str, ...]:
    root = Path(catalog_root or default_collection_catalog_root()).expanduser().resolve()
    codes: set[str] = set()
    if not root.is_dir():
        return ()
    for yaml_path in sorted(root.glob("*.yaml")):
        try:
            raw = load_yaml(yaml_path)
        except (OSError, ValueError):
            continue
        codes.update(_walk_press_group_codes(raw))
    return tuple(sorted(codes))


def _normalize_base_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("code") or ""))


def build_media_group_registry(
    *,
    note_path: str | Path | None = None,
    catalog_root: str | Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    note = Path(note_path or default_media_group_note_path()).expanduser().resolve()
    if not note.is_file():
        raise FileNotFoundError(f"media group note not found: {note}")
    translation, release, combinations = _parse_note(note)

    base_by_code = {row["code"]: row for row in (*translation, *release)}
    for code, extra in _CATALOG_RELEASE_GROUPS.items():
        row = base_by_code.get(code)
        if row is None:
            row = _definition_row(code, "release")
            release.append(row)
            base_by_code[code] = row
        for name in extra["names"]:
            _append_unique(row["names"], name)
        for alias in extra["aliases"]:
            _append_unique(row["aliases"], alias)
        row["sources"].append({"kind": extra["source"]})

    combination_by_code: dict[str, dict[str, Any]] = {}
    normalized_combinations: list[dict[str, Any]] = []
    for row in release:
        row["classifier_family"] = str(row["code"])
    for row in combinations:
        code = str(row["code"])
        if code in combination_by_code:
            raise ValueError(f"duplicate media group combination: {code}")
        members = [str(member) for member in row["members"]]
        if not 2 <= len(members) <= 4:
            raise ValueError(f"media group combination {code} must contain 2-4 members")
        release_members = [member for member in members if base_by_code.get(member, {}).get("kind") == "release"]
        translation_members = [
            member for member in members if base_by_code.get(member, {}).get("kind") == "translation"
        ]
        unknown_members = [member for member in members if member not in base_by_code]
        classifier_family = (
            "VCB"
            if "VCB" in release_members
            else "JSUM"
            if "JSUM" in release_members
            else release_members[0]
            if len(release_members) == 1
            else ""
        )
        normalized = {
            **row,
            "release_groups": release_members,
            "translation_groups": translation_members,
            "unknown_members": unknown_members,
            "classifier_family": classifier_family,
        }
        combination_by_code[code] = normalized
        normalized_combinations.append(normalized)

    observed = observed_catalog_group_codes(catalog_root)
    known_codes = set(base_by_code) | set(combination_by_code)
    legacy = [
        {
            "code": code,
            "kind": "catalog-only",
            "names": [],
            "aliases": [],
            "source": "collection-detail catalog",
            "classifier_family": "VCB" if code.startswith("VCB") else "",
        }
        for code in observed
        if code not in known_codes
    ]

    release = _normalize_base_rows(release)
    translation = _normalize_base_rows(translation)
    normalized_combinations.sort(key=lambda row: str(row["code"]))
    legacy.sort(key=lambda row: str(row["code"]))
    note_hash = hashlib.sha256(note.read_bytes()).hexdigest()
    moment = generated_at or datetime.now()
    all_codes = sorted(known_codes | {row["code"] for row in legacy})
    duplicate_definitions = [
        {
            "code": str(row["code"]),
            "reason": "Note.h contains multiple definitions; names and aliases were retained",
            "sources": [dict(source) for source in row.get("sources") or []],
        }
        for row in (*translation, *release)
        if len(row.get("sources") or []) > 1 and str(row["code"]) not in _CATALOG_RELEASE_GROUPS
    ]
    corrected_combinations = [
        {
            "code": str(row["code"]),
            "reason": "undefined historic member spelling was normalized",
            "corrections": [dict(correction) for correction in row.get("corrections") or []],
            "sources": [dict(source) for source in row.get("sources") or []],
        }
        for row in normalized_combinations
        if row.get("corrections")
    ]
    return {
        "version": 1,
        "generated_at": moment.isoformat(timespec="seconds"),
        "sources": {
            "note": {
                "path": _NOTE_RELATIVE_PATH.as_posix(),
                "sha256": note_hash,
            },
            "catalog_root": "data/features/collection-detail/db",
        },
        "rules": {
            "combination_member_limit": 4,
            "catalog_storage_field": "press_group",
            "member_corrections": dict(_MEMBER_CORRECTIONS),
        },
        "release_groups": release,
        "translation_groups": translation,
        "combinations": normalized_combinations,
        "catalog_only_groups": legacy,
        "review_required": [*duplicate_definitions, *corrected_combinations],
        "press_group_codes": all_codes,
        "statistics": {
            "release_group_count": len(release),
            "translation_group_count": len(translation),
            "combination_count": len(normalized_combinations),
            "catalog_only_group_count": len(legacy),
            "press_group_code_count": len(all_codes),
            "observed_catalog_code_count": len(observed),
        },
    }


def _validate_group_code(code: Any, *, label: str) -> str:
    value = _clean_text(code).upper()
    if not _CODE_RE.fullmatch(value):
        raise ValueError(f"{label} has invalid code: {code!r}")
    return value


def validate_media_group_registry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("media group registry must be a version 1 YAML object")
    seen: set[str] = set()
    atomic_codes: set[str] = set()
    combination_rows: list[dict[str, Any]] = []
    for section in ("release_groups", "translation_groups", "combinations", "catalog_only_groups"):
        rows = raw.get(section)
        if not isinstance(rows, list):
            raise ValueError(f"media group registry section must be a list: {section}")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{section}[{index}] must be an object")
            code = _validate_group_code(row.get("code"), label=f"{section}[{index}]")
            if code in seen:
                raise ValueError(f"duplicate media group code: {code}")
            seen.add(code)
            if section in {"release_groups", "translation_groups"}:
                atomic_codes.add(code)
            if section == "combinations":
                combination_rows.append(row)
                members = row.get("members")
                if not isinstance(members, list) or not 2 <= len(members) <= 4:
                    raise ValueError(f"combination {code} must contain 2-4 members")
                normalized_members = [
                    _validate_group_code(member, label=f"combination {code} member")
                    for member in members
                ]
                if len(normalized_members) != len(set(normalized_members)):
                    raise ValueError(f"combination {code} contains duplicate members")
    for row in combination_rows:
        code = _validate_group_code(row.get("code"), label="combination")
        members = [_validate_group_code(member, label=f"combination {code} member") for member in row["members"]]
        unknown = sorted(set(members) - atomic_codes)
        if unknown:
            raise ValueError(
                f"combination {code} references undefined or non-atomic members: {', '.join(unknown)}"
            )
        translation_groups = row.get("translation_groups")
        if not isinstance(translation_groups, list) or len(translation_groups) > 4:
            raise ValueError(f"combination {code} translation_groups must be a list of at most 4 codes")
        normalized_translations = {
            _validate_group_code(member, label=f"combination {code} translation group")
            for member in translation_groups
        }
        if not normalized_translations.issubset(set(members)):
            raise ValueError(f"combination {code} translation_groups must be members of the combination")
    codes = raw.get("press_group_codes")
    if not isinstance(codes, list):
        raise ValueError("press_group_codes must be a list")
    normalized_codes = [
        _validate_group_code(code, label=f"press_group_codes[{index}]")
        for index, code in enumerate(codes)
    ]
    if len(normalized_codes) != len(set(normalized_codes)):
        raise ValueError("press_group_codes contains duplicate codes")
    if set(normalized_codes) != seen:
        raise ValueError("press_group_codes does not match the registry sections")
    return raw


def load_media_group_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path or default_media_group_registry_path()).expanduser().resolve()
    if not registry_path.is_file():
        return validate_media_group_registry(build_media_group_registry())
    return validate_media_group_registry(load_yaml(registry_path))


def save_media_group_registry(
    *,
    path: str | Path | None = None,
    note_path: str | Path | None = None,
    catalog_root: str | Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    registry_path = Path(path or default_media_group_registry_path()).expanduser().resolve()
    payload = validate_media_group_registry(
        build_media_group_registry(note_path=note_path, catalog_root=catalog_root)
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_yaml_string(payload)
    temporary = registry_path.with_suffix(registry_path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(registry_path)
    return registry_path, payload


def media_group_registry_api_payload(path: str | Path | None = None) -> dict[str, Any]:
    raw = load_media_group_registry(path)
    options: list[dict[str, Any]] = []
    for section in ("release_groups", "translation_groups", "combinations", "catalog_only_groups"):
        for row in raw[section]:
            code = str(row["code"])
            names = [str(name) for name in row.get("names", []) if str(name).strip()]
            members = [str(member) for member in row.get("members", []) if str(member).strip()]
            label = (
                f"{code} = {' + '.join(members)}"
                if members
                else f"{code} · {' / '.join(names)}"
                if names
                else code
            )
            options.append(
                {
                    "code": code,
                    "label": label,
                    "kind": str(row.get("kind") or ""),
                    "members": members or [code],
                    "release_groups": list(row.get("release_groups") or ([code] if section == "release_groups" else [])),
                    "translation_groups": list(
                        row.get("translation_groups") or ([code] if section == "translation_groups" else [])
                    ),
                    "classifier_family": str(row.get("classifier_family") or ("VCB" if code == "VCB" else "")),
                }
            )
    kind_order = {"release": 0, "combination": 1, "translation": 2, "catalog-only": 3}
    options.sort(key=lambda item: (kind_order.get(str(item["kind"]), 9), str(item["code"])))
    return {
        **raw,
        "registry_path": str(Path(path or default_media_group_registry_path()).expanduser().resolve()),
        "options": options,
    }


def media_group_code_known(code: str, path: str | Path | None = None) -> bool:
    wanted = _clean_text(code).upper()
    return wanted in {str(item).upper() for item in load_media_group_registry(path)["press_group_codes"]}


def media_group_classifier_family(code: str, path: str | Path | None = None) -> str:
    wanted = _clean_text(code).upper()
    if wanted.startswith("VCB"):
        return "VCB"
    raw = load_media_group_registry(path)
    for section in ("release_groups", "combinations", "catalog_only_groups"):
        for row in raw[section]:
            if str(row.get("code") or "").upper() != wanted:
                continue
            if wanted == "VCB":
                return "VCB"
            return _clean_text(row.get("classifier_family")).upper()
    return ""


__all__ = [
    "build_media_group_registry",
    "default_collection_catalog_root",
    "default_media_group_note_path",
    "default_media_group_registry_path",
    "load_media_group_registry",
    "media_group_classifier_family",
    "media_group_code_known",
    "media_group_registry_api_payload",
    "observed_catalog_group_codes",
    "save_media_group_registry",
    "validate_media_group_registry",
]
