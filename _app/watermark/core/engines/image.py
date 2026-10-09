"""Image enhancement engine: Real-ESRGAN upscaling + GFPGAN face restore."""

import logging
import os
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from ..config import load
from ..output import make_output_path
from ..record import done_info, mark_done
from .models import MODELS, model_path

log = logging.getLogger(__name__)

_device = None
_lock = threading.Lock()
_upsamplers = {}
_face_enhancer = None
_heif_registered = False
_normalized = set()


def _patch_torchvision_compat():
    """basicsr 1.4.2 imports a module removed in torchvision>=0.18.

    Provide a shim so imports keep working on current torchvision.
    """
    try:
        import sys
        import types

        import torchvision.transforms.functional as F

        name = "torchvision.transforms.functional_tensor"
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.rgb_to_grayscale = F.rgb_to_grayscale
            sys.modules[name] = mod
    except Exception:
        pass


def device():
    global _device
    if _device is None:
        try:
            import torch
            _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        except Exception:
            _device = None
    return _device


def register_heif():
    global _heif_registered
    if not _heif_registered:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
            _heif_registered = True
        except Exception:
            pass


def ensure_checkpoint_loadable(key: str) -> None:
    """把权重整理成 RealESRGANer 能直接读取的标准形态。

    处理两种社区模型的常见差异（每个模型只整理一次，原子写入）：
    1. 「裸 state_dict」—— 官方权重带 params / params_ema 外层，
       社区模型（如 4x-UltraSharp）多为裸 state_dict，直接加载报
       `KeyError: 'params'`。这里补一层 params 包装。
    2. 「旧版 ESRGAN 命名」—— 旧 BasicSR 训练出的权重键名为
       `model.0` / `model.1.sub.N.RDB1.conv1.0` / `model.3` …，
       与 Real-ESRGAN 的 `conv_first` / `body.N.rdb1.conv1` / `conv_up1`
       不一致，这里做一次键名映射（映射后再校验，失败则保留原样）。
    """
    if key in _normalized:
        return
    p = model_path(key)
    if not (p.exists() and p.stat().st_size > 1024 * 1024):
        _normalized.add(key)
        return
    try:
        import torch
        sd = torch.load(str(p), map_location="cpu", weights_only=True)
        inner = sd.get("params") or sd.get("params_ema") if isinstance(sd, dict) else None
        if inner is None:
            inner = sd
        changed = None

        # 1) 旧版 ESRGAN 命名 -> Real-ESRGAN 命名
        if isinstance(inner, dict) and any(
                str(k).startswith("model.0.") for k in inner):
            remapped = _remap_legacy_rrdb(inner)
            if _loadable(remapped, key):
                inner = remapped
                changed = "键名映射（旧版 ESRGAN → Real-ESRGAN）"
            else:
                log.warning("%s 疑似旧版命名，但映射后仍无法加载，保留原样", p.name)

        # 2) 补 params 外层
        if changed or not isinstance(sd, dict) or (
                "params" not in sd and "params_ema" not in sd):
            tmp = p.with_suffix(p.suffix + ".norm")
            torch.save({"params": inner}, str(tmp))
            os.replace(str(tmp), str(p))
            log.info("已规范化权重 %s：%s", p.name, changed or "补齐 params 外层")
        _normalized.add(key)
    except Exception as exc:
        log.warning("整理权重失败（将按原样尝试加载）：%s -> %s", p.name, exc)
        _normalized.add(key)


_LEGACY_TOP = {
    "model.0": "conv_first",
    "model.3": "conv_up1",
    "model.6": "conv_up2",
    "model.8": "conv_hr",
    "model.10": "conv_last",
}


