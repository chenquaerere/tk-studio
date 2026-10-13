"""FastAPI 服务层（v4.0 第 2 期）。

这是「移除 Gradio」的落点：原生窗口加载本地 HTML，
HTML 通过这里的 HTTP API 与业务逻辑通信。

设计原则
--------
1. 业务函数直接复用 app.py 里已验证的实现（迁移期），**不重写**；
2. 进度通过 SSE（/api/jobs/{id}/events）推送结构化事件；
3. 任务由 server/jobs.py 的 JobStore 统一管理；
4. 本模块不 import gradio —— 需要的替身在 desktop.py 启动前注入。

第 3 期会把 app.py 里的业务函数抽到 server/services.py，
届时本文件的 import 换成 services 即可，路由不变。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

# 路径一律从统一路径层取，禁止按 __file__ 数层数
# （合并后模块层级已变化，手写 parents[N] 极易指错目录）。
from common import paths as _paths
from common import tls as _tls

PROJECT_ROOT = _paths.APP_ROOT


def _load_cfg() -> dict:
    import watermark.core.config as _c
    return _c.load()


def _runtime_json() -> dict:
    """读程序启动时写的 runtime/server.json（端口、监听地址、HTTPS 状态）。"""
    try:
        f = PROJECT_ROOT / "runtime" / "server.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}




try:
    from watermark.core.version import APP_TITLE, VERSION, BUILD_DATE
except Exception:  # pragma: no cover - 极端情况兜底
    APP_TITLE, VERSION, BUILD_DATE = "TK去水印高清程序", "4.0.0", ""

from server.jobs import STORE
from server.state import EventKind, JobKind, JobState
from ui import bus

log = logging.getLogger("tk.api")

FRONTEND_DIR = _paths.FRONTEND_DIR
UPLOAD_DIR = PROJECT_ROOT / "runtime" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_TITLE, version=VERSION, docs_url=None, redoc_url=None)

# 采集子系统路由（/api/collector/*）。
# 单独成模块：采集内核与去水印内核共用同一服务进程与 JobStore，
# 但路由、序列化、任务编排各自独立，避免 api.py 继续膨胀。
# 必须在 app 创建之后再 include（collector_api 里也要用到 JobStore）。
from server.collector_api import router as _collector_router  # noqa: E402

app.include_router(_collector_router)

# 延迟导入业务内核（server/services.py，与界面完全无关），避免 import 顺序问题
_core = None
_core_lock = threading.Lock()


def core():
    """拿到业务内核模块（server.services）。

    v4.0 第 2 期末：业务逻辑已从 app.py 抽到 server/services.py，
    本服务层不再依赖 gradio，也不再需要 gradio 替身。
    """
    global _core
    with _core_lock:
        if _core is None:
            import server.services as _svc
            _core = _svc
        return _core


# ----------------------------------------------------------------- 基础

@app.get("/api/ping")
def ping():
    return {"ok": True, "title": APP_TITLE, "version": VERSION, "build": BUILD_DATE}


@app.get("/api/health")
def health():
    c = core()
    return {
        "ok": True,
        "version": VERSION,
        "env": c.env_text() if hasattr(c, "env_text") else "",
        # 给顶栏用的短标签（不要塞整条 ffmpeg 路径）
        "gpu": c._gpu_label() if hasattr(c, "_gpu_label") else "",
    }


@app.get("/api/notifications")
def notifications():
    """渲染层轮询用户提示（替代 gr.Info / gr.Warning）。"""
    return {"items": bus.drain_notifications()}


# ----------------------------------------------------------------- 设置

@app.get("/api/settings")
def get_settings():
    c = core()
    values = c.settings_values()
    # v4.0.1：lan_password 已移除
    # 顺序必须与 services.settings_values() 一一对应；末尾两项是补回来的
    # 「保存了却读不回」的字段（video_skip_similar / face_work_side）。
    keys = ["output_root", "date_folder", "face_enhance", "tile", "half",
            "video_encoder", "video_crf", "interpolate", "skip_existing",
            "keep_frames", "max_output_size", "video_max_side", "beep_on_done",
            "lan_access", "video_skip_similar", "face_work_side",
            "https_enabled"]
    return {"values": dict(zip(keys, values)), "raw": _load_cfg()}


@app.post("/api/settings")
async def set_settings(payload: dict):
    c = core()
    # 参数顺序须与 services.settings_save 一致
    order = ["output_root", "date_folder", "face_enhance", "tile", "half",
             "encoder", "crf", "interpolate", "skip", "skipsim", "keep",
             "max_size", "vidmax", "beep", "facework", "lan_access",
             "https_enabled"]
    src = payload or {}
    alias = {
        "output_root": "output_root", "date_folder": "date_folder",
        "face_enhance": "face_enhance", "tile": "tile", "half": "half",
        "encoder": "video_encoder", "crf": "video_crf",
        "interpolate": "interpolate", "skip": "skip_existing",
        "skipsim": "video_skip_similar", "keep": "keep_frames",
        "max_size": "max_output_size", "vidmax": "video_max_side",
        "beep": "beep_on_done", "facework": "face_work_side",
        "lan_access": "lan_access",
        "https_enabled": "https_enabled",
    }
    cur = _load_cfg()
    args = []
    for name in order:
        key = alias[name]
        args.append(src.get(key, src.get(name, cur.get(key))))
    msg = c.settings_save(*args)
    # 输出目录可能变了：白名单（/api/file 的 403 判定）必须立刻跟着换
    refresh_allowed_paths()
    return {"message": msg}


# ----------------------------------------------------------------- 任务

@app.get("/api/jobs")
def list_jobs(limit: int = 50):
    return {"jobs": bus.snapshot_all(limit)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = bus.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.snapshot_dict()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, since: int = 0):
    """SSE：把 Job 的结构化事件实时推给前端。

    支持 since 参数做断线重连补齐。
    """
    job = bus.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _on_event(ev):
        # 从工作线程投递到事件循环
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ev)
        except Exception:
            pass

    for ev in job.events_since(since):
        queue.put_nowait(ev)
    unsub = job.subscribe(_on_event, replay=False)

    async def gen():
        try:
            # 先补一条当前快照，前端首屏即可渲染
            yield _sse("snapshot", job.snapshot_dict())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(ev.kind.value if hasattr(ev.kind, "value") else str(ev.kind),
                           ev.to_dict())
                if ev.kind in (EventKind.JOB_DONE, EventKind.JOB_FAILED,
                               EventKind.JOB_CANCELLED):
                    break
        finally:
            unsub()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/events")
async def all_events(request: Request):
    """SSE：所有任务的事件汇总（底部任务栏用）。"""
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _on_event(ev):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ev)
        except Exception:
            pass

    unsub = STORE.subscribe_all(_on_event)

    async def gen():
        try:
            yield _sse("hello", {"ok": True})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse("event", ev.to_dict())
        finally:
            unsub()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    job = bus.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    job.pause_job()
    return {"ok": True, "message": "已暂停"}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    job = bus.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    job.resume_job()
    return {"ok": True, "message": "已继续"}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = bus.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    job.cancel_job()
    return {"ok": True, "message": "已请求取消"}


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str):
    """取任务的最终结果（解析条目 / 下载文件 / 高清输出）。

    前端也可以在 JOB_DONE 事件里直接拿到；这个接口用于**断线重连后补齐**。
    """
    job = bus.get_job(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    from server.media import outputs_payload
    values = job.as_status()
    gallery = outputs_payload(values.get("gallery") or [])
    vids = outputs_payload(values.get("vids") or [])
    # 图片任务把结果放在 gallery，视频任务放在 vids；
    # files 是下载任务的字段。这里合并成统一的 outputs，避免「高清图片结果为空」。
    files = outputs_payload(values.get("files") or [])
    merged: list[dict] = []
    seen = set()
    for x in files + gallery + vids:
        if x["path"] in seen:
            continue
        seen.add(x["path"])
        merged.append(x)
    zip_path = values.get("zip_path") or ""
    return {
        "job": job.snapshot_dict(),
        "items": values.get("items") or [],
        "outputs": merged,
        "files": merged,
        "gallery": [x for x in gallery if x["is_image"]] or
                   [x for x in merged if x["is_image"]],
        "videos": [x for x in vids if x["is_video"]] or
                  [x for x in merged if x["is_video"]],
        "zip": ({"path": zip_path, "url": file_url(zip_path),
                 "name": Path(zip_path).name}
                if zip_path and Path(zip_path).is_file() else None),
        "summary": values.get("summary") or values.get("final") or "",
        "errors": values.get("errors") or [],
    }


def _outputs_payload(paths) -> list[dict]:
    """兼容旧调用：转交 server/media.py。"""
    from server.media import outputs_payload
    return outputs_payload(paths)


# ----------------------------------------------------------------- 去水印

@app.post("/api/tiktok/parse")
def tiktok_parse(payload: dict):
    """解析链接。

    v4.0 第 3 期：改为**异步任务**，立即返回 job_id，进度与结果走 SSE。
    原来（审计第 6 条）是在请求线程里串行解析，链接一多界面就卡死、
    无法取消、也没有进度。现在：
      - 解析在后台线程进行，每解析完一条推一次进度事件
      - 结果放在 JOB_DONE 事件的 data.items 里（失败项也带上原因）
      - 可随时取消
    """
    c = core()
    p = payload or {}
    text = p.get("text", "")
    out_dir = p.get("out_dir", "")

    job = bus.create_job(JobKind.TIKTOK_PARSE, label="解析链接")
    t = threading.Thread(
        target=_parse_worker, args=(c, job, text, out_dir),
        daemon=True, name=f"tk-parse-{job.id}")
    t.start()
    return {"job_id": job.id}


def _parse_worker(c, job, text: str, out_dir: str) -> None:
    """后台解析工作线程。"""
    from watermark.tiktok.parser import parse_input
    job.set_state(JobState.RUNNING)
    try:
        urls = parse_input(text or "")
        if not urls:
            job["message"] = "未识别到有效链接（支持 TikTok / 抖音 分享链接或口令）"
            job.finish(job["message"])
            job.emit(EventKind.STATE, data={"items": [], "count": 0,
                                            "failed": 0, "message": job["message"]})
            return
        downloader = c._get_tk_downloader()
        items, failed = [], []
        total = len(urls)
        job.stage("parse", "解析链接", total=total)
        for i, u in enumerate(urls, 1):
            if job.cancelled:
                job.cancel_finish(f"已取消：解析了 {i-1}/{total} 条")
                return
            job.progress((i - 1) / total, f"正在解析 {i}/{total}")
            try:
                info = downloader.parse_info(u)
            except Exception as exc:
                info = None
                job.log(f"解析异常 {u[:40]}：{exc}", level="error")
            if info is None:
                reason = ""
                try:
                    reason = downloader.last_error(u) or "解析失败"
                except Exception:
                    reason = "解析失败"
                failed.append({"index": i, "url": u, "reason": reason[:120]})
                job.log(f"✗ #{i} {reason[:80]}", level="error")
            else:
                d = c.video_info_dict(info)
                d["index"] = i
                d["url"] = u
                items.append(d)
                job.log(f"✓ #{i} {d['title'][:40]}", level="ok")
            job.emit(EventKind.ITEM,
                     frac=i / total,
                     message=f"解析 {i}/{total}",
                     data={"index": i, "total": total})
            job["done"] = i
        # 回填「解析结果」供下载使用（沿用原设计的全局 map，保持与旧行为一致）
        infos, options, options_map = [], [], {}
        for d in items:
            try:
                infos.append(downloader.parse_info(d["url"]))
            except Exception:
                infos.append(None)
        for d, info in zip(items, infos):
            i = d["index"]
            label = f"{i}. {d['title'][:30]}"
            if d["kind"] == "images" and d["image_count"] > 1:
                for j in range(1, d["image_count"] + 1):
                    lab = f"{i}-{j}. {d['title'][:24]} 第{j}/{d['image_count']}张"
                    options.append(lab)
                    options_map[lab] = (i, j)
            else:
                options.append(label)
                options_map[label] = (i, None)
        with c._parse_lock:
            c._parsed_infos.clear()
            c._parsed_infos.extend(infos)
            c._parsed_options.clear()
            c._parsed_options.extend(options)
            c._parsed_options_map.clear()
            c._parsed_options_map.update(options_map)
        msg = (f"识别 {total} 条链接，成功 {len(items)} 条，失败 {len(failed)} 条。"
               f"（TikTok 走 tikwm，抖音走本地 a_bogus 解析）")
        # v4.0.2 关键修复：必须先推结果、再 job.finish()。
        # 原来顺序反了 —— finish() 会让前端关掉 SSE 连接，
        # 之后发出的这条 STATE 事件前端根本收不到，
        # 表现就是「解析完成但没有结果、下载按钮一直是禁用状态」。
        job.emit(EventKind.STATE, data={
            "items": items, "count": len(items), "failed": failed,
            "urls": total, "choices": options, "message": msg,
        })
        job.finish(msg)
    except Exception as e:
        log.exception("解析线程异常")
        job.emit(EventKind.STATE, data={"items": [], "count": 0,
                                        "failed": [], "message": f"解析失败：{e}"})
        job.fail(f"解析失败：{e}")


@app.post("/api/tiktok/download")
def tiktok_download(payload: dict):
    """启动下载任务，立即返回 job_id；进度走 SSE。"""
    c = core()
    p = payload or {}
    if not c.tk_worker_begin():
        raise HTTPException(409, "已有下载任务在运行")

    selected = p.get("selected") or []
    sel_idx = p.get("sel_idx", "")
    out_dir = p.get("out_dir", "")
    auto_hd = bool(p.get("auto_hd"))

    c._tk_cancel.clear()
    c._tk_pause.set()
    job = bus.create_job(JobKind.TIKTOK_DOWNLOAD, label="去水印下载",
                         cancel=c._tk_cancel, pause=c._tk_pause)

    def _run_tk():
        try:
            c._tk_download_worker(selected, sel_idx, out_dir, job, auto_hd)
        finally:
            c.tk_worker_end()

    t = threading.Thread(target=_run_tk, daemon=True, name=f"tk-dl-{job.id}")
    try:
        t.start()
    except Exception:
        c.tk_worker_end()          # 线程没起来也要把位置让出来
        raise
    return {"job_id": job.id}


@app.get("/api/tiktok/history")
def tiktok_history():
    return {"rows": core().do_load_history()}


@app.post("/api/tiktok/history/clear")
def tiktok_history_clear():
    return {"message": core().do_clear_history()}


@app.get("/api/tiktok/recent")
def tiktok_recent():
    files, msg = core().recent_files_info()
    return {"files": files, "message": msg}


@app.post("/api/tiktok/open-dir")
def tiktok_open_dir():
    return {"message": core().do_open_tk_dir()}


@app.get("/api/tiktok/settings")
def tiktok_settings_get():
    """去水印模块的设置（保存目录 / 并发 / 重试 / 画质 / 代理）。

    这些值原本只能靠 `/api/tiktok/parse|download` 的 `out_dir` 参数间接改写，
    界面上没有入口 —— 用户无法指定素材落盘位置。
    另外返回 `default_dir` 供前端「恢复默认目录」使用（去水印与高清同一个默认根）。
    """
    from watermark.core.config import DEFAULT_OUTPUT_ROOT

    out_root, concurrent, retries, quality, proxy = core().tk_settings_values()
    return {"values": {"output_dir": out_root, "max_concurrent": concurrent,
                       "max_retries": retries, "quality": quality,
                       "proxy": proxy},
            "default_dir": str(DEFAULT_OUTPUT_ROOT)}


@app.post("/api/tiktok/settings")
def tiktok_settings_set(payload: dict):
    """保存去水印设置。会顺带把新目录加入允许访问白名单并重建下载器。"""
    c = core()
    cur = c.tk_settings_values()
    src = payload or {}
    args = (src.get("output_dir", cur[0]), src.get("max_concurrent", cur[1]),
            src.get("max_retries", cur[2]), src.get("quality", cur[3]),
            src.get("proxy", cur[4]))
    msg = c.tk_settings_save(*args)
    # 下载目录可能变了：白名单必须跟着换，否则新目录里的文件 /api/file 一直 403
    refresh_allowed_paths()
    return {"message": msg}


# ----------------------------------------------------------------- 系统

@app.post("/api/system/pick-folder")
def system_pick_folder(payload: dict):
    """弹原生「选择文件夹」对话框，返回绝对路径。

    用独立子进程实现（见 server/folder_picker.py 的说明）：
    原生窗口模式与浏览器模式都能用，不依赖 window.pywebview。
    用户取消返回 `{"path": "", "cancelled": true}`，不算错误。
    """
    from server.folder_picker import pick_folder

    initial = str((payload or {}).get("initial") or "")
    path, err = pick_folder(initial)
    if err:
        raise HTTPException(500, err)
    return {"path": path, "cancelled": not path}


# ----------------------------------------------------------------- 日志查看
# 只暴露「本程序自己写出来的日志」，且**不接收任意路径** ——
# 前端的 file 参数只是一个 key，必须能在下面这份白名单里命中，
# 所以不存在 `../../` 之类的目录穿越面。

_LINE_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")
_LEVEL_ALIAS = {"WARN": "WARNING", "FATAL": "CRITICAL"}


def _log_files() -> "list[dict]":
    """可读取的日志文件清单（每次调用现算，文件轮转后依然正确）。"""
    out = []
    seen = set()

    def add(path: Path, label: str) -> None:
        try:
            rp = path.resolve()
            if rp in seen or not rp.is_file():
                return
            st = rp.stat()
            rel = rp.relative_to(_paths.APP_ROOT)
        except (OSError, ValueError):
            return
        seen.add(rp)
        out.append({
            "key": rel.as_posix(),
            "name": label,
            "path": str(rp),
            "size": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })

    # 1) 统一日志：全应用根 logger（采集内核 / 服务层都写这里）
    add(_paths.LOGS_DIR / "app.log", "统一日志 app.log")
    # 2) 按日滚动的历史日志（保留天数由 log_keep_days 控制）
    try:
        for f in sorted(_paths.LOGS_DIR.glob("*.log"), reverse=True)[:40]:
            add(f, f.name)
    except OSError:
        pass
    # 3) 素材制作侧的服务日志（去水印 / 高清的 RotatingFileHandler）
    add(_paths.APP_LOG_PATH, "素材制作 app.log")
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _resolve_log(key: str) -> "Optional[Path]":
    """key → 白名单内的真实文件路径；命中不了返回 None（不是「按路径打开」）。"""
    files = _log_files()
    if not files:
        return None
    if not key:
        for f in files:
            if f["key"] == "logs/app.log":
                return Path(f["path"])
        return Path(files[0]["path"])
    for f in files:
        if f["key"] == key or f["name"] == key:
            return Path(f["path"])
    return None


def _tail_lines(path: Path, n: int) -> "list[str]":
    """读取文件尾部 n 行：只读末尾一小段，大日志也不全量载入内存。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    block = min(size, max(96 * 1024, n * 240))
    with open(path, "rb") as fh:
        if size > block:
            fh.seek(size - block)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # 从中间截断时首行大概率是半行，丢掉
    if size > block and lines:
        lines = lines[1:]
    return lines[-n:] if n > 0 else lines


