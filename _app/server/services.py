"""TK去水印高清程序 v2.0 - 合并版入口

板块一：去水印（TikTok / 抖音 无水印下载，抖音走本地 a_bogus 解析）
板块二：高清修复（图片修复 / 视频修复 / 混合批量）
板块三：模型中心（模型库 / 组合预设 / 性能参考）
板块四：更新日志
板块五：素材库（本地下载素材浏览）
板块六：工具箱（网络诊断 / 视频转GIF / 视频抽帧 / 批量重命名）
板块七：设置（去水印设置 / 高清设置 / 系统 / 关于）

运行: python app.py（推荐使用高清 venv: venv\Scripts\python.exe app.py）
浏览器打开 http://127.0.0.1:7860
"""
import logging
import os
import re
import shutil
import sys
import threading
import time
import hashlib
import json
import concurrent.futures
from logging.handlers import RotatingFileHandler
from pathlib import Path

from tqdm import tqdm

# pythonw（无控制台）模式下 sys.stdout/stderr 为 None，
# uvicorn 配置日志时调用 isatty() 会崩溃，这里兜底重定向
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout

# ---------- 高清板块 ----------
from watermark.core.config import PROJECT_ROOT, load, save
from watermark.core.output import day_dir
from watermark.core.utils import (ENCODE_TIMEOUT, IMAGE_EXTS, VIDEO_EXTS, env_info,
                                  find_ffmpeg, fmt_timeout)
from watermark.core.engines.models import (
    MODELS, download_model, download_all_missing, model_status,
    model_cards, selectable_keys,
)
from watermark.core.engines.image import process_image_file, release_models
from watermark.core.engines.video import process_video, process_video_sharp, CancelledError
from watermark.core import presets
from watermark.core.presets import PRESETS, PRESET_MAP
from watermark.core.version import VERSION, APP_NAME, APP_TITLE, BUILD_DATE
from watermark.core import changelog as changelog_mod

# ---------- 去水印板块 ----------
from watermark.tiktok.parser import parse_input, detect_platform
from watermark.tiktok.downloader import TikTokDownloader
from watermark.tiktok.models import DownloadStatus
from watermark.tiktok.history import HistoryManager
from watermark.tiktok.config import config as tk_config, APP_VERSION as TK_APP_VERSION
from watermark.tiktok.logger import log as tk_log

log = logging.getLogger("tk.services")

# v4.0 第 1 期：界面无关的契约层。
# 业务代码只通过 ui.bus 与界面交互，不再直接依赖 gradio；
# 第 2 期把渲染层换成 FastAPI + HTML/JS 时，本文件无需改动。
from ui import bus
from server.state import EventKind, JobKind, JobState

# 线程异常也要记下来：sys.excepthook 只管主线程，
# 守护线程里逃出来的异常原来会静默进 devnull。
def _thread_excepthook(args):
    try:
        logging.critical("子线程未捕获异常",
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    except Exception:
        pass


threading.excepthook = _thread_excepthook

CANCEL = threading.Event()
_DEMO = None

# 去水印运行时状态
_tk_downloader = None
_tk_downloader_lock = threading.Lock()
_parsed_infos = []
_parsed_options = []
_parsed_options_map = {}
_parse_lock = threading.Lock()

# 下载/高清任务的暂停、取消、并发占用状态
_tk_pause = threading.Event()
_tk_pause.set()          # set=运行, clear=暂停
_tk_cancel = threading.Event()
_tk_worker_running = False
_tk_worker_lock = threading.Lock()
_hd_pause = threading.Event()
_hd_pause.set()
_hd_worker_running = False
_hd_worker_lock = threading.Lock()

# 最近下载的素材（供一键送入高清）
_tk_recent_files: list = []

IMAGE_MODEL_KEYS = [k for k in MODELS if k != "GFPGANv1.4"]

# 可选模型按「推荐顺序」排列：轻量快速在前，重模型在后
_MODEL_ORDER = ["realesr-general-x4v3", "realesr-animevideov3", "RealESRGAN_x2plus",
                "RealESRGAN_x4plus", "4x-UltraSharp", "RealESRGAN_x4plus_anime_6B"]
MODEL_CHOICES = [(MODELS[k]["name"], k) for k in _MODEL_ORDER if k in MODELS]
SCALE_CHOICES = ["自动（推荐）", "2x", "4x"]
PRESET_CHOICES = presets.labels()
VIDEO_SIDE_CHOICES = [("⚡ 仅锐化 1080p（最快，无 AI）", "sharp"),
                      ("极速 1080p（AI 最快）", 1920),
                      ("平衡 2K（推荐）", 2560),
                      ("画质 4K", 3840),
                      ("不限制", 0)]
QUALITY_CHOICES = ["best", "worst", "720p", "1080p"]


def selectable_model_options() -> list[dict]:
    """给前端用的模型下拉项：{value: key, label: 中文名}（v4.0 修复）。

    原来 API 直接把 MODEL_CHOICES 的元组丢给前端，前端把整个元组当成了
    option 的 value，于是提交上来的 model_key 变成了
    "Real-ESRGAN 通用轻量 x4（快速）,realesr-general-x4v3" 这种字符串，
    后端 `MODELS[key]` 直接 KeyError —— 高清处理全部失败。
    """
    out = []
    for k in _MODEL_ORDER:
        if k not in MODELS:
            continue
        info = MODELS[k]
        if info.get("stage") == "planned":
            continue
        out.append({"value": k, "label": info.get("name", k)})
    return out


def selectable_scale_options() -> list[dict]:
    return [{"value": s, "label": s} for s in SCALE_CHOICES]


def selectable_side_options() -> list[dict]:
    return [{"value": str(v), "label": label} for label, v in VIDEO_SIDE_CHOICES]


def resolve_model_key(value, kind: str = "image") -> str:
    """把「可能是名称/『名称,key』/key」的输入统一解析成合法模型 key。

    这是一个防御性修复：前端/手机端/旧缓存都可能传来不规范的值，
    与其在 `MODELS[key]` 处抛一个看不懂的 KeyError，不如在这里纠正，
    实在无法识别时才回退到默认模型并记录原因。
    """
    if value is None:
        value = ""
    raw = str(value).strip()
    # 1) 直接就是合法 key
    if raw in MODELS and MODELS[raw].get("stage") != "planned":
        return raw
    # 2) 形如 "显示名,key"（旧前端的 bug 产物）
    if "," in raw:
        tail = raw.rsplit(",", 1)[-1].strip()
        if tail in MODELS and MODELS[tail].get("stage") != "planned":
            return tail
        raw = tail
    # 3) 按显示名匹配
    for k, info in MODELS.items():
        if info.get("stage") == "planned":
            continue
        if raw == str(info.get("name", "")).strip():
            return k
    # 4) 兜底：不含中文的短标识里做包含匹配（容忍前后缀差异）
    low = raw.lower()
    for k in _MODEL_ORDER:
        if k in MODELS and (k.lower() in low or low in k.lower()):
            return k
    # 5) 全都不像 -> 用该类型的默认模型
    fallback = "realesr-general-x4v3"
    logging.warning(f"无法识别的模型值 {value!r}（{kind}），回退为 {fallback}")
    return fallback


def setup_logging() -> None:
    handler = RotatingFileHandler(
        PROJECT_ROOT / "app.log", maxBytes=2 * 1024 * 1024, backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _excepthook(tp, val, tb):
    logging.critical("未捕获异常", exc_info=(tp, val, tb))


sys.excepthook = _excepthook


# ---------- 高清通用工具 ----------

def _to_paths(value):
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    out = []
    for x in value:
        if isinstance(x, (str, Path)):
            out.append(str(x))
        elif hasattr(x, "path"):
            out.append(x.path)
    return out


def _collect_inputs(files, folder):
    paths = _to_paths(files)
    if folder and str(folder).strip():
        root = Path(str(folder).strip())
        if root.is_dir():
            paths += [str(p) for p in sorted(root.rglob("*")) if p.is_file()]
    seen, result = set(), []
    for s in paths:
        rp = str(Path(s).resolve())
        if rp not in seen:
            seen.add(rp)
            result.append(rp)
    return result


def _add_allowed_path(root: str) -> None:
    """Gradio 只允许返回位于 allowed_paths 内/当前目录/临时目录的文件。"""
    global _DEMO
    if _DEMO is None or not root:
        return
    ap = getattr(_DEMO, "allowed_paths", None)
    if isinstance(ap, list) and root not in ap:
        ap.append(root)


# 封面本地缓存目录（避免 UI 依赖远程 CDN 可达性）
_COVER_DIR = PROJECT_ROOT / ".cover_cache"


def _download_cover(url: str, timeout: float = 8.0):
    """下载封面到本地缓存，返回本地路径；失败返回 None。"""
    try:
        if not url or not str(url).startswith("http"):
            return None
        _COVER_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(url.encode()).hexdigest()[:12]
        path = _COVER_DIR / f"{key}.img"
        if path.exists() and path.stat().st_size > 1024:
            return str(path)
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 1024:
            ct = r.headers.get("Content-Type", "").lower()
            if "webp" in ct:
                path = path.with_suffix(".webp")
            elif "png" in ct:
                path = path.with_suffix(".png")
            elif "jpeg" in ct or "jpg" in ct:
                path = path.with_suffix(".jpg")
            else:
                path = path.with_suffix(".jpg")
            path.write_bytes(r.content)
            _add_allowed_path(str(_COVER_DIR))
            return str(path)
    except Exception:
        pass
    return None


def _cache_covers_parallel(covers) -> list:
    """把封面 URL 并行下载到本地缓存；下载失败则保留远程 URL。"""
    urls = [c for c in covers if c and str(c).startswith("http")]
    if not urls:
        return [c for c in covers if c]
    local = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(urls))) as pool:
        fut = {pool.submit(_download_cover, u): u for u in urls}
        for f in concurrent.futures.as_completed(fut):
            u = fut[f]
            try:
                p = f.result()
                if p:
                    local[u] = p
            except Exception:
                pass
    out = []
    for c in covers:
        if c and c in local:
            out.append(local[c])
        elif c and str(c).startswith("http"):
            out.append(c)  # 下载失败退回远程 URL
        else:
            out.append(None)
    return [c for c in out if c]


