"""全应用唯一的路径来源。

为什么要有这个模块：
    采集内核与去水印内核原本各自用 `Path(__file__).parent.parent` 推导项目根，
    合并后模块层级发生变化（多了一层 `_app/watermark/`），这类推导会静默指错目录。
    所有路径统一从这里取，杜绝"按 __file__ 数层数"的写法。

目录布局约定（`APP_ROOT` = 程序根，即 exe / desktop.py 所在目录）：

    APP_ROOT/
      _app/            应用代码（= APP_DIR，已加入 sys.path）
        common/        本模块所在
        collector/     采集内核
        watermark/     去水印 + 高清内核
        server/        FastAPI 服务层
        ui/            事件总线等界面无关组件
        frontend/      统一 Web UI
        assets/        icon.ico / logo
      data/            用户数据（数据库、登录态）
      logs/  exports/  links_log/  backups/  runtime/
      models/  tools/ffmpeg/  gfpgan/  venv/  python-runtime/
      config.json  app.log
"""
from __future__ import annotations

import os
from pathlib import Path

_THIS = Path(__file__).resolve()

APP_DIR = _THIS.parents[1]        # .../_app
APP_ROOT = _THIS.parents[2]       # 程序根

# ---- 代码资源 ----
ASSETS_DIR = APP_DIR / "assets"
FRONTEND_DIR = APP_DIR / "frontend"
ICON_PATH = ASSETS_DIR / "icon.ico"

# ---- 用户数据与产物 ----
DATA_DIR = APP_ROOT / "data"
LOGS_DIR = APP_ROOT / "logs"
EXPORTS_DIR = APP_ROOT / "exports"
LINKS_LOG_DIR = APP_ROOT / "links_log"
BACKUPS_DIR = APP_ROOT / "backups"
RUNTIME_DIR = APP_ROOT / "runtime"

# ---- 外部资源（随程序分发）----
MODELS_DIR = APP_ROOT / "models"
FFMPEG_DIR = APP_ROOT / "tools" / "ffmpeg"
GFPGAN_DIR = APP_ROOT / "gfpgan"

# ---- 关键文件 ----
DB_PATH = DATA_DIR / "tiktok_link_collector.db"
APP_LOG_PATH = APP_ROOT / "app.log"
RUNTIME_FILE = RUNTIME_DIR / "server.json"
COLLECTOR_DOWNLOADS_DIR = APP_ROOT / "downloads"

# 统一配置文件路径。允许用环境变量覆盖，便于测试与多实例。
CONFIG_PATH = Path(os.environ.get("TKSTUDIO_CONFIG",
                                  str(APP_ROOT / "config.json")))

# 旧配置遗留副本所在目录（迁移用）
LEGACY_DIR = APP_ROOT / "docs"

ALL_DIRS = (DATA_DIR, LOGS_DIR, EXPORTS_DIR, LINKS_LOG_DIR, BACKUPS_DIR,
            RUNTIME_DIR, COLLECTOR_DOWNLOADS_DIR)


def ensure_dirs() -> None:
    """确保所有可写目录存在（幂等，可重复调用）。"""
    for d in ALL_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def resolve(value) -> Path:
    """把配置里的路径值解析成绝对路径（相对路径按 APP_ROOT 解析）。"""
    p = Path(str(value or "")).expanduser()
    return p if p.is_absolute() else (APP_ROOT / p)
