"""桌宠窗口：tkinter 透明置顶窗 + 程序化绘制的橘猫。

- 猫是 canvas 按设计坐标画出来的（无外部美术资产，版权干净），
  绘制以 (85, 100) 为缩放中心，支持成长阶段的整体缩放。
- 气泡走 BubbleQueue 排队，事件不会被后来的消息立刻冲掉。
"""
from __future__ import annotations

import math
import tkinter as tk

from .bubble_queue import BubbleQueue
from .theme import (BELLY, BLUSH, BODY, BODY_DARK, FONT, LINE, PINK,
                    SICK_BODY)

W, H = 240, 340
PET_OX, PET_OY = 35, 165          # 猫的绘制原点（气泡区在上方）
TRANSPARENT = "#010101"           # 透明色键：猫不会用到的颜色

MENU_TOP = [
    ("今天过得怎么样", "status"),
    ("我的成就", "achievements"),
]
MENU_BOTTOM = [
    ("手动喂一包粮", "treat"),
    ("来句吐槽", "roast"),
    ("生成本周周报", "weekly"),
    ("生成分享卡", "share_card"),
    ("打开数据文件夹", "open_folder"),
]


class PetWindow(tk.Tk):
    """把主窗口本身做成桌宠。callbacks 由 HabitPetApp 注入。"""

    def __init__(self, callbacks: dict):
        super().__init__()
        self.callbacks = callbacks
        self.title("Habit Pet")
        self.geometry(f"{W}x{H}")
        self.configure(bg=TRANSPARENT)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:      # 非 Windows 没有透明色键，退化为不透明小窗
            self.configure(bg="#fff8ef")

        self.canvas = tk.Canvas(self, width=W, height=H, bg=TRANSPARENT,
                                highlightthickness=0)
        self.canvas.pack()

        self.bubble = tk.Label(
            self, text="", bg="#fffdf0", fg="#4a3200", wraplength=W - 24,
            justify="left", font=(FONT, 10), relief="solid", bd=1,
        )
        self.bq = BubbleQueue()
        self._bubble_job = None

        # 拖拽 & 撸猫
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self._drag_from = None
        self._moved = False

        self.menu = tk.Menu(self, tearoff=0)
        for label, key in MENU_TOP:
            self.menu.add_command(label=label, command=lambda k=key: self._cb(k))
        # 专注菜单项标签动态变化（开始/结束 + 剩余时间）
        self.menu.add_command(label="开始专注", command=lambda: self._cb("focus_toggle"))
        self._focus_idx = self.menu.index("end")
        for label, key in MENU_BOTTOM:
            self.menu.add_command(label=label, command=lambda k=key: self._cb(k))
        self._autostart_idx = None
        try:
            self.menu.add_separator()
            self.menu.add_command(label="开机自启",
                                  command=lambda: self._cb("autostart_toggle"))
            self._autostart_idx = self.menu.index("end")
        except tk.TclError:
            pass
        self.menu.add_command(label="退出", command=lambda: self._cb("quit"))

    def _cb(self, key: str):
        fn = self.callbacks.get(key)
        return fn() if fn else None

    # ------------------------------------------------------------------ 气泡

    def show_bubble(self, text: str, secs: float = 6.0) -> None:
        if self.bq.push(text, secs):
            self._display_bubble(self.bq.current)

    def _display_bubble(self, item: tuple[str, float]) -> None:
        text, secs = item
        if self._bubble_job:
            self.after_cancel(self._bubble_job)
        self.bubble.config(text=text)
        self.bubble.place(x=10, y=4, width=W - 20)
        self._bubble_job = self.after(int(secs * 1000), self._bubble_timeout)

    def _bubble_timeout(self) -> None:
        self.bubble.place_forget()
        self._bubble_job = None
        nxt = self.bq.on_hidden()
        if nxt:
            self._display_bubble(nxt)

    # ------------------------------------------------------------------ 交互

    def _on_press(self, e):
        self._drag_from = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y())
        self._moved = False

    def _on_motion(self, e):
        if not self._drag_from:
            return
        sx, sy, wx, wy = self._drag_from
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) + abs(dy) > 4:
            self._moved = True
            self.geometry(f"+{wx + dx}+{wy + dy}")

    def _on_release(self, e):
        if self._drag_from and not self._moved:
            self._cb("pet")
        self._drag_from = None

    def _on_menu(self, e):
        if self._focus_idx is not None and self.callbacks.get("focus_state"):
            self.menu.entryconfigure(self._focus_idx,
                                     label=str(self.callbacks["focus_state"]()))
        if (self._autostart_idx is not None
                and self.callbacks.get("autostart_state")):
            enabled = bool(self.callbacks["autostart_state"]())
            self.menu.entryconfigure(
                self._autostart_idx,
                label="开机自启：开" if enabled else "开机自启：关")
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def save_position(self) -> tuple[int, int]:
        return self.winfo_x(), self.winfo_y()

    def place_default(self) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # 底部留出任务栏高度，避免窗口下缘被遮挡
        self.geometry(f"+{sw - W - 30}+{sh - H - 80}")

    # ------------------------------------------------------------------ 绘制

    def render(self, expr: str, frame: int, scale: float = 1.0,
               focus: dict | None = None) -> None:
        c = self.canvas
        c.delete("all")
        ox, oy = PET_OX, PET_OY
        if expr == "runaway":
            self._draw_runaway(c, ox, oy, scale)
            return

        # 以 (85, 100) 为中心把设计坐标映射到缩放后的画布坐标
        cx, cy = ox + 85, oy + 100
        def P(x: float, y: float):
            return (cx + (x - 85) * scale, cy + (y - 100) * scale)

        bob = int(math.sin(frame / 5) * 3) if expr == "happy" else \
              int(math.sin(frame / 9) * 2)
        body_color = SICK_BODY if expr == "sick" else BODY
        lw = max(1, round(2 * scale))

        # 尾巴（摇摆）
        wag = int(math.sin(frame / 6) * 16)
        c.create_arc([*P(4, 38 + bob), *P(48, 98 + bob)],
                     start=95 + wag, extent=130, style="arc",
                     outline=BODY_DARK, width=6)

        # 耳朵 + 内耳
        c.create_polygon([*P(50, 66 + bob), *P(40, 20 + bob), *P(86, 44 + bob)],
                         fill=body_color, outline=LINE, width=lw)
        c.create_polygon([*P(120, 66 + bob), *P(130, 20 + bob), *P(84, 44 + bob)],
                         fill=body_color, outline=LINE, width=lw)
        c.create_polygon([*P(54, 56 + bob), *P(49, 32 + bob), *P(74, 44 + bob)],
                         fill=PINK, outline="")
        c.create_polygon([*P(116, 56 + bob), *P(121, 32 + bob), *P(96, 44 + bob)],
                         fill=PINK, outline="")

        # 身体 + 肚皮 + 爪子
        c.create_oval([*P(38, 50 + bob), *P(132, 152 + bob)],
                      fill=body_color, outline=LINE, width=lw)
        c.create_oval([*P(62, 102 + bob), *P(108, 148 + bob)],
                      fill=BELLY, outline="")
        c.create_oval([*P(56, 138 + bob), *P(84, 154 + bob)],
                      fill=body_color, outline=LINE, width=lw)
        c.create_oval([*P(86, 138 + bob), *P(114, 154 + bob)],
                      fill=body_color, outline=LINE, width=lw)

        # 胡须
        for dy in (-3, 3):
            c.create_line([*P(44, 96 + bob + dy), *P(12, 90 + bob + dy * 2)],
                          fill=LINE, width=1)
            c.create_line([*P(126, 96 + bob + dy), *P(158, 90 + bob + dy * 2)],
                          fill=LINE, width=1)

        self._draw_face(c, P, bob, expr, frame, scale)

        if expr == "sleepy":
            c.create_text(*P(138, 34 - (frame % 40) / 3), text="Z",
                          font=(FONT, 12, "bold"), fill=LINE)
            c.create_text(*P(152, 20 - (frame % 40) / 3), text="z",
                          font=(FONT, 9), fill=LINE)
        if expr == "hungry":
            c.create_text(ox + 85, oy + 2, text="（粮缸已空，求 commit）",
                          font=(FONT, 9), fill=LINE)
        if expr == "focus":
            if focus and focus.get("paused"):
                txt = "专注暂停（离开键盘太久）"
            else:
                txt = f"专注中 · 剩 {focus.get('left_min', '?')} 分钟"
            c.create_text(ox + 85, oy + 2, text=txt,
                          font=(FONT, 9, "bold"), fill=LINE)
        if expr == "happy" and frame % 90 < 45:
            c.create_text(*P(134, 40 + bob), text="♥",
                          font=(FONT, 13), fill="#ff7f7f")

    def _draw_face(self, c, P, bob, expr, frame, scale) -> None:
        lx, rx = 68, 102
        ly = 84 + bob
        blink = (frame % 52) < 3
        ew = max(1, round(2 * scale))
        if expr == "sick":
            for ex in (lx, rx):
                c.create_line([*P(ex - 5, ly - 5), *P(ex + 5, ly + 5)],
                              fill=LINE, width=ew)
                c.create_line([*P(ex - 5, ly + 5), *P(ex + 5, ly - 5)],
                              fill=LINE, width=ew)
        elif expr == "sleepy" or blink:
            for ex in (lx, rx):
                c.create_line([*P(ex - 6, ly), *P(ex + 6, ly)], fill=LINE, width=ew)
        elif expr == "happy":
            for ex in (lx, rx):
                c.create_arc([*P(ex - 7, ly - 6), *P(ex + 7, ly + 6)],
                             start=200, extent=140, style="arc",
                             outline=LINE, width=3)
        elif expr == "sad":
            for ex in (lx, rx):
                c.create_oval([*P(ex - 5, ly - 4), *P(ex + 5, ly + 6)],
                              fill=LINE, outline="")
                c.create_line([*P(ex - 6, ly - 8), *P(ex + 6, ly - 5)],
                              fill=LINE, width=ew)
        else:
            for ex in (lx, rx):
                c.create_oval([*P(ex - 5, ly - 6), *P(ex + 5, ly + 4)],
                              fill=LINE, outline="")
                c.create_oval([*P(ex - 1, ly - 4), *P(ex + 2, ly - 1)],
                              fill="white", outline="")

        # 鼻子
        c.create_polygon([*P(80, 100 + bob), *P(90, 100 + bob), *P(85, 106 + bob)],
                         fill=PINK, outline=LINE, width=1)
        # 嘴
        mx, my = 85, 114 + bob
        if expr == "happy":
            c.create_arc([*P(mx - 8, my - 4), *P(mx, my + 6)], start=190,
                         extent=160, style="arc", outline=LINE, width=ew)
            c.create_arc([*P(mx, my - 4), *P(mx + 8, my + 6)], start=190,
                         extent=160, style="arc", outline=LINE, width=ew)
        elif expr in ("sad", "sick"):
            c.create_arc([*P(mx - 6, my + 2), *P(mx + 6, my - 6)], start=20,
                         extent=140, style="arc", outline=LINE, width=ew)
        elif expr == "hungry":
            c.create_oval([*P(mx - 4, my - 2), *P(mx + 4, my + 8)],
                          fill="#d97b7b", outline=LINE)
            c.create_oval([*P(mx + 14, my + 10), *P(mx + 18, my + 16)],
                          fill="#9fd4ff", outline="")
        elif expr == "sleepy":
            c.create_oval([*P(mx - 3, my), *P(mx + 3, my + 4)],
                          outline=LINE, width=ew)
        else:
            c.create_arc([*P(mx - 6, my - 3), *P(mx + 6, my + 5)], start=200,
                         extent=140, style="arc", outline=LINE, width=ew)
        # 高兴时的腮红
        if expr == "happy":
            c.create_oval([*P(52, 100 + bob), *P(64, 106 + bob)], fill=BLUSH,
                          outline="")
            c.create_oval([*P(106, 100 + bob), *P(118, 106 + bob)], fill=BLUSH,
                          outline="")

    def _draw_runaway(self, c, ox, oy, scale) -> None:
        cx, cy = ox + 85, oy + 100
        def P(x: float, y: float):
            return (cx + (x - 85) * scale, cy + (y - 100) * scale)
        c.create_rectangle([*P(45, 96), *P(125, 150)],
                           fill="#cfa76b", outline=LINE, width=2)
        for i in range(1, 4):
            y = 96 + i * 13
            c.create_line([*P(45, y), *P(125, y)], fill=LINE, width=1)
        c.create_arc([*P(55, 62), *P(115, 122)], start=0, extent=180,
                     style="arc", outline=LINE, width=4)
        c.create_text(*P(85, 40), text="（猫窝空了）",
                      font=(FONT, 11, "bold"), fill=LINE)
        c.create_text(*P(85, 176), text="本喵离家出走了，三天后回来",
                      font=(FONT, 9), fill=LINE)
