"""「刷新昵称」修复的回归测试（2026-10-12，用户反馈：昵称刷新了还是不出来）。

用户原话：「为什么昵称刷新了还是不出来？」

先看探针证明的事实（`probe_refresh_api.py`，2026-10-12）：
    刷新前 kiahwilhelm75 display_name='' missing=1
    POST /api/collector/accounts/refresh-nicknames → job_id=6d1bb178ee83
    SSE：`[log] @kiahwilhelm75 → Kiro`
    刷新后 kiahwilhelm75 display_name='Kiro' missing=0
也就是说**取得到**，但两件事让用户看不到结果：

1. 内核 `ProfileCollector.fetch_nickname()` 原来是「导航完固定等 4 秒，再扫一遍
   响应」。实测单账号 9.3 秒（含拉起浏览器），首屏晚到就返回空串，而且没有
   第二次机会。
2. 后端 `_nickname_worker` 的每账号结果只走 `job.log()`（SSE），不写文件日志；
   前端 `watchJob({log: null})` 又把 SSE 日志丢掉，完成提示还写死
   「账号昵称已更新」——失败彻底不可见。

本文件锁四件事（每条都能被反向补丁打红）：
A. 内核：响应已到就**一次都不等**；6.5 秒才到的也等得到（用户那次就是这么丢的）；
   等待有界；接口始终不回时用主页 DOM 兜底，且只认「确实是一张正常主页」。
B. 后端：每个账号的结果真的写进 `tk.collector_api` 日志；结尾报**真正取到**的数量。
C. 前端：每账号日志接进界面（`#ac-nick-log`）；有失败就不说「已更新」，
   并把原因留在页面上。
D. 契约：服务端那句「成功刷新 x/y 个昵称」与前端解析用的正则配套。

放在 collector/tests 的原因同其它用例：这是唯一会被 `unittest discover`
跑到的目录；这里只用假的 page/provider/parser 与源码文本，无副作用。
"""
import unittest

NICK = "Kiro"
ACCOUNT = "kiahwilhelm75"


# --------------------------------------------------------------------------
#  假的浏览器环境：只实现 fetch_nickname 会用到的那几个方法
# --------------------------------------------------------------------------
class _Clock:
    """假时钟（毫秒推进）。"""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def advance(self, ms: float) -> None:
        self.t += ms / 1000.0


class _TimeStub:
    """只暴露 monotonic() —— 用它替换 profile_collector.time。

    不能直接改 stdlib 的 time.monotonic：那会影响同进程里所有代码。
    模块属性替换是安全的，测完在 addCleanup 里还原。
    """

    def __init__(self, clock: _Clock):
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.t


class _FakeLocator:
    def __init__(self, texts):
        self._texts = list(texts)

    @property
    def first(self):
        return self

    def count(self) -> int:
        return len(self._texts)

    def text_content(self):
        return self._texts[0] if self._texts else None


class _FakeResponse:
    def __init__(self, body: str):
        self.url = "https://www.tiktok.com/api/post/item_list/?count=35"
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self.status = 200
        self._body = body

    def text(self) -> str:
        return self._body


class _FakePage:
    """假页面：wait_for_timeout 推进假时钟，到点把 item_list 响应交给回调。"""

    def __init__(self, clock: _Clock, *, body=None, deliver_after_ms=None, dom=None):
        self.clock = clock
        self.waits = []
        self.closed = False
        self._body = body
        self._deliver_after_ms = deliver_after_ms
        self._delivered = False
        self._dom = dom or {}
        self._on_response = None
        self._t0 = clock.t

    def set_on_response(self, fn) -> None:
        self._on_response = fn

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)
        self.clock.advance(ms)
        if self._delivered or self._body is None or self._deliver_after_ms is None:
            return
        if self.clock.t - self._t0 >= self._deliver_after_ms / 1000.0:
            self._delivered = True
            self._on_response(_FakeResponse(self._body))

    def locator(self, selector):
        return self._dom.get(selector, _FakeLocator([]))

    def close(self) -> None:
        self.closed = True


class _BoomPage(_FakePage):
    """locator 直接抛异常 —— 模拟「页面已经关了 / 选择器不支持」。"""

    def locator(self, selector):
        raise RuntimeError("target closed")


class _FakeContext:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts = []          # [(user_data_dir, context)]

    def new_context(self, user_data_dir):
        ctx = _FakeContext()
        self.contexts.append((user_data_dir, ctx))
        return ctx


class _FakeSession:
    def __init__(self):
        self.logins = []

    def user_data_dir_for(self, account):
        return f"/tmp/sessions/{account.account_id}"

    def ensure_login(self, page):
        self.logins.append(page)


