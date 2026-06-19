from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, TypedDict, cast


class DateRangeData(TypedDict):
    start: str
    end: str


# 结构化 data（如 date.collection-type）；后者为普通 dict，不在此穷举。
AttributeDatum = str | DateRangeData | dict[str, Any]


@dataclass
class CatalogAttribute:
    """与 samples/tv-jp.yaml 一致：type + data；description 仅在有文案时写入 YAML。"""

    type: str
    data: AttributeDatum
    description: str = ""


@dataclass
class JpTvEntry:
    attributes: list[CatalogAttribute]


@dataclass
class JpTvDocument:
    """作品列表；根为数组或含 works（或 entries）的对象；额外顶层键（如 browse）不参与条目解析。"""

    works: list[JpTvEntry]


_TYPE_DATE = "date"

TV_JP_PRESS_FORMAT_KEY = "press_format"
TV_JP_PRESS_GROUP_KEY = "press_group"
TV_JP_PRESS_PATH_KEY = "press_path"
TV_JP_DOMAIN_KEY = "domain"
TV_JP_RELEASE_TYPE_KEY = "release_type"


def jp_tv_press_pair_from_row(row: object) -> tuple[str, str] | None:
    """从 ``collectioned`` 单行对象取出「格式, 组」。"""
    if not isinstance(row, dict):
        return None
    r = cast(dict[str, Any], row)
    fm = r.get(TV_JP_PRESS_FORMAT_KEY)
    gr = r.get(TV_JP_PRESS_GROUP_KEY)
    if isinstance(fm, str) and isinstance(gr, str):
        return (fm, gr)
    return None


def _canonical_markers_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        if s not in out:
            out.append(s)
    return out


def strip_tv_jp_tag_decoration(inner: str) -> str:
    """去掉 bracket 正文前导的 ``_`` 与 ``-``；用尽后若无字符则退回原文。"""
    if not inner:
        return inner
    t = re.sub(r"^[_\-]+", "", inner)
    return t if t else inner


def _pairs_to_collectioned_row(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {
            TV_JP_PRESS_FORMAT_KEY: strip_tv_jp_tag_decoration(a),
            TV_JP_PRESS_GROUP_KEY: strip_tv_jp_tag_decoration(b),
        }
        for a, b in pairs
    ]


def _collectioned_to_tag_pairs(ce: object) -> list[tuple[str, str]] | None:
    if not isinstance(ce, list) or not ce:
        return None
    tup: list[tuple[str, str]] = []
    for row in ce:
        pr = jp_tv_press_pair_from_row(row) if isinstance(row, dict) else None
        if pr:
            tup.append(
                (
                    strip_tv_jp_tag_decoration(pr[0]),
                    strip_tv_jp_tag_decoration(pr[1]),
                )
            )
        elif isinstance(row, list) and len(row) == 2:
            x, y = row[0], row[1]
            if isinstance(x, str) and isinstance(y, str):
                tup.append((strip_tv_jp_tag_decoration(x), strip_tv_jp_tag_decoration(y)))
    return tup or None


def _sanitize_yaml_collectioned_rows(rows: Any) -> list[dict[str, str]]:
    """读入 YAML 的 collectioned：统一去掉 press_format / press_group 前导 _ / -。"""
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        pr = jp_tv_press_pair_from_row(row)
        if pr:
            fm, gr = pr
            item = {
                TV_JP_PRESS_FORMAT_KEY: strip_tv_jp_tag_decoration(fm),
                TV_JP_PRESS_GROUP_KEY: strip_tv_jp_tag_decoration(gr),
            }
            if isinstance(row, dict):
                press_path = _normalize_rel_path(row.get(TV_JP_PRESS_PATH_KEY), "press_path")
                if press_path:
                    item[TV_JP_PRESS_PATH_KEY] = press_path
            out.append(item)
    return out


