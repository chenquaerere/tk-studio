"""原生文件夹选择：在独立子进程里跑 tkinter askdirectory。

为什么要用子进程
----------------
1) tkinter 必须在**主线程**且独占 Tk 实例；本进程的主线程已归 pywebview，
   在 uvicorn 的工作线程里碰 tkinter 会直接崩（Windows 上表现为解释器退出）。
2) 独立进程天然隔离：弹窗关掉即退出，不污染主进程状态，
   也不与 WebView 抢消息循环。

为什么不用 pywebview 的 create_file_dialog
------------------------------------------
它只在「原生窗口模式」下可用。局域网 / 浏览器访问时前端拿不到
`window.pywebview`，功能会静默消失。子进程方案对所有形态一致可用，
且不需要把 window 对象泄漏到服务层。

为什么不用 pythonw
------------------
启动器优先用 pythonw.exe 拉起服务（无控制台），而 pythonw 下 `sys.stdout`
为 None，拿不回选中路径。这里主动换成同目录的 python.exe，
并用 CREATE_NO_WINDOW 把它自己的控制台窗口藏起来。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Tuple

log = logging.getLogger("tk.folder_picker")

# 子进程脚本：弹出「选择文件夹」对话框，把结果以 JSON 打到 stdout。
# 注意 stdout 用 UTF-8 重配 —— 中文路径在 cp936 控制台下会直接抛 UnicodeEncodeError。
_SCRIPT = r'''
import json, os, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import tkinter as tk
from tkinter import filedialog

initial = sys.argv[1] if len(sys.argv) > 1 else ""
if not (initial and os.path.isdir(initial)):
    initial = os.path.expanduser("~")

root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.update()

path = ""
try:
    path = filedialog.askdirectory(initialdir=initial, mustexist=False,
                                   title="选择素材保存目录")
except Exception:
    path = ""
finally:
    try:
        root.destroy()
    except Exception:
        pass

print(json.dumps({"path": path or ""}, ensure_ascii=False))
'''


def _no_window_flag() -> int:
    """Windows 下隐藏子进程控制台；其它平台为 0。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _python_for_dialog() -> str:
    """挑一个「能写 stdout」的解释器（pythonw 会丢输出，换回 python.exe）。"""
    exe = Path(sys.executable)
    if exe.name.lower().startswith("pythonw"):
        alt = exe.with_name("python.exe")
        if alt.exists():
            return str(alt)
    return str(exe)


def pick_folder(initial: str = "", timeout: int = 300) -> Tuple[str, str]:
    """弹出原生文件夹选择框。

    返回 `(path, error)`：
      - 用户选好      → (路径, "")
      - 用户取消/关闭 → ("", "")
      - 失败          → ("", 错误说明)

    超时（默认 300 秒）视为取消 —— 避免用户把对话框开着不关时，
    后端工作线程被永久占住。
    """
    py = _python_for_dialog()
    try:
        proc = subprocess.run(
            [py, "-c", _SCRIPT, initial or ""],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=_no_window_flag(),
        )
    except subprocess.TimeoutExpired:
        log.warning("文件夹选择超时（%ss），按取消处理", timeout)
        return "", ""
    except Exception as exc:  # noqa: BLE001
        log.warning("文件夹选择进程启动失败: %s", exc)
        return "", f"无法启动目录选择器：{exc}"

    out = (proc.stdout or "").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except Exception:
                continue
            return str(data.get("path") or ""), ""

    err = (proc.stderr or "").strip().splitlines()
    detail = err[-1] if err else "未返回结果"
    log.warning("文件夹选择未返回有效结果：%s", detail)
    return "", f"目录选择器未返回结果（{detail}）"
