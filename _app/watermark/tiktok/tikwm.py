"""
tikwm.com 公共 API 客户端 (v3.2)

设计目标:
- 唯一下载路径 (yt-dlp 完全不再下载,只留解析兜底,甚至兜底也基本不用)
- 强制走 MagicHut 11304 HTTP 代理;用户配的 socks5://127.0.0.1:11305 自动切到 11304
- 干净命名: {idx:03d}_{author[:20]}_{title[:30]}_{video_id}.mp4
            图集: {idx:03d}_{author[:20]}_{title[:30]}_{video_id}_{序号:02d}.jpeg
- 严禁写 .info.json / .description / .vtt / 封面 .jpg,只留 .mp4 / .jpg
- 每 5% 触发 on_progress 回调,流式下载,大文件不卡
- 重试 2 次 (用户网络不稳场景)
- 全局速率控制: tikwm 公共 API 限流 1 req/s,所有 parse 请求串行排队
- v3.2: POST 加 hd=1, _to_video_info 优先级 hdplay > play > wmplay,
       extra 里记录 quality="hd"|"sd"|"wm", hd_size, size, wm_size
       默认 hd_mode=True (无水印 + 尽量高清)

API: POST https://www.tikwm.com/api/   form: url=<分享链接>&hd=1
"""
import re
import time
import threading
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any

import requests

from .models import VideoInfo, ContentType
from .logger import log
from .parse_cache import is_stale_status

# 从异常文本里兜底提取 HTTP 状态码（requests 的 str(HTTPError) 形如 "403 Client Error..."）
_STALE_HINT_RE = re.compile(r"\b(401|403|404|410)\b")


# ---------------------------------------------------------------------------
# 代理自动探测
# ---------------------------------------------------------------------------
# 候选代理端口(按流行度排)
PROXY_CANDIDATES = [
    "http://127.0.0.1:7890",      # Clash
    "http://127.0.0.1:10809",     # V2RayN HTTP
    "http://127.0.0.1:1080",      # 通用 SOCKS(先试 http)
    "http://127.0.0.1:8888",      # 旧 SS
    "socks5://127.0.0.1:11305",   # MagicHut SOCKS
    "socks5://127.0.0.1:10808",   # V2Ray SOCKS
    "http://127.0.0.1:11304",     # MagicHut HTTP
]

_last_detected_proxy = ""


