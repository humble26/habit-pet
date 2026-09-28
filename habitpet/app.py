"""主程序：把状态机、采集器、台词、UI 接到一起。"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from .collectors.gitfeed import GitPoller
from .collectors import idle
from . import autostart
from . import growth
from . import share_card
from .config import load_config
from .narrator import Narrator
from . import report as report_mod
from .state import PetState

SPEAKABLE = ("feed", "treat", "sit_hit", "rested", "hungry",
             "late_night", "runaway", "came_home", "pet",
             "pet_runaway", "welcome_back",
             "stage_up", "streak_milestone", "streak_broken",
             "achievement",
             "focus_start", "focus_done", "focus_paused",
             "focus_resumed", "focus_cancel")


class HabitPetApp:
    """PetWindow 由外部构造后传入；回调在 __init__ 里注入窗口。"""

    def __init__(self, window: PetWindow, config_dir: Path, smoke: bool = False) -> None:
        self.window = window
        self.config_dir = config_dir
        config_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = config_dir / "state.json"
        self.reports_dir = config_dir / "reports"

        self.cfg, created = load_config(config_dir)
        self.state = PetState.load(self.state_path, self.cfg["mechanics"])
        self.state.catch_up(dt.datetime.now())

        self.narrator = Narrator(self.cfg["llm"], log=self._log)
        self.poller = GitPoller(self.cfg["repos"], log=self._log)

        self._frame = 0
        self._smoke = smoke
        self._roast_running = False
        # Tkinter 的 after/event 非线程安全：工作线程只往队列里放结果，
        # 由主线程的 _second_loop 消费
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()

        self.callbacks = {
            "pet": self._on_pet,
            "status": self._show_status,
            "achievements": self._show_achievements,
            "focus_toggle": self._toggle_focus,
            "focus_state": self._focus_menu_label,
            "treat": self._on_treat,
            "roast": self._roast_now,
            "weekly": self._make_weekly,
            "share_card": self._make_share_card,
            "open_folder": self._open_folder,
            "autostart_state": autostart.is_enabled,
            "autostart_toggle": self._toggle_autostart,
            "quit": self._quit,
        }
        window.callbacks.update(self.callbacks)

        self._restore_window_pos()
        self._drain_events()
        if created:
            window.show_bubble(
                "初次见面，我是你的 Habit Pet！\n"
                "右键我 → 打开数据文件夹，把你的 git 仓库路径填进 config.json，"
                "commit 就能喂我啦。", secs=12)
        if smoke:
            window.after(4000, self._smoke_finish)

        window.after(200, self._fast_loop)
        window.after(1000, self._second_loop)
        window.after(5000, self._git_loop)
        window.after(60000, self._autosave_loop)

    # ------------------------------------------------------------ 日志

    def _log(self, msg: str) -> None:
        try:
            with open(self.config_dir / "log.txt", "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
        except OSError:
            pass

    # ------------------------------------------------------------ 窗口位置

    def _restore_window_pos(self) -> None:
        x, y = self.state.window_x, self.state.window_y
        if x < 0 or y < 0:
            self.window.place_default()
        else:
            self.window.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------ 循环

    def _fast_loop(self) -> None:
        # 渲染循环绝不能被单次异常杀死，否则宠物永久定格
        try:
            self._frame += 1
            now = dt.datetime.now()
            focus_info = None
            if self.state.focus:
                focus_info = {"left_min": round(self.state.focus.remaining_minutes()),
                              "paused": self.state.focus.paused}
            self.window.render(self.state.expression(now), self._frame,
                               scale=growth.stage_scale(self.state),
                               focus=focus_info)
        except Exception as e:
            self._log(f"[fast_loop] {e!r}")
        self.window.after(150, self._fast_loop)

    def _second_loop(self) -> None:
        try:
            now = dt.datetime.now()
            self.state.tick(1.0, now, idle.is_active())
            self._drain_events()
            self._drain_ui_queue()
            self._check_daily_roast(now)
        except Exception as e:
            self._log(f"[second_loop] {e!r}")
        self.window.after(1000, self._second_loop)

    def _git_loop(self) -> None:
        try:
            if self.state.runaway_until is None and self.cfg["repos"]:
                now = dt.datetime.now().astimezone()
                since = self.state.last_poll or (
                    now - dt.timedelta(seconds=self.cfg["git_poll_seconds"] * 2)
                ).isoformat(timespec="seconds")
                total, since_new = self.poller.poll(since)
                self.state.last_poll = since_new
                if total > 0:
                    self.state.feed(total)
                    self._drain_events()
        except Exception as e:
            self._log(f"[git_loop] {e!r}")
        # 下限 30 秒，防止配置成 0/negative 造成忙轮询
        self.window.after(max(int(self.cfg["git_poll_seconds"]), 30) * 1000,
                          self._git_loop)

    def _autosave_loop(self) -> None:
        try:
            if self.state.dirty:
                self._save()
        except Exception as e:
            self._log(f"[autosave] {e!r}")
        self.window.after(60000, self._autosave_loop)

    # ------------------------------------------------------------ 事件 → 台词

    def _drain_events(self) -> None:
        while self.state.events:
            ev = self.state.events.pop(0)
            kind = ev.pop("kind")
            if kind in SPEAKABLE:
                self.window.show_bubble(self.narrator.line(kind, **ev))

    # ------------------------------------------------------------ 交互回调

    def _on_pet(self) -> None:
        self.state.pet_me()
        self._drain_events()

    def _on_treat(self) -> None:
        self.state.treat()
        self._drain_events()

    def _show_status(self) -> None:
        self.window.show_bubble("\n".join(self.state.status_lines(dt.datetime.now())),
                                secs=9)

    def _show_achievements(self) -> None:
        self.window.show_bubble("\n".join(growth.achievement_status(self.state)),
                                secs=12)

    # ------------------------------------------------------------ 专注模式

    def _toggle_focus(self) -> None:
        if self.state.focus:
            self.state.cancel_focus()
        elif self.state.runaway_until:
            self.window.show_bubble("本喵不在家，专注个鬼。先把我哄回来。")
            return
        else:
            self.state.start_focus()
        self._drain_events()

    def _focus_menu_label(self) -> str:
        f = self.state.focus
        if not f:
            mins = float(self.state.mechanics.get("focus_minutes", 25))
            return f"开始专注 {mins:.0f} 分钟"
        paused = "（已暂停）" if f.paused else ""
        return f"结束专注 · 剩 {f.remaining_minutes():.0f} 分{paused}"

    # ------------------------------------------------------------ 分享卡

    def _make_share_card(self) -> None:
        if self._roast_running:
            self.window.show_bubble("正在吐槽中，稍等……")
            return
        self._roast_running = True

        def work() -> None:
            try:
                path = share_card.render_weekly_card(self.state, self.reports_dir)
                self._ui_queue.put(("share_done", path, None))
            except Exception as e:
                self._ui_queue.put(("share_done", None, repr(e)))

        threading.Thread(target=work, daemon=True).start()

    def _toggle_autostart(self) -> bool:
        enabled = autostart.toggle()
        self.window.show_bubble("已开启开机自启，本喵以后自动上桌 🐾" if enabled
                                else "已关闭开机自启")
        return enabled

    def _roast_now(self) -> None:
        self._roast(mark_auto=False)

    def _check_daily_roast(self, now: dt.datetime) -> None:
        c = self.cfg["llm"]
        hh, mm = int(c.get("daily_roast_hour", 21)), int(c.get("daily_roast_minute", 30))
        today = now.date().isoformat()
        if (now.strftime("%H:%M") >= f"{hh:02d}:{mm:02d}"
                and self.state.last_roast_date != today):
            self._roast(mark_auto=True)

    def _snapshot(self) -> dict:
        t = self.state.today
        return {
            "commits": t.commits,
            "active_hours": round(t.active_minutes / 60, 1),
            "sit_hits": t.sit_hits,
            "breaks": t.breaks,
            "late_minutes": round(t.late_minutes),
            "satiety": round(self.state.satiety),
            "mood": round(self.state.mood),
            "health": round(self.state.health),
            "lifespan_max": round(self.state.lifespan_max),
        }

    def _roast(self, mark_auto: bool) -> None:
        if self._roast_running:
            return
        self._roast_running = True
        data = self._snapshot()

        def work() -> None:
            # 任何异常都不能吞掉队列投递，否则 _roast_running 永久锁死
            try:
                text, is_llm = self.narrator.daily_roast(data)
            except Exception as e:
                self._log(f"[roast] 工作线程异常，降级模板：{e!r}")
                text, is_llm = self.narrator.fallback_roast(data), False
            self._ui_queue.put(("roast_done", text, is_llm, mark_auto))

        threading.Thread(target=work, daemon=True).start()

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                item = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            if kind == "roast_done":
                _, text, is_llm, mark_auto = item
                self._apply_roast(text, is_llm, mark_auto)
            elif kind == "weekly_done":
                _, _ok, msg = item
                self._roast_running = False
                self.window.show_bubble(msg, secs=10)
            elif kind == "share_done":
                _, path, err = item
                self._roast_running = False
                if path:
                    self.window.show_bubble(f"分享卡已生成：\n{path}", secs=10)
                    if os.name == "nt":
                        try:
                            os.startfile(path)  # noqa: S606
                        except OSError:
                            pass
                elif err is not None:
                    self.window.show_bubble(f"分享卡生成失败：{err}", secs=8)
                else:
                    self.window.show_bubble("分享卡需要 Pillow：pip install pillow",
                                            secs=8)

    def _apply_roast(self, text: str, is_llm: bool, mark_auto: bool) -> None:
        self._roast_running = False
        self.state.today.roasts += 1
        if mark_auto:
            self.state.last_roast_date = dt.date.today().isoformat()
        try:
            path = report_mod.write_daily(self.state, text, is_llm, self.reports_dir)
            self._log(f"[roast] 日报已写入 {path}")
        except Exception as e:
            self._log(f"[roast] 日报写入失败：{e!r}")
        self.window.show_bubble(text, secs=8)
        self._save()

    def _make_weekly(self) -> None:
        if self._roast_running:
            self.window.show_bubble("正在吐槽中，稍等……")
            return
        self._roast_running = True

        def work() -> None:
            try:
                path = report_mod.write_weekly(self.state, self.reports_dir, self.narrator)
                self._ui_queue.put(("weekly_done", True, f"周报已生成：\n{path}"))
            except Exception as e:
                self._ui_queue.put(("weekly_done", False, f"周报生成失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _open_folder(self) -> None:
        if os.name == "nt":
            os.startfile(self.config_dir)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(self.config_dir)])

    # ------------------------------------------------------------ 生命周期

    def _save(self) -> None:
        x, y = self.window.save_position()
        self.state.window_x, self.state.window_y = x, y
        self.state.save(self.state_path)

    def _quit(self) -> None:
        try:
            self._save()
        finally:
            self.window.destroy()

    def _smoke_finish(self) -> None:
        now = dt.datetime.now()
        info = {
            "expression": self.state.expression(now),
            "stage": growth.stage_name(self.state),
            "streak": self.state.active_streak,
            "achievements": len(self.state.unlocked),
            "status": self.state.status_lines(now),
            "repos": self.cfg["repos"],
            "llm_enabled": self.cfg["llm"].get("enabled"),
            "has_key": bool(self.narrator._api_key()),
        }
        self._save()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        self.window.destroy()
