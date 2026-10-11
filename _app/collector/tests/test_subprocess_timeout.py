"""子进程超时（回归）。

背景（v1.4.3 审计发现的真缺陷）：`watermark.core.utils.run_quiet()` 以及
`probe_video()` / `_pick_encoders()` / 工具页的两处 `subprocess.run()` 全都
没有 `timeout=`。ffmpeg/ffprobe 一旦卡住（输出文件被播放器占用、磁盘掉线、
下载中断留下的半截文件、显卡驱动挂死），后果不是「慢」，而是：

* 任务线程永远停在 `subprocess.run()` 里，`hd_worker_begin()` /
  `tk_worker_begin()` 占用的标志不会被释放 —— 界面按钮一直转，
  再点一次报「已有任务在运行」，用户只能重启程序；
* 工具页（转 GIF / 抽帧）的请求线程被占住，接口一直不返回。

现在统一给了宽松上限：探测类 `PROBE_TIMEOUT`（90 秒），
编码类 `ENCODE_TIMEOUT`（1 小时）。超时抛 `SubprocessTimeout`
（RuntimeError 子类，原来的编码器回退逻辑 catch 不到 subprocess 的
`TimeoutExpired`，所以必须换类型）。

放在 collector/tests 的原因同 test_settings_roundtrip.py：这是唯一会被
`unittest discover` 跑到的测试目录。本用例不调用真实 ffmpeg。
"""
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

APP_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (APP_ROOT / rel).read_text(encoding="utf-8", errors="replace")


class TestTimeoutConstants(unittest.TestCase):
    def test_limits_are_reachable_and_generous(self):
        from watermark.core.utils import ENCODE_TIMEOUT, PROBE_TIMEOUT
        from watermark.core.utils import fmt_timeout

        self.assertEqual(PROBE_TIMEOUT, 90)
        self.assertEqual(ENCODE_TIMEOUT, 3600)
        self.assertGreater(ENCODE_TIMEOUT, PROBE_TIMEOUT)
        self.assertEqual(fmt_timeout(3600), "1 小时")
        self.assertEqual(fmt_timeout(300), "5 分钟")
        self.assertEqual(fmt_timeout(90), "90 秒")

    def test_subprocess_timeout_is_a_runtime_error(self):
        from watermark.core.utils import SubprocessTimeout

        exc = SubprocessTimeout(["D:/tools/ffmpeg/bin/ffmpeg.exe", "-i", "x"], 3600)
        self.assertIsInstance(exc, RuntimeError)
        # 报错要能直接给用户看：哪个程序、超了多久、可能的原因
        self.assertIn("ffmpeg.exe", str(exc))
        self.assertIn("1 小时", str(exc))
        self.assertIn("占用", str(exc))
        self.assertEqual(exc.limit, 3600)


class TestRunQuiet(unittest.TestCase):
    def test_default_limit_is_encode_timeout(self):
        from watermark.core import utils

        with mock.patch.object(utils.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)) as fake:
            utils.run_quiet(["ffmpeg", "-i", "x"])
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs.get("timeout"), utils.ENCODE_TIMEOUT)
        # 老契约不能丢：不弹黑窗 + stdin 给 DEVNULL（v4.0 的黑窗修复）
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertIn("creationflags", kwargs)
        self.assertTrue(kwargs.get("check"))

    def test_explicit_limit_overrides_default(self):
        from watermark.core import utils

        with mock.patch.object(utils.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)) as fake:
            utils.run_quiet(["ffprobe", "x"], timeout=5)
        self.assertEqual(fake.call_args.kwargs.get("timeout"), 5)

    def test_timeout_expired_becomes_subprocess_timeout(self):
        from watermark.core import utils

        with mock.patch.object(utils.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("ffmpeg", 7)):
            with self.assertRaises(utils.SubprocessTimeout) as ctx:
                utils.run_quiet(["D:/tools/ffmpeg/bin/ffmpeg.exe"], timeout=7)
        self.assertEqual(ctx.exception.limit, 7)
        self.assertIn("7 秒", str(ctx.exception))

    def test_other_failures_still_raise_called_process_error(self):
        """超时之外的行为不能变（编码器回退逻辑靠这个异常类型）。"""
        from watermark.core import utils

        with mock.patch.object(
                utils.subprocess, "run",
                side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr=b"boom")):
            with self.assertRaises(subprocess.CalledProcessError):
                utils.run_quiet(["ffmpeg"])

    def test_real_subprocess_times_out(self):
        """真起一个睡眠进程，确认 timeout 真的被传给了 subprocess。"""
        from watermark.core.utils import SubprocessTimeout, run_quiet

        with self.assertRaises(SubprocessTimeout):
            run_quiet([sys.executable, "-c", "import time; time.sleep(30)"],
                      timeout=1)


