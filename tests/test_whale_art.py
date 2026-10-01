"""鲸鱼娘渲染管线测试：缩放 / 镜像 / 调色 / 自定义角色（不依赖 Tk）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from habitpet import whale_art
from habitpet.whale_art import WhaleSprite


@unittest.skipUnless(whale_art.HAS_PIL, "需要 Pillow")
class TestWhaleSprite(unittest.TestCase):
    def setUp(self):
        self.sp = WhaleSprite()

    def test_available_default_asset(self):
        self.assertTrue(self.sp.available())

    def test_scaled_output_keeps_aspect(self):
        im = self.sp.pil_image(200)
        self.assertIsNotNone(im)
        self.assertEqual(im.mode, "RGB")
        self.assertEqual(im.height, 200)
        base = self.sp._base or self.sp._load_base()
        expect = round(base.width * 200 / base.height)
        self.assertLessEqual(abs(im.width - expect), 2)

    def test_flip_is_horizontal_mirror(self):
        from PIL import Image, ImageChops
        a = self.sp.pil_image(160, flip=False)
        b = self.sp.pil_image(160, flip=True)
        self.assertNotEqual(a.tobytes(), b.tobytes())
        mirrored = a.transpose(Image.FLIP_LEFT_RIGHT)
        self.assertIsNone(
            ImageChops.difference(b, mirrored).getbbox())

    def test_tones_change_pixels(self):
        normal = self.sp.pil_image(150)
        for tone in ("sick", "sad", "away"):
            self.assertNotEqual(normal.tobytes(),
                                self.sp.pil_image(150, tone=tone).tobytes(),
                                tone)

    def test_cache_returns_same_object(self):
        self.assertIs(self.sp.pil_image(180), self.sp.pil_image(180))

    def test_custom_image_override(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            img_path = Path(d) / "custom.png"
            Image.new("RGBA", (80, 100), (255, 0, 0, 255)).save(img_path)
            sp = WhaleSprite(image_path=img_path)
            self.assertTrue(sp.available())
            im = sp.pil_image(50)
            self.assertEqual(im.height, 50)

    def test_missing_custom_falls_back_to_default(self):
        sp = WhaleSprite(image_path="Z:/nope/missing.png")
        self.assertTrue(sp.available())
        self.assertTrue(str(sp.path).endswith("DSniang1.png"))

    def test_gif_frames_expand(self):
        frames = self.sp.gif_frames(height=64)
        self.assertEqual(len(frames), 10)
        self.assertTrue(all(f.mode == "RGB" for f in frames))

    def test_unavailable_returns_none(self):
        sp = WhaleSprite(assets_dir=Path("Z:/nope"))
        self.assertFalse(sp.available())
        self.assertIsNone(sp.pil_image(100))
        self.assertEqual(sp.gif_frames(), [])


if __name__ == "__main__":
    unittest.main()
