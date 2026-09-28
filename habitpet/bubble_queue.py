"""事件气泡排队：保证每条消息完整展示，不被后来的消息立刻冲掉。"""


class BubbleQueue:
    def __init__(self, capacity: int = 4) -> None:
        self.capacity = capacity
        self.pending: list[tuple[str, float]] = []
        self.current: tuple[str, float] | None = None
        self.showing = False

    def push(self, text: str, secs: float = 6.0) -> bool:
        """入队。返回 True 表示当前空闲、需要窗口立即显示这条。"""
        if self.showing:
            if len(self.pending) >= self.capacity:
                self.pending.pop(0)          # 挤掉最老的积压
            self.pending.append((text, secs))
            return False
        self.showing = True
        self.current = (text, secs)
        return True

    def on_hidden(self) -> tuple[str, float] | None:
        """当前气泡已隐藏：取下一条继续显示，没有则回到空闲态。"""
        if self.pending:
            self.current = self.pending.pop(0)
            return self.current
        self.current = None
        self.showing = False
        return None
