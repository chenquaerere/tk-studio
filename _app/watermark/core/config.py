"""去水印内核的配置读写 —— 委托给统一配置层 `common.config`。

对外语义与合并前**完全一致**：`load()` 仍返回"去水印那套键"的扁平字典
（含 output_root / skip_existing / video_scale 等），`save()` 仍接受整包字典。
因此本内核其余代码（output.py / engines / server/services.py）一行都不用改。

实际落盘位置从原来的 `<root>/config.json`（扁平）变为
`<root>/config.json` 的 `watermark` / `hd` 分区，旧文件由 common.config
在首次加载时自动迁移并备份。
"""
from __future__ import annotations

from common import config as _cfg
from common import paths

PROJECT_ROOT = paths.APP_ROOT
CONFIG_PATH = paths.CONFIG_PATH
DEFAULT_OUTPUT_ROOT = paths.COLLECTOR_DOWNLOADS_DIR

# 兼容既有引用点：合并前这里是一个扁平 DEFAULTS 字典
DEFAULTS = dict(_cfg.DEFAULTS["watermark"])
DEFAULTS.update(_cfg.DEFAULTS["hd"])


def load() -> dict:
    """返回去水印分区的完整配置（默认值 + 已存值，含 HD 键）。"""
    return _cfg.section("watermark")


def save(cfg: dict) -> None:
    """合并写入（只覆盖传入的键，未传入的保持原值 —— 避免误清配置）。"""
    _cfg.update("watermark", cfg)


def get(key: str):
    return load()[key]
