"""历史保留天数：设置项必须真的清理过期任务，且绝不能碰素材库作品。

回归背景（2026-10-10 审计）：
    设置页有「历史保留天数」（`history_keep_days`，默认 90），但清理代码
    一行都没有 —— 填了等于没填。本测试锁住三个不变量：

      1. 过期的**任务记录**会被清理；
      2. 未过期、时间未知（legacy 行）、还没结束（pending/running/stopped，
         与「可续采」判定同源）的任务一律保留；
      3. `videos`（素材库作品）一条都不能少，只把 `first_task_id` 置空；
      4. `history_keep_days` = 0 或非法值 = 永久保留，一条都不删。
"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest

from collector.bootstrap import CollectorServices
from collector.core.models import Account, Video
from collector.db.database import Database

CUTOFF = "2026-01-01 00:00:00"
OLD = "2020-01-01 00:00:00"
FUTURE = "2099-01-01 00:00:00"


class _Cfg:
    """只给 prune_history 用的最小配置替身（不起真 CollectorServices）。"""

    def __init__(self, days):
        self._days = days

    def get(self, key, default=None):
        return self._days if key == "history_keep_days" else default


def _seed(db: Database) -> None:
    """两条已完成任务（一旧一新），各带一条作品。"""
    db.insert_account(Account(account_id="a1", username="u1",
                              profile_url="https://x/@u1"))
    db.insert_task("TASK-OLD", created_at=OLD, status="completed")
    db.insert_task("TASK-NEW", created_at=FUTURE, status="completed")
    db.insert_video(Video(video_id="111", account_id="a1", username="u1",
                          video_url="https://x/111", first_task_id="TASK-OLD"))
    db.insert_video(Video(video_id="222", account_id="a1", username="u1",
                          video_url="https://x/222", first_task_id="TASK-NEW"))


def _task_count(db: Database) -> int:
    row = db.query_one("SELECT COUNT(*) AS c FROM collect_tasks")
    return int(row["c"]) if row else 0


class TestDeleteTasksBefore(unittest.TestCase):
    """Database.delete_tasks_before：只删过期任务，作品不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_only_expired_task_is_removed(self):
        _seed(self.db)
        self.assertEqual(self.db.delete_tasks_before(CUTOFF), 1)
        self.assertIsNone(self.db.query_one(
            "SELECT 1 FROM collect_tasks WHERE task_id='TASK-OLD'"))
        self.assertIsNotNone(self.db.query_one(
            "SELECT 1 FROM collect_tasks WHERE task_id='TASK-NEW'"))

    def test_videos_survive_with_null_reference(self):
        _seed(self.db)
        self.db.delete_tasks_before(CUTOFF)
        self.assertEqual(self.db.count_videos(), 2)          # 铁律⑤：作品不能被删
        row = self.db.query_one("SELECT first_task_id FROM videos WHERE video_id='111'")
        self.assertIsNone(row["first_task_id"])              # 只置空引用

    def test_task_children_cascade_only_for_removed_task(self):
        _seed(self.db)
        self.db.insert_collect_log("TASK-OLD", "a1", "旧任务日志", video_id="111")
        self.db.insert_collect_log("TASK-NEW", "a1", "新任务日志", video_id="222")
        self.db.delete_tasks_before(CUTOFF)
        rows = self.db.query("SELECT task_id FROM collect_logs ORDER BY task_id")
        self.assertEqual([r["task_id"] for r in rows], ["TASK-NEW"])

    def test_undated_and_unfinished_tasks_are_kept(self):
        _seed(self.db)
        # 时间未知的 legacy 行，以及「还没结束」的任务（与可续采判定同源）
        self.db.execute("INSERT INTO collect_tasks(task_id, created_at, status) "
                        "VALUES('T-LEGACY','','completed')")
        for status in ("running", "pending", "stopped"):
            self.db.execute("INSERT INTO collect_tasks(task_id, created_at, status) "
                            f"VALUES('T-{status.upper()}','{OLD}','{status}')")
        self.assertEqual(self.db.delete_tasks_before(CUTOFF), 1)   # 只有 TASK-OLD
        self.assertEqual(_task_count(self.db), 5)


class TestPruneHistory(unittest.TestCase):
    """CollectorServices.prune_history：配置 → 清理接线与安全闸。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))
        # 绕过 __init__：真起 CollectorServices 会建日志、装浏览器内核
        self.boot = object.__new__(CollectorServices)
        self.boot.db = self.db
        self.boot.logger = logging.getLogger("test.history_retention")
        self.boot.config = _Cfg(90)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_prunes_with_configured_days(self):
        _seed(self.db)
        self.boot.config = _Cfg(30)
        self.assertEqual(self.boot.prune_history(), 1)
        self.assertEqual(self.db.count_videos(), 2)
        self.assertEqual(_task_count(self.db), 1)

    def test_explicit_keep_days_wins(self):
        _seed(self.db)
        self.boot.config = _Cfg(0)          # 配置说"永久保留"
        self.assertEqual(self.boot.prune_history(30), 1)   # 显式传参仍生效
        self.assertEqual(self.db.count_videos(), 2)

    def test_zero_means_keep_forever(self):
        _seed(self.db)
        self.boot.config = _Cfg(0)
        self.assertEqual(self.boot.prune_history(), 0)
        self.assertEqual(_task_count(self.db), 2)
        self.assertEqual(self.db.count_videos(), 2)

    def test_negative_and_bad_values_keep_forever(self):
        _seed(self.db)
        for bad in (-1, "abc", None):
            with self.subTest(bad=bad):
                self.boot.config = _Cfg(bad)
                self.assertEqual(self.boot.prune_history(), 0)
        self.assertEqual(_task_count(self.db), 2)


if __name__ == "__main__":
    unittest.main()
