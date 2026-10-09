"""生成 server/services.py —— 从 app.py 抽出「与界面无关」的业务内核。

第 25~1757 行是业务逻辑（几乎不碰 gradio），
第 1758 行起是 build_ui() 等 Gradio 装配代码。

本脚本把这部分抽出来，并做三处必要处理：
1. 替换少量 gr.* 调用（notify/progress/update）为 bus 或替身；
2. 补上原来定义在 UI 段、但业务/API 需要的几个函数：
   _refresh_all / _apply_img_tier / models_cards_html / presets_html /
   update_notice_html / changelog_markdown；
3. 来源为 app.py :: def <name>( 以及支持多行签名的 def。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app.py"
DST = ROOT / "server" / "services.py"

# 业务段与 UI 段的分界：build_ui 定义处
text = SRC.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

ui_idx = None
for i, ln in enumerate(lines):
    if ln.startswith("def build_ui("):
        ui_idx = i
        break
if ui_idx is None:
    sys.exit("未找到 build_ui 定义，无法定位分界")
core = "".join(lines[:ui_idx]).rstrip() + "\n"

# ---- 1) 替换与界面强耦合的调用 -------------------------------------------
replacements = [
    # 用户提示：原来直接调 gr.Info / gr.Warning / gr.Error
    ('gr.Info(f"✅ 处理完成：成功 {ok} 项")',
     'bus.notify(f"✅ 处理完成：成功 {ok} 项", "ok")'),
    ('gr.Warning(f"处理完成：成功 {ok} 项，失败 {errs} 项")',
     'bus.notify(f"处理完成：成功 {ok} 项，失败 {errs} 项", "warn")'),
    ('gr.Error("全部处理失败，请查看“处理信息”")',
     'bus.notify("全部处理失败，请查看“处理信息”", "error")'),
    ('gr.Info("✅ 全部下载完成")',
     'bus.notify("✅ 全部下载完成", "ok")'),
    ('gr.Warning(f"完成 {n[1]} 个，失败 {job[\'fail\']} 个")',
     'bus.notify(f"完成 {n[1]} 个，失败 {job[\'fail\']} 个", "warn")'),
    # do_parse 里的进度条与下拉选项更新
    ('    progress = gr.Progress(track_tqdm=False)\n', ''),
    ('        progress((i - 1) / len(urls), desc=f"正在解析 {i}/{len(urls)}")\n',
     '        bus.notify(f"正在解析 {i}/{len(urls)}", "info")\n'),
    ('            gr.update(choices=options, value=[]), msg, len(infos))',
     '            {"choices": options, "value": []}, msg, len(infos))'),
    # tqdm 进度（模型下载）在无控制台下不可见，改为无效
    # 注意 app.py 里这两处的缩进层级不同，用小写精确匹配 + 兜底清行
    ('            progress = gr.Progress(track_tqdm=True)\n', ''),
    # 样式体系已迁到前端（frontend/css/app.css），服务层不再下发 CSS
    ('CSS = tk_theme.CSS', 'CSS = ""   # 样式已迁到 frontend/css/app.css'),
    # 下面这个 models_cards_html 用了 ui.theme，稍后会被纯 HTML 版本覆盖
    ('        return tk_theme.models_html(model_cards())',
     '        return _html_escape(model_cards())'),
    ('        return tk_theme.note(f"模型卡片渲染失败：{exc}")',
     '        return f"模型卡片渲染失败：{_html_escape(exc)}"'),
    ('    badge = (tk_theme.tag("规划中", "planned") if planned\n'
     '             else tk_theme.tag("可用", "ready"))',
     '    badge = ("规划中" if planned else "可用")'),
    ('        return tk_theme.release_html(changelog_mod.latest())',
     '        return _html_escape(changelog_mod.latest())'),
    ('    return tk_theme.stat_row_html([', '    return _stat_row_html(['),
]
for old, new in replacements:
    if old not in core:
        print(f"[warn] 未找到待替换片段（可能已改）: {old[:60]!r}")
    core = core.replace(old, new)

# 兜底：任何剩余的 `xxx = gr.Progress(...)` 或裸 `gr.Progress(...)` 整行都删掉
core = re.sub(r"^[ \t]*(?:[A-Za-z_]\w* = )?gr\.Progress\([^\n]*\)\n", "", core,
              flags=re.MULTILINE)
# 兜底：任何残留的 tk_theme.<方法>(...) 调用整体删行（这些是纯展示辅助）
core = re.sub(r"^[ \t]*.*tk_theme\.[^\n]*\n", "", core, flags=re.MULTILINE)

assert "gr." not in core, "业务段仍残留 gr. 调用：\n" + "\n".join(
    f"  {i+1}: {l.rstrip()}" for i, l in enumerate(core.splitlines())
    if re.search(r"\bgr\.", l))

# ---- 2) 改造 import 头 ----------------------------------------------------
head_old = "import gradio as gr\nfrom tqdm import tqdm\n"
head_new = ("from tqdm import tqdm\n")
if head_old in core:
    core = core.replace(head_old, head_new)
else:
    # 兜底：逐行剔除 gradio 导入
    core = "\n".join(l for l in core.splitlines()
                     if not l.strip().startswith("import gradio")) + "\n"

# ui/theme 仍在业务段被引用（models_cards_html 等），但那些函数会被我们重写，
# 这里保留 import 会在无 gradio 环境下失败 —— 直接去掉，后面用纯 HTML 实现。
core = core.replace("from ui import theme as tk_theme\n", "")

# ---- 3) 补回被 UI 段"带走"的、但业务/API 需要的函数 -----------------------
EXTRA = '''

# =====================================================================
# 以下函数原来在 app.py 的 UI 段（build_ui 附近），但它们提供的是
# 「给前端用的数据/HTML」，属于服务层职责。v4.0 抽到 services.py，
# 并去掉对 gradio / ui.theme 的依赖，改为纯 HTML 生成。
# =====================================================================

def _refresh_all():
    """模型库刷新：返回（表格数据, 卡片 HTML）。"""
    return models_table(), models_cards_html()


def _apply_img_tier(tier, face):
    """按「档位」快速设定图片模型/倍率（混合批量页用）。"""
    mapping = {
        "快（general-x4v3）": ("realesr-general-x4v3", "自动（推荐）"),
        "均衡（x2plus）": ("RealESRGAN_x2plus", "2x"),
        "极致（x4plus）": ("RealESRGAN_x4plus", "4x"),
        "动漫（anime-6B）": ("RealESRGAN_x4plus_anime_6B", "4x"),
    }
    model, scale = mapping.get(tier, (None, None))
    if model is None:
        return tier, face
    return model, scale


def _html_escape(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _stat_row_html(items) -> str:
    """指标卡行 HTML。items: [(数值, 标签, 颜色), ...]"""
    cells = []
    for it in items or []:
        try:
            value, label, color = (list(it) + ["", "", ""])[:3]
        except Exception:
            continue
        cells.append(
            f'<div class="tk-stat tk-stat-{_html_escape(color)}">'
            f'<div class="tk-stat-value">{_html_escape(value)}</div>'
            f'<div class="tk-stat-label">{_html_escape(label)}</div></div>')
    return '<div class="tk-stat-row">' + "".join(cells) + "</div>"


def model_cards_items():
    """模型卡片的结构化数据（给新前端用，比 HTML 更好渲染）。"""
    rows = []
    for m in model_status():
        rows.append({
            "key": m.get("key", ""),
            "category": m.get("分类", ""),
            "name": m.get("模型", ""),
            "size": m.get("体积", ""),
            "vram": m.get("显存", ""),
            "stage": m.get("接入状态", ""),
            "weights": m.get("权重", ""),
        })
    return rows


def models_cards_html():
    """模型卡片 HTML（服务端渲染，供前端直接插入）。"""
    cards = []
    for m in model_cards_items():
        stage = m["stage"]
        cls = "tk-tag-ready" if "已接入" in stage else (
            "tk-tag-planned" if "规划" in stage else "tk-tag-optional")
        cards.append(
            '<div class="tk-model-card">'
            f'<div class="tk-model-name">{_html_escape(m["name"])}</div>'
            f'<div class="tk-model-meta">{_html_escape(m["category"])} · '
            f'{_html_escape(m["size"])} · 显存 {_html_escape(m["vram"])}</div>'
            f'<span class="tk-tag {cls}">{_html_escape(stage)}</span>'
            "</div>")
    return '<div class="tk-model-grid">' + "".join(cards) + "</div>"


def presets_html():
    """预设清单 HTML。"""
    items = []
    for p in PRESETS:
        items.append(
            '<div class="tk-preset-card">'
            f'<div class="tk-preset-name">{_html_escape(p.get("icon", ""))} '
            f'{_html_escape(p.get("label", ""))}</div>'
            f'<div class="tk-preset-desc">{_html_escape(p.get("desc", ""))}</div>'
            "</div>")
    return '<div class="tk-preset-grid">' + "".join(items) + "</div>"


def update_notice_html():
    """「本次更新」提示 HTML（未读时才有内容）。"""
    try:
        seen = str(load().get("last_seen_version", "") or "")
        if seen == VERSION:
            return ""
        return (f'<div class="tk-notice">已更新到 v{VERSION}，'
                f'可到「设置 → 关于」查看更新日志。</div>')
    except Exception:
        return ""


def changelog_markdown():
    """更新日志 Markdown。"""
    try:
        return changelog_mod.markdown()
    except Exception:
        try:
            return changelog_mod.get_changelog()
        except Exception as e:
            return f"（更新日志读取失败：{e}）"
'''

out = core.rstrip() + "\n" + EXTRA
DST.parent.mkdir(parents=True, exist_ok=True)
DST.write_text(out, encoding="utf-8")
print(f"已生成 {DST}  行数={len(out.splitlines())}")
print(f"业务段来源行数={ui_idx}（app.py 第 1~{ui_idx} 行）")
