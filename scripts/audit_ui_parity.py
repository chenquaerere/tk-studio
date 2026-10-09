# -*- coding: utf-8 -*-
"""原版界面  <->  合并版界面  控件级对照审计（W8）。

背景
----
合并工程「TK 素材工坊」的底线是 **功能零丢失 + 界面按原版**。
前两轮修复合集都靠人眼逐页翻，结果每轮都还有漏网 —— 说明方法错了。

本脚本改用**静态比对**：
  1. 用 AST 把原桌面程序 `app/ui/**` 里所有 **用户可见文案** 抽出来
     （按钮 text / 标签 text / 输入框 placeholder_text / 下拉 values /
       PageHeader 的标题与副标题）；
  2. 去 emoji、去空白与标点后，在合并版前端（index.html + js/*.js）
     与后端（_app/server/*.py）里做子串查找；
  3. 输出「原版有 · 合并版找不到」的清单。

判据（为什么可信）
------------------
前端的每一个按钮/标签都必须在某处有对应实现 —— 找不到文案，就意味着
该控件**从未被搬过来**。这比"看截图回忆原版长什么样"可靠得多。

用法
----
    venv\\Scripts\\python.exe scripts\\audit_ui_parity.py
    venv\\Scripts\\python.exe scripts\\audit_ui_parity.py --json out.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LEGACY = Path(r"D:\TikTokLinkCollector")
MERGED = Path(r"D:\Tiktok-去水印")

# --- 归一化：去 emoji / 去空白标点 / 小写 -------------------------------------
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF"
    "\u2300-\u23FF\uFE0F\u200d\u20e3]"
)
_PUNCT = re.compile(r"[\s\u3000·｜|「」『』【】（）()：:；;、,，。.！!？?~～\-—_/\\*'\"`]+")

# 过于通用、单独存在无意义的文案（占位符 / 标点 / 纯数字），不参与判据
_IGNORE = {
    "", "无", "暂无", "提示", "请稍候", "完成", "失败", "全部", "确定", "取消",
    "是", "否", "关闭", "清空", "导出", "复制", "复制全部", "刷新", "保存",
    "true", "false", "0", "1", "2", "3",
}

# 原版有、但合并版刻意用**等价交互**承载的文案（经人工逐条确认，非遗漏）。
# 每条都注明合并版里的对应形态，避免这里变成"为了刷绿而开的洞"。
_EQUIVALENT = {
    # tkinter 的模态错误框 → 合并版统一走非模态 toast（不打断操作）
    "知道了",
    # 「下载完成 → 查看下载 / 暂不」→ 合并版在任务中心列进度、详情页可直接打开目录
    "查看下载", "暂不",
    # 原版独立「链接窗口」面板 → 合并版并入采集结果页（整行复制 / 导出 TXT / 分组）
    # 注意：「复制选中」自 W11 起已**原样还原**（`#rs-copy-sel` + 链接窗口里各一个），
    # 所以它从白名单里移除了 —— 现在是硬性判据，掉了就报红。
    # 原版窗口标题副标题 → 合并版是统一品牌「TK 素材工坊」，标题栏在 index.html
    "账号作品链接采集",
    "数据本地存储严格串行采集最新n条",
}


def norm(s: str) -> str:
    return _PUNCT.sub("", _EMOJI.sub("", s or "")).lower()


# --- 1) 从原版源码提取可见文案 ------------------------------------------------
class _TextCollector(ast.NodeVisitor):
    """抓 CTk* 控件的 text / placeholder_text / values / title。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, int]] = []

    def _add(self, val, lineno: int) -> None:
        if isinstance(val, str):
            self.items.append((val, lineno))

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        fname = ""
        if isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        elif isinstance(node.func, ast.Name):
            fname = node.func.id

        if fname.startswith("CTk") or fname in ("PageHeader", "Toast"):
            for kw in node.keywords:
                if kw.arg in ("text", "placeholder_text", "title"):
                    if isinstance(kw.value, ast.Constant):
                        self._add(kw.value.value, node.lineno)
                elif kw.arg == "values" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    for el in kw.value.elts:
                        if isinstance(el, ast.Constant):
                            self._add(el.value, node.lineno)
            if fname == "PageHeader":
                for arg in node.args[1:3]:
                    if isinstance(arg, ast.Constant):
                        self._add(arg.value, node.lineno)
        self.generic_visit(node)