class _FakeProvider:
    def __init__(self, page, *, body=None, deliver_on_load=False):
        self.page = page
        self.body = body
        self.deliver_on_load = deliver_on_load

    def load_profile(self, context, username, on_response=None):
        self.page.set_on_response(on_response)
        if self.deliver_on_load and self.body is not None:
            # 首屏响应比任何 wait 都早 —— 旧代码仍要白等 4 秒
            on_response(_FakeResponse(self.body))
        return self.page


def _body(nickname: str = NICK, username: str = ACCOUNT) -> str:
    """真实结构的 item_list 响应体（author.nickname 是本功能唯一的昵称来源）。"""
    from collector.tests.fixtures import make_item, make_item_list

    item = make_item("7689051423832575252", 1790246798, username=username)
    item["author"]["nickname"] = nickname
    return make_item_list([item])


# --------------------------------------------------------------------------
#  A. 内核：有界轮询 + DOM 兜底
# --------------------------------------------------------------------------
class TestNicknameKernel(unittest.TestCase):
    def setUp(self):
        import collector.collect.profile_collector as pc

        self.pc = pc
        self.clock = _Clock()
        original = pc.time
        pc.time = _TimeStub(self.clock)
        self.addCleanup(setattr, pc, "time", original)

    def _run(self, page, *, nickname_wait_ms=10000, body=None, deliver_on_load=False):
        from collector.collect.profile_collector import ProfileCollector
        from collector.collect.video_parser import VideoParser
        from collector.core.models import Account

        browser, session = _FakeBrowser(), _FakeSession()
        collector = ProfileCollector(
            browser, session,
            _FakeProvider(page, body=body, deliver_on_load=deliver_on_load),
            VideoParser(), nickname_wait_ms=nickname_wait_ms)
        account = Account(account_id=ACCOUNT, username=ACCOUNT,
                          profile_url=f"https://www.tiktok.com/@{ACCOUNT}")
        return collector.fetch_nickname(account), browser, session

    def _dom(self, title=None, user=ACCOUNT):
        """按内核自己的选择器常量搭 DOM 桩（选择器本身另有断言钉住）。"""
        dom = {}
        if title is not None:
            dom[self.pc.ProfileCollector._DOM_TITLE_SELECTOR] = _FakeLocator([title])
        if user is not None:
            dom[self.pc.ProfileCollector._DOM_USER_SELECTOR] = _FakeLocator([user])
        return dom

    # ---- 不再白等 -------------------------------------------------------
    def test_returns_at_once_when_the_response_already_landed(self):
        page = _FakePage(self.clock)
        nick, _, _ = self._run(page, body=_body(), deliver_on_load=True)
        self.assertEqual(nick, NICK)
        self.assertEqual(page.waits, [], "响应已经到手，不该再等固定 4 秒")
        self.assertEqual(self.clock.t, 1000.0)

    def test_a_late_response_is_still_waited_for(self):
        """用户那次失败：首屏 6.5 秒才回来，旧的『固定等 4 秒』直接返回空串。"""
        page = _FakePage(self.clock, body=_body(), deliver_after_ms=6500)
        nick, _, _ = self._run(page, body=_body(), nickname_wait_ms=10000)
        self.assertEqual(nick, NICK)
        self.assertGreater(sum(page.waits), 4000, "旧实现等不到 4 秒之后就放弃了")
        self.assertLessEqual(sum(page.waits), 10000, "等待必须是有界的")

    def test_gives_up_within_the_budget(self):
        page = _FakePage(self.clock)                 # 接口始终不回，DOM 也没有
        nick, _, _ = self._run(page, nickname_wait_ms=1000)
        self.assertEqual(nick, "")
        self.assertEqual(sum(page.waits), 1000)
        self.assertTrue(all(w == 250 for w in page.waits), "轮询粒度应为 250ms")

    def test_zero_budget_does_not_wait_at_all(self):
        page = _FakePage(self.clock, dom=self._dom(title=NICK))
        nick, _, _ = self._run(page, nickname_wait_ms=0)
        self.assertEqual(nick, NICK)
        self.assertEqual(page.waits, [])

    # ---- DOM 兜底 -------------------------------------------------------
    def test_dom_fallback_reads_the_profile_title(self):
        page = _FakePage(self.clock, dom=self._dom(title=NICK))
        nick, _, _ = self._run(page, nickname_wait_ms=0)
        self.assertEqual(nick, NICK)

    def test_dom_fallback_requires_a_normal_profile_page(self):
        """没有用户名副标题 = 不是正常主页（错误页/不存在），不能把提示当昵称。"""
        page = _FakePage(self.clock, dom=self._dom(title="出错了", user=None))
        nick, _, _ = self._run(page, nickname_wait_ms=0)
        self.assertEqual(nick, "")

    def test_dom_fallback_rejects_absurd_titles(self):
        long_page = _FakePage(self.clock, dom=self._dom(title="x" * 65))
        self.assertEqual(self._run(long_page, nickname_wait_ms=0)[0], "")
        edge_page = _FakePage(self.clock, dom=self._dom(title="x" * 64))
        self.assertEqual(self._run(edge_page, nickname_wait_ms=0)[0], "x" * 64)

    def test_dom_errors_are_swallowed(self):
        page = _BoomPage(self.clock, dom=self._dom(title=NICK))
        nick, _, _ = self._run(page, nickname_wait_ms=0)   # 不该抛出去
        self.assertEqual(nick, "")

    def test_dom_selectors_are_the_tiktok_ones(self):
        pc = self.pc.ProfileCollector
        self.assertEqual(pc._DOM_TITLE_SELECTOR, 'h1[data-e2e="user-title"]')
        self.assertEqual(pc._DOM_USER_SELECTOR, 'h2[data-e2e="user-subtitle"]')

    # ---- 资源回收 -------------------------------------------------------
    def test_context_and_page_are_closed_even_when_nothing_came_back(self):
        page = _FakePage(self.clock)
        nick, browser, session = self._run(page, nickname_wait_ms=0)
        self.assertEqual(nick, "")
        self.assertTrue(page.closed)
        self.assertEqual(session.logins, [page])
        self.assertEqual(len(browser.contexts), 1)
        self.assertTrue(browser.contexts[0][1].closed)


