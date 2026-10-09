"""
抖音 (Douyin) 无水印解析引擎 v2.0

架构:
- 直连解析（抖音为国内服务，不走海外代理）
- 短链解析: https://v.douyin.com/xxx/ -> https://www.douyin.com/{video|note}/{aweme_id}
- 主路径: web detail API (a_bogus 签名 + ttwid cookie)
      GET https://www.douyin.com/aweme/v1/web/aweme/detail/?{params}&a_bogus=...
- 兜底路径(按顺序):
      1. iesdouyin share 页面正则提取 (window._ROUTER_DATA 等)
      2. tikwm 公共 API (某些抖音链接 tikwm 也能解)
      3. yt-dlp (若已安装)

支持:
- 视频 (play_addr 无水印直链)
- 图文/图集 (images[].url_list 无水印原图)
"""
import json
import random
import re
import string
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .models import VideoInfo, ContentType
from .logger import log
from .abogus import ABogus, BrowserFingerprintGenerator

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

TTWID_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_DATA = (
    '{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com",'
    '"migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}'
)

DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# 短链域名
_SHORT_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+", re.I)
# 标准链接提取 aweme_id
_ID_RE = re.compile(r"/(?:video|note|photo|slides|discover)/(\d+)")
_MODAL_ID_RE = re.compile(r"modal_id=(\d+)")

# 图片 URL 尺寸升级模板（把 q75.webp 换成更高清格式）
_TPL_RE = re.compile(r"~tplv-[^/]+")

# v2.1: 最近一次解析失败原因（供 UI 显示）
_LAST_ERROR = ""


