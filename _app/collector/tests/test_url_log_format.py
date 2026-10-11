"""日志里的链接必须看得见作品 id（v1.4.3）。

2026-10-10 的真实日志把一条 61 字符的标准链接按前 60 字符截断：

    https://www.tiktok.com/@pyone.pay42/video/7693929770291105032
                                                        └─ 被切掉的就是 id 末位

于是日志里写着 `…/video/769392977029110503`，看起来像「采集到的 id 少了一位」，
实际请求发的是完整链接（只是撞上了 tikwm 的 1 req/s 限流）。这类「日志看起来
像数据坏了」的假故障最费排查时间，所以：

1. `short_url()` 缩略时**保留结尾**（作品 id 在结尾）；
2. 顺带补上 tikwm 时间槽的第二个漏洞 —— 只在发送前预约挡不住「预约了但发送
   被拖慢」，请求返回后再压一次时间槽，保证相邻请求按**完成时刻**间隔。
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from watermark.tiktok import tikwm
from watermark.tiktok.urls import short_url

FULL_URL = "https://www.tiktok.com/@pyone.pay42/video/7693929770291105032"
VIDEO_ID = "7693929770291105032"


class TestShortUrl(unittest.TestCase):
    def test_keeps_the_video_id(self):
        """真实踩坑的这条链接：缩略后必须还能看见完整的 id。"""
        self.assertEqual(len(FULL_URL), 61)          # 正好比 60 多一个字符
        got = short_url(FULL_URL)
        self.assertTrue(got.endswith(VIDEO_ID), got)
        self.assertLessEqual(len(got), 64)
        self.assertEqual(got, FULL_URL, "61 字符的链接在 64 预算内应完整保留")

    def test_old_style_would_have_hidden_it(self):
        """对照：老写法确实会切掉 id 末位（这就是当时的日志表现）。"""
        self.assertNotIn(VIDEO_ID, FULL_URL[:60])
        self.assertEqual(FULL_URL[:60].endswith(VIDEO_ID[:-1]), True)

    def test_short_url_untouched(self):
        self.assertEqual(short_url("https://v.douyin.com/abc/"),
                         "https://v.douyin.com/abc/")

    def test_empty_and_none(self):
        self.assertEqual(short_url(""), "")
        self.assertEqual(short_url(None), "")

    def test_budget_is_respected_and_tiny_budget_still_readable(self):
        long_url = "https://www.tiktok.com/@x/video/" + "9" * 40
        got = short_url(long_url, 40)
        self.assertEqual(len(got), 40)
        self.assertTrue(got.startswith("..."))
        self.assertTrue(long_url.endswith(got[3:]), "省略的必须是开头，保留的必须是结尾")
        # 预算小于省略号长度时也要保留至少 8 个字符，不能只剩 "..."
        self.assertEqual(short_url("abcdefghijklmn", 5), "...ghijklmn")
        # 省略开头意味着只要预算够放下 id（19 位 + 省略号）就能看全
        self.assertTrue(short_url(FULL_URL, 24).endswith(VIDEO_ID))
        # 预算再小也只会切掉 id 的开头几位，不会把昵称那种模板文本留在日志里
        tiny = short_url(FULL_URL, 20)
        self.assertEqual(len(tiny), 20)
        self.assertTrue(tiny.endswith(VIDEO_ID[-17:]))


class _Resp:
    def __init__(self, status_code=500, text="boom"):
        self.status_code = status_code
        self.text = text

    def json(self):                                   # pragma: no cover
        return {}


class _Session:
    """记录每次 POST 的起止时刻，永远失败（走重试日志分支）。"""

    def __init__(self, sleep_s: float = 0.02):
        self.calls = []
        self.sleep_s = sleep_s

    def post(self, url, data=None, proxies=None, timeout=None):
        start = time.monotonic()
        time.sleep(self.sleep_s)
        end = time.monotonic()
        self.calls.append((start, end))
        return _Resp()


def _client(session, max_retries: int = 1) -> tikwm.TikwmClient:
    c = tikwm.TikwmClient.__new__(tikwm.TikwmClient)
    c.proxy_url = ""
    c.timeout = 1
    c.max_retries = max_retries
    c.hd_mode = True
    c._proxies = None
    c.session = session
    return c


class TestParseLogShowsId(unittest.TestCase):
    def setUp(self):
        self.sess = _Session()
        self.client = _client(self.sess)
        patch = mock.patch.object(tikwm, "resolve_short_url", lambda url, proxies=None: url)
        patch.start()
        self.addCleanup(patch.stop)

    def test_failure_log_contains_full_video_id(self):
        with self.assertLogs("tiktok_no_watermark", level="WARNING") as cap:
            with mock.patch.object(tikwm.time, "sleep", lambda _s: None):
                self.assertIsNone(self.client.parse(FULL_URL))
        text = "\n".join(cap.output)
        self.assertIn(VIDEO_ID, text)

    def test_giveup_log_contains_full_video_id(self):
        with self.assertLogs("tiktok_no_watermark", level="ERROR") as cap:
            with mock.patch.object(tikwm.time, "sleep", lambda _s: None):
                self.client.parse(FULL_URL)
        text = "\n".join(cap.output)
        self.assertIn(f"解析放弃 {FULL_URL}", text)
        self.assertIn(VIDEO_ID, text)


class TestThrottleCountsCompletion(unittest.TestCase):
    """相邻请求按「上一次完成」间隔，而不是只按「上一次发送」间隔。"""

    def setUp(self):
        self._old_interval = tikwm._MIN_INTERVAL
        self._old_last = tikwm._LAST_REQUEST_AT
        tikwm._MIN_INTERVAL = 0.25
        tikwm._LAST_REQUEST_AT = 0.0
        self.addCleanup(self._restore)

    def _restore(self):
        tikwm._MIN_INTERVAL = self._old_interval
        tikwm._LAST_REQUEST_AT = self._old_last

    def test_second_request_waits_after_first_completion(self):
        sess = _Session(sleep_s=0.05)
        client = _client(sess)
        with mock.patch.object(tikwm, "resolve_short_url", lambda url, proxies=None: url):
            with mock.patch.object(tikwm.time, "sleep", lambda _s: None):
                client.parse(FULL_URL)
                client.parse(FULL_URL)
        self.assertEqual(len(sess.calls), 2)
        first_end = sess.calls[0][1]
        second_start = sess.calls[1][0]
        self.assertGreaterEqual(second_start - first_end, tikwm._MIN_INTERVAL - 0.02,
                                f"两次请求完成/开始只隔 {second_start - first_end:.3f}s")

    def test_mark_request_done_never_moves_the_slot_backwards(self):
        tikwm._LAST_REQUEST_AT = time.monotonic() + 10          # 未来时刻
        before = tikwm._LAST_REQUEST_AT
        tikwm._mark_request_done()
        self.assertEqual(tikwm._LAST_REQUEST_AT, before,
                         "把时间槽往后挪会让下一次请求少等，是反效果")


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