def _remap_legacy_rrdb(sd: dict) -> dict:
    """旧版 ESRGAN / BasicSR 命名的 RRDB 权重 → Real-ESRGAN 命名。"""
    import re
    pat = re.compile(
        r"^model\.1\.sub\.(\d+)\.RDB(\d+)\.conv(\d+)\.0\.(weight|bias)$")
    out = {}
    for k, v in sd.items():
        if k.startswith("model.1.sub."):
            m = pat.match(k)
            if m:
                i, j, c, t = m.groups()
                out[f"body.{i}.rdb{j}.conv{c}.{t}"] = v
                continue
            parts = k.split(".")                 # model.1.sub.{i}.<rest...>
            rest = parts[4:]
            if rest and rest[0] == "conv_last":
                out[f"body.{parts[3]}.conv_last.{rest[-1]}"] = v
            else:                                # sub.{num_block}.* 即 conv_body
                out[f"conv_body.{rest[-1]}"] = v
            continue
        parts = k.split(".")
        top = f"{parts[0]}.{parts[1]}" if len(parts) > 1 else k
        if top in _LEGACY_TOP:
            out[f"{_LEGACY_TOP[top]}.{parts[-1]}"] = v
        else:
            out[k] = v
    return out


def _loadable(sd: dict, key: str) -> bool:
    """用对应架构试装一次，确认键名映射正确。"""
    try:
        import torch
        info = MODELS[key]
        if info["arch"] == "rrdb":
            from basicsr.archs.rrdbnet_arch import RRDBNet
            net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                          num_block=info["num_block"],
                          num_grow_ch=info.get("num_grow_ch", 32),
                          scale=info["scale"])
        else:
            from realesrgan.archs.srvgg_arch import SRVGGNetCompact
            net = SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64,
                                  num_conv=int(info.get("num_conv", 32)),
                                  upscale=4, act_type="prelu")
        net.load_state_dict(sd, strict=True)
        del net
        return True
    except Exception:
        return False


def get_upsampler(key: str, tile_override: int | None = None):
    _patch_torchvision_compat()
    cfg = load()
    tile = int(tile_override if tile_override is not None else cfg.get("tile", 0) or 0)
    dev = device()
    if dev is None:
        raise RuntimeError("未安装 PyTorch，请先运行 scripts/setup_env.bat")
    half = bool(cfg.get("half", True)) and dev.type == "cuda"
    cache_key = (key, tile)
    with _lock:
        if cache_key in _upsamplers:
            return _upsamplers[cache_key]
        info = MODELS[key]
        p = model_path(key)
        if not (p.exists() and p.stat().st_size > 1024 * 1024):
            from .models import download_model
            try:
                download_model(key)
            except Exception as exc:
                raise FileNotFoundError(f"模型 {key} 缺失且自动下载失败：{exc}") from exc
        ensure_checkpoint_loadable(key)
        if info["arch"] == "rrdb":
            from basicsr.archs.rrdbnet_arch import RRDBNet
            net = RRDBNet(
                num_in_ch=3, num_out_ch=3, num_feat=64,
                num_block=info["num_block"], num_grow_ch=32, scale=info["scale"],
            )
        else:  # srvgg
            from realesrgan.archs.srvgg_arch import SRVGGNetCompact
            net = SRVGGNetCompact(
                num_in_ch=3, num_out_ch=3, num_feat=64,
                num_conv=int(info.get("num_conv", 32)),
                upscale=4, act_type="prelu",
            )
        from realesrgan import RealESRGANer
        up = RealESRGANer(
            scale=info["scale"], model_path=str(p), model=net,
            tile=tile, tile_pad=10, pre_pad=0, half=half, device=dev,
        )
        _upsamplers[key] = up
        return up


def release_models() -> int:
    """卸载所有已缓存模型并回收显存（v4.0 新增）。

    原来模型只进不出：切换模型、切换 tile、二次运行都会把新的网络留在显存里，
    而且全代码里没有任何 torch.cuda.empty_cache()。在 12GB 卡上连续换模型跑，
    很容易后一次直接 OOM。

    返回释放掉的模型个数。
    """
    global _face_enhancer
    n = 0
    with _lock:
        n = len(_upsamplers) + (1 if _face_enhancer is not None else 0)
        _upsamplers.clear()
        _face_enhancer = None
    if n:
        try:
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
        except Exception:
            logging.getLogger(__name__).debug("释放显存失败", exc_info=True)
    return n