# --------------------------------------------------------------------------
#  B. 后端 worker：结果落日志 + 数量只说真的
# --------------------------------------------------------------------------
class _DbStub:
    def __init__(self, unchanged=()):
        self.unchanged = set(unchanged)
        self.calls = []

    def set_account_display_name(self, account_id, display_name):
        self.calls.append((account_id, display_name))
        return account_id not in self.unchanged


class _CollectorStub:
    def __init__(self, results):
        self.results = results

    def fetch_nickname(self, account):
        value = self.results.get(account.account_id, "")
        if isinstance(value, Exception):
            raise value
        return value


class _CtxStub:
    def __init__(self, db, collector):
        self.db = db
        self._collector = collector

    def build_collector(self):
        return self._collector


class TestNicknameWorker(unittest.TestCase):
    def _accounts(self, *ids):
        from collector.core.models import Account

        return [Account(account_id=i, username=i,
                        profile_url=f"https://www.tiktok.com/@{i}") for i in ids]

    def _run(self, results, *, ids=("a1", "a2", "a3"), unchanged=(), cancelled=False):
        from server import collector_api
        from server.jobs import Job
        from server.state import JobKind

        db = _DbStub(unchanged)
        original = collector_api.ctx
        collector_api.ctx = lambda: _CtxStub(db, _CollectorStub(results))
        self.addCleanup(setattr, collector_api, "ctx", original)

        job = Job("t-nick", JobKind.COLLECT, label="刷新昵称 · 测试")
        if cancelled:
            job.cancel_job()
        collector_api._nickname_worker(job, self._accounts(*ids))
        return job, db

    def test_summary_counts_only_the_nicknames_that_really_came_back(self):
        from server.state import JobState

        job, db = self._run({"a1": NICK, "a2": "", "a3": RuntimeError("boom")})
        self.assertEqual(job.snapshot.state, JobState.DONE)
        self.assertEqual(job.snapshot.message,
                         "成功刷新 1/3 个昵称（2 个没取到，详见日志）")
        self.assertEqual(job.snapshot.values["ok"], 1)
        self.assertEqual(job.snapshot.values["missed"], 2)
        self.assertEqual(db.calls, [("a1", NICK)])

    def test_every_miss_is_written_to_the_app_log(self):
        with self.assertLogs("tk.collector_api", level="WARNING") as cm:
            self._run({"a1": NICK, "a2": "", "a3": RuntimeError("boom")})
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("@a2", text)          # 没取到
        self.assertIn("@a3", text)          # 抛异常
        self.assertIn("boom", text)

    def test_a_success_is_written_to_the_app_log_too(self):
        with self.assertLogs("tk.collector_api", level="INFO") as cm:
            self._run({"a1": NICK}, ids=("a1",))
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("a1", text)
        self.assertIn(NICK, text)

    def test_unchanged_nickname_is_flagged_but_still_counts(self):
        from server.state import EventKind

        job, _ = self._run({"a1": NICK}, ids=("a1",), unchanged=("a1",))
        self.assertEqual(job.snapshot.message, "成功刷新 1/1 个昵称")
        self.assertEqual(job.snapshot.values["ok"], 1)
        logs = [e.message for e in job.events_since(0) if e.kind == EventKind.LOG]
        self.assertTrue(any("（昵称未变化）" in m for m in logs), logs)

    def test_miss_events_carry_the_warn_level(self):
        from server.state import EventKind

        job, _ = self._run({"a1": NICK, "a2": ""}, ids=("a1", "a2"))
        warns = [e for e in job.events_since(0)
                 if e.kind == EventKind.LOG and e.level == "warn"]
        self.assertTrue(any("未取到昵称" in e.message for e in warns), warns)

    def test_cancel_stops_before_any_write(self):
        from server.state import JobState

        job, db = self._run({"a1": NICK}, ids=("a1",), cancelled=True)
        self.assertEqual(job.snapshot.state, JobState.CANCELLED)
        self.assertEqual(db.calls, [])

    def test_refresh_is_refused_while_a_collect_job_runs(self):
        from fastapi import HTTPException
        from server import collector_api

        original = collector_api.STORE.find_active
        collector_api.STORE.find_active = lambda kind: object()
        self.addCleanup(setattr, collector_api.STORE, "find_active", original)
        with self.assertRaises(HTTPException) as ctx:
            collector_api.refresh_nicknames({"account_ids": ["a1"]})
        self.assertEqual(ctx.exception.status_code, 409)


