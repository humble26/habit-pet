"""日报 / 周报生成（宠物口吻），写入 ~/.habitpet/reports/。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .narrator import Narrator
from .state import PetState


def week_summary(state: PetState) -> dict:
    """最近 7 天（含今天）的逐日数据与汇总。"""
    today = dt.date.today()
    days = []
    for i in range(6, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        if d == state.today.date:
            days.append(state.today.to_dict())
        else:
            days.append(state.history.get(d, {"date": d, "commits": 0,
                                              "active_minutes": 0.0, "sit_hits": 0,
                                              "breaks": 0, "late_minutes": 0.0,
                                              "roasts": 0, "focus_count": 0}))
    total = {
        "commits": sum(d["commits"] for d in days),
        "active_hours": round(sum(d["active_minutes"] for d in days) / 60, 1),
        "sit_hits": sum(d["sit_hits"] for d in days),
        "breaks": sum(d["breaks"] for d in days),
        "late_hours": round(sum(d["late_minutes"] for d in days) / 60, 1),
        "focus_count": sum(d.get("focus_count", 0) for d in days),
    }
    return {"days": days, "total": total}


def write_daily(state: PetState, roast: str, is_llm: bool, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    t = state.today
    lines = [
        f"# 🐾 Habit Pet 日报 · {t.date}",
        "",
        f"> {roast}",
        f"> （{'LLM 生成' if is_llm else '离线模板'}）",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| commit 喂食 | {t.commits} 次 |",
        f"| 伏案活跃 | {t.active_minutes / 60:.1f} 小时 |",
        f"| 久坐扣血 | {t.sit_hits} 次 |",
        f"| 有效休息 | {t.breaks} 次 |",
        f"| 专注完成 | {t.focus_count} 颗番茄 |",
        f"| 熬夜时长 | {t.late_minutes:.0f} 分钟 |",
        f"| 寿命上限 | {state.lifespan_max:.0f}/100 |",
        "",
        f"当前状态：饱食 {state.satiety:.0f} · 心情 {state.mood:.0f} · 健康 {state.health:.0f}",
        "",
    ]
    path = out_dir / f"daily-{t.date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_weekly(state: PetState, out_dir: Path, narrator: Narrator) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = week_summary(state)
    total = summary["total"]
    comment, is_llm = narrator.weekly_comment(total)
    today = dt.date.today()
    week_no = today.isocalendar()[1]
    lines = [
        f"# 🐾 Habit Pet 周报 · {today.year} 年第 {week_no} 周",
        "",
        f"> {comment}",
        f"> （{'LLM 生成' if is_llm else '离线模板'}）",
        "",
        "## 本周汇总",
        "",
        f"- commit 喂食：**{total['commits']}** 次",
        f"- 伏案活跃：**{total['active_hours']}** 小时",
        f"- 专注完成：**{total.get('focus_count', 0)}** 颗番茄",
        f"- 久坐扣血：**{total['sit_hits']}** 次（休息 {total['breaks']} 次）",
        f"- 熬夜：**{total['late_hours']}** 小时",
        "",
        "## 逐日明细",
        "",
        "| 日期 | commits | 活跃(h) | 专注 | 久坐 | 休息 | 熬夜(min) |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in summary["days"]:
        lines.append(
            f"| {d['date']} | {d['commits']} | {d['active_minutes'] / 60:.1f} "
            f"| {d.get('focus_count', 0)} | {d['sit_hits']} | {d['breaks']} "
            f"| {d['late_minutes']:.0f} |"
        )
    lines += ["", f"累计 commit 喂食总数：{state.total_commits}", ""]
    path = out_dir / f"weekly-{today.isocalendar()[0]}-W{week_no:02d}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
