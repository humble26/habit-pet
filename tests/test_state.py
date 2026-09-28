"""状态机机制测试：喂食 / 久坐 / 休息 / 熬夜 / 离家出走 / 跨天 / 存档。"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from habitpet.config import DEFAULT_MECHANICS
from habitpet.state import PetState


def make_state(**overrides) -> PetState:
    m = {**DEFAULT_MECHANICS, **overrides}
    return PetState(m)


def advance(st: PetState, minutes: float, start: dt.datetime, active: bool,
            step_seconds: float = 60.0) -> dt.datetime:
    """按步长推进状态机，返回结束时刻。"""
    now = start
    steps = int(minutes * 60 / step_seconds)
    for _ in range(steps):
        now += dt.timedelta(seconds=step_seconds)
        st.tick(step_seconds, now, active)
    return now


class TestFeeding(unittest.TestCase):
    def test_feed_raises_satiety_and_counts(self):
        st = make_state()
        st.feed(1)
        self.assertAlmostEqual(st.satiety, 80 + 14, places=5)
        self.assertEqual(st.total_commits, 1)
        self.assertEqual(st.today.commits, 1)

    def test_feed_clamped_at_100(self):
        st = make_state()
        st.feed(99)
        self.assertEqual(st.satiety, 100.0)

    def test_satiety_decays_over_time(self):
        st = make_state()
        advance(st, 120, dt.datetime(2026, 9, 13, 10, 0, 0), active=False)
        self.assertAlmostEqual(st.satiety, 80 - 4 * 2, places=5)


class TestSitting(unittest.TestCase):
    def test_continuous_work_triggers_damage(self):
        st = make_state()
        end = advance(st, 45, dt.datetime(2026, 9, 13, 10, 0, 0), active=True)
        self.assertAlmostEqual(st.health, 90 - 8, places=5)
        self.assertEqual(st.today.sit_hits, 1)
        self.assertEqual(st.today.active_minutes, 45 * 60 / 60)

    def test_break_regens_health(self):
        st = make_state()
        now = dt.datetime(2026, 9, 13, 10, 0, 0)
        now = advance(st, 45, now, active=True)          # 掉一次血
        now = advance(st, 10, now, active=False)         # 休息
        self.assertAlmostEqual(st.health, 90 - 8 + 5, places=5)
        self.assertEqual(st.today.breaks, 1)
        self.assertEqual(st.continuous_work_min, 0.0)

    def test_late_night_drains_lifespan(self):
        st = make_state()
        advance(st, 60, dt.datetime(2026, 9, 13, 1, 0, 0), active=True)
        self.assertAlmostEqual(st.lifespan_max, 100 - 3, places=5)
        self.assertAlmostEqual(st.today.late_minutes, 60, places=5)

    def test_daytime_no_lifespan_drain(self):
        st = make_state()
        advance(st, 60, dt.datetime(2026, 9, 13, 14, 0, 0), active=True)
        self.assertEqual(st.lifespan_max, 100.0)


class TestRunaway(unittest.TestCase):
    def test_health_zero_runs_away_and_returns(self):
        st = make_state(sit_damage=200.0)
        start = dt.datetime(2026, 9, 13, 10, 0, 0)
        now = advance(st, 45, start, active=True)
        self.assertTrue(st.runaway_until)
        kinds = [e["kind"] for e in st.events]
        self.assertIn("runaway", kinds)
        # 出走期间不饥饿
        satiety = st.satiety
        now = advance(st, 60, now, active=True)
        self.assertEqual(st.satiety, satiety)
        # 三天后回来（回来后继续休息还会回血，所以 >= 60）
        now = advance(st, 3 * 24 * 60 + 2, now, active=False)
        self.assertIsNone(st.runaway_until)
        self.assertGreaterEqual(st.health, 60)
        kinds = [e["kind"] for e in st.events]
        self.assertIn("came_home", kinds)

    def test_expression_runaway(self):
        st = make_state(sit_damage=200.0)
        st.tick(45 * 60, dt.datetime(2026, 9, 13, 10, 0, 0), True)
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 10, 1, 0)), "runaway")


class TestExpressions(unittest.TestCase):
    def test_priority_order(self):
        st = make_state()
        st.satiety, st.mood, st.health = 10, 80, 80
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 14, 0)), "hungry")
        st.health = 10
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 14, 0)), "sick")
        st.health = 90
        st.satiety = 90
        st.mood = 10
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 14, 0)), "sad")
        st.mood = 90
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 14, 0)), "happy")
        self.assertEqual(st.expression(dt.datetime(2026, 9, 13, 2, 0)), "sleepy")


class TestDayRollAndSave(unittest.TestCase):
    def test_day_rollover_archives_history(self):
        st = make_state()
        st.today.date = "2026-09-12"
        st.today.commits = 5
        now = dt.datetime(2026, 9, 13, 0, 0, 1)
        st.tick(1.0, now, False)
        self.assertEqual(st.today.date, "2026-09-13")
        self.assertEqual(st.history["2026-09-12"]["commits"], 5)

    def test_save_load_roundtrip(self):
        st = make_state()
        st.feed(2)
        st.satiety = 55.5
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            st.save(path)
            st2 = PetState.load(path, DEFAULT_MECHANICS)
        self.assertEqual(st2.satiety, 55.5)
        self.assertEqual(st2.total_commits, 2)
        self.assertEqual(st2.today.commits, 2)

    def test_load_corrupt_file_returns_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("not json{", encoding="utf-8")
            st = PetState.load(path, DEFAULT_MECHANICS)
        self.assertEqual(st.satiety, 80.0)

    def test_load_wrong_typed_field_returns_fresh(self):
        # unlocked 存成了列表 → 数值区段整体回退，不能炸掉启动
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({"version": 1, "unlocked": ["a", "b"],
                                        "satiety": 33}), encoding="utf-8")
            st = PetState.load(path, DEFAULT_MECHANICS)
        self.assertEqual(st.satiety, 80.0)
        self.assertEqual(st.unlocked, {})

    def test_load_bad_dates_degrade_per_field(self):
        # 坏日期字段逐个回退，不丢整个存档
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({
                "version": 1, "satiety": 55, "total_commits": 7,
                "born_at": "not-a-date", "last_save": "garbage",
                "runaway_until": "!!!", "last_poll": None,
            }), encoding="utf-8")
            st = PetState.load(path, DEFAULT_MECHANICS)
        self.assertEqual(st.satiety, 55)          # 好数据保留
        self.assertEqual(st.total_commits, 7)
        self.assertIsNone(st.last_save)           # 坏日期安全回退
        self.assertIsNone(st.runaway_until)
        st.catch_up(dt.datetime.now())            # 不应抛异常

    def test_load_clamps_corrupt_stage_idx(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({"version": 1, "stage_idx": 99,
                                        "satiety": 50}), encoding="utf-8")
            st = PetState.load(path, DEFAULT_MECHANICS)
        self.assertEqual(st.stage_idx, 2)   # 钳制到最高阶段，防渲染越界

    def test_catch_up_applies_offline_hunger(self):
        st = make_state()
        st.last_save = (dt.datetime.now() - dt.timedelta(hours=2)
                        ).isoformat(timespec="seconds")
        st.catch_up(dt.datetime.now())
        self.assertAlmostEqual(st.satiety, 80 - 8, delta=0.1)

    def test_events_serializable(self):
        st = make_state()
        st.feed(1)
        ev = st.events[0]
        json.dumps(ev, ensure_ascii=False)   # 不应抛异常


if __name__ == "__main__":
    unittest.main()
