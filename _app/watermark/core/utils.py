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


# --- 子进程超时上限（v1.4.3）------------------------------------------------
# 之前所有 ffmpeg 调用都没有 timeout：一旦卡住（文件被别的程序占用、磁盘掉线、
# 下载中断留下的半截文件、显卡驱动挂死），任务线程就永远停在那里，而 HD /
# 下载的「正在运行」标志不会被释放 —— 界面上按钮一直转，再点报「已有任务在
# 运行」，用户只能重启程序。
# 取值刻意宽松，正常任务不可能碰到：探测 90 秒，单条编码 1 小时。
PROBE_TIMEOUT = 90
ENCODE_TIMEOUT = 3600


def fmt_timeout(sec: int) -> str:
    """把秒数写成人话（进错误提示）。"""
    sec = int(sec)
    if sec >= 3600 and sec % 3600 == 0:
        return f"{sec // 3600} 小时"
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60} 分钟"
    return f"{sec} 秒"


class SubprocessTimeout(RuntimeError):
    """子进程超时被中止（默认上限见 ENCODE_TIMEOUT）。

    RuntimeError 的子类：既让任务线程能带着可读信息结束，又不依赖
    subprocess 的异常类型 —— TimeoutExpired 不是 CalledProcessError 的子类，
    原来的编码器回退逻辑 catch 不到它。
    """

    def __init__(self, cmd, limit: int):
        self.cmd = [str(c) for c in (cmd or [])]
        self.limit = int(limit)
        name = Path(self.cmd[0]).name if self.cmd else "命令"
        super().__init__(
            f"{name} 执行超过 {fmt_timeout(self.limit)}，已强制中止："
            "文件可能被其它程序占用、磁盘异常或进程卡死"
        )


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


def run_quiet(cmd: list, timeout: int | None = None, **kwargs) -> subprocess.CompletedProcess:
    """静默执行子进程：不弹控制台窗口，输出走管道。

    v4.0：补上 creationflags=CREATE_NO_WINDOW（原来缺失，导致每条 ffmpeg
    命令都会闪一个黑窗，一个视频 3~7 次）。
    v1.4.3：补超时（默认 ENCODE_TIMEOUT）。卡住的子进程会抛
    SubprocessTimeout，而不是让任务线程永远挂着、占用标志永不释放。
    """
    for k, v in no_window_kwargs().items():
        kwargs.setdefault(k, v)
    limit = ENCODE_TIMEOUT if timeout is None else int(timeout)
    try:
        return subprocess.run(cmd, check=True, capture_output=True,
                              timeout=limit, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise SubprocessTimeout(cmd, limit) from exc


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