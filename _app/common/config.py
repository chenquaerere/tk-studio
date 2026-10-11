"""统一配置：单一 config.json，按命名空间分区。

文件结构
--------
    {
      "app":        { ... 全局（主题、语言、版本足迹）... },
      "collector":  { ... 原采集器 app/config.py 的全部键 ... },
      "watermark":  { ... 原去水印 core/config.py 的全部键 ... },
      "hd":         { ... 高清超分参数（从 watermark 中单列，便于前端分区展示）... }
    }

为什么分区而不是拍平
-------------------
    两个程序各自有近 30 个同名/近名键（如 output_root、max_retry、video_scale）。
    拍平必然互相覆盖。分区后各自语义独立，且 `section("watermark")` 返回的字典
    与原 `core.config.load()` 完全一致 —— 老代码一行不用改。

旧配置自动迁移（一次性、可回滚）
-------------------------------
    首次加载时若发现 config.json 还是"扁平"格式（去水印 v4 的写法），
    会把它整体归入 `watermark` 分区；同时把 `docs/_legacy_collector_config.json`
    并入 `collector` 分区。迁移前先备份为 `config.legacy-<时间戳>.json`。

    迁移是**合并**而非覆盖：旧文件里有的键全部保留，新默认值只补缺失项，
    因此不存在"迁移后丢配置"的情况。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from . import paths

CONFIG_PATH = paths.CONFIG_PATH
LEGACY_DIR = paths.LEGACY_DIR

SECTIONS = ("app", "collector", "watermark", "hd")

# 去水印分区中「属于高清超分」的键：单列到 hd 分区，前端设置页分开展示。
_HD_KEYS = {
    "face_enhance", "face_work_side", "image_model", "image_scale", "tile",
    "half", "video_model", "video_scale", "video_encoder", "video_crf",
    "interpolate", "max_output_size", "video_max_side", "video_skip_similar",
    "video_skip_thr", "video_sharp",
}

DEFAULTS: Dict[str, Dict[str, Any]] = {
    # 2026-10-11 清理：theme / language / first_run / window 四个键全仓库没有任何
    # 读取点（窗口尺寸实际存在 runtime/window_state.json，由 desktop.py 的
    # load_window_state / save_window_state 负责），删掉以免 config.json 里的键骗人。
    "app": {
        "last_seen_version": "",
    },
    # 原采集器 app/config.py DEFAULTS（含部署库已有键）
    # 2026-10-11 清理：default_date / auto_open_browser_on_start / browser_type /
    # auto_save / task_interval_ms / error_wait_ms 这六个键没有任何读取点
    # （界面里也没有对应控件），保留只会让 config.json 看起来像有这些开关。
    "collector": {
        "default_collect_count": 4,
        "timezone": "Asia/Shanghai",
        "browser_headless": True,
        "page_load_timeout_ms": 30000,
        "max_retry": 3,
        "account_interval_seconds": 4.0,
        "db_path": "data/tiktok_link_collector.db",
        "export_dir": "exports",
        "links_log_dir": "links_log",
        "history_keep_days": 90,
        "notify_sound": True,
        # 通知卡片外观（2026-10-10 用户反馈：弹窗要出现在**程序界面**右下角）
        #   notify_card_width / _scale   卡片宽度、字号/内距缩放，保存即生效
        # 2026-10-11 清理：原先的 notify_in_app / notify_desktop_when_unfocused
        # 两个开关（前台用卡片 / 后台用桌面浮层）已删除 —— 完成提醒固定画在
        # 程序界面右下角，桌面浮层不再被任何完成消息使用，留着开关只会误导。
        "notify_card_width": 420,
        "notify_card_scale": 1.0,
        "log_level": "INFO",
        "log_dir": "logs",
        "log_keep_days": 30,
        "update_repo": "",
        "update_url": "",
        "auto_check_update": True,
        "proxy_enabled": False,
        "proxy_type": "http",
        "proxy_host": "",
        "proxy_port": "",
        "chat_last_count": 22,
    },
    # 原去水印 core/config.py DEFAULTS（保留全部键；HD 键在 hd 分区也有副本）
    "watermark": {
        "output_root": str(paths.APP_ROOT / "downloads"),
        "date_folder": True,
        "skip_existing": True,
        "keep_frames": False,
        "beep_on_done": True,
        "lan_access": False,
        # 2026-10-11 清理：lan_password 早已不再参与任何校验（api.py 的键表里
        # v4.0.1 就标注了「已移除」），tk_concurrent / tk_retries / tk_quality /
        # tk_proxy 四个键也没有读取点 —— 一并删除，避免「设了密码就安全」的错觉。
    },
    "hd": {
        "face_enhance": False,
        "face_work_side": 4096,
        "image_model": "realesr-general-x4v3",
        "image_scale": 4,
        "tile": 0,
        "half": True,
        "video_model": "realesr-general-x4v3",
        "video_scale": 2,
        "video_encoder": "auto",
        "video_crf": 18,
        "interpolate": False,
        "max_output_size": 4096,
        "video_max_side": 1920,
        "video_skip_similar": True,
        "video_skip_thr": 1.5,
        "video_sharp": True,
    },
}

_lock = threading.RLock()
_migrated_flag = False


# ------------------------------------------------------------------ 读写底层

def _read_json(path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _is_namespaced(raw: Dict[str, Any]) -> bool:
    """判断是否已是分区格式。

    判据：所有顶层键都在已知分区内，且至少有一个分区是 dict。
    只要出现任何"分区外的顶层键"，就认定还是旧的扁平格式。
    """
    if not raw:
        return False
    if any(k not in SECTIONS for k in raw):
        return False
    return any(isinstance(raw.get(k), dict) for k in SECTIONS)


def _split_flat(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """扁平 → 分区。HD 键同时归入 watermark 与 hd，保证老代码与新前端都能读到。"""
    wm: Dict[str, Any] = {}
    hd: Dict[str, Any] = {}
    app: Dict[str, Any] = {}
    for k, v in raw.items():
        if k in ("theme", "language"):
            app[k] = v
        elif k == "last_seen_version":
            app[k] = v
            wm[k] = v
        else:
            wm[k] = v
            if k in _HD_KEYS:
                hd[k] = v
    return {"app": app, "collector": {}, "watermark": wm, "hd": hd}


def _legacy_collector() -> Dict[str, Any]:
    """读取旧采集器配置（迁移源）。"""
    for name in ("_legacy_collector_config.json", "collector_config.json"):
        p = LEGACY_DIR / name
        if p.exists():
            data = _read_json(p)
            if data:
                return data
    return {}


def migrate(force: bool = False) -> bool:
    """必要时执行一次性迁移。返回 True 表示本次做了迁移。"""
    global _migrated_flag
    with _lock:
        if _migrated_flag and not force:
            return False

        raw = _read_json(CONFIG_PATH)
        is_flat = bool(raw) and not _is_namespaced(raw)

        # 是否还需要补并旧采集器配置
        collector_stored = raw.get("collector", {}) if isinstance(raw, dict) else {}
        need_collector = not collector_stored

        if not is_flat and not need_collector:
            _migrated_flag = True
            return False

        if is_flat:
            backup = CONFIG_PATH.with_name(
                f"config.legacy-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                backup.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            except OSError:
                pass
            merged = _split_flat(raw)
        else:
            merged = {k: (raw.get(k) if isinstance(raw.get(k), dict) else {})
                      for k in SECTIONS}

        if need_collector:
            merged["collector"] = dict(_legacy_collector())

        _write_json(CONFIG_PATH, merged)
        _migrated_flag = True
        return True


# ------------------------------------------------------------------ 对外 API

def load() -> Dict[str, Dict[str, Any]]:
    """返回完整的分区配置（已合并默认值）。"""
    migrate()
    raw = _read_json(CONFIG_PATH)
    out: Dict[str, Dict[str, Any]] = {}
    for name in SECTIONS:
        merged = dict(DEFAULTS.get(name, {}))
        stored = raw.get(name)
        if isinstance(stored, dict):
            merged.update(stored)
        out[name] = merged
    # watermark 分区要能读到 HD 键（老代码 load() 直接取 video_scale 等）
    for k, v in out["hd"].items():
        out["watermark"].setdefault(k, v)
    return out


def section(name: str) -> Dict[str, Any]:
    if name not in SECTIONS:
        raise KeyError(f"未知配置分区: {name}")
    return load()[name]


def get(name: str, key: str, default: Any = None) -> Any:
    return section(name).get(key, default)


def update(name: str, values: Optional[Dict[str, Any]] = None, **kw: Any) -> None:
    """合并写入某个分区（只覆盖传入的键，其余保留）。"""
    if name not in SECTIONS:
        raise KeyError(f"未知配置分区: {name}")
    payload = dict(values or {})
    payload.update(kw)
    if not payload:
        return
    with _lock:
        raw = _read_json(CONFIG_PATH)
        if not _is_namespaced(raw):
            raw = _split_flat(raw) if raw else {k: {} for k in SECTIONS}
        for k in SECTIONS:
            if not isinstance(raw.get(k), dict):
                raw[k] = {}
        raw[name].update(payload)
        # HD 键同步双写，保证 watermark 分区始终可读
        if name == "hd":
            raw.setdefault("watermark", {}).update(
                {k: v for k, v in payload.items() if k in _HD_KEYS})
        elif name == "watermark":
            sync = {k: v for k, v in payload.items() if k in _HD_KEYS}
            if sync:
                raw.setdefault("hd", {}).update(sync)
        _write_json(CONFIG_PATH, raw)


def reset_cache() -> None:
    """仅测试用：清掉"已迁移"标记。"""
    global _migrated_flag
    _migrated_flag = False
