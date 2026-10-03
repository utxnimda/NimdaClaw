"""Read-only catalog identities and candidates used by landing workflows.

Record references retain file, row, press segment, and content-hash identity.
No catalog mutation or transaction coordination belongs in this module.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from collection_detail.payload import build_collectioned_ordered
from collection_detail.save import _file_sha256
from media_directory_organizer.catalog import (
    MediaCatalog,
    normalized_identity,
    normalized_press_group,
    normalized_value,
    path_key,
)
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.input_validation import parse_record_index
from work_catalog_yaml.jp_tv.validate import (
    entry_air_dates,
    entry_collection_type_data,
    entry_country_slug,
    entry_display_name,
    entry_domain_slug,
    entry_release_type_slug,
    load_jp_tv_entries_from_yaml,
)
from work_catalog_yaml.yaml_io import load_yaml_string


class CatalogReadSession:
    """Read each file once during one read-only landing phase.

    Parsed entries and their reference hashes come from exactly the same bytes.
    Sessions are local to a preview/normalization call and never survive a write
    or the preview-to-apply boundary. Full press records are built only on use.
    """

    def __init__(self) -> None:
        self._files: dict[Path, tuple[bytes, list[Any], str]] = {}
        self._records: dict[tuple[Path, int], dict[str, Any]] = {}

    def _file(self, source: Path) -> tuple[bytes, list[Any], str]:
        source = source.resolve()
        if source not in self._files:
            content = source.read_bytes()
            entries = load_jp_tv_entries_from_yaml(load_yaml_string(content.decode("utf-8")))
            self._files[source] = content, entries, _file_sha256(content)
        return self._files[source]

    def content(self, source: Path) -> bytes:
        return self._file(source)[0]

    def entries(self, source: Path) -> list[Any]:
        return self._file(source)[1]

    def record(self, source: Path, relative: str, index: int) -> dict[str, Any]:
        source = source.resolve()
        key = (source, index)
        if key not in self._records:
            _content, entries, digest = self._file(source)
            record = _catalog_entry_record(source, relative, index, entries[index])
            record["source_sha256"] = digest
            self._records[key] = record
        return self._records[key]

    def iter_entries(self, catalog_root: Path) -> Iterator[tuple[Path, str, int, Any]]:
        root = catalog_root.resolve()
        for candidate in sorted(root.glob("*.yaml"), key=lambda path: path.name.casefold()):
            source, relative = _catalog_source(str(candidate), catalog_root=root)
            for index, entry in enumerate(self.entries(source)):
                yield source, relative, index, entry


def _catalog_source(
    raw: Any,
    *,
    catalog_root: Path,
) -> tuple[Path, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("数据库作品缺少 catalog_file")
    source = Path(raw.strip()).expanduser()
    if not source.is_absolute():
        source = catalog_root.joinpath(*raw.strip().replace("\\", "/").split("/"))
    source = source.resolve()
    if (
        source.parent != catalog_root.resolve()
        or source.suffix.casefold() != ".yaml"
    ):
        raise ValueError(f"catalog_file 不在配置的数据库目录内：{source}")
    if not source.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{source}")
    return source, source.relative_to(catalog_root.resolve()).as_posix()


def _press_key_for_row(position: int, row: Mapping[str, Any]) -> str:
    segment = str(row.get("segment") or "main").strip() or "main"
    continuation = row.get("continuation_index")
    continuation_text = "" if continuation is None else str(continuation)
    return (
        f"{position}:{segment}:{continuation_text}:"
        f"{str(row.get('press_format') or '').strip()}:"
        f"{str(row.get('press_group') or '').strip()}"
    )


def _catalog_entry_record(
    source: Path,
    yaml_source_rel: str,
    index: int,
    entry: Any,
) -> dict[str, Any]:
    data = entry_collection_type_data(entry)
    begin_date, end_date = entry_air_dates(entry)
    ordered = build_collectioned_ordered(data)
    presses: list[dict[str, Any]] = []
    segment_positions: Counter[tuple[str, int | None]] = Counter()
    for position, row in enumerate(ordered):
        if not isinstance(row, Mapping):
            continue
        segment = str(row.get("segment") or "main").strip() or "main"
        continuation_index = row.get("continuation_index")
        segment_key = (segment, continuation_index if segment == "continuation" else None)
        segment_position = segment_positions[segment_key]
        segment_positions[segment_key] += 1
        presses.append(
            {
                "position": position,
                "press_key": _press_key_for_row(position, row),
                "segment": segment,
                "segment_position": segment_position,
                "continuation_index": continuation_index,
                "continuation_title": str(row.get("continuation_title") or "").strip(),
                "press_format": str(row.get("press_format") or "").strip(),
                "press_group": str(row.get("press_group") or "").strip(),
                "press_path": str(row.get("press_path") or "")
                .strip()
                .replace("\\", "/")
                .strip("/"),
            }
        )
    work = {
        "yaml_source_rel": yaml_source_rel,
        "index_in_file": index,
        "work_key": f"{yaml_source_rel}#{index}",
        "name": entry_display_name(entry).strip(),
        "path": str(data.get("path") or "").strip(),
        "domain": entry_domain_slug(entry).strip(),
        "country": entry_country_slug(entry).strip(),
        "release_type": entry_release_type_slug(entry).strip(),
        "markers": [
            str(marker).strip()
            for marker in (data.get("markers") or [])
            if isinstance(marker, str) and str(marker).strip()
        ],
        "date": {
            "start": str(begin_date or "").strip(),
            "end": str(end_date or "").strip(),
        },
    }
    return {
        "source": source,
        "work": work,
        "presses": presses,
    }


def _catalog_record_for_assignment(
    assignment: Mapping[str, Any],
    *,
    catalog_root: Path,
    cache: dict[tuple[str, str], dict[str, Any]],
    session: CatalogReadSession | None = None,
) -> dict[str, Any]:
    raw_ref = assignment.get("catalog_ref")
    if isinstance(raw_ref, Mapping):
        # The same row may arrive with different name/hash evidence. Never
        # bypass reference validation merely because its row number was seen.
        return _record_from_work_ref(raw_ref, catalog_root=catalog_root, session=session)
    source, yaml_source_rel = _catalog_source(
        assignment.get("catalog_file"),
        catalog_root=catalog_root,
    )
    work_name = str(assignment.get("work_name") or "").strip()
    key = (str(source), work_name)
    cached = cache.get(key)
    if cached is not None:
        return cached
    reader = session or CatalogReadSession()
    matches = [
        (index, entry)
        for index, entry in enumerate(reader.entries(source))
        if entry_display_name(entry).strip() == work_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"数据库文件中必须且只能有一个精确作品名 {work_name!r}，实际找到 {len(matches)} 个"
        )
    index, entry = matches[0]
    record = reader.record(source, yaml_source_rel, index)
    cache[key] = record
    return record


def _press_ref(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        "press_key": str(row.get("press_key") or ""),
        "press_format": str(row.get("press_format") or ""),
        "press_group": str(row.get("press_group") or ""),
        "press_path": str(row.get("press_path") or ""),
    }


def _work_ref(record: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    work = record["work"]
    return {
        "yaml_source_rel": str(work["yaml_source_rel"]),
        "index_in_file": int(work["index_in_file"]),
        "work_name": str(work["name"]),
        "work_path": str(work["path"]),
        "presses": [_press_ref(row) for row in rows],
    }


def _date_identity(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _press_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("press_format") or "").strip().casefold(),
        normalized_press_group(str(row.get("press_group") or "")),
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
        and Path(work.source_file).resolve()
        == Path(str(catalog_change["target"])).expanduser().resolve()
        and work.source_index == int(catalog_change.get("index_in_file", -1))
    ]
    if len(matches) != 1:
        raise ValueError(
            "shortcut retry requires exactly one existing database work matching name and path"
        )
    target = Path(str(catalog_change["target"])).expanduser().resolve()
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


def _record_from_work_ref(
    raw_ref: Mapping[str, Any],
    *,
    catalog_root: Path,
    session: CatalogReadSession | None = None,
) -> dict[str, Any]:
    source, yaml_source_rel = _catalog_source(
        raw_ref.get("yaml_source_rel"),
        catalog_root=catalog_root,
    )
    index = parse_record_index(raw_ref.get("index_in_file"), label="work_ref.index_in_file")
    reader = session or CatalogReadSession()
    entries = reader.entries(source)
    if index < 0 or index >= len(entries):
        raise ValueError(f"数据库作品索引已经变化：{yaml_source_rel}#{index}")
    record = reader.record(source, yaml_source_rel, index)
    expected_name = str(raw_ref.get("work_name") or "").strip()
    if not expected_name or record["work"]["name"] != expected_name:
        raise ValueError(f"数据库作品名已经变化：{yaml_source_rel}#{index}")
    expected_sha256 = str(raw_ref.get("source_sha256") or "").strip().casefold()
    if expected_sha256 and expected_sha256 != record["source_sha256"]:
        raise ValueError(f"数据库文件在候选生成后已经变化：{yaml_source_rel}")
    return record


def _all_catalog_records_for_root(
    *,
    catalog_root: Path,
    root: Path,
    session: CatalogReadSession | None = None,
) -> list[dict[str, Any]]:
    reader = session or CatalogReadSession()
    root_key = path_key(root)
    records: list[dict[str, Any]] = []
    for source, relative, index, entry in reader.iter_entries(catalog_root):
        if _entry_path_matches_root(entry, root_key):
            records.append(reader.record(source, relative, index))
    return records


def _entry_path_matches_root(entry: Any, root_key: str) -> bool:
    raw_path = str(entry_collection_type_data(entry).get("path") or "").strip()
    if not raw_path:
        return False
    try:
        return path_key(Path(raw_path).expanduser().resolve()) == root_key
    except OSError:
        return False


def _iso_repair_date(value: Any) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return raw


def _catalog_ref_for_record(record: Mapping[str, Any]) -> dict[str, Any]:
    work = record["work"]
    return {
        "yaml_source_rel": str(work["yaml_source_rel"]),
        "index_in_file": int(work["index_in_file"]),
        "work_name": str(work["name"]),
        "source_sha256": str(record.get("source_sha256") or "")
        or _file_sha256(Path(str(record["source"])).read_bytes()),
    }


def _repair_draft_for_record(
    record: Mapping[str, Any],
    *,
    root: Path,
    assignments: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    work = record["work"]
    assignment_paths: dict[tuple[str, str], str] = {}
    for assignment in assignments or []:
        pair = (
            normalized_value(str(assignment.get("press_format") or "")),
            normalized_press_group(str(assignment.get("press_group") or "")),
        )
        relpath = str(assignment.get("target_relpath") or "").strip().replace("\\", "/").strip("/")
        if pair[0] and relpath:
            assignment_paths[pair] = relpath

    presses: list[dict[str, Any]] = []
    known_pairs: set[tuple[str, str]] = set()
    record_pair_counts: Counter[tuple[str, str]] = Counter()
    for raw_row in record.get("presses") or []:
        if isinstance(raw_row, Mapping):
            record_pair_counts[
                (
                    normalized_value(str(raw_row.get("press_format") or "")),
                    normalized_press_group(str(raw_row.get("press_group") or "")),
                )
            ] += 1
    for row in record.get("presses") or []:
        if not isinstance(row, Mapping):
            continue
        press_format = str(row.get("press_format") or "").strip()
        press_group = str(row.get("press_group") or "").strip()
        if not press_format:
            continue
        pair = (normalized_value(press_format), normalized_press_group(press_group))
        known_pairs.add(pair)
        presses.append(
            {
                "press_key": str(row.get("press_key") or ""),
                "segment": str(row.get("segment") or "main"),
                "continuation_index": row.get("continuation_index"),
                "continuation_title": str(row.get("continuation_title") or ""),
                "press_format": press_format,
                "press_group": press_group,
                "press_path": (
                    assignment_paths[pair]
                    if record_pair_counts[pair] == 1 and pair in assignment_paths
                    else str(row.get("press_path") or "")
                    .strip()
                    .replace("\\", "/")
                    .strip("/")
                ),
            }
        )
    for pair, relpath in assignment_paths.items():
        if pair in known_pairs:
            continue
        sample = next(
            (
                assignment
                for assignment in assignments or []
                if normalized_value(str(assignment.get("press_format") or "")) == pair[0]
                and normalized_press_group(str(assignment.get("press_group") or "")) == pair[1]
            ),
            {},
        )
        presses.append(
            {
                "press_format": str(sample.get("press_format") or "").strip(),
                "press_group": str(sample.get("press_group") or "").strip(),
                "press_path": relpath,
            }
        )
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
        "presses": presses,
    }


def _catalog_records_matching_repair_draft(
    patch: Mapping[str, Any],
    *,
    catalog_root: Path,
    root: Path,
    session: CatalogReadSession | None = None,
) -> list[dict[str, Any]]:
    reader = session or CatalogReadSession()
    root_key = path_key(root)
    wanted_name = normalized_identity(str(patch.get("name") or ""))
    records: list[dict[str, Any]] = []
    for source, relative, index, entry in reader.iter_entries(catalog_root):
        existing_name = normalized_identity(entry_display_name(entry).strip())
        name_match = bool(
            wanted_name
            and existing_name
            and (
                existing_name == wanted_name
                or existing_name.startswith(wanted_name)
                or wanted_name.startswith(existing_name)
            )
        )
        if name_match or _entry_path_matches_root(entry, root_key):
            records.append(reader.record(source, relative, index))
    return records


def _public_repair_candidate(record: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    catalog_ref = _catalog_ref_for_record(record)
    members = [
        {
            "catalog_ref": dict(catalog_ref),
            "work_name": str(record["work"].get("name") or ""),
            "press_key": str(row.get("press_key") or ""),
            "press_format": str(row.get("press_format") or ""),
            "press_group": str(row.get("press_group") or ""),
            "press_path": str(row.get("press_path") or ""),
            "source_names": [],
        }
        for row in record.get("presses") or []
        if isinstance(row, Mapping)
        and str(row.get("press_key") or "").strip()
        and str(row.get("press_format") or "").strip()
    ]
    return {
        "catalog_ref": catalog_ref,
        "draft_work": _repair_draft_for_record(record, root=root),
        "shared_target_members": members,
    }


def _registration_existing_catalog_candidates(
    root: Path,
    *,
    sources: list[Mapping[str, Any]],
    catalog_root: Path,
    session: CatalogReadSession | None = None,
) -> list[dict[str, Any]]:
    """Offer prefix-related existing rows without claiming an automatic match.

    Missing ``path`` is common for split-cour records.  Those rows cannot anchor
    ``MediaCatalog.matching_family_for_root`` yet, so registration exposes exact
    immutable references for explicit multi-selection instead of suggesting a
    duplicate database work.
    """

    raw_hints = [root.name]
    for source in sources:
        for key in ("work_title_hint", "suggested_work_name"):
            value = str(source.get(key) or "").strip()
            if value:
                raw_hints.append(value)
    identities: set[str] = set()
    for raw in raw_hints:
        stripped = re.sub(
            r"(?:[\s_.-]+(?:bd(?:rip)?|dvd(?:rip)?|web-?dl|webrip|"
            r"1080p?|2160p?|720p?|4k))(?:\([^)]*\))?$",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        for value in (raw, stripped):
            identity = normalized_identity(value)
            if len(identity) >= 4:
                identities.add(identity)
    if not identities or not catalog_root.is_dir():
        return []

    candidates: list[dict[str, Any]] = []
    reader = session or CatalogReadSession()
    for source, relative, index, entry in reader.iter_entries(catalog_root):
        work_identity = normalized_identity(entry_display_name(entry).strip())
        if not work_identity or not any(
            work_identity == hint
            or work_identity.startswith(hint)
            or hint.startswith(work_identity)
            for hint in identities
        ):
            continue
        record = reader.record(source, relative, index)
        candidates.append(_public_repair_candidate(record, root=root))
    candidates.sort(
        key=lambda candidate: (
            str((candidate.get("draft_work") or {}).get("date", {}).get("start") or ""),
            str((candidate.get("draft_work") or {}).get("name") or "").casefold(),
        )
    )
    return candidates


def _record_name_matches_patch(record: Mapping[str, Any], patch: Mapping[str, Any]) -> bool:
    return normalized_identity(str(record["work"].get("name") or "")) == normalized_identity(
        str(patch.get("name") or "")
    )


def _record_path_matches_root(record: Mapping[str, Any], root: Path) -> bool:
    raw_path = str(record["work"].get("path") or "").strip()
    if not raw_path:
        return False
    try:
        return path_key(Path(raw_path).expanduser().resolve()) == path_key(root)
    except OSError:
        return False