def _normalize_rel_path(raw: Any, label: str) -> str:
    if not isinstance(raw, str):
        return ""
    s = raw.strip().replace("\\", "/")
    s = re.sub(r"/+", "/", s).strip("/")
    if not s:
        return ""
    if re.match(r"^[A-Za-z]:", s) or s.startswith("/"):
        raise ValueError(f"collection-type.data.{label} 必须为相对路径")
    parts = s.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"collection-type.data.{label} 包含非法路径片段")
    return s


def _normalize_work_path(raw: Any, label: str) -> str:
    if not isinstance(raw, str):
        return ""
    raw_s = raw.strip()
    if not raw_s:
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", raw_s) or raw_s.startswith(("/", "\\")):
        return str(Path(raw_s).expanduser()).rstrip("\\/")
    return _normalize_rel_path(raw_s, label)


def _deep_copy_mapping(dc: dict[str, Any]) -> dict[str, Any]:
    import copy as _copy

    return cast(dict[str, Any], _copy.deepcopy(dc))


def _collection_type_domain_release(dc: Mapping[str, Any]) -> tuple[str, str]:
    """从已规整的 ``collection-type.data`` 读取 ``domain``、``release_type``（皆为非空字符串）。"""
    d = dc.get(TV_JP_DOMAIN_KEY)
    rt = dc.get(TV_JP_RELEASE_TYPE_KEY)
    if not isinstance(d, str) or not d.strip():
        raise ValueError("collection-type.data.domain 须为非空字符串")
    if not isinstance(rt, str) or not rt.strip():
        raise ValueError("collection-type.data.release_type 须为非空字符串")
    return d.strip(), rt.strip()


def _canonicalize_domain_release_keys(work: dict[str, Any]) -> None:
    """校验并规整 ``domain`` / ``release_type`` 键（均为顶层标量字符串）。"""
    ds, ln = _collection_type_domain_release(work)
    work[TV_JP_DOMAIN_KEY] = ds
    work[TV_JP_RELEASE_TYPE_KEY] = ln


def _normalize_collection_type_dict(dc: dict[str, Any]) -> dict[str, Any]:
    work = _deep_copy_mapping(dc)
    _canonicalize_domain_release_keys(work)
    path_s = _normalize_work_path(work.get("path"), "path")
    if path_s:
        work["path"] = path_s
    else:
        work.pop("path", None)
    cel = list(work.get("collectioned") or [])
    work["collectioned"] = _sanitize_yaml_collectioned_rows(cel)

    work["markers"] = _canonical_markers_list(work.get("markers"))

    raw_cont = work.get("continuations")
    if isinstance(raw_cont, list) and raw_cont:
        conts: list[dict[str, Any]] = []
        for blk in raw_cont:
            if not isinstance(blk, dict):
                continue
            cc = _canonical_continuation_block(blk)
            if cc is not None:
                conts.append(cc)
        if conts:
            work["continuations"] = conts
        else:
            work.pop("continuations", None)
    else:
        work.pop("continuations", None)

    return work


def _emit_collection_type_yaml_dict(dc: dict[str, Any]) -> dict[str, Any]:
    """写出 data 结构与 samples/tv-jp.yaml 一致。

    ``continuations`` 保留原续行的 ``collectioned`` 与可选 ``title``，避免写回时丢失行结构。
    """
    n = _normalize_collection_type_dict(dc)
    fds, fln = _collection_type_domain_release(n)
    out: dict[str, Any] = {
        TV_JP_DOMAIN_KEY: fds,
        TV_JP_RELEASE_TYPE_KEY: fln,
    }
    if n.get("path"):
        out["path"] = str(n["path"])
    out["collectioned"] = list(n["collectioned"])
    out["markers"] = list(n["markers"])
    raw_cont = n.get("continuations")
    if isinstance(raw_cont, list) and raw_cont:
        seq: list[dict[str, Any]] = []
        for blk in raw_cont:
            if not isinstance(blk, dict):
                continue
            o: dict[str, Any] = {}
            cel = blk.get("collectioned")
            if isinstance(cel, list) and cel:
                o["collectioned"] = cel
            if blk.get("title") is not None and str(blk.get("title")).strip():
                o["title"] = str(blk["title"]).strip()
            if o:
                seq.append(o)
        if seq:
            out["continuations"] = seq
    return out


