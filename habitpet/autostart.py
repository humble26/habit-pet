"""开机自启（Windows 注册表 HKCU Run）。

键路径/值名可注入，测试时写临时键，不碰真实启动项。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if os.name == "nt":
    import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "HabitPet"


def _command() -> str:
    launch_pyw = Path(__file__).resolve().parent.parent / "launch.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{launch_pyw}"'


def is_enabled(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> bool:
    if os.name != "nt":
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.QueryValueEx(k, value_name)
            return True
    except OSError:
        return False


def enable(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> None:
    if os.name != "nt":
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, _command())


def disable(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> None:
    if os.name != "nt":
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, value_name)
    except FileNotFoundError:
        pass


def toggle(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> bool:
    if is_enabled(key_path, value_name):
        disable(key_path, value_name)
        return False
    enable(key_path, value_name)
    return True
