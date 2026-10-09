import shutil
import subprocess
import sys
from pathlib import Path

from common import paths

PROJECT_ROOT = paths.APP_ROOT
TOOLS_DIR = PROJECT_ROOT / "tools"

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif",
    ".heic", ".heif",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".m4v", ".ts",
    ".webm", ".mts", ".m2ts", ".3gp",
}

# --- Windows 无控制台运行的关键开关（v4.0）-------------------------------
# 程序由 pythonw.exe 拉起（无控制台）。此时创建子进程，Windows 会为每个子进程
# 新分配一个控制台窗口 —— 这就是「高清处理时 cmd 一直闪」的根因。
# 注意：capture_output 只是接管管道，**不能**阻止控制台分配，
# 必须显式传 CREATE_NO_WINDOW。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def no_window_kwargs() -> dict:
    """给所有子进程调用补齐「不弹黑窗」参数。"""
    return {
        "creationflags": CREATE_NO_WINDOW,
        # 无控制台时 stdin 句柄可能是无效的，显式给 DEVNULL 更稳
        "stdin": subprocess.DEVNULL,
    }


def _find_tool(name: str) -> Path | None:
    exe = shutil.which(name)
    if exe:
        return Path(exe)
    cand = TOOLS_DIR / "ffmpeg" / "bin" / f"{name}.exe"
    return cand if cand.exists() else None


def find_ffmpeg() -> Path | None:
    return _find_tool("ffmpeg")


def find_ffprobe() -> Path | None:
    return _find_tool("ffprobe")


def run_quiet(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """静默执行子进程：不弹控制台窗口，输出走管道。

    v4.0：补上 creationflags=CREATE_NO_WINDOW（原来缺失，导致每条 ffmpeg
    命令都会闪一个黑窗，一个视频 3~7 次）。
    """
    for k, v in no_window_kwargs().items():
        kwargs.setdefault(k, v)
    return subprocess.run(cmd, check=True, capture_output=True, **kwargs)


def env_info() -> dict:
    info = {
        "python": sys.version.split()[0],
        "torch": "-",
        "cuda": "-",
        "gpu": "未检测到 / 未安装 torch",
        "ffmpeg": "未找到（可运行 scripts/setup_ffmpeg.ps1）",
    }
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda"] = str(torch.version.cuda)
        if torch.cuda.is_available():
            info["gpu"] = (
                f"{torch.cuda.get_device_name(0)} "
                f"({torch.cuda.get_device_properties(0).total_memory / 2**30:.1f} GB)"
            )
        else:
            info["gpu"] = "仅 CPU（未检测到可用 CUDA）"
    except Exception as exc:
        info["gpu"] = f"torch 导入失败: {exc}"
    ff = find_ffmpeg()
    if ff:
        info["ffmpeg"] = str(ff)
    return info