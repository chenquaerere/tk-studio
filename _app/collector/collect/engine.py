"""CollectEngine —— 正式采集编排层。

职责：
- 严格单账号串行：一个账号完整采集并保存后才进入下一个。
- 每确认一个作品立即写库（insert_video + collect_logs），程序崩溃/停止不丢已采数据。
- 账号间可配置等待间隔（account_interval_seconds）。
- 进度回调 on_progress。
- 停止/恢复：已采数据持久化，未开始账号 PENDING，当前账号 PARTIAL/FAILED。
- 异常隔离：单账号失败（含 LoginRequired）不阻断整体任务。

采集规则（2026-09-25 起，用户规定）：
- **不过滤日期**：直接抓取账号当前最新的 N 条作品（N = 每账号数量）。
  不管是今天、昨天还是更早，按主页时间倒序取前 N 条。
- 发布时间尽力解析并记录（createTime），无法确认的照常采集，publish_date 记空。
- **替换式记录**：新任务开始时删除本次所有账号的历史作品记录，
  多次采集（含同日多次测试）一律按最后一次的结果替换。
- **失败回滚（v1.4.3 / v1.4.7）**：删除前留快照，某账号失败**或被中止**且一条未采到时原样写回；
  首个账号确认登录态失效则提前结束，后面的账号不清旧记录。
- 每日链接日志同日文件同样按最后一次运行整体替换。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from collector.core.collect_logic import recent_dates, resolve_item
from collector.core.date_resolver import DateResolver
from collector.core.dedup import VideoDedup
from collector.core.models import (
    Account,
    AccountCollectResult,
    DateConfidence,
    Video,
)
from collector.core.url_normalizer import normalize_url
from collector.collect.exceptions import LoginRequired, RateLimited, SiteBlocked
from collector.log.logger import get_logger

# on_progress(current_index, total, account_username, status, detail)
ProgressCallback = Callable[[int, int, str, str, dict], None]


class CollectEngine:
    def __init__(
        self,
        db,
        collector,
        resolver: DateResolver,
        max_retry: int = 1,
        account_interval_seconds: float = 4.0,
        consecutive_older_stop: int = 3,
        daily_links=None,
        logger=None,
    ):
        self.db = db
        self.collector = collector          # ProfileCollector
        self.resolver = resolver
        self.max_retry = max(0, max_retry)
        self.account_interval_seconds = max(0.0, account_interval_seconds)
        self.consecutive_older_stop = max(1, consecutive_older_stop)
        self.daily_links = daily_links      # 可选 DailyLinksLog，作品入库后追加链接
        self.logger = logger or get_logger("collector.engine")
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()               # set = 运行；clear = 暂停（与 Job.pause 同语义）
        self._run_urls: List[str] = []  # 本次运行采集的链接（链接日志累计写入用）
        self._rate_backoff = 0          # 限流退避等级（0 无退避，随 429 递增）
        self._blocked_backoff_level = 0  # 站点空响应退避等级（v1.4.7）
        self._abort_reason = ""         # 非空则提前结束（登录态失效熔断，v1.4.3）

    # ---- 停止 / 暂停控制 ----
    def request_stop(self) -> None:
        self._stop.set()

    def request_pause(self) -> None:
        """请求暂停：在**账号边界**生效 —— 当前账号会采完，已采数据保留。"""
        self._pause.clear()

    def request_resume(self) -> None:
        self._pause.set()

    def reset(self) -> None:
        self._stop.clear()
        self._pause.set()

    def _stopped(self) -> bool:
        return self._stop.is_set()

    def _wait_if_paused(self) -> None:
        """暂停点：阻塞到恢复运行（或被停止信号唤醒）。

        只在**账号边界**等待，不打断进行中的账号 ——
        半途挂起会让「清除旧记录 + 采回新记录」这组动作失去原子性，
        页面上也会出现一个状态不明的账号。
        停止优先级高于暂停：停在这里同样能立刻响应「停止」。
        """
        while not self._pause.wait(0.3):
            if self._stopped():
                return

    # ---- 主入口 ----
    def run(
        self,
        accounts: List[Account],
        target_date: str,
        task_id: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> dict:
        task_id = task_id or self.db.next_task_id(target_date)
        self.db.insert_task(task_id, target_date=target_date, status="running")

        # 确保账号已入库（幂等），保证 task_accounts / videos 外键有效
        for acc in accounts:
            self.db.insert_account(acc)

        # 替换式记录：删除本次账号的历史作品（多次采集按最后一次替换）
        # 删除动作已下移到主循环里「每个账号开始采集时」执行（见 _clear_account_videos），
        # 这里只初始化汇总计数器。
        #
        # ⚠️ 这是**不可逆**的一步，且发生在打开浏览器之前 —— 也就是说
        #    「选错了账号范围」会立刻造成数据丢失，而不是等采完才发现。
        #    2026-10-08 实测事故：上游把空参数隐式兜底成"全部账号"，
        #    84 条作品在这一步被清空（已从备份恢复，见 docs/INCIDENT-20261008.md）。
        #    因此这里额外记录「本次将影响多少条既有记录」，
        #    条数偏大时提高日志级别，便于事后追溯与告警。
        #
        # ⚠️ 2026-10-10 起：清除动作**下移到每个账号开始采集时**（见下面主循环），
        #    让「清除旧记录 + 采回新记录」成为单账号的原子单元。
        #    旧写法在任务开头一次性清除全部所选账号：中途关窗/停止会让排在后面的
        #    账号「旧记录已删、新记录未采」（docs/INCIDENT-20261008.md 记录的
        #    「采集中断留空缺」）。所有账号都采完时，结果与旧写法完全一致。
        removed_total = 0
        removed_accounts = 0

        # ⚠️ 绝不做「跨运行去重」（历史 bug，2026-09-27 修复）
        #   旧实现：dedup = VideoDedup(initial=self.db.load_all_video_ids())
        #   把**全库已有 video_id** 装进去重器 → 重新抓取时，最新作品被当成「重复」
        #   跳过，然后继续往后捞更旧的：
        #     · 拿到的不是最新的（与「从最新开始拿」的要求相反）
        #     · 或凑不够数量 → 用户回填时发现「少了两条链接」
        #   实测后果：2026-09-24 有 40 条作品被标记「重复跳过」且**从未入库**。
        #   用户明确要求：不管之前采过没有，每次抓取都要从最新一条开始、依次取够数量。
        #   → 去重范围收窄为「单账号 + 单次采集内」（仅用于处理置顶作品在列表里
        #     重复渲染），在 _collect_once 内部新建，重试时也会重置。

        summary = {
            "task_id": task_id,
            "target_date": target_date,
            "account_count": len(accounts),
            "completed": 0, "partial": 0, "empty": 0,
            "failed": 0, "login_required": 0, "skipped": 0,
            "target_total": 0, "actual_total": 0,
            "new_videos": 0, "duplicates": 0, "unconfirmed": 0,
            "pinned_skipped": 0,     # 本次跳过的置顶作品总数（非近期，不占名额）
            "pinned_collected": 0,   # 本次额外采集的近期置顶总数（今天/昨天发布）
            "restored": 0,           # 未采到作品的账号回滚写回的旧记录条数（v1.4.3/1.4.7）
            "blocked": 0,            # 被 TikTok 拒绝返回作品列表的账号数（v1.4.7）
            "login_aborted": False,  # 是否因登录态失效提前结束（后面的账号不清旧记录）
            "links_written": 0,      # 本次链接日志**新增**条数（全天累计，去重后）
            "links_kept": 0,         # 本次没采到链接、保留下来的当日链接条数
            "accounts": [],
        }

        total = len(accounts)
        self.logger.info("任务 %s 开始：%d 个账号，最新N条模式（记录日期 %s）",
                         task_id, total, target_date)
        self._run_urls = []  # 本次运行采到的全部链接（供链接日志累计写入）
        try:
            for idx, acc in enumerate(accounts):
                # 登录态失效熔断（v1.4.3）：第一个账号确认登录失效后，后面的
                # 账号一律跳过 —— 不清旧记录、不开浏览器。否则每个账号都会
                # 「删得掉、采不回」，把整批账号的历史记录挨个清空。
                if self._abort_reason:
                    self.db.upsert_task_account(task_id, acc.account_id, acc.username,
                                                acc.collect_count, 0, "skipped",
                                                self._abort_reason)
                    summary["skipped"] += 1
                    self._emit(on_progress, idx, total, acc.username, "skipped",
                               {"aborted": True, "reason": self._abort_reason})
                    continue

                # 暂停闸门放在账号最前面：保证单个账号的
                # 「清除旧记录 + 采回新记录」不会被半途打断
                self._wait_if_paused()
                if self._stopped():
                    self.db.upsert_task_account(task_id, acc.account_id, acc.username,
                                                acc.collect_count, 0, "skipped", "任务已停止")
                    summary["skipped"] += 1
                    self._emit(on_progress, idx, total, acc.username, "skipped", {})
                    continue

                # 账号间等待（首个账号前不等）
                if idx > 0 and self.account_interval_seconds > 0:
                    self._interruptible_sleep(self.account_interval_seconds)
                    if self._stopped():
                        self.db.upsert_task_account(task_id, acc.account_id, acc.username,
                                                    acc.collect_count, 0, "skipped", "任务已停止")
                        summary["skipped"] += 1
                        continue

                # 替换式记录：只清除**本账号**的旧记录 —— 紧接其后的采集使
                # 「清除 + 采集」成为原子单元，中断最多影响正在采的这一个账号。
                # 删除前留一份快照：该账号整体失败且一条都没采到时原样写回，
                # 避免登录态失效之类的失败把历史记录变成空白（v1.4.3）。
                removed, snapshot = self._clear_account_videos(acc)
                if removed:
                    removed_total += removed
                    removed_accounts += 1

                result = self._collect_account(acc, task_id, target_date, on_progress, idx, total)
                # 回滚条件（v1.4.3 起）：失败且一条都没采到 → 原样写回。
                # 2026-10-12 用户报「采 15 个账号、每个 3 条，最终只有 12 条」：
                # 11 个账号被 TikTok 空响应拒绝却被判成 empty（今日没有作品），
                # 而 empty 不回滚 —— 于是一次「先清后采」就把旧记录清成了空白。
                # v1.4.7 让这种拒绝在采集器里抛 SiteBlocked（见 profile_collector），
                # 它统一走这条 failed 分支：重试用尽后回滚保留旧记录。
                #
                # 2026-10-12 22:01 又踩到同一条缝的另一半：用户采到一半**手动停止**，
                # 该账号旧记录已被「先清后采」删除，而停止路径给的状态是 skipped ——
                # 不在回滚条件里，于是 @user1986540439405 的 3 条记录被清空且没写回
                # （事后从自动备份恢复）。中止同样属于异常中断，必须一起回滚；
                # 只有 empty（真的一次作品都没有）保持「替换式清空」语义。
                if (snapshot and result.status in ("failed", "skipped")
                        and result.actual_count == 0):
                    back = self.db.restore_videos(snapshot)
                    summary["restored"] += back
                    note = "（本次未采到任何作品，已回滚保留原记录）"
                    result.error_reason = (result.error_reason or "") + note
                    self.logger.warning(
                        "账号 @%s 本次一条都没采到（status=%s），已回滚保留 %d 条旧记录",
                        acc.username, result.status, back)
                self.logger.info("账号 @%s 完成：status=%s target=%d actual=%d err=%r",
                                 acc.username, result.status, result.target_count,
                                 result.actual_count, result.error_reason)
                summary["accounts"].append(result)
                summary["target_total"] += result.target_count
                summary["actual_total"] += result.actual_count
                summary["new_videos"] += result.diagnostics.get("collected", 0)
                summary["duplicates"] += result.diagnostics.get("duplicates", 0)
                summary["unconfirmed"] += result.diagnostics.get("date_unconfirmed", 0)
                summary["pinned_skipped"] += result.diagnostics.get("pinned_skipped", 0)
                summary["pinned_collected"] += result.diagnostics.get("pinned_collected", 0)

                if result.status == "completed":
                    summary["completed"] += 1
                elif result.status == "partial":
                    summary["partial"] += 1
                elif result.status == "empty":
                    summary["empty"] += 1
                elif result.status == "skipped":
                    # v1.4.7 给「被中止的账号」定了 skipped 语义（旧记录已回滚保留），
                    # 但这里的汇总分支当时写成了 pass —— 于是摘要里永远看不到
                    # 有多少账号是被中止的，287 行那段 login_aborted 提示也跟着失效。
                    summary["skipped"] += 1
                elif result.status == "failed":
                    summary["failed"] += 1
                    if result.diagnostics.get("site_blocked"):
                        summary["blocked"] += 1
                    if result.diagnostics.get("login_required"):
                        summary["login_required"] += 1
                        if not self._abort_reason:
                            self._abort_reason = "登录态失效，任务已停止（未清除旧记录）"
                            summary["login_aborted"] = True
                            self.logger.warning(
                                "登录态失效：本次任务提前结束，后面的账号不会清除旧记录")

                self.db.upsert_task_account(
                    task_id, acc.account_id, acc.username,
                    result.target_count, result.actual_count,
                    result.status, result.error_reason,
                )
        finally:
            # 替换式记录汇总（含停止/异常场景）：量级偏大时告警，便于核对账号范围
            if removed_total:
                self.logger.info("本次替换式记录共清除 %d 条既有作品（%d 个账号）",
                                 removed_total, removed_accounts)
            if removed_total > 50:
                self.logger.warning(
                    "⚠️ 替换式记录清除了 %d 条既有作品，量级偏大 —— "
                    "请核对本次采集的账号范围是否正确", removed_total)
            if summary.get("blocked"):
                self.logger.warning(
                    "%d 个账号被 TikTok 拒绝返回作品列表（空响应/页面报错），已自动重试仍失败；"
                    "这些账号的旧记录已回滚保留，建议登录后再采或稍后重试",
                    summary["blocked"])
            if summary.get("restored"):
                self.logger.info("已回滚保留 %d 条旧记录（本次未采到作品的账号）",
                                 summary["restored"])
            if summary.get("login_aborted") and summary.get("skipped"):
                self.logger.warning("登录态失效导致任务提前结束：%d 个账号未采集（旧记录未清除）",
                                    summary["skipped"])
            # 链接日志：全天累计（去重追加），不再「按最后一次运行整体替换」
            #
            # 2026-10-12 用户反馈「链接日志里账号不全」：替换式写法下，一次只勾了
            # 部分账号的采集会把之前跑过的账号从当日日志里整个抹掉（实测：15 个
            # 越南账号跑完，当日文件里早先跑过的美区/缅甸账号全没了）。v1.4.7 改为
            # 累计写入：当天跑过的每个账号都留在日志里，只增不减。
            # 同时保留 v1.4.3 的保护：本次没采到链接时一个字都不写 —— 旧实现是
            # `replace([])`，用 "w" 打开会把当日文件清成 0 字节。
            if self.daily_links is not None:
                try:
                    if self._run_urls:
                        summary["links_written"] = self.daily_links.append_many(
                            self._run_urls, day=target_date)
                    else:
                        kept = self.daily_links.load(day=target_date)
                        summary["links_kept"] = len(kept)
                        if kept:
                            self.logger.info(
                                "本次没有采到任何链接，保留当日链接日志原样（%d 条未清空）",
                                len(kept))
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("每日链接日志替换失败: %s", exc)

        # 更新任务汇总
        status = "stopped" if self._stopped() else "completed"
        self.db.update_task(
            task_id, status=status, account_count=total,
            target_total=summary["target_total"], actual_total=summary["actual_total"],
            success_count=summary["completed"] + summary["partial"],
            fail_count=summary["failed"], completed_at=_now(),
        )
        summary["status"] = status
        return summary

    # ---- 替换式记录：单账号清除 ----
    def _clear_account_videos(self, acc: Account):
        """清除该账号的既有作品（替换式记录），返回 (清除条数, 回滚快照)。

        ⚠️ 调用点必须紧邻该账号的采集动作（见 run() 主循环）：
        「清除 + 采集」是同一个原子单元，中断最多影响正在采的那一个账号，
        而不是让排在后面的账号「旧记录已删、新记录未采」。

        快照在删除**之前**取，供采集整体失败且一条未采到时原样写回（v1.4.3）；
        删除失败时返回空快照（没删就没有可回滚的东西）。
        """
        try:
            snapshot = self.db.snapshot_videos_for_account(acc.account_id)
        except Exception as exc:  # noqa: BLE001 —— 快照失败不阻断采集
            self.logger.warning("账号 @%s 旧记录快照失败（本次不可回滚）: %s",
                                acc.username, exc)
            snapshot = []
        try:
            removed = self.db.delete_videos_for_account(acc.account_id)
        except Exception as exc:  # noqa: BLE001 —— 清理失败不阻断采集
            self.logger.warning("账号 @%s 旧记录清除失败: %s", acc.username, exc)
            return 0, []
        if removed:
            self.logger.info("账号 @%s 旧记录已清除：%d 条（替换式记录，已留 %d 条回滚快照）",
                             acc.username, removed, len(snapshot))
        return removed, snapshot

    # ---- 单账号采集 ----
    def _collect_account(self, acc, task_id, target_date, on_progress, idx, total):
        """采集单个账号（含重试）。

        ⚠️ 每次尝试都在 _collect_once 内新建去重集合 —— 重试时不复用，
        否则「第 1 次尝试已采到的作品」会在第 2 次尝试里被当成重复而全部跳过。
        """
        last_error = ""
        login_required = False
        site_blocked = False
        attempts = 0
        for attempt in range(1, self.max_retry + 1):
            if self._stopped():
                return self._result(acc, target_date, 0, "skipped", "任务已停止", {}, [])
            try:
                result = self._collect_once(acc, task_id, target_date,
                                            on_progress, idx, total, attempt)
                self._rate_backoff = 0  # 采集成功，退避等级清零
                return result
            except LoginRequired as exc:
                login_required = True
                last_error = str(exc) or "登录态失效"
                self.logger.warning("账号 @%s 登录态失效: %s", acc.username, last_error)
                break  # 登录/验证问题重试无意义，直接判失败
            except RateLimited as exc:
                last_error = str(exc) or "触发限流"
                attempts = attempt
                self.logger.warning("账号 @%s 第 %d/%d 次触发限流: %s",
                                    acc.username, attempt, self.max_retry, last_error)
                if attempt < self.max_retry:
                    self._backoff_sleep()
            except SiteBlocked as exc:
                # TikTok 空响应/页面报错（v1.4.7）：换一次新导航往往就过了，
                # 因此按退避重试；重试用尽才判失败（旧记录会回滚，不会丢）。
                site_blocked = True
                last_error = str(exc) or "TikTok 未返回作品列表"
                attempts = attempt
                self.logger.warning(
                    "账号 @%s 第 %d/%d 次未拿到作品列表（TikTok 空响应/页面报错）: %s",
                    acc.username, attempt, self.max_retry, last_error)
                if attempt < self.max_retry:
                    self._blocked_backoff()
            except Exception as exc:  # noqa: BLE001 —— 单账号失败不得阻断整体
                last_error = str(exc) or type(exc).__name__
                attempts = attempt
                self.logger.warning("账号 @%s 第 %d/%d 次尝试失败: %s",
                                    acc.username, attempt, self.max_retry, last_error)
                if attempt < self.max_retry:
                    time.sleep(1.0)
        diag = {"login_required": login_required, "site_blocked": site_blocked,
                "attempts": attempts or self.max_retry}
        return self._result(acc, target_date, 0, "failed", last_error, diag, [])

    def _collect_once(self, acc, task_id, target_date, on_progress, idx, total, attempt):
        diag = {
            "found": 0, "date_resolved": 0, "date_unconfirmed": 0,
            "duplicates": 0, "collected": 0,
            "pinned_skipped": 0,     # 置顶但非近期 → 跳过条数
            "pinned_collected": 0,   # 置顶且为「今天/昨天」→ 额外采集条数（不占名额）
            "attempts": attempt,
            "author_nickname": "",   # 账号昵称：从首个带昵称的作品上取（零额外请求）
        }
        # 「最新 N 条」的名额**只由非置顶作品占用**；近期置顶属于额外采集。
        non_pinned_count = 0
        # 近期日期集合（今天 / 昨天，按配置时区）—— 用于判断置顶是否值得额外采集
        recent = recent_dates(self.resolver, 2)
        collected: List[Video] = []
        # 去重范围 = **本账号 + 本次采集**。
        # 每次调用都新建集合：
        #   · 不加载库中已有 ID（否则重新抓取时最新作品会被跳过 —— 见 run() 说明）
        #   · 不跨重试复用（否则第 2 次尝试会把第 1 次采到的作品全部跳过）
        # 唯一作用是处理「同一作品在主页列表里出现两次」（如置顶作品），
        # 避免同一个视频占用两个名额、导致最终拿到的不同作品数不足。
        dedup = VideoDedup()

        def on_item(item):
            nonlocal non_pinned_count
            diag["found"] += 1

            # 0) 账号昵称：从首个带昵称的作品上取（放在最前，即使该作品随后被
            #    去重跳过/已达数量而停止，也仍能拿到昵称用于账号页辨识）。
            if not diag["author_nickname"]:
                nick = (getattr(item, "nickname", "") or "").strip()
                if nick:
                    diag["author_nickname"] = nick

            # 1) 置顶作品：**不占「最新 N 条」名额**（2026-09-28 用户实测修复）。
            #    置顶被排在列表最前（不按发布时间），若照单计入会挤掉真正新发布的作品：
            #    用户有 2 个账号各置顶 1 条，结果各少拿 1 条新作品。
            #    依据 item_list 的 `isPinnedItem=true`（探针实测：置顶 true、普通 null）。
            #    · 置顶发布于**今天 / 昨天** → 它本身就是新作品 → **额外采集**（结果多于 N 条）
            #    · 其它（较旧的置顶）→ 跳过、不采集
            extra_pinned = False
            if getattr(item, "is_pinned", False):
                resolve_item(item, self.resolver)   # 先解析时间才能判断日期
                if (item.publish_date or "") not in recent:
                    diag["pinned_skipped"] += 1
                    self.db.insert_collect_log(
                        task_id, acc.account_id,
                        f"置顶作品跳过（非近期，不占名额） video_id={item.video_id}",
                        level="info", video_id=item.video_id,
                    )
                    return True
                extra_pinned = True
                diag["pinned_collected"] += 1
                self.db.insert_collect_log(
                    task_id, acc.account_id,
                    f"置顶作品额外采集（{item.publish_date}） video_id={item.video_id}",
                    level="info", video_id=item.video_id,
                )

            # 2) 同一账号内同一作品重复出现 → 跳过且不计数，
            #    保证最终拿到的是 N 条**不同**的作品。
            #    注意顺序：放在数量检查之前，这样重复条目能被准确统计（diagnostics）。
            if dedup.is_duplicate(item.video_id):
                diag["duplicates"] += 1
                self.db.insert_collect_log(
                    task_id, acc.account_id,
                    f"同一作品重复出现，跳过 video_id={item.video_id}",
                    level="info", video_id=item.video_id,
                )
                return True

            # 3) 非置顶作品已取满 N 条 → 停止（额外采集的近期置顶不占名额）
            if not extra_pinned and non_pinned_count >= acc.collect_count:
                return False

            # 4) 解析发布时间（尽力记录；无法确认照常采集，日期记空）
            resolve_item(item, self.resolver)
            publish_time = ""
            publish_date = ""
            if item.confidence == DateConfidence.CONFIRMED.value and item.publish_datetime:
                diag["date_resolved"] += 1
                publish_time = item.publish_datetime.strftime("%Y-%m-%d %H:%M:%S")
                publish_date = item.publish_date or ""
            else:
                diag["date_unconfirmed"] += 1

            # 5) 标准化 URL 并入库
            normalized, vid, _ = normalize_url(item.raw_url, username_hint=item.username or acc.username)
            video_id = vid or item.video_id
            video = Video(
                video_id=video_id,
                account_id=acc.account_id,
                username=item.username or acc.username,
                video_url=normalized or item.raw_url,
                publish_time=publish_time,
                publish_date=publish_date,
                first_task_id=task_id,
            )
            # 立即持久化：video 唯一插入 + 采集日志
            inserted = self.db.insert_video(video)
            self.db.insert_collect_log(
                task_id, acc.account_id,
                "采集入库" if inserted else "已存在(重复)",
                level="info", video_id=video_id,
            )
            dedup.mark(video_id)
            collected.append(video)
            diag["collected"] += 1
            if not extra_pinned:
                non_pinned_count += 1
            self._run_urls.append(video.video_url)

            self._emit(on_progress, idx, total, acc.username, "collecting",
                       {"video_id": video_id, "collected": len(collected)})
            # 非置顶已达数量 → 停止本账号（额外采集的近期置顶不影响停止条件）
            return non_pinned_count < acc.collect_count

        profile_diag = self.collector.collect(acc, on_item)
        diag.update({k: profile_diag[k] for k in ("scrolls", "item_list_requests")
                     if k in profile_diag})
        diag["create_time_order"] = "descending" if self._is_descending(
            profile_diag.get("create_times", [])) else "unknown"

        # 昵称（TikTok 显示名）：采集时顺手写回账号，零额外请求。
        # 优先用 engine 自己从作品上收集的；collector 的 diag 作为兜底。
        # 用途：账号管理页显示昵称，便于辨识账号与分类。
        nickname = ((diag.get("author_nickname") or "")
                    or (profile_diag.get("author_nickname") or "")).strip()
        if nickname:
            try:
                if self.db.set_account_display_name(acc.account_id, nickname):
                    self.logger.info("账号 @%s 昵称已更新：%s", acc.username, nickname)
            except Exception as exc:  # noqa: BLE001 —— 昵称失败不影响采集
                self.logger.warning("账号 @%s 昵称写入失败: %s", acc.username, exc)

        # 可观测性：这两个信号用来区分「账号真的没作品」与「页面/监听异常」。
        # 2026-09-27 曾因「item_list 响应监听晚于页面导航」导致首屏最新一批被静默丢弃，
        # 表面现象是「拿到更旧的作品 / 数量对不上」，但日志里毫无痕迹 —— 故补上这两条告警。
        if profile_diag.get("item_list_requests", 0) == 0:
            self.logger.warning(
                "账号 @%s 未捕获到任何 item_list 响应（页面加载异常或接口变更？）",
                acc.username)
        if len(collected) > 1 and diag["create_time_order"] != "descending":
            self.logger.warning(
                "账号 @%s 作品顺序非时间倒序（create_time_order=%s）—— "
                "可能漏掉了首屏最新的一批，请检查采集结果",
                acc.username, diag["create_time_order"])

        # 状态按「非置顶作品数」判定：额外采集的近期置顶不算入目标名额，
        # 否则「只采到置顶、非置顶没采到」会被误判成完成。
        status = self._status_for(acc.collect_count, non_pinned_count)
        return self._result(acc, target_date, len(collected), status, "", diag, collected)

    # ---- helpers ----
    def _result(self, acc, target_date, actual, status, error, diag, videos):
        return AccountCollectResult(
            account_id=acc.account_id,
            username=acc.username,
            target_date=target_date,
            target_count=acc.collect_count,
            actual_count=actual,
            status=status,
            videos=videos,
            error_reason=error,
            diagnostics=diag,
        )

    @staticmethod
    def _status_for(target_count, actual):
        if actual >= target_count:
            return "completed"
        if actual > 0:
            return "partial"
        return "empty"

    @staticmethod
    def _is_descending(times: List) -> bool:
        """判断 createTime 序列是否整体递减（最新→最旧）。"""
        nums = [float(t) for t in times if t is not None]
        if len(nums) < 3:
            return True  # 样本过少，视为未知
        inversions = sum(1 for i in range(len(nums) - 1) if nums[i] < nums[i + 1])
        return inversions <= max(1, len(nums) // 10)

    def _interruptible_sleep(self, seconds: float) -> None:
        """可被停止信号打断的等待。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stopped():
                return
            time.sleep(min(0.2, seconds))

    def _backoff_sleep(self) -> None:
        """限流指数退避：10s → 20s → 40s → 60s 封顶，可被停止信号打断。"""
        delay = min(60.0, 10.0 * (2 ** self._rate_backoff))
        self._rate_backoff += 1
        self.logger.info("限流退避等待 %.0f 秒后重试", delay)
        self._interruptible_sleep(delay)

    def _blocked_backoff(self) -> None:
        """站点空响应退避：3s → 6s → 12s（15s 封顶），可被停止信号打断。

        比 429 的退避短：实测「换一次新导航」经常直接拿到数据，等太久反而拖长任务。
        """
        delay = min(15.0, 3.0 * (2 ** self._blocked_backoff_level))
        self._blocked_backoff_level += 1
        self.logger.info("等待 %.0f 秒后重试该账号（TikTok 未返回作品列表）", delay)
        self._interruptible_sleep(delay)

    @staticmethod
    def _emit(cb, idx, total, username, status, detail):
        if cb is not None:
            try:
                cb(idx, total, username, status, detail)
            except Exception:
                pass


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
