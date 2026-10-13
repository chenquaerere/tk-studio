"""ProfileCollector —— 账号主页作品采集（P10）。

组合 BrowserManager / SessionManager / TikTokProvider / VideoParser，
完成单账号主页作品的流式采集。

关键设计：
- 单账号串行：一次只处理一个账号，采集完（或失败）才进入下一个。
- 数据来源优先网络响应（/api/post/item_list/），不依赖 DOM 相对时间。
- 通过 on_item(item) -> bool 回调把每条作品交给上层（引擎）做日期判定/去重/入库；
  回调返回 False 表示「达到数量或确认无更多目标日期作品」，立即停止本账号。
- 滚动触发 item_list 分页；连续多次滚动无新作品视为「没有更多作品」。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from collector.core.models import Account, ParsedVideoItem

from .exceptions import RateLimited, SiteBlocked

# on_item 回调：返回 True 继续采集，False 停止本账号
ItemCallback = Callable[[ParsedVideoItem], bool]


class ProfileCollector:
    def __init__(
        self,
        browser,
        session,
        provider,
        parser,
        initial_wait_ms: int = 4000,
        nickname_wait_ms: int = 10000,
        scroll_wait_ms: int = 1200,
        max_scrolls: int = 30,
        idle_scrolls_before_stop: int = 4,
    ):
        self.browser = browser
        self.session = session
        self.provider = provider
        self.parser = parser
        self.initial_wait_ms = initial_wait_ms
        self.nickname_wait_ms = nickname_wait_ms
        self.scroll_wait_ms = scroll_wait_ms
        self.max_scrolls = max_scrolls
        self.idle_scrolls_before_stop = idle_scrolls_before_stop

    def fetch_nickname(self, account: Account) -> str:
        """只取账号昵称（TikTok 显示名），不采集作品。

        数据来源与采集完全一致：首屏 `/api/post/item_list/` 响应里的
        `author.nickname` —— **无需额外接口请求**，代价就是打开一次主页。
        供「刷新昵称」功能给已有账号补昵称使用。

        ⚠️ 2026-10-12 修复（用户反馈「为什么昵称刷新了还是不出来？」）：
        原来是「导航完固定等 4 秒再去看响应」—— 冷启动/首屏晚到时 4 秒
        不够，于是**明明能取到昵称却返回空串**，而失败在前端完全看不见。
        现在改成有界轮询：一解析出昵称立刻返回（通常比原来更快），最多
        等 `nickname_wait_ms`；接口始终不回时再用页面 DOM 兜底（TikTok
        主页 `<h1 data-e2e="user-title">` 就是昵称）。

        返回昵称；取不到（未登录/无作品/被风控）时返回空串。
        """
        context = self.browser.new_context(self.session.user_data_dir_for(account))
        bodies: List[str] = []
        page = None
        try:
            def on_response(resp):
                try:
                    if ("/api/post/item_list/" in resp.url
                            and "json" in resp.headers.get("content-type", "")):
                        body = resp.text()
                        if body:
                            bodies.append(body)
                except Exception:  # noqa: BLE001
                    pass

            # ⚠️ 监听必须在导航之前注册（首屏响应可能在 <1s 内返回）
            page = self.provider.load_profile(context, account.username,
                                              on_response=on_response)
            self.session.ensure_login(page)

            # 有界轮询：拿到就返回，不再固定等满 4 秒
            deadline = time.monotonic() + max(0, self.nickname_wait_ms) / 1000.0
            while True:
                nick = self._nickname_from_bodies(bodies)
                if nick:
                    return nick
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(250)

            # 兜底：item_list 一直没回（接口变动/被风控）时读页面标题
            return self._nickname_from_dom(page)
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    # TikTok 主页标题选择器：昵称渲染在这里；副标题是用户名，用来确认
    # 「这是一张正常主页」—— 页面出错/账号不存在时不能把提示文案当昵称。
    _DOM_TITLE_SELECTOR = 'h1[data-e2e="user-title"]'
    _DOM_USER_SELECTOR = 'h2[data-e2e="user-subtitle"]'

    def _nickname_from_bodies(self, bodies: List[str]) -> str:
        """从已收到的 item_list 响应里取 author.nickname（取不到返回空串）。"""
        for body in bodies:
            for item in self.parser.parse_item_list(body):
                if item.nickname:
                    return item.nickname.strip()
        return ""

    def _nickname_from_dom(self, page) -> str:
        """接口没回时的兜底：读主页标题。

        只在「确实是一张正常主页」（有用户名副标题）时才采信，且标题要短
        得像昵称；否则返回空串 —— 绝不把错误提示写进昵称。
        """
        try:
            if page.locator(self._DOM_USER_SELECTOR).count() < 1:
                return ""
            text = (page.locator(self._DOM_TITLE_SELECTOR).first
                    .text_content() or "").strip()
        except Exception:  # noqa: BLE001 —— 页面已关/选择器不支持都算取不到
            return ""
        return text if 0 < len(text) <= 64 else ""

    def fetch(self, account: Account) -> List[ParsedVideoItem]:
        """满足旧 Fetcher 协议：一次性返回全部解析结果（测试/简单场景用）。"""
        out: List[ParsedVideoItem] = []
        self.collect(account, lambda it: (out.append(it), True)[1])
        return out

    def collect(self, account: Account, on_item: ItemCallback) -> Dict:
        """单账号流式采集。返回诊断 dict。"""
        diag = {
            "found": 0,
            "item_list_requests": 0,
            "item_list_responses": 0,   # 所有 item_list 响应（含空响应，v1.4.7）
            "item_list_empty": 0,       # 200 但 0 字节：TikTok 拒绝返回的特征（v1.4.7）
            "item_list_refused": 0,     # 有 body 但没有 itemList 键（TikTok 拒绝形状）
            "page_error": False,        # 页面渲染出「Something went wrong」（v1.4.7）
            "scrolls": 0,
            "stopped_by_callback": False,
            "rate_limited": False,
            "author_nickname": "",   # 账号昵称：从 item_list 的 author.nickname 顺手取回
            "pinned_skipped": 0,     # 置顶作品条数（供诊断：置顶不占「最新 N 条」名额）
            "create_times": [],  # 记录 createTime 顺序，供排序可靠性诊断（已排除置顶）
        }

        context = self.browser.new_context(self.session.user_data_dir_for(account))
        try:
            # 监听 item_list 网络响应。
            # ⚠️ 必须在导航**之前**注册（通过 load_profile 的 on_response 参数）：
            #    首屏那次响应 = 最新一批作品，晚注册会漏掉它 → 只能拿到更旧的作品。
            queue: List[str] = []

            def on_response(resp):
                try:
                    url = resp.url
                    if "/api/post/item_list/" in url and "json" in resp.headers.get("content-type", ""):
                        if resp.status == 429:
                            diag["rate_limited"] = True
                            return
                        diag["item_list_responses"] += 1
                        body = resp.text()
                        if not body:
                            # 200 + 空响应：TikTok 匿名访问被拒的典型特征（v1.4.7）
                            diag["item_list_empty"] += 1
                            return
                        queue.append(body)
                        diag["item_list_requests"] += 1
                        if not any(f'"{k}"' in body
                                   for k in getattr(self.parser, "item_list_keys",
                                                    ("itemList",))):
                            # 有 body 却没有作品列表键 —— 同样是拒绝形状（v1.4.7）。
                            # 键名取自 parser，接口改名时这里自动跟随。
                            diag["item_list_refused"] += 1
                except Exception:
                    pass

            page = self.provider.load_profile(context, account.username,
                                              on_response=on_response)
            self.session.ensure_login(page)  # 登录/验证失效抛 LoginRequired

            # 等待首批响应落地
            page.wait_for_timeout(self.initial_wait_ms)

            emitted_ids = set()
            idle = 0
            for _ in range(self.max_scrolls):
                new_items = 0
                while queue:
                    body = queue.pop(0)
                    for item in self.parser.parse_item_list(body):
                        if not item.video_id or item.video_id in emitted_ids:
                            continue
                        emitted_ids.add(item.video_id)
                        diag["found"] += 1
                        new_items += 1
                        # 昵称：零额外请求，从首个带昵称的作品上取
                        if not diag["author_nickname"] and item.nickname:
                            diag["author_nickname"] = item.nickname.strip()
                        # 排序诊断：**排除置顶作品**再记录 createTime ——
                        # 置顶作品被排在列表最前、时间却是旧的，会把「顺序是否
                        # 时间倒序」的判定拉成异常，造成误告警。
                        if item.raw_publish_time is not None and not item.is_pinned:
                            diag["create_times"].append(item.raw_publish_time)
                        # 交给上层；False → 停止本账号
                        if not on_item(item):
                            diag["stopped_by_callback"] = True
                            return diag

                # 滚动触发更多 item_list
                try:
                    page.mouse.wheel(0, 2500)
                except Exception:
                    pass
                page.wait_for_timeout(self.scroll_wait_ms)
                diag["scrolls"] += 1

                if new_items == 0:
                    idle += 1
                    if idle >= self.idle_scrolls_before_stop:
                        break  # 连续多次无新作品 → 没有更多
                else:
                    idle = 0

            # 限流检测：一个作品都没采到且命中 429 → 抛退避异常，交由引擎指数退避。
            # 必须排在站点拒绝检测之前：429 有专门的退避策略，且它同样
            # 「收不到任何响应」，否则会被误判成站点拒绝。
            if diag.get("rate_limited") and diag["found"] == 0:
                raise RateLimited("触发 TikTok 限流(HTTP 429)，需退避等待后重试")

            # 站点拒绝检测（v1.4.7，2026-10-12 用户报「采 15 个账号只回 12 条」）：
            # 匿名访问时 TikTok 会对主页作品列表返回 200 + 空响应，页面渲染
            # 「Something went wrong」—— 此前被判成 empty（今日没有作品），
            # 替换式记录随即把旧记录清空，表现为「采集完链接反而变少了」。
            # 这里显式抛出，交由引擎退避重试；重试用尽才判失败并回滚旧记录。
            if diag["found"] == 0:
                diag["page_error"] = self._page_error(page)
                if self._looks_blocked(diag):
                    raise SiteBlocked(
                        "TikTok 未返回作品列表（空响应/页面报错，疑似风控或需要登录）")

            return diag
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

    # ---- 站点拒绝判定（v1.4.7） ----
    # 正常主页不会出现这两句；出现即说明 TikTok 自己都认为这次请求失败了。
    _PAGE_ERROR_MARKERS = ("something went wrong", "sorry about that")

    def _page_error(self, page) -> bool:
        """页面是否渲染出 TikTok 的错误提示。"""
        try:
            text = page.evaluate(
                "() => (document.body ? document.body.innerText : '')") or ""
        except Exception:  # noqa: BLE001 —— 页面已关/环境不支持都算「看不出来」
            return False
        low = str(text).lower()
        return any(m in low for m in self._PAGE_ERROR_MARKERS)

    @staticmethod
    def _looks_blocked(diag: Dict) -> bool:
        """判断「一个作品都没采到」是不是被站点拒绝了（而不是账号真的没作品）。

        命中任一即视为拒绝（此时应退避重试，而不是当作「今日没有作品」）：
        - 页面渲染出 TikTok 的错误提示；
        - 压根没收到 item_list 响应（页面加载异常）；
        - 收到的响应要么是 0 字节、要么没有作品列表键
          （正常响应一定带 itemList，哪怕是空数组）。
        只有「收到了正常响应但一个作品都没有」才返回 False —— 那才是真的
        没有作品，交给替换式记录规则处理。
        """
        if diag.get("page_error"):
            return True
        if diag.get("rate_limited"):
            return False   # 限流走独立分支（RateLimited），不按站点拒绝处理
        responses = int(diag.get("item_list_responses", 0) or 0)
        if responses <= 0:
            return True
        empty = int(diag.get("item_list_empty", 0) or 0)
        refused = int(diag.get("item_list_refused", 0) or 0)
        return (empty + refused) >= responses
