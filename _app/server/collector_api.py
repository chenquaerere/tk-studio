"""采集子系统 HTTP API（`/api/collector/*`）。

定位
----
把迁移后的采集内核（`_app/collector/`）接到 FastAPI 上，与去水印 / 高清
**共用同一个服务进程、同一个 JobStore、同一条 SSE 通道**。
前端的 13 个视图里，采集侧的 7 个视图全部由本模块提供数据。

设计约定
--------
1. **长任务一律异步**：采集 / 聊天搜索都立即返回 `job_id`，进度走
   `/api/jobs/{job_id}/events`（SSE）—— 复用去水印已建好的基础设施，
   不再像旧版 tkinter 那样用 `after` 泵轮询。
2. **服务容器是进程级单例**：`CollectorServices` 惰性构建（首次请求才打开数据库），
   这样"未使用采集功能就不加载 Playwright"。
3. **不做跨运行去重等业务判断**：本层只做参数校验与序列化，
   所有采集规则一律由内核决定（见 collector 包注释中的五条铁律）。
4. **导出/下载路径受控**：只允许当前导出目录与 `links_log/` 下的文件；
   导出目录可在设置页改（`export_dir`），下载白名单跟着一起变。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from common import config as ucfg
from common import paths as _paths
from common.version import APP_VERSION

from collector.bootstrap import CollectorServices
from collector.chat.models import CHAT_TYPE_FRIEND, CHAT_TYPE_GROUP, ChatCandidate, ChatTarget
from collector.core import regions as region_util
from collector.core.date_resolver import DateResolver
from collector.core.models import Account, status_message
from server.jobs import STORE
from server.state import EventKind, JobKind, JobState

router = APIRouter(prefix="/api/collector", tags=["collector"])

# 前端「只看未分类」的筛选哨兵值。
# 为什么不复用空串：HTTP query 里 `region=` 与「不传 region」无法区分，
# 需要一个显式值来表达"我要筛未分类"。
REGION_UNSPECIFIED = "__none__"

# ------------------------------------------------------------------ 服务容器

_ctx: Optional[CollectorServices] = None
_ctx_lock = threading.Lock()


def ctx() -> CollectorServices:
    """进程级采集服务容器（惰性构建）。

    auto_backup=True：沿用原程序的「每日首次启动自动备份数据库」，
    依据 settings.last_backup_date 去重，不会重复备份。
    """
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            _ctx = CollectorServices(auto_backup=True)
        return _ctx


def shutdown() -> None:
    """关停时释放浏览器与数据库（由 desktop.py 调用）。"""
    global _ctx
    with _ctx_lock:
        if _ctx is not None:
            try:
                _ctx.close()
            except Exception:
                pass
            _ctx = None


# --------------------------------------------------- 可编辑目录（导出 / 日志）

def _setting_dir(key: str, default: Path) -> Path:
    """设置页里可编辑的目录：留空用默认目录，相对路径按程序根解析。

    为什么不像 db_path 那样只在启动时解析一次：
    「导出目录」改完要立刻能用（不然又成了"设置了不生效"），
    所以每次用到都重新读配置；「日志目录」是启动期初始化的，改完需重启
    （bootstrap 里读同一个键，前端已注明）。
    """
    try:
        raw = ctx().config.get(key)
    except Exception:  # noqa: BLE001 —— 容器不可用时退回默认目录
        raw = None
    if raw is None or not str(raw).strip():
        return default
    return _paths.resolve(raw)


def _exports_dir() -> Path:
    """当前生效的导出目录（默认 exports/）。"""
    return _setting_dir("export_dir", _paths.EXPORTS_DIR)


def _logs_dir() -> Path:
    """当前生效的日志目录（默认 logs/）。"""
    return _setting_dir("log_dir", _paths.LOGS_DIR)


# (设置键, 界面名称, 默认目录) —— 设置页里可编辑、且保存时要先建好的目录。
_DIR_SETTING_KEYS = (("export_dir", "导出目录", _paths.EXPORTS_DIR),
                     ("log_dir", "日志目录", _paths.LOGS_DIR))


def _ensure_setting_dirs(values: dict) -> None:
    """校验并创建设置里的可编辑目录：不可用时抛 400，绝不「半保存」。

    先验证再落盘，避免出现「设置已保存，但导出时才发现目录用不了」。
    """
    for key, label, default in _DIR_SETTING_KEYS:
        if key not in values:
            continue
        try:
            _paths.resolve(values.get(key) or default).mkdir(parents=True,
                                                             exist_ok=True)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"{label}无法使用：{exc}")


# ------------------------------------------------------------------ 序列化

def _row(r: Any) -> dict:
    try:
        return dict(r)
    except Exception:
        return {}


def _account_dict(r: Any) -> dict:
    d = _row(r)
    region = region_util.normalize_region(d.get("region"))
    return {
        "account_id": d.get("account_id", ""),
        "username": d.get("username", ""),
        "display_name": d.get("display_name", "") or "",
        "region": region,
        "region_display": region_util.region_display(region),
        "profile_url": d.get("profile_url", ""),
        "collect_count": int(d.get("collect_count") or 0),
        "enabled": bool(d.get("enabled", 1)),
        "login_status": d.get("login_status") or "unknown",
        "remark": d.get("remark", "") or "",
        "last_collect_time": d.get("last_collect_time") or "",
        "last_collect_result": status_message(
            d.get("last_collect_result") or "") if d.get("last_collect_result") else "",
    }


def _video_dict(r: Any) -> dict:
    d = _row(r)
    return {
        "video_id": d.get("video_id", ""),
        "account_id": d.get("account_id", ""),
        "username": d.get("username", ""),
        "video_url": d.get("video_url", ""),
        "publish_time": d.get("publish_time") or "",
        "publish_date": d.get("publish_date") or "",
        "collect_time": d.get("first_collect_time") or "",
        "task_id": d.get("first_task_id") or "",
    }


def _candidate_dict(c: ChatCandidate) -> dict:
    return {
        "chat_type": c.chat_type,
        "type_label": "好友" if c.chat_type == CHAT_TYPE_FRIEND else "群聊",
        "name": c.name,
        "handle": c.handle,
        "uid": c.uid,
        "sec_uid": c.sec_uid,
        "conversation_id": c.conversation_id,
        "subtitle": c.subtitle,
        "display": c.display,
        "stable_key": c.stable_key,
    }


def _target_dict(t: ChatTarget) -> dict:
    return {
        "chat_type": t.chat_type,
        "type_label": t.type_label,
        "name": t.name,
        "handle": t.handle,
        "uid": t.uid,
        "sec_uid": t.sec_uid,
        "conversation_id": t.conversation_id,
        "detail_label": t.detail_label,
    }


def _get_account(account_id: str) -> Account:
    """按 account_id 取账号；不存在则 404。"""
    row = ctx().db.get_account(account_id)
    if row is None:
        raise HTTPException(404, f"账号不存在: {account_id}")
    d = _row(row)
    return Account(
        account_id=d.get("account_id", ""),
        username=d.get("username", ""),
        profile_url=d.get("profile_url", ""),
        display_name=d.get("display_name", "") or "",
        remark=d.get("remark", "") or "",
        region=d.get("region", "") or "",
        enabled=bool(d.get("enabled", 1)),
        login_status=d.get("login_status") or "unknown",
        collect_count=int(d.get("collect_count") or 4),
    )


# ------------------------------------------------------------------ 元信息

# 应用内通知卡片的外观边界：宽度 280~720px（更窄标题就折行，更宽会压住页面内容），
# 缩放 0.8~1.6（等比放大字号与内距）。越界一律收敛，避免前端算出畸形卡片。
NOTIFY_CARD_WIDTH_RANGE = (280, 720)
NOTIFY_CARD_SCALE_RANGE = (0.8, 1.6)

# 通知相关的布尔开关：脚本/curl 直接 POST 字符串时必须按语义解析
# （bool("false") == True 是个坑，2026-10-10 一并兜住）
_NOTIFY_BOOL_KEYS = {"notify_sound"}


def _clamp_number(value, low, high, default):
    """把任意输入收敛成 [low, high] 内的数字；无法解析时用默认值。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:                      # NaN
        return default
    return min(high, max(low, n))


