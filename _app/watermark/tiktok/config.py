"""
应用配置
"""
import json
import os
import shutil
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List

from .logger import log


def _read_core_version() -> str:
    """从 core/version.py 读版本号，保持全局单一来源（v4.0）。

    这里**不**用 `from watermark.core.version import VERSION`：
    `core/` 内部用的是相对导入，而历史上根目录还有一个同名的 app.py，
    在打包/冻结场景下包名可能产生歧义。直接读文件最稳，且没有循环依赖。
    """
    try:
        import re
        f = Path(__file__).resolve().parents[1] / "core" / "version.py"
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0"


APP_NAME = "TikTok 去水印"
APP_VERSION = _read_core_version()


def _legacy_app_dir() -> Path:
    """旧版位置：`%APPDATA%\\TikTokNoWatermark`（合并前的独立去水印程序）。"""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif os.name == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "TikTokNoWatermark"


def get_app_dir() -> Path:
    """应用数据目录。

    v1.0 合并版起统一到 `<程序根>/data/download/`：
    全应用的用户数据只在一处（data/），便于备份与打包。
    旧版 `%APPDATA%\\TikTokNoWatermark` 会在首次访问时**自动搬迁**，
    因此用户既有的下载配置与历史记录不会丢。
    """
    try:
        from common import paths as _paths
        app_dir = _paths.DATA_DIR / "download"
    except Exception:  # pragma: no cover - 极端兜底（脱离工程单独跑）
        app_dir = _legacy_app_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy(app_dir)
    return app_dir


def _migrate_legacy(app_dir: Path) -> None:
    """把旧 `%APPDATA%` 下的 config/history 一次性搬到新目录（只搬缺失的）。"""
    legacy = _legacy_app_dir()
    if not legacy.exists() or legacy.resolve() == app_dir.resolve():
        return
    for name in ("config.json", "history.json"):
        src, dst = legacy / name, app_dir / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


CONFIG_FILE = get_app_dir() / "config.json"
HISTORY_FILE = get_app_dir() / "history.json"


def _default_download_dir() -> Path:
    """默认下载目录：`<程序根>/downloads`（与旧版一致，不改变用户习惯）。"""
    try:
        from common import paths as _paths
        return _paths.COLLECTOR_DOWNLOADS_DIR
    except Exception:  # pragma: no cover
        return Path(__file__).resolve().parents[3] / "downloads"


@dataclass
class AppConfig:
    """Application configuration"""
    # Download settings
    output_dir: str = str(_default_download_dir())
    max_concurrent: int = 3               # max concurrent downloads
    max_retries: int = 3                  # retry count on failure

    # Video quality
    video_quality: str = "best"           # best / worst / 720p / 1080p

    # Proxy settings (empty = auto-detect)
    proxy_url: str = ""

    # UI settings
    color_theme: str = "blue"             # blue / green / dark-blue

    def save(self):
        """Persist config to CONFIG_FILE"""
        CONFIG_FILE.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    @classmethod
    def load(cls) -> "AppConfig":
        """从文件加载,不存在则使用默认值"""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                # 过滤掉不存在的字段
                valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
                filtered = {k: v for k, v in data.items() if k in valid_keys}
                return cls(**filtered)
            except Exception as e:
                log.warning(f"配置文件损坏: {e},使用默认值", exc_info=True)
                # 备份损坏文件
                try:
                    shutil.copy2(CONFIG_FILE, CONFIG_FILE.with_suffix(".json.bak"))
                except Exception:
                    pass
        return cls()


# 全局配置实例
config = AppConfig.load()