def _legacy_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("app/ui/pages", "app/ui/components"):
        d = LEGACY / sub
        if d.is_dir():
            files += sorted(p for p in d.glob("*.py") if p.name != "__init__.py")
    for extra in ("app/ui/app_window.py", "app/ui/link_window.py",
                  "app/ui/widgets.py", "app/ui/theme.py"):
        p = LEGACY / extra
        if p.is_file():
            files.append(p)
    return files


def extract_legacy() -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = {}
    for f in _legacy_files():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            print(f"!! 解析失败 {f.name}: {exc}")
            continue
        c = _TextCollector()
        c.visit(tree)
        # 去重（同一文案保留最小行号）
        seen: dict[str, int] = {}
        for text, line in c.items:
            if text not in seen or line < seen[text]:
                seen[text] = line
        out[f.stem] = sorted(seen.items(), key=lambda kv: kv[1])
    return out


# --- 2) 合并版可见文本大池 ----------------------------------------------------
def load_merged_blob() -> tuple[str, dict[str, int]]:
    """返回 (归一化后的全文, {原始文件: 命中文本数})。"""
    parts: list[str] = []
    counts: dict[str, int] = {}
    targets: list[Path] = []
    fe = MERGED / "_app" / "frontend"
    for p in sorted(fe.rglob("*.html")) + sorted(fe.rglob("*.js")) + sorted(fe.rglob("*.css")):
        targets.append(p)
    for p in sorted((MERGED / "_app" / "server").glob("*.py")):
        targets.append(p)
    for p in sorted((MERGED / "_app" / "collector").rglob("*.py")):
        targets.append(p)
    for extra in ("_app/common/version.py",):
        p = MERGED / extra
        if p.is_file():
            targets.append(p)
    for p in targets:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n = norm(raw)
        parts.append(n)
        counts[str(p.relative_to(MERGED))] = len(n)
    return "".join(parts), counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把结果写成 JSON")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="同时打印每页命中的文案（默认只打印缺失）")
    args = ap.parse_args()

    legacy = extract_legacy()
    blob, _ = load_merged_blob()

    report: dict[str, dict] = {}
    total = miss_total = 0
    print("=" * 78)
    print("原版界面  <->  合并版界面  控件级对照审计")
    print("=" * 78)

    for page, items in legacy.items():
        hits, misses = [], []
        for text, line in items:
            key = norm(text)
            if not key or key in _IGNORE or key in _EQUIVALENT or len(key) < 2:
                continue
            total += 1
            (hits if key in blob else misses).append((text, line))
        miss_total += len(misses)
        report[page] = {
            "total": len(hits) + len(misses),
            "hit": len(hits),
            "missing": [{"text": t, "line": ln} for t, ln in misses],
        }
        flag = "OK " if not misses else "!! "
        print(f"\n{flag}{page:24s} 命中 {len(hits):3d} / {len(hits)+len(misses):3d}"
              + ("" if not misses else f"   ★ 缺失 {len(misses)}"))
        for text, line in misses:
            print(f"      ✗ L{line:<5d} {text!r}")
        if args.verbose:
            for text, line in hits:
                print(f"      ✓ L{line:<5d} {text!r}")

    print("\n" + "=" * 78)
    print(f"合计：原版文案 {total} 项，合并版找不到 {miss_total} 项 "
          f"（命中率 {(total - miss_total) / max(1, total) * 100:.1f}%）")
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {args.json}")
    return 0 if miss_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
