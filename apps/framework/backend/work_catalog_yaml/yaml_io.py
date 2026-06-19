"""使用 ruamel.yaml 统一读写，便于控制缩进与行宽等业务相关 dump 行为。"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import IO, Any

from ruamel.yaml import YAML


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
    y = _yaml_reader()
    if isinstance(path, (str, Path)):
        with Path(path).open(encoding="utf-8") as fp:
            return y.load(fp)
    return y.load(path)


def load_yaml_string(source: str) -> Any:
    """从 UTF-8 文本解析 YAML（多用于上传 / 测试中）。"""
    return _yaml_reader().load(StringIO(source))


def dump_yaml_string(data: Any) -> str:
    buf = StringIO()
    _yaml_writer().dump(data, buf)
    text = buf.getvalue()
    # ruamel 常以换行结尾；与原先 PyYAML 习惯一致
    if text and not text.endswith("\n"):
        text += "\n"
    return text
