"""桌宠窗口：tkinter 透明置顶窗 + 鲸鱼娘（图片渲染）。

- 形象来自 assets/DSniang1.png（可被自定义角色图替换，见 whale_art），
  按成长阶段缩放；透明走色键（whale_art.KEY_HEX）。
- 交互并入 DSH 小鲸鱼挂件的玩法：按压 Q 弹 + 音效、松开回弹、
  拖拽贴边吸附（贴左时水平镜像）、点按触发 rua 动图。
- 位置限制：拖动/恢复/松手全程把**形象**夹在屏幕内——形象可以精确贴住
  屏幕边（窗口的透明边距允许探出屏幕，看不见也不挡点击），不会滑出去；
  底部留出任务栏高度。
- 气泡走 BubbleQueue 排队，事件不会被后来的消息立刻冲掉。
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont

from .bubble_queue import BubbleQueue
from .theme import (BUBBLE_BG, BUBBLE_BORDER, BUBBLE_FG, DEEP, FONT, HEART,
                    MUTED)
from .whale_art import KEY_HEX, WhaleSprite

W, H = 280, 384
BASE_PET_H = 236                 # 鲸鱼娘基础显示高度（成长阶段 scale=1.0）
PET_BOTTOM_PAD = 10
SNAP_DIST = 22                   # 拖拽松手后离屏幕边缘多近算贴边
TASKBAR_MARGIN = 40              # 位置限制时屏幕底部留出的任务栏高度
ART_BOX_FALLBACK = (48, 138, 232, 374)   # 渲染前的形象估算包围盒（相对窗口）
RUA_TICKS = 10                   # rua.gif 播一圈占用的渲染帧数（150ms/帧）
PHOTO_CACHE_MAX = 72             # 形象缓存上限（张），防长跑内存膨胀

MENU_TOP = [
    ("今天过得怎么样", "status"),
    ("我的成就", "achievements"),
]
MENU_MIDDLE = [
    ("手动喂一包粮", "treat"),
    ("来句吐槽", "roast"),
    ("生成本周周报", "weekly"),
    ("生成分享卡", "share_card"),
    ("打开数据文件夹", "open_folder"),
]

_TONE_BY_EXPR = {"sick": "sick", "sad": "sad"}

CARD_RADIUS = 12                 # 气泡卡圆角
CARD_TAIL = 9                    # 指向鲸鱼娘的小尾巴高度
CARD_PAD_X, CARD_PAD_Y = 13, 9
CARD_MAX_W = 320                 # 卡内换行上限（视觉宽度）


def _round_rect_pts(x0: float, y0: float, x1: float, y1: float,
                    r: float) -> list[float]:
    """圆角矩形点列（smooth 多边形用，同 _chip 手法）。"""
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r,
        x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r,
        x0, y0 + r, x0, y0,
    ]


class _BubbleCard(tk.Toplevel):
    """独立圆角气泡卡片：不再寄生在宠物窗口里。

    - 贴边停车/窗口越界时气泡也不会被裁
    - 宽度按内容自适应（上限 CARD_MAX_W），带指向鲸鱼娘的小尾巴
    - 点击穿透 + 不抢焦点：气泡不挡桌面操作
    """

    def __init__(self, master: "PetWindow") -> None:
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", KEY_HEX)
            self.configure(bg=KEY_HEX)
        except tk.TclError:            # 非 Windows：退化不透明卡片
            self.configure(bg=BUBBLE_BG)
        self.canvas = tk.Canvas(self, bg=self.cget("bg"), bd=0,
                                highlightthickness=0)
        self.canvas.pack()
        self.font = tkfont.Font(family=FONT, size=10)
        self.line_h = self.font.metrics("linespace")
        self._cw = 0                   # 上次渲染的尺寸（move_to 平移用）
        self._ch = 0
        self._tail_id = None           # 尾巴多边形（跟随平移只改坐标）
        self._below = False            # 本轮是放在脚下（头顶放不下时）

    def _apply_click_through(self) -> None:
        """点击穿透 + 不抢焦点（WS_EX_TRANSPARENT | WS_EX_NOACTIVATE）。

        必须在窗口真正 map 之后设置：Tk 首次显示时会重写 ex-style，
        创建时设的位会被抹掉，所以每次 show_text 后再补设一次。
        """
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.GetAncestor.restype = wintypes.HWND
            u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                         ctypes.c_long]
            hwnd = u.GetAncestor(self.winfo_id(), 2)       # GA_ROOT
            ex = u.GetWindowLongW(hwnd, -20)
            if not (ex & 0x20 and ex & 0x08000000):
                u.SetWindowLongW(hwnd, -20, ex | 0x20 | 0x08000000)
        except Exception:
            pass

    @staticmethod
    def _anchor_pos(w: int, h: int, center_x: int, top_y: int, bottom_y: int,
                    screen_w: int, screen_h: int, below: bool) -> tuple[int, int]:
        x = min(max(6, center_x - w // 2), max(6, screen_w - w - 6))
        if below:                      # 放脚下：尾巴朝上
            y = min(max(6, bottom_y + 6), max(6, screen_h - h - 6))
        else:                          # 放头顶：尾巴朝下
            y = min(max(6, top_y - 6), screen_h - 6) - h
        return x, max(6, y)

    @staticmethod
    def _tail_pts(w: int, h: int, x: int, center_x: int,
                  below: bool) -> list[float]:
        """尾巴尖指向锚点（卡被挤到屏幕边时也不指偏）。"""
        tx = min(max(center_x - x, CARD_RADIUS + 14),
                 w - CARD_RADIUS - 14)
        if below:
            return [tx - 7, CARD_TAIL + 2, tx, 1, tx + 7, CARD_TAIL + 2]
        box_bottom = h - CARD_TAIL
        return [tx - 7, box_bottom - 2, tx, h - 1, tx + 7, box_bottom - 2]

    def move_to(self, center_x: int, top_y: int, bottom_y: int,
                screen_w: int, screen_h: int) -> None:
        """仅平移（尺寸不变）：鲸鱼娘动的时候气泡跟着走，不重画。"""
        if not self._cw:
            return
        x, y = self._anchor_pos(self._cw, self._ch, center_x, top_y,
                                bottom_y, screen_w, screen_h, self._below)
        self.geometry(f"+{x}+{y}")
        if self._tail_id is not None:      # 尾巴随手拖实时改指向
            try:
                self.canvas.coords(
                    self._tail_id,
                    *self._tail_pts(self._cw, self._ch, x, center_x,
                                    self._below))
            except Exception:
                self._tail_id = None

    def show_text(self, text: str, center_x: int, top_y: int, bottom_y: int,
                  screen_w: int, screen_h: int) -> None:
        """渲染并定位。

        center_x=锚点水平中心；top_y/bottom_y=形象头顶/脚底的屏幕 y。
        头顶装不下（贴屏幕顶停车）就改放脚下，尾巴朝上——不挡她的脸。
        """
        lines = self._wrap(text)
        tw = max((self.font.measure(ln) for ln in lines), default=12)
        w = min(CARD_MAX_W, tw + CARD_PAD_X * 2)
        h = self.line_h * len(lines) + CARD_PAD_Y * 2 + CARD_TAIL
        self._cw, self._ch = w, h
        self._below = top_y - 6 - h < 6
        x, y = self._anchor_pos(w, h, center_x, top_y, bottom_y,
                                screen_w, screen_h, self._below)
        self.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas
        c.configure(width=w, height=h)
        c.delete("all")
        if self._below:
            box_top, box_bottom = CARD_TAIL, h - 1
        else:
            box_top, box_bottom = 1, h - CARD_TAIL
        c.create_polygon(
            _round_rect_pts(1, box_top, w - 1, box_bottom, CARD_RADIUS),
            smooth=True, fill=BUBBLE_BG, outline=BUBBLE_BORDER, width=1)
        self._tail_id = c.create_polygon(
            self._tail_pts(w, h, x, center_x, self._below),
            fill=BUBBLE_BG, outline=BUBBLE_BORDER, width=1)
        ty = CARD_PAD_Y + (CARD_TAIL if self._below else 0)
        for ln in lines:
            c.create_text(CARD_PAD_X, ty, text=ln, font=self.font,
                          fill=BUBBLE_FG, anchor="nw")
            ty += self.line_h
        self.deiconify()
        self.lift()
        self._apply_click_through()    # 显示后再补设：Tk 映射时重写过 ex-style

    def _wrap(self, text: str) -> list[str]:
        """换行：优先在空格处断，「¥0.00」这类整体不会从中间劈开；
        没有空格的 CJK 长句再按字符贪心断。"""
        limit = CARD_MAX_W - CARD_PAD_X * 2
        out: list[str] = []
        for raw in str(text).split("\n"):
            if not raw:
                out.append("")
                continue
            pieces: list[str] = []
            seg = ""
            for ch in raw:
                if ch == " ":
                    if seg:
                        pieces.append(seg)
                        seg = ""
                    pieces.append(" ")
                else:
                    seg += ch
            if seg:
                pieces.append(seg)
            cur = ""
            for piece in pieces:
                if piece == " ":
                    if cur:
                        cur += " "
                    continue
                if self.font.measure(cur + piece) <= limit:
                    cur += piece
                    continue
                if cur.strip():
                    out.append(cur.rstrip())
                    cur = ""
                if self.font.measure(piece) <= limit:
                    cur = piece
                    continue
                for ch in piece:              # 超长整段：退回字符贪心
                    if cur and self.font.measure(cur + ch) > limit:
                        out.append(cur)
                        cur = ch
                    else:
                        cur += ch
            out.append(cur.rstrip() if cur.strip() else "")
        return out or [""]


class PetWindow(tk.Tk):
    """把主窗口本身做成桌宠。callbacks / sprite / sound 由 HabitPetApp 注入。"""

    def __init__(self, callbacks: dict) -> None:
        super().__init__()
        self.callbacks = callbacks
        self.sprite = WhaleSprite()
        self.sound = None                      # SoundPlayer，由 app 注入
        self.title("Habit Pet")
        self.geometry(f"{W}x{H}")
        self.configure(bg=KEY_HEX)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-transparentcolor", KEY_HEX)
        except tk.TclError:      # 非 Windows 没有透明色键，退化为不透明小窗
            self.configure(bg="#eef4ff")

        self.canvas = tk.Canvas(self, width=W, height=H, bg=KEY_HEX,
                                highlightthickness=0)
        self.canvas.pack()

        self.bubble = tk.Label(
            self, text="", bg=BUBBLE_BG, fg=BUBBLE_FG, wraplength=W - 36,
            justify="left", font=(FONT, 10), relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BUBBLE_BORDER,
        )
        self.bq = BubbleQueue()
        self._bubble_job = None
        self._card: "_BubbleCard | None" = None    # 独立圆角气泡卡（懒创建）
        self._bubble_visible = False

        # 形象缓存 / 动画状态
        self._photos: dict[tuple, object] = {}
        self._font9 = tkfont.Font(family=FONT, size=9)
        self._font10b = tkfont.Font(family=FONT, size=10, weight="bold")
        self._squash = 1.0                     # Q 弹缩放（1.0 = 常态）
        self._anim_job = None
        self._rua_end = -1                     # 渲染帧号游标：大于它才播 rua
        self._rua_len = 0
        self._click_n = 0
        self._render_sig = None                # 上一帧内容签名（增量渲染用）
        self._last_bob = 0
        self._items: dict = {}                 # 画布元素 id（快速平移用）

        # 拖拽 & 点击 & 贴边
        self.snap_enabled = True
        self.mirrored = False                  # 贴左边时镜像（面向屏幕中央）
        self._drag_from = None
        self._moved = False
        self._art_box_rel = None               # 形象像素包围盒（render 时更新）
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self.bind("<Destroy>", self._on_self_destroy, add="+")

        self._build_menu()

    def _on_self_destroy(self, e) -> None:
        """窗口销毁前撤掉挂着的定时器（防退出后 "invalid command name"）。"""
        if e.widget is not self:
            return
        for name in ("_bubble_job", "_anim_job"):
            job = getattr(self, name, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, name, None)
        cb = self.callbacks.get("on_destroy")   # 让 app 撤掉自己的循环定时器
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _build_menu(self) -> None:
        self.menu = tk.Menu(self, tearoff=0)
        for label, key in MENU_TOP:
            self.menu.add_command(label=label, command=lambda k=key: self._cb(k))
        self.menu.add_separator()
        # 专注菜单项标签动态变化（开始/结束 + 剩余时间）
        self.menu.add_command(label="开始专注", command=lambda: self._cb("focus_toggle"))
        self._focus_idx = self.menu.index("end")
        for label, key in MENU_MIDDLE:
            self.menu.add_command(label=label, command=lambda k=key: self._cb(k))
        self.menu.add_separator()
        # 小鲸鱼记账（动态标签，弹菜单时刷新）
        self.menu.add_command(label="余额 · 点击刷新",
                              command=lambda: self._cb("balance_click"))
        self._balance_idx = self.menu.index("end")
        # 剩余积分（Trae CN / TraeWork CN / WorkBuddy / Qoder CN）：
        # 子菜单内容在每次弹菜单时按缓存结果重建
        self._credits_sub = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="剩余积分", menu=self._credits_sub)
        self._rebuild_credits_menu()
        # GitHub 连接（动态标签：连接/已连接 @xxx，弹菜单时刷新）
        self.menu.add_command(label="连接 GitHub 账号",
                              command=lambda: self._cb("github_click"))
        self._github_idx = self.menu.index("end")
        # 补喂云端历史推送（一次性；已补喂后标签变 ✓）
        self.menu.add_command(label="补喂云端历史推送",
                              command=lambda: self._cb("github_backfill"))
        self._gh_backfill_idx = self.menu.index("end")
        self.menu.add_separator()
        # 设置子菜单：开关类收在一起，主菜单只留常用项
        self._settings_sub = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="设置", menu=self._settings_sub)
        self._settings_sub.add_command(
            label="余额预警：开", command=lambda: self._cb("alert_toggle"))
        self._alert_idx = self._settings_sub.index("end")
        self._settings_sub.add_command(
            label="贴边吸附：开", command=lambda: self._cb("snap_toggle"))
        self._snap_idx = self._settings_sub.index("end")
        # 音效子菜单
        self._sound_var = tk.StringVar(value="duck")
        sub = tk.Menu(self._settings_sub, tearoff=0)
        for val, label in (("duck", "小黄鸭"), ("sound1", "音效1"),
                           ("off", "关闭")):
            sub.add_radiobutton(
                label=label, value=val, variable=self._sound_var,
                command=lambda v=val: self._cb("sound_set", v))
        self._settings_sub.add_cascade(label="音效", menu=sub)
        # 开机自启（动态标签）
        self._autostart_idx = None
        try:
            self._settings_sub.add_command(
                label="开机自启",
                command=lambda: self._cb("autostart_toggle"))
            self._autostart_idx = self._settings_sub.index("end")
        except tk.TclError:
            pass
        self.menu.add_command(label="退出", command=lambda: self._cb("quit"))

    def _cb(self, key: str, *args):
        fn = self.callbacks.get(key)
        return fn(*args) if fn else None

    # ------------------------------------------------------------------ 气泡

    def show_bubble(self, text: str, secs: float = 6.0) -> None:
        if self.bq.push(text, secs):
            self._display_bubble(self.bq.current)

    def _display_bubble(self, item: tuple[str, float]) -> None:
        text, secs = item
        if self._bubble_job:
            self.after_cancel(self._bubble_job)
        self._show_bubble_text(text)
        self._bubble_visible = True
        self._bubble_job = self.after(int(secs * 1000), self._bubble_timeout)

    def _bubble_anchor(self) -> tuple[int, int, int, int, int]:
        """气泡锚点：形象头顶/脚底的屏幕 y + 屏幕尺寸（卡自己选上/下摆放）。"""
        left, top, right, bottom = self._art_box()
        wx, wy = self.winfo_x(), self.winfo_y()
        return (wx + (left + right) // 2, wy + top, wy + bottom,
                self.winfo_screenwidth(), self.winfo_screenheight())

    def _show_bubble_text(self, text: str) -> None:
        """优先用独立圆角卡片；异常时退回窗口内 Label（老方案）。"""
        try:
            if self._card is None:
                self._card = _BubbleCard(self)
            self._card.show_text(text, *self._bubble_anchor())
            return
        except Exception:
            try:
                if self._card is not None:
                    self._card.destroy()
            except Exception:
                pass
            self._card = None
        self.bubble.config(text=text)
        self.bubble.place(x=8, y=4, width=W - 16)

    def _reposition_bubble(self) -> None:
        """她动了，气泡卡跟着走（显示中才动；只平移不重画）。"""
        if not (self._bubble_visible and self.bq.current):
            return
        if self._card is not None:
            try:
                self._card.move_to(*self._bubble_anchor())
                return
            except Exception:
                pass
        # 窗口内 Label 气泡天然随窗口走，无需处理

    def _bubble_timeout(self) -> None:
        if self._bubble_job:               # 手动触发时把待命定时器一并撤掉
            try:
                self.after_cancel(self._bubble_job)
            except Exception:
                pass
            self._bubble_job = None
        self._bubble_visible = False
        if self._card is not None:
            try:
                self._card.withdraw()
            except Exception:
                self._card = None
        self.bubble.place_forget()
        nxt = self.bq.on_hidden()
        if nxt:
            self._display_bubble(nxt)

    # ------------------------------------------------------------------ 交互

    def _on_press(self, e):
        self._drag_from = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y())
        self._moved = False
        self._squash_to(0.93, ms=60)
        if self.sound:
            self.sound.press()

    def _on_motion(self, e):
        if not self._drag_from:
            return
        sx, sy, wx, wy = self._drag_from
        dx, dy = e.x_root - sx, e.y_root - sy
        if abs(dx) + abs(dy) > 4:
            if not self._moved:
                self._squash_to(1.0, ms=60)   # 进入拖拽，收起按压态
            self._moved = True
            nx, ny = self._clamp_pos(wx + dx, wy + dy)
            self.geometry(f"+{nx}+{ny}")
            self._reposition_bubble()

    def _on_release(self, e):
        if self._drag_from and not self._moved:
            self._click_n += 1
            self._spring()
            self._cb("pet")
            if self.sound:
                self.sound.release()
            # 每两次点击播一次摸摸头动图（有素材才播）
            if self._click_n % 2 == 0:
                self._start_rua()
        elif self._moved:
            self._snap_to_edge()
            if self.sound:
                self.sound.release()
        self._drag_from = None

    def _rebuild_credits_menu(self) -> None:
        """按当前缓存重建「剩余积分」子菜单（点某家=看详情并刷新那一家）。"""
        try:
            rows = self._cb("credits_rows") or []
        except Exception:
            rows = []
        self._credits_sub.delete(0, "end")
        for label, key in rows:
            self._credits_sub.add_command(
                label=str(label),
                command=lambda k=key: self._cb("credits_click", k))
        if rows:
            self._credits_sub.add_separator()
        self._credits_sub.add_command(label="全部刷新",
                                      command=lambda: self._cb("credits_click", ""))

    def _refresh_menu(self) -> None:
        """弹菜单前刷新动态标签（设置类在子菜单里，别写错菜单）。"""
        self._rebuild_credits_menu()
        if self._focus_idx is not None and self.callbacks.get("focus_state"):
            self.menu.entryconfigure(self._focus_idx,
                                     label=str(self.callbacks["focus_state"]()))
        if self.callbacks.get("balance_label"):
            self.menu.entryconfigure(self._balance_idx,
                                     label=str(self.callbacks["balance_label"]()))
        if self.callbacks.get("alert_state"):
            on = bool(self.callbacks["alert_state"]())
            self._settings_sub.entryconfigure(
                self._alert_idx, label="余额预警：开" if on else "余额预警：关")
        if self.callbacks.get("github_label"):
            self.menu.entryconfigure(
                self._github_idx, label=str(self.callbacks["github_label"]()))
        if self.callbacks.get("github_backfill_label"):
            self.menu.entryconfigure(
                self._gh_backfill_idx,
                label=str(self.callbacks["github_backfill_label"]()))
        if self.callbacks.get("snap_state"):
            on = bool(self.callbacks["snap_state"]())
            self._settings_sub.entryconfigure(
                self._snap_idx, label="贴边吸附：开" if on else "贴边吸附：关")
        if self._autostart_idx is not None \
                and self.callbacks.get("autostart_state"):
            enabled = bool(self.callbacks["autostart_state"]())
            self._settings_sub.entryconfigure(
                self._autostart_idx,
                label="开机自启：开" if enabled else "开机自启：关")
        if self.callbacks.get("sound_pack"):
            self._sound_var.set(str(self.callbacks["sound_pack"]()))

    def _on_menu(self, e):
        self._refresh_menu()
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------ Q 弹 / rua

    def _squash_to(self, target: float, ms: int = 70) -> None:
        """把形象缩放缓动到 target（Q 弹动画的每一步）。"""
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        state = {"target": target}

        def step():
            d = state["target"] - self._squash
            if abs(d) < 0.012:
                self._squash = state["target"]
                self._anim_job = None
                return
            self._squash += d * 0.5
            self._anim_job = self.after(ms, step)

        step()

    def _spring(self) -> None:
        """松开回弹：先过冲再回到 1.0。"""
        self._squash_to(1.06, ms=50)
        self.after(80, lambda: self._squash_to(1.0, ms=60))

    def _start_rua(self) -> None:
        frames = self._rua_photos()
        if not frames:
            return
        self._rua_len = len(frames)
        # 渲染帧号游标：接下来的 RUA_TICKS 帧播一轮动图
        self._rua_end = getattr(self, "_frame_now", 0) + RUA_TICKS

    def _rua_photos(self) -> list:
        key = ("rua",)
        hit = self._photos.get(key)
        if hit is not None:
            return hit
        photos = []
        try:
            from PIL import ImageTk
            for im in self.sprite.gif_frames(height=int(BASE_PET_H * 0.9)):
                photos.append(ImageTk.PhotoImage(im))
        except Exception:
            photos = []
        self._photos[key] = photos
        return photos

    # ------------------------------------------------------------ 贴边吸附

    def _art_box(self) -> tuple[int, int, int, int]:
        """形象相对窗口的像素包围盒 (l, t, r, b)；渲染前用估算值。"""
        return self._art_box_rel or ART_BOX_FALLBACK

    def _clamp_pos(self, x: int, y: int) -> tuple[int, int]:
        """位置限制：按「形象看得见」的范围夹取。

        形象可以精确贴住屏幕边；窗口的透明边距允许探出屏幕外
        （不可见也不挡点击），但形象本身永远留在屏幕里。
        """
        left, _top, right, bottom = self._art_box()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = min(max(-left, int(x)), max(-left, sw - right))
        y = min(max(0, int(y)), max(0, sh - TASKBAR_MARGIN - bottom))
        return x, y

    def place_at(self, x: int, y: int) -> None:
        """放到指定坐标（自动限制在屏幕内；恢复存档位置用）。"""
        nx, ny = self._clamp_pos(x, y)
        self.geometry(f"+{nx}+{ny}")

    def _nudge_into_art_bounds(self) -> None:
        """形象范围变化（成长/姿态切换）后，位置越界就轻轻挪回。"""
        x, y = self.winfo_x(), self.winfo_y()
        nx, ny = self._clamp_pos(x, y)
        if (nx, ny) != (x, y):
            self.geometry(f"+{nx}+{ny}")
            self._reposition_bubble()

    def _snap_to_edge(self) -> None:
        x, y = self._clamp_pos(self.winfo_x(), self.winfo_y())
        if not self.snap_enabled:
            self.mirrored = False
            self.geometry(f"+{x}+{y}")     # 关掉吸附也一样出不去的
            return
        left, _top, right, _bottom = self._art_box()
        sw = self.winfo_screenwidth()
        edge = None
        if x + left <= SNAP_DIST:
            x, edge = -left, "left"
        elif x + right >= sw - SNAP_DIST:
            x, edge = sw - right, "right"
        self.geometry(f"+{x}+{y}")
        self.mirrored = edge == "left"
        self._reposition_bubble()

    def save_position(self) -> tuple[int, int]:
        return self.winfo_x(), self.winfo_y()

    def place_default(self) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # 底部留出任务栏高度，避免窗口下缘被遮挡
        self.geometry(f"+{sw - W - 30}+{sh - H - 80}")

    # ------------------------------------------------------------------ 绘制

    def _photo(self, tone: str | None, height: float, flip: bool):
        key = (tone or "", round(float(height)), bool(flip))
        hit = self._photos.get(key)
        if hit is not None:
            return hit
        im = self.sprite.pil_image(height, flip=flip, tone=tone)
        if im is None:
            return None
        try:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(im)
        except Exception:
            return None
        self._photos[key] = photo
        self._cap_photos()
        return photo

    def _cap_photos(self) -> None:
        """形象缓存上限：超出就淘汰最老的（rua 动图帧保留）。"""
        if len(self._photos) <= PHOTO_CACHE_MAX:
            return
        for k in list(self._photos.keys()):
            if len(self._photos) <= PHOTO_CACHE_MAX:
                break
            if k == ("rua",):
                continue
            self._photos.pop(k, None)

    @staticmethod
    def _chip_text(expr: str, focus: dict | None) -> str | None:
        """表情对应的头顶小纸条文案（没有则 None）。"""
        if expr == "hungry":
            return "（饿得想吃键盘了）"
        if expr == "focus":
            f = focus or {}
            if f.get("paused"):
                return "专注暂停（离开键盘太久）"
            return f"专注中 · 剩 {f.get('left_min', '?')} 分钟"
        if expr == "runaway":
            return "（她出海散心去了）"
        return None

    def render(self, expr: str, frame: int, scale: float = 1.0,
               focus: dict | None = None) -> None:
        c = self.canvas
        self._frame_now = frame
        pet_h = BASE_PET_H * scale * self._squash
        bob = int(math.sin(frame / 5) * 3) if expr == "happy" else \
              int(math.sin(frame / 9) * 2)
        tone = "away" if expr == "runaway" else _TONE_BY_EXPR.get(expr)

        # 摸摸头动图接管形象（离家时除外）：帧号决定播到第几帧
        rua_idx = None
        if expr != "runaway" and frame < self._rua_end and self._rua_len:
            rua_idx = (frame - (self._rua_end - RUA_TICKS)) % self._rua_len

        chip = self._chip_text(expr, focus)
        sig = (expr, tone, round(pet_h), self.mirrored, rua_idx, chip)

        # 增量渲染：内容没变（非逐帧动画表情）时只做整体平移，不重画
        dynamic = expr in ("sleepy", "happy") or rua_idx is not None
        if (not dynamic and sig == self._render_sig
                and self._items.get("img") is not None):
            d = bob - self._last_bob
            if d:
                c.move("all", 0, d)
                if self._art_box_rel:
                    l, t, r, b = self._art_box_rel
                    self._art_box_rel = (l, t + d, r, b + d)
                self._nudge_into_art_bounds()
                self._reposition_bubble()
            self._last_bob = bob
            return

        c.delete("all")
        self._items = {}
        cx = W // 2
        pet_top = H - PET_BOTTOM_PAD - pet_h + bob

        img = self._photo(tone, pet_h, self.mirrored)
        if img is None:
            c.create_text(cx, H - 70, text="（形象素材缺失）",
                          font=(FONT, 10), fill=DEEP)
            self._render_sig = sig
            return

        if rua_idx is not None:
            frames = self._rua_photos()
            if frames:
                img = frames[rua_idx % len(frames)]

        self._items["img"] = c.create_image(
            cx, H - PET_BOTTOM_PAD + bob, image=img, anchor="s")
        # 记录形象的实际像素范围（相对窗口），位置限制按它计算：
        # 形象可以贴住屏幕边，窗口的透明边距允许出屏
        try:
            iw, ih = int(img.width()), int(img.height())
        except Exception:
            iw, ih = int(pet_h * 0.8), int(pet_h)
        art_bottom = H - PET_BOTTOM_PAD + bob
        self._art_box_rel = (cx - iw // 2, art_bottom - ih,
                             cx - iw // 2 + iw, art_bottom)
        self._nudge_into_art_bounds()

        if expr == "sleepy":
            drift = (frame % 40) / 3
            c.create_text(cx + pet_h * 0.34, pet_top + 26 - drift, text="Z",
                          font=(FONT, 12, "bold"), fill=DEEP)
            c.create_text(cx + pet_h * 0.42, pet_top + 12 - drift, text="z",
                          font=(FONT, 9), fill=MUTED)
        if chip:
            self._items["chip"] = self._chip(
                c, cx, pet_top - 2, chip, bold=expr in ("focus", "runaway"))
        if expr == "happy":
            for i in range(2):
                t = (frame + i * 30) % 60
                if t < 40:
                    hx = cx + (-1) ** i * pet_h * 0.30
                    hy = pet_top + 34 - t * 1.1
                    c.create_text(hx, hy, text="♥", font=(FONT, 11),
                                  fill=HEART)

        self._render_sig = sig
        self._last_bob = bob

    def _chip(self, c, x: float, y: float, text: str,
              bold: bool = False) -> list:
        """头顶小纸条：圆角底色 + 文字，保证任何桌面上都读得清。"""
        f = self._font10b if bold else self._font9
        w = f.measure(text) + 18
        h = f.metrics("linespace") + 8
        x0, y0, x1, y1 = x - w / 2, y - h, x + w / 2, y
        r = h / 2
        pts = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r,
            x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r,
            x0, y0 + r, x0, y0,
        ]
        poly = c.create_polygon(pts, smooth=True, fill=BUBBLE_BG,
                                outline=BUBBLE_BORDER, width=1)
        txt = c.create_text(x, (y0 + y1) / 2, text=text, font=f,
                            fill=BUBBLE_FG)
        return [poly, txt]
