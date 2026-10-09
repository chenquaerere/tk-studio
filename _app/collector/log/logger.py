"""采集器日志入口 —— 委托给全应用统一日志（common.logging_setup）。

保留原模块的对外接口（setup_logging / get_logger），
使采集内核（engine / services / chat …）的调用点一行不改。
"""
from __future__ import annotations

import logging
from typing import Optional

from common import logging_setup as _ls

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(log_dir: Optional[str] = None,
                  level: str = "INFO",
                  console: bool = True,
                  keep_days: int = 30) -> None:
    """初始化日志系统（幂等）。签名与旧实现保持一致。"""
    _ls.setup(level=level, log_dir=log_dir, keep_days=keep_days, console=console)


def get_logger(name: str = "tiktok_link_collector") -> logging.Logger:
    """获取命名 logger；未初始化时用默认配置兜底。"""
    return _ls.get_logger(name)


def _normalize_level(level: str) -> int:
    return _ls._normalize_level(level)