def test_proxy_connection(proxy_url: str, timeout: float = 3.0) -> bool:
    """测试代理是否可用"""
    try:
        resp = requests.get(
            "https://www.tiktok.com/",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return resp.status_code < 500
    except Exception:
        return False


def detect_best_proxy(on_found=None) -> str:
    """按顺序探测最佳代理,返回第一个可用的 URL,没有则返回空字符串"""
    global _last_detected_proxy

    # 1) 先检查系统代理
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if "=" in server:
                    parts = dict(
                        p.split("=", 1) for p in server.split(";") if "=" in p
                    )
                    server = parts.get("https") or parts.get("http") or ""
                if server and not server.startswith("http"):
                    server = "http://" + server
                if server and test_proxy_connection(server, timeout=2):
                    _last_detected_proxy = server
                    if on_found:
                        on_found(server)
                    return server
    except Exception:
        pass

    # 2) 遍历候选列表
    for proxy in PROXY_CANDIDATES:
        if test_proxy_connection(proxy, timeout=2):
            _last_detected_proxy = proxy
            if on_found:
                on_found(proxy)
            return proxy

    return ""


def start_async_detect(on_found=None):
    """后台线程启动探测"""
    threading.Thread(
        target=lambda: detect_best_proxy(on_found), daemon=True
    ).start()


def get_detected_proxy() -> str:
    """获取已探测到的代理(可能为空)"""
    return _last_detected_proxy


# v4.0：改为「被动代理发现」。
#
# 为什么不再自动做网络探测：
#   原实现（以及我最初写的后台探测）会去请求 https://www.tiktok.com/ 判断
#   代理是否可用。在「没有代理 / 网络受限」的机器上，这些请求会一直等到
#   超时，多个候选串起来能卡几十秒。用户看到的却是「点了没反应」。
#
# 现在的策略（全部是本地读取，零网络开销）：
#   1. 调用方显式给了代理 → 用它；
#   2. 读 Windows 系统代理设置 → 有就说明用户在用代理，直接采用；
#   3. 都没用 → 直连（不探测、不等待）。
#
# 想主动扫描候选端口时，显式调用 `detect_best_proxy()` 或
# `start_async_detect()`（保留原接口），它不会影响普通请求路径。
_def_proxy_checked = False


def system_proxy() -> str:
    """读取 Windows 系统代理设置（纯本地注册表读取，不发网络请求）。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            if not server:
                return ""
            if "=" in server:
                parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
                server = parts.get("https") or parts.get("http") or ""
            server = (server or "").strip()
            if server and not server.startswith(("http://", "https://", "socks")):
                server = "http://" + server
            return server
    except Exception:
        return ""


def ensure_proxy_detected(explicit: str = "") -> str:
    """返回当前应使用的代理（可能为空 = 直连）。

    纯本地判断，绝不发网络请求，因此**不会阻塞请求路径**。
    """
    global _def_proxy_checked, _last_detected_proxy
    if explicit:
        return normalize_proxy(explicit)
    if _last_detected_proxy:
        return _last_detected_proxy
    if not _def_proxy_checked:
        _def_proxy_checked = True
        p = system_proxy()
        if p:
            _last_detected_proxy = p
            log.info(f"[tikwm] 采用系统代理: {p}")
    return _last_detected_proxy


def proxied(proxy_url: str) -> Optional[Dict[str, str]]:
    """构造 requests 的 proxies 参数；直连时返回 None（而不是空串字典）。"""
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


# ---------------------------------------------------------------------------

TIKWM_API = "https://www.tikwm.com/api/"

WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# v4.0: 不再硬编码某个第三方本地代理。
# 原来 DEFAULT_PROXY 写死 http://127.0.0.1:11304，导致「没装该工具的用户」
# 每条链接都要先在代理上超时重试再回退，10~20 秒/链接。
# 现在改为：优先用用户显式配置的代理 → 其次自动探测结果 → 否则直连。
DEFAULT_PROXY = ""   # 兼容旧引用；真正的取值走 resolve_proxy()

# tikwm 公共 API 限流: 1 request/second
# 用「条件变量 + 预约时间槽」实现，既不持锁 sleep，也不会忙等。
_RATE_COND = threading.Condition()
_LAST_REQUEST_AT = 0.0
_MIN_INTERVAL = 1.15  # 1.1s 留 50ms 余量


def _throttle():
    """全局限流：保证相邻请求间隔 >= _MIN_INTERVAL。

    v4.0 修复了两个问题：
    1. 原来 `with _RATE_LOCK:` 把 sleep 也包在锁里 → 所有线程被串行化，
       每个请求的等待被放大成「前一个请求完整耗时 + 1.15s」。
    2. 不能简单改成「锁内占位 + 锁外 sleep」—— 多个线程可能算出同一个
       占位时间，先醒来的线程会因为时间还没到而**忙等空转**（实测会把
       解释器退出也拖住）。
    现在用条件变量：等够时间才返回，线程既不空转也不互相阻塞。
    """
    global _LAST_REQUEST_AT
    with _RATE_COND:
        while True:
            wait = _MIN_INTERVAL - (time.monotonic() - _LAST_REQUEST_AT)
            if wait <= 0:
                _LAST_REQUEST_AT = time.monotonic()
                _RATE_COND.notify_all()
                return
            _RATE_COND.wait(timeout=wait)


def _is_rate_limit_error(msg: str) -> bool:
    """Detect tikwm rate-limit errors; they need a longer retry wait."""
    m = (msg or "").lower()
    return (
        "free api limit" in m
        or "1 request/second" in m
        or "too many requests" in m
    )


def _is_douyin_url(url: str) -> bool:
    """v4.0: 检测是否为抖音链接"""
    return bool(re.search(r'douyin\.com|iesdouyin', url, re.IGNORECASE))


# v2.1: 最近一次解析失败原因（供 UI 显示）
_LAST_ERROR = ""


def _set_last_error(msg: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = msg or ""


def get_last_error() -> str:
    """返回最近一次解析失败的原因。"""
    return _LAST_ERROR


def normalize_proxy(proxy: str) -> str:
    """
    socks5://127.0.0.1:11305  ->  http://127.0.0.1:11304
    11305                       ->  http://127.0.0.1:11304 (假定是 MagicHut SOCKS 端口)
    11304                       ->  http://127.0.0.1:11304
    http://127.0.0.1:7890       ->  http://127.0.0.1:7890 (原样)
    "" / None                   ->  "" (由调用方决定兜底)
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        scheme, rest = proxy.split("://", 1)
        if scheme.lower().startswith("socks"):
            # SOCKS 改同 host 的 11304 (MagicHut 的 HTTP 入口)
            if ":" in rest:
                _host, _port = rest.rsplit(":", 1)
                if _port == "11305":
                    return f"http://{_host}:11304"
            return f"http://{rest}"
        if not scheme:
            return f"http://{rest}"
        return proxy
    if proxy.isdigit():
        # 纯数字端口: 11305 -> 11304, 11304 -> 11304, 其他保持
        if proxy == "11305":
            return "http://127.0.0.1:11304"
        return f"http://127.0.0.1:{proxy}"
    if ":" in proxy and not proxy.startswith("["):
        return f"http://{proxy}"
    return proxy


# Windows 保留设备名：这些名字（含带扩展名形式）不能作为文件名
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_name(s: str, max_len: int = 40) -> str:
    """过滤文件系统非法字符，压缩空白，截断到 max_len。

    v4.0 加固：
    - 处理 Windows 保留设备名（CON/NUL/COM1..9/LPT1..9）——
      否则创建这些名字会直接失败或行为异常；
    - 处理结尾的点号/空格（Windows 会静默去掉，导致路径对不上）；
    - emoji / 代理对字符按「字符」而非「字节」截断，避免切出半个字符。
    """
    if not s:
        return ""
    # 去非法字符（含控制字符）
    s = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", str(s))
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("._- ")
    if not s:
        return ""
    # 截断时按 Unicode 字符切，并把结尾可能残留的点/空格再去一次
    s = s[:max_len].strip("._- ")
    if not s:
        return ""
    # 保留设备名检测（取主名，忽略扩展名）
    stem = s.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        s = "_" + s
    return s


def _fit_path(dir_path: "Path", name: str, suffix: str, budget: int = 250) -> str:
    """保证 dir_path/name+suffix 的总长度不超过 Windows MAX_PATH 预算。

    路径太长（emiji 标题 + 日期目录容易超 260）会让 open() 抛
    FileNotFoundError，而错误信息完全看不出真正原因。
    这里按需截断文件名主体，必要时追加短哈希保证唯一。
    """
    import hashlib

    try:
        dir_len = len(str(dir_path)) + 1        # +1 = 路径分隔符
    except Exception:
        dir_len = 0
    room = max(24, budget - dir_len - len(suffix))
    if len(name) <= room:
        return name + suffix
    tag = hashlib.md5(name.encode("utf-8", "ignore")).hexdigest()[:8]
    keep = max(8, room - len(tag) - 1)
    return name[:keep].rstrip("._- ") + "_" + tag + suffix


# ---------------------------------------------------------------------------
# Local short-link resolution (thread-safe, cached 1h)
# ---------------------------------------------------------------------------
# Only TikTok short links (vm/vt/m/t) are resolved locally. Douyin short
# links are left to the tikwm server, to avoid regional blocking by douyin
# when the local proxy exits overseas.
_SHORT_LINK_RE = re.compile(
    r"https?://(?:vm|vt|m)\.tiktok\.com/\S+|"
    r"https?://www\.tiktok\.com/t/\S+",
    re.IGNORECASE,
)
_SHORT_LINK_TTL = 3600.0  # video/photo IDs are immutable, cache is safe
_short_link_cache: Dict[str, tuple] = {}
_short_link_lock = threading.Lock()


def resolve_short_url(url: str, proxies: Optional[Dict[str, str]] = None,
                      timeout: float = 8.0) -> str:
    """Follow short-link redirects to a canonical URL; cache result 1h.

    Non-short links are returned unchanged. On failure the original URL is
    returned so the caller can still try the tikwm server.
    """
    if not url or not _SHORT_LINK_RE.search(url):
        return url
    now = time.monotonic()
    with _short_link_lock:
        cached = _short_link_cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
    resolved = url
    try:
        with requests.get(
            url, allow_redirects=True, timeout=timeout, stream=True,
            proxies=proxies or {},
            headers={"User-Agent": WEB_UA},
        ) as resp:
            if resp.url:
                resolved = resp.url
    except Exception as e:
        log.debug(f"[tikwm] short link resolve failed, fallback: {url[:60]} - {e}")
    with _short_link_lock:
        _short_link_cache[url] = (time.monotonic() + _SHORT_LINK_TTL, resolved)
    return resolved


class TikwmClient:
    """
    tikwm.com 客户端 (v3.1)

    强制约定:
    - 默认走 11304 HTTP 代理
    - parse() 返回 VideoInfo
    - download(info, out_dir, idx, on_progress) 下载到 out_dir
    - 失败抛异常 (Downloader 那边接住)
    """

    def __init__(
        self,
        proxy_url: str = "",
        timeout: int = 30,
        max_retries: int = 2,
        hd_mode: bool = True,
    ):
        if proxy_url:
            self.proxy_url = normalize_proxy(proxy_url)
        else:
            # v4.0：懒探测，不阻塞首次请求；探测到之后再走代理
            self.proxy_url = ensure_proxy_detected()
        self.timeout = timeout
        self.max_retries = max_retries
        self.hd_mode = bool(hd_mode)  # v3.2: HD 画质默认开

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": WEB_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://www.tikwm.com",
            "Referer": "https://www.tikwm.com/",
        })

        # 直连时传 None，避免 requests 收到 {"http": "", "https": ""} 报错
        self._proxies: Optional[Dict[str, str]] = proxied(self.proxy_url)

    def refresh_proxy(self) -> None:
        """后台探测到代理后调用：把已存在的 session 切到代理。"""
        p = ensure_proxy_detected()
        if p and p != self.proxy_url:
            self.proxy_url = p
            self._proxies = proxied(p)

    # ====================================================================
    # 1. 解析
    # ====================================================================
    def parse(self, url: str) -> Optional[VideoInfo]:
        """
        解析单条 TikTok/Douyin 链接 -> VideoInfo
        失败返回 None (不抛, 让上层决定要不要 fallback)

        v2.0: 抖音链接直接转 DouyinClient（本地 a_bogus 解析），
              tikwm API 不支持抖音链接，旧版 yt-dlp 兜底已由
              DouyinClient 的完整兜底链取代。
        """
        if not url:
            return None
        url = url.strip()
        if not url:
            return None

        # 抖音: 转独立引擎 (直连, 不走海外代理)
        if _is_douyin_url(url):
            from .douyin import DouyinClient
            log.info(f"[tikwm] 抖音链接转 DouyinClient: {url[:60]}")
            try:
                return DouyinClient(timeout=self.timeout,
                                    max_retries=max(2, self.max_retries)).parse(url)
            except Exception as e:
                log.warning(f"[tikwm] DouyinClient 异常: {e}")
            return None

        # Resolve short links locally (cached) to improve parse success rate.
        canonical = resolve_short_url(url, proxies=self._proxies)
        if canonical != url:
            log.info(f"[tikwm] short link: {url[:60]} -> {canonical[:60]}")
            url = canonical

        last_err = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                _throttle()
                resp = self.session.post(
                    TIKWM_API,
                    data={"url": url, "hd": "1"} if self.hd_mode else {"url": url},
                    proxies=self._proxies,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {resp.status_code}: {resp.text[:120]}"
                    )
                j = resp.json()
                if j.get("code") != 0:
                    raise RuntimeError(
                        f"tikwm error: code={j.get('code')} msg={j.get('msg')}"
                    )
                data = j.get("data") or {}
                if not data:
                    raise RuntimeError("tikwm 返回空 data")
                return self._to_video_info(data, url)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning(
                    f"[tikwm] 解析失败 ({attempt}/{self.max_retries}) "
                    f"{url[:60]}  -> {last_err}"
                )
                if attempt < self.max_retries:
                    # Rate-limit errors need a longer wait before retry.
                    if _is_rate_limit_error(last_err):
                        time.sleep(5.0)
                    else:
                        time.sleep(0.8 * attempt)

        # v2.1: 代理路径失败后自动直连重试一次
        # (tikwm.com 位于 Cloudflare 后, 国内部分网络直连可达;
        #  用户配置了代理但代理失效/未开启时, 直连可能成功)
        if self.proxy_url:
            log.info(f"[tikwm] 代理路径失败({last_err[:70]}), 尝试直连兜底")
            try:
                _throttle()
                resp = self.session.post(
                    TIKWM_API,
                    data={"url": url, "hd": "1"} if self.hd_mode else {"url": url},
                    proxies={},
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    j = resp.json()
                    if j.get("code") == 0 and j.get("data"):
                        log.info("[tikwm] 直连兜底成功")
                        return self._to_video_info(j["data"], url)
                    last_err = (f"tikwm error: code={j.get('code')} "
                                f"msg={j.get('msg')}")
                else:
                    last_err = f"直连 HTTP {resp.status_code}"
            except Exception as e2:
                last_err = f"直连兜底失败: {type(e2).__name__}: {e2}"
                log.warning(last_err)

        _set_last_error(last_err)
        log.error(f"[tikwm] 解析放弃 {url[:60]}: {last_err}")
        return None

    def _douyin_post_parse(self, url: str) -> Optional[VideoInfo]:
        """
        纯 tikwm POST 解析（供 DouyinClient 兜底调用，避免循环路由）。
        部分抖音链接 tikwm 也能解析成功。
        """
        last_err = ""
        for attempt in range(1, max(1, self.max_retries) + 1):
            try:
                _throttle()
                resp = self.session.post(
                    TIKWM_API,
                    data={"url": url, "hd": "1"} if self.hd_mode else {"url": url},
                    proxies=self._proxies,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                j = resp.json()
                if j.get("code") != 0:
                    raise RuntimeError(
                        f"tikwm error: code={j.get('code')} msg={j.get('msg')}"
                    )
                data = j.get("data") or {}
                if not data:
                    raise RuntimeError("tikwm 返回空 data")
                return self._to_video_info(data, url)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.debug(f"[tikwm] douyin 兜底失败 ({attempt}): {last_err}")
                if attempt < self.max_retries:
                    time.sleep(1.0 * attempt)
        _set_last_error(f"tikwm 兜底: {last_err}")
        return None

    def _parse_douyin_fallback(self, url: str) -> Optional[VideoInfo]:
        """v4.0: yt-dlp 兜底解析抖音链接"""
        try:
            import yt_dlp
            opts = {
                "quiet": True, "no_warnings": True,
                "proxy": self.proxy_url if self.proxy_url else None,
                "extract_flat": False,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    download_url = (info.get("url") or "").strip()
                    thumb = (info.get("thumbnail") or "").strip()
                    return VideoInfo(
                        url=url,
                        video_id=info.get("id", ""),
                        title=(info.get("title") or "").strip(),
                        author=(info.get("uploader") or "").strip(),
                        author_id=(info.get("uploader_id") or "").strip(),
                        duration=float(info.get("duration") or 0),
                        thumbnail=thumb,
                        content_type=ContentType.VIDEO,
                        download_url=download_url,
                        extra={
                            "platform": "douyin",
                            "extractor": "yt-dlp",
                            "webpage_url": info.get("webpage_url", url),
                            "quality": "sd",
                        },
                    )
        except Exception as e:
            log.warning(f"[tikwm] yt-dlp 抖音兜底失败: {e}")
        return None

    # ====================================================================
    # 2. 下载 (核心)
    # ====================================================================
    def download(
        self,
        info: VideoInfo,
        output_dir: str | Path,
        idx: int = 1,
        on_progress: Optional[Callable[[int, int], None]] = None,
        image_sel: Optional[List[int]] = None,
    ) -> str:
        """
        下载到 output_dir,文件名按 {idx:03d}_{author}_{title}_{video_id}.mp4 规则

        image_sel: 图集内要下载的图片序号（1-based,可选子集;None=全部）

        on_progress(downloaded_bytes, total_bytes)
            - 视频: total = Content-Length
            - 图集: total = 所有图片大小之和,按下载进度累加
            - 每 5% 触发一次,避免 UI 刷太狠

        返回: 主文件路径 (视频: 视频文件; 图集: 第一张图片的路径)
        失败抛异常 (含详细原因)

        v2.0: 抖音素材直连下载(不走海外代理), 图片扩展名按 Content-Type 自适应
              (抖音图集为 webp)。
        """
        if not info:
            raise RuntimeError("VideoInfo 为空")

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # v2.0: 抖音素材直连, TikTok 素材走代理
        is_douyin = str(info.extra.get("platform", "")) == "douyin"
        proxies = {} if is_douyin else self._proxies

        # 安全文件名
        author = _safe_name(info.author, max_len=20) or "unknown"
        title = _safe_name(info.title, max_len=30) or "video"
        vid = (info.video_id or str(int(time.time() * 1000))).strip()
        idx_p = max(1, int(idx))

        # 视频
        if info.content_type == ContentType.VIDEO and info.download_url:
            # v4.0: 用 _fit_path 保证总长度不超 Windows MAX_PATH 预算，
            # 否则 emoji/超长标题 + 日期目录会直接 open() 失败。
            base = f"{idx_p:03d}_{author}_{title}_{vid}"
            filename = _fit_path(out, base, ".mp4")
            target = out / filename
            self._download_file(
                info.download_url, target, on_progress, label=f"video:{vid}",
                proxies=proxies,
            )
            return str(target)

        # 图集: 图片直接放 out 目录,不创建子目录
        # 命名: {idx:03d}_{author}_{title}_{vid}_{序号:02d}.<ext>
        if info.content_type == ContentType.IMAGE and info.image_urls:
            # 支持只下载指定图片（image_sel 为 1-based 序号子集）
            sel = [int(x) for x in (image_sel or [])
                   if 1 <= int(x) <= len(info.image_urls)]
            indices = sorted(set(sel)) if sel else list(range(1, len(info.image_urls) + 1))
            total_imgs = len(indices)
            saved_paths: List[str] = []
            for n, i in enumerate(indices, start=1):
                img_url = info.image_urls[i - 1]
                suffix = ".webp" if is_douyin else ".jpeg"
                # v4.0: 图集文件名也要走路径预算，避免超长路径
                fname = _fit_path(out, f"{idx_p:03d}_{author}_{title}_{vid}_{i:02d}",
                                  suffix)
                fpath = out / fname
                # v2.1.2: 原始命名已存在 -> 直接复用，不生成重复副本
                if fpath.exists() and fpath.stat().st_size > 1024:
                    saved_paths.append(str(fpath))
                    continue
                # 同名文件已存在(极少情况),加序号
                counter = 1
                while fpath.exists():
                    fname = _fit_path(
                        out,
                        f"{idx_p:03d}_{author}_{title}_{vid}_{i:02d}_{counter}",
                        suffix)
                    fpath = out / fname
                    counter += 1
                # 单图下载: on_progress 把单图进度映射成全局 (n-1)/N ~ n/N
                def _wrap(d: int, t: int, _i: int = n, _n: int = total_imgs):
                    if on_progress is None:
                        return
                    if t <= 0:
                        on_progress(_i, _n)
                        return
                    frac = (_i - 1) / _n + (d / t) / _n
                    on_progress(int(frac * 1000), 1000)

                self._download_file(
                    img_url, fpath, _wrap, label=f"img{i}/{total_imgs}:{vid}",
                    proxies=proxies,
                )
                saved_paths.append(str(fpath))

            if on_progress is not None:
                on_progress(1000, 1000)
            if not saved_paths:
                raise RuntimeError("图集全部图片下载失败")
            return saved_paths[0]  # 主路径: 第一张图

        raise RuntimeError(
            f"无可下载资源: type={info.content_type.value} url={info.url[:60]}"
        )

    # ====================================================================
    # 内部: 单文件下载 (重试 + 进度)
    # ====================================================================
    def _download_file(
        self,
        url: str,
        target: Path,
        on_progress: Optional[Callable[[int, int], None]] = None,
        label: str = "",
        proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        单文件下载,失败抛异常 (调用方 try/except)
        - 已存在且非空 -> 跳过
        - Content-Length 校验: 实际下载 < 95% 视为不完整
        - 5% 步进触发 on_progress
        - v2.0: proxies 可传 {} 表示直连; 图片扩展名按 Content-Type 自适应
        - v2.1.1: 按 CDN 域名设置正确的 Referer (修复抖音视频 403 防盗链)
        """
        if target.exists() and target.stat().st_size > 1024:
            log.info(f"[tikwm] 跳过已存在: {target.name}")
            # v4.0：以前这里也报 100%，让上层以为「刚下载完成」。
            # 现在只发一次进度，不谎报完成（内容本来就在本地）。
            if on_progress is not None:
                on_progress(1000, 1000)
            return

        if proxies is None:
            proxies = self._proxies
        tmp = target.with_suffix(target.suffix + ".part")
        last_err = ""

        # v2.1.1: 抖音 CDN 防盗链校验 Referer，必须覆盖掉 tikwm 的 Referer
        dl_headers: Optional[Dict[str, str]] = None
        if "douyinvod.com" in url or "douyinpic.com" in url or "douyinstatic.com" in url:
            dl_headers = {"Referer": "https://www.douyin.com/", "User-Agent": WEB_UA}
        elif "tiktokcdn" in url or "tiktok.com" in url:
            dl_headers = {"Referer": "https://www.tiktok.com/", "User-Agent": WEB_UA}

        for attempt in range(1, self.max_retries + 1):
            try:
                with self.session.get(
                    url,
                    proxies=proxies,
                    timeout=self.timeout,
                    stream=True,
                    allow_redirects=True,
                    headers=dl_headers,
                ) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length", 0) or 0)

                    downloaded = 0
                    last_pct = -1
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            if on_progress is not None and total > 0:
                                pct = downloaded * 100 // total
                                if pct - last_pct >= 5 or pct == 100:
                                    last_pct = pct
                                    try:
                                        on_progress(downloaded, total)
                                    except Exception:
                                        log.debug("下载进度回调异常", exc_info=True)

                    if total > 0 and downloaded < total * 0.95:
                        raise RuntimeError(
                            f"下载不完整: {downloaded}/{total} bytes"
                        )
                    if downloaded == 0:
                        raise RuntimeError("下载为空 (0 bytes)")

                    # v2.0: 按 Content-Type 修正图片扩展名 (webp/jpeg/png)
                    final = target
                    ct = (resp.headers.get("Content-Type") or "").lower()
                    if target.suffix.lower() in (".jpeg", ".webp", ".png", ".jpg"):
                        if "webp" in ct and target.suffix.lower() != ".webp":
                            final = target.with_suffix(".webp")
                        elif "png" in ct and target.suffix.lower() != ".png":
                            final = target.with_suffix(".png")
                        elif ("jpeg" in ct or "jpg" in ct) and \
                                target.suffix.lower() not in (".jpg", ".jpeg"):
                            final = target.with_suffix(".jpg")

                    if final != target and final.exists() and final.stat().st_size > 1024:
                        # 目标真实扩展名已存在。
                        # v4.0 修复：原来这里把刚下好的 tmp 直接删掉并报 100%，
                        # 属于「假成功」—— 用户以为重下了一次，其实用的是旧文件。
                        # 现在改为：用新下载的内容覆盖（同目录原子替换）。
                        tmp.replace(final)
                        log.info(f"[tikwm] 已覆盖同名文件: {final.name}")
                        if on_progress is not None:
                            try:
                                on_progress(downloaded, max(downloaded, total))
                            except Exception:
                                log.debug("下载完成回调异常", exc_info=True)
                        return

                    tmp.replace(final)
                    log.info(
                        f"[tikwm] OK ({downloaded/1024:.0f}KB) "
                        f"[{label or final.name}]"
                    )
                    if on_progress is not None:
                        try:
                            on_progress(downloaded, max(downloaded, total))
                        except Exception:
                            log.debug("下载完成回调异常", exc_info=True)
                    return

            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.warning(
                    f"[tikwm] 下载失败 ({attempt}/{self.max_retries}) "
                    f"[{label or target.name}]: {last_err}"
                )
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except Exception:
                        log.debug("清理临时文件异常", exc_info=True)
                # v4.0: 401/403/410/404 说明这个签名直链已经失效，
                # 继续重试只是白等（原来会把 3 次重试全耗在死链上）。
                # 直接抛出，让上层清掉缓存后重新解析拿新地址。
                code = None
                resp = getattr(e, "response", None)
                if resp is not None:
                    code = getattr(resp, "status_code", None)
                if code is None:
                    m = _STALE_HINT_RE.search(str(e) or "")
                    if m:
                        code = int(m.group(1))
                if code is not None and is_stale_status(int(code)):
                    raise RuntimeError(
                        f"资源地址已过期（HTTP {code}），请重新解析该链接"
                    ) from e
                if attempt < self.max_retries:
                    time.sleep(0.6 * attempt)

        raise RuntimeError(
            f"下载失败 (重试 {self.max_retries} 次后放弃): {last_err}"
        )

    # ====================================================================
    # 内部: tikwm JSON -> VideoInfo
    # ====================================================================
    def _to_video_info(self, data: Dict[str, Any], url: str) -> VideoInfo:
        images = data.get("images") or []
        play_url = (data.get("play") or "").strip()
        wmplay_url = (data.get("wmplay") or "").strip()
        hdplay_url = (data.get("hdplay") or "").strip()  # v3.2
        size = int(data.get("size") or 0)
        wm_size = int(data.get("wm_size") or 0)
        hd_size = int(data.get("hd_size") or 0)

        # v3.2: 画质优先级 hdplay > play > wmplay
        # - 有图集: 走图集分支
        # - hdplay 存在且 hd_size > 0 (部分场景 hd_size=0 但 hdplay 是真 url) -> HD
        # - 否则 play (无水印) -> SD
        # - 再否则 wmplay (有水印) -> WM
        if images:
            content_type = ContentType.IMAGE
            download_url = ""
            quality = "image"
        elif hdplay_url:
            content_type = ContentType.VIDEO
            download_url = hdplay_url
            quality = "hd"
        elif play_url:
            content_type = ContentType.VIDEO
            download_url = play_url
            quality = "sd"
        elif wmplay_url:
            # 兜底: 无 hdplay/play 但有 wmplay (有水印), 仍标 VIDEO
            content_type = ContentType.VIDEO
            download_url = wmplay_url
            quality = "wm"
        else:
            content_type = ContentType.VIDEO
            download_url = ""
            quality = "none"

        # 作者
        author_obj = data.get("author") or {}
        if isinstance(author_obj, dict):
            author = (
                author_obj.get("nickname")
                or author_obj.get("unique_id")
                or ""
            )
            author_id = author_obj.get("unique_id") or author_obj.get("id") or ""
        else:
            author = str(author_obj) if author_obj else ""
            author_id = ""

        # 音乐
        music_url = (data.get("music") or "").strip()
        music_title = ""
        music_info = data.get("music_info") or {}
        if isinstance(music_info, dict):
            music_title = music_info.get("title") or ""
            if not music_url:
                music_url = (music_info.get("play") or "").strip()

        # 图集 URL 列表
        image_urls: List[str] = []
        for img in images:
            if isinstance(img, str):
                if img:
                    image_urls.append(img)
            elif isinstance(img, dict):
                u = img.get("url") or img.get("image_url") or ""
                if u:
                    image_urls.append(u)

        # 缩略图 (优先 origin_cover 静态图)
        thumbnail = (
            data.get("origin_cover")
            or data.get("cover")
            or data.get("ai_dynamic_cover")
            or ""
        )

        return VideoInfo(
            url=url,
            video_id=str(data.get("id") or ""),
            title=(data.get("title") or "").strip(),
            author=author,
            author_id=str(author_id) if author_id else "",
            duration=float(data.get("duration") or 0),
            thumbnail=thumbnail,
            content_type=content_type,
            download_url=download_url,
            image_urls=image_urls,
            music_url=music_url,
            music_title=music_title,
            create_time=int(data.get("create_time") or 0),
            like_count=int(data.get("digg_count") or 0),
            comment_count=int(data.get("comment_count") or 0),
            share_count=int(data.get("share_count") or 0),
            view_count=int(data.get("play_count") or 0),
            extra={
                "platform": "douyin" if _is_douyin_url(url) else "tiktok",
                "extractor": "tikwm",
                "webpage_url": url,
                "size": size,
                "wm_size": wm_size,
                "hd_size": hd_size,           # v3.2
                "quality": quality,            # v3.2: "hd" | "sd" | "wm" | "image" | "none"
                "hd_url": hdplay_url,          # v3.2: 备查
                "play_url": play_url,          # v3.2: 备查
                "wm_url": wmplay_url,          # v3.2: 备查
                "region": data.get("region", ""),
                "is_watermarked": bool(wmplay_url and not play_url and not hdplay_url),
            },
        )



