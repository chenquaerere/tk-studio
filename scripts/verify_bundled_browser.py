"""校验「随身浏览器内核」真的生效（零依赖便携采集的前提）。

背景
----
Playwright 默认从 `%LOCALAPPDATA%\\ms-playwright` 取浏览器内核 —— 换台电脑
（或换用户账户）就找不到，采集功能直接不可用。合并后的程序把内核打进
`runtime/ms-playwright`，由 `collector/bootstrap.py::_use_bundled_browsers()`
在导入 playwright 之前设置 `PLAYWRIGHT_BROWSERS_PATH` 接管查找。

本脚本要回答三个问题：
  T1 环境变量生效后，解析出的可执行文件**确实落在程序目录内**吗？
  T2 用这个内核**真的能启动浏览器并渲染页面**吗？
  T3 **换台电脑还能跑吗**？—— 把 `LOCALAPPDATA` 指向一个空目录，
     等价于「这台机器上从来没有装过 Playwright 内核」；此时只靠程序目录
     内的内核必须仍能启动。这一项才是「零依赖便携」的真正证明。

⚠️ 曾经想过用「把 PLAYWRIGHT_BROWSERS_PATH 指向空目录 → 应当报找不到内核」
来反证，实测**假设是错的**：Playwright 在该目录为空时会自行补齐内核
（`executable_path` 照样解析成功，落在那个空目录里）。
所以改用 T3 的「模拟干净机器」方案，它更贴近真实场景。

用法：
    venv\\Scripts\\python.exe scripts\\verify_bundled_browser.py
退出码 0 表示全部通过。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "_app"          # 内置内核校验的子进程需要它才能 import 应用代码
BUNDLED = ROOT / "runtime" / "ms-playwright"
SHOT = ROOT / "docs" / "screenshots" / "15-内置浏览器内核验证.png"

OK, FAIL = "[PASS]", "[FAIL]"
_failed: list[str] = []


def check(name: str, fn):
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - 验收脚本要展示任何异常
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    print(f"  {OK if ok else FAIL} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _failed.append(name)
    return ok


def main() -> int:
    print("=" * 68)
    print("随身浏览器内核校验 · runtime/ms-playwright")
    print("=" * 68)

    # ---------- T0 目录就位 ----------
    def t0():
        if not BUNDLED.is_dir():
            return False, f"目录不存在：{BUNDLED}"
        dirs = sorted(p.name for p in BUNDLED.iterdir() if p.is_dir())
        need = ("chromium-1243",)
        miss = [d for d in need if d not in dirs]
        if miss:
            return False, f"缺关键内核目录 {miss}；现有 {dirs}"
        files = sum(1 for _ in BUNDLED.rglob("*") if _.is_file())
        return True, f"{files} 个文件；内核目录 {', '.join(dirs)}"
    check("T0 内核目录就位", t0)

    # 关键：在导入 playwright 之前设置，模拟 bootstrap 的行为
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BUNDLED)

    from playwright.sync_api import sync_playwright

    # ---------- T1 解析路径落在程序目录内 ----------
    state: dict = {}

    def t1():
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
        state["exe"] = exe
        try:
            rel = exe.relative_to(BUNDLED)
        except ValueError:
            return False, f"落到了程序目录之外：{exe}"
        if not exe.is_file():
            return False, f"可执行文件不存在：{exe}"
        return True, f"{rel}  ({exe.stat().st_size / 1048576:.1f} MB)"
    check("T1 内核可执行文件指向内置目录", t1)

    # ---------- T2 真实启动 + 渲染 ----------
    HTML = ("<meta charset=utf-8><style>"
            "body{background:#0E1116;color:#E6E9F0;font:600 30px/1.6 system-ui;"
            "display:grid;place-items:center;height:100vh;margin:0}"
            "b{color:#6C63FF}</style>"
            "<div>随身内核运行正常<br><b>bundled browser OK</b></div>")

    def t2():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--headless=new"])
            try:
                page = browser.new_page(viewport={"width": 900, "height": 520})
                # 用 set_content 而非 data: URL —— data: URL 里的 `#` 会被当作
                # fragment 分隔符，把后面的 CSS 整段吃掉（写这段时踩过）。
                page.set_content(HTML)
                page.wait_for_timeout(400)
                text = page.inner_text("div")
                ver = browser.version
                SHOT.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SHOT))
            finally:
                browser.close()
        if "bundled browser OK" not in text:
            return False, f"页面文本异常：{text!r}"
        return True, (f"渲染成功 · Chromium {ver} · 截图 "
                      f"{SHOT.relative_to(ROOT)}")
    check("T2 内置内核真实启动并渲染", t2)

    # ---------- T3 模拟「干净机器」 ----------
    def t3():
        """把 LOCALAPPDATA 指到空目录 = 这台机器从没装过 Playwright 内核。

        这是「换台电脑直接双击就能用」的等价条件：系统缓存里什么都没有，
        程序只能用自己目录里的那份内核。能起来才算真正便携。
        """
        import subprocess

        code = (
            "import os, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    print('EXE=' + p.chromium.executable_path)\n"
            "    b = p.chromium.launch(headless=False, args=['--headless=new'])\n"
            "    pg = b.new_page()\n"
            "    pg.set_content('<h1 id=t>clean machine OK</h1>')\n"
            "    print('TXT=' + pg.inner_text('#t'))\n"
            "    print('VER=' + b.version)\n"
            "    b.close()\n"
        ) % (APP_DIR,)

        with tempfile.TemporaryDirectory(prefix="clean_machine_") as fake_local:
            env = dict(os.environ)
            env["LOCALAPPDATA"] = fake_local              # 系统缓存"消失"
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(BUNDLED)
            # 顺带断掉一起可能被当作缓存的路径
            env.pop("PLAYWRIGHT_BROWSERS_PATH_OVERRIDE", None)
            r = subprocess.run([sys.executable, "-c", code], env=env,
                               capture_output=True, text=True, timeout=180,
                               cwd=str(APP_DIR))
            cache_existed = (Path(fake_local) / "ms-playwright").exists()

        out, err = r.stdout or "", r.stderr or ""
        if r.returncode != 0:
            tail = (err.strip().splitlines() or ["(无 stderr)"])[-1]
            return False, f"干净环境下启动失败：{tail}"
        if "clean machine OK" not in out:
            return False, f"启动成功但页面异常：{out.strip()[:200]}"
        exe = ""
        for line in out.splitlines():
            if line.startswith("EXE="):
                exe = line[4:]
        if not exe.startswith(str(BUNDLED)):
            return False, f"用的不是内置内核：{exe}"
        if cache_existed:
            return False, "模拟环境被污染：假 LOCALAPPDATA 下出现了 ms-playwright"
        return True, ("系统缓存为空仍启动成功 · "
                      f"内核 {Path(exe).name} · 未回写假缓存目录")
    check("T3 模拟干净机器（无系统缓存）", t3)

    print("=" * 68)
    if _failed:
        print(f"未通过 {len(_failed)} 项：{_failed}")
        return 1
    print("全部通过：随身内核可用，且不会回退到系统缓存。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
