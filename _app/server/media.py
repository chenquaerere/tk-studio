"""媒体/文件相关的通用工具（v4.0）。

原来 `_outputs_payload` / `file_url` 定义在 `server/api.py`，
而 `server/services.py` 又需要它们 —— 形成 services <-> api 的循环依赖
（靠函数内 import 绕开，既不干净也容易踩坑）。

抽到本模块后：
- api 与 services 都从这里导入，无循环；
- 结果文件的分类（图片/视频）与受控 URL 生成集中一处。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif"}
VID_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".ts"}


def file_url(path: str) -> str:
    """把本地路径转成前端可直接使用的受控 URL（/api/file）。"""
    return "/api/file?path=" + quote(str(path), safe="")


def classify(path: str | Path) -> str:
    """返回 image / video / other。"""
    suf = Path(path).suffix.lower()
    if suf in IMG_EXTS:
        return "image"
    if suf in VID_EXTS:
        return "video"
    return "other"


def outputs_payload(paths) -> list[dict]:
    """把输出列表转成前端可直接渲染的结构。

    兼容两种输入（v4.0 修复）：
      - 路径字符串 / Path：来自下载任务（files）、素材库
      - 已经是本函数产出的字典：来自高清任务推送的 outputs/gallery/videos
        （之前没兼容这种，导致「高清图片处理完成但结果为空」）
    """
    out: list[dict] = []
    seen = set()
    for p in paths or []:
        try:
            # 已经是结构化条目 —— 补齐字段后直接采用
            if isinstance(p, dict):
                path = str(p.get("path") or "")
                if not path or path in seen:
                    continue
                fp = Path(path)
                if not fp.is_file():
                    continue
                seen.add(path)
                kind = p.get("kind") or classify(fp)
                out.append({
                    "path": path,
                    "url": p.get("url") or file_url(path),
                    "name": p.get("name") or fp.name,
                    "kind": kind,
                    "is_image": kind == "image",
                    "is_video": kind == "video",
                    "size": p.get("size") or fp.stat().st_size,
                })
                continue

            fp = Path(p)
            key = str(fp)
            if key in seen or not fp.is_file():
                continue
            seen.add(key)
            kind = classify(fp)
            out.append({
                "path": key,
                "url": file_url(key),
                "name": fp.name,
                "kind": kind,
                "is_image": kind == "image",
                "is_video": kind == "video",
                "size": fp.stat().st_size,
            })
        except Exception:
            continue
    return out
