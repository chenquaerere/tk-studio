"""完成提醒：bus 卡片标记 + 任务 notify 事件 + **只有一个弹窗出口**（回归）。

背景（2026-10-10 两次返工、2026-10-11 第三次返工后定稿）：
1. 水印侧「高清修复 / 下载完成」的提醒，原来的实现只写在一对**死函数**
   （services._notify / services.do_download）里 —— 界面改走 HTTP 之后
   没人再调用它们，于是 `card=True` 打了也白打，`beep_on_done` 开关也永远
   不生效（2026-10-10 的这次复查才发现）。
2. 真正在跑的是任务的 `notify` 字段：工作线程写
   `job["notify"] = ("ok"|"warn"|"error", "给用户看的一句话")`，
   server/jobs.py 把它翻成 NOTIFY 事件（带 level 与 message），
   前端 app.js 的 SSE 处理器再交给 notifyCompletion()。
3. **2026-10-11 用户第三次反馈「还是在桌面右下角弹」**：只要按「窗口有没有焦点」
   分流（app.js 一套、collector.js 另一套），pywebview 下 document.hasFocus()
   经常是 false，最后几乎总会走 server/notifier.py 的桌面浮层，而且两个板块的
   弹窗长得不一样。现在**只有一条通道**：三个板块（采集链接 / 去水印 / 高清修复）
   全部调用 app.js 的 notifyCompletion()，卡片画在程序界面右下角
   （css/app.css 的 #notify-stack），桌面浮层不再被任何完成消息使用；
   窗口不在前台时卡片常驻（timeout=0）而不是弹到桌面上。

放在 collector/tests 的原因同 test_settings_roundtrip.py：这是唯一会被
`unittest discover` 跑到的测试目录，用例只依赖 ui.bus / 源码文本，无副作用。
"""
import json
import re
import unittest


def _strip_comments(text, style):
    """剥掉注释再比对。

    删配置键时会在原处留一句「2026-10-11 清理：xxx 已删除」的说明，里面自然带着
    被删键的名字；如果直接对整份文件 assertNotIn，就会把「写了注释」误判成
    「键还在」。所以先剥注释，再看代码/配置里到底还有没有这个键。
    """
    if style == "py":
        text = re.sub(r'"""(?:.|\n)*?"""', '""', text)
        text = re.sub(r"'''(?:.|\n)*?'''", "''", text)
        return re.sub(r"#[^\n]*", "", text)
    if style == "js":
        text = re.sub(r"/\*(?:.|\n)*?\*/", "", text)
        return re.sub(r"//[^\n]*", "", text)
    if style == "html":
        return re.sub(r"<!--(?:.|\n)*?-->", "", text)
    raise ValueError(style)


class TestNotifyCardFlag(unittest.TestCase):
    def setUp(self):
        from ui import bus

        self.bus = bus
        bus.drain_notifications()          # 清掉别的用例/上一次留下的消息
        self.addCleanup(bus.drain_notifications)

    def _take(self, message):
        return next((x for x in self.bus.drain_notifications()
                     if x["message"] == message), None)

    def test_default_is_plain_toast(self):
        self.bus.notify("进度 3/20", "info")
        item = self._take("进度 3/20")
        self.assertIsNotNone(item)
        self.assertIs(item["card"], False)
        self.assertEqual(item["level"], "info")

    def test_card_flag_is_recorded(self):
        self.bus.notify("✅ 处理完成：成功 2 项", "ok", card=True)
        item = self._take("✅ 处理完成：成功 2 项")
        self.assertIsNotNone(item)
        self.assertIs(item["card"], True)
        self.assertEqual(item["level"], "ok")

    def test_payload_shape_unchanged_for_old_consumers(self):
        """老消费方只读 message / level / ts，新增键不能把它们挤掉。"""
        self.bus.notify("普通提示", "warn")
        item = self._take("普通提示")
        for key in ("message", "level", "ts"):
            self.assertIn(key, item)


