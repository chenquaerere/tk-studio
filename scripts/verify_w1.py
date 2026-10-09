"""W1 验收：共享层 + 统一配置迁移（无损校验）。

覆盖：
  A. 路径层 — APP_ROOT / APP_DIR / FRONTEND_DIR / ICON 指向正确且存在
  B. 版本层 — 品牌与版本号唯一来源，且去水印内核拿到的是同一个版本
  C. 配置迁移 — 扁平旧配置 → 分区新配置，逐键比对"零丢失"
  D. 兼容性 — watermark.core.config.load() 语义与合并前一致
  E. 双写 — hd 分区与 watermark 分区对 HD 键保持一致

用法：python scripts/verify_w1.py
退出码 0 = 全通过。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "_app"

# 必须在导入 common 之前改掉配置路径，避免污染真实 config.json
_TMPDIR = Path(tempfile.mkdtemp(prefix="tkstudio_w1_"))
_TMP_CFG = _TMPDIR / "config.json"
os.environ["TKSTUDIO_CONFIG"] = str(_TMP_CFG)

for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> bool:
    RESULTS.append((bool(ok), label + (f"  {extra}" if extra else "")))
    return bool(ok)


# 一份"旧扁平配置"（去水印 v4 的真实形态，含用户当前的 lan_access / output_root）
FLAT_LEGACY = {
    "output_root": r"C:\Users\25057\Desktop\素材\高清后",
    "date_folder": True,
    "face_enhance": False,
    "face_work_side": 4096,
    "image_model": "realesr-general-x4v3",
    "image_scale": 4,
    "tile": 0,
    "half": True,
    "video_model": "realesr-general-x4v3",
    "video_scale": 4,
    "video_encoder": "auto",
    "video_crf": 18,
    "interpolate": False,
    "skip_existing": True,
    "keep_frames": False,
    "max_output_size": 4096,
    "video_max_side": 1920,
    "video_skip_similar": True,
    "video_skip_thr": 1.5,
    "video_sharp": False,
    "beep_on_done": True,
    "lan_access": True,
    "lan_password": "",
    "last_seen_version": "3.0.1",
}


def main() -> int:
    print("=" * 66)
    print("W1 验收：共享层与统一配置迁移")
    print("=" * 66)

    # ---------- A. 路径层 ----------
    print("\n[A] 路径层")
    from common import paths
    check(paths.APP_ROOT == ROOT, "APP_ROOT 指向程序根",
          f"{paths.APP_ROOT}")
    check(paths.APP_DIR == APP_DIR, "APP_DIR 指向 _app", f"{paths.APP_DIR}")
    check(paths.FRONTEND_DIR.is_dir(), "frontend 目录存在",
          str(paths.FRONTEND_DIR))
    check(paths.ICON_PATH.is_file(), "icon.ico 存在", str(paths.ICON_PATH))
    check(paths.APP_LOG_PATH.parent == ROOT, "app.log 落在程序根")
    paths.ensure_dirs()
    check(all(d.is_dir() for d in paths.ALL_DIRS), "可写目录已全部创建")

    # ---------- B. 版本层 ----------
    print("\n[B] 版本层")
    from common import version
    check(version.APP_NAME == "TK 素材工坊", "品牌名统一",
          version.APP_NAME)
    check(version.APP_TITLE == f"TK 素材工坊 v{version.APP_VERSION}",
          "标题由唯一来源拼装", version.APP_TITLE)
    from watermark.core import version as wm_ver
    check(wm_ver.VERSION == version.APP_VERSION,
          "去水印内核版本与统一版本一致",
          f"{wm_ver.VERSION} == {version.APP_VERSION}")

    # ---------- C. 配置迁移 ----------
    print("\n[C] 配置迁移（扁平 → 分区，零丢失）")
    _TMP_CFG.write_text(json.dumps(FLAT_LEGACY, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    from common import config as cfg

    # 迁移源（旧采集器的独立 config.json）已在 W5 随临时目录清理掉了。
    # 为了让本脚本保持「可重复验证迁移零丢失」的能力，而不是依赖仓库里
    # 那份一次性历史文件，这里就在临时目录里现造一份。
    _legacy_dir = _TMPDIR / "docs"
    _legacy_dir.mkdir(parents=True, exist_ok=True)
    LEGACY_COLLECTOR = {
        "default_collect_count": 4,
        "timezone": "Asia/Shanghai",
        "browser_headless": True,
        "max_retry": 3,
        "account_interval_seconds": 4.0,
        "history_keep_days": 90,
        "log_keep_days": 30,
        "chat_last_count": 22,
        "auto_check_update": True,
        "update_repo": "chenquaerere/tiktok-link-collector",
    }
    (_legacy_dir / "_legacy_collector_config.json").write_text(
        json.dumps(LEGACY_COLLECTOR, ensure_ascii=False, indent=2),
        encoding="utf-8")
    cfg.LEGACY_DIR = _legacy_dir

    migrated = cfg.migrate()
    check(migrated, "首次加载触发了迁移")

    raw = json.loads(_TMP_CFG.read_text(encoding="utf-8"))
    check(all(k in raw for k in cfg.SECTIONS), "文件已变为分区格式",
          str(sorted(raw.keys())))
    backups = list(_TMPDIR.glob("config.legacy-*.json"))
    check(len(backups) == 1, "迁移前已备份旧文件",
          backups[0].name if backups else "（无备份）")

    full = cfg.load()
    missing = []
    for k, v in FLAT_LEGACY.items():
        found = (full["watermark"].get(k) == v or full["hd"].get(k) == v
                 or full["app"].get(k) == v)
        if not found:
            missing.append(k)
    check(not missing, "旧扁平配置的每个键都被保留",
          f"缺失 {missing}" if missing else f"共 {len(FLAT_LEGACY)} 键全部保留")

    check(full["watermark"]["output_root"] == FLAT_LEGACY["output_root"],
          "用户自定义 output_root 未被默认值覆盖",
          full["watermark"]["output_root"])
    check(full["watermark"]["lan_access"] is True, "用户开关 lan_access 保留")

    col = full["collector"]
    check(col.get("default_collect_count") == 4,
          "旧采集器配置已并入 collector 分区",
          f"default_collect_count={col.get('default_collect_count')}")
    check(col.get("chat_last_count") == 22,
          "采集器运行时键保留", f"chat_last_count={col.get('chat_last_count')}")
    check(str(col.get("update_repo", "")).startswith("chenquaerere/"),
          "采集器仓库地址保留", str(col.get("update_repo")))
    check(len(col) >= 28, "collector 分区键数完整", f"{len(col)} 个键")

    # ---------- D. 兼容性 ----------
    print("\n[D] 去水印内核兼容性")
    from watermark.core import config as wm_cfg
    got = wm_cfg.load()
    check(got["video_scale"] == 4, "load() 可读到 HD 键 video_scale",
          str(got["video_scale"]))
    check("skip_existing" in got and "output_root" in got,
          "load() 键集与合并前一致（扁平语义）")
    wm_cfg.save({"video_crf": 20})
    check(cfg.section("watermark")["video_crf"] == 20,
          "save() 写回统一配置的 watermark 分区")
    check(cfg.section("hd")["video_crf"] == 20,
          "HD 键自动同步到 hd 分区（双写）")

    # ---------- E. 幂等 ----------
    print("\n[E] 幂等性")
    before = _TMP_CFG.read_text(encoding="utf-8")
    cfg.reset_cache()
    cfg.migrate()
    after = _TMP_CFG.read_text(encoding="utf-8")
    check(before == after, "再次加载不会重复迁移或改动文件")

    # ---------- 汇总 ----------
    print("\n" + "=" * 66)
    passed = sum(1 for ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for ok, label in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print("-" * 66)
    print(f"{passed}/{total} 通过" + ("　✅ W1 验收通过"
                                    if passed == total else "　❌ 存在失败项"))
    print("=" * 66)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
