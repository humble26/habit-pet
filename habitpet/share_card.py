"""周报分享卡：把本周数据画成一张可发出去的 PNG（产出即宣传物）。

依赖可选的 Pillow；没安装时返回 None，由 UI 提示安装。
猫的绘制与桌宠共用 theme.py 配色和同一套设计坐标（以 85,100 为中心缩放）。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import growth
from .report import week_summary
from .state import PetState
from .theme import (BELLY, BODY, BODY_DARK, CARD_BG, LINE, PINK, BLUSH)

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:                    # pragma: no cover
    HAS_PIL = False

CARD_W, CARD_H = 960, 700

_FONT_CANDIDATES = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
                    "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/arial.ttf"]


def _font(size: int, bold: bool = False):
    if not HAS_PIL:
        return None
    paths = ([_FONT_CANDIDATES[0]] if bold else []) + _FONT_CANDIDATES
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()   # pragma: no cover


def _bar(d, x: float, y: float, w: float, h: float, ratio: float,
         track: str = "#eadfce", fill: str = BODY_DARK) -> None:
    ratio = max(0.0, min(1.0, ratio))
    d.rectangle([x, y, x + w, y + h], fill=track)
    if ratio > 0:
        d.rectangle([x, y, x + w * ratio, y + h], fill=fill)


def _draw_cat(d, cx: float, cy: float, s: float) -> None:
    """开心表情的猫（与 pet_window 同一套设计坐标）。"""
    def P(x: float, y: float):
        return (cx + (x - 85) * s, cy + (y - 100) * s)

    lw = max(2, round(2 * s))
    # 尾巴
    d.arc([*P(4, 38), *P(48, 98)], start=110, end=320,
          fill=BODY_DARK, width=max(4, round(5 * s)))
    # 耳朵
    d.polygon([P(50, 66), P(40, 20), P(86, 44)], fill=BODY, outline=LINE, width=lw)
    d.polygon([P(120, 66), P(130, 20), P(84, 44)], fill=BODY, outline=LINE, width=lw)
    d.polygon([P(54, 56), P(49, 32), P(74, 44)], fill=PINK)
    d.polygon([P(116, 56), P(121, 32), P(96, 44)], fill=PINK)
    # 身体 + 肚皮 + 爪子
    d.ellipse([*P(38, 50), *P(132, 152)], fill=BODY, outline=LINE, width=lw)
    d.ellipse([*P(62, 102), *P(108, 148)], fill=BELLY)
    d.ellipse([*P(56, 138), *P(84, 154)], fill=BODY, outline=LINE, width=lw)
    d.ellipse([*P(86, 138), *P(114, 154)], fill=BODY, outline=LINE, width=lw)
    # 眼睛（眯眯笑 ∩∩）
    d.arc([*P(61, 76), *P(75, 92)], 180, 360, fill=LINE, width=max(3, lw))
    d.arc([*P(95, 76), *P(109, 92)], 180, 360, fill=LINE, width=max(3, lw))
    # 鼻子 + w 嘴
    d.polygon([P(80, 100), P(90, 100), P(85, 106)], fill=PINK, outline=LINE)
    d.arc([*P(77, 108), *P(85, 118)], 0, 180, fill=LINE, width=lw)
    d.arc([*P(85, 108), *P(93, 118)], 0, 180, fill=LINE, width=lw)
    # 腮红 + 胡须
    d.ellipse([*P(52, 100), *P(64, 106)], fill=BLUSH)
    d.ellipse([*P(106, 100), *P(118, 106)], fill=BLUSH)
    for dy in (-3, 3):
        d.line([*P(44, 96 + dy), *P(12, 90 + dy * 2)], fill=LINE, width=2)
        d.line([*P(126, 96 + dy), *P(158, 90 + dy * 2)], fill=LINE, width=2)


def render_weekly_card(state: PetState, out_dir: Path) -> Path | None:
    """生成分享卡；没有 Pillow 时返回 None。"""
    if not HAS_PIL:
        return None
    summary = week_summary(state)
    total = summary["total"]
    today = dt.date.today()
    start = today - dt.timedelta(days=6)

    img = Image.new("RGB", (CARD_W, CARD_H), CARD_BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline=BODY, width=4)
    d.text((40, 34), "Habit Pet 周报", fill=LINE, font=_font(40, bold=True))
    d.text((40, 92), f"{start.isoformat()} ~ {today.isoformat()}"
                     f"  ·  第 {today.isocalendar()[1]} 周",
           fill="#8a7a5c", font=_font(20))

    _draw_cat(d, 190, 330, 2.0)

    tx, ty, step = 380, 150, 48
    lines = [
        (f"成长阶段：{growth.stage_name(state)}"
         f"（成长值 {growth.growth_points(state)}）", None),
        (f"连续活跃 {state.active_streak} 天（最高 {state.best_streak}）", None),
        (f"commit 喂食：{total['commits']} 次",
         min(1.0, total["commits"] / 30)),
        (f"伏案活跃：{total['active_hours']} 小时",
         min(1.0, total["active_hours"] / 40)),
        (f"专注完成：{total.get('focus_count', 0)} 颗番茄",
         min(1.0, total.get("focus_count", 0) / 10)),
        (f"久坐扣血 {total['sit_hits']} 次 · 休息 {total['breaks']} 次", None),
        (f"熬夜：{total['late_hours']} 小时（寿上限 {state.lifespan_max:.0f}/100）",
         min(1.0, total["late_hours"] / 8)),
        (f"成就：{len(state.unlocked)}/{len(growth.ACHIEVEMENTS)}",
         len(state.unlocked) / max(1, len(growth.ACHIEVEMENTS))),
    ]
    y = ty
    for text, ratio in lines:
        d.text((tx, y), text, fill=LINE, font=_font(24))
        if ratio is not None:
            _bar(d, tx, y + 32, 420, 12, ratio)
            y += 16
        y += step

    d.text((40, CARD_H - 48),
           f"Habit Pet v0.3.0 · 所有数据只来自这台电脑 · 生成于 {today.isoformat()}",
           fill="#8a7a5c", font=_font(18))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"card-{today.isoformat()}.png"
    img.save(path, "PNG")
    return path
