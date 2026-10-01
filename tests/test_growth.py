"""养成系统测试：成长阶段 / streak / 成就。"""
from __future__ import annotations

import datetime as dt
import unittest

from habitpet import growth
from habitpet.config import DEFAULT_MECHANICS
from habitpet.state import DayStats, PetState


def make_state(**overrides) -> PetState:
    return PetState({**DEFAULT_MECHANICS, **overrides})


def roll_to(st: PetState, today: dt.date, *, commits: int = 0,
            active_minutes: float = 0.0) -> None:
    """把 state.today 伪造成 yesterday 的数据，然后跨天触发结算。"""
    st.today = DayStats(today.isoformat(), commits=commits,
                        active_minutes=active_minutes)
    st.tick(1.0, dt.datetime.combine(today + dt.timedelta(days=1),
                                     dt.time(0, 0, 1)), False)


class TestStages(unittest.TestCase):
    def test_new_pet_is_calf(self):
        st = make_state()
        self.assertEqual(growth.stage_name(st), "幼鲸")
        self.assertAlmostEqual(growth.stage_scale(st), 0.82)
        self.assertEqual(growth.stage_index(growth.growth_points(st)), 0)

    def test_feed_crossing_threshold_evolves(self):
        st = make_state()
        st.feed(20)   # 20 commits → 鲸鱼娘
        self.assertEqual(st.stage_idx, 1)
        self.assertAlmostEqual(growth.stage_scale(st), 1.0)
        self.assertTrue(any(e["kind"] == "stage_up" and e["stage"] == "鲸鱼娘"
                            for e in st.events))

    def test_deep_whale_at_100(self):
        st = make_state()
        st.feed(100)
        self.assertEqual(growth.stage_name(st), "深海鲸鱼娘")
        self.assertAlmostEqual(growth.stage_scale(st), 1.14)

    def test_stage_never_regresses(self):
        st = make_state()
        st.feed(100)   # feed 内部已触发一次进化事件
        st.stage_idx = 2
        before = sum(1 for e in st.events if e["kind"] == "stage_up")
        growth.refresh_stage(st)   # 成长值没变，不应降级也不应重复事件
        self.assertEqual(st.stage_idx, 2)
        after = sum(1 for e in st.events if e["kind"] == "stage_up")
        self.assertEqual(before, after)


class TestStreak(unittest.TestCase):
    def test_active_day_increments(self):
        st = make_state()
        day = dt.date(2026, 9, 10)
        roll_to(st, day, commits=1)
        self.assertEqual(st.active_streak, 1)
        self.assertEqual(st.active_days_total, 1)
        roll_to(st, day + dt.timedelta(days=1), commits=1)
        self.assertEqual(st.active_streak, 2)

    def test_active_by_minutes(self):
        st = make_state()
        roll_to(st, dt.date(2026, 9, 10), active_minutes=31)
        self.assertEqual(st.active_streak, 1)

    def test_inactive_day_resets_without_event_below_3(self):
        st = make_state()
        roll_to(st, dt.date(2026, 9, 10), commits=1)
        roll_to(st, dt.date(2026, 9, 11))   # 没干活
        self.assertEqual(st.active_streak, 0)
        self.assertFalse(any(e["kind"] == "streak_broken" for e in st.events))

    def test_milestone_and_break(self):
        st = make_state()
        day = dt.date(2026, 9, 1)
        for i in range(3):
            roll_to(st, day + dt.timedelta(days=i), commits=1)
        self.assertEqual(st.active_streak, 3)
        self.assertTrue(any(e["kind"] == "streak_milestone" and e["days"] == 3
                            for e in st.events))
        self.assertGreaterEqual(st.best_streak, 3)
        roll_to(st, day + dt.timedelta(days=3))   # 断签
        self.assertEqual(st.active_streak, 0)
        self.assertTrue(any(e["kind"] == "streak_broken" and e["days"] == 3
                            for e in st.events))
        self.assertEqual(st.best_streak, 3)       # 最高纪录保留

    def test_gap_days_break_streak(self):
        st = make_state()
        day = dt.date(2026, 9, 1)
        for i in range(4):
            roll_to(st, day + dt.timedelta(days=i), commits=1)
        self.assertEqual(st.active_streak, 4)
        # 三天没开程序：today 还是 9-4 的数据，直接跳到 9-8 才触发结算
        st.today = DayStats("2026-09-04", commits=1)
        st.tick(1.0, dt.datetime(2026, 9, 8, 0, 0, 1), False)
        self.assertEqual(st.active_streak, 0)
        self.assertTrue(any(e["kind"] == "streak_broken" for e in st.events))


class TestAchievements(unittest.TestCase):
    def test_commit_milestones(self):
        st = make_state()
        st.feed(10)
        self.assertIn("first_feed", st.unlocked)
        self.assertIn("commits_10", st.unlocked)
        self.assertNotIn("commits_50", st.unlocked)
        self.assertTrue(any(e["kind"] == "achievement" for e in st.events))

    def test_night_owl(self):
        st = make_state()
        # 凌晨 1 点活跃 60 分钟
        now = dt.datetime(2026, 9, 13, 1, 0, 0)
        for _ in range(60):
            now += dt.timedelta(seconds=60)
            st.tick(60.0, now, True)
        got = growth.check_new(st)
        self.assertIn("night_owl", [a.id for a in got])

    def test_zenith(self):
        st = make_state()
        st.satiety = st.mood = st.health = 95.0
        got = growth.check_new(st)
        self.assertIn("zenith", [a.id for a in got])

    def test_comeback(self):
        st = make_state(sit_damage=200.0)
        now = dt.datetime(2026, 9, 13, 10, 0, 0)
        for _ in range(45):
            now += dt.timedelta(seconds=60)
            st.tick(60.0, now, True)
        now += dt.timedelta(days=3, seconds=2)
        st.tick(1.0, now, False)
        self.assertTrue(st.came_back_once)
        self.assertIn("comeback", st.unlocked)

    def test_early_bird_and_veteran_and_zen(self):
        st = make_state()
        st.early_today = True
        st.today.breaks = 5
        st.born_at = (dt.datetime.now() - dt.timedelta(days=101)
                      ).isoformat(timespec="seconds")
        got = growth.check_new(st)
        ids = [a.id for a in got]
        for aid in ("early_bird", "zen", "veteran"):
            self.assertIn(aid, ids)

    def test_no_double_unlock(self):
        st = make_state()
        st.feed(1)
        first = len(st.unlocked)
        st.feed(1)
        self.assertEqual(len(st.unlocked), first)   # 不重复解锁

    def test_achievement_status_text(self):
        st = make_state()
        st.feed(1)
        lines = growth.achievement_status(st)
        self.assertTrue(any("成就" in line for line in lines))
        self.assertTrue(any("初见鲸喜" in line for line in lines))

    def test_achievement_fold_line_when_many(self):
        """点亮超过 8 枚时折叠：只展示前 8 枚 + 一行「另有 N 枚」。"""
        st = make_state()
        for a in growth.ACHIEVEMENTS:
            st.unlocked[a.id] = "2026-01-01"
        n = len(growth.ACHIEVEMENTS)
        lines = growth.achievement_status(st)
        self.assertTrue(
            any(line == f"…另有 {n - 8} 枚已点亮" for line in lines))
        self.assertFalse(any(line.startswith("🔒") for line in lines))
        self.assertEqual(sum(1 for ln in lines if ln.startswith("✅")), 8)


if __name__ == "__main__":
    unittest.main()
