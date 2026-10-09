"""采集子系统装配层（原 `app/app.py::AppServices` 的 UI 无关替代品）。

差异对照
--------
    原 AppServices                    本 CollectorServices
    ------------------------------    ------------------------------------
    base_dir = app_dir()              路径全部来自 common.paths（唯一来源）
    Config(<base>/config.json)        统一配置的 collector 分区
    resolve_path(base, key)           paths.resolve(配置值)
    if sys.frozen: exe 目录           已由 paths 处理（PyInstaller 场景兼容）
    run() 启动 tkinter 主窗口          无（界面由 server/ + frontend/ 承载）

职责：把 配置 → 日志 → 数据库 → 服务 → 采集引擎 组装起来。
采集引擎与浏览器**惰性构建**：不采集就不加载 Playwright（省启动开销）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from common import config as ucfg
from common import logging_setup, paths

from .config import Config
from .constants import DEFAULT_TIMEZONE
from .core.date_resolver import DateResolver
from .db.database import Database
from .log.daily_links import DailyLinksLog


class CollectorServices:
    """采集子系统的依赖容器（单例，供服务层/前端共享）。"""

    def __init__(self, config: Optional[Config] = None,
                 logger=None, auto_backup: bool = False):
        self.config = config or Config()

        # 便携式浏览器内核：若程序目录里带了 ms-playwright，就用它，
        # 不再依赖用户 %LOCALAPPDATA% 下的缓存 —— 这是「零依赖便携」的关键。
        # 必须在 playwright 被 import 之前设置（本函数是最早的采集入口）。
        _use_bundled_browsers()

        # 日志先于其它模块初始化
        logging_setup.setup(
            level=str(self.config.get("log_level", "INFO")),
            log_dir=str(paths.LOGS_DIR),
            keep_days=int(self.config.get("log_keep_days", 30)),
        )
        self.logger = logger or logging_setup.get_logger("collector")

        paths.ensure_dirs()

        # 数据库：路径来自配置，统一按程序根解析。
        # ⚠️ 绝不改动 SCHEMA_VERSION —— 见 db/database.py 顶部说明。
        db_path = paths.resolve(self.config.get("db_path"))
        self.db = Database(str(db_path))

        # 每日链接归档
        self.daily_links = DailyLinksLog(
            str(paths.resolve(self.config.get("links_log_dir"))))

        # 服务层
        from .services.account_service import AccountService
        from .services.result_service import ResultService
        self.account_service = AccountService(self.db)
        self.result_service = ResultService(self.db)

        # 惰性单例
        self._engine = None
        self._collector = None

        # 运行期标记（仅内存，不落盘）：
        # 结果页据此决定是否显示链接 —— 未完成本次采集则保持空态，
        # 不显示上次运行残留（避免"没采却已显示"的误解）。
        self.collected_this_session = False
        # 最近一次采集任务 ID：结果页默认只显示"本次任务"采到的链接。
        self.last_task_id = ""

        if auto_backup:
            try:
                self.auto_backup()
            except Exception:  # noqa: BLE001 —— 备份失败不阻断启动
                self.logger.warning("启动自动备份失败", exc_info=True)

    # ------------------------------------------------------------ 时区
    def timezone(self) -> str:
        tz = str(self.config.get("timezone") or "").strip()
        if not tz or tz.lower() == "auto":
            return DEFAULT_TIMEZONE
        return tz

    # ------------------------------------------------------------ 采集器
    def build_collector(self):
        """构建 ProfileCollector（浏览器 / 会话 / 站点 / 解析器）。"""
        if self._collector is not None:
            return self._collector
        from .collect.browser import BrowserManager
        from .collect.profile_collector import ProfileCollector
        from .collect.provider import TikTokProvider
        from .collect.session import SessionManager
        from .collect.video_parser import VideoParser

        browser = BrowserManager(
            headless=bool(self.config.get("browser_headless")),
            proxy=self.config.proxy_config(),
        )
        # ⚠️ 登录态目录必须是 <程序根>/data/sessions —— 与旧程序一致，
        #    这样迁移过来的 cookie 才能直接复用（无需重新登录 21 个账号）。
        session = SessionManager(data_dir=str(paths.DATA_DIR))
        provider = TikTokProvider(
            load_timeout_ms=int(self.config.get("page_load_timeout_ms", 30000)))
        self._collector = ProfileCollector(browser, session, provider, VideoParser())
        return self._collector

    def build_engine(self):
        """构建 CollectEngine（单例）。"""
        if self._engine is not None:
            return self._engine
        from .collect.engine import CollectEngine

        self._engine = CollectEngine(
            self.db,
            self.build_collector(),
            DateResolver(self.timezone()),
            max_retry=int(self.config.get("max_retry", 3)),
            account_interval_seconds=float(
                self.config.get("account_interval_seconds", 4.0)),
            daily_links=self.daily_links,
            logger=logging_setup.get_logger("collector.engine"),
        )
        return self._engine

    def reset_engine(self) -> None:
        """配置变更后重建引擎（代理 / 间隔 / 无头模式等）。"""
        self._engine = None
        self._collector = None

    # ------------------------------------------------------------ 聊天采集
    def build_chat_service(self):
        """构建聊天链接采集服务（轻量对象，直接新建）。"""
        from .services.chat_service import ChatService

        return ChatService(
            # ChatService 内部按 <base_dir>/data/sessions 存放登录态，
            # 传程序根即可与作品采集共用同一套 cookie。
            base_dir=str(paths.APP_ROOT),
            config=self.config,
            db=self.db,
            logger=logging_setup.get_logger("collector.chat"),
        )

    # ------------------------------------------------------------ 配置保存
    def save_config(self) -> None:
        self.config.save()

    def update_config(self, values: dict) -> None:
        """从 Web 设置页批量更新采集配置并落盘。"""
        self.config.update_many(values)
        self.config.save()
        self.reset_engine()

    # ------------------------------------------------------------ 更新检查
    def resolve_update_url(self) -> str:
        custom = str(self.config.get("update_url") or "").strip()
        if custom:
            return custom
        repo = str(self.config.get("update_repo") or "").strip()
        if repo:
            from .update import github_latest_url
            return github_latest_url(repo)
        return ""

    def check_for_update(self):
        from .update import check_update

        url = self.resolve_update_url()
        if not url:
            return None
        return check_update(url, ucfg.get("app", "last_seen_version") or _app_version())

    # ------------------------------------------------------------ 备份 / 恢复
    @property
    def backup_dir(self) -> Path:
        return paths.BACKUPS_DIR

    def backup_database(self, keep: int = 10) -> Optional[Path]:
        """备份数据库到 backups/（保留最近 keep 份）。"""
        from datetime import datetime

        d = self.backup_dir
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"tiktok_link_collector_{datetime.now():%Y%m%d_%H%M%S}.db"
        self.db.backup(str(target))
        self.logger.info("数据库已备份到 %s", target)
        try:
            backups = sorted(d.glob("*.db"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            for old in backups[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        return target

    def list_backups(self) -> list:
        d = self.backup_dir
        if not d.exists():
            return []
        return sorted(d.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    def restore_database(self, source_path: str) -> bool:
        try:
            self.db.restore(source_path)
            self.logger.info("数据库已从 %s 恢复", source_path)
            return True
        except Exception:  # noqa: BLE001
            self.logger.exception("数据库恢复失败")
            return False

    def auto_backup(self) -> Optional[Path]:
        """启动时每日备份一次（按 settings.last_backup_date 去重）。"""
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        if self.db.get_setting("last_backup_date") == today:
            return None
        target = self.backup_database()
        self.db.set_setting("last_backup_date", today)
        return target

    # ------------------------------------------------------------ 清理
    def close(self) -> None:
        """关闭 Playwright（若本次创建过）与数据库。

        ⚠️ 必须 stop 浏览器：Playwright driver 是独立子进程，
        不 stop 会导致驱动进程残留（历史踩坑）。
        """
        try:
            collector = self._collector
            browser = getattr(collector, "browser", None) if collector else None
            if browser is not None:
                browser.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass


def _use_bundled_browsers() -> None:
    """优先使用随程序分发的浏览器内核（`runtime/ms-playwright`）。

    Playwright 默认从 `%LOCALAPPDATA%\\ms-playwright` 找内核 —— 换台电脑就跑不起来。
    本程序把内核打进程序目录，实现"拷走整个文件夹就能用"。
    已经显式设置过环境变量时不覆盖（方便开发机指向系统缓存）。
    """
    import os

    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    bundled = paths.RUNTIME_DIR / "ms-playwright"
    try:
        if bundled.is_dir() and any(bundled.iterdir()):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    except OSError:
        pass


def _app_version() -> str:
    from common.version import APP_VERSION
    return APP_VERSION