def _finalize_entry_collection_canonical(entry: JpTvEntry) -> JpTvEntry:
    attrs: list[CatalogAttribute] = []
    for a in entry.attributes:
        if a.type == "collection-type" and isinstance(a.data, dict):
            attrs.append(
                CatalogAttribute(
                    a.type,
                    cast(AttributeDatum, _normalize_collection_type_dict(a.data)),
                    a.description,
                )
            )
        else:
            attrs.append(a)
    return JpTvEntry(attributes=attrs)


def build_tv_jp_entry_attributes(
    start: str,
    end: str,
    *,
    title: str = "",
    pairs: list[tuple[str, str]] | None = None,
    markers: list[str] | None = None,
    continuations: list[dict[str, Any]] | None = None,
    country: str = "japan",
    domain: str = "animation",
    release_type: str = "tv",
) -> list[CatalogAttribute]:
    """构造一条数据「tv-jp」属性列表（对齐 tv-jp.yaml）。"""
    coll: dict[str, Any] = {
        TV_JP_DOMAIN_KEY: domain,
        TV_JP_RELEASE_TYPE_KEY: release_type,
        "collectioned": _pairs_to_collectioned_row(list(pairs or [])),
        "markers": list(markers or []),
    }
    if continuations:
        coll["continuations"] = continuations
    coll = _normalize_collection_type_dict(coll)
    return [
        CatalogAttribute(_TYPE_DATE, cast(AttributeDatum, {"start": start, "end": end})),
        CatalogAttribute("collection-type", cast(AttributeDatum, coll)),
        CatalogAttribute("country", country),
        CatalogAttribute("name", title),
    ]


def _canonical_continuation_block(blk: dict[str, Any]) -> dict[str, Any] | None:
    """续行的 ``collectioned``（同主行）与可选 ``title``。"""
    title_s = ""
    if isinstance(blk.get("title"), str) and str(blk["title"]).strip():
        title_s = str(blk["title"]).strip()
    rows = _sanitize_yaml_collectioned_rows(list(blk.get("collectioned") or []))
    out: dict[str, Any] = {}
    if rows:
        out["collectioned"] = list(rows)
    if title_s:
        out["title"] = title_s
    return out if out else None


def _assert_attribute(raw: object, label: str) -> CatalogAttribute:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} 须为对象")
    t = raw.get("type")
    datum = raw.get("data")
    desc = raw.get("description")
    if not isinstance(t, str):
        raise ValueError(f"{label}.type 须为字符串")
    dstr = cast(str, desc) if isinstance(desc, str) else ""
    if isinstance(datum, str):
        return CatalogAttribute(type=t, data=datum, description=dstr)
    if isinstance(datum, dict):
        if "start" in datum and "end" in datum:
            st, en = datum.get("start"), datum.get("end")
            if not isinstance(st, str) or not isinstance(en, str):
                raise ValueError(f"{label}.data.start / end 须为字符串")
            return CatalogAttribute(
                type=t,
                data=cast(AttributeDatum, {"start": st, "end": en}),
                description=dstr,
            )
        return CatalogAttribute(type=t, data=dict(datum), description=dstr)
    raise ValueError(f"{label}.data 类型不支持")


def _finalize_tv_jp(attrs: list[CatalogAttribute]) -> list[CatalogAttribute]:
    kinds = [a.type for a in attrs]
    out = list(attrs)
    if "country" not in kinds:
        out.append(CatalogAttribute("country", "japan"))
    if "name" not in kinds:
        out.append(CatalogAttribute("name", ""))
    return out


def _copy_attr_shallow(a: CatalogAttribute) -> CatalogAttribute:
    if isinstance(a.data, dict):
        return CatalogAttribute(a.type, dict(a.data), a.description)
    return CatalogAttribute(a.type, a.data, a.description)


