"""养成与留存系统（v0.2 新增）。

对应前景报告的核心判断："留存是最大风险，解法是把宠物养成挂在用户真实成长上，
让弃养成本变成养成记录清零。"

三个部分：
- 成长阶段：成长值 = commit 数 + 活跃天数×5，阶段只升不降（幼鲸 → 鲸鱼娘 → 深海鲸鱼娘）
- 连续活跃 streak：每天有 commit 或伏案 ≥30 分钟记为活跃天，跨天时结算
- 成就：一次性里程碑，解锁即永久保存
"""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:                     # 仅类型标注，避免与 state 循环导入
    from .state import DayStats, PetState

ACTIVE_DAY_MINUTES = 30               # 伏案满 30 分钟记为活跃天
STREAK_MILESTONES = (3, 7, 14, 30, 60, 100)

# ---------------------------------------------------------------- 成长阶段

# (名称, 所需成长值, 绘制缩放)
STAGES: list[tuple[str, int, float]] = [
    ("幼鲸", 0, 0.82),
    ("鲸鱼娘", 20, 1.0),
    ("深海鲸鱼娘", 100, 1.14),
]


def growth_points(state: "PetState") -> int:
    return int(state.total_commits + state.active_days_total * 5)


def stage_index(points: int) -> int:
    idx = 0
    for i, (_, need, _scale) in enumerate(STAGES):
        if points >= need:
            idx = i
    return idx


def stage_name(state: "PetState") -> str:
    return STAGES[state.stage_idx][0]


def stage_scale(state: "PetState") -> float:
    return STAGES[state.stage_idx][2]


def next_stage_need(state: "PetState") -> Optional[int]:
    idx = state.stage_idx
    return STAGES[idx + 1][1] if idx + 1 < len(STAGES) else None


def refresh_stage(state: "PetState") -> None:
    """成长值变化后调用，跨过阈值则触发进化事件（只升不降）。"""
    idx = stage_index(growth_points(state))
    if idx > state.stage_idx:
        state.stage_idx = idx
        state.events.append({"kind": "stage_up", "stage": STAGES[idx][0]})


# ---------------------------------------------------------------- 连续活跃

def is_active_day(archived: "DayStats") -> bool:
    return archived.commits > 0 or archived.active_minutes >= ACTIVE_DAY_MINUTES


def update_streak(state: "PetState", archived: "DayStats",
                  rollover_date: dt.date) -> None:
    """跨天结算（state._roll_day 调用）。archived 是刚归档的昨天。

    rollover_date 是跨天后的日期；archived 应恰好是它的前一天，否则视为断档。
    """
    if is_active_day(archived):
        state.active_days_total += 1

    expected = (rollover_date - dt.timedelta(days=1)).isoformat()
    gap = archived.date != expected

    if gap or not is_active_day(archived):
        if state.active_streak >= 3:
            state.events.append({"kind": "streak_broken", "days": state.active_streak})
        state.active_streak = 0
        return

    state.active_streak += 1
    state.best_streak = max(state.best_streak, state.active_streak)
    if state.active_streak in STREAK_MILESTONES:
        state.events.append({"kind": "streak_milestone", "days": state.active_streak})


# ---------------------------------------------------------------- 成就

class Achievement:
    __slots__ = ("id", "title", "desc", "check")

    def __init__(self, aid: str, title: str, desc: str,
                 check: Callable[["PetState"], bool]) -> None:
        self.id = aid
        self.title = title
        self.desc = desc
        self.check = check


def _companion_days(state: "PetState") -> int:
    born = dt.datetime.fromisoformat(state.born_at)
    return (dt.datetime.now() - born).days


