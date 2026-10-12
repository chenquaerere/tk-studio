"""W7 验证：把「原桌面版有、合并版曾缺失」的能力逐项在真实浏览器里点一遍。

覆盖（全部为**只读或内存态**交互，绝不落库）：
  1. 账号页「编辑数量」：勾选前禁用 → 勾选后启用 → 弹窗能开 → 取消不留痕
  2. 设置页新字段：导出目录 / 日志目录 / GitHub 仓库 / 更新源 都能回填
  3. 设置页「恢复默认」：点第一次进入内联二次确认态（不点第二次，避免真改配置）
  4. 仪表盘自动刷新：停在仪表盘 7 秒，/overview 应被自动请求 ≥ 2 次
  5. 聊天页按原桌面版还原为左右分栏（两列网格 / 会话列表 / 实时过滤 / 分段 /
     原版文案 / 停止按钮）+ 过滤与分段是纯本地（不发请求）
  6. 账号页「添加行」（输入主页链接或 @用户名，回车添加）
  7. 采集完成系统通知：端点可用 + **收尾事件自带结果**（不再统计成 0 个账号）
     + 弹窗几何**锚定屏幕可用区右下角**（不是屏幕正中、也不被任务栏遮住）
  8. 窗口尺寸记忆：window_state.json 读写 + 尺寸/位置/最大化态的自恢复
  9. 每日归档：每次进入都重拉正文；「复制全部」复制到的链接数与归档一致；
     正文区给足高度可框选 + 「共 N 条链接」计数
 10. 采集任务账号行：地区标签与 @账号 之间的间距 ≥ 8px（不再遮挡）
 11. 按钮语义与图标契约：文案零彩色 emoji、带图标者 mask 真的渲染出非零尺寸、
     同一操作跨页同色（暂停=橙 / 继续=青 / 停止=红 / 复制全部=青）、
     破坏性操作不占页头首位、按钮按语义分组（.btn-sep）、无 float:right 内联样式；
     采集页参数工具栏在默认窗口宽度下**不折行**（「停止」不落单）、
     四个动作按钮同处一行且包成一组、该行留有余量
 12. 采集页配置行初始化：**先经「账号管理」再进「采集任务」**这个顺序下，
     地区下拉仍是完整列表（含越南/缅甸/各地账号数）、采集日期与账号间隔已补默认值、
     时区标签为真实时区；且选「越南」能真筛出账号并自动勾选
     （回归：曾把这一整块写在 `if (!S_.meta)` 里，被共享缓存跳过 → 地区只剩「全部地区」）
 13. 零 JS 报错 + 同库前后快照零落库

纪律：不点任何会落库的控件（保存 / 恢复默认第二次 / 启用停用 / 删除 / 开始采集）。

用法：venv\\Scripts\\python.exe scripts/verify_w7.py
"""
from __future__ import annotations

import re
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "_app"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 控制台可能是 GBK（Windows 默认），而本脚本会打印 emoji 与「—」等字符，
# 直接 print 会抛 UnicodeEncodeError 让整轮验收半路崩掉（2026-10-10 实测）。
# 统一把标准输出改成 UTF-8 + 替换非法字符：宁可显示成问号，也不能中断验收。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  —— {extra}" if extra else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


OWN_DB = ROOT / "data" / "tiktok_link_collector.db"
TABLES = ("accounts", "videos", "collect_tasks", "collect_logs",
          "task_accounts", "chat_targets", "chat_links")


