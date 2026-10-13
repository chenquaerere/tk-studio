"""TikTok 拒绝返回作品列表：识别、退避重试、以及「一条都没采到就绝不丢记录」（v1.4.7）。

背景（用户实测 2026-10-12）：
    用户报「采集 15 个账号、每个 3 条，最终只有 12 条」。现场探针（真浏览器）证明
    失败账号主页 URL 正常、无登录弹窗、无验证码，但 `/api/post/item_list/` 只回
    `200 + 0 字节` 或 `200 + 333 字节的 TotalCount:0`（**没有 itemList 键**），
    页面渲染 TikTok 自己的错误文案「Something went wrong / Sorry about that!」。
    此前这种「一个作品都没有」被判成 `empty`（今日没有作品），而 v1.4.3 的回滚只在
    `failed` 时生效 —— 于是「先清后采」把 11 个账号的旧记录清成了空白。

修法（两层，缺一不可）：
    1. 采集器认出「站点拒绝」→ 抛 SiteBlocked（新增），而不是安静地返回 0 条；
    2. 引擎对 SiteBlocked 退避重试（3s→6s→12s），重试用尽判 failed
       → 已有的「失败且零条 → 回滚保留旧记录」分支自然生效。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from collector.collect.engine import CollectEngine
from collector.collect.exceptions import LoginRequired, RateLimited, SiteBlocked
from collector.collect.profile_collector import ProfileCollector
from collector.collect.video_parser import VideoParser
from collector.core.date_resolver import DateResolver
from collector.core.models import Account, ParsedVideoItem, Video
from collector.db.database import Database

TARGET = "2026-09-24"
PC_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "collect", "profile_collector.py")
ENGINE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "collect", "engine.py")

# ---- 三种真实响应形态（取自 2026-10-12 现场探针） ----
OK_BODY = ('{"itemList":[{"id":"7100000000000000001","desc":"a","createTime":1786000000,'
           '"author":{"uniqueId":"demo","nickname":"Kiro"}},'
           '{"id":"7100000000000000002","desc":"b","createTime":1785900000,'
           '"author":{"uniqueId":"demo","nickname":"Kiro"}}],'
           '"HasMoreAfter":false,"TotalCount":"2"}')
EMPTY_LIST_BODY = '{"itemList":[],"HasMoreAfter":false,"TotalCount":"0"}'
REFUSAL_BODY = ('{"AllViewed":false,"CurrentPosition":"0","HasMoreAfter":false,'
                '"HasMoreBefore":false,"MaxCursor":"0","MinCursor":"0",'
                '"TotalCount":"0","extra":{}}')
PAGE_ERROR_TEXT = "Something went wrong\nSorry about that! Please try again later.\nRefresh"
NORMAL_PAGE_TEXT = "demo\n0 Following\n0 Followers\n"


# ---------- 采集器内核用的假件 ----------
class _Resp:
    def __init__(self, body, status=200,
                 url="https://www.tiktok.com/api/post/item_list/?count=30",
                 ctype="application/json"):
        self.url = url
        self.status = status
        self._body = body
        self.headers = {"content-type": ctype}

    def text(self):
        return self._body


class _Mouse:
    def __init__(self):
        self.wheels = []

    def wheel(self, x, y):
        self.wheels.append((x, y))


class _Page:
    def __init__(self, text=NORMAL_PAGE_TEXT, evaluate_boom=False):
        self.waits = []
        self.mouse = _Mouse()
        self.closed = False
        self.evaluate_calls = 0
        self._text = text
        self._boom = evaluate_boom

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate(self, expr):
        self.evaluate_calls += 1
        if self._boom:
            raise RuntimeError("页面已关闭")
        return self._text

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, directory):
        self.directory = directory
        self.closed = False

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self):
        self.contexts = []

    def new_context(self, directory):
        ctx = _Context(directory)
        self.contexts.append(ctx)
        return ctx


class _Session:
    def __init__(self, login_exc=None):
        self.login_exc = login_exc

    def user_data_dir_for(self, account):
        return os.path.join(tempfile.gettempdir(), "tk-sessions", account.username)

    def ensure_login(self, page):
        if self.login_exc is not None:
            raise self.login_exc


class _Provider:
    """导航时按脚本投递响应（真实代码是在导航前注册 on_response）。"""

    def __init__(self, responses, page_text=NORMAL_PAGE_TEXT, evaluate_boom=False):
        self.responses = responses
        self.page = _Page(page_text, evaluate_boom)
        self.loaded = []

    def load_profile(self, context, username, on_response=None):
        self.loaded.append(username)
        for resp in self.responses:
            if on_response is not None:
                on_response(resp)
        return self.page


class _ListParser(VideoParser):
    """可指定列表键名的解析器（验证拒绝判定跟随 parser.item_list_keys）。"""

    def __init__(self, keys):
        super().__init__()
        self.item_list_keys = keys


def _acc(aid="a1", uname="demo_alpha", count=3):
    return Account(account_id=aid, username=uname,
                   profile_url=f"https://www.tiktok.com/@{uname}",
                   collect_count=count)


def _collector(responses, page_text=NORMAL_PAGE_TEXT, parser=None,
               evaluate_boom=False, session=None):
    browser = _Browser()
    provider = _Provider(responses, page_text, evaluate_boom)
    session = session or _Session()
    pc = ProfileCollector(browser, session, provider, parser or VideoParser(),
                          initial_wait_ms=0, scroll_wait_ms=0,
                          max_scrolls=2, idle_scrolls_before_stop=1)
    return pc, provider, browser


class TestSiteBlockedKernel(unittest.TestCase):
    """采集器层：认出「站点拒绝」而不是安静地返回 0 条。"""

    # ---- 现场形态 1：200 + 0 字节 ----
    def test_empty_body_response_raises_site_blocked(self):
        pc, provider, browser = _collector([_Resp("")])
        with self.assertRaises(SiteBlocked):
            pc.collect(_acc(), lambda item: True)
        self.assertTrue(provider.page.closed, "抛异常也要关页面")
        self.assertTrue(browser.contexts[0].closed, "抛异常也要关 context")

    # ---- 现场形态 2：200 + 没有 itemList 键的 TotalCount:0 ----
    def test_totalcount_only_response_raises_site_blocked(self):
        pc, _, _ = _collector([_Resp(REFUSAL_BODY)])
        with self.assertRaises(SiteBlocked) as cm:
            pc.collect(_acc(), lambda item: True)
        self.assertIn("未返回作品列表", str(cm.exception))

    # ---- 现场形态 3：压根没收到 item_list 响应 ----
    def test_no_item_list_response_at_all_raises_site_blocked(self):
        pc, _, _ = _collector([])
        with self.assertRaises(SiteBlocked):
            pc.collect(_acc(), lambda item: True)

    # ---- 页面自己报错：即使响应「看起来是空列表」也算拒绝 ----
    def test_page_error_text_raises_even_with_empty_list(self):
        pc, _, _ = _collector([_Resp(EMPTY_LIST_BODY)], page_text=PAGE_ERROR_TEXT)
        with self.assertRaises(SiteBlocked):
            pc.collect(_acc(), lambda item: True)

    # ---- 反向：真没作品（正常空列表 + 正常页面）不算拒绝 ----
    def test_genuinely_empty_profile_is_not_blocked(self):
        pc, _, _ = _collector([_Resp(EMPTY_LIST_BODY)])
        diag = pc.collect(_acc(), lambda item: True)
        self.assertEqual(diag["found"], 0)
        self.assertEqual(diag["item_list_responses"], 1)
        self.assertEqual(diag["item_list_refused"], 0)
        self.assertEqual(diag["item_list_empty"], 0)
        self.assertFalse(diag["page_error"])

    # ---- 反向：正常主页照常返回作品 ----
    def test_normal_profile_returns_items(self):
        pc, _, _ = _collector([_Resp(OK_BODY)])
        got = []
        diag = pc.collect(_acc(), lambda item: got.append(item.video_id) or True)
        self.assertEqual(diag["found"], 2)
        self.assertEqual(got, ["7100000000000000001", "7100000000000000002"])
        self.assertEqual(diag["author_nickname"], "Kiro")
        self.assertEqual(diag["item_list_refused"], 0)

    # ---- 429 仍然是限流（有独立退避），不能被算成站点拒绝 ----
    def test_rate_limited_still_wins_over_site_blocked(self):
        pc, _, _ = _collector([_Resp("", status=429)])
        with self.assertRaises(RateLimited):
            pc.collect(_acc(), lambda item: True)

    def test_rate_limited_diag_not_treated_as_blocked(self):
        self.assertFalse(ProfileCollector._looks_blocked(
            {"rate_limited": True, "item_list_responses": 0}))

    # ---- 登录态失效优先于拒绝判定（重试无意义） ----
    def test_login_required_still_raised(self):
        pc, _, _ = _collector([_Resp("")], session=_Session(LoginRequired("登录态失效")))
        with self.assertRaises(LoginRequired):
            pc.collect(_acc(), lambda item: True)

    # ---- 拒绝判定的判定表 ----
    def test_looks_blocked_matrix(self):
        lb = ProfileCollector._looks_blocked
        self.assertTrue(lb({"page_error": True}))
        self.assertTrue(lb({"item_list_responses": 0}))
        self.assertTrue(lb({"item_list_responses": 2, "item_list_empty": 2}))
        self.assertTrue(lb({"item_list_responses": 2, "item_list_empty": 1,
                            "item_list_refused": 1}))
        self.assertTrue(lb({"item_list_responses": 1, "item_list_refused": 1}))
        self.assertFalse(lb({"item_list_responses": 1}))
        self.assertFalse(lb({"item_list_responses": 3, "item_list_empty": 1}))
        self.assertFalse(lb({"item_list_responses": 1, "item_list_empty": 1,
                             "rate_limited": True}))

    # ---- 页面取文本失败不能算「页面报错」 ----
    def test_page_error_false_when_evaluate_fails(self):
        pc, _, _ = _collector([], evaluate_boom=True)
        self.assertFalse(pc._page_error(_Page(evaluate_boom=True)))

    def test_page_error_is_case_insensitive(self):
        pc, _, _ = _collector([])
        self.assertTrue(pc._page_error(_Page("SORRY ABOUT THAT! try later")))

    # ---- 拒绝形状的键名跟随 parser（接口改名时不能误判） ----
    def test_refusal_keys_follow_parser(self):
        pc, _, _ = _collector([_Resp('{"videos":[],"TotalCount":"0"}')],
                              parser=_ListParser(("videos",)))
        diag = pc.collect(_acc(), lambda item: True)
        self.assertEqual(diag["item_list_refused"], 0, "videos 键是合法列表键")

    def test_unknown_key_body_is_refused(self):
        pc, _, _ = _collector([_Resp('{"videos":[],"TotalCount":"0"}')],
                              parser=_ListParser(("itemList",)))
        with self.assertRaises(SiteBlocked):
            pc.collect(_acc(), lambda item: True)

    # ---- 非空校验：拒绝检测必须在滚动循环之后、返回诊断之前 ----
    def test_source_order_after_scroll_loop(self):
        with open(PC_SRC, encoding="utf-8") as fh:
            src = fh.read()
        i_loop = src.index("for _ in range(self.max_scrolls):")
        i_rate = src.index('raise RateLimited("触发 TikTok 限流')
        i_block = src.index("raise SiteBlocked(")
        i_ret = src.index("            return diag\n", i_block)  # 本方法真正返回处
        self.assertLess(i_loop, i_rate, "拒绝/限流检测必须在滚动之后")
        self.assertLess(i_rate, i_block, "限流必须排在站点拒绝之前")
        self.assertLess(i_block, i_ret)


class BlockedCollector:
    """collect() 前 fail_times 次抛 SiteBlocked，之后正常返回。"""

    def __init__(self, fail_times=99, items_by_account=None):
        self.fail_times = fail_times
        self.items = items_by_account or {}
        self.calls = []
        self.seen = {}

    def collect(self, account, on_item):
        self.calls.append(account.username)
        n = self.seen.get(account.username, 0) + 1
        self.seen[account.username] = n
        if n <= self.fail_times:
            raise SiteBlocked("TikTok 未返回作品列表（空响应/页面报错，疑似风控或需要登录）")
        items = self.items.get(account.username, [])
        for it in items:
            if not on_item(it):
                break
        return {"found": len(items), "scrolls": 1, "item_list_requests": 1,
                "create_times": []}


class EmptyCollector:
    """collect() 正常返回但一条都没有（真没作品）。"""

    def collect(self, account, on_item):
        return {"found": 0, "scrolls": 1, "item_list_requests": 1, "create_times": []}


class _OldVideo:
    @staticmethod
    def make(aid, uname, vid, date="2026-09-20"):
        return Video(
            video_id=vid, account_id=aid, username=uname,
            video_url=f"https://www.tiktok.com/@{uname}/video/{vid}",
            publish_time=f"{date} 09:00:00", publish_date=date,
            first_collect_time="2026-09-20 10:00:00", first_task_id="task-old",
            status="ok",
        )


class TestSiteBlockedEngine(unittest.TestCase):
    """引擎层：退避重试 + 重试用尽判失败 + 旧记录回滚。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))
        self.resolver = DateResolver("Asia/Shanghai")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _engine(self, collector, **kw):
        eng = CollectEngine(self.db, collector, self.resolver,
                            account_interval_seconds=0.0, **kw)
        self.sleeps = []
        eng._interruptible_sleep = lambda seconds: self.sleeps.append(seconds)
        return eng

    def _seed(self, aid, uname, n):
        self.db.insert_account(_acc(aid, uname))
        self.db.insert_task("task-old", target_date="2026-09-20", status="completed")
        for i in range(n):
            self.db.insert_video(_OldVideo.make(aid, uname, f"{aid}-v{i}"))

    def _ids(self, aid):
        return sorted(r["video_id"] for r in self.db.query(
            "SELECT video_id FROM videos WHERE account_id = ?", (aid,)))

    # ---- 核心修复：被拒绝 → failed → 旧记录回滚保留 ----
    def test_blocked_account_keeps_history(self):
        self._seed("a1", "demo_alpha", 3)
        eng = self._engine(BlockedCollector(), max_retry=1)
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["blocked"], 1)
        self.assertEqual(s["empty"], 0, "拒绝不能被算成「今日没有作品」")
        self.assertEqual(self._ids("a1"), ["a1-v0", "a1-v1", "a1-v2"],
                         "被拒绝时旧记录必须原样保留")
        self.assertEqual(s["restored"], 3)
        self.assertIn("回滚", s["accounts"][0].error_reason)

    def test_blocked_flag_reaches_diagnostics(self):
        self._seed("a1", "demo_alpha", 1)
        eng = self._engine(BlockedCollector(), max_retry=1)
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)
        self.assertTrue(s["accounts"][0].diagnostics.get("site_blocked"))
        self.assertIn("未返回作品列表", s["accounts"][0].error_reason)

    # ---- 重试确实有用：第 2 次成功后不判失败、不回滚 ----
    def test_retry_recovers_and_does_not_rollback(self):
        self._seed("a1", "demo_alpha", 2)
        items = [ParsedVideoItem(video_id="7100000000000000009", username="demo_alpha",
                                 raw_url="https://www.tiktok.com/@demo_alpha/video/7100000000000000009",
                                 raw_publish_time=None, time_source="none")]
        eng = self._engine(BlockedCollector(fail_times=1, items_by_account={
            "demo_alpha": items}), max_retry=3)
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        self.assertEqual(s["failed"], 0)
        self.assertEqual(s["blocked"], 0)
        self.assertEqual(s["restored"], 0)
        self.assertEqual(self.sleeps, [3.0], "只退避一次（3 秒）")
        self.assertEqual(self._ids("a1"), ["7100000000000000009"])

    # ---- 退避节奏可打断、有上限，且不在最后一次尝试上空等 ----
    def test_blocked_backoff_sequence(self):
        eng = self._engine(BlockedCollector())
        for _ in range(5):
            eng._blocked_backoff()
        self.assertEqual(self.sleeps, [3.0, 6.0, 12.0, 15.0, 15.0])

    def test_no_sleep_after_last_attempt(self):
        self._seed("a1", "demo_alpha", 1)
        eng = self._engine(BlockedCollector(), max_retry=3)
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(self.sleeps, [3.0, 6.0], "最后一次尝试不再空等")

    def test_backoff_level_is_shared_across_accounts(self):
        self._seed("a1", "demo_alpha", 1)
        self._seed("a2", "demo_beta", 1)
        eng = self._engine(BlockedCollector(), max_retry=2)
        eng.run([_acc("a1", "demo_alpha"), _acc("a2", "demo_beta")], TARGET)
        self.assertEqual(self.sleeps, [3.0, 6.0], "退避等级跨账号累积")

    # ---- 反向：真没作品仍然是「替换式记录」（不改变 v1.4.3 契约） ----
    def test_genuinely_empty_still_replaces_history(self):
        self._seed("a1", "demo_alpha", 3)
        eng = self._engine(EmptyCollector(), max_retry=1)
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        self.assertEqual(s["empty"], 1)
        self.assertEqual(s["blocked"], 0)
        self.assertEqual(s["restored"], 0)
        self.assertEqual(self._ids("a1"), [], "真没作品 → 旧记录按替换式规则清掉")

    # ---- 反向：登录态失效仍然熔断，且不算站点拒绝 ----
    def test_login_required_still_aborts_without_blocked_count(self):
        self._seed("a1", "demo_alpha", 2)
        self._seed("a2", "demo_beta", 1)

        class _LoginCollector:
            def __init__(self):
                self.calls = []

            def collect(self, account, on_item):
                self.calls.append(account.username)
                raise LoginRequired("登录态失效，请重新登录")

        collector = _LoginCollector()
        eng = self._engine(collector, max_retry=3)
        s = eng.run([_acc("a1", "demo_alpha"), _acc("a2", "demo_beta")], TARGET)

        self.assertEqual(collector.calls, ["demo_alpha"], "第二个账号不该再开浏览器")
        self.assertEqual(s["blocked"], 0)
        self.assertTrue(s["login_aborted"])
        self.assertEqual(self.sleeps, [], "登录失效不做站点退避")

    # ---- 有作品时不回滚（不能被新分支吃掉） ----
    def test_items_present_means_no_rollback(self):
        self._seed("a1", "demo_alpha", 2)
        items = [ParsedVideoItem(video_id="7100000000000000010", username="demo_alpha",
                                 raw_url="https://www.tiktok.com/@demo_alpha/video/7100000000000000010",
                                 raw_publish_time=None, time_source="none")]
        eng = self._engine(BlockedCollector(fail_times=0, items_by_account={
            "demo_alpha": items}), max_retry=1)
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)
        self.assertEqual(s["restored"], 0)
        self.assertEqual(self._ids("a1"), ["7100000000000000010"])

    # ---- 非空校验：SiteBlocked 分支必须在 Exception 兜底之前 ----
    def test_source_order_site_blocked_before_generic(self):
        with open(ENGINE_SRC, encoding="utf-8") as fh:
            src = fh.read()
        i_rate = src.index("except RateLimited as exc:")
        i_block = src.index("except SiteBlocked as exc:")
        i_any = src.index("except Exception as exc:  # noqa: BLE001 —— 单账号失败不得阻断整体")
        self.assertLess(i_rate, i_block)
        self.assertLess(i_block, i_any, "被兜底分支吞掉就永远不会重试")


