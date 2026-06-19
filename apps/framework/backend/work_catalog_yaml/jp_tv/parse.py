"""将 [JP][TVInfo][年].txt 正文解析为「作品条目」并写入 YAML。

- 会自动去掉 UTF-8 BOM；整行 ``// …`` 视为注释跳过。
- 主行行首为 ``[YYYYMMDD][YYYYMMDD]``；``[____MS]`` 为收集标记；其余 bracket 内文前导 ``_`` / ``-`` 会去掉后再写入 collectioned。
- 条目形态见 ``samples/tv-jp.yaml``：``collection-type.data`` 为 ``domain``（字符串大类）、``release_type``、``collectioned``、``markers``、可选 ``continuations``。
- YAML 根为作品数组；``parse-batch`` 默认写入 ``data/features/collection-detail/db/<源 txt 文件名>.yaml``。
"""
from __future__ import annotations

import re
from pathlib import Path

from work_catalog_yaml.jp_tv.validate import (
    JpTvEntry,
    append_collection_continuation,
    build_tv_jp_entry_attributes,
    jp_tv_txt_relpath_from_entry,
    jp_tv_works_to_plain_list,
    merge_tv_jp_collect_markers,
)
from work_catalog_yaml.layout import default_source_data_dir, workspace_parsed_yaml_dir
from work_catalog_yaml.yaml_io import dump_yaml_string

JP_TVINFO_FILENAME_MARK = "[JP][TVInfo]"

_PRIMARY = re.compile(r"^\[(\d{8})\]\[(\d{8})\]\s*(.*)$")
_CONT_WITH_PIPE = re.compile(r"^\s+(.*?)\s*\|\s*(.*)$")
_CONT_TAGS_ONLY = re.compile(r"^\s+((?:\[[^\]]*\])+)\s*$")
_BRACKET_TOKEN = re.compile(r"\[([^\]]*)\]")
_LEGACY_FIXED_SPLIT = re.compile(r"\s{2,}")

_TV_JP_COLLECT_UNDER = 4
_TV_JP_COLLECT_CODE_LABEL = {"M": "music", "S": "subs"}


def _extract_brackets(tag_section: str) -> list[str]:
    return _BRACKET_TOKEN.findall(tag_section)


class JpTvParseError(ValueError):
    def __init__(self, msg: str, *, line_no: int | None = None) -> None:
        pre = f"第 {line_no} 行: " if line_no is not None else ""
        super().__init__(pre + msg)
        self.line_no = line_no


def _expand_tv_jp_collect_marker_inner(inner: str) -> list[str] | None:
    """[____MS] 内文____MS → music, subs；否则非收集标记括号，返回 None。"""
    mo = re.match(r"^_+", inner)
    if not mo or len(mo.group(0)) < _TV_JP_COLLECT_UNDER:
        return None
    suf = inner[len(mo.group(0)) :]
    if not suf or not re.fullmatch(r"[MS_]+", suf):
        return None
    out: list[str] = []
    for ch in suf:
        if ch == "_":
            continue
        lbl = _TV_JP_COLLECT_CODE_LABEL.get(ch)
        if lbl and lbl not in out:
            out.append(lbl)
    return out or None


def partition_collect_markers(tag_section: str) -> tuple[list[tuple[str, str]] | None, list[str]]:
    """从 tag 区段拆出 bracket 对（collectioned）与收集标记（markers）。"""
    tokens = _extract_brackets(tag_section)
    markers: list[str] = []
    plain: list[str] = []
    for t in tokens:
        expanded = _expand_tv_jp_collect_marker_inner(t)
        if expanded is not None:
            for L in expanded:
                if L not in markers:
                    markers.append(L)
        else:
            plain.append(t)
    if not plain:
        return None, markers
    if len(plain) % 2 != 0:
        plain.append("")
    pairs_out = [(plain[i], plain[i + 1]) for i in range(0, len(plain), 2)]
    return pairs_out, markers


def _partition_tag_section(tag_section: str) -> tuple[list[tuple[str, str]] | None, list[str]]:
    """解析 bracket 标签；若旧格式只给出裸格式名，则按空组收进 collectioned。"""
    tag_s = tag_section.strip()
    pairs, markers = partition_collect_markers(tag_s)
    if pairs or markers or not tag_s:
        return pairs, markers
    return [(tag_s, "")], []


