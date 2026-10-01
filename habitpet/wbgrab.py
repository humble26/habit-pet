"""WorkBuddy 服务端额度 token 抓取（本机 CDP，只读，纯标准库）。

WorkBuddy 的账号登录态以「双进程内存 + 加密落盘」保存，磁盘上没有可读副本；
但客户端界面自己会带 Bearer 调服务端接口。本工具连本机 Chromium 调试端口，
注入只读请求钩子后刷新一次界面，从应用自己的请求里读走 Authorization，
再用 /billing/meter/get-user-resource-summary 校验收到的数值。

用法（Windows）：
    1) 完全退出 WorkBuddy（含托盘）；
    2) python -m habitpet.wbgrab --launch        # 自动找安装路径，带调试端口启动
       （或手动： "…\\WorkBuddy.exe" --remote-debugging-port=9333）
    3) python -m habitpet.wbgrab                 # 只抓取+校验，不改配置
       python -m habitpet.wbgrab --write         # 顺手写入 ~/.habitpet/config.json

token 只在内存里流转；只有显式加 --write 才会写进你自己的配置文件。
实测该 token 有效期约 30 天；此后重开一次 WorkBuddy 再跑本工具即可。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .credits import (EP_WB_CHECKIN, EP_WB_SUMMARY, _fmt_num, _wb_checkin_seg,
                      wb_parse_summary)

DEFAULT_PORT = 9333

# 只记录授权头形状（长度/前缀），完整值存入页面内存变量，供本工具读取。
_HOOK_JS = r"""(() => {
  window.__wbcap = [];
  const rec = (url, h, v) => {
    try {
      if (!/^authorization$/i.test(h)) return;
      const s = String(v || "");
      window.__wbcap.push({ url: String(url).slice(0, 180), len: s.length });
      if (/^Bearer /i.test(s)) window.__wbtok = s;
    } catch (e) {}
  };
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.setRequestHeader = function (h, v) { rec(this.__u, h, v); return XS.apply(this, arguments); };
  const of = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const hs = (init && init.headers) || (input && input.headers);
      if (hs) {
        if (typeof hs.forEach === "function") hs.forEach((v, k) => rec(url, k, v));
        else if (Array.isArray(hs)) hs.forEach(([k, v]) => rec(url, k, v));
        else Object.entries(hs).forEach(([k, v]) => rec(url, k, v));
      }
    } catch (e) {}
    return of.apply(this, arguments);
  };
})();"""


class WbGrabError(RuntimeError):
    pass


# ──────────────────────────────────────────────── 极简 WebSocket（客户端）──

class _CdpWs:
    """够用就好的 WS 客户端：文本帧 + 扩展长度 + 分片 + ping/pong。"""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        m = re.match(r"ws://([^/:]+)(?::(\d+))?(/.*)$", url)
        if not m:
            raise WbGrabError(f"bad ws url: {url[:60]}")
        host, port, path = m.group(1), int(m.group(2) or 80), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WbGrabError("握手被断开")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise WbGrabError("握手失败：" + head.decode("latin1")[:120])
        self._buf = rest
        self._frag = b""

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WbGrabError("连接被关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        return bytes(header) + bytes(b ^ mask[i % 4]
                                     for i, b in enumerate(payload))

    def send_text(self, text: str) -> None:
        self.sock.sendall(self._frame(0x1, text.encode("utf-8")))

    def recv_text(self) -> str:
        while True:
            b1, b2 = self._read_exact(2)
            fin, op, ln = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            data = self._read_exact(ln) if ln else b""
            if b2 & 0x80:                       # 服务端本不该加 mask，兼容处理
                mkey = self._read_exact(4)
                data = bytes(b ^ mkey[i % 4] for i, b in enumerate(data))
            if op == 0x9:                       # ping → pong
                self.sock.sendall(self._frame(0xA, data))
                continue
            if op == 0x8:
                raise WbGrabError("对端关闭了连接")
            if op == 0xA:
                continue
            if op == 0x0:                       # 续帧
                self._frag += data
                if fin:
                    out, self._frag = self._frag, b""
                    return out.decode("utf-8", "replace")
                continue
            if op == 0x1:
                if fin:
                    return data.decode("utf-8", "replace")
                self._frag = data
                continue

    def close(self) -> None:
        try:
            self.sock.sendall(self._frame(0x8, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class _Cdp:
    """一次只等一个响应的 CDP 会话（事件直接忽略）。"""

    def __init__(self, ws: _CdpWs) -> None:
        self.ws = ws
        self._id = 0

    def call(self, method: str, params: dict | None = None,
             timeout: float = 15.0):
        self._id += 1
        cid = self._id
        self.ws.send_text(json.dumps({"id": cid, "method": method,
                                      "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise WbGrabError(f"{method} 超时")
            self.ws.sock.settimeout(max(0.5, left))
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == cid:
                return msg

    def evaluate(self, expr: str, timeout: float = 15.0):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": True}, timeout)
        try:
            return r["result"]["result"].get("value")
        except (KeyError, TypeError):
            return None


def find_page(port: int, retries: int = 1, gap: float = 0.5) -> dict | None:
    """返回 WorkBuddy 主窗口的 CDP page target（优先 renderer/index.html）。"""
    url = f"http://127.0.0.1:{port}/json/list"
    for _ in range(max(1, retries)):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                targets = json.load(r)
        except Exception:
            targets = None
        if isinstance(targets, list):
            pages = [t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            for t in pages:
                if "renderer/index.html" in (t.get("url") or ""):
                    return t
            if pages:
                return pages[0]
        time.sleep(gap)
    return None


def grab_token(port: int, wait_seconds: float = 30.0) -> str:
    """连接 CDP 取 Bearer：页面里已有就直接用，否则注入钩子刷新一次再等。"""
    page = find_page(port)
    if page is None:
        raise WbGrabError("no-target")
    ws = _CdpWs(page["webSocketDebuggerUrl"])
    try:
        cdp = _Cdp(ws)
        tok = cdp.evaluate("window.__wbtok || ''")
        if not (isinstance(tok, str) and tok.startswith("Bearer ")):
            cdp.call("Page.enable")
            cdp.call("Page.addScriptToEvaluateOnNewDocument",
                     {"source": _HOOK_JS})
            cdp.call("Page.reload", {"ignoreCache": False})
            deadline = time.time() + wait_seconds
            tok = ""
            while time.time() < deadline:
                time.sleep(1.0)
                try:
                    t = cdp.evaluate("window.__wbtok || ''", timeout=5)
                except Exception:
                    continue
                if isinstance(t, str) and t.startswith("Bearer "):
                    tok = t
                    break
        if not tok:
            raise WbGrabError("未抓到登录态（界面可能还没加载完或被登出）")
        return tok[7:]
    finally:
        ws.close()


# ──────────────────────────────────────────────────────────── 校验/写入 ──

def _post_json(url: str, token: str, body: dict, timeout: float = 8.0):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "Accept": "application/json",
                 "Accept-Language": "zh-CN",
                 "User-Agent": "WorkBuddy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception as e:
        return None, repr(e)[:80]


def verify_token(token: str):
    """用抓到的 token 直查服务端，返回 (summary_parsed, smeta, checkin, cmeta)。"""
    sm_j, smeta = _post_json(EP_WB_SUMMARY, token, {})
    ck_j, cmeta = _post_json(EP_WB_CHECKIN, token, {})
    parsed = wb_parse_summary(sm_j) if isinstance(sm_j, dict) else None
    ck = None
    if isinstance(ck_j, dict):
        ck = ck_j.get("data") if isinstance(ck_j.get("data"), dict) else ck_j
        if not (isinstance(ck, dict) and "today_checked_in" in ck):
            ck = None
    return parsed, smeta, ck, cmeta


def mask_token(token: str) -> str:
    return f"{token[:8]}…（共 {len(token)} 字符）"


def write_config(config_dir: Path, token: str) -> Path:
    """把 token 写进 <config_dir>/config.json 的 credits.workbuddy_token（原子写）。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass
    credits = data.get("credits")
    if not isinstance(credits, dict):
        credits = {}
        data["credits"] = credits
    credits["workbuddy_token"] = token
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def _find_workbuddy() -> str | None:
    cands: list[str] = []
    which = shutil.which("WorkBuddy")
    if which:
        cands.append(which)
    la = os.environ.get("LOCALAPPDATA")
    pf = os.environ.get("ProgramFiles")
    if la:
        cands.append(str(Path(la) / "Programs" / "WorkBuddy" / "WorkBuddy.exe"))
    if pf:
        cands.append(str(Path(pf) / "WorkBuddy" / "WorkBuddy.exe"))
    cands += [r"E:\workbuddy\WorkBuddy.exe", r"E:\WorkBuddy\WorkBuddy.exe",
              r"D:\workbuddy\WorkBuddy.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


# ────────────────────────────────────────────────────────────────── CLI ──

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="python -m habitpet.wbgrab",
        description="抓取 WorkBuddy 登录态并校验服务端额度（只读，token 不入日志）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"WorkBuddy 调试端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--launch", action="store_true",
                    help="抓不到端口时尝试带 --remote-debugging-port 启动 WorkBuddy")
    ap.add_argument("--write", action="store_true",
                    help="把 token 写入 config.json 的 credits.workbuddy_token")
    ap.add_argument("--config-dir", default="",
                    help="配置目录（默认 ~/.habitpet）")
    ap.add_argument("--wait", type=float, default=30.0,
                    help="刷新页面后等待抓到登录态的秒数（默认 30）")
    args = ap.parse_args(argv)

    if find_page(args.port) is None and args.launch:
        exe = _find_workbuddy()
        if not exe:
            print("✘ 没找到 WorkBuddy.exe，请手动以调试端口启动：")
            print(f'   "…\\WorkBuddy.exe" --remote-debugging-port={args.port}')
            return 2
        print(f"启动 {exe} --remote-debugging-port={args.port} …")
        subprocess.Popen([exe, f"--remote-debugging-port={args.port}"],
                         close_fds=True)

    page = find_page(args.port, retries=24 if args.launch else 1)
    if page is None:
        print(f"✘ 连不上本机调试端口 {args.port}。")
        print("  请先完全退出 WorkBuddy（含托盘），然后任选一种：")
        print("    python -m habitpet.wbgrab --launch")
        print(f'    或手动: "…\\WorkBuddy.exe" --remote-debugging-port={args.port}')
        return 2
    print(f"已连接：{page.get('title') or '(untitled)'}")

    try:
        token = grab_token(args.port, wait_seconds=args.wait)
    except WbGrabError as e:
        print(f"✘ 抓取失败：{e}")
        return 1
    print(f"✔ 已抓取登录态：{mask_token(token)}（未打印全文）")

    parsed, smeta, ck, _cmeta = verify_token(token)
    if parsed:
        tags = " · ".join(t for t in (parsed.get("plan"),
                                      f"{parsed['packages']} 个资源包") if t)
        print(f"✔ 服务端额度：剩余 {_fmt_num(parsed['remain'])} credits"
              f"（{tags}），已用 {_fmt_num(parsed['used'])}/"
              f"{_fmt_num(parsed['total'])}")
    if ck:
        print("✔ 签到：" + _wb_checkin_seg(ck))
    if not parsed and smeta in (401, 403):
        print("✘ 服务端拒绝该 token（401/403），未写入。")
        print("  请确认 WorkBuddy 里处于登录状态，完全退出后重开再试。")
        return 1
    if not parsed:
        print(f"… 未能用 Python 直连校验（status={smeta}），"
              "token 已抓到但未确认；如需仍可加 --write 写入。")

    if args.write:
        cfg_dir = Path(args.config_dir) if args.config_dir \
            else Path.home() / ".habitpet"
        path = write_config(cfg_dir, token)
        print(f"✔ 已写入 {path}（credits.workbuddy_token）")
        print("  桌宠下次轮询（或右键菜单 → 剩余积分 → 刷新）即生效。")
    else:
        shown = args.config_dir or "~/.habitpet"
        print(f"--write 未指定：未写配置。加 --write 可写入 {shown}/config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
