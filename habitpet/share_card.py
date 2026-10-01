"""周报分享卡：把本周数据画成一张可发出去的 PNG（产出即宣传物）。

依赖可选的 Pillow；没安装时返回 None，由 UI 提示安装。
鲸鱼娘插画直接使用 assets/DSniang1.png（与桌宠同一张形象图）；
没有素材时退化为占位符，卡片功能不受影响。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import __version__, growth
from .report import week_summary
from .state import PetState
from .theme import ACCENT, CARD_BG, DEEP, HAIR, MUTED
from .whale_art import ASSETS_DIR, DEFAULT_PET

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:                    # pragma: no cover
    HAS_PIL = False

CARD_W, CARD_H = 960, 770

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
         track: str = "#dde9f7", fill: str = ACCENT) -> None:
    ratio = max(0.0, min(1.0, ratio))
    d.rectangle([x, y, x + w, y + h], fill=track)
    if ratio > 0:
        d.rectangle([x, y, x + w * ratio, y + h], fill=fill)


def _paste_whale(card, cx: float, cy: float, size: float) -> bool:
    """把鲸鱼娘贴到卡片上（居中 cx,cy，高 size px）；失败返回 False。"""
    try:
        im = Image.open(ASSETS_DIR / DEFAULT_PET).convert("RGBA")
        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
        w = max(1, round(im.width * size / im.height))
        im = im.resize((w, max(1, round(size))), Image.LANCZOS)
        card.paste(im, (round(cx - w / 2), round(cy - size / 2)), im)
        return True
    except Exception:
        return False


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

    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline=ACCENT, width=4)
    d.text((40, 34), "Habit Pet 周报 · 鲸鱼娘", fill=HAIR, font=_font(40, bold=True))
    d.text((40, 92), f"{start.isoformat()} ~ {today.isoformat()}"
                     f"  ·  第 {today.isocalendar()[1]} 周",
           fill=MUTED, font=_font(20))

    if not _paste_whale(img, 195, 400, 330):
        d.text((90, 380), "🐋", fill=ACCENT, font=_font(80, bold=True))

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
    if state.gh_login:
        gh = total.get("gh_pushes", 0)
        lines.insert(6, (f"GitHub @{state.gh_login}：本周云端投喂 {gh} 个 commit"
                         f"（累计 {state.gh_remote_commits}）",
                         min(1.0, gh / 20) if gh else None))
    y = ty
    for text, ratio in lines:
        d.text((tx, y), text, fill=DEEP, font=_font(24))
        if ratio is not None:
            _bar(d, tx, y + 32, 420, 12, ratio)
            y += 16
        y += step

    d.text((40, CARD_H - 48),
           f"Habit Pet v{__version__} · 鲸鱼娘版 · "
           f"数据只留在你电脑/你的 GitHub 账号 · 生成于 {today.isoformat()}",
           fill=MUTED, font=_font(18))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"card-{today.isoformat()}.png"
    img.save(path, "PNG")
    return path
