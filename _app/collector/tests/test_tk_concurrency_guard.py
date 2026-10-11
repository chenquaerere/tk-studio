"""去水印下载的「同时只能跑一个」守卫（回归）。

背景：与 test_hd_concurrency_guard.py 同一类 v4.0 重构遗留 ——
占用标志 `_tk_worker_running` 原来只由 Gradio 时代的
`services.do_download()` 生成器写入；界面改走 `POST /api/tiktok/download`
之后没有任何代码再写它，于是
`if getattr(c, "_tk_worker_running", False): raise HTTPException(409, …)`
永远为假：连点两次「下载」会真的起两个下载线程，往同一个输出目录写同名文件。

现在由 `services.tk_worker_begin()/tk_worker_end()` 原子占用/释放，
`/api/tiktok/download` 在**起线程之前**占用，线程的 finally 里释放。

放在 collector/tests 的原因同 test_settings_roundtrip.py：这是唯一会被
`unittest discover` 跑到的测试目录。本用例不起线程、不联网、不碰真实配置。
"""
import types
import unittest


class _FakeJob:
    id = "job-probe"


def _payload(**over):
    p = {"selected": [{"url": "https://example.invalid/1"}], "sel_idx": "",
         "out_dir": "", "auto_hd": False}
    p.update(over)
    return p


class TestWorkerSlotHelpers(unittest.TestCase):
    def setUp(self):
        from server import services

        self.services = services
        services.tk_worker_end()                       # 从干净状态开始
        self.addCleanup(services.tk_worker_end)

    def test_second_begin_is_refused_until_end(self):
        self.assertFalse(self.services.tk_worker_busy())
        self.assertTrue(self.services.tk_worker_begin())
        self.assertTrue(self.services.tk_worker_busy())
        self.assertFalse(self.services.tk_worker_begin())   # 第二个任务被挡住
        self.services.tk_worker_end()
        self.assertFalse(self.services.tk_worker_busy())
        self.assertTrue(self.services.tk_worker_begin())    # 释放后可以再跑

    def test_end_is_idempotent(self):
        self.services.tk_worker_begin()
        self.services.tk_worker_end()
        self.services.tk_worker_end()
        self.assertFalse(self.services.tk_worker_busy())
        self.assertTrue(self.services.tk_worker_begin())

    def test_hd_and_tk_slots_are_independent(self):
        """下载和高清是两个位置：一边在跑不影响另一边。"""
        self.assertTrue(self.services.tk_worker_begin())
        self.assertTrue(self.services.hd_worker_begin())
        self.services.hd_worker_end()
        self.assertTrue(self.services.tk_worker_busy())


class TestDownloadEndpoint(unittest.TestCase):
    """/api/tiktok/download：拒绝并发、失败也让出位置。"""

    def setUp(self):
        from server import api

        self.api = api
        self.threads = []
        self.ended = []
        self.calls = []

        self.addCleanup(setattr, api, "core", api.core)
        self.addCleanup(setattr, api, "bus", api.bus)
        self.addCleanup(setattr, api, "threading", api.threading)

        sink = self.threads

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None, name=None):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.name = name
                self.started = False
                self.fail = False
                sink.append(self)

            def start(self):
                if self.fail:
                    raise RuntimeError("线程启动失败")
                self.started = True

        self._FakeThread = _FakeThread
        api.threading = types.SimpleNamespace(Thread=_FakeThread)
        api.bus = types.SimpleNamespace(create_job=lambda *a, **k: _FakeJob())

    def _core(self, begin_result):
        calls = self.calls
        ended = self.ended

        class _Core:
            _tk_cancel = types.SimpleNamespace(clear=lambda: None)
            _tk_pause = types.SimpleNamespace(set=lambda: None)

            def tk_worker_begin(self):
                calls.append("begin")
                return begin_result

            def tk_worker_end(self):
                ended.append(True)

            def _tk_download_worker(self, *args):
                calls.append("worker")

        core = _Core()
        self.api.core = lambda: core
        return core

    def test_busy_slot_returns_409_and_starts_nothing(self):
        from fastapi import HTTPException

        self._core(begin_result=False)
        with self.assertRaises(HTTPException) as ctx:
            self.api.tiktok_download(_payload())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("已有下载任务在运行", str(ctx.exception.detail))
        self.assertEqual(self.threads, [])
        self.assertEqual(self.ended, [])

    def test_free_slot_wraps_worker_and_releases_after_success(self):
        self._core(begin_result=True)
        out = self.api.tiktok_download(_payload())
        self.assertEqual(out, {"job_id": "job-probe"})
        self.assertEqual(self.calls, ["begin"])           # 只占用，还没执行
        self.assertEqual(len(self.threads), 1)
        thread = self.threads[0]
        self.assertTrue(thread.started)
        self.assertEqual(thread.name, "tk-dl-job-probe")
        self.assertTrue(thread.daemon)
        thread.target()                                   # 跑线程体
        self.assertEqual(self.calls, ["begin", "worker"])
        self.assertEqual(self.ended, [True])              # 正常结束要释放

    def test_worker_exception_still_releases_slot(self):
        core = self._core(begin_result=True)

        def _boom(*args):
            self.calls.append("worker")
            raise RuntimeError("下载炸了")

        core._tk_download_worker = _boom
        self.api.tiktok_download(_payload())
        with self.assertRaises(RuntimeError):
            self.threads[0].target()
        self.assertEqual(self.ended, [True])

    def test_thread_start_failure_releases_slot(self):
        self._core(begin_result=True)
        original = self._FakeThread

        def _factory(*a, **k):
            t = original(*a, **k)
            t.fail = True
            return t

        self.api.threading = types.SimpleNamespace(Thread=_factory)
        with self.assertRaises(RuntimeError):
            self.api.tiktok_download(_payload())
        self.assertEqual(self.ended, [True])


class TestGuardSources(unittest.TestCase):
    """源码级断言：守卫必须由启动接口持有，不能退回旧写法。"""

    def setUp(self):
        from common import paths

        self.root = paths.APP_ROOT / "_app"
        self.api_src = (self.root / "server" / "api.py").read_text(encoding="utf-8")
        self.svc_src = (self.root / "server" / "services.py").read_text(
            encoding="utf-8")

    def test_api_uses_begin_and_finally_end(self):
        self.assertIn("if not c.tk_worker_begin():", self.api_src)
        self.assertIn('raise HTTPException(409, "已有下载任务在运行")', self.api_src)
        self.assertIn("def _run_tk():", self.api_src)
        self.assertIn("c.tk_worker_end()", self.api_src)
        self.assertNotIn("getattr(c, \"_tk_worker_running\"", self.api_src)

    def test_helpers_exist_and_gradio_generator_is_gone(self):
        for name in ("def tk_worker_begin(", "def tk_worker_end(",
                     "def tk_worker_busy("):
            self.assertIn(name, self.svc_src)
        # 旧的 Gradio 生成器整体删除，避免有人再往这条路上接
        self.assertNotIn("def do_download(", self.svc_src)


if __name__ == "__main__":
    unittest.main()
