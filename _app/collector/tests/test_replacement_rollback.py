"""替换式记录的「后悔药」：登录态失效不再清空历史记录（v1.4.3）。

背景（用户实测风险）：
    替换式记录 = 先删该账号旧记录、再采新记录。登录态失效时每个账号都是
    「删得掉、采不回」—— `ensure_login` 在采集路径里抛 LoginRequired，
    旧记录已经被删除，新记录一条没有，账号变成空白。

修法（两层）：
    1. 删除前留快照；账号整体失败且一条都没采到 → 原样写回。
    2. 第一个账号确认登录失效 → 熔断：后面的账号直接跳过，根本不清旧记录。
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

from collector.collect.engine import CollectEngine
from collector.collect.exceptions import LoginRequired
from collector.core.date_resolver import DateResolver
from collector.core.models import Account, ParsedVideoItem, Video
from collector.db.database import Database

TARGET = "2026-09-24"
ENGINE_SRC = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "collect", "engine.py")


def _acc(aid, uname, count=4):
    return Account(account_id=aid, username=uname,
                   profile_url=f"https://www.tiktok.com/@{uname}",
                   collect_count=count)


def _old_video(aid, uname, vid, date="2026-09-20"):
    """一条「历史记录」：字段齐全，回滚后应当逐字段一致。"""
    return Video(
        video_id=vid, account_id=aid, username=uname,
        video_url=f"https://www.tiktok.com/@{uname}/video/{vid}",
        publish_time=f"{date} 09:00:00", publish_date=date,
        first_collect_time="2026-09-20 10:00:00", first_task_id="task-old",
        status="ok",
    )


def _item(video_id, username):
    """一条「刚采到」的作品（无发布时间 → 按未确认日期入库）。"""
    return ParsedVideoItem(
        video_id=video_id, username=username,
        raw_url=f"https://www.tiktok.com/@{username}/video/{video_id}",
        raw_publish_time=None, time_source="none")


class FailingCollector:
    """collect() 一律抛 LoginRequired —— 模拟登录态失效。"""

    def __init__(self, exc=None):
        self.calls = []
        self._exc = exc or LoginRequired("登录态失效，请重新登录")

    def collect(self, account, on_item):
        self.calls.append(account.username)
        raise self._exc


class OkCollector:
    """collect() 正常返回（用于对照：成功时旧记录确实被替换）。"""

    def __init__(self, items_by_account=None):
        self.calls = []
        self.items = items_by_account or {}

    def collect(self, account, on_item):
        self.calls.append(account.username)
        items = self.items.get(account.username, [])
        for it in items:
            if not on_item(it):
                break
        return {"found": len(items), "scrolls": 1, "item_list_requests": 1,
                "create_times": []}


class TestHelpers(unittest.TestCase):
    """database.snapshot_videos_for_account / restore_videos。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed(self, aid="a1", uname="demo_alpha", n=3):
        self.db.insert_account(_acc(aid, uname))
        # videos.first_task_id 有外键约束：历史记录所属任务必须先入库
        self.db.insert_task("task-old", target_date="2026-09-20", status="completed")
        for i in range(n):
            self.db.insert_video(_old_video(aid, uname, f"{aid}-v{i}"))

    def test_snapshot_carries_all_columns(self):
        self._seed(n=2)
        rows = self.db.snapshot_videos_for_account("a1")
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(
                set(r.keys()),
                {"video_id", "account_id", "username", "video_url", "publish_time",
                 "publish_date", "first_collect_time", "first_task_id", "status"})
        one = {r["video_id"]: r for r in rows}["a1-v0"]
        self.assertEqual(one["video_url"], "https://www.tiktok.com/@demo_alpha/video/a1-v0")
        self.assertEqual(one["first_task_id"], "task-old")

    def test_snapshot_unknown_account_is_empty(self):
        self.assertEqual(self.db.snapshot_videos_for_account("nope"), [])

    def test_restore_brings_rows_back_verbatim(self):
        self._seed(n=3)
        before = sorted(self.db.snapshot_videos_for_account("a1"),
                        key=lambda r: r["video_id"])
        removed = self.db.delete_videos_for_account("a1")
        self.assertEqual(removed, 3)
        self.assertEqual(self.db.count_videos(), 0)

        written = self.db.restore_videos(before)
        self.assertEqual(written, 3)
        after = sorted(self.db.snapshot_videos_for_account("a1"),
                       key=lambda r: r["video_id"])
        self.assertEqual(after, before)

    def test_restore_is_insert_or_ignore(self):
        self._seed(n=1)
        rows = self.db.snapshot_videos_for_account("a1")
        # 记录还在库里 → 写回应为 0（不重复、不报错）
        self.assertEqual(self.db.restore_videos(rows), 0)
        self.assertEqual(self.db.count_videos(), 1)

    def test_restore_empty_is_noop(self):
        self.assertEqual(self.db.restore_videos([]), 0)
        self.assertEqual(self.db.count_videos(), 0)


