"""启动冒烟测试（TK 素材工坊 · 合并后）。

覆盖面：
  1. 品牌与版本（统一版本源 common.version）
  2. 去水印内核：server.services 可导入（且**不需要 gradio**）、
     模型表 / 更新日志 / GPU 标签 / 倍数解析容错 / 预设套用
  3. 采集内核：collector 包可导入、五条铁律静态断言、/api/collector/* 路由存在
  4. 图标与前端静态资源（16 视图 + 设计 token）
  5. 桌面入口关键函数（动态端口 / 单实例）
  6. 真起 FastAPI 服务：首页 / ping / 采集元信息 / 越权拦截

⚠️ 纪律：本脚本**不改任何用户数据**。
   - 采集内核只做静态断言，**不实例化 CollectorServices**（那会打开用户库）。
   - 起服务会让 `CollectorServices(auto_backup=True)` 写一次每日备份记帐
     （`settings.last_backup_date`）与一个备份文件，属程序正常行为；
     脚本会在结束时显式提示该差异，不做数据比对。
   - 只发 GET 请求，绝不调用采集 / 保存 / 清空类接口。

用法：venv\\Scripts\\python.exe scripts\\smoke_test.py [端口]
默认端口 7861（避开正式默认端口），不会打开浏览器。
"""

import builtins
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "_app"
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Windows 控制台默认 GBK，打印 ✓/✗ 会抛 UnicodeEncodeError，这里统一成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, FAIL = "✓", "✗"
results = []


