"""W2 采集内核移植脚本（可审计 / 幂等）。

把采集器的**无 UI 核心层**从 `D:\\TikTokLinkCollector\\app` 搬到
`D:\\Tiktok-去水印\\_app\\collector`，并改写包名前缀。

移植边界（铁律）：只搬"UI 无关"的层，绝不搬 tkinter 代码。

    源                              目标
    app/core/          →  collector/core/         数据模型 / 纯采集逻辑 / 去重 / 时区
    app/db/            →  collector/db/           SQLite DAO（21 账号 / 84 作品所在）
    app/services/      →  collector/services/     账号 / 结果 / 聊天 服务
    app/collector/     →  collector/collect/      Playwright 浏览器 / 会话 / 解析 / 引擎
    app/export/        →  collector/export/       CSV/TXT/XLSX 导出
    app/log/           →  collector/log/          日志 + 每日链接归档
    app/chat/          →  collector/chat/         好友/群聊链接采集
    app/constants.py   →  collector/constants.py
    app/update.py      →  collector/update.py
    app/config.py      →  （不搬）改为 collector/config.py：统一配置的 collector 分区适配层

    app/ui/**          →  不搬（W4 全部重写为网页视图）
    app/app.py         →  不搬（改为 collector/bootstrap.py：无 UI 的装配层）

包名改写规则（按长度优先，避免 app.collector 被 app.core 规则误伤）：
    app.collector → collector.collect
    app.<other>   → collector.<other>

相对导入（from .models import ...）天然跟随目录搬迁，无需改写。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

SRC = Path(r"D:\TikTokLinkCollector")
DST = Path(r"D:\Tiktok-去水印\_app\collector")

# (源相对路径, 目标相对路径)
COPY_DIRS = [
    ("app/core", "core"),
    ("app/db", "db"),
    ("app/services", "services"),
    ("app/collector", "collect"),   # 改名：避免 collector.collector 这种叠字
    ("app/export", "export"),
    ("app/log", "log"),
    ("app/chat", "chat"),
]
COPY_FILES = [
    ("app/constants.py", "constants.py"),
    ("app/update.py", "update.py"),
    ("app/__init__.py", "__init__.py"),
]

# 纯逻辑测试（不依赖 tkinter）；UI 相关测试不迁移（W4 前端重写后再补网页版断言）
TEST_FILES = [
    "test_account_regions.py", "test_account_service.py", "test_chat.py",
    "test_collect_logic.py", "test_daily_links.py", "test_database.py",
    "test_date_resolver.py", "test_engine.py", "test_export.py",
    "test_publish_time_resolver.py", "test_result_service.py", "test_resume.py",
    "test_session.py", "test_task_runner.py", "test_url_normalizer.py",
    "test_video_parser.py", "fixtures.py", "__init__.py",
]

# 包名改写：(正则, 替换)——长前缀在前
_PKG_NAMES = ["collector", "core", "db", "services", "export", "log", "chat",
              "constants", "config", "update"]


def _rewrite(text: str) -> tuple[str, list[str]]:
    """改写包名前缀，返回 (新文本, 残留问题列表)。"""
    residual: list[str] = []

    # 1) app.collector.X → collector.collect.X（必须在通用规则前）
    text = re.sub(r"\bapp\.collector\.", "collector.collect.", text)
    text = re.sub(r"\bapp\.collector\b(?=\s+import)", "collector.collect", text)
    text = re.sub(r"\bapp\.collector\b(?![\w.])", "collector.collect", text)

    # 2) app.<other>.X → collector.<other>.X
    names = "|".join(n for n in _PKG_NAMES if n != "collector")
    text = re.sub(rf"\bapp\.({names})\.", r"collector.\1.", text)
    # 3) from app.<other> import ...  （模块名后直接跟 import）
    text = re.sub(rf"\bapp\.({names})(\s+import)", r"collector.\1\2", text)

    # 4) 测试包内部引用
    text = re.sub(r"\bfrom tests\.", "from collector.tests.", text)
    text = re.sub(r"\bimport tests\.", "import collector.tests.", text)
    text = re.sub(r"\bfrom mocks\.", "from collector.tests.mocks.", text)

    # 5) 残留检查
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r"\bapp\.(ui|app)\b", line):
            residual.append(f"  第{i}行 仍引用 UI/入口: {line.strip()[:100]}")
        elif re.search(r"(?<![\w.])app\.(core|db|services|collector|export|log|chat|constants|config|update)\b", line):
            residual.append(f"  第{i}行 残留旧前缀: {line.strip()[:100]}")
    return text, residual


def _copy_py(src: Path, dst: Path) -> tuple[int, list[str]]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_text(encoding="utf-8")
    new, residual = _rewrite(raw)
    dst.write_text(new, encoding="utf-8")
    return len(raw.splitlines()), residual


def main() -> int:
    if not SRC.exists():
        print(f"源目录不存在: {SRC}")
        return 1

    # 清掉上次可能的残留（只清我们管理的子目录，不动 common/ 等）
    for _, rel in COPY_DIRS:
        p = DST / rel
        if p.exists():
            shutil.rmtree(p)
    (DST / "tests").mkdir(parents=True, exist_ok=True)
    (DST / "tests" / "mocks").mkdir(parents=True, exist_ok=True)

    total_files = 0
    total_lines = 0
    problems: list[str] = []

    for srcrel, dstrel in COPY_DIRS:
        sdir = SRC / srcrel
        if not sdir.exists():
            problems.append(f"  缺少源目录: {srcrel}")
            continue
        for f in sorted(sdir.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            rel = f.relative_to(sdir)
            lines, res = _copy_py(f, DST / dstrel / rel)
            total_files += 1
            total_lines += lines
            problems += [f"  [{srcrel}/{rel}] {r.strip()}" for r in res]

    for srcrel, dstrel in COPY_FILES:
        f = SRC / srcrel
        if not f.exists():
            problems.append(f"  缺少源文件: {srcrel}")
            continue
        lines, res = _copy_py(f, DST / dstrel)
        total_files += 1
        total_lines += lines
        problems += [f"  [{srcrel}] {r.strip()}" for r in res]

    # 测试
    tdir = SRC / "tests"
    for name in TEST_FILES:
        f = tdir / name
        if not f.exists():
            problems.append(f"  缺少测试文件: tests/{name}")
            continue
        lines, res = _copy_py(f, DST / "tests" / name)
        total_files += 1
        total_lines += lines
        problems += [f"  [tests/{name}] {r.strip()}" for r in res]

    # mocks 包
    for f in sorted((SRC / "mocks").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        rel = f.relative_to(SRC / "mocks")
        lines, res = _copy_py(f, DST / "tests" / "mocks" / rel)
        total_files += 1
        total_lines += lines
        problems += [f"  [mocks/{rel}] {r.strip()}" for r in res]

    print(f"移植文件: {total_files} 个，{total_lines} 行")
    if problems:
        print(f"\n需要人工处理 {len(problems)} 处：")
        for p in problems:
            print(p)
    else:
        print("包名改写干净，无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
