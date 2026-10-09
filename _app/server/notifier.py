"""采集完成 / 失败的系统通知（**桌面右下角浮层** + 提示音）。

为什么要它
----------
原桌面程序在采集结束后会调 `app/ui/notify.py` 的 `notify_success()` /
`notify_failure()` —— Windows 上是 `winsound.MessageBeep`。合并成 Web 形态后
这条链路断了：采集结束只剩程序底部一行 3 秒就消失的 toast，
**用户切到别的程序时完全感知不到采集已经结束**。

为什么弹窗要独立进程
--------------------
1) tkinter 必须在主线程独占一个 Tk 实例，而本进程主线程已归 pywebview
   （与 `folder_picker.py` 同一个理由，在 uvicorn 工作线程里碰 tkinter 会直接崩）。
2) 弹窗要「弹出来、几秒后自己消失」，不能占住后端的请求线程。
3) 独立进程天然隔离：关掉即退出，不影响主进程。

为什么是屏幕**右下角**（且要避开任务栏 / 不遮挡内容）
-----------------------------------------------------
v1.3.0 贴屏幕右下角但**没有扣任务栏**，且主窗口最大化时看着像"程序里的卡片"；
v1.3.1 改成屏幕正中后又会压住屏幕中央的内容（2026-10-08 二次反馈：
"弹窗出现在右下角，而不是随意出现遮挡"）。
现在的规则：
  * 用 `SPI_GETWORKAREA` 取**可用区**（已扣任务栏，任务栏在左/右/上也照样对）；
  * 锚定可用区右下角，留 24px 边距 —— 标准桌面通知的位置；
  * 多条通知沿**竖直方向向上错位**（PITCH=212），不会互相盖住；
  * 高度按正文行数自适应（168~196px），长汇总不裁字。

提示音不放在子进程里 —— `winsound.MessageBeep` 是异步的、立即返回，
放在服务进程里执行可以做到「采集一结束就有声音」，不用等子进程启动。
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger("tk.notifier")

# 子进程脚本：**屏幕居中**的无边框置顶弹窗，带图标 / 标题 / 正文 / 「知道了」按钮，
# N 毫秒后自动消失；鼠标悬停暂停倒计时，点击任意处 / 按钮 / Esc 立即关闭。
# 参数用 base64(UTF-8 JSON) 传递 —— 中文标题经 argv 在个别环境下会被转码。
_TOAST_SCRIPT = r'''
import base64, json, os, sys
import tkinter as tk

try:
    payload = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
except Exception:
    payload = {}

title   = payload.get("title") or "TK 素材工坊"
message = payload.get("message") or ""
kind    = payload.get("kind") or "info"
timeout = int(payload.get("timeout") or 9000)
slot    = max(0, int(payload.get("slot") or 0))

ACCENT = {"info": "#3ecfcf", "ok": "#22c55e",
          "warn": "#f59e0b", "error": "#ef4444"}.get(kind, "#3ecfcf")
ICON   = {"info": "\u2139", "ok": "\u2714", "warn": "\u26a0",
          "error": "\u2716"}.get(kind, "\u2139")

BG, FG, FG2, LINE = "#1a1e2b", "#e6e8ee", "#9aa3b5", "#2c3346"

root = tk.Tk()
root.withdraw()                      # 先藏起来，等几何算好再显示，避免闪一下
root.overrideredirect(True)
root.configure(bg=BG)
try:
    root.attributes("-topmost", True)
except Exception:
    pass

W = 468
MARGIN = 24      # 距离屏幕可用区（已扣任务栏）边角的留白
PITCH = 212      # 多条通知同时到达时的纵向错位步长

sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()


def _work_area():
    """屏幕可用区（x, y, w, h）—— 已扣掉任务栏。

    任务栏可能在底部 / 顶部 / 左右，所以整块矩形都要用，不能只减屏高。
    取不到就退回「屏幕减去底部 48px 任务栏」，保证通知不会被任务栏挡住。
    """
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                      ctypes.byref(r), 0):
            _w, _h = r.right - r.left, r.bottom - r.top
            # 合理性校验：算出来的可用区必须落在屏幕内，否则视为不可信
            if 200 <= _w <= sw and 200 <= _h <= sh:
                return r.left, r.top, _w, _h
    except Exception:
        pass
    return 0, 0, sw, max(200, sh - 48)


wx, wy, ww, wh = _work_area()

# 高度自适应：采集汇总可能带「置顶额外采集 / 跳过」等补充说明，一行放不下。
# 按中文字宽粗略估算行数，避免正文被裁掉。
_text = message or "\u2014"
_cpl = max(12, (W - 80) // 13)
_lines = 0
for _seg in _text.split("\n"):
    _lines += max(1, -(-len(_seg) // _cpl))
H = max(168, min(196, 100 + _lines * 22))

# 固定在屏幕**右下角**（任务栏上方）：桌面级通知的常规位置，
# 既不会像居中弹窗那样压住屏幕正中的内容，也不会跑到程序窗口里面去。
X = max(wx, wx + ww - W - MARGIN)
Y = max(wy, wy + wh - H - MARGIN - slot * PITCH)
root.geometry("{}x{}+{}+{}".format(W, H, X, Y))

# 外框 1px 描边（tkinter 无圆角，用描边 + 色条做出卡片感）
tk.Frame(root, bg=LINE).place(x=0, y=0, width=W, height=H)
tk.Frame(root, bg=BG).place(x=1, y=1, width=W - 2, height=H - 2)
tk.Frame(root, bg=ACCENT).place(x=1, y=1, width=5, height=H - 2)

body = tk.Frame(root, bg=BG)
body.place(x=24, y=18, width=W - 46, height=H - 34)

head = tk.Frame(body, bg=BG)
head.pack(fill="x")
tk.Label(head, text=ICON, bg=BG, fg=ACCENT, font=("Segoe UI Symbol", 15, "bold")
         ).pack(side="left", padx=(0, 10))
tk.Label(head, text=title, bg=BG, fg=FG, anchor="w",
         font=("Microsoft YaHei UI", 12, "bold"), justify="left"
         ).pack(side="left", fill="x", expand=True)

tk.Label(body, text=message or "\u2014", bg=BG, fg=FG2, anchor="nw", justify="left",
         wraplength=W - 74, font=("Microsoft YaHei UI", 10)
         ).pack(fill="both", expand=True, pady=(10, 0))

foot = tk.Frame(body, bg=BG)
foot.pack(fill="x", pady=(6, 0))

closed = {"v": False}

def close(*_a):
    if closed["v"]:
        return
    closed["v"] = True
    try:
        root.destroy()
    except Exception:
        pass

btn = tk.Button(foot, text="\u77e5\u9053\u4e86", command=close, relief="flat",
                bg=ACCENT, fg="#0b1116", activebackground=ACCENT,
                activeforeground="#0b1116", bd=0, highlightthickness=0,
                padx=18, pady=4, cursor="hand2",
                font=("Microsoft YaHei UI", 9, "bold"))
btn.pack(side="right")

# ---- 自动关闭（鼠标悬停时暂停，避免用户正在看就被收走）----
state = {"left": timeout, "id": None, "paused": False}

def tick():
    if closed["v"]:
        return
    if not state["paused"]:
        state["left"] -= 100
        if state["left"] <= 0:
            close()
            return
    state["id"] = root.after(100, tick)

def hold(_e=None):
    state["paused"] = True

def release(_e=None):
    state["paused"] = False

root.bind("<Button-1>", close)
root.bind("<Escape>", close)
for w in (root, body, head, foot, btn):
    w.bind("<Enter>", hold, add="+")
    w.bind("<Leave>", release, add="+")

# ---- 淡入 ----
try:
    root.attributes("-alpha", 0.2)
except Exception:
    pass

def fade(i=0):
    if closed["v"]:
        return
    try:
        root.attributes("-alpha", min(0.98, 0.2 + i * 0.16))
    except Exception:
        return
    if i < 5:
        root.after(26, lambda: fade(i + 1))

root.deiconify()
try:
    root.lift()
except Exception:
    pass
root.after(10, fade)
root.after(100, tick)

# ---- 诊断钩子：设了 TK_NOTIFY_DEBUG 就把最终几何写出去（验收脚本用）----
_dbg = os.environ.get("TK_NOTIFY_DEBUG")
if _dbg:
    try:
        with open(_dbg, "w", encoding="utf-8") as f:
            json.dump({"x": X, "y": Y, "w": W, "h": H,
                       "screen": [sw, sh], "work": [wx, wy, ww, wh],
                       "margin": MARGIN, "slot": slot,
                       "kind": kind, "title": title}, f)
    except Exception:
        pass

root.mainloop()
'''


def _no_window_flag() -> int:
    """Windows 下隐藏子进程控制台；其它平台为 0。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _python_exe() -> str:
    """挑一个能正常启动 tkinter 的解释器（pythonw 下 tkinter 同样可用，
    但这里统一换成 python.exe + CREATE_NO_WINDOW，避免个别环境 pythonw
    缺少 tk 关联）。"""
    exe = Path(sys.executable)
    if exe.name.lower().startswith("pythonw"):
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def _beep(kind: str = "info") -> None:
    """系统提示音（与原桌面版 app/ui/notify.py 完全一致的语义）。"""
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(0x40 if kind != "error" else 0x10)  # info / error
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"
                 if kind != "error" else "/System/Library/Sounds/Sosumi.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 — 提示音失败绝不能影响主流程
        pass


