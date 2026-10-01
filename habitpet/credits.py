"""剩余积分聚合查询：Trae CN / TraeWork CN / WorkBuddy / Qoder CN。

全部只读，登录凭据只进内存、绝不写日志或落盘（结果缓存里只有数字和文案）。

- Trae CN / TraeWork CN：读客户端 %APPDATA%/<客户端>/User/globalStorage/
  storage.json 里 iCubeAuthInfo://icube.cloudide 的自加密凭据
  （AES-128-CBC，密钥由随机块 + 固定 salt 经 SHA-512 派生），
  调 api.trae.cn 查额度（total/consumed）与今日签到状态。
- Qoder CN：读 Electron userData 的 Chromium OSCrypt 存储——
  Local State 的 os_crypt.encrypted_key（DPAPI 解出主密钥）→
  auth.v1.dat（v10 格式；实测其 16B 尾巴不是标准 GCM tag，按 CTR 解出后
  用 JSON 结构自检代替校验）→ 调 openapi.qoder.com.cn。实测 0.4.3 的
  真实额度端点为 /sash/api/v2/me/usage（qoderUsage.userQuota 的
  total/used/remaining），另配 /sash/api/v1/ai-conversations/credits-summary
  （累计消耗与峰值）作补充与兜底；命中端点会缓存复用（自愈）。
- WorkBuddy：优先走服务端实时状态——token 取自 config.json 的
  credits.workbuddy_token（用 `python -m habitpet.wbgrab` 一键抓取/更新，
  详见该模块）。有 token 时调 workbuddy.cn /billing/meter/get-user-resource-summary
  （各资源包的 CycleTotal/Remain/UsedCapacity 汇总）与
  /v2/billing/meter/checkin-activity-status；token 失效或无 token 时降级为
  本地台账：签到状态取自客户端日志（~/.workbuddy/logs/main*.log 的
  [Checkin] 记录），今日消耗取自 workbuddy.db 的 session_usage.credit_json。

任何一步失败都降级为一行提示，不影响桌宠其他功能；网络查询在后台线程，
主循环只消费队列（与 BalanceTracker 同一套路）。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

MIN_POLL_SECONDS = 300
DEFAULT_POLL_SECONDS = 1800
HTTP_TIMEOUT = 8

# ════════════════════════════════════════════════════════════ 小工具 ══

def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_num(v) -> str:
    x = _f(v)
    if x is None:
        return str(v)
    if abs(x - round(x)) < 1e-6:
        return f"{int(round(x))}"
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _tail_text(path: Path, nbytes: int) -> str:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def _result(status: str, summary: str, short: str = "",
            lines: list[str] | None = None) -> dict:
    return {"status": status,            # ok / warn / off / err
            "summary": summary,          # 菜单里的整句
            "short": short or summary,   # 状态气泡里的短词
            "lines": lines or [],
            "at": dt.datetime.now().isoformat(timespec="seconds")}


# ══════════════════════════════════════════════════ AES-128-CBC（Trae）══
# 教科书实现，用于解开 Trae 客户端 storage.json 的自加密值。
# salt 常量来自公开的社区工具（L0NE-6/Trae-AutoCheckin 的提取脚本）。

SALT_A = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243,
                215, 251, 124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68,
                196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61, 238, 76,
                149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178,
                118, 91, 162, 73, 109, 139, 209, 37])
SALT_B = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128,
                236, 95, 96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159,
                147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176, 200,
                235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214,
                38, 225, 105, 20, 99, 85, 33, 12, 125])
SALT_AES = bytes(a ^ b for a, b in zip(SALT_A, SALT_B))


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _build_sbox() -> list[int]:
    inv = [0] * 256
    for i in range(1, 256):
        for j in range(1, 256):
            if _gmul(i, j) == 1:
                inv[i] = j
                break
    sb = [0] * 256
    for i in range(256):
        x = inv[i] if i else 0
        s = x
        for _ in range(4):
            x = ((x << 1) | (x >> 7)) & 0xFF
            s ^= x
        sb[i] = s ^ 0x63
    return sb


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128（Nk=4）。AES-256 走 _expand_key_nk。"""
    return _expand_key_nk(key, 4)


def _expand_key_nk(key: bytes, nk: int) -> list[list[int]]:
    nr = nk + 6
    total = 4 * (nr + 1)
    w = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
    for i in range(nk, total):
        t = w[i - 1][:]
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _rk(w, r: int) -> list[int]:
    ws = w[r * 4:r * 4 + 4]
    return [ws[c][k] for c in range(4) for k in range(4)]


def _addrk(s, k):
    return [s[i] ^ k[i] for i in range(16)]


def _mix_columns(s):
    o = []
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        o += [_gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3],
              a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3],
              a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3),
              _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)]
    return o


def _inv_mix_columns(s):
    o = []
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        o += [_gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9),
              _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13),
              _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11),
              _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)]
    return o


def _encrypt_block_nr(w, nr: int, block: bytes) -> bytes:
    s = _addrk(list(block), _rk(w, 0))
    for r in range(1, nr):
        s = [SBOX[b] for b in s]
        s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
             s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
        s = _mix_columns(s)
        s = _addrk(s, _rk(w, r))
    s = [SBOX[b] for b in s]
    s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
         s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
    s = _addrk(s, _rk(w, nr))
    return bytes(s)


def _decrypt_block_nr(w, nr: int, block: bytes) -> bytes:
    s = _addrk(list(block), _rk(w, nr))
    for r in range(nr - 1, 0, -1):
        s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
             s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
        s = [INV_SBOX[b] for b in s]
        s = _addrk(s, _rk(w, r))
        s = _inv_mix_columns(s)
    s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
         s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
    s = [INV_SBOX[b] for b in s]
    s = _addrk(s, _rk(w, 0))
    return bytes(s)


