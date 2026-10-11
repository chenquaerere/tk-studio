"""历史死函数不该复活（v1.4.3 清理）。

这些函数在界面改走 HTTP + 任务事件之后就没有任何调用者了，
但它们曾经造成过两次「修了却没生效」的真实故障：
`_notify` / `do_download` 是完成提醒的假实现，
`_hd_worker_running` / `_tk_worker_running` 的占用标志只由死代码写。
所以这里把它们钉住：删掉的东西不许悄悄回来，活着的替代品必须还在。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]          # _app
SERVICES = APP_DIR / "server" / "services.py"
API = APP_DIR / "server" / "api.py"

GONE = (
    "do_parse",
    "do_download",
    "_notify",
    "handle_mixed",
    "safe_mixed",
    "safe_image_only",
    "safe_video_only",
    "safe_download_one",
    "safe_download_all",
    "safe_settings",
    "cancel_run",
    "cleanup_work",
    "paste_clipboard_into",
    "clear_inputs",
    "tk_pause_action",
    "tk_resume_action",
    "tk_cancel_action",
    "hd_pause_action",
    "hd_resume_action",
    "library_open_dir",
    "_tk_progress_text",
    "_tk_progress_pct",
    "_hd_progress_pct",
    "_hd_progress_text",
    "shutdown_hint",
    "save_all",
)

LIVE = {
    SERVICES: ("def tk_worker_begin(", "def tk_worker_end(", "def hd_worker_begin(",
               "def _tk_download_worker(", "def _hd_worker("),
    API: ("def _parse_worker(", "c.tk_worker_begin()", "c.hd_worker_begin()"),
}


class TestDeadFunctionsGone(unittest.TestCase):
    def test_no_dead_definition_left(self):
        for path in (SERVICES, API):
            text = path.read_text(encoding="utf-8")
            for name in GONE:
                with self.subTest(file=path.name, name=name):
                    self.assertIsNone(
                        re.search(rf"^\s*def {re.escape(name)}\(", text, re.M),
                        f"{path.name} 里又出现了死函数 {name}",
                    )
            # `_save_cfg` 只删了 api.py 的那份定义
        self.assertNotIn("def _save_cfg(", API.read_text(encoding="utf-8"))

    def test_live_replacements_present(self):
        for path, needles in LIVE.items():
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(file=path.name, needle=needle):
                    self.assertIn(needle, text)

    def test_services_still_compiles_and_has_content(self):
        # 清理掉的是 191 行历史代码，不是整个文件
        text = SERVICES.read_text(encoding="utf-8")
        self.assertGreater(len(text.splitlines()), 1200)
        self.assertIn("def settings_values(", text)
        self.assertIn("def request_shutdown(", text)


if __name__ == "__main__":
    unittest.main()
