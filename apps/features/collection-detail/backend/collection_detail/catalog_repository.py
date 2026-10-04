"""Authoritative collection catalog access, independent of page and disk views.

Only catalog YAML documents provide work and collection records. Filesystem
observations and shortcut indexes are derived projections, never replacement DBs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Any, Iterable, Iterator, TypedDict

from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.validate import (
    TV_JP_PRESS_FORMAT_KEY, TV_JP_PRESS_GROUP_KEY, TV_JP_PRESS_PATH_KEY, JpTvEntry,
    entry_air_dates, entry_collection_type_data, entry_country_slug,
    entry_display_name, entry_domain_slug, entry_release_type_slug,
    jp_tv_press_pair_from_row, load_jp_tv_entries_from_yaml,
    strip_tv_jp_tag_decoration,
)
from work_catalog_yaml.operation_progress import report_progress
from work_catalog_yaml.paths import normalize_copied_path
from work_catalog_yaml.yaml_io import load_yaml_string


@dataclass(frozen=True)
class CatalogSource:
    path: Path
    relative_path: str
    source_sha256: str
    data: bytes


@dataclass(frozen=True)
class CatalogDocument:
    path: Path
    relative_path: str
    source_sha256: str
    document: Any
    entries: list[JpTvEntry]


class CatalogPress(TypedDict):
    press_key: str
    press_format: str
    press_group: str
    press_path: str
    label: str
    segment: str
    continuation_index: int | None
    continuation_title: str


class CatalogWork(TypedDict):
    work_key: str
    yaml_source_rel: str
    index_in_file: int
    name: str
    path: str
    year: str
    year_label: str
    begin_date: str
    end_date: str
    date_range_label: str
    domain: str
    domain_label: str
    country: str
    country_label: str
    release_type: str
    release_type_label: str
    press: list[CatalogPress]


def _str_or_blank(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _compact_shortcut_date(raw: Any) -> str:
    value = _str_or_blank(raw)
    if re.fullmatch(r"\d{8}", value):
        return value
    matched = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if matched is not None:
        return "".join(matched.groups())
    return value


def _shortcut_date_range_label(begin_date: str, end_date: str) -> str:
    if begin_date and end_date:
        return f"[{begin_date}][{end_date}]"
    return f"[{begin_date or end_date}]" if begin_date or end_date else ""


def _year_from_entry(entry: Any, yaml_rel: str) -> str:
    try:
        start, _end = entry_air_dates(entry)
    except ValueError:
        start = ""
    m = re.search(r"(19|20)\d{2}", str(start))
    if m:
        return m.group(0)
    m = re.search(r"\[(?:JP|CN|US)?[^\]]*\]\[.*?\]\[((?:19|20)\d{2})\]", yaml_rel)
    if m:
        return m.group(1)
    m = re.search(r"(19|20)\d{2}", yaml_rel)
    return m.group(0) if m else ""


def _air_date_parts(entry: Any) -> tuple[str, str]:
    try:
        start, end = entry_air_dates(entry)
    except ValueError:
        return "", ""
    return _compact_shortcut_date(start), _compact_shortcut_date(end)


def _enum_display(settings: JpTvBrowseSettings, enum_key: str, raw: str) -> str:
    labels = settings.enum_labels.get(enum_key, {})
    return labels.get(raw, raw)


def _work_key(yaml_rel: str, index_in_file: int) -> str:
    return f"{yaml_rel}#{int(index_in_file)}"


def _press_key(position: int, row: dict[str, Any]) -> str:
    fm = _str_or_blank(row.get(TV_JP_PRESS_FORMAT_KEY))
    gp = _str_or_blank(row.get(TV_JP_PRESS_GROUP_KEY))
    seg = _str_or_blank(row.get("segment")) or "main"
    cont = row.get("continuation_index")
    cont_s = "" if cont is None else str(cont)
    return f"{position}:{seg}:{cont_s}:{fm}:{gp}"


def _press_key_position(press_key: str) -> int | None:
    head = str(press_key or "").split(":", 1)[0]
    try:
        pos = int(head)
    except (TypeError, ValueError):
        return None
    return pos if pos >= 0 else None


def _press_label(row: dict[str, Any]) -> str:
    fm = _str_or_blank(row.get(TV_JP_PRESS_FORMAT_KEY))
    gp = _str_or_blank(row.get(TV_JP_PRESS_GROUP_KEY))
    if fm and gp:
        return f"{fm}-{gp}"
    return fm or gp or "press"


def _catalog_relpath(settings: JpTvBrowseSettings, yaml_abs: Path) -> str:
    if settings.filesystem_root is None:
        return yaml_abs.name
    try:
        return yaml_abs.resolve().relative_to(settings.filesystem_root.resolve()).as_posix()
    except ValueError:
        return yaml_abs.name


def _catalog_yaml_paths(settings: JpTvBrowseSettings) -> list[Path]:
    if settings.filesystem_root is not None:
        root = settings.filesystem_root.resolve()
        if root.is_dir():
            paths = [
                p.resolve()
                for p in sorted(root.glob("*.yaml"))
                if p.is_file() and not p.name.startswith(".") and p.name != "link-index.yaml"
            ]
            # Explicit nested sources are valid DB files, not basename fallbacks.
            # Refresh top-level years too: a save can add one after settings loaded.
            for raw in settings.resolved_catalog_yaml_paths:
                path = Path(raw).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    continue
                if path.is_file() and path.suffix == ".yaml" and not path.name.startswith(".") and path.name != "link-index.yaml":
                    paths.append(path)
            return sorted(set(paths))
    return [Path(abs_s).resolve() for abs_s in settings.resolved_catalog_yaml_paths]


def _sanitize_collection_rows(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, str]] = []
    for row in raw:
        if isinstance(row, dict):
            pr = jp_tv_press_pair_from_row(row)
            if pr:
                row_out = {
                    TV_JP_PRESS_FORMAT_KEY: strip_tv_jp_tag_decoration(pr[0]),
                    TV_JP_PRESS_GROUP_KEY: strip_tv_jp_tag_decoration(pr[1]),
                }
                if isinstance(row.get(TV_JP_PRESS_PATH_KEY), str) and row[TV_JP_PRESS_PATH_KEY].strip():
                    row_out[TV_JP_PRESS_PATH_KEY] = row[TV_JP_PRESS_PATH_KEY].strip().replace("\\", "/")
                rows.append(row_out)
    return rows


def ordered_press_rows(coll: dict[str, Any]) -> list[dict[str, Any]]:
    """主行 collectioned 在前，其后按顺序追加每条续行的 collectioned；每项带 segment 元数据便于前端贴标签区分。"""
    seq: list[dict[str, Any]] = []
    for row in _sanitize_collection_rows(coll.get("collectioned")):
        seq.append({**row, "segment": "main", "continuation_index": None, "continuation_title": None})

    raw = coll.get("continuations")
    if isinstance(raw, list):
        for bi, blk in enumerate(raw):
            if not isinstance(blk, dict):
                continue
            title = (
                str(blk["title"]).strip()
                if isinstance(blk.get("title"), str) and str(blk["title"]).strip()
                else None
            )
            rows = _sanitize_collection_rows(blk.get("collectioned"))
            if not rows and not title:
                continue
            for row in rows:
                seq.append(
                    {
                        **row,
                        "segment": "continuation",
                        "continuation_index": bi,
                        "continuation_title": title,
                    },
                )

    return seq



def _load_catalog_works(
    settings: JpTvBrowseSettings,
    *,
    catalog_overrides: dict[Path, bytes] | None = None,
    catalog_paths: list[Path] | None = None,
) -> list[dict[str, Any]]:
    works_out: list[dict[str, Any]] = []
    catalog_paths = _catalog_yaml_paths(settings) if catalog_paths is None else catalog_paths
    for file_index, fp in enumerate(catalog_paths):
        report_progress("读取作品数据库", completed=file_index, total=len(catalog_paths), unit="数据文件", detail=str(fp))
        if not fp.is_file():
            continue
        yaml_rel = _catalog_relpath(settings, fp)
        raw_text = (catalog_overrides[fp].decode("utf-8")
                    if catalog_overrides is not None and fp in catalog_overrides
                    else fp.read_text(encoding="utf-8"))
        entries = load_jp_tv_entries_from_yaml(load_yaml_string(raw_text))
        for idx, entry in enumerate(entries):
            name = entry_display_name(entry)
            domain = entry_domain_slug(entry)
            country = entry_country_slug(entry)
            release_type = entry_release_type_slug(entry)
            year = _year_from_entry(entry, yaml_rel)
            begin_date, end_date = _air_date_parts(entry)
            coll = entry_collection_type_data(entry)
            work_path = normalize_copied_path(coll.get("path")).replace("\\", "/")
            press_rows: list[dict[str, Any]] = []
            for pos, row in enumerate(ordered_press_rows(coll)):
                fm = _str_or_blank(row.get(TV_JP_PRESS_FORMAT_KEY))
                gp = _str_or_blank(row.get(TV_JP_PRESS_GROUP_KEY))
                if not fm and not gp:
                    continue
                press_rows.append(
                    {
                        "press_key": _press_key(pos, row),
                        "press_format": fm,
                        "press_group": gp,
                        "press_path": normalize_copied_path(row.get(TV_JP_PRESS_PATH_KEY)).replace("\\", "/"),
                        "label": _press_label(row),
                        "segment": row.get("segment") or "main",
                        "continuation_index": row.get("continuation_index"),
                        "continuation_title": row.get("continuation_title") or "",
                    },
                )
            works_out.append(
                {
                    "work_key": _work_key(yaml_rel, idx),
                    "yaml_source_rel": yaml_rel,
                    "index_in_file": idx,
                    "name": name,
                    "path": work_path,
                    "year": year,
                    "year_label": f"[{year}]" if year else "",
                    "begin_date": begin_date,
                    "end_date": end_date,
                    "date_range_label": _shortcut_date_range_label(begin_date, end_date),
                    "domain": domain,
                    "domain_label": _enum_display(settings, "domain", domain),
                    "country": country,
                    "country_label": _enum_display(settings, "country", country),
                    "release_type": release_type,
                    "release_type_label": _enum_display(settings, "release_type", release_type),
                    "press": press_rows,
                },
            )
    report_progress("作品数据库读取完成", completed=len(catalog_paths), total=len(catalog_paths), unit="数据文件", detail=f"共 {len(works_out)} 条作品记录")
    return works_out


def clean_relative_catalog_path(raw: Any, *, label: str) -> str:
    value = normalize_copied_path(raw).replace("\\", "/")
    if not value:
        return ""
    if value.startswith("/") or PureWindowsPath(value).drive:
        raise ValueError(f"{label} 必须为相对路径")
    value = re.sub(r"/+", "/", value).rstrip("/")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{label} 包含非法路径片段")
    return value


def resolve_catalog_directory(
    media_root: Path,
    work_path: str,
    press_path: str | None = None,
    *,
    resolve_links: bool = True,
) -> Path:
    """Resolve stored paths, never guess a directory from work or release names."""
    value = normalize_copied_path(work_path)
    if not value:
        raise ValueError("path 不能为空")
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError("path 包含非法路径片段")

    def absolute(path: Path) -> Path:
        path = path.expanduser()
        return path.resolve() if resolve_links else Path(os.path.abspath(path))

    if value.startswith(("/", "\\")) or PureWindowsPath(value).is_absolute():
        work_dir = absolute(Path(value))
    else:
        root = absolute(media_root)
        work_dir = absolute(root / clean_relative_catalog_path(value, label="path"))
        work_dir.relative_to(root)
    if press_path is None:
        return work_dir
    relative = clean_relative_catalog_path(press_path, label="press_path")
    if not relative:
        raise ValueError("press_path 不能为空")
    target = absolute(work_dir / relative)
    target.relative_to(work_dir)
    return target


class CatalogRepository:
    """Read current saved works; never infer or write bindings from disk caches."""

    def __init__(self, settings: JpTvBrowseSettings) -> None:
        self.settings = settings
        self._read_paths: tuple[Path, ...] | None = None

    def catalog_paths(self) -> list[Path]:
        # Snapshot membership for one read phase, never cache mutable DB bytes.
        # Create a new repository for the next request or after a catalog write.
        if self._read_paths is None:
            self._read_paths = tuple(_catalog_yaml_paths(self.settings))
        return list(self._read_paths)

    def relative_path(self, path: Path) -> str:
        return _catalog_relpath(self.settings, path)

    def read_source(self, path: Path) -> CatalogSource:
        candidate = path.resolve()
        if candidate not in self.catalog_paths():
            raise ValueError("作品文件不在当前 DB 数据列表中")
        data = candidate.read_bytes()
        return CatalogSource(candidate, self.relative_path(candidate), hashlib.sha256(data).hexdigest(), data)

    def read_document(self, path: Path) -> CatalogDocument:
        source = self.read_source(path)
        document = load_yaml_string(source.data.decode("utf-8"))
        return CatalogDocument(source.path, source.relative_path, source.source_sha256,
                               document, load_jp_tv_entries_from_yaml(document))

    def read_documents(self, paths: Iterable[Path] | None = None) -> Iterator[CatalogDocument]:
        for path in self.catalog_paths() if paths is None else paths:
            yield self.read_document(path)

    def load_works(self, *, catalog_overrides: dict[Path, bytes] | None = None) -> list[dict[str, Any]]:
        return _load_catalog_works(self.settings, catalog_overrides=catalog_overrides, catalog_paths=self.catalog_paths())


# Stable domain helpers reused by editable and read-only projections.
work_key = _work_key
press_key = _press_key
press_key_position = _press_key_position
press_label = _press_label
compact_shortcut_date = _compact_shortcut_date
shortcut_date_range_label = _shortcut_date_range_label
sanitize_collection_rows = _sanitize_collection_rows