def _encrypt_block(w, block: bytes) -> bytes:
    """AES-128 单块加密（w 来自 _expand_key）。"""
    return _encrypt_block_nr(w, 10, block)


def _decrypt_block(w, block: bytes) -> bytes:
    """AES-128 单块解密。"""
    return _decrypt_block_nr(w, 10, block)


def aes128_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    w = _expand_key(key)
    pad = 16 - len(data) % 16
    data = data + bytes([pad]) * pad
    out, prev = b"", bytearray(iv)
    for off in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        prev = bytearray(_encrypt_block(w, blk))
        out += bytes(prev)
    return out


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("ciphertext not block aligned")
    w = _expand_key(key)
    out, prev = b"", list(iv)
    for off in range(0, len(data), 16):
        blk = list(data[off:off + 16])
        out += bytes(a ^ b for a, b in zip(_decrypt_block(w, bytes(blk)), prev))
        prev = blk
    return out


def trae_decrypt_storage_value(b64: str) -> bytes:
    """解开 Trae storage.json 里 iCubeAuthInfo 的加密值。

    结构：base64( 6B 头 | 32B 随机块 | AES-128-CBC 密文 )；
    密钥/IV：sha512(sha512(随机块) + SALT_AES) 的前 32 字节拆成 key+iv，
    明文前 64 字节是填充块，尾部按 PKCS7（旧版可能是零填充）去掉。
    """
    buf = base64.b64decode(b64)
    rb, enc = buf[6:38], buf[38:]
    h = hashlib.sha512(rb).digest()
    fh = hashlib.sha512(h + SALT_AES).digest()
    pt = aes128_cbc_decrypt(fh[:16], fh[16:32], enc)[64:]
    pad = pt[-1] if pt else 0
    if 1 <= pad <= 16:
        return pt[:-pad]
    return pt.rstrip(b"\x00").rstrip()


# ══════════════════════════════════════ Windows 加密原语（DPAPI / GCM）══

def _dpapi_unprotect(data: bytes) -> bytes | None:
    """CryptUnprotectData（当前用户上下文）。非 Windows 或失败返回 None。"""
    if os.name != "nt":
        return None
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ─ AES-256-GCM（纯 Python，Chromium OSCrypt v10 口径）─────────────
# Windows CNG 的 GCM 实现对 16 字节 tag 支持不齐（默认 tag 长度 12 且
# 属性不可改），而 OSCrypt 用 16 字节 tag，所以这里自带实现。
# 数据量都是几百字节级别，性能无忧；正确性由 NIST 测试向量背书。

def _gf128_mul(x: int, y: int) -> int:
    """GF(2^128) 乘法（GCM 的 GHASH 用；MSB-first 位序，R=0xe1)。"""
    z, v = 0, y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ (0xE1 << 120)
        else:
            v >>= 1
    return z


def _ghash(h: int, data: bytes) -> int:
    y = 0
    for i in range(0, len(data), 16):
        y = _gf128_mul(y ^ int.from_bytes(data[i:i + 16], "big"), h)
    return y


def _gcm_ctr(w, nr: int, j0: bytes, data: bytes) -> bytes:
    ctr = int.from_bytes(j0[12:], "big")
    out = bytearray()
    for off in range(0, len(data), 16):
        ctr = (ctr + 1) & 0xFFFFFFFF
        ks = _encrypt_block_nr(w, nr, j0[:12] + ctr.to_bytes(4, "big"))
        out += bytes(a ^ b for a, b in zip(data[off:off + 16], ks))
    return bytes(out)


def aes256_gcm_encrypt(key: bytes, nonce: bytes,
                       pt: bytes) -> tuple[bytes, bytes] | tuple[None, None]:
    """返回 (ciphertext, tag16)；参数不对返回 (None, None)。"""
    if len(key) != 32 or len(nonce) != 12:
        return None, None
    w = _expand_key_nk(key, 8)
    h = int.from_bytes(_encrypt_block_nr(w, 14, b"\x00" * 16), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    ct = _gcm_ctr(w, 14, j0, pt)
    s = _ghash(h, ct + b"\x00" * 8 + (len(ct) * 8).to_bytes(8, "big"))
    tag = (int.from_bytes(_encrypt_block_nr(w, 14, j0), "big") ^ s)
    return ct, tag.to_bytes(16, "big")


def aes256_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes,
                       tag: bytes) -> bytes | None:
    """校验 tag 后解密；任何不符返回 None。"""
    if len(key) != 32 or len(nonce) != 12 or len(tag) != 16:
        return None
    import hmac as _hmac
    w = _expand_key_nk(key, 8)
    h = int.from_bytes(_encrypt_block_nr(w, 14, b"\x00" * 16), "big")
    j0 = nonce + b"\x00\x00\x00\x01"
    s = _ghash(h, ct + b"\x00" * 8 + (len(ct) * 8).to_bytes(8, "big"))
    expected = (int.from_bytes(_encrypt_block_nr(w, 14, j0), "big") ^ s)
    if not _hmac.compare_digest(expected.to_bytes(16, "big"), tag):
        return None
    return _gcm_ctr(w, 14, j0, ct)


# ══════════════════════════════════════════════════════ Trae 系 Provider ══

TRAE_STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"
TRAE_API = "https://api.trae.cn"
EP_TRAE_ENTITLE = TRAE_API + "/trae/api/v2/pay/user_current_entitlement_list"
EP_TRAE_CHECKIN = TRAE_API + "/trae/api/v2/ug/checkin_credits/status"
TRAE_AUTH_FAIL_CODES = (1001, 1002)

