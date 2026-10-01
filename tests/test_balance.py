"""小鲸鱼记账测试：凭据解析 / 观测算法 / 预警 / 轮询节流（全离线）。"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from habitpet.balance import BalanceTracker, format_money


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.exc:
            raise self.exc
        return FakeResp(self.payload)


def make_tracker(tmp: str, conf=None, session=None, creds=None) -> BalanceTracker:
    return BalanceTracker(
        Path(tmp), conf or {}, session=session,
        creds_path=Path(creds) if creds else Path(tmp) / "no-creds.yaml")


def wait_for(pred, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def at() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class TestKeyResolution(unittest.TestCase):
    def test_config_key_wins(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"api_key": "sk-conf"})
            self.assertEqual(t.key, "sk-conf")

    def test_env_key_used_when_config_empty(self):
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items()
                   if k != "DEEPSEEK_API_KEY"}
            env["DEEPSEEK_API_KEY"] = "sk-env"
            with mock.patch.dict(os.environ, env, clear=True):
                t = make_tracker(d)
            self.assertEqual(t.key, "sk-env")

    def test_dsh_credentials_parsed_readonly(self):
        with tempfile.TemporaryDirectory() as d:
            creds = Path(d) / ".credentials.yaml"
            creds.write_text(
                "# DSH 凭据\n"
                "OTHER_KEY: whatever\n"
                "DEEPSEEK_API_KEY: \"sk-dsh-123\"  # 注释\n",
                encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k != "DEEPSEEK_API_KEY"}
            with mock.patch.dict(os.environ, env, clear=True):
                t = make_tracker(d, creds=creds)
            self.assertEqual(t.key, "sk-dsh-123")

    def test_dsh_ref_style_value_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            creds = Path(d) / ".credentials.yaml"
            creds.write_text("DEEPSEEK_API_KEY: env:SOME_VAR\n",
                             encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k != "DEEPSEEK_API_KEY"}
            with mock.patch.dict(os.environ, env, clear=True):
                t = make_tracker(d, creds=creds)
            self.assertEqual(t.key, "")
            self.assertFalse(t.enabled())

    def test_disabled_config_skips_key_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"enabled": False, "api_key": "sk-x"})
            self.assertEqual(t.key, "")
            self.assertFalse(t.enabled())


class TestFetch(unittest.TestCase):
    def test_fetch_prefers_cny(self):
        payload = {"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "1.00"},
            {"currency": "CNY", "total_balance": "88.50"}]}
        with tempfile.TemporaryDirectory() as d:
            sess = FakeSession(payload)
            t = make_tracker(d, conf={"api_key": "k"}, session=sess)
            info = t.fetch()
        self.assertEqual(info["balance"], 88.5)
        self.assertEqual(info["currency"], "CNY")
        self.assertEqual(len(sess.calls), 1)
        self.assertTrue(sess.calls[0]["headers"]["Authorization"]
                        .startswith("Bearer "))

    def test_fetch_bad_payload_returns_none(self):
        for payload in ({}, {"balance_infos": []}, None,
                        {"balance_infos": [{"currency": "CNY",
                                            "total_balance": "abc"}]}):
            with tempfile.TemporaryDirectory() as d:
                t = make_tracker(d, conf={"api_key": "k"},
                                 session=FakeSession(payload))
                self.assertIsNone(t.fetch(), payload)

    def test_worker_reports_error_event(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"api_key": "k"},
                             session=FakeSession(exc=RuntimeError("net down")))
            t._work()
            events = t.drain()
        self.assertEqual(events[0][0], "balance_err")


class TestObservation(unittest.TestCase):
    def test_first_sample_anchors_day(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d)
            t.observe({"balance": 100.0, "currency": "CNY", "at": at()})
            snap = t.snapshot()
        self.assertEqual(snap["balance"], 100.0)
        self.assertEqual(snap["today_used"], 0.0)

    def test_decrease_accumulates_usage(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d)
            for v in (100.0, 97.5, 95.0):
                t.observe({"balance": v, "currency": "CNY", "at": at()})
            snap = t.snapshot()
        self.assertAlmostEqual(snap["today_used"], 5.0, places=5)
        self.assertAlmostEqual(snap["balance"], 95.0, places=5)

    def test_recharge_does_not_erase_usage(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d)
            for v in (100.0, 90.0, 190.0, 188.0):
                t.observe({"balance": v, "currency": "CNY", "at": at()})
            snap = t.snapshot()
        self.assertAlmostEqual(snap["today_used"], 12.0, places=5)
        self.assertAlmostEqual(snap["balance"], 188.0, places=5)

    def test_new_day_resets_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d)
            t.observe({"balance": 100.0, "currency": "CNY", "at": at()})
            t.observe({"balance": 90.0, "currency": "CNY", "at": at()})
            t.data["date"] = "2000-01-01"          # 伪造跨天
            t.observe({"balance": 85.0, "currency": "CNY", "at": at()})
            snap = t.snapshot()
        self.assertAlmostEqual(snap["today_used"], 0.0, places=5)
        self.assertAlmostEqual(snap["balance"], 85.0, places=5)

    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d)
            t.observe({"balance": 66.6, "currency": "CNY", "at": at()})
            t2 = make_tracker(d)
            snap = t2.snapshot()
        self.assertEqual(snap["balance"], 66.6)


class TestAlertAndView(unittest.TestCase):
    def test_low_alert_fires_once_per_day(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"api_key": "k", "low_alert": 50})
            t.observe({"balance": 40.0, "currency": "CNY", "at": at()})
            self.assertTrue(t.check_low())
            self.assertFalse(t.check_low())        # 同一天不重复报警

    def test_alert_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"api_key": "k", "low_alert": 50,
                                      "alert_enabled": False})
            t.observe({"balance": 40.0, "currency": "CNY", "at": at()})
            self.assertFalse(t.check_low())

    def test_status_and_menu_texts(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"api_key": "k"})
            self.assertIn("暂无数据", t.status_lines()[0])
            self.assertIn("暂无数据", t.menu_label())
            t.observe({"balance": 12.34, "currency": "CNY", "at": at()})
            self.assertIn("¥12.34", t.status_lines()[0])
            self.assertIn("¥12.34", t.menu_label())

    def test_disabled_feature_texts(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"enabled": False})
            self.assertIn("关闭", t.status_lines()[0])
            self.assertIn("已关闭", t.menu_label())

    def test_maybe_poll_thread_and_throttle(self):
        payload = {"balance_infos": [{"currency": "CNY",
                                      "total_balance": "7.00"}]}
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d, conf={"api_key": "k", "poll_seconds": 60},
                             session=FakeSession(payload))
            self.assertTrue(t.maybe_poll(force=True))
            self.assertTrue(wait_for(lambda: bool(t.drain())))
            # 刚轮询过：未到间隔的自动轮询应被节流
            self.assertFalse(t.maybe_poll())

    def test_maybe_poll_disabled_without_key(self):
        with tempfile.TemporaryDirectory() as d:
            t = make_tracker(d)                    # 无 key
            self.assertFalse(t.maybe_poll(force=True))


class TestFormatMoney(unittest.TestCase):
    def test_currency_symbols(self):
        self.assertEqual(format_money(3.5, "CNY"), "¥3.50")
        self.assertEqual(format_money(3.5, "USD"), "$3.50")
        self.assertEqual(format_money(3.5, "EUR"), "EUR 3.50")


if __name__ == "__main__":
    unittest.main()
