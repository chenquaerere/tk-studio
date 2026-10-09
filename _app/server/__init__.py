"""应用层（v4.0）：任务注册表、结构化事件、HTTP 服务。

本包不依赖任何 UI 框架，供 Gradio 过渡层（ui/bus.py）与
第 2 期的 FastAPI 服务层共同使用。
"""

from .jobs import STORE, Job, JobStore
from .state import Event, EventKind, JobKind, JobSnapshot, JobState

__all__ = [
    "STORE", "Job", "JobStore",
    "Event", "EventKind", "JobKind", "JobSnapshot", "JobState",
]
