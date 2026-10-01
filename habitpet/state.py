"""宠物状态机与持久化。

数值设计对应《前景报告 05》的产品设计：
- 饱食度：git commit 喂食，随时间自然衰减
- 心情：摸摸头 / 被 commit 喂食提升，缓慢衰减
- 健康度：连续伏案超阈值扣血，起身休息回复
- 寿命上限：深夜（默认 0-6 点）仍活跃则缓慢下降，下限 30
- 健康归零不判死亡 —— 离家出走三天，回来给你带不好吃的
"""
from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path
from typing import Any, Optional

from . import growth
from .focus import FocusSession

STATE_VERSION = 1
RUNAWAY_DAYS = 3
MIN_LIFESPAN = 30.0
MAX_CATCHUP_HOURS = 18.0       # 关机期间最多补算这么久的饥饿
HISTORY_KEEP_DAYS = 35

RUNAWAY_REASONS = [
    "嫌你久坐都不来摸摸头",
    "饿着肚子还要陪你加班",
    "被你的凌晨键盘声吵到离家出海",
]


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


class DayStats:
    """单日行为统计，驱动日报/周报。"""

    __slots__ = ("date", "commits", "active_minutes", "sit_hits",
                 "breaks", "late_minutes", "roasts", "focus_count",
                 "gh_pushes")

    def __init__(self, date: str, commits: int = 0, active_minutes: float = 0.0,
                 sit_hits: int = 0, breaks: int = 0, late_minutes: float = 0.0,
                 roasts: int = 0, focus_count: int = 0, gh_pushes: int = 0) -> None:
        self.date = date
        self.commits = commits
        self.active_minutes = active_minutes
        self.sit_hits = sit_hits
        self.breaks = breaks
        self.late_minutes = late_minutes
        self.roasts = roasts
        self.focus_count = focus_count
        self.gh_pushes = gh_pushes       # 当天来自 GitHub 云端的远程投喂

    @classmethod
    def for_day(cls, d: dt.date) -> "DayStats":
        return cls(d.isoformat())

    def to_dict(self) -> dict:
        return {
            "date": self.date, "commits": self.commits,
            "active_minutes": round(self.active_minutes, 1),
            "sit_hits": self.sit_hits, "breaks": self.breaks,
            "late_minutes": round(self.late_minutes, 1),
            "roasts": self.roasts, "focus_count": self.focus_count,
            "gh_pushes": self.gh_pushes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DayStats":
        return cls(
            date=d.get("date", ""),
            commits=int(d.get("commits", 0)),
            active_minutes=float(d.get("active_minutes", 0.0)),
            sit_hits=int(d.get("sit_hits", 0)),
            breaks=int(d.get("breaks", 0)),
            late_minutes=float(d.get("late_minutes", 0.0)),
            roasts=int(d.get("roasts", 0)),
            focus_count=int(d.get("focus_count", 0)),
            gh_pushes=int(d.get("gh_pushes", 0)),
        )


def _safe_iso(value, default):
    """日期字段容错解析：坏值回退默认，不拖垮整个存档。"""
    if value is None:
        return default
    try:
        dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return default
    return value


class PetState:
    """宠物数值与事件队列。所有时间用本地时间。"""

    def __init__(self, mechanics: dict) -> None:
        self.mechanics = dict(mechanics)
        self.satiety = 80.0
        self.mood = 70.0
        self.health = 90.0
        self.lifespan_max = 100.0
        self.total_commits = 0
        self.born_at = dt.datetime.now().isoformat(timespec="seconds")
        self.runaway_until: Optional[str] = None
        self.runaway_reason = ""
        self.last_roast_date = ""
        self.last_save: Optional[str] = None
        self.last_poll: Optional[str] = None     # git 喂食的上次扫描时间
        self.window_x = -1
        self.window_y = -1
        # 养成系统（逻辑见 growth.py）
        self.active_streak = 0
        self.best_streak = 0
        self.active_days_total = 0
        self.total_late_minutes = 0.0
        self.came_back_once = False
        self.early_today = False
        self.unlocked: dict[str, str] = {}
        self.stage_idx = 0
        # GitHub 云端连接（轮询逻辑见 collectors/github.py）
        self.gh_login = ""
        self.gh_last_event_id = ""       # 已消费到的事件游标（远程投喂去重）
        self.gh_remote_commits = 0       # 云端累计投喂的 commit 数
        self.gh_repos_seen: list[str] = []   # 云端投喂过的仓库（激励多仓库）
        self.gh_backfilled = False       # 历史推送是否已一次性补喂过
        self.gh_fed_ids: list[str] = []  # 已喂过的事件 ID（事件级去重：基线/轮询/补喂不重喂）
        # 专注模式（会话本身不持久化，重启即取消）
        self.focus: Optional[FocusSession] = None
        self.total_focus_count = 0
        self.today = DayStats.for_day(dt.date.today())
        self.history: dict[str, dict] = {}
        self.events: list[dict] = []             # 待 UI 消费的事件
        self.dirty = False
        # 运行时（不持久化）
        self.continuous_work_min = 0.0
        self.idle_min = 0.0
        self._break_counted = True
        self._worked_since_break = False   # 本次休息之前是否工作过（决定休息是否回血）
        self._hungry_notified = False
        self._late_flag_date = ""

    # ------------------------------------------------------------------ 事件

    def feed(self, commits: int, remote: bool = False,
             repos: Optional[list[str]] = None) -> None:
        """git 提交喂食。remote=True 表示来自 GitHub 云端事件（远程投喂）。"""
        if commits <= 0 or self.runaway_until:
            return
        m = self.mechanics
        self.satiety = clamp(self.satiety + m["commit_feed"] * commits)
        self.mood = clamp(self.mood + m["commit_mood"] * commits)
        self.total_commits += commits
        self.today.commits += commits
        self._hungry_notified = False
        if remote:
            self.gh_remote_commits += commits
            self.today.gh_pushes += commits
            for r in repos or []:
                if r and r not in self.gh_repos_seen and len(self.gh_repos_seen) < 200:
                    self.gh_repos_seen.append(r)
            self.events.append({"kind": "gh_feed", "n": commits,
                                "repos": "、".join((repos or [])[:3]) or "云端",
                                "total": self.gh_remote_commits})
        else:
            self.events.append({"kind": "feed", "n": commits})
        self._growth_tick()
        self.dirty = True

    def connect_github(self, login: str) -> bool:
        """记录云端账号；首次接入会触发成就事件。返回是否是新账号。"""
        login = (login or "").strip()
        if not login or login == self.gh_login:
            return False
        self.gh_login = login
        self.events.append({"kind": "gh_connected", "login": login})
        self._growth_tick()
        self.dirty = True
        return True

    def treat(self) -> None:
        """手动喂一包粮（右键菜单），不计入 commit。"""
        if self.runaway_until:
            return
        self.satiety = clamp(self.satiety + self.mechanics["treat_feed"])
        self.mood = clamp(self.mood + 2.0)
        self._hungry_notified = False
        self.events.append({"kind": "treat"})
        self._growth_tick()
        self.dirty = True

    def pet_me(self) -> None:
        """摸摸头。"""
        if self.runaway_until:
            self.events.append({"kind": "pet_runaway"})
            return
        self.mood = clamp(self.mood + 2.0)
        self.events.append({"kind": "pet"})
        self._growth_tick()
        self.dirty = True

    # ---------------------------------------------------------------- 专注

    def start_focus(self, minutes: float | None = None) -> None:
        if self.focus or self.runaway_until:
            return
        mins = float(minutes or self.mechanics.get("focus_minutes", 25))
        self.focus = FocusSession(mins)
        self.events.append({"kind": "focus_start", "mins": mins})
        self.dirty = True

    def cancel_focus(self) -> None:
        if not self.focus:
            return
        self.focus = None
        self.events.append({"kind": "focus_cancel"})
        self.dirty = True

    def _finish_focus(self) -> None:
        mins = round(self.focus.total_seconds / 60)
        reward = float(self.mechanics.get("focus_reward_mood", 10))
        self.focus = None
        self.mood = clamp(self.mood + reward)
        self.total_focus_count += 1
        self.today.focus_count += 1
        self.events.append({"kind": "focus_done", "mins": mins, "reward": reward})
        self._growth_tick()
        self.dirty = True

    # ------------------------------------------------------------------ 心跳

    def tick(self, dt_seconds: float, now: dt.datetime, active: bool) -> None:
        """推进一次状态。active 表示这段时间用户在键鼠活跃。"""
        self._roll_day(now)
        if self.runaway_until:
            if now >= dt.datetime.fromisoformat(self.runaway_until):
                self._come_home()
            return
        m = self.mechanics
        self.satiety = clamp(self.satiety - m["satiety_decay_per_hour"] * dt_seconds / 3600)
        self.mood = clamp(self.mood - 0.5 * dt_seconds / 3600)

        if active:
            self.idle_min = 0.0
            if self.focus:
                # 专注期不算久坐：正事不是病，打断连续工作计数
                self.continuous_work_min = 0.0
            else:
                self.continuous_work_min += dt_seconds / 60
            self.today.active_minutes += dt_seconds / 60
            if now.hour == 6:
                self.early_today = True
            self._worked_since_break = True
            self._break_counted = False
            if (not self.focus
                    and self.continuous_work_min >= m["sit_limit_minutes"]):
                self.health = clamp(self.health - m["sit_damage"])
                self.today.sit_hits += 1
                self.continuous_work_min = 0.0
                self.events.append({"kind": "sit_hit", "mins": m["sit_limit_minutes"],
                                    "dmg": m["sit_damage"]})
            if self._is_late_night(now):
                self.today.late_minutes += dt_seconds / 60
                self.total_late_minutes += dt_seconds / 60
                self.lifespan_max = max(
                    MIN_LIFESPAN,
                    self.lifespan_max - m["late_night_drain_per_hour"] * dt_seconds / 3600,
                )
                if self._late_flag_date != now.date().isoformat():
                    self._late_flag_date = now.date().isoformat()
                    self.events.append({"kind": "late_night", "h": now.hour})
        else:
            self.idle_min += dt_seconds / 60
            if (self.idle_min >= m["break_rest_minutes"]
                    and self._worked_since_break):
                self._worked_since_break = False
                self.continuous_work_min = 0.0
                self.health = clamp(self.health + m["break_regen"])
                if not self._break_counted:
                    self._break_counted = True
                    self.today.breaks += 1
                    self.events.append({"kind": "rested", "mins": self.idle_min,
                                        "regen": m["break_regen"]})

        if self.satiety < 20 and not self._hungry_notified:
            self._hungry_notified = True
            self.events.append({"kind": "hungry", "s": self.satiety})

        if self.focus:
            f = self.focus
            if active:
                if f.paused:
                    f.paused = False
                    self.events.append({"kind": "focus_resumed"})
                f.elapsed += dt_seconds
                if f.finished:
                    self._finish_focus()
            else:
                grace = float(m.get("focus_grace_minutes", 10))
                if self.idle_min >= grace and not f.paused:
                    f.paused = True
                    self.events.append({"kind": "focus_paused"})

        if self.health <= 0:
            self._run_away(now)
        self.dirty = True

    # ------------------------------------------------------------------ 出走

    def _run_away(self, now: dt.datetime) -> None:
        until = now + dt.timedelta(days=RUNAWAY_DAYS)
        self.runaway_until = until.isoformat(timespec="seconds")
        self.runaway_reason = random.choice(RUNAWAY_REASONS)
        self.continuous_work_min = 0.0
        self.health = 0.0
        self.events.append({"kind": "runaway", "days": RUNAWAY_DAYS,
                            "reason": self.runaway_reason})
        self.dirty = True

    def _come_home(self) -> None:
        self.runaway_until = None
        self.runaway_reason = ""
        self.health = 60.0
        self.mood = clamp(self.mood + 30.0, 0, 100)
        self.satiety = 65.0
        self.came_back_once = True
        self.events.append({"kind": "came_home"})
        self._growth_tick()
        self.dirty = True

    def _growth_tick(self) -> None:
        """成长值/成就可能在任何事件后变化，统一在这里结算成事件。"""
        growth.refresh_stage(self)
        for a in growth.check_new(self):
            self.events.append({"kind": "achievement",
                                "title": a.title, "desc": a.desc})

    # ------------------------------------------------------------------ 查询

    def _is_late_night(self, now: dt.datetime) -> bool:
        start = int(self.mechanics["late_night_start"]) % 24
        end = int(self.mechanics["late_night_end"]) % 24
        h = now.hour
        if start <= end:
            return start <= h < end
        return h >= start or h < end

    def expression(self, now: dt.datetime) -> str:
        """决定当前表情动作，优先级从高到低。"""
        if self.runaway_until:
            return "runaway"
        if self.health < 25:
            return "sick"
        if self.focus:
            return "focus"
        if self.satiety < 25:
            return "hungry"
        if self._is_late_night(now):
            return "sleepy"
        if self.mood < 30:
            return "sad"
        if self.mood > 70 and self.satiety > 40:
            return "happy"
        return "idle"

    @staticmethod
    def _bar(v: float, width: int = 10) -> str:
        filled = round(clamp(v) / 100 * width)
        return "█" * filled + "░" * (width - filled)

    def status_lines(self, now: dt.datetime) -> list[str]:
        lines = [
            f"🐋 {growth.stage_name(self)} · 陪伴 {(now - dt.datetime.fromisoformat(self.born_at)).days + 1} 天",
            f"饱食度 {self._bar(self.satiety)} {self.satiety:.0f}",
            f"心情   {self._bar(self.mood)} {self.mood:.0f}",
            f"健康   {self._bar(self.health)} {self.health:.0f}",
            f"寿命   {self._bar(self.lifespan_max)} {self.lifespan_max:.0f}",
            f"连续活跃 {self.active_streak} 天（最高 {self.best_streak}）· "
            f"成长值 {growth.growth_points(self)}",
            f"累计 commit 喂食 {self.total_commits} 次"
            + (f" · 云端投喂 {self.gh_remote_commits}" if self.gh_login else ""),
        ]
        if self.focus:
            left = self.focus.remaining_minutes()
            paused = "（已暂停）" if self.focus.paused else ""
            lines.append(f"专注中 · 剩 {left:.0f} 分钟{paused}")
        if self.runaway_until:
            back = dt.datetime.fromisoformat(self.runaway_until)
            lines.append(f"⚠ 离家出走中（{self.runaway_reason}），"
                         f"{back.strftime('%m-%d %H:%M')} 回来")
        return lines

    # ------------------------------------------------------------------ 存档

    def _roll_day(self, now: dt.datetime) -> None:
        today_str = now.date().isoformat()
        if self.today.date != today_str:
            archived = self.today
            self.history[archived.date] = archived.to_dict()
            self.history = dict(sorted(self.history.items())[-HISTORY_KEEP_DAYS:])
            self.today = DayStats.for_day(now.date())
            self.early_today = False
            growth.update_streak(self, archived, now.date())
            self._growth_tick()

    def catch_up(self, now: dt.datetime) -> None:
        """关机期间的补算：只算饥饿和心情，不产生健康/寿命变化。"""
        last = None
        if self.last_save:
            try:
                last = dt.datetime.fromisoformat(self.last_save)
            except (TypeError, ValueError):
                last = None
        if last:
            hours = min((now - last).total_seconds() / 3600, MAX_CATCHUP_HOURS)
            if hours > 0.5:
                self.satiety = clamp(self.satiety - self.mechanics["satiety_decay_per_hour"] * hours)
                self.mood = clamp(self.mood - 0.5 * hours)
                self.events.append({"kind": "welcome_back", "h": hours})

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "satiety": round(self.satiety, 2),
            "mood": round(self.mood, 2),
            "health": round(self.health, 2),
            "lifespan_max": round(self.lifespan_max, 2),
            "total_commits": self.total_commits,
            "born_at": self.born_at,
            "runaway_until": self.runaway_until,
            "runaway_reason": self.runaway_reason,
            "last_roast_date": self.last_roast_date,
            "last_poll": self.last_poll,
            "last_save": dt.datetime.now().isoformat(timespec="seconds"),
            "window_x": self.window_x,
            "window_y": self.window_y,
            "active_streak": self.active_streak,
            "best_streak": self.best_streak,
            "active_days_total": self.active_days_total,
            "total_late_minutes": round(self.total_late_minutes, 1),
            "came_back_once": self.came_back_once,
            "unlocked": self.unlocked,
            "stage_idx": self.stage_idx,
            "total_focus_count": self.total_focus_count,
            "gh_login": self.gh_login,
            "gh_last_event_id": self.gh_last_event_id,
            "gh_remote_commits": self.gh_remote_commits,
            "gh_repos_seen": self.gh_repos_seen[-200:],
            "gh_backfilled": self.gh_backfilled,
            "gh_fed_ids": self.gh_fed_ids[-500:],
            "today": self.today.to_dict(),
            "history": self.history,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        self.dirty = False

    @classmethod
    def load(cls, path: Path, mechanics: dict) -> "PetState":
        st = cls(mechanics)
        if not path.exists():
            return st
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return st
        try:
            # 数值/容器字段：类型不对就整体回退新档
            st.satiety = clamp(float(data.get("satiety", 80)))
            st.mood = clamp(float(data.get("mood", 70)))
            st.health = clamp(float(data.get("health", 90)))
            st.lifespan_max = max(MIN_LIFESPAN, float(data.get("lifespan_max", 100)))
            st.total_commits = int(data.get("total_commits", 0))
            st.window_x = int(data.get("window_x", -1))
            st.window_y = int(data.get("window_y", -1))
            st.active_streak = int(data.get("active_streak", 0))
            st.best_streak = int(data.get("best_streak", 0))
            st.active_days_total = int(data.get("active_days_total", 0))
            st.total_late_minutes = float(data.get("total_late_minutes", 0.0))
            st.came_back_once = bool(data.get("came_back_once", False))
            st.unlocked = dict(data.get("unlocked", {}))
            st.stage_idx = min(max(int(data.get("stage_idx", 0)), 0),
                               len(growth.STAGES) - 1)
            st.total_focus_count = int(data.get("total_focus_count", 0))
            st.gh_remote_commits = int(data.get("gh_remote_commits", 0))
            st.today = DayStats.from_dict(data.get("today", {}))
            st.history = dict(data.get("history", {}))
        except (TypeError, ValueError):
            return cls(mechanics)
        # 日期字段：坏值逐个回退，不丢弃整个存档
        st.born_at = _safe_iso(data.get("born_at"), st.born_at)
        st.runaway_until = _safe_iso(data.get("runaway_until"), None)
        st.runaway_reason = str(data.get("runaway_reason", "") or "")
        st.last_roast_date = str(data.get("last_roast_date", "") or "")
        st.last_save = _safe_iso(data.get("last_save"), None)
        st.last_poll = _safe_iso(data.get("last_poll"), None)
        st.gh_login = str(data.get("gh_login", "") or "")
        st.gh_last_event_id = str(data.get("gh_last_event_id", "") or "")
        st.gh_backfilled = bool(data.get("gh_backfilled", False))
        try:
            st.gh_fed_ids = [str(x) for x in
                             (data.get("gh_fed_ids") or []) if str(x)][-500:]
        except TypeError:
            st.gh_fed_ids = []
        try:
            st.gh_repos_seen = [str(x) for x in
                                (data.get("gh_repos_seen") or [])][-200:]
        except TypeError:
            st.gh_repos_seen = []
        if not st.today.date:
            st.today = DayStats.for_day(dt.date.today())
        return st
