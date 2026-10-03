"""Broadcast date formatting without guessing unknown calendar components.

Storage uses eight-character YYYYMMDD strings; 00 and X placeholders from the
legacy catalog remain intact. Blank dates stay blank. Calendar checking is
opt-in so reading an old catalog does not fail because of a historical typo.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any


_COMPACT = re.compile(r"[0-9X]{8}", re.ASCII)
_SEPARATED = re.compile(r"([0-9X]{4})([-/])([0-9X]{1,2})\2([0-9X]{1,2})", re.ASCII)


def normalize_air_date(value: Any, *, validate_calendar: bool = False) -> str:
    """Return the lossless compact form, or reject an ambiguous input format."""
    if value is None:
        return ""
    if isinstance(value, datetime) or isinstance(value, bool):
        raise ValueError("播出日期必须为 YYYY-MM-DD 或 YYYYMMDD，不能包含时间")
    if isinstance(value, date):
        raw = f"{value.year:04d}{value.month:02d}{value.day:02d}"
    elif isinstance(value, (str, int)):
        raw = str(value).strip().upper()
    else:
        raise ValueError("播出日期必须为 YYYY-MM-DD 或 YYYYMMDD")
    if not raw:
        return ""
    separated = _SEPARATED.fullmatch(raw)
    if separated:
        year, _, month, day = separated.groups()
        raw = year + month.zfill(2) + day.zfill(2)
    if _COMPACT.fullmatch(raw) is None:
        raise ValueError(f"播出日期格式不正确：{value!s}；请填写 YYYY-MM-DD 或 YYYYMMDD")
    if validate_calendar:
        year, month, day = raw[:4], raw[4:6], raw[6:8]
        known_year = int(year) if year.isdigit() and int(year) else None
        known_month = int(month) if month.isdigit() and int(month) else None
        known_day = int(day) if day.isdigit() and int(day) else None
        if known_month is not None and not 1 <= known_month <= 12:
            raise ValueError(f"播出日期月份不正确：{value!s}")
        if known_day is not None and not 1 <= known_day <= 31:
            raise ValueError(f"播出日期日期不正确：{value!s}")
        if known_month is not None and known_day is not None:
            try:
                # A leap year permits Feb 29 when the year is unknown.
                date(known_year or 2000, known_month, known_day)
            except ValueError as error:
                raise ValueError(f"播出日期不是有效的日历日期：{value!s}") from error
    return raw