def _coerce_setting(key: str, value):
    """把设置值收敛到合法类型/范围。前端已经传对类型，这里兜底脚本直接 POST。"""
    if key in _NOTIFY_BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("", "0", "false", "no", "off")
    if key == "notify_card_width":
        return int(_clamp_number(value, *NOTIFY_CARD_WIDTH_RANGE, 420))
    if key == "notify_card_scale":
        return round(_clamp_number(value, *NOTIFY_CARD_SCALE_RANGE, 1.0), 2)
    return value


@router.get("/meta")
def meta():
    """采集模块的元信息：地区字典、时区、状态文案、默认值。"""
    c = ctx()
    return {
        "regions": region_util.region_options(ctx().db.used_regions()),
        "regions_used": ctx().db.used_regions(),
        "region_stats": [{"region": r, "count": n, "label": region_util.region_display(r)}
                         for r, n in ctx().account_service.region_stats()],
        "region_none": region_util.REGION_NONE,
        "region_unspecified": REGION_UNSPECIFIED,
        "default_collect_count": int(c.config.get("default_collect_count", 4)),
        "timezone": c.timezone(),
        # 采集结束是否播提示音 / 弹系统通知（原桌面版有此开关，默认开）
        "notify_sound": bool(c.config.get("notify_sound", True)),
        # 通知卡片外观（2026-10-10，2026-10-11 收窄）：完成提醒固定画在程序
        # 界面右下角，只有一条通道；可调的只剩卡片宽度/缩放。
        "notify_card_width": int(_clamp_number(
            c.config.get("notify_card_width", 420), *NOTIFY_CARD_WIDTH_RANGE, 420)),
        "notify_card_scale": round(_clamp_number(
            c.config.get("notify_card_scale", 1.0), *NOTIFY_CARD_SCALE_RANGE, 1.0), 2),
        "status_messages": {
            "completed": "完成", "partial": "当日作品不足", "empty": "今日没有发现作品",
            "failed": "失败", "skipped": "已跳过", "pending": "等待中", "running": "采集中",
        },
        "version": APP_VERSION,
    }


# ------------------------------------------------------------------ 仪表盘

@router.get("/overview")
def overview():
    """仪表盘聚合统计。

    ⚠️ **纯只读**：这里只做 COUNT / SELECT，绝不顺手补列、迁移或改设置。
    历史教训：统计端点里做"顺手修复"最容易在用户真实库上造成扰动。
    """
    c = ctx()
    db = c.db
    today = _today(c)
    accounts = [_account_dict(r) for r in db.list_accounts()]
    recent = [_task_dict(r) for r in db.list_tasks(limit=10)]
    unfinished_row = db.find_unfinished_task()
    return {
        "today": today,
        "timezone": c.timezone(),
        "accounts": {
            "total": len(accounts),
            "enabled": sum(1 for a in accounts if a["enabled"]),
            "disabled": sum(1 for a in accounts if not a["enabled"]),
            "missing_nickname": sum(1 for a in accounts if not a["display_name"]),
            "regions": [{"region": r, "count": n, "label": region_util.region_display(r)}
                        for r, n in c.account_service.region_stats()],
        },
        "videos": {
            "total": db.count_videos(),
            "today": db.count_videos_by_publish_date(today),
        },
        "tasks": {
            "total": db.count_tasks(),
            "recent": recent,
            "unfinished": _task_dict(unfinished_row) if unfinished_row is not None else None,
        },
        "chat": {
            "targets": db.count_chat_targets(),
            "links": db.count_chat_links(),
        },
        "session": {
            "collected_this_session": bool(c.collected_this_session),
            "last_task_id": c.last_task_id or "",
        },
    }


# ------------------------------------------------------------------ 账号

@router.get("/accounts")
def list_accounts(region: Optional[str] = None,
                  enabled_only: bool = False,
                  include_disabled: bool = True):
    """账号列表 + 地区统计。

    region 省略 = 全部；传 `__none__` = 只看未分类。
    """
    c = ctx()
    want = None
    if region is not None and region != "":
        want = "" if region == REGION_UNSPECIFIED else region
    rows = c.db.list_accounts(
        enabled_only=(enabled_only and not include_disabled), region=want)
    accounts = [_account_dict(r) for r in rows]
    return {
        "accounts": accounts,
        "total": len(accounts),
        "enabled": sum(1 for a in accounts if a["enabled"]),
        "missing_nickname": sum(1 for a in accounts if not a["display_name"]),
        "stats": [{"region": r, "count": n, "label": region_util.region_display(r)}
                  for r, n in c.account_service.region_stats()],
    }


@router.post("/accounts")
def add_accounts(payload: dict):
    """新增 / 批量导入账号。

    body: {"raws": ["@user", "https://www.tiktok.com/@user", ...],
           "collect_count": 4, "region": "越南", "remark": ""}
    也接受单条 {"raw": "..."}。
    """
    raws: List[str] = []
    if isinstance(payload.get("raws"), list):
        raws = [str(x) for x in payload["raws"]]
    elif payload.get("raw"):
        raws = [str(payload["raw"])]
    raws = [r.strip() for r in raws if str(r).strip()]
    if not raws:
        raise HTTPException(400, "没有可导入的账号")

    count = int(payload.get("collect_count") or ctx().config.get("default_collect_count", 4))
    region = region_util.normalize_region(payload.get("region"))

    results = ctx().account_service.import_many(
        raws, collect_count=count, region=region)
    return {
        "ok": True,
        "added": len(results.get("imported", [])),
        "skipped": len(results.get("skipped", [])),
        "failed": len(results.get("failed", [])),
        "imported": results.get("imported", []),
        "details": (results.get("failed", []) + results.get("skipped", [])),
    }