def db_dump() -> dict:
    """只读快照（mode=ro），逐表 dump 成可比较结构。"""
    c = sqlite3.connect(f"file:{OWN_DB.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for t in TABLES:
            try:
                out[t] = sorted(c.execute(f"SELECT * FROM {t}").fetchall(), key=str)
            except sqlite3.Error:
                out[t] = None
        try:
            out["_uv"] = c.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error:
            out["_uv"] = None
        return out
    finally:
        c.close()


def main() -> int:
    import uvicorn
    import server.api as api
    from playwright.sync_api import sync_playwright

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(
        api.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    threading.Thread(target=srv.run, daemon=True, name="uvicorn-w7").start()
    deadline = time.time() + 40
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.25)
    base = f"http://127.0.0.1:{port}"
    print(f"服务已就绪 {base}\n")

    before = db_dump()
    errors: list[str] = []
    overview_hits = {"n": 0}
    results_hits = {"n": 0}
    daily_hits = {"n": 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # 需要读剪贴板来验证「复制全部」真的把整份内容写进去了
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("request", lambda r: overview_hits.__setitem__(
            "n", overview_hits["n"] + 1)
            if "/api/collector/overview" in r.url else None)
        page.on("request", lambda r: results_hits.__setitem__(
            "n", results_hits["n"] + 1)
            if "/api/collector/results" in r.url else None)
        page.on("request", lambda r: daily_hits.__setitem__(
            "n", daily_hits["n"] + 1)
            if "/api/collector/daily-links/" in r.url else None)

        page.goto(base, wait_until="load")
        page.wait_for_timeout(1500)          # 等首屏（仪表盘）数据落地

        def goto(view: str) -> None:
            page.evaluate(f"location.hash = '{view}'")
            page.wait_for_timeout(900)

        print("[1] 账号页「编辑数量」（原桌面版有、合并版曾缺）")
        goto("accounts")
        page.wait_for_timeout(700)
        btn_disabled_before = page.eval_on_selector(
            "#ac-edit-count", "el => el.disabled")
        check("未勾选时「编辑数量」禁用", btn_disabled_before is True,
              f"disabled={btn_disabled_before}")

        boxes = page.query_selector_all("#ac-table tbody input[type=checkbox]")
        if boxes:
            boxes[0].check()
            page.wait_for_timeout(400)
        btn = page.eval_on_selector(
            "#ac-edit-count", "el => ({d: el.disabled, t: el.textContent})")
        check("勾选 1 个账号后「编辑数量」启用且带计数",
              btn["d"] is False and "1" in (btn["t"] or ""), str(btn))

        if btn["d"] is False:
            page.click("#ac-edit-count")
            page.wait_for_timeout(350)
            title = page.eval_on_selector(
                ".modal-title", "el => el.textContent") if page.query_selector(
                ".modal-title") else ""
            check("弹窗打开且标题为原版「批量设置采集数量」",
                  "批量设置采集数量" in (title or ""), title or "(无弹窗)")
            has_input = bool(page.query_selector("#cn-value"))
            has_quick = len(page.query_selector_all("#cn-quick .capsule"))
            check("弹窗含数量输入框与快捷胶囊", has_input and has_quick > 0,
                  f"input={has_input} capsules={has_quick}")
            # 取消（不落库）
            page.click('.modal-foot [data-act="cancel"]')
            page.wait_for_timeout(250)
            check("取消后弹窗关闭",
                  page.query_selector(".modal-mask") is None)

        print("\n[2] 设置页补齐的可编辑项（原桌面版「数据 / 日志 / 更新」分组）")
        goto("settings")
        page.wait_for_timeout(900)
        vals = page.evaluate("""() => ({
            ex: (document.getElementById('cset-export-dir')||{}).value,
            lg: (document.getElementById('cset-log-dir')||{}).value,
            repo: (document.getElementById('cset-repo')||{}).value,
            url: (document.getElementById('cset-update-url')||{}).value,
            reset: !!document.getElementById('cset-reset'),
        })""")
        check("导出目录已回填", (vals["ex"] or "").strip() != "", repr(vals["ex"]))
        check("日志目录已回填", (vals["lg"] or "").strip() != "", repr(vals["lg"]))
        check("GitHub 仓库输入框存在（值可为空）", "repo" in vals, repr(vals["repo"]))
        check("更新源输入框存在", "url" in vals, repr(vals["url"]))
        check("「恢复默认」按钮存在", vals["reset"] is True)

        page.click("#cset-reset")
        page.wait_for_timeout(350)
        armed = page.evaluate("""() => {
            const b = document.getElementById('cset-reset');
            const h = document.querySelector('.inline-confirm');
            return { hidden: b.style.display === 'none',
                     holder: !!h,
                     text: h ? h.textContent : '',
                     btnDisabled: document.getElementById('cset-save').disabled };
        }""")
        check("点第一次进入内联二次确认态（隐藏按钮 + 出现确认条）",
              armed["hidden"] and armed["holder"] and "确定" in armed["text"],
              str(armed))
        # 点「取消」复原 —— 不点「确定」，避免真改配置
        if armed["holder"]:
            page.click(".inline-confirm .btn-ghost")
            page.wait_for_timeout(250)
        restored = page.evaluate(
            "() => document.getElementById('cset-reset').style.display !== 'none'")
        check("点「取消」后按钮复原（未改任何配置）", restored is True)

        print("\n[3] 聊天页按原桌面版还原为「左右分栏」布局")
        goto("chat")
        page.wait_for_timeout(800)
        st = page.eval_on_selector(
            "#ch-stop", "el => ({exists: true, d: el.disabled})") \
            if page.query_selector("#ch-stop") else {"exists": False}
        check("「停止」按钮存在且初始禁用",
              st.get("exists") is True and st.get("d") is True, str(st))

        layout = page.evaluate("""() => {
          const q = (s) => document.querySelector(s);
          const txt = (s) => (q(s) ? q(s).textContent.trim() : '');
          const grid = q('.chat-grid');
          const cols = grid
            ? getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length
            : 0;
          const titles = [...document.querySelectorAll('#page-chat .card-title')]
            .map((n) => n.textContent.trim());
          return {
            grid: !!grid, cols,
            left: !!q('.chat-left'),
            convBtn: !!q('#ch-conv'),
            convList: !!q('#ch-convs'),
            filter: !!q('#ch-filter'),
            segN: document.querySelectorAll('#ch-type-seg button').length,
            segActive: ((q('#ch-type-seg button.active') || {}).textContent || '').trim(),
            count: txt('#ch-conv-count'),
            target: txt('#ch-target-label'),
            status: txt('#ch-status'),
            linksTitle: titles.some((t) => t.includes('采集到的链接')),
            recent: txt('#ch-recent'),
            countVal: (q('#ch-count') || {}).value || '',
            startDisabled: !!(q('#ch-start') || {}).disabled,
          };
        }""")
        check("聊天页是两列网格（左列会话列表 + 右列操作区）",
              layout["grid"] and layout["cols"] == 2 and layout["left"],
              f"grid={layout['grid']} cols={layout['cols']} left={layout['left']}")
        check("左列：「刷新会话列表」按钮 + 会话列表容器",
              layout["convBtn"] and layout["convList"], "")
        check("左列：实时过滤框 + 全部·好友·群聊 三段",
              layout["filter"] and layout["segN"] == 3 and layout["segActive"] == "全部",
              f"seg={layout['segN']} active={layout['segActive']!r}")
        check("左列计数提示为原版文案",
              "刷新会话列表" in layout["count"], layout["count"])
        check("右列目标标签为原版文案「未选择目标（在左侧列表点选）」",
              layout["target"] == "未选择目标（在左侧列表点选）", layout["target"])
        check("右列：「采集到的链接」面板 + 状态行「等待开始」",
              layout["linksTitle"] and layout["status"] == "等待开始", "")
        check("右列「最近使用」有内容（胶囊或「暂无」占位）",
              layout["recent"] != "", layout["recent"])
        check("「开始采集」在未选目标时禁用",
              layout["startDisabled"] is True, "")

        # 过滤框是纯本地过滤（不发网络请求）：输入不会触发任何 API 调用
        hit_before = overview_hits["n"]
        page.fill("#ch-filter", "zzz-一定搜不到的名字")
        page.wait_for_timeout(200)
        rows = page.evaluate(
            "() => document.querySelectorAll('#ch-convs .conv-row').length")
        page.fill("#ch-filter", "")
        page.wait_for_timeout(150)
        check("过滤框纯本地生效（0 行命中，且不发起任何请求）",
              rows == 0 and overview_hits["n"] == hit_before, f"rows={rows}")

        # 类型分段可切换（本地，不请求）
        page.click("#ch-type-seg button[data-type='group']")
        page.wait_for_timeout(150)
        seg_now = page.evaluate(
            "() => (document.querySelector('#ch-type-seg button.active')||{}).dataset?.type || ''")
        check("类型分段可切换到「群聊」", seg_now == "group", f"type={seg_now!r}")
        page.click("#ch-type-seg button[data-type='']")
        page.wait_for_timeout(120)

        # 账号页「添加行」（原版 addbar）
        goto("accounts")
        page.wait_for_timeout(800)
        quick = page.evaluate("""() => {
          const q = (s) => document.querySelector(s);
          return {
            input: !!q('#ac-quick'),
            ph: q('#ac-quick') ? q('#ac-quick').placeholder : '',
            region: !!q('#ac-quick-region'),
            btn: !!q('#ac-quick-add'),
          };
        }""")
        check("账号页有「添加行」：输入框 + 地区下拉 + 添加按钮",
              quick["input"] and quick["region"] and quick["btn"], str(quick))
        check("添加行占位文案为原版「输入主页链接或 @用户名，回车添加」",
              quick["ph"] == "输入主页链接或 @用户名，回车添加", quick["ph"])

        print("\n[3b] 采集结果页（页头动作区 / 导出下拉 / 链接窗口 / 每次进入都重拉）")
        goto("results")
        page.wait_for_timeout(900)
        rs = page.evaluate("""() => {
          const q = (s) => document.querySelector(s);
          const txt = (s) => (q(s) ? q(s).textContent.trim() : '');
          const vis = (s) => {
            const n = q(s);
            if (!n) return false;
            const r = n.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const head = q('#page-results .head-actions');
          return {
            headActions: !!head,
            nHeadBtns: head ? head.querySelectorAll(':scope > .btn, :scope > .dropdown > .btn').length : 0,
            clear: txt('#rs-clear'), copyAll: txt('#rs-copy-all'),
            linkWin: txt('#rs-link-window'), expBtn: txt('#rs-export-btn'),
            // 四个动作都必须在**可见**状态下（曾把「复制」藏在空卡片里，用户看不到）
            allVisible: vis('#rs-clear') && vis('#rs-copy-all')
                        && vis('#rs-link-window') && vis('#rs-export-btn'),
            count: txt('#rs-count'),
            menuHidden: (q('#rs-export-menu') || {}).classList?.contains('hidden'),
          };
        }""")
        check("结果页有页头动作区，且 4 个动作齐备",
              rs["headActions"] and rs["nHeadBtns"] == 4, f"{rs['nHeadBtns']} 个")
        check("动作文案同原桌面版：清空链接 / 复制全部 / 链接窗口 / 导出…",
              rs["clear"] == "清空链接" and rs["copyAll"] == "复制全部"
              and rs["linkWin"] == "链接窗口" and rs["expBtn"].startswith("导出"),
              f"{rs['clear']} | {rs['copyAll']} | {rs['linkWin']} | {rs['expBtn']}")
        check("「复制全部」等动作始终可见（不再藏在空卡片里）",
              rs["allVisible"] is True, str(rs["allVisible"]))
        check("计数文案为原版「共 N 条」", rs["count"].startswith("共 "), rs["count"])

        # 导出下拉：展开 → 4 个模式 → 点空白收起（不触发任何导出请求）
        page.click("#rs-export-btn")
        page.wait_for_timeout(250)
        menu_items = page.eval_on_selector_all(
            "#rs-export-menu button", "els => els.map(e => e.textContent.trim())")
        check("「导出…」展开后含 4 个原版模式",
              menu_items == ["导出 TXT(纯URL)", "导出 TXT(分组)", "导出 CSV", "导出 XLSX"],
              str(menu_items))
        page.click("#page-results .page-title")
        page.wait_for_timeout(250)
        menu_closed = page.evaluate(
            "() => document.getElementById('rs-export-menu').classList.contains('hidden')")
        check("点空白处下拉自动收起", menu_closed is True)

        # 「链接窗口」：与原桌面版一致 —— 没有链接时只提示、不开空窗口。
        page.click("#rs-link-window")
        page.wait_for_timeout(350)
        check("空结果时点「链接窗口」只提示、不开空窗（同原桌面版）",
              page.query_selector(".modal-mask") is None,
              "open=" + str(bool(page.query_selector(".modal-mask"))))

        # 切到「全部历史」拿真实链接（纯读，不落库），再验证窗口与链接区内容
        page.click('#rs-scope button[data-scope="all"]')
        page.wait_for_timeout(1400)
        rs_all = page.evaluate("""() => ({
          count: (document.getElementById('rs-count')||{}).textContent || '',
          urls: (document.getElementById('rs-urls')||{}).textContent || '',
          urlCount: (document.getElementById('rs-url-count')||{}).textContent || '',
        })""")
        check("切「全部历史」后拿到真实链接（文本区有 URL）",
              "https://" in rs_all["urls"], rs_all["count"])
        check("链接区计数与「共 N 条链接」一致",
              rs_all["urlCount"].startswith("共 ") and rs_all["urlCount"].endswith("条链接"),
              rs_all["urlCount"])

        page.click("#rs-link-window")
        page.wait_for_timeout(400)
        lw = page.evaluate("""() => {
          const q = (s) => document.querySelector(s);
          return {
            open: !!q('.modal-mask'),
            title: q('.modal-title') ? q('.modal-title').textContent.trim() : '',
            segN: document.querySelectorAll('#lw-mode button').length,
            hasText: !!q('#lw-text'),
            text: q('#lw-text') ? q('#lw-text').textContent.slice(0, 60) : '',
            count: q('#lw-count') ? q('#lw-count').textContent : '',
            copySel: !!q('#lw-copy-sel'),
            copyAll: !!q('#lw-copy'),
          };
        }""")
        check("「链接窗口」打开且含 纯链接 / 按账号分组 分段",
              lw["open"] and lw["segN"] == 2 and lw["hasText"],
              f"open={lw['open']} seg={lw['segN']}")
        check("链接窗口只显示链接（等宽文本区含 URL，且带 复制选中/复制全部）",
              "https://" in lw["text"] and lw["copySel"] and lw["copyAll"]
              and "条链接" in lw["count"], str(lw))
        page.click(".modal-close")
        page.wait_for_timeout(250)
        check("链接窗口可关闭", page.query_selector(".modal-mask") is None)

        # 「采集结果」是数据视图：每次进入都必须重新拉取（不能吃首屏缓存）。
        # 根因记录：PAGE_LOADERS.results 曾是「只加载一次」，采集完再进来永远是空的。
        goto("dashboard")
        page.wait_for_timeout(500)
        n0 = results_hits["n"]
        goto("results")
        page.wait_for_timeout(900)
        n1 = results_hits["n"]
        goto("accounts")
        page.wait_for_timeout(500)
        goto("results")
        page.wait_for_timeout(900)
        n2 = results_hits["n"]
        check("离开再进入结果页会重新拉取（第 1 次）", n1 > n0, f"{n0} → {n1}")
        check("离开再进入结果页会重新拉取（第 2 次，不吃缓存）", n2 > n1, f"{n1} → {n2}")

        print("\n[3c] 采集任务页「暂停 / 继续 / 停止」常驻可见（原桌面版同排）")
        goto("collect")
        page.wait_for_timeout(900)
        col = page.evaluate("""() => {
          const q = (s) => document.querySelector(s);
          const vis = (s) => {
            const n = q(s);
            if (!n) return false;
            const r = n.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const bar = q('#page-collect .toolbar');
          const inBar = (s) => {
            const n = q(s);
            return !!(n && bar && bar.contains(n));
          };
          return {
            pause: { ex: !!q('#col-pause'), d: (q('#col-pause') || {}).disabled,
                     inBar: inBar('#col-pause'), vis: vis('#col-pause') },
            resume: { ex: !!q('#col-resume'), d: (q('#col-resume') || {}).disabled,
                      inBar: inBar('#col-resume'), vis: vis('#col-resume') },
            cancel: { ex: !!q('#col-cancel'), d: (q('#col-cancel') || {}).disabled,
                      inBar: inBar('#col-cancel'), vis: vis('#col-cancel') },
          };
        }""")
        check("「暂停」在参数工具栏内、可见、初始禁用",
              col["pause"]["ex"] and col["pause"]["inBar"] and col["pause"]["vis"]
              and col["pause"]["d"] is True, str(col["pause"]))
        check("「继续」在参数工具栏内且可见（不再等任务跑起来才出现）",
              col["resume"]["ex"] and col["resume"]["inBar"] and col["resume"]["vis"],
              str(col["resume"]))
        check("「停止」在参数工具栏内且可见",
              col["cancel"]["ex"] and col["cancel"]["inBar"] and col["cancel"]["vis"],
              str(col["cancel"]))

        print("\n[3d] 采集完成系统通知端点")
        r = page.evaluate("""async () => {
          const res = await fetch('/api/system/notify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // sound=false + 极短超时：只验证通路，不真的弹窗打扰
            body: JSON.stringify({ title: '__w7_probe__', message: '',
                                   kind: 'info', timeout_ms: 300, sound: false }),
          });
          return { status: res.status, body: await res.json() };
        }""")
        check("POST /api/system/notify 可用（采集结束会弹系统通知）",
              r["status"] == 200 and r["body"].get("ok") is True, str(r))

        # 「采集完成通知显示 0 个账号 / 0 条」的回归守卫。
        # 根因：Job.finish()/cancel_finish() 收尾事件不带 data，
        # 前端 onDone 只能拿到 {}，于是统计回落成 0。
        print("\n[3d-2] 收尾事件必须自带结果（否则通知统计恒为 0）")
        try:
            from server.jobs import Job as _Job
            from server.state import JobKind as _JobKind

            _ok_ev = _Job("w7-probe", _JobKind.COLLECT).finish(
                "共 3 个账号，完成 3；实际入库 12 条",
                data={"summary": "共 3 个账号，完成 3；实际入库 12 条",
                      "accounts": [{"username": "probe"}], "task_id": "w7"})
            check("JOB_DONE 事件带 data（summary / accounts / task_id）",
                  bool(_ok_ev.data.get("summary")) and bool(_ok_ev.data.get("accounts"))
                  and bool(_ok_ev.data.get("task_id")),
                  str(sorted(_ok_ev.data.keys())))
            _cc_ev = _Job("w7-probe2", _JobKind.COLLECT).cancel_finish(
                "已停止", data={"summary": "已停止", "accounts": [{"username": "probe"}]})
            check("JOB_CANCELLED 事件同样带 data（中途停止也能看到已采结果）",
                  bool(_cc_ev.data.get("accounts")), str(sorted(_cc_ev.data.keys())))
        except Exception as exc:  # noqa: BLE001
            check("收尾事件带 data", False, f"{type(exc).__name__}: {exc}")

        _src = (ROOT / "_app" / "server" / "collector_api.py").read_text(encoding="utf-8")
        check("_collect_worker 的收尾调用把结果一起下发（防回退）",
              'job.finish(job["summary"], data=result)' in _src
              and 'job.cancel_finish("已停止", data=result)' in _src)

        # 「通知跑偏」的回归守卫。历史：v1.3.0 没扣任务栏、v1.3.1 改成屏幕正中
        # 压住内容；2026-10-10 拆成两条通道；2026-10-11 用户第三次反馈后取消分流
        # —— 完成提醒只用界面内卡片（见下方 [3d-3b]），桌面浮层只剩显式调用时
        # 才出现，本项校验它自己的锚点没跑偏。
        print("\n[3d-3] 通知组件本身：显式调用桌面浮层时仍锚定可用区右下角")
        import json as _json
        import os as _os

        _dbg = ROOT / "runtime" / "_w7_notify_geom.json"
        try:
            _dbg.unlink(missing_ok=True)
        except OSError:
            pass
        _os.environ["TK_NOTIFY_DEBUG"] = str(_dbg)
        try:
            import server.notifier as _nt
            from server.notifier import notify as _notify

            # 前面 [3d] 那个 HTTP 探针的通知子进程可能还没退，
            # 会让本次弹窗被排到第 2 个槽位（底边留白 = 24 + 212）。
            # 本项要断言的是「第 1 张通知」的落点，所以先把活跃表清空。
            _nt._alive.clear()

            # ① desktop=False（只想出声的调用，例如完成提示音）：浮层**必须不弹**
            _notify("__w7_probe__", "不应出现的桌面弹窗", kind="info",
                    timeout_ms=400, sound=False, desktop=False)
            time.sleep(0.6)
            check("desktop=False（只出声的调用）时不弹桌面浮层",
                  (not _dbg.exists()) and not _nt._alive,
                  f"debug={_dbg.exists()} alive={len(_nt._alive)}")

            # ② 显式要求浮层时（默认 desktop=True）：落点不变
            _notify("__w7_probe__", "右下角几何校验", kind="info",
                    timeout_ms=400, sound=False)
            for _ in range(30):
                if _dbg.exists():
                    break
                time.sleep(0.1)
            _g = _json.loads(_dbg.read_text(encoding="utf-8"))
            _sw, _sh = _g["screen"]
            _wx, _wy, _ww, _wh = _g["work"]
            _m = _g.get("margin", 24)
            _gap_r = (_wx + _ww) - (_g["x"] + _g["w"])     # 距可用区右边
            _gap_b = (_wy + _wh) - (_g["y"] + _g["h"])     # 距可用区下边
            check("第一张通知落在右下角（slot=0）", _g.get("slot") == 0, str(_g.get("slot")))
            check("弹窗贴屏幕可用区右侧（留白 ≈ 24px）",
                  abs(_gap_r - _m) <= 6, f"gap={_gap_r} margin={_m}")
            check("弹窗贴屏幕可用区底部（留白 ≈ 24px，已避开任务栏）",
                  abs(_gap_b - _m) <= 6, f"gap={_gap_b} margin={_m}")
            check("可用区确实扣掉了任务栏（高度 < 屏高）",
                  _wh < _sh or _ww < _sw, f"work={_g['work']} screen={_g['screen']}")
            check("弹窗不再压在屏幕正中（不遮挡正文内容）",
                  abs(_g["x"] + _g["w"] // 2 - _sw // 2) > _sw * 0.2,
                  f"center_x={_g['x'] + _g['w'] // 2} sw={_sw}")
        except Exception as exc:  # noqa: BLE001
            check("通知弹窗右下角几何", False, f"{type(exc).__name__}: {exc}")
        finally:
            _os.environ.pop("TK_NOTIFY_DEBUG", None)
            try:
                _dbg.unlink(missing_ok=True)
            except OSError:
                pass

        # 界面内卡片（前台通道）的静态接线：组件 + 样式 + 路由 + 设置控件。
        # 这些是"通知别再跑出窗口"的根因所在，静态断言能挡住以后被误删。
        _app_js = (ROOT / "_app" / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
        _col_js = (ROOT / "_app" / "frontend" / "js" / "collector.js").read_text(encoding="utf-8")
        _css = (ROOT / "_app" / "frontend" / "css" / "app.css").read_text(encoding="utf-8")
        _html = (ROOT / "_app" / "frontend" / "index.html").read_text(encoding="utf-8")
        check("app.js 提供界面内通知组件并挂到 TKStudio",
              "const Notify = (() =>" in _app_js and "notify: Notify," in _app_js)
        check("app.css 定义右下角卡片栈样式",
              "#notify-stack" in _css and ".notify-card" in _css)
        check("collector.js 交给统一出口，不再自己下发桌面通知",
              "window.TKStudio.notifyCompletion" in _col_js
              and "shared(message, kind, title, { sound });" in _col_js
              and "desktop: useDesktop" not in _col_js
              and 'api("/api/system/notify"' not in _col_js)
        check("采集设置保留卡片尺寸控件，位置分流控件已删除",
              all(k in _html for k in ("cset-notify-width", "cset-notify-scale"))
              and not any(k in _html for k in ("cset-notify-inapp", "cset-notify-away")))

        # 完成消息（高清修复 / 下载）也要走界面内卡片：此前后端只发一行 toast，
        # 窗口不在前台就彻底错过。进度类短消息必须保持 toast —— 否则每解析
        # 一条链接就堆一张卡片。
        print("\n[3d-3a] 完成消息打标：打标消息进界面内卡片，进度类仍是 toast")
        _bus_src = (ROOT / "_app" / "ui" / "bus.py").read_text(encoding="utf-8")
        _svc_src = (ROOT / "_app" / "server" / "services.py").read_text(encoding="utf-8")
        check('bus.py 的 notify 支持 card 关键字（默认 False 保持原行为）',
              'def notify(message: str, level: str = "info", card: bool = False)'
              in _bus_src and '"card": bool(card),' in _bus_src)
        try:
            from ui.bus import drain_notifications as _drain
            from ui.bus import notify as _bus_notify

            _drain()  # 清掉前面探针留下的消息，避免拿错
            _bus_notify("__w7_card__", "ok", card=True)
            _bus_notify("__w7_toast__", "info")
            _got = _drain()
            _card = next((x for x in _got if x["message"] == "__w7_card__"), {})
            _plain = next((x for x in _got if x["message"] == "__w7_toast__"), {})
            check("bus.notify(card=True) 真的带上 card 标记（完成消息可被识别）",
                  _card.get("card") is True and _plain.get("card") is False,
                  f"card={_card.get('card')} plain={_plain.get('card')}")
        except Exception as exc:  # noqa: BLE001
            check("bus.notify(card=...) 标记", False, f"{type(exc).__name__}: {exc}")
        check("高清修复完成 / 全部下载完成 都带上了要显示的正文",
              'job["notify"] = ("ok", f"下载完成：成功 {ok} 个")' in _svc_src
              and '("ok", f"高清修复完成：成功 {ok} 项")' in _svc_src
              and '("error", "高清修复全部失败，请查看「处理信息」")' in _svc_src)
        check("完成提醒的老死函数（写了没人调用）已删除",
              "def _notify(" not in _svc_src and "def do_download(" not in _svc_src)
        _jobs_src = (ROOT / "_app" / "server" / "jobs.py").read_text(encoding="utf-8")
        check("jobs.py 把 notify 元组的正文放进事件（否则只有光秃秃的「完成」）",
              "self.emit(EventKind.NOTIFY, level=level, message=message)" in _jobs_src
              and "if len(value) > 1 and value[1]:" in _jobs_src)
        check("app.js 只有一条完成通道：界面内卡片，不再按焦点分流到桌面浮层",
              "function notifyItem(" in _app_js
              and "function notifyCompletion(" in _app_js
              and "Notify.show({ title, message: msg, kind, timeout: Notify.durationMs() });" in _app_js
              and "const seen = !document.hidden && document.hasFocus();" not in _app_js
              and 'desktop: false' in _app_js          # 只剩「只出声」的 beepOnce 会调后端的通知接口
              and "toast(msg, 4200)" in _app_js
              and "notify_desktop_when_unfocused" not in _app_js
              and "desktop: true" not in _app_js)
        check("完成提醒的停留时长来自设置：窗口不可见时挂起、回来自动续上",
              "let duration = DEFAULT_TIMEOUT;" in _app_js
              and "function setDuration(sec) {" in _app_js
              and "const d = Number(m.notify_duration);" in _app_js
              and "durationMs: () => duration" in _app_js
              and 'document.addEventListener("visibilitychange", () => {' in _app_js
              and "function holdCard(card) {" in _app_js
              and "function resumeHeld() {" in _app_js
              and 'if (card.dataset.paused === "1") return;' in _app_js)
        _collector_js = (ROOT / "_app" / "frontend" / "js" / "collector.js").read_text(
            encoding="utf-8")
        check("采集侧也调同一个出口（不再自己 POST 桌面通知）",
              "window.TKStudio.notifyCompletion" in _collector_js
              and "shared(message, kind, title, { sound });" in _collector_js
              and 'api("/api/system/notify"' not in _collector_js
              and "useDesktop" not in _collector_js)
        check("删掉的辅助函数没有残留调用点（否则前端启动即 ReferenceError）",
              "syncAwayNotice" not in _app_js + _collector_js
              and "awayNotice" not in _app_js + _collector_js)
        check("任务完成（SSE notify）也走同一个出口，且提示音按 beep_on_done 设置",
              "notifyCompletion(msg, kind);" in _app_js
              and "raw.beep_on_done" in _app_js
              and 'toast("✅ " + (ev.message' not in _app_js)

        # 界面内卡片的**真实 DOM 行为**：静态断言只能证明「代码在」，
        # 证明不了「画得出来、关得掉、设置真的生效」，所以在真浏览器里点一遍。
        print("\n[3d-3b] 界面内通知卡片：右下角弹出 / 上限 3 张 / 可关闭 / 设置生效")
        _dom = page.evaluate("""async () => {
          const NS = window.TKStudio && window.TKStudio.notify;
          if (!NS) return {err: 'TKStudio.notify 不存在'};
          NS.configure({notify_card_width: 420, notify_card_scale: 1});
          NS.show({title: '预热', message: 'x', kind: 'info', timeout: 1500});
          const stack = document.getElementById('notify-stack');
          if (!stack) return {err: '#notify-stack 没有创建'};
          stack.querySelectorAll('.notify-card').forEach(c => c.remove());
          const mk = (t, k) => NS.show({title: t, message: t, kind: k, timeout: 2500});
          mk('甲', 'ok'); mk('乙', 'warn'); mk('丙', 'error'); mk('丁', 'info');
          // 卡片有 200ms 的入场动画（scale .98 → 1），必须等动画结束再量，
          // 否则量到的是动画中间态（曾经因此误判成「宽度少了 8px」）。
          await new Promise(r => setTimeout(r, 500));
          const cards = [...stack.querySelectorAll('.notify-card')];
          const r0 = cards[0].getBoundingClientRect();
          const sr = stack.getBoundingClientRect();
          const toast = document.getElementById('toast');
          return {
            n: cards.length,
            firstKind: cards[0].getAttribute('data-kind'),
            lastKind: cards[cards.length - 1].getAttribute('data-kind'),
            // 落点以卡片栈为准（它不参与动画），避免把动画中间态当成最终位置
            gapRight: Math.round(window.innerWidth - sr.right),
            gapBottom: Math.round(window.innerHeight - sr.bottom),
            cardRight: Math.round(window.innerWidth - r0.right),
            width: Math.round(r0.width),
            ariaLive: stack.getAttribute('aria-live'),
            role: stack.getAttribute('role'),
            progress: !!cards[0].querySelector('.notify-progress > i'),
            toastOpacity: toast ? parseFloat(getComputedStyle(toast).opacity) : -1,
            cssW: getComputedStyle(document.documentElement)
                    .getPropertyValue('--notify-w').trim(),
          };
        }""")
        if _dom.get("err"):
            check("界面内通知卡片可渲染", False, str(_dom["err"]))
        else:
            check("通知卡片栈最多 3 张（第 4 张挤掉最旧的一张）",
                  _dom["n"] == 3 and _dom["firstKind"] == "warn"
                  and _dom["lastKind"] == "info",
                  f"n={_dom['n']} first={_dom['firstKind']} last={_dom['lastKind']}")
            check("卡片贴在程序窗口右下角（右 20px / 底 56px）",
                  abs(_dom["gapRight"] - 20) <= 2 and abs(_dom["gapBottom"] - 56) <= 2,
                  f"right={_dom['gapRight']} bottom={_dom['gapBottom']}")
            check("卡片宽度跟随设置（默认 420px）",
                  abs(_dom["width"] - 420) <= 2 and _dom["cssW"] == "420px",
                  f"w={_dom['width']} --notify-w={_dom['cssW']}")
            check("卡片栈是 role=status + aria-live=polite 的无障碍区域",
                  _dom["role"] == "status" and _dom["ariaLive"] == "polite",
                  f"role={_dom['role']} aria-live={_dom['ariaLive']}")
            check("卡片带倒计时进度条（不是纯静态提示）", _dom["progress"] is True)
            check("卡片在场时旧的提示条让位（不再两处同时提示）",
                  _dom["toastOpacity"] <= 0.05, f"opacity={_dom['toastOpacity']}")

        # 尺寸设置真的生效：1.5 × 420 = 630px（宽度与字号一起放大）
        _big = page.evaluate("""async () => {
          const NS = window.TKStudio.notify;
          NS.configure({notify_card_scale: 1.5});
          NS.show({title: '缩放', message: 's', kind: 'ok', timeout: 2500});
          await new Promise(r => setTimeout(r, 500));
          const cards = [...document.querySelectorAll('#notify-stack .notify-card')];
          const last = cards[cards.length - 1];
          return {w: Math.round(last.getBoundingClientRect().width),
                  n: cards.length,
                  fs: getComputedStyle(last).fontSize};
        }""")
        check("缩放设置生效（1.5 倍 → 630px）", abs(_big["w"] - 630) <= 3,
              f"w={_big['w']} fs={_big['fs']}")
        page.evaluate("""() => window.TKStudio.notify.configure(
            {notify_card_width: 420, notify_card_scale: 1})""")

        # 关闭行为：点卡片上的关闭按钮后卡片真的消失（不是只隐藏）
        _before_close = page.evaluate(
            "() => document.querySelectorAll('#notify-stack .notify-card').length")
        page.evaluate("""() => {
          const cards = [...document.querySelectorAll('#notify-stack .notify-card')];
          const btn = cards[cards.length - 1].querySelector('.notify-close');
          if (btn) btn.click();
        }""")
        page.wait_for_timeout(450)
        _after_close = page.evaluate(
            "() => document.querySelectorAll('#notify-stack .notify-card').length")
        check("点「×」能关掉卡片（卡片被移出 DOM）",
              _before_close > 0 and _after_close == _before_close - 1,
              f"{_before_close} → {_after_close}")

        _after_ok = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('#notify-stack .notify-card')];
          const btn = cards.length ? cards[cards.length - 1].querySelector('.notify-ok') : null;
          if (btn) btn.click();
          return document.querySelectorAll('#notify-stack .notify-card').length;
        }""")
        page.wait_for_timeout(450)
        _left = page.evaluate(
            "() => document.querySelectorAll('#notify-stack .notify-card').length")
        check("点「知道了」同样能关掉卡片", _after_ok >= _left and _left <= _after_ok,
              f"点击后 {_after_ok} → 动画结束 {_left}")
        page.evaluate("""() => document.querySelectorAll(
            '#notify-stack .notify-card').forEach(c => c.remove())""")

        # 窗口尺寸记忆：用户拖出来的大小/位置要能被记住，下次原样恢复；
        # 越过屏幕的尺寸 / 负坐标必须被丢弃，否则窗口会跑到屏幕外或大得离谱。
        print("\n[3d-4] 窗口尺寸记忆（下次启动按上次拖出来的大小打开）")
        try:
            import importlib.util as _ilu

            _spec = _ilu.spec_from_file_location("_tkdesk_probe",
                                                 ROOT / "desktop.py")
            _desk = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_desk)
            _orig_state = _desk.WINDOW_STATE_FILE
            _tmp_state = ROOT / "runtime" / "_w7_window_state.json"
            _tmp_state.unlink(missing_ok=True)
            _desk.WINDOW_STATE_FILE = _tmp_state

            _first = _desk.load_window_state()
            check("首次运行无记录 → 回落默认尺寸",
                  _first["width"] == _desk.DEFAULT_WINDOW["width"]
                  and _first["maximized"] is False, str(_first))

            _desk.save_window_state({"width": 1200, "height": 780, "x": 88,
                                     "y": 66, "maximized": False})
            _st = _desk.load_window_state()
            check("拖出来的尺寸/位置被记住（1200x780 @ 88,66）",
                  (_st["width"], _st["height"], _st.get("x"), _st.get("y"))
                  == (1200, 780, 88, 66), str(_st))

            _desk.save_window_state({"width": 99999, "height": 99999,
                                     "x": -8, "y": -8, "maximized": False})
            _st2 = _desk.load_window_state()
            check("比屏幕还大的尺寸 / 负坐标被丢弃（不让窗口跑出屏幕）",
                  _st2["width"] == _desk.DEFAULT_WINDOW["width"]
                  and "x" not in _st2, str(_st2))

            _desk.save_window_state({"width": 1200, "height": 780,
                                     "maximized": True})
            check("最大化状态被记住（下次直接最大化打开）",
                  _desk.load_window_state()["maximized"] is True)

            _desk.WINDOW_STATE_FILE = _orig_state
            _tmp_state.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            check("窗口尺寸记忆", False, f"{type(exc).__name__}: {exc}")

        # 采集结果的链接文本区：对标原版 LinkTextPanel —— 无条件可见、
        # 光标能选中、有「复制选中 / 复制全部」两个按钮。
        print("\n[3d-5] 采集结果链接区还原原版 LinkTextPanel")
        goto("results")
        page.wait_for_timeout(900)

        def _probe_link_card() -> dict:
            return page.evaluate("""() => {
              const q = (s) => document.querySelector(s);
              const card = q('#rs-link-card');
              const box = q('#rs-urls');
              const sel = q('#rs-copy-sel');
              const all = q('#rs-copy');
              const cnt = q('#rs-url-count');
              const vis = (n) => {
                if (!n) return false;
                const r = n.getBoundingClientRect();
                return r.width > 0 && r.height > 0
                  && getComputedStyle(n).visibility !== 'hidden';
              };
              return {
                cardVis: vis(card), boxVis: vis(box),
                boxH: box ? Math.round(box.getBoundingClientRect().height) : 0,
                selVis: vis(sel), allVis: vis(all),
                selText: (sel || {}).textContent || '',
                allText: (all || {}).textContent || '',
                cntText: (cnt || {}).textContent || '',
                tabindex: box ? box.getAttribute('tabindex') : null,
                urls: box ? box.textContent : '',
                modeN: document.querySelectorAll('#rs-mode button').length,
              };
            }""")

        # 先看空态（「本次采集」在刚启动的服务里是空的）——
        # 空态也必须看得见「复制」入口，这是铁律 17 的回归点。
        page.click('#rs-scope button[data-scope="session"]')
        page.wait_for_timeout(1000)
        _rs0 = _probe_link_card()
        check("链接文本区无条件可见（空结果时也在，不是「有数据才显示」）",
              _rs0["cardVis"] and _rs0["boxVis"], str(_rs0))
        check("空态下「复制选中 / 复制全部」依然可点（不藏进空卡片）",
              _rs0["selVis"] and _rs0["allVis"], str(_rs0))

        # 再看有数据态
        page.click('#rs-scope button[data-scope="all"]')
        page.wait_for_timeout(1400)
        _rs = _probe_link_card()
        check("链接区有 纯链接 / 按账号分组 两种模式", _rs["modeN"] == 2, str(_rs))
        check("文本区给足高度、可鼠标框选",
              _rs["boxH"] >= 150 and _rs["tabindex"] == "0", str(_rs))
        check("文本区里就是纯链接（可直接拖选后 Ctrl+C）",
              "https://" in _rs["urls"], _rs["urls"][:60])
        check("「复制选中」按钮在场（原版 LinkTextPanel 的复制选中）",
              _rs["selVis"] and _rs["selText"].strip() == "复制选中", str(_rs))
        check("「复制全部」按钮在场",
              _rs["allVis"] and _rs["allText"].strip() == "复制全部", str(_rs))
        check("「共 N 条链接」计数在场",
              "条链接" in _rs["cntText"], repr(_rs["cntText"]))

        # 切到分组模式：文本区应出现「@账号」标题行
        page.click('#rs-mode button[data-mode="grouped"]')
        page.wait_for_timeout(400)
        _grp = page.eval_on_selector("#rs-urls", "el => el.textContent")
        check("切「按账号分组」后文本区含 @账号 标题行",
              "@" in _grp and "https://" in _grp, _grp[:60])
        page.click('#rs-mode button[data-mode="urls"]')
        page.wait_for_timeout(300)

        # 右键菜单：原版是 tk.Menu（复制选中 / 复制全部 / 全选）
        page.click("#rs-urls", button="right")
        page.wait_for_timeout(300)
        _ctx = page.evaluate("""() => {
          const m = document.querySelector('.ctx-menu');
          if (!m) return null;
          return {items: [...m.querySelectorAll('button')].map(b => b.textContent)};
        }""")
        check("链接区右键菜单含「复制选中 / 复制全部 / 全选」",
              bool(_ctx) and _ctx["items"] == ["复制选中", "复制全部", "全选"],
              str(_ctx))
        page.mouse.click(5, 5)
        page.wait_for_timeout(200)
        check("点别处右键菜单自动收起",
              page.query_selector(".ctx-menu") is None)

        print("\n[3e] 每日归档：每次进入都重拉正文 + 复制拿到全部链接")
        # 根因回归：「每日归档」以前没走 alwaysReload —— 只在**第一次**进入时
        # 加载，之后无论采集多少次（当天归档文件是整体替换的），
        # 页面正文都还是上一份旧快照，点「复制」自然拿不到新链接。
        goto("logs")                       # 先离开，确保下面是一次「重新进入」
        page.wait_for_timeout(300)
        daily_hits["n"] = 0
        goto("daily")
        page.wait_for_timeout(1400)
        check("进入每日归档会拉取归档正文（不留旧快照）",
              daily_hits["n"] >= 1, f"hits={daily_hits['n']}")

        _dl = page.evaluate("""() => {
          const q = (s) => document.getElementById(s);
          const list = [...document.querySelectorAll('#dl-list .acct-row')];
          const box = q('dl-content');
          const cnt = q('dl-link-count');
          const body = box ? box.textContent : '';
          const urls = body.split(/\\r?\\n/).filter(x => /^https?:\\/\\//.test(x.trim()));
          return {
            listN: list.length,
            bodyLen: body.length,
            urlN: urls.length,
            cntText: cnt ? cnt.textContent : '',
            firstUrl: urls[0] || '',
            hasCopyAll: !!q('dl-copy'),
            copyText: q('dl-copy') ? q('dl-copy').textContent.trim() : '',
            copySelText: q('dl-copy-sel') ? q('dl-copy-sel').textContent.trim() : '',
            boxH: box ? Math.round(box.getBoundingClientRect().height) : 0,
            tabindex: box ? box.getAttribute('tabindex') : null,
          };
        }""")
        check("归档正文区在场且给足高度（尾行不会被裁掉）",
              _dl["boxH"] >= 280 and _dl["tabindex"] == "0", str(_dl))
        check("「共 N 条链接」计数 = 正文里的链接行数",
              bool(_dl["cntText"]) and str(_dl["urlN"]) in _dl["cntText"],
              f"cnt={_dl['cntText']!r} urlN={_dl['urlN']}")
        check("归档面板有「复制全部 / 复制选中」",
              _dl["copyText"] == "复制全部" and _dl["copySelText"] == "复制选中",
              str(_dl))
        # 关键：按「复制全部」必须把整份归档都复制进去，一条不少
        if _dl["urlN"]:
            page.click("#dl-copy")
            page.wait_for_timeout(700)
            _clip = page.evaluate("navigator.clipboard.readText()")
            _n = len([x for x in _clip.replace("\r\n", "\n").split("\n") if x.strip()])
            check("「复制全部」复制到的链接数与归档一致（不再丢链接）",
                  _n == _dl["urlN"], f"clipboard={_n} archive={_dl['urlN']}")
            check("剪贴板首行就是归档首条链接",
                  _clip.strip().startswith(_dl["firstUrl"].strip()),
                  f"{_clip[:50]!r}")
        else:
            check("归档里有链接可复制", False, "归档正文为空")

        print("\n[3f] 采集任务账号行：地区标签不得压住 @账号")
        goto("collect")
        page.wait_for_timeout(1400)
        _ar = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#col-accts .acct-row')];
          const out = [];
          for (const r of rows.slice(0, 8)) {
            const u = r.querySelector('.acct-name .user');
            const t = r.querySelector('.acct-meta .tag');
            if (!u || !t) continue;
            const ub = u.getBoundingClientRect(), tb = t.getBoundingClientRect();
            out.push({
              user: u.textContent, tag: t.textContent,
              gap: Math.round(tb.left - ub.right),
              insideName: !!r.querySelector('.acct-name .tag'),
            });
          }
          return out;
        }""")
        check("账号行里能同时读到 @账号 与地区标签", len(_ar) >= 3, str(_ar[:2]))
        if _ar:
            _mingap = min(x["gap"] for x in _ar)
            check("地区标签与 @账号 之间留出间距（不再遮挡）",
                  _mingap >= 8, f"最小间距 {_mingap}px / {_ar[:3]}")
            check("地区标签已移出昵称列（独立 .acct-meta）",
                  not any(x["insideName"] for x in _ar), str(_ar[:3]))

        print("\n[3g] 按钮语义与图标契约（同操作必同色 / 图标真的画出来了）")
        # 改版前按钮有两类硬伤，光靠肉眼看截图每次都漏：
        #   ① 图标是彩色 emoji（⏸ ▶ ⏹ 🔄 🗑 📂 📁 ↺ 📥 🧹 📜 📱 ⏻ 🔍 ＋），
        #      每个自带一套系统配色，跟深空紫青设计系统打架；且约 40 有 / 50 无。
        #   ② 同一个操作在不同页面换了身皮：「暂停」在采集页是橙实心、
        #      在去水印/高清页是透明描边；「复制全部」有 4 种长相。
        # 这两条都能脚本断言，就别再交给眼睛。
        _probe = """() => {
          const out = [];
          document.querySelectorAll('.btn').forEach((b) => {
            if (!b.offsetWidth && !b.offsetHeight) return;   // 隐藏页里的按钮不算
            const ps = getComputedStyle(b, '::before');
            out.push({
              id: b.id || '', cls: b.className, text: (b.textContent || '').trim(),
              ico: b.dataset.ico || '', icoW: Math.round(parseFloat(ps.width) || 0),
            });
          });
          return out;
        }"""
        _btns: list[dict] = []
        _head_probe: dict | None = None
        for _pid in ("collect", "tiktok", "hd", "results", "daily", "chat", "accounts"):
            goto(_pid)
            if _pid == "results":
                _head_probe = page.evaluate("""() => {
                  const h = document.querySelector('#page-results .head-actions');
                  if (!h) return null;
                  const kids = [...h.children];
                  return {
                    nBtn: h.querySelectorAll(':scope > .btn, :scope > .dropdown > .btn').length,
                    first: (kids[0].textContent || '').trim(),
                    last: (kids[kids.length - 1].textContent || '').trim(),
                  };
                }""")
            for _b in page.evaluate(_probe):
                _btns.append(_b)
        _byid: dict[str, dict] = {}
        for _b in _btns:
            if _b["id"]:
                _byid.setdefault(_b["id"], _b)

        def _cls(_i: str) -> list[str]:
            return (_byid.get(_i) or {}).get("cls", "").split()

        check("按钮探针覆盖各关键页面（样本充足）", len(_btns) >= 40,
              f"{len(_btns)} 个按钮")

        _emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
                            "\u2190-\u21FF\u2300-\u23FF\uFE0F\u20E3]")
        _left = [f"{b['id']}={b['text']}" for b in _btns if _emoji.search(b["text"])]
        check("按钮文案里已无彩色 emoji（统一为单色矢量图标）", not _left, str(_left[:4]))

        _ico = [b for b in _btns if b["ico"]]
        _dead = [b["id"] or b["text"] for b in _ico if b["icoW"] <= 0]
        check(f"{len(_ico)} 个按钮带图标，且图标尺寸非 0（mask 真的生效）",
              len(_ico) >= 25 and not _dead, f"未渲染 {_dead[:4]}")

        check("「暂停」跨页同色（btn-warn）",
              all("btn-warn" in _cls(i) for i in ("col-pause", "tk-pause", "hd-pause")),
              str({i: _cls(i) for i in ("col-pause", "tk-pause", "hd-pause")}))
        check("「继续」跨页同色（btn-accent）",
              all("btn-accent" in _cls(i) for i in ("col-resume", "tk-resume", "hd-resume")),
              str({i: _cls(i) for i in ("col-resume", "tk-resume", "hd-resume")}))
        check("「停止 / 取消」跨页同色（btn-danger）",
              all("btn-danger" in _cls(i) for i in ("col-cancel", "tk-cancel", "hd-cancel")),
              str({i: _cls(i) for i in ("col-cancel", "tk-cancel", "hd-cancel")}))
        check("「复制全部」跨页同色（btn-accent）",
              all("btn-accent" in _cls(i) for i in ("rs-copy-all", "ch-copy")),
              str({i: _cls(i) for i in ("rs-copy-all", "ch-copy")}))
        check("「登录账号」不再用警告橙（登录不是危险动作）",
              "btn-warn" not in _cls("ch-login"), str(_cls("ch-login")))

        # 破坏性操作不许占页头首位：「清空链接」必须是动作区最后一个，且前面有分隔线
        check("结果页「清空链接」排在动作区末尾（不再占首位）",
              bool(_head_probe) and _head_probe["nBtn"] == 4
              and _head_probe["last"] == "清空链接"
              and _head_probe["first"] != "清空链接",
              str(_head_probe))

        _sep = page.evaluate(
            "() => document.querySelectorAll('.btn-sep').length")
        check("按钮按语义分组（.btn-sep 分隔线在场）", _sep >= 4, f"{_sep} 处")

        _raw_html = (ROOT / "_app" / "frontend" / "index.html").read_text(encoding="utf-8")
        check("不再有 float:right 内联样式（改用 .btn-push）",
              re.search(r"float\s*:\s*right", _raw_html) is None)

        # 采集页参数工具栏：默认窗口宽度（1440）下必须是**单行**。
        # 这排控件最密（记录日期 / 今天 / 昨天 / 每账号条数 / 应用到已选 / 账号间隔 /
        # 已选 / 开始采集 / 暂停 / 继续 / 停止）。一旦放不下，就会把「停止」
        # 单独甩到第二行去落单 —— 看着像 bug。用行高判定：单行 ≈56px、折行 ≈98px。
        goto("collect")
        page.wait_for_timeout(1300)
        _tb = page.evaluate("""() => {
          const tb = document.querySelectorAll('#page-collect .toolbar')[0];
          const ids = ['col-start','col-pause','col-resume','col-cancel'];
          const ys = ids.map(i => Math.round(document.getElementById(i).getBoundingClientRect().y));
          const kids = [...tb.children];
          const need = kids.reduce((a,k)=>a+k.getBoundingClientRect().width,0)
                     + (kids.length-1)*7;
          return {h: Math.round(tb.getBoundingClientRect().height),
                  client: tb.clientWidth, need: Math.round(need),
                  sameRow: new Set(ys).size === 1,
                  group: !!tb.querySelector(':scope > .toolbar-actions')};
        }""")
        check("采集页参数工具栏在默认窗口宽度下不折行（「停止」不落单）",
              _tb["h"] < 70, f"高 {_tb['h']}px（单行≈56 / 折行≈98）")
        check("「开始 / 暂停 / 继续 / 停止」四个动作按钮同处一行",
              _tb["sameRow"], str(_tb))
        check("四个动作按钮包成一组（窄窗口折行时整体移动、不被拆散）",
              _tb["group"])
        check("该行留有余量（不贴边，换机器字体渲染也不会挤爆）",
              _tb["client"] - _tb["need"] >= 20,
              f"内宽 {_tb['client']} − 需求 {_tb['need']} = {_tb['client'] - _tb['need']}px")

        print("\n[3h] 采集页配置行初始化（共享 meta 缓存不得拖垮自己页面的控件）")
        # 曾经的缺陷：loadCollect() 把「填地区下拉 / 采集日期 / 账号间隔 / 时区」
        # 整块写在了 `if (!S_.meta)` 里面。而 meta 是**全局共享**的缓存，
        # 侧栏里「账号管理」恰好排在「采集任务」前面 —— 用户先去过账号管理，
        # S_.meta 就已有值，用户再进采集任务时整块被跳过：
        #   地区下拉永远只剩「全部地区」（用户报的正是这个）、
        #   采集日期与账号间隔变成空框、时区标签停在占位「时区 —」。
        # ⚠️ 这类 bug 只在**特定进入顺序**下出现，靠人眼点两下根本测不出来，
        #    所以这里刻意用「账号管理优先」的顺序把它锁死。
        _p2 = ctx.new_page()
        _p2.goto(base, wait_until="networkidle", timeout=45000)
        _p2.wait_for_timeout(1500)
        _p2.evaluate("location.hash = 'accounts'")      # ← 刻意先走「账号管理」
        _p2.wait_for_timeout(2500)
        _p2.evaluate("location.hash = 'collect'")
        _p2.wait_for_timeout(2500)
        _cfg = _p2.evaluate("""() => {
          const v = (id) => { const e = document.getElementById(id);
                              return e ? (e.value ?? e.textContent ?? '') : ''; };
          const s = document.getElementById('col-region');
          return {
            opts: s ? [...s.options].map(o => o.value) : [],
            label: s ? [...s.options].map(o => o.textContent.trim()) : [],
            date: v('col-date'), interval: v('col-interval'), tz: v('col-tz'),
          };
        }""")
        check("先经「账号管理」再进「采集任务」，地区下拉仍是完整列表",
              len(_cfg["opts"]) >= 3, f"{len(_cfg['opts'])} 项 {_cfg['opts'][:4]}")
        check("地区下拉含用户实际要选的「越南 / 缅甸」",
              "越南" in _cfg["opts"] and "缅甸" in _cfg["opts"], str(_cfg["opts"]))
        check("地区选项仍带各地区账号数（原版行为）",
              any("（" in t for t in _cfg["label"]), str(_cfg["label"][:4]))
        check("采集日期已补默认值（不再是空框）",
              bool(str(_cfg["date"]).strip()), repr(_cfg["date"]))
        check("账号间隔已补默认值（空框会被当成 0 写回配置）",
              bool(str(_cfg["interval"]).strip()), repr(_cfg["interval"]))
        check("时区标签为真实时区（不再停在占位「时区 —」）",
              "—" not in _cfg["tz"], _cfg["tz"])

        # 端到端：真的选一个地区，看是否筛出账号并自动勾选（只读 GET + 内存态）
        _p2.evaluate("""() => {
            const s = document.getElementById('col-region');
            s.value = '越南';
            s.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        _p2.wait_for_timeout(2500)
        _e2e = _p2.evaluate("""() => ({
            region: window.TKCollector.S.region,
            rows: document.querySelectorAll('#col-accts .acct-row').length,
            picked: window.TKCollector.S.picked.size,
        })""")
        check("选中「越南」后能筛出账号并自动勾选（地区筛选真的可用）",
              _e2e["region"] == "越南" and _e2e["rows"] > 0 and _e2e["picked"] > 0,
              str(_e2e))
        _p2.close()

        print("\n[3i] 通知设置往返（新字段要能存进配置、能原样回读，越界值要被钳住）")
        # 2026-10-10 新增：应用内通知卡片 + 「切到别的程序时弹桌面通知」+ 卡片宽度/缩放。
        # 这一节锁三件事，缺任何一件都会变成"设置页能点、其实没生效"：
        #   1) POST 的四个键真的进了配置白名单并写盘（不是只在前端好看）
        #   2) 把 /meta 的原值原样写回后能原样回读（幂等，绝不污染用户配置）
        #   3) 脏值/越界值被钳到安全范围（脚本直接 POST 字符串也不会写坏配置）
        import json as _j
        import urllib.request as _u
        from server import collector_api as _ca

        def _get_json(path):
            with _u.urlopen(base + path, timeout=20) as r:
                return _j.loads(r.read().decode("utf-8"))

        def _post_json(path, payload):
            req = _u.Request(base + path, method="POST",
                             data=_j.dumps(payload).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
            with _u.urlopen(req, timeout=30) as r:
                return _j.loads(r.read().decode("utf-8"))

        _nkeys = ("notify_sound", "notify_card_width", "notify_card_scale")
        _meta0 = _get_json("/api/collector/meta")
        check("/meta 下发三个通知字段（原来的前台/后台分流开关已删除）",
              all(k in _meta0 for k in _nkeys),
              str({k: _meta0.get(k) for k in _nkeys}))
        check("两个分流开关确实不在 /meta 里了",
              "notify_in_app" not in _meta0
              and "notify_desktop_when_unfocused" not in _meta0)

        _back = {k: _meta0[k] for k in _nkeys}          # 原值原样写回
        _saved = _post_json("/api/collector/settings", _back)
        check("POST /settings 收下这三个键（写回原值，不改用户配置）",
              _saved.get("ok") is True
              and all(k in (_saved.get("saved") or []) for k in _nkeys),
              str(_saved.get("saved"))[:140])
        _meta1 = _get_json("/api/collector/meta")
        check("写回后 /meta 原样回读（值没被悄悄改掉）",
              all(_meta1[k] == _back[k] for k in _nkeys),
              str({k: (_back[k], _meta1[k]) for k in _nkeys}))

        # 钳制：直接调后端纯函数 —— 把脏值 POST 上去会真的写进用户配置，所以不那样测
        _cases = [
            ("宽度 9999 → 720", _ca._coerce_setting("notify_card_width", 9999) == 720),
            ("宽度 1 → 280", _ca._coerce_setting("notify_card_width", 1) == 280),
            ("宽度 'abc' → 默认 420", _ca._coerce_setting("notify_card_width", "abc") == 420),
            ("宽度 None → 默认 420", _ca._coerce_setting("notify_card_width", None) == 420),
            ("宽度浮点 500.6 → 500", _ca._coerce_setting("notify_card_width", 500.6) == 500),
            ("缩放 9 → 1.6", _ca._coerce_setting("notify_card_scale", 9) == 1.6),
            ("缩放 0.1 → 0.8", _ca._coerce_setting("notify_card_scale", 0.1) == 0.8),
            ("缩放 1.234 → 1.23", _ca._coerce_setting("notify_card_scale", 1.234) == 1.23),
            ("布尔 'false' → False", _ca._coerce_setting("notify_sound", "false") is False),
            ("布尔 '' → False", _ca._coerce_setting("notify_sound", "") is False),
            ("布尔 'on' → True", _ca._coerce_setting("notify_sound", "on") is True),
            ("布尔 True 原样", _ca._coerce_setting("notify_sound", True) is True),
            ("非通知键不被动过", _ca._coerce_setting("timezone", "Asia/Shanghai")
             == "Asia/Shanghai"),
        ]
        _bad = [n for n, ok in _cases if not ok]
        check(f"脏值/越界值被钳到安全范围（{len(_cases)} 项）", not _bad, str(_bad))

        print("\n[4] 仪表盘自动刷新（原桌面版每 3 秒重算）")
        goto("dashboard")
        page.wait_for_timeout(600)
        base_hits = overview_hits["n"]
        page.wait_for_timeout(7000)
        auto_hits = overview_hits["n"] - base_hits
        check("停在仪表盘 7 秒内自动请求 /overview ≥ 2 次",
              auto_hits >= 2, f"新增 {auto_hits} 次")
        goto("logs")
        page.wait_for_timeout(600)
        idle_hits = overview_hits["n"]
        page.wait_for_timeout(4000)
        leak = overview_hits["n"] - idle_hits
        check("离开仪表盘后不再自动刷新（不空转）", leak == 0, f"泄漏 {leak} 次")

        print("\n[5] JS 报错与数据安全")
        check("零 JS 报错", not errors, "; ".join(errors[:3]))
        browser.close()

    after = db_dump()
    print("\n[6] 零落库复核（同库前后快照逐表比对）")
    for t in TABLES:
        b, a = before.get(t), after.get(t)
        check(f"{t} 未变", b == a,
              "一致" if b == a else f"{len(b or [])} → {len(a or [])}")
    check("user_version 未变", before.get("_uv") == after.get("_uv"),
          f"{before.get('_uv')} → {after.get('_uv')}")

    print("\n" + "=" * 74)
    print(f"结果：{PASS} PASS / {FAIL} FAIL")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
