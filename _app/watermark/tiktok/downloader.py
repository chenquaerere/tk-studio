"""
TikTok 无水印下载引擎 (v3.2)

唯一下载路径: TikwmClient
- 完全不依赖 yt-dlp 下载
- 每日自动建子目录 downloads/YYYY-MM-DD/
- 每个任务独立 try/except,失败不抛到外层
- 状态: pending -> parsing -> downloading -> done / failed
- 实时进度回调 (5% 步进,UI 不卡)
- v3.2: 接收 pause_event (threading.Event), 每次下载前 wait(), 暂停时不占 CPU

旧版保留的 yt-dlp 兜底已彻底删除 (用户已经骂过)
"""
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import date
from typing import Optional, Callable, Dict, List

import requests

from .models import VideoInfo, ContentType, DownloadTask, DownloadStatus
from .logger import log
from .config import config
from .tikwm import (
    TikwmClient, DEFAULT_PROXY, get_detected_proxy, ensure_proxy_detected,
    get_last_error as _get_last_error,
)
from .parse_cache import lookup as cache_lookup, save as cache_save, evict as cache_evict

# v2.1: 解析失败原因记录（UI 显示具体错误）
_parse_error_map: Dict[str, str] = {}
_parse_error_lock = threading.Lock()


# ====================================================================
# 进度回调签名 (供 UI 端约定)
# ====================================================================
# on_progress(task: DownloadTask)     进度/状态更新
# on_each(idx: int, task: DownloadTask)  批量下载每条完成时
# on_batch_done(tasks: list)         整批完成时