def get_face_enhancer():
    """GFPGAN face restorer. 背景已由超分模型处理，这里不重复放大背景。"""
    _patch_torchvision_compat()
    global _face_enhancer
    with _lock:
        if _face_enhancer is None:
            from gfpgan import GFPGANer
            p = model_path("GFPGANv1.4")
            if not (p.exists() and p.stat().st_size > 1024 * 1024):
                from .models import download_model
                try:
                    download_model("GFPGANv1.4")
                except Exception as exc:
                    raise FileNotFoundError(f"人脸修复模型缺失且自动下载失败：{exc}") from exc
            _face_enhancer = GFPGANer(
                model_path=str(p), upscale=1, arch="clean",
                channel_multiplier=2, bg_upsampler=None, device=device(),
            )
        return _face_enhancer


def load_image_bgr(path: Path) -> np.ndarray:
    register_heif()
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return np.array(im)[:, :, ::-1].copy()  # RGB -> BGR (cv2 / Real-ESRGAN convention)


def save_image_bgr(path: Path, img_bgr: np.ndarray, quality: int = 95) -> None:
    rgb = img_bgr[:, :, ::-1]
    img = Image.fromarray(rgb)
    if str(path).lower().endswith((".jpg", ".jpeg")):
        img.save(path, quality=quality, subsampling=1)  # 4:2:2，兼顾速度与质量
    else:
        img.save(path)


def _limit_output(img_bgr: np.ndarray, max_side: int) -> np.ndarray:
    """将输出限制在指定长边像素以内（高质量缩放），避免超大图。"""
    h, w = img_bgr.shape[:2]
    long_side = max(h, w)
    if long_side <= max_side:
        return img_bgr
    scale = max_side / long_side
    new_w = max(1, int(w * scale) & ~1)
    new_h = max(1, int(h * scale) & ~1)
    rgb = img_bgr[:, :, ::-1]
    resized = Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS)
    return np.array(resized)[:, :, ::-1]


def _shrink_input(img_bgr: np.ndarray, target_long: int) -> np.ndarray:
    """把输入先缩小，让模型输出接近目标长边，避免对超大图做全尺寸超分。"""
    h, w = img_bgr.shape[:2]
    scale = target_long / max(h, w)
    if scale >= 1.0:
        return img_bgr
    new_w = max(1, int(w * scale) & ~1)
    new_h = max(1, int(h * scale) & ~1)
    rgb = img_bgr[:, :, ::-1]
    return np.array(Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS))[:, :, ::-1]


def _enhance_faces_fast(img_bgr: np.ndarray, enh, work_side: int = 4096) -> np.ndarray:
    """GFPGAN 人脸修复提速：大图时先缩小到 work_side 检测+修复，
    再把修复后的脸区放大贴回原图（保留非人脸区域的超分细节）。

    旧方式直接在 8000px 级大图上检测，单张要 8~30 秒；此方式约 0.5~1 秒。
    """
    h, w = img_bgr.shape[:2]
    if max(h, w) <= work_side:
        _, _, restored = enh.enhance(
            img_bgr, has_aligned=False, only_center_face=False, paste_back=True
        )
        return restored
    s = work_side / max(h, w)
    nw = max(1, int(w * s) & ~1)
    nh = max(1, int(h * s) & ~1)
    small = np.array(
        Image.fromarray(img_bgr[:, :, ::-1]).resize((nw, nh), Image.LANCZOS)
    )[:, :, ::-1].copy()
    _, _, restored_small = enh.enhance(
        small, has_aligned=False, only_center_face=False, paste_back=True
    )
    boxes = list(getattr(enh.face_helper, "det_faces", []) or [])
    if not boxes:
        return img_bgr
    out = img_bgr.copy()
    fx = w / nw
    fy = h / nh
    for box in boxes:
        try:
            bx1, by1, bx2, by2 = (float(v) for v in box[:4])
        except Exception:
            continue
        bx1, by1, bx2, by2 = int(bx1), int(by1), int(bx2), int(by2)
        bx1, by1 = max(0, bx1), max(0, by1)
        bx2, by2 = min(nw, bx2), min(nh, by2)
        if bx2 <= bx1 or by2 <= by1:
            continue
        padx = int((bx2 - bx1) * 0.25)
        pady = int((by2 - by1) * 0.25)
        sx1, sy1 = max(0, bx1 - padx), max(0, by1 - pady)
        sx2, sy2 = min(nw, bx2 + padx), min(nh, by2 + pady)
        crop_small = restored_small[sy1:sy2, sx1:sx2]
        dx1, dy1 = int(sx1 * fx), int(sy1 * fy)
        dx2, dy2 = int(sx2 * fx), int(sy2 * fy)
        if dx2 <= dx1 or dy2 <= dy1:
            continue
        crop_full = np.array(
            Image.fromarray(crop_small[:, :, ::-1]).resize(
                (dx2 - dx1, dy2 - dy1), Image.LANCZOS
            )
        )[:, :, ::-1].copy()
        mask = np.ones((dy2 - dy1, dx2 - dx1), np.float32)
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(1, int((dx2 - dx1) * 0.05)))
        mask = mask[:, :, None]
        region = out[dy1:dy2, dx1:dx2]
        out[dy1:dy2, dx1:dx2] = (
            region.astype(np.float32) * (1 - mask)
            + crop_full.astype(np.float32) * mask
        ).astype(np.uint8)
    return out


