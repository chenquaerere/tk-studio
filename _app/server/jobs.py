"""任务注册表：把「全局标志 + 裸字典」升级为「可订阅的任务对象」（v4.0 第 1 期）。

为什么需要它
------------
v3.0.1 的做法是：

    _tk_pause / _tk_cancel / _hd_pause   ← 模块级全局 Event
    status = {...}                        ← 裸字典，工作线程直接改
    while True: yield ...                 ← 生成器每 0.3s 轮询字典

问题（已在审计中确认）：
1. 全局标志在 queue(default_concurrency_limit=4) 下，多个任务会互相踩；
2. 进度没有结构，界面只能靠「每 0.3s 读字典」模拟，无法做真实事件推送；
3. 关闭程序时没有统一的「停掉所有任务」入口。

本模块提供 Job：它既保留了 status 字典的读写方式（**旧代码零改动**），
又能把每次变化变成结构化事件推给订阅者。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Iterable, Optional

from .state import Event, EventKind, JobKind, JobSnapshot, JobState

# status 字典里这些键对应到 Job 的结构化字段
_FIELD_MAP = {
    "stage": "stage",
    "stage_done": "stage_frac",       # 特殊处理（与 stage_total 一起算）
    "summary": "message",
    "frac": "frac",
    "current": "message",
}
# 这些键只进 values，不做结构化映射
_VALUES_KEYS = {
    "img_total", "vid_total", "img_done", "vid_done", "stage_total",
    "stage_elapsed", "gallery", "vids", "files", "zip_path", "auto_hd",
    "final", "errors", "notify", "total", "ok", "fail", "elapsed",
    "lines", "pct",
}


class Job:
    """一个可订阅、可暂停、可取消的任务。

    用法（兼容旧写法）：
        job = store.create(JobKind.HD_MIXED, label="批量高清")
        job["img_total"] = 3        # 旧代码怎么写，现在就怎么写
        job.emit(EventKind.STAGE, stage="upscale", stage_label="超分")
        job.cancel.is_set()         # 每个任务自己的取消位，互不影响
    """

    def __init__(self, job_id: str, kind: JobKind, label: str = "",
                 cancel: Optional[threading.Event] = None,
                 pause: Optional[threading.Event] = None) -> None:
        self.id = job_id
        self.kind = kind
        self.snapshot = JobSnapshot(id=job_id, kind=kind, label=label)
        # 默认每个任务自带独立的暂停/取消位（修掉全局标志互踩的问题）；
        # 过渡期允许外部注入旧的全局 Event，行为保持不变。
        self.cancel = cancel or threading.Event()
        self.pause = pause or threading.Event()
        self.pause.set()                      # set = 运行，clear = 暂停
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[Event], None]] = []
        self._events: list[Event] = []
        self._seq = 0
        self._max_history = 500               # 事件回放上限，防止长任务吃内存
        self._unsubscribe_bus: Optional[Callable[[], None]] = None

    def attach_bus(self, fn: Callable[[Event], None]) -> None:
        """把「汇总总线转发」挂到本任务上，并记住退订句柄（避免泄漏）。"""
        self._unsubscribe_bus = self.subscribe(fn, replay=False)

    def dispose(self) -> None:
        """任务结束后释放资源：退订总线、清空历史。"""
        if self._unsubscribe_bus is not None:
            try:
                self._unsubscribe_bus()
            except Exception:
                pass
            self._unsubscribe_bus = None
        with self._lock:
            self._events = []
            self._subscribers = []

    # ---------------------------------------------------------------- 订阅
    def subscribe(self, fn: Callable[[Event], None],
                  replay: bool = True) -> Callable[[], None]:
        """订阅事件。replay=True 时先补发历史事件（重连场景）。"""
        with self._lock:
            self._subscribers.append(fn)
            history = list(self._events) if replay else []
        for ev in history:
            try:
                fn(ev)
            except Exception:
                pass

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return _unsubscribe

    def events_since(self, seq: int) -> list[Event]:
        """取 seq 之后的事件（SSE 断线重连补齐用）。"""
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    # ---------------------------------------------------------------- 状态
    def set_state(self, state: JobState) -> None:
        with self._lock:
            self.snapshot.state = state
            if state == JobState.RUNNING and self.snapshot.started_at is None:
                self.snapshot.started_at = time.time()
            if state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED):
                self.snapshot.finished_at = time.time()

    def pause_job(self) -> None:
        self.pause.clear()
        self.set_state(JobState.PAUSED)

    def resume_job(self) -> None:
        self.pause.set()
        if self.snapshot.state == JobState.PAUSED:
            self.set_state(JobState.RUNNING)

    def cancel_job(self) -> None:
        self.cancel.set()
        self.pause.set()                      # 取消时解除暂停，避免卡在等待

    def request_stop(self) -> None:
        """供「关闭程序」统一调用：让任务尽快退出。"""
        self.cancel_job()

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    @property
    def paused(self) -> bool:
        return not self.pause.is_set()

    def wait_if_paused(self, poll: float = 0.2) -> bool:
        """暂停时阻塞。返回 True 表示应继续，False 表示已被取消。

        替代旧代码里到处重复的：
            while not _tk_pause.is_set():
                if _tk_cancel.is_set(): break
                time.sleep(0.2)
        """
        while not self.pause.is_set():
            if self.cancel.is_set():
                return False
            time.sleep(poll)
        return not self.cancel.is_set()

    # ---------------------------------------------------------------- 事件
    def emit(self, kind: EventKind, **kwargs) -> Event:
        """发一条结构化事件，并同步刷到快照上。"""
        with self._lock:
            self._seq += 1
            ev = Event(kind=kind, job_id=self.id, seq=self._seq, **kwargs)
            self._events.append(ev)
            if len(self._events) > self._max_history:
                self._events = self._events[-self._max_history:]
            self._apply(ev)
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def log(self, message: str, level: str = "info") -> Event:
        return self.emit(EventKind.LOG, message=message, level=level)

    def notify(self, level: str, message: str = "") -> Event:
        return self.emit(EventKind.NOTIFY, level=level, message=message)

    def stage(self, stage: str, label: str = "", total: int = 0) -> Event:
        with self._lock:
            self.snapshot.stage = stage
            self.snapshot.stage_label = label or stage
            self.snapshot.stage_frac = 0.0
        return self.emit(EventKind.STAGE, stage=stage, stage_label=label or stage,
                         data={"total": total})

    def progress(self, frac: float, message: str = "", **data) -> Event:
        frac = max(0.0, min(1.0, float(frac or 0.0)))
        with self._lock:
            self.snapshot.frac = frac
            if message:
                self.snapshot.message = message
        return self.emit(EventKind.PROGRESS, frac=frac, message=message, data=data)

    def finish(self, message: str = "", data: Optional[dict] = None) -> Event:
        """任务正常结束。

        ⚠️ `data` 必须能带结果 —— 前端 `onDone` 只从收尾事件里取数据。
        这里原来不带 data，于是「采集完成」的 toast / 系统通知永远显示
        「共 0 个账号：完成 0」（结果只存在快照 values 里，收尾事件是空的）。
        """
        self.set_state(JobState.DONE)
        if message:
            self.snapshot.message = message
        return self.emit(EventKind.JOB_DONE, frac=1.0, message=message,
                         level="ok" if not self.snapshot.error else "warn",
                         data=dict(data or {}))

    def fail(self, error: str, data: Optional[dict] = None) -> Event:
        with self._lock:
            self.snapshot.error = error
        self.set_state(JobState.FAILED)
        return self.emit(EventKind.JOB_FAILED, message=error, level="error",
                         data=dict(data or {}))

    def cancel_finish(self, message: str = "已取消",
                      data: Optional[dict] = None) -> Event:
        """任务被取消 / 中途停止 —— 同样要把已经采到的部分结果带出去。"""
        self.set_state(JobState.CANCELLED)
        return self.emit(EventKind.JOB_CANCELLED, message=message, level="warn",
                         data=dict(data or {}))

    def _apply(self, ev: Event) -> None:
        """把事件反映到快照上。"""
        s = self.snapshot
        if ev.frac is not None:
            s.frac = ev.frac
        if ev.stage:
            s.stage = ev.stage
        if ev.stage_frac is not None:
            s.stage_frac = ev.stage_frac
        if ev.stage_label:
            s.stage_label = ev.stage_label
        if ev.message:
            s.message = ev.message
        if ev.data:
            s.values.update(ev.data)

    # ------------------------------------------------- status 字典兼容层
    def __setitem__(self, key: str, value) -> None:
        """旧代码 `status["xxx"] = v` 的兼容入口。

        既更新快照的 values（供新前端读取），也在关键键上产生结构化事件。
        """
        handled = False
        with self._lock:
            self.snapshot.values[key] = value
            if key == "current":
                self.snapshot.message = str(value)
            elif key == "frac":
                self.snapshot.frac = max(0.0, min(1.0, float(value or 0)))
            elif key == "stage":
                self.snapshot.stage = str(value)
            elif key == "summary":
                self.snapshot.message = str(value)
            elif key == "final":
                self.snapshot.message = str(value)
            elif key == "notify":
                handled = True
                level = "info"
                if isinstance(value, tuple) and value:
                    raw = str(value[0])
                    level = {"ok": "ok", "warn": "warn", "error": "error"}.get(raw, "info")
                self.emit(EventKind.NOTIFY, level=level)
                handled = True
            # stage_done / stage_total 一起算阶段进度
            if key in ("stage_done", "stage_total"):
                total = self.snapshot.values.get("stage_total") or 0
                done = self.snapshot.values.get("stage_done") or 0
                try:
                    self.snapshot.stage_frac = (float(done) / float(total)) if total else 0.0
                except Exception:
                    self.snapshot.stage_frac = 0.0
        if key == "notify":
            return
        # 结构化变化一律发一条 STATE 事件，前端可据此局部刷新
        self.emit(EventKind.STATE, stage_frac=self.snapshot.stage_frac,
                  data={key: _jsonable(value)})

    def __getitem__(self, key: str):
        return self.snapshot.values[key]

    def get(self, key: str, default=None):
        return self.snapshot.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.snapshot.values

    def update(self, mapping: dict) -> None:
        for k, v in mapping.items():
            self[k] = v

    def as_status(self) -> dict:
        """导出为旧代码/旧界面期望的普通字典（只读用途）。"""
        with self._lock:
            return dict(self.snapshot.values)

    def snapshot_dict(self) -> dict:
        return self.snapshot.to_dict()


def _jsonable(value):
    """把 set 之类的值转成可 JSON 序列化的形式。"""
    if isinstance(value, set):
        return sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class JobStore:
    """全局任务注册表（单例）。"""

    def __init__(self, max_jobs: int = 100) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._max_jobs = max_jobs
        self._bus_subscribers: list[Callable[[Event], None]] = []

    def create(self, kind: JobKind, label: str = "",
               cancel: Optional[threading.Event] = None,
               pause: Optional[threading.Event] = None,
               job_id: Optional[str] = None) -> Job:
        jid = job_id or uuid.uuid4().hex[:12]
        job = Job(jid, kind, label=label, cancel=cancel, pause=pause)
        # 汇总总线：所有任务的事件都会转发一份，便于「底部任务栏」统一展示。
        # 用 attach_bus 记住退订句柄 —— 原来用匿名 lambda 订阅，任务结束后
        # JobStore 仍持有对已结束 Job 的引用，长跑会一直累积。
        if self._bus_subscribers:
            job.attach_bus(lambda ev: self._fanout(ev))
        with self._lock:
            self._jobs[jid] = job
            self._order.append(jid)
            # 淘汰最老的已结束任务（并释放其总线订阅，避免长跑累积）
            while len(self._order) > self._max_jobs:
                old = self._order.pop(0)
                victim = self._jobs.get(old)
                if victim and victim.snapshot.state in (
                        JobState.DONE, JobState.FAILED, JobState.CANCELLED):
                    self._jobs.pop(old, None)
                    victim.dispose()
                else:
                    self._order.append(old)
                    break
        job.emit(EventKind.JOB_CREATED, message=label)
        return job

    def _fanout(self, ev: Event) -> None:
        for fn in list(self._bus_subscribers):
            try:
                fn(ev)
            except Exception:
                pass

    def subscribe_all(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        """订阅所有任务的事件（底部任务栏 / 日志面板用）。"""
        with self._lock:
            self._bus_subscribers.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._bus_subscribers:
                    self._bus_subscribers.remove(fn)

        return _unsubscribe

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[j] for j in self._order if j in self._jobs]

    def active(self) -> list[Job]:
        return [j for j in self.all()
                if j.snapshot.state in (JobState.PENDING, JobState.RUNNING,
                                        JobState.PAUSED)]

    def find_active(self, kind: Optional[JobKind] = None) -> Optional[Job]:
        """找同类型正在跑的任务（用于「已有任务在运行」提示）。"""
        for j in self.active():
            if kind is None or j.kind == kind:
                return j
        return None

    def cancel_all(self) -> int:
        n = 0
        for j in self.active():
            j.request_stop()
            n += 1
        return n

    def snapshots(self, limit: int = 50) -> list[dict]:
        return [j.snapshot_dict() for j in self.all()[-limit:]]


# 进程级单例
STORE = JobStore()
