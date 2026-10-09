"""发布目录完整性校验（TK 素材工坊 · 文件夹即程序形态）。

检查「文件夹即程序」形态下，程序根是否具备可运行所需的全部要件，
并输出关键文件 md5 清单，便于归档与逐版本比对。

用法：
    python scripts/verify_release.py            # 校验 + 打印 md5 清单
    python scripts/verify_release.py --write    # 同时写 docs/RELEASE-MANIFEST.md

⚠️ 纪律：只读。数据库一律走 `mode=ro`，不改任何用户数据。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def app_version() -> str:
    """版本号唯一来源：`_app/common/version.py` 的 APP_VERSION。

    ⚠️ 不要在本脚本里硬编码版本号 —— 曾因此让发布清单标题长期停在 v1.0.0。
    """
    try:
        src = (ROOT / "_app" / "common" / "version.py").read_text(encoding="utf-8")
        m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', src, re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, FAIL = "✓", "✗"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 要件清单 ───────────────────────────────────────────────────────────
REQUIRED_FILES = [
    ("启动器", "TK素材工坊.exe"),
    ("启动器配置", "TK素材工坊.exe.config"),
    ("桌面入口", "desktop.py"),
    ("统一配置", "config.json"),
    ("更新日志", "CHANGELOG.md"),
    ("说明文档", "README.md"),
    ("exe/托盘图标", "assets/icon.ico"),
    ("应用图标", "_app/assets/icon.ico"),
    ("应用包初始化", "_app/__init__.py"),
    ("共享层·路径", "_app/common/paths.py"),
    ("共享层·版本", "_app/common/version.py"),
    ("共享层·配置", "_app/common/config.py"),
    ("服务层·主 API", "_app/server/api.py"),
    ("服务层·采集 API", "_app/server/collector_api.py"),
    ("服务层·任务", "_app/server/jobs.py"),
    ("服务层·业务内核", "_app/server/services.py"),
    ("前端·页面", "_app/frontend/index.html"),
    ("前端·设计系统", "_app/frontend/css/app.css"),
    ("前端·导航内核", "_app/frontend/js/app.js"),
    ("前端·采集逻辑", "_app/frontend/js/collector.js"),
    ("采集内核·装配", "_app/collector/bootstrap.py"),
    ("采集内核·引擎", "_app/collector/collect/engine.py"),
    ("采集内核·浏览器", "_app/collector/collect/provider.py"),
    ("采集内核·数据库", "_app/collector/db/database.py"),
    ("采集内核·纯逻辑", "_app/collector/core/collect_logic.py"),
    ("去水印内核·引擎目录", "_app/watermark/core/engines"),
    ("去水印内核·解析", "_app/watermark/tiktok"),
    ("内置 Python", "python-runtime/python.exe"),
    ("虚拟环境", "venv/Scripts/python.exe"),
    # 随身浏览器内核（零依赖采集的关键，W5-E）
    ("随身内核·Chromium", "runtime/ms-playwright/chromium-1243/chrome-win64/chrome.exe"),
    ("随身内核·转码", "runtime/ms-playwright/ffmpeg-1011/ffmpeg-win64.exe"),
]

REQUIRED_DIRS = [
    ("超分模型", "models"),
    ("转码工具", "tools"),
    ("人脸修复", "gfpgan"),
    ("用户数据", "data"),
    ("登录态", "data/sessions"),
    ("下载配置", "data/download"),
    ("日志", "logs"),
    ("备份", "backups"),
    ("文档", "docs"),
    ("开发脚本", "scripts"),
    ("随身内核", "runtime/ms-playwright"),
]

# 参与 md5 清单的关键文件（不含大体积运行时/模型/用户数据）
MANIFEST_FILES = [
    "TK素材工坊.exe",
    "desktop.py",
    "config.json",
    "README.md",
    "CHANGELOG.md",
    "assets/icon.ico",
    "_app/__init__.py",
    "_app/common/paths.py",
    "_app/common/version.py",
    "_app/common/config.py",
    "_app/common/logging_setup.py",
    "_app/common/errors.py",
    "_app/server/api.py",
    "_app/server/collector_api.py",
    "_app/server/jobs.py",
    "_app/server/services.py",
    "_app/frontend/index.html",
    "_app/frontend/css/app.css",
    "_app/frontend/js/app.js",
    "_app/frontend/js/collector.js",
    "_app/collector/bootstrap.py",
    "_app/collector/collect/engine.py",
    "_app/collector/collect/provider.py",
    "_app/collector/db/database.py",
    "_app/collector/core/collect_logic.py",
]


def main() -> int:
    write_manifest = "--write" in sys.argv

    print("=" * 74)
    print(f"发布目录完整性校验 · {ROOT}")
    print("=" * 74)

    # 1) 必需文件
    missing = []
    for label, rel in REQUIRED_FILES:
        p = ROOT / rel
        if not p.exists():
            missing.append(f"{label}({rel})")
    check(f"必需文件 {len(REQUIRED_FILES)} 项",
          not missing,
          "全部就位" if not missing else f"缺：{missing}")

    # 2) 必需目录
    missing_dir = []
    for label, rel in REQUIRED_DIRS:
        if not (ROOT / rel).is_dir():
            missing_dir.append(f"{label}({rel})")
    check(f"必需目录 {len(REQUIRED_DIRS)} 项",
          not missing_dir,
          "全部就位" if not missing_dir else f"缺：{missing_dir}")

    # 3) 图标同源
    ico_root = ROOT / "assets" / "icon.ico"
    ico_app = ROOT / "_app" / "assets" / "icon.ico"
    if ico_root.exists() and ico_app.exists():
        m1, m2 = md5(ico_root), md5(ico_app)
        check("图标同源（根 vs _app）", m1 == m2, f"md5 {m1}")
    else:
        check("图标同源（根 vs _app）", False, "图标缺失")

    # 4) 登录态数量
    sess = ROOT / "data" / "sessions"
    if sess.is_dir():
        n = sum(1 for d in sess.iterdir() if d.is_dir())
        check("登录态目录", n > 0, f"{n} 个账号目录")
    else:
        check("登录态目录", False, "缺 data/sessions")

    # 5) 数据库（只读）
    db = ROOT / "data" / "tiktok_link_collector.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            uv = cur.execute("PRAGMA user_version").fetchone()[0]
            n_acc = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            n_vid = cur.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            n_task = cur.execute("SELECT COUNT(*) FROM collect_tasks").fetchone()[0]
            con.close()
            check("数据库可读且结构版本未变", uv == 2,
                  f"user_version={uv} · 账号 {n_acc} · 作品 {n_vid} · 任务 {n_task}")
        except sqlite3.Error as e:
            check("数据库可读且结构版本未变", False, f"{type(e).__name__}: {e}")
    else:
        check("数据库可读且结构版本未变", False, "缺 data/tiktok_link_collector.db")

    # 6) 前端视图完整性
    #    ⚠️ 不再硬编码视图清单 —— 曾因写死 13 项而在扩到 16 视图后长期漏检
    #    （dashboard / history / logs 三个从未被校验）。
    #    改为从 index.html 自身推导：侧栏每个 data-page 都必须有对应的 <section id="page-*">。
    html = (ROOT / "_app" / "frontend" / "index.html")
    if html.exists():
        text = html.read_text(encoding="utf-8")
        nav = sorted(set(re.findall(r'data-page="([a-z0-9-]+)"', text)))
        sections = set(re.findall(r'id="page-([a-z0-9-]+)"', text))
        orphan_nav = [p for p in nav if p not in sections]
        no_nav = sorted(sections - set(nav))
        check(f"前端视图完整（导航 {len(nav)} 项 / 页面 {len(sections)} 个）",
              bool(nav) and not orphan_nav,
              "全部就位" if not orphan_nav
              else f"导航指向了不存在的页面：{orphan_nav}")
        check("前端视图无冗余页面（每个 section 都挂得上导航）",
              not no_nav,
              "全部就位" if not no_nav else f"无导航入口的页面：{no_nav}")
    else:
        check("前端视图完整（导航 N 项 / 页面 N 个）", False, "缺 index.html")

    # 7) 随身内核版本与 venv 里的 Playwright 对得上吗
    #
    # Playwright 靠目录名 `chromium-<revision>` 找内核。升级 playwright 包后
    # revision 会变，老的目录还在、新的找不到 —— 报错信息是
    # "Executable doesn't exist ... run playwright install"，很容易被误判成
    # 路径配置问题。这里提前把两者对齐性断言出来。
    browsers_json = (ROOT / "venv" / "Lib" / "site-packages" / "playwright"
                     / "driver" / "package" / "browsers.json")
    pw_dir = ROOT / "runtime" / "ms-playwright"
    if browsers_json.exists() and pw_dir.is_dir():
        try:
            import json
            data = json.loads(browsers_json.read_text(encoding="utf-8"))
            want = {b["name"]: b["revision"] for b in data.get("browsers", [])
                    if b.get("name") in ("chromium", "ffmpeg")}
            have = {d.name.rsplit("-", 1)[0]: d.name.rsplit("-", 1)[1]
                    for d in pw_dir.iterdir()
                    if d.is_dir() and "-" in d.name}
            bad = [f"{n}: 需 {r}，有 {have.get(n, '无')}"
                   for n, r in want.items() if have.get(n) != r]
            check("随身内核版本与 Playwright 对齐", not bad,
                  f"chromium-{have.get('chromium', '?')} · "
                  f"ffmpeg-{have.get('ffmpeg', '?')}"
                  + (f" | 失配：{bad}" if bad else ""))
        except (OSError, ValueError, KeyError) as e:
            check("随身内核版本与 Playwright 对齐", False,
                  f"{type(e).__name__}: {e}")
    else:
        check("随身内核版本与 Playwright 对齐", False,
              "缺 browsers.json 或 runtime/ms-playwright")

    # 8) 便携内核体积（提醒异常膨胀，例如误把 headless_shell 也带进来）
    if pw_dir.is_dir():
        size = sum(f.stat().st_size for f in pw_dir.rglob("*") if f.is_file())
        mb = size / 1048576
        # 完整 Chromium ≈ 433M + ffmpeg/winldd ≈ 4M；带 headless_shell 会多 271M
        check("随身内核体积合理（< 600 MB）", mb < 600,
              f"{mb:.0f} MB" + ("（疑似多带了 headless_shell）" if mb >= 600 else ""))
    else:
        check("随身内核体积合理（< 600 MB）", False, "缺 runtime/ms-playwright")

    # ── 汇总 ──
    print()
    failed = 0
    for name, ok, detail in results:
        print(f"  {OK if ok else FAIL}  {name:<26} {detail}")
        if not ok:
            failed += 1

    # ── md5 清单 ──
    print()
    print("-" * 74)
    print("关键文件 md5 清单")
    print("-" * 74)
    rows = []
    for rel in MANIFEST_FILES:
        p = ROOT / rel
        if p.exists():
            m = md5(p)
            rows.append((rel, m, p.stat().st_size))
            print(f"  {m}  {p.stat().st_size:>9}  {rel}")
        else:
            print(f"  {'-' * 32}  {'缺失':>9}  {rel}")

    if write_manifest:
        out = ROOT / "docs" / "RELEASE-MANIFEST.md"
        lines = [
            f"# 发布清单 · TK 素材工坊 v{app_version()}",
            "",
            "> 由 `scripts/verify_release.py --write` 生成。",
            "> 对比两次发布的 md5，可快速定位哪些文件发生了变化。",
            "",
            "## 关键文件",
            "",
            "| 文件 | 大小 (B) | md5 |",
            "|---|---:|---|",
        ]
        for rel, m, size in rows:
            lines.append(f"| `{rel}` | {size:,} | `{m}` |")
        lines += ["", "## 校验摘要", "",
                  f"- 必需文件 {len(REQUIRED_FILES)} 项、必需目录 {len(REQUIRED_DIRS)} 项",
                  f"- 失败项：{failed}"]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n已写出 {out}")

    print()
    print("=" * 74)
    print(f"共 {len(results)} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