def _parse_account_file(name: str, data: bytes) -> List[str]:
    """把上传的文件解析成「候选账号字符串」列表。

    解析规则与旧桌面程序一致：**逐行取第一个非空单元格** ——
    「一行一个链接」「CSV / TSV 首列是链接」「Excel 首列是链接」都能吃下。

    ⚠️ 只做**提取**，不做合法性校验 —— 判断统一交给 `import_many`，
    避免两处规则不一致（旧版踩过：UI 层与数据层各判一次，结论不同）。
    """
    ext = Path(str(name or "")).suffix.lower()
    raws: List[str] = []

    if ext in (".xlsx", ".xlsm"):
        try:
            import io

            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        for cell in row:
                            text = str(cell).strip() if cell is not None else ""
                            if text:
                                raws.append(text)
                                break
            finally:
                wb.close()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                400, f"Excel 解析失败：{exc}（旧式 .xls 请先另存为 .xlsx）")
        return raws

    text = data.decode("utf-8-sig", errors="replace")
    for line in text.splitlines():
        for cell in re.split(r"[,\t;]", line):
            cell = cell.strip().strip('"').strip()
            if cell:
                raws.append(cell)
                break
    return raws


@router.post("/accounts/import-file")
async def import_accounts_file(request: Request, name: str = "",
                               region: str = "", collect_count: int = 0,
                               dry_run: bool = False):
    """从上传的文件批量导入账号（文本 / CSV / TSV / Excel）。

    直接收原始字节（body 即文件内容，文件名走 `?name=` 查询参数），
    这样不必引入 multipart 依赖，前端一个 fetch 就能传。

    `dry_run=1` 时**只解析、不落库**，把候选项回传给前端填进文本框 ——
    沿用旧桌面程序「先读取、再由用户点导入」的两步节奏，
    避免选错文件就直接改库。
    """
    data = await request.body()
    if not data:
        raise HTTPException(400, "文件为空")

    raws = [r for r in _parse_account_file(name, data) if r]
    if not raws:
        raise HTTPException(400, "未能从该文件读取到有效账号")

    if dry_run:
        return {"ok": True, "filename": name,
                "parsed": len(raws), "raws": raws[:2000]}

    c = ctx()
    count = int(collect_count or 0) or int(
        c.config.get("default_collect_count", 4))
    results = c.account_service.import_many(
        raws, collect_count=count,
        region=region_util.normalize_region(region))
    return {
        "ok": True,
        "filename": name,
        "parsed": len(raws),
        "added": len(results.get("imported", [])),
        "skipped": len(results.get("skipped", [])),
        "failed": len(results.get("failed", [])),
        "details": (results.get("failed", []) + results.get("skipped", []))[:20],
    }


@router.post("/accounts/refresh-nicknames")
def refresh_nicknames(payload: dict):
    """刷新账号昵称（异步）。只取昵称、不采集作品。

    body: {"account_ids": [...]}  —— 省略 = 刷新全部「昵称待获取」的账号。
    """
    if STORE.find_active(JobKind.COLLECT) is not None:
        raise HTTPException(409, "已有采集任务在运行，请先等待完成")

    c = ctx()
    ids = [str(x) for x in (payload.get("account_ids") or [])]
    rows = c.db.list_accounts()
    targets: List[Account] = []
    for r in rows:
        d = _row(r)
        if ids and d.get("account_id") not in ids:
            continue
        if not ids and (d.get("display_name") or "").strip():
            continue
        targets.append(Account(
            account_id=d.get("account_id", ""), username=d.get("username", ""),
            profile_url=d.get("profile_url", ""),
            region=d.get("region", "") or ""))

    if not targets:
        raise HTTPException(400, "没有需要刷新昵称的账号")

    job = STORE.create(JobKind.COLLECT, label=f"刷新昵称 · {len(targets)} 个账号")
    t = threading.Thread(target=_nickname_worker, args=(job, targets),
                         name="nickname-job", daemon=True)
    t.start()
    return {"ok": True, "job_id": job.id, "account_count": len(targets)}


