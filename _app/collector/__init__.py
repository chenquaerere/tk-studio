"""采集内核（原 TikTok 作品链接采集器 v1.2.1）。

与 UI 完全解耦：本包内**不含任何 tkinter / 界面代码**。
界面由 FastAPI 服务层（server/）+ 网页前端（frontend/）承载。

分层
----
    core/        数据模型、纯采集逻辑、去重、时区解析、地区字典
    db/          SQLite 数据访问层（accounts / videos / collect_tasks / chat_* …）
    collect/     Playwright 浏览器、持久化登录态、作品解析、采集引擎
    chat/        好友 / 群聊视频链接采集
    services/    账号 / 结果 / 聊天编排服务
    export/      CSV / TXT / XLSX 导出
    log/         日志（委托 common.logging_setup）+ 每日链接归档
    config.py    统一配置的 collector 分区适配层
    bootstrap.py 无 UI 的装配层（原 app/app.py 的 AppServices 替代品）

⚠️ 五条不可违反的铁律（详见 docs/MERGE_PLAN.md 与 tests）
  1. 数据库永不自增 SCHEMA_VERSION（否则 DROP 全表，清空用户账号与作品）
  2. 绝不做跨运行去重（只允许"单账号 + 单次采集内"去重）
  3. response 监听必须早于 page.goto（否则首屏最新一批被丢弃）
  4. 置顶作品识别用 isPinnedItem：跳过且不占名额；今天/昨天发布的额外采集
  5. engine 与 collect_logic 两套实现必须同步修改
"""
from common.version import APP_VERSION as __version__