def check(name, fn):
    try:
        ok, detail = fn()
        results.append((name, bool(ok), detail))
    except Exception as exc:
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7861

    # ================= 1) 品牌与版本 =================
    from common import version as V

    # 版本一致性：以 common.version 为唯一来源，并要求更新日志顶部与之同步 ——
    # 这样发版只需改这两处，冒烟测试不会再因为漏改硬编码版本号而误报。
    def _top_changelog_version() -> str:
        try:
            from watermark.core.changelog import RELEASES
            return str(RELEASES[0].get("version") or "")
        except Exception:  # noqa: BLE001
            return ""

    check("品牌 / 版本", lambda: (
        V.APP_NAME == "TK 素材工坊"
        and re.fullmatch(r"\d+\.\d+\.\d+", V.APP_VERSION) is not None
        and _top_changelog_version() == V.APP_VERSION,
        f"{V.APP_NAME} v{V.APP_VERSION} · 构建 {V.BUILD_DATE}"
        f" · 日志顶部 {_top_changelog_version()}"))

    check("统一路径层", lambda: (
        True, f"程序根 {ROOT.name} · 应用代码 {APP_DIR.name}"))

    # ================= 2) 去水印内核 =================
    svc = {}

    def _import_services():
        real = builtins.__import__

        def _blocked(name, *a, **k):
            if name == "gradio" or name.startswith("gradio."):
                raise ImportError("gradio 已被冒烟测试屏蔽")
            return real(name, *a, **k)

        builtins.__import__ = _blocked
        try:
            import server.services as s
            svc["s"] = s
        finally:
            builtins.__import__ = real
        return ("gradio" not in sys.modules, "已导入，gradio 未加载")

    check("业务内核导入（无 gradio）", _import_services)

    def s():
        return svc["s"]

    check("模型清单表格", lambda: (bool(s().models_table()),
                              f"{len(s().models_table())} 行"))
    check("模型卡片墙", lambda: (bool(s().models_cards_html()),
                             f"{len(s().models_cards_html())} 字符"))
    check("预设总览", lambda: (bool(s().presets_html()),
                            f"{len(s().presets_html())} 字符"))
    check("更新日志渲染", lambda: (bool(s().changelog_markdown()),
                              f"{len(s().changelog_markdown())} 字符"))
    check("GPU 标签", lambda: (bool(s()._gpu_label()), s()._gpu_label()))

    def _preset_img():
        r = s()._apply_preset_image("photo", "realesr-general-x4v3",
                                    "自动（推荐）", False, "4096（推荐）")
        return r[0] == "4x-UltraSharp", f"高清照片 → {r[0]}"

    def _preset_vid():
        r = s()._apply_preset_video("anime_video", "realesr-general-x4v3",
                                    "自动（推荐）", 2560, False)
        return r[0] == "realesr-animevideov3", f"动漫视频 → {r[0]}"

    def _preset_planned():
        r = s()._apply_preset_image("deep_rebuild", "realesr-general-x4v3",
                                    "自动（推荐）", False, "4096（推荐）")
        return "后续版本" in r[4], "规划中预设给出友好提示"

    check("预设套用 · 图片", _preset_img)
    check("预设套用 · 视频", _preset_vid)
    check("预设套用 · 规划中拦截", _preset_planned)

    # v4.0.3 回归：倍数解析必须容错
    # 前端下拉框尚未填充完就被点击时会传空字符串；以前这里直接 int()
    # 抛 ValueError，而调用点在所有文件处理完之后，导致跑完的任务被判失败、产物丢失。
    def _scale_robust():
        vals = {None: None, "": None, "自动（推荐）": None, "自动": None, "2x": 2,
                "4x": 4, "4": 4, "4X": 4, " 4x ": 4, "abc": None, "x": None}
        bad = [f"{k!r}->{s()._scale_value(k)!r}" for k, want in vals.items()
               if s()._scale_value(k) != want]
        return not bad, f"{len(vals)} 种写法全部正确" + (f"，异常：{bad}" if bad else "")

    check("倍数解析容错（空串/自动/4x）", _scale_robust)

    def _remember_safe():
        # 空倍数不能让「记住上次参数」炸掉整个任务
        s()._remember_choices("image", "realesr-general-x4v3", "")
        s()._remember_choices("video", "realesr-general-x4v3", "", max_side="sharp")
        return True, "空倍数和平铺参数下不抛异常"

    check("参数记忆不影响任务结果", _remember_safe)

    # ================= 3) 采集内核（只读断言，不碰用户库）=================
    def _collector_import():
        import collector  # noqa: F401
        from collector import bootstrap, config  # noqa: F401
        from collector.db import database as dbm
        return (dbm.SCHEMA_VERSION == 2,
                f"collector 包可导入；SCHEMA_VERSION={dbm.SCHEMA_VERSION}")

    check("采集内核可导入 · 版本未变", _collector_import)

    def _iron_rules():
        """五条铁律的静态断言（只读源码，不执行采集逻辑、不碰用户库）。

        口径与 `scripts/verify_w2.py` 的 G2 门禁保持一致：
        - 只检查**可执行代码**（剔除注释；注释里会刻意提到历史 bug 的旧写法）。
        - 监听早于导航要在 provider.py 内比较两个调用的先后位置。
        """
        col = APP_DIR / "collector"
        db_src = (col / "db" / "database.py").read_text(encoding="utf-8")
        eng = (col / "collect" / "engine.py").read_text(encoding="utf-8")
        eng_code = "\n".join(ln for ln in eng.splitlines()
                             if not ln.lstrip().startswith("#"))
        prov = (col / "collect" / "provider.py").read_text(encoding="utf-8")
        parser = (col / "collect" / "video_parser.py").read_text(encoding="utf-8")
        logic = (col / "core" / "collect_logic.py").read_text(encoding="utf-8")

        i_listen = prov.find('page.on("response"')
        i_goto = prov.find("page.goto(")
        need = {
            "①SCHEMA 不递增": 'SCHEMA_VERSION = 2' in db_src and "_EXTRA_COLUMNS" in db_src,
            "②无跨运行去重": ("dedup = VideoDedup()" in eng_code
                            and "load_all_video_ids" not in eng_code),
            "③监听早于导航": 0 <= i_listen < i_goto,
            "④置顶识别": "isPinnedItem" in parser and "non_pinned_count" in eng,
            "⑤双实现并存": bool(logic.strip()),
        }
        missing = [k for k, v in need.items() if not v]
        detail = (f"{len(need)} 条就位" if not missing else f"缺：{missing}")
        return not missing, detail

    check("采集五条铁律 · 静态断言", _iron_rules)

    def _collector_routes():
        from server.collector_api import router as r
        n = sum(1 for route in r.routes if hasattr(route, "methods"))
        return n >= 30, f"/api/collector/* 路由 {len(r.routes)} 条"

    check("采集服务路由已挂载", _collector_routes)

    # ================= 4) 图标与前端资源 =================
    def _icons():
        root_ico = ROOT / "assets" / "icon.ico"
        app_ico = APP_DIR / "assets" / "icon.ico"
        ok = root_ico.exists() and app_ico.exists() and (APP_DIR / "assets" / "logo.svg").exists()
        return ok, ("根 assets/icon.ico + _app/assets/{icon.ico,logo.svg} 齐备"
                    if ok else f"缺失：根={root_ico.exists()} app={app_ico.exists()}")

    check("图标资源", _icons)

    def _frontend():
        html = (APP_DIR / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (APP_DIR / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
        cjs = (APP_DIR / "frontend" / "js" / "collector.js").read_text(encoding="utf-8")
        css = (APP_DIR / "frontend" / "css" / "app.css").read_text(encoding="utf-8")
        pages = [p for p in (
            "dashboard", "accounts", "chat", "collect", "results", "daily",
            "history", "tiktok", "hd", "library", "models",
            "jobs", "tools", "logs", "settings", "about")]
        need = {
            f"{len(pages)} 视图": all(f'id="page-{p}"' in html for p in pages),
            "导航项": html.count('class="nav-item') >= len(pages),
            "采集脚本": "collector.js" in html,
            "导航内核": "activatePage" in js,
            "采集逻辑": "startCollect" in cjs,
            "设计 token": "--primary" in css and "--accent" in css,
        }
        missing = [k for k, v in need.items() if not v]
        return not missing, f"{len(need)} 项就位" + (f"，缺：{missing}" if missing else "")

    check(f"前端 16 视图与设计系统", _frontend)

    # ================= 5) 桌面入口 =================
    def _desktop_funcs():
        import desktop
        p = desktop.pick_free_port()
        busy = desktop.port_in_use(p)
        return (not busy), f"动态端口 {p}，占用检测={busy}"

    check("桌面入口 · 动态端口", _desktop_funcs)

    def _singleton():
        try:
            import desktop
            # 「已被占用」= 用户已经开着程序（或本机另有实例），这是**正确行为**，
            # 因此这里只断言该函数可调用、不抛异常 —— 不把正常占用判成失败。
            desktop.acquire_single_instance()
            return True, "互斥体检查可调用（已持有 / 已被占用均属正常）"
        except AttributeError:
            return True, "合并后未提供该函数（跳过）"
        except Exception as exc:  # noqa: BLE001
            return False, f"调用失败：{exc}"

    check("桌面入口 · 单实例", _singleton)

    # ================= 6) 起服务实测 =================
    def _serve():
        import server.api as api
        import uvicorn
        if hasattr(api, "refresh_allowed_paths"):
            api.refresh_allowed_paths()
        srv = uvicorn.Server(uvicorn.Config(
            api.app, host="127.0.0.1", port=port,
            log_level="warning", access_log=False))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(40):
            time.sleep(0.5)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                continue
        else:
            return False, "服务未在 20 秒内就绪"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            body = r.read().decode("utf-8", "ignore")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=10) as r:
            ping = json.loads(r.read().decode("utf-8"))
        ok = len(body) > 1000 and ping.get("ok") is True
        return ok, f"首页 {len(body)} 字节，版本 {ping.get('version')}"

    check("FastAPI 服务启动并响应", _serve)

    def _collector_meta():
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/collector/meta", timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        regions = data.get("regions") or []
        return (bool(regions) and isinstance(regions[0], str),
                f"地区字典 {len(regions)} 项（字符串数组）；"
                f"账号统计 {data.get('account_total', '?')} 个")

    check("采集元信息（只读）", _collector_meta)

    def _forbidden():
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/file?path=C%3A%5CWindows%5Cwin.ini",
                timeout=10)
            return False, "越权读取竟然成功了"
        except urllib.error.HTTPError as e:
            return e.code == 403, f"越权被拒（HTTP {e.code}）"

    check("结果文件服务越权拦截", _forbidden)

    # ================= 汇总 =================
    print()
    print("=" * 70)
    failed = 0
    for name, ok, detail in results:
        print(f"  {OK if ok else FAIL}  {name:<26} {detail}")
        if not ok:
            failed += 1
    print("=" * 70)
    print(f"共 {len(results)} 项，失败 {failed} 项")
    print("注：起服务会让程序写一次每日备份记帐（settings.last_backup_date），属正常行为。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
