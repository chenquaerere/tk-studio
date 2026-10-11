"""设置页「日志目录」必须真的生效（回归）。

背景：`log_dir` 和「导出目录」一样，曾经只在设置页显示、没有任何代码读它，
日志永远写死 `logs/`。日志目录属于启动期初始化（改完要重启），
所以这里直接测启动期的解析函数，不启动整个服务容器。
"""
import tempfile
import unittest
from pathlib import Path

from collector.bootstrap import resolve_log_dir
from common import paths


class _Cfg:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


class TestResolveLogDir(unittest.TestCase):
    def test_blank_uses_default(self):
        self.assertEqual(resolve_log_dir(_Cfg({"log_dir": "  "})),
                         paths.LOGS_DIR)

    def test_missing_key_uses_default(self):
        self.assertEqual(resolve_log_dir(_Cfg({})), paths.LOGS_DIR)

    def test_relative_is_app_root_based_and_created(self):
        target = paths.APP_ROOT / "logs_test_probe"

        def _cleanup():
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()

        self.addCleanup(_cleanup)
        self.assertEqual(resolve_log_dir(_Cfg({"log_dir": "logs_test_probe"})),
                         target)
        self.assertTrue(target.is_dir())

    def test_absolute_is_used_and_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "tk-logs"
            self.assertEqual(resolve_log_dir(_Cfg({"log_dir": str(target)})),
                             target)
            self.assertTrue(target.is_dir())

    def test_unusable_dir_falls_back_to_default(self):
        # 盘符不存在 / 无权限时退回默认目录 —— 绝不因为路径写坏而挡住启动
        bad = "R:/tk-not-here/logs"
        self.assertEqual(resolve_log_dir(_Cfg({"log_dir": bad})), paths.LOGS_DIR)


if __name__ == "__main__":
    unittest.main()
