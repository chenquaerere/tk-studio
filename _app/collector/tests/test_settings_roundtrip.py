"""水印设置「保存了要能读回来」（回归）。

背景：`settings_save()` 一直在写 `video_skip_similar` / `face_work_side`，
但 `GET /api/settings` 的键表里没有这两项 —— 设置页每次打开都显示默认值，
用户再点一次「保存」就把自己之前的选择覆盖掉。

为什么放在 collector/tests：这是本仓库唯一会被 `unittest discover` 跑到的测试目录。
用例把 `services.load` / `services.save` 换成内存实现，绝不碰真实 config.json。
"""
import unittest
from pathlib import Path

# settings_save 的位置参数顺序（与 server/api.py 的 order 数组一致）
SAVE_ORDER = ["out_root", "date_folder", "face_enhance", "tile", "half",
              "encoder", "crf", "interpolate", "skip", "skipsim", "keep",
              "max_size", "vidmax", "beep", "facework", "lan_access",
              "https_enabled"]

BASE_CFG = {
    "output_root": "D:/tk-out", "date_folder": True, "face_enhance": False,
    "tile": 0, "half": False, "video_encoder": "auto", "video_crf": 18,
    "interpolate": False, "skip_existing": True, "keep_frames": False,
    "max_output_size": 4096, "video_max_side": 1920, "beep_on_done": True,
    "lan_access": False, "video_skip_similar": True, "face_work_side": 4096,
    "https_enabled": False,
}


class TestWatermarkSettingsRoundTrip(unittest.TestCase):
    def setUp(self):
        from server import api, services

        self.api, self.services = api, services
        self.cfg = dict(BASE_CFG)
        self._orig = (services.load, services.save)

        def _load():
            return dict(self.cfg)

        def _save(c):
            self.cfg.clear()
            self.cfg.update(c)

        services.load = _load
        services.save = _save
        self.addCleanup(self._restore)

    def _restore(self):
        self.services.load, self.services.save = self._orig

    def _save_values(self, **changes):
        args = {name: BASE_CFG.get(name) for name in SAVE_ORDER}
        args.update(changes)
        # 统一按 save 顺序取值，缺省的用 BASE_CFG 里对应键的当前值兜底
        current = self.cfg
        positional = []
        key_by_name = {
            "out_root": "output_root", "date_folder": "date_folder",
            "face_enhance": "face_enhance", "tile": "tile", "half": "half",
            "encoder": "video_encoder", "crf": "video_crf",
            "interpolate": "interpolate", "skip": "skip_existing",
            "skipsim": "video_skip_similar", "keep": "keep_frames",
            "max_size": "max_output_size", "vidmax": "video_max_side",
            "beep": "beep_on_done", "facework": "face_work_side",
            "lan_access": "lan_access",
            "https_enabled": "https_enabled",
        }
        for name in SAVE_ORDER:
            positional.append(changes.get(name, current.get(key_by_name[name])))
        return self.services.settings_save(*positional)

    def _reported(self):
        return self.api.get_settings()["values"]

    def test_key_and_value_counts_match(self):
        self.assertEqual(len(self.services.settings_values()),
                         len(self._reported()))

    def test_previously_missing_two_are_reported(self):
        reported = self._reported()
        self.assertIn("video_skip_similar", reported)
        self.assertIn("face_work_side", reported)

    def test_changed_choices_survive_save(self):
        self._save_values(skipsim=False, facework=1536, max_size=2048,
                          vidmax=1280, interpolate=True)
        reported = self._reported()
        self.assertIs(reported["video_skip_similar"], False)
        self.assertEqual(reported["face_work_side"], 1536)
        self.assertEqual(reported["max_output_size"], 2048)
        self.assertEqual(reported["video_max_side"], 1280)
        self.assertIs(reported["interpolate"], True)

    def test_every_field_round_trips(self):
        """每个字段都改一遍再读回来 —— 任何一侧漏字段都会在这里挂掉。"""
        self._save_values(
            out_root="D:/tk-out-2", date_folder=False, face_enhance=True,
            tile=2, half=True, encoder="libx264", crf=23, interpolate=True,
            skip=False, skipsim=False, keep=True, max_size=1024, vidmax=720,
            beep=False, facework=2048, lan_access=True, https_enabled=True)
        reported = self._reported()
        expected = {
            "output_root": str(Path("D:/tk-out-2")),
            "date_folder": False, "face_enhance": True, "tile": 2, "half": True,
            "video_encoder": "libx264", "video_crf": 23, "interpolate": True,
            "skip_existing": False, "keep_frames": True,
            "max_output_size": 1024, "video_max_side": 720,
            "beep_on_done": False, "lan_access": True,
            "video_skip_similar": False, "face_work_side": 2048,
            "https_enabled": True,
        }
        self.assertEqual(sorted(reported), sorted(expected))
        for key, want in expected.items():
            self.assertEqual(reported[key], want, f"{key} 没有读回来")

    def test_real_config_untouched(self):
        """用例全程走内存实现：不得写真实 config.json。"""
        from common import paths

        before = (paths.CONFIG_PATH.read_text(encoding="utf-8")
                  if paths.CONFIG_PATH.is_file() else None)
        self._save_values(facework=1234, skipsim=False)
        after = (paths.CONFIG_PATH.read_text(encoding="utf-8")
                 if paths.CONFIG_PATH.is_file() else None)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