def _line_level(line: str) -> str:
    """从日志行里认出级别（只看行首一小段，避免正文里的单词误判）。"""
    m = _LINE_LEVEL_RE.search(line[:96])
    if not m:
        return "OTHER"
    return _LEVEL_ALIAS.get(m.group(1), m.group(1))


@app.get("/api/system/logs")
def system_logs(file: str = "", lines: int = 500, level: str = "", query: str = ""):
    """读取日志尾部 + 清单。

    这是个**纯读**端点（日志文件不是业务数据），可放心暴露给前端。
    """
    files = _log_files()
    p = _resolve_log(file)
    if p is None:
        return {"files": files, "file": "", "lines": [], "shown": 0,
                "read": 0, "path": "", "size": 0,
                "message": "没有找到日志文件（程序运行后才有内容）"}

    n = max(50, min(5000, int(lines or 500)))
    raw = _tail_lines(p, n)
    want = (level or "").strip().upper()
    kw = (query or "").strip().lower()
    rows = []
    for line in raw:
        lv = _line_level(line)
        if want and want != "ALL" and lv != want:
            continue
        if kw and kw not in line.lower():
            continue
        rows.append({"level": lv, "text": line})
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return {
        "files": files,
        "file": p.relative_to(_paths.APP_ROOT).as_posix(),
        "path": str(p),
        "size": size,
        "lines": rows,
        "read": len(raw),
        "shown": len(rows),
        "truncated": size > max(96 * 1024, n * 240),
    }


