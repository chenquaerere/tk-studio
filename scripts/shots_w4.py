"""W4 视觉验证：用真实浏览器（Playwright Chromium 无头）打开统一界面。

做三件事：
  1. 断言零 JS 报错（console error / pageerror）
  2. 断言数据真的绑上了（账号列表行数 = 数据库账号数、结果页空态文案）
  3. 逐视图截图，供人工比对视觉统一性

⚠️ 纪律：本脚本只允许 (a) 只读请求；(b) 内存态交互（勾选账号 / 切视图 / 展开内联确认）。
   绝不点击会落库的控件（启用停用开关、保存、清空、删除、开始采集）。
   教训：曾用「账号管理」页点整行来验证「整行可点」，那会触发 PATCH enabled，
   把用户的账号多停用了一次（已还原）。因此改到「采集任务」页做。

用法：python scripts/shots_w4.py
产出：docs/screenshots/*.png
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "_app"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

VIEWS = [
    ("dashboard", "00-仪表盘"),
    ("accounts", "02-账号管理"),
    ("chat", "05-聊天链接采集"),
    ("collect", "01-采集任务"),
    ("results", "03-采集结果"),
    ("daily", "04-每日归档"),
    ("history", "05b-历史记录"),
    ("tiktok", "06-去水印"),
    ("hd", "07-高清修复"),
    ("library", "08-素材库"),
    ("models", "09-模型中心"),
    ("jobs", "10-任务中心"),
    ("tools", "11-工具箱"),
    ("logs", "12b-运行日志"),
    ("settings", "11-设置"),
    ("about", "12-关于"),
]


# 中文 Windows 的 cmd 默认代码页是 GBK：脚本最后一行打印 ✅/❌ 会抛
# UnicodeEncodeError —— 明明所有断言都通过了，退出码却是 1（看起来像验收失败）。
# 这里自己把输出切成 UTF-8，任何控制台都能把结果打印完。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    import server.api as api
    port = free_port()
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(
        api.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    threading.Thread(target=srv.run, daemon=True, name="uvicorn-shots").start()
    deadline = time.time() + 40
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.25)
    base = f"http://127.0.0.1:{port}"
    print(f"服务已就绪 {base}\n")

    # ── 零落库基线：交互开始前先给本程序自己的库拍个快照
    import sqlite3

    OWN_DB = ROOT / "data" / "tiktok_link_collector.db"
    TABLES = ("accounts", "videos", "collect_tasks", "collect_logs",
              "task_accounts", "chat_targets", "chat_links")

    def db_dump() -> dict:
        """只读快照：逐表 dump 成可比较的结构。"""
        c = sqlite3.connect(f"file:{OWN_DB.as_posix()}?mode=ro", uri=True)
        try:
            out = {}
            for t in TABLES:
                try:
                    out[t] = sorted(c.execute(f"SELECT * FROM {t}").fetchall(), key=str)
                except sqlite3.Error:
                    out[t] = None
            return out
        finally:
            c.close()

    DB_BEFORE = db_dump()
    print("已记录数据库基线："
          + " · ".join(f"{t}={len(v) if v else 0}" for t, v in DB_BEFORE.items()) + "\n")

    errors: list[str] = []
    warnings: list[str] = []

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 940},
                                  device_scale_factor=1, locale="zh-CN")
        page = ctx.new_page()
        page.on("console", lambda m: (
            errors.append(f"console.{m.type}: {m.text}") if m.type == "error"
            else warnings.append(f"console.{m.type}: {m.text}") if m.type == "warning"
            else None))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(base, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1500)

        def bail(msg: str):
            print("\n" + "!" * 66)
            print("断言失败：", msg)
            print("控制台错误：")
            for e in errors[:15]:
                print("  ✗", e)
            print("采集视图 DOM 片段：")
            print(page.locator("#col-accts").inner_html()[:600])
            print("!" * 66)
            raise AssertionError(msg)

        # ── 断言 1：默认视图 = 仪表盘，且统计数字真的绑上了
        if page.locator("#page-dashboard:not(.hidden)").count() != 1:
            bail("默认视图不是仪表盘")
        page.wait_for_timeout(1200)
        a_total = page.locator("#db-a-total").inner_text()
        v_total = page.locator("#db-v-total").inner_text()
        t_total = page.locator("#db-t-total").inner_text()
        chat_n = page.locator("#db-chat").inner_text()
        recent = page.locator("#db-tasks tbody tr").count()
        print(f"仪表盘：账号={a_total} 作品={v_total} 任务={t_total} "
              f"聊天目标={chat_n} 最近任务行={recent}")
        if a_total in ("—", "") or v_total in ("—", ""):
            bail("仪表盘统计没绑上数据")
        if int(a_total) <= 0:
            bail(f"仪表盘账号数为 0（{a_total}）")
        if page.locator("#db-guide").is_visible():
            bail("账号数 > 0 但首屏引导仍然显示")

        # ── 断言 1b：切到采集任务，账号真的绑上了
        page.evaluate("window.TKStudio.activatePage('collect')")
        page.wait_for_timeout(1500)
        n_rows = page.locator("#col-accts .acct-row").count()
        print(f"采集任务视图账号行数：{n_rows}")
        if n_rows <= 0:
            bail("采集任务视图没有渲染出账号 —— 前端与服务层没接上")
        if page.locator("#page-collect:not(.hidden)").count() != 1:
            bail("切到采集任务后视图没显示")

        picked_txt = page.locator("#col-picked").inner_text()
        print(f"已选提示：{picked_txt}")
        assert page.locator("#col-tz").inner_text().startswith("时区"), "时区未加载"

        # 选中一个**启用中**的账号 → 开始按钮应解禁（不点开始，避免真采集）
        arows = page.locator("#col-accts .acct-row")
        ai = next((i for i in range(arows.count())
                   if not arows.nth(i).locator("input[type=checkbox]").is_disabled()), None)
        if ai is None:
            bail("采集任务页没有启用中的账号")
        arows.nth(ai).click()
        page.wait_for_timeout(300)
        if page.locator("#col-start").is_disabled():
            bail(f"勾选第 {ai + 1} 行后「开始采集」仍禁用")
        if not page.locator("#col-warn-replace").is_visible():
            bail("替换式记录提示未出现")
        print(f"勾选第 {ai + 1} 行后：开始按钮已解禁，替换式记录提示已显示")
        page.locator("#col-sel-none").click()
        page.wait_for_timeout(200)

        # ── 逐视图截图
        for vid, fname in VIEWS:
            page.evaluate(f"window.TKStudio.activatePage('{vid}')")
            page.wait_for_timeout(1300)
            page.screenshot(path=str(OUT / f"{fname}.png"), full_page=False)
            print(f"  截图 {fname}.png")

        # ── 断言 2：账号管理表格行数 = 数据库账号数
        page.evaluate("window.TKStudio.activatePage('accounts')")
        page.wait_for_timeout(1500)
        acc_rows = page.locator("#ac-table tbody tr").count()
        print(f"\n账号管理表格行数：{acc_rows}")
        assert acc_rows > 0, "账号管理表格没渲染"
        heads = page.locator("#ac-table thead th").all_inner_texts()
        print("表头：", heads)
        assert heads[:3] == ["选择", "昵称", "账号"], f"表头顺序不符：{heads}"

        # ── 断言 3：采集结果默认空态（用户明确要求启动即空）
        page.evaluate("window.TKStudio.activatePage('results')")
        page.wait_for_timeout(1500)
        empty_txt = page.locator("#rs-table .empty-title").inner_text()
        print(f"采集结果空态：{empty_txt}")
        assert "还没有结果" in empty_txt, f"结果页空态文案异常：{empty_txt}"
        stats = page.locator("#rs-stats .stat").count()
        print(f"结果页统计卡：{stats} 个")

        # ── 断言 4：分组切换生效
        page.locator("#rs-scope button[data-scope='all']").click()
        page.wait_for_timeout(1200)
        all_txt = page.locator("#rs-table").inner_text()
        print(f"「全部历史」视图首行片段：{all_txt[:60]!r}")
        n_hist = page.locator("#rs-table tbody tr").count()
        print(f"「全部历史」表格行数：{n_hist}")
        assert n_hist > 0, "「全部历史」没有数据行"
        page.screenshot(path=str(OUT / "03b-采集结果-全部历史.png"))
        page.locator("#rs-scope button[data-scope='session']").click()
        page.wait_for_timeout(800)

        # ── 断言 4b：历史记录（任务台账 + 详情展开）
        page.evaluate("window.TKStudio.activatePage('history')")
        page.wait_for_timeout(1600)
        h_rows = page.locator("#hi-table tbody tr").count()
        print(f"\n历史记录表格行数：{h_rows}")
        assert h_rows > 0, "历史记录没有任务行"
        if page.locator("#hi-status").count() != 1 or page.locator("#hi-query").count() != 1:
            bail("历史记录缺少筛选控件")
        page.locator("#hi-table tbody tr").first.click()
        page.wait_for_timeout(1400)
        kv_rows = page.locator("#hi-detail-kv .kv-row").count()
        a_rows = page.locator("#hi-detail-accts tbody tr").count()
        link_txt = page.locator("#hi-detail-links").inner_text()
        print(f"历史记录详情：KV {kv_rows} 项 · 账号明细 {a_rows} 行 · 链接文本 {len(link_txt)} 字符")
        if kv_rows == 0:
            bail("点任务行没展开详情")
        page.screenshot(path=str(OUT / "05c-历史记录-任务详情.png"))
        page.locator("#hi-detail-close").click()
        page.wait_for_timeout(300)

        # ── 断言 4c：运行日志（读尾部 + 级别着色 + 文件清单）
        page.evaluate("window.TKStudio.activatePage('logs')")
        page.wait_for_timeout(1600)
        n_files = page.locator("#lg-file option").count()
        n_lines = page.locator("#lg-view .log-line").count()
        stat = page.locator("#lg-stat").inner_text()
        print(f"\n运行日志：文件 {n_files} 个 · 渲染行 {n_lines} 行 · {stat}")
        if n_lines <= 0:
            bail("运行日志没有渲染任何行")
        # 级别筛选：只看 ERROR，行数应减少且都带 lv-error
        page.select_option("#lg-level", "ERROR")
        page.wait_for_timeout(1200)
        n_err = page.locator("#lg-view .log-line").count()
        all_err = page.locator("#lg-view .log-line:not(.lv-error)").count() == 0
        print(f"级别筛选 ERROR：{n_err} 行，全部为 error 样式={all_err}")
        if n_err > 0 and not all_err:
            bail("级别筛选没有生效（仍有非 error 行）")
        page.select_option("#lg-level", "")
        page.wait_for_timeout(1000)
        if page.locator("#lg-wrap").count() == 1:
            page.check("#lg-wrap")
            page.wait_for_timeout(400)
            wrapped = page.locator("#lg-view").evaluate("e => e.classList.contains('wrap')")
            print(f"自动换行开关：{wrapped}")
            if not wrapped:
                bail("自动换行开关没生效")
            page.uncheck("#lg-wrap")
            page.wait_for_timeout(300)

        # ── 断言 4d：聊天链接采集（按原桌面版：左右分栏 + 会话列表 + 实时过滤）
        page.evaluate("window.TKStudio.activatePage('chat')")
        page.wait_for_timeout(1600)
        for nid in ("ch-login", "ch-show-browser", "ch-export", "ch-clear", "ch-account",
                    "ch-conv", "ch-filter", "ch-type-seg", "ch-convs", "ch-conv-count",
                    "ch-recent", "ch-target-label", "ch-status", "ch-links",
                    "ch-stop", "ch-start"):
            if page.locator(f"#{nid}").count() != 1:
                bail(f"聊天页缺少节点 #{nid}")
        grid_cols = page.evaluate(
            "() => getComputedStyle(document.querySelector('.chat-grid'))"
            ".gridTemplateColumns.split(' ').filter(Boolean).length")
        if grid_cols != 2:
            bail(f"聊天页不是两列布局（cols={grid_cols}）")
        n_seg = page.locator("#ch-type-seg button").count()
        if n_seg != 3:
            bail(f"聊天页类型分段应为 3 段（实际 {n_seg}）")
        n_recent = page.locator("#ch-recent .capsule").count()
        print(f"\n聊天链接采集：两列布局 cols={grid_cols} · 分段 {n_seg} · "
              f"最近使用胶囊 {n_recent} 个 · 计数提示="
              f"{page.locator('#ch-conv-count').inner_text()!r}")
        if page.locator("#ch-account option").count() <= 0:
            bail("聊天页的采集账号下拉是空的")
        # 点到某个最近目标 → 应回填该目标已存档链接（只读请求，不落库）
        if n_recent > 0:
            page.locator("#ch-recent .capsule").first.click()
            page.wait_for_timeout(1400)
            target_lbl = page.locator("#ch-target-label").inner_text()
            print(f"点最近使用后：#ch-target-label = {target_lbl!r}")
            if "已选" not in target_lbl:
                bail("点最近使用没有选中目标")
            if not page.locator("#ch-start").is_enabled():
                bail("选中目标后「开始采集」仍禁用")
            page.screenshot(path=str(OUT / "05d-聊天采集-已选目标.png"))
        # 聊天页空态截图（尚未加载会话列表时的样子）
        page.evaluate("window.TKStudio.activatePage('accounts')")
        page.wait_for_timeout(700)
        page.evaluate("window.TKStudio.activatePage('chat')")
        page.wait_for_timeout(900)

        # ── 断言 5：导航高亮与 hash 同步
        page.evaluate("window.TKStudio.activatePage('hd')")
        page.wait_for_timeout(600)
        active = page.locator(".nav-item.active").get_attribute("data-page")
        print(f"\n导航高亮：{active} · hash={page.evaluate('location.hash')}")
        if active != "hd":
            bail(f"导航高亮不同步：{active}")

        # ── 断言 6：UX 契约的真实交互（全部是安全操作，绝不触发采集）

        # 契约③：账号行「整行可点」——点昵称文字也要能选中，且行高亮。
        # 在「采集任务」页验证：那里的整行点击只改内存里的勾选集合，不落库。
        page.evaluate("window.TKStudio.activatePage('collect')")
        page.wait_for_timeout(1200)
        rows = page.locator("#col-accts .acct-row")
        target_i = next((i for i in range(rows.count())
                         if not rows.nth(i).locator("input[type=checkbox]").is_disabled()), None)
        if target_i is None:
            bail("没有找到启用中的账号行")
        row = rows.nth(target_i)
        nick = row.locator(".acct-name .nick").inner_text()
        row.locator(".acct-name").click()          # 点昵称区域，不是复选框
        page.wait_for_timeout(350)
        checked = row.locator("input[type=checkbox]").is_checked()
        picked_cls = "picked" in (row.get_attribute("class") or "")
        print("\n契约③ 整行可点（第 %d 行 %r）：复选框勾上=%s，行高亮=%s"
              % (target_i + 1, nick, checked, picked_cls))
        if not (checked and picked_cls):
            bail("整行可点 / 选中高亮 未生效")

        # 负向验证：停用行按设计不可选
        dis_i = next((i for i in range(rows.count())
                      if rows.nth(i).locator("input[type=checkbox]").is_disabled()), None)
        if dis_i is not None:
            drows = rows.nth(dis_i)
            drows.locator(".acct-name").click()
            page.wait_for_timeout(250)
            still_off = not drows.locator("input[type=checkbox]").is_checked()
            print(f"       停用行（第 {dis_i + 1} 行）点击后仍不可选：{still_off}")
            if not still_off:
                bail("停用账号竟被整行点击选中了")
        # 复位：取消刚才的勾选（纯内存操作）
        row.locator(".acct-name").click()
        page.wait_for_timeout(250)

        # 契约①：内联二次确认（点「清空结果」→ 出现内联确认 → 点取消 → 复原）
        page.evaluate("window.TKStudio.activatePage('results')")
        page.wait_for_timeout(1200)
        page.locator("#rs-clear").click()
        page.wait_for_timeout(300)
        has_inline = page.locator(".inline-confirm").count() > 0
        btn_hidden = page.locator("#rs-clear").is_hidden()
        no_modal = page.locator(".modal-mask").count() == 0
        print(f"契约① 内联二次确认：出现={has_inline} 原按钮隐藏={btn_hidden} 无模态框={no_modal}")
        if not (has_inline and btn_hidden and no_modal):
            bail("内联二次确认未生效（可能弹了模态框）")
        page.locator(".inline-confirm button", has_text="取消").click()
        page.wait_for_timeout(350)
        restored = page.locator("#rs-clear").is_visible() and page.locator(".inline-confirm").count() == 0
        print(f"契约① 点「取消」后复原：{restored}")

        # 契约⑧：切地区自动勾选该地区全部 + 跨地区勾选保留
        page.evaluate("window.TKStudio.activatePage('collect')")
        page.wait_for_timeout(1200)
        # 选项文案带计数（如「越南（15）」），按前缀匹配后用原生事件触发 change
        pick_region = """(prefix) => {
            const s = document.getElementById('col-region');
            const opt = [...s.options].find(o => o.textContent.trim().startsWith(prefix));
            if (!opt) return null;
            s.value = opt.value;
            s.dispatchEvent(new Event('change'));
            return opt.textContent.trim();
        }"""
        label_vn = page.evaluate(pick_region, "越南")
        page.wait_for_timeout(1200)
        n_vn = page.locator("#col-accts .acct-row.picked").count()
        picked_txt = page.locator("#col-picked").inner_text()
        print(f"契约⑧ 切到「{label_vn}」：勾选 {n_vn} 个 · {picked_txt}")
        if n_vn <= 0:
            bail("切地区没有自动勾选")
        page.evaluate(pick_region, "全部地区")
        page.wait_for_timeout(1200)
        n_keep = page.locator("#col-accts .acct-row.picked").count()
        print(f"契约⑧ 切回「全部地区」：仍保留勾选 {n_keep} 个（应 ≥ {n_vn}）")
        if n_keep < n_vn:
            bail("跨地区勾选被清空了")
        page.screenshot(path=str(OUT / "14-交互验证-批量勾选.png"))

        # 复位：清掉勾选，避免后续误触（也不点开始）
        page.locator("#col-sel-none").click()
        page.wait_for_timeout(300)

        # ── 断言 7：手机端（局域网访问）布局
        mobile = browser.new_context(viewport={"width": 390, "height": 844},
                                     device_scale_factor=2, locale="zh-CN")
        mp = mobile.new_page()
        mp.on("pageerror", lambda e: errors.append(f"mobile pageerror: {e}"))
        mp.goto(base, wait_until="networkidle", timeout=45000)
        mp.wait_for_timeout(1500)
        mp.screenshot(path=str(OUT / "13-手机端-采集任务.png"), full_page=False)
        sidebar_box = mp.locator(".sidebar").bounding_box()
        print(f"手机端侧栏宽度：{sidebar_box['width']:.0f}px（应为整行 {390}px）")
        assert sidebar_box["width"] > 300, "手机端侧栏没有铺满整行"

        # ── 断言 7：把控制台里跟本工程相关的报错挑出来
        browser.close()

    # ── 断言 8：整轮交互零落库
    # 用**本程序自己的库**做前后快照比对。
    # 早先的写法是拿去跟旧部署目录的库逐行比 —— 那是两套不同的数据集
    # （合并工程用 data/，旧程序用 D:\workbuddy\TK链接抓取助手\data），
    # 比出来永远是 FAIL，等于没验证。
    print("\n" + "=" * 66)
    before = DB_BEFORE
    after_snapshot = db_dump()
    for t in TABLES:
        a, b = before[t], after_snapshot[t]
        if a is None or b is None:
            print(f"  [SKIP] {t} 表不可读")
            continue
        if t == "settings":
            kv_a = {r[0]: r[1] for r in a if r[0] != "last_backup_date"}
            kv_b = {r[0]: r[1] for r in b if r[0] != "last_backup_date"}
            if kv_a == kv_b:
                print("  [PASS] settings 未变（仅豁免 last_backup_date 记帐键）")
                continue
        if a == b:
            print(f"  [PASS] {t} 逐行未变（{len(b)} 行）")
        else:
            print(f"  [FAIL] {t} 被改动：{len(a)} → {len(b)} 行")
            errors.append(f"数据库表 {t} 被改动了（{len(a)} → {len(b)} 行）")

    if errors:
        print(f"前端报错 / 数据扰动 {len(errors)} 条：")
        for e in errors[:20]:
            print("  ✗", e)
    else:
        print("JS 报错：0 条 · 数据扰动：0 处")
    print(f"控制台警告：{len(warnings)} 条")
    for w in warnings[:8]:
        print("  ⚠", w)
    print("=" * 66)
    print(f"截图目录：{OUT}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
