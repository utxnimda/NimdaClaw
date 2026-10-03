"""Small boundary validators shared by catalog editing workflows."""
from __future__ import annotations

import re
import sys
from typing import Any


def parse_record_index(value: Any, *, label: str = "index_in_file") -> int:
    """Accept an integer identity without truncating floats or accepting booleans."""
    if isinstance(value, bool):
        raise ValueError(f"{label} 非法：须为非负整数")
    if isinstance(value, int):
        index = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        try:
            index = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label} 非法：整数超出范围") from exc
    else:
        raise ValueError(f"{label} 非法：须为非负整数")
    if not 0 <= index <= sys.maxsize:
        raise ValueError(f"{label} 非法：整数超出范围")
    return index