def _finalize_loaded_tv_jp_attributes(attrs: list[CatalogAttribute]) -> list[CatalogAttribute]:
    """校验并规整已加载的 jp-tv 四段模型（仅支持当前定义的 schema）。"""
    kinds = {a.type for a in attrs}
    if "broadcast" in kinds:
        raise ValueError('禁止使用 attributes 类型 "broadcast"；请改用 "date"')
    if _TYPE_DATE not in kinds:
        raise ValueError("作品须含 date（data.start / data.end）")
    if "collection-type" not in kinds:
        raise ValueError("作品须含 collection-type")

    merged = [_copy_attr_shallow(a) for a in attrs]
    for i, a in enumerate(merged):
        if a.type == "collection-type" and isinstance(a.data, dict):
            dc = _normalize_collection_type_dict(_deep_copy_mapping(a.data))
            merged[i] = CatalogAttribute(a.type, cast(AttributeDatum, dc), a.description)
    return _finalize_tv_jp(merged)


def _assert_work_item(e: object, i: int, *, lbl: str | None = None) -> JpTvEntry:
    label = lbl if lbl is not None else f"works[{i}]"
    if not isinstance(e, dict):
        raise ValueError(f"{label} 须为对象")

    attrs_raw = e.get("attributes")
    if not isinstance(attrs_raw, list):
        raise ValueError(f"{label} 须含 attributes 数组")
    attributes = [_assert_attribute(x, f"{label}.attributes[{k}]") for k, x in enumerate(attrs_raw)]

    final_attrs = _finalize_loaded_tv_jp_attributes(attributes)
    return _finalize_entry_collection_canonical(JpTvEntry(attributes=final_attrs))


def load_jp_tv_yaml_document(raw: Any) -> JpTvDocument:
    if isinstance(raw, dict):
        for key in ("works", "entries"):
            wl = raw.get(key)
            if isinstance(wl, list):
                return JpTvDocument(works=[_assert_work_item(x, i) for i, x in enumerate(wl)])
        raise ValueError("YAML 根对象须有 works（或 entries）数组")
    if isinstance(raw, list):
        return JpTvDocument(
            works=[_assert_work_item(x, i, lbl=f"[{i}]") for i, x in enumerate(raw)],
        )
    raise ValueError("YAML 根须为含 works（或 entries）的对象，或作品数组")


def load_jp_tv_entries_from_yaml(raw: Any) -> list[JpTvEntry]:
    return load_jp_tv_yaml_document(raw).works


# ---------- 读写辅助 ----------

_DOMAIN_DIR_SLUG = {
    "animation": "Animation",
    "game": "Game",
    "music": "Music",
    "image": "Image",
}
_COUNTRY_DIR_SLUG = {"japan": "Japan"}
_RELEASE_TYPE_DIR_SLUG = {"tv": "TV", "ova": "OVA", "movie": "Movie", "album": "Album"}


def entry_air_dates(entry: JpTvEntry) -> tuple[str, str]:
    """从 ``date`` 属性读取播出档期 ``(start, end)``（YYYYMMDD）。"""
    for a in entry.attributes:
        if a.type == _TYPE_DATE and isinstance(a.data, dict):
            bd = a.data
            return str(bd["start"]), str(bd["end"])
    raise ValueError("作品中缺少 date 播出日期")


def entry_collection_type_data(entry: JpTvEntry) -> dict[str, Any]:
    for a in entry.attributes:
        if a.type == "collection-type" and isinstance(a.data, dict):
            return a.data
    raise ValueError("作品中缺少 collection-type")


def entry_display_name(entry: JpTvEntry) -> str:
    for a in entry.attributes:
        if a.type == "name":
            return str(a.data) if isinstance(a.data, str) else ""
    return ""


def entry_country_slug(entry: JpTvEntry) -> str:
    for a in entry.attributes:
        if a.type == "country" and isinstance(a.data, str):
            return a.data
    return "japan"


def entry_domain_slug(entry: JpTvEntry) -> str:
    dc = entry_collection_type_data(entry)
    return _collection_type_domain_release(dc)[0]


