"""界面总线（v4.0 第 1 期）：UI 无关的对外接口。

业务代码（app.py / core / tiktok）只通过本模块和界面打交道，
**不再直接 import gradio**。这样第 2 期把渲染层换成
FastAPI + HTML/JS 时，业务侧零改动。

职责：
- 暴露任务注册表（server/jobs.py）的便捷入口；
- 记录「需要提示给用户」的短消息（原来靠 gr.Info / gr.Warning）；
- 提供旧界面的兼容接口（gr.update 之类的收口点）。

本模块本身不 import gradio；gradio 只在 ui/gradio_bridge.py 里出现。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from server.jobs import STORE, Job
from server.state import Event, EventKind, JobKind, JobState

__all__ = [
    "STORE", "Job", "JobKind", "JobState", "Event", "EventKind",
    "create_job", "get_job", "active_jobs", "cancel_all",
    "notify", "drain_notifications", "on_event", "snapshot_all",
]


# ---------------------------------------------------------------- 任务入口

def create_job(kind: JobKind, label: str = "", **kw) -> Job:
    """建一个任务。业务代码统一从这里拿 Job。"""
    return STORE.create(kind, label=label, **kw)


def get_job(job_id: str) -> Optional[Job]:
    return STORE.get(job_id)


def active_jobs(kind: Optional[JobKind] = None) -> list[Job]:
    if kind is None:
        return STORE.active()
    return [j for j in STORE.active() if j.kind == kind]


def find_active(kind: Optional[JobKind] = None) -> Optional[Job]:
    return STORE.find_active(kind)


def cancel_all() -> int:
    """关停时调用：让所有在跑的任务尽快退出。"""
    return STORE.cancel_all()


def snapshot_all(limit: int = 50) -> list[dict]:
    return STORE.snapshots(limit)


def on_event(fn: Callable[[Event], None]) -> Callable[[], None]:
    """订阅所有任务事件（未来的 SSE / 任务栏都走这里）。"""
    return STORE.subscribe_all(fn)


# ---------------------------------------------------------------- 用户提示
# 原来业务代码用 gr.Info(...) / gr.Warning(...) 直接弹提示，
# 那是把 UI 行为写进了业务层。现在改为写进一个队列，
# 由渲染层（Gradio 或 FastAPI）自行决定怎么显示。

_notifications: list[dict] = []
_notify_lock = threading.Lock()
_MAX_NOTIFICATIONS = 50


def notify(message: str, level: str = "info") -> None:
    """记录一条需要提示用户的信息（info/ok/warn/error）。"""
    with _notify_lock:
        _notifications.append({
            "message": str(message),
            "level": level,
            "ts": time.time(),
        })
        if len(_notifications) > _MAX_NOTIFICATIONS:
            del _notifications[:-_MAX_NOTIFICATIONS]


def drain_notifications() -> list[dict]:
    """取出并清空待提示消息（渲染层每次刷新时调用）。"""
    with _notify_lock:
        out = list(_notifications)
        _notifications.clear()
    return out


def peek_notifications() -> list[dict]:
    with _notify_lock:
        return list(_notifications)
