"""Model registry + download helpers.

字段说明
--------
category  —— 分类：超分 / 人脸 / 图像重建 / 视频重建
stage     —— 接入状态：
               "ready"    已接入处理流水线，可在「高清修复」中直接选用
               "optional" 已登记可下载，但尚未接入流水线
               "planned"  规划中，仅展示，不提供自动下载
download  —— "auto"   可用内置下载器自动下载
               "manual" 需手动下载（多文件 / 大体积权重）
             仅 stage="ready" 的模型会被「下载全部模型」收录。
arch      —— 推理架构标识，决定用哪个引擎加载
note      —— 一句话说明，展示在模型卡片上
vram_gb   —— 显存占用估算（GB），扩散类模型才有
source    —— 项目主页，手动下载时参考
"""

import urllib.request
from pathlib import Path

from ..config import PROJECT_ROOT

MODELS_DIR = PROJECT_ROOT / "models"

MODELS = {
    # ================= 超分 · 已接入流水线 =================
    "RealESRGAN_x4plus": {
        "name": "Real-ESRGAN x4（通用/照片，推荐）",
        "category": "超分",
        "stage": "ready",
        "download": "auto",
        "arch": "rrdb",
        "scale": 4,
        "num_block": 23,
        "num_grow_ch": 32,
        "file": "RealESRGAN_x4plus.pth",
        "size_mb": 64,
        "short": "x4",
        "note": "经典全能款，静态照片细节上限高",
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
    },
    "RealESRGAN_x2plus": {
        "name": "Real-ESRGAN x2（通用/照片，细节增强）",
        "category": "超分",
        "stage": "ready",
        "download": "auto",
        "arch": "rrdb",
        "scale": 2,
        "num_block": 23,
        "num_grow_ch": 32,
        "file": "RealESRGAN_x2plus.pth",
        "size_mb": 64,
        "short": "x2",
        "note": "2x 放大，速度快且伪影少，适合短视频源",
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    },
    "RealESRGAN_x4plus_anime_6B": {
        "name": "Real-ESRGAN x4（动漫/插画）",
        "category": "超分",
        "stage": "ready",
        "download": "auto",
        "arch": "rrdb",
        "scale": 4,
        "num_block": 6,
        "num_grow_ch": 32,
        "file": "RealESRGAN_x4plus_anime_6B.pth",
        "size_mb": 18,
        "short": "x4a",
        "note": "二次元素材专用，真人素材会出「塑料感」",
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
    },
    "realesr-general-x4v3": {
        "name": "Real-ESRGAN 通用轻量 x4（快速）",
        "category": "超分",
        "stage": "ready",
        "download": "auto",
        "arch": "srvgg",
        "scale": 4,
        "num_conv": 32,
        "file": "realesr-general-x4v3.pth",
        "size_mb": 5,
        "short": "v3",
        "note": "体积仅 4.9MB，对压缩噪声抑制好，速度/质量平衡最佳",
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
    },

    # ================= 超分 · 新增可选（现有引擎直接支持） =================
    "4x-UltraSharp": {
        "name": "4x-UltraSharp（真实照片锐化）",
        "category": "超分",
        "stage": "optional",
        "download": "auto",
        "arch": "rrdb",
        "scale": 4,
        "num_block": 23,
        "num_grow_ch": 32,
        "file": "4x-UltraSharp.pth",
        "size_mb": 67,
        "short": "ultra",
        "note": "社区口碑第一，真实照片锐度普遍优于原版 x4plus",
        # HuggingFace 在部分网络下不可达，优先用国内镜像，失败再回退官方源
        "urls": [
            "https://hf-mirror.com/lokCX/4x-Ultrasharp/resolve/main/4x-UltraSharp.pth",
            "https://huggingface.co/lokCX/4x-Ultrasharp/resolve/main/4x-UltraSharp.pth",
        ],
    },
    "realesr-animevideov3": {
        "name": "Real-ESRGAN 动漫视频 x4（番剧专用）",
        "category": "超分",
        "stage": "optional",
        "download": "auto",
        "arch": "srvgg",
        "scale": 4,
        "num_conv": 16,
        "file": "realesr-animevideov3.pth",
        "size_mb": 2,
        "short": "av3",
        "note": "动漫视频专用，体积仅 2.5MB，比 general-x4v3 更贴合番剧线条",
        "url": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
    },

    # ================= 人脸 =================
    "GFPGANv1.4": {
        "name": "GFPGAN v1.4 人脸修复（已接入）",
        "category": "人脸",
        "stage": "ready",
        "download": "auto",
        "arch": "gfpgan",
        "scale": 1,
        "file": "GFPGANv1.4.pth",
        "size_mb": 333,
        "short": "face",
        "note": "经典人脸重建，速度快；重建过强时脸部会「换头」",
        "url": "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/GFPGANv1.4.pth",
    },
    "CodeFormer": {
        "name": "CodeFormer 人脸修复（质量更高）",
        "category": "人脸",
        "stage": "optional",
        "download": "auto",
        "arch": "codeformer",
        "scale": 1,
        "file": "codeformer.pth",
        "size_mb": 359,
        "short": "cf",
        "note": "码本+Transformer，严重退化输入下优于 GFPGAN，支持 fidelity 参数调节",
        "fidelity_default": 0.5,
        "url": "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/codeformer.pth",
    },
    "RestoreFormer": {
        "name": "RestoreFormer 人脸修复（备选）",
        "category": "人脸",
        "stage": "optional",
        "download": "auto",
        "arch": "restoreformer",
        "scale": 1,
        "file": "RestoreFormer.pth",
        "size_mb": 277,
        "short": "rf",
        "note": "gfpgan 包已内置该架构，接入成本最低",
        # 注意路径全小写：大写会 307 跳转，多一跳更容易被中断
        "urls": [
            "https://hf-mirror.com/leonelhs/gfpgan/resolve/main/RestoreFormer.pth",
            "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/RestoreFormer.pth",
        ],
    },

    # ================= 图像重建（扩散） =================
    "DiffBIR": {
        "name": "DiffBIR v2.1 图像深度重建（扩散）",
        "category": "图像重建",
        "stage": "planned",
        "download": "manual",
        "arch": "diffbir",
        "scale": 4,
        "file": "diffbir",
        "kind": "dir",
        "size_mb": 1700,
        "short": "diffbir",
        "note": "扩散式盲修复，内置 CodeFormer；需额外下载 SD2.1 基座，12GB 显存可跑",
        "vram_gb": 8,
        "source": "https://github.com/XPixelGroup/DiffBIR",
        "url": None,
    },

    # ================= 视频重建（扩散） =================
    "FlashVSR": {
        "name": "FlashVSR 视频深度超分（扩散）",
        "category": "视频重建",
        "stage": "planned",
        "download": "manual",
        "arch": "flashvsr",
        "scale": 4,
        "file": "flashvsr",
        "kind": "dir",
        "size_mb": 2000,
        "short": "fvsr",
        "note": "流式一步扩散，官方标注 12GB 显存 + 32GB 内存；建议按 4x 使用",
        "vram_gb": 11,
        "source": "https://github.com/OpenImagingLab/FlashVSR",
        "url": None,
    },
}

