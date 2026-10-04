"""从浏览页写回 JP TV 数据 YAML，并在 ``History`` 目录保留带时间戳的备份。"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import shutil
import unicodedata
from uuid import uuid4
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, cast
from io import StringIO

from ruamel.yaml import YAML

from work_catalog_yaml.jp_tv.browse_settings import (
    JpTvBrowseSettings,
    resolve_safe_yaml_under_root,
)
from work_catalog_yaml.jp_tv.dates import normalize_air_date
from work_catalog_yaml.jp_tv.validate import (
    TV_JP_DOMAIN_KEY,
    TV_JP_PRESS_FORMAT_KEY,
    TV_JP_PRESS_GROUP_KEY,
    TV_JP_PRESS_PATH_KEY,
    TV_JP_RELEASE_TYPE_KEY,
    entry_air_dates,
    entry_collection_type_data,
    entry_country_slug,
    entry_display_name,
    entry_domain_slug,
    entry_release_type_slug,
    load_jp_tv_entries_from_yaml,
)
from work_catalog_yaml.layout import feature_data_root
from work_catalog_yaml.input_validation import parse_record_index
from work_catalog_yaml.operation_progress import operation_context, report_progress
from work_catalog_yaml.paths import normalize_copied_path
from work_catalog_yaml.persistence import (
    FileWrite,
    atomic_write_bytes as _atomic_write_bytes,
    commit_file_writes,
    directory_write_transaction,
    history_snapshot_name,
)
from work_catalog_yaml.media_groups import (
    media_group_code_known,
    normalize_press_group,
    normalized_press_group,
)
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


@contextmanager
def catalog_write_transaction(
    filesystem_root: Path,
    *,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Serialize catalog writes across threads and local app processes."""

    with directory_write_transaction(filesystem_root, timeout_seconds=timeout_seconds):
        yield


def _assert_save_target_allowed(target: Path, settings: JpTvBrowseSettings) -> None:
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，禁止写盘")
    root = settings.filesystem_root.resolve()
    target_r = target.resolve()
    target_r.relative_to(root)
    if not target_r.is_file():
        raise ValueError("目标文件不存在或不是普通文件")


def history_catalog_root(settings: JpTvBrowseSettings) -> Path:
    """保存前备份目录：collection-detail feature data 下的 ``history``。"""
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root")
    db = settings.filesystem_root.resolve()
    feature_db = (feature_data_root("collection-detail") / "db").resolve()
    try:
        db.relative_to(feature_db)
        return (feature_data_root("collection-detail") / "history").resolve()
    except ValueError:
        return (db.parent / "History").resolve()


def current_year_catalog_relpath(*, now: datetime | None = None) -> str:
    """Compatibility default for a Japanese work with no known broadcast year."""
    dt = now or datetime.now()
    return f"[JP][TVInfo][{dt:%Y}].yaml"


def _works_list_mut(doc: Any) -> list[Any]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for k in ("works", "entries"):
            wl = doc.get(k)
            if isinstance(wl, list):
                return cast(list[Any], wl)
    raise ValueError("YAML 根须为作品数组或含 works / entries 的对象")


def _find_attr_list(work: Any) -> list[Any] | None:
    if not isinstance(work, dict):
        return None
    attrs = work.get("attributes")
    return cast(list[Any], attrs) if isinstance(attrs, list) else None


def _set_scalar_attr(work: dict[str, Any], typ: str, data: Any) -> None:
    attrs = _find_attr_list(work)
    if attrs is None:
        raise ValueError("作品缺少 attributes")
    for i, a in enumerate(attrs):
        if isinstance(a, dict) and a.get("type") == typ:
            na = dict(a)
            na["data"] = data
            attrs[i] = na
            return
    attrs.append({"type": typ, "data": data})


def _clean_rel_path(raw: Any) -> str:
    return normalize_copied_path(raw).replace("\\", "/").strip("/")


def _collection_ordered_to_coll_data(
    ordered: Any,
    *,
    domain: str,
    release_type: str,
    path: Any,
    markers: Any,
) -> dict[str, Any]:
    domain_s = domain.strip()
    release_type_s = release_type.strip()
    if not domain_s:
        raise ValueError("domain 不能为空")
    if not release_type_s:
        raise ValueError("release_type 不能为空")

    mk: list[str] = []
    raw_mk = markers if isinstance(markers, list) else []
    for x in raw_mk:
        if isinstance(x, str):
            s = x.strip()
            if s:
                mk.append(s)

    if not isinstance(ordered, list):
        ordered = []

    main: list[dict[str, str]] = []
    cont_map: dict[int, list[dict[str, str]]] = {}
    titles: dict[int, str] = {}

    for it in ordered:
        if not isinstance(it, dict):
            continue
        fm_raw = it.get(TV_JP_PRESS_FORMAT_KEY)
        gp_raw = it.get(TV_JP_PRESS_GROUP_KEY)
        fm = fm_raw.strip() if isinstance(fm_raw, str) else ""
        gp = gp_raw.strip() if isinstance(gp_raw, str) else ""
        if not fm and not gp:
            continue
        pair = {
            TV_JP_PRESS_FORMAT_KEY: fm or "",
            TV_JP_PRESS_GROUP_KEY: gp or "",
        }
        press_path = _clean_rel_path(it.get(TV_JP_PRESS_PATH_KEY))
        if press_path:
            pair[TV_JP_PRESS_PATH_KEY] = press_path
        seg = it.get("segment")
        if seg == "continuation":
            try:
                bi = int(it.get("continuation_index"))
            except (TypeError, ValueError):
                bi = 0
            cont_map.setdefault(bi, []).append(pair)
            ttl = it.get("continuation_title")
            if isinstance(ttl, str) and ttl.strip():
                titles.setdefault(bi, ttl.strip())
        else:
            main.append(pair)

    out: dict[str, Any] = {
        TV_JP_DOMAIN_KEY: domain_s,
        TV_JP_RELEASE_TYPE_KEY: release_type_s,
    }
    # The work root may be absolute (including UNC); do not strip its root.
    path_s = normalize_copied_path(path).replace("\\", "/")
    if path_s:
        out["path"] = path_s
    out["collectioned"] = main
    out["markers"] = mk
    if cont_map:
        seq: list[dict[str, Any]] = []
        for bi in sorted(cont_map.keys()):
            blk: dict[str, Any] = {"collectioned": cont_map[bi]}
            if bi in titles:
                blk["title"] = titles[bi]
            seq.append(blk)
        out["continuations"] = seq
    return out


def _row_ref_from_body_item(raw: Any, *, label: str) -> tuple[str, int]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} 须为对象")
    ysr = raw.get("yaml_source_rel")
    rel_s = ysr.strip().replace("\\", "/") if isinstance(ysr, str) else ""
    if not rel_s:
        raise ValueError(f"{label}.yaml_source_rel 须为非空字符串")
    ii = parse_record_index(raw.get("index_in_file"), label=f"{label}.index_in_file")
    return rel_s, ii


