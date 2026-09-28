"""git 喂食采集器测试：真实建一个临时仓库做提交。"""
from __future__ import annotations

import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path

from habitpet.collectors.gitfeed import GitFeedError, GitPoller, commits_since


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def make_repo(commits: int = 2) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="habitpet_git_"))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "cat@habitpet.local")
    _git(repo, "config", "user.name", "cat")
    for i in range(commits):
        (repo / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", f"c{i}")
    return repo


class TestGitFeed(unittest.TestCase):
    def test_counts_commits_since(self):
        repo = make_repo(2)
        past = dt.datetime.now().astimezone() - dt.timedelta(hours=1)
        n = commits_since(repo, past.isoformat(timespec="seconds"))
        self.assertEqual(n, 2)

    def test_no_new_commits_since_future(self):
        repo = make_repo(2)
        future = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
        n = commits_since(repo, future.isoformat(timespec="seconds"))
        self.assertEqual(n, 0)

    def test_empty_repo_counts_zero_and_not_disabled(self):
        empty = Path(tempfile.mkdtemp(prefix="habitpet_emptyrepo_"))
        _git(empty, "init", "-q")
        past = (dt.datetime.now().astimezone()
                - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        self.assertEqual(commits_since(empty, past), 0)
        logs = []
        poller = GitPoller([str(empty)], log=logs.append)
        total, _ = poller.poll(past)
        self.assertEqual(total, 0)
        self.assertNotIn(empty, poller._disabled, "空仓库不应被拉黑")

    def test_invalid_repo_raises_and_poller_disables(self):
        bogus = Path(tempfile.mkdtemp(prefix="habitpet_notrepo_"))
        past = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with self.assertRaises(GitFeedError):
            commits_since(bogus, past)
        logs = []
        poller = GitPoller([str(bogus)], log=logs.append)
        total, _ = poller.poll(past)
        self.assertEqual(total, 0)
        # 第二次轮询应直接跳过被拉黑的仓库
        total2, _ = poller.poll(past)
        self.assertEqual(total2, 0)

    def test_poller_sums_multiple_repos(self):
        past = (dt.datetime.now().astimezone()
                - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        r1, r2 = make_repo(1), make_repo(2)
        poller = GitPoller([str(r1), str(r2)])
        total, since = poller.poll(past)
        self.assertEqual(total, 3)
        self.assertTrue(since)


if __name__ == "__main__":
    unittest.main()
