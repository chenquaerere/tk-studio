"""全应用统一日志。

合并前两个程序各有一套日志初始化：
  - 采集器 `app/log/logger.py`：根 logger + `logs/app.log` 按日滚动 + console
  - 去水印 `tiktok/logger.py`：具名 logger（保留原样，独立输出）

本模块提供**唯一的根 logger 初始化入口**，避免两套配置互相叠加导致重复输出。
采集内核的旧入口 `collector.log.logger.setup_logging()` 已改为委托到此。

行为约定：
  - 幂等：重复调用只生效一次（`_configured`）。
  - 位置：`<APP_ROOT>/logs/app.log`，按日滚动，保留 `keep_days` 天。
  - 级别：默认 INFO；可由统一配置 `app.log_level` 覆盖。
"""
from __future__ import annotations

import logging
import os
import threading
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from . import paths

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_lock = threading.Lock()
_configured = False


def _normalize_level(level) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level or "INFO").upper(), logging.INFO)


def setup(level: str = "INFO", log_dir: Optional[str] = None,
          keep_days: int = 30, console: bool = True) -> None:
    """初始化根 logger（幂等）。"""
    global _configured
    with _lock:
        if _configured:
            return

        target = str(log_dir) if log_dir else str(paths.LOGS_DIR)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            target = str(paths.APP_ROOT)

        root = logging.getLogger()
        root.setLevel(_normalize_level(level))
        fmt = logging.Formatter(_FORMAT)

        try:
            fh = TimedRotatingFileHandler(
                os.path.join(target, "app.log"), when="midnight",
                interval=1, backupCount=max(1, int(keep_days)), encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(root.level)
            root.addHandler(fh)
        except OSError:
            pass  # 日志不可写不应阻断程序启动

        if console:
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            ch.setLevel(root.level)
            root.addHandler(ch)

        _configured = True


def get_logger(name: str = "tkstudio") -> logging.Logger:
    """获取具名 logger；未初始化则用默认配置兜底。"""
    if not _configured:
        setup()
    return logging.getLogger(name)


def reset() -> None:
    """仅测试用：允许重新初始化。"""
    global _configured
    _configured = False
