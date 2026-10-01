"""GitHub 连接测试：远端 URL 归一 / 事件解析 / 登录链 / 基线去重 /
ETag 与 401 重试 / 状态机远程投喂 / 成就（全离线，不碰网络与 gh CLI）。"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from habitpet import growth
from habitpet.collectors import github as gh
from habitpet.config import DEFAULT_MECHANICS
from habitpet.state import DayStats, PetState


# ------------------------------------------------------------ 测试替身 --

class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = dict(headers or {})

    def json(self):
        return self._payload


class FakeSession:
    """按队列返回响应；队列空了就报错，防止测试偷偷拖到网络。"""

    def __init__(self, gets=None, exc=None):
        self.gets = list(gets or [])
        self.exc = exc
        self.calls: list[tuple] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url, dict(headers or {})))
        if self.exc:
            raise self.exc
        if not self.gets:
            raise AssertionError(f"unexpected GET {url}")
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def wait_for(pred, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def today_noon_utc() -> dt.datetime:
    """今天的本地正午（换算成 UTC），保证任何运行时刻都算「今天」。"""
    local = dt.datetime.combine(dt.date.today(), dt.time(12, 0)).astimezone()
    return local.astimezone(dt.timezone.utc)


def ev(eid: str, repo: str, commits: int, when: dt.datetime | None = None,
       distinct: int | None = None, type_: str = "PushEvent") -> dict:
    return {
        "id": eid, "type": type_,
        "repo": {"name": repo},
        "payload": {"size": commits,
                    "distinct_size": commits if distinct is None else distinct},
        "created_at": _iso(when or today_noon_utc()),
    }


def poller_with(gets, conf_extra=None, local=None, login="humble26",
                token="t0") -> gh.GitHubPoller:
    conf = {"enabled": True, "feed": True, "username": login, "token": token}
    conf.update(conf_extra or {})
    p = gh.GitHubPoller(conf, repos=[], client=gh.GitHubClient(conf),
                        log=lambda _m: None)
    p._local = {"me/local"} if local is None else set(local)
    return p


# ------------------------------------------------------------ 远端归一 --

class TestNormalizeRemote(unittest.TestCase):
    def test_variants(self):
        cases = {
            "https://github.com/a/b.git": "a/b",
            "https://github.com/a/b": "a/b",
            "https://github.com/a/b/": "a/b",
            "git@github.com:a/b.git": "a/b",
            "ssh://git@github.com/a/b.git": "a/b",
            "https://GitHub.com/a/b.git": "a/b",
        }
        for url, want in cases.items():
            self.assertEqual(gh.normalize_remote(url), want, url)

    def test_non_github_and_garbage(self):
        for url in ("https://gitee.com/a/b.git", "", "not a url",
                    "https://github.com/only-one", None):
            self.assertIsNone(gh.normalize_remote(url or ""))

    def test_local_repo_names_uses_git(self):
        def fake_cli(args, timeout=10.0):
            if args[-3:] == ["remote", "get-url", "origin"]:
                return "git@github.com:Me/Repo.git" if "E:/ok" in args else None
            if args[-1] == "github.user":
                return "humble26"
            return None

        with mock.patch.object(gh, "_run_cli", side_effect=fake_cli):
            names = gh.local_repo_names(["E:/ok", "E:/no-remote"])
            self.assertEqual(names, {"me/repo"})
            self.assertEqual(gh.git_config_github_user(), "humble26")


# ------------------------------------------------------------ 事件解析 --

class TestParsePushEvents(unittest.TestCase):
    def test_only_push_events(self):
        events = [{"id": "1", "type": "WatchEvent", "payload": {}},
                  ev("2", "a/b", 3)]
        out = gh.parse_push_events(events)
        self.assertEqual([p["id"] for p in out], ["2"])

    def test_commits_fallback_chain(self):
        base = {"id": "x", "type": "PushEvent", "repo": {"name": "a/b"},
                "created_at": _iso(utc_now())}
        # distinct_size 优先
        self.assertEqual(gh.parse_push_events(
            [{**base, "payload": {"size": 5, "distinct_size": 2}}])[0]["commits"], 2)
        # 退回 size
        self.assertEqual(gh.parse_push_events(
            [{**base, "payload": {"size": 5}}])[0]["commits"], 5)
        # 退回 commits 数组长度
        self.assertEqual(gh.parse_push_events(
            [{**base, "payload": {"commits": [1, 2, 3]}}])[0]["commits"], 3)
        # 全缺（GitHub 事件接口的精简 payload）→ None，交给 compare 补算
        reduced = gh.parse_push_events(
            [{**base, "payload": {"push_id": 9, "before": "a" * 40,
                                  "head": "b" * 40}}])[0]
        self.assertIsNone(reduced["commits"])
        self.assertEqual(reduced["before"], "a" * 40)
        self.assertEqual(reduced["head"], "b" * 40)

    def test_time_parse(self):
        out = gh.parse_push_events([ev("t", "a/b", 1, utc_now())])
        self.assertIsNotNone(out[0]["at"])
        self.assertIsNotNone(out[0]["at"].tzinfo)
        bad = ev("t2", "a/b", 1)
        bad["created_at"] = "garbage"
        self.assertIsNone(gh.parse_push_events([bad])[0]["at"])

    def test_garbage_events_ignored(self):
        self.assertEqual(gh.parse_push_events([None, 42, {}, "x"]), [])


# ------------------------------------------------------------ 登录链 --

class TestTokenChain(unittest.TestCase):
    def test_config_token_wins(self):
        client = gh.GitHubClient({"token": "cfg"})
        with mock.patch.object(gh, "gh_cli_token") as cli:
            self.assertEqual(client.token(), "cfg")
            cli.assert_not_called()

    def test_gh_cli_fallback_and_refresh(self):
        client = gh.GitHubClient({})
        with mock.patch.object(gh, "gh_cli_token",
                               side_effect=["t_old", "t_new"]) as cli:
            self.assertEqual(client.token(), "t_old")
            self.assertEqual(client.token(), "t_old")       # 命中缓存
            self.assertEqual(cli.call_count, 1)
            self.assertEqual(client.token(refresh=True), "t_new")

    def test_gh_cli_token_sanity(self):
        def run(rc, stdout):
            return types.SimpleNamespace(returncode=rc, stdout=stdout,
                                         stderr="")
        good = "gho_" + "x" * 36
        with mock.patch.object(gh.subprocess, "run", return_value=run(0, good)):
            self.assertEqual(gh.gh_cli_token(), good)
        for rc, out in ((1, good), (0, "two\nlines"), (0, "short")):
            with mock.patch.object(gh.subprocess, "run",
                                   return_value=run(rc, out)):
                self.assertIsNone(gh.gh_cli_token())
        with mock.patch.object(gh.subprocess, "run",
                               side_effect=FileNotFoundError):
            self.assertIsNone(gh.gh_cli_token())


# ------------------------------------------------------------ 取数与去重 --

class TestFetch(unittest.TestCase):
    def _events_resp(self, events, etag=None, headers=None):
        h = dict(headers or {})
        if etag:
            h["ETag"] = etag
        return FakeResp(events, 200, h)

    def test_baseline_feeds_only_fresh_remote(self):
        old = utc_now() - dt.timedelta(days=3)
        events = [
            ev("n1", "me/remote", 3),                 # 今天、24h 内 → 投喂
            ev("n2", "me/local", 5),                  # 本地仓库 → 不投喂
            ev("n3", "me/remote", 7, when=old),       # 太旧 → 不投喂
        ]
        sess = FakeSession(gets=[self._events_resp(events, etag="e1")])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["status"], "baseline")
        self.assertEqual(r["fed"], 3)
        self.assertEqual(r["fed_repos"], {"me/remote": 3})
        self.assertEqual(r["fed_ids"], ["n1"])
        self.assertEqual(r["cursor"], "n1")
        self.assertIn("首次连接", r["note"])
        # today_pushes 统计所有仓库（含本地的推送到 GitHub 的那部分）
        self.assertEqual(r["today_pushes"], 3 + 5)
        self.assertEqual(r["recent"][0], "me/remote")

    def test_cursor_consumes_only_new_remote(self):
        events = [
            ev("id2", "me/remote", 2),        # 新：投喂
            ev("id1", "me/local", 9),         # 新但本地仓库 → 不投喂
            ev("id0", "me/other", 4),         # 游标（已消费过）
        ]
        sess = FakeSession(gets=[self._events_resp(events)])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("id0")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["fed"], 2)
        self.assertEqual(r["fed_repos"], {"me/remote": 2})
        self.assertEqual(r["cursor"], "id2")

    def test_baseline_skips_already_fed_ids(self):
        """时间窗重叠回归：基线刷新时，喂过的事件 ID 不允许重喂。"""
        events = [ev("n1", "me/remote", 3), ev("n2", "me/remote", 5)]
        sess = FakeSession(gets=[self._events_resp(events)])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("", {"n1"})              # n1 已喂过（上次轮询/基线窗口内）
        self.assertEqual(r["status"], "baseline")
        self.assertEqual(r["fed"], 5)
        self.assertEqual(r["fed_ids"], ["n2"])

    def test_cursor_path_skips_already_fed_ids(self):
        """游标截取与 fed_ids 过滤叠加：双喂窗口内的事件不再重复计入。"""
        events = [ev("id2", "me/remote", 2), ev("id0", "me/other", 4)]
        sess = FakeSession(gets=[self._events_resp(events)])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("id0", {"id2"})          # id2 在游标前，但已喂过
        self.assertEqual(r["fed"], 0)
        self.assertEqual(r["fed_ids"], [])

    def test_cursor_rolled_off_falls_back_to_baseline(self):
        events = [ev("new", "me/remote", 4)]
        sess = FakeSession(gets=[self._events_resp(events)])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("long-gone-id")
        self.assertEqual(r["status"], "baseline")
        self.assertEqual(r["fed"], 4)
        self.assertIn("滚动", r["note"])

    def test_feed_disabled_still_advances_cursor(self):
        events = [ev("id9", "me/remote", 6)]
        sess = FakeSession(gets=[self._events_resp(events)])
        p = poller_with([], login="me", token="t0",
                        conf_extra={"feed": False})
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["fed"], 0)
        self.assertEqual(r["fed_ids"], [])
        self.assertEqual(r["cursor"], "id9")
        self.assertIn("只展示", r["note"])

    def test_etag_and_304_carries_last(self):
        events = [ev("id1", "me/remote", 2)]
        sess = FakeSession(gets=[self._events_resp(events, etag="e1"),
                                 FakeResp({}, 304)])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        first = p._fetch("")
        p._last = first
        second = p._fetch(first["cursor"])
        self.assertTrue(second.get("no_change"))
        self.assertEqual(second["cursor"], first["cursor"])
        self.assertEqual(second["today_pushes"], first["today_pushes"])
        self.assertEqual(second["fed"], 0)
        send = sess.calls[1][2]
        self.assertEqual(send.get("If-None-Match"), "e1")

    def test_401_refreshes_token_then_retries(self):
        events = [ev("id1", "me/remote", 1)]
        sess = FakeSession(gets=[FakeResp({}, 401),
                                 self._events_resp(events)])
        conf = {"enabled": True, "feed": True, "username": "me"}
        p = gh.GitHubPoller(conf, repos=[],
                            client=gh.GitHubClient(conf, session=sess))
        p._local = set()
        with mock.patch.object(gh, "gh_cli_token",
                               side_effect=["t_old", "t_new"]):
            r = p._fetch("")
        self.assertEqual(r["status"], "baseline")
        auths = [c[2].get("Authorization") for c in sess.calls]
        self.assertEqual(auths, ["Bearer t_old", "Bearer t_new"])

    def test_401_same_token_falls_back_to_anon(self):
        events = [ev("id1", "me/remote", 1)]
        sess = FakeSession(gets=[FakeResp({}, 401),
                                 self._events_resp(events)])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["mode"], "anon")
        self.assertNotIn("Authorization", sess.calls[1][2])
        self.assertIn("匿名", r["note"] + str(r.get("deg", "")))

    def test_rate_limit(self):
        sess = FakeSession(gets=[FakeResp(
            {}, 403, {"X-RateLimit-Remaining": "0"})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["status"], "rate")

    def test_no_login_found(self):
        p = gh.GitHubPoller({"enabled": True}, repos=[],
                            client=gh.GitHubClient({"enabled": True}),
                            log=lambda _m: None)
        with mock.patch.object(gh, "gh_cli_token", return_value=None), \
             mock.patch.object(gh, "git_config_github_user", return_value=None):
            r = p._fetch("")
        self.assertEqual(r["status"], "err")
        self.assertIn("没有可用", r["note"])

    def test_login_resolved_via_api_and_cached(self):
        events = [ev("id1", "me/repo", 1)]
        user_resp = FakeResp({"login": "humble26"}, 200)
        sess = FakeSession(gets=[user_resp, self._events_resp(events),
                                 self._events_resp(events)])
        conf = {"enabled": True, "feed": True, "token": "t0"}
        p = gh.GitHubPoller(conf, repos=[],
                            client=gh.GitHubClient(conf, session=sess))
        p._local = set()
        r1 = p._fetch("")
        self.assertEqual(r1["login"], "humble26")
        self.assertIn("/user", sess.calls[0][1])
        r2 = p._fetch(r1["cursor"])
        self.assertEqual(r2["login"], "humble26")
        self.assertEqual(len([c for c in sess.calls
                              if c[1].endswith("/user")]), 1)

    def test_network_error_degrades(self):
        sess = FakeSession(exc=OSError("boom"))
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["status"], "err")


def reduced_ev(eid: str, repo: str, before: str, head: str,
               when: dt.datetime | None = None, push_id: int = 123) -> dict:
    """GitHub 事件接口的真实形态：PushEvent 精简 payload（无 size/commits）。"""
    return {"id": eid, "type": "PushEvent", "repo": {"name": repo},
            "payload": {"repository_id": 1, "push_id": push_id,
                        "ref": "refs/heads/main", "head": head,
                        "before": before},
            "created_at": _iso(when or today_noon_utc())}


class TestReducedPayloadCompare(unittest.TestCase):
    """真实事件接口不给 size：提交数必须走 compare 补算（2026-10 实测）。"""

    def test_resolved_via_compare(self):
        events = [reduced_ev("id1", "me/remote", "a" * 40, "b" * 40)]
        sess = FakeSession(gets=[FakeResp(events, 200, {}),
                                 FakeResp({"total_commits": 3}, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["fed"], 3)
        self.assertEqual(r["fed_repos"], {"me/remote": 3})
        self.assertEqual(r["today_pushes"], 3)
        compare_url = [c[1] for c in sess.calls if "/compare/" in c[1]]
        self.assertEqual(compare_url,
                         [f"https://api.github.com/repos/me/remote/"
                          f"compare/{'a' * 40}...{'b' * 40}"])

    def test_count_cached_across_polls(self):
        events = [reduced_ev("id1", "me/remote", "a" * 40, "b" * 40)]
        sess = FakeSession(gets=[FakeResp(events, 200, {}),
                                 FakeResp({"total_commits": 3}, 200, {}),
                                 FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        first = p._fetch("")
        p._last = first
        second = p._fetch(first["cursor"])     # 游标=最新 → 无新事件
        self.assertEqual(second["fed"], 0)
        self.assertEqual(second["today_pushes"], 3)
        self.assertEqual(len([c for c in sess.calls if "/compare/" in c[1]]), 1,
                         "同一 push 不允许重复 compare")

    def test_branch_delete_and_first_push_skip_compare(self):
        events = [reduced_ev("id1", "me/remote", "a" * 40, "0" * 40),      # 删分支
                  reduced_ev("id2", "me/other", "0" * 40, "c" * 40,
                             push_id=124)]                                 # 首推分支
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")                        # 队列只有事件响应，调了 compare 会报错
        self.assertEqual(r["fed"], 0)
        self.assertEqual(r["today_pushes"], 0)

    def test_compare_failure_counts_zero(self):
        events = [reduced_ev("id1", "me/remote", "a" * 40, "b" * 40)]
        sess = FakeSession(gets=[FakeResp(events, 200, {}),
                                 FakeResp({}, 404, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch("")
        self.assertEqual(r["status"], "baseline")
        self.assertEqual(r["fed"], 0)


class TestBackfill(unittest.TestCase):
    """历史补喂：翻页、只喂游标之后的旧事件、一次性。"""

    def test_feeds_all_when_no_cursor(self):
        events = [ev("n1", "me/a", 2), ev("n2", "me/b", 3),
                  ev("n3", "me/a", 1)]
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch_backfill("")
        self.assertTrue(r["backfill"])
        self.assertEqual(r["fed"], 6)
        self.assertEqual(r["fed_repos"], {"me/a": 3, "me/b": 3})
        self.assertEqual(r["fed_ids"], ["n1", "n2", "n3"])
        self.assertEqual(r["cursor"], "n1")
        self.assertEqual(r["pushes"], 3)

    def test_backfill_skips_already_fed_ids(self):
        """双喂回归：基线已喂过的事件，补喂翻到了也不许再喂。"""
        events = [ev("n1", "me/a", 2), ev("n2", "me/b", 3),
                  ev("n3", "me/a", 1), ev("n4", "me/c", 5)]
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch_backfill("n2", {"n3"})   # n3 已被基线喂过
        self.assertEqual(r["fed"], 5)          # 只剩 n4(5)
        self.assertEqual(r["fed_ids"], ["n4"])
        self.assertEqual(r["pushes"], 1)

    def test_only_events_older_than_cursor(self):
        events = [ev("n1", "me/a", 2), ev("n2", "me/b", 3),
                  ev("n3", "me/a", 1), ev("n4", "me/c", 5)]
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch_backfill("n2")           # n1/n2 已消费过
        self.assertEqual(r["fed"], 6)         # 只剩 n3(1) + n4(5)
        self.assertEqual(r["fed_repos"], {"me/a": 1, "me/c": 5})
        self.assertEqual(r["cursor"], "n2")   # 游标不动

    def test_backfill_respects_local_dedupe(self):
        events = [ev("n1", "me/local", 9), ev("n2", "me/remote", 4)]
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")   # _local = {"me/local"}
        p.client._session = sess
        r = p._fetch_backfill("")
        self.assertEqual(r["fed"], 4)
        self.assertEqual(r["pushes"], 1)

    def test_backfill_feed_disabled(self):
        events = [ev("n1", "me/a", 2)]
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0",
                        conf_extra={"feed": False})
        p.client._session = sess
        r = p._fetch_backfill("")
        self.assertEqual(r["fed"], 0)
        self.assertTrue(r["backfill"])

    def test_backfill_paginates(self):
        page1 = [ev(f"p{i}", f"me/r{i % 3}", 1) for i in range(100)]
        page2 = [ev("q1", "me/x", 2)]
        sess = FakeSession(gets=[FakeResp(page1, 200, {}),
                                 FakeResp(page2, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        r = p._fetch_backfill("")
        self.assertEqual(r["events"], 101)
        self.assertEqual(r["fed"], 102)
        self.assertEqual(len([c for c in sess.calls if "page=2" in c[1]]), 1)

    def test_start_backfill_thread_and_guards(self):
        events = [ev("n1", "me/a", 1)]
        sess = FakeSession(gets=[FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        self.assertTrue(p.start_backfill(""))
        self.assertTrue(wait_for(lambda: bool(p.drain())))
        self.assertFalse(p._running)
        p.conf["enabled"] = False
        self.assertFalse(p.start_backfill(""))

    def test_backfill_login_missing(self):
        p = gh.GitHubPoller({"enabled": True}, repos=[],
                            client=gh.GitHubClient({"enabled": True}),
                            log=lambda _m: None)
        with mock.patch.object(gh, "gh_cli_token", return_value=None), \
             mock.patch.object(gh, "git_config_github_user", return_value=None):
            r = p._fetch_backfill("")
        self.assertEqual(r["status"], "err")
        self.assertTrue(r["backfill"])


# ------------------------------------------------------------ 轮询与视图 --

class TestPollerThreadAndViews(unittest.TestCase):
    def test_maybe_poll_thread_and_throttle(self):
        events = [ev("id1", "me/repo", 1)]
        sess = FakeSession(gets=[FakeResp(events, 200, {}),
                                 FakeResp(events, 200, {})])
        p = poller_with([], login="me", token="t0")
        p.client._session = sess
        self.assertTrue(p.maybe_poll("", force=True))
        self.assertTrue(wait_for(lambda: bool(p.drain())))
        self.assertFalse(p._running)
        # fed_ids 透传到取数层：已喂过的事件不再计入
        self.assertTrue(p.maybe_poll("", {"id1"}, force=True))
        self.assertTrue(wait_for(lambda: bool(p.drain())))
        self.assertEqual(p._last.get("fed"), 0)
        self.assertEqual(p._last.get("fed_ids"), [])
        # 间隔未到 → 不再轮询
        self.assertFalse(p.maybe_poll("id1"))
        # 关闭后直接拒绝
        p.conf["enabled"] = False
        self.assertFalse(p.maybe_poll("id1", force=True))

    def test_menu_label_and_status_lines(self):
        p = poller_with([], login="me", token="t0")
        self.assertEqual(p.menu_label(), "连接 GitHub 账号")
        self.assertIn("还没连接", p.status_lines()[0])
        p._last = {"status": "ok", "login": "humble26", "today_pushes": 7,
                   "fed": 2, "recent": ["a/b", "c/d"], "note": ""}
        self.assertIn("@humble26", p.menu_label())
        lines = p.status_lines()
        self.assertEqual(len(lines), 1, "状态气泡里只留紧凑一行，防底部溢出")
        self.assertIn("今日推送 7", lines[0])
        self.assertIn("云端投喂 +2", lines[0])
        p._last = {"status": "err", "note": "网络请求失败"}
        self.assertIn("连接异常", p.menu_label())
        self.assertIn("网络请求失败", p.status_lines()[0])
        p.conf["enabled"] = False
        self.assertIn("已关闭", p.menu_label())
        self.assertIn("已关闭", p.status_lines()[0])

    def test_smoke_summary(self):
        p = poller_with([], login="me", token="t0")
        p._last = {"status": "ok", "login": "humble26", "mode": "token",
                   "today_pushes": 3, "fed": 1}
        self.assertEqual(p.smoke_summary()["login"], "humble26")


# ------------------------------------------------------------ 状态机 --

class TestStateIntegration(unittest.TestCase):
    def _state(self) -> PetState:
        return PetState(dict(DEFAULT_MECHANICS))

    def test_feed_remote(self):
        st = self._state()
        st.feed(3, remote=True, repos=["a/b", "a/b", "c/d"])
        self.assertEqual(st.gh_remote_commits, 3)
        self.assertEqual(st.today.gh_pushes, 3)
        self.assertEqual(st.total_commits, 3)
        self.assertEqual(st.gh_repos_seen, ["a/b", "c/d"])
        kinds = [e["kind"] for e in st.events]
        self.assertIn("gh_feed", kinds)
        self.assertNotIn("feed", kinds)
        # 远程投喂也计入成就
        self.assertIn("gh_remote_1", st.unlocked)

    def test_remote_feed_skipped_when_runaway(self):
        st = self._state()
        st.runaway_until = "2099-01-01T00:00:00"
        st.feed(5, remote=True)
        self.assertEqual(st.gh_remote_commits, 0)

    def test_connect_github(self):
        st = self._state()
        self.assertTrue(st.connect_github("humble26"))
        self.assertEqual(st.gh_login, "humble26")
        self.assertIn("gh_connect", st.unlocked)
        kinds = [e["kind"] for e in st.events]
        self.assertIn("gh_connected", kinds)
        self.assertFalse(st.connect_github("humble26"))    # 无变化不重复
        self.assertFalse(st.connect_github(""))

    def test_daystats_roundtrip(self):
        d = DayStats.for_day(dt.date.today())
        d.gh_pushes = 4
        self.assertEqual(DayStats.from_dict(d.to_dict()).gh_pushes, 4)
        self.assertEqual(DayStats.from_dict({"date": "x"}).gh_pushes, 0)

    def test_roll_day_archives_gh_pushes(self):
        st = self._state()
        st.today.gh_pushes = 5
        yday = dt.date.today() - dt.timedelta(days=1)
        st.today.date = yday.isoformat()
        st._roll_day(dt.datetime.now())
        self.assertEqual(st.history[yday.isoformat()]["gh_pushes"], 5)

    def test_save_load_roundtrip(self):
        st = self._state()
        st.connect_github("humble26")
        st.feed(7, remote=True, repos=["a/b"])
        st.gh_backfilled = True
        st.gh_fed_ids = ["e1", "e2"]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            st.save(path)
            st2 = PetState.load(path, dict(DEFAULT_MECHANICS))
        self.assertEqual(st2.gh_login, "humble26")
        self.assertEqual(st2.gh_remote_commits, 7)
        self.assertEqual(st2.gh_repos_seen, ["a/b"])
        self.assertTrue(st2.gh_backfilled)
        self.assertEqual(st2.gh_fed_ids, ["e1", "e2"])

    def test_old_save_without_gh_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps({"satiety": 55, "today": {},
                                        "history": {}}), encoding="utf-8")
            st = PetState.load(path, dict(DEFAULT_MECHANICS))
        self.assertEqual(st.gh_login, "")
        self.assertEqual(st.gh_remote_commits, 0)
        self.assertEqual(st.gh_repos_seen, [])
        self.assertEqual(st.gh_fed_ids, [])
        self.assertEqual(st.today.gh_pushes, 0)

    def test_status_line_shows_github(self):
        st = self._state()
        self.assertFalse(any("云端投喂" in ln for ln in st.status_lines(
            dt.datetime.now())))
        st.connect_github("humble26")
        self.assertTrue(any("云端投喂" in ln for ln in st.status_lines(
            dt.datetime.now())))


# ------------------------------------------------------------ app 接线 --

class TestAppDrain(unittest.TestCase):
    def _make_app(self, github_enabled=False):
        from habitpet.app import HabitPetApp
        from habitpet.pet_window import PetWindow
        cfgdir = Path(tempfile.mkdtemp(prefix="habitpet_gh_"))
        (cfgdir / "config.json").write_text(json.dumps({
            "repos": [], "llm": {"enabled": False},
            "sound": {"enabled": False}, "balance": {"enabled": False},
            "credits": {"enabled": False},
            "github": {"enabled": github_enabled},
        }, ensure_ascii=False), encoding="utf-8")
        w = PetWindow(callbacks={})
        w.withdraw()
        return HabitPetApp(w, cfgdir), w

    def test_drain_applies_state(self):
        app, w = self._make_app()
        try:
            app.github._queue.put(("github_done", {
                "status": "ok", "login": "humble26", "mode": "token",
                "cursor": "idX", "fed": 3, "fed_repos": {"a/b": 3},
                "fed_ids": ["id1"],
                "today_pushes": 8, "recent": ["a/b"], "note": "",
                "at": "10-01 12:00"}))
            app._drain_github_events()
            self.assertEqual(app.state.gh_login, "humble26")
            self.assertEqual(app.state.gh_last_event_id, "idX")
            self.assertEqual(app.state.gh_remote_commits, 3)
            self.assertEqual(app.state.today.gh_pushes, 3)
            self.assertEqual(app.state.gh_fed_ids, ["id1"])
            self.assertIn("a/b", app.state.gh_repos_seen)
            self.assertIn("gh_connect", app.state.unlocked)
        finally:
            w.destroy()

    def test_drain_backfill(self):
        app, w = self._make_app(github_enabled=True)
        try:
            app.github._queue.put(("github_done", {
                "status": "ok", "backfill": True, "login": "humble26",
                "cursor": "idNew", "fed": 5, "fed_repos": {"a/b": 5},
                "fed_ids": ["z1", "z2"],
                "span_days": 25, "pushes": 10, "events": 87,
                "recent": [], "note": "", "at": "10-01 16:00"}))
            app._drain_github_events()
            self.assertTrue(app.state.gh_backfilled)
            self.assertEqual(app.state.gh_remote_commits, 5)
            self.assertEqual(app.state.gh_last_event_id, "idNew")
            self.assertEqual(app.state.gh_fed_ids, ["z1", "z2"])
            # 已补喂后手动再点 → 守卫拦下，不会再起线程、不会再投喂
            app._github_backfill()
            self.assertFalse(app.github._running)
            self.assertEqual(app.state.gh_remote_commits, 5)
        finally:
            w.destroy()

    def test_record_gh_fed_dedupes_and_caps(self):
        app, w = self._make_app()
        try:
            app._record_gh_fed({"fed_ids": ["a", "a", "b", ""]})
            self.assertEqual(app.state.gh_fed_ids, ["a", "b"])
            app.state.gh_fed_ids = [f"x{i}" for i in range(500)]
            app._record_gh_fed({"fed_ids": ["tail"]})
            self.assertEqual(len(app.state.gh_fed_ids), 500)
            self.assertEqual(app.state.gh_fed_ids[-1], "tail")
            app._record_gh_fed({})            # 无 key 不炸
            self.assertEqual(len(app.state.gh_fed_ids), 500)
        finally:
            w.destroy()


# ------------------------------------------------------------ 成就表 --

class TestGrowthTable(unittest.TestCase):
    OLD_IDS = {"first_feed", "commits_10", "commits_50", "commits_100",
               "commits_365", "streak_3", "streak_7", "streak_30",
               "comeback", "night_owl", "early_bird", "zen", "zenith",
               "veteran", "focus_1", "focus_10", "focus_50"}

    def test_ids_unique_and_backward_compatible(self):
        ids = [a.id for a in growth.ACHIEVEMENTS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(self.OLD_IDS <= set(ids),
                        "老存档兼容：原有成就 ID 不允许改动")

    def test_github_achievements_unlock(self):
        st = PetState(dict(DEFAULT_MECHANICS))
        st.gh_remote_commits = 20
        st.gh_repos_seen = ["a", "b", "c", "d", "e"]
        got = {a.id for a in growth.check_new(st)}
        self.assertTrue({"gh_remote_1", "gh_remote_20", "gh_repos_5"} <= got)
        self.assertNotIn("gh_remote_100", got)
        self.assertNotIn("gh_connect", got)     # 还没连账号


if __name__ == "__main__":
    unittest.main()
