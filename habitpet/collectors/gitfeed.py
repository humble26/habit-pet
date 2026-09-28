"""git 提交喂食：扫描本地仓库的新 commit。

只读取 git log 的提交哈希，不看任何文件内容；仓库失效时静默跳过并记录日志。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional


class GitFeedError(RuntimeError):
    pass


def _popen_kwargs() -> dict:
    """Windows 下防止弹出控制台窗口。"""
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _git(repo: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, **_popen_kwargs(),
        )
    except FileNotFoundError as e:
        raise GitFeedError("找不到 git 命令，喂食功能停用") from e
    except subprocess.TimeoutExpired as e:
        raise GitFeedError(f"git 超时：{repo}") from e
    if proc.returncode != 0:
        raise GitFeedError(f"git 失败：{repo} -> {proc.stderr.strip()[:200]}")
    return proc.stdout


def commits_since(repo: Path, since_iso: str) -> int:
    """统计 repo 自 since_iso 以来所有分支上的新提交数。

    用 rev-list --count 而不是 log：空仓库（还没有首次提交）显式返回 0，
    在旧版 git 上也不会因报错被误拉黑；仓库不存在时抛 GitFeedError。
    """
    out = _git(repo, ["rev-list", "--all", "--count", f"--since={since_iso}"])
    try:
        return int(out.strip() or "0")
    except ValueError as e:
        raise GitFeedError(f"无法解析 rev-list 输出：{out[:100]!r}") from e


class GitPoller:
    """周期扫描多个仓库，把新 commit 数回调给状态机。"""

    def __init__(self, repos: list[str], log: Optional[Callable[[str], None]] = None) -> None:
        self.repos = [Path(r) for r in repos]
        self.log = log or (lambda _msg: None)
        self._disabled: set[Path] = set()

    def poll(self, since_iso: str) -> tuple[int, str]:
        """返回 (新 commit 总数, 新的 since 时间戳)。"""
        now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        total = 0
        for repo in self.repos:
            if repo in self._disabled:
                continue
            try:
                n = commits_since(repo, since_iso)
            except GitFeedError as e:
                self.log(f"[gitfeed] {e}，该仓库已停用")
                self._disabled.add(repo)
                continue
            total += n
        return total, now_iso