def _new_work_from_row_patch(patch: dict[str, Any]) -> dict[str, Any]:
    if "raw_record" in patch:
        record = normalize_raw_record(patch["raw_record"])
        record.setdefault("id", "work_" + uuid4().hex)
        record.setdefault("schema_version", 1)
        return record
    date_patch = patch.get("date") or {}
    start = date_patch.get("start") if isinstance(date_patch, dict) else ""
    end = date_patch.get("end") if isinstance(date_patch, dict) else ""
    domain_raw = patch.get(TV_JP_DOMAIN_KEY)
    release_raw = patch.get(TV_JP_RELEASE_TYPE_KEY)
    country_raw = patch.get("country")
    name_raw = patch.get("name")

    if not isinstance(domain_raw, str) or not domain_raw.strip():
        raise ValueError("新增行 domain 不能为空")
    if not isinstance(release_raw, str) or not release_raw.strip():
        raise ValueError("新增行 release_type 不能为空")
    country_s = country_raw.strip() if isinstance(country_raw, str) and country_raw.strip() else "japan"
    name_s = name_raw if isinstance(name_raw, str) else ""

    coll_data = _collection_ordered_to_coll_data(
        patch.get("collectioned_ordered"),
        domain=domain_raw,
        release_type=release_raw,
        path=patch.get("path"),
        markers=patch.get("markers"),
    )
    return {
        "id": "work_" + uuid4().hex,
        "schema_version": 1,
        "attributes": [
            {
                "type": "date",
                "data": {
                    "start": normalize_air_date(start, validate_calendar=True),
                    "end": normalize_air_date(end, validate_calendar=True),
                },
            },
            {
                "type": "collection-type",
                "data": coll_data,
            },
            {
                "type": "country",
                "data": country_s,
            },
            {
                "type": "name",
                "data": name_s,
            },
        ],
    }


def _apply_row_patch_to_work(work: Any, patch: dict[str, Any]) -> None:
    if not isinstance(work, dict):
        raise ValueError("作品必须为对象")
    if "raw_record" in patch:
        replacement = normalize_raw_record(patch["raw_record"], previous=work)
        work.clear()
        work.update(replacement)
        return

    attrs = _find_attr_list(work)
    if attrs is None:
        raise ValueError("作品缺少 attributes")

    date_patch = patch.get("date") or {}
    start = date_patch.get("start") if isinstance(date_patch, dict) else None
    end = date_patch.get("end") if isinstance(date_patch, dict) else None
    if isinstance(start, str) and isinstance(end, str):
        previous_dates = next((
            attribute["data"] for attribute in attrs
            if isinstance(attribute, dict) and attribute.get("type") == "date"
            and isinstance(attribute.get("data"), dict)
        ), {})
        normalized_dates: dict[str, str] = {}
        for field, value in (("start", start), ("end", end)):
            normalized = normalize_air_date(value)
            try:
                previous = normalize_air_date(previous_dates.get(field))
            except ValueError:
                previous = None
            # A full-table save must not force users to invent corrections for
            # unrelated historical calendar errors. Only changed dates are new input.
            if normalized != previous:
                normalize_air_date(normalized, validate_calendar=True)
            normalized_dates[field] = normalized
        _set_scalar_attr(work, "date", {**previous_dates, **normalized_dates})

    name_raw = patch.get("name")
    if isinstance(name_raw, str):
        _set_scalar_attr(work, "name", name_raw)

    country_raw = patch.get("country")
    if isinstance(country_raw, str) and country_raw.strip():
        _set_scalar_attr(work, "country", country_raw.strip())

    domain_raw = patch.get(TV_JP_DOMAIN_KEY)
    release_raw = patch.get(TV_JP_RELEASE_TYPE_KEY)
    markers_raw = patch.get("markers")
    ordered_raw = patch.get("collectioned_ordered")

    if isinstance(domain_raw, str) and isinstance(release_raw, str):
        coll_data = _collection_ordered_to_coll_data(
            ordered_raw,
            domain=domain_raw,
            release_type=release_raw,
            path=patch.get("path"),
            markers=markers_raw,
        )
        previous_collection = next((a.get("data") for a in attrs
                                    if isinstance(a, dict) and a.get("type") == "collection-type"
                                    and isinstance(a.get("data"), dict)), {})
        _set_scalar_attr(work, "collection-type", _preserve_collection_extensions(previous_collection, coll_data, ordered_raw))