def _nickname_worker(job, accounts: List[Account]) -> None:
    c = ctx()
    try:
        collector = c.build_collector()
        job.set_state(JobState.RUNNING)
        job.emit(EventKind.JOB_STARTED, message=f"开始获取 {len(accounts)} 个账号的昵称")
        ok = 0
        for i, acc in enumerate(accounts):
            if job.cancelled:
                job.cancel_finish("已停止")
                return
            try:
                nick = collector.fetch_nickname(acc) or ""
                if nick:
                    c.db.set_account_display_name(acc.account_id, nick)
                    ok += 1
                    job.log(f"@{acc.username} → {nick}", level="ok")
                else:
                    job.log(f"@{acc.username} 未取到昵称", level="warn")
            except Exception as exc:  # noqa: BLE001 —— 单账号失败不阻断
                job.log(f"@{acc.username} 失败：{exc}", level="warn")
            job.progress((i + 1) / max(1, len(accounts)),
                         message=f"@{acc.username}")
        job["ok"] = ok
        job.progress(1.0, message=f"成功获取 {ok}/{len(accounts)} 个昵称")
        job.finish(f"成功获取 {ok}/{len(accounts)} 个昵称")
    except Exception as exc:  # noqa: BLE001
        import traceback
        job.log(traceback.format_exc(), level="error")
        job.fail(f"{type(exc).__name__}: {exc}")


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, payload: dict):
    """更新单个账号（数量 / 地区 / 备注 / 启停 / 昵称）。

    ⚠️ 只更新传入的字段 —— 整个对象覆盖会误清地区（历史踩坑）。
    """
    c = ctx()
    _get_account(account_id)
    kwargs: dict = {}
    if "collect_count" in payload:
        try:
            kwargs["collect_count"] = max(1, int(payload["collect_count"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "collect_count 必须是正整数")
    if "region" in payload:
        kwargs["region"] = region_util.normalize_region(payload["region"])
    if "remark" in payload:
        kwargs["remark"] = str(payload["remark"] or "")
    if "display_name" in payload:
        kwargs["display_name"] = str(payload["display_name"] or "")
    if "enabled" in payload:
        c.db.set_account_enabled(account_id, bool(payload["enabled"]))
        return {"ok": True, "account": _account_dict(c.db.get_account(account_id))}
    if not kwargs:
        raise HTTPException(400, "没有可更新的字段")
    ok = c.account_service.update(account_id, **kwargs)
    return {"ok": bool(ok), "account": _account_dict(c.db.get_account(account_id))}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    """删除账号（级联删除其作品与日志）。"""
    _get_account(account_id)
    ok = ctx().account_service.delete(account_id)
    return {"ok": bool(ok), "deleted": account_id}


@router.post("/accounts/regions")
def set_regions(payload: dict):
    """批量设置地区。body: {"account_ids": [...], "region": "越南"}"""
    ids = [str(x) for x in (payload.get("account_ids") or [])]
    if not ids:
        raise HTTPException(400, "account_ids 不能为空")
    region = region_util.normalize_region(payload.get("region"))
    n = ctx().account_service.set_regions(ids, region)
    return {"ok": True, "updated": n, "region": region_util.region_display(region)}


@router.post("/accounts/collect-count")
def set_collect_count(payload: dict):
    """批量设置「每账号采集数量」（原桌面版「编辑数量」按钮）。

    body: {"account_ids": [...], "collect_count": 4}

    ⚠️ 只改数量，不动地区/备注/启用状态 —— 逐字段 PATCH，
    避免整对象覆盖（历史踩坑：覆盖式更新会清空地区）。
    """
    ids = [str(x) for x in (payload.get("account_ids") or [])]
    if not ids:
        raise HTTPException(400, "account_ids 不能为空")
    try:
        count = int(payload.get("collect_count"))
    except (TypeError, ValueError):
        raise HTTPException(400, "collect_count 必须是整数")
    count = max(0, min(200, count))
    c = ctx()
    updated = 0
    for aid in ids:
        if c.db.get_account(aid) is None:
            continue
        if c.account_service.update(aid, collect_count=count):
            updated += 1
    return {"ok": True, "updated": updated, "collect_count": count}


@router.post("/accounts/{account_id}/reset-login")
def reset_login(account_id: str):
    """清除某账号的登录态（下次采集需重新登录）。"""
    import shutil

    acc = _get_account(account_id)
    d = _paths.DATA_DIR / "sessions" / _safe_name(acc.account_id or acc.username)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    ctx().db.execute(
        "UPDATE accounts SET login_status = 'unknown' WHERE account_id = ?", (account_id,))
    return {"ok": True, "message": "登录态已清除"}


def _safe_name(name: str) -> str:
    import re
    return re.sub(r"[^\w\-]", "_", name or "account")


# ------------------------------------------------------------------ 采集任务

@router.post("/collect")
def start_collect(payload: dict):
    """启动一次采集（异步）。返回 job_id，进度走 SSE。

    body:
      {
        "account_ids": ["a", "b"],     # 显式指定账号
        "region": "越南",               # 或按地区（也支持 "__none__" = 未分类）
        "all": true,                   # 或显式声明"全部启用账号"
        "target_date": "2026-10-08",   # 可选，仅作记录标签（规则是"最新 N 条"）
        "collect_count": 4             # 可选，覆盖每账号数量
      }

    ⚠️ 三者必须显式给出其一；空请求一律 400。
       采集会**先删除所选账号的既有作品记录**（替换式记录），
       隐式兜底成"全部账号"的代价太大，因此不做任何猜测。
    """
    c = ctx()

    if STORE.find_active(JobKind.COLLECT) is not None:
        raise HTTPException(409, "已有采集任务在运行，请先等待完成或取消")

    ids = [str(x) for x in (payload.get("account_ids") or [])]
    region = payload.get("region")
    want_region = region not in (None, "", region_util.REGION_FILTER_ALL)

    # ⚠️ 安全闸门：必须**显式**给出选择范围。
    # 不设这道闸门时，一个空的 {} 请求会静默回退成"全部启用账号" →
    # 立刻开浏览器、并先删除这些账号的既有作品记录（替换式记录）。
    # 实测事故：验收脚本误传空列表，84 条作品被清空（已从备份恢复）。
    # 因此"全选"必须由调用方显式声明，绝不做隐式兜底。
    if not ids and not want_region and not payload.get("all"):
        raise HTTPException(
            400, "未指定采集范围：请传 account_ids、region，或显式传 all=true 表示全部启用账号")

    if ids:
        accounts = [_get_account(i) for i in ids]
    else:
        want = None
        if want_region:
            want = "" if region == REGION_UNSPECIFIED else str(region)
        rows = c.db.list_accounts(enabled_only=True, region=want)
        accounts = []
        for r in rows:
            d = _row(r)
            accounts.append(Account(
                account_id=d.get("account_id", ""), username=d.get("username", ""),
                profile_url=d.get("profile_url", ""),
                display_name=d.get("display_name", "") or "",
                region=d.get("region", "") or "", enabled=True,
                collect_count=int(d.get("collect_count") or 4)))

    if not accounts:
        raise HTTPException(400, "没有选中任何启用中的账号")

    override = payload.get("collect_count")
    if override is not None:
        try:
            n = max(1, int(override))
            for a in accounts:
                a.collect_count = n
        except (TypeError, ValueError):
            raise HTTPException(400, "collect_count 必须是正整数")

    target_date = str(payload.get("target_date") or "").strip() or _today(c)

    job = STORE.create(JobKind.COLLECT,
                       label=f"作品采集 · {len(accounts)} 个账号")
    job["total"] = len(accounts)
    job["ok"] = 0
    job["fail"] = 0
    job["target_date"] = target_date

    t = threading.Thread(target=_collect_worker, args=(job, accounts, target_date),
                         name="collect-job", daemon=True)
    t.start()
    return {"ok": True, "job_id": job.id, "account_count": len(accounts),
            "target_date": target_date}


def _collect_worker(job, accounts: List[Account], target_date: str) -> None:
    """采集工作线程：把引擎回调翻译成结构化事件。"""
    c = ctx()
    engine = None
    try:
        engine = c.build_engine()
        job.set_state(JobState.RUNNING)
        job.emit(EventKind.JOB_STARTED,
                 message=f"开始采集 {len(accounts)} 个账号（最新作品模式）")

        # 取消联动：前端 cancel → Job.cancel → 引擎 request_stop
        watcher_stop = threading.Event()

        def _watch() -> None:
            # 取消 / 暂停联动：`job.cancelled` 是标志位，`job.pause` 是 Event
            # （set = 运行 / clear = 暂停），语义与引擎完全一致，直接转译。
            while not watcher_stop.wait(0.3):
                if job.cancelled:
                    engine.request_stop()
                    return
                if job.pause.is_set():
                    engine.request_resume()
                else:
                    engine.request_pause()

        threading.Thread(target=_watch, name="collect-cancel-watch",
                         daemon=True).start()

        done = {"n": 0}

        def on_progress(idx: int, total: int, username: str,
                        status: str, detail: dict) -> None:
            done["n"] = idx
            job.log(f"@{username} {status}", level="info")
            job.emit(EventKind.ITEM, stage=_status_stage(status),
                     data={"username": username, "status": status,
                           "index": idx + 1, "total": total, **detail})
            job.progress((idx + 1) / max(1, total),
                         message=f"@{username} · {_status_cn(status)}")

        summary = engine.run(accounts, target_date, on_progress=on_progress)
        watcher_stop.set()

        # 会话标记：结果页据此决定是否显示链接
        c.collected_this_session = True
        c.last_task_id = summary.get("task_id", "")

        job["ok"] = summary.get("completed", 0) + summary.get("partial", 0)
        job["fail"] = summary.get("failed", 0)
        job["task_id"] = summary.get("task_id", "")
        job["summary"] = _summary_text(summary)
        job["accounts"] = [_result_dict(r) for r in summary.get("accounts", [])]

        # 收尾事件必须自带结果 —— 前端 onDone / toast / 系统通知只从事件里取数，
        # 不带就被统计成「0 个账号、0 条」。
        result = {
            "task_id": job["task_id"],
            "summary": job["summary"],
            "ok": job["ok"],
            "fail": job["fail"],
            "account_count": summary.get("account_count", len(accounts)),
            "new_videos": summary.get("new_videos", 0),
            "accounts": job["accounts"],
        }

        if summary.get("status") == "stopped":
            job.cancel_finish("已停止", data=result)
        else:
            job.progress(1.0, message=job["summary"])
            job.finish(job["summary"], data=result)
    except Exception as exc:  # noqa: BLE001
        import traceback
        job.log(traceback.format_exc(), level="error")
        job.fail(f"{type(exc).__name__}: {exc}")


def _result_dict(r) -> dict:
    return {
        "account_id": r.account_id,
        "username": r.username,
        "target_count": r.target_count,
        "actual_count": r.actual_count,
        "status": r.status,
        "status_label": status_message(r.status, r.actual_count, r.target_count),
        "error": r.error_reason or "",
        "diagnostics": dict(r.diagnostics or {}),
        "videos": [{"video_id": v.video_id, "video_url": v.video_url,
                    "publish_time": v.publish_time or "",
                    "publish_date": v.publish_date or ""} for v in (r.videos or [])],
    }


def _summary_text(s: dict) -> str:
    parts = [f"完成 {s.get('completed', 0)}"]
    if s.get("partial"):
        parts.append(f"部分 {s['partial']}")
    if s.get("empty"):
        parts.append(f"无作品 {s['empty']}")
    if s.get("failed"):
        parts.append(f"失败 {s['failed']}")
    extra = []
    if s.get("pinned_collected"):
        extra.append(f"额外采集 {s['pinned_collected']} 条置顶（今天/昨天发布，不占数量）")
    if s.get("pinned_skipped"):
        extra.append(f"跳过 {s['pinned_skipped']} 条旧置顶作品（不计入数量）")
    if s.get("restored"):
        extra.append(f"已回滚保留 {s['restored']} 条旧记录（采集失败且无新记录，未丢失）")
    if s.get("login_aborted"):
        extra.append("登录态失效已提前停止，后面账号的旧记录未清除")
    if s.get("links_kept"):
        extra.append(f"当日链接日志保留 {s['links_kept']} 条（本次没采到链接，未清空）")
    text = f"共 {s.get('account_count', 0)} 个账号，" + "，".join(parts)
    text += f"；实际入库 {s.get('new_videos', 0)} 条"
    if extra:
        text += "；" + "；".join(extra)
    return text


_STATUS_STAGE = {
    "completed": "completed", "partial": "partial", "empty": "empty",
    "failed": "failed", "skipped": "skipped", "collecting": "collecting",
}
_STATUS_CN = {
    "completed": "完成", "partial": "当日作品不足", "empty": "今日没有发现作品",
    "failed": "失败", "skipped": "已跳过", "collecting": "采集中",
}


def _status_stage(s: str) -> str:
    return _STATUS_STAGE.get(s, s)


def _status_cn(s: str) -> str:
    return _STATUS_CN.get(s, s)


def _task_dict(r: Any) -> dict:
    """任务行 → 前端字典（`/tasks`、`/overview`、任务恢复共用同一映射）。"""
    d = _row(r)
    status = d.get("status", "") or ""
    return {
        "task_id": d.get("task_id", ""),
        "target_date": d.get("target_date", "") or "",
        "created_at": d.get("created_at", "") or "",
        "completed_at": d.get("completed_at") or "",
        "status": status,
        "status_label": _status_cn(status),
        "account_count": int(d.get("account_count") or 0),
        "target_total": int(d.get("target_total") or 0),
        "actual_total": int(d.get("actual_total") or 0),
        "success_count": int(d.get("success_count") or 0),
        "fail_count": int(d.get("fail_count") or 0),
    }


def _today(c: CollectorServices) -> str:
    return datetime.now(DateResolver(c.timezone()).tz).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ 任务历史

@router.get("/tasks")
def list_tasks(limit: int = 50):
    c = ctx()
    rows = c.db.list_tasks(limit=max(1, min(500, limit)))
    out = [_task_dict(r) for r in rows]
    return {"tasks": out, "total": len(out), "grand_total": c.db.count_tasks()}


@router.get("/tasks/{task_id}")
def task_detail(task_id: str):
    c = ctx()
    row = c.db.get_task(task_id)
    if row is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    rows = c.db.list_task_accounts(task_id)
    videos = c.db.query_videos(task_id=task_id)
    return {
        "task": _task_dict(row),
        "accounts": [{
            "account_id": r["account_id"], "username": r["username"],
            "target_count": r["target_count"], "actual_count": r["actual_count"],
            "status": r["status"], "status_label": status_message(r["status"]),
            "error": r["error_reason"] or "",
        } for r in rows],
        "videos": [_video_dict(v) for v in videos],
        "url_text": c.result_service.task_video_urls(task_id),
    }


# ------------------------------------------------------------------ 采集结果

@router.get("/results")
def results(scope: str = "session", account_id: Optional[str] = None,
            publish_date: Optional[str] = None, task_id: Optional[str] = None):
    """查询采集结果。

    scope=session：只显示「本次运行采到的」（由 CollectorServices.last_task_id 决定）；
                   若本次尚未采集过则返回空 —— 这是用户明确要求的观感：
                   打开程序时结果区必须是空的。
    scope=all：全部历史。
    """
    c = ctx()
    tid = None
    if scope == "session":
        if not c.collected_this_session or not c.last_task_id:
            return {"rows": [], "url_text": "", "grouped_text": "", "stats": [],
                    "scope": scope, "task_id": "", "empty_reason": "本次运行尚未采集"}
        tid = task_id or c.last_task_id
    elif task_id:
        tid = task_id

    rows = c.result_service.query_videos(
        account_id=account_id or None, publish_date=publish_date or None, task_id=tid)
    data = [_video_dict(r) for r in rows]
    data.sort(key=lambda x: (x["collect_time"], x["publish_time"]), reverse=True)
    return {
        "rows": data,
        "url_text": c.result_service.to_url_text(rows),
        "grouped_text": c.result_service.to_grouped_text(rows),
        "stats": c.result_service.account_stats(rows),
        "scope": scope,
        "task_id": tid or "",
        "latest_collect_time": max((x["collect_time"] for x in data), default=""),
    }


@router.post("/results/clear")
def clear_results(payload: dict):
    """清空采集结果（按账号 / 日期过滤；都不传 = 清空全部）。"""
    n = ctx().result_service.clear_videos(
        account_id=payload.get("account_id") or None,
        publish_date=payload.get("publish_date") or None)
    return {"ok": True, "deleted": n}


@router.get("/logs")
def collect_logs(task_id: Optional[str] = None, account_id: Optional[str] = None,
                 limit: int = 500):
    """采集日志（供"运行日志"视图）。"""
    sql = "SELECT * FROM collect_logs WHERE 1=1"
    params: list = []
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    if account_id:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(5000, limit)))
    rows = ctx().db.query(sql, tuple(params))
    return {"logs": [{
        "task_id": r["task_id"] or "", "account_id": r["account_id"] or "",
        "video_id": r["video_id"] or "", "level": r["level"] or "info",
        "message": r["message"] or "", "created_at": r["created_at"] or "",
    } for r in rows]}


