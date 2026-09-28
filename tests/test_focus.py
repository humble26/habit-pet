"""专注模式测试：计时 / 暂停恢复 / 完成奖励 / 豁免久坐 / 取消 / 成就。"""
from __future__ import annotations

import datetime as dt
import unittest

from habitpet.config import DEFAULT_MECHANICS
from habitpet.state import PetState


def make_state(**overrides) -> PetState:
    return PetState({**DEFAULT_MECHANICS, **overrides})


def tick_minutes(st: PetState, minutes: float, start: dt.datetime, active: bool,
                 step: float = 60.0) -> dt.datetime:
    now = start
    for _ in range(int(minutes * 60 / step)):
        now += dt.timedelta(seconds=step)
        st.tick(step, now, active)
    return now


class TestFocus(unittest.TestCase):
    def test_start_and_expression(self):
        st = make_state()
        st.mood = 90.0
        st.start_focus(25)
        self.assertIsNotNone(st.focus)
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 14, 0)), "focus")
        self.assertTrue(any(e["kind"] == "focus_start" for e in st.events))

    def test_start_refused_when_runaway(self):
        st = make_state()
        st.runaway_until = "2026-09-20T00:00:00"
        st.start_focus()
        self.assertIsNone(st.focus)

    def test_completes_and_rewards(self):
        st = make_state(focus_minutes=1)
        st.start_focus(1)
        tick_minutes(st, 1.1, dt.datetime(2026, 9, 13, 10, 0, 0), active=True)
        self.assertIsNone(st.focus)
        self.assertEqual(st.total_focus_count, 1)
        self.assertEqual(st.today.focus_count, 1)
        self.assertAlmostEqual(st.mood, 70 + 10, delta=0.5)
        kinds = [e["kind"] for e in st.events]
        self.assertIn("focus_done", kinds)
        self.assertIn("focus_1", st.unlocked)   # 成就自动解锁

    def test_sit_damage_suspended_during_focus(self):
        st = make_state()
        st.start_focus(60)
        tick_minutes(st, 90, dt.datetime(2026, 9, 13, 10, 0, 0), active=True)
        self.assertEqual(st.today.sit_hits, 0)   # 专注不算久坐
        self.assertEqual(st.health, 90.0)

    def test_pause_on_leave_and_resume(self):
        st = make_state(focus_grace_minutes=5)
        st.start_focus(25)
        now = tick_minutes(st, 5, dt.datetime(2026, 9, 13, 10, 0, 0), active=True)
        now = tick_minutes(st, 6, now, active=False)   # 离开 6 分钟
        self.assertTrue(st.focus.paused)
        self.assertTrue(any(e["kind"] == "focus_paused" for e in st.events))
        elapsed_before = st.focus.elapsed
        now = tick_minutes(st, 2, now, active=False)   # 继续离开：不再累计
        self.assertEqual(st.focus.elapsed, elapsed_before)
        now = tick_minutes(st, 1, now, active=True)    # 回来
        self.assertFalse(st.focus.paused)
        self.assertTrue(any(e["kind"] == "focus_resumed" for e in st.events))
        self.assertGreater(st.focus.elapsed, elapsed_before)

    def test_cancel(self):
        st = make_state()
        st.start_focus()
        st.cancel_focus()
        self.assertIsNone(st.focus)
        self.assertTrue(any(e["kind"] == "focus_cancel" for e in st.events))

    def test_double_start_refused(self):
        st = make_state()
        st.start_focus()
        st.start_focus()
        kinds = [e["kind"] for e in st.events]
        self.assertEqual(kinds.count("focus_start"), 1)

    def test_status_line_shows_focus(self):
        st = make_state()
        st.start_focus(25)
        lines = " ".join(st.status_lines(dt.datetime(2026, 9, 13, 14, 0)))
        self.assertIn("专注中", lines)
        self.assertIn("25", lines)

    def test_save_load_keeps_focus_total(self):
        import tempfile
        from pathlib import Path
        st = make_state()
        st.start_focus(1)
        tick_minutes(st, 1.1, dt.datetime(2026, 9, 13, 10, 0, 0), active=True)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            st.save(path)
            st2 = PetState.load(path, DEFAULT_MECHANICS)
        self.assertEqual(st2.total_focus_count, 1)
        self.assertIsNone(st2.focus)   # 会话本身不持久化


if __name__ == "__main__":
    unittest.main()