class TestEngineRollback(unittest.TestCase):
    """引擎层：失败回滚 + 登录失效熔断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))
        self.resolver = DateResolver("Asia/Shanghai")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _engine(self, collector, **kw):
        return CollectEngine(self.db, collector, self.resolver,
                             account_interval_seconds=0.0, **kw)

    def _seed(self, aid, uname, n):
        # videos 的 account_id / first_task_id 都有外键约束：两者必须先入库
        self.db.insert_account(_acc(aid, uname))
        self.db.insert_task("task-old", target_date="2026-09-20", status="completed")
        for i in range(n):
            self.db.insert_video(_old_video(aid, uname, f"{aid}-v{i}"))

    def _ids(self, aid):
        return sorted(r["video_id"] for r in
                      self.db.query("SELECT video_id FROM videos WHERE account_id = ?", (aid,)))

    # ---- 单账号：登录失效不清空 ----
    def test_login_failure_keeps_history(self):
        self._seed("a1", "demo_alpha", 3)
        eng = self._engine(FailingCollector())
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        self.assertEqual(s["failed"], 1)
        self.assertEqual(self._ids("a1"), ["a1-v0", "a1-v1", "a1-v2"], "旧记录必须原样保留")
        self.assertEqual(s["restored"], 3)
        self.assertTrue(s["login_aborted"])
        self.assertIn("回滚", s["accounts"][0].error_reason)

    # ---- 多账号：熔断，后面的账号连删都不删 ----
    def test_login_failure_aborts_remaining_accounts(self):
        self._seed("a1", "demo_alpha", 2)
        self._seed("a2", "demo_beta", 3)
        collector = FailingCollector()
        eng = self._engine(collector)
        s = eng.run([_acc("a1", "demo_alpha"), _acc("a2", "demo_beta")], TARGET)

        self.assertEqual(collector.calls, ["demo_alpha"], "第二个账号不该再开浏览器")
        self.assertEqual(self._ids("a1"), ["a1-v0", "a1-v1"])
        self.assertEqual(self._ids("a2"), ["a2-v0", "a2-v1", "a2-v2"],
                         "后面账号的旧记录不该被清除")
        self.assertEqual(s["skipped"], 1)
        rows = self.db.query("SELECT status, error_reason FROM task_accounts WHERE account_id = ?",
                             ("a2",))
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertIn("登录态失效", rows[0]["error_reason"])
        self.assertNotIn("回滚", rows[0]["error_reason"])

    # ---- 对照：采集成功仍然是「替换式记录」 ----
    def test_success_still_replaces_history(self):
        self._seed("a1", "demo_alpha", 3)
        eng = self._engine(OkCollector({"demo_alpha": []}))
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        self.assertEqual(s["restored"], 0)
        self.assertFalse(s["login_aborted"])
        # 账号主页没有作品（empty）→ 旧记录按替换式记录规则清掉，不回滚
        self.assertEqual(s["accounts"][0].status, "empty")
        self.assertEqual(self._ids("a1"), [])

    # ---- 快照拿不到时不能把采集搞崩 ----
    def test_snapshot_failure_does_not_break_collect(self):
        self._seed("a1", "demo_alpha", 1)
        eng = self._engine(OkCollector({"demo_alpha": []}))

        def boom(account_id):
            raise RuntimeError("快照失败")
        self.db.snapshot_videos_for_account = boom

        s = eng.run([_acc("a1", "demo_alpha")], TARGET)
        self.assertEqual(s["accounts"][0].status, "empty")
        self.assertEqual(s["restored"], 0)

    # ---- 非空校验：删除点必须紧挨快照与回滚分支 ----
    def test_source_order_snapshot_then_delete_then_restore(self):
        with open(ENGINE_SRC, encoding="utf-8") as fh:
            src = fh.read()
        # (a) 清除函数内部：先快照、后删除
        helper = src[src.index("def _clear_account_videos"):src.index("# ---- 单账号采集 ----")]
        i_snap = helper.index("snapshot = self.db.snapshot_videos_for_account")
        i_del = helper.index("removed = self.db.delete_videos_for_account")
        self.assertLess(i_snap, i_del, "必须先取快照再删除")
        # (b) 主循环里：熔断检查 → 清除（带快照）→ 回滚
        loop = src[src.index("for idx, acc in enumerate(accounts):"):src.index("finally:")]
        i_abort = loop.index("if self._abort_reason:")
        i_clear = loop.index("removed, snapshot = self._clear_account_videos(acc)")
        i_back = loop.index("back = self.db.restore_videos(snapshot)")
        self.assertLess(i_abort, i_clear, "熔断检查必须在任何清除动作之前")
        self.assertLess(i_clear, i_back, "回滚分支必须在删除之后")
        # 回滚只针对「失败且一条未采到」
        self.assertIn('result.status == "failed" and result.actual_count == 0', loop)


class TestSummaryTextMentionsRollback(unittest.TestCase):
    """结果文案要把「回滚保留」说出来，否则用户看不出记录没丢。"""

    def test_summary_text(self):
        try:
            from server.collector_api import _summary_text
        except Exception as exc:  # pragma: no cover - 环境缺依赖时跳过
            self.skipTest(f"无法导入 collector_api: {exc}")
        text = _summary_text({"account_count": 2, "completed": 1, "failed": 1,
                              "new_videos": 5, "restored": 3, "login_aborted": True})
        self.assertIn("已回滚保留 3 条旧记录", text)
        self.assertIn("登录态失效已提前停止", text)
        plain = _summary_text({"account_count": 1, "completed": 1, "new_videos": 4})
        self.assertNotIn("回滚", plain)
        self.assertNotIn("登录态失效", plain)

    def test_summary_text_mentions_kept_links(self):
        try:
            from server.collector_api import _summary_text
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"无法导入 collector_api: {exc}")
        text = _summary_text({"account_count": 1, "failed": 1, "new_videos": 0,
                              "links_kept": 7})
        self.assertIn("保留 7 条", text)


class TestDailyLinksNotTruncated(unittest.TestCase):
    """一次没采到链接的运行，不能把当日链接日志清空（v1.4.3）。

    `DailyLinksLog.replace([])` 是 "w" 打开 —— 空列表＝清空文件。
    登录态失效 / 全部失败 / 刚开跑就被停止都会走这条路径。
    """

    def setUp(self):
        from collector.log.daily_links import DailyLinksLog
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))
        self.resolver = DateResolver("Asia/Shanghai")
        self.links = DailyLinksLog(os.path.join(self.tmp.name, "links"))
        self.db.insert_task("task-old", target_date="2026-09-20", status="completed")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _engine(self, collector):
        return CollectEngine(self.db, collector, self.resolver,
                             account_interval_seconds=0.0,
                             daily_links=self.links)

    def test_empty_run_keeps_existing_link_log(self):
        self.links.replace(["https://www.tiktok.com/@a/video/1",
                            "https://www.tiktok.com/@a/video/2"], day=TARGET)
        eng = self._engine(FailingCollector())
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        self.assertEqual(len(self.links.load(day=TARGET)), 2, "当日链接日志不该被清空")
        self.assertEqual(s["links_kept"], 2)
        self.assertEqual(s["links_written"], 0)

    def test_successful_run_replaces_link_log(self):
        self.links.replace(["https://www.tiktok.com/@a/video/old"], day=TARGET)
        eng = self._engine(OkCollector({"demo_alpha": [
            _item("7700000000000000001", "demo_alpha")]}))
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)

        urls = self.links.load(day=TARGET)
        self.assertEqual(len(urls), 1)
        self.assertIn("7700000000000000001", urls[0])
        self.assertEqual(s["links_written"], 1)
        self.assertEqual(s["links_kept"], 0)

    def test_empty_run_with_no_existing_file_stays_empty(self):
        eng = self._engine(FailingCollector())
        s = eng.run([_acc("a1", "demo_alpha")], TARGET)
        self.assertEqual(self.links.load(day=TARGET), [])
        self.assertEqual(s["links_kept"], 0)


if __name__ == "__main__":
    unittest.main()
