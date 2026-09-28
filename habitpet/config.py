"""配置加载：数据与配置统一放在 ~/.habitpet/，首次运行自动生成。"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_VERSION = 1

DEFAULT_MECHANICS: dict = {
    "satiety_decay_per_hour": 4.0,   # 饱食度每小时自然衰减
    "commit_feed": 14.0,             # 每个 commit 喂饱的饱食度
    "commit_mood": 3.0,              # 每个 commit 附带的心情
    "treat_feed": 20.0,              # 手动喂一包粮
    "sit_limit_minutes": 45,         # 连续伏案多久触发久坐掉血
    "sit_damage": 8.0,               # 触发一次扣的健康
    "break_rest_minutes": 10,        # 离开键盘多久算一次有效休息
    "break_regen": 5.0,              # 一次休息回复的健康
    "late_night_start": 0,           # 熬夜判定起（含），24h 制
    "late_night_end": 6,             # 熬夜判定止（不含）
    "late_night_drain_per_hour": 3.0,  # 熬夜每小时掉的寿命上限
    "focus_minutes": 25,             # 专注模式单次时长
    "focus_grace_minutes": 10,       # 专注中离开键盘多久自动暂停
    "focus_reward_mood": 10.0,       # 完成一颗番茄的心情奖励
}

DEFAULT_CONFIG: dict = {
    "version": CONFIG_VERSION,
    # 要扫描的 git 仓库（本地路径），commit 数喂饱食度
    "repos": [],
    "git_poll_seconds": 300,
    "llm": {
        "enabled": True,
        # 留空则读环境变量 GLM_API_KEY；两者都没有时用内置模板吐槽
        "api_key": "",
        "model": "glm-4-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "daily_roast_hour": 21,     # 每日吐槽+日报的触发时刻
        "daily_roast_minute": 30,
    },
    "mechanics": dict(DEFAULT_MECHANICS),
}


def default_config_dir() -> Path:
    return Path.home() / ".habitpet"


def load_config(config_dir: Path) -> tuple[dict, bool]:
    """返回 (config, 是否首次生成)。字段缺失自动补默认值。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    created = False
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            user = {}
        _merge(cfg, user)
    else:
        created = True
        path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    cfg["mechanics"] = {**DEFAULT_MECHANICS, **cfg.get("mechanics", {})}
    return cfg, created


def _merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