class TestJobNotifyEvent(unittest.TestCase):
    """任务 notify 字段 → NOTIFY 事件（含正文）。"""

    def _job(self):
        from ui.bus import create_job, JobKind

        job = create_job(JobKind.TIKTOK_DOWNLOAD, label="通知用例")
        self.addCleanup(lambda: self._forget(job))
        return job

    def _forget(self, job):
        from server.jobs import STORE

        try:
            STORE.remove(job.id)
        except Exception:
            pass

    def _notify_event(self, job):
        return next((e for e in job.events_since(0)
                     if e.kind.value == "notify"), None)

    def test_tuple_message_reaches_the_event(self):
        job = self._job()
        job["notify"] = ("ok", "下载完成：成功 3 个")
        ev = self._notify_event(job)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.level, "ok")
        self.assertEqual(ev.message, "下载完成：成功 3 个")

    def test_warn_and_error_levels(self):
        job = self._job()
        job["notify"] = ("warn", "下载完成：成功 3 个，失败 1 个")
        ev = self._notify_event(job)
        self.assertEqual(ev.level, "warn")
        self.assertEqual(ev.message, "下载完成：成功 3 个，失败 1 个")

    def test_level_only_tuple_still_works(self):
        """老写法（只有等级、没有正文）不能崩，也不能丢掉等级。"""
        job = self._job()
        job["notify"] = ("error",)
        ev = self._notify_event(job)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.level, "error")
        self.assertEqual(ev.message, "")

    def test_plain_string_is_treated_as_message(self):
        job = self._job()
        job["notify"] = "任务完成"
        ev = self._notify_event(job)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.level, "info")
        self.assertEqual(ev.message, "任务完成")


