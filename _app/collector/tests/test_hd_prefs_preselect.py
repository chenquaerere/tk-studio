"""高清页「上次用的参数」要真的填回表单（回归）。

背景（v4.0 的「存了不生效」家族）：`services._remember_choices()` 每次都把
本次用的模型/档位/勾选存进配置（image_model、video_model、image_scale、
video_scale、video_sharp、video_max_side、face_enhance、interpolate），
但前端 `app.js` 加载高清页时把下拉项写死成「realesr-general-x4v3 / 自动 /
1920」—— 用户上次选的模型、档位、开关重启后全部丢失。

修法：前端从 `GET /api/settings` 的 `raw`（完整配置）读这些值再预选。
本用例锁住三件事：
1. 配置里确实有这些键（前端的数据来源存在）；
2. `_remember_choices()` 存的数值与前端预选时的解读一致（4 → 「4x」、
   video_sharp → 「仅锐化」、「video_max_side」→ 档位）；
3. `app.js` 真的读了它们，且不再是写死的默认值。

放在 collector/tests 的原因同 test_settings_roundtrip.py：这是唯一会被
`unittest discover` 跑到的测试目录。本用例不写真实配置（用内存替身）。
"""
import unittest

REMEMBERED = ("image_model", "video_model", "image_scale", "video_scale",
              "video_sharp", "video_max_side", "face_enhance", "interpolate")


class TestConfigCarriesPrefs(unittest.TestCase):
    def test_flattened_config_has_all_remembered_keys(self):
        from watermark.core import config as wconfig

        cfg = wconfig.load()
        for key in REMEMBERED:
            self.assertIn(key, cfg, f"配置里缺少 {key}，前端就无从预选")
        self.assertEqual(cfg["image_scale"], 4)
        self.assertIn(cfg["video_sharp"], (True, False))
        self.assertEqual(cfg["video_max_side"], 1920)

    def test_settings_endpoint_raw_exposes_prefs(self):
        """前端就是从 /api/settings 的 raw 里取的（只读调用，不改配置）。"""
        from server import api

        data = api.get_settings()
        self.assertIn("raw", data)
        for key in REMEMBERED:
            self.assertIn(key, data["raw"], f"/api/settings raw 缺少 {key}")


class TestRememberChoices(unittest.TestCase):
    def setUp(self):
        from server import services

        self.services = services
        self.cfg = {}
        self.addCleanup(setattr, services, "load", services.load)
        self.addCleanup(setattr, services, "save", services.save)
        services.load = lambda: dict(self.cfg)
        services.save = lambda cfg: self.cfg.clear() or self.cfg.update(cfg)

    def test_image_choice_round_trip(self):
        self.services._remember_choices("image", "realesr-general-x4v3", "4x",
                                        face=True)
        self.assertEqual(self.cfg["image_model"], "realesr-general-x4v3")
        self.assertEqual(self.cfg["image_scale"], 4)      # 前端据此选「4x」
        self.assertTrue(self.cfg["face_enhance"])

    def test_video_sharp_and_side(self):
        self.services._remember_choices("video", "realesr-general-x4v3", "2x",
                                        interpolate=True, max_side="sharp")
        self.assertEqual(self.cfg["video_scale"], 2)      # 前端据此选「2x」
        self.assertTrue(self.cfg["video_sharp"])          # 前端据此选「仅锐化」
        self.assertTrue(self.cfg["interpolate"])

        self.services._remember_choices("video", "realesr-general-x4v3", "4x",
                                        max_side=2560)
        self.assertFalse(self.cfg["video_sharp"])
        self.assertEqual(self.cfg["video_max_side"], 2560)

    def test_auto_scale_falls_back_to_model_scale(self):
        """「自动（推荐）」存的是模型自带的倍数，不是 0/空。"""
        self.services._remember_choices("image", "realesr-general-x4v3",
                                        "自动（推荐）")
        self.assertIn(self.cfg["image_scale"], (2, 4))


class TestFrontendPreselects(unittest.TestCase):
    def setUp(self):
        from common import paths

        self.src = (paths.APP_ROOT / "_app" / "frontend" / "js" / "app.js").read_text(
            encoding="utf-8")

    def test_app_reads_each_remembered_pref(self):
        for token in ("pref.image_model", "pref.video_model", "pref.image_scale",
                      "pref.video_scale", "pref.video_sharp",
                      "pref.video_max_side", "pref.face_enhance",
                      "pref.interpolate", "pref.max_output_size"):
            self.assertIn(token, self.src, f"app.js 没有读 {token}")

    def test_hd_selects_no_longer_hardcoded(self):
        self.assertNotIn('fillOpts($("hd-scale"), m.scales || [], "自动（推荐）")',
                         self.src)
        self.assertNotIn('fillOpts($("hd-side"), m.sides, "1920")', self.src)


if __name__ == "__main__":
    unittest.main()
