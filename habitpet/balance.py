"""小鲸鱼记账（并入 DSH 挂件玩法，独立实现）：DeepSeek 余额直查 + 今日已用观测。

- key 解析顺序：habitpet 配置 → 环境变量 DEEPSEEK_API_KEY →
  DSH 凭据 ~/.dsh/.credentials.yaml（只读，值不进日志）
- 余额：GET https://api.deepseek.com/user/balance（Bearer），优先取 CNY 账户
- 今日已用：余额差观测——当天余额的**下降**累计为消费；余额上升（充值/赠金）
  单独记录、不冲抵已有消费（与 DSH 挂件的记账口径一致）
- 持久化：~/.habitpet/balance.json（原子写）；查询在后台线程，主循环只消费队列
- 网络 / 密钥任何失败都静默降级（回到"暂无数据"），绝不影响桌宠主循环
"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import threading
from pathlib import Path

API_URL = "https://api.deepseek.com/user/balance"
MIN_POLL_SECONDS = 60
DSH_CREDS = Path.home() / ".dsh" / ".credentials.yaml"

_CRED_LINE = re.compile(r"^\s*DEEPSEEK_API_KEY\s*:\s*(.+?)\s*$")


def format_money(value: float, currency: str = "CNY") -> str:
    cur = (currency or "CNY").upper()
    sym = {"CNY": "¥", "USD": "$"}.get(cur, cur + " ")
    return f"{sym}{value:.2f}"


class BalanceTracker:
    """余额观测器。所有对外方法都保证不抛异常、不阻塞主线程。"""

    def __init__(self, config_dir: Path, conf: dict, log=None,
                 session=None, creds_path: Path | None = None) -> None:
        self.dir = Path(config_dir)
        self.conf = dict(conf or {})
        self.log = log or (lambda _m: None)
        self.path = self.dir / "balance.json"
        self._session = session                 # 测试注入；None 时用 requests
        self._creds_path = Path(creds_path) if creds_path else DSH_CREDS
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._running = False
        self._last_poll = 0.0
        self._no_key_logged = False
        self.key = (self.resolve_key()
                    if bool(self.conf.get("enabled", True)) else "")
        self.data = self._load()

    # ------------------------------------------------------------ 凭据

    def resolve_key(self) -> str:
        k = str(self.conf.get("api_key") or "").strip()
        if k:
            return k
        k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if k:
            return k
        return self._key_from_dsh()

    def _key_from_dsh(self) -> str:
        """只读解析 DSH 凭据文件里的 DEEPSEEK_API_KEY 行（YAML 的最简子集）。"""
        try:
            if not self._creds_path.exists():
                return ""
            for raw in self._creds_path.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                m = _CRED_LINE.match(raw)
                if not m:
                    continue
                val = m.group(1).strip()
                if val[:1] in ('"', "'"):   # 引号值：取引号内内容
                    end = val.find(val[0], 1)
                    if end == -1:
                        return ""
                    val = val[1:end]
                else:                       # 裸值：截到行尾注释
                    val = val.split("#", 1)[0].strip()
                if not val or val.startswith(("env:", "${", "*")):
                    return ""               # 非法/引用型值一律当没有
                return val
        except OSError:
            return ""
        return ""

    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True)) and bool(self.key)

    # ------------------------------------------------------------ 轮询

    def maybe_poll(self, now: float | None = None, force: bool = False) -> bool:
        """到点/手动时启动后台查询线程。返回是否真的启动了。"""
        if not self.enabled() or self._running:
            return False
        now = now if now is not None else dt.datetime.now().timestamp()
        interval = max(MIN_POLL_SECONDS,
                       int(self.conf.get("poll_seconds", 300) or 300))
        if not force and now - self._last_poll < interval:
            return False
        self._running = True
        self._last_poll = now
        threading.Thread(target=self._work, daemon=True).start()
        return True

    def _work(self) -> None:
        try:
            info = self.fetch()
            if info:
                self.observe(info)
            self._queue.put(("balance_ok", self.snapshot()))
        except Exception as e:
            self._queue.put(("balance_err", repr(e)))

    def fetch(self) -> dict | None:
        """查询余额接口。返回 {balance, currency, at} 或 None（静默）。"""
        data = self._get_json(API_URL)
        if not isinstance(data, dict):
            return None
        infos = data.get("balance_infos") or []
        if not isinstance(infos, list) or not infos:
            return None
        pick = next(
            (i for i in infos
             if isinstance(i, dict)
             and str(i.get("currency", "")).upper() == "CNY"),
            infos[0],
        )
        if not isinstance(pick, dict):
            return None
        try:
            balance = float(pick.get("total_balance"))
        except (TypeError, ValueError):
            return None
        return {"balance": balance,
                "currency": str(pick.get("currency") or "CNY").upper(),
                "at": dt.datetime.now().isoformat(timespec="seconds")}

    def _get_json(self, url: str):
        sess = self._session
        if sess is None:
            try:
                import requests
            except ImportError:
                return None
            sess = requests
        resp = sess.get(url, headers={"Authorization": f"Bearer {self.key}"},
                        timeout=12)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ 记账

    def observe(self, info: dict) -> None:
        """把一次余额观测并入今日账本（下降=消费；上升=充值，不冲消费）。"""
        today = dt.date.today().isoformat()
        d = self.data if isinstance(self.data, dict) else {}
        if d.get("date") != today:
            d = {"date": today, "currency": info["currency"],
                 "opening": info["balance"], "last": info["balance"],
                 "debit": 0.0, "credit": 0.0,
                 "alerted_date": d.get("alerted_date", "")}
        else:
            try:
                last = float(d.get("last", info["balance"]))
            except (TypeError, ValueError):
                last = info["balance"]
            delta = last - info["balance"]
            if delta > 0:
                d["debit"] = round(float(d.get("debit", 0.0)) + delta, 6)
            elif delta < 0:
                d["credit"] = round(float(d.get("credit", 0.0)) - delta, 6)
            d["last"] = info["balance"]
            d["currency"] = info["currency"]
        d["updated_at"] = info["at"]
        self.data = d
        self._save()

    def check_low(self) -> bool:
        """低于阈值预警（每天最多一次）。返回是否本次触发。"""
        if not self.conf.get("alert_enabled", True):
            return False
        try:
            threshold = float(self.conf.get("low_alert", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        snap = self.snapshot()
        if threshold <= 0 or snap is None or snap["balance"] > threshold:
            return False
        today = dt.date.today().isoformat()
        if self.data.get("alerted_date") == today:
            return False
        self.data["alerted_date"] = today
        self._save()
        return True

    # ------------------------------------------------------------ 对外视图

    def snapshot(self) -> dict | None:
        d = self.data if isinstance(self.data, dict) else {}
        if "last" not in d:
            return None
        try:
            return {
                "balance": float(d["last"]),
                "currency": str(d.get("currency", "CNY")),
                "today_used": float(d.get("debit", 0.0))
                if d.get("date") == dt.date.today().isoformat() else 0.0,
                "updated_at": str(d.get("updated_at", "")),
                "date": str(d.get("date", "")),
            }
        except (TypeError, ValueError):
            return None

    def status_lines(self) -> list[str]:
        snap = self.snapshot()
        if snap is None:
            if not self.conf.get("enabled", True):
                return ["💰 余额：小鲸鱼记账已关闭（config.json → balance.enabled）"]
            if not self.key:
                return ["💰 余额：未配置 key（config.json / 环境变量 / DSH 凭据任一即可）"]
            return ["💰 余额：暂无数据（右键菜单点一次刷新）"]
        used = snap["today_used"]
        line = (f"💰 余额 {format_money(snap['balance'], snap['currency'])}"
                f" · 今日已用 ≈ {format_money(used, snap['currency'])}")
        if snap["date"] != dt.date.today().isoformat():
            line += "（今日尚未观测）"
        return [line]

    def menu_label(self) -> str:
        snap = self.snapshot()
        if not self.conf.get("enabled", True):
            return "余额 · 已关闭（点我看说明）"
        if snap is None:
            if not self.key:
                return "余额 · 未配置（点我看说明）"
            return "余额 · 暂无数据（点我查询）"
        return f"余额 · {format_money(snap['balance'], snap['currency'])}（点我刷新）"

    def drain(self) -> list[tuple]:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    # ------------------------------------------------------------ 持久化

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            self.log(f"[balance] 账本写入失败：{e!r}")
