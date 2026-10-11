"""打包完整版：把项目目录打成 zip，归档到指定目录。

用法：
    python scripts/package.py                       # 默认输出到上级归档目录
    python scripts/package.py <输出目录>
    python scripts/package.py --dry-run             # 只列清单，不写文件

说明：
- 压缩包内顶层目录会被重命名为当前版本（例如 TK素材工坊_v1.1.1）
- 自动跳过 __pycache__ / 日志 / 用户数据 / 运行期产物
  ⚠️ 用户数据（data/ 里的数据库与 21 个账号登录态、downloads/ 素材、
  backups/ 备份、exports/ 导出）**一律不进分发包**，避免把个人数据发出去。
- `runtime/` 下**只保留 `ms-playwright`（随身浏览器内核）**，其余（端口文件
  `server.json`、上传临时、测试产物）都是运行期垃圾，不进包。
- `docs/` 保留文档，但跳过 `screenshots/`（体积大且随版本变动，无分发价值）。
- 使用 ZIP_DEFLATED，自动启用 zip64（含大体积 torch 动态库）
"""

import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 合并后应用代码在 _app/ 下（common / collector / watermark / server / ui / frontend）
APP_DIR = ROOT / "_app"
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.version import APP_NAME, APP_VERSION  # noqa: E402

