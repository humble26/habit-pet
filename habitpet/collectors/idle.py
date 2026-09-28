"""键鼠空闲检测（Windows GetLastInputInfo）。

只回答"用户最近有没有动键鼠"，不读任何输入内容，零隐私风险。
非 Windows 平台没有现成的输入检测，返回"永远不在座"——
久坐扣血/熬夜掉血/休息回血全部停用（宁可不动手，不可误伤），
git 喂食与数值自然衰减不受影响。
"""
from __future__ import annotations

import os

_ACTIVE_THRESHOLD_SECONDS = 60.0


def _idle_seconds_windows() -> float:
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    delta = (tick - lii.dwTime) & 0xFFFFFFFF   # 处理计数器回绕
    return delta / 1000.0


def idle_seconds() -> float:
    """距离上次键鼠输入的秒数。非 Windows 返回极大值（视为不在座）。"""
    if os.name == "nt":
        return _idle_seconds_windows()
    return 1e9


def is_active(threshold: float = _ACTIVE_THRESHOLD_SECONDS) -> bool:
    """60 秒内动过键鼠即视为"伏案中"。"""
    return idle_seconds() < threshold