@app.post("/api/system/open-log-dir")
def system_open_log_dir():
    """在资源管理器里打开日志目录（失败不影响查看，前端会提示路径）。"""
    d = _paths.LOGS_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(500, f"无法创建日志目录：{exc}")
    if hasattr(os, "startfile"):
        try:
            os.startfile(str(d))       # noqa: S606 — Windows 专用，与其它打开目录一致
        except OSError as exc:
            raise HTTPException(500, f"打开目录失败：{exc}")
    return {"ok": True, "path": str(d)}


@app.post("/api/system/notify")
def system_notify(payload: dict):
    """发一条采集通知。body: {title, message, kind, sound, timeout_ms, desktop}

    对应原桌面程序 `app/ui/notify.py` 的能力 —— 采集结束时提醒用户，
    这样切到别的窗口也不会漏掉「采集已完成」。**不阻塞**，立即返回。

    `desktop=True`（默认）弹**桌面右下角浮层**；`desktop=False` 只播提示音。
    窗口在前台时卡片由前端画在**程序界面**右下角（见 frontend/js/app.js 的
    Notify 组件），此时必须传 desktop=False —— 否则通知会"跑到程序外面"，
    这正是 2026-10-10 用户反馈的那个 bug。
    """
    from server.notifier import notify

    p = payload or {}
    title = str(p.get("title") or "TK 素材工坊")[:80]
    message = str(p.get("message") or "")[:300]
    kind = str(p.get("kind") or "info")[:16]
    if kind not in ("info", "ok", "warn", "error"):
        kind = "info"
    try:
        timeout_ms = int(p.get("timeout_ms") or 9000)
    except (TypeError, ValueError):
        timeout_ms = 9000
    sound = bool(p.get("sound", True))
    desktop = bool(p.get("desktop", True))

    ok, err = notify(title, message, kind, timeout_ms=timeout_ms,
                     sound=sound, desktop=desktop)
    return {"ok": ok, "error": err}


