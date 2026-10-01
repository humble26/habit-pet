"""音效系统测试：不真实出声，只覆盖选包 / 开关 / 降级路径。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from habitpet.sound import PACKS, SoundPlayer
from habitpet.whale_art import ASSETS_DIR


class TestSoundPlayer(unittest.TestCase):
    def test_builtin_assets_present(self):
        for spec in PACKS.values():
            for f in (spec["press"], spec["release"]):
                self.assertTrue((ASSETS_DIR / f).exists(), f)
        self.assertTrue((ASSETS_DIR / "minecraft-exp-orb.wav").exists())

    def test_pack_select_and_invalid(self):
        p = SoundPlayer(ASSETS_DIR, pack="duck", enabled=True)
        self.assertEqual(p.pack, "duck")
        self.assertTrue(p.set_pack("sound1"))
        self.assertEqual(p.pack, "sound1")
        self.assertFalse(p.set_pack("nope"))
        self.assertEqual(p.pack, "sound1")

    def test_disabled_is_noop(self):
        p = SoundPlayer(ASSETS_DIR, enabled=False)
        p.press(); p.release(); p.task_done()      # 不应抛异常
        if os.name == "nt":
            p.set_enabled(True)
            self.assertTrue(p.enabled)

    def test_press_release_map_to_pack_files(self):
        p = SoundPlayer(ASSETS_DIR, pack="duck", enabled=True)
        with mock.patch.object(p, "_play_wav"), \
                mock.patch.object(p, "_play_mci") as m:
            p.press()
            self.assertTrue(str(m.call_args[0][0]).endswith("Ya1.mp3"))
            m.reset_mock()
            p.release()
            self.assertTrue(str(m.call_args[0][0]).endswith("Ya2.mp3"))
            p.set_pack("sound1")
            m.reset_mock()
            p.press()
            self.assertTrue(str(m.call_args[0][0]).endswith("D1.mp3"))
            m.reset_mock()
            p.release()
            self.assertTrue(str(m.call_args[0][0]).endswith("D2.mp3"))

    def test_task_done_uses_wav(self):
        p = SoundPlayer(ASSETS_DIR, enabled=True)
        with mock.patch.object(p, "_play_wav") as w, \
                mock.patch.object(p, "_play_mci") as m:
            p.task_done()
            w.assert_called_once()
            self.assertTrue(str(w.call_args[0][0])
                            .endswith("minecraft-exp-orb.wav"))
            m.assert_not_called()

    def test_missing_files_silent(self):
        with tempfile.TemporaryDirectory() as d:
            p = SoundPlayer(d, enabled=True)
            with mock.patch.object(p, "_play_wav") as w, \
                    mock.patch.object(p, "_play_mci") as m:
                p.press()
                w.assert_not_called()
                m.assert_not_called()

    def test_play_failure_degrades_to_silence(self):
        p = SoundPlayer(ASSETS_DIR, enabled=True)
        with mock.patch.object(p, "_play_mci", side_effect=OSError("boom")):
            p.press()                              # 首次失败 → 整体静音
            self.assertTrue(p._failed)
            p.release()                            # 之后不再尝试（无异常）


if __name__ == "__main__":
    unittest.main()
