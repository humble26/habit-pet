"""位置限制：拖拽 / 松手 / 存档恢复都不能让鲸鱼娘离开屏幕。

注意：withdrawn 窗口的 winfo_x/y 会滞后（只在 map 时刷新），
所以断言「已应用的位置」统一用 geometry() 查询字符串读取。
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from habitpet.pet_window import TASKBAR_MARGIN, H, W, PetWindow


def _pos(w) -> tuple[int, int]:
    """读取请求的窗口位置（"+x+y"），withdrawn 状态下也可靠。"""
    _size, x, y = w.geometry().split("+")
    return int(x), int(y)


class TestPositionClamp(unittest.TestCase):
    def _win(self) -> PetWindow:
        w = PetWindow(callbacks={})
        w.withdraw()
        return w

    def test_clamp_pos_bounds(self):
        w = self._win()
        try:
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            self.assertEqual(w._clamp_pos(-999, -999), (0, 0))
            self.assertEqual(
                w._clamp_pos(sw + 999, sh + 999),
                (max(0, sw - W), max(0, sh - H - TASKBAR_MARGIN)))
            self.assertEqual(w._clamp_pos(120, 240), (120, 240))
        finally:
            w.destroy()

    def test_drag_cannot_leave_screen(self):
        w = self._win()
        try:
            w.place_at(300, 300)
            w.update_idletasks()
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            base = _pos(w)
            w._moved = True                       # 跳过按压动画
            w._drag_from = (0, 0, base[0], base[1])
            # 往右下猛拖：停在右下边界（不会出去）
            w._on_motion(types.SimpleNamespace(x_root=sw * 3, y_root=sh * 3))
            self.assertEqual(
                _pos(w), (max(0, sw - W), max(0, sh - H - TASKBAR_MARGIN)))
            # 再往左上猛拖：停在左上角
            w._drag_from = (sw * 3, sh * 3, *_pos(w))
            w._on_motion(types.SimpleNamespace(x_root=-9999, y_root=-9999))
            self.assertEqual(_pos(w), (0, 0))
        finally:
            w.destroy()

    def test_snap_off_still_keeps_on_screen(self):
        w = self._win()
        try:
            w.snap_enabled = False
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            # 固定 _snap_to_edge 的输入=越界位置，验证即使关掉吸附也会夹回屏幕
            with mock.patch.object(PetWindow, "winfo_x", return_value=sw - 64), \
                 mock.patch.object(PetWindow, "winfo_y", return_value=sh - 64):
                w._snap_to_edge()
            x, y = _pos(w)
            self.assertLessEqual(x + W, sw)
            self.assertLessEqual(y + H, sh)
            self.assertFalse(w.mirrored)
        finally:
            w.destroy()

    def test_restore_offscreen_saved_pos_clamped(self):
        from habitpet.app import HabitPetApp
        cfgdir = Path(tempfile.mkdtemp(prefix="habitpet_pos_"))
        (cfgdir / "config.json").write_text(json.dumps({
            "repos": [], "llm": {"enabled": False},
            "sound": {"enabled": False}, "balance": {"enabled": False},
            "credits": {"enabled": False},
            "github": {"enabled": False},
        }, ensure_ascii=False), encoding="utf-8")
        w = PetWindow(callbacks={})
        w.withdraw()
        try:
            app = HabitPetApp(w, cfgdir)
            app.state.window_x = 999999
            app.state.window_y = 999999
            app._restore_window_pos()
            self.assertEqual(
                _pos(w),
                (max(0, w.winfo_screenwidth() - W),
                 max(0, w.winfo_screenheight() - H - TASKBAR_MARGIN)))
        finally:
            w.destroy()


if __name__ == "__main__":
    unittest.main()