# 分类展示顺序
CATEGORY_ORDER = ["超分", "人脸", "图像重建", "视频重建"]

# 接入状态对应的中文标签
STAGE_LABEL = {
    "ready": "已接入",
    "optional": "可选",
    "planned": "规划中",
}


def model_path(key: str) -> Path:
    return MODELS_DIR / MODELS[key]["file"]


def model_exists(key: str) -> bool:
    """判断模型权重是否已就位（兼容单文件与多文件目录两种形态）。"""
    info = MODELS.get(key)
    if not info:
        return False
    p = model_path(key)
    if info.get("kind") == "dir":
        try:
            return p.is_dir() and any(p.iterdir())
        except Exception:
            return False
    try:
        return p.exists() and p.stat().st_size > 1024 * 1024
    except Exception:
        return False


def model_status() -> list[dict]:
    """模型清单（供「模型中心 → 模型库」表格使用）。"""
    rows = []
    for key, info in MODELS.items():
        size = info["size_mb"]
        rows.append(
            {
                "key": key,
                "分类": info.get("category", "其他"),
                "模型": info["name"],
                "体积": f"约 {size} MB" if size >= 1 else f"约 {size * 1024:.0f} KB",
                "显存": f"约 {info['vram_gb']} GB" if info.get("vram_gb") else "—",
                "接入状态": STAGE_LABEL.get(info.get("stage", "ready"), "未知"),
                "权重": "已就位" if model_exists(key) else "未下载",
                "说明": info.get("note", ""),
            }
        )
    return rows


