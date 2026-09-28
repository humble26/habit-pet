"""开机自启测试：写入临时注册表键，不碰真实启动项。"""
from __future__ import annotations

import os
import unittest

from habitpet import autostart

TEST_KEY = r"Software\HabitPetTestKey"
TEST_VALUE = "HabitPetTest"


@unittest.skipUnless(os.name == "nt", "Windows only")
class TestAutostart(unittest.TestCase):
    def tearDown(self):
        autostart.disable(TEST_KEY, TEST_VALUE)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteKey(k, "HabitPetTestKey")
        except OSError:
            pass

    def test_toggle_roundtrip(self):
        self.assertFalse(autostart.is_enabled(TEST_KEY, TEST_VALUE))
        self.assertTrue(autostart.toggle(TEST_KEY, TEST_VALUE))
        self.assertTrue(autostart.is_enabled(TEST_KEY, TEST_VALUE))
        self.assertFalse(autostart.toggle(TEST_KEY, TEST_VALUE))
        self.assertFalse(autostart.is_enabled(TEST_KEY, TEST_VALUE))

    def test_command_points_to_launch_pyw(self):
        autostart.enable(TEST_KEY, TEST_VALUE)
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as k:
            cmd, _ = winreg.QueryValueEx(k, TEST_VALUE)
        self.assertIn("pythonw.exe", cmd)
        self.assertIn("launch.pyw", cmd)


if __name__ == "__main__":
    unittest.main()