class TikTokDownloader:
    """
    TikTok 下载器 (v3.1)
    - 每日一个子文件夹
    - TikwmClient 是唯一解析 + 下载路径
    - 多线程并发, 每个任务独立 try/except
    """

    def __init__(
        self,
        output_dir: str = "./downloads",
        max_concurrent: int = 3,
        proxy_url: str = "",
    ):
        self.base_output_dir = Path(output_dir)
        self.base_output_dir.mkdir(parents=True, exist_ok=True)
        self.max_concurrent = max(1, int(max_concurrent))
        if proxy_url:
            self.proxy_url = proxy_url
        else:
            # v4.0：懒探测，不阻塞；无代理时留空 = 直连
            self.proxy_url = ensure_proxy_detected()

        # 并发信号量
        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._tasks: Dict[str, DownloadTask] = {}
        self._lock = threading.Lock()

        # Tikwm 客户端 (懒构造)
        self._tikwm: Optional[TikwmClient] = None
        self._tikwm_lock = threading.Lock()

    # ====================================================================
    # Tikwm 客户端
    # ====================================================================
    def _get_tikwm(self) -> TikwmClient:
        with self._tikwm_lock:
            if self._tikwm is None:
                self._tikwm = TikwmClient(
                    proxy_url=self.proxy_url,
                    timeout=30,
                    max_retries=config.max_retries,
                    hd_mode=(config.video_quality != "worst"),
                )
            return self._tikwm

    def reload_tikwm(self):
        with self._tikwm_lock:
            self._tikwm = None

    # ====================================================================
    # 公共: 解析
    # ====================================================================
    def parse_info(self, url: str) -> Optional[VideoInfo]:
        """单条解析,失败返回 None (v2.1: 失败原因记录到 parse_error_map 供 UI 显示)"""
        if not url or not url.strip():
            return None
        url = url.strip()
        # 本地缓存: 相同链接/相同视频 7 天内直接命中, 节省 tikwm 限流配额
        cached = cache_lookup(url)
        if cached is not None:
            log.info(f"[parse] 缓存命中: {cached.video_id or url[:50]}")
            return cached
        try:
            info = self._get_tikwm().parse(url)
            if info:
                log.info(
                    f"[parse] OK: {info.video_id} type={info.content_type.value}"
                )
                cache_save(url, info)
                with _parse_error_lock:
                    _parse_error_map.pop(url, None)
            else:
                err = _get_last_error()
                with _parse_error_lock:
                    _parse_error_map[url] = err or "解析失败（无返回结果）"
            return info
        except Exception as e:
            log.error(f"[parse] 异常 {url[:60]}: {e}")
            with _parse_error_lock:
                _parse_error_map[url] = f"{type(e).__name__}: {e}"
            return None

    @staticmethod
    def last_error(url: str) -> str:
        """返回最近一次解析失败的原因（供 UI 显示）。"""
        with _parse_error_lock:
            return _parse_error_map.get(url, "")

    def parse_batch(
        self,
        urls: List[str],
        on_each: Optional[Callable[[int, int, Optional[VideoInfo]], None]] = None,
    ) -> List[Optional[VideoInfo]]:
        """批量解析,空字符串/空白会被跳过"""
        # 过滤空
        clean: List[str] = [u.strip() for u in urls if u and u.strip()]
        if not clean:
            return []

        results: List[Optional[VideoInfo]] = [None] * len(clean)
        max_workers = min(8, len(clean))

        def _worker(i: int, u: str):
            try:
                results[i] = self.parse_info(u)
            except Exception as e:
                log.error(f"[parse_batch] {u[:60]} 异常: {e}")
                results[i] = None
            finally:
                if on_each:
                    try:
                        on_each(i, len(clean), results[i])
                    except Exception:
                        log.debug("回调异常", exc_info=True)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_worker, i, u) for i, u in enumerate(clean)]
            for f in futures:
                f.result()
        return results

    # ====================================================================
    # 公共: 下载
    # ====================================================================
    def get_today_dir(self) -> Path:
        """今天的子目录 downloads/YYYY-MM-DD/"""
        today = date.today().isoformat()
        d = self.base_output_dir / today
        d.mkdir(parents=True, exist_ok=True)
        return d

    def download(
        self,
        info: VideoInfo,
        idx: int = 1,
        on_progress: Optional[Callable[[DownloadTask], None]] = None,
        pause_event: Optional[threading.Event] = None,  # v3.2
        image_sel: Optional[List[int]] = None,  # 图集内指定图片序号（1-based）
    ) -> DownloadTask:
        """
        下载单个 VideoInfo -> DownloadTask
        失败不抛, 把错误塞进 task.error, status = FAILED

        v3.2: 每次下载前 wait(pause_event), 暂停时阻塞在 Event 上不占 CPU
        """
        task = DownloadTask(
            task_id=f"dl_{int(time.time()*1000)}_{hash(info.url) & 0xFFFF:04x}",
            info=info,
            output_dir=str(self.get_today_dir()),
        )
        with self._lock:
            self._tasks[task.task_id] = task

        def _emit():
            if on_progress:
                try:
                    on_progress(task)
                except Exception:
                    log.debug("回调异常", exc_info=True)

        def _set_status(s: DownloadStatus):
            task.status = s
            _emit()

        try:
            if not info:
                raise RuntimeError("VideoInfo 为空")

            # v3.2: 进入下载前, 等待 pause_event (set 立即通过, clear 阻塞)
            if pause_event is not None:
                pause_event.wait()

            _set_status(DownloadStatus.PARSING)
            _emit()

            client = self._get_tikwm()
            out_dir = self.get_today_dir()

            # 内部进度回调: 0-1000 (避免 UI 一直刷)
            _prog_start = time.monotonic()
            _prog_last_d = 0
            _prog_last_t = _prog_start

            def _on_prog(d: int, t: int):
                nonlocal _prog_last_d, _prog_last_t
                now = time.monotonic()
                dt = now - _prog_last_t
                if dt >= 0.8 and d >= _prog_last_d:
                    speed_bps = (d - _prog_last_d) / dt
                    _prog_last_d, _prog_last_t = d, now
                    task.speed = _fmt_speed(speed_bps)
                    if speed_bps > 0 and t > d:
                        task.eta = _fmt_eta((t - d) / speed_bps)
                if t > 0:
                    pct = min(100.0, d / t * 100.0)
                else:
                    # 拿不到 total, 单图计数法: 简单 +1 推进
                    pct = min(99.0, task.progress + 0.5)
                task.progress = pct
                task.downloaded_bytes = d
                task.total_bytes = t
                if task.status != DownloadStatus.DOWNLOADING:
                    _set_status(DownloadStatus.DOWNLOADING)
                else:
                    _emit()

            _set_status(DownloadStatus.DOWNLOADING)
            _emit()

            file_path = client.download(
                info, output_dir=out_dir, idx=idx, on_progress=_on_prog,
                image_sel=image_sel,
            )

            if not file_path:
                raise RuntimeError("下载返回空路径")

            task.file_path = file_path
            task.progress = 100.0
            task.downloaded_bytes = task.total_bytes or 0
            task.finished_at = _now()
            _set_status(DownloadStatus.COMPLETED)
            log.info(f"[download] OK: {file_path}")
            return task

        except Exception as e:
            task.error = f"{type(e).__name__}: {e}"
            task.finished_at = _now()
            _set_status(DownloadStatus.FAILED)
            log.error(f"[download] FAIL {info.url if info else '?'}: {e}")
            # 下载失败时清掉缓存, 下次解析强制刷新拿新地址
            if info:
                cache_evict(info.url, info.video_id)
            return task

    def download_batch(
        self,
        infos: List[VideoInfo],
        on_each: Optional[Callable[[int, DownloadTask], None]] = None,
        on_progress: Optional[Callable[[DownloadTask], None]] = None,
        pause_event: Optional[threading.Event] = None,  # v3.2
    ) -> List[DownloadTask]:
        """
        批量下载
        - 跳过 None/空
        - 每个任务独立 try/except (download() 内部已捕获)
        - max_concurrent 控制并发
        - 序号 idx 按入参顺序递增 (1-based)
        - v3.2: pause_event 传给每个 download(), 暂停时所有 worker 阻塞在 Event.wait()
        """
        # 过滤无效
        indexed: List[tuple[int, VideoInfo]] = []
        for i, inf in enumerate(infos, start=1):
            if inf is not None:
                indexed.append((i, inf))

        results: List[DownloadTask] = []
        results_lock = threading.Lock()
        threads: List[threading.Thread] = []

        def _worker(idx: int, info: VideoInfo):
            with self._semaphore:
                task = self.download(
                    info, idx=idx, on_progress=on_progress,
                    pause_event=pause_event,
                )
                with results_lock:
                    results.append(task)
                if on_each:
                    try:
                        on_each(idx, task)
                    except Exception:
                        log.debug("回调异常", exc_info=True)

        for idx, info in indexed:
            t = threading.Thread(target=_worker, args=(idx, info), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        return results


def _fmt_speed(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _fmt_eta(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _now():
    from datetime import datetime
    return datetime.now()
