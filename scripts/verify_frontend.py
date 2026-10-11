# -*- coding: utf-8 -*-
"""前端行为验收：在真实 WebView2 窗口里跑真实服务，检查关键交互。

当前覆盖「高清修复 · 一键清空列表」：
  · 加文件后出现清单工具条与数量统计
  · 逐个「移除」仍然可用
  · 工具条上的「🗑 清空」与素材区的「🗑 清空列表」都能一次清空
  · 空列表再点清空只给提示、不报错
  · 任务进行中拒绝清空；任务结束后立刻可以清空

用法（在项目根目录）：
    venv\\Scripts\\python.exe scripts\\verify_frontend.py
退出码 0 = 全部通过。
"""
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 既要有程序根（common/ 的上一级布局），也要有 _app（server/ collector/ ... 包在这里）
for _p in (str(ROOT / "_app"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

# 控制台默认可能是 GBK，打印 ✓/✗ 会 UnicodeEncodeError，导致验收脚本
# 在最后打印结果时才崩掉（看起来像测试失败，其实全过了）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.getLogger("tk").handlers = [logging.NullHandler()]


def safeprint(*args, **kwargs):
    """打印不因控制台编码而失败。

    即便 stdout 是 GBK（且 reconfigure 也没生效），也绝不让编码问题
    把「验收通过」变成「脚本崩溃」。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(text.encode("utf-8", "replace") + b"\n")
            buf.flush()
        else:
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")


import uvicorn  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _make_png(path: Path, size: int = 64) -> None:
    """生成一张纯色 PNG，避免依赖测试素材文件。"""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes((200, 60, 60)) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    port = _free_port()
    from server.api import app as fastapi_app

    threading.Thread(target=uvicorn.Server(uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning"
    )).run, daemon=True).start()
    if not _wait_port(port):
        print("服务未就绪")
        return 2

    # 通过真实 HTTP 上传一张图，拿到服务器端路径
    tmp = ROOT / "runtime" / "verify_frontend.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    _make_png(tmp)
    boundary = "----tk" + uuid.uuid4().hex
    body = (
        ("--%s\r\n" % boundary).encode()
        + b'Content-Disposition: form-data; name="files"; filename="verify_frontend.png"\r\n'
        + b"Content-Type: image/png\r\n\r\n"
        + tmp.read_bytes()
        + ("\r\n--%s--\r\n" % boundary).encode()
    )
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    uploaded = json.loads(
        urllib.request.urlopen(req, timeout=60).read().decode("utf-8")).get("files") or []

    result = {}
    checks = []

    # 「倍数」的默认值不再是写死的「自动（推荐）」：HD 页现在按上次保存的
    # 档位预选（_remember_choices → /api/settings.raw）。期望值也从接口算出来，
    # 否则这条断言会退化成「只认写死默认值」的假门禁。
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/settings", timeout=15) as _r:
            _raw = (json.loads(_r.read().decode("utf-8")) or {}).get("raw") or {}
    except Exception:
        _raw = {}
    _scale = _raw.get("image_scale")
    if _scale is None:
        _scale = _raw.get("video_scale")
    try:
        _scale = int(_scale)
    except (TypeError, ValueError):
        _scale = 0
    want_scale = {2: "2x", 4: "4x"}.get(_scale, "自动（推荐）")

    def expect(name, got, want):
        ok = got == want
        checks.append((name, ok, f"{got!r}（期望 {want!r}）"))
        return ok

    def worker(window):
        try:
            time.sleep(3.5)   # 等 boot() 把下拉框/预设填好

            def js(code):
                return window.evaluate_js(code)

            expect("前端调试入口就位", js("typeof window.__TK === 'object'"), True)
            expect("倍数下拉框已填充", js("document.getElementById('hd-scale').options.length"), 3)
            expect("倍数默认值（按上次保存的档位预选）",
                   js("document.getElementById('hd-scale').value"), want_scale)

            # ---- 1. 加文件 ----
            js("""
              window.__TK.setHdFiles([
                new File(['a'], 'a.jpg', {type:'image/jpeg'}),
                new File(['b'], 'b.png', {type:'image/png'}),
                new File(['c'], 'c.mp4', {type:'video/mp4'})
              ]);
              window.__TK.setHdServerFiles(['C:/x/1.jpg', 'C:/x/2.mp4']);
            """)
            expect("清单行数", js("document.querySelectorAll('#hd-picked .file-row').length"), 5)
            expect("出现清单工具条", js("!!document.querySelector('#hd-picked .filelist-bar')"), True)
            expect("工具条在清单最上方",
                   js("document.querySelector('#hd-picked').firstElementChild.className"),
                   "filelist-bar")
            expect("工具条数量文案",
                   js("document.querySelector('#hd-picked .fl-count').textContent"),
                   "共 5 个文件（3 个待上传，2 个已在服务器）")

            # ---- 2. 逐个移除仍然可用 ----
            js("document.querySelectorAll('#hd-picked .file-row a')[0].click()")
            expect("逐个移除后行数",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 4)

            # ---- 3. 工具条「清空」 ----
            js("document.querySelector('#hd-picked .filelist-bar button').click()")
            time.sleep(0.3)
            expect("工具条清空后行数",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 0)
            expect("工具条清空后待上传数为 0", js("window.__TK.getHdFiles().length"), 0)
            expect("工具条清空后服务器文件为 0", js("window.__TK.getHdServerFiles().length"), 0)
            expect("清空后工具条消失",
                   js("!!document.querySelector('#hd-picked .filelist-bar')"), False)
            expect("清空后提示恢复默认",
                   js("document.getElementById('hd-drop-hint').textContent"),
                   "支持 jpg / png / webp / heic，mp4 / mov / mkv 等")

            # ---- 4. 素材区「清空列表」按钮 ----
            js("window.__TK.setHdFiles([new File(['z'], 'z.webp', {type:'image/webp'})])")
            expect("再次加文件后行数",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 1)
            js("document.getElementById('hd-clear').click()")
            time.sleep(0.3)
            expect("按钮清空后行数",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 0)

            # ---- 5. 空列表再点：只提示不报错 ----
            js("document.getElementById('hd-clear').click()")
            time.sleep(0.3)
            expect("空列表清空后仍为 0 行",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 0)

            # ---- 6. 任务进行中拒绝清空 ----
            js(f"window.__TK.setHdServerFiles({json.dumps(uploaded)});")
            expect("任务前清单行数",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 1)
            js("document.getElementById('hd-start').click()")
            for _ in range(100):
                if js("window.__TK.getHdJob()"):
                    break
                time.sleep(0.2)
            expect("任务已创建", bool(js("window.__TK.getHdJob()")), True)
            expect("任务进行中标记", js("window.__TK.isHdBusy()"), True)
            js("document.getElementById('hd-clear').click()")
            time.sleep(0.35)
            expect("任务进行中清空被拒绝（行数不变）",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 1)
            expect("拒绝原因已提示",
                   "任务进行中" in str(js("(document.querySelector('.toast,.tk-toast,#toast')"
                                          "||{}).textContent || ''")), True)

            # ---- 7. 等任务结束，清空应生效 ----
            for _ in range(500):
                if js("!document.getElementById('hd-start').disabled"):
                    break
                time.sleep(0.5)
            expect("任务已结束", js("window.__TK.isHdBusy()"), False)
            expect("结果画廊有产出",
                   js("document.querySelectorAll('#hd-gallery img').length") >= 1, True)
            time.sleep(0.4)
            js("document.getElementById('hd-clear').click()")
            time.sleep(0.35)
            expect("任务结束后清空生效",
                   js("document.querySelectorAll('#hd-picked .file-row').length"), 0)
            expect("任务结束后服务器文件被清空",
                   js("window.__TK.getHdServerFiles().length"), 0)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    try:
        import webview
    except ImportError:
        print("未安装 pywebview，跳过前端行为验收")
        return 0

    window = webview.create_window("前端行为验收", f"http://127.0.0.1:{port}/",
                                   width=1200, height=850)
    webview.start(worker, window, gui="edgechromium")

    try:
        tmp.unlink()
    except OSError:
        pass

    print("=" * 66)
    failed = 0
    for name, ok, detail in checks:
        safeprint(f"  {'✓' if ok else '✗'}  {name:<26} {detail}")
        if not ok:
            failed += 1
    if result.get("error"):
        safeprint("  异常:", result["error"])
        failed += 1
    print("=" * 66)
    print(f"共 {len(checks)} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