TRAE_CLIENTS = {
    # 键：名称，值：候选 storage.json 目录（按顺序探测，取第一个存在的）
    "Trae CN": ["Trae CN", "Trae"],
    "TraeWork CN": ["TRAE SOLO CN", "TRAE SOLO", "TraeWork CN"],
}


def trae_storage_candidates(client_dirs: list[str]) -> list[Path]:
    ad = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return [Path(ad) / n / "User" / "globalStorage" / "storage.json"
            for n in client_dirs]


def trae_stable_device_id(seed: str) -> str:
    """与社区工具一致的兜底设备号：md5('trae:'+refreshToken) 截 16 位数字。"""
    h = hashlib.md5(("trae:" + seed).encode("utf-8")).hexdigest()
    return str(int(h[:15], 16))[:16].rjust(16, "0")


def _unescape_js(s: str) -> str:
    """有的客户端把中文昵称写成 \\uXXXX 转义，展示前还原回来。

    调用点是 regex 从 JSON 文本里截出的值（不含裸引号），可直接按 JSON 字符串解析。
    """
    if "\\u" not in s:
        return s
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s


def trae_read_creds(storage_path: Path) -> dict:
    """只读解析 storage.json。返回 {access, refresh, uid, nickname, device_id}。"""
    s = json.loads(storage_path.read_text(encoding="utf-8"))
    enc = s.get(TRAE_STORAGE_KEY)
    creds = {"access": "", "refresh": "", "uid": "", "nickname": "", "device_id": ""}
    if enc:
        txt = trae_decrypt_storage_value(enc).decode("utf-8", "replace")
        m = re.search(r'"refreshToken"\s*:\s*"([^"]+)"', txt)
        creds["refresh"] = m.group(1) if m else ""
        m = re.search(r'"token"\s*:\s*"([^"]+)"', txt)
        creds["access"] = m.group(1) if m else ""
        m = re.search(r'"userId"\s*:\s*"(\d+)"', txt)
        creds["uid"] = m.group(1) if m else ""
        m = re.search(r'"username"\s*:\s*"([^"]*)"', txt)
        creds["nickname"] = _unescape_js(m.group(1)) if m else ""
    # 设备号优先用客户端真实值：storage.json 的 iCubeAuthInfo://icube-dc:<digits>
    for k in s:
        if k.startswith("iCubeAuthInfo://icube-dc:"):
            d = k.split(":")[-1]
            if d.isdigit():
                creds["device_id"] = d
                break
    if not creds["device_id"] and creds["refresh"]:
        creds["device_id"] = trae_stable_device_id(creds["refresh"])
    return creds


class TraeProvider:
    """一家 Trae 系客户端（Trae CN / TraeWork CN）。"""

    def __init__(self, key: str, label: str, client_dirs: list[str],
                 short: str, session=None) -> None:
        self.key, self.label, self.short = key, label, short
        self.storage_path_candidates = trae_storage_candidates(client_dirs)
        self._session = session

    def storage_path(self) -> Path | None:
        for p in self.storage_path_candidates:
            if p.exists():
                return p
        return None

    def _post(self, url: str, headers: dict, body: dict):
        sess = self._session
        if sess is None:
            try:
                import requests
            except ImportError:
                return None
            sess = requests
        try:
            resp = sess.post(url, headers=headers,
                             data=json.dumps(body).encode("utf-8"),
                             timeout=HTTP_TIMEOUT)
            if resp.status_code >= 400 and resp.status_code not in (401, 403):
                return None
            return resp.json()
        except Exception:
            return None

    def query(self) -> dict:
        try:
            return self._query()
        except Exception as e:            # 兜底：任何意外都不许掀桌
            return _result("err", f"查询出错：{e!r}"[:120])

    def _query(self) -> dict:
        path = self.storage_path()
        if path is None:
            return _result("off", "未找到客户端数据（先安装并登录一次）")
        try:
            creds = trae_read_creds(path)
        except Exception:
            return _result("warn", "凭据解密失败（客户端版本可能变了）")
        if not creds["access"]:
            return _result("warn", "未找到登录凭据（打开一次客户端）")
        hdr = {"Authorization": "Cloud-IDE-JWT " + creds["access"],
               "X-User-Region": "cn",
               "x-device-id": creds["device_id"],
               "Content-Type": "application/json",
               "User-Agent": "Trae/1.0"}
        ent = self._post(EP_TRAE_ENTITLE, hdr,
                         {"require_usage": True, "full_data": True})
        code = (ent or {}).get("code")
        if ent is None or code in TRAE_AUTH_FAIL_CODES:
            note = "登录态已过期：打开一次客户端刷新" if code in \
                TRAE_AUTH_FAIL_CODES else "额度接口无响应"
            return _result("warn", note)
        checkin = self._post(EP_TRAE_CHECKIN, hdr, {"req_source": 1})
        summary = ent.get("usage_summary") or {}
        total, used = _f(summary.get("total_amount")), _f(summary.get("consumed_amount"))
        remain = round(total - used, 2) if (total is not None and used is not None) else None
        parts_short, parts_line = [], []
        if remain is not None:
            parts_short.append(f"剩 {_fmt_num(remain)}")
            parts_line.append(f"剩余 {_fmt_num(remain)}（已用 {_fmt_num(used)}/{_fmt_num(total)}）")
        else:
            parts_line.append("接口未返回额度字段")
        ci = checkin or {}
        if ci.get("code") == 0:
            if ci.get("checked_in"):
                parts_short.append("今日已领")
                parts_line.append(f"今日签到已领 +{_fmt_num(ci.get('credits', 0))}")
            elif ci.get("enable"):
                parts_short.append("今日未领")
                parts_line.append("今日签到还没领")
        status = "ok" if remain is not None else "warn"
        note = creds["nickname"] or creds["uid"]
        if note:
            parts_line.append(f"账号：{note}")
        summary = " · ".join(parts_short) if parts_short else "已连接"
        short = parts_short[0] if parts_short else "已连接"
        return _result(status, summary, short, parts_line)


