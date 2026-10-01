"""音效系统：按压 / 松开 / 任务结束音（并入 DSH 挂件的音效玩法）。

- wav 用 stdlib winsound（SND_ASYNC，不阻塞 UI 线程）；
- mp3 走 winmm MCI（ctypes mciSendStringW，异步播放）；
- 两者都不引入第三方依赖；任何失败静默降级（记一次日志），绝不影响主循环；
- 非 Windows 平台全部静默（本项目的目标平台是 Windows 桌面）。
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

try:
    import winsound
except ImportError:                # 非 Windows
    winsound = None                # type: ignore[assignment]

PACKS: dict[str, dict] = {
    "duck": {"name": "小黄鸭", "press": "Ya1.mp3", "release": "Ya2.mp3"},
    "sound1": {"name": "音效1", "press": "D1.mp3", "release": "D2.mp3"},
}
TASK_DONE_FILE = "minecraft-exp-orb.wav"
DEFAULT_PACK = "duck"


class SoundPlayer:
    """音效播放器。enabled=False 或平台不支持时全部调用都是 no-op。"""

    def __init__(self, assets_dir: Path | str, pack: str = DEFAULT_PACK,
                 enabled: bool = True, log=None) -> None:
        self.assets_dir = Path(assets_dir)
        self.pack = pack if pack in PACKS else DEFAULT_PACK
        self.log = log or (lambda _m: None)
        self._mci = None
        self._mci_alias: dict[str, str] = {}
        self._failed = False
        self.enabled = bool(enabled) and os.name == "nt"

    # ------------------------------------------------------------ 设置

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on) and os.name == "nt"

    def set_pack(self, name: str) -> bool:
        if name in PACKS:
            self.pack = name
            return True
        return False

    def close(self) -> None:
        """退出时尽力关闭 MCI 设备（进程结束也会释放，这里只是更干净）。"""
        if self._mci is None:
            return
        for alias in self._mci_alias.values():
            try:
                self._mci(f"close {alias}", None, 0, None)
            except Exception:
                pass
        self._mci_alias.clear()

    # ------------------------------------------------------------ 播放

    def press(self) -> None:
        self._play(PACKS[self.pack]["press"])

    def release(self) -> None:
        self._play(PACKS[self.pack]["release"])

    def task_done(self) -> None:
        self._play(TASK_DONE_FILE)

    def _play(self, filename: str) -> None:
        if not self.enabled or self._failed:
            return
        path = self.assets_dir / filename
        if not path.exists():
            return
        try:
            if path.suffix.lower() == ".wav":
                self._play_wav(path)
            else:
                self._play_mci(path)
        except Exception as e:
            self._failed = True
            self.log(f"[sound] 播放失败，本次运行静音：{e!r}")

    def _play_wav(self, path: Path) -> None:
        if winsound is None:
            return
        winsound.PlaySound(str(path), winsound.SND_FILENAME
                           | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def _play_mci(self, path: Path) -> None:
        if sys.platform != "win32":
            return
        if self._mci is None:
            self._mci = ctypes.windll.winmm.mciSendStringW  # type: ignore[attr-defined]
        key = str(path)
        alias = self._mci_alias.get(key)
        if alias is None:
            alias = f"hpsnd{len(self._mci_alias)}"
            rc = self._mci(f'open "{key}" type mpegvideo alias {alias}', None, 0, None)
            if rc != 0:
                # 个别环境不带 type 反而可行；再试一次
                rc = self._mci(f'open "{key}" alias {alias}', None, 0, None)
            if rc != 0:
                raise OSError(f"mci open rc={rc}")
            self._mci_alias[key] = alias
        # 连点时重放：先停再从头播，避免堆积
        self._mci(f"stop {alias}", None, 0, None)
        if self._mci(f"seek {alias} to start", None, 0, None) != 0:
            pass                       # 部分设备不支持 seek，直接播即可
        rc = self._mci(f"play {alias}", None, 0, None)
        if rc != 0:
            raise OSError(f"mci play rc={rc}")
