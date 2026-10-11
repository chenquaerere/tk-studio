"""白名单要跟着设置走：换了输出目录，新目录里的文件不能一直 403。

缺陷背景（2026-10-10 复盘）：
`/api/file` 的允许目录白名单只在导入时算一次，用户改完输出目录以后，
新目录里的图片/视频预览与下载会一直 403，非得重启程序才好 ——
和「设置页写了不生效」是同一类问题。

这里用 `api._allow_sources_override` 把「配置来源」换成临时文件，
再配合 `api._load_cfg` 的替身，全程不碰真实的 config.json。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_APP_DIR = Path(__file__).resolve().parents[2]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from server import api  # noqa: E402


class _CfgState:
    """测试用的可变配置：output_root 指向哪个目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self) -> dict:
        return {"output_root": str(self.root)}


class TestAllowListFollowsConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.dir_a = base / "out-a"
        self.dir_b = base / "out-b"
        self.project = base / "project"
        self.uploads = base / "uploads"
        for d in (self.dir_a, self.dir_b, self.project, self.uploads):
            d.mkdir(parents=True, exist_ok=True)
        (self.dir_a / "a.mp4").write_bytes(b"a")
        (self.dir_b / "b.mp4").write_bytes(b"b")

        # 假配置文件：修改时间就是「用户改了设置」的信号
        self.cfg_file = base / "config.json"
        self.cfg_file.write_text("{}", encoding="utf-8")

        self.state = _CfgState(self.dir_a)
        patches = [
            mock.patch.object(api, "_load_cfg", lambda: self.state.load()),
            mock.patch.object(api, "PROJECT_ROOT", self.project),
            mock.patch.object(api, "UPLOAD_DIR", self.uploads),
            mock.patch.object(api, "_allow_sources_override", [self.cfg_file]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # 清掉上一个用例留下的缓存，保证从「没刷过」开始
        api._ALLOW_PREFIXES = []
        api._ALLOW_STAMP = -1.0
        api._ALLOW_SOURCES = []

    def _bump_cfg(self) -> None:
        """模拟用户保存设置：配置内容与修改时间都变。"""
        time.sleep(0.01)
        self.cfg_file.write_text('{"changed": 1}', encoding="utf-8")
        st = self.cfg_file.stat()
        os.utime(self.cfg_file, (st.st_atime, st.st_mtime + 1.0))

    def test_current_output_dir_allowed_other_not(self) -> None:
        api.refresh_allowed_paths()
        self.assertTrue(api._is_allowed(self.dir_a / "a.mp4"))
        self.assertFalse(api._is_allowed(self.dir_b / "b.mp4"))

    def test_new_dir_allowed_after_config_changes_without_explicit_refresh(self) -> None:
        """换了输出目录 → 下一次判定就自动重刷，不用重启程序。"""
        api.refresh_allowed_paths()
        self.assertFalse(api._is_allowed(self.dir_b / "b.mp4"))

        self.state.root = self.dir_b
        self._bump_cfg()
        # 故意不调用 refresh_allowed_paths()：靠 mtime 变化自动发现
        self.assertTrue(api._is_allowed(self.dir_b / "b.mp4"))
        self.assertFalse(api._is_allowed(self.dir_a / "a.mp4"))

    def test_unchanged_config_does_not_reshuffle_whitelist(self) -> None:
        api.refresh_allowed_paths()
        first = list(api._ALLOW_PREFIXES)
        self.assertTrue(api._is_allowed(self.dir_a / "a.mp4"))
        self.assertEqual([str(p) for p in api._ALLOW_PREFIXES], [str(p) for p in first])

    def test_project_and_upload_stay_allowed(self) -> None:
        api.refresh_allowed_paths()
        self.assertTrue(api._is_allowed(self.project / "anything.txt"))
        self.assertTrue(api._is_allowed(self.uploads / "up.png"))

    def test_outside_path_still_refused(self) -> None:
        api.refresh_allowed_paths()
        outside = Path(self._tmp.name) / "elsewhere.txt"
        outside.write_text("x", encoding="utf-8")
        self.assertFalse(api._is_allowed(outside))

    def test_save_endpoints_refresh_whitelist(self) -> None:
        """两个设置保存接口都要显式重刷（静态断言，防止以后再漏）。"""
        src = (_APP_DIR / "server" / "api.py").read_text(encoding="utf-8")
        body_settings = src.split("async def set_settings(", 1)[1].split("\n@app.", 1)[0]
        self.assertIn("refresh_allowed_paths()", body_settings)
        body_tk = src.split("def tiktok_settings_set(", 1)[1].split("\n@app.", 1)[0]
        self.assertIn("refresh_allowed_paths()", body_tk)


if __name__ == "__main__":
    unittest.main()
