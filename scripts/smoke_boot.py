"""W0 启动冒烟：在无 GUI 环境下验证服务层可以真正跑起来。

做的事：
  1) 按 desktop.py 的方式挂载 _app 到 sys.path
  2) import server.api（会连带加载 watermark.core / watermark.tiktok 全链路）
  3) 后台线程跑 uvicorn，真实 HTTP 请求若干端点
  4) 输出每条端点的状态码与响应片段

用法：python scripts/smoke_boot.py
退出码 0 = 全通过。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "_app"
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ENDPOINTS = ["/api/ping", "/api/health", "/api/settings", "/api/jobs",
             "/api/tiktok/history", "/api/models/status", "/api/env"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    print("=" * 62)
    print("W0 启动冒烟")
    print("=" * 62)

    print("[1/4] import server.api …")
    try:
        import server.api as api
    except Exception as e:
        print(f"  [FAIL] 导入失败：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1
    print(f"  [OK] app = {api.app.title!r}")

    try:
        api.refresh_allowed_paths()
        api.set_lan_open(False)
    except Exception as e:
        print(f"  [WARN] 初始化辅助函数失败（不阻断）：{e}")

    print("[2/4] 探测业务模块可见性 …")
    probes = [
        ("watermark.core.version", "VERSION"),
        ("watermark.core.config", "PROJECT_ROOT"),
        ("server.services", "do_load_history"),
        ("server.jobs", "STORE"),
        ("ui.bus", "cancel_all"),
    ]
    for mod, attr in probes:
        try:
            m = __import__(mod, fromlist=[attr])
            val = getattr(m, attr)
            shown = str(val)[:70]
            print(f"  [OK] {mod}.{attr} = {shown}")
        except Exception as e:
            print(f"  [FAIL] {mod}.{attr}: {type(e).__name__}: {e}")
            return 1

    port = free_port()
    print(f"[3/4] 启动 uvicorn @127.0.0.1:{port} …")
    import uvicorn
    cfg = uvicorn.Config(api.app, host="127.0.0.1", port=port,
                         log_level="warning", access_log=False)
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True, name="uvicorn-smoke").start()

    deadline = time.time() + 40
    up = False
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                up = True
                break
        time.sleep(0.25)
    if not up:
        print("  [FAIL] 端口未就绪")
        return 1
    print("  [OK] 服务已就绪")

    print("[4/4] HTTP 端点冒烟 …")
    ok = True
    for ep in ENDPOINTS:
        url = f"http://127.0.0.1:{port}{ep}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                body = r.read(240).decode("utf-8", "replace").replace("\n", " ")
                mark = "OK" if r.status == 200 else "!!"
                if r.status != 200:
                    ok = False
                print(f"  [{mark}] {r.status} {ep}  {body[:110]}")
        except urllib.error.HTTPError as e:
            print(f"  [ !!] {e.code} {ep}  {e.reason}")
            ok = False
        except Exception as e:
            print(f"  [ !!] ---- {ep}  {type(e).__name__}: {e}")
            ok = False

    # 根页面（前端）单独看
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            html = r.read(400).decode("utf-8", "replace")
            has = "<html" in html.lower() or "<!doctype" in html.lower()
            print(f"  [{'OK' if has else '!!'}] {r.status} /  前端首页 {len(html)}+ 字节")
            ok = ok and has
    except Exception as e:
        print(f"  [ !!] /  前端首页 {type(e).__name__}: {e}")
        ok = False

    print("=" * 62)
    print("✅ 启动冒烟通过" if ok else "❌ 启动冒烟有失败项")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
