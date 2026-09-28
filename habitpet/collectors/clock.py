"""时钟判定：熬夜时段。"""
from __future__ import annotations

import datetime as dt


def is_late_night(now: dt.datetime, start_hour: int, end_hour: int) -> bool:
    """判断是否处于熬夜时段（支持跨午夜区间，如 0-6 点）。"""
    start = start_hour % 24
    end = end_hour % 24
    h = now.hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end