def _set_last_error(msg: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = msg or ""


def get_last_error() -> str:
    """返回最近一次抖音解析失败的原因。"""
    return _LAST_ERROR


# v2.1: ttwid 模块级缓存（多个链接批量解析时共享，避免重复注册触发风控）
_TTWID_CACHE: Dict[str, float] = {"value": "", "at": 0.0}
_TTWID_TTL = 12 * 3600.0  # 12 小时


class DouyinClient:
    """抖音解析客户端（直连，不使用海外代理）"""

    def __init__(self, timeout: int = 20, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.douyin.com/",
            "Origin": "https://www.douyin.com",
        })
        self._ttwid_lock = threading.Lock()
        self._abogus = ABogus(user_agent=UA,
                              fp=BrowserFingerprintGenerator.generate_fingerprint("Edge"))

    # ==================================================================
    # ttwid 管理 (模块级缓存, 12 小时)
    # ==================================================================
    def get_ttwid(self) -> str:
        global _TTWID_CACHE
        now = time.monotonic()
        with self._ttwid_lock:
            cached = _TTWID_CACHE.get("value") or ""
            if cached and (now - _TTWID_CACHE.get("at", 0.0)) < _TTWID_TTL:
                return cached
            try:
                resp = self.session.post(
                    TTWID_URL, data=TTWID_DATA, timeout=self.timeout,
                    headers={"Content-Type": "application/json; charset=utf-8",
                             "User-Agent": UA},
                )
                if resp.status_code == 200:
                    t = resp.cookies.get("ttwid")
                    if not t:
                        try:
                            j = resp.json()
                            t = (j.get("data") or {}).get("ttwid") or j.get("ttwid")
                        except Exception:
                            t = None
                    if t:
                        _TTWID_CACHE = {"value": t, "at": time.monotonic()}
                        log.info(f"[douyin] ttwid 获取成功: {t[:24]}...")
                        return t
                log.warning(f"[douyin] ttwid 注册失败: HTTP {resp.status_code}")
            except Exception as e:
                log.warning(f"[douyin] ttwid 注册异常: {e}")
        return ""

    # ==================================================================
    # 链接处理
    # ==================================================================
    @staticmethod
    def is_douyin_url(url: str) -> bool:
        return bool(re.search(r"douyin\.com|iesdouyin", url or "", re.I))

    def resolve_short_url(self, url: str) -> str:
        """跟随 v.douyin.com 短链跳转，返回标准链接（失败原样返回）。

        v4.0 修复：原来用 `stream=True` 却没有关闭响应，
        每解析一条短链就泄漏一个连接（socket 一直挂到 GC）。
        现在用 with 上下文确保释放。
        """
        if not _SHORT_RE.search(url):
            return url
        try:
            with self.session.get(url, allow_redirects=True,
                                  timeout=self.timeout) as resp:
                if resp.url:
                    return resp.url
        except Exception as e:
            log.debug(f"[douyin] 短链跳转失败 {url[:50]}: {e}")
        return url

    @staticmethod
    def extract_aweme_id(url: str) -> Optional[str]:
        m = _ID_RE.search(url) or _MODAL_ID_RE.search(url)
        return m.group(1) if m else None

    # ==================================================================
    # web detail API (a_bogus)
    # ==================================================================
    def _build_params(self, aweme_id: str, ms_token: str = "") -> str:
        params = (
            "device_platform=webapp&aid=6383&channel=channel_pc_web"
            f"&aweme_id={aweme_id}&pc_client_type=1&version_code=290100"
            "&version_name=29.1.0&cookie_enabled=true&screen_width=1920"
            "&screen_height=1080&browser_language=zh-CN&browser_platform=Win32"
            "&browser_name=Edge&browser_version=131.0.0.0&browser_online=true"
            "&engine_name=Blink&engine_version=131.0.0.0&os_name=Windows"
            "&os_version=10&cpu_core_num=12&device_memory=8&platform=PC"
            "&downlink=10&effective_type=4g&round_trip_time=50"
        )
        if ms_token:
            params += f"&msToken={ms_token}"
        return params

    def _fetch_detail(self, aweme_id: str) -> Optional[dict]:
        """调用 web detail API，成功返回 aweme_detail dict
        v2.1: 每次重试都换新 msToken + 新签名 + 随机指纹，降低风控概率"""
        ttwid = self.get_ttwid()
        last_err = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                # 每次尝试生成全新 msToken + a_bogus（风控时会返回空响应，换签名重试）
                ms_token = "".join(random.choices(
                    string.ascii_letters + string.digits, k=182)) + "=="
                params = self._build_params(aweme_id, ms_token)
                signed = self._abogus.generate_abogus(params=params, body="")[0]
                url = DETAIL_API + "?" + signed
                headers = {
                    "User-Agent": UA,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.douyin.com/",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
                if ttwid:
                    headers["Cookie"] = f"ttwid={ttwid}"
                resp = self.session.get(url, timeout=self.timeout, headers=headers)
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}"
                    raise RuntimeError(last_err)
                text = resp.text
                if not text or len(text) < 10:
                    last_err = "空响应 (可能被风控)"
                    raise RuntimeError(last_err)
                j = resp.json()
                if j.get("status_code") != 0:
                    last_err = f"status_code={j.get('status_code')} msg={j.get('status_msg')}"
                    raise RuntimeError(last_err)
                detail = j.get("aweme_detail")
                if not detail:
                    last_err = "无 aweme_detail"
                    raise RuntimeError(last_err)
                return detail
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning(f"[douyin] detail API 失败 ({attempt}/{self.max_retries}) "
                            f"{aweme_id}: {last_err}")
                if attempt < self.max_retries:
                    # 风控/空响应时多等一会儿再换签名重试
                    time.sleep(1.5 * attempt)
        log.warning(f"[douyin] detail API 放弃 {aweme_id}: {last_err}")
        _set_last_error(f"detail API: {last_err}")
        return None

    # ==================================================================
    # 兜底: 分享页正则
    # ==================================================================
    def _fetch_share_page(self, aweme_id: str, kind: str = "video") -> Optional[dict]:
        """iesdouyin 分享页正则提取 (新版页面为混淆 JS, 仅作兜底)"""
        for k in (kind, "video", "note"):
            url = f"https://www.iesdouyin.com/share/{k}/{aweme_id}/"
            try:
                resp = self.session.get(url, timeout=self.timeout,
                                        headers={"User-Agent": UA,
                                                 "Referer": "https://www.douyin.com/"})
                if resp.status_code != 200:
                    continue
                html = resp.text
                m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
                              html, re.S)
                if not m:
                    continue
                data = json.loads(m.group(1))
                detail = (data.get("loaderData") or {}).get(
                    f"video_({aweme_id})/page", {})
                info = detail.get("videoInfoRes") or {}
                items = info.get("item_list") or []
                if items:
                    return items[0]
                # note 结构
                aw = data.get("aweme") or {}
                d = aw.get("detail")
                if d:
                    return d
            except Exception as e:
                log.debug(f"[douyin] 分享页解析失败 {url[:60]}: {e}")
        return None

    # ==================================================================
    # 主入口: parse
    # ==================================================================
    def parse(self, url: str) -> Optional[VideoInfo]:
        """解析抖音链接 -> VideoInfo；失败返回 None（不抛异常）"""
        if not url or not url.strip():
            return None
        url = url.strip()
        _set_last_error("")

        # 1) 短链 -> 标准链接
        canonical = self.resolve_short_url(url)
        if canonical != url:
            log.info(f"[douyin] 短链: {url[:50]} -> {canonical[:70]}")
            url = canonical

        aweme_id = self.extract_aweme_id(url)
        if not aweme_id:
            log.warning(f"[douyin] 无法从链接提取作品 ID: {url[:80]}")
            return None

        # 2) detail API (主路径)
        detail = self._fetch_detail(aweme_id)
        if not detail:
            # 3) 分享页兜底
            kind = "note" if "/note/" in url else "video"
            detail = self._fetch_share_page(aweme_id, kind)

        if detail:
            info = self._to_video_info(detail, url)
            if info and (info.download_url or info.image_urls):
                return info

        # 4) tikwm 兜底 (某些抖音链接 tikwm 能处理)
        try:
            from .tikwm import TikwmClient
            c = TikwmClient(timeout=15, max_retries=1)
            info = c._douyin_post_parse(url)
            if info:
                return info
        except Exception as e:
            log.debug(f"[douyin] tikwm 兜底失败: {e}")

        # 5) yt-dlp 兜底 (若已安装)
        info = self._parse_ytdlp(url)
        if info:
            return info

        err = get_last_error() or "detail API 失败 / 无可用素材地址"
        _set_last_error(f"抖音解析失败: {err}")
        log.error(f"[douyin] 解析放弃: {url[:80]} ({err})")
        return None

    # ==================================================================
    # yt-dlp 兜底
    # ==================================================================
    def _parse_ytdlp(self, url: str) -> Optional[VideoInfo]:
        try:
            import yt_dlp
            opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    return VideoInfo(
                        url=url,
                        video_id=str(info.get("id", "")),
                        title=(info.get("title") or "").strip(),
                        author=(info.get("uploader") or "").strip(),
                        author_id=(info.get("uploader_id") or "").strip(),
                        duration=float(info.get("duration") or 0),
                        thumbnail=(info.get("thumbnail") or "").strip(),
                        content_type=ContentType.VIDEO,
                        download_url=(info.get("url") or "").strip(),
                        extra={"platform": "douyin", "extractor": "yt-dlp",
                               "quality": "sd"},
                    )
        except Exception as e:
            log.warning(f"[douyin] yt-dlp 兜底失败: {e}")
        return None

    # ==================================================================
    # 数据映射: aweme JSON -> VideoInfo
    # ==================================================================
    def _to_video_info(self, d: dict, url: str) -> VideoInfo:
        # 作者
        author = ""
        author_id = ""
        au = d.get("author") or {}
        if isinstance(au, dict):
            author = au.get("nickname") or au.get("unique_id") or ""
            author_id = str(au.get("uid") or au.get("unique_id") or "")

        # 视频
        video = d.get("video") or {}
        play_addr = video.get("play_addr") or {}
        play_list = play_addr.get("url_list") or []
        # 无水印直链优先 (play_addr 即无水印), 其次 play_addr_h264
        download_url = ""
        for u in play_list:
            if u and u.startswith("http"):
                download_url = u
                break
        if not download_url:
            h264 = (video.get("play_addr_h264") or {}).get("url_list") or []
            for u in h264:
                if u and u.startswith("http"):
                    download_url = u
                    break
        if not download_url:
            download_url = (video.get("download_addr") or {}).get("url_list") or []
            if isinstance(download_url, list) and download_url:
                download_url = download_url[0]
            else:
                download_url = ""

        # 图集
        image_urls: List[str] = []
        for img in d.get("images") or []:
            if not isinstance(img, dict):
                continue
            urls = img.get("url_list") or []
            for u in urls:
                if u and u.startswith("http"):
                    image_urls.append(u)
                    break

        # 画质标记
        if image_urls:
            content_type = ContentType.IMAGE
            quality = "image"
        elif download_url:
            content_type = ContentType.VIDEO
            quality = "hd"
        else:
            content_type = ContentType.VIDEO
            quality = "none"

        stats = d.get("statistics") or {}

        # 封面: cover 可能是 dict {height, uri, url_list} 或字符串
        cover = (video.get("cover") or video.get("origin_cover")) or {}
        if isinstance(cover, dict):
            cover_list = cover.get("url_list") or []
            thumbnail = cover_list[0] if cover_list else ""
        else:
            thumbnail = str(cover or "")

        return VideoInfo(
            url=url,
            video_id=str(d.get("aweme_id") or ""),
            title=(d.get("desc") or "").strip(),
            author=author,
            author_id=author_id,
            duration=float(d.get("duration") or 0),
            thumbnail=thumbnail,
            content_type=content_type,
            download_url=download_url,
            image_urls=image_urls,
            create_time=int(d.get("create_time") or 0),
            like_count=int(stats.get("digg_count") or 0),
            comment_count=int(stats.get("comment_count") or 0),
            share_count=int(stats.get("share_count") or 0),
            view_count=int(stats.get("play_count") or 0),
            extra={
                "platform": "douyin",
                "extractor": "douyin-web",
                "webpage_url": url,
                "quality": quality,
                "region": "CN",
                "is_watermarked": False,
            },
        )
