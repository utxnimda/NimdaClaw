from __future__ import annotations

from typing import Literal, TypedDict, Union

from work_catalog_yaml.jp_tv.validate import (
    JpTvEntry,
    _collectioned_to_tag_pairs,
    entry_air_dates,
    entry_collection_type_data,
    entry_display_name,
)


class PipeRow(TypedDict):
    kind: Literal["pipe"]
    before: str
    title: str


class TagsRow(TypedDict):
    kind: Literal["tags"]
    line: str


Row = Union[PipeRow, TagsRow]


def render_tag_pairs(pairs: list[tuple[str, str]] | None) -> str:
    if not pairs:
        return ""
    return "".join(f"[{a}][{b}]" for a, b in pairs)


_COLLECT_MARKER_RENDER_CODE = {"music": "M", "subs": "S"}


def render_collect_markers(markers: object) -> str:
    if not isinstance(markers, list):
        return ""
    codes: list[str] = []
    for marker in markers:
        if not isinstance(marker, str):
            continue
        code = _COLLECT_MARKER_RENDER_CODE.get(marker.strip())
        if code and code not in codes:
            codes.append(code)
    return f"[____{''.join(codes)}]" if codes else ""


def render_jp_tv_text(entries: list[JpTvEntry]) -> str:
    sequence: list[Row] = []

    for entry in entries:
        bd_s, bd_e = entry_air_dates(entry)
        coll = entry_collection_type_data(entry)
        main_pairs = _collectioned_to_tag_pairs(coll.get("collectioned"))
        prefix = f"[{bd_s}][{bd_e}]"
        main_before = prefix + render_tag_pairs(main_pairs) + render_collect_markers(coll.get("markers"))
        title = entry_display_name(entry)
        sequence.append(PipeRow(kind="pipe", before=main_before, title=title))

        for blk in coll.get("continuations") or []:
            if not isinstance(blk, dict):
                continue
            tail = render_tag_pairs(_collectioned_to_tag_pairs(blk.get("collectioned")))
            ind = " " * len(prefix) + tail
            tpart = blk.get("title")
            ttitle = str(tpart).strip() if isinstance(tpart, str) else ""
            if ttitle:
                sequence.append(PipeRow(kind="pipe", before=ind, title=ttitle))
            else:
                sequence.append(TagsRow(kind="tags", line=ind))

    pipe_befores = [r["before"] for r in sequence if r["kind"] == "pipe"]
    max_before = max(pipe_befores, key=len) if pipe_befores else ""
    max_len = len(max_before)

    out_lines: list[str] = []
    for row in sequence:
        if row["kind"] == "tags":
            out_lines.append(row["line"])
            continue
        assert row["kind"] == "pipe"
        before = row["before"]
        ttitle = row["title"]
        pad = max(1, max_len - len(before) + 1)
        out_lines.append(f"{before}{' ' * pad}| {ttitle}")

    body = "\n".join(out_lines)
    return body + "\n" if body else ""
