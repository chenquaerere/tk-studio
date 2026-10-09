"""解析结果本地缓存

- SQLite 单表, key = 输入 URL 或 "vid:视频ID"
- 命中条件: TTL 内有成功解析记录
- 下载失败时主动删除对应缓存, 下次解析重新请求 tikwm 拿新地址
- 线程安全: 全局锁 + sqlite 自带锁

v4.0 重要修复
-------------
原来 TTL = 7 天。但缓存里存的是 **带签名的 CDN 直链**，实际有效期只有数小时。
结果：昨天解析成功的链接，今天拿来下载全是 403 —— 用户看到「下载失败」，
却不知道真正原因是缓存里的地址过期了。

现在：
- TTL 降到 45 分钟（可按需调）；
- 下载遇到 401/403/410 时**直接失效该条缓存**（而不是把 3 次重试全耗在死链上）。
"""
import json
import re
import sqlite3
import threading
import time
from typing import Optional

from .models import VideoInfo, ContentType
from .config import get_app_dir

CACHE_DB = get_app_dir() / "parse_cache.db"
# v4.0: 7 天 -> 45 分钟（签名直链的有效期量级）
TTL_SECONDS = 45 * 60
_VID_RE = re.compile(r"/(?:video|photo)/(\d+)")

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS parse_cache "
        "(key TEXT PRIMARY KEY, payload TEXT, saved_at REAL)"
    )
    return conn


def _serialize(info: VideoInfo) -> dict:
    return {
        "url": info.url,
        "video_id": info.video_id,
        "title": info.title,
        "author": info.author,
        "author_id": info.author_id,
        "duration": info.duration,
        "thumbnail": info.thumbnail,
        "content_type": info.content_type.value,
        "download_url": info.download_url,
        "image_urls": info.image_urls,
        "music_url": info.music_url,
        "music_title": info.music_title,
        "create_time": info.create_time,
        "like_count": info.like_count,
        "comment_count": info.comment_count,
        "share_count": info.share_count,
        "view_count": info.view_count,
        "extra": info.extra,
    }


def _deserialize(d: dict) -> VideoInfo:
    # v2.0: 兼容旧缓存中 cover 为 dict 的情况
    thumb = d.get("thumbnail", "")
    if isinstance(thumb, dict):
        ul = thumb.get("url_list") or []
        thumb = ul[0] if ul else ""
    if not isinstance(thumb, str):
        thumb = str(thumb or "")
    return VideoInfo(
        url=d.get("url", ""),
        video_id=d.get("video_id", ""),
        title=d.get("title", ""),
        author=d.get("author", ""),
        author_id=d.get("author_id", ""),
        duration=float(d.get("duration", 0) or 0),
        thumbnail=thumb,
        content_type=ContentType(d.get("content_type", "video")),
        download_url=d.get("download_url", ""),
        image_urls=d.get("image_urls") or [],
        music_url=d.get("music_url", ""),
        music_title=d.get("music_title", ""),
        create_time=int(d.get("create_time", 0) or 0),
        like_count=int(d.get("like_count", 0) or 0),
        comment_count=int(d.get("comment_count", 0) or 0),
        share_count=int(d.get("share_count", 0) or 0),
        view_count=int(d.get("view_count", 0) or 0),
        extra=d.get("extra") or {},
    )


def _get(key: str) -> Optional[VideoInfo]:
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT payload, saved_at FROM parse_cache WHERE key=?", (key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        payload, saved_at = row
        if time.time() - float(saved_at) > TTL_SECONDS:
            _delete(key)
            return None
        return _deserialize(json.loads(payload))
    except Exception:
        return None


def _delete(key: str) -> None:
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM parse_cache WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def lookup(url: str) -> Optional[VideoInfo]:
    """按输入 URL 查缓存; 标准链接再按 video_id 二次命中"""
    with _lock:
        cached = _get(url)
        if cached is not None:
            return cached
        m = _VID_RE.search(url)
        if m:
            return _get("vid:" + m.group(1))
        return None


def save(url: str, info: VideoInfo) -> None:
    """写入两条索引: 输入 URL + vid:视频ID"""
    if info is None:
        return
    payload = json.dumps(_serialize(info), ensure_ascii=False)
    now = time.time()
    keys = [url]
    if info.video_id:
        keys.append("vid:" + info.video_id)
    with _lock:
        try:
            conn = _conn()
            try:
                for k in keys:
                    conn.execute(
                        "INSERT OR REPLACE INTO parse_cache (key, payload, saved_at) "
                        "VALUES (?,?,?)",
                        (k, payload, now),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass


def evict(url: str, video_id: str = "") -> None:
    """下载失败时删除缓存, 下次解析强制刷新"""
    keys = [url]
    if video_id:
        keys.append("vid:" + video_id)
    with _lock:
        for k in keys:
            _delete(k)


# v4.0: 这些状态码说明「缓存里的签名地址已失效」，不该再重试
STALE_STATUS = {401, 403, 410, 404}


def is_stale_status(code: int) -> bool:
    """HTTP 状态码是否表示签名直链已失效。"""
    return int(code or 0) in STALE_STATUS
