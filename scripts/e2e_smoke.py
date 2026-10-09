"""端到端冒烟：合并后的程序能否真的"干活"（TK 素材工坊 v1.1.1）。

三个阶段
--------
  A  去水印下载 1 条   —— 走真实 Tikwm 解析 + 下载，校验产物落盘
  B  高清超分 1 张     —— 合成一张测试图，跑 Real-ESRGAN 2x，校验输出尺寸
  C  真实采集 1 账号   —— **默认不跑**（替换式记录 = 破坏性），
                          必须显式给 `--allow-collect --account-id <id>` 才执行

用法
----
    venv\\Scripts\\python.exe scripts\\e2e_smoke.py
    venv\\Scripts\\python.exe scripts\\e2e_smoke.py --url "https://www.tiktok.com/@x/video/123"
    venv\\Scripts\\python.exe scripts\\e2e_smoke.py --allow-collect --account-id 3

安全纪律
--------
- 数据库**只读**（`mode=ro`），从里面取一条既有作品链接用于阶段 A。
- 阶段 A/B 的产物一律写进 `runtime/e2e_smoke/`（测试目录），
  绝不写进用户的 `downloads/` 素材库。
- 阶段 C 是**破坏性**的：采集引擎会先删除该账号的既有作品记录再重新采集，
  因此默认关闭，且必须显式指定单个账号。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "_app"
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, FAIL = "✓", "✗"
OUT_DIR = ROOT / "runtime" / "e2e_smoke"
DB = ROOT / "data" / "tiktok_link_collector.db"
PROXY = "http://127.0.0.1:11304"


def hr(t: str) -> None:
    print()
    print("-" * 74)
    print(t)
    print("-" * 74)


def pick_sample_url() -> str | None:
    """从采集库只读取一条既有作品链接作为阶段 A 的样本。"""
    if not DB.exists():
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT video_url FROM videos WHERE video_url LIKE '%tiktok.com%' LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


# ── 阶段 A：去水印下载 1 条 ──────────────────────────────────────────────
def stage_a(url: str | None) -> bool:
    hr("阶段 A · 去水印下载 1 条")
    if not url:
        url = pick_sample_url()
    if not url:
        print(f"  {FAIL} 没有可用样本链接（采集库为空且未提供 --url）")
        return False
    # 隐私：只回显链接尾部，完整链接来自用户自己的库
    print(f"  样本：…{url[-28:]}")

    from watermark.tiktok.downloader import TikTokDownloader

    # ⚠️ 输出目录带时间戳：下载模块有「文件已存在就跳过」的断点续传逻辑，
    #    复用同一目录会让第二次运行直接跳过下载，验不出真实下载链路。
    run_dir = OUT_DIR / f"dl_{int(time.time())}"
    dl = TikTokDownloader(output_dir=str(run_dir), max_concurrent=1, proxy_url=PROXY)
    t0 = time.time()
    info = dl.parse_info(url)
    if info is None:
        print(f"  {FAIL} 解析失败：{getattr(dl, 'last_error', lambda *_: '未知')}")
        return False
    n_img = len(getattr(info, "images", None) or [])
    print(f"  解析成功（{time.time() - t0:.1f}s）："
          f"id={getattr(info, 'video_id', getattr(info, 'id', '?'))} "
          f"kind={'图集' if n_img else '视频'}")

    task = dl.download(info, idx=1)
    status = getattr(task, "status", None)
    status_txt = getattr(status, "value", status)
    err = getattr(task, "error", "") or ""

    files = sorted(p for p in run_dir.rglob("*") if p.is_file())
    fresh = [p for p in files if p.stat().st_size > 0]
    if not fresh:
        print(f"  {FAIL} 没有新产物落盘：status={status_txt} error={err}")
        return False
    total = 0
    for p in fresh:
        total += p.stat().st_size
        print(f"  · {p.relative_to(ROOT)}  {p.stat().st_size / 1024:.1f} KB")
    print(f"  {OK} 去水印下载成功：{len(fresh)} 个文件，"
          f"{total / 1024:.1f} KB，用时 {time.time() - t0:.1f}s（status={status_txt}）")
    return True


# ── 阶段 B：高清超分 1 张 ────────────────────────────────────────────────
def stage_b() -> bool:
    hr("阶段 B · 高清超分 1 张")
    import numpy as np
    from PIL import Image, ImageDraw

    from watermark.core.engines.image import (
        enhance_image, load_image_bgr, save_image_bgr, get_upsampler,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src_p = OUT_DIR / "e2e_src_200x150.png"
    dst_p = OUT_DIR / "e2e_upscaled_2x.png"

    # 合成测试图：棋盘格 + 圆 + 斜线，小尺寸便于观察放大效果
    w, h = 200, 150
    im = Image.new("RGB", (w, h), (24, 28, 38))
    d = ImageDraw.Draw(im)
    for y in range(0, h, 20):
        for x in range(0, w, 20):
            if (x // 20 + y // 20) % 2 == 0:
                d.rectangle([x, y, x + 19, y + 19], fill=(40, 46, 62))
    d.ellipse([50, 35, 150, 115], outline=(108, 99, 255), width=3)
    d.line([20, 130, 180, 20], fill=(37, 244, 238), width=2)
    im.save(src_p)
    print(f"  测试图：{src_p.relative_to(ROOT)} ({w}x{h})")

    t0 = time.time()
    get_upsampler("realesr-general-x4v3")          # 预热（首次加载模型较慢）
    img = load_image_bgr(src_p)
    # ⚠️ 关键：enhance_image 的最终缩放只在 max_side > 0 时执行 ——
    # 模型永远按原生倍率（本模型 4x）输出，然后由 _limit_output 收到
    # min(max_side, 长边 × outscale)；max_side=0 会跳过限制、直接吐出 4x。
    # 这里按真实调用（process_image_file 的默认档）给 4096。
    out = enhance_image(img, "realesr-general-x4v3", outscale=2,
                        face_enhance=False, max_side=4096)
    save_image_bgr(dst_p, out)
    oh, ow = out.shape[:2]
    print(f"  输出：{dst_p.relative_to(ROOT)} ({ow}x{oh})，"
          f"耗时 {time.time() - t0:.1f}s，{dst_p.stat().st_size / 1024:.1f} KB")

    if ow == w * 2 and oh == h * 2:
        print(f"  {OK} 超分成功：{w}x{h} → {ow}x{oh}（2x 精确命中）")
        return True
    print(f"  {FAIL} 输出尺寸不符：期望 {w * 2}x{h * 2}，实得 {ow}x{oh}")
    return False


# ── 阶段 C：真实采集 1 账号（破坏性，默认关闭）────────────────────────────
def stage_c(account_id: int, count: int) -> bool:
    hr("阶段 C · 真实采集 1 账号（破坏性）")
    print("  ⚠️  采集是「替换式记录」：会先删除该账号既有作品记录，再重新采集。")
    from collector.bootstrap import CollectorServices

    svc = CollectorServices(auto_backup=True)
    try:
        accounts = getattr(svc, "accounts", None)
        target = None
        if accounts is not None:
            for a in (accounts.list() if hasattr(accounts, "list") else []):
                if getattr(a, "id", None) == account_id:
                    target = a
                    break
        if target is None:
            print(f"  {FAIL} 未找到账号 id={account_id}")
            return False
        names = getattr(target, "username", "?")
        print(f"  目标账号：id={account_id} username={names}")

        t0 = time.time()
        engine = svc.build_engine() if hasattr(svc, "build_engine") else None
        if engine is None:
            print(f"  {FAIL} 装配层未提供 build_engine()，跳过（请在界面上手动验证）")
            return False
        result = engine.run([target], count=count)
        print(f"  采集完成，用时 {time.time() - t0:.1f}s：{result}")
        return True
    finally:
        try:
            svc.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="TK 素材工坊端到端冒烟")
    ap.add_argument("--url", default=None, help="阶段 A 使用的 TikTok 链接")
    ap.add_argument("--skip-a", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    ap.add_argument("--allow-collect", action="store_true",
                    help="允许执行阶段 C（破坏性：替换式删除该账号既有作品）")
    ap.add_argument("--account-id", type=int, default=0)
    ap.add_argument("--count", type=int, default=3)
    args = ap.parse_args()

    print("=" * 74)
    # 版本从唯一来源取，避免脚本里的版本号随发版过期
    from common.version import APP_TITLE

    print(f"端到端冒烟 · {APP_TITLE}")
    print("=" * 74)
    print(f"测试产物目录：{OUT_DIR.relative_to(ROOT)}（不会写进 downloads/ 素材库）")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, bool]] = []
    if not args.skip_a:
        results.append(("A 去水印下载 1 条", stage_a(args.url)))
    if not args.skip_b:
        results.append(("B 高清超分 1 张", stage_b()))

    if args.allow_collect and args.account_id:
        results.append((f"C 真实采集账号 {args.account_id}", stage_c(args.account_id, args.count)))
    else:
        print()
        print("阶段 C 未执行（默认关闭）。")
        print("  要执行请加：--allow-collect --account-id <账号id>")
        print("  ⚠️ 该阶段会**替换式删除**目标账号的既有作品记录，请确认后再跑。")

    print()
    print("=" * 74)
    failed = 0
    for name, ok in results:
        print(f"  {OK if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print("=" * 74)
    print(f"共 {len(results)} 个阶段，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
