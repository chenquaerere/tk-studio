"""采集引擎异常类型。"""


class CollectorError(Exception):
    """采集引擎基础异常。"""


class LoginRequired(CollectorError):
    """需要人工登录/验证（CAPTCHA / 重新登录 / 访问限制）。"""


class RateLimited(CollectorError):
    """触发 TikTok 限流（HTTP 429 / 请求过于频繁），应退避等待后重试。"""


class SiteBlocked(CollectorError):
    """TikTok 拒绝返回主页作品列表（HTTP 200 + 空响应 / 页面报错）。

    2026-10-12 实测：匿名访问时 TikTok 对 `/api/post/item_list/` 返回 200 + 0 字节，
    或只回 TotalCount:0 形状的 JSON，页面渲染「Something went wrong」。
    账号**并非没有作品**，只是这一批请求被拒 —— 应退避后重试，而不是当作
    「今日没有作品」把旧记录清空。
    """
