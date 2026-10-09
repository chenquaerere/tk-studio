"""数据结构层：结构化事件与任务快照（v4.0 第 1 期）。

设计目标
--------
把「界面如何显示进度」和「业务如何产生进度」彻底分开：

    业务代码  --emit(Event)-->  JobStore  --→ 界面（Gradio / FastAPI+SSE 都行）

这样 v4.0 第 2 期把渲染层从 Gradio 换成 FastAPI + HTML/JS 时，
业务代码一行都不用改。

本模块**不依赖任何 UI 框架**，也不依赖 gradio。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class JobKind(str, Enum):
    """任务大类。"""

    TIKTOK_DOWNLOAD = "tiktok_download"
    TIKTOK_PARSE = "tiktok_parse"
    HD_IMAGE = "hd_image"
    HD_VIDEO = "hd_video"
    HD_MIXED = "hd_mixed"
    MODEL_DOWNLOAD = "model_download"
    TOOL = "tool"
    # ---- 合并版新增：作品链接采集 / 好友群聊链接采集 ----
    COLLECT = "collect"              # 作品链接采集（按账号 / 按地区）
    CHAT_COLLECT = "chat_collect"    # 好友 / 群聊视频链接采集


class JobState(str, Enum):
    """任务生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventKind(str, Enum):
    """事件种类。SSE 直接把它当事件名用。"""

    JOB_CREATED = "job_created"
    JOB_STARTED = "job_started"
    STATE = "state"          # 状态字段变化（图片/视频计数等）
    STAGE = "stage"          # 进入某个阶段：抽帧/超分/编码
    PROGRESS = "progress"    # 0.0~1.0 总进度
    ITEM = "item"            # 单项开始/完成
    LOG = "log"              # 一行日志
    NOTIFY = "notify"        # 需要向用户弹提示（ok/warn/error）
    JOB_DONE = "job_done"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"


@dataclass(frozen=True)
class Event:
    """一条结构化进度事件。不可变，便于跨线程与多订阅者传递。"""

    kind: EventKind
    job_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)
    frac: Optional[float] = None          # 0.0~1.0，当前任务总进度
    stage: str = ""                        # 阶段键：extract/upscale/encode...
    stage_label: str = ""                  # 阶段中文名，直接显示
    stage_frac: Optional[float] = None     # 阶段内进度
    message: str = ""                      # 给用户看的一句话
    level: str = "info"                    # info / ok / warn / error
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转成可直接 JSON 序列化、经 SSE 下发的形式。"""
        return {
            "kind": self.kind.value if isinstance(self.kind, EventKind) else str(self.kind),
            "job_id": self.job_id,
            "seq": self.seq,
            "ts": self.ts,
            "frac": self.frac,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "stage_frac": self.stage_frac,
            "message": self.message,
            "level": self.level,
            "data": self.data,
        }


@dataclass
class JobSnapshot:
    """任务的完整当前状态快照（供界面一次性拉取或重连后补齐）。"""

    id: str
    kind: JobKind
    state: JobState = JobState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    label: str = ""
    frac: float = 0.0
    stage: str = ""
    stage_label: str = ""
    stage_frac: float = 0.0
    message: str = ""
    error: str = ""
    # 业务侧自由字段（图片数、视频数、输出路径、zip 路径……）
    values: dict = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind.value if isinstance(self.kind, JobKind) else str(self.kind),
            "state": self.state.value if isinstance(self.state, JobState) else str(self.state),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "label": self.label,
            "frac": self.frac,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "stage_frac": self.stage_frac,
            "message": self.message,
            "error": self.error,
            "elapsed": self.elapsed,
            "values": dict(self.values),
        }
        return d
