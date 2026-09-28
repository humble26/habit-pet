"""台词系统测试：模板兜底必须永不哑火，LLM 不可用时自动降级。"""
from __future__ import annotations

import unittest

from habitpet.narrator import Narrator

NO_LLM = {"enabled": True, "api_key": "", "model": "glm-4-flash",
          "base_url": "http://127.0.0.1:1/nothing"}   # 不存在的地址


class TestTemplates(unittest.TestCase):
    def test_all_event_kinds_render(self):
        n = Narrator(NO_LLM)
        kinds = {
            "feed": {"n": 3}, "treat": {}, "sit_hit": {"mins": 45, "dmg": 8},
            "rested": {"mins": 10, "regen": 5}, "hungry": {"s": 12.0},
            "late_night": {"h": 1}, "runaway": {"days": 3, "reason": "x"},
            "came_home": {}, "pet": {}, "welcome_back": {"h": 5.0},
        }
        for kind, kw in kinds.items():
            text = n.line(kind, **kw)
            self.assertTrue(text and text != "……", f"{kind} 台词为空")

    def test_placeholder_substituted(self):
        n = Narrator(NO_LLM)
        text = n.line("feed", n=7)
        self.assertIn("7", text)


class TestRoastFallback(unittest.TestCase):
    def test_roast_falls_back_without_key(self):
        n = Narrator(NO_LLM)
        data = {"commits": 4, "active_hours": 6.5, "sit_hits": 2, "breaks": 3,
                "late_minutes": 0, "satiety": 60, "mood": 70, "health": 90,
                "lifespan_max": 100}
        text, is_llm = n.daily_roast(data)
        self.assertFalse(is_llm)
        self.assertTrue(text)

    def test_roast_nocommit_variant(self):
        n = Narrator(NO_LLM)
        data = {"commits": 0, "active_hours": 1.0, "sit_hits": 0, "breaks": 0,
                "late_minutes": 0, "satiety": 30, "mood": 50, "health": 90,
                "lifespan_max": 100}
        text, is_llm = n.daily_roast(data)
        self.assertFalse(is_llm)
        self.assertTrue(("0" in text or "零" in text))

    def test_llm_failure_degrades(self):
        # 指向不存在的本地端口，请求必然失败 → 应回退模板而不是抛异常
        n = Narrator({**NO_LLM, "api_key": "fake-key"})
        data = {"commits": 1, "active_hours": 2.0, "sit_hits": 1, "breaks": 1,
                "late_minutes": 0, "satiety": 80, "mood": 60, "health": 90,
                "lifespan_max": 100}
        text, is_llm = n.daily_roast(data)
        self.assertFalse(is_llm)
        self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
