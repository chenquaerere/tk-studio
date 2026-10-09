"""重新生成程序根目录的 CHANGELOG.md。

CHANGELOG.md 是**产物**，数据源是 `_app/watermark/core/changelog.py` 的 RELEASES。
以前靠手工同步，很容易漏（v4.0.0 就漏了：代码里加了条目，
但根目录 CHANGELOG.md 还停在 v3.0.1）。

用法：
    venv\\Scripts\\python.exe scripts\\gen_changelog.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 合并后应用代码在 _app/ 下
APP_DIR = ROOT / "_app"
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.version import APP_NAME, BUILD_DATE, APP_VERSION as VERSION  # noqa: E402
from watermark.core import changelog as cl                              # noqa: E402


def main() -> int:
    parts = [
        f"# {APP_NAME} · 更新日志",
        "",
        "> 本文件由 `_app/watermark/core/changelog.py` 生成，请勿手工编辑。",
        "> 改版本时在 `RELEASES` 顶部新增一条记录，然后运行：",
        "> `venv\\Scripts\\python.exe scripts\\gen_changelog.py`",
        "",
        f"当前版本：**v{VERSION}** · 构建日期 {BUILD_DATE}",
        "",
        "---",
        "",
    ]
    for rel in cl.RELEASES:
        parts.append(cl.render_markdown(rel))
        parts.append("")
        parts.append("---")
        parts.append("")

    text = "\n".join(parts).rstrip() + "\n"
    out = ROOT / "CHANGELOG.md"
    out.write_text(text, encoding="utf-8")
    print(f"已生成 {out}")
    print(f"  版本 {VERSION} · 构建日期 {BUILD_DATE}")
    print(f"  收录 {len(cl.RELEASES)} 个版本，{len(text.splitlines())} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