# ------------------------------------------------------------------ 导出

@router.post("/export")
def export(payload: dict):
    """导出采集结果。

    body: {"format": "txt"|"csv"|"xlsx", "task_id": "...", "account_id": "...",
           "mode": "urls"|"grouped"}
    """
    from collector.export.export_service import ExportService
    from collector.export.models import AccountStat, ExportRow

    c = ctx()
    fmt = str(payload.get("format") or "txt").lower()
    if fmt not in ("txt", "csv", "xlsx"):
        raise HTTPException(400, "format 只支持 txt / csv / xlsx")

    rows_raw = c.result_service.query_videos(
        account_id=payload.get("account_id") or None,
        publish_date=payload.get("publish_date") or None,
        task_id=payload.get("task_id") or None)
    if not rows_raw:
        raise HTTPException(400, "没有可导出的数据")

    export_rows = [ExportRow(
        account=r["account_id"], username=r["username"], video_id=r["video_id"],
        video_url=r["video_url"], publish_time=r["publish_time"] or "",
        publish_date=r["publish_date"] or "",
        collect_time=r["first_collect_time"] or "",
        task_id=r["first_task_id"] or "", status=r["status"] or "ok",
    ) for r in rows_raw]

    export_dir = _exports_dir()
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = export_dir / f"采集结果_{stamp}.{fmt}"
    svc = ExportService
    if fmt == "txt":
        svc.export_txt(export_rows, str(target),
                       mode=str(payload.get("mode") or "urls"))
    elif fmt == "csv":
        svc.export_csv(export_rows, str(target))
    else:
        stats = [AccountStat(account=str(s.get("account", "")),
                             username=str(s.get("username", "")),
                             target_count=int(s.get("target_count") or 0),
                             actual_count=int(s.get("actual_count") or 0),
                             status=str(s.get("status") or ""))
                 for s in c.result_service.account_stats(rows_raw)]
        svc.export_xlsx(export_rows, stats, str(target))

    return {"ok": True, "path": str(target), "name": target.name,
            "count": len(export_rows), "format": fmt}


