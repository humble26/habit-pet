"""主程序：把状态机、采集器、台词、音效、小鲸鱼记账、UI 接到一起。"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from .collectors.gitfeed import GitPoller
from .collectors.github import GitHubPoller
from .collectors import idle
from . import autostart
from .balance import BalanceTracker, format_money
from .credits import CreditsTracker
from . import growth
from . import share_card
from .config import load_config, save_config
from .narrator import Narrator
from . import report as report_mod
from .sound import PACKS, SoundPlayer
from .state import PetState
from .whale_art import ASSETS_DIR

SPEAKABLE = ("feed", "treat", "sit_hit", "rested", "hungry",
             "late_night", "runaway", "came_home", "pet",
             "pet_runaway", "welcome_back",
             "stage_up", "streak_milestone", "streak_broken",
             "achievement",
             "focus_start", "focus_done", "focus_paused",
             "focus_resumed", "focus_cancel",
             "gh_connected", "gh_feed")


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

        # 音效 & 小鲸鱼记账（并入 DSH 小鲸鱼挂件的玩法与功能）
        snd_cfg = self.cfg.get("sound", {})
        self.sound = SoundPlayer(ASSETS_DIR,
                                 pack=str(snd_cfg.get("pack", "duck")),
                                 enabled=bool(snd_cfg.get("enabled", True)),
                                 log=self._log)
        self.balance = BalanceTracker(config_dir, self.cfg.get("balance", {}),
                                      log=self._log)
        # 剩余积分：Trae CN / TraeWork CN / WorkBuddy / Qoder CN
        self.credits = CreditsTracker(config_dir, self.cfg.get("credits", {}),
                                      log=self._log)
        # GitHub 云端连接（远程投喂 / 动态展示 / 专属成就）
        self.github = GitHubPoller(self.cfg.get("github", {}),
                                   repos=self.cfg["repos"], log=self._log)
        self._manual_credits = False
        self._manual_github = False
        self._manual_balance = False
        window.sound = self.sound
        window.snap_enabled = bool(self.cfg.get("snap", True))
        if self.cfg.get("pet_image") and hasattr(window, "sprite"):
            window.sprite.set_image(self.cfg["pet_image"])

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
            # 小鲸鱼记账
            "balance_click": self._balance_click,
            "balance_label": self.balance.menu_label,
            "alert_toggle": self._toggle_alert,
            # 剩余积分
            "credits_rows": self.credits.menu_rows,
            "credits_click": self._credits_click,
            # GitHub 云端连接
            "github_click": self._github_click,
            "github_label": self.github.menu_label,
            "github_backfill": self._github_backfill,
            "github_backfill_label": self._github_backfill_label,
            "alert_state": lambda: bool(
                self.cfg["balance"].get("alert_enabled", True)),
            # 交互设置
            "snap_toggle": self._toggle_snap,
            "snap_state": lambda: bool(self.cfg.get("snap", True)),
            "sound_set": self._set_sound,
            "sound_pack": self._sound_pack_state,
            "quit": self._quit,
        }
        window.callbacks.update(self.callbacks)

        self._restore_window_pos()
        self._drain_events()
        if created:
            window.show_bubble(
                "初次见面，我是你的鲸鱼娘！\n"
                "右键我 → 打开数据文件夹，把你的 git 仓库路径填进 config.json，"
                "commit 就能把我喂饱啦。", secs=12)
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
            if not self._smoke:
                self.balance.maybe_poll()
                self._drain_balance_events()
                self.credits.maybe_poll()
                self._drain_credits_events()
                self.github.maybe_poll(self.state.gh_last_event_id,
                                       set(self.state.gh_fed_ids))
                self._drain_github_events()
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
            # 任务结束音：专注完成 / 进化（对应挂件的"每轮结束音"玩法）
            if kind in ("focus_done", "stage_up"):
                self.sound.task_done()

    # ------------------------------------------------------------ 交互回调

    def _on_pet(self) -> None:
        self.state.pet_me()
        self._drain_events()

    def _on_treat(self) -> None:
        self.state.treat()
        self._drain_events()

    def _show_status(self) -> None:
        lines = self.state.status_lines(dt.datetime.now())
        # 顺序=优先级：窗口只有 384px 高，内容超长时底部（积分）先被裁，
        # GitHub 行紧随核心状态，保证可见
        lines += self.github.status_lines()
        lines += self.balance.status_lines()
        lines += self.credits.status_lines()
        self.window.show_bubble("\n".join(lines), secs=11)

    def _show_achievements(self) -> None:
        self.window.show_bubble("\n".join(growth.achievement_status(self.state)),
                                secs=12)

    # ------------------------------------------------------------ 专注模式

    def _toggle_focus(self) -> None:
        if self.state.focus:
            self.state.cancel_focus()
        elif self.state.runaway_until:
            self.window.show_bubble("本鲸不在家，专注个鬼。先把我哄回来。")
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
        self.window.show_bubble("已开启开机自启，本鲸以后自动上桌 🐋" if enabled
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

    # ------------------------------------------------------------ 小鲸鱼记账

    def _balance_click(self) -> None:
        if not self.balance.conf.get("enabled", True):
            self.window.show_bubble(
                "小鲸鱼记账已在 config.json 里关闭（balance.enabled = false）。",
                secs=7)
            return
        snap = self.balance.snapshot()
        if not self.balance.enabled() and snap is None:
            self.window.show_bubble(
                "还没配 DeepSeek 余额查询的 key，三种方式任选：\n"
                "① config.json 的 balance.api_key\n"
                "② 环境变量 DEEPSEEK_API_KEY\n"
                "③ DSH 凭据 ~/.dsh/.credentials.yaml（只读自动读取）",
                secs=12)
            return
        if self.balance.maybe_poll(force=True):
            self._manual_balance = True
            if snap:
                self.window.show_bubble(
                    f"正在刷新余额……\n当前 "
                    f"{format_money(snap['balance'], snap['currency'])}"
                    f" · 今日已用 ≈ "
                    f"{format_money(snap['today_used'], snap['currency'])}",
                    secs=6)
            else:
                self.window.show_bubble("正在查询余额……", secs=6)
        else:
            self._show_balance_snapshot(fallback="查询正忙，稍等再点一次。")

    def _show_balance_snapshot(self, fallback: str = "") -> None:
        snap = self.balance.snapshot()
        if snap:
            self.window.show_bubble(
                f"💰 余额 {format_money(snap['balance'], snap['currency'])}\n"
                f"今日已用 ≈ {format_money(snap['today_used'], snap['currency'])}"
                f"（按余额差观测）\n观测时间：{snap['updated_at'] or '—'}",
                secs=9)
        elif fallback:
            self.window.show_bubble(fallback, secs=6)

    def _drain_balance_events(self) -> None:
        for kind, payload in self.balance.drain():
            if kind == "balance_ok":
                if self.balance.check_low():
                    snap = self.balance.snapshot() or {}
                    self.window.show_bubble(self.narrator.line(
                        "balance_low",
                        balance=format_money(snap.get("balance", 0.0),
                                             snap.get("currency", "CNY"))))
                if self._manual_balance:
                    self._manual_balance = False
                    self._show_balance_snapshot()
            elif kind == "balance_err":
                self._log(f"[balance] 查询失败：{payload}")
                if self._manual_balance:
                    self._manual_balance = False
                    self.window.show_bubble(
                        "余额查询失败（网络或密钥问题，详见 log.txt）", secs=7)

    def _toggle_alert(self) -> bool:
        cur = bool(self.cfg["balance"].get("alert_enabled", True))
        self.cfg["balance"]["alert_enabled"] = not cur
        self._save_cfg()
        if cur:
            self.window.show_bubble("余额预警已关闭。")
            return False
        try:
            thr = float(self.cfg["balance"].get("low_alert", 0.0) or 0.0)
        except (TypeError, ValueError):
            thr = 0.0
        if thr <= 0:
            self.window.show_bubble(
                "余额预警已开启，但阈值还是 0：\n改 config.json 的 "
                "balance.low_alert 为正数后才会真的提醒。", secs=10)
        else:
            self.window.show_bubble(f"余额预警已开启：低于 {thr:g} 时提醒你。")
        return True

    # ------------------------------------------------------------ 剩余积分

    def _credits_click(self, key: str = "") -> None:
        """点某家=先亮出缓存详情再刷新那一家；key 为空=全部刷新。"""
        if not self.credits.conf.get("enabled", True):
            self.window.show_bubble(
                "剩余积分已在 config.json 里关闭（credits.enabled = false）。",
                secs=7)
            return
        if not self.credits.enabled():
            self.window.show_bubble(
                "四家积分查询都被单独关掉了（config.json → credits.providers）。",
                secs=7)
            return
        if key:
            self._show_credits_detail(key)
        if self.credits.maybe_poll(force=True, keys=[key] if key else None):
            self._manual_credits = True
            if not key:
                self.window.show_bubble("正在查询四家剩余积分……", secs=5)
        else:
            self.window.show_bubble("积分查询正忙，稍等再点一次。", secs=5)

    def _show_credits_detail(self, key: str) -> None:
        r = self.credits.results.get(key) or {}
        label = next((p.label for p in self.credits.providers if p.key == key),
                     key)
        lines = r.get("lines") or []
        body = "\n".join(lines) if lines else (r.get("summary") or "暂无详情")
        at = r.get("at", "")
        tail = f"\n（{at}）" if at else ""
        self.window.show_bubble(f"{label}\n{body}{tail}", secs=12)

    def _drain_credits_events(self) -> None:
        for kind, keys, err in self.credits.drain():
            if kind != "credits_done":
                continue
            if err:
                self._log(f"[credits] 轮询异常：{err}")
            manual, self._manual_credits = self._manual_credits, False
            if not manual:
                # 后台轮询的失败也留个痕，方便排查
                for k, r in self.credits.results.items():
                    if r.get("status") == "err":
                        self._log(f"[credits] {k}：{r.get('summary', '')}")
                continue
            picked = keys or [p.key for p in self.credits.providers]
            if len(picked) == 1:
                self._show_credits_detail(picked[0])
                return
            lines = []
            for p in self.credits.providers:
                if p.key in picked:
                    lines.append(f"{p.label}：{self.credits.short_of(p.key) or '—'}")
            self.window.show_bubble("\n".join(lines) or "没有可显示的积分数据。",
                                    secs=11)

    # ------------------------------------------------------------ GitHub 云端

    def _github_click(self) -> None:
        if not self.github.enabled():
            self.window.show_bubble(
                "GitHub 连接已在 config.json 里关闭（github.enabled = false）。",
                secs=7)
            return
        if self.github.maybe_poll(self.state.gh_last_event_id,
                                  set(self.state.gh_fed_ids), force=True):
            self._manual_github = True
            self.window.show_bubble("正在连接 GitHub……", secs=5)
        else:
            self.window.show_bubble("GitHub 查询正忙，稍等再点一次。", secs=5)

    def _record_gh_fed(self, payload: dict) -> None:
        """把本轮真正喂过的事件 ID 记进 state，供后续轮询/补喂过滤（防双喂）。"""
        ids = [str(x) for x in (payload.get("fed_ids") or []) if str(x)]
        if not ids:
            return
        for i in ids:
            if i not in self.state.gh_fed_ids:
                self.state.gh_fed_ids.append(i)
        if len(self.state.gh_fed_ids) > 500:
            self.state.gh_fed_ids = self.state.gh_fed_ids[-500:]
        self.state.dirty = True

    def _github_backfill_label(self) -> str:
        return ("补喂云端历史：已补喂 ✓" if self.state.gh_backfilled
                else "补喂云端历史推送")

    def _github_backfill(self) -> None:
        if not self.github.enabled():
            self.window.show_bubble(
                "GitHub 连接已在 config.json 里关闭（github.enabled = false）。",
                secs=7)
            return
        if self.state.gh_backfilled:
            self.window.show_bubble("云端历史已经补喂过啦（只补一次，防撑坏）。",
                                    secs=8)
            return
        if self.state.runaway_until:
            self.window.show_bubble("本鲸不在家，回来再谈补喂的事。", secs=6)
            return
        self._drain_github_events()      # 先清空已完成的轮询结果，防毫秒级双喂
        if self.github.start_backfill(self.state.gh_last_event_id,
                                      set(self.state.gh_fed_ids)):
            self.window.show_bubble(
                "正在翻找云端的历史推送……\n（要逐条补算提交数，可能要一两分钟）",
                secs=12)
        else:
            self.window.show_bubble("GitHub 查询正忙，稍等再点一次。", secs=6)

    def _show_github_summary(self, payload: dict) -> None:
        login = payload.get("login") or "?"
        mode = payload.get("mode")
        mode_txt = ("登录态（含私有仓库）" if mode == "token"
                    else "匿名（只有公开数据）")
        lines = [f"🐙 GitHub：@{login}", f"已连接 · {mode_txt}"]
        today = payload.get("today_pushes")
        if isinstance(today, int):
            lines.append(f"今日推送 {today} 个 commit")
        if payload.get("fed"):
            lines.append(f"本次云端投喂 +{payload['fed']} 个 commit")
        if payload.get("recent"):
            lines.append("最近：" + "、".join(list(payload["recent"])[:3]))
        if payload.get("note"):
            lines.append(str(payload["note"]))
        if payload.get("at"):
            lines.append(f"（{payload['at']}）")
        self.window.show_bubble("\n".join(lines), secs=12)

    def _drain_github_events(self) -> None:
        for kind, payload in self.github.drain():
            if kind != "github_done":
                continue
            if payload.get("backfill"):
                self._drain_github_backfill(payload)
                continue
            st = payload.get("status")
            if st in ("ok", "baseline"):
                manual, self._manual_github = self._manual_github, False
                self.state.connect_github(str(payload.get("login") or ""))
                cur = str(payload.get("cursor") or "")
                if cur and cur != self.state.gh_last_event_id:
                    self.state.gh_last_event_id = cur
                    self.state.dirty = True
                fed = int(payload.get("fed") or 0)
                if fed > 0:
                    repos = list((payload.get("fed_repos") or {}).keys())
                    self.state.feed(fed, remote=True, repos=repos)
                self._record_gh_fed(payload)
                self._drain_events()     # 立刻把 gh_connected / gh_feed 台词吐出来
                if manual:
                    self._show_github_summary(payload)
            else:
                note = payload.get("note") or "连接异常"
                self._log(f"[github] {note}")
                if self._manual_github:
                    self._manual_github = False
                    self.window.show_bubble(f"GitHub：{note}", secs=10)

    def _drain_github_backfill(self, payload: dict) -> None:
        st = payload.get("status")
        if st != "ok":
            note = payload.get("note") or "补喂失败"
            self._log(f"[github] 补喂：{note}")
            self.window.show_bubble(f"补喂云端历史失败：{note}", secs=10)
            return
        fed = int(payload.get("fed") or 0)
        n_repos = len(payload.get("fed_repos") or {})
        days = int(payload.get("span_days") or 0)
        if fed > 0:
            repos = list((payload.get("fed_repos") or {}).keys())
            self.state.feed(fed, remote=True, repos=repos)
        self._record_gh_fed(payload)
        self.state.gh_backfilled = True
        cur = str(payload.get("cursor") or "")
        if cur and not self.state.gh_last_event_id:
            self.state.gh_last_event_id = cur     # 首连即补喂：补上正常轮询游标
        self.state.dirty = True
        self._drain_events()                      # 先把投喂/成就台词排进气泡队列
        if fed > 0:
            summary = (f"☁️ 云端历史补喂完成：\n"
                       f"+{fed} 个 commit · {n_repos} 个仓库 · "
                       f"覆盖最近约 {days} 天\n"
                       f"（GitHub 活动接口最多回溯约 90 天）")
        else:
            summary = "云端历史没有可补喂的推送（或都已被喂过）。"
        self.window.show_bubble(summary, secs=13)

    # ------------------------------------------------------------ 交互设置

    def _toggle_snap(self) -> bool:
        cur = bool(self.cfg.get("snap", True))
        self.cfg["snap"] = not cur
        self._save_cfg()
        self.window.snap_enabled = not cur
        if cur:
            self.window.show_bubble("贴边吸附已关闭（自由摆放）。")
        else:
            self.window.show_bubble("贴边吸附已开启：拖到屏幕边缘松手会吸附，"
                                    "贴左时水平镜像。")
        return not cur

    def _set_sound(self, value: str) -> None:
        if value == "off":
            self.sound.set_enabled(False)
            self.cfg["sound"]["enabled"] = False
            self.window.show_bubble("音效已关闭。")
        else:
            self.sound.set_pack(value)
            self.sound.set_enabled(True)
            self.cfg["sound"]["enabled"] = True
            self.cfg["sound"]["pack"] = value
            self.window.show_bubble(f"音效已切换：{PACKS[value]['name']}。")
        self._save_cfg()

    def _sound_pack_state(self) -> str:
        return self.sound.pack if self.sound.enabled else "off"

    def _save_cfg(self) -> None:
        try:
            save_config(self.cfg, self.config_dir)
        except OSError as e:
            self._log(f"[config] 写回失败：{e!r}")

    # ------------------------------------------------------------ 生命周期

    def _save(self) -> None:
        x, y = self.window.save_position()
        self.state.window_x, self.state.window_y = x, y
        self.state.save(self.state_path)

    def _quit(self) -> None:
        try:
            self._save()
            self.sound.close()
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
            "sound": {"enabled": self.sound.enabled, "pack": self.sound.pack},
            "balance": self.balance.snapshot(),
            "balance_key": bool(self.balance.key),
            "credits": self.credits.smoke_summary(),
            "github": self.github.smoke_summary(),
        }
        self._save()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        self.window.destroy()
