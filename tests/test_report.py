"""日报/周报生成测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from habitpet.narrator import Narrator
from habitpet.report import week_summary, write_daily, write_weekly
from habitpet.state import PetState
from habitpet.config import DEFAULT_MECHANICS


class TestReports(unittest.TestCase):
    def setUp(self):
        self.st = PetState(DEFAULT_MECHANICS)
        self.st.feed(3)
        self.st.today.active_minutes = 240
        self.st.today.sit_hits = 2
        self.st.today.breaks = 3
        self.st.today.late_minutes = 90
        self.tmp = tempfile.mkdtemp(prefix="habitpet_report_")

    def test_daily_report_written(self):
        path = write_daily(self.st, "今天摸鱼太多，本喵扣你粮。", False, Path(self.tmp))
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("日报", text)
        self.assertIn("3", text)
        self.assertIn("本喵", text)

    def test_week_summary_includes_today(self):
        s = week_summary(self.st)
        self.assertEqual(s["total"]["commits"], 3)
        self.assertEqual(len(s["days"]), 7)

    def test_weekly_report_written(self):
        n = Narrator({"enabled": True, "api_key": "", "model": "x", "base_url": ""})
        path = write_weekly(self.st, Path(self.tmp), n)
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("周报", text)
        self.assertIn("commit", text)


if __name__ == "__main__":
    unittest.main()