# 运行期产物 + 开发期产物 + 用户数据，不进压缩包
SKIP_DIRS = {
    "__pycache__", ".cover_cache", "logs", ".git", ".venv",
    "scripts", "release",
    # —— 用户数据，绝不外发 ——
    "data", "downloads", "backups", "exports", "links_log",
    # —— 打包产物临时目录 ——
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
SKIP_DIR_PREFIX = ("_backup_", "venv.bak")
# 文件名前缀跳过：配置迁移留下的旧配置备份是 `config.legacy-<时间戳>.json`，
# 用精确名匹配会漏（曾经真漏过一次），所以按前缀判。
# `_legacy_*` 同理 —— 那些是 W1 迁移时的旧配置快照，含用户绝对路径，不该外发。
SKIP_FILE_PREFIX = ("config.legacy", "_legacy")
SKIP_FILES = {"app.log", "launcher.log", "python_path.txt", "config.legacy",
              "server.json"}
SKIP_SUFFIX = (".pyc", ".pyo", ".part", ".tmp")

# runtime/ 下白名单：只有它进包
RUNTIME_KEEP = {"ms-playwright"}
# docs/ 下黑名单：这些不进包
DOCS_SKIP = {"screenshots"}
# 内核目录里由本机 `playwright install` 写下的来源标记，跨机复制后只会误导，
# 不影响运行（Playwright 查内核只看 chromium-<revision>/ 目录是否存在）
SKIP_REL_PREFIX = ("runtime/ms-playwright/.links",)


# 中文 Windows 的 cmd 默认代码页是 GBK：脚本最后一行打印 ✅/❌ 会抛
# UnicodeEncodeError —— 明明所有断言都通过了，退出码却是 1（看起来像验收失败）。
# 这里自己把输出切成 UTF-8，任何控制台都能把结果打印完。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if any(part in SKIP_DIRS for part in parts):
        return True
    if any(part.startswith(SKIP_DIR_PREFIX) for part in parts):
        return True
    if rel.name.startswith(SKIP_FILE_PREFIX):
        return True
    if rel.as_posix().startswith(SKIP_REL_PREFIX):
        return True
    # runtime/ 只放行白名单
    if parts[0] == "runtime" and (len(parts) < 2 or parts[1] not in RUNTIME_KEEP):
        return True
    # docs/ 跳过截图
    if parts[0] == "docs" and len(parts) > 1 and parts[1] in DOCS_SKIP:
        return True
    return False



def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    default_out = ROOT.parent
    out_dir = Path(argv[0]) if argv else default_out
    out_dir.mkdir(parents=True, exist_ok=True)

    # 用完整版本号命名，便于保留历史版本、支持回滚
    # 例：TK素材工坊_v1.0.0.zip / 内部目录 TK素材工坊_v1.0.0
    arc_root = f"{APP_NAME.replace(' ', '')}_v{APP_VERSION}"
    zip_path = out_dir / f"{arc_root}.zip"
    # 先写临时文件，最后原子替换：
    # 直接 unlink 大体积旧包会被安全策略拦截（无法移入回收站）
    tmp_zip = zip_path.with_name(zip_path.name + ".part")
    tmp_zip.unlink(missing_ok=True)

    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if _excluded(rel):
            continue
        if rel.name in SKIP_FILES or p.suffix.lower() in SKIP_SUFFIX:
            continue
        files.append((p, rel))

    # ── 用户数据闸门 ──────────────────────────────────────────────
    # 分发包一旦带上登录态 / 数据库 / 素材，就是把个人数据发出去了。
    # 这里做硬断言，宁可打包失败也不放行。
    USER_DATA_TOPS = {"data", "downloads", "backups", "exports", "links_log"}
    leaked = [rel.as_posix() for _, rel in files if rel.parts[0] in USER_DATA_TOPS]
    if leaked:
        print(f"❌ 中止：检测到 {len(leaked)} 个用户数据文件混入待打包清单，"
              f"例如 {leaked[:5]}", flush=True)
        return 2
    # 反向确认：内核必须在包里
    kernel_key = "runtime/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    if not any(rel.as_posix() == kernel_key for _, rel in files):
        print("❌ 中止：随身浏览器内核不在待打包清单里（零依赖便携会失效）",
              flush=True)
        return 3
    print("✅ 用户数据闸门：0 个文件混入；随身内核已就位", flush=True)

    total_bytes = sum(p.stat().st_size for p, _ in files)
    print(f"待打包 {len(files)} 个文件，原始大小 "
          f"{total_bytes / 1024 ** 3:.2f} GB", flush=True)
    print(f"输出：{zip_path}", flush=True)

    # 分区统计：确认内核进包、用户数据不进包
    def _bucket(rel: Path) -> str:
        top = rel.parts[0]
        if top == "runtime":
            return "随身内核"
        if top == "_app":
            return "应用代码"
        if top in ("venv", "python-runtime"):
            return "运行时"
        if top in ("models", "tools", "gfpgan"):
            return "模型与工具"
        return "其它"
    buckets: dict[str, list[int]] = {}
    for p, rel in files:
        b = buckets.setdefault(_bucket(rel), [0, 0])
        b[0] += 1
        b[1] += p.stat().st_size
    for name, (n, sz) in sorted(buckets.items(), key=lambda kv: -kv[1][1]):
        print(f"    {name:<10} {n:>7} 文件  {sz / 1024 ** 3:>6.2f} GB", flush=True)

    if dry_run:
        print("\n[dry-run] 根目录直属文件（便于人工核对有无配置/日志残留）：",
              flush=True)
        for p, rel in sorted(files, key=lambda x: x[1].as_posix()):
            if len(rel.parts) == 1:
                print(f"    {p.stat().st_size:>9} B  {rel.name}", flush=True)
        print("\n[dry-run] 体积最大的 8 个待打包文件：", flush=True)
        for p, rel in sorted(files, key=lambda x: -x[0].stat().st_size)[:8]:
            print(f"    {p.stat().st_size / 1048576:>8.1f} MB  {rel.as_posix()}",
                  flush=True)
        return 0

    t0 = time.time()
    done = 0
    last_report = 0
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as zf:
        for i, (p, rel) in enumerate(files, 1):
            zf.write(p, arcname=f"{arc_root}/{rel.as_posix()}")
            done += p.stat().st_size
            now = time.time()
            if now - last_report > 5:
                last_report = now
                pct = done / max(1, total_bytes) * 100
                print(f"  [{pct:5.1f}%] {i}/{len(files)} 文件  "
                      f"{done / 1024 ** 3:.2f}/{total_bytes / 1024 ** 3:.2f} GB  "
                      f"用时 {now - t0:.0f}s", flush=True)

    os.replace(str(tmp_zip), str(zip_path))
    size = zip_path.stat().st_size
    print(f"完成：{zip_path}", flush=True)
    print(f"压缩包 {size / 1024 ** 3:.2f} GB（原始 {total_bytes / 1024 ** 3:.2f} GB，"
          f"压缩率 {size / max(1, total_bytes) * 100:.0f}%），"
          f"耗时 {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
