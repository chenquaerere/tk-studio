"""load_image_bgr 的读取行为（回归）。

这个用例锁三件事：
1. 读出来的数组是 BGR、uint8、三通道（cv2 / Real-ESRGAN 的约定）；
2. 灰度 / 带 EXIF 旋转的图会被转成 RGB、方向正确；
3. 读完立刻删源文件必须成功 —— 句柄不能拖到 GC 才关。

说明（诚实版）：旧写法 `im = Image.open(path)` 在 CPython 下靠引用计数也会在
函数返回时关闭句柄，实测删文件同样成功。改成 `with ... load()` 是为了让释放
**确定发生在读完之后**（异常路径同样关闭），并符合 Pillow 的推荐写法。
"""
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from watermark.core.engines.image import load_image_bgr


class TestLoadImageBgr(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name, im, **kw):
        p = self.dir / name
        im.save(p, **kw)
        return p

    def test_rgb_is_returned_as_bgr_uint8(self):
        # 造一张已知像素的图：左上角纯红（RGB 255,0,0）→ BGR 应为 (0,0,255)
        arr = np.zeros((4, 5, 3), dtype=np.uint8)
        arr[0, 0] = (255, 0, 0)
        p = self._write("rgb.png", Image.fromarray(arr))
        out = load_image_bgr(p)
        self.assertEqual(out.shape, (4, 5, 3))
        self.assertEqual(out.dtype, np.uint8)
        self.assertEqual(tuple(out[0, 0]), (0, 0, 255))

    def test_gray_is_converted_to_three_channels(self):
        p = self._write("gray.png", Image.fromarray(
            np.full((6, 7), 128, dtype=np.uint8)))          # 单通道 → Pillow 判为 "L"
        out = load_image_bgr(p)
        self.assertEqual(out.shape, (6, 7, 3))

    def test_rgba_is_converted_to_three_channels(self):
        p = self._write("rgba.png", Image.fromarray(
            np.zeros((3, 3, 4), dtype=np.uint8)))           # 4 通道 → Pillow 判为 "RGBA"
        out = load_image_bgr(p)
        self.assertEqual(out.shape, (3, 3, 3))

    def test_exif_rotation_is_applied(self):
        im = Image.fromarray(np.zeros((4, 8, 3), dtype=np.uint8))
        exif = Image.Exif()
        exif[0x0112] = 6          # orientation=6 → 顺时针旋转 90°，宽高互换
        p = self.dir / "rot.jpg"
        im.save(p, exif=exif)
        out = load_image_bgr(p)
        self.assertEqual(out.shape, (8, 4, 3), "EXIF 旋转没有被应用")

    def test_source_file_can_be_removed_right_after_reading(self):
        p = self._write("del.png", Image.fromarray(
            np.zeros((10, 10, 3), dtype=np.uint8)))
        load_image_bgr(p)
        os.remove(p)          # Windows 上句柄没关会抛 PermissionError
        self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()