# --------------------------------------------------------------------------
#  C. 前端：每账号日志进界面 + 失败不再说「已更新」
# --------------------------------------------------------------------------
class TestRefreshFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from common import paths

        root = paths.APP_ROOT / "_app"
        cls.col = (root / "frontend" / "js" / "collector.js").read_text(encoding="utf-8")
        cls.html = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    def _fn(self):
        """只取 refreshNicknames() 的函数体 —— 断言作用域越小越不容易假绿。"""
        start = self.col.index("async function refreshNicknames(")
        end = self.col.index("\n  async function ", start)
        return self.col[start:end]

    def test_the_per_account_log_is_shown_in_the_page(self):
        fn = self._fn()
        self.assertIn('log: "ac-nick-log"', fn)
        self.assertNotIn("log: null", fn)
        self.assertIn('id="ac-nick-log"', self.html)

    def test_the_result_is_parsed_out_of_the_summary_sentence(self):
        self.assertIn(r"/(\d+)\s*\/\s*(\d+)/.exec(msg)", self._fn())

    def test_a_partial_result_is_not_reported_as_success(self):
        fn = self._fn()
        self.assertIn('notifySystem("昵称刷新完成", msg || "账号昵称已更新", '
                      'bad ? "warn" : "ok")', fn)
        self.assertNotIn('"账号昵称已更新", "ok")', fn)      # 旧的写死成功

    def test_failure_and_cancel_have_their_own_notice(self):
        fn = self._fn()
        self.assertIn('notifySystem("昵称刷新失败", msg || "任务失败", "error")', fn)
        self.assertIn('notifySystem("昵称刷新已取消", msg || "已停止", "warn")', fn)

    def test_a_partial_result_stays_on_the_page(self):
        fn = self._fn()
        self.assertIn("个账号没取到昵称", fn)
        self.assertIn("tip.innerHTML = `刷新昵称失败：<strong>${esc(e.message)}</strong>`;",
                      fn)

    def test_the_hint_no_longer_promises_three_to_five_seconds(self):
        self.assertIn("6～10 秒", self.col)
        self.assertNotIn("3～5 秒", self.col)


class TestSummaryContract(unittest.TestCase):
    """服务端那句话就是前端解析的输入 —— 两边必须配套。"""

    def test_server_summary_matches_the_frontend_regex(self):
        from common import paths

        api_src = (paths.APP_ROOT / "_app" / "server" / "collector_api.py").read_text(
            encoding="utf-8")
        self.assertIn('summary = f"成功刷新 {ok}/{len(accounts)} 个昵称"', api_src)
        col = (paths.APP_ROOT / "_app" / "frontend" / "js" / "collector.js").read_text(
            encoding="utf-8")
        self.assertIn(r"/(\d+)\s*\/\s*(\d+)/", col)


if __name__ == "__main__":
    unittest.main()
