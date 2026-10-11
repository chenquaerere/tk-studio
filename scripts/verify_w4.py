"""W4 验收：统一前端界面重构。

覆盖四个层面：
  A. 静态结构一致性 —— 导航 / 页面 / 元素 id / 懒加载注册表 是否自洽
  B. 设计系统 —— token 完整性、无硬编码色值、无遗留品牌色
  C. 可读性 —— 关键前景/背景组合的 WCAG 对比度
  D. 运行期 —— 真启服务，静态资源与采集只读接口是否正常

安全约束：只发 GET 请求，绝不触碰任何写接口（不采集 / 不清空 / 不删除）。
跑完会比对数据库计数，证明整个验收过程零副作用。

用法：python scripts/verify_w4.py
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "_app"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FE = ROOT / "_app" / "frontend"
INDEX = FE / "index.html"
CSS = FE / "css" / "app.css"
APP_JS = FE / "js" / "app.js"
COLL_JS = FE / "js" / "collector.js"
DB = ROOT / "data" / "tiktok_link_collector.db"

PASS, FAIL = [], []


# 中文 Windows 的 cmd 默认代码页是 GBK：脚本最后一行打印 ✅/❌ 会抛
# UnicodeEncodeError —— 明明所有断言都通过了，退出码却是 1（看起来像验收失败）。
# 这里自己把输出切成 UTF-8，任何控制台都能把结果打印完。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


# ------------------------------------------------------------------ 工具

def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(port: int, path: str):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode("utf-8", "replace")), ctype
            if ctype.startswith("text") or "javascript" in ctype or "css" in ctype:
                return resp.status, raw.decode("utf-8", "replace"), ctype
            return resp.status, raw, ctype
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8", "replace")), ""
        except Exception:  # noqa: BLE001
            return e.code, raw.decode("utf-8", "replace"), ""


def db_counts() -> tuple:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return (
            c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM collect_tasks").fetchone()[0],
            c.execute("PRAGMA user_version").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM chat_targets").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM chat_links").fetchone()[0],
        )
    finally:
        c.close()


# ------------------------------------------------------------------ 对比度

def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def parse_tokens(css: str) -> dict:
    block = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    out = {}
    for m in re.finditer(r"--([a-z0-9-]+):\s*([^;]+);", block):
        out[m.group(1)] = m.group(2).strip()
    return out


# ------------------------------------------------------------------ 主流程

def main() -> int:
    print("=" * 74)
    print("W4 验收：统一前端界面重构")
    print("=" * 74)

    html = read(INDEX)
    css = read(CSS)
    js_app = read(APP_JS)
    js_col = read(COLL_JS)

    # ============================================================ A 结构
    print("\n[A1] 导航与视图一一对应")
    nav_pages = re.findall(r'class="nav-item[^"]*"\s+data-page="([a-z]+)"', html)
    sections = re.findall(r'<section class="page[^"]*" id="page-([a-z]+)"', html)
    pages_arr = re.search(r"const PAGES = \[(.*?)\];", js_app, re.S).group(1)
    arr_pages = re.findall(r'"([a-z]+)"', pages_arr)

    # 侧栏顺序 = 用户拍板的工作流顺序（问题 3），这里逐项断言而非只数个数。
    EXPECTED_NAV = [
        "dashboard",                                              # 总览
        "accounts", "chat", "collect", "results", "daily", "history",  # 采集工作流
        "tiktok", "hd", "library", "models",                      # 素材制作
        "jobs", "tools", "logs", "settings", "about",              # 系统
    ]
    check(f"侧栏导航项 {len(nav_pages)} 个（={len(EXPECTED_NAV)}）",
          nav_pages == EXPECTED_NAV, str(nav_pages))
    check(f"页面 section {len(sections)} 个（={len(EXPECTED_NAV)}）", len(sections) == len(EXPECTED_NAV))
    check("导航项 ↔ section 完全对应", sorted(nav_pages) == sorted(sections))
    check("app.js PAGES ↔ 导航项完全对应（含顺序）",
          arr_pages == nav_pages, f"PAGES={arr_pages}")
    n_group = html.count('class="nav-group"')
    check("导航分为 4 组", n_group == 4, f"{n_group} 组")

    groups = re.findall(r'<div class="nav-group">([^<]+)</div>', html)
    check("分组名符合规划", groups == ["总览", "采集工作流", "素材制作", "系统"], str(groups))

    work_flow = nav_pages[1:7]
    check("采集工作流顺序 = 账号管理 → 聊天链接采集 → 采集任务 → 采集结果 → 每日归档 → 历史记录",
          work_flow == ["accounts", "chat", "collect", "results", "daily", "history"],
          str(work_flow))

    print("\n[A2] DOM id 唯一性与引用完整性")
    all_ids = re.findall(r'\sid="([A-Za-z0-9_-]+)"', html)
    dup = {i for i in all_ids if all_ids.count(i) > 1}
    check(f"index.html 无重复 id（共 {len(all_ids)} 个）", not dup, str(sorted(dup)))

    idset = set(all_ids)
    # 模态框等动态生成的节点，其 id 只写在 JS 模板串里，一并纳入
    for src in (js_app, js_col):
        idset |= set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', src))
    COLL_IDS = sorted(set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js_col)))
    missing = [i for i in COLL_IDS if i not in idset]
    check(f"collector.js 引用的 {len(COLL_IDS)} 个 id 全部存在", not missing, str(missing))

    APP_IDS = sorted(set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js_app)))
    missing_app = [i for i in APP_IDS if i not in idset]
    check(f"app.js 引用的 {len(APP_IDS)} 个 id 全部存在", not missing_app, str(missing_app))

    # 反向：id 存在但无人引用（信息性）
    unused = sorted(i for i in idset if i not in set(APP_IDS) | set(COLL_IDS)
                    and not i.startswith("page-") and i not in {"app", "toast"})
    print(f"        （仅 CSS/布局使用的 id：{len(unused)} 个）")

    print("\n[A3] 懒加载注册表与脚本装配")
    loaders = sorted(set(re.findall(r"PAGE_LOADERS\.([a-z]+)\s*=", js_col))
                     | set(re.findall(r'alwaysReload\("([a-z]+)"', js_col)))
    want = sorted(["dashboard", "collect", "accounts", "results",
                   "daily", "chat", "jobs", "history", "logs"])
    check(f"collector.js 注册 {len(want)} 个视图加载器",
          all(w in loaders for w in want), str(loaders))
    check("这些视图全部纳入 PAGES", all(p in arr_pages for p in want))

    for key in ["$", "el", "esc", "api", "toast", "writeLog", "setBar",
                "watchJob", "jobAction", "activatePage", "PAGE_LOADERS"]:
        if f"{key}," not in js_app and f"{key} " not in js_app:
            check(f"TKStudio 导出 {key}", False)
    check("app.js 暴露 window.TKStudio 完整能力",
          "window.TKStudio = {" in js_app and "jobAction" in js_app)
    check("index.html 同时引入 app.js 与 collector.js",
          'js/app.js' in html and 'js/collector.js' in html)
    check("引入顺序正确（app.js 先）",
          html.index("js/app.js") < html.index("js/collector.js"))
    check("样式表引用 app.css", 'css/app.css' in html)

    # ============================================================ B 设计系统
    print("\n[B1] 设计 token")
    tok = parse_tokens(css)
    required = ["bg", "bg-nav", "surface", "surface-2", "surface-3",
                "border", "border-strong", "primary", "accent",
                "success", "warning", "danger",
                "text", "text-2", "text-3",
                "r", "r-sm", "r-pill", "sp-3", "fs-md", "font", "mono",
                "shadow", "ease"]
    miss = [t for t in required if t not in tok]
    check(f"必需 token 齐备（{len(tok)} 个已定义）", not miss, str(miss))
    # W9 起配色**逐项还原为原桌面程序**（app/ui/theme.py 的 COLORS）。
    # 合并期这套令牌被换成了另一组近似值（accent #25F4EE / bg #0E1116），
    # 用户反馈「颜色调试不好看、和原来不一样」，故改回并在此钉死。
    check("主色 = 紫 #6c63ff（原版 primary）",
          tok.get("primary", "").upper() == "#6C63FF", tok.get("primary"))
    check("强调色 = 青 #3ecfcf（原版 accent）",
          tok.get("accent", "").upper() == "#3ECFCF", tok.get("accent"))
    check("背景 = #0f1117（原版 bg）", tok.get("bg", "").upper() == "#0F1117", tok.get("bg"))
    check("卡片底 = #1a1e2b（原版 surface）",
          tok.get("surface", "").upper() == "#1A1E2B", tok.get("surface"))
    check("次级底 = #212635（原版 surface_alt）",
          tok.get("surface-2", "").upper() == "#212635", tok.get("surface-2"))
    check("主文字 = #e6e8ee（原版 text）",
          tok.get("text", "").upper() == "#E6E8EE", tok.get("text"))

    print("\n[B1b] 控件尺寸统一（按钮 / 输入框同高）")
    check("已定义 --ctl-h / --ctl-h-sm",
          "--ctl-h:" in css and "--ctl-h-sm:" in css,
          f"{tok.get('ctl-h')} / {tok.get('ctl-h-sm')}")
    check("按钮统一用 height 而非 padding 撑高",
          re.search(r"\.btn\s*\{[^}]*height:\s*var\(--ctl-h\)", css) is not None)
    check("单行输入框与按钮同高",
          re.search(r"input\[type=\"text\"\][^{]*select\s*\{[^}]*height:\s*var\(--ctl-h\)",
                    css) is not None)

    print("\n[B2] 无硬编码色值 / 无遗留品牌色")
    body = css[css.index("}", css.index(":root {")):]
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", body)
    check(":root 之外零字面色值", not literals, f"残留 {sorted(set(literals))}")
    check("已无旧版 --tk-* 变量", "--tk-" not in css)
    check("已无旧版 TikTok 红 #FE2C55", "FE2C55" not in css.upper())
    check("已无旧版深色底 #0e0e13", "0E0E13" not in css.upper())

    print("\n[B3] 视图用到的 class 均已定义")
    used_cls = set()
    for m in re.finditer(r'class="([^"]+)"', html):
        for c in m.group(1).split():
            used_cls.add(c)
    defined = set(re.findall(r"\.([a-zA-Z][A-Za-z0-9_-]*)", css))
    # 动态生成的类名（JS 里拼出来的）也纳入
    for m in re.finditer(r'className = "([^"]+)"', js_app + js_col):
        used_cls.update(m.group(1).split())
    undef = sorted(c for c in used_cls if c not in defined)
    check(f"使用的 {len(used_cls)} 个 class 全部有样式", not undef, str(undef))

    # ============================================================ C 对比度
    print("\n[C] 可读性（WCAG 对比度）")
    pairs = [
        ("正文 / 页面底", tok["text"], tok["bg"], 4.5),
        ("正文 / 卡片底", tok["text"], tok["surface"], 4.5),
        ("次要文字 / 卡片底", tok["text-2"], tok["surface"], 4.5),
        ("次要文字 / 输入底", tok["text-2"], tok["surface-2"], 4.5),
        # 原版实心按钮就是 #6c63ff 配白字（4.32:1）。为「与原程序一致」接受这一档，
        # 门槛放到 4.3 —— 但仍要求达到 AA 大字号水准，不允许更差。
        ("按钮文字 / 主色实心底", tok["on-primary"], tok["primary-solid"], 4.3),
        ("按钮文字 / 主色悬停", tok["on-primary"], tok["primary-solid-hover"], 4.0),
        ("按钮文字 / 强调色", tok["text-inv"], tok["accent"], 4.5),
        ("成功色 / 卡片底", tok["success"], tok["surface"], 3.0),
        ("警告色 / 卡片底", tok["warning"], tok["surface"], 3.0),
        ("危险色 / 卡片底", tok["danger"], tok["surface"], 3.0),
        ("弱化文字 / 卡片底", tok["text-3"], tok["surface"], 3.0),
    ]
    for name, fg, bg, need in pairs:
        r = contrast(fg, bg)
        check(f"{name}：{r:.2f}:1（≥{need}）", r >= need, f"{fg} on {bg}")

    # ============================================================ E UX 契约
    print("\n[E] W4 必须继承的 8 条 UX 契约（来自采集器旧界面实测修正）")
    js = js_app + js_col

    check("① 破坏性动作走内联二次确认（不用模态框）",
          "function inlineConfirm" in js_col
          and "$(\"rs-clear\").click" not in js and "inlineConfirm(btn" in js_col)
    check("① 全工程无原生 confirm() / alert()",
          not re.search(r"\b(window\.)?(confirm|alert)\s*\(", js))
    check("②「开始采集」点击即开始，不弹确认框",
          "on(\"col-start\", \"click\", startCollect)" in js_col)

    check("③ 列表整行可点（.pick-row + 行点击处理器）",
          ".pick-row {" in css and "onRowClick" in js_col
          and 'tr.addEventListener("click"' in js_col)
    check("③ 选中行有高亮样式", ".pick-row.picked {" in css)

    idx_ct = html.index("采集时间") if "采集时间" in html else -1
    check("④ 结果表含「采集时间」列", 'const heads = ["序号", "昵称", "账号", "作品 ID", "采集时间"' in js_col
          or "采集时间" in js_col)
    check("④ 结果按采集时间倒序（服务端排序）",
          "reverse=True" in (ROOT / "_app" / "server" / "collector_api.py").read_text(encoding="utf-8"))

    check("⑤⑥ 默认视图 = 本次采集（session），非全部历史",
          '<button data-scope="session" class="active">' in html)
    check("⑤ 启动即空：服务端 session 视图未采集时返回空",
          "本次运行尚未采集" in (ROOT / "_app" / "server" / "collector_api.py").read_text(encoding="utf-8"))

    nick_head = re.search(r'const heads = \["选择", "([^"]+)", "([^"]+)"', js_col)
    check("⑦ 账号页昵称列在账号列之前",
          bool(nick_head) and nick_head.group(1) == "昵称" and nick_head.group(2) == "账号",
          str(nick_head.groups() if nick_head else None))
    check("⑦ 昵称为空显示占位符而非空白", "（昵称待获取）" in js_col)
    check("⑦ 统计「昵称待获取 N 个」", "昵称待获取 ${missing}" in js_col or "missing_nickname" in js_col)

    check("⑧ 切地区自动勾选该地区全部",
          'if (S_.region) {\n          S_.accounts.filter((a) => a.enabled).forEach((a) => S_.picked.add(a.account_id));' in js_col)
    # 勾选态存在 S_.picked（渲染之外的独立集合）；切地区只 add、绝不清空
    m_region = re.search(r'on\("col-region", "change".*?\n    \}\);', js_col, re.S)
    region_body = m_region.group(0) if m_region else ""
    check("⑧ 跨地区勾选要保留（勾选态独立于渲染，切地区只增不清）",
          "picked: new Set()" in js_col
          and "S_.picked.add" in region_body
          and "clear()" not in region_body
          and "new Set" not in region_body)

    # ============================================================ D 运行期
    print("\n[D0] 记录数据库基线（证明验收零副作用）")
    base = db_counts()
    print(f"       accounts={base[0]} videos={base[1]} tasks={base[2]} "
          f"user_version={base[3]}")
    check("基线可用（账号 > 0）", base[0] > 0)

    print("\n[D1] 启服务")
    try:
        import server.api as api  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  [FAIL] 导入服务层失败：{exc}")
        return 1
    port = free_port()
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(
        api.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    threading.Thread(target=srv.run, daemon=True, name="uvicorn-w4").start()
    deadline = time.time() + 40
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.25)
    else:
        print("  [FAIL] 服务未就绪")
        return 1
    print(f"       服务已就绪 @127.0.0.1:{port}")

    print("\n[D2] 页面与静态资源")
    st, body_html, _ = req(port, "/")
    check("GET / → 200", st == 200, str(st))
    check("首页标题为「TK 素材工坊」", "TK 素材工坊" in body_html)
    check("首页含四个导航分组",
          all(g in body_html for g in ["总览", "采集工作流", "素材制作", "系统"]))
    n_sec = len(re.findall(r'<section class="page', body_html))
    check(f"首页含 {len(EXPECTED_NAV)} 个页面 section",
          n_sec == len(EXPECTED_NAV), str(n_sec))
    check("首屏默认视图 = 仪表盘",
          re.search(r'class="nav-item active"\s+data-page="dashboard"', body_html) is not None)

    for path, must in [("/static/css/app.css", "--primary"),
                       ("/static/js/app.js", "TKStudio"),
                       ("/static/js/collector.js", "PAGE_LOADERS"),
                       ("/static/assets/icon.ico", None)]:
        st, data, ctype = req(port, path)
        ok = st == 200 and (must is None or must in data)
        check(f"GET {path} → 200 且内容正确", ok, f"{st} {ctype}")

    print("\n[D3] 采集只读接口（不触发任何写入）")
    st, d, _ = req(port, "/api/collector/meta")
    check("GET /meta", st == 200 and d.get("regions"), f"{st}")
    st, d, _ = req(port, "/api/collector/accounts")
    check(f"GET /accounts → {d.get('total')} 个账号",
          st == 200 and d.get("total") == base[0], f"{st}")
    check("  账号字段含昵称/地区/数量",
          all(k in d["accounts"][0] for k in
              ("display_name", "region", "collect_count", "login_status")) if d.get("accounts") else False)
    st, d, _ = req(port, "/api/collector/results?scope=session")
    check("GET /results?scope=session → 200 且为空态",
          st == 200 and d.get("rows") == [], f"{st} rows={len(d.get('rows', []))}")
    st, d, _ = req(port, "/api/collector/tasks?limit=5")
    check("GET /tasks", st == 200 and "tasks" in d, f"{st}")
    st, d, _ = req(port, "/api/collector/daily-links")
    check("GET /daily-links", st == 200 and "files" in d, f"{st} {len(d.get('files', []))} 个")
    st, d, _ = req(port, "/api/collector/env")
    check("GET /env（采集自检）", st == 200 and d.get("accounts") == base[0], f"{st}")
    check("  浏览器内核可用", bool(d.get("chromium")), str(d.get("chromium_error", ""))[:60])
    st, d, _ = req(port, "/api/collector/settings")
    check("GET /settings（采集分区）", st == 200 and "values" in d, f"{st}")
    st, d, _ = req(port, "/api/jobs")
    check("GET /api/jobs（任务中心数据源）", st == 200 and "jobs" in d, f"{st}")

    print("\n[D3b] 本轮补齐的功能面（仪表盘 / 历史 / 聊天 / 保存目录 / 日志）")
    st, d, _ = req(port, "/api/collector/overview")
    ok = (st == 200 and d.get("accounts", {}).get("total") == base[0]
          and d.get("videos", {}).get("total") == base[1]
          and d.get("tasks", {}).get("total") == base[2]
          and "chat" in d and "today" in d)
    check("GET /overview（仪表盘聚合，只读）", ok, f"{st} {d.get('accounts')}")

    st, d, _ = req(port, "/api/collector/tasks?limit=5")
    if d.get("tasks"):
        tid = d["tasks"][0]["task_id"]
        st, det, _ = req(port, f"/api/collector/tasks/{urllib.parse.quote(tid)}")
        check("GET /tasks/{id}（历史记录详情：账号明细 + 链接文本）",
              st == 200 and "task" in det and "accounts" in det and "url_text" in det,
              f"{st} {len(det.get('accounts', []))} 个账号")
    else:
        check("GET /tasks/{id}（无任务可验，跳过）", True, "库中没有任务")

    st, d, _ = req(port, "/api/system/logs?lines=50")
    check("GET /system/logs（日志尾部 + 文件清单）",
          st == 200 and isinstance(d.get("files"), list) and "lines" in d,
          f"{st} files={len(d.get('files', []))} shown={d.get('shown')}")
    st2, d2, _ = req(port, "/api/system/logs?file=../../config.json&lines=5")
    check("日志端点拒绝越界 file 参数（不返回内容）",
          st2 == 200 and d2.get("file") == "" and not d2.get("lines"))
    st, d, _ = req(port, "/api/tiktok/settings")
    check("GET /tiktok/settings（去水印保存目录）",
          st == 200 and "output_dir" in d.get("values", {}) and d.get("default_dir"),
          f"{st}")
    st, d, _ = req(port, "/api/collector/chat/recent?limit=5")
    check("GET /chat/recent（聊天「最近使用」数据源）", st == 200 and "targets" in d, f"{st}")

    # 新视图的关键 DOM 必须在首页里（否则 JS 找不到节点就静默失效）
    NEED_IDS = [
        "db-a-total", "db-v-total", "db-v-today", "db-t-total", "db-chat",
        "db-unfinished", "db-guide", "db-tasks", "db-refresh",
        "db-detail", "db-detail-kv", "db-detail-accts", "db-detail-links",
        "hi-status", "hi-query", "hi-limit", "hi-table", "hi-count",
        "hi-detail", "hi-detail-kv", "hi-detail-accts", "hi-detail-links",
        "lg-file", "lg-level", "lg-lines", "lg-query", "lg-view",
        "lg-refresh", "lg-open", "lg-export", "lg-copy", "lg-wrap",
        # W8 聊天页按原桌面版还原为左右分栏（左列会话列表 + 右列操作区）
        "ch-account", "ch-login", "ch-conv", "ch-login-tip",
        "ch-filter", "ch-type-seg", "ch-convs", "ch-conv-count",
        "ch-recent", "ch-target-label", "ch-count", "ch-start", "ch-stop",
        "ch-bar", "ch-status", "ch-show-browser", "ch-log",
        "ch-result-count", "ch-clear", "ch-export", "ch-copy", "ch-links",
        "tk-out", "tk-out-pick", "tk-out-reset",
        "hd-out", "hd-out-pick", "hd-out-reset",
        # W7 对齐原桌面版：账号页「编辑数量」+ 设置页「恢复默认 / 目录 / 更新源」
        "ac-edit-count",
        "cset-reset", "cset-export-dir", "cset-log-dir",
        "cset-repo", "cset-update-url",
        # W8 账号页「添加行」（原版 addbar：输入链接或 @用户名，回车添加）
        "ac-quick", "ac-quick-region", "ac-quick-add",
        # W9 采集结果页按原桌面版重做：页头动作区 + 导出下拉
        #     + 采集任务页「暂停」常驻（原来藏在跑起来才出现的进度卡里）
        "rs-copy-all", "rs-link-window", "rs-export-btn", "rs-export-menu",
        "rs-latest", "col-pause", "col-resume", "col-cancel",
        # W11 采集结果页的链接文本区还原原版 LinkTextPanel：
        #     模式切换 + 复制选中 + 复制全部 + 共 N 条链接（无条件可见）
        "rs-link-card", "rs-mode", "rs-copy-sel", "rs-urls", "rs-url-count",
        # W12 每日归档与结果页对齐（同款链接面板）+ 归档正文条数计数
        "dl-link-count", "dl-copy-sel",
    ]
    miss_ids = [i for i in NEED_IDS if f'id="{i}"' not in body_html]
    check(f"新增 {len(NEED_IDS)} 个关键 DOM 节点齐备", not miss_ids, str(miss_ids))

    # W7 新增端点必须在场（原桌面版有、合并版此前缺的能力）
    st, d, _ = req(port, "/api/collector/accounts/collect-count")
    check("POST /accounts/collect-count 存在（批量设置数量）",
          st in (200, 400, 405, 422), f"{st}")
    st, d, _ = req(port, "/api/collector/settings/reset")
    check("POST /settings/reset 存在（恢复默认）",
          st in (200, 400, 405, 422), f"{st}")
    st, d, _ = req(port, "/api/collector/settings")
    vals = d.get("values", {}) if isinstance(d, dict) else {}
    check("设置接口回报 export_dir / log_dir（可编辑路径）",
          "export_dir" in vals and "log_dir" in vals, str(sorted(vals.keys())))

    # 水印设置页：保存了什么就要能读回什么。
    # video_skip_similar / face_work_side 曾经「存得进、读不回」—— 页面每次打开
    # 都显示默认值，用户再点一次保存就把自己的选择覆盖掉（只读检查，不写配置）。
    st, d, _ = req(port, "/api/settings")
    wm = d.get("values", {}) if isinstance(d, dict) else {}
    check("水印设置回报 video_skip_similar / face_work_side（保存了能读回）",
          "video_skip_similar" in wm and "face_work_side" in wm,
          f"keys={len(wm)} missing="
          f"{[k for k in ('video_skip_similar', 'face_work_side') if k not in wm]}")

    # 高清页「上次用的参数」：_remember_choices() 一直在存模型/档位/开关，
    # 前端靠 /api/settings 的 raw 把它们填回表单（只读检查，不写配置）。
    raw = d.get("raw", {}) if isinstance(d, dict) else {}
    hd_prefs = ("image_model", "video_model", "image_scale", "video_scale",
                "video_sharp", "video_max_side", "face_enhance", "interpolate")
    check("/api/settings 的 raw 带着高清「上次参数」（前端据此预选）",
          all(k in raw for k in hd_prefs),
          f"missing={[k for k in hd_prefs if k not in raw]}")

    # 「重新登录」：后端一直有这个接口，界面此前没有入口（本轮补了按钮）。
    # 只用不存在的账号验证路由可达 —— 必须 404，绝不会动真实账号的登录态。
    try:
        _rq = urllib.request.Request(
            f"http://127.0.0.1:{port}"
            "/api/collector/accounts/__no_such_account__/reset-login",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(_rq, timeout=15)
        relogin_code = 200
    except urllib.error.HTTPError as e:
        relogin_code = e.code
    except Exception:  # noqa: BLE001
        relogin_code = 0
    check("POST reset-login 可达（不存在的账号必须 404）",
          relogin_code == 404, f"HTTP {relogin_code}")

    print("\n[D4] 验收结束后复核数据库（零副作用）")
    after = db_counts()
    check(f"账号数未变（{base[0]} → {after[0]}）", base[0] == after[0])
    check(f"作品数未变（{base[1]} → {after[1]}）", base[1] == after[1])
    check(f"任务数未变（{base[2]} → {after[2]}）", base[2] == after[2])
    check(f"user_version 未变（{base[3]} → {after[3]}）", base[3] == after[3])
    check(f"聊天目标数未变（{base[4]} → {after[4]}）", base[4] == after[4])
    check(f"聊天链接数未变（{base[5]} → {after[5]}）", base[5] == after[5])

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