def enhance_image(img_bgr: np.ndarray, model_key: str, outscale: int | None = None,
                  face_enhance: bool = False, max_side: int | None = None) -> np.ndarray:
    info = MODELS[model_key]
    native = info["scale"]
    src_long = max(img_bgr.shape[:2])
    if outscale is None:
        # 自动档：小图（长边<1024）才用模型原生倍率（x4）真放大；
        # 大图用 2x 保留原始细节，避免“先下采样再超分”导致画面发糊
        outscale = native if src_long < 1024 else 2
    if max_side is None:
        max_side = int(load().get("max_output_size", 4096) or 0)
    work = img_bgr
    if max_side > 0:
        long_side = max(work.shape[:2])
        if long_side * outscale > max_side:
            # 缩小输入，使模型输出约等于 max_side，显著加速并避免爆显存
            work = _shrink_input(work, max_side / outscale)
    # RealESRGANer 内部总是按模型原生倍率计算，outscale 只是最后一步 resize；
    # RTX 50 系等显卡上，RRDB 模型输出 8192px 级大图会触发慢核卡死几分钟，
    # 因此模型输出超过 4096 时自动改用分块推理（实测 8192 输出分块约 6 秒）。
    model_out = max(work.shape[:2]) * native
    tile = 256 if (info["arch"] == "rrdb" and model_out > 4096) else None
    up = get_upsampler(model_key, tile_override=tile)
    try:
        output, _ = up.enhance(work, outscale=native)
    except Exception as exc:
        # 大图显存不足时自动改为分块处理重试一次
        if "out of memory" in str(exc).lower():
            up = get_upsampler(model_key, tile_override=256)
            output, _ = up.enhance(work, outscale=native)
        else:
            raise
    if face_enhance:
        enh = get_face_enhancer()
        face_work = int(load().get("face_work_side", 2048) or 2048)
        output = _enhance_faces_fast(output, enh, face_work)
    if max_side > 0:
        output = _limit_output(output, min(max_side, src_long * outscale))
    return output


def process_image_file(input_path: Path, model_key: str, outscale: int | None = None,
                       face_enhance: bool = False, max_side: int | None = None) -> Path:
    """Enhance one image file and return the output path."""
    cfg = load()
    tag = MODELS[model_key]["short"]
    opts = {
        "model": model_key,
        "scale": outscale or MODELS[model_key]["scale"],
        "face": bool(face_enhance),
        "max": int(max_side or 0) or int(cfg.get("max_output_size", 4096) or 0),
    }
    if cfg.get("skip_existing", True):
        prev = done_info(input_path, opts)
        if prev:
            return prev
    out_path = make_output_path(input_path, tag=tag)
    img = load_image_bgr(input_path)
    out = enhance_image(img, model_key, outscale, face_enhance, max_side=max_side)
    save_image_bgr(out_path, out)
    mark_done(input_path, out_path, opts)
    return out_path