def entry_release_type_slug(entry: JpTvEntry) -> str:
    dc = entry_collection_type_data(entry)
    return _collection_type_domain_release(dc)[1]


def taxonomy_path_segments_from_slugs(m: str, c: str, s: str) -> tuple[str, str, str]:
    md = _DOMAIN_DIR_SLUG.get(m, m.replace("_", " ").title().replace(" ", ""))
    cd = _COUNTRY_DIR_SLUG.get(c, c.replace("_", " ").title().replace(" ", ""))
    sd = _RELEASE_TYPE_DIR_SLUG.get(s, s.replace("_", " ").upper().replace(" ", ""))
    return md, cd, sd


def jp_tv_txt_relpath_from_entry(entry: JpTvEntry, txt_basename: str) -> str:
    m = entry_domain_slug(entry)
    c = entry_country_slug(entry)
    q = entry_release_type_slug(entry)
    base = txt_basename.replace("\\", "/").split("/")[-1]
    seg_a, seg_b, seg_lane = taxonomy_path_segments_from_slugs(m, c, q)
    return "/".join((seg_a, seg_b, seg_lane, base))


def attribute_to_plain(a: CatalogAttribute) -> dict[str, Any]:
    if a.type == "collection-type" and isinstance(a.data, dict):
        data_out: dict[str, Any] | str = _emit_collection_type_yaml_dict(a.data)
    else:
        data_out = a.data
    o: dict[str, Any] = {"type": a.type, "data": data_out}
    if a.description:
        o["description"] = a.description
    return o


def jp_tv_entry_to_plain_dict(entry: JpTvEntry) -> dict[str, Any]:
    return {"attributes": [attribute_to_plain(x) for x in entry.attributes]}


def jp_tv_works_to_plain_list(works: list[JpTvEntry]) -> list[dict[str, Any]]:
    return [jp_tv_entry_to_plain_dict(w) for w in works]


def append_collection_continuation(
    entry: JpTvEntry,
    *,
    tag_pairs: list[tuple[str, str]] | None,
    title: str | None,
) -> JpTvEntry:
    """在 collection-type.data.continuations 末尾追加一项（txt 解析续行）。"""
    attrs: list[CatalogAttribute] = []
    updated = False
    for a in entry.attributes:
        if a.type == "collection-type" and isinstance(a.data, dict):
            dc = _deep_copy_mapping(a.data)
            conts = list(dc.get("continuations") or [])
            block: dict[str, Any] = {
                "collectioned": _pairs_to_collectioned_row(list(tag_pairs or []))
            }
            if title is not None and str(title).strip():
                block["title"] = title.strip()
            conts.append(block)
            dc["continuations"] = conts
            dc = _normalize_collection_type_dict(dc)
            attrs.append(CatalogAttribute(a.type, cast(AttributeDatum, dc), a.description))
            updated = True
        else:
            attrs.append(_copy_attr_shallow(a))
    if not updated:
        raise ValueError("作品中缺少 collection-type，无法追加续行")
    return _finalize_entry_collection_canonical(JpTvEntry(attributes=attrs))


def merge_tv_jp_collect_markers(entry: JpTvEntry, additions: list[str]) -> JpTvEntry:
    """将解析得到的收集标记（如 music / subs）并入 collection-type.data.markers，去重保序。"""
    if not additions:
        return entry
    attrs: list[CatalogAttribute] = []
    for a in entry.attributes:
        if a.type == "collection-type" and isinstance(a.data, dict):
            dc = _normalize_collection_type_dict(_deep_copy_mapping(a.data))
            cur_m = _canonical_markers_list(list(dc.get("markers") or []))
            for x in additions:
                if isinstance(x, str) and x.strip():
                    canon = x.strip()
                    if canon not in cur_m:
                        cur_m.append(canon)
            dc["markers"] = cur_m
            attrs.append(CatalogAttribute(a.type, cast(AttributeDatum, dc), a.description))
        else:
            attrs.append(_copy_attr_shallow(a))
    return _finalize_entry_collection_canonical(JpTvEntry(attributes=attrs))