# ----------------------------------------------------------------- 高清

@app.get("/api/hd/models")
def hd_models():
    c = core()
    return {
        # v4.0 修复：给出 {value,label} 结构，前端才能把「key」正确提交回来
        "models": c.selectable_model_options(),
        "scales": c.selectable_scale_options(),
        "sides": c.selectable_side_options(),
        # 兼容字段（旧调用方）
        "choices": c.MODEL_CHOICES,
        "table": c.models_table(),
        "headers": c.MODEL_TABLE_HEADERS,
        "cards": c.models_cards_html(),
        "presets": [{"key": p["key"], "label": p["label"], "icon": p.get("icon", ""),
                     "desc": p.get("desc", "")} for p in c.PRESETS],
    }


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(default=[])):
    """上传文件到工作目录，返回服务器端路径（供 HD 处理使用）。"""
    saved = []
    for f in files or []:
        name = Path(f.filename or "file").name
        # 防目录穿越 + 去重
        name = re.sub(r'[\\/:*?"<>|]+', "_", name)
        dest = UPLOAD_DIR / f"{int(time.time()*1000)}_{name}"
        data = await f.read()
        dest.write_bytes(data)
        saved.append(str(dest))
    return {"files": saved}


def _norm_side(value) -> str:
    """把视频档位规整成引擎期望的形式（'sharp' 或整数字符串）。

    前端可能传来 "sharp" / 1920 / "1920" / "1920（推荐）"，
    引擎只认 "sharp" 与纯数字，这里统一。
    """
    s = str(value if value is not None else "").strip()
    if s.lower().startswith("sharp") or "锐化" in s:
        return "sharp"
    m = re.search(r"\d+", s)
    return m.group(0) if m else "1920"


