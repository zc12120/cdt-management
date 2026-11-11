"""Daily keepalive windows, with overnight periods owned by their start date."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
import datetime as dt
import re
from typing import Any


@dataclass(frozen=True)
class KeepaliveWindow:
    start_time: str = ""
    end_time: str = ""
    start_day: int = 0
    end_day: int = 0

    @classmethod
    def parse(cls, payload: dict[str, Any], *, strict: bool = True) -> KeepaliveWindow:
        """Validate API input; tolerate damaged stored dimensions when evaluating."""
        try:
            start = str(payload.get("start_time") or "").strip()
            end = str(payload.get("end_time") or "").strip()
            if bool(start) != bool(end):
                raise ValueError("运行时段的起止时刻要成对填写，或都留空（全天）")
            if start:
                if any(
                    not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
                    for value in (start, end)
                ):
                    raise ValueError("运行时段要用 HH:MM 格式，例如 08:00")
                if start == end:
                    raise ValueError("运行时段的起止时刻不能相同；全天运行请选「不限」")
        except ValueError:
            if strict:
                raise
            start = end = ""

        try:
            try:
                first = int(payload.get("start_day") or 0)
                last = int(payload.get("end_day") or 0)
            except (TypeError, ValueError):
                raise ValueError("运行日期必须是 1–31 的数字") from None
            if bool(first) != bool(last):
                raise ValueError("运行日期的起止日要成对填写，或都留空（全月）")
            if first or last:
                if not (1 <= first <= 31 and 1 <= last <= 31):
                    raise ValueError("运行日期必须在 1–31 之间")
                if last < first:
                    raise ValueError("运行日期的结束日不能早于开始日（不支持跨月）")
        except ValueError:
            if strict:
                raise
            first = last = 0
        return cls(start, end, first, last)

    @property
    def overnight(self) -> bool:
        # Midnight remains the end of the current day, as in existing schedules.
        return bool(
            self.start_time
            and self.end_time != "00:00"
            and self.end_time < self.start_time
        )

    @property
    def summary(self) -> str:
        parts = []
        if self.start_time:
            end = "24:00" if self.end_time == "00:00" else self.end_time
            if self.overnight:
                end = "次日 " + end
            parts.append(f"每天 {self.start_time}–{end}")
        if self.start_day:
            suffix = "（按开始日期）" if self.overnight else ""
            parts.append(f"{self.start_day}–{self.end_day} 日{suffix}")
        return " · ".join(parts)

    def contains(self, now: dt.datetime) -> bool:
        """Start is inclusive, end exclusive; early hours belong to yesterday."""
        anchor = now.date()
        if self.start_time:
            minute = now.hour * 60 + now.minute
            start = int(self.start_time[:2]) * 60 + int(self.start_time[3:])
            end = int(self.end_time[:2]) * 60 + int(self.end_time[3:])
            if self.overnight:
                if not (minute >= start or minute < end):
                    return False
                if minute < end:
                    anchor -= dt.timedelta(days=1)
            else:
                end = end or 24 * 60
                if not start <= minute < end:
                    return False
        if self.start_day:
            days = calendar.monthrange(anchor.year, anchor.month)[1]
            return min(self.start_day, days) <= anchor.day <= min(self.end_day, days)
        return True
