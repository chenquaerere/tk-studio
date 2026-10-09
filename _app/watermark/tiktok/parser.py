"""
链接解析器 (纯本地,无网络) v2.0

支持识别 TikTok + 抖音 各类链接格式:
- TikTok 短链:  https://vm.tiktok.com/XXX/  https://vt.tiktok.com/XXX/
- TikTok 标准:  https://www.tiktok.com/@user/video/123  /photo/123  /t/XXX
- 抖音短链:     https://v.douyin.com/XXX/
- 抖音标准:     https://www.douyin.com/video/123  /note/123(图文)  /photo/123
- 抖音分享页:   https://www.iesdouyin.com/share/video/123  /share/note/123
- 抖音发现页:   https://www.douyin.com/discover?modal_id=123
- 抖音用户主页: https://www.douyin.com/user/xxx
- 分享口令(从文本中提取 URL), 自动补 https:// 前缀

【重要】不做短链跳转!短链由解析引擎在 parse_info 时自动 follow redirect。
任何在 parser 里的同步网络请求都会卡住 UI 主线程。
"""
import re
from typing import Optional, List


# TikTok + 抖音 URL 匹配模式
URL_PATTERNS = [
    # ---------- TikTok ----------
    r"https?://vm\.tiktok\.com/[A-Za-z0-9_-]+/?",
    r"https?://vt\.tiktok\.com/[A-Za-z0-9_-]+/?",
    r"https?://www\.tiktok\.com/@[A-Za-z0-9._-]+/video/\d+",
    r"https?://www\.tiktok\.com/@[A-Za-z0-9._-]+/photo/\d+",
    r"https?://m\.tiktok\.com/v/\d+",
    r"https?://www\.tiktok\.com/t/[A-Za-z0-9_-]+/?",
    # ---------- 抖音 ----------
    # 短链
    r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?",
    # 标准链接 (video 视频 / note 图文 / photo 图集)
    r"https?://www\.douyin\.com/video/\d+",
    r"https?://www\.douyin\.com/note/\d+",
    r"https?://www\.douyin\.com/photo/\d+",
    r"https?://www\.douyin\.com/slides/\d+",
    # 移动端
    r"https?://m\.douyin\.com/(?:share/)?(?:video|note|photo)/\d+",
    # 分享页
    r"https?://www\.iesdouyin\.com/share/video/\d+",
    r"https?://www\.iesdouyin\.com/share/note/\d+",
    r"https?://www\.iesdouyin\.com/share/photo/\d+",
    # 发现页
    r"https?://www\.douyin\.com/discover\?modal_id=\d+",
    # 用户主页 (会被解析引擎自动展开)
    r"https?://www\.douyin\.com/user/[A-Za-z0-9_-]+",
]

# 无协议时自动补 https:// 的域名列表
_BARE_HOSTS = (
    "vm.tiktok.com", "vt.tiktok.com", "www.tiktok.com", "m.tiktok.com",
    "tiktok.com", "v.douyin.com", "www.douyin.com", "douyin.com",
    "m.douyin.com", "www.iesdouyin.com", "iesdouyin.com",
)

# 预编译
_COMPILED = [re.compile(p) for p in URL_PATTERNS]


def _normalize_url(url: str) -> str:
    """
    规范化单条 URL:
    - 去除前后空白
    - 自动补 https:// 前缀
    - 去除路径尾的杂字符
    """
    url = url.strip()
    # 去除中文/英文标点
    url = url.rstrip(".,;:!?。，；：！？")

    # 自动补协议
    if not url.startswith(("http://", "https://")):
        if url.startswith(_BARE_HOSTS):
            url = "https://" + url

    return url


def extract_urls_from_text(text: str) -> List[str]:
    """从一段文本中提取所有 TikTok/抖音 链接(纯本地,O(n))"""
    found: List[tuple] = []
    for pattern in _COMPILED:
        for m in pattern.finditer(text):
            found.append((m.start(), _normalize_url(m.group(0))))
    # 按原文位置排序，保证用户粘贴顺序与解析表格序号一致
    found.sort(key=lambda x: x[0])
    urls: List[str] = []
    seen = set()
    for _, u in found:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def is_tiktok_url(url: str) -> bool:
    """判断是否为 TikTok/抖音 链接"""
    url = _normalize_url(url)
    return any(p.search(url) for p in _COMPILED)


def detect_platform(url: str) -> str:
    """识别平台: tiktok / douyin / unknown"""
    if "tiktok.com" in url:
        return "tiktok"
    if "douyin.com" in url or "iesdouyin.com" in url:
        return "douyin"
    return "unknown"


def detect_url_type(url: str) -> str:
    """识别 URL 类型: video / photo / note / short / unknown"""
    if re.search(r"/video/\d+", url):
        return "video"
    if re.search(r"/photo/\d+", url):
        return "photo"
    if re.search(r"/note/\d+", url):
        return "note"
    if re.search(r"(vm|vt|v)\.(tiktok|douyin)\.com|/(t|discover|slides)/", url):
        return "short"
    return "unknown"


def extract_video_id(url: str) -> Optional[str]:
    """从 URL 中提取作品 ID(数字部分); 短链返回 None"""
    m = re.search(r"/(?:video|photo|note|slides)/(\d+)", url)
    return m.group(1) if m else None


def parse_input(text: str) -> List[str]:
    """
    主入口: 解析用户输入,返回 URL 列表

    【纯本地,毫秒级】不做任何网络请求。
    短链会在解析引擎 parse_info 时自动 follow redirect。

    支持:
    - 一行一个 URL
    - 一行多个 URL(用空格/逗号/换行分隔)
    - 纯分享口令(从文本中提取 URL)
    - 自动补 https:// 前缀
    - TikTok + 抖音
    """
    if not text:
        return []

    # 1. 提取 URL(纯正则)
    urls = extract_urls_from_text(text)

    # 2. 如果没匹配到,尝试按行处理(可能用户输入了裸 URL)
    if not urls:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # 整行就一个 URL 的情况
            norm = _normalize_url(line)
            if is_tiktok_url(norm):
                urls.append(norm)

    # 3. 去重,保持顺序
    return list(dict.fromkeys(urls))


def parse_input_quick_count(text: str, limit: int = 10000) -> int:
    """
    快速估算链接数(用于大文本场景,提前停止以避免 UI 卡顿)
    """
    if len(text) > limit * 100:
        text = "\n".join(text.splitlines()[:limit])
    return len(parse_input(text))


if __name__ == "__main__":
    test_inputs = [
        "https://www.tiktok.com/@user/video/7234567890123456789",
        "https://vm.tiktok.com/ABC123/",
        # 抖音
        "https://v.douyin.com/L4FJNR3/",
        "https://www.douyin.com/video/7234567890123456789",
        "https://www.douyin.com/note/7546907495592168714",
        "https://www.iesdouyin.com/share/note/7546907495592168714",
        "2.84 nqe:/ 测试 https://v.douyin.com/L4FJNR3/ 复制此链接",
        # 无协议
        "vm.tiktok.com/ABC123/",
        "v.douyin.com/L4FJNR3/",
        "tiktok.com/@user/video/123",
        # 各种分享口令
        "8.94 mDu:/ test https://www.tiktok.com/t/ZMxxxxx/ 复制链接",
        "0.43 复制打开抖音 https://v.douyin.com/L4FJNR3/",
    ]
    for inp in test_inputs:
        urls = parse_input(inp)
        plat = detect_platform(urls[0]) if urls else "无"
        print(f"[{plat:>7}] {inp[:50]!r:50}")
        for u in urls:
            print(f"           -> {u}")
