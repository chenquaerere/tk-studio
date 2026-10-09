"""模型组合预设：把「模型 × 倍率 × 人脸 × 档位」收敛成一个可读的场景名。

设计目标
--------
用户不需要理解 RealESRGAN 各模型的差异，选一个场景即可得到一组合理参数。
预设只提供「默认值」，用户手动改过参数后即视为自定义，预设按钮变为未选中态。

字段约定
--------
image     图片参数：model / scale / face
video     视频参数：model / scale / face / side
scale     "自动（推荐）" | "2x" | "4x"
side      视频输出长边档位（与 app.py 的 VIDEO_SIDE_CHOICES 对应）
"""

PRESETS = [
    {
        "key": "fast_preview",
        "icon": "⚡",
        "label": "极速预览",
        "desc": "大批量先出效果，速度优先",
        "image": {"model": "realesr-general-x4v3", "scale": "2x", "face": False},
        "video": {"model": "realesr-general-x4v3", "scale": "2x",
                  "face": False, "side": 1920},
    },
    {
        "key": "balanced",
        "icon": "🎯",
        "label": "均衡推荐",
        "desc": "日常通用，速度与质量平衡",
        "image": {"model": "realesr-general-x4v3", "scale": "自动（推荐）", "face": True},
        "video": {"model": "realesr-general-x4v3", "scale": "自动（推荐）",
                  "face": True, "side": 2560},
    },
    {
        "key": "photo",
        "icon": "🖼",
        "label": "高清照片",
        "desc": "真人照片、商品图、风景",
        "image": {"model": "4x-UltraSharp", "scale": "自动（推荐）", "face": True},
        "video": {"model": "4x-UltraSharp", "scale": "自动（推荐）",
                  "face": True, "side": 2560},
    },
    {
        "key": "portrait",
        "icon": "🧑",
        "label": "人像特写",
        "desc": "脸与手占比大的素材",
        "image": {"model": "RealESRGAN_x2plus", "scale": "2x", "face": True},
        "video": {"model": "RealESRGAN_x2plus", "scale": "2x",
                  "face": True, "side": 1920},
    },
    {
        "key": "anime_image",
        "icon": "🎨",
        "label": "动漫插画",
        "desc": "二次元图片与立绘",
        "image": {"model": "RealESRGAN_x4plus_anime_6B", "scale": "4x", "face": False},
        "video": {"model": "RealESRGAN_x4plus_anime_6B", "scale": "4x",
                  "face": False, "side": 2560},
    },
    {
        "key": "anime_video",
        "icon": "📺",
        "label": "动漫视频",
        "desc": "番剧与动画短视频",
        "image": {"model": "RealESRGAN_x4plus_anime_6B", "scale": "4x", "face": False},
        "video": {"model": "realesr-animevideov3", "scale": "自动（推荐）",
                  "face": False, "side": 2560},
    },
    {
        "key": "video_general",
        "icon": "🎞",
        "label": "视频通用",
        "desc": "TikTok / 抖音短视频",
        "image": {"model": "realesr-general-x4v3", "scale": "自动（推荐）", "face": True},
        "video": {"model": "realesr-general-x4v3", "scale": "自动（推荐）",
                  "face": True, "side": 2560},
    },
    {
        "key": "max_quality",
        "icon": "🏆",
        "label": "极致画质",
        "desc": "单张精修，不怕等",
        "image": {"model": "4x-UltraSharp", "scale": "4x", "face": True},
        "video": {"model": "4x-UltraSharp", "scale": "4x",
                  "face": True, "side": 3840},
    },
    {
        "key": "deep_rebuild",
        "icon": "💎",
        "label": "深度重建",
        "desc": "严重模糊，需生成式重建（规划中）",
        "image": {"model": "DiffBIR", "scale": "自动（推荐）", "face": True},
        "video": {"model": "FlashVSR", "scale": "4x", "face": True, "side": 2560},
        "planned": True,
    },
]

PRESET_MAP = {p["key"]: p for p in PRESETS}


def labels() -> list[tuple[str, str]]:
    """返回 [(显示名, key)]，供下拉框使用。"""
    return [(f"{p['icon']} {p['label']}", p["key"]) for p in PRESETS]


def get(key: str) -> dict:
    return PRESET_MAP.get(key, {})


def for_kind(key: str, kind: str) -> dict:
    """取某个预设中图片或视频的参数（kind: "image" | "video"）。"""
    return PRESET_MAP.get(key, {}).get(kind, {})


def describe(key: str, kind: str) -> str:
    """生成一行可读的参数说明，用于预设按钮提示或预设总览页。"""
    from .engines.models import MODELS
    p = PRESET_MAP.get(key)
    if not p:
        return ""
    cfg = p.get(kind, {})
    model = MODELS.get(cfg.get("model", ""), {}).get("name", cfg.get("model", "—"))
    parts = [f"模型：{model}", f"倍率：{cfg.get('scale', '自动')}"]
    if kind == "image":
        parts.append("人脸修复：" + ("开" if cfg.get("face") else "关"))
    else:
        side = cfg.get("side")
        parts.append(f"输出长边：{side if side else '不限制'}")
    return " ｜ ".join(parts)