@router.get("/export/download")
def export_download(path: str = Query(...)):
    """下载导出文件（受控：只允许当前导出目录下的文件）。"""
    p = Path(path).resolve()
    try:
        p.relative_to(_exports_dir().resolve())
    except ValueError:
        raise HTTPException(403, "只能下载导出目录下的文件")
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(p), filename=p.name)


@router.get("/exports")
def list_exports():
    d = _exports_dir()
    if not d.is_dir():
        return {"files": []}
    files = sorted(d.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:100]
    return {"files": [{"name": f.name, "path": str(f),
                       "size": f.stat().st_size,
                       "mtime": datetime.fromtimestamp(
                           f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
                      for f in files if f.is_file()]}


# ------------------------------------------------------------------ 每日链接归档

@router.get("/daily-links")
def daily_links():
    d = _paths.LINKS_LOG_DIR
    if not d.is_dir():
        return {"files": []}
    files = sorted(d.glob("*.txt"), reverse=True)[:120]
    return {"files": [{"day": f.stem, "path": str(f), "size": f.stat().st_size}
                      for f in files]}


@router.get("/daily-links/{day}")
def daily_link_content(day: str):
    if not day.isdigit() or len(day) != 8:
        raise HTTPException(400, "日期格式应为 YYYYMMDD")
    p = _paths.LINKS_LOG_DIR / f"{day}.txt"
    if not p.is_file():
        raise HTTPException(404, "该日期没有归档文件")
    return {"day": day, "content": p.read_text(encoding="utf-8", errors="replace")}


# ------------------------------------------------------------------ 聊天采集

@router.get("/chat/recent")
def chat_recent(limit: int = 5):
    svc = ctx().build_chat_service()
    targets = svc.recent_targets(limit=max(1, min(50, limit)))
    return {"targets": [_target_dict(t) for t in targets]}


@router.get("/chat/links")
def chat_links(chat_type: str, name: str = "", handle: str = "",
               uid: str = "", sec_uid: str = "", conversation_id: str = ""):
    svc = ctx().build_chat_service()
    t = ChatTarget(chat_type=chat_type, name=name, conversation_id=conversation_id,
                   handle=handle, uid=uid, sec_uid=sec_uid)
    links = svc.links_for(t)
    return {"links": links, "count": len(links),
            "url_text": "\n".join(links)}


@router.delete("/chat/links")
def chat_links_clear(chat_type: str, name: str = "", handle: str = "",
                     uid: str = "", sec_uid: str = "", conversation_id: str = ""):
    """清空**某一个目标**已存档的链接。

    这是 `replace_links` 的对称操作（写入即整体替换，清空即写入空列表），
    只作用于单个好友/群聊，不影响其它目标；前端已做内联二次确认。
    """
    t = ChatTarget(chat_type=chat_type, name=name, conversation_id=conversation_id,
                   handle=handle, uid=uid, sec_uid=sec_uid)
    if not (t.conversation_id or t.sec_uid or t.uid):
        raise HTTPException(400, "目标缺少稳定标识（conversation_id / sec_uid / uid）")
    svc = ctx().build_chat_service()
    n = svc.clear_links(t)
    return {"ok": True, "cleared": n, "target": _target_dict(t)}


@router.post("/chat/login")
def chat_login(payload: dict):
    """弹出**有窗口**的浏览器，等用户人工登录消息页（轮询 sessionid cookie）。

    这是采集模块里唯一会主动弹窗的入口 —— 登录必须人工完成，无法后台化。
    登录态落到程序目录的 `data/sessions/`，之后长期免登录。
    """
    if STORE.find_active(JobKind.CHAT_COLLECT) is not None:
        raise HTTPException(409, "已有聊天任务在运行，请先等待完成")
    acc = _get_account(str(payload.get("account_id") or ""))
    try:
        timeout_s = int(payload.get("timeout") or 300)
    except (TypeError, ValueError):
        timeout_s = 300

    job = STORE.create(JobKind.CHAT_COLLECT, label=f"登录 · @{acc.username}")
    t = threading.Thread(target=_chat_login_worker, args=(job, acc, timeout_s),
                         name="chat-login-job", daemon=True)
    t.start()
    return {"ok": True, "job_id": job.id, "account": acc.username}


def _chat_login_worker(job, acc: Account, timeout_s: int) -> None:
    """把「等人工登录」的进度通过 SSE 透出去，前端据此提示用户去看浏览器窗口。"""
    try:
        svc = ctx().build_chat_service()
        job.set_state(JobState.RUNNING)
        job.emit(EventKind.JOB_STARTED,
                 message=f"已为 @{acc.username} 打开浏览器，请在窗口中完成登录")

        def on_status(msg: str) -> None:
            job.log(str(msg), level="info")
            job.emit(EventKind.STAGE, stage_label=str(msg), message=str(msg))

        ok = svc.open_messages_login(acc, timeout_s=timeout_s,
                                     on_status=on_status)
        if ok:
            job.progress(1.0, message="登录成功，已保存登录态")
            job.finish("登录成功，已保存登录态")
        else:
            job.fail(f"登录未完成（{timeout_s} 秒内未检测到登录态）")
    except Exception as exc:  # noqa: BLE001
        import traceback
        job.log(traceback.format_exc(), level="error")
        job.fail(f"{type(exc).__name__}: {exc}")


@router.post("/chat/search")
def chat_search(payload: dict):
    """搜索好友 / 群聊目标（异步）。返回 job_id。"""
    if STORE.find_active(JobKind.CHAT_COLLECT) is not None:
        raise HTTPException(409, "已有聊天任务在运行，请先等待完成")
    account_id = str(payload.get("account_id") or "")
    acc = _get_account(account_id)
    query = str(payload.get("query") or "")
    chat_type = str(payload.get("chat_type") or "")
    mode = str(payload.get("mode") or "search")   # search | all

    job = STORE.create(JobKind.CHAT_COLLECT, label=f"聊天目标 · @{acc.username}")
    t = threading.Thread(target=_chat_search_worker,
                         args=(job, acc, query, chat_type, mode),
                         name="chat-search-job", daemon=True)
    t.start()
    return {"ok": True, "job_id": job.id}


def _chat_search_worker(job, acc: Account, query: str, chat_type: str,
                        mode: str) -> None:
    try:
        svc = ctx().build_chat_service()
        job.set_state(JobState.RUNNING)
        job.emit(EventKind.JOB_STARTED, message=f"正在打开 @{acc.username} 的消息页…")

        def on_stage(msg: str) -> None:
            job.log(str(msg), level="info")
            job.emit(EventKind.STAGE, stage_label=str(msg), message=str(msg))

        if mode == "all":
            res = svc.list_all_targets(acc, on_stage=on_stage)
            candidates = res.get("conversations") or []
        else:
            res = svc.search_targets(acc, query, chat_type=chat_type)
            candidates = res.get("candidates") or []
        if res.get("error"):
            job["error"] = res["error"]
        items = [_candidate_dict(c) for c in candidates]
        job["items"] = items
        job.progress(1.0, message=f"找到 {len(items)} 个可选项")
        job.finish(f"找到 {len(items)} 个可选项")
    except Exception as exc:  # noqa: BLE001
        import traceback
        job.log(traceback.format_exc(), level="error")
        job.fail(f"{type(exc).__name__}: {exc}")


@router.post("/chat/collect")
def chat_collect(payload: dict):
    """采集某个好友/群聊的链接（异步）。"""
    if STORE.find_active(JobKind.CHAT_COLLECT) is not None:
        raise HTTPException(409, "已有聊天任务在运行，请先等待完成")
    acc = _get_account(str(payload.get("account_id") or ""))
    tgt = payload.get("target") or {}
    if not tgt.get("chat_type"):
        raise HTTPException(400, "缺少 target.chat_type")
    target = ChatTarget(
        chat_type=str(tgt.get("chat_type")),
        name=str(tgt.get("name") or ""),
        conversation_id=str(tgt.get("conversation_id") or ""),
        handle=str(tgt.get("handle") or ""),
        uid=str(tgt.get("uid") or ""),
        sec_uid=str(tgt.get("sec_uid") or ""),
    )
    if not (target.conversation_id or target.sec_uid or target.uid):
        raise HTTPException(400, "目标缺少稳定标识（conversation_id / sec_uid / uid）")
    count = max(1, int(payload.get("count") or ctx().config.get("chat_last_count", 22)))

    job = STORE.create(JobKind.CHAT_COLLECT,
                       label=f"聊天链接 · {target.name or target.chat_type}")
    t = threading.Thread(
        target=_chat_collect_worker,
        args=(job, acc, target, count, bool(payload.get("show_browser"))),
        name="chat-collect-job", daemon=True)
    t.start()
    return {"ok": True, "job_id": job.id, "target": _target_dict(target)}


def _chat_collect_worker(job, acc: Account, target: ChatTarget, count: int,
                         show_browser: bool = False) -> None:
    try:
        svc = ctx().build_chat_service()
        # 「显示浏览器窗口」：内核早已支持 show_browser 开关
        # （chat_service._build_browser 读到它就强制 headless=False）
        svc.show_browser = bool(show_browser)
        job.set_state(JobState.RUNNING)
        job.emit(EventKind.JOB_STARTED,
                 message=f"采集 {target.name}（{target.type_label}）最近 {count} 条")

        def on_progress(msg) -> None:
            job.log(str(msg), level="info")
            job.emit(EventKind.STAGE, stage_label=str(msg), message=str(msg))

        def on_ratio(done, total) -> None:
            try:
                job.progress(float(done) / max(1, float(total)),
                             message=f"已抓 {done}/{total}")
            except (TypeError, ValueError):
                pass

        result = svc.collect(acc, target, count, on_progress=on_progress,
                             stop_event=job.cancel, on_ratio=on_ratio)
        links = [li.video_url for li in (result.links or [])]
        job["links"] = links
        job["url_text"] = "\n".join(links)
        job["status"] = result.status
        job["target"] = _target_dict(result.target)
        if result.error:
            job["error"] = result.error
        msg = f"{result.status} · 抓到 {len(links)} 条链接"
        if result.error:
            job.fail(f"{msg}（{result.error}）")
        elif result.status == "stopped":
            job.cancel_finish(msg)
        else:
            job.progress(1.0, message=msg)
            job.finish(msg)
    except Exception as exc:  # noqa: BLE001
        import traceback
        job.log(traceback.format_exc(), level="error")
        job.fail(f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ 设置

_COLLECTOR_SETTING_KEYS = [
    "default_collect_count", "timezone", "browser_headless", "page_load_timeout_ms",
    "max_retry", "account_interval_seconds",
    "history_keep_days", "notify_sound", "log_level", "log_keep_days",
    "update_repo", "update_url",
    "auto_check_update", "proxy_enabled", "proxy_type", "proxy_host", "proxy_port",
    "chat_last_count",
    # 原桌面版「数据 / 日志」分组里可编辑的两个路径（此前只在「关于」页只读展示）
    "export_dir", "log_dir",
    # 通知卡片外观（2026-10-10）
    "notify_card_width", "notify_card_scale",
]


@router.get("/settings")
def get_settings():
    d = ucfg.section("collector")
    return {"values": {k: d.get(k) for k in _COLLECTOR_SETTING_KEYS}}


@router.post("/settings")
def set_settings(payload: dict):
    values = {k: _coerce_setting(k, v)
              for k, v in (payload or {}).items() if k in _COLLECTOR_SETTING_KEYS}
    if not values:
        raise HTTPException(400, "没有可保存的设置项")
    # 可编辑目录先验证再落盘：写不进去就报错，不留下"设置已保存但目录不可用"。
    _ensure_setting_dirs(values)
    ucfg.update("collector", values)
    c = ctx()
    c.config.load()          # 热更新到当前服务容器
    c.reset_engine()         # 代理/间隔/无头模式变更后重建引擎
    if "log_level" in values:
        # 日志级别可以立刻改（根 logger 的 level 是运行期可变的）；
        # `log_keep_days` 只影响文件轮转份数，需重启才生效 —— 前端已注明。
        try:
            import logging as _logging

            _logging.getLogger().setLevel(
                str(c.config.get("log_level", "INFO")).upper())
        except (ValueError, TypeError):  # noqa: PERF203
            c.logger.warning("日志级别热更新失败：%r", values.get("log_level"))
    if "history_keep_days" in values:
        # 历史保留天数可以立刻生效：只清理过期的任务记录，
        # 素材库作品与可续采任务都不在清理范围内（见 CollectorServices.prune_history）。
        try:
            removed = c.prune_history()
            if removed:
                c.logger.info("按新设置清理了 %d 条过期任务记录", removed)
        except Exception:  # noqa: BLE001 —— 清理失败不影响设置已保存
            c.logger.warning("按新设置清理采集历史失败", exc_info=True)
    return {"ok": True, "saved": list(values.keys())}


@router.post("/settings/reset")
def reset_settings(payload: dict = None):
    """恢复采集配置默认值（原桌面版设置页「恢复默认」）。

    body 可传 {"keys": [...]} 只重置部分键；缺省则重置全部采集键。
    只写白名单内的键，绝不触碰数据库与用户数据。
    """
    from collector.config import DEFAULTS as _DEFAULTS

    keys = [k for k in ((payload or {}).get("keys") or _COLLECTOR_SETTING_KEYS)
            if k in _COLLECTOR_SETTING_KEYS]
    if not keys:
        raise HTTPException(400, "没有可恢复的设置项")
    values = {k: _DEFAULTS[k] for k in keys if k in _DEFAULTS}
    if not values:
        raise HTTPException(400, "默认值表中没有这些键")
    ucfg.update("collector", values)
    c = ctx()
    c.config.load()
    c.reset_engine()
    return {"ok": True, "reset": sorted(values.keys())}


@router.post("/settings/test-proxy")
def test_proxy():
    """测试当前代理配置是否可用（不启动浏览器，只做 TCP 连通）。"""
    import socket

    pc = ctx().config.proxy_config()
    if not pc:
        return {"ok": False, "message": "未启用代理或未填写地址"}
    server = str(pc.get("server") or "")
    hostport = server.split("://")[-1]
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        return {"ok": False, "message": f"代理地址格式不正确: {server}"}
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return {"ok": True, "message": f"代理可达：{server}"}
    except OSError as exc:
        return {"ok": False, "message": f"代理不可达：{exc}"}


# ------------------------------------------------------------------ 维护

@router.get("/backups")
def list_backups():
    c = ctx()
    return {"backups": [{"name": p.name, "path": str(p),
                         "size": p.stat().st_size,
                         "mtime": datetime.fromtimestamp(
                             p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
                        for p in c.list_backups()]}


@router.post("/backup")
def do_backup():
    p = ctx().backup_database()
    return {"ok": True, "path": str(p), "name": p.name}


@router.post("/restore")
def do_restore(payload: dict):
    src = str(payload.get("path") or "")
    p = Path(src).resolve()
    try:
        p.relative_to(_paths.BACKUPS_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "只能从 backups/ 目录恢复")
    if not p.is_file():
        raise HTTPException(404, "备份文件不存在")
    ok = ctx().restore_database(str(p))
    if not ok:
        raise HTTPException(500, "恢复失败，请查看日志")
    return {"ok": True, "restored": p.name}


@router.get("/update-check")
def update_check():
    """检查采集模块的更新（沿用原 GitHub Releases 配置）。"""
    info = ctx().check_for_update()
    if info is None:
        return {"has_update": False}
    return {"has_update": True, "version": info.version, "url": info.url,
            "notes": info.notes, "source": info.source}


@router.get("/env")
def env_info():
    """采集环境自检：Playwright / 浏览器内核 / 数据库 / 目录。"""
    c = ctx()
    info: dict = {
        "db_path": str(_paths.DB_PATH),
        "db_exists": _paths.DB_PATH.is_file(),
        "accounts": c.db.count_accounts(),
        "videos": c.db.count_videos(),
        "data_dir": str(_paths.DATA_DIR),
        "sessions": str(_paths.DATA_DIR / "sessions"),
        # 这些目录只读展示（前端不提供手改入口）：路径写坏会连带影响
        # 日志轮转与结果导出，风险远大于收益。
        # 例外：exports / logs 两个目录在设置页可改，这里显示的就是当前生效值。
        "exports_dir": str(_exports_dir()),
        "logs_dir": str(_logs_dir()),
        "links_log_dir": str(_paths.LINKS_LOG_DIR),
        "backups_dir": str(_paths.BACKUPS_DIR),
        "timezone": c.timezone(),
        "headless": bool(c.config.get("browser_headless")),
    }
    try:
        import playwright  # noqa: F401
        info["playwright"] = True
    except ImportError:
        info["playwright"] = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            info["chromium"] = p.chromium.executable_path is not None
    except Exception as exc:  # noqa: BLE001
        info["chromium"] = False
        info["chromium_error"] = str(exc)[:200]
    # 登录态目录数量
    sdir = _paths.DATA_DIR / "sessions"
    info["session_dirs"] = len([d for d in sdir.iterdir() if d.is_dir()]) \
        if sdir.is_dir() else 0
    return info
