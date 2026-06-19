"""tv-jp 符号表（YAML）：每类枚举 kind 有一批合法取值，每项可有 ``description`` 与扩展 ``extra``。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Mapping

from work_catalog_yaml.jp_tv.validate import (
    JpTvEntry,
    TV_JP_PRESS_FORMAT_KEY,
    TV_JP_PRESS_GROUP_KEY,
    entry_collection_type_data,
    entry_country_slug,
    entry_domain_slug,
    entry_release_type_slug,
)
from work_catalog_yaml.yaml_io import load_yaml


@dataclass(frozen=True)
class SymbolValueSpec:
    """单个枚举取值 + 可读描述与扩展字典（类比 protobuf enum value options）。"""

    code: str
    description: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SymbolKindSpec:
    """一类符号（类比 ``enum class`` 或 protobuf ``Enum`` message）。"""

    kind_id: str
    label: str
    description: str
    meta: Mapping[str, Any]
    values: Mapping[str, SymbolValueSpec]


@dataclass(frozen=True)
class TvJpSymbolTable:
    version: int
    kinds: Mapping[str, SymbolKindSpec]


def _mapping_str_any(obj: Any, *, ctx: str) -> dict[str, Any]:
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise ValueError(f"{ctx} 须为对象")
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if not isinstance(k, str):
            raise ValueError(f"{ctx} 的键须为字符串")
        out[k] = v
    return out


def _parse_value_spec(code: str, raw: Any) -> SymbolValueSpec:
    if isinstance(raw, str):
        return SymbolValueSpec(code=code, description=raw.strip(), extra={})
    if not isinstance(raw, dict):
        raise ValueError(f"values.{code} 须为字符串或对象")
    d = raw.get("description")
    desc = d.strip() if isinstance(d, str) else ""
    extra = raw.get("extra")
    if extra is None:
        extra = raw.get("extend")
    ex = _mapping_str_any(extra, ctx=f"values.{code}.extra")
    return SymbolValueSpec(code=code, description=desc, extra=ex)


def tv_jp_symbol_table_from_mapping(raw: Any) -> TvJpSymbolTable:
    """载入根对象 ``{ version?, kinds }``。"""
    if not isinstance(raw, dict):
        raise ValueError("符号表根须为对象")
    ver_raw = raw.get("version")
    iv = int(ver_raw) if isinstance(ver_raw, int) else 1
    kinds_raw = raw.get("kinds")
    if not isinstance(kinds_raw, dict):
        raise ValueError("符号表须有 kinds 对象")

    kinds_out: dict[str, SymbolKindSpec] = {}
    for kind_id, body in kinds_raw.items():
        if not isinstance(kind_id, str):
            raise ValueError("kinds 的键须为字符串")
        if not isinstance(body, dict):
            raise ValueError(f"kinds.{kind_id} 须为对象")
        lbl = body.get("label")
        lab = lbl.strip() if isinstance(lbl, str) else kind_id
        d0 = body.get("description")
        desc_kind = d0.strip() if isinstance(d0, str) else ""
        meta = _mapping_str_any(body.get("meta"), ctx=f"kinds.{kind_id}.meta")
        vals_raw = body.get("values")
        if not isinstance(vals_raw, dict):
            raise ValueError(f"kinds.{kind_id}.values 须为对象")
        vals: dict[str, SymbolValueSpec] = {}
        for code, vraw in vals_raw.items():
            if not isinstance(code, str):
                raise ValueError(f"kinds.{kind_id}.values 的键须为字符串")
            vals[code] = _parse_value_spec(code, vraw)
        kinds_out[kind_id] = SymbolKindSpec(
            kind_id=kind_id,
            label=lab,
            description=desc_kind,
            meta=meta,
            values=vals,
        )
    return TvJpSymbolTable(version=iv, kinds=kinds_out)


def load_tv_jp_symbol_table_yaml(path: str | Path | IO[str]) -> TvJpSymbolTable:
    return tv_jp_symbol_table_from_mapping(load_yaml(path))


def builtin_tv_jp_symbol_table() -> TvJpSymbolTable:
    """与数据缺省 taxonomy / `[____MS]` 解析一致的基准表。"""
    return tv_jp_symbol_table_from_mapping(
        {
            "version": 1,
            "kinds": {
                "country": {
                    "label": "国家（country.data）",
                    "description": "country 属性的 slug",
                    "meta": {},
                    "values": {
                        "japan": {
                            "description": "日本",
                            "extra": {"taxonomy_dir": "Japan"},
                        },
                    },
                },
                "domain": {
                    "label": "数据大类（data.domain）",
                    "description": "animation / game …；与同层字段 ``release_type``（tv/ova/movie …）配合标注作品形态",
                    "meta": {},
                    "values": {
                        "animation": {
                            "description": "动画",
                            "extra": {"taxonomy_dir": "Animation"},
                        },
                        "game": {
                            "description": "游戏",
                            "extra": {"taxonomy_dir": "Game"},
                        },
                        "music": {
                            "description": "音乐",
                            "extra": {"taxonomy_dir": "Music"},
                        },
                        "image": {
                            "description": "图集 / 画集",
                            "extra": {"taxonomy_dir": "Image"},
                        },
                    },
                },
                "release_type": {
                    "label": "作品形态归类（data.release_type）",
                    "description": "``collection-type.data.release_type``：tv · ova · movie · album …",
                    "meta": {},
                    "values": {
                        "tv": {
                            "description": "电视档期",
                            "extra": {"taxonomy_dir": "TV"},
                        },
                        "ova": {
                            "description": "OVA / 映像盘",
                            "extra": {"taxonomy_dir": "OVA"},
                        },
                        "movie": {
                            "description": "剧场版",
                            "extra": {"taxonomy_dir": "Movie"},
                        },
                        "album": {
                            "description": "音乐专辑等非番组视频主线",
                            "extra": {"taxonomy_dir": "Album"},
                        },
                    },
                },
                "collect_marker_letter": {
                    "label": "收集标记括号中的字母码",
                    "description": "``[____x…]`` 中下划线后缀仅含 M/S/_ 时的字母含义（与 jp_tv.parse 一致）",
                    "meta": {},
                    "values": {
                        "M": {"description": "music"},
                        "S": {"description": "subs"},
                    },
                },
                "marker_label": {
                    "label": "markers 展开文案",
                    "description": "collection-type.data.markers 中的字面量",
                    "meta": {},
                    "values": {
                        "music": {"description": "由字母 M 展开"},
                        "subs": {"description": "由字母 S 展开"},
                    },
                },
            },
        }
    )


def merge_tv_jp_symbol_tables(base: TvJpSymbolTable, overlay: TvJpSymbolTable | None) -> TvJpSymbolTable:
    if overlay is None:
        return base
    kinds_m: dict[str, SymbolKindSpec] = {}
    for kid, kd in base.kinds.items():
        kinds_m[kid] = kd
    for kid, od in overlay.kinds.items():
        bd = kinds_m.get(kid)
        if bd is None:
            kinds_m[kid] = od
            continue
        vcomb = dict(bd.values)
        vcomb.update(od.values)
        kinds_m[kid] = SymbolKindSpec(
            kind_id=kid,
            label=od.label or bd.label,
            description=od.description or bd.description,
            meta={**bd.meta, **od.meta},
            values=vcomb,
        )
    return TvJpSymbolTable(version=max(base.version, overlay.version), kinds=kinds_m)


def merged_tv_jp_symbol_table(extra_path: str | Path | None = None) -> TvJpSymbolTable:
    b = builtin_tv_jp_symbol_table()
    if extra_path is None:
        return b
    ov = load_tv_jp_symbol_table_yaml(Path(extra_path))
    return merge_tv_jp_symbol_tables(b, ov)


def _slug_msg(kind: SymbolKindSpec | None, value: str, *, path: str) -> str | None:
    if kind is None or not kind.values:
        return None
    if value in kind.values:
        return None
    allow = ", ".join(sorted(kind.values.keys()))
    return f"{path}: {value!r} 不在枚举 {kind.kind_id}（允许 {allow}）"


def list_tv_jp_symbol_violations(entry: JpTvEntry, table: TvJpSymbolTable | None) -> list[str]:
    """按符号表检查作品；不设某 kind 或其 values 为空则跳过该域。"""
    if table is None:
        return []
    ks = table.kinds
    err: list[str] = []

    msg = _slug_msg(ks.get("country"), entry_country_slug(entry), path="country.data")
    if msg:
        err.append(msg)

    try:
        coll = entry_collection_type_data(entry)
    except ValueError:
        err.append("缺少 collection-type")
        return err

    msg = _slug_msg(ks.get("domain"), entry_domain_slug(entry), path="collection-type.data.domain")
    if msg:
        err.append(msg)
    msg = _slug_msg(ks.get("release_type"), entry_release_type_slug(entry), path="collection-type.data.release_type")
    if msg:
        err.append(msg)

    ml_kind = ks.get("marker_label")
    if ml_kind and ml_kind.values:
        mraw = coll.get("markers")
        lst = mraw if isinstance(mraw, list) else []
        for i, lab in enumerate(lst):
            if not isinstance(lab, str):
                err.append(f"collection-type.markers[{i}] 须为字符串")
                continue
            msg = _slug_msg(ml_kind, lab.strip(), path=f"collection-type.markers[{i}]")
            if msg:
                err.append(msg)

    fk = ks.get("press_format")
    gk = ks.get("press_group")
    cel = coll.get("collectioned")
    if isinstance(cel, list) and (fk or gk):
        for i, row in enumerate(cel):
            if not isinstance(row, dict):
                continue
            fm = row.get(TV_JP_PRESS_FORMAT_KEY)
            gp = row.get(TV_JP_PRESS_GROUP_KEY)
            if fk and isinstance(fm, str):
                msg = _slug_msg(fk, fm, path=f"collection-type.collectioned[{i}].{TV_JP_PRESS_FORMAT_KEY}")
                if msg:
                    err.append(msg)
            if gk and isinstance(gp, str):
                msg = _slug_msg(gk, gp, path=f"collection-type.collectioned[{i}].{TV_JP_PRESS_GROUP_KEY}")
                if msg:
                    err.append(msg)

    conts = coll.get("continuations")
    if isinstance(conts, list) and (fk or gk):
        for bi, blk in enumerate(conts):
            if not isinstance(blk, dict):
                continue
            bcel = blk.get("collectioned")
            if not isinstance(bcel, list):
                continue
            for j, row in enumerate(bcel):
                if not isinstance(row, dict):
                    continue
                fm = row.get(TV_JP_PRESS_FORMAT_KEY)
                gp = row.get(TV_JP_PRESS_GROUP_KEY)
                if fk and isinstance(fm, str):
                    msg = _slug_msg(
                        fk,
                        fm,
                        path=(
                            f"collection-type.continuations[{bi}]"
                            f".collectioned[{j}].{TV_JP_PRESS_FORMAT_KEY}"
                        ),
                    )
                    if msg:
                        err.append(msg)
                if gk and isinstance(gp, str):
                    msg = _slug_msg(
                        gk,
                        gp,
                        path=(
                            f"collection-type.continuations[{bi}]"
                            f".collectioned[{j}].{TV_JP_PRESS_GROUP_KEY}"
                        ),
                    )
                    if msg:
                        err.append(msg)

    return err


def symbol_table_to_plain_dict(tab: TvJpSymbolTable) -> dict[str, Any]:
    """与 YAML 输入同构的可序列化 dict。"""
    kinds_o: dict[str, Any] = {}
    for kid, kd in tab.kinds.items():
        vals_o: dict[str, Any] = {}
        for code, vs in kd.values.items():
            if vs.description or vs.extra:
                cell: dict[str, Any] = {}
                if vs.description:
                    cell["description"] = vs.description
                if vs.extra:
                    cell["extra"] = dict(vs.extra)
                vals_o[code] = cell
            else:
                vals_o[code] = {}
        kinds_o[kid] = {
            "label": kd.label,
            "description": kd.description,
            "meta": dict(kd.meta),
            "values": vals_o,
        }
    return {"version": tab.version, "kinds": kinds_o}


def clone_tv_jp_symbol_table(tab: TvJpSymbolTable) -> TvJpSymbolTable:
    """深拷贝为新的不可变结构。"""
    return tv_jp_symbol_table_from_mapping(copy.deepcopy(symbol_table_to_plain_dict(tab)))
