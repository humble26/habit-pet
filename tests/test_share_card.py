"""分享卡生成测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from habitpet import share_card
from habitpet.config import DEFAULT_MECHANICS
from habitpet.state import PetState


def make_state() -> PetState:
    st = PetState(DEFAULT_MECHANICS)
    st.feed(23)
    st.total_focus_count = 4
    st.today.focus_count = 2
    st.today.active_minutes = 300
    st.today.sit_hits = 3
    st.today.breaks = 4
    st.today.late_minutes = 40
    st.active_streak = 5
    st.best_streak = 12
    return st


@unittest.skipUnless(share_card.HAS_PIL, "需要 Pillow")
class TestShareCard(unittest.TestCase):
    def test_card_rendered(self):
        st = make_state()
        out = Path(tempfile.mkdtemp(prefix="habitpet_card_"))
        path = share_card.render_weekly_card(st, out)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 5000)
        from PIL import Image
        img = Image.open(path)
        img.verify()                     # 完整性校验
        self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))

    def test_no_pil_returns_none(self):
        st = make_state()
        out = Path(tempfile.mkdtemp(prefix="habitpet_card2_"))
        real = share_card.HAS_PIL
        try:
            share_card.HAS_PIL = False
            self.assertIsNone(share_card.render_weekly_card(st, out))
        finally:
            share_card.HAS_PIL = real


if __name__ == "__main__":
    unittest.main()
