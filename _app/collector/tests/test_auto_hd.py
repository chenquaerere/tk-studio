"""「下载后自动送入高清（仅图片）」必须真的把图片送去高清。

缺陷（2026-10-10 复盘）：勾选框 `#tk-auto-hd` 只把 `auto_hd: true` 发给后端，
后端在 `_tk_download_worker` 里算了 `job["auto_hd"] = 图片数量`，
**前端没有任何代码读这个值**，也没有任何地方启动高清任务 ——
于是这个勾选框是个死开关：用户以为下载完会自动高清，其实什么都不会发生。

修法：前端自己记住本次下载到的图片（`pickAutoHdImages`），
下载任务正常结束后把它们加入高清列表、切到高清页并复用既有的「开始处理」流程。

这里用 node 真跑一遍 `pickAutoHdImages`（node 不在 PATH 时自动跳过），
并用反向对照证明「不过滤 is_image 的老写法确实会把视频也送进高清」。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[2]
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

_APP_JS = _APP_DIR / "frontend" / "js" / "app.js"
_INDEX_HTML = _APP_DIR / "frontend" / "index.html"
_SERVICES_PY = _APP_DIR / "server" / "services.py"

# 纯函数，缩进两级（IIFE 内），结尾是两空格 + }
_HELPER_RE = re.compile(r"function pickAutoHdImages\(outputs\) \{[\s\S]*?\n  \}")
# 下载按钮的处理块：从记录勾选状态到开始下载的 toast
_DOWNLOAD_RE = re.compile(
    r'const wantAutoHd = \$\("tk-auto-hd"\)\.checked;[\s\S]*?\n      toast\(picked\.length')

NODE = shutil.which("node")


def _helper_source() -> str:
    src = _APP_JS.read_text(encoding="utf-8")
    m = _HELPER_RE.search(src)
    if not m:
        raise AssertionError("app.js 里找不到 pickAutoHdImages（测试需要更新）")
    return m.group(0)


def _download_block() -> str:
    src = _APP_JS.read_text(encoding="utf-8")
    m = _DOWNLOAD_RE.search(src)
    if not m:
        raise AssertionError("app.js 里找不到下载按钮的处理块（测试需要更新）")
    return m.group(0)


class TestAutoHdSource(unittest.TestCase):
    def test_helper_keeps_only_images_with_path(self) -> None:
        helper = _helper_source()
        self.assertIn("is_image", helper)
        self.assertIn("path", helper)
        self.assertIn(".filter(", helper)
        self.assertIn(".map(", helper)

    def test_checkbox_is_read_once_and_used_for_both_request_and_auto_hd(self) -> None:
        block = _download_block()
        self.assertIn('const wantAutoHd = $("tk-auto-hd").checked;', block)
        self.assertIn("auto_hd: wantAutoHd,", block)
        # 老写法（请求里内联读勾选框）会让「这次到底勾没勾」与后续判断不一致
        self.assertNotIn('auto_hd: $("tk-auto-hd").checked', block)

    def test_only_collects_when_the_box_was_ticked(self) -> None:
        block = _download_block()
        self.assertIn(
            "if (wantAutoHd) autoHdImages = pickAutoHdImages(outputs);", block)

    def test_on_done_is_wired_and_guarded(self) -> None:
        block = _download_block()
        self.assertIn("onDone: (kind) => {", block)
        self.assertIn(
            'if (!wantAutoHd || kind !== "job_done" || !autoHdImages.length) return;',
            block)

    def test_on_done_really_hands_files_to_the_hd_page(self) -> None:
        block = _download_block()
        self.assertIn("hdServerFiles = hdServerFiles.concat(paths);", block)
        self.assertIn("autoHdImages.filter((p) => !hdServerFiles.includes(p))", block)
        self.assertIn("renderPicked();", block)
        self.assertIn('activatePage("hd");', block)
        self.assertIn('const btn = $("hd-start");', block)
        self.assertIn("btn.click();", block)
        # 已有高清任务在跑时只入列、不硬起第二个任务（后端会 409）
        self.assertIn("if (hdBusy) {", block)

    def test_label_promise_matches_the_filter(self) -> None:
        html = _INDEX_HTML.read_text(encoding="utf-8")
        m = re.search(r'id="tk-auto-hd"[^>]*/>([^<]*)<', html)
        self.assertIsNotNone(m, "index.html 里找不到 tk-auto-hd 勾选框")
        self.assertIn("自动送入高清", m.group(1))
        self.assertIn("仅图片", m.group(1))

    def test_backend_flag_is_about_images_too(self) -> None:
        """后端算的图片数量与前端过滤口径一致（都用图片判定）。"""
        src = _SERVICES_PY.read_text(encoding="utf-8")
        self.assertIn('_tk_recent_files = [p for p in downloaded_files', src)
        self.assertIn("IMAGE_EXTS", src)
        self.assertIn('job["auto_hd"] = len(_tk_recent_files)', src)


@unittest.skipUnless(NODE, "node 不在 PATH，跳过行为验证")
class TestAutoHdBehaviour(unittest.TestCase):
    MIXED = [
        {"path": "/out/1.jpeg", "is_image": True},
        {"path": "/out/2.mp4", "is_image": False},
        {"path": "/out/3.webp", "is_image": True},
        {"is_image": True},                 # 缺 path
        {"path": "", "is_image": True},     # 空 path
        "not-an-object",
        None,
    ]

    def _run(self, body: str) -> str:
        script = _helper_source() + "\n" + body
        proc = subprocess.run([NODE, "-e", script], capture_output=True,
                              text=True, encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_picks_images_in_order(self) -> None:
        out = self._run(
            "process.stdout.write(JSON.stringify(pickAutoHdImages(%s)));"
            % json.dumps(self.MIXED, ensure_ascii=False))
        self.assertEqual(json.loads(out), ["/out/1.jpeg", "/out/3.webp"])

    def test_handles_missing_or_empty_outputs(self) -> None:
        out = self._run(
            "process.stdout.write(JSON.stringify(["
            "pickAutoHdImages(undefined), pickAutoHdImages(null), pickAutoHdImages([])"
            "]));")
        self.assertEqual(json.loads(out), [[], [], []])

    def test_keeps_duplicates_so_the_caller_can_dedupe(self) -> None:
        dup = [{"path": "/out/a.jpeg", "is_image": True}] * 2
        out = self._run(
            "process.stdout.write(JSON.stringify(pickAutoHdImages(%s)));"
            % json.dumps(dup))
        self.assertEqual(json.loads(out), ["/out/a.jpeg", "/out/a.jpeg"])

    def test_old_naive_mapping_would_send_videos_to_hd(self) -> None:
        """反向对照：只取 path、不看 is_image 的老写法会把视频也送进高清。"""
        out = self._run(
            "const naive = (outputs) => (outputs || []).map((o) => o && o.path);\n"
            "process.stdout.write(JSON.stringify(naive(%s)));"
            % json.dumps(self.MIXED, ensure_ascii=False))
        self.assertIn("/out/2.mp4", json.loads(out))   # 视频混进高清列表
        picked = self._run(
            "process.stdout.write(JSON.stringify(pickAutoHdImages(%s)));"
            % json.dumps(self.MIXED, ensure_ascii=False))
        self.assertNotIn("/out/2.mp4", json.loads(picked))


if __name__ == "__main__":
    unittest.main()