ACHIEVEMENTS: list[Achievement] = [
    Achievement("first_feed", "初见鲸喜", "第一次用 commit 喂食",
                lambda s: s.total_commits >= 1),
    Achievement("commits_10", "青铜饲养员", "累计喂食 10 个 commit",
                lambda s: s.total_commits >= 10),
    Achievement("commits_50", "白银饲养员", "累计喂食 50 个 commit",
                lambda s: s.total_commits >= 50),
    Achievement("commits_100", "黄金饲养员", "累计喂食 100 个 commit",
                lambda s: s.total_commits >= 100),
    Achievement("commits_365", "王者饲养员", "累计喂食 365 个 commit",
                lambda s: s.total_commits >= 365),
    Achievement("streak_3", "三日温存", "连续活跃 3 天",
                lambda s: s.best_streak >= 3),
    Achievement("streak_7", "周全勤", "连续活跃 7 天",
                lambda s: s.best_streak >= 7),
    Achievement("streak_30", "月全勤", "连续活跃 30 天",
                lambda s: s.best_streak >= 30),
    Achievement("comeback", "破镜重圆", "被你气到出海散心，又回来了",
                lambda s: s.came_back_once),
    Achievement("night_owl", "夜航鲸认证", "累计陪你熬夜满 1 小时",
                lambda s: s.total_late_minutes >= 60),
    Achievement("early_bird", "早鸟", "早上 6 点档就开始敲键盘",
                lambda s: s.early_today),
    Achievement("zen", "劳逸结合", "单日有效休息满 5 次",
                lambda s: s.today.breaks >= 5
                or any(h.get("breaks", 0) >= 5 for h in s.history.values())),
    Achievement("zenith", "身心健康", "饱食/心情/健康同时不低于 90",
                lambda s: s.satiety >= 90 and s.mood >= 90 and s.health >= 90),
    Achievement("veteran", "百日夜", "陪伴满 100 天",
                lambda s: _companion_days(s) >= 100),
    Achievement("focus_1", "第一颗番茄", "完成一次专注",
                lambda s: s.total_focus_count >= 1),
    Achievement("focus_10", "番茄农夫", "累计完成 10 次专注",
                lambda s: s.total_focus_count >= 10),
    Achievement("focus_50", "番茄大亨", "累计完成 50 次专注",
                lambda s: s.total_focus_count >= 50),
    # GitHub 云端（v0.6，远程投喂与多仓库激励）
    Achievement("gh_connect", "接入云端", "鲸鱼娘连上了你的 GitHub 账号",
                lambda s: bool(s.gh_login)),
    Achievement("gh_remote_1", "深海快递", "收到第一次来自 GitHub 云端的投喂",
                lambda s: s.gh_remote_commits >= 1),
    Achievement("gh_remote_20", "云端饲养员", "云端累计投喂 20 个 commit",
                lambda s: s.gh_remote_commits >= 20),
    Achievement("gh_remote_100", "七海巡游", "云端累计投喂 100 个 commit",
                lambda s: s.gh_remote_commits >= 100),
    Achievement("gh_repos_5", "八爪鱼", "云端投喂来自 5 个不同的仓库",
                lambda s: len(s.gh_repos_seen) >= 5),
]


def check_new(state: "PetState") -> list[Achievement]:
    """检查并解锁新成就，返回本次解锁列表（state._growth_tick 会转成事件）。"""
    got: list[Achievement] = []
    for a in ACHIEVEMENTS:
        if a.id in state.unlocked:
            continue
        try:
            if a.check(state):
                state.unlocked[a.id] = dt.date.today().isoformat()
                got.append(a)
        except Exception:
            continue
    return got


def achievement_status(state: "PetState") -> list[str]:
    """成就面板文案（右键菜单“我的成就”）。"""
    unlocked = state.unlocked
    lines = [f"🏆 成就 {len(unlocked)}/{len(ACHIEVEMENTS)} · "
             f"连续活跃 {state.active_streak} 天（最高 {state.best_streak}）"]
    shown = 0
    for a in ACHIEVEMENTS:
        if a.id in unlocked and shown < 8:
            lines.append(f"✅ {a.title}")
            shown += 1
        elif a.id not in unlocked:
            pass
    locked = [a for a in ACHIEVEMENTS if a.id not in unlocked]
    if locked:
        nxt = locked[0]
        lines.append(f"🔒 下一项：{nxt.title}（{nxt.desc}）")
    if len(unlocked) > 8:
        lines.append(f"…另有 {len(unlocked) - 8} 枚已点亮")
    return lines