@app.post("/api/hd/from-recent")
def hd_from_recent(payload: dict = None):
    """把「最近下载的素材」转成服务器端可用路径（v4.0 修复）。

    这些文件本来就在服务器磁盘上，完全不需要再上传一遍。
    原来前端只能把目录名填进输入框，用户还得手动翻文件夹 —— 很别扭。
    """
    c = core()
    files, msg = c.recent_files_info()
    from server.media import outputs_payload
    items = outputs_payload(files)
    # 只挑图片/视频，并区分类型
    want = str((payload or {}).get("kind") or "").strip()
    if want == "image":
        items = [x for x in items if x["is_image"]]
    elif want == "video":
        items = [x for x in items if x["is_video"]]
    return {
        "paths": [x["path"] for x in items],
        "items": items,
        "message": msg or (f"已导入最近下载 {len(items)} 个素材" if items
                           else "暂无最近下载的素材"),
    }


@app.post("/api/hd/start")
def hd_start(payload: dict):
    """启动高清任务（图片/视频/混合），立即返回 job_id。"""
    c = core()
    p = payload or {}
    mode = p.get("mode", "both")            # image | video | both
    files = p.get("files") or []
    folder = p.get("folder", "")
    # v4.0 修复：模型值统一解析成合法 key。
    # 之前前端把「显示名,key」整串提交上来，导致 MODELS[key] KeyError，
    # 高清处理 100% 失败（报错信息里能看到那串名字）。
    img_model = c.resolve_model_key(
        p.get("image_model") or _load_cfg().get("image_model"), "image")
    video_model = c.resolve_model_key(
        p.get("video_model") or _load_cfg().get("video_model"), "video")
    img_scale = p.get("img_scale", "自动（推荐）")
    vid_scale = p.get("vid_scale", "自动（推荐）")
    # 视频档位：接受 "sharp" / 1920 / "1920" / "1920（推荐）"
    vid_side = _norm_side(p.get("vid_side", 1920))
    face = bool(p.get("face", False))
    interpolate = bool(p.get("interpolate", False))
    out_root = p.get("out_root") or _load_cfg().get("output_root")
    img_max = p.get("img_max", "4096（推荐）")

    if not files and not str(folder).strip():
        raise HTTPException(422, "请先选择文件或填写文件夹路径")

    if not c.hd_worker_begin():
        raise HTTPException(409, "已有高清任务在运行")

    c.CANCEL.clear()
    c._hd_pause.set()
    kind = {"image": JobKind.HD_IMAGE, "video": JobKind.HD_VIDEO}.get(mode, JobKind.HD_MIXED)
    job = bus.create_job(kind, label=f"高清-{mode}",
                         cancel=c.CANCEL, pause=c._hd_pause)

    def _run_hd():
        # 无论正常结束、取消还是抛异常，都要把「高清处理」位置让出来，
        # 否则一次失败会让后续任务全部被 409 挡住。
        try:
            c._hd_worker(files, folder, img_model, video_model, img_scale,
                         vid_scale, vid_side, face, interpolate, out_root,
                         img_max, job, mode)
        finally:
            c.hd_worker_end()

    t = threading.Thread(target=_run_hd, daemon=True, name=f"hd-{job.id}")
    try:
        t.start()
    except Exception:
        c.hd_worker_end()
        raise
    return {"job_id": job.id}


