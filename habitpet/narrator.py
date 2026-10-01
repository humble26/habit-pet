"""宠物台词系统：LLM 每日吐槽（GLM Flash）+ 离线模板兜底。

人设：一只生活在程序员桌面上的鲸鱼娘（DeepSeek 小鲸鱼），毒舌但可爱，
自称「本鲸」。
设计约束（对应报告"防跑偏"）：LLM 只做"数据 → 一句话"的有限发挥，
所有事件台词走模板；LLM 不可用时自动降级，宠物永远不会哑巴。
"""
from __future__ import annotations

import json
import os
import random
from typing import Optional

try:
    import requests
except ImportError:          # 没装 requests 也能跑，只是没有 LLM 吐槽
    requests = None          # type: ignore[assignment]

SYSTEM_PROMPT = (
    "你是一只生活在程序员桌面上的鲸鱼娘（DeepSeek 小鲸鱼），毒舌但可爱。"
    "根据用户给你的 JSON 行为数据，用不超过45个字说一句吐槽。"
    "要求：直接说人话，不要罗列字段名，可适当夸张，最多一个表情。"
    "数据平淡时也要有点态度。"
)

SYSTEM_PROMPT_WEEK = (
    "你是一只生活在程序员桌面上的鲸鱼娘（DeepSeek 小鲸鱼），毒舌但可爱。"
    "根据用户一周的行为数据总结，用不超过60个字点评这周，"
    "要求：直接说人话，不罗列字段名，毒舌里带一点关心。"
)

FEED_LINES = [
    "检测到 {n} 个 commit，开饭啦！",
    "{n} 次提交进账，鱼粮缸发出清脆的响声。",
    "又写代码了？{n} 个 commit 换一勺鱼粮，这买卖本鲸不亏。",
]
TREAT_LINES = [
    "投喂成功。虽然治标不治本，但本鲸勉为其难收下了。",
    "手动加餐？看来你还是有点良心的。",
]
SIT_LINES = [
    "已经连续伏案 {mins:.0f} 分钟了！健康 -{dmg:.0f}，起来走两步，本鲸看着你都快长在椅子上了。",
    "你 {mins:.0f} 分钟没挪窝了……健康 -{dmg:.0f}，再这样本鲸就吐水给你看。",
]
REST_LINES = [
    "起来活动了？健康 +{regen:.0f}，这才是本鲸养的人类。",
    "休息 {mins:.0f} 分钟，血条回了一点，勉强放心。",
]
HUNGRY_LINES = [
    "鱼缸见底……饱食度只剩 {s:.0f} 了，今天一个 commit 都没有吗？",
    "饿到本鲸开始啃你的鼠标线了。快去提交点什么！",
]
LATE_LINES = [
    "这个点还醒着？寿命上限正在融化，本鲸先睡了，你自己掂量。",
]
RUNAWAY_LINES = [
    "健康值归零，本鲸出海散心了！{days} 天后回来，我会带不好吃的。（原因：{reason}）",
]
CAME_HOME_LINES = [
    "本鲸回来了。在外面吹够了海风，先 commit 两个再说话。",
    "别问这几天怎么过的。给口鱼粮，我们就当无事发生。",
]
PET_LINES = [
    "呼咕、呼咕……（舒服的鲸叫）",
    "再摸就要收费了。",
    "嗯……心情 +2，继续。",
]
STAGE_UP_LINES = [
    "叮——本鲸进化成{stage}了！这都是你一口一口 commit 喂出来的。",
    "恭喜！本鲸已成长为见多识广的{stage}，请继续投喂。",
]
STREAK_LINES = [
    "连续活跃 {days} 天了，本鲸的鱼粮缸也连续 {days} 天没空过。",
    "{days} 天全勤！你负责努力，本鲸负责监工。",
]
STREAK_BROKEN_LINES = [
    "连续 {days} 天的活跃记录断了……从头再来吧，本鲸假装没生气。",
]
ACHIEVEMENT_LINES = [
    "🏆 成就达成：{title}！{desc}",
    "叮！解锁成就「{title}」——{desc}",
]
FOCUS_START_LINES = [
    "专注 {mins:.0f} 分钟开始！本鲸就守在旁边盯着你，摸鱼就拍你尾巴。",
    "好，{mins:.0f} 分钟，这局本鲸陪你到底。",
]
FOCUS_DONE_LINES = [
    "专注 {mins} 分钟达成！心情 +{reward:.0f}，起来晃两圈再战。",
    "番茄收下！{mins} 分钟一秒没跑，本鲸很满意。",
]
FOCUS_PAUSED_LINES = [
    "人呢？！专注计时暂停了，键盘都要凉了。",
]
FOCUS_RESUMED_LINES = [
    "回来得正好，专注继续。",
]
FOCUS_CANCEL_LINES = [
    "专注取消了……本鲸假装没看见。",
]
PET_RUNAWAY_LINES = [
    "（工位是空的。她还没回来。）",
]
WELCOME_LINES = [
    "离开了 {h:.1f} 小时，本鲸饿得能吃下一整条蓝鲸。先喂饭。",
]
NOCOMMIT_LINES = [
    "今天 {commits} 个 commit……本鲸已经饿得开始反思当初为什么选了你。",
    "零提交的一天。你的鲸鱼靠空气维持生命体征。",
]
OK_LINES = [
    "今天 {commits} 个 commit，活跃 {active_hours:.1f} 小时，久坐 {sit_hits} 次——还能抢救。",
    "{commits} 次提交、{breaks} 次休息，节奏还行，本鲸勉强满意。",
]
WEEK_FALLBACK_LINES = [
    "本周 {commits} 个 commit、活跃 {active_hours:.1f} 小时、熬夜 {late_hours:.1f} 小时。数据不会说谎，但本鲸可以替你婉转。",
    "这周 {commits} 次提交、{sit_hits} 次久坐扣血、{breaks} 次休息。总体及格，本鲸盯着你呢。",
]
LATE_ROAST_LINES = [
    "本周熬夜 {late_hours:.1f} 小时，本鲸的寿上限都在替你买单。",
]
BALANCE_LOW_LINES = [
    "🔔 余额预警：账户只剩 {balance} 了，再这么烧下去本鲸就要喝西北风了。",
    "余额只剩 {balance} 了！省着点用，本鲸还等着你养呢。",
]
GH_CONNECTED_LINES = [
    "GitHub 洋流接通了！@{login} 的每次推送，本鲸在海里都能闻到。",
    "连上 @{login} 了。以后你在别的机器上敲的代码，也归本鲸监督。",
]
GH_FEED_LINES = [
    "云端投喂 +{n} 个 commit（{repos}）！隔着一片海都能闻到鱼粮味。",
    "收到来自 {repos} 的 {n} 个 commit，本鲸的云端粮仓叮当作响。",
]