def catalog_record_sha256(record: Any) -> str:
    """Stable identity for one raw record, independent of neighboring edits."""
    from collection_detail.work_detail import json_work_record
    return hashlib.sha256(json.dumps(json_work_record(record), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _preserve_collection_extensions(previous: dict[str, Any], updated: dict[str, Any], ordered: Any = None) -> dict[str, Any]:
    """Table patches own known columns, never extension metadata they cannot show."""
    managed = {"domain", "release_type", "path", "markers", "collectioned", "continuations"}
    result = {**{key: copy.deepcopy(value) for key, value in previous.items() if key not in managed}, **updated}

    ordered = [row for row in (ordered if isinstance(ordered, list) else []) if isinstance(row, dict)
               and (str(row.get("press_format") or "").strip() or str(row.get("press_group") or "").strip())]

    def merge_rows(old: Any, new: Any, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        old = old if isinstance(old, list) else []
        new = new if isinstance(new, list) else []
        used: set[int] = set()
        merged = []
        for position, row in enumerate(new):
            original = incoming[position].get("_source_press_index") if position < len(incoming) else None
            exact = [i for i, candidate in enumerate(old) if i not in used and isinstance(candidate, dict)
                     and all(candidate.get(key, "") == row.get(key, "")
                             for key in ("press_format", "press_group", "press_path"))]
            if original is not None:
                if type(original) is not int or original < 0 or original >= len(old) or original in used:
                    raise ValueError("压制记录来源序号已变化或重复，请重新加载收集列表")
                index = original
            else:
                index = exact[0] if len(exact) == 1 else position if len(old) == len(new) and position not in used else None
            base = old[index] if index is not None and index < len(old) and isinstance(old[index], dict) else {}
            if index is not None:
                used.add(index)
            extras = {key: copy.deepcopy(value) for key, value in base.items()
                      if key not in {"press_format", "press_group", "press_path"}}
            merged.append({**extras, **row})
        return merged

    result["collectioned"] = merge_rows(previous.get("collectioned"), updated.get("collectioned"),
                                        [row for row in ordered if row.get("segment") != "continuation"])
    old_blocks = previous.get("continuations") or []
    if "continuations" in result:
        blocks = []
        continuation_indices = sorted({int(row.get("continuation_index") or 0) for row in ordered if row.get("segment") == "continuation"})
        for index, block in enumerate(result["continuations"]):
            source_index = continuation_indices[index] if index < len(continuation_indices) else index
            old = old_blocks[source_index] if 0 <= source_index < len(old_blocks) and isinstance(old_blocks[source_index], dict) else {}
            extras = {key: copy.deepcopy(value) for key, value in old.items() if key not in {"title", "collectioned"}}
            incoming = [row for row in ordered if row.get("segment") == "continuation"
                        and int(row.get("continuation_index") or 0) == source_index]
            blocks.append({**extras, **block, "collectioned": merge_rows(old.get("collectioned"), block.get("collectioned"), incoming)})
        result["continuations"] = blocks
    return result


def normalize_raw_record(raw: Any, *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the full JSON-editable record without discarding extension fields."""
    from collection_detail.catalog_repository import clean_relative_catalog_path
    from collection_detail.work_detail import json_work_record

    if not isinstance(raw, dict):
        raise ValueError("raw_record 必须为完整作品对象")
    try:
        record = json_work_record(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("作品记录必须为有限、无循环的 JSON 数据") from exc
    # Old editing surfaces do not own the optional library extensions. A raw
    # record posted without these fields must not erase stable IDs or sources.
    for key in ("id", "schema_version", "classifications", "metadata", "source_refs"):
        if previous is not None and key in previous and key not in record:
            record[key] = copy.deepcopy(previous[key])
    if previous is not None and previous.get("id") and record.get("id") != previous["id"]:
        raise ValueError("已有作品稳定 ID 不允许更改")
    if "id" in record and (not isinstance(record["id"], str) or re.fullmatch(r"work_[0-9a-f]{32}", record["id"]) is None):
        raise ValueError("作品 id 必须为 work_ 加 32 位小写十六进制稳定标识")
    if "schema_version" in record and (type(record["schema_version"]) is not int or record["schema_version"] != 1):
        raise ValueError("不支持的作品 schema_version")
    attrs = record.get("attributes")
    if not isinstance(attrs, list):
        raise ValueError("作品记录缺少 attributes 数组")
    by_type: dict[str, dict[str, Any]] = {}
    for attr in attrs:
        if not isinstance(attr, dict) or not isinstance(attr.get("type"), str):
            raise ValueError("attributes 每项必须包含字符串 type")
        typ = attr["type"]
        if typ in {"name", "country", "date", "collection-type"}:
            if typ in by_type:
                raise ValueError(f"作品记录存在重复属性：{typ}")
            by_type[typ] = attr
    for typ in ("name", "country", "date", "collection-type"):
        if typ not in by_type:
            raise ValueError(f"作品记录缺少必要属性：{typ}")
    for typ in ("name", "country"):
        if not isinstance(by_type[typ].get("data"), str) or not by_type[typ]["data"].strip():
            raise ValueError(f"作品 {typ} 不能为空")
        by_type[typ]["data"] = by_type[typ]["data"].strip()
    prior_attrs = {a.get("type"): a.get("data") for a in (previous or {}).get("attributes", []) if isinstance(a, dict)}
    dates = by_type["date"].get("data")
    if not isinstance(dates, dict):
        raise ValueError("date.data 必须为对象")
    for field in ("start", "end"):
        value = normalize_air_date(dates.get(field))
        prior = prior_attrs.get("date")
        try:
            previous_value = normalize_air_date(prior.get(field)) if isinstance(prior, dict) else None
        except ValueError:
            previous_value = None
        if value != previous_value:
            normalize_air_date(value, validate_calendar=True)
        dates[field] = value
    coll = by_type["collection-type"].get("data")
    if not isinstance(coll, dict):
        raise ValueError("collection-type.data 必须为对象")
    if "path" in coll:
        coll["path"] = normalize_copied_path(coll["path"]).replace("\\", "/")
        if ".." in coll["path"].split("/"):
            raise ValueError("path 包含非法路径片段")
    blocks = [coll]
    continuations = coll.get("continuations", [])
    if not isinstance(continuations, list) or not all(isinstance(block, dict) for block in continuations):
        raise ValueError("continuations 必须为对象数组")
    blocks.extend(continuations)
    for block in blocks:
        rows = block.get("collectioned", [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("collectioned 必须为压制记录对象数组")
        for row in rows:
            for key in ("press_format", "press_group"):
                if not isinstance(row.get(key), str):
                    raise ValueError(f"压制记录 {key} 必须为字符串（无压制组请使用空字符串）")
                row[key] = row[key].strip()
            if not row["press_format"]:
                raise ValueError("压制记录 press_format 不能为空")
            if "press_path" in row:
                row["press_path"] = clean_relative_catalog_path(row["press_path"], label="press_path")
    load_jp_tv_entries_from_yaml([record])
    return record


def _assert_record_version(patch: dict[str, Any], work: Any, source: bytes | None, *, label: str) -> None:
    expected = patch.get("source_sha256")
    record_hash = patch.get("record_sha256")
    if "raw_record" in patch and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
        raise ValueError(f"{label}: 完整记录修改缺少有效 source_sha256，请重新加载")
    if expected is not None:
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValueError(f"{label}: source_sha256 无效")
        current_record = catalog_record_sha256(work)
        if record_hash is not None and record_hash != current_record:
            raise ValueError(f"{label}: 作品记录已变化，请重新预览")
        if hashlib.sha256(source or b"").hexdigest() != expected and record_hash != current_record:
            raise ValueError(f"{label}: 数据库文件已变化，请重新加载或预览")


def _browse_save_yaml_from_ui_body_unlocked(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[tuple[Path, str]]:
    """校验 body，按 ``yaml_source_rel`` 写回一至多个数据文件并写入 ``History/`` 快照。

    返回每项 ``(写入的绝对路径, 备份文件名（在 History/ 内）)``。
    """
    rows_raw = body.get("rows")
    rows = rows_raw if isinstance(rows_raw, list) else []
    if rows_raw is not None and not isinstance(rows_raw, list):
        raise ValueError("rows 须为数组")

    new_rows_raw = body.get("new_rows")
    new_rows = new_rows_raw if isinstance(new_rows_raw, list) else []
    if new_rows_raw is not None and not isinstance(new_rows_raw, list):
        raise ValueError("new_rows 须为数组")

    deleted_rows_raw = body.get("deleted_rows")
    deleted_rows = deleted_rows_raw if isinstance(deleted_rows_raw, list) else []
    if deleted_rows_raw is not None and not isinstance(deleted_rows_raw, list):
        raise ValueError("deleted_rows 须为数组")

    if not rows and not new_rows and not deleted_rows:
        raise ValueError(
            "缺少 rows / new_rows / deleted_rows（或为空）；请勾选「编辑模式」后提交，并使用「从配置加载默认文件」打开（不要使用仅上传预览）",
        )

    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，禁止写盘")

    client_path = body.get("path")
    singles = tuple(settings.resolved_catalog_yaml_paths)
    only_target = Path(singles[0]).resolve() if len(singles) == 1 else None
    clip: Path | None = None
    if isinstance(client_path, str) and client_path.strip():
        clip = Path(client_path.strip()).expanduser().resolve()
        if only_target is not None and clip.resolve() != only_target.resolve():
            raise ValueError("请求的 path 与当前可写 YAML 不一致，请刷新后重试")

    by_rel: dict[str, dict[int, dict[str, Any]]] = {}
    new_by_rel: dict[str, list[dict[str, Any]]] = {}
    delete_by_rel: dict[str, set[int]] = {}
    delete_versions: dict[tuple[str, int], dict[str, Any]] = {}

    for ri, rp in enumerate(rows):
        rel_s, ii = _row_ref_from_body_item(rp, label=f"rows[{ri}]")

        tgt_probe = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s)

        if clip is not None and len(singles) == 1 and tgt_probe.resolve() != clip.resolve():
            raise ValueError("rows 内含与当前打开的 YAML 不一致的 yaml_source_rel")

        by_rel.setdefault(rel_s, {})[ii] = rp

    for ni, nr in enumerate(new_rows):
        if not isinstance(nr, dict):
            raise ValueError(f"new_rows[{ni}] 须为对象")
        if "raw_record" in nr:
            record = normalize_raw_record(nr["raw_record"])
            entry = load_jp_tv_entries_from_yaml([record])[0]
            nr = {**nr, "raw_record": record, "country": entry_country_slug(entry),
                  "date": {"start": entry_air_dates(entry)[0]}}
        date_patch = nr.get("date")
        start = date_patch.get("start") if isinstance(date_patch, dict) else ""
        # New rows have no existing file identity. Derive the destination on the
        # server, ignoring a stale or forged client yaml_source_rel entirely.
        rel_s = catalog_relpath_for_new_work(nr.get("country") or "japan", start, now=now)
        resolve_safe_yaml_under_root(settings.filesystem_root, rel_s)
        new_by_rel.setdefault(rel_s, []).append(nr)

    for di, dr in enumerate(deleted_rows):
        rel_s, ii = _row_ref_from_body_item(dr, label=f"deleted_rows[{di}]")
        tgt_probe = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s)
        if clip is not None and len(singles) == 1 and tgt_probe.resolve() != clip.resolve():
            raise ValueError("deleted_rows 内含与当前打开的 YAML 不一致的 yaml_source_rel")
        delete_by_rel.setdefault(rel_s, set()).add(ii)
        delete_versions[(rel_s, ii)] = dr

    sorted_rels = sorted(set(by_rel.keys()) | set(new_by_rel.keys()) | set(delete_by_rel.keys()))
    if not sorted_rels:
        raise ValueError("无可写文件路径")

    out: list[tuple[Path, str]] = []
    staged: list[FileWrite] = []
    for file_index, rel_s in enumerate(sorted_rels):
        report_progress("校验并准备数据库文件", completed=file_index, total=len(sorted_rels), unit="文件", detail=rel_s,
                        context={"stage": "保存作品数据库", "action": "准备并校验文件", "yaml_source_rel": rel_s,
                                 "target_path": str(settings.filesystem_root / rel_s)})
        target = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s).expanduser().resolve()
        new_seq = new_by_rel.get(rel_s, [])
        target_existed = target.is_file()
        with operation_context(stage="保存作品数据库", action="读取修改前快照", source_path=str(target), yaml_source_rel=rel_s):
            previous = target.read_bytes() if target_existed else None
        if target_existed:
            _assert_save_target_allowed(target, settings)
            raw_text = previous.decode("utf-8")
            doc = load_yaml_string(raw_text)
        else:
            if not new_seq:
                _assert_save_target_allowed(target, settings)
            doc = []
        works = _works_list_mut(doc)

        idx_map = by_rel.get(rel_s, {})
        delete_set = delete_by_rel.get(rel_s, set())
        for ii in sorted(idx_map.keys()):
            if ii in delete_set:
                continue
            rp = idx_map[ii]
            with operation_context(stage="保存作品数据库", action="校验并更新作品记录", source_path=str(target),
                                   yaml_source_rel=rel_s, index_in_file=ii, object=str(rp.get("name") or "")):
                if ii < 0 or ii >= len(works):
                    raise ValueError(f"{rel_s}: index_in_file 越界：{ii}")
                _assert_record_version(rp, works[ii], previous, label=f"{rel_s}#{ii}")
                _apply_row_patch_to_work(works[ii], rp)

        for ii in sorted(delete_set, reverse=True):
            if ii < 0 or ii >= len(works):
                raise ValueError(f"{rel_s}: 删除 index_in_file 越界：{ii}")
            _assert_record_version(delete_versions[(rel_s, ii)], works[ii], previous, label=f"{rel_s}#{ii}")
            del works[ii]

        for nr in new_seq:
            with operation_context(stage="保存作品数据库", action="校验并新增作品记录", target_path=str(target),
                                   yaml_source_rel=rel_s, index_in_file=len(works), object=str(nr.get("name") or "")):
                works.append(_new_work_from_row_patch(nr))

        new_text = dump_yaml_string(doc)
        try:
            load_jp_tv_entries_from_yaml(load_yaml_string(new_text))
        except Exception as e:
            raise ValueError(f"{rel_s} 写回后的 YAML 校验失败：{e}") from e

        hist_root = history_catalog_root(settings)
        hist_name = ""
        if target_existed:
            hist_name = history_snapshot_name(target)
        staged.append(FileWrite(target, new_text.encode("utf-8"), previous, hist_root / hist_name if hist_name else None))
        out.append((target, hist_name))
        report_progress("数据库文件校验完成", completed=file_index + 1, total=len(sorted_rels), unit="文件", detail=rel_s)

    # Stable identities are catalog-wide, not scoped to a year file. This also
    # protects creates through older collection/organizer entry points.
    from collection_detail.catalog_repository import CatalogRepository
    identity_sources = {write.target.resolve(): write.content for write in staged}
    catalog_paths = set(CatalogRepository(settings).catalog_paths()) | set(identity_sources)
    identity_owners: dict[str, str] = {}
    for source_path in catalog_paths:
        content = identity_sources.get(source_path.resolve())
        content = source_path.read_bytes() if content is None else content
        for index, record in enumerate(_works_list_mut(load_yaml_string(content.decode("utf-8")))):
            identity = record.get("id") if isinstance(record, dict) else None
            if identity is None:
                continue
            if not isinstance(identity, str) or re.fullmatch(r"work_[0-9a-f]{32}", identity) is None:
                raise ValueError(f"作品稳定 ID 无效：{source_path.name}#{index}")
            owner = f"{source_path.name}#{index}"
            if identity in identity_owners:
                raise ValueError(f"作品稳定 ID 重复：{identity_owners[identity]} / {owner}")
            identity_owners[identity] = owner

    if dry_run:
        return out
    report_progress("备份并原子写入数据库", completed=0, total=len(staged), unit="文件")
    for write in staged:
        report_progress("数据库文件已通过校验，准备纳入原子提交", context={"stage": "保存作品数据库", "action": "准备写入与历史备份",
                        "target_path": str(write.target), "history_path": str(write.history_path or "")})
    with operation_context(stage="保存作品数据库", action="批量原子提交，失败则回滚", target_path=str(settings.filesystem_root), operation_count=len(staged)):
        commit_file_writes(staged)
    report_progress("数据库写入完成", completed=len(staged), total=len(staged), unit="文件")
    return out


def browse_save_yaml_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
    now: datetime | None = None,
) -> list[tuple[Path, str]]:
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，禁止写盘")
    report_progress("等待数据库写入锁")
    with catalog_write_transaction(settings.filesystem_root):
        report_progress("准备校验数据库修改")
        return _browse_save_yaml_from_ui_body_unlocked(body, settings=settings, now=now)


def preview_save_yaml_from_ui_body(body: dict[str, Any], *, settings: JpTvBrowseSettings) -> list[tuple[Path, str]]:
    """Run exact save validation without locks, backups or filesystem writes."""
    return _browse_save_yaml_from_ui_body_unlocked(body, settings=settings, dry_run=True)


_ISO_DATE_RE = re.compile(r"^(?P<year>(?:19|20)\d{2})-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
_COUNTRY_FILE_CODES = {
    "japan": "JP",
    "korea": "KR",
    "china": "CN",
    "usa": "US",
    "uk": "UK",
}


@dataclass(frozen=True)
class CatalogMutationReceipt:
    target: Path
    target_existed: bool
    previous_bytes: bytes
    history_path: Path | None
    work_ref: dict[str, Any]
    written_sha256: str


# Compatibility name for callers that still describe the operation as an
# append. Receipts now cover both append and update mutations.
CatalogAppendReceipt = CatalogMutationReceipt


class CatalogRollbackConflictError(RuntimeError):
    """The catalog changed after our write, so restoring would lose data."""


def _normalized_catalog_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def catalog_relpath_for_new_work(country: str, begin_date: Any, *, now: datetime | None = None) -> str:
    country_key = str(country or "").strip().casefold()
    country_code = _COUNTRY_FILE_CODES.get(country_key)
    if country_code is None:
        raise ValueError(f"新增作品暂不支持国家代码：{country}")
    normalized_date = normalize_air_date(begin_date, validate_calendar=True)
    year = normalized_date[:4]
    if not year.isascii() or not year.isdigit() or int(year) == 0:
        year = f"{(now or datetime.now()):%Y}"
    return f"[{country_code}][TVInfo][{year}].yaml"


def _strict_new_work_patch(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValueError("新增作品信息必须是对象")
    name = patch.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("新增作品名不能为空")
    date = patch.get("date")
    if not isinstance(date, dict):
        raise ValueError("新增作品必须填写日期")
    begin_date = str(date.get("start") or "").strip()
    end_date = str(date.get("end") or "").strip()
    if _ISO_DATE_RE.fullmatch(begin_date) is None:
        raise ValueError("新增作品开始日期必须为 YYYY-MM-DD")
    if end_date and _ISO_DATE_RE.fullmatch(end_date) is None:
        raise ValueError("新增作品结束日期必须为 YYYY-MM-DD 或留空")
    normalize_air_date(begin_date, validate_calendar=True)
    normalize_air_date(end_date, validate_calendar=True)
    if end_date and end_date < begin_date:
        raise ValueError("新增作品结束日期不能早于开始日期")
    path = patch.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("新增作品 path 不能为空")
    domain = patch.get(TV_JP_DOMAIN_KEY)
    release_type = patch.get(TV_JP_RELEASE_TYPE_KEY)
    country = patch.get("country")
    for label, value in (
        ("domain", domain),
        ("country", country),
        ("release_type", release_type),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"新增作品 {label} 不能为空")
    presses = patch.get("collectioned_ordered")
    if not isinstance(presses, list) or not presses:
        raise ValueError("新增作品至少需要一个压制记录")
    normalized_presses: list[dict[str, Any]] = []
    press_keys: set[tuple[str, str]] = set()
    for index, raw in enumerate(presses):
        if not isinstance(raw, dict):
            raise ValueError(f"新增作品压制记录 {index + 1} 必须是对象")
        press_format = str(raw.get(TV_JP_PRESS_FORMAT_KEY) or "").strip()
        raw_group = raw.get(TV_JP_PRESS_GROUP_KEY)
        if TV_JP_PRESS_GROUP_KEY not in raw or not isinstance(raw_group, str):
            raise ValueError(f"新增作品压制记录 {index + 1} 必须选择组简称或无压制组")
        press_group = normalize_press_group(raw_group).upper()
        press_path_raw = raw.get(TV_JP_PRESS_PATH_KEY)
        if not isinstance(press_path_raw, str):
            press_path = ""
        else:
            candidate = press_path_raw.strip().replace("\\", "/")
            if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
                raise ValueError(f"新增作品压制记录 {index + 1} 的目标目录必须是相对路径")
            parts = candidate.strip("/").split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise ValueError(f"新增作品压制记录 {index + 1} 的目标目录包含非法路径片段")
            press_path = "/".join(parts)
        if not press_format or not press_path:
            raise ValueError(f"新增作品压制记录 {index + 1} 必须填写格式和目标目录")
        if press_group and not media_group_code_known(press_group):
            raise ValueError(f"新增作品压制记录 {index + 1} 使用了未登记的组简称：{press_group}")
        key = (press_format.casefold(), normalized_press_group(press_group))
        if key in press_keys:
            raise ValueError(f"新增作品存在重复压制记录：{press_format}/{press_group}")
        press_keys.add(key)
        normalized_presses.append(
            {
                TV_JP_PRESS_FORMAT_KEY: press_format,
                TV_JP_PRESS_GROUP_KEY: press_group,
                TV_JP_PRESS_PATH_KEY: press_path,
            }
        )
    return {
        **patch,
        "name": name.strip(),
        "date": {"start": begin_date, "end": end_date},
        "path": path.strip(),
        TV_JP_DOMAIN_KEY: str(domain).strip(),
        "country": str(country).strip().casefold(),
        TV_JP_RELEASE_TYPE_KEY: str(release_type).strip(),
        "collectioned_ordered": normalized_presses,
    }


def _catalog_files(settings: JpTvBrowseSettings) -> list[Path]:
    if settings.filesystem_root is None:
        raise ValueError("未配置 collection-detail filesystem_root")
    root = settings.filesystem_root.resolve()
    return [path.resolve() for path in sorted(root.glob("*.yaml")) if path.is_file()]


@dataclass(frozen=True)
class _CatalogMatch:
    source: Path
    index: int
    exact: bool
    source_sha256: str


def _existing_catalog_match(
    patch: dict[str, Any],
    settings: JpTvBrowseSettings,
    *,
    allow_shared_path_with_different_name: bool = False,
) -> _CatalogMatch | None:
    wanted_name = _normalized_catalog_identity(patch["name"])
    wanted_path = os.path.normcase(os.path.abspath(str(patch["path"])))
    wanted_presses = {
        (
            str(row[TV_JP_PRESS_FORMAT_KEY]).casefold(),
            normalized_press_group(str(row[TV_JP_PRESS_GROUP_KEY])),
            str(row[TV_JP_PRESS_PATH_KEY]).replace("\\", "/").casefold(),
        )
        for row in patch["collectioned_ordered"]
    }
    for yaml_path in _catalog_files(settings):
        source_bytes = yaml_path.read_bytes()
        entries = load_jp_tv_entries_from_yaml(load_yaml_string(source_bytes.decode("utf-8")))
        for index, entry in enumerate(entries):
            name_matches = _normalized_catalog_identity(entry_display_name(entry)) == wanted_name
            data = entry_collection_type_data(entry)
            existing_path = str(data.get("path") or "").strip()
            path_matches = bool(existing_path) and os.path.normcase(os.path.abspath(existing_path)) == wanted_path
            if not name_matches and not path_matches:
                continue
            if allow_shared_path_with_different_name and path_matches and not name_matches:
                # A family root may intentionally contain several distinct works.  This
                # explicit opt-in permits independent DB records sharing a root;
                # a normalized-name match remains fail-closed below.
                continue
            if name_matches != path_matches:
                raise ValueError(
                    "数据库中已有同名作品或相同 path，但两者没有同时匹配；请先在作品数据库中人工核对"
                )
            existing_presses = {
                (
                    str(row.get(TV_JP_PRESS_FORMAT_KEY) or "").strip().casefold(),
                    normalized_press_group(str(row.get(TV_JP_PRESS_GROUP_KEY) or "")),
                    str(row.get(TV_JP_PRESS_PATH_KEY) or "").strip().replace("\\", "/").casefold(),
                )
                for row in data.get("collectioned", [])
                if isinstance(row, dict)
            }
            existing_start, existing_end = entry_air_dates(entry)
            normalize_date = lambda value: re.sub(r"[^0-9]", "", str(value or ""))
            metadata_matches = (
                entry_country_slug(entry).strip().casefold() == patch["country"].casefold()
                and entry_domain_slug(entry).strip().casefold() == patch[TV_JP_DOMAIN_KEY].casefold()
                and entry_release_type_slug(entry).strip().casefold()
                == patch[TV_JP_RELEASE_TYPE_KEY].casefold()
                and normalize_date(existing_start) == normalize_date(patch["date"]["start"])
                and normalize_date(existing_end) == normalize_date(patch["date"]["end"])
            )
            return _CatalogMatch(
                yaml_path,
                index,
                metadata_matches and wanted_presses.issubset(existing_presses),
                _file_sha256(source_bytes),
            )
    return None


def _file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preview_catalog_work_append(
    patch: Any,
    *,
    settings: JpTvBrowseSettings,
    allow_shared_path_with_different_name: bool = False,
) -> dict[str, Any]:
    normalized = _strict_new_work_patch(patch)
    existing = _existing_catalog_match(
        normalized,
        settings,
        allow_shared_path_with_different_name=allow_shared_path_with_different_name,
    )
    if existing is not None:
        if not existing.exact:
            raise ValueError("数据库中已有该作品，但作品信息或压制记录不同；请先在作品数据库中核对并合并")
        return {
            "action": "already_exists",
            "target": str(existing.source),
            "yaml_source_rel": existing.source.name,
            "index_in_file": existing.index,
            "before_sha256": existing.source_sha256,
            "after_sha256": existing.source_sha256,
            "patch": normalized,
        }

    if settings.filesystem_root is None:
        raise ValueError("未配置 collection-detail filesystem_root")
    relpath = catalog_relpath_for_new_work(normalized["country"], normalized["date"]["start"])
    target = resolve_safe_yaml_under_root(settings.filesystem_root, relpath).resolve()
    previous = target.read_bytes() if target.is_file() else b""
    doc = load_yaml_string(previous.decode("utf-8")) if previous else []
    works = _works_list_mut(doc)
    index = len(works)
    works.append(_new_work_from_row_patch(normalized))
    after_text = dump_yaml_string(doc)
    load_jp_tv_entries_from_yaml(load_yaml_string(after_text))
    return {
        "action": "append",
        "target": str(target),
        "yaml_source_rel": relpath,
        "index_in_file": index,
        "before_sha256": _file_sha256(previous),
        "after_sha256": _file_sha256(after_text.encode("utf-8")),
        "patch": normalized,
        "_after_text": after_text,
    }


def apply_catalog_yaml_mutation(
    *,
    target: Path,
    after_bytes: bytes,
    settings: JpTvBrowseSettings,
    expected_before_sha256: str,
    work_ref: dict[str, Any],
) -> CatalogMutationReceipt:
    """CAS-write one catalog YAML and return an exact rollback receipt."""

    if settings.filesystem_root is None:
        raise ValueError("未配置 collection-detail filesystem_root")
    with catalog_write_transaction(settings.filesystem_root):
        root = settings.filesystem_root.resolve()
        target = target.expanduser().resolve()
        target.relative_to(root)
        if target.parent != root or target.suffix.casefold() not in {".yaml", ".yml"}:
            raise ValueError("作品数据库写入目标必须是配置目录内的 YAML 文件")
        if target.exists() and not target.is_file():
            raise ValueError("作品数据库写入目标不是普通文件")

        target_existed = target.is_file()
        previous = target.read_bytes() if target_existed else b""
        if _file_sha256(previous) != str(expected_before_sha256):
            raise ValueError("作品数据库在写入前发生变化，尚未写入；请重新预览")

        history_path: Path | None = None
        if target_existed:
            history_root = history_catalog_root(settings)
            history_root.mkdir(parents=True, exist_ok=True)
            history_path = history_root / history_snapshot_name(target)
            shutil.copy2(target, history_path)

        written_sha256 = _file_sha256(after_bytes)
        _atomic_write_bytes(target, after_bytes)
        if not target.is_file() or _file_sha256(target.read_bytes()) != written_sha256:
            raise OSError("作品数据库原子写入后的内容校验失败")
        return CatalogMutationReceipt(
            target=target,
            target_existed=target_existed,
            previous_bytes=previous,
            history_path=history_path,
            work_ref=dict(work_ref),
            written_sha256=written_sha256,
        )


def rollback_catalog_yaml_mutation(receipt: CatalogMutationReceipt | None) -> None:
    """CAS-rollback without overwriting a later successful catalog write."""

    if receipt is None:
        return
    with catalog_write_transaction(receipt.target.parent):
        if not receipt.target.is_file():
            raise CatalogRollbackConflictError(
                "作品数据库在修复写入后已被删除或替换；为避免覆盖后续改动，拒绝自动回滚"
            )
        current_sha256 = _file_sha256(receipt.target.read_bytes())
        if current_sha256 != receipt.written_sha256:
            raise CatalogRollbackConflictError(
                "作品数据库在修复写入后又有其他成功改动；为避免丢失数据，拒绝自动回滚"
            )
        if receipt.target_existed:
            _atomic_write_bytes(receipt.target, receipt.previous_bytes)
        else:
            receipt.target.unlink()


def append_catalog_work_from_preview(
    patch: Any,
    *,
    settings: JpTvBrowseSettings,
    expected_before_sha256: str,
    allow_shared_path_with_different_name: bool = False,
) -> tuple[dict[str, Any], CatalogMutationReceipt | None]:
    if settings.filesystem_root is None:
        raise ValueError("未配置 collection-detail filesystem_root")
    with catalog_write_transaction(settings.filesystem_root):
        preview = preview_catalog_work_append(
            patch,
            settings=settings,
            allow_shared_path_with_different_name=allow_shared_path_with_different_name,
        )
        if str(preview["before_sha256"]) != str(expected_before_sha256):
            raise ValueError("作品数据库在预览后发生变化，尚未写入；请重新预览")
        if preview["action"] == "already_exists":
            public = {key: value for key, value in preview.items() if not key.startswith("_")}
            return public, None
        target = Path(str(preview["target"])).resolve()
        work_ref = {
            "yaml_source_rel": preview["yaml_source_rel"],
            "index_in_file": preview["index_in_file"],
        }
        receipt = apply_catalog_yaml_mutation(
            target=target,
            after_bytes=str(preview["_after_text"]).encode("utf-8"),
            settings=settings,
            expected_before_sha256=expected_before_sha256,
            work_ref=work_ref,
        )
        public = {key: value for key, value in preview.items() if not key.startswith("_")}
        return public, receipt


def rollback_catalog_work_append(receipt: CatalogAppendReceipt | None) -> None:
    """Compatibility wrapper for the CAS-protected catalog rollback."""

    rollback_catalog_yaml_mutation(receipt)


_ENUM_EDIT_KEYS = {TV_JP_PRESS_FORMAT_KEY, TV_JP_PRESS_GROUP_KEY}


def _yaml_rt() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    y.allow_unicode = True
    y.width = 10_000_000
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def _enum_item_value(item: Any) -> str | None:
    if isinstance(item, str):
        s = item.strip()
        return s if s else None
    if isinstance(item, bool):
        return None
    if isinstance(item, (int, float)):
        s = str(item).strip()
        return s if s else None
    if isinstance(item, dict):
        for k in ("value", "code"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        keys = list(item.keys())
        if len(keys) == 1 and isinstance(keys[0], str) and keys[0].strip():
            return keys[0].strip()
    return None


def _set_enum_item_value(item: Any, new_value: str) -> Any:
    if isinstance(item, str):
        return new_value
    if isinstance(item, dict):
        for k in ("value", "code"):
            if k in item:
                item[k] = new_value
                return item
        keys = list(item.keys())
        if len(keys) == 1 and isinstance(keys[0], str):
            old_key = keys[0]
            item[new_value] = item.pop(old_key)
            return item
    return {"value": new_value}


def _find_or_create_enum_values(raw: Any, enum_key: str) -> list[Any]:
    if not isinstance(raw, dict):
        raise ValueError("浏览配置须为对象，无法编辑 enum")
    enums = raw.get("enum")
    if not isinstance(enums, list):
        enums = []
        raw["enum"] = enums
    for blk in enums:
        if not isinstance(blk, dict):
            continue
        name = blk.get("name")
        if isinstance(name, str) and name.strip() == enum_key:
            vals = blk.get("values")
            if not isinstance(vals, list):
                vals = []
                blk["values"] = vals
            return vals
    blk_new: dict[str, Any] = {"name": enum_key, "values": []}
    enums.append(blk_new)
    return cast(list[Any], blk_new["values"])


def _enum_values_set(values: list[Any]) -> set[str]:
    out: set[str] = set()
    for item in values:
        v = _enum_item_value(item)
        if v:
            out.add(v)
    return out


def _apply_one_enum_edit_to_values(values: list[Any], edit: dict[str, str]) -> bool:
    action = edit["action"]
    old = edit.get("value", "")
    new = edit.get("new_value", "")
    changed = False

    if action == "add":
        if new and new not in _enum_values_set(values):
            values.append(new)
            changed = True
        return changed

    if action == "delete":
        kept: list[Any] = []
        for item in values:
            if _enum_item_value(item) == old:
                changed = True
                continue
            kept.append(item)
        if changed:
            values[:] = kept
        return changed

    if action == "rename":
        seen_new = new in _enum_values_set(values)
        kept2: list[Any] = []
        for item in values:
            if _enum_item_value(item) != old:
                kept2.append(item)
                continue
            if seen_new:
                changed = True
                continue
            kept2.append(_set_enum_item_value(item, new))
            seen_new = True
            changed = True
        if changed:
            values[:] = kept2
        return changed

    return False


def _normalize_enum_edits(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("edits 须为非空数组")
    out: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"edits[{i}] 须为对象")
        enum_key = item.get("enum_key")
        action = item.get("action")
        if not isinstance(enum_key, str) or enum_key.strip() not in _ENUM_EDIT_KEYS:
            raise ValueError(f"edits[{i}].enum_key 仅支持 press_format / press_group")
        if not isinstance(action, str) or action.strip() not in {"add", "delete", "rename"}:
            raise ValueError(f"edits[{i}].action 非法")
        act = action.strip()
        val = item.get("value")
        new_val = item.get("new_value")
        val_s = val.strip() if isinstance(val, str) else ""
        new_s = new_val.strip() if isinstance(new_val, str) else ""
        if act in {"delete", "rename"} and not val_s:
            raise ValueError(f"edits[{i}].value 不能为空")
        if act in {"add", "rename"} and not new_s:
            raise ValueError(f"edits[{i}].new_value 不能为空")
        if act == "rename" and val_s == new_s:
            continue
        out.append(
            {
                "enum_key": enum_key.strip(),
                "action": act,
                "value": val_s,
                "new_value": new_s,
            },
        )
    if not out:
        raise ValueError("没有可应用的枚举变更")
    return out


def _apply_enum_renames_to_doc(doc: Any, renames: list[dict[str, str]]) -> int:
    works = _works_list_mut(doc)
    changed = 0
    for work in works:
        attrs = _find_attr_list(work)
        if attrs is None:
            continue
        coll: dict[str, Any] | None = None
        for a in attrs:
            if isinstance(a, dict) and a.get("type") == "collection-type" and isinstance(a.get("data"), dict):
                coll = cast(dict[str, Any], a["data"])
                break
        if coll is None:
            continue

        row_lists: list[Any] = [coll.get("collectioned")]
        conts = coll.get("continuations")
        if isinstance(conts, list):
            for blk in conts:
                if isinstance(blk, dict):
                    row_lists.append(blk.get("collectioned"))

        for rows in row_lists:
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for rn in renames:
                    k = rn["enum_key"]
                    if row.get(k) == rn["value"]:
                        row[k] = rn["new_value"]
                        changed += 1
    return changed


def _browse_apply_enum_edits_from_ui_body_unlocked(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
    config_path: Path | None,
) -> dict[str, Any]:
    report_progress("校验枚举配置修改")
    edits = _normalize_enum_edits(body.get("edits"))
    if config_path is None or not config_path.is_file():
        raise ValueError("当前未使用可写的浏览配置文件，无法编辑枚举")
    if config_path.name == "browse_config.default.yaml":
        raise ValueError("当前使用包内兜底配置，不能直接编辑枚举；请先使用工程配置文件")

    y = _yaml_rt()
    config_previous = config_path.read_bytes()
    raw_cfg = y.load(config_previous.decode("utf-8")) or {}
    if not isinstance(raw_cfg, dict):
        raise ValueError(f"浏览配置须为对象：{config_path}")

    config_changed = False
    for ed in edits:
        vals = _find_or_create_enum_values(raw_cfg, ed["enum_key"])
        if _apply_one_enum_edit_to_values(vals, ed):
            config_changed = True

    renames = [ed for ed in edits if ed["action"] == "rename"]
    data_writes: list[dict[str, Any]] = []
    staged: list[FileWrite] = []
    if renames and settings.filesystem_root is not None:
        hist_root = history_catalog_root(settings)
        catalog_paths = settings.resolved_catalog_yaml_paths
        for file_index, abs_s in enumerate(catalog_paths):
            target = Path(abs_s).resolve()
            report_progress("检查数据库中的枚举引用", completed=file_index, total=len(catalog_paths), unit="文件", detail=target.name)
            _assert_save_target_allowed(target, settings)
            previous = target.read_bytes()
            raw_text = previous.decode("utf-8")
            doc = load_yaml_string(raw_text)
            touched = _apply_enum_renames_to_doc(doc, renames)
            if touched <= 0:
                continue
            new_text = dump_yaml_string(doc)
            try:
                load_jp_tv_entries_from_yaml(load_yaml_string(new_text))
            except Exception as e:
                raise ValueError(f"{target.name} 枚举同步后的 YAML 校验失败：{e}") from e
            hist_name = history_snapshot_name(target)
            staged.append(FileWrite(target, new_text.encode("utf-8"), previous, hist_root / hist_name))
            data_writes.append(
                {
                    "path": str(target),
                    "history_file": hist_name,
                    "changes": touched,
                },
            )

    if config_changed:
        buffer = StringIO()
        y.dump(raw_cfg, buffer)
        staged.append(FileWrite(config_path, buffer.getvalue().encode("utf-8"), config_previous))
    report_progress("备份并写入枚举与数据库修改", completed=0, total=len(staged), unit="文件")
    commit_file_writes(staged)
    report_progress("枚举与数据库修改完成", completed=len(staged), total=len(staged), unit="文件")
    return {
        "config_path": str(config_path.resolve()),
        "config_changed": config_changed,
        "edits_applied": edits,
        "data_writes": data_writes,
    }


def browse_apply_enum_edits_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
    config_path: Path | None,
) -> dict[str, Any]:
    if settings.filesystem_root is None:
        return _browse_apply_enum_edits_from_ui_body_unlocked(
            body,
            settings=settings,
            config_path=config_path,
        )
    with catalog_write_transaction(settings.filesystem_root):
        return _browse_apply_enum_edits_from_ui_body_unlocked(
            body,
            settings=settings,
            config_path=config_path,
        )


def annotate_save_capabilities(
    payload: dict[str, Any],
    *,
    yaml_disk_abs: str | None,
    settings: JpTvBrowseSettings,
    catalog_default: bool = False,
    catalog_disk_abs_paths: tuple[str, ...] | None = None,
) -> None:
    """为浏览 payload 增加 ``save`` 字段。"""
    if not payload.get("ok"):
        return

    def _hint_sibling_history() -> str:
        if settings.filesystem_root is None:
            return ""
        try:
            return str(history_catalog_root(settings))
        except (OSError, ValueError):
            return ""

    sibling_hist = _hint_sibling_history()

    if catalog_default and settings.resolved_catalog_yaml_paths:
        if catalog_disk_abs_paths is not None and len(catalog_disk_abs_paths) > 0:
            abs_list = [str(Path(p).resolve()) for p in catalog_disk_abs_paths]
        else:
            abs_list = [str(Path(p).resolve()) for p in settings.resolved_catalog_yaml_paths]
        mf = len(abs_list) > 1
        payload["save"] = {
            "enabled": True,
            "multi_file": mf,
            "target_path": abs_list[0],
            "target_paths": abs_list,
            "history_hint": sibling_hist,
            "help": "保存时按数据文件的 yaml_source_rel 写回 DB；每次保存先在 collection-detail 的 history 中写入带时间戳的备份。",
        }
        return

    if yaml_disk_abs:
        fp = Path(yaml_disk_abs)
        if fp.is_file():
            legacy_hist = str(fp.resolve().parent / "History")
            payload["save"] = {
                "enabled": True,
                "multi_file": False,
                "target_path": str(fp.resolve()),
                "target_paths": [str(fp.resolve())],
                "history_hint": sibling_hist or legacy_hist,
                "help": "编辑后保存会先在 collection-detail 的 history 中写入备份（若未配置 DB 路径则退化到目标文件同目录 History）。",
            }
            return

    payload["save"] = {
        "enabled": False,
        "multi_file": False,
        "target_path": None,
        "target_paths": [],
        "history_hint": sibling_hist,
        "reason": "仅「从配置加载数据」打开的会话可写入磁盘（上传预览不可直接保存以防误覆盖）。",
    }
