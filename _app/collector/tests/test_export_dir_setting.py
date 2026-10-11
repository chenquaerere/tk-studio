"""设置页「导出目录 / 日志目录」必须真的生效（回归）。

背景：这两个输入框此前只存在于设置页，没有任何代码读它们 ——
导出永远写死 exports/、日志永远写死 logs/，改了等于没改。
本用例锁住三件事：

1. 留空 / 相对路径 / 绝对路径分别解析成期望目录（相对路径按程序根）；
2. 保存设置时先建目录，建不出来就报 400（不允许「半保存」）；
3. 「下载导出文件」的目录白名单跟着导出目录一起走（既不能越权，
   也不能把历史默认行为改坏）。
"""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from common import paths
from server import collector_api


class _Cfg:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Ctx:
    def __init__(self, values):
        self.config = _Cfg(values)


class _PatchedCtx(unittest.TestCase):
    """把 collector_api.ctx 换成假容器（不碰真实配置与数据库）。"""

    def patch_ctx(self, **values):
        self._set_ctx(lambda: _Ctx(values))

    def _set_ctx(self, factory):
        original = collector_api.ctx
        collector_api.ctx = factory
        self.addCleanup(setattr, collector_api, "ctx", original)


class TestExportsDir(_PatchedCtx):
    def test_blank_uses_default(self):
        self.patch_ctx(export_dir="   ")
        self.assertEqual(collector_api._exports_dir(), paths.EXPORTS_DIR)

    def test_missing_key_uses_default(self):
        self.patch_ctx()
        self.assertEqual(collector_api._exports_dir(), paths.EXPORTS_DIR)

    def test_relative_is_app_root_based(self):
        self.patch_ctx(export_dir="exports_test")
        self.assertEqual(collector_api._exports_dir(),
                         paths.APP_ROOT / "exports_test")

    def test_absolute_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "tk-exports"
            self.patch_ctx(export_dir=str(target))
            self.assertEqual(collector_api._exports_dir(), target)

    def test_broken_container_falls_back(self):
        def _boom():
            raise RuntimeError("容器不可用")

        self._set_ctx(_boom)
        self.assertEqual(collector_api._exports_dir(), paths.EXPORTS_DIR)


class TestLogsDir(_PatchedCtx):
    def test_blank_uses_default(self):
        self.patch_ctx(log_dir="")
        self.assertEqual(collector_api._logs_dir(), paths.LOGS_DIR)

    def test_relative_is_app_root_based(self):
        self.patch_ctx(log_dir="logs_test")
        self.assertEqual(collector_api._logs_dir(), paths.APP_ROOT / "logs_test")


class TestEnsureSettingDirs(unittest.TestCase):
    def test_creates_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b"
            collector_api._ensure_setting_dirs({"export_dir": str(target)})
            self.assertTrue(target.is_dir())

    def test_blank_uses_default_without_error(self):
        # 留空 = 用默认目录，不能因为空值报错（默认目录是应用布局的一部分）
        collector_api._ensure_setting_dirs({"export_dir": "", "log_dir": ""})
        self.assertTrue(paths.EXPORTS_DIR.is_dir())
        self.assertTrue(paths.LOGS_DIR.is_dir())

    def test_unusable_dir_raises_400(self):
        bad = ("R:/tk-not-here/exports" if os.name == "nt"
               else "/proc/tk-not-here/exports")
        with self.assertRaises(HTTPException) as caught:
            collector_api._ensure_setting_dirs({"export_dir": bad})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("导出目录", str(caught.exception.detail))

    def test_unrelated_keys_are_ignored(self):
        # 只有目录键需要验证，别的设置项不该在这里被碰
        collector_api._ensure_setting_dirs({"log_level": "DEBUG",
                                            "history_keep_days": 30})


class TestExportDownloadWhitelist(_PatchedCtx):
    def test_file_in_custom_dir_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "exp"
            d.mkdir()
            target = d / "采集结果.txt"
            target.write_text("x", encoding="utf-8")
            self.patch_ctx(export_dir=str(d))
            resp = collector_api.export_download(path=str(target))
            self.assertEqual(Path(resp.path).resolve(), target.resolve())

    def test_file_outside_custom_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "exp"
            d.mkdir()
            outside = Path(tmp) / "secret.txt"
            outside.write_text("x", encoding="utf-8")
            self.patch_ctx(export_dir=str(d))
            with self.assertRaises(HTTPException) as caught:
                collector_api.export_download(path=str(outside))
            self.assertEqual(caught.exception.status_code, 403)

    def test_default_exports_dir_still_allowed(self):
        # 不配置 export_dir 时历史行为不变：exports/ 下的文件照样能下载
        paths.ensure_dirs()
        target = paths.EXPORTS_DIR / "__tk_probe_export__.txt"
        target.write_text("x", encoding="utf-8")
        try:
            self.patch_ctx()
            resp = collector_api.export_download(path=str(target))
            self.assertEqual(Path(resp.path).resolve(), target.resolve())
        finally:
            target.unlink(missing_ok=True)

    def test_missing_file_inside_reports_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "exp"
            d.mkdir()
            self.patch_ctx(export_dir=str(d))
            with self.assertRaises(HTTPException) as caught:
                collector_api.export_download(path=str(d / "nope.txt"))
            self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
