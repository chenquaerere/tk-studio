"""去水印内核的版本信息 —— 已并入统一版本源 `common.version`。

保留本模块是为了不破坏既有调用点（`from ..version import VERSION` 等）。
"""
from __future__ import annotations

from common.version import (  # noqa: F401
    APP_NAME,
    APP_TITLE,
    APP_VERSION as VERSION,
    BUILD_DATE,
)

LEGACY_VERSION = "4.0.4"   # 合并前该内核的最后版本，仅作文档追溯

__all__ = ["VERSION", "BUILD_DATE", "APP_NAME", "APP_TITLE", "LEGACY_VERSION"]
