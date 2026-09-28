"""命令行入口：python -m habitpet [--smoke] [--config-dir DIR]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="habitpet", description="现实数据喂养的桌面宠物")
    parser.add_argument("--smoke", action="store_true",
                        help="冒烟测试：启动 4 秒后自动退出并打印状态")
    parser.add_argument("--config-dir", default=None,
                        help="配置/存档目录（默认 ~/.habitpet）")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk  # noqa: F401  (确保 tk 环境可用)

    from .app import HabitPetApp
    from .config import default_config_dir
    from .pet_window import PetWindow

    cfg_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    window = PetWindow(callbacks={})   # 回调由 HabitPetApp 注入
    app = HabitPetApp(window, cfg_dir, smoke=args.smoke)
    try:
        window.mainloop()
    except KeyboardInterrupt:
        pass
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
