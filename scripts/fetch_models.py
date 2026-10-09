"""模型权重下载器（TK 素材工坊）。

把「高清修复」要用的模型权重一次性准备好，省去逐个手动下载。

用法
----
    venv\\Scripts\\python.exe scripts\\fetch_models.py --list
    venv\\Scripts\\python.exe scripts\\fetch_models.py                :: 全部「已接入」模型
    venv\\Scripts\\python.exe scripts\\fetch_models.py --min          :: 只下最小可用（约 5 MB）
    venv\\Scripts\\python.exe scripts\\fetch_models.py --all          :: 含可选模型
    venv\\Scripts\\python.exe scripts\\fetch_models.py --face         :: 额外准备人脸修复权重
    venv\\Scripts\\python.exe scripts\\fetch_models.py --key GFPGANv1.4 --key 4x-UltraSharp
    venv\\Scripts\\python.exe scripts\\fetch_models.py --proxy http://127.0.0.1:11304

说明
----
- 下载源取自**程序自身的那份注册表**（`_app/watermark/core/engines/models.py`），
  与程序内自动下载完全同源，支持多源回退、每个源重试 2 次、`.part` 临时文件。
- **其实不下载也能用**：程序在首次用到某个模型时会自动下载。
  本脚本的用途是"提前一次性下好"，适合网络慢、想离线部署、或要打包便携版的场景。
- 只写 `models\\` 与 `gfpgan\\weights\\` 两个目录，不碰任何用户数据。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_app"))

from watermark.core.engines import models as M  # noqa: E402

# 最小可用：装了它「高清修复」就能跑（程序默认模型）
MINIMAL_KEY = "realesr-general-x4v3"

# 人脸修复需要的两个辅助权重（由 facexlib 提供；程序在首次启用「人脸修复」时
# 也会自动下载到同一位置，这里只是提前备好）。地址与 facexlib 官方一致。
FACE_DIR = ROOT / "gfpgan" / "weights"
FACE_WEIGHTS = [
    ("detection_Resnet50_Final.pth",
     "https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth"),
    ("parsing_parsenet.pth",
     "https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth"),
]

MIN_BYTES = 1024 * 1024  # 小于 1 MB 视为未就位（与 engines/models.py 判据一致）


# --------------------------------------------------------------- 进度输出

def _make_cb(label: str):
    """返回一个进度回调：同一行刷新百分比，不刷屏。"""
    last = [-1]

    def cb(frac: float, text: str) -> None:
        pct = max(0, min(int(frac * 100), 100))
        if pct == last[0] and frac < 1.0:
            return
        last[0] = pct
        sys.stdout.write(f"\r    {label:<32} {pct:3d}%  {text[:36]:<36}")
        sys.stdout.flush()

    return cb


def _fmt(mb: float) -> str:
    return f"{mb:.1f} MB" if mb < 1024 else f"{mb / 1024:.2f} GB"


# --------------------------------------------------------------- 清单

def status_rows(keys: list[str]) -> list[tuple[str, str, int, bool]]:
    rows = []
    for k in keys:
        info = M.MODELS[k]
        rows.append((k, info["file"], int(info.get("size_mb") or 0), M.model_exists(k)))
    return rows


def print_list(keys: list[str]) -> None:
    print(f"\n模型目录：{M.MODELS_DIR}\n")
    print(f"  {'状态':<6} {'模型 key':<30} {'文件':<36} {'大小':>9}  说明")
    print("  " + "-" * 104)
    total_missing = 0
    for k, f, mb, ok in status_rows(keys):
        stage = M.MODELS[k].get("stage")
        tag = "已就位" if ok else ("缺失" if stage == "ready" else "未下载")
        if not ok:
            total_missing += mb
        note = M.MODELS[k].get("note", "")[:22]
        print(f"  {tag:<6} {k:<30} {f:<36} {mb:>6} MB  {note}")
    print("  " + "-" * 104)
    n_missing = sum(1 for _, _, _, ok in status_rows(keys) if not ok)
    print(f"  共 {len(keys)} 个，已就位 {len(keys) - n_missing} 个，"
          f"缺失 {n_missing} 个（约需下载 {_fmt(total_missing)}）\n")


# --------------------------------------------------------------- 下载

def download_extra(url: str, dest: Path) -> None:
    """下载一个额外权重（facexlib 系列）到 dest。"""
    if dest.exists() and dest.stat().st_size > MIN_BYTES:
        print(f"    {dest.name:<32} 已就位，跳过")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    cb = _make_cb(dest.name)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        read = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            read += len(chunk)
            if total:
                cb(min(read / total, 1.0), f"{read / 2**20:.0f} MB / {total / 2**20:.0f} MB")
    if total and tmp.stat().st_size != total:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载不完整（{tmp.stat().st_size}/{total} 字节）")
    if tmp.stat().st_size < MIN_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载内容异常（文件过小）")
    tmp.rename(dest)
    cb(1.0, "完成")
    sys.stdout.write("\n")
    sys.stdout.flush()


def fetch(keys: list[str]) -> int:
    """下载给定 key 的模型，返回成功数量。"""
    ok = 0
    for i, k in enumerate(keys, 1):
        info = M.MODELS[k]
        label = info["file"]
        if M.model_exists(k):
            print(f"  [{i}/{len(keys)}] {label:<34} 已就位，跳过")
            ok += 1
            continue
        print(f"  [{i}/{len(keys)}] {label:<34} {_fmt(int(info.get('size_mb') or 0))}")
        try:
            M.download_model(k, _make_cb(label))
            sys.stdout.write("\n")
            print(f"           -> 完成：{M.model_path(k)}")
            ok += 1
        except Exception as exc:
            sys.stdout.write("\n")
            print(f"           -> 失败：{exc}")
    return ok


# --------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(
        description="下载「高清修复」所需的模型权重（与程序内自动下载同源）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("说明", 1)[-1],
    )
    ap.add_argument("--list", "-l", action="store_true", help="只列出清单与状态，不下载")
    ap.add_argument("--min", action="store_true",
                    help=f"只下最小可用集合（{MINIMAL_KEY}，约 5 MB）")
    ap.add_argument("--all", action="store_true", help="含可选模型（CodeFormer 等）")
    ap.add_argument("--face", action="store_true",
                    help="额外准备人脸修复所需的人脸检测/解析权重（约 186 MB）")
    ap.add_argument("--key", action="append", default=[], metavar="KEY",
                    help="只下指定模型（可重复；key 见 --list）")
    ap.add_argument("--proxy", metavar="URL",
                    help="走代理下载，例如 http://127.0.0.1:11304")
    args = ap.parse_args()

    if args.proxy:
        os.environ["HTTP_PROXY"] = args.proxy
        os.environ["HTTPS_PROXY"] = args.proxy
        print(f"已设置代理：{args.proxy}")

    auto_keys = [k for k, v in M.MODELS.items() if v.get("download") == "auto"]

    # 决定要处理哪些模型
    if args.key:
        bad = [k for k in args.key if k not in M.MODELS]
        if bad:
            print(f"未知的模型 key：{bad}\n")
            print("可用 key：")
            for k in M.MODELS:
                print(f"  {k}")
            return 2
        if any(M.MODELS[k].get("download") != "auto" for k in args.key):
            print("注意：所选模型没有自动下载源，需按项目主页手动下载。")
        keys = args.key
    elif args.min:
        keys = [MINIMAL_KEY]
    elif args.all:
        keys = auto_keys
    else:
        keys = M.ready_keys()  # 默认：已接入流水线的模型

    if args.list:
        print_list(keys if keys else auto_keys)
        return 0

    print(f"\n模型目录：{M.MODELS_DIR}")
    print(f"本次处理 {len(keys)} 个模型：{', '.join(keys)}\n")

    ok = fetch(keys)

    if args.face:
        print(f"\n人脸修复辅助权重（{FACE_DIR}）")
        for name, url in FACE_WEIGHTS:
            try:
                download_extra(url, FACE_DIR / name)
            except Exception as exc:
                print(f"    {name:<32} 失败：{exc}")

    print(f"\n完成：{ok}/{len(keys)} 个模型就位。")
    if ok < len(keys):
        print("未完成的模型可重新运行本脚本 —— 已下载的会跳过，失败的会重试。")
        return 1
    print("现在可以到「素材制作 -> 高清修复」里直接选模型使用了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
