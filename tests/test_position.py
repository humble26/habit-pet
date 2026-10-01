"""位置限制：按形象像素范围夹取——形象能精确贴屏幕边，但不会滑出屏幕。

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

from habitpet.pet_window import (ART_BOX_FALLBACK, TASKBAR_MARGIN,
                                 PetWindow)


def _pos(w) -> tuple[int, int]:
    """读取请求的窗口位置（"+x+y"），withdrawn 状态下也可靠。"""
    _size, x, y = w.geometry().split("+")
    return int(x), int(y)


LEFT, _TOP, RIGHT, BOTTOM = ART_BOX_FALLBACK


class TestPositionClamp(unittest.TestCase):
    def _win(self) -> PetWindow:
        w = PetWindow(callbacks={})
        w.withdraw()
        return w

    def test_clamp_pos_bounds(self):
        w = self._win()
        try:
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            # 形象可贴满屏幕边（窗口允许按透明边距出屏）
            self.assertEqual(w._clamp_pos(-999, -999), (-LEFT, 0))
            self.assertEqual(
                w._clamp_pos(sw + 999, sh + 999),
                (sw - RIGHT, sh - TASKBAR_MARGIN - BOTTOM))
            self.assertEqual(w._clamp_pos(10, 20), (10, 20))
        finally:
            w.destroy()

    def test_clamp_scales_with_art_box(self):
        """成长变大（形象包围盒变宽）后，右边界随之收窄。"""
        w = self._win()
        try:
            sw, _sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w._art_box_rel = (30, 100, 250, 374)     # 更大的形象
            self.assertEqual(w._clamp_pos(sw + 999, 0), (sw - 250, 0))
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
            # 往右下猛拖：形象贴住右下（透明边距可出屏，形象不出去）
            w._on_motion(types.SimpleNamespace(x_root=sw * 3, y_root=sh * 3))
            self.assertEqual(
                _pos(w), (sw - RIGHT, sh - TASKBAR_MARGIN - BOTTOM))
            # 再往左上猛拖：形象贴住左上
            w._drag_from = (sw * 3, sh * 3, *_pos(w))
            w._on_motion(types.SimpleNamespace(x_root=-9999, y_root=-9999))
            self.assertEqual(_pos(w), (-LEFT, 0))
        finally:
            w.destroy()

    def test_snap_off_still_keeps_on_screen(self):
        w = self._win()
        try:
            w.snap_enabled = False
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            # 固定 _snap_to_edge 的输入=越界位置，验证关吸附也会夹回
            with mock.patch.object(PetWindow, "winfo_x", return_value=sw - 20), \
                 mock.patch.object(PetWindow, "winfo_y", return_value=sh - 20):
                w._snap_to_edge()
            self.assertEqual(
                _pos(w), (sw - RIGHT, sh - TASKBAR_MARGIN - BOTTOM))
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
                (w.winfo_screenwidth() - RIGHT,
                 w.winfo_screenheight() - TASKBAR_MARGIN - BOTTOM))
        finally:
            w.destroy()

    def test_saved_flush_positions_survive_restore(self):
        """存档里的贴边位置在恢复时不被多挪（估算盒比实测盒略窄）。"""
        w = self._win()
        try:
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w.place_at(sw - 247, 60)              # stage1 形象贴右缘时的窗口位
            self.assertEqual(_pos(w), (sw - 247, 60))
            w.place_at(-46, 60)                   # 贴左缘
            self.assertEqual(_pos(w), (-46, 60))
        finally:
            w.destroy()

    def test_art_box_growth_nudges_back(self):
        """形象变大导致越界时，_nudge_into_art_bounds 把窗口轻挪回界内。"""
        w = self._win()
        try:
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w._art_box_rel = (30, 100, 250, 374)
            with mock.patch.object(PetWindow, "winfo_x", return_value=sw - 20), \
                 mock.patch.object(PetWindow, "winfo_y", return_value=sh - 60):
                w._nudge_into_art_bounds()
            self.assertEqual(
                _pos(w), (sw - 250, sh - TASKBAR_MARGIN - 374))
        finally:
            w.destroy()


if __name__ == "__main__":
    unittest.main()
