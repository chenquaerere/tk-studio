"""版本与品牌信息的唯一来源。

合并后本模块取代原两个程序的版本文件：
  - 原采集器 `app/constants.py`（APP_VERSION 1.2.1）
  - 原去水印 `core/version.py`（VERSION 4.0.4）
两者的历史版本号保留在 LEGACY_VERSIONS 里，便于文档追溯。
"""
from __future__ import annotations

APP_NAME = "TK 素材工坊"
APP_NAME_EN = "TK Studio"
APP_VERSION = "1.4.3"
BUILD_DATE = "2026-10-10"

APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
APP_SLOGAN = "采集 · 去水印 · 高清修复，一站式素材工作台"

# 合并前的两条产品线（仅用于文档 / 关于页展示）
LEGACY_VERSIONS = {
    "collector": "1.2.1",   # TK 作品链接采集器
    "watermark": "4.0.4",   # TK 去水印高清程序
}

__version__ = APP_VERSION
