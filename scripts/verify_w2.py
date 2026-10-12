"""W2 验收：采集内核移植的四道门禁。

门禁设计原则 —— 每条都对应一个"曾经真的出过事"的点，
不做泛泛的"能 import"检查。

  G1  UI 无关性：collector 包内不得出现 tkinter / customtkinter
                  （否则 Web 化后会在无图形环境里炸掉，且拖慢启动）
  G2  五条铁律在位：对源码做结构断言
                  1. 数据库不自增 SCHEMA_VERSION（否则 DROP 全表清用户数据）
                  2. 不做跨运行去重（否则重新抓取会把最新作品当重复跳过）
                  3. response 监听早于 page.goto（否则首屏最新一批被静默丢弃）
                  4. 置顶作品用 isPinnedItem 识别 + 今天/昨天额外采集
                  5. engine 与 collect_logic 两套实现同步
  G3  真实库零丢失：在副本上比对 账号/作品/任务 数量 + user_version
                    （原库 D:\\workbuddy\\TK链接抓取助手\\data 只读，绝不动）
  G4  装配层可用：CollectorServices 能构建、服务层方法可调用、代理配置正确
  G5  关停释放：关窗 / 点「关闭程序」必须真的释放采集子系统
                  （collector_api.shutdown() 曾被写进 docstring 却无人调用，
                    导致 Playwright 驱动与 Chromium 残留）
  G6  历史保留天数真的生效：设置页写了「历史保留天数」，代码里必须真有清理，
                  且只清理过期任务记录 —— 素材库作品一条都不能少
  G7  导出 / 日志目录真的生效：设置页那两个路径输入框曾经无人读取
                   （导出写死 exports/、日志写死 logs/），
                   下载白名单必须跟着导出目录一起走，目录不可用时保存要报错
  G8  高清链路：并发守卫（曾被 Gradio 时代函数独占写入，改走 HTTP 后失效，
                  连点两次真的起两个线程）、上次参数预选（_remember_choices
                   存了却没人读）、设置兜底值唯一、旧适配壳与死代码已删除
  G9  「重新登录」：后端一直有清登录态的接口，界面此前没有任何入口
  G10 去水印下载链路：并发守卫（同一个「Gradio 时代函数独占写入」的坑，
                   下载这边也中了一次）、完成提醒必须能真的弹出来
                   （提醒原来写在没人调用的 do_download/_notify 里）
  G11 历史死函数清理：16 个没人调用的旧入口删掉后不许复活，
                   活着的替代入口（tk/hd 守卫、_parse_worker）必须还在
  G12 日志与限流：链接缩略后仍看得见作品 id（原来 url[:60] 会切掉末位），
                   tikwm 时间槽按「上一次请求完成」间隔（原来会撞 1 req/s 限流）
  G13 白名单跟着设置走：换了输出/下载目录后 /api/file 立刻认新目录
                   （原来白名单只在导入时算一次，新目录一直 403 到重启）
  G14 前端转义连引号一起处理：昵称/地区里带引号不会撑破表单
                   （原来 esc() 只转义 & < >，属性位置能被引号提前闭合）
  G15 子进程超时：卡住的 ffmpeg 不再永久占住任务线程（原来一个 timeout 都没有）
  G16 替换式记录的后悔药：登录态失效不再把所选账号的历史记录挨个清空
                   （原来先删后采且失败不回滚，重新登录前点采集＝记录变空）
  G17 链接日志不被清空：一次没采到链接的运行不再把当日链接文件写成 0 字节
                   （原来 replace([]) 无条件执行 ＝ 把上次成功采集的成果删掉）
  G18 「下载后自动送入高清（仅图片）」不再是个死开关
                   （原来只把 auto_hd 发给后端，前端没人读、也没人开高清任务）
  G19 配置默认值里不再有「没人读的死键」，示例配置与 DEFAULTS 键集一致
                   （原来 15 个键只写在 DEFAULTS 里：theme/language/first_run/
                     window、default_date/browser_type/auto_save/
                     task_interval_ms/error_wait_ms、lan_password/tk_* 等）
  G20 三个板块的「任务完成」提醒只走一个界面内弹窗（画在程序窗口右下角）
                    （原来 app.js / collector.js 各写一套、还按窗口焦点分流：
                      在 pywebview 下 hasFocus() 常为 false，于是几乎总会弹到
                      桌面右下角，而且两个板块的弹窗长得不一样）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "_app"
sys.path.insert(0, str(APP_DIR))

COLLECTOR = APP_DIR / "collector"
ORIGINAL_DB = Path(r"D:\workbuddy\TK链接抓取助手\data\tiktok_link_collector.db")
COPY_DB = APP_DIR.parent / "data" / "tiktok_link_collector.db"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


# ───────────────────────────────────────────── G1 UI 无关性
def g1() -> None:
    print("\nG1  UI 无关性（collector 包内不得有 tkinter）")
    offenders: list[str] = []
    for f in COLLECTOR.rglob("*.py"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^\s*(import|from)\s+(tkinter|customtkinter)", text, re.M):
            offenders.append(str(f.relative_to(COLLECTOR)))
    check("collector 包内零 tkinter/customtkinter", not offenders,
          f"违例文件: {offenders}" if offenders else "核心层与 UI 完全解耦")

    # app/ui 目录也不应被搬进来
    check("未搬迁任何 app/ui 页面代码", not (COLLECTOR / "ui").exists())


# ───────────────────────────────────────────── G2 五条铁律
def g2() -> None:
    print("\nG2  五条铁律在位")

    db_src = (COLLECTOR / "db" / "database.py").read_text(encoding="utf-8")
    m = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", db_src, re.M)
    check("① 数据库 SCHEMA_VERSION 保持 2（未递增，不清库）",
          bool(m) and m.group(1) == "2",
          f"SCHEMA_VERSION = {m.group(1) if m else '未找到'}")
    check("① 新字段走 _EXTRA_COLUMNS + ALTER TABLE（幂等加列）",
          "_EXTRA_COLUMNS" in db_src and "ALTER TABLE" in db_src)

    eng = (COLLECTOR / "collect" / "engine.py").read_text(encoding="utf-8")
    # 只看可执行代码，注释里会提到历史 bug 的旧写法（刻意的说明文字）
    eng_code = "\n".join(ln for ln in eng.splitlines()
                         if not ln.lstrip().startswith("#"))
    check("② 去重器在单次采集内新建（不做跨运行去重）",
          "dedup = VideoDedup()" in eng_code and "load_all_video_ids" not in eng_code,
          "可执行代码中未出现 load_all_video_ids")
    check("② 去重范围注释明确标注「本账号 + 本次采集」",
          "本账号 + 本次采集" in eng or "去重范围" in eng)

    prov = (COLLECTOR / "collect" / "provider.py").read_text(encoding="utf-8")
    i_listen = prov.find('page.on("response"')
    i_goto = prov.find("page.goto(")
    check("③ response 监听早于 page.goto（首屏最新一批不丢）",
          0 <= i_listen < i_goto, f"on={i_listen} < goto={i_goto}")

    parser = (COLLECTOR / "collect" / "video_parser.py").read_text(encoding="utf-8")
    check("④ 置顶识别字段为 isPinnedItem", "isPinnedItem" in parser)
    check("④ 近期置顶额外采集（不占名额）",
          "recent_dates" in eng and "pinned_collected" in eng and "extra_pinned" in eng)
    check("④ 置顶不占「最新 N 条」名额，仅非置顶计数",
          "non_pinned_count" in eng)

    logic = (COLLECTOR / "core" / "collect_logic.py").read_text(encoding="utf-8")
    check("⑤ engine 与 collect_logic 两套实现同步（均处理置顶/去重顺序）",
          "is_pinned" in logic and "recent_dates" in logic and "pinned_skipped" in logic)


# ───────────────────────────────────────────── G3 真实库零丢失
def g3() -> None:
    print("\nG3  真实库零丢失（比对副本，原库只读）")

    def snap(p: Path) -> dict | None:
        if not p.exists():
            return None
        uri = f"file:{p.as_posix()}?mode=ro"
        c = sqlite3.connect(uri, uri=True)
        try:
            out = {"user_version": c.execute("PRAGMA user_version").fetchone()[0]}
            for t in ("accounts", "videos", "collect_tasks"):
                try:
                    out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    out[t] = -1
            out["names"] = sorted(r[0] for r in
                                  c.execute("SELECT username FROM accounts"))
            return out
        finally:
            c.close()

    a, b = snap(ORIGINAL_DB), snap(COPY_DB)
    check("原库与副本均存在", a is not None and b is not None)
    if a and b:
        check("user_version 一致且为 2",
              a["user_version"] == b["user_version"] == 2,
              f"原 {a['user_version']} / 副本 {b['user_version']}")
        # videos / collect_tasks 的**行数**只在 W0b 那一刻相等 ——
        # 副本是冻结快照，而真实库此后还会被正常采集写入（替换式记录、
        # 任务台账持续追加），所以行数只能作为参考信息打印，不能当断言。
        # 真正的不变量是：结构版本没被升过、账号名单一条不少。
        print(f"        （参考）作品 原 {a['videos']} / 副本 {b['videos']}；"
              f"任务 原 {a['collect_tasks']} / 副本 {b['collect_tasks']}")
        check("账号数一致（账号是唯一不该被迁移/采集改动的维度）",
              a["accounts"] == b["accounts"],
              f"原 {a['accounts']} / 副本 {b['accounts']}")
        check("账号名单逐一一致（含地区分类）", a["names"] == b["names"],
              f"{len(b['names'])} 个账号")
        check("账号数不为 0（未被清库）", b["accounts"] > 0)


# ───────────────────────────────────────────── G4 装配层可用
def g4() -> None:
    print("\nG4  装配层可用（CollectorServices）")
    from collector.bootstrap import CollectorServices

    ctx = CollectorServices()
    check("CollectorServices 构建成功", ctx is not None)
    check("数据库已连接", ctx.db is not None)

    accounts = ctx.account_service.list()
    check(f"账号服务可读（{len(accounts)} 个账号）", len(accounts) > 0)
    check("地区统计可用",
          len(ctx.account_service.region_stats()) > 0,
          str(ctx.account_service.region_stats()))

    rows = ctx.result_service.query_videos()
    check(f"结果服务可读（{len(rows)} 条作品）", len(rows) > 0)

    tz = ctx.timezone()
    check(f"时区解析（{tz}）", bool(tz))

    check("代理未启用时返回 None（不硬编码任何地址）",
          ctx.config.proxy_config() is None)

    # 聊天服务可构建（不实际启动浏览器）
    chat = ctx.build_chat_service()
    check("聊天采集服务可构建", chat is not None)

    # 采集引擎可构建（惰性；此处只验证装配，不启动 Playwright）
    check("采集引擎为惰性构建（未采集则未实例化）", ctx._engine is None)

    ctx.close()


# ───────────────────────────────────────────── G5 关停释放接线
def g5() -> None:
    print("\nG5  关停释放（Playwright 驱动 + 数据库必须真的被释放）")

    api_src = (APP_DIR / "server" / "collector_api.py").read_text(encoding="utf-8")
    svc_src = (APP_DIR / "server" / "services.py").read_text(encoding="utf-8")
    desk_src = (APP_DIR.parent / "desktop.py").read_text(encoding="utf-8")

    check("collector_api 提供 shutdown() 关停钩子", "def shutdown()" in api_src)
    check("services 提供 close_collector_runtime()",
          "def close_collector_runtime()" in svc_src)

    # 曾经的真实缺陷：collector_api.shutdown() 的 docstring 写着「由 desktop.py
    # 调用」，但全项目没有任何调用点 —— 关窗后 Chromium / Playwright 驱动残留。
    rs = svc_src[svc_src.index("def request_shutdown("):].split("def shutdown_app(")[0]
    check("界面「关闭程序」路径调用 close_collector_runtime()",
          "close_collector_runtime()" in rs)
    ds = desk_src[desk_src.index("def do_shutdown("):].split("\ndef main(")[0]
    check("关窗路径（desktop.do_shutdown）调用 close_collector_runtime()",
          "close_collector_runtime()" in ds)

    # 行为断言：用假容器验证「真的调用 close」且「没有容器时不创建容器」
    from server import collector_api, services

    class _Fake:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    old = collector_api._ctx
    try:
        fake = _Fake()
        collector_api._ctx = fake                    # type: ignore[assignment]
        services.close_collector_runtime()
        check("关停会释放已存在的采集容器（并清空引用）",
              fake.closed == 1 and collector_api._ctx is None,
              f"closed={fake.closed} 残留={collector_api._ctx!r}")
        collector_api._ctx = None
        services.close_collector_runtime()
        check("从未采集过时不创建容器（关停不会白开数据库）",
              collector_api._ctx is None)
    finally:
        collector_api._ctx = old                     # type: ignore[assignment]


# ───────────────────────────────────────────── G6 历史保留天数真的生效
def g6() -> None:
    print("\nG6  历史保留天数（写了就得真的清理，且不能碰素材库作品）")
    import logging
    import tempfile

    from collector.bootstrap import CollectorServices
    from collector.core.models import Account, Video
    from collector.db.database import Database

    db_src = (COLLECTOR / "db" / "database.py").read_text(encoding="utf-8")
    boot_src = (COLLECTOR / "bootstrap.py").read_text(encoding="utf-8")
    api_src = (APP_DIR / "server" / "collector_api.py").read_text(encoding="utf-8")
    js_src = (APP_DIR / "frontend" / "js" / "collector.js").read_text(encoding="utf-8")

    check("Database 提供 delete_tasks_before()", "def delete_tasks_before(" in db_src)
    check("CollectorServices 提供 prune_history()", "def prune_history(" in boot_src)
    init_src = boot_src[boot_src.index("def __init__("):boot_src.index("def prune_history(")]
    check("启动流程调用 prune_history()", "prune_history()" in init_src)
    check("保存设置后立刻生效（collector_api 调 prune_history）",
          "prune_history()" in api_src)
    check("「启动时检查更新」真的被前端读取（auto_check_update）",
          "auto_check_update" in js_src)

    # 行为断言：过期任务被清理 / 未过期保留 / 作品一条不少
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "retention.db"))
        try:
            db.insert_account(Account(account_id="a1", username="u1",
                                      profile_url="https://x/@u1"))
            db.insert_task("TASK-OLD", created_at="2020-01-01 00:00:00", status="completed")
            db.insert_task("TASK-NEW", created_at="2099-01-01 00:00:00", status="completed")
            db.insert_video(Video(video_id="111", account_id="a1", username="u1",
                                  video_url="https://x/111", first_task_id="TASK-OLD"))
            db.insert_video(Video(video_id="222", account_id="a1", username="u1",
                                  video_url="https://x/222", first_task_id="TASK-NEW"))

            class _Cfg:
                def __init__(self, days: object) -> None:
                    self._days = days

                def get(self, key, default=None):
                    return self._days if key == "history_keep_days" else default

            svc = object.__new__(CollectorServices)   # 只测 prune_history，不起浏览器
            svc.db = db
            svc.logger = logging.getLogger("verify.w2.g6")
            svc.config = _Cfg(90)

            removed = svc.prune_history()
            old = db.query_one("SELECT 1 FROM collect_tasks WHERE task_id='TASK-OLD'")
            new = db.query_one("SELECT 1 FROM collect_tasks WHERE task_id='TASK-NEW'")
            check("只清理过期任务记录", removed == 1 and old is None and new is not None,
                  f"removed={removed}")
            check("素材库作品一条都没少", db.count_videos() == 2,
                  f"videos={db.count_videos()}")
            row = db.query_one("SELECT first_task_id FROM videos WHERE video_id='111'")
            check("被清理任务只置空 first_task_id（不是删作品）",
                  row is not None and row["first_task_id"] is None)

            svc.config = _Cfg(0)
            check("history_keep_days=0 → 永久保留，一条都不删",
                  svc.prune_history() == 0 and db.count_videos() == 2)
            svc.config = _Cfg("abc")
            check("history_keep_days 非法值 → 不清理（不猜着删）",
                  svc.prune_history() == 0)
        finally:
            db.close()


# ───────────────────────────────────────────── G7 导出 / 日志目录真的生效
def g7() -> None:
    print("\nG7  导出目录 / 日志目录（设置页填了就得真的用上）")
    import tempfile

    from fastapi import HTTPException

    from common import paths as _p
    from server import collector_api as ca

    api_src = (APP_DIR / "server" / "collector_api.py").read_text(encoding="utf-8")
    boot_src = (COLLECTOR / "bootstrap.py").read_text(encoding="utf-8")
    html_src = (APP_DIR / "frontend" / "index.html").read_text(encoding="utf-8")

    check("导出目录读设置（_exports_dir）", "def _exports_dir(" in api_src)
    check("日志目录读设置（_logs_dir）", "def _logs_dir(" in api_src)
    check("保存设置先建目录再落盘（_ensure_setting_dirs）",
          "_ensure_setting_dirs(values)" in api_src)
    # 写死 _paths.EXPORTS_DIR 的旧写法只剩两处「默认值」定义，
    # 真正用到目录的地方（写入 / 下载白名单 / 列表 / 环境自检）都必须走 _exports_dir()
    check("导出写入 / 下载白名单 / 列表不再写死 EXPORTS_DIR",
          api_src.count("_paths.EXPORTS_DIR") == 2
          and "_paths.EXPORTS_DIR.mkdir" not in api_src
          and "relative_to(_paths.EXPORTS_DIR" not in api_src,
          f"count={api_src.count('_paths.EXPORTS_DIR')}")
    check("启动时的日志目录读设置（bootstrap.resolve_log_dir）",
          "def resolve_log_dir(" in boot_src
          and "log_dir=str(resolve_log_dir(self.config))" in boot_src)
    check("设置页写明「立即生效」与「重启后生效」",
          "立即生效" in html_src and "重启后生效" in html_src)

    class _Cfg:
        def __init__(self, values: dict) -> None:
            self._v = dict(values)

        def get(self, key, default=None):
            return self._v.get(key, default)

    class _Ctx:
        def __init__(self, values: dict) -> None:
            self.config = _Cfg(values)

    old_ctx = ca.ctx
    try:
        ca.ctx = lambda: _Ctx({"export_dir": "exports_probe"})
        check("相对路径按程序根解析",
              ca._exports_dir() == _p.APP_ROOT / "exports_probe")
        ca.ctx = lambda: _Ctx({})
        check("留空时退回默认 exports/", ca._exports_dir() == _p.EXPORTS_DIR)

        with tempfile.TemporaryDirectory() as tmp:
            ca.ctx = lambda: _Ctx({"export_dir": tmp})
            inside = Path(tmp) / "a.txt"
            inside.write_text("x", encoding="utf-8")
            resp = ca.export_download(path=str(inside))
            check("白名单跟着自定义导出目录走（目录内可下载）",
                  Path(resp.path).resolve() == inside.resolve())
            outside = Path(tmp).parent / "__tk_outside_probe__.txt"
            outside.write_text("x", encoding="utf-8")
            try:
                ca.export_download(path=str(outside))
                blocked = False
            except HTTPException as exc:
                blocked = exc.status_code == 403
            finally:
                outside.unlink(missing_ok=True)
            check("导出目录之外的路径仍被拒（403）", blocked)

        bad = ("R:/tk-not-here/exports" if os.name == "nt"
               else "/proc/tk-not-here/exports")
        try:
            ca._ensure_setting_dirs({"export_dir": bad})
            rejected = False
        except HTTPException as exc:
            rejected = exc.status_code == 400
        check("目录不可用时保存设置报 400（不半保存）", rejected)
    finally:
        ca.ctx = old_ctx


def g8() -> None:
    """G8 高清链路：并发守卫、上次参数预选、兜底值唯一、旧壳删净。"""
    services_src = (APP_DIR / "server" / "services.py").read_text(encoding="utf-8")
    api_src = (APP_DIR / "server" / "api.py").read_text(encoding="utf-8")
    app_js = (APP_DIR / "frontend" / "js" / "app.js").read_text(encoding="utf-8")

    # 1) 「同时只能跑一个高清任务」的守卫必须由启动接口持有。
    #    原来占用标志只由 Gradio 时代的 handle_mixed() 写，界面改走
    #    /api/hd/start 之后没人再写它 —— 409 永远不触发，
    #    连点两次「开始处理」会真的起两个线程抢显存、写同一个输出目录。
    check("hd_worker_begin/end/busy 三个助手存在",
          all(x in services_src for x in ("def hd_worker_begin(",
                                          "def hd_worker_end(",
                                          "def hd_worker_busy(")))
    check("启动接口先占用、无论成败都在 finally 里释放",
          "if not c.hd_worker_begin():" in api_src
          and "def _run_hd():" in api_src
          and "c.hd_worker_end()" in api_src)
    check("不再直接读 _hd_worker_running（旧写法）",
          "c._hd_worker_running" not in api_src)

    from server import services
    services.hd_worker_end()
    first = services.hd_worker_begin()
    second = services.hd_worker_begin()
    services.hd_worker_end()
    third = services.hd_worker_begin()
    services.hd_worker_end()
    check("第二个高清任务被拒、让出后可再占用",
          (first, second, third) == (True, False, True),
          f"begin 序列 = {(first, second, third)}")

    # 2) 「上次用的参数」要真的填回表单（_remember_choices 存了却没人读）
    for token in ("pref.image_model", "pref.video_model", "pref.image_scale",
                  "pref.video_sharp", "pref.video_max_side",
                  "pref.max_output_size"):
        check(f"app.js 预选读取 {token}", token in app_js)
    check("高清下拉项不再是写死的默认值",
          'fillOpts($("hd-scale"), m.scales || [], "自动（推荐）")' not in app_js
          and 'fillOpts($("hd-side"), m.sides, "1920")' not in app_js)

    from watermark.core import config as wconfig
    cfg = wconfig.load()
    missing = [k for k in ("image_model", "video_model", "image_scale",
                           "video_scale", "video_sharp", "video_max_side",
                           "face_enhance", "interpolate") if k not in cfg]
    check("配置里带着全部「上次参数」", not missing, f"缺少 {missing}")

    # 3) 同一项设置只能有一个兜底值（界面对 4096，引擎不能按 2048 跑）
    from common import config as cconfig
    image_src = (APP_DIR / "watermark" / "core" / "engines" / "image.py").read_text(
        encoding="utf-8")
    check("face_work_side 兜底值与 DEFAULTS 一致（无 2048 残留）",
          cconfig.DEFAULTS["hd"]["face_work_side"] == 4096
          and "face_work_side\", 2048" not in image_src
          and "facework or 2048" not in services_src)

    # 4) Gradio 时代的死壳不该再回来：它们既吞异常，
    #    又会把「同时只能跑一个」的标志重新写歪。
    legacy = ("def handle_mixed(", "def safe_mixed(", "def safe_image_only(",
              "def safe_video_only(", "def safe_download_one(",
              "def safe_download_all(", "def safe_settings(",
              "def shutdown_hint(")
    left = [n for n in legacy if n in services_src]
    check("Gradio 时代的处理/下载适配壳已删净", not left, f"仍在：{left}")

    dead = {
        "save_all": APP_DIR / "common" / "config.py",
        "PageLoadFailed": APP_DIR / "collector" / "collect" / "exceptions.py",
        "ParseFailed": APP_DIR / "collector" / "collect" / "exceptions.py",
        "CollectTask": APP_DIR / "collector" / "core" / "models.py",
    }
    still = [name for name, path in dead.items()
             if name in path.read_text(encoding="utf-8")]
    check("确认无人引用的死代码已删除", not still, f"仍在：{still}")


def g10() -> None:
    """G10 去水印下载链路：并发守卫必须真的生效 + 完成提醒必须能弹出来。

    两处都是同一类「v4.0 重构遗留」：能跑的代码搬到了新位置，
    旧位置留下的函数没人调用 —— 于是守卫永远不触发、提醒永远不弹。
    """
    services_src = (APP_DIR / "server" / "services.py").read_text(encoding="utf-8")
    api_src = (APP_DIR / "server" / "api.py").read_text(encoding="utf-8")
    jobs_src = (APP_DIR / "server" / "jobs.py").read_text(encoding="utf-8")
    app_js = (APP_DIR / "frontend" / "js" / "app.js").read_text(encoding="utf-8")

    # 1) 「同时只能跑一个下载任务」
    check("tk_worker_begin/end/busy 三个助手存在",
          all(x in services_src for x in ("def tk_worker_begin(",
                                          "def tk_worker_end(",
                                          "def tk_worker_busy(")))
    check("下载接口先占用、无论成败都在 finally 里释放",
          "if not c.tk_worker_begin():" in api_src
          and "def _run_tk():" in api_src
          and "c.tk_worker_end()" in api_src)
    check("不再直接读 _tk_worker_running（旧写法）",
          'getattr(c, "_tk_worker_running"' not in api_src)

    from server import services
    services.tk_worker_end()
    first = services.tk_worker_begin()
    second = services.tk_worker_begin()
    services.tk_worker_end()
    third = services.tk_worker_begin()
    services.tk_worker_end()
    check("第二个下载任务被拒、让出后可再占用",
          (first, second, third) == (True, False, True),
          f"begin 序列 = {(first, second, third)}")

    # 2) 完成提醒：正文要一路带到前端，且只有一个出口决定卡片/桌面浮层
    check("notify 元组带正文（下载 / 高清各分支）",
          'job["notify"] = ("ok", f"下载完成：成功 {ok} 个")' in services_src
          and '("ok", f"高清修复完成：成功 {ok} 项")' in services_src)
    check("jobs.py 把正文放进 NOTIFY 事件",
          "self.emit(EventKind.NOTIFY, level=level, message=message)" in jobs_src)
    check("前端完成提醒只有一个出口（前台卡片 / 后台桌面浮层）",
          "function notifyCompletion(" in app_js
          and "notifyCompletion(msg, kind);" in app_js)
    check("提示音跟着 beep_on_done 设置走",
          "raw.beep_on_done" in app_js)
    check("写了没人调用的旧提醒函数已删除",
          "def _notify(" not in services_src
          and "def do_download(" not in services_src)


def g11() -> None:
    """G11 历史死函数清理：删掉的旧入口不许回来，活着的新入口必须还在。

    这些都是「界面改走 HTTP 之后没人再调用」的 Gradio 时代函数
    （v1.4.3 一次清掉 16 个 / 191 行）。留着它们的代价已经被两次真实故障
    证明：占用标志只由死函数写 → 并发守卫失效；提醒只写在死函数里 →
    用户永远收不到完成通知。所以这里把它们钉死。
    """
    services_p = APP_DIR / "server" / "services.py"
    api_p = APP_DIR / "server" / "api.py"
    services_src = services_p.read_text(encoding="utf-8")
    api_src = api_p.read_text(encoding="utf-8")

    dead = (
        "do_parse", "do_download", "_notify", "handle_mixed", "safe_mixed",
        "safe_image_only", "safe_video_only", "safe_download_one",
        "safe_download_all", "safe_settings", "cancel_run", "cleanup_work",
        "paste_clipboard_into", "clear_inputs", "tk_pause_action",
        "tk_resume_action", "tk_cancel_action", "hd_pause_action",
        "hd_resume_action", "library_open_dir", "_tk_progress_text",
        "_tk_progress_pct", "_hd_progress_pct", "_hd_progress_text",
        "shutdown_hint",
    )
    left = [n for n in dead
            if f"def {n}(" in services_src or f"def {n}(" in api_src]
    check("历史死函数没有复活", not left, f"仍然存在：{left}")
    check("api.py 的 _save_cfg 也已清理", "def _save_cfg(" not in api_src)

    check("活着的替代入口都还在",
          all(x in services_src for x in ("def tk_worker_begin(",
                                          "def hd_worker_begin(",
                                          "def settings_values(",
                                          "def _tk_download_worker("))
          and "def _parse_worker(" in api_src)

    ok = True
    for p in (services_p, api_p):
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as exc:                    # pragma: no cover
            ok = False
            print(f"    {p.name} 语法错误：{exc}")
    check("清理后的两个文件语法正常", ok)


def g12() -> None:
    """G12 日志里的链接看得见作品 id + tikwm 时间槽按「完成时刻」间隔。

    两条都是 2026-10-10 真实日志暴露出来的：
    1. `url[:60]` 会把 61 字符的标准 TikTok 链接切掉 id 末位，日志里看起来
       像「采集到的 id 少了一位」的假故障（其实请求发的是完整链接）；
    2. 只在发送前预约时间槽挡不住「预约了但发送被拖慢」，上一次请求返回后
       0.35 秒就再次发请求 → 撞上 tikwm 的 1 req/s 限流。
    """
    tiktok = APP_DIR / "watermark" / "tiktok"
    urls_src = (tiktok / "urls.py").read_text(encoding="utf-8")
    check("short_url 保留结尾（作品 id 在结尾）",
          "def short_url(" in urls_src and 'return "..." + text[-keep:]' in urls_src)

    users = ("tikwm.py", "douyin.py", "downloader.py")
    heads = {n: (tiktok / n).read_text(encoding="utf-8") for n in users}
    check("三个模块都改用 short_url",
          all("from .urls import short_url" in s for s in heads.values()))
    old = [(n, pat) for n, s in heads.items()
           for pat in ("url[:60]", "url[:50]", "url[:80]", "canonical[:70]")
           if pat in s]
    check("链接日志不再按前 N 个字符截断", not old, f"仍有：{old}")

    tikwm_src = heads["tikwm.py"]
    check("tikwm 请求返回后压一次时间槽",
          tikwm_src.count("_mark_request_done()") == 4   # 1 个 def + 3 个 POST 点
          and "def _mark_request_done(" in tikwm_src)

    sys.path.insert(0, str(APP_DIR))
    try:
        from watermark.tiktok.urls import short_url as _short
        url = "https://www.tiktok.com/@pyone.pay42/video/7693929770291105032"
        check("真实踩坑链接缩略后仍带完整 id",
              _short(url).endswith("7693929770291105032")
              and url[:60].endswith("7693929770291105032") is False)

        from watermark.tiktok import tikwm as _tk
        import time as _time
        old_interval, old_last = _tk._MIN_INTERVAL, _tk._LAST_REQUEST_AT
        try:
            _tk._MIN_INTERVAL = 0.3
            _tk._LAST_REQUEST_AT = 0.0
            _tk._throttle()                    # 第一次立刻放行
            _tk._mark_request_done()           # 记下「刚完成」
            t0 = _time.monotonic()
            _tk._throttle()
            waited = _time.monotonic() - t0
        finally:
            _tk._MIN_INTERVAL, _tk._LAST_REQUEST_AT = old_interval, old_last
        check("下一次请求按上一次完成时刻等待", waited >= 0.25,
              f"只等了 {waited:.3f}s")
    finally:
        sys.path.remove(str(APP_DIR))


def g9() -> None:
    """G9 「重新登录」：后端能清登录态，界面也要有入口。

    反向缺陷（比设置页那一类更隐蔽）：接口一直在，界面从来没有按钮 ——
    登录态失效时用户只能手工去删 data/sessions 目录。
    """
    api_src = (APP_DIR / "server" / "collector_api.py").read_text(encoding="utf-8")
    js_src = (APP_DIR / "frontend" / "js" / "collector.js").read_text(encoding="utf-8")

    check("接口 /accounts/{id}/reset-login 存在",
          '@router.post("/accounts/{account_id}/reset-login")' in api_src)
    check("接口真的删登录态目录并置回 unknown",
          'DATA_DIR / "sessions"' in api_src
          and "login_status = 'unknown'" in api_src)
    check("账号行有「重新登录」按钮并调用该接口",
          "/reset-login`" in js_src and 'btnRelogin' in js_src
          and "重新登录" in js_src)
    check("按钮有二次确认与失败提示",
          "清除 @${a.username} 的登录态？" in js_src
          and 'toast("清除登录态失败：' in js_src)


def g13() -> None:
    """G13 `/api/file` 白名单跟着设置走：换了输出目录不用重启程序。

    缺陷（2026-10-10 复盘）：白名单只在导入时算一次。用户在设置里把
    输出/下载目录换到别处以后，新目录里的图片预览与下载会一直 403
    （`GET /api/file` 拒绝），非得重启程序才好 —— 「设置改了不生效」的一类。
    """
    api_src = (APP_DIR / "server" / "api.py").read_text(encoding="utf-8")
    check("白名单记录了配置文件的修改时间",
          "_ALLOW_STAMP" in api_src and "def _allow_stamp(" in api_src
          and "def _allow_config_paths(" in api_src)
    check("_is_allowed 发现配置变过就重刷",
          "if not _ALLOW_PREFIXES or _allow_stamp() != _ALLOW_STAMP:" in api_src)

    body_settings = api_src.split("async def set_settings(", 1)[1].split("\n@app.", 1)[0]
    body_tk = api_src.split("def tiktok_settings_set(", 1)[1].split("\n@app.", 1)[0]
    check("两个设置保存接口都显式重刷白名单",
          "refresh_allowed_paths()" in body_settings
          and "refresh_allowed_paths()" in body_tk)

    # 行为验证：用一个临时「配置文件」当设置来源，改它以后新目录立刻可用
    import tempfile
    from unittest import mock
    tmp = tempfile.TemporaryDirectory()
    sys.path.insert(0, str(APP_DIR))
    from server import api as api_mod
    old_prefixes, old_stamp, old_sources = (
        api_mod._ALLOW_PREFIXES, api_mod._ALLOW_STAMP, api_mod._ALLOW_SOURCES)
    try:
        base = Path(tmp.name)
        dir_a, dir_b = base / "out-a", base / "out-b"
        for d in (dir_a, dir_b):
            d.mkdir(parents=True, exist_ok=True)
            (d / "x.mp4").write_bytes(b"x")
        cfg = base / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        state = {"root": dir_a}
        with mock.patch.object(api_mod, "_load_cfg", lambda: {"output_root": str(state["root"])}), \
                mock.patch.object(api_mod, "PROJECT_ROOT", base / "proj"), \
                mock.patch.object(api_mod, "UPLOAD_DIR", base / "up"), \
                mock.patch.object(api_mod, "_allow_sources_override", [cfg]):
            api_mod.refresh_allowed_paths()
            before = api_mod._is_allowed(dir_a / "x.mp4"), api_mod._is_allowed(dir_b / "x.mp4")
            state["root"] = dir_b
            import os as _os
            import time as _time
            _time.sleep(0.01)
            cfg.write_text('{"changed": 1}', encoding="utf-8")
            st = cfg.stat()
            _os.utime(cfg, (st.st_atime, st.st_mtime + 1.0))
            after = api_mod._is_allowed(dir_b / "x.mp4"), api_mod._is_allowed(dir_a / "x.mp4")
        check("换目录前：只有旧目录可用", before == (True, False), f"实测 {before}")
        check("换目录后（不重启、不显式重刷）：新目录立刻可用",
              after == (True, False), f"实测 {after}")
    finally:
        api_mod._ALLOW_PREFIXES, api_mod._ALLOW_STAMP, api_mod._ALLOW_SOURCES = (
            old_prefixes, old_stamp, old_sources)
        sys.path.remove(str(APP_DIR))
        tmp.cleanup()


def g14() -> None:
    """G14 前端转义连引号一起处理：昵称/地区带引号不能撑破表单。

    缺陷（2026-10-10 复盘）：app.js 的 `esc()` 只转义 `& < >`，却被用在
    `value="${esc(...)}"` 这种属性位置。昵称/地区/备注里有一个 `"` 就会
    提前闭合属性 —— 表单显示不全，甚至能注入 `onfocus` 之类的属性
    （pywebview 里的前端能直接调本地接口，属性注入不是纯理论问题）。
    """
    js_dir = APP_DIR / "frontend" / "js"
    app_src = (js_dir / "app.js").read_text(encoding="utf-8")
    coll_src = (js_dir / "collector.js").read_text(encoding="utf-8")

    import re as _re

    m = _re.search(r"const esc = [\s\S]*?&#39;\"\);", app_src)
    esc_stmt = m.group(0) if m else ""
    check("esc() 同时转义单双引号",
          '.replace(/"/g, "&quot;")' in esc_stmt
          and ".replace(/'/g, \"&#39;\")" in esc_stmt
          and '.replace(/&/g, "&amp;")' in esc_stmt)

    def _strip_comments(s: str) -> str:
        s = _re.sub(r"/\*[\s\S]*?\*/", "", s)
        return _re.sub(r"(?m)^\s*//.*$", "", s)

    offenders = []
    for name, src in (("app.js", app_src), ("collector.js", coll_src)):
        for mm in _re.finditer(r'="\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"',
                               _strip_comments(src)):
            expr = mm.group(1).strip()
            if "esc(" in expr or "counts[0]" in expr:
                continue
            if _re.fullmatch(r'[A-Za-z_$][\w$.\[\]?]*|""|\'\'', expr):
                continue
            offenders.append((name, expr))
    check("属性位置的插值都走 esc()（或纯数字）", not offenders, f"未转义：{offenders}")

    # 明确的内联 HTML 出口（H(...)）也必须逐个转义
    h_calls = _re.findall(r"\bH\(\s*`([^`]*)`", coll_src)
    bad_h = [c for c in h_calls
             if _re.search(r"\$\{(?!esc\(|shortTime\(|[a-zA-Z_$][\w$.]*\s*\|\||\s*[0-9]+\s*\})", c)]
    check("H(...) 内联 HTML 里的插值也都转义了", not bad_h, f"可疑：{bad_h}")

    node = shutil.which("node")
    if not node:
        check("node 可用（前端转义行为验证）", True, "跳过：node 不在 PATH")
        return
    import json as _json
    payload = '小"明 onfocus=alert(1)'
    script = (esc_stmt + "\n"
              "const p = " + _json.dumps(payload) + ";\n"
              "const old = (v) => String(v == null ? '' : v)"
              ".replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
              "process.stdout.write(JSON.stringify([esc(p), old(p)]));")
    proc = subprocess.run([node, "-e", script], capture_output=True,
                          text=True, encoding="utf-8", timeout=30)
    if proc.returncode != 0:
        check("node 跑得动 esc()", False, proc.stderr[-160:])
        return
    new_out, old_out = _json.loads(proc.stdout)
    check("带引号的昵称转义后没有裸引号",
          '"' not in new_out and "&quot;" in new_out, f"实测 {new_out!r}")
    check("老写法确实会被引号撑破（反向对照）",
          '"' in old_out, f"实测 {old_out!r}")


def g15() -> None:
    """G15 子进程超时：卡住的 ffmpeg 不能把任务线程永久占住。

    缺陷（2026-10-10 审计）：`run_quiet()` / `probe_video()` /
    `_pick_encoders()` / 工具页两处 `subprocess.run()` 都没有 `timeout=`。
    ffmpeg 一旦卡住（输出文件被播放器占用、磁盘掉线、半截文件、驱动挂死），
    任务线程永远停在 subprocess 里，`hd_worker_begin()` / `tk_worker_begin()`
    占用的标志不释放 —— 按钮一直转，再点报「已有任务在运行」，只能重启程序。
    """
    utils_src = (APP_DIR / "watermark" / "core" / "utils.py").read_text(encoding="utf-8")
    video_src = (APP_DIR / "watermark" / "core" / "engines" / "video.py").read_text(
        encoding="utf-8")
    svc_src = (APP_DIR / "server" / "services.py").read_text(encoding="utf-8")

    check("utils 定义了探测/编码两档超时上限",
          "PROBE_TIMEOUT = 90" in utils_src and "ENCODE_TIMEOUT = 3600" in utils_src)
    check("超时异常是 RuntimeError 子类（旧的编码器回退才 catch 得到）",
          "class SubprocessTimeout(RuntimeError):" in utils_src)
    check("run_quiet 默认带超时，且超时被翻译成可读错误",
          "def run_quiet(cmd: list, timeout: int | None = None" in utils_src
          and "timeout=limit" in utils_src
          and "raise SubprocessTimeout(cmd, limit) from exc" in utils_src)
    check("ffprobe 探测带超时并给出人话错误",
          "timeout=PROBE_TIMEOUT" in video_src
          and "读取视频信息超时" in video_src)
    check("编码器探测带超时",
          video_src.count("timeout=PROBE_TIMEOUT") == 2,
          f"实测 {video_src.count('timeout=PROBE_TIMEOUT')} 处")
    check("两处编码器回退循环都 catch 超时",
          video_src.count(
              "except (subprocess.CalledProcessError, SubprocessTimeout) as exc:") == 2)
    check("超时信息会出现在「所有编码器均失败」的原因里",
          "isinstance(exc, SubprocessTimeout)" in video_src)
    check("工具页两处 ffmpeg 调用带超时并返回可读错误",
          svc_src.count("timeout=ENCODE_TIMEOUT") == 2
          and svc_src.count("except subprocess.TimeoutExpired:") == 2)

    # 行为验证：真起一个睡眠子进程，必须很快抛超时（而不是等 30 秒）
    import time as _time
    sys.path.insert(0, str(APP_DIR))
    from watermark.core.utils import SubprocessTimeout, run_quiet  # noqa: PLC0415

    t0 = _time.time()
    try:
        run_quiet([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        check("睡眠子进程在超时上限处被中止", False, "没有超时，命令直接跑完了")
    except SubprocessTimeout as exc:
        used = _time.time() - t0
        check("睡眠子进程在超时上限处被中止", used < 15, f"实测 {used:.1f}s")
        check("超时错误写清了「哪个程序超了多久」",
              "执行超过 1 秒" in str(exc) and "python" in str(exc).lower(),
              f"实测 {str(exc)[:80]}")


def g16() -> None:
    """G16 替换式记录的后悔药：登录态失效不再把历史记录清空。

    缺陷（2026-10-10 审计）：替换式记录是「先删旧记录、再采新记录」，
    而登录态失效（`ensure_login` 抛 LoginRequired）发生在**删除之后**。
    于是「重新登录前点一次采集」＝ 每个所选账号的旧记录被挨个删空、
    新记录一条没有。修法：删除前留快照，账号失败且零入库就写回；
    首个账号确认登录失效即熔断，后面的账号连删都不删。
    """
    db_src = (APP_DIR / "collector" / "db" / "database.py").read_text(encoding="utf-8")
    eng_src = (APP_DIR / "collector" / "collect" / "engine.py").read_text(encoding="utf-8")
    api_src = (APP_DIR / "server" / "collector_api.py").read_text(encoding="utf-8")

    check("数据库层提供快照与写回两个方法",
          "def snapshot_videos_for_account(self, account_id: str)" in db_src
          and "def restore_videos(self, rows" in db_src)
    check("写回是 INSERT OR IGNORE（不重复、不覆盖新记录）",
          "INSERT OR IGNORE INTO videos({cols})" in db_src)
    check("快照与写回共用同一组字段（9 列）",
          db_src.count("_VIDEO_COLUMNS") >= 4)
    check("清除账号旧记录时同时产出回滚快照",
          "removed, snapshot = self._clear_account_videos(acc)" in eng_src)
    helper = eng_src[eng_src.index("def _clear_account_videos"):
                     eng_src.index("# ---- 单账号采集 ----")]
    check("快照在删除之前取（删完再拍等于没拍）",
          helper.index("snapshot = self.db.snapshot_videos_for_account")
          < helper.index("removed = self.db.delete_videos_for_account"))
    check("只有「失败且一条未采到」才回滚",
          'if snapshot and result.status == "failed" and result.actual_count == 0:' in eng_src
          and "back = self.db.restore_videos(snapshot)" in eng_src)
    check("登录态失效会熔断（后面的账号不清旧记录）",
          "self._abort_reason = \"登录态失效，任务已停止（未清除旧记录）\"" in eng_src
          and "if self._abort_reason:" in eng_src
          and eng_src.index("if self._abort_reason:")
          < eng_src.index("removed, snapshot = self._clear_account_videos(acc)"))
    check("汇总里带上回滚/熔断计数",
          '"restored": 0' in eng_src and '"login_aborted": False' in eng_src)
    check("结果文案说明记录没丢",
          "已回滚保留" in api_src and "登录态失效已提前停止" in api_src)

    # 行为验证：真跑一遍「登录失效的两账号采集」，旧记录必须一条不少
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "collector.tests.test_replacement_rollback"],
        cwd=str(APP_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    ok = r.returncode == 0 and "\nOK" in (r.stderr or "")
    tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
    check("回滚/熔断的行为测试全绿（真库真引擎）", ok, f"实测 {tail[0][:60]}")


def g17() -> None:
    """G17 链接日志不再被「没采到链接的运行」清空。

    缺陷（2026-10-10 审计）：`DailyLinksLog.replace()` 用 "w" 打开，
    传空列表 = 把当日文件清成 0 字节。而引擎里是**无条件**调用它：
    登录态失效、全部账号失败、刚开跑就被停止 —— 这些运行一条链接都没有，
    却会把上一次成功采集留下的当日链接日志删掉，且没有任何提示。
    """
    eng_src = (APP_DIR / "collector" / "collect" / "engine.py").read_text(encoding="utf-8")
    links_src = (APP_DIR / "collector" / "log" / "daily_links.py").read_text(encoding="utf-8")
    api_src = (APP_DIR / "server" / "collector_api.py").read_text(encoding="utf-8")

    check("只有真的采到链接才整体替换当日链接日志",
          "if self._run_urls:" in eng_src
          and "summary[\"links_written\"] = self.daily_links.replace(" in eng_src)
    check("没采到链接时读取并保留当日文件（不写空）",
          "kept = self.daily_links.load(day=target_date)" in eng_src
          and 'summary["links_kept"] = len(kept)' in eng_src
          and "保留当日链接日志原样" in eng_src)
    check("汇总里带上写入/保留两个计数",
          '"links_written": 0' in eng_src and '"links_kept": 0' in eng_src)
    check("结果文案说明链接日志被保留",
          "当日链接日志保留" in api_src)
    check("replace 的空列表语义在文档里写明（不是靠调用方猜）",
          "传空列表" in links_src and "0 字节" in links_src)

    # 行为验证：先写两份链接，再跑一次「登录失效」的采集，文件必须原样
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "collector.tests.test_replacement_rollback"],
        cwd=str(APP_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    ok = r.returncode == 0 and (r.stderr or "").strip().endswith("OK")
    tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
    check("链接日志保留的行为测试全绿", ok, f"实测 {tail[0][:60]}")


def g18() -> None:
    """G18 「下载后自动送入高清（仅图片）」不再是死开关。

    缺陷（2026-10-10 审计）：勾选框 `#tk-auto-hd` 只把 auto_hd 发给后端，
    后端 `_tk_download_worker` 算出 `job["auto_hd"] = 图片数量` 之后，
    **前端没有任何代码读它**、也没有任何地方启动高清任务 —— 勾了等于没勾。
    """
    app_src = (APP_DIR / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    html_src = (APP_DIR / "frontend" / "index.html").read_text(encoding="utf-8")
    svc_src = (APP_DIR / "server" / "services.py").read_text(encoding="utf-8")

    check("前端有了挑图片的纯函数（只看 is_image + path）",
          "function pickAutoHdImages(outputs)" in app_src
          and ".filter((o) => o && o.is_image && o.path)" in app_src)
    check("勾选状态在开始下载前记录一次，请求与后续判断用同一个值",
          'const wantAutoHd = $("tk-auto-hd").checked;' in app_src
          and "auto_hd: wantAutoHd," in app_src
          and 'auto_hd: $("tk-auto-hd").checked' not in app_src)
    check("按勾选收集本次下载到的图片",
          "if (wantAutoHd) autoHdImages = pickAutoHdImages(outputs);" in app_src)
    check("下载任务结束时接线并有守卫（勾选 + 成功 + 有图片）",
          "onDone: (kind) => {" in app_src
          and 'if (!wantAutoHd || kind !== "job_done" || !autoHdImages.length) return;'
              in app_src)
    check("真的把图片交给高清页（入列 + 切页 + 走既有开始流程）",
          "hdServerFiles = hdServerFiles.concat(paths);" in app_src
          and "renderPicked();" in app_src
          and 'activatePage("hd");' in app_src
          and 'const btn = $("hd-start");' in app_src
          and "btn.click();" in app_src)
    check("高清任务在跑时不硬起第二个（只入列，避免 409）",
          "if (hdBusy) {" in app_src and "稍后点「开始处理」" in app_src)
    check("勾选框文案与实现口径一致（自动送入高清 / 仅图片）",
          'id="tk-auto-hd"' in html_src and "自动送入高清" in html_src
          and "仅图片" in html_src)
    check("后端仍然只把图片计入自动高清（与前端同一口径）",
          'job["auto_hd"] = len(_tk_recent_files)' in svc_src
          and "_tk_recent_files = [p for p in downloaded_files" in svc_src)

    # 行为验证：node 真跑 pickAutoHdImages（含「不过滤会把视频也送进高清」的反向对照）
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "collector.tests.test_auto_hd"],
        cwd=str(APP_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    ok = r.returncode == 0 and (r.stderr or "").strip().endswith("OK")
    tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
    check("自动高清的行为测试全绿", ok, f"实测 {tail[0][:60]}")


def g19() -> None:
    """G19 配置默认值里不再有「没人读的死键」。

    缺陷（2026-10-11 审计）：`common/config.py` 的 DEFAULTS 里躺着 15 个键，
    全仓库没有任何读取点，界面上也没有控件 —— 它们照旧写进用户的
    config.json，看起来像有这些开关（其中 lan_password 更容易让人误以为
    局域网访问有密码保护）。清理后由用例
    `collector/tests/test_settings_keys_are_live.py` 长期钉住。
    """
    cfg_src = (APP_DIR / "common" / "config.py").read_text(encoding="utf-8")
    defaults_src = cfg_src.split("_lock = threading.RLock()")[0]
    example = json.loads((APP_DIR.parent / "config.example.json").read_text(encoding="utf-8"))

    dead = ["theme", "language", "first_run", "window",
            "default_date", "auto_open_browser_on_start", "browser_type",
            "auto_save", "task_interval_ms", "error_wait_ms",
            "lan_password", "tk_concurrent", "tk_retries", "tk_quality", "tk_proxy"]
    still_there = [k for k in dead if f'"{k}"' in defaults_src]
    check("DEFAULTS 里的 15 个死键都删掉了", not still_there,
          f"还剩 {still_there}" if still_there else "15 个键已清空")

    from common import config as _cconfig
    for sec, keys in _cconfig.DEFAULTS.items():
        check(f"示例配置 [{sec}] 与 DEFAULTS 键集一致",
              sorted(example.get(sec, {})) == sorted(keys),
              f"示例 {len(example.get(sec, {}))} / 默认 {len(keys)}")
    check("示例配置没有多余分区",
          sorted(example) == sorted(_cconfig.DEFAULTS), str(sorted(example)))

    from server import collector_api as _ca
    extra = [k for k in _ca._COLLECTOR_SETTING_KEYS
             if k not in _cconfig.DEFAULTS["collector"]]
    check("采集设置白名单只暴露 DEFAULTS 里真实存在的键",
          not extra and "task_interval_ms" not in _ca._COLLECTOR_SETTING_KEYS
          and "error_wait_ms" not in _ca._COLLECTOR_SETTING_KEYS,
          f"多余 {extra}")

    check("verify_w1 不再用固定数字断言 collector 键数（删键不再误伤验收）",
          "len(col) >= 28" not in (APP_DIR.parent / "scripts" / "verify_w1.py").read_text(
              encoding="utf-8")
          and "旧采集器配置的每个键都并进了 collector 分区"
              in (APP_DIR.parent / "scripts" / "verify_w1.py").read_text(encoding="utf-8"))

    # 行为验证：真跑「每个默认键都必须有人读」的用例
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "collector.tests.test_settings_keys_are_live"],
        cwd=str(APP_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    ok = r.returncode == 0 and (r.stderr or "").strip().endswith("OK")
    tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
    check("配置死键扫描的行为测试全绿", ok, f"实测 {tail[0][:60]}")


def g20() -> None:
    """G20 三个板块共用一个界面内弹窗（绝不再弹到桌面右下角）。

    缺陷（2026-10-11 用户第三次反馈）：app.js 与 collector.js 各有一套完成提醒，
    而且都按窗口焦点分流 —— 前台画卡片、后台 POST /api/system/notify 让后端用
    tkinter 弹桌面浮层。pywebview 里 document.hasFocus() 经常是 false，于是
    「去水印 / 高清修复」几乎总是弹到桌面右下角，而「采集链接」又是另一套长相。
    现在只保留 app.js 的 notifyCompletion()（画 #notify-stack 卡片），
    collector.js 通过 window.TKStudio 调它；两个分流开关一并删除。

    缺陷（2026-10-11 用户第四次反馈）：「弹窗通知没有设置时长吗？每次都要我
    把弹窗点掉」——同一个 hasFocus 坑的残留：窗口没焦点就 timeout=0 常驻。
    现在卡片停留时长由设置项 notify_duration 决定（默认 9 秒，0 = 不自动关
    闭，范围内收敛到 3~120 秒），窗口不可见时挂起倒计时、回来自动续上。
    """
    root = APP_DIR.parent
    app = (APP_DIR / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    col = (APP_DIR / "frontend" / "js" / "collector.js").read_text(encoding="utf-8")
    css = (APP_DIR / "frontend" / "css" / "app.css").read_text(encoding="utf-8")
    html = (APP_DIR / "frontend" / "index.html").read_text(encoding="utf-8")

    check("全前端只有一个 notifyCompletion（三个板块共用）",
          app.count("function notifyCompletion(") == 1
          and "function notifyCompletion(" not in col
          and "window.TKStudio.notifyCompletion" in col
          and "shared(message, kind, title, { sound });" in col,
          f"app.js {app.count('function notifyCompletion(')} 处 / collector 有委托="
          + str("window.TKStudio.notifyCompletion" in col))

    check("完成提醒一定画成界面内卡片，而且卡片自己有停留时长（不用手点）",
          "Notify.show({ title, message: msg, kind, timeout: Notify.durationMs() });"
          in app
          and "const seen = !document.hidden && document.hasFocus();" not in app
          and 'if (Number(timeout) === 0) card.classList.add("notify-sticky");' in app
          and ".notify-sticky .notify-progress { display: none; }" in css)

    stack = css.split("#notify-stack {")[1].split("}")[0]
    check("卡片锚在程序窗口右下角（不是屏幕右下角）",
          "position: fixed" in stack and "right: 20px" in stack
          and "bottom: 56px" in stack)

    check("任何完成路径都不再申请桌面浮层",
          "desktop: true" not in app and "desktop: true" not in col
          and 'api("/api/system/notify"' not in col
          and "useDesktop" not in col
          and "notify_desktop_when_unfocused" not in app)

    check("删掉的辅助函数没有残留调用点（否则前端启动就 ReferenceError）",
          "syncAwayNotice" not in app and "syncAwayNotice" not in col
          and "awayNotice" not in app and "awayNotice" not in col)

    from common import config as _cconfig
    from server import collector_api as _ca
    example = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    gone = ("notify_in_app", "notify_desktop_when_unfocused")
    check("两个分流开关从配置 / 接口 / 界面里彻底消失",
          all(k not in _cconfig.DEFAULTS["collector"] for k in gone)
          and all(k not in _ca._COLLECTOR_SETTING_KEYS for k in gone)
          and all(k not in example.get("collector", {}) for k in gone)
          and "cset-notify-inapp" not in html and "cset-notify-away" not in html)
    check("卡片尺寸 / 比例两个设置键仍在（尺寸可调这条需求）",
          "notify_card_width" in _cconfig.DEFAULTS["collector"]
          and "notify_card_scale" in _cconfig.DEFAULTS["collector"]
          and "cset-notify-width" in html and "cset-notify-scale" in html)

    check("停留时长可在设置里改：0 = 不自动关闭，否则收敛到 3~120 秒",
          "notify_duration" in _cconfig.DEFAULTS["collector"]
          and "notify_duration" in _ca._COLLECTOR_SETTING_KEYS
          and "notify_duration" in example.get("collector", {})
          and _ca._notify_duration(0) == 0 and _ca._notify_duration(9) == 9
          and _ca._notify_duration(1) == 3 and _ca._notify_duration(999) == 120
          and _ca._notify_duration("abc") == 9,
          f"默认 {_cconfig.DEFAULTS['collector']['notify_duration']}s / 收敛 "
          f"{_ca._notify_duration(1)}~{_ca._notify_duration(999)}s")

    check("窗口看不见时倒计时挂起、回来自动续上（既不漏消息也不用点掉）",
          'document.addEventListener("visibilitychange", () => {' in app
          and "function holdCard(card) {" in app
          and "function resumeHeld() {" in app
          and "if (document.hidden) holdCard(card);" in app
          and 'if (card.dataset.paused === "1") return;' in app
          and 'if (card.dataset.paused !== "1") return;' in app)

    check("时长设置真的接到界面上（下拉框 + 回填 + 保存 + 启动读取）",
          'notifyDuration: "cset-notify-duration",' in col
          and "notify_duration: num(CSET.notifyDuration)," in col
          and "values.notify_duration == null ? 9" in col
          and 'id="cset-notify-duration"' in html
          and "const d = Number(m.notify_duration);" in app
          and 'const d = await api("/api/collector/settings");' in app)

    r = subprocess.run(
        [sys.executable, "-m", "unittest", "collector.tests.test_notify_card"],
        cwd=str(APP_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    ok = r.returncode == 0 and (r.stderr or "").strip().endswith("OK")
    tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
    check("统一弹窗的行为 / 源码测试全绿", ok, f"实测 {tail[0][:60]}")


if __name__ == "__main__":
    print("=" * 68)
    print("W2 验收：采集内核移植")
    print("=" * 68)
    g1()
    g2()
    g3()
    g4()
    g5()
    g6()
    g7()
    g8()
    g9()
    g10()
    g11()
    g12()
    g13()
    g14()
    g15()
    g16()
    g17()
    g18()
    g19()
    g20()
    print("\n" + "=" * 68)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
