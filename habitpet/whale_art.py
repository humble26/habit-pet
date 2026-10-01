"""鲸鱼娘形象渲染：PNG 加载 / 阶段缩放 / 镜像 / 状态调色 / 自定义角色。

- 只依赖 Pillow 做像素处理；PhotoImage 由 UI 层创建（本模块不 import tkinter，
  便于单测覆盖缩放、镜像、调色逻辑）。
- 管线：内容 bbox 裁剪 → 缩放 → 状态调色 → 镜像 → alpha 阈值 → 合成到透明色键背景。
- 透明色键取接近线稿的深蓝：半透明边缘合成后不显脏边（tk 的 -transparentcolor
  只支持色键，不支持逐像素 alpha）。
"""
from __future__ import annotations

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
DEFAULT_PET = "DSniang1.png"

KEY_RGB = (26, 39, 73)          # 透明色键 #1a2749（深蓝，接近线稿色）
KEY_HEX = "#1a2749"
ALPHA_CUTOFF = 30               # alpha 低于该值直接判背景（消掉浅色残影）
_CACHE_CAP = 60

TONES = ("sick", "sad", "away")

try:
    from PIL import Image, ImageEnhance
    HAS_PIL = True
except ImportError:                # pragma: no cover
    HAS_PIL = False


class WhaleSprite:
    """鲸鱼娘（或自定义角色图）渲染器，所有像素处理都是惰性 + 缓存。"""

    def __init__(self, assets_dir: Path | None = None,
                 image_path: str | Path | None = None, log=None) -> None:
        self.dir = Path(assets_dir) if assets_dir else ASSETS_DIR
        self.log = log or (lambda _m: None)
        self.path = self._pick_image(image_path)
        self._base = None
        self._cache: dict[tuple, "Image.Image"] = {}

    def _pick_image(self, override):
        if override:
            p = Path(override)
            if p.exists():
                return p
            self.log(f"[whale_art] 自定义角色不存在，回退默认：{p}")
        return self.dir / DEFAULT_PET

    def set_image(self, image_path: str | Path | None) -> bool:
        """切换角色图（None = 回默认）。返回是否可用。"""
        self.path = self._pick_image(image_path)
        self._base = None
        self._cache.clear()
        return self.available()

    def available(self) -> bool:
        return HAS_PIL and self.path.exists()

    # ------------------------------------------------------------ 像素管线

    def _load_base(self):
        im = Image.open(self.path).convert("RGBA")
        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
        return im

    def pil_image(self, height: float, flip: bool = False,
                  tone: str | None = None):
        """按目标显示高度（px）返回合成好的 RGB 图；不可用/失败返回 None。

        tone: None（正常）/ "sick"（生病灰）/ "sad"（低落降饱和）/
              "away"（离家：灰度 + 压暗）。
        """
        if not self.available():
            return None
        key = (round(float(height)), bool(flip), tone or "")
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        try:
            if self._base is None:
                self._base = self._load_base()
            base = self._base
            h = max(1, round(float(height)))
            w = max(1, round(base.width * h / base.height))
            im = base.resize((w, h), Image.LANCZOS)
            im = self._apply_tone(im, tone)
            if flip:
                im = im.transpose(Image.FLIP_LEFT_RIGHT)
            alpha = im.getchannel("A").point(
                lambda v: 0 if v < ALPHA_CUTOFF else v)
            im.putalpha(alpha)
            bg = Image.new("RGBA", im.size, KEY_RGB + (255,))
            bg.alpha_composite(im)
            out = bg.convert("RGB")
        except Exception as e:     # 坏图 / 内存：静默降级
            self.log(f"[whale_art] 渲染失败：{e!r}")
            return None
        if len(self._cache) >= _CACHE_CAP:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = out
        return out

    @staticmethod
    def _apply_tone(im, tone):
        if tone == "sick":
            return ImageEnhance.Color(im).enhance(0.12)
        if tone == "sad":
            return ImageEnhance.Color(im).enhance(0.55)
        if tone == "away":
            g = ImageEnhance.Color(im).enhance(0.0)
            return ImageEnhance.Brightness(g).enhance(0.72)
        return im

    def gif_frames(self, name: str = "rua.gif", height: float = 170):
        """rua.gif 的逐帧 RGB 图（同样合成到色键背景）；失败返回空列表。"""
        if not HAS_PIL:
            return []
        key = ("gif", name, round(float(height)))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        path = self.dir / name
        frames: list = []
        try:
            with Image.open(path) as g:
                for frame in _iter_frames(g):
                    im = frame.convert("RGBA")
                    h = max(1, round(float(height)))
                    w = max(1, round(im.width * h / im.height))
                    im = im.resize((w, h), Image.LANCZOS)
                    alpha = im.getchannel("A").point(
                        lambda v: 0 if v < ALPHA_CUTOFF else v)
                    im.putalpha(alpha)
                    bg = Image.new("RGBA", im.size, KEY_RGB + (255,))
                    bg.alpha_composite(im)
                    frames.append(bg.convert("RGB"))
        except Exception as e:
            self.log(f"[whale_art] 动图读取失败：{e!r}")
            return []
        if len(self._cache) >= _CACHE_CAP:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = frames
        return frames


def _iter_frames(gif):
    """展开 GIF 全部帧（copy 后返回，避免惰性 seek 失效）。"""
    if not HAS_PIL:
        return []
    from PIL import ImageSequence
    return [f.copy() for f in ImageSequence.Iterator(gif)]