class TestEnginesUseTimeouts(unittest.TestCase):
    def test_probe_video_has_timeout_and_friendly_error(self):
        from watermark.core import utils
        from watermark.core.engines import video

        with mock.patch.object(video, "find_ffprobe", return_value=pathlib.Path("ffprobe")):
            with mock.patch.object(video.subprocess, "check_output",
                                   side_effect=subprocess.TimeoutExpired("ffprobe", 90)):
                with self.assertRaises(RuntimeError) as ctx:
                    video.probe_video(pathlib.Path("D:/probe/clip.mp4"))
        self.assertIn("超时", str(ctx.exception))
        self.assertIn("clip.mp4", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, subprocess.TimeoutExpired)

    def test_probe_video_passes_probe_timeout(self):
        from watermark.core import utils
        from watermark.core.engines import video

        with mock.patch.object(video, "find_ffprobe", return_value=pathlib.Path("ffprobe")):
            with mock.patch.object(
                    video.subprocess, "check_output",
                    return_value='{"streams":[{"codec_type":"video","r_frame_rate":"25/1",'
                                 '"width":2,"height":2}],"format":{"duration":"1"}}'
            ) as fake:
                video.probe_video(pathlib.Path("D:/probe/clip.mp4"))
        self.assertEqual(fake.call_args.kwargs.get("timeout"), utils.PROBE_TIMEOUT)

    def test_encoder_probe_passes_probe_timeout(self):
        from watermark.core import utils
        from watermark.core.engines import video

        video._ENCODER_CACHE = None      # 绕开缓存，确保真的走探测分支
        try:
            with mock.patch.object(video, "find_ffmpeg", return_value=pathlib.Path("ffmpeg")):
                with mock.patch.object(video.subprocess, "check_output",
                                       return_value="h264_nvenc libx264") as fake:
                    video._pick_encoders({"video_encoder": "auto"})
            self.assertEqual(fake.call_args.kwargs.get("timeout"), utils.PROBE_TIMEOUT)
        finally:
            video._ENCODER_CACHE = None

    def test_encode_loops_catch_subprocess_timeout(self):
        """两处编码器回退循环必须同时 catch 超时，否则超时直接炸掉整个任务。"""
        src = _read("watermark/core/engines/video.py")
        self.assertEqual(
            src.count("except (subprocess.CalledProcessError, SubprocessTimeout) as exc:"), 2)

    def test_short_err_surfaces_timeout_text(self):
        from watermark.core.engines.video import _short_err
        from watermark.core.utils import SubprocessTimeout

        msg = _short_err(SubprocessTimeout(["D:/tools/ffmpeg/bin/ffmpeg.exe"], 3600))
        self.assertIn("ffmpeg.exe", msg)
        self.assertIn("1 小时", msg)

    def test_toolbox_ffmpeg_calls_have_timeout(self):
        src = _read("server/services.py")
        self.assertEqual(src.count("timeout=ENCODE_TIMEOUT"), 2)
        self.assertEqual(src.count("except subprocess.TimeoutExpired:"), 2)
        self.assertIn("from watermark.core.utils import (ENCODE_TIMEOUT", src)

    def test_no_ffmpeg_call_left_without_timeout(self):
        """回归闸门：音频/视频工具里的 ffmpeg 调用都得带 timeout。"""
        offenders = []
        for rel in ("watermark/core/utils.py", "watermark/core/engines/video.py",
                    "server/services.py"):
            src = _read(rel)
            for m in re.finditer(r"subprocess\.(run|check_output|call)\(", src):
                # 手工配对括号，取整段调用（正则 [^)]* 会被 no_window_kwargs() 截断）
                i = m.end() - 1
                depth = 0
                for j in range(i, min(len(src), i + 1200)):
                    if src[j] == "(":
                        depth += 1
                    elif src[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                body = src[i:j + 1]
                if "timeout" in body:
                    continue
                offenders.append(f"{rel}: {body[:70]!r}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
