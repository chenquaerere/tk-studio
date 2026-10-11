"""「重新登录」（清除账号登录态）必须真的能清干净（回归）。

背景：后端一直有 POST /api/collector/accounts/{id}/reset-login，界面却没有入口 ——
登录态失效时用户没有任何办法让它重新登录，只能手工去删 data/sessions 目录。
本用例锁三件事：

1. 真的删掉 data/sessions/<账号> 目录（含里面的文件），并把 login_status 置回 unknown；
2. 目录本来就不存在时也要成功（幂等，不能因为「没得删」而报错）；
3. 账号名里的路径字符要被消毒（不能顺着账号名删到别的目录）；
4. 账号不存在时仍然 404（不静默成功）。
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from server import collector_api


class _Db:
    def __init__(self, rows):
        self.rows = dict(rows)
        self.sql = []

    def get_account(self, account_id):
        return self.rows.get(account_id)

    def execute(self, sql, params=()):
        self.sql.append((sql, tuple(params)))
        return None


class _Ctx:
    def __init__(self, db):
        self.db = db


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self._orig_data_dir = collector_api._paths.DATA_DIR
        collector_api._paths.DATA_DIR = self.data_dir
        self.addCleanup(self._restore_data_dir)

    def _restore_data_dir(self):
        collector_api._paths.DATA_DIR = self._orig_data_dir

    def patch_db(self, rows):
        db = _Db(rows)
        original = collector_api.ctx
        collector_api.ctx = lambda: _Ctx(db)
        self.addCleanup(setattr, collector_api, "ctx", original)
        return db

    def session_dir(self, name):
        return self.data_dir / "sessions" / name


class TestResetLogin(_Base):
    def test_removes_session_dir_and_marks_unknown(self):
        d = self.session_dir("acc1")
        (d / "nested").mkdir(parents=True)
        (d / "nested" / "cookies.bin").write_bytes(b"x")
        db = self.patch_db({"acc1": {"account_id": "acc1", "username": "alice"}})

        out = collector_api.reset_login("acc1")

        self.assertEqual(out.get("ok"), True)
        self.assertFalse(d.exists(), "登录态目录应该被删掉")
        self.assertTrue(any("login_status = 'unknown'" in s for s, _ in db.sql),
                        f"应该把登录态置回 unknown，实际执行：{db.sql}")
        self.assertEqual(db.sql[0][1], ("acc1",))

    def test_missing_dir_is_still_ok(self):
        # 目录不存在（或已经被删过）时不能报错：幂等
        db = self.patch_db({"acc2": {"account_id": "acc2", "username": "bob"}})
        out1 = collector_api.reset_login("acc2")
        out2 = collector_api.reset_login("acc2")
        self.assertEqual((out1.get("ok"), out2.get("ok")), (True, True))
        self.assertEqual(len(db.sql), 2)

    def test_unknown_account_is_404(self):
        self.patch_db({})
        with self.assertRaises(HTTPException) as caught:
            collector_api.reset_login("nope")
        self.assertEqual(caught.exception.status_code, 404)

    def test_username_is_sanitised(self):
        # 账号名里带路径字符时，只能删消毒后的那一个目录
        d = self.session_dir("a_b_c")
        d.mkdir(parents=True)
        keep = self.data_dir / "sessions" / "other"
        keep.mkdir(parents=True)
        self.patch_db({"acc3": {"account_id": "a/b:c", "username": "x"}})

        collector_api.reset_login("acc3")

        self.assertFalse(d.exists())
        self.assertTrue(keep.is_dir(), "不能碰别的账号目录")

    def test_safe_name(self):
        self.assertEqual(collector_api._safe_name("a/b:c"), "a_b_c")
        self.assertEqual(collector_api._safe_name(""), "account")
        self.assertEqual(collector_api._safe_name(None), "account")


if __name__ == "__main__":
    unittest.main()