class TestLiveCompletionWiring(unittest.TestCase):
    """源码级防回归：死函数不能复活，三个板块必须走同一个出口。"""

    @classmethod
    def setUpClass(cls):
        from common import paths

        cls.root = paths.APP_ROOT / "_app"
        cls.svc = (cls.root / "server" / "services.py").read_text(encoding="utf-8")
        cls.jobs = (cls.root / "server" / "jobs.py").read_text(encoding="utf-8")
        cls.app = (cls.root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
        cls.col = (cls.root / "frontend" / "js" / "collector.js").read_text(
            encoding="utf-8")
        cls.css = (cls.root / "frontend" / "css" / "app.css").read_text(
            encoding="utf-8")
        cls.html = (cls.root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.api = (cls.root / "server" / "collector_api.py").read_text(
            encoding="utf-8")
        cls.cfg = (cls.root / "common" / "config.py").read_text(encoding="utf-8")
        cls.ex = json.loads((paths.APP_ROOT / "config.example.json").read_text(
            encoding="utf-8"))

    def test_dead_notifiers_stay_deleted(self):
        self.assertNotIn("def _notify(", self.svc)
        self.assertNotIn("def do_download(", self.svc)

    def test_emitters_carry_human_text(self):
        self.assertIn('job["notify"] = ("ok", f"下载完成：成功 {ok} 个")', self.svc)
        self.assertIn('("warn", f"下载完成：成功 {ok} 个，失败 {fail} 个")', self.svc)
        self.assertIn('("ok", f"高清修复完成：成功 {ok} 项")', self.svc)
        self.assertIn('("warn", f"高清修复完成：成功 {ok} 项，失败 {len(errors)} 项")', self.svc)
        self.assertIn('("error", "高清修复全部失败，请查看「处理信息」")', self.svc)

    def test_jobs_forwards_the_message(self):
        self.assertIn('self.emit(EventKind.NOTIFY, level=level, message=message)',
                      self.jobs)
        self.assertIn("if len(value) > 1 and value[1]:", self.jobs)

    def test_frontend_routes_completion_through_one_exit(self):
        self.assertIn("function notifyCompletion(", self.app)
        self.assertIn("notifyCompletion(msg, kind);", self.app)
        # 卡片画在程序界面右下角；窗口不在前台时常驻（timeout=0）
        self.assertIn("Notify.show({ title, message: msg, kind, timeout: seen ? 9000 : 0 });",
                      self.app)
        self.assertIn("const seen = !document.hidden && document.hasFocus();", self.app)
        self.assertIn('if (Number(timeout) === 0) card.classList.add("notify-sticky");',
                      self.app)
        self.assertIn(".notify-sticky .notify-progress { display: none; }", self.css)
        # 常驻卡片不能被 hover 的 pause/resume 叫醒成一个 9 秒定时卡
        self.assertEqual(self.app.count(
            'if (card.classList.contains("notify-sticky")) return;'), 2)
        self.assertIn("beepOnDone", self.app)
        self.assertIn("raw.beep_on_done", self.app)
        # 老的一行 toast（连正文都写死「完成」）必须消失
        self.assertNotIn('toast("✅ " + (ev.message', self.app)


class TestOneUnifiedPopup(unittest.TestCase):
    """2026-10-11：三个板块共用一个界面内弹窗，任何完成消息都不许再弹桌面浮层。"""

    @classmethod
    def setUpClass(cls):
        from common import paths

        cls.root = paths.APP_ROOT / "_app"
        cls.app = (cls.root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
        cls.col = (cls.root / "frontend" / "js" / "collector.js").read_text(
            encoding="utf-8")
        cls.css = (cls.root / "frontend" / "css" / "app.css").read_text(
            encoding="utf-8")
        cls.html = (cls.root / "frontend" / "index.html").read_text(encoding="utf-8")
        cls.api = (cls.root / "server" / "collector_api.py").read_text(
            encoding="utf-8")
        cls.cfg = (cls.root / "common" / "config.py").read_text(encoding="utf-8")
        cls.ex = (paths.APP_ROOT / "config.example.json").read_text(encoding="utf-8")

    def test_completion_is_said_once_not_twice(self):
        """同一次完成只由卡片说一遍：主采集 / 重试不再各发一条重复 toast。"""
        col = _strip_comments(self.col, style="js")
        self.assertNotIn("toast(`${head}：${summarizeJob(j)}`", col)
        self.assertNotIn('toast(`${kind === "job_done" ? "重试完成"', col)
        # 采集 / 重试的完成提示只走 afterCollectEnd（内部就是 notifySystem）
        self.assertIn('afterCollectEnd(kind, j, "采集");', col)
        self.assertIn('afterCollectEnd(kind, j, "重试");', col)
        # 原来只有一行 toast 的完成提示，现在也走同一张卡片
        for call in ('notifySystem("昵称刷新完成"', 'notifySystem("采集完成"',
                     'notifySystem("导入完成"'):
            self.assertIn(call, col)

    def test_only_one_implementation_in_the_whole_frontend(self):
        self.assertEqual(self.app.count("function notifyCompletion("), 1)
        self.assertNotIn("function notifyCompletion(", self.col)
        self.assertIn("notifyCompletion,", self.app)          # 导出给 collector.js
        self.assertIn("window.TKStudio.notifyCompletion", self.col)

    def test_collector_delegates_with_its_own_sound_switch(self):
        self.assertIn("shared(message, kind, title, { sound });", self.col)
        self.assertIn("const sound = meta.notify_sound !== false;", self.col)
        # 采集侧不再自己 POST 桌面通知（那条路就是「弹到桌面上」的来源）；
        # 注释里会解释这段历史，所以只看剥掉注释后的代码。
        self.assertNotIn("/api/system/notify",
                         _strip_comments(self.col, style="js"))

    def test_no_completion_path_can_ask_for_the_desktop_overlay(self):
        self.assertNotIn("desktop: true", self.app)
        self.assertNotIn("desktop: useDesktop", self.col)
        self.assertNotIn("useDesktop", self.col)
        # notifyCompletion 里不该再有 /api/system/notify（提示音走 beepOnce）
        body = self.app.split("function notifyCompletion(")[1].split("\n  }")[0]
        self.assertNotIn("/api/system/notify", body)

    def test_card_is_anchored_inside_the_program_window(self):
        stack = self.css.split("#notify-stack {")[1].split("}")[0]
        self.assertIn("position: fixed", stack)
        self.assertIn("right: 20px", stack)
        self.assertIn("bottom: 56px", stack)                  # 在程序状态栏之上
        self.assertIn("document.body.appendChild(host);", self.app)

    def test_the_two_channel_switches_are_gone_everywhere(self):
        """两个「前台卡片 / 后台浮层」开关必须彻底消失。

        注意：清理说明的注释里仍会写到这两个键名（记录删了什么），所以这里比对的是
        **剥掉注释后的代码** 与 **解析后的配置键**，而不是整份文件文本 —— 否则
        「写了注释」会被当成「开关还在」。
        """
        strip_js = (self.app, self.col)
        for text in strip_js:
            body = _strip_comments(text, style="js")
            self.assertNotIn("notify_in_app", body)
            self.assertNotIn("notify_desktop_when_unfocused", body)
        self.assertNotIn("notify_in_app", _strip_comments(self.api, style="py"))
        self.assertNotIn("notify_desktop_when_unfocused",
                         _strip_comments(self.api, style="py"))

        # 解析后的配置：DEFAULTS 与示例配置里都不该再有这两个键
        from common import config as cfgmod

        self.assertNotIn("notify_in_app", cfgmod.DEFAULTS["collector"])
        self.assertNotIn("notify_desktop_when_unfocused", cfgmod.DEFAULTS["collector"])
        ex = json.loads(self.ex)
        self.assertNotIn("notify_in_app", ex.get("collector", {}))
        self.assertNotIn("notify_desktop_when_unfocused", ex.get("collector", {}))

        html = _strip_comments(self.html, style="html")
        self.assertNotIn("cset-notify-inapp", html)
        self.assertNotIn("cset-notify-away", html)
        # 卡片尺寸这两个键必须留着（首要需求：尺寸/比例可调）
        self.assertIn("notify_card_width", cfgmod.DEFAULTS["collector"])
        self.assertIn("notify_card_scale", cfgmod.DEFAULTS["collector"])
        self.assertIn("cset-notify-width", html)
        self.assertIn("cset-notify-scale", html)

    def test_no_dangling_reference_to_the_deleted_helpers(self):
        """删掉函数时必须同时删掉调用点。

        真实教训（2026-10-11）：删掉 app.js 里的 syncAwayNotice() 定义后，模块顶部
        仍留着一句 syncAwayNotice(); —— `node --check` 只看语法，静态字符串断言也
        只查「新代码在不在」，于是谁都没发现它会抛 ReferenceError 把整个前端带崩。
        这里对「被删掉的标识符」做全字面扫描，两个 JS 文件都不许再出现。
        """
        for name in ("syncAwayNotice", "awayNotice", "awayDesktop",
                     "notifyInApp", "notifyAway", "useDesktop"):
            for src, label in ((self.app, "app.js"), (self.col, "collector.js")):
                body = _strip_comments(src, style="js")
                self.assertNotIn(name, body, f"{label} 里还残留 {name} 的引用")

    def test_all_three_modules_land_on_the_same_function(self):
        """采集 / 去水印 / 高清修复三类完成消息都必须落到同一个函数。"""
        self.assertIn("function notifySystem(title, message, kind) {", self.col)
        self.assertIn("notifySystem(title, summarizeJob(j), uk);", self.col)
        self.assertIn("notifyCompletion(msg, kind);", self.app)      # SSE notify
        self.assertIn('notifyCompletion(msg, n.level || "info");', self.app)


if __name__ == "__main__":
    unittest.main()
