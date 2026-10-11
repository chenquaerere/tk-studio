"""链接在日志里的显示格式。

TikTok / 抖音链接里真正有用的信息在**结尾**（作品 id），开头永远是
`https://www.tiktok.com/@用户名/video/` 这种模板文本。原来日志统一用
`url[:60]` 截断，而一条标准链接正好是 61 个字符：

    https://www.tiktok.com/@pyone.pay42/video/7693929770291105032
    ↑ 60 个字符到这里为止 ─────────────────────────────────┘

2026-10-10 的真实日志里，它就显示成 `…/video/769392977029110503`，
看起来像「采集到的 id 少了一位」这种假故障，排查时被误导过一次
（其实请求发的是完整链接，只是接口恰好撞上限流）。

所以日志统一改用 `short_url()`：超长时省略开头、保留结尾。
"""
from __future__ import annotations

DEFAULT_BUDGET = 64


def short_url(url: str, budget: int = DEFAULT_BUDGET) -> str:
    """缩到 `budget` 字符以内；超出时省略开头，**保留结尾**（作品 id 在这边）。

    budget 太小时（如 5）也要保证能看出点东西：至少保留 8 个字符。
    """
    text = str(url or "")
    if len(text) <= budget:
        return text
    keep = max(8, int(budget) - 3)
    return "..." + text[-keep:]