class Narrator:
    """台词生成器。llm_cfg 为 config["llm"]。"""

    def __init__(self, llm_cfg: dict, log=None) -> None:
        self.cfg = llm_cfg
        self.log = log or (lambda _m: None)

    # ------------------------------------------------------------ 模板台词

    def line(self, kind: str, **kw) -> str:
        table = {
            "feed": FEED_LINES, "treat": TREAT_LINES,
            "sit_hit": SIT_LINES, "rested": REST_LINES,
            "hungry": HUNGRY_LINES, "late_night": LATE_LINES,
            "runaway": RUNAWAY_LINES, "came_home": CAME_HOME_LINES,
            "pet": PET_LINES, "pet_runaway": PET_RUNAWAY_LINES,
            "welcome_back": WELCOME_LINES,
            "stage_up": STAGE_UP_LINES,
            "streak_milestone": STREAK_LINES,
            "streak_broken": STREAK_BROKEN_LINES,
            "achievement": ACHIEVEMENT_LINES,
            "focus_start": FOCUS_START_LINES,
            "focus_done": FOCUS_DONE_LINES,
            "focus_paused": FOCUS_PAUSED_LINES,
            "focus_resumed": FOCUS_RESUMED_LINES,
            "focus_cancel": FOCUS_CANCEL_LINES,
            "balance_low": BALANCE_LOW_LINES,
            "gh_connected": GH_CONNECTED_LINES,
            "gh_feed": GH_FEED_LINES,
        }
        pool = table.get(kind)
        if not pool:
            return "……"
        text = random.choice(pool).format(**kw)
        return text

    def fallback_roast(self, data: dict) -> str:
        if data.get("commits", 0) == 0:
            return random.choice(NOCOMMIT_LINES).format(**data)
        if data.get("late_minutes", 0) > 60:
            return random.choice(LATE_ROAST_LINES).format(
                late_hours=data["late_minutes"] / 60)
        return random.choice(OK_LINES).format(**data)

    # ------------------------------------------------------------ LLM 吐槽

    def _api_key(self) -> str:
        return (self.cfg.get("api_key") or os.environ.get("GLM_API_KEY") or "").strip()

    def _llm(self, system: str, user_payload: str, max_chars: int) -> Optional[str]:
        if requests is None or not self.cfg.get("enabled", True):
            return None
        key = self._api_key()
        if not key:
            return None
        try:
            resp = requests.post(
                self.cfg.get("base_url"),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={
                    "model": self.cfg.get("model", "glm-4-flash"),
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_payload},
                    ],
                    "max_tokens": 150,
                    "temperature": 0.9,
                },
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:   # 网络/密钥/解析任何失败都降级到模板
            self.log(f"[narrator] LLM 降级：{e}")
            return None
        text = text.strip().strip('"“”`')
        return text[:max_chars] if text else None

    def daily_roast(self, data: dict) -> tuple[str, bool]:
        """(台词, 是否来自 LLM)。data 是当日统计快照。"""
        payload = json.dumps(data, ensure_ascii=False)
        text = self._llm(SYSTEM_PROMPT, payload, max_chars=80)
        if text:
            return text, True
        return self.fallback_roast(data), False

    def weekly_comment(self, data: dict) -> tuple[str, bool]:
        payload = json.dumps(data, ensure_ascii=False)
        text = self._llm(SYSTEM_PROMPT_WEEK, payload, max_chars=100)
        if text:
            return text, True
        if data.get("commits", 0) == 0:
            return ("本周零提交。本鲸饿瘦了，你呢？", False)
        return random.choice(WEEK_FALLBACK_LINES).format(**data), False