# ══════════════════════════════════════════════════════════ Qoder Provider ══

QODER_API = "https://openapi.qoder.com.cn"
# 实测（Qoder CN 0.4.3）真实端点在前；后几条是早期误报候选保留的兜底，
# 找不到真端点时可自愈换用（命中结果会缓存复用）。
QODER_CREDITS_SUMMARY = "/sash/api/v1/ai-conversations/credits-summary"
QODER_PATHS = [
    "/sash/api/v2/me/usage",
    QODER_CREDITS_SUMMARY,
    "/sash/api/v1/me/usage",
    "/sash/api/v1/me/credits",
    "/sash/api/v1/me/quota",
]
QODER_VERSION = "0.4.3"


def qoder_userdata_dirs() -> list[Path]:
    ad = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return [Path(ad) / "com.qodercn.app.stable", Path(ad) / "com.qoder.app.stable"]


def qoder_os_crypt_key(userdata: Path) -> bytes | None:
    try:
        ls = json.loads((userdata / "Local State").read_text(encoding="utf-8"))
        enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    except Exception:
        return None
    if enc[:5] == b"DPAPI":
        enc = enc[5:]
    return _dpapi_unprotect(enc)


def qoder_decrypt_v10(blob: bytes, key: bytes | None) -> bytes | None:
    """Chromium OSCrypt 风格 v10：v10 + 12B nonce + 密文 + 16B 尾巴。

    实测 Qoder CN 0.4.3 的 16 字节尾巴不是标准 GCM tag（多种 GHASH 口径都对
    不上），所以按 CTR 解密后做 JSON 结构自检来代替 tag 校验——key/nonce 不
    对时解出的必然是乱码，不会误收。这个函数只服务于 auth.v1.dat（JSON）。
    """
    if key is None or blob[:3] != b"v10" or len(blob) < 3 + 12 + 16:
        return None
    w = _expand_key_nk(key, 8)
    j0 = blob[3:15] + b"\x00\x00\x00\x01"
    pt = _gcm_ctr(w, 14, j0, blob[15:-16])
    try:
        obj = json.loads(pt.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return pt if isinstance(obj, dict) else None


def _collect_token_candidates(obj, want: str = "auth") -> list[str]:
    """从解密后的凭据 JSON 里收集 token 候选（按名字优先度排序）。

    want='auth' → 访问 token；want='machine' → Cosy-MachineToken。
    只认形状像 token 的字符串；绝不外传、不打印。
    """
    hits: list[tuple[int, str]] = []

    def walk(o, name: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, str(k))
        elif isinstance(o, list):
            for v in o:
                walk(v, name)
        elif isinstance(o, str) and 16 <= len(o) <= 8192:
            if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", o):
                return
            lk = name.lower()
            if want == "auth":
                if not any(w in lk for w in ("token", "jwt", "auth", "credential")):
                    return
                if any(w in lk for w in ("refresh", "machine", "expire", "csrf")):
                    return
                score = 0 if "access" in lk else (1 if lk in ("token", "jwt") else 2)
            else:
                if not ("machine" in lk and "token" in lk):
                    return
                score = 0
            hits.append((score, o))

    walk(obj, "")
    seen, out = set(), []
    for _, t in sorted(hits, key=lambda x: x[0]):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _qoder_extract_numbers(obj) -> list[tuple[str, float]]:
    nums: list[tuple[str, float]] = []

    def walk(o, p: str) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{p}.{k}" if p else str(k))
        elif isinstance(o, list):
            for v in o[:8]:
                walk(v, p)
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            nums.append((p, float(o)))

    walk(obj, "")
    return nums


def qoder_parse_credits(j: dict) -> tuple[str, str, list[str]] | None:
    """尽力识别额度字段，返回 (summary, short, lines) 或 None。"""
    nums = _qoder_extract_numbers(j)
    if not nums:
        return None

    def pick(*pats, exclude=()):
        for p, v in nums:
            lp = p.lower()
            if any(e in lp for e in exclude):
                continue
            if any(pat in lp for pat in pats):
                return p, v
        return None, None

    remain_p, remain = pick("remain", "left", "available", "balance", "credit",
                            exclude=("refresh", "used", "usage", "consume",
                                     "spent"))
    total_p, total = pick("total", "quota", "limit", "grant")
    used_p, used = pick("used", "usage", "consume", "spent", "consumed")
    lines = []
    if remain is not None:
        short = f"剩 {_fmt_num(remain)}"
        summary = short
        lines.append(f"剩余 {_fmt_num(remain)}（字段 {remain_p}）")
    elif total is not None and used is not None:
        r = total - used
        short = f"剩 {_fmt_num(r)}"
        summary = f"剩 {_fmt_num(r)}（已用 {_fmt_num(used)}/{_fmt_num(total)}）"
        lines.append(summary)
    elif total is not None:
        short = f"额度 {_fmt_num(total)}"
        summary = short
        lines.append(f"额度 {_fmt_num(total)}（字段 {total_p}）")
    else:
        return None
    if used is not None and remain is not None:
        lines.append(f"已用：{_fmt_num(used)}")
    return summary, short, lines


def qoder_parse_usage(j: dict) -> tuple[str, str, list[str]] | None:
    """v2/me/usage 的结构化解析（实测：qoderUsage.userQuota 下
    total/used/remaining，单位为 credits；orgResourcePackage 为团队资源包）。"""
    qu = (j or {}).get("qoderUsage")
    if not isinstance(qu, dict):
        return None
    q = qu.get("userQuota") if isinstance(qu.get("userQuota"), dict) else {}
    total, used, remain = _f(q.get("total")), _f(q.get("used")), _f(q.get("remaining"))
    if remain is None and total is not None and used is not None:
        remain = round(total - used, 4)
    if remain is None:
        return None
    unit = str(q.get("unit") or "credits")
    short = f"剩 {_fmt_num(remain)}"
    lines = []
    if total is not None and used is not None:
        pct = _f(q.get("percentage"))
        tail = f"，{_fmt_num(pct * 100)}%" if pct is not None else ""
        lines.append(f"剩余 {_fmt_num(remain)} {unit}"
                     f"（已用 {_fmt_num(used)}/{_fmt_num(total)}{tail}）")
    else:
        lines.append(f"剩余 {_fmt_num(remain)} {unit}")
    org = qu.get("orgResourcePackage")
    if isinstance(org, dict) and org.get("available") is True:
        o_r = _f(org.get("remaining"))
        if o_r:
            lines.append(f"团队资源包剩余 {_fmt_num(o_r)} {unit}")
    if qu.get("isQuotaExceeded") is True:
        short = "额度用尽"
        lines.append("⚠ 额度已用尽")
    exp = _f(qu.get("expiresAt"))
    if exp:
        try:
            day = dt.datetime.fromtimestamp(exp / 1000.0).date().isoformat()
            lines.append(f"套餐有效期至 {day}")
        except (OverflowError, OSError, ValueError):
            pass
    return short, short, lines


def qoder_parse_summary(j: dict) -> tuple[str, str, list[str]] | None:
    """credits-summary 解析（实测：totalCredits / peakCredits / peakDate）。

    只有累计消耗、没有剩余额度，作补充展示或真端点失效时的兜底。
    """
    total = _f((j or {}).get("totalCredits"))
    if total is None:
        return None
    peak, peak_date = _f(j.get("peakCredits")), str(j.get("peakDate") or "")
    short = f"累计 {_fmt_num(total)}"
    line = f"AI 累计消耗 {_fmt_num(total)} credits"
    if peak:
        line += f"（峰值 {_fmt_num(peak)}/天"
        line += f"，{peak_date}）" if peak_date else "）"
    return short, short, [line]


def _qoder_parse_by_path(path: str, j: dict) -> tuple[str, str, list[str]] | None:
    """按端点选结构化解析器，识别不了再退回通用数字提取。"""
    if isinstance(j.get("qoderUsage"), dict):
        got = qoder_parse_usage(j)
        if got:
            return got
    if "credits-summary" in path:
        got = qoder_parse_summary(j)
        if got:
            return got
    return qoder_parse_credits(j)


class QoderProvider:
    def __init__(self, session=None, hint_endpoint: str = "") -> None:
        self.key, self.label, self.short = "qoder_cn", "Qoder CN", "Qoder"
        self._session = session
        self.found_endpoint = hint_endpoint  # 上次命中的端点（来自缓存）

    def _get(self, url: str, headers: dict):
        sess = self._session
        if sess is None:
            try:
                import requests
            except ImportError:
                return None, "no-requests"
            sess = requests
        try:
            resp = sess.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            try:
                return resp.json(), resp.status_code
            except ValueError:
                return None, resp.status_code
        except Exception as e:
            return None, repr(e)

    def query(self) -> dict:
        try:
            return self._query()
        except Exception as e:
            return _result("err", f"查询出错：{e!r}"[:120])

    def _query(self) -> dict:
        userdata = next((d for d in qoder_userdata_dirs()
                         if (d / "auth.v1.dat").exists()), None)
        if userdata is None:
            return _result("off", "未找到 Qoder 客户端数据（先安装并登录一次）")
        key = qoder_os_crypt_key(userdata)
        try:
            blob = (userdata / "auth.v1.dat").read_bytes()
        except OSError:
            return _result("warn", "登录凭据不可读（正在被客户端占用？）")
        pt = qoder_decrypt_v10(blob, key)
        if pt is None:
            return _result("warn", "登录凭据解密失败（客户端版本可能变了）")
        try:
            obj = json.loads(pt.decode("utf-8", "replace"))
        except ValueError:
            return _result("warn", "登录凭据格式无法识别")
        tokens = _collect_token_candidates(obj, "auth")
        if not tokens:
            return _result("warn", "凭据里没有可用的 token（结构可能变了）")
        # 过期就不必再打接口了（实测字段：expiresAt / refreshTokenExpiresAt）
        exp = str(obj.get("expiresAt") or "")
        if exp:
            try:
                exp_dt = dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if exp_dt < dt.datetime.now(dt.timezone.utc):
                    return _result("warn", "登录态已过期：打开一次 Qoder 刷新")
            except ValueError:
                pass
        user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
        who = str(user.get("name") or user.get("email") or "")
        try:
            machine_id = (userdata / "auth.machine-id").read_text(
                encoding="utf-8").strip()
        except OSError:
            machine_id = ""

        paths = ([self.found_endpoint] if self.found_endpoint else []) + \
            [p for p in QODER_PATHS if p != self.found_endpoint]
        best_note = ""
        any_json = False
        for path in paths:
            n_resp = n_invalid = 0
            for tok in tokens[:3]:
                headers = {
                    "Authorization": "Bearer " + tok,
                    "Accept": "application/json",
                    "User-Agent": "Qoder",
                    "Cosy-ClientType": "10",
                    "Cosy-Version": QODER_VERSION,
                }
                if machine_id:
                    headers["Cosy-MachineId"] = machine_id
                j, meta = self._get(QODER_API + path, headers)
                if j is None:
                    continue
                n_resp += 1
                any_json = True
                if str(j.get("code", "")) == "TOKEN_INVALID":
                    n_invalid += 1
                    continue
                if meta == 404 or str(j.get("errorCode", "")).lower() == "notfound":
                    continue                  # 该路由不存在，换下一个候选
                parsed = _qoder_parse_by_path(path, j)
                if parsed:
                    self.found_endpoint = path
                    summary, short, lines = parsed
                    if path == "/sash/api/v2/me/usage":
                        # 真额度端点：顺带取累计消耗/峰值做补充行（失败不影响主结果）
                        j2, _m2 = self._get(QODER_API + QODER_CREDITS_SUMMARY,
                                            headers)
                        p2 = qoder_parse_summary(j2) if isinstance(j2, dict) \
                            else None
                        if p2:
                            lines.extend(p2[2])
                    if who:
                        lines.append(f"账号：{who}")
                    lines.append(f"数据端点：{path}")
                    return _result("ok", summary, short, lines)
                if not best_note:
                    best_note = f"已连上 {path} 但没识别出额度字段"
            if n_resp and n_invalid == n_resp:
                # 该端点所有 token 都被判无效：是登录态问题，换端点也没用
                return _result("warn", "登录态无效：打开一次 Qoder 刷新")
        if best_note:
            return _result("warn", best_note)
        if any_json:
            # 登录态可用（服务端认可 token），只是额度端点还没识别出来；
            # 候选表命中一个新端点后会自动缓存复用（自愈）
            lines = []
            if who:
                lines.append(f"账号：{who}")
            if exp:
                lines.append(f"登录态有效至 {exp[:10]}")
            lines.append("额度接口暂未识别（其余查询正常，见 README）")
            return _result("warn", "已登录 · 额度接口未识别", "已登录", lines)
        return _result("warn", "查询失败（网络或接口变动）")


# ═══════════════════════════════════════════════════════ WorkBuddy Provider ══

WB_API = "https://www.workbuddy.cn"
EP_WB_SUMMARY = WB_API + "/billing/meter/get-user-resource-summary"
EP_WB_CHECKIN = WB_API + "/v2/billing/meter/checkin-activity-status"


def wb_home() -> Path:
    return Path.home() / ".workbuddy"


def wb_latest_checkin(home: Path) -> dict | None:
    """从客户端日志里取最近一条 [Checkin] fetchCheckinStatus success 记录。"""
    logs = home / "logs"
    cands = [logs / "main.log", logs / "main.old.log"]
    try:
        cands += sorted(logs.glob("*/main.log"), reverse=True)
    except OSError:
        pass
    for p in cands:
        try:
            if not p.exists():
                continue
            txt = _tail_text(p, 1024 * 1024)
        except OSError:
            continue
        for line in reversed(txt.splitlines()):
            if "fetchCheckinStatus success" not in line:
                continue
            try:
                obj = json.loads(line)
                msg = obj.get("message") or []
                data = msg[1] if len(msg) > 1 and isinstance(msg[1], dict) else None
            except ValueError:
                continue
            if not data:
                continue
            out = dict(data)
            out["_log_at"] = str(obj.get("timestamp", ""))
            return out
    return None


def wb_today_usage(db_path: Path, today: dt.date) -> tuple[float, int] | None:
    """本地台账：sum(session_usage.credit_json 值) for 今天更新的会话。"""
    if not db_path.exists():
        return None
    tmp = Path(tempfile.gettempdir()) / "habitpet_wb.sqlite"
    try:
        shutil.copy2(db_path, tmp)
        con = sqlite3.connect(f"file:{tmp.as_posix()}?mode=ro&immutable=1",
                              uri=True)
    except Exception:
        return None
    try:
        rows = con.execute(
            "SELECT credit_json, updated_at FROM session_usage").fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    total, n = 0.0, 0
    for cj, ua in rows:
        day = _wb_row_day(ua)
        if day != today:
            continue
        try:
            d = json.loads(cj) if isinstance(cj, str) else (cj or {})
            vals = [float(v) for v in d.values()]
        except (ValueError, TypeError, AttributeError):
            continue
        if vals:
            total += sum(vals)
            n += 1
    return (round(total, 2), n) if n else None


def _wb_row_day(updated_at) -> dt.date | None:
    try:
        ts = float(updated_at)
        if ts > 1e12:
            ts /= 1000.0
        return dt.datetime.fromtimestamp(ts).date()
    except (TypeError, ValueError):
        pass
    try:
        return dt.date.fromisoformat(str(updated_at)[:10])
    except ValueError:
        return None


def wb_parse_summary(j: dict) -> dict | None:
    """服务端资源汇总解析（实测：data.Packages[].CycleTotal/Remain/UsedCapacity，
    CapacityUnit=credits）。返回 {remain, used, total, packages, plan, paid}。"""
    data = (j or {}).get("data")
    if not isinstance(data, dict):
        return None
    pkgs = data.get("Packages")
    if not isinstance(pkgs, list) or not pkgs:
        return None
    total = used = remain = 0.0
    n = 0
    for p in pkgs:
        if not isinstance(p, dict):
            continue
        t = _f(p.get("CycleTotalCapacity"))
        u = _f(p.get("CycleUsedCapacity"))
        r = _f(p.get("CycleRemainCapacity"))
        if r is None and (t is not None or u is not None):
            r = (t or 0.0) - (u or 0.0)
        if t is None and u is None and r is None:
            continue
        total += t or 0.0
        used += u or 0.0
        remain += r or 0.0
        n += 1
    if not n:
        return None
    return {"remain": round(remain, 2), "used": round(used, 2),
            "total": round(total, 2), "packages": n,
            "plan": str(data.get("SubscriptionPackageName") or ""),
            "paid": bool(data.get("IsPaidUser"))}


def _wb_checkin_seg(ck: dict, from_log: bool = False) -> str:
    checked = bool(ck.get("today_checked_in"))
    streak = ck.get("streak_days")
    credit = ck.get("today_credit")
    total = ck.get("total_credits")
    if checked:
        seg = f"今日已签 +{_fmt_num(credit)}" if credit is not None \
            else "今日已签到"
        extra = []
        if streak:
            extra.append(f"连续 {_fmt_num(streak)} 天")
        if total is not None:
            extra.append(f"累计 {_fmt_num(total)}")
        if extra:
            seg += f"（{'，'.join(extra)}）"
    else:
        seg = "今日还未签到"
        if streak:
            seg += f"（连续 {_fmt_num(streak)} 天）"
    if from_log:
        at = str(ck.get("_log_at", ""))[:10]
        if at and at != dt.date.today().isoformat():
            seg = f"最近一次（{at}）：" + seg.replace("今日", "")
    return seg


class WorkBuddyProvider:
    """有 token 走服务端实时额度；无 token 或登录态失效时用本地台账降级。

    token 来自 config.json 的 credits.workbuddy_token（python -m habitpet.wbgrab
    可一键抓取），只进内存、绝不写日志。
    """

    def __init__(self, token: str = "", home: Path | None = None,
                 session=None) -> None:
        self.key, self.label, self.short = "workbuddy", "WorkBuddy", "WB"
        self._token = (token or "").strip()
        self._home = home or wb_home()
        self._session = session

    def _post(self, url: str, headers: dict, body: dict):
        """返回 (json|None, status|错误摘要)。"""
        sess = self._session
        if sess is None:
            try:
                import requests
            except ImportError:
                return None, "no-requests"
            sess = requests
        try:
            resp = sess.post(url, headers=headers,
                             data=json.dumps(body).encode("utf-8"),
                             timeout=HTTP_TIMEOUT)
            try:
                return resp.json(), resp.status_code
            except ValueError:
                return None, resp.status_code
        except Exception as e:
            return None, repr(e)

    def query(self) -> dict:
        try:
            return self._query()
        except Exception as e:
            return _result("err", f"查询出错：{e!r}"[:120])

    def _query(self) -> dict:
        if not self._home.exists():
            return _result("off", "未找到 WorkBuddy 数据目录")
        shorts, details = [], []
        parsed = None
        ck = None
        auth_fail = False

        if self._token:
            hdr = {"Authorization": "Bearer " + self._token,
                   "Content-Type": "application/json",
                   "Accept": "application/json",
                   "Accept-Language": "zh-CN",
                   "User-Agent": "WorkBuddy/1.0"}
            ck_j, cmeta = self._post(EP_WB_CHECKIN, hdr, {})
            sm_j, smeta = self._post(EP_WB_SUMMARY, hdr, {})
            if isinstance(sm_j, dict):
                parsed = wb_parse_summary(sm_j)
            if isinstance(ck_j, dict):
                ck = ck_j.get("data") if isinstance(ck_j.get("data"), dict) \
                    else ck_j
            ck_ok = isinstance(ck, dict) and "today_checked_in" in ck
            if not parsed and not ck_ok:
                auth_fail = cmeta in (401, 403) or smeta in (401, 403)

        if parsed:
            shorts.append(f"剩 {_fmt_num(parsed['remain'])}")
            seg = f"剩余 {_fmt_num(parsed['remain'])} credits"
            tags = [t for t in (parsed.get("plan"),
                                f"{parsed['packages']} 个资源包") if t]
            if tags:
                seg += f"（{' · '.join(tags)}）"
            seg += (f"，已用 {_fmt_num(parsed['used'])}/"
                    f"{_fmt_num(parsed['total'])}")
            details.append(seg)
        if isinstance(ck, dict) and "today_checked_in" in ck:
            shorts.append("已签" if ck.get("today_checked_in") else "未签")
            details.append("签到：" + _wb_checkin_seg(ck))

        if not shorts:
            # 本地降级：日志签到 + 台账消耗
            checkin = wb_latest_checkin(self._home)
            if checkin is not None:
                checkin = dict(checkin)
                checkin["_from"] = "log"
                shorts.append("已签" if checkin.get("today_checked_in")
                              else "未签")
                details.append(_wb_checkin_seg(checkin, from_log=True))
            usage = wb_today_usage(self._home / "workbuddy.db",
                                   dt.date.today())
            if usage is not None:
                total, n = usage
                shorts.append(f"耗{_fmt_num(total)}")
                details.append(f"本机今日消耗 ≈ {_fmt_num(total)} 分"
                               f"（{n} 个会话）")
            if self._token and auth_fail:
                details.append("token 已失效：重开 WorkBuddy 后跑 "
                               "python -m habitpet.wbgrab --write 更新")
            elif not self._token:
                details.append("本地口径（日志/台账）；python -m "
                               "habitpet.wbgrab 可解锁服务端实时额度")
        if not shorts:
            return _result("warn", "暂无可读数据（打开一次 WorkBuddy 再看）")
        return _result("ok", " · ".join(details), " ".join(shorts), details)


# ══════════════════════════════════════════════════════════ 聚合 Tracker ══

class CreditsTracker:
    """四家积分的后台轮询器（与 BalanceTracker 同一套路，绝不阻塞主线程）。"""

    def __init__(self, config_dir: Path, conf: dict, log=None,
                 session=None) -> None:
        self.dir = Path(config_dir)
        self.conf = dict(conf or {})
        self.log = log or (lambda _m: None)
        self.path = self.dir / "credits.json"
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._running = False
        self._last_poll = 0.0
        self.cache = self._load()
        self.results: dict[str, dict] = dict(self.cache.get("results") or {})
        self.providers = self._build_providers(session)

    # ------------------------------------------------------------ 构建

    def _build_providers(self, session) -> list:
        pconf = self.conf.get("providers") or {}

        def on(key: str) -> bool:
            return bool((pconf.get(key) or {}).get("enabled", True))

        provs: list = []
        if on("trae_cn"):
            provs.append(TraeProvider("trae_cn", "Trae CN",
                                      TRAE_CLIENTS["Trae CN"], "Trae",
                                      session=session))
        if on("traework_cn"):
            provs.append(TraeProvider("traework_cn", "TraeWork CN",
                                      TRAE_CLIENTS["TraeWork CN"], "TraeWork",
                                      session=session))
        if on("workbuddy"):
            provs.append(WorkBuddyProvider(
                token=str(self.conf.get("workbuddy_token") or ""),
                session=session))
        if on("qoder_cn"):
            provs.append(QoderProvider(
                session=session,
                hint_endpoint=str(self.cache.get("qoder_endpoint") or "")))
        return provs

    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True)) and bool(self.providers)

    # ------------------------------------------------------------ 轮询

    def maybe_poll(self, now: float | None = None, force: bool = False,
                   keys: list[str] | None = None) -> bool:
        if not self.enabled() or self._running:
            return False
        now = now if now is not None else dt.datetime.now().timestamp()
        interval = max(MIN_POLL_SECONDS,
                       int(self.conf.get("poll_seconds", DEFAULT_POLL_SECONDS)
                           or DEFAULT_POLL_SECONDS))
        if not force and now - self._last_poll < interval:
            return False
        self._running = True
        self._last_poll = now
        threading.Thread(target=self._work, args=(keys,), daemon=True).start()
        return True

    def _work(self, keys: list[str] | None = None) -> None:
        try:
            picked = [p for p in self.providers
                      if not keys or p.key in keys]
            for p in picked:
                try:
                    res = p.query()
                except Exception as e:   # provider 已兜底，这里再保险一层
                    res = _result("err", f"查询出错：{e!r}"[:120])
                self.results[p.key] = res
            self._save()
            self._queue.put(("credits_done", list(keys or []), None))
        except Exception as e:
            self._queue.put(("credits_done", list(keys or []), repr(e)))
        finally:
            self._running = False

    # ------------------------------------------------------------ 对外视图

    def short_of(self, key: str) -> str:
        r = self.results.get(key)
        if not r:
            return ""
        return str(r.get("short") or r.get("summary") or "")

    def status_lines(self) -> list[str]:
        if not self.conf.get("enabled", True):
            return ["🎁 积分：已关闭（config.json → credits.enabled）"]
        if not self.providers:
            return ["🎁 积分：四家都被单独关掉了（config.json → credits.providers）"]
        if not self.results:
            return ["🎁 积分：暂无数据（右键菜单 → 剩余积分 点一次刷新）"]
        chunks = []
        for p in self.providers:
            r = self.results.get(p.key)
            # 气泡只放拿到数的；有提示没数据的给个短横线，细节留给右键菜单
            if r and r.get("status") == "ok":
                chunks.append(f"{p.short} {r.get('short', '')}".strip())
            else:
                chunks.append(f"{p.short} —")
        lines, cur = [], "🎁 积分"
        for c in chunks:
            if len(cur) + len(c) + 3 > 30:
                lines.append(cur)
                cur = "　　" + c
            else:
                cur += " · " + c
        lines.append(cur)
        return lines

    def menu_rows(self) -> list[tuple[str, str]]:
        """给右键子菜单用：[(label, provider_key)]。"""
        rows = []
        for p in self.providers:
            r = self.results.get(p.key)
            if not r:
                rows.append((f"{p.label}：点我查询", p.key))
            else:
                status = r.get("status")
                mark = "✓" if status == "ok" else ("…" if status == "warn" else "×")
                rows.append((f"{p.label}：{mark} {r.get('summary', '')}",
                             p.key))
        return rows

    def drain(self) -> list[tuple]:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events

    def smoke_summary(self) -> dict:
        return {p.key: {"status": (self.results.get(p.key) or {}).get("status"),
                        "summary": (self.results.get(p.key) or {}).get("summary")}
                for p in self.providers}

    # ------------------------------------------------------------ 持久化

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        hints = self.cache.get("qoder_endpoint", "")
        for p in self.providers:
            if isinstance(p, QoderProvider) and p.found_endpoint:
                hints = p.found_endpoint
        data = {"updated": dt.datetime.now().isoformat(timespec="seconds"),
                "qoder_endpoint": hints,
                "results": self.results}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
            self.cache = data
        except OSError as e:
            self.log(f"[credits] 缓存写入失败：{e!r}")


# ══════════════════════════════════════════════════════════════ 命令行 ══

def _main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    keys = [a for a in argv[1:] if not a.startswith("-")]
    # 默认复用 ~/.habitpet/config.json 的 credits 段（含 workbuddy_token）；
    # 也可显式传 provider key 只查其中几家。
    conf: dict = {}
    try:
        raw = json.loads((Path.home() / ".habitpet" / "config.json")
                         .read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("credits"), dict):
            conf = dict(raw["credits"])
    except (OSError, ValueError):
        pass
    conf["enabled"] = True
    if keys:
        conf["providers"] = {
            k: {"enabled": k in keys}
            for k in ("trae_cn", "traework_cn", "workbuddy", "qoder_cn")}
    tr = CreditsTracker(Path.home() / ".habitpet", conf)
    out = {}
    for p in tr.providers:
        r = p.query()
        out[p.key] = {"status": r["status"], "summary": r["summary"],
                      "lines": r["lines"]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
