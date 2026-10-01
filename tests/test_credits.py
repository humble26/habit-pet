"""剩余积分测试：加密原语 / Trae 凭据与查询 / Qoder 解密链与字段识别 /
WorkBuddy 本地台账 / CreditsTracker 缓存与视图（全离线，不碰真实凭据与网络）。"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from habitpet import credits as cr


# ------------------------------------------------------------ 测试替身 --

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    """按队列返回响应；队列空了就报错，防止测试偷偷拖到网络。"""

    def __init__(self, posts=None, gets=None, exc=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.exc = exc
        self.calls: list[tuple] = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append(("post", url, dict(headers or {})))
        if self.exc:
            raise self.exc
        if not self.posts:
            raise AssertionError(f"unexpected POST {url}")
        return FakeResp(self.posts.pop(0))

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url, dict(headers or {})))
        if self.exc:
            raise self.exc
        if not self.gets:
            raise AssertionError(f"unexpected GET {url}")
        return FakeResp(self.gets.pop(0))


def wait_for(pred, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def wrap_trae_value(payload: dict) -> str:
    """用与解密同源的原语把 payload 封成 storage.json 的值（逆过程）。"""
    rb = os.urandom(32)
    fh = hashlib.sha512(hashlib.sha512(rb).digest() + cr.SALT_AES).digest()
    # 客户端是 JS，中文按原样（不转义）写进 JSON
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    inner = cr.aes128_cbc_encrypt(fh[:16], fh[16:32], b"\x00" * 64 + body)
    return base64.b64encode(b"\x00" * 6 + rb + inner).decode()


def write_trae_storage(root: Path, client: str, payload: dict,
                       dev_key: str | None = "1747546889791155") -> Path:
    p = root / client / "User" / "globalStorage" / "storage.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    s = {cr.TRAE_STORAGE_KEY: wrap_trae_value(payload)}
    if dev_key:
        s[f"iCubeAuthInfo://icube-dc:{dev_key}"] = "x"
    p.write_text(json.dumps(s), encoding="utf-8")
    return p


def dpapi_protect(data: bytes) -> bytes:
    """测试用：本机 CryptProtectData（只保护测试数据，不是真实凭据）。"""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    bi = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    bo = DATA_BLOB()
    assert ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo))
    try:
        return ctypes.string_at(bo.pbData, bo.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(bo.pbData)


# ------------------------------------------------------------ AES 原语 --

class TestAESCore(unittest.TestCase):
    def test_fips197_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        w = cr._expand_key(key)
        self.assertEqual(cr._encrypt_block(w, pt), ct)
        self.assertEqual(cr._decrypt_block(w, ct), pt)

    def test_cbc_roundtrip_and_padding(self):
        key, iv = os.urandom(16), os.urandom(16)
        for data in (b"", b"x", b"hello habitpet" * 3, os.urandom(64)):
            ct = cr.aes128_cbc_encrypt(key, iv, data)
            pt = cr.aes128_cbc_decrypt(key, iv, ct)
            pad = pt[-1]                      # 原始原语不做去填充，调用方自行处理
            self.assertEqual(pt[:-pad], data)

    def test_cbc_rejects_unaligned(self):
        with self.assertRaises(ValueError):
            cr.aes128_cbc_decrypt(b"k" * 16, b"i" * 16, b"abc")

    def test_trae_blob_roundtrip(self):
        payload = {"refreshToken": "rt-1", "token": "at-1", "userId": "42"}
        out = cr.trae_decrypt_storage_value(wrap_trae_value(payload))
        self.assertEqual(json.loads(out.decode()), payload)


class TestGCM(unittest.TestCase):
    """AES-256-GCM：NIST / GCM spec 公开测试向量 + 结构测试。"""

    def test_aes256_fips197_block(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f"
                            "101112131415161718191a1b1c1d1e1f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
        w = cr._expand_key_nk(key, 8)
        self.assertEqual(cr._encrypt_block_nr(w, 14, pt), ct)

    def test_gcm_zero_length_vector(self):
        # GCM spec Test Case 1：全零密钥/nonce、空明文 → 固定 tag
        ct, tag = cr.aes256_gcm_encrypt(b"\x00" * 32, b"\x00" * 12, b"")
        self.assertEqual(ct, b"")
        self.assertEqual(tag.hex(), "530f8afbc74536b9a963b4f1c4cb738b")

    def test_gcm_16_zeroes_vector(self):
        # GCM spec Test Case 2
        ct, tag = cr.aes256_gcm_encrypt(b"\x00" * 32, b"\x00" * 12, b"\x00" * 16)
        self.assertEqual(ct.hex(), "cea7403d4d606b6e074ec5d3baf39d18")
        self.assertEqual(tag.hex(), "d0d1c8a799996bf0265b98b5d48ab919")
        self.assertEqual(cr.aes256_gcm_decrypt(b"\x00" * 32, b"\x00" * 12,
                                               ct, tag), b"\x00" * 16)

    def test_roundtrip_and_tamper(self):
        key, nonce = os.urandom(32), os.urandom(12)
        pt = b"qoder-auth-sample" * 4
        ct, tag = cr.aes256_gcm_encrypt(key, nonce, pt)
        self.assertEqual(cr.aes256_gcm_decrypt(key, nonce, ct, tag), pt)
        bad = bytes([tag[0] ^ 1]) + tag[1:]
        self.assertIsNone(cr.aes256_gcm_decrypt(key, nonce, ct, bad))
        bad_ct = bytes([ct[0] ^ 1]) + ct[1:]
        self.assertIsNone(cr.aes256_gcm_decrypt(key, nonce, bad_ct, tag))
        self.assertIsNone(cr.aes256_gcm_decrypt(key, nonce, ct, tag[:12]))

    def test_decrypt_v10_layout(self):
        key, nonce = os.urandom(32), os.urandom(12)
        pt = json.dumps({"accessToken": "x" * 40}).encode()
        ct, tag = cr.aes256_gcm_encrypt(key, nonce, pt)
        blob = b"v10" + nonce + ct + tag
        self.assertEqual(cr.qoder_decrypt_v10(blob, key), pt)
        self.assertIsNone(cr.qoder_decrypt_v10(b"v11" + nonce + ct + tag, key))
        self.assertIsNone(cr.qoder_decrypt_v10(blob, None))
        # 明文不是 JSON 字典时自检拒绝（代替 tag 校验）
        ct2, tag2 = cr.aes256_gcm_encrypt(key, nonce, b"just plain bytes")
        self.assertIsNone(cr.qoder_decrypt_v10(b"v10" + nonce + ct2 + tag2, key))


# --------------------------------------------------------- Windows 原语 --

@unittest.skipUnless(os.name == "nt", "Windows only")
class TestWindowsCrypto(unittest.TestCase):
    def test_dpapi_roundtrip(self):
        data = b"habitpet-test-blob"
        self.assertEqual(cr._dpapi_unprotect(dpapi_protect(data)), data)


# ------------------------------------------------------------ Trae 系 --

class TestTraeCreds(unittest.TestCase):
    def test_creds_parsed_with_real_device_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_trae_storage(Path(d), "Trae CN",
                                   {"refreshToken": "rt-1", "token": "at-1",
                                    "userId": "2363265005414807",
                                    "username": "鲸鱼测试"})
            c = cr.trae_read_creds(p)
        self.assertEqual(c["access"], "at-1")
        self.assertEqual(c["refresh"], "rt-1")
        self.assertEqual(c["uid"], "2363265005414807")
        self.assertEqual(c["nickname"], "鲸鱼测试")
        self.assertEqual(c["device_id"], "1747546889791155")

    def test_device_id_falls_back_to_derived(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_trae_storage(Path(d), "Trae CN",
                                   {"refreshToken": "rt-9", "token": "at"},
                                   dev_key=None)
            c = cr.trae_read_creds(p)
        self.assertEqual(c["device_id"], cr.trae_stable_device_id("rt-9"))
        self.assertEqual(len(c["device_id"]), 16)


class TestTraeProvider(unittest.TestCase):
    def _provider(self, root: Path, client: str, sess) -> cr.TraeProvider:
        env = {"APPDATA": str(root)}
        with mock.patch.dict(os.environ, env):
            return cr.TraeProvider("trae_cn", "Trae CN", [client], "Trae",
                                   session=sess)

    def test_query_ok_combines_entitlement_and_checkin(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_trae_storage(root, "Trae CN",
                               {"refreshToken": "rt", "token": "at-1"})
            sess = FakeSession(posts=[
                {"code": 0, "usage_summary": {"total_amount": 1000,
                                              "consumed_amount": 188.4}},
                {"code": 0, "checked_in": True, "credits": 100, "enable": True},
            ])
            p = self._provider(root, "Trae CN", sess)
            res = p.query()
        self.assertEqual(res["status"], "ok")
        self.assertIn("811.6", res["summary"])
        self.assertIn("已领", res["summary"])
        self.assertEqual(sess.calls[0][1], cr.EP_TRAE_ENTITLE)
        auth = sess.calls[0][2]["Authorization"]
        self.assertTrue(auth.startswith("Cloud-IDE-JWT "))
        self.assertEqual(sess.calls[0][2]["x-device-id"], "1747546889791155")

    def test_query_expired_token_short_circuits(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_trae_storage(root, "Trae CN",
                               {"refreshToken": "rt", "token": "at"})
            # 只留一个响应：token 失效时应直接返回，不再打签到接口
            sess = FakeSession(posts=[{"code": 1001, "message": "auth"}])
            p = self._provider(root, "Trae CN", sess)
            res = p.query()
        self.assertEqual(res["status"], "warn")
        self.assertIn("过期", res["summary"])
        self.assertEqual(len(sess.calls), 1)

    def test_missing_storage_is_off(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._provider(Path(d), "Trae CN", FakeSession())
            res = p.query()
        self.assertEqual(res["status"], "off")
        self.assertIn("未找到客户端", res["summary"])

    def test_garbage_credential_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pth = root / "Trae CN" / "User" / "globalStorage" / "storage.json"
            pth.parent.mkdir(parents=True)
            pth.write_text(json.dumps({cr.TRAE_STORAGE_KEY: "bm90LXJlYWw="}),
                           encoding="utf-8")
            p = self._provider(root, "Trae CN", FakeSession())
            res = p.query()
        self.assertEqual(res["status"], "warn")


# ------------------------------------------------------------- Qoder --

class TestQoderHelpers(unittest.TestCase):
    def test_collect_token_candidates_auth(self):
        obj = {"accessToken": "A" * 40, "refreshToken": "R" * 40,
               "machineToken": "M" * 40,
               "nested": {"jwt": "J" * 40, "expiresAt": 123}}
        got = cr._collect_token_candidates(obj, "auth")
        self.assertEqual(got[0], "A" * 40)          # access 优先
        self.assertIn("J" * 40, got)
        self.assertNotIn("R" * 40, got)
        self.assertNotIn("M" * 40, got)

    def test_collect_token_candidates_machine(self):
        obj = {"accessToken": "A" * 40, "machineToken": "M" * 40}
        self.assertEqual(cr._collect_token_candidates(obj, "machine"),
                         ["M" * 40])

    def test_parse_credits_remaining(self):
        parsed = cr.qoder_parse_credits(
            {"data": {"credits": {"remaining": 1234.5, "used": 765}}})
        self.assertIsNotNone(parsed)
        summary, short, lines = parsed
        self.assertIn("1234.5", summary)
        self.assertTrue(any("remaining" in l for l in lines))

    def test_parse_credits_total_minus_used(self):
        parsed = cr.qoder_parse_credits({"quota": {"total": 100, "used": 30}})
        summary, short, _ = parsed
        self.assertIn("70", summary)

    def test_parse_credits_none_when_unknown(self):
        self.assertIsNone(cr.qoder_parse_credits({}))
        self.assertIsNone(cr.qoder_parse_credits({"a": 1, "ok": True}))

    def test_query_no_userdata_is_off(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"APPDATA": d}):
                p = cr.QoderProvider(session=FakeSession())
                res = p.query()
        self.assertEqual(res["status"], "off")


@unittest.skipUnless(os.name == "nt", "Windows only")
class TestQoderFullChain(unittest.TestCase):
    def _make_userdata(self, root: Path, payload: dict | None = None) -> Path:
        master = os.urandom(32)
        ud = root / "com.qodercn.app.stable"
        ud.mkdir(parents=True)
        (ud / "Local State").write_text(json.dumps({
            "os_crypt": {"encrypted_key": base64.b64encode(
                b"DPAPI" + dpapi_protect(master)).decode()}}),
            encoding="utf-8")
        nonce = os.urandom(12)
        pt = json.dumps(payload or {"accessToken": "T" * 40}).encode()
        ct, tag = cr.aes256_gcm_encrypt(master, nonce, pt)
        (ud / "auth.v1.dat").write_bytes(b"v10" + nonce + ct + tag)
        (ud / "auth.machine-id").write_text("m" * 36, encoding="utf-8")
        return ud

    def test_oscrypt_chain_reaches_invalid_token_status(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_userdata(root)
            sess = FakeSession(gets=[{"code": "TOKEN_INVALID",
                                      "message": "invalid token"}] * 6)
            with mock.patch.dict(os.environ, {"APPDATA": str(root)}):
                p = cr.QoderProvider(session=sess)
                res = p.query()
        self.assertEqual(res["status"], "warn")
        self.assertIn("登录态无效", res["summary"])
        url = sess.calls[0][1]
        self.assertTrue(url.startswith(cr.QODER_API + "/sash/api/v1/me/"))
        self.assertTrue(sess.calls[0][2]["Authorization"].startswith("Bearer "))

    def test_logged_in_but_endpoint_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_userdata(root, {
                "token": "T" * 40, "refreshToken": "R" * 40,
                "expiresAt": "2099-01-01T00:00:00Z",
                "user": {"name": "测试用户"}})
            sess = FakeSession(gets=[{"errorCode": "NotFound",
                                      "errorMessage": "Not found"}] * 12)
            with mock.patch.dict(os.environ, {"APPDATA": str(root)}):
                p = cr.QoderProvider(session=sess)
                res = p.query()
        self.assertIn("额度接口未识别", res["summary"])
        self.assertTrue(any("测试用户" in l for l in res["lines"]))

    def test_expired_token_short_circuits_without_http(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_userdata(root, {"token": "T" * 40,
                                       "expiresAt": "2000-01-01T00:00:00Z"})
            sess = FakeSession()               # 队列为空：一旦发请求就抛错
            with mock.patch.dict(os.environ, {"APPDATA": str(root)}):
                res = cr.QoderProvider(session=sess).query()
        self.assertIn("过期", res["summary"])
        self.assertEqual(sess.calls, [])


# ---------------------------------------------------------- WorkBuddy --

class TestWorkBuddy(unittest.TestCase):
    def test_latest_checkin_from_log(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "logs").mkdir()
            lines = [
                {"timestamp": "2026-09-30T01:00:00.000Z", "message": [
                    "[Checkin] fetchCheckinStatus success",
                    {"today_checked_in": False, "streak_days": 1,
                     "today_credit": 100}]},
                {"timestamp": "2026-10-01T04:46:14.312Z", "message": [
                    "[Checkin] fetchCheckinStatus success",
                    {"today_checked_in": True, "streak_days": 2,
                     "today_credit": 100}]},
            ]
            (home / "logs" / "main.log").write_text(
                "\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            got = cr.wb_latest_checkin(home)
        self.assertTrue(got["today_checked_in"])
        self.assertEqual(got["streak_days"], 2)
        self.assertEqual(got["_log_at"], "2026-10-01T04:46:14.312Z")

    def test_row_day_formats(self):
        now = dt.datetime.now()
        self.assertEqual(cr._wb_row_day(int(now.timestamp() * 1000)),
                         now.date())
        self.assertEqual(cr._wb_row_day(now.date().isoformat()),
                         now.date())
        self.assertIsNone(cr._wb_row_day("nonsense"))

    def test_today_usage_sums_credit_json(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "workbuddy.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE session_usage (session_id TEXT, used INT,"
                        " size INT, updated_at INT, credit_json TEXT)")
            today_ms = int(dt.datetime.now().timestamp() * 1000)
            old_ms = today_ms - 3 * 86400 * 1000
            con.execute("INSERT INTO session_usage VALUES (?,?,?,?,?)",
                        ("s1", 1, 2, today_ms, json.dumps({"a": 1.5, "b": 2.5})))
            con.execute("INSERT INTO session_usage VALUES (?,?,?,?,?)",
                        ("s2", 1, 2, today_ms, json.dumps({"c": 0.5})))
            con.execute("INSERT INTO session_usage VALUES (?,?,?,?,?)",
                        ("s3", 1, 2, old_ms, json.dumps({"d": 99})))
            con.commit()
            con.close()
            got = cr.wb_today_usage(db, dt.date.today())
        self.assertEqual(got, (4.5, 2))

    def test_provider_degrades_to_local_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "logs").mkdir()
            (home / "logs" / "main.log").write_text(json.dumps(
                {"timestamp": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                 "message": ["[Checkin] fetchCheckinStatus success",
                             {"today_checked_in": True, "streak_days": 2,
                              "today_credit": 100}]}), encoding="utf-8")
            p = cr.WorkBuddyProvider(home=home, session=FakeSession())
            res = p.query()
        self.assertEqual(res["status"], "ok")
        self.assertIn("已签", res["summary"])
        self.assertIn("连续 2 天", res["summary"])

    def test_provider_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            p = cr.WorkBuddyProvider(home=Path(d), session=FakeSession())
            res = p.query()
        self.assertEqual(res["status"], "warn")
        with tempfile.TemporaryDirectory() as d:
            p = cr.WorkBuddyProvider(home=Path(d) / "missing",
                                     session=FakeSession())
            self.assertEqual(p.query()["status"], "off")


# ---------------------------------------------------------- Tracker --

class TestCreditsTracker(unittest.TestCase):
    def _isolated(self):
        """把 Trae/Qoder/WorkBuddy 的探测路径都指向临时目录，避免碰真实数据。"""
        env_dir = tempfile.mkdtemp(prefix="hp_env_")
        wb_dir = tempfile.mkdtemp(prefix="hp_wb_")
        return (mock.patch.dict(os.environ, {"APPDATA": env_dir}),
                mock.patch.object(cr, "wb_home", lambda: Path(wb_dir)))

    def _tracker(self, conf=None):
        env_p, wb_p = self._isolated()
        with env_p, wb_p:
            return cr.CreditsTracker(Path(tempfile.mkdtemp(prefix="hp_cfg_")),
                                     conf or {})

    def test_provider_filtering(self):
        t = self._tracker({"providers": {"workbuddy": {"enabled": False},
                                         "qoder_cn": {"enabled": False}}})
        self.assertEqual([p.key for p in t.providers],
                         ["trae_cn", "traework_cn"])

    def test_work_round_and_cache_reload(self):
        env_p, wb_p = self._isolated()
        with env_p, wb_p:
            t = cr.CreditsTracker(Path(tempfile.mkdtemp(prefix="hp_cfg_")),
                                  {"poll_seconds": 300})
            self.assertEqual(len(t.providers), 4)
            t._work()
            events = t.drain()
            self.assertEqual(events[0][0], "credits_done")
            self.assertEqual(set(t.results), {"trae_cn", "traework_cn",
                                              "workbuddy", "qoder_cn"})
            # 全新实例应从 credits.json 读回同样的结果
            t2 = cr.CreditsTracker(t.dir, {"poll_seconds": 300})
            self.assertEqual(set(t2.results), {"trae_cn", "traework_cn",
                                               "workbuddy", "qoder_cn"})
            raw = json.loads((t.dir / "credits.json")
                             .read_text(encoding="utf-8"))
            self.assertIn("results", raw)

    def test_status_lines_states(self):
        t = self._tracker({"enabled": False})
        self.assertIn("关闭", t.status_lines()[0])
        t = self._tracker({})
        self.assertIn("暂无数据", t.status_lines()[0])
        t.results = {"trae_cn": {"status": "ok", "summary": "剩 812",
                                 "short": "剩 812"}}
        lines = t.status_lines()
        self.assertTrue(any("Trae 剩 812" in l for l in lines))

    def test_menu_rows_marks(self):
        t = self._tracker({})
        rows = {key: label for label, key in t.menu_rows()}
        self.assertIn("Trae CN", rows["trae_cn"])
        t.results = {"trae_cn": {"status": "ok", "summary": "剩 812"}}
        rows = {key: label for label, key in t.menu_rows()}
        self.assertIn("✓", rows["trae_cn"])

    def test_maybe_poll_throttle(self):
        env_p, wb_p = self._isolated()
        with env_p, wb_p:
            t = cr.CreditsTracker(Path(tempfile.mkdtemp(prefix="hp_cfg_")),
                                  {"poll_seconds": 300})
            self.assertTrue(t.maybe_poll(force=True))
            self.assertTrue(wait_for(lambda: t.drain()))
            self.assertFalse(t.maybe_poll())  # 未到间隔，节流

    def test_format_num(self):
        self.assertEqual(cr._fmt_num(812.0), "812")
        self.assertEqual(cr._fmt_num(811.6), "811.6")
        self.assertEqual(cr._fmt_num("x"), "x")


if __name__ == "__main__":
    unittest.main()
