"""专注模式（番茄钟）。

设计：专注期间的连续伏案不再扣健康（这是"正事"，不是久坐），
计时只累计"在座"时间——离开键盘超过容忍期自动暂停，回来自动继续；
完成一颗番茄发心情奖励。鲸鱼娘全程在旁边盯着。
"""
from __future__ import annotations


class FocusSession:
    """一次专注会话。elapsed 只在键鼠活跃时累计（暂停 = 停止累计）。"""

    __slots__ = ("total_seconds", "elapsed", "paused")

    def __init__(self, minutes: float) -> None:
        self.total_seconds = float(minutes) * 60
        self.elapsed = 0.0
        self.paused = False

    def remaining_minutes(self) -> float:
        return max(0.0, (self.total_seconds - self.elapsed) / 60)

    @property
    def finished(self) -> bool:
        return self.elapsed >= self.total_seconds
