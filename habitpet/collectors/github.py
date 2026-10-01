"""GitHub 连接：远程投喂 + 动态展示 + 专属统计。

- 登录态全自动复用：config.github.token → 本机 gh CLI（`gh auth token`）→ 匿名公开数据
- 只读 push 事件（仓库名 / commit 数 / 时间），不读取任何代码内容
- token 只在内存里流转，不进日志、不落磁盘（gh CLI 的凭据留在 gh 自己那里）
- 「远程投喂」只补本地 GitPoller 管不到的仓库：本地配置的仓库按 origin 归一化后排除，
  同一个 push 不会被本地扫描和云端事件喂两遍
- 首次连接 / 游标滚出事件列表时按「新基线」处理：只认 24 小时内远程仓库的推送，
  上限 40 个 commit，不会一上来回溯几百条历史把宠物撑死
"""
from __future__ import annotations

import datetime as dt
import os
import queue
import re
import subprocess
import threading
from typing import Callable, Optional

API_ROOT = "https://api.github.com"
DEFAULT_POLL_SECONDS = 300
MIN_POLL_SECONDS = 60            # 也覆盖 GitHub 的 X-Poll-Interval 下限
BASELINE_WINDOW_HOURS = 24
BASELINE_MAX_COMMITS = 40
_UA = "habit-pet/0.6 (desktop pet; +https://github.com/humble26/habit-pet)"


# ---------------------------------------------------------------- 本地命令

def _run_cli(args: list[str], timeout: float = 10.0) -> Optional[str]:
    """跑一个只读命令取 stdout；失败/超时/不存在都返回 None。"""
    kw: dict = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kw = {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, **kw)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def gh_cli_token() -> Optional[str]:
    """复用本机 gh CLI 的登录 token（只在内存里传递，绝不打印）。"""
    out = _run_cli(["gh", "auth", "token"])
    if out and "\n" not in out and 20 <= len(out) <= 400:
        return out
    return None


def git_config_github_user() -> Optional[str]:
    """很多开发者在 git config 里写过 github.user，匿名模式下也够用。"""
    return _run_cli(["git", "config", "--get", "github.user"])


_RX_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
                        re.IGNORECASE)


