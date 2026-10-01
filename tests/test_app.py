"""主程序线程模型回归测试（无头窗口）。

重点：吐槽工作线程崩溃后 _roast_running 必须能恢复，否则吐槽/周报永久锁死。
"""
from __future__ import annotations

import json
import tempfile
import datetime as dt
import time
import unittest
from pathlib import Path

from habitpet.app import HabitPetApp
from habitpet.pet_window import PetWindow


def make_app() -> tuple[HabitPetApp, PetWindow, Path]:
    cfgdir = Path(tempfile.mkdtemp(prefix="habitpet_app_"))
    (cfgdir / "config.json").write_text(json.dumps({
        "repos": [], "git_poll_seconds": 300, "llm": {"enabled": False},
        # 测试环境不发真实请求、不出声音
        "sound": {"enabled": False},
        "balance": {"enabled": False},
        "credits": {"enabled": False},
        "github": {"enabled": False},
    }, ensure_ascii=False), encoding="utf-8")
    w = PetWindow(callbacks={})
    w.withdraw()
    app = HabitPetApp(w, cfgdir)
    # 屏蔽"每日自动吐槽"：测试聚焦线程模型本身，不受墙钟时间影响
    app.state.last_roast_date = dt.date.today().isoformat()
    return app, w, cfgdir


def pump(w: PetWindow, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        w.update()
        time.sleep(0.02)


class TestRoastThreads(unittest.TestCase):
    def test_roast_survives_narrator_crash(self):
        app, w, _cfgdir = make_app()
        try:
            def boom(_data):
                raise RuntimeError("narrator exploded")

            app.narrator.daily_roast = boom
            app._roast_now()
            pump(w, 2.0)
            self.assertFalse(app._roast_running, "线程崩溃后互斥锁必须释放")
            self.assertTrue(list(app.reports_dir.glob("daily-*.md")),
                            "崩溃后应降级模板并照常写日报")
        finally:
            w.destroy()

    def test_roast_lock_prevents_double_run(self):
        app, w, _cfgdir = make_app()
        try:
            calls = []

            def slow(data):
                calls.append(1)
                time.sleep(0.5)
                return "ok", False

            app.narrator.daily_roast = slow
            app._roast_now()
            app._roast_now()          # 第二次应被互斥锁挡掉
            app._make_weekly()        # 周报也应被挡掉
            pump(w, 2.0)
            self.assertEqual(len(calls), 1)
            self.assertFalse(app._roast_running)
        finally:
            w.destroy()

    def test_weekly_report_via_queue(self):
        app, w, _cfgdir = make_app()
        try:
            app._make_weekly()
            pump(w, 2.0)
            self.assertFalse(app._roast_running)
            self.assertTrue(list(app.reports_dir.glob("weekly-*.md")))
        finally:
            w.destroy()


if __name__ == "__main__":
    unittest.main()