def _split_legacy_fixed_tail(tail: str) -> tuple[str, str | None]:
    """兼容早期固定列：``格式`` 与 ``标题`` 之间用多空格分隔，无 ``|``。"""
    stripped = tail.strip()
    if not stripped or "[" in stripped:
        return tail.rstrip(), None
    left_right = _LEGACY_FIXED_SPLIT.split(stripped, maxsplit=1)
    if len(left_right) != 2:
        return "", stripped
    return left_right[0].strip(), left_right[1].strip()


def _parse_primary_tail(tail: str) -> tuple[list[tuple[str, str]] | None, list[str], str]:
    if "|" in tail:
        tag_part, rhs = tail.split("|", 1)
        pairs, markers = _partition_tag_section(tag_part.strip())
        return pairs, markers, rhs.lstrip().rstrip()

    tag_part, title = _split_legacy_fixed_tail(tail)
    pairs, markers = _partition_tag_section(tag_part.strip())
    return pairs, markers, title or ""


def parse_jp_tv_txt_lines(lines: list[str]) -> list[JpTvEntry]:
    entries_out: list[JpTvEntry] = []
    current: JpTvEntry | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        entries_out.append(current)
        current = None

    for line_no, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.lstrip().startswith("//"):
            continue

        pm = _PRIMARY.match(line)
        if pm:
            flush()
            start_s, end_s, tail = pm.group(1), pm.group(2), pm.group(3)
            pairs, markers, title = _parse_primary_tail(tail)
            current = JpTvEntry(
                attributes=build_tv_jp_entry_attributes(
                    start_s, end_s, title=title, pairs=pairs, markers=markers,
                ),
            )
            continue

        if current is None:
            raise JpTvParseError(
                "缺少主行的续行片段（应在行首出现 [YYYYMMDD][YYYYMMDD] …）",
                line_no=line_no,
            )

        wm = _CONT_WITH_PIPE.match(line)
        if wm:
            tag_part = wm.group(1).strip()
            ttitle = wm.group(2).strip()
            pairs, extra_m = _partition_tag_section(tag_part.strip())
            current = append_collection_continuation(
                current, tag_pairs=pairs, title=ttitle or None
            )
            if extra_m:
                current = merge_tv_jp_collect_markers(current, extra_m)
            continue

        tm = _CONT_TAGS_ONLY.match(line)
        if tm:
            pairs, extra_m = partition_collect_markers(tm.group(1))
            current = append_collection_continuation(current, tag_pairs=pairs, title=None)
            if extra_m:
                current = merge_tv_jp_collect_markers(current, extra_m)
            continue

        raise JpTvParseError(f"无法识别的行格式: {line!r}", line_no=line_no)

    flush()
    return entries_out


def parse_jp_tv_txt_content(text: str) -> list[JpTvEntry]:
    text = text.replace("\r\n", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    return parse_jp_tv_txt_lines(text.split("\n"))


def parse_jp_tv_txt_file(path: str | Path) -> list[JpTvEntry]:
    """解析 txt 为作品列表（tv-jp 属性块）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return parse_jp_tv_txt_content(text)


def _empty_entry_for_path_infer() -> JpTvEntry:
    return JpTvEntry(attributes=build_tv_jp_entry_attributes("00000000", "00000000"))


def infer_txt_relpath_for_materialize(entries: list[JpTvEntry], yaml_path: str | Path) -> str:
    """根据首条 collection-type + country + yaml 文件名推断 Data 下 txt 相对路径。"""
    name = Path(yaml_path).with_suffix(".txt").name
    if entries:
        return jp_tv_txt_relpath_from_entry(entries[0], name)
    return jp_tv_txt_relpath_from_entry(_empty_entry_for_path_infer(), name)


def iter_jp_tvinfo_txt_files(txt_root: Path) -> list[Path]:
    root = txt_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"txt 根目录不存在: {root}")
    return sorted(p for p in root.rglob("*.txt") if JP_TVINFO_FILENAME_MARK in p.name)


def parse_jp_tv_batch(
    txt_root: Path | None = None,
    yaml_root: Path | None = None,
    *,
    force: bool = False,
) -> tuple[list[Path], list[Path]]:
    tr = (txt_root or default_source_data_dir()).resolve()
    yr = (yaml_root or workspace_parsed_yaml_dir()).resolve()

    targets = iter_jp_tvinfo_txt_files(tr)
    written: list[Path] = []
    skipped: list[Path] = []

    for src in targets:
        dest = yr / Path(src.name).with_suffix(".yaml")
        if dest.exists() and not force:
            skipped.append(dest)
            continue
        entries = parse_jp_tv_txt_file(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(dump_yaml_string(jp_tv_works_to_plain_list(entries)), encoding="utf-8")
        written.append(dest)

    return written, skipped
