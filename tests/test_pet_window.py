"""桌宠窗口本轮优化回归：增量渲染 / 独立圆角气泡卡 / 菜单结构。

- 增量渲染：内容签名不变时只平移画布元素（bob 抖动不再全量重画）
- 气泡卡：独立 Toplevel，按内容换行、贴屏幕边不再被裁、尾巴指向锚点
- 菜单：开关类都收进「设置」子菜单，动态标签写进正确的菜单对象
"""
from __future__ import annotations

import unittest

from habitpet.pet_window import CARD_MAX_W, PetWindow


class TestRenderFastPath(unittest.TestCase):
    def _win(self) -> PetWindow:
        w = PetWindow(callbacks={})
        w.withdraw()
        return w

    def test_idle_frames_reuse_canvas_items(self):
        """空闲表情：bob 抖动只平移，不重画（元素 id 不变）。"""
        w = self._win()
        try:
            w.render("idle", 1)
            self.assertIn("img", w._items)
            ids = w.canvas.find_all()
            self.assertTrue(ids)
            w.render("idle", 5)              # bob 变了，内容没变
            self.assertEqual(w.canvas.find_all(), ids)
        finally:
            w.destroy()

    def test_expression_change_redraws(self):
        """表情变化必须走重画（chip 等元素跟着更新）。"""
        w = self._win()
        try:
            w.render("idle", 1)
            ids = w.canvas.find_all()
            w.render("hungry", 2)
            self.assertNotEqual(w.canvas.find_all(), ids)
            self.assertIn("chip", w._items)  # 小纸条元素已登记
        finally:
            w.destroy()

    def test_sleepy_is_dynamic(self):
        """sleepy 有逐帧动画（Z 漂移），不走快速平移路径。"""
        w = self._win()
        try:
            w.render("sleepy", 1)
            ids = w.canvas.find_all()
            w.render("sleepy", 2)
            self.assertNotEqual(w.canvas.find_all(), ids)
        finally:
            w.destroy()


class TestBubbleCard(unittest.TestCase):
    def _win(self) -> PetWindow:
        w = PetWindow(callbacks={})
        w.withdraw()
        return w

    def test_show_bubble_creates_card_and_times_out(self):
        w = self._win()
        try:
            w.show_bubble("测试气泡", secs=3)
            self.assertIsNotNone(w._card)
            self.assertTrue(w._bubble_visible)
            w._bubble_timeout()
            self.assertFalse(w._bubble_visible)
            self.assertIsNone(w._bubble_job)
        finally:
            w.destroy()

    def test_long_text_wraps_within_max_width(self):
        w = self._win()
        try:
            w._show_bubble_text("很长的消息" * 40)
            card = w._card
            self.assertIsNotNone(card)
            card.update_idletasks()
            self.assertLessEqual(card._cw, CARD_MAX_W)
            self.assertGreater(card._ch, card.line_h)     # 不止一行
        finally:
            w.destroy()

    def test_wrap_prefers_spaces(self):
        """带空格的数字串不从中劈开（防止「¥0 / .00」这类难看断行）。"""
        w = self._win()
        try:
            w._show_bubble_text("x")
            card = w._card
            lines = card._wrap("词" * 200 + " ¥12.34")
            self.assertTrue(any(ln.endswith("¥12.34") for ln in lines))
            self.assertFalse(any(ln.endswith("¥12.") for ln in lines))
        finally:
            w.destroy()

    def test_card_position_clamped_on_screen(self):
        w = self._win()
        try:
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w._show_bubble_text("边缘测试")
            card = w._card
            card.show_text("边缘测试", -500, 600, 900, sw, sh)
            _size, x, y = card.geometry().split("+")
            self.assertGreaterEqual(int(x), 6)
            self.assertGreaterEqual(int(y), 6)
            card.show_text("边缘测试", sw + 500, 600, 900, sw, sh)
            _size, x, y = card.geometry().split("+")
            self.assertLessEqual(int(x) + card._cw, sw - 6)
        finally:
            w.destroy()

    def test_below_when_no_room_above(self):
        """贴屏幕顶停车：头顶放不下 → 卡片改放脚下、尾巴朝上，不挡脸。"""
        w = self._win()
        try:
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w._show_bubble_text("试一试")
            card = w._card
            card.show_text("试一试", 300, 4, 374, sw, sh)   # 头顶只剩 4px
            self.assertTrue(card._below)
            _size, x, y = card.geometry().split("+")
            self.assertEqual(int(y), 374 + 6)               # 紧贴脚底
            card.show_text("试一试", 300, 500, 800, sw, sh)  # 头顶有位置
            self.assertFalse(card._below)
            _size, x, y = card.geometry().split("+")
            self.assertEqual(int(y), 500 - 6 - card._ch)
        finally:
            w.destroy()

    def test_move_to_keeps_size(self):
        w = self._win()
        try:
            w._show_bubble_text("平移测试")
            card = w._card
            before = card.geometry().split("+")[0]
            card.move_to(400, 400, 800, w.winfo_screenwidth(),
                         w.winfo_screenheight())
            after = card.geometry().split("+")[0]
            self.assertEqual(before, after)               # 尺寸不变，只挪位置
        finally:
            w.destroy()


