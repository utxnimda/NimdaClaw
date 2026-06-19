"""将 JP TV YAML 条目按 collection-type（domain + release_type）分桶并生成浏览用数据结构。

后续新增 ``animation-tv`` 以外的展示规则：实现
:class:`JpTvBrowsePresenter` 并调用 :func:`register_jp_tv_browse_presenter`。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypedDict


from work_catalog_yaml.jp_tv.validate import (
    TV_JP_PRESS_FORMAT_KEY,
    TV_JP_PRESS_GROUP_KEY,
    TV_JP_PRESS_PATH_KEY,
    JpTvEntry,
    entry_air_dates,
    entry_collection_type_data,
    entry_country_slug,
    entry_display_name,
    entry_domain_slug,
    entry_release_type_slug,
    jp_tv_press_pair_from_row,
    strip_tv_jp_tag_decoration,
)


def jp_tv_collection_type_profile_key(entry: JpTvEntry) -> str:
    """形如 ``animation-tv``，由 ``domain``（大类码）与 ``release_type``（tv/ova/movie…）拼接。"""
    return f"{entry_domain_slug(entry)}-{entry_release_type_slug(entry)}"


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


def _continuations_plain(coll: dict[str, Any]) -> list[dict[str, Any]]:
    raw = coll.get("continuations")
    if not isinstance(raw, list):
        return []
    blocks: list[dict[str, Any]] = []
    for blk in raw:
        if not isinstance(blk, dict):
            continue
        o: dict[str, Any] = {}
        cel = blk.get("collectioned")
        if cel is not None:
            o["collectioned"] = _sanitize_collection_rows(cel)
        if isinstance(blk.get("title"), str) and str(blk["title"]).strip():
            o["title"] = str(blk["title"]).strip()
        if o:
            blocks.append(o)
    return blocks


def build_collectioned_ordered(coll: dict[str, Any]) -> list[dict[str, Any]]:
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


class JpTvBrowsePresenter(ABC):
    """按 profile_key（与 :func:`jp_tv_collection_type_profile_key` 一致）提供卡片字段。"""

    profile_key: str

    @abstractmethod
    def profile_label(self) -> str:
        ...

    @abstractmethod
    def row_payload(
        self,
        entry: JpTvEntry,
        *,
        index_in_file: int,
        yaml_source_rel: str | None = None,
    ) -> dict[str, Any]:
        ...


_REGISTRY: dict[str, JpTvBrowsePresenter] = {}


def register_jp_tv_browse_presenter(p: JpTvBrowsePresenter) -> None:
    _REGISTRY[p.profile_key] = p


class _FallbackJpTvBrowsePresenter(JpTvBrowsePresenter):
    profile_key = "_fallback"

    def profile_label(self) -> str:
        return "通用（未注册的 domain / release_type）"

    def row_payload(
        self,
        entry: JpTvEntry,
        *,
        index_in_file: int,
        yaml_source_rel: str | None = None,
    ) -> dict[str, Any]:
        return minimal_jp_tv_browse_row_payload(
            entry,
            index_in_file=index_in_file,
            yaml_source_rel=yaml_source_rel,
        )


class AnimationTvJpBrowsePresenter(JpTvBrowsePresenter):
    profile_key = "animation-tv"

    def profile_label(self) -> str:
        return "动画 · TV（animation / tv，作品形态归类）"

    def row_payload(
        self,
        entry: JpTvEntry,
        *,
        index_in_file: int,
        yaml_source_rel: str | None = None,
    ) -> dict[str, Any]:
        return minimal_jp_tv_browse_row_payload(
            entry,
            index_in_file=index_in_file,
            yaml_source_rel=yaml_source_rel,
        )


def minimal_jp_tv_browse_row_payload(
    entry: JpTvEntry,
    *,
    index_in_file: int,
    yaml_source_rel: str | None = None,
) -> dict[str, Any]:
    """各 Presenter 可复用的基础字段：date、collections、country、name（及可选 markers/continuations）。"""
    date_block: dict[str, str] = {"start": "", "end": ""}
    try:
        ds, de = entry_air_dates(entry)
        date_block["start"], date_block["end"] = ds, de
    except ValueError:
        pass

    try:
        coll = entry_collection_type_data(entry)
    except ValueError:
        coll = {}

    markers_raw = coll.get("markers") if isinstance(coll.get("markers"), list) else []
    markers_s = [str(x) for x in markers_raw if isinstance(x, str)]

    ordered = build_collectioned_ordered(coll)

    return {
        "index_in_file": index_in_file,
        "yaml_source_rel": yaml_source_rel or "",
        "profile_key_at_row": jp_tv_collection_type_profile_key(entry),
        "date": date_block,
        "country": entry_country_slug(entry),
        "name": entry_display_name(entry),
        "collectioned": _sanitize_collection_rows(coll.get("collectioned")),
        "collectioned_ordered": ordered,
        "path": str(coll.get("path") or ""),
        "markers": markers_s,
        "continuations": _continuations_plain(coll),
        "domain": entry_domain_slug(entry),
        "release_type": entry_release_type_slug(entry),
    }


_FALLBACK_PRESENT = _FallbackJpTvBrowsePresenter()


def jp_tv_browse_presenter_for(profile_key: str) -> JpTvBrowsePresenter:
    return _REGISTRY.get(profile_key) or _FALLBACK_PRESENT


class BrowsePayload(TypedDict, total=False):
    """``build_jp_tv_browse_payload`` 的根对象（JSON 友好）。"""

    ok: bool
    error: str
    profile_groups: list[dict[str, Any]]
    counts_by_profile: dict[str, int]
    filename: str | None


register_jp_tv_browse_presenter(AnimationTvJpBrowsePresenter())


def build_jp_tv_browse_payload(
    works: list[JpTvEntry],
    *,
    row_meta: list[tuple[str | None, int]] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """按 profile 分组；组顺序为 profile_key 字典序。

    ``row_meta`` 与 ``works`` 同长；（``yaml_source_rel``, ``index_in_file``）；缺省等价于单列无来源后缀、序号为全局下标。
    """
    if not isinstance(works, list):
        return {"ok": False, "error": "works 须为条目列表", "filename": filename}

    if row_meta is None:
        row_meta = [(None, gi) for gi in range(len(works))]
    if len(row_meta) != len(works):
        return {
            "ok": False,
            "error": "row_meta 长度须与 works 一致",
            "filename": filename,
        }

    buckets: dict[str, list[tuple[int, JpTvEntry]]] = {}
    for i, entry in enumerate(works):
        pk = jp_tv_collection_type_profile_key(entry)
        buckets.setdefault(pk, []).append((i, entry))

    profile_order = sorted({jp_tv_collection_type_profile_key(w) for w in works})

    counts_by_profile: dict[str, int] = {}
    profile_groups: list[dict[str, Any]] = []

    for pk in profile_order:
        pairs = buckets.get(pk, [])
        counts_by_profile[pk] = len(pairs)

        presenter = jp_tv_browse_presenter_for(pk)
        explicitly_registered = pk in _REGISTRY
        label = presenter.profile_label()
        if not explicitly_registered:
            label = f"{label} · 「{pk}」"

        rows_payload = []
        for gidx, ent in pairs:
            yrel, iif = row_meta[gidx]
            rows_payload.append(
                presenter.row_payload(
                    ent,
                    index_in_file=iif,
                    yaml_source_rel=yrel,
                ),
            )

        profile_groups.append(
            {
                "profile_key": pk,
                "registered_profile": explicitly_registered,
                "profile_label": label,
                "presenter_key": presenter.profile_key,
                "rows": rows_payload,
            }
        )

    return {
        "ok": True,
        "filename": filename,
        "profile_groups": profile_groups,
        "counts_by_profile": counts_by_profile,
        "total": len(works),
    }


def build_jp_tv_browse_payload_single_group_order(
    works: list[JpTvEntry],
    *,
    row_meta: list[tuple[str | None, int]] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """与 :func:`build_jp_tv_browse_payload` 相同数据，但 ``profile_groups`` 按文件中 profile **首次出现**排序。"""
    base = build_jp_tv_browse_payload(works, row_meta=row_meta, filename=filename)
    if not base.get("ok"):
        return base

    first_pos: dict[str, int] = {}
    for i, w in enumerate(works):
        pk = jp_tv_collection_type_profile_key(w)
        if pk not in first_pos:
            first_pos[pk] = i

    groups = list(base.get("profile_groups") or [])
    groups.sort(key=lambda g: first_pos.get(str(g.get("profile_key")), 10**9))

    nb = dict(base)
    nb["profile_groups"] = groups
    return nb