@app.post("/api/hd/preset")
def hd_preset(payload: dict):
    """套用场景预设，返回应该填进表单的参数。"""
    c = core()
    p = payload or {}
    key = p.get("key")
    kind = p.get("kind", "image")
    if kind == "image":
        model, scale, face, mx = c._apply_preset_image(
            key, p.get("image_model"), p.get("img_scale"),
            p.get("face"), p.get("img_max"))
        return {"image_model": model, "img_scale": scale,
                "face": face, "img_max": mx}
    model, scale, side, interp = c._apply_preset_video(
        key, p.get("video_model"), p.get("vid_scale"),
        p.get("vid_side"), p.get("interpolate"))
    return {"video_model": model, "vid_scale": scale,
            "vid_side": side, "interpolate": interp}


# ----------------------------------------------------------------- 模型/系统

@app.get("/api/models/status")
def models_status():
    """模型卡片（结构化数据 + HTML 两种形式）。"""
    c = core()
    return {
        "items": c.model_cards_items(),
        "html": c.models_cards_html(),
        "table": c.models_table(),
        "headers": c.MODEL_TABLE_HEADERS,
        "categories": sorted({m.get("category", "") for m in c.model_cards_items()
                              if m.get("category")}),
    }


@app.get("/api/models/presets")
def models_presets():
    """预设清单（结构化）。"""
    c = core()
    out = []
    for p in c.PRESETS:
        item = {"key": p.get("key"), "label": p.get("label"),
                "icon": p.get("icon", ""), "desc": p.get("desc", ""),
                "kind": p.get("kind", "both")}
        try:
            item["image"] = c.presets.for_kind(p["key"], "image")
        except Exception:
            item["image"] = None
        try:
            item["video"] = c.presets.for_kind(p["key"], "video")
        except Exception:
            item["video"] = None
        out.append(item)
    return {"presets": out, "html": c.presets_html()}


@app.post("/api/models/download")
def models_download(payload: dict):
    """下载模型权重。放到后台任务里，避免请求卡住。"""
    c = core()
    p = payload or {}
    key = p.get("key")
    job = bus.create_job(JobKind.MODEL_DOWNLOAD, label=f"下载模型 {key or '全部'}")

    def _run():
        job.set_state(JobState.RUNNING)
        job.stage("download", "下载权重")
        try:
            msg = c.download_all() if not key else c.download_one(key)
            job.finish(msg or "下载完成")
        except Exception as exc:
            job.fail(f"下载失败：{exc}")

    threading.Thread(target=_run, daemon=True, name=f"dl-{job.id}").start()
    return {"job_id": job.id}


@app.get("/api/library")
def library(limit: int = 300):
    """素材库：本地输出目录里的图片/视频。"""
    from server.media import outputs_payload
    c = core()
    data = {"rows": [], "gallery": [], "items": []}
    try:
        data["rows"] = c.library_list(limit)
    except Exception as exc:
        log.warning(f"素材库读取失败: {exc}")
    try:
        data["gallery"] = c.library_gallery()
    except Exception as exc:
        log.warning(f"素材库画廊失败: {exc}")
    # library_gallery() 返回的是路径列表；转成含受控 URL 的结构给前端
    try:
        paths = [r for r in (data.get("gallery") or []) if isinstance(r, (str, Path))]
        data["items"] = outputs_payload(paths)
    except Exception:
        data["items"] = []
    return data


@app.get("/api/library/roots")
def library_roots():
    try:
        return {"roots": [str(p) for p in core().library_roots()]}
    except Exception as exc:
        return {"roots": [], "error": str(exc)}


@app.post("/api/tools/{name}")
def run_tool(name: str, payload: dict):
    c = core()
    p = payload or {}
    if name == "gif":
        return {"message": c.video_to_gif(p.get("video"), p.get("fps", 12),
                                          p.get("scale", 480))}
    if name == "frames":
        return {"message": c.video_extract_frames(p.get("video"), p.get("fps", 1),
                                                  p.get("out_dir", ""))}
    if name == "rename":
        return {"message": c.batch_rename(p.get("pattern"), p.get("start", 1))}
    if name == "diagnose":
        return {"message": c.diag_network()}
    if name == "cleanup":
        return {"message": c.cleanup_tmp()}
    raise HTTPException(404, f"未知工具: {name}")


@app.get("/api/env")
def env():
    return {"text": core().env_text()}