class TestMenuStructure(unittest.TestCase):
    def _win(self, callbacks=None) -> PetWindow:
        w = PetWindow(callbacks=callbacks or {})
        w.withdraw()
        return w

    @staticmethod
    def _labels(menu) -> list[str]:
        out = []
        for i in range(menu.index("end") + 1):
            if menu.type(i) == "separator":
                continue
            out.append(menu.entrycget(i, "label"))
        return out

    def _main_labels(self, w: PetWindow) -> list[str]:
        return self._labels(w.menu)

    def _sub_labels(self, w: PetWindow) -> list[str]:
        return self._labels(w._settings_sub)

    def test_settings_cascade_hides_toggles(self):
        w = self._win()
        try:
            main = self._main_labels(w)
            self.assertIn("设置", main)
            self.assertIn("退出", main)
            # 开关类不再散在主菜单里
            for label in main:
                self.assertFalse(label.startswith("余额预警"))
                self.assertFalse(label.startswith("贴边吸附"))
                self.assertFalse(label.startswith("开机自启"))
            sub = self._sub_labels(w)
            self.assertIn("余额预警：开", sub)
            self.assertIn("贴边吸附：开", sub)
            self.assertIn("音效", sub)
            self.assertIn("开机自启", sub)
        finally:
            w.destroy()

    def test_refresh_menu_updates_right_menus(self):
        """动态标签写进正确的菜单（设置项在子菜单、其余在主菜单）。"""
        calls = {
            "focus_state": lambda: "结束专注（剩 12 分钟）",
            "balance_label": lambda: "余额 12.3 元",
            "github_label": lambda: "GitHub @humble26",
            "github_backfill_label": lambda: "补喂云端历史推送 ✓",
            "alert_state": lambda: False,
            "snap_state": lambda: False,
            "autostart_state": lambda: True,
            "sound_pack": lambda: "off",
        }
        w = self._win(calls)
        try:
            w._refresh_menu()
            self.assertEqual(w.menu.entrycget(w._focus_idx, "label"),
                             "结束专注（剩 12 分钟）")
            self.assertEqual(w.menu.entrycget(w._balance_idx, "label"),
                             "余额 12.3 元")
            self.assertEqual(w.menu.entrycget(w._github_idx, "label"),
                             "GitHub @humble26")
            self.assertEqual(w.menu.entrycget(w._gh_backfill_idx, "label"),
                             "补喂云端历史推送 ✓")
            self.assertEqual(
                w._settings_sub.entrycget(w._alert_idx, "label"),
                "余额预警：关")
            self.assertEqual(
                w._settings_sub.entrycget(w._snap_idx, "label"),
                "贴边吸附：关")
            if w._autostart_idx is not None:
                self.assertEqual(
                    w._settings_sub.entrycget(w._autostart_idx, "label"),
                    "开机自启：开")
            self.assertEqual(w._sound_var.get(), "off")
        finally:
            w.destroy()


if __name__ == "__main__":
    unittest.main()
