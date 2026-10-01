"""桌宠窗口：tkinter 透明置顶窗 + 鲸鱼娘（图片渲染）。

- 形象来自 assets/DSniang1.png（可被自定义角色图替换，见 whale_art），
  按成长阶段缩放；透明走色键（whale_art.KEY_HEX）。
- 交互并入 DSH 小鲸鱼挂件的玩法：按压 Q 弹 + 音效、松开回弹、
  拖拽贴边吸附（贴左时水平镜像）、点按触发 rua 动图。
- 位置限制：拖动/恢复/松手全程把窗口夹在屏幕内（到边就停，鲸鱼娘不会
  被拖出屏幕；底部留出任务栏高度）。
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
RUA_TICKS = 10                   # rua.gif 播一圈占用的渲染帧数（150ms/帧）

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

        # 形象缓存 / 动画状态
        self._photos: dict[tuple, object] = {}
        self._font9 = tkfont.Font(family=FONT, size=9)
        self._font10b = tkfont.Font(family=FONT, size=10, weight="bold")
        self._squash = 1.0                     # Q 弹缩放（1.0 = 常态）
        self._anim_job = None
        self._rua_end = -1                     # 渲染帧号游标：大于它才播 rua
        self._rua_len = 0
        self._click_n = 0

        # 拖拽 & 点击 & 贴边
        self.snap_enabled = True
        self.mirrored = False                  # 贴左边时镜像（面向屏幕中央）
        self._drag_from = None
        self._moved = False
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)

        self._build_menu()

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
        self.menu.add_command(label="余额预警：开",
                              command=lambda: self._cb("alert_toggle"))
        self._alert_idx = self.menu.index("end")
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
        self.menu.add_command(label="贴边吸附：开",
                              command=lambda: self._cb("snap_toggle"))
        self._snap_idx = self.menu.index("end")
        self.menu.add_separator()
        # 音效子菜单
        self._sound_var = tk.StringVar(value="duck")
        sub = tk.Menu(self.menu, tearoff=0)
        for val, label in (("duck", "小黄鸭"), ("sound1", "音效1"),
                           ("off", "关闭")):
            sub.add_radiobutton(
                label=label, value=val, variable=self._sound_var,
                command=lambda v=val: self._cb("sound_set", v))
        self.menu.add_cascade(label="音效", menu=sub)
        # 开机自启（动态标签）
        self._autostart_idx = None
        try:
            self.menu.add_command(label="开机自启",
                                  command=lambda: self._cb("autostart_toggle"))
            self._autostart_idx = self.menu.index("end")
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
        self.bubble.config(text=text)
        self.bubble.place(x=8, y=4, width=W - 16)
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

    def _on_menu(self, e):
        self._rebuild_credits_menu()
        if self._focus_idx is not None and self.callbacks.get("focus_state"):
            self.menu.entryconfigure(self._focus_idx,
                                     label=str(self.callbacks["focus_state"]()))
        if self.callbacks.get("balance_label"):
            self.menu.entryconfigure(self._balance_idx,
                                     label=str(self.callbacks["balance_label"]()))
        if self.callbacks.get("alert_state"):
            on = bool(self.callbacks["alert_state"]())
            self.menu.entryconfigure(
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
            self.menu.entryconfigure(
                self._snap_idx, label="贴边吸附：开" if on else "贴边吸附：关")
        if self._autostart_idx is not None \
                and self.callbacks.get("autostart_state"):
            enabled = bool(self.callbacks["autostart_state"]())
            self.menu.entryconfigure(
                self._autostart_idx,
                label="开机自启：开" if enabled else "开机自启：关")
        if self.callbacks.get("sound_pack"):
            self._sound_var.set(str(self.callbacks["sound_pack"]()))
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

    def _clamp_pos(self, x: int, y: int) -> tuple[int, int]:
        """位置限制：把窗口坐标夹回屏幕内（拖到边缘就停在边缘）。"""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = min(max(0, int(x)), max(0, sw - W))
        y = min(max(0, int(y)), max(0, sh - H - TASKBAR_MARGIN))
        return x, y

    def place_at(self, x: int, y: int) -> None:
        """放到指定坐标（自动限制在屏幕内；恢复存档位置用）。"""
        nx, ny = self._clamp_pos(x, y)
        self.geometry(f"+{nx}+{ny}")

    def _snap_to_edge(self) -> None:
        x, y = self._clamp_pos(self.winfo_x(), self.winfo_y())
        if not self.snap_enabled:
            self.mirrored = False
            self.geometry(f"+{x}+{y}")     # 关掉吸附也一样出不去的
            return
        sw = self.winfo_screenwidth()
        edge = None
        if x <= SNAP_DIST:
            x, edge = 0, "left"
        elif x + W >= sw - SNAP_DIST:
            x, edge = sw - W, "right"
        self.geometry(f"+{x}+{y}")
        self.mirrored = edge == "left"

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
        return photo

    def render(self, expr: str, frame: int, scale: float = 1.0,
               focus: dict | None = None) -> None:
        c = self.canvas
        self._frame_now = frame
        c.delete("all")
        cx = W // 2
        pet_h = BASE_PET_H * scale * self._squash
        bob = int(math.sin(frame / 5) * 3) if expr == "happy" else \
              int(math.sin(frame / 9) * 2)
        pet_top = H - PET_BOTTOM_PAD - pet_h + bob

        tone = "away" if expr == "runaway" else _TONE_BY_EXPR.get(expr)
        img = self._photo(tone, pet_h, self.mirrored)
        if img is None:
            c.create_text(cx, H - 70, text="（形象素材缺失）",
                          font=(FONT, 10), fill=DEEP)
            return

        # 摸摸头动图接管形象（离家时除外）
        if expr != "runaway" and frame < self._rua_end and self._rua_len:
            frames = self._rua_photos()
            if frames:
                idx = (frame - (self._rua_end - RUA_TICKS)) % len(frames)
                img = frames[idx]

        c.create_image(cx, H - PET_BOTTOM_PAD + bob, image=img, anchor="s")

        if expr == "sleepy":
            drift = (frame % 40) / 3
            c.create_text(cx + pet_h * 0.34, pet_top + 26 - drift, text="Z",
                          font=(FONT, 12, "bold"), fill=DEEP)
            c.create_text(cx + pet_h * 0.42, pet_top + 12 - drift, text="z",
                          font=(FONT, 9), fill=MUTED)
        if expr == "hungry":
            self._chip(c, cx, pet_top - 2, "（饿得想吃键盘了）")
        if expr == "focus":
            if focus and focus.get("paused"):
                txt = "专注暂停（离开键盘太久）"
            else:
                txt = f"专注中 · 剩 {focus.get('left_min', '?')} 分钟"
            self._chip(c, cx, pet_top - 2, txt, bold=True)
        if expr == "runaway":
            self._chip(c, cx, pet_top - 2, "（她出海散心去了）", bold=True)
        if expr == "happy":
            for i in range(2):
                t = (frame + i * 30) % 60
                if t < 40:
                    hx = cx + (-1) ** i * pet_h * 0.30
                    hy = pet_top + 34 - t * 1.1
                    c.create_text(hx, hy, text="♥", font=(FONT, 11),
                                  fill=HEART)

    def _chip(self, c, x: float, y: float, text: str,
              bold: bool = False) -> None:
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
        c.create_polygon(pts, smooth=True, fill=BUBBLE_BG,
                         outline=BUBBLE_BORDER, width=1)
        c.create_text(x, (y0 + y1) / 2, text=text, font=f, fill=BUBBLE_FG)
