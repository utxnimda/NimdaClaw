"""使用 ruamel.yaml 统一读写，便于控制缩进与行宽等业务相关 dump 行为。"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import IO, Any

from ruamel.yaml import YAML

from work_catalog_yaml.yaml_cache import YamlParseCache


_PARSED_YAML_CACHE = YamlParseCache()


def _yaml_reader() -> YAML:
    y = YAML(typ="safe")
    return y


def _yaml_writer() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.allow_unicode = True
    # 避免在极长行上过度折行；大块文本使用 literal block 更清晰
    y.width = 10_000_000
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def load_yaml(path: str | Path | IO[str]) -> Any:
    if isinstance(path, (str, Path)):
        # Always read current content, even on a cache hit. Path/mtime caches can
        # miss same-size edits or atomic replacements with preserved timestamps.
        with Path(path).open(encoding="utf-8") as fp:
            source = fp.read()
        return _load_yaml_content(source, name=str(path))
    # Preserve caller-owned stream position, lifetime and parser diagnostics.
    return _yaml_reader().load(path)


def _load_yaml_content(source: str, *, name: str | None = None) -> Any:
    def parse() -> Any:
        stream = StringIO(source)
        if name is not None:
            stream.name = name
        return _yaml_reader().load(stream)

    return _PARSED_YAML_CACHE.parse(source, parse)


def load_yaml_string(source: str) -> Any:
    """从 UTF-8 文本解析 YAML（多用于上传 / 测试中）。"""
    return _load_yaml_content(source)


def dump_yaml_string(data: Any) -> str:
    buf = StringIO()
    _yaml_writer().dump(data, buf)
    text = buf.getvalue()
    # ruamel 常以换行结尾；与原先 PyYAML 习惯一致
    if text and not text.endswith("\n"):
        text += "\n"
    return text