def model_cards() -> list[dict]:
    """按分类归组的模型卡片数据（供卡片式渲染）。"""
    groups = []
    for cat in CATEGORY_ORDER:
        items = [dict(info, key=k) for k, info in MODELS.items()
                 if info.get("category") == cat]
        if not items:
            continue
        for it in items:
            it["installed"] = model_exists(it["key"])
            it["stage_label"] = STAGE_LABEL.get(it.get("stage", "ready"), "未知")
        groups.append({"category": cat, "items": items})
    return groups


def category_of(key: str) -> str:
    return MODELS.get(key, {}).get("category", "其他")


def ready_keys() -> list[str]:
    """已接入流水线、可自动下载的模型 key 列表。"""
    return [k for k, v in MODELS.items()
            if v.get("stage") == "ready" and v.get("download") == "auto"]


def selectable_keys() -> list[str]:
    """可用于处理流程的模型（架构已被现有引擎支持的超分模型）。"""
    ok_arch = {"rrdb", "srvgg"}
    return [k for k, v in MODELS.items()
            if v.get("category") == "超分" and v.get("arch") in ok_arch]


def download_urls(info: dict) -> list[str]:
    """取模型的所有下载源（支持 urls 列表 + 单个 url，按顺序回退）。"""
    urls = list(info.get("urls") or [])
    if info.get("url"):
        urls.append(info["url"])
    return urls


def download_model(key: str, progress_cb=None) -> Path:
    """下载模型权重到 models/ 目录，多源自动回退。"""
    info = MODELS[key]
    p = model_path(key)
    if model_exists(key):
        if progress_cb:
            progress_cb(1.0, "已存在，跳过")
        return p
    urls = download_urls(info)
    if not urls:
        raise RuntimeError(
            f"「{info['name']}」需手动下载（多文件 / 大体积权重）。\n"
            f"请前往项目主页获取：{info.get('source', '（未提供）')}\n"
            f"下载后放入：{MODELS_DIR}"
        )
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    last_err = None
    for i, url in enumerate(urls, 1):
        # 同一源最多试 2 次：大文件下载偶发连接被重置，重试往往即可成功
        for attempt in (1, 2):
            tmp.unlink(missing_ok=True)
            try:
                if progress_cb:
                    hint = f"下载源 {i}/{len(urls)}：{url.split('/')[2]}"
                    if attempt > 1:
                        hint += f"（第 {attempt} 次尝试）"
                    progress_cb(0.0, hint)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                    read = 0
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        read += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(read / total, 1.0), f"{read / 2**20:.0f} MB")
                if total and tmp.stat().st_size != total:
                    raise RuntimeError(
                        f"下载不完整（{tmp.stat().st_size}/{total} 字节）")
                if tmp.stat().st_size < 1024:
                    raise RuntimeError("下载内容异常（文件过小）")
                tmp.rename(p)
                if progress_cb:
                    progress_cb(1.0, "完成")
                return p
            except Exception as exc:
                last_err = exc
        if progress_cb:
            progress_cb(0.0, f"下载源 {i} 失败，尝试下一个...")
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"所有下载源均失败：{last_err}")


def download_all_missing(progress_cb=None) -> int:
    """仅下载「已接入流水线」的模型，避免误拉 GB 级规划中权重。"""
    n = 0
    for key in ready_keys():
        if not model_exists(key):
            if progress_cb:
                progress_cb(0.0, f"下载 {MODELS[key]['file']} ...")
            download_model(key, progress_cb)
            n += 1
    return n