@app.get("/api/changelog")
def changelog():
    c = core()
    return {"markdown": c.changelog_markdown()}


@app.get("/api/lan-info")
def lan_info():
    """手机访问信息（v4.0.1）。

    返回本机局域网 IP、实际端口与是否已对外监听。
    端口是动态的，所以必须从后端取，不能让前端猜。
    """
    import socket as _socket

    cfg = _load_cfg()
    enabled = bool(cfg.get("lan_access", False))
    port = 0
    host = ""
    rt = _runtime_json()
    if rt:
        port = int(rt.get("port") or 0)
        host = str(rt.get("host") or "")
    ip = ""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    https_port = int(rt.get("https_port") or 0)
    return {
        "enabled": enabled,
        "listening_lan": host == "0.0.0.0",
        "host": host,
        "port": port,
        "ip": ip,
        "url": f"http://{ip}:{port}/" if (ip and port) else "",
        # v1.5.0：HTTPS 是否正在监听（手机「保存到相册」需要它）
        "https_enabled": bool(rt.get("https_enabled")),
        "https_port": https_port,
        "https_url": f"https://{ip}:{https_port}/" if (ip and https_port) else "",
        "note": ("已开启：手机连同一个 WiFi 后打开上面的地址即可使用，无需登录。"
                 if enabled else
                 "未开启：当前只允许本机访问。勾选「允许局域网访问」保存后重启生效。"),
    }


# ------------------------------------------------------- HTTPS（v1.5.0）

def _https_state() -> dict:
    """HTTPS 的「配置想要什么」与「进程实际提供了什么」两套状态。

    分开返回是刻意的：开关改完必须重启才生效，界面要能如实显示
    「已勾选但还没生效」，否则用户会以为点一下就好了。
    """
    rt = _runtime_json()
    desired = bool(_load_cfg().get("https_enabled", False))
    running = bool(rt.get("https_enabled"))
    port = int(rt.get("https_port") or 0)
    st = _tls.cert_info()
    ip = st.get("lan_ip") or ""
    http_port = int(rt.get("port") or 0)
    return {
        "desired": desired,
        "running": running,
        "need_restart": desired != running,
        "port": port,
        "http_port": http_port,
        "ip": ip,
        "local_ips": st.get("local_ips") or [],
        "http_url": f"http://{ip}:{http_port}/" if (ip and http_port) else "",
        "https_url": f"https://{ip}:{port}/" if (ip and port) else "",
        "lan_access": bool(_load_cfg().get("lan_access", False)),
        "cert": {
            "exists": st.get("exists"),
            "ok": st.get("ok"),
            "reason": st.get("reason"),
            "fingerprint_short": st.get("fingerprint_short", ""),
            "fingerprint_sha256": st.get("fingerprint_sha256", ""),
            "not_before": st.get("not_before", ""),
            "not_after": st.get("not_after", ""),
            "days_left": st.get("days_left", 0),
            "key_bits": st.get("key_bits", 0),
            "hosts": st.get("hosts") or [],
            "ip_covered": st.get("ip_covered"),
            "missed_ips": st.get("missed_ips") or [],
            "missing": not bool(st.get("exists")),
        },
        "download_url": "/api/https/cert",
    }


@app.get("/api/https-info")
def https_info():
    """HTTPS 状态（只读，不产生任何副作用）。"""
    return _https_state()


@app.post("/api/https-settings")
def https_settings(payload: dict):
    """开关 HTTPS。

    ⚠️ 与设置页其它项一样**写配置、重启生效** —— 正在跑的 uvicorn 不会
    因为改配置就换监听方式。返回值里带 ``need_restart`` 供界面提示。
    """
    src = payload or {}
    if "enabled" not in src:
        # 铁律 6：写操作不允许「参数缺省就按某个默认值写下去」
        raise HTTPException(400, "缺少 enabled 参数")
    enabled = bool(src.get("enabled"))
    import common.config as _cc
    _cc.update("watermark", {"https_enabled": enabled})
    st = _https_state()
    st["message"] = ("已开启 HTTPS（保存后重启程序生效）"
                     if enabled else "已关闭 HTTPS（重启程序生效）")
    return st


@app.post("/api/https/regenerate")
def https_regenerate():
    """重新签发自签证书。

    什么时候要用：换了网络 / 换了网卡导致局域网 IP 变了、证书快过期、
    或者证书文件被替换过。**重启才生效**，且手机需要重新安装新证书
    （旧证书在新证书生效后就对不上了）。
    """
    try:
        _tls.generate()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"重新生成证书失败: {exc}")
        raise HTTPException(500, f"生成证书失败：{exc}") from exc
    st = _https_state()
    st["message"] = "证书已重新生成 —— 重启程序生效，手机需重新安装一次证书"
    return st


@app.get("/api/https/cert")
def https_cert():
    """下载证书（.crt），供手机安装。

    走 HTTP 也能下 —— 手机安装证书本来就是「先装后用」的顺序，
    所以这个端点不要求当前连接已是 HTTPS。
    """
    cert = _tls.CERT_PATH
    if not cert.is_file():
        raise HTTPException(404, "还没有证书：请先在设置里勾选「启用 HTTPS」并重启程序")
    return FileResponse(
        str(cert),
        media_type="application/x-x509-ca-cert",
        filename="TK素材工坊.crt",
        content_disposition_type="attachment",
    )


@app.get("/api/output-dir")
def output_dir():
    c = core()
    c.open_output()
    return {"ok": True}