def _apply_output_dir(out_root):
    if out_root and str(out_root).strip():
        cfg = load()
        root = str(Path(str(out_root).strip()).expanduser())
        if cfg.get("output_root") != root:
            cfg["output_root"] = root
            save(cfg)
        _add_allowed_path(root)


def _scale_value(s):
    """把「自动（推荐） / 2x / 4 / 『4』」等写法统一解析成倍数；无法识别时返回 None。

    v4.0 修复：以前这里直接 int()，前端若传来空字符串（例如下拉框还没填充完
    就被点击，或第三方调用方漏传），会抛
    `ValueError: invalid literal for int() with base 10: ''`。
    这个异常发生在 `_remember_choices` 里 —— 也就是所有文件都处理完之后 ——
    结果整个任务被标记为失败，辛苦跑完的产物全部丢失。
    """
    if s is None:
        return None
    raw = str(s).strip()
    if not raw or "自动" in raw or "模型" in raw:
        return None
    try:
        return int(raw.replace("x", "").replace("X", ""))
    except (TypeError, ValueError):
        return None


def _img_max_value(s):
    """从“4096（推荐）”等下拉标签中解析数值。"""
    s = str(s or "")
    for x in ("2048", "4096", "8192"):
        if x in s:
            return int(x)
    return 4096


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}小时{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _fmt_size(n: float) -> str:
    try:
        n = float(n or 0)
    except Exception:
        return "-"
    if n >= 2 ** 30:
        return f"{n / 2 ** 30:.2f} GB"
    if n >= 2 ** 20:
        return f"{n / 2 ** 20:.1f} MB"
    if n >= 2 ** 10:
        return f"{n / 2 ** 10:.0f} KB"
    return f"{n:.0f} B"


def _summary(ok, errs, error_list, t_start=None):
    lines = [f"完成 {ok} 项，失败/取消 {errs} 项", f"输出目录：{day_dir()}"]
    if t_start:
        lines.append(f"总耗时：{_fmt_duration(time.time() - t_start)}")
    for e in error_list[:15]:
        lines.append(f"- {e}")
    if len(error_list) > 15:
        lines.append(f"... 还有 {len(error_list) - 15} 条错误未显示")
    return "\n".join(lines)


def _remember_choices(kind, model, scale, face=None, interpolate=None, max_side=None):
    """记住本次选择，下次打开界面默认使用。

    这是「锦上添花」的偏好保存，绝不能让它影响任务结果：
    v4.0 修复为整体 try/except —— 以前任何一个字段解析失败都会让
    已经处理完成的任务被判为失败（产物丢失）。
    """
    try:
        cfg = load()
        prefix = "image" if kind == "image" else "video"
        cfg[f"{prefix}_model"] = model
        sv = _scale_value(scale)
        cfg[f"{prefix}_scale"] = sv if sv else MODELS[model]["scale"]
        if kind == "image" and face is not None:
            cfg["face_enhance"] = bool(face)
        if kind == "video" and interpolate is not None:
            cfg["interpolate"] = bool(interpolate)
        if kind == "video" and max_side is not None:
            if str(max_side) == "sharp":
                cfg["video_sharp"] = True
            else:
                cfg["video_sharp"] = False
                try:
                    cfg["video_max_side"] = int(max_side or 0)
                except (TypeError, ValueError):
                    cfg["video_max_side"] = 0
        save(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("保存上次使用的参数失败（不影响任务结果）：%s", exc)




def pick_folder():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="选择素材文件夹")
    root.destroy()
    return folder or ""


def open_output():
    os.startfile(str(day_dir()))
    return "已打开输出目录"




# ---------- 剪贴板 ----------

def read_clipboard() -> str:
    """读取系统剪贴板文本（tkinter 方式）。"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            text = root.clipboard_get()
        except Exception:
            text = ""
        root.destroy()
        return text or ""
    except Exception:
        return ""






# ---------- 去水印工具 ----------

def _get_tk_downloader() -> TikTokDownloader:
    global _tk_downloader
    with _tk_downloader_lock:
        if _tk_downloader is None:
            tk_log.info(
                f"初始化下载器: output={tk_config.output_dir} "
                f"并发={tk_config.max_concurrent}"
            )
            _tk_downloader = TikTokDownloader(
                output_dir=tk_config.output_dir,
                max_concurrent=tk_config.max_concurrent,
                proxy_url=tk_config.proxy_url,
            )
        return _tk_downloader


def _reset_tk_downloader():
    global _tk_downloader
    with _tk_downloader_lock:
        _tk_downloader = None


def _apply_tk_output_dir(value):
    if value and str(value).strip():
        root = str(Path(str(value).strip()).expanduser())
        if tk_config.output_dir != root:
            tk_config.output_dir = root
            tk_config.save()
            _add_allowed_path(root)
        _reset_tk_downloader()


def _parse_selection(text, total):
    """解析 '1,3-5' / 'all' / 空 -> 序号列表"""
    text = (text or "").strip()
    if not text or text.lower() in ("all", "全部"):
        return list(range(1, total + 1))
    result = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo = int(a) if a.strip() else 1
                hi = int(b) if b.strip() else total
            except ValueError:
                continue
            lo, hi = max(1, lo), min(total, hi)
            if hi >= lo:
                result.extend(range(lo, hi + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                result.append(n)
    seen, out = set(), []
    for n in result:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------- 去水印处理器 ----------





def _tk_eta_text(status):
    total = int(status.get("total", 0) or 0)
    done = int(status.get("ok", 0) or 0) + int(status.get("fail", 0) or 0)
    elapsed = float(status.get("elapsed", 0.0) or 0.0)
    if not total or done <= 0 or done >= total or elapsed <= 0:
        return ""
    eta = elapsed / done * (total - done)
    return f"预计剩余 {_fmt_duration(eta)}"


def _tk_download_worker(selected, sel_idx, out_dir, job, auto_hd):
    """去水印下载工作线程。

    v4.0 第 1 期：第 4 个参数由「裸 status 字典」改为 ui.bus 的 Job 对象。
    """
    global _tk_recent_files
    job.set_state(JobState.RUNNING)
    try:
        _apply_tk_output_dir(out_dir)
        with _parse_lock:
            infos = list(_parsed_infos)
            opt_map = dict(_parsed_options_map)
        if not infos or not any(infos):
            job["lines"] = ["请先粘贴链接并点击“解析链接”"]
            job["final"] = "请先粘贴链接并点击“解析链接”"
            job.finish(job["final"])
            return
        plan = {}
        if selected or str(sel_idx or "").strip():
            # v4.0 修复：兼容两种「选中」表达方式 ——
            #   a) 下拉标签字符串（旧 Gradio 界面用）
            #   b) 纯序号整数/字符串（新前端勾选列表用）
            # 之前只认 (a)，新前端传 [1,2,3] 时 opt_map.get("1") 全落空，
            # plan 为空 -> 「所选项目没有可下载的任务」-> 看起来就是「下载不了」。
            idx_set = set()
            for item in (selected or []):
                key = opt_map.get(str(item))
                if key:
                    link, img = key
                    if not (1 <= link <= len(infos)):
                        continue
                    if img is None:
                        plan[link] = None          # 整条下载
                    else:
                        cur = plan.get(link)
                        if link not in plan:
                            plan[link] = {img}
                        elif isinstance(cur, set):
                            cur.add(img)
                    continue
                # 退化为序号
                m = re.search(r"\d+", str(item))
                if m:
                    idx_set.add(int(m.group(0)))
            # 序号范围（手填 "1,3-5"）
            idx_set.update(_parse_selection(sel_idx, len(infos)))
            for i in sorted(idx_set):
                if 1 <= i <= len(infos) and infos[i - 1] is not None:
                    # 已由标签指定了具体图片时不覆盖
                    plan.setdefault(i, None)
        targets = [
            (i, infos[i - 1],
             (sorted(plan[i]) if isinstance(plan[i], set) else None))
            for i in sorted(plan) if infos[i - 1] is not None
        ]
        if not targets:
            job["lines"] = ["所选项目没有可下载的任务"]
            job["final"] = "所选项目没有可下载的任务"
            job.finish(job["final"])
            return
        downloader = _get_tk_downloader()
        history = HistoryManager()
        job["total"] = len(targets)
        ok = fail = 0
        lines = []
        t_start = time.time()
        downloaded_files: list = []
        for idx, info, img_sel in targets:
            # 暂停时阻塞在这里，直到“继续”或“取消”（Job 内部处理两者关系）
            if not job.wait_if_paused():
                job["final"] = (f"已取消：完成 {ok} 个，失败 {fail} 个\n"
                                + "\n".join(lines))
                job.cancel_finish(job["final"])
                return
            job["current"] = f"#{idx} {(info.title or '')[:30]}"
            job["pct"] = 0
            job.stage("download", "下载中", total=len(targets))
            job.emit(EventKind.ITEM, message=f"开始下载 #{idx}",
                     frac=(ok + fail) / max(1, len(targets)),
                     data={"index": idx, "title": info.title or ""})

            def _cb(task, _idx=idx, _t=info.title or ""):
                job["current"] = f"#{_idx} {_t[:30]}"
                job["pct"] = task.progress
                job.emit(EventKind.PROGRESS,
                         frac=(ok + fail + float(task.progress or 0)) / max(1, len(targets)),
                         stage="download",
                         stage_frac=float(task.progress or 0) / 100.0
                         if float(task.progress or 0) > 1 else float(task.progress or 0),
                         message=f"#{_idx} {_t[:30]}",
                         data={"index": _idx, "title": _t})

            task = downloader.download(info, idx=idx, on_progress=_cb,
                                       image_sel=img_sel)
            if task.status == DownloadStatus.COMPLETED:
                ok += 1
                try:
                    history.add(task)
                except Exception as e:
                    tk_log.warning(f"写入历史失败: {e}")
                lines.append(f"✓ #{idx} {task.file_path}")
                downloaded_files.append(task.file_path)
                job.log(f"✓ #{idx} {task.file_path}", level="ok")
            else:
                fail += 1
                lines.append(f"✗ #{idx} {task.error or '下载失败'}")
                job.log(f"✗ #{idx} {task.error or '下载失败'}", level="error")
            job["ok"], job["fail"] = ok, fail
            job["lines"] = list(lines)
            job["current"] = ""
            job["elapsed"] = time.time() - t_start
            job.emit(EventKind.ITEM, message=f"完成 #{idx}",
                     frac=(ok + fail) / max(1, len(targets)),
                     level="ok" if task.status == DownloadStatus.COMPLETED else "error",
                     data={"index": idx, "ok": ok, "fail": fail,
                           "done": ok + fail, "total": len(targets)})

        # 收集本次任务的所有文件（图集 task.file_path 只是第一张，
        # 按本次任务的 video_id 匹配补齐其余图片，避免混入之前下载的文件）
        try:
            task_ids = {infos[i - 1].video_id for i in sorted(plan)
                        if infos[i - 1] is not None and infos[i - 1].video_id}
            today_dir = Path(tk_config.output_dir) / time.strftime("%Y-%m-%d")
            if today_dir.is_dir() and task_ids:
                for p in sorted(today_dir.rglob("*")):
                    if p.is_file() and p.suffix.lower() in \
                            IMAGE_EXTS | {".mp4", ".mov", ".webm", ".m4v"}:
                        if any(vid and vid in p.name for vid in task_ids):
                            if str(p) not in downloaded_files:
                                downloaded_files.append(str(p))
        except Exception:
            pass
        if auto_hd and downloaded_files:
            _tk_recent_files = [p for p in downloaded_files
                                if Path(p).suffix.lower() in IMAGE_EXTS]
            job["auto_hd"] = len(_tk_recent_files)
        else:
            _tk_recent_files = list(downloaded_files)

        # v2.1.1: 记录下载完成的文件 (供手机浏览器保存到手机)
        job["files"] = list(downloaded_files)

        # v2.1.2: 把本次下载的文件打包成 ZIP (手机端一键下载到手机)
        if downloaded_files:
            try:
                import zipfile as _zf
                ts = time.strftime("%Y%m%d_%H%M%S")
                zip_dir = Path(tk_config.output_dir)
                zip_dir.mkdir(parents=True, exist_ok=True)
                zip_path = zip_dir / f"TK下载_{ts}.zip"
                with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED,
                                 compresslevel=6) as z:
                    seen = set()
                    for f in sorted(downloaded_files):
                        p = Path(f)
                        if not (p.is_file() and p.suffix.lower() in
                                IMAGE_EXTS | {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi"}):
                            continue
                        # v2.1.3: ZIP 内按 图片/视频 分目录，方便手机端分类导入相册
                        if p.suffix.lower() in IMAGE_EXTS:
                            arc = "图片/" + p.name
                        else:
                            arc = "视频/" + p.name
                        if arc in seen:
                            arc = (f"{'图片' if p.suffix.lower() in IMAGE_EXTS else '视频'}"
                                   f"/{p.stem}_{p.stat().st_size}{p.suffix.lower()}")
                        seen.add(arc)
                        z.write(p, arcname=arc)
                if zip_path.exists() and zip_path.stat().st_size > 0:
                    job["zip_path"] = str(zip_path)
                    _add_allowed_path(str(zip_dir))
                    lines.append(f"📦 已打包: {zip_path.name}（手机端下载 ZIP 后解压，"
                                 f"图片进「图片」文件夹）")
                    job["final"] = (f"完成 {ok} 个，失败 {fail} 个\n"
                                    + "\n".join(lines))
                    job.log(f"已打包 ZIP: {zip_path.name}", level="ok")
            except Exception as e:
                tk_log.warning(f"打包 ZIP 失败: {e}")

        job["final"] = job.get("final") or f"完成 {ok} 个，失败 {fail} 个\n" + "\n".join(lines)
        if ok > 0 and fail == 0:
            job["notify"] = ("ok", f"下载完成：成功 {ok} 个")
        elif fail > 0:
            job["notify"] = ("warn", f"下载完成：成功 {ok} 个，失败 {fail} 个")
        # v4.0 第 3 期：把最终产物作为一条事件推给前端（画廊 / 打包下载）
        try:
            from server.media import outputs_payload, file_url
            payload = outputs_payload(downloaded_files)
            job.emit(EventKind.STATE, data={
                "outputs": payload,
                "gallery": [x for x in payload if x["is_image"]],
                "videos": [x for x in payload if x["is_video"]],
                "zip_path": job.get("zip_path") or "",
                "zip_url": (file_url(job["zip_path"])
                            if job.get("zip_path") else ""),
                "ok_count": ok,
                "fail_count": fail,
            })
        except Exception as exc:  # 结果推送失败不应影响任务结果
            tk_log.warning(f"推送下载结果失败: {exc}")
        if job.cancelled:
            job.cancel_finish(job["final"])
        else:
            job.finish(job["final"])
    except Exception as e:
        tk_log.exception("下载线程异常")
        job["final"] = f"下载失败：{e}"
        job.fail(str(e))




def _runtime_port() -> int:
    """读取当前实际监听端口（v4.0）。

    端口是动态分配的，写在这里的 runtime/server.json 里。
    原来诊断信息里写死 7860，一旦换端口就会误导用户。
    """
    try:
        f = PROJECT_ROOT / "runtime" / "server.json"
        if f.exists():
            return int(json.loads(f.read_text(encoding="utf-8")).get("port") or 0)
    except Exception:
        pass
    return 0


def video_info_dict(info) -> dict:
    """把 VideoInfo 转成前端友好的普通字典（v4.0）。

    原 do_parse 直接把行/选项喂给 Gradio 控件；新前端需要结构化数据
    （封面、标题、作者、时长、能否下载），所以单独提供这个转换。
    """
    if info is None:
        return {}
    try:
        ctype = info.content_type.value
    except Exception:
        ctype = "video"
    plat = "TikTok" if info.extra.get("platform") == "tiktok" else "抖音"
    n_imgs = len(info.image_urls or []) or 1
    kind = "video" if ctype == "video" else "images"
    cover = info.thumbnail or ((info.image_urls or [None])[0])
    return {
        "video_id": info.video_id,
        "platform": plat,
        "platform_key": info.extra.get("platform", ""),
        "kind": kind,
        "title": info.title or "(无标题)",
        "author": info.author or "",
        "quality": info.extra.get("quality", "") or "-",
        "duration": int(info.duration or 0),
        "image_count": n_imgs,
        "cover": cover if isinstance(cover, str) and cover.startswith("http") else "",
        "url": info.url or "",
        "downloadable": True,
    }








def do_load_history():
    rows = []
    try:
        h = HistoryManager()
        for r in h.get_all()[:100]:
            size = _fmt_size(r.get("file_size", 0))
            rows.append([
                r.get("downloaded_at", "")[:19],
                r.get("platform", ""),
                r.get("content_type", ""),
                (r.get("title") or "")[:36],
                r.get("author", ""),
                size,
                r.get("file_path", ""),
            ])
    except Exception as e:
        tk_log.warning(f"读取历史失败: {e}")
    return rows


def do_clear_history():
    try:
        HistoryManager().clear()
    except Exception as e:
        return f"清空失败: {e}"
    return "历史记录已清空 ✓"


def do_open_tk_dir():
    root = Path(tk_config.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(root))
    except Exception:
        pass
    return f"已打开：{root}"


def tk_settings_values():
    return (tk_config.output_dir, tk_config.max_concurrent, tk_config.max_retries,
            tk_config.video_quality, tk_config.proxy_url)


def tk_settings_save(out_root, concurrent, retries, quality, proxy):
    _apply_tk_output_dir(out_root)
    tk_config.max_concurrent = max(1, int(concurrent or 3))
    tk_config.max_retries = max(0, int(retries or 3))
    tk_config.video_quality = quality
    tk_config.proxy_url = (proxy or "").strip()
    tk_config.save()
    _reset_tk_downloader()
    return "去水印设置已保存 ✓（下次解析/下载生效）"


# ---------- 高清处理器 ----------

def _hd_stage_text(status):
    stage = status.get("stage", "")
    done = int(status.get("stage_done", 0) or 0)
    total = int(status.get("stage_total", 0) or 0)
    if not total:
        return ""
    name = "图片" if stage == "image" else ("视频" if stage == "video" else "")
    return f"{name}阶段 {done}/{total}"


def _hd_eta_text(status):
    done = int(status.get("stage_done", 0) or 0)
    total = int(status.get("stage_total", 0) or 0)
    elapsed = float(status.get("stage_elapsed", 0.0) or 0.0)
    if not total or done <= 0 or done >= total or elapsed <= 0:
        return ""
    eta = elapsed / done * (total - done)
    return f"预计剩余 {_fmt_duration(eta)}"






def _hd_worker(files, folder, image_model, video_model, img_scale, vid_scale,
               vid_side, face, interpolate, out_root, img_max, job,
               mode="both"):
    """mode: "image" 只处理图片 / "video" 只处理视频 / "both" 自动分流。

    v4.0 第 1 期：第 12 个参数由「裸 status 字典」改为 ui.bus 的 Job 对象，
    进度以结构化事件产出（阶段/阶段进度/总进度/日志/提示）。
    """
    job.set_state(JobState.RUNNING)
    try:
        _apply_output_dir(out_root)
        paths = _collect_inputs(files, folder)
        if mode == "image":
            paths = [p for p in paths if Path(p).suffix.lower() in IMAGE_EXTS]
        elif mode == "video":
            paths = [p for p in paths if Path(p).suffix.lower() in VIDEO_EXTS]
        images = [p for p in paths if Path(p).suffix.lower() in IMAGE_EXTS]
        videos = [p for p in paths if Path(p).suffix.lower() in VIDEO_EXTS]
        job["img_total"] = len(images)
        job["vid_total"] = len(videos)
        if not images and not videos:
            msg = ("没有找到可处理的文件（支持 jpg/png/webp/heic 图片"
                   "和 mp4/mov/mkv 等视频）")
            job["summary"] = msg
            job.finish(msg)
            return
        img_results, vid_results, errors = [], [], []
        t_start = time.time()

        # ---------------- 图片阶段 ----------------
        job.stage("image", "图片超分", total=len(images))
        job.progress(0.0, f"图片阶段开始（{len(images)} 个）")
        job["errors"] = []
        t_stage = time.time()
        for idx, p in enumerate(images, 1):
            if not job.wait_if_paused():
                errors.append("用户取消（图片阶段）")
                break
            name = Path(p).name
            job["current"] = f"图片 {idx}/{len(images)} [{name}]"
            job.progress(0.02 + 0.4 * (idx - 1) / max(1, len(images)),
                         f"图片 {idx}/{len(images)} [{name}]")
            try:
                out = process_image_file(Path(p), image_model,
                                         _scale_value(img_scale), bool(face),
                                         max_side=_img_max_value(img_max))
                img_results.append(str(out))
                job["gallery"] = list(img_results)
                job.log(f"✓ {name}")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                job.log(f"✗ {name}: {exc}", level="error")
            job["img_done"] = idx
            job["stage_done"] = idx
            job["stage_elapsed"] = time.time() - t_stage
            job.progress(0.02 + 0.4 * idx / max(1, len(images)),
                         f"图片 {idx}/{len(images)} 完成")

        # ---------------- 视频阶段 ----------------
        job.stage("video", "视频超分", total=len(videos))
        job["stage_done"] = 0
        job["errors"] = list(errors)
        t_stage = time.time()
        for idx, p in enumerate(videos, 1):
            if not job.wait_if_paused():
                errors.append("用户取消（视频阶段）")
                break
            name = Path(p).name

            def _cb(frac, msg, _idx=idx, _name=name):
                job["current"] = f"视频 {_idx}/{len(videos)} [{_name}] {msg}"
                job["frac"] = frac or 0
                job.emit(EventKind.PROGRESS, frac=frac or 0,
                         stage="video",
                         message=f"视频 {_idx}/{len(videos)} [{_name}] {msg}",
                         data={"video_index": _idx, "video_name": _name})

            try:
                if str(vid_side) == "sharp":
                    out = process_video_sharp(
                        Path(p), 1920, bool(interpolate),
                        progress_cb=_cb, cancel=job.cancel,
                    )
                else:
                    out = process_video(
                        Path(p), video_model, _scale_value(vid_scale),
                        bool(interpolate), progress_cb=_cb, cancel=job.cancel,
                        max_side=int(vid_side or 0),
                    )
                vid_results.append(str(out))
                job["vids"] = list(vid_results)
                job.log(f"✓ {name} → {out}")
            except CancelledError:
                errors.append("用户取消（视频阶段）")
                break
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                job.log(f"✗ {name}: {exc}", level="error")
            job["vid_done"] = idx
            job["stage_done"] = idx
            job["stage_elapsed"] = time.time() - t_stage

        _remember_choices("image", image_model, img_scale, face=face)
        _remember_choices("video", video_model, vid_scale,
                          interpolate=interpolate, max_side=vid_side)
        ok = len(img_results) + len(vid_results)
        job["summary"] = _summary(ok, len(errors), errors, t_start)
        job["errors"] = list(errors)
        # 元组的第二项是要给用户看的文字（jobs.py 会把它放进 NOTIFY 事件）。
        if ok == 0 and errors:
            job["notify"] = ("error", "高清修复全部失败，请查看「处理信息」")
        elif errors:
            job["notify"] = ("warn", f"高清修复完成：成功 {ok} 项，失败 {len(errors)} 项")
        else:
            job["notify"] = ("ok", f"高清修复完成：成功 {ok} 项")
        # v4.0 第 3 期：把图片/视频产物推给前端画廊
        try:
            from server.media import outputs_payload
            payload = outputs_payload(list(img_results) + list(vid_results))
            job.emit(EventKind.STATE, data={
                "outputs": payload,
                "gallery": [x for x in payload if x["is_image"]],
                "videos": [x for x in payload if x["is_video"]],
                "ok_count": ok,
                "error_count": len(errors),
            })
        except Exception as exc:
            logging.warning(f"推送高清结果失败: {exc}")
        if job.cancelled:
            job.cancel_finish(job["summary"])
        elif ok == 0 and errors:
            job.fail(errors[0])
        else:
            job.finish(job["summary"])
    except Exception as e:
        logging.exception("批量高清处理线程异常")
        job["summary"] = f"处理失败：{e}"
        job.fail(str(e))
    finally:
        # v4.0：任务结束后卸载模型并回收显存。
        # 原来模型只进不出，连续换模型/二次运行会把显存吃满（12GB 卡尤其明显）。
        try:
            n = release_models()
            if n:
                logging.info(f"已释放 {n} 个模型并回收显存")
        except Exception:
            logging.debug("释放显存失败", exc_info=True)


def tk_worker_begin() -> bool:
    """尝试占用「去水印下载」位置：返回 False 表示已有任务在跑。

    和高清那边（hd_worker_begin）是同一类 v4.0 重构遗留：占用标志
    `_tk_worker_running` 原来只由 Gradio 时代的 do_download() 生成器设置，
    界面改走 /api/tiktok/download 之后没人再写它，于是
    `/api/tiktok/download` 里「已有下载任务在运行」的判断永远为假 ——
    连点两次「下载」会真的起两个下载线程，往同一个输出目录写同名文件。
    """
    global _tk_worker_running
    with _tk_worker_lock:
        if _tk_worker_running:
            return False
        _tk_worker_running = True
        return True


def tk_worker_end() -> None:
    """释放「去水印下载」位置（工作线程结束/启动失败都要调用）。"""
    global _tk_worker_running
    with _tk_worker_lock:
        _tk_worker_running = False


def tk_worker_busy() -> bool:
    with _tk_worker_lock:
        return _tk_worker_running


def hd_worker_begin() -> bool:
    """尝试占用「高清处理」位置：返回 False 表示已有任务在跑。

    背景（v4.0 重构遗留）：占用标志原来只由 Gradio 时代的 handle_mixed()
    生成器设置，而界面改走 /api/hd/start 后没人再写这个标志，导致
    「已有高清任务在运行」的判断永远为假——连点两次「开始处理」会真的起两个
    工作线程，抢同一块显存、往同一个输出目录写文件。现在由启动接口通过
    这两个函数原子地占用/释放。
    """
    global _hd_worker_running
    with _hd_worker_lock:
        if _hd_worker_running:
            return False
        _hd_worker_running = True
        return True


def hd_worker_end() -> None:
    """释放「高清处理」位置（工作线程结束/启动失败都要调用）。"""
    global _hd_worker_running
    with _hd_worker_lock:
        _hd_worker_running = False


def hd_worker_busy() -> bool:
    with _hd_worker_lock:
        return _hd_worker_running






def recent_files_info():
    """显示最近下载的素材数量，并返回文件列表（供导入高清）。"""
    files = list(_tk_recent_files)
    files = [p for p in files if Path(p).exists()]
    if not files:
        return [], "暂无最近下载的素材。先去「🎬 去水印」下载一些素材吧。"
    types = {}
    for p in files:
        ext = Path(p).suffix.lower()
        types[ext] = types.get(ext, 0) + 1
    desc = "，".join(f"{k[1:]}:{v}" for k, v in types.items())
    return files, f"最近下载 {len(files)} 个文件（{desc}），已导入下方文件列表。"


# ---------- 素材库 ----------

LIB_EXTS = IMAGE_EXTS | {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi"}


def library_roots():
    roots = [Path(tk_config.output_dir)]
    hd_root = Path(load().get("output_root", "") or ".")
    if hd_root not in roots:
        roots.append(hd_root)
    return roots


def library_list(limit: int = 500):
    """扫描下载/输出目录，返回最近文件列表。"""
    rows = []
    seen = set()
    try:
        for root in library_roots():
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*"), key=lambda x: x.stat().st_mtime
                            if x.is_file() else 0, reverse=True):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in LIB_EXTS:
                    continue
                rp = str(p)
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    size = _fmt_size(p.stat().st_size)
                except Exception:
                    size = "-"
                rows.append([
                    time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                    p.suffix.lstrip(".").upper(),
                    size,
                    str(p),
                ])
                if len(rows) >= limit:
                    return rows
    except Exception as e:
        tk_log.warning(f"素材库扫描失败: {e}")
    return rows


def library_gallery():
    """返回最近 30 张图片缩略图。"""
    imgs = []
    try:
        for root in library_roots():
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*"), key=lambda x: x.stat().st_mtime
                            if x.is_file() else 0, reverse=True):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in IMAGE_EXTS:
                    continue
                imgs.append(str(p))
                if len(imgs) >= 30:
                    return imgs
    except Exception:
        pass
    return imgs




# ---------- 工具箱 ----------

def diag_network():
    """网络诊断：检测 tikwm / 抖音 / 代理连通性。"""
    import requests as _req
    lines = [f"🔍 网络诊断 @ {time.strftime('%Y-%m-%d %H:%M:%S')}", "-" * 40]
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    # 1) 抖音短链
    try:
        t0 = time.time()
        r = _req.get("https://v.douyin.com/mevt1NTKQ3U/", allow_redirects=True,
                     timeout=8, headers={"User-Agent": ua})
        lines.append(f"✅ 抖音短链解析: HTTP {r.status_code} "
                     f"({(time.time()-t0)*1000:.0f}ms) -> {r.url[:60]}")
    except Exception as e:
        lines.append(f"❌ 抖音短链解析失败: {type(e).__name__}")

    # 2) 抖音 ttwid + detail API
    try:
        from watermark.tiktok.douyin import DouyinClient
        c = DouyinClient(timeout=8, max_retries=1)
        ttwid = c.get_ttwid()
        lines.append(("✅ 抖音 ttwid 注册成功" if ttwid else "⚠️ 抖音 ttwid 注册失败"))
        detail = c._fetch_detail("7546907495592168714")
        lines.append(("✅ 抖音 detail API (a_bogus) 可用" if detail
                      else "❌ 抖音 detail API 返回空（可能被风控）"))
    except Exception as e:
        lines.append(f"❌ 抖音解析引擎异常: {type(e).__name__}: {e}")

    # 3) tikwm API
    try:
        t0 = time.time()
        r = _req.post("https://www.tikwm.com/api/",
                      data={"url": "https://vt.tiktok.com/ZSVB6cVMD/"},
                      timeout=8, headers={"User-Agent": ua})
        j = r.json() if r.text else {}
        if j.get("code") == 0:
            lines.append(f"✅ tikwm API 可用: HTTP {r.status_code} "
                         f"({(time.time()-t0)*1000:.0f}ms)")
        else:
            lines.append(f"⚠️ tikwm API 返回: code={j.get('code')} msg={j.get('msg')}")
    except Exception as e:
        lines.append(f"❌ tikwm API 不可用: {type(e).__name__}: {e}")

    # 4) 代理探测
    try:
        from watermark.tiktok.tikwm import detect_best_proxy, get_detected_proxy
        proxy = get_detected_proxy() or detect_best_proxy()
        if proxy:
            lines.append(f"✅ 代理探测: {proxy}")
        else:
            lines.append("ℹ️ 未探测到本地代理（TikTok 可能需代理才能访问）")
    except Exception as e:
        lines.append(f"❌ 代理探测异常: {type(e).__name__}: {e}")

    # 5) 局域网 IP（手机访问用）
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        lan_on = bool(load().get("lan_access", False))
        # v4.0: 实际端口是动态的（不再固定 7860），从运行时文件读真实端口
        port = _runtime_port() or 7860
        if lan_on:
            lines.append(f"📱 手机访问地址: http://{ip}:{port}（手机连同一 WiFi 后打开）")
        else:
            lines.append(f"📱 本机局域网 IP: {ip}（需在设置开启“局域网访问”并重启）")
    except Exception:
        lines.append("📱 无法获取本机局域网 IP")

    lines.append("-" * 40)
    lines.append("提示：抖音解析走本地直连；TikTok 解析走 tikwm 公共 API + 代理。")
    return "\n".join(lines)


def _ffmpeg_path():
    ff = find_ffmpeg()
    if ff:
        return str(ff)
    return "ffmpeg"


def video_to_gif(video, fps: int, scale: int):
    """视频转 GIF（ffmpeg）。"""
    if not video:
        return "请先选择视频文件", None
    vpath = Path(video)
    if not vpath.exists():
        return "视频文件不存在", None
    out = vpath.with_name(vpath.stem + ".gif")
    i = 1
    while out.exists():
        out = vpath.with_name(f"{vpath.stem}_{i}.gif")
        i += 1
    ff = _ffmpeg_path()
    scale = max(64, min(1280, int(scale or 480)))
    cmd = [ff, "-y", "-i", str(vpath), "-vf",
           f"fps={max(1, int(fps or 12))},scale={scale}:-1:flags=lanczos",
           str(out)]
    import subprocess
    # v4.0: 显式 UTF-8 解码。原来 text=True 会按系统编码(中文 Windows=GBK)
    # 解码 ffmpeg 的 UTF-8 输出，中文路径会乱码甚至抛 UnicodeDecodeError。
    # v1.4.3: 补超时，卡住的 ffmpeg 不再让工具页一直转圈。
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=ENCODE_TIMEOUT,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return (f"转换失败：ffmpeg 超过 {fmt_timeout(ENCODE_TIMEOUT)} 没有结束，"
                "已强制中止（文件可能被占用或损坏）"), None
    if r.returncode != 0:
        return f"转换失败：{r.stderr[-300:]}", None
    return f"✅ 已生成：{out}（{_fmt_size(out.stat().st_size)}）", str(out)


def video_extract_frames(video, fps: int, out_dir: str):
    """视频抽帧（ffmpeg）。"""
    if not video:
        return "请先选择视频文件"
    vpath = Path(video)
    if not vpath.exists():
        return "视频文件不存在"
    if not out_dir or not str(out_dir).strip():
        out_dir = str(vpath.parent / f"{vpath.stem}_frames")
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    pattern = str(out_root / "frame_%04d.jpg")
    ff = _ffmpeg_path()
    import subprocess
    cmd = [ff, "-y", "-i", str(vpath), "-vf",
           f"fps={max(1, int(fps or 1))}", "-q:v", "2", pattern]
    # v4.0: 显式 UTF-8 解码（同 video_to_gif），并统计抽帧前后差值，
    # 避免把上一次运行残留的 frame_*.jpg 也算进本次数量。
    before = len(list(out_root.glob("frame_*.jpg")))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=ENCODE_TIMEOUT,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return (f"抽帧失败：ffmpeg 超过 {fmt_timeout(ENCODE_TIMEOUT)} 没有结束，"
                "已强制中止（文件可能被占用或损坏）")
    if r.returncode != 0:
        return f"抽帧失败：{r.stderr[-300:]}"
    n = len(list(out_root.glob("frame_*.jpg"))) - before
    return f"✅ 已抽取 {max(0, n)} 帧到：{out_root}"


def batch_rename(pattern: str, start: int):
    """把最近下载素材批量重命名为 {prefix}{序号}{ext}。"""
    files = [p for p in _tk_recent_files if Path(p).exists()]
    if not files:
        return "暂无最近下载的素材，请先在「🎬 去水印」下载。"
    pattern = (pattern or "素材").strip()
    start = max(1, int(start or 1))
    done = 0
    lines = []
    for i, p in enumerate(files, start=start):
        src = Path(p)
        new_name = f"{pattern}{i:03d}{src.suffix.lower()}"
        dst = src.with_name(new_name)
        j = 1
        while dst.exists() and dst != src:
            dst = src.with_name(f"{pattern}{i:03d}_{j}{src.suffix.lower()}")
            j += 1
        try:
            src.rename(dst)
            lines.append(f"{src.name} -> {dst.name}")
            done += 1
        except Exception as e:
            lines.append(f"{src.name} 失败: {e}")
    return f"✅ 已重命名 {done} 个文件\n" + "\n".join(lines[:20])


def cleanup_tmp():
    """清理 .work / .part / 缓存临时文件。"""
    removed = 0
    try:
        for root in library_roots() + [Path(tk_config.output_dir)]:
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                try:
                    if p.is_file() and (p.suffix == ".part" or ".work" in str(p)):
                        p.unlink()
                        removed += 1
                except Exception:
                    pass
            work = root / ".work"
            if work.is_dir():
                shutil.rmtree(work, ignore_errors=True)
    except Exception:
        pass
    return f"已清理 {removed} 个临时文件"


# ---------- 关闭程序 ----------

def close_collector_runtime() -> None:
    """释放采集子系统：Playwright 驱动子进程 + SQLite 连接。

    v1.4.3 修复：`server.collector_api.shutdown()` 的 docstring 写着
    「关停时释放浏览器与数据库（由 desktop.py 调用）」，但实际上**全项目
    没有任何调用点**。于是关窗、或点界面「关闭程序」之后，只要本次运行
    用过采集/聊天，Playwright driver（node.exe）与 Chromium 就会变成孤儿
    进程留在后台，数据库连接也不会关闭 —— 正是 collector/bootstrap.py
    里反复警告的「不 stop 会导致驱动进程残留（历史踩坑）」。
    现在把它接进两条关停路径：desktop.do_shutdown（关窗）
    与 services.request_shutdown（界面按钮 / /api/shutdown）。
    """
    try:
        from server import collector_api
        collector_api.shutdown()
    except Exception as e:                     # noqa: BLE001 — 关停必须兜底
        logging.warning(f"释放采集子系统失败: {e}")


def request_shutdown(reason: str = "用户请求") -> None:
    """协作式关停（v4.0）。

    原来只有 threading.Timer(1.0, os._exit(0))：不关闭 Gradio 服务、不通知
    正在跑的任务、不刷日志，会截断正在写的 zip/.part 并残留端口。
    现在先让任务停下来、关掉服务释放端口，再兜底退出。
    """
    logging.info(f"开始关停：{reason}")
    try:
        CANCEL.set()
        _tk_cancel.set()
        _hd_pause.clear()
        _tk_pause.clear()
        # v4.0 第 1 期：统一的「停掉所有任务」入口。
        # 过渡期任务注入的是旧全局 Event，两套都要设；第 2 期独立信号后
        # 这一行就足够覆盖全部任务。
        n = bus.cancel_all()
        if n:
            logging.info(f"已请求取消 {n} 个进行中的任务")
    except Exception:
        pass
    # 释放 Playwright 驱动子进程与数据库连接（必须在 os._exit 之前）。
    close_collector_runtime()
    try:
        if _DEMO is not None:
            _DEMO.close()          # 释放监听端口
    except Exception as e:
        logging.warning(f"关闭服务失败: {e}")
    try:
        (PROJECT_ROOT / "runtime" / "server.json").unlink(missing_ok=True)
    except Exception:
        pass


def shutdown_app():
    """点击「关闭程序」后：先协作式关停，再兜底退出。"""
    import threading as _t

    def _die():
        try:
            request_shutdown("界面关闭按钮")
        except Exception:
            pass
        os._exit(0)

    _t.Timer(1.0, _die).start()
    return "⏻ 程序正在关闭…… 1 秒后进程结束。"


# ---------- 设置 ----------

def settings_values():
    c = load()
    return (c["output_root"], c["date_folder"], c["face_enhance"],
            int(c.get("tile", 0) or 0), c["half"], c["video_encoder"],
            int(c["video_crf"]), c["interpolate"], c["skip_existing"],
            c["keep_frames"], int(c.get("max_output_size", 8192) or 0),
            int(c.get("video_max_side", 2560) or 0),
            bool(c.get("beep_on_done", True)),
            bool(c.get("lan_access", False)),
            # 这两项 settings_save() 一直在写，但设置页读不回来（填了保存、
            # 重新打开页面又变回默认值，再点保存就把用户的选择覆盖掉）。
            # 放在末尾是为了不改动上面已有的配对顺序。
            bool(c.get("video_skip_similar", True)),
            int(c.get("face_work_side", 4096) or 4096),
            # v1.5.0：内置 HTTPS（手机「保存到相册 / 分享」的前提）
            bool(c.get("https_enabled", False)))


def settings_save(out_root, date_folder, face_enhance, tile, half,
                  encoder, crf, interpolate, skip, skipsim, keep,
                  max_size, vidmax, beep, facework, lan_access,
                  https_enabled=False):
    """保存设置（v4.0.1：已移除局域网访问密码参数）。

    ``https_enabled`` 带默认值：位置参数是既有的脆弱契约
    （见 test_settings_roundtrip.py），末尾追加可避免影响老调用点。
    """
    c = load()
    c["output_root"] = str(Path(str(out_root)).expanduser())
    c["date_folder"] = bool(date_folder)
    c["face_enhance"] = bool(face_enhance)
    # 兜底值与 common/config.py 的 DEFAULTS["face_work_side"] 保持一致（4096），
    # 避免「界面显示一个值、实际按另一个值处理」。
    c["face_work_side"] = int(facework or 4096)
    c["tile"] = int(tile or 0)
    c["half"] = bool(half)
    c["video_encoder"] = encoder
    c["video_crf"] = int(crf)
    c["interpolate"] = bool(interpolate)
    c["skip_existing"] = bool(skip)
    c["video_skip_similar"] = bool(skipsim)
    c["keep_frames"] = bool(keep)
    c["max_output_size"] = int(max_size or 0)
    c["video_max_side"] = int(vidmax or 0)
    c["beep_on_done"] = bool(beep)
    c["lan_access"] = bool(lan_access)
    c["https_enabled"] = bool(https_enabled)
    # 密码字段已废弃，清空以免残留在配置里造成误解
    c.pop("lan_password", None)
    save(c)
    return "设置已保存 ✓（局域网访问 / HTTPS 需重启程序生效）"


MODEL_TABLE_HEADERS = ["分类", "模型", "体积", "显存", "接入状态", "权重"]


def models_table():
    return [[r["分类"], r["模型"], r["体积"], r["显存"], r["接入状态"], r["权重"]]
            for r in model_status()]


# 说明（v4.0.4）：这里原来还有一份 models_cards_html / presets_html /
# update_notice_html 的旧实现，被文件后面同名的新实现覆盖，属于死代码。
# 其中旧的 presets_html 还引用了一个根本不存在的变量 badge —— 一旦有人
# 删掉后面的新实现，它会立刻 NameError。已整块删除，不要再加回来。


def mark_version_seen():
    try:
        cfg = load()
        if (cfg.get("last_seen_version") or "") != VERSION:
            cfg["last_seen_version"] = VERSION
            save(cfg)
            logging.info(f"更新通知已展示并记录：v{VERSION}")
    except Exception:
        logging.exception("记录已读版本失败")


def changelog_markdown():
    return "\n\n---\n\n".join(
        changelog_mod.render_markdown(r) for r in changelog_mod.RELEASES[:4])


def download_one(key):
    with tqdm(total=100, desc=f"下载 {MODELS[key]['file']}") as pbar:
        def cb(frac, msg):
            pbar.n = int(frac * 100)
            pbar.set_postfix_str(msg)
            pbar.refresh()
        download_model(key, cb)
    return models_table(), f"下载完成：{MODELS[key]['file']}"


def download_all():
    with tqdm(total=100, desc="下载全部模型") as pbar:
        def cb(frac, msg):
            pbar.n = int(frac * 100)
            pbar.set_postfix_str(msg)
            pbar.refresh()
        n = download_all_missing(cb)
    return models_table(), f"共下载/检查 {n} 个模型"


def env_text():
    info = env_info()
    return "\n".join(f"{k}: {v}" for k, v in info.items())


# ---------- 预设套用 ----------

# 预设 → 图片输出长边上限档位
_PRESET_IMG_MAX = {"fast_preview": "2048（最快）"}


def _apply_preset_image(key, cur_model, cur_scale, cur_face, cur_max):
    """把预设套用到「图片修复」页的参数上。"""
    p = presets.get(key)
    if not p:
        return cur_model, cur_scale, cur_face, cur_max, ""
    cfg = presets.for_kind(key, "image")
    model = cfg.get("model", cur_model)
    label = f"{p['icon']} {p['label']}"
    if model not in MODELS:
        return cur_model, cur_scale, cur_face, cur_max, f"⚠ 预设「{label}」所用模型不可用"
    if MODELS[model].get("stage") == "planned":
        return cur_model, cur_scale, cur_face, cur_max, (
            f"⚠ 「{label}」需要生成式重建引擎，将在后续版本接入，敬请期待")
    maxv = _PRESET_IMG_MAX.get(key, "4096（推荐）")
    return (model, cfg.get("scale", cur_scale), bool(cfg.get("face", cur_face)),
            maxv, f"已套用预设：{label} ｜ {presets.describe(key, 'image')}")


def _apply_preset_video(key, cur_model, cur_scale, cur_side, cur_interp):
    """把预设套用到「视频修复」页的参数上。"""
    p = presets.get(key)
    if not p:
        return cur_model, cur_scale, cur_side, cur_interp, ""
    cfg = presets.for_kind(key, "video")
    model = cfg.get("model", cur_model)
    label = f"{p['icon']} {p['label']}"
    if model not in MODELS:
        return cur_model, cur_scale, cur_side, cur_interp, f"⚠ 预设「{label}」所用模型不可用"
    if MODELS[model].get("stage") == "planned":
        return cur_model, cur_scale, cur_side, cur_interp, (
            f"⚠ 「{label}」需要生成式超分引擎，将在后续版本接入，敬请期待")
    return (model, cfg.get("scale", cur_scale), cfg.get("side", cur_side),
            cur_interp, f"已套用预设：{label} ｜ {presets.describe(key, 'video')}")


# ---------- UI ----------
# 样式体系集中在 ui/theme.py（设计 token + 卡片/按钮/标签/进度等组件样式）
CSS = ""   # 样式已迁到 frontend/css/app.css


def _gpu_label() -> str:
    """顶部状态胶囊用的显卡短标签。"""
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            short = name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
            vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            return f"{short} · {vram:.0f}GB"
        return "CPU 模式"
    except Exception:
        return ""


def do_tk_stats(count_text, selected, files):
    """「去水印」页顶部的小指标卡片（由已有组件变化驱动，不改动处理逻辑）。"""
    try:
        n = int(str(count_text).strip() or 0)
    except Exception:
        n = 0
    try:
        parsed = len(_parsed_options)
    except Exception:
        parsed = 0
    sel = len(selected or [])
    done = len([f for f in (files or []) if f])
    return _stat_row_html([
        (str(n), "识别链接", "cyan"),
        (str(parsed), "解析项目", "red"),
        (str(sel), "已勾选", "amber"),
        (str(done), "已下载", "green"),
    ])


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


# 注意：changelog_markdown() 定义在上方（用 changelog_mod.render_markdown + RELEASES）。
# 这里原来还有第二份定义，调用的是并不存在的 changelog_mod.markdown() /
# get_changelog()，因为 Python 取最后一份定义，导致「关于」页永远只显示
# 「（更新日志读取失败：module 'core.changelog' has no attribute 'get_changelog'）」。
# 已删除，不要再加回来。