def normalize_remote(url: str) -> Optional[str]:
    """把各种 GitHub 远端写法归一成 owner/repo。

    支持 https://github.com/a/b(.git) / git@github.com:a/b.git /
    ssh://git@github.com/a/b.git；其它 host 返回 None。
    """
    m = _RX_REMOTE.search((url or "").strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def local_repo_names(repos: list[str]) -> set[str]:
    """本地仓库的 origin 归一集合（小写），用来排除重复投喂。"""
    names: set[str] = set()
    for r in repos or []:
        url = _run_cli(["git", "-C", str(r), "remote", "get-url", "origin"])
        name = normalize_remote(url or "")
        if name:
            names.add(name.lower())
    return names


# ---------------------------------------------------------------- 事件解析

def _parse_time(value) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def parse_push_events(events: list[dict]) -> list[dict]:
    """从 /users/{u}/events 响应里抽 PushEvent（保持新→旧顺序）。

    每项：{"id", "repo"("owner/name"), "commits"(distinct_size 兜底链), "at"}。
    """
    out: list[dict] = []
    for e in events or []:
        if not isinstance(e, dict) or e.get("type") != "PushEvent":
            continue
        payload = e.get("payload") or {}
        commits = payload.get("distinct_size")
        if not isinstance(commits, int):
            commits = payload.get("size")
        if not isinstance(commits, int):
            commits = len(payload.get("commits") or [])
        repo = ((e.get("repo") or {}).get("name") or "")
        out.append({
            "id": str(e.get("id") or ""),
            "repo": str(repo),
            "commits": max(0, int(commits or 0)),
            "at": _parse_time(e.get("created_at")),
        })
    return out


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- HTTP 封装

class GitHubClient:
    """会话注入便于测试；token 解析带缓存，失效时可刷新重试。"""

    def __init__(self, conf: dict, session=None, log: Optional[Callable[[str], None]] = None) -> None:
        self.conf = dict(conf or {})
        self._session = session
        self.log = log or (lambda _m: None)
        self._token: Optional[str] = None
        self._resolved = False

    def _sess(self):
        sess = self._session
        if sess is None:
            import requests
            sess = requests
        return sess

    def _headers(self, token: str) -> dict:
        h = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _UA,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def token(self, refresh: bool = False) -> str:
        """config 手填 → gh CLI；只在内存缓存。"""
        if refresh:
            self._resolved = False
        if not self._resolved:
            self._resolved = True
            tok = str(self.conf.get("token") or "").strip()
            if not tok:
                tok = gh_cli_token() or ""
            self._token = tok or None
        return self._token or ""

    def fetch_login(self, token: str) -> Optional[str]:
        try:
            resp = self._sess().get(f"{API_ROOT}/user",
                                    headers=self._headers(token), timeout=15)
        except Exception as e:
            self.log(f"[github] /user 请求失败：{e!r}")
            return None
        if resp.status_code == 200:
            try:
                return str((resp.json() or {}).get("login") or "") or None
            except Exception:
                return None
        return None

    def fetch_events(self, login: str, token: str, etag: str = "") -> dict:
        """返回 {status: ok/none/auth/rate/err, events?, etag?, poll_floor?, note?}。"""
        headers = self._headers(token)
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = self._sess().get(
                f"{API_ROOT}/users/{login}/events?per_page=100",
                headers=headers, timeout=15)
        except Exception as e:
            return {"status": "err", "note": f"网络请求失败：{str(e)[:80]}"}
        code = resp.status_code
        hdr = getattr(resp, "headers", None) or {}
        if code == 304:
            return {"status": "none"}
        if code == 200:
            try:
                events = resp.json() or []
            except Exception:
                return {"status": "err", "note": "事件响应解析失败"}
            return {"status": "ok", "events": events,
                    "etag": hdr.get("ETag", "") or "",
                    "rate_left": _as_int(hdr.get("X-RateLimit-Remaining")),
                    "poll_floor": _as_int(hdr.get("X-Poll-Interval"))}
        if code in (401, 403):
            if _as_int(hdr.get("X-RateLimit-Remaining")) == 0:
                return {"status": "rate", "note": "GitHub 接口限流"}
            return {"status": "auth", "note": "登录态被拒绝"}
        if code == 404:
            return {"status": "err", "note": f"找不到用户 {login}"}
        return {"status": "err", "note": f"HTTP {code}"}


# ---------------------------------------------------------------- 轮询器

class GitHubPoller:
    """给 UI 层的即插即用接口（对齐 balance / credits 的轮询模式）。"""

    def __init__(self, conf: dict, repos: Optional[list[str]] = None,
                 client: Optional[GitHubClient] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.conf = dict(conf or {})
        self.log = log or (lambda _m: None)
        self.client = client or GitHubClient(self.conf, log=self.log)
        self._repos = list(repos or [])
        self._local: Optional[set[str]] = None
        self._login_cache = ""
        self._etag = ""
        self._poll_floor = 0
        self._last: dict = {}
        self._running = False
        self._last_poll = 0.0
        self._queue: "queue.Queue[tuple]" = queue.Queue()

    # ------------------------------------------------------------ 基础

    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def local_names(self) -> set[str]:
        """惰性计算本地仓库 origin 集合（只算一次）。"""
        if self._local is None:
            self._local = local_repo_names(self._repos)
        return self._local

    def last(self) -> dict:
        return self._last

    # ------------------------------------------------------------ 轮询

    def maybe_poll(self, cursor: str = "", force: bool = False,
                   now: Optional[float] = None) -> bool:
        if not self.enabled() or self._running:
            return False
        t = now if now is not None else dt.datetime.now().timestamp()
        interval = max(MIN_POLL_SECONDS, self._poll_floor,
                       int(self.conf.get("poll_seconds", DEFAULT_POLL_SECONDS)
                           or DEFAULT_POLL_SECONDS))
        if not force and t - self._last_poll < interval:
            return False
        self._running = True
        self._last_poll = t
        threading.Thread(target=self._work, args=(str(cursor or ""),),
                         daemon=True).start()
        return True

    def _work(self, cursor: str) -> None:
        try:
            payload = self._fetch(cursor)
        except Exception as e:               # 线程里兜住一切，绝不上抛
            payload = {"status": "err", "note": f"查询出错：{e!r}"[:120]}
        self._last = payload
        self._queue.put(("github_done", payload))
        self._running = False

    # ------------------------------------------------------------ 连接解析

    def _resolve(self) -> tuple[str, str]:
        """(login, token)。login：config → /user → git config github.user。"""
        conf_login = str(self.conf.get("username") or "").strip().lstrip("@")
        if conf_login:
            return conf_login, self.client.token()
        if self._login_cache:
            return self._login_cache, self.client.token()
        token = self.client.token()
        login = ""
        if token:
            login = self.client.fetch_login(token) or ""
        if not login:
            login = git_config_github_user() or ""
        if login:
            self._login_cache = login
        return login, token

    def _fetch_events_retry(self, login: str, token: str) -> dict:
        """401 时刷新 token 重试一次，再不行降匿名（只看公开事件）。"""
        r = self.client.fetch_events(login, token, self._etag)
        if r.get("status") == "auth" and token:
            token2 = self.client.token(refresh=True)
            if token2 and token2 != token:
                r = self.client.fetch_events(login, token2, self._etag)
            if r.get("status") == "auth":
                r = self.client.fetch_events(login, "", self._etag)
                if r.get("status") == "ok":
                    r["deg"] = "匿名模式：只看得见公开推送"
                else:
                    self._login_cache = ""   # 下一轮重新解析账号
        return r

    # ------------------------------------------------------------ 取数

    def _fresh_remote(self, remote: list[dict]) -> list[dict]:
        """新基线：24h 内、远程仓库、上限 40 个 commit 的推送。"""
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            hours=BASELINE_WINDOW_HOURS)
        picked, total = [], 0
        for p in remote:
            if p["commits"] <= 0 or not p["at"] or p["at"] < since:
                continue
            if total >= BASELINE_MAX_COMMITS:
                break
            picked.append(p)
            total += p["commits"]
        return picked

    def _fetch(self, cursor: str) -> dict:
        login, token = self._resolve()
        mode = "token" if token else "anon"
        if not login:
            return {"status": "err", "mode": mode,
                    "note": "没有可用的 GitHub 登录：装 gh CLI 跑 gh auth login，"
                            "或在 config.json 填 github.username / github.token"}
        r = self._fetch_events_retry(login, token)
        st = r.get("status")
        if st == "rate":
            return {"status": "rate", "login": login, "mode": mode,
                    "note": "GitHub 接口限流了，过一会儿自动恢复"}
        if st == "auth":
            return {"status": "err", "login": login, "mode": mode,
                    "note": "GitHub 登录态失效：gh auth status 检查一下"}
        if st == "err":
            return {"status": "err", "login": login, "mode": mode,
                    "note": r.get("note") or "网络或接口错误"}
        if st == "none":                      # 304：无新事件，沿用上次观测
            prev = self._last or {}
            return {"status": "ok", "login": login,
                    "mode": prev.get("mode", mode), "cursor": cursor,
                    "fed": 0, "fed_repos": {},
                    "today_pushes": prev.get("today_pushes"),
                    "recent": prev.get("recent") or [],
                    "rate_left": prev.get("rate_left"),
                    "note": "没有新动态", "no_change": True,
                    "at": dt.datetime.now().strftime("%m-%d %H:%M")}

        # ---- 200：有事件
        self._etag = r.get("etag") or self._etag
        floor = r.get("poll_floor")
        if isinstance(floor, int) and floor > 0:
            self._poll_floor = max(MIN_POLL_SECONDS, floor)
        if r.get("deg"):
            mode = "anon"
        pushes = parse_push_events(r.get("events") or [])
        local = self.local_names()
        remote = [p for p in pushes if p["repo"].lower() not in local]

        today = dt.datetime.now().astimezone().date()
        today_pushes = sum(
            p["commits"] for p in pushes
            if p["at"] and p["at"].astimezone().date() == today)

        # ---- 相对游标截取新事件
        baseline = False
        idx = next((i for i, p in enumerate(pushes)
                    if p["id"] and p["id"] == cursor), None)
        if not cursor or idx is None:
            baseline = True
            new_pushes = self._fresh_remote(remote)
        else:
            new_pushes = [p for p in pushes[:idx]
                          if p["repo"].lower() not in local]

        newest = next((p["id"] for p in pushes if p["id"]), "") or cursor

        fed_repos: dict[str, int] = {}
        fed = 0
        if self.conf.get("feed", True):
            for p in new_pushes:
                if p["commits"] <= 0:
                    continue
                fed += p["commits"]
                fed_repos[p["repo"]] = fed_repos.get(p["repo"], 0) + p["commits"]
        note = ""
        if baseline and not cursor:
            note = "首次连接 · 只回溯 24 小时内的远程推送"
        elif baseline:
            note = "事件列表已滚动，按新基线接续"
        if r.get("deg"):
            note = (note + " · " if note else "") + str(r["deg"])
        if not self.conf.get("feed", True):
            note = (note + " · " if note else "") + "云端投喂已关（只展示）"

        recent: list[str] = []
        for p in pushes:
            if p["repo"] and p["repo"] not in recent:
                recent.append(p["repo"])
            if len(recent) >= 5:
                break
        return {
            "status": "baseline" if baseline else "ok",
            "login": login, "mode": mode, "cursor": newest,
            "fed": fed, "fed_repos": fed_repos,
            "today_pushes": today_pushes, "recent": recent,
            "rate_left": r.get("rate_left"), "note": note,
            "at": dt.datetime.now().strftime("%m-%d %H:%M"),
        }

    # ------------------------------------------------------------ 对外视图

    def menu_label(self) -> str:
        if not self.enabled():
            return "GitHub：已关闭（config.json）"
        st = (self._last or {}).get("status")
        if st in ("ok", "baseline"):
            return f"GitHub @{self._last.get('login')} · 点我刷新"
        if st:
            return "GitHub：连接异常（点我重试）"
        return "连接 GitHub 账号"

    def status_lines(self) -> list[str]:
        """状态气泡里的 GitHub 段（独立于 state.status_lines）。"""
        if not self.enabled():
            return ["🐙 GitHub：已关闭（config.json → github.enabled）"]
        last = self._last or {}
        if not last:
            return ["🐙 GitHub：还没连接（右键菜单 → 连接 GitHub 账号）"]
        st = last.get("status")
        if st in ("ok", "baseline"):
            bits = [f"🐙 GitHub @{last.get('login') or '?'}"]
            today = last.get("today_pushes")
            if isinstance(today, int):
                bits.append(f"今日推送 {today} 个 commit")
            if last.get("fed"):
                bits.append(f"云端投喂 +{last['fed']}")
            lines = [" · ".join(bits)]
            if last.get("recent"):
                lines.append("最近推送：" + "、".join(list(last["recent"])[:3]))
            if last.get("note"):
                lines.append(last["note"])
            return lines
        return [f"🐙 GitHub：{last.get('note') or '连接异常'}"]

    def drain(self) -> list[tuple]:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def smoke_summary(self) -> dict:
        last = self._last or {}
        return {"status": last.get("status"), "login": last.get("login"),
                "mode": last.get("mode"),
                "today_pushes": last.get("today_pushes"),
                "fed": last.get("fed")}


# ---------------------------------------------------------------- 命令行

if __name__ == "__main__":              # python -m habitpet.collectors.github
    import json
    import sys

    conf = {"enabled": True, "feed": True}
    if len(sys.argv) > 1:
        conf["username"] = sys.argv[1]
    poller = GitHubPoller(conf, repos=[], log=lambda m: print(m))
    result = poller._fetch(cursor="")
    safe = {k: v for k, v in result.items() if k != "token"}
    print(json.dumps(safe, ensure_ascii=False, indent=2, default=str))