class TestSummaryMentionsBlocked(unittest.TestCase):
    """结果文案：用户要能看出「被拒绝」和「记录没丢」。"""

    def _summary(self, s):
        try:
            from server.collector_api import _summary_text
        except Exception as exc:  # pragma: no cover - 环境缺依赖时跳过
            self.skipTest(f"无法导入 collector_api: {exc}")
        return _summary_text(s)

    def test_blocked_and_restored_lines(self):
        text = self._summary({"account_count": 15, "failed": 11, "empty": 0,
                              "new_videos": 0, "blocked": 11, "restored": 33})
        self.assertIn("11 个账号被 TikTok 拒绝返回作品列表", text)
        self.assertIn("已回滚保留 33 条旧记录", text)
        self.assertIn("本次未采到新作品，未丢失", text)

    def test_skipped_line(self):
        """被中止的账号也要在结果文案里看得见（v1.4.7：以前 skipped 不进计数器）。"""
        text = self._summary({"account_count": 15, "completed": 8, "skipped": 1,
                              "new_videos": 24})
        self.assertIn("中止 1", text)

    def test_no_blocked_line_when_clean(self):
        text = self._summary({"account_count": 15, "completed": 15, "new_videos": 45})
        self.assertNotIn("拒绝返回作品列表", text)
        self.assertNotIn("回滚", text)


if __name__ == "__main__":
    unittest.main()