@app.post("/api/shutdown")
def shutdown():
    c = core()
    threading.Timer(0.5, lambda: c.request_shutdown("API 请求")).start()
    return {"ok": True}


# ----------------------------------------------------------------- 静态前端

def _index_response() -> HTMLResponse:
    idx = FRONTEND_DIR / "index.html"
    if not idx.exists():
        return HTMLResponse(
            "<html><body style='background:#0e0e13;color:#f9fafb;"
            "font-family:Segoe UI,sans-serif;padding:24px'>"
            "<h2>前端尚未构建</h2>"
            "<p>API 已就绪，请访问 <a style='color:#25F4EE' href='/api/ping'>/api/ping</a> "
            "验证服务。</p></body></html>",
            status_code=200)
    return HTMLResponse(idx.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def index():
    return _index_response()


# 结果文件下载：只允许白名单目录，防止任意文件读取
_ALLOW_PREFIXES: list[Path] = []
# 白名单依赖的配置文件 → 它们被改动（用户换了输出目录）就要重刷，见 _is_allowed
_ALLOW_STAMP: float = -1.0
_ALLOW_SOURCES: list[Path] = []
# 测试用：断言「配置改了白名单会自动重刷」时替换配置来源
_allow_sources_override: list[Path] = []


def _allow_config_paths() -> list[Path]:
    """白名单依赖的配置文件：水印侧 config.json + 去水印侧 config.json。"""
    out: list[Path] = []
    try:
        import watermark.core.config as _c
        out.append(Path(_c.CONFIG_PATH))
    except Exception:
        pass
    try:
        from watermark.tiktok import config as _tk_cfg
        out.append(Path(_tk_cfg.CONFIG_FILE))
    except Exception:
        pass
    return _allow_sources_override or out


def _allow_stamp() -> float:
    """取所有来源配置文件的最后修改时间（没有文件算 0）。"""
    stamp = 0.0
    for p in _ALLOW_SOURCES:
        try:
            stamp = max(stamp, Path(p).stat().st_mtime)
        except OSError:
            continue
    return stamp


def refresh_allowed_paths() -> None:
    """刷新可下载目录白名单（输出目录 / 下载目录 / 上传目录 / 项目根）。

    白名单是缓存的，所以配置一改就得重刷：用户把下载/输出目录换到别处以后，
    新目录里的文件会一直 403（结果区看不到图、点下载报错），非得重启程序才好。
    这属于「设置改了不生效」的同一类缺陷，2026-10-10 复盘时发现。
    """
    global _ALLOW_PREFIXES, _ALLOW_STAMP, _ALLOW_SOURCES
    cfg = _load_cfg()
    from watermark.tiktok.config import config as tk_config
    cands = [PROJECT_ROOT, UPLOAD_DIR,
             Path(str(cfg.get("output_root") or PROJECT_ROOT / "output")),
             Path(str(getattr(tk_config, "output_dir", PROJECT_ROOT)))]
    out = []
    for p in cands:
        try:
            out.append(Path(p).resolve())
        except Exception:
            continue
    _ALLOW_PREFIXES = out
    _ALLOW_SOURCES = _allow_config_paths()
    _ALLOW_STAMP = _allow_stamp()


def _is_allowed(p: Path) -> bool:
    # 配置文件的修改时间一变就重刷：换了输出目录之后不用重启程序
    if not _ALLOW_PREFIXES or _allow_stamp() != _ALLOW_STAMP:
        refresh_allowed_paths()
    try:
        rp = p.resolve()
    except Exception:
        return False
    for base in _ALLOW_PREFIXES:
        try:
            rp.relative_to(base)
            return True
        except ValueError:
            continue
    return False


@app.get("/api/file")
def get_file(path: str, inline: int = 0):
    """受控地提供结果文件（图片/视频/ZIP）给前端显示或下载。

    `inline=1` 时用 `Content-Disposition: inline`：浏览器内直接渲染，
    供手机端「打开原图 → 长按 → 存储到照片」使用（详见 media.preview_url）。
    默认仍是 attachment（桌面端「打开」= 下载），行为不变。
    """
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    if not _is_allowed(p):
        raise HTTPException(403, "路径不在允许范围内")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=mime, filename=p.name,
                        content_disposition_type="inline" if inline else "attachment")


def file_url(path: str) -> str:
    """把本地路径转成前端可直接使用的受控 URL。"""
    from server.media import file_url as _fu
    return _fu(path)


def mount_frontend() -> None:
    """把 frontend/ 挂成静态资源（如果存在）。"""
    if FRONTEND_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ----------------------------------------------------------------- 局域网

# v4.0.1：按要求**移除**账号密码登录。
# 开启局域网后，同一网络内的设备直接访问，不再需要认证。
# 安全边界因此完全落在「是否开启局域网访问」这一个开关上：
#   - 关闭（默认）：只监听 127.0.0.1，外部无法访问；
#   - 开启：监听 0.0.0.0，同一 WiFi 下任何设备都能打开、解析、下载、读取结果文件。
# 请只在可信网络（比如家里）里开启。

lan_open = False


def set_lan_open(enabled: bool) -> None:
    """记录当前是否为对外监听（仅用于日志与前端提示展示）。"""
    global lan_open
    lan_open = bool(enabled)
    if lan_open:
        log.info("已开启局域网访问（无密码，同一网络内的设备可直接访问）")


mount_frontend()
refresh_allowed_paths()
