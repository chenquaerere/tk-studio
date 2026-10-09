"""W0 代码迁移脚本（可重复执行 / 幂等）。

做三件事：
1. 包名改写：core.* -> watermark.core.*，tiktok.* -> watermark.tiktok.*
   （server.* 与 ui.* 保持原名）
2. 修正 __file__ 相对路径深度：包下移一层后 parent.parent 会指错目录
3. 补 __init__.py

只改导入语句与明确的路径推导行，不做任何语义改动。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "_app"

# ---- 1) 导入改写 ----
IMPORT_RULES = [
    (re.compile(r"\bfrom core(?=[.\s])"), "from watermark.core"),
    (re.compile(r"\bimport core(?=[.\s])"), "import watermark.core"),
    (re.compile(r"\bfrom tiktok(?=[.\s])"), "from watermark.tiktok"),
    (re.compile(r"\bimport tiktok(?=[.\s])"), "import watermark.tiktok"),
]

# ---- 2) 路径深度修正（精确到行，避免误伤）----
#   _app/xxx/yyy.py -> app_root = parents[2]；_app/watermark/xxx/yyy.py -> parents[3]
PATH_FIXES = [
    (APP / "server" / "api.py",
     "PROJECT_ROOT = Path(__file__).resolve().parent.parent",
     "PROJECT_ROOT = Path(__file__).resolve().parents[2]"),
    (APP / "watermark" / "core" / "config.py",
     "PROJECT_ROOT = Path(__file__).resolve().parent.parent",
     "PROJECT_ROOT = Path(__file__).resolve().parents[3]"),
    (APP / "watermark" / "core" / "utils.py",
     "PROJECT_ROOT = Path(__file__).resolve().parent.parent",
     "PROJECT_ROOT = Path(__file__).resolve().parents[3]"),
    (APP / "watermark" / "tiktok" / "config.py",
     'f = Path(__file__).resolve().parent.parent / "core" / "version.py"',
     'f = Path(__file__).resolve().parents[1] / "core" / "version.py"'),
    (APP / "watermark" / "tiktok" / "config.py",
     'output_dir: str = str(Path(__file__).parent.parent / "downloads")',
     'output_dir: str = str(Path(__file__).resolve().parents[3] / "downloads")'),
    (APP / "watermark" / "tiktok" / "logger.py",
     'log_dir = Path(__file__).parent.parent / "logs"',
     'log_dir = Path(__file__).resolve().parents[3] / "logs"'),
]

INIT_FILES = [
    APP / "__init__.py",
    APP / "watermark" / "__init__.py",
]

HEADER = '"""TK 素材工坊 · 统一应用包。"""\n'


def main() -> int:
    changed_imports = 0
    for py in sorted(APP.rglob("*.py")):
        text = orig = py.read_text(encoding="utf-8")
        for pat, rep in IMPORT_RULES:
            text = pat.sub(rep, text)
        if text != orig:
            py.write_text(text, encoding="utf-8")
            changed_imports += 1
            print(f"  [import] {py.relative_to(APP)}")
    print(f"导入改写完成：{changed_imports} 个文件")

    fixed = 0
    for path, old, new in PATH_FIXES:
        text = path.read_text(encoding="utf-8")
        if new in text:
            print(f"  [skip] {path.name} 已修正")
            continue
        if old not in text:
            print(f"  [MISS] {path.relative_to(APP)} 未找到目标行：{old}")
            continue
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        fixed += 1
        print(f"  [path] {path.relative_to(APP)}")
    print(f"路径深度修正：{fixed} 处")

    for init in INIT_FILES:
        if not init.exists():
            init.write_text(HEADER, encoding="utf-8")
            print(f"  [init] {init.relative_to(APP.parent)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