# 最近弹出的弹窗进程：用于给多张通知错位（避免叠在同一处，看起来只有一条）。
_alive: List[subprocess.Popen] = []
_alive_lock = threading.Lock()


def _next_slot() -> int:
    with _alive_lock:
        _alive[:] = [p for p in _alive if p.poll() is None]
        return min(len(_alive), 3)


def notify(title: str, message: str = "", kind: str = "info",
           timeout_ms: int = 9000, sound: bool = True) -> Tuple[bool, str]:
    """弹系统通知（桌面居中弹窗）。返回 `(ok, error)`，**不阻塞**。

    kind: info | ok | warn | error（决定弹窗色条/图标与提示音类型）
    """
    if sound:
        _beep("error" if kind in ("error", "warn") else "info")

    if sys.platform != "win32":
        # 非 Windows 只做提示音（原版也是这么降级的）
        return True, ""

    try:
        payload = base64.b64encode(json.dumps(
            {"title": title, "message": message, "kind": kind,
             "timeout": int(timeout_ms), "slot": _next_slot()},
            ensure_ascii=False).encode("utf-8")).decode("ascii")
        proc = subprocess.Popen(
            [_python_exe(), "-c", _TOAST_SCRIPT, payload],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_no_window_flag(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("系统通知弹出失败：%s", exc)
        return False, f"通知弹出失败：{exc}"
    with _alive_lock:
        _alive.append(proc)
    return True, ""
