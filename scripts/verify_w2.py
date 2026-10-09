"""W2 验收：采集内核移植的四道门禁。

门禁设计原则 —— 每条都对应一个"曾经真的出过事"的点，
不做泛泛的"能 import"检查。

  G1  UI 无关性：collector 包内不得出现 tkinter / customtkinter
                  （否则 Web 化后会在无图形环境里炸掉，且拖慢启动）
  G2  五条铁律在位：对源码做结构断言
                  1. 数据库不自增 SCHEMA_VERSION（否则 DROP 全表清用户数据）
                  2. 不做跨运行去重（否则重新抓取会把最新作品当重复跳过）
                  3. response 监听早于 page.goto（否则首屏最新一批被静默丢弃）
                  4. 置顶作品用 isPinnedItem 识别 + 今天/昨天额外采集
                  5. engine 与 collect_logic 两套实现同步
  G3  真实库零丢失：在副本上比对 账号/作品/任务 数量 + user_version
                    （原库 D:\\workbuddy\\TK链接抓取助手\\data 只读，绝不动）
  G4  装配层可用：CollectorServices 能构建、服务层方法可调用、代理配置正确
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "_app"
sys.path.insert(0, str(APP_DIR))

COLLECTOR = APP_DIR / "collector"
ORIGINAL_DB = Path(r"D:\workbuddy\TK链接抓取助手\data\tiktok_link_collector.db")
COPY_DB = APP_DIR.parent / "data" / "tiktok_link_collector.db"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


# ───────────────────────────────────────────── G1 UI 无关性
def g1() -> None:
    print("\nG1  UI 无关性（collector 包内不得有 tkinter）")
    offenders: list[str] = []
    for f in COLLECTOR.rglob("*.py"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^\s*(import|from)\s+(tkinter|customtkinter)", text, re.M):
            offenders.append(str(f.relative_to(COLLECTOR)))
    check("collector 包内零 tkinter/customtkinter", not offenders,
          f"违例文件: {offenders}" if offenders else "核心层与 UI 完全解耦")

    # app/ui 目录也不应被搬进来
    check("未搬迁任何 app/ui 页面代码", not (COLLECTOR / "ui").exists())


# ───────────────────────────────────────────── G2 五条铁律
def g2() -> None:
    print("\nG2  五条铁律在位")

    db_src = (COLLECTOR / "db" / "database.py").read_text(encoding="utf-8")
    m = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", db_src, re.M)
    check("① 数据库 SCHEMA_VERSION 保持 2（未递增，不清库）",
          bool(m) and m.group(1) == "2",
          f"SCHEMA_VERSION = {m.group(1) if m else '未找到'}")
    check("① 新字段走 _EXTRA_COLUMNS + ALTER TABLE（幂等加列）",
          "_EXTRA_COLUMNS" in db_src and "ALTER TABLE" in db_src)

    eng = (COLLECTOR / "collect" / "engine.py").read_text(encoding="utf-8")
    # 只看可执行代码，注释里会提到历史 bug 的旧写法（刻意的说明文字）
    eng_code = "\n".join(ln for ln in eng.splitlines()
                         if not ln.lstrip().startswith("#"))
    check("② 去重器在单次采集内新建（不做跨运行去重）",
          "dedup = VideoDedup()" in eng_code and "load_all_video_ids" not in eng_code,
          "可执行代码中未出现 load_all_video_ids")
    check("② 去重范围注释明确标注「本账号 + 本次采集」",
          "本账号 + 本次采集" in eng or "去重范围" in eng)

    prov = (COLLECTOR / "collect" / "provider.py").read_text(encoding="utf-8")
    i_listen = prov.find('page.on("response"')
    i_goto = prov.find("page.goto(")
    check("③ response 监听早于 page.goto（首屏最新一批不丢）",
          0 <= i_listen < i_goto, f"on={i_listen} < goto={i_goto}")

    parser = (COLLECTOR / "collect" / "video_parser.py").read_text(encoding="utf-8")
    check("④ 置顶识别字段为 isPinnedItem", "isPinnedItem" in parser)
    check("④ 近期置顶额外采集（不占名额）",
          "recent_dates" in eng and "pinned_collected" in eng and "extra_pinned" in eng)
    check("④ 置顶不占「最新 N 条」名额，仅非置顶计数",
          "non_pinned_count" in eng)

    logic = (COLLECTOR / "core" / "collect_logic.py").read_text(encoding="utf-8")
    check("⑤ engine 与 collect_logic 两套实现同步（均处理置顶/去重顺序）",
          "is_pinned" in logic and "recent_dates" in logic and "pinned_skipped" in logic)


# ───────────────────────────────────────────── G3 真实库零丢失
def g3() -> None:
    print("\nG3  真实库零丢失（比对副本，原库只读）")

    def snap(p: Path) -> dict | None:
        if not p.exists():
            return None
        uri = f"file:{p.as_posix()}?mode=ro"
        c = sqlite3.connect(uri, uri=True)
        try:
            out = {"user_version": c.execute("PRAGMA user_version").fetchone()[0]}
            for t in ("accounts", "videos", "collect_tasks"):
                try:
                    out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    out[t] = -1
            out["names"] = sorted(r[0] for r in
                                  c.execute("SELECT username FROM accounts"))
            return out
        finally:
            c.close()

    a, b = snap(ORIGINAL_DB), snap(COPY_DB)
    check("原库与副本均存在", a is not None and b is not None)
    if a and b:
        check("user_version 一致且为 2",
              a["user_version"] == b["user_version"] == 2,
              f"原 {a['user_version']} / 副本 {b['user_version']}")
        # videos / collect_tasks 的**行数**只在 W0b 那一刻相等 ——
        # 副本是冻结快照，而真实库此后还会被正常采集写入（替换式记录、
        # 任务台账持续追加），所以行数只能作为参考信息打印，不能当断言。
        # 真正的不变量是：结构版本没被升过、账号名单一条不少。
        print(f"        （参考）作品 原 {a['videos']} / 副本 {b['videos']}；"
              f"任务 原 {a['collect_tasks']} / 副本 {b['collect_tasks']}")
        check("账号数一致（账号是唯一不该被迁移/采集改动的维度）",
              a["accounts"] == b["accounts"],
              f"原 {a['accounts']} / 副本 {b['accounts']}")
        check("账号名单逐一一致（含地区分类）", a["names"] == b["names"],
              f"{len(b['names'])} 个账号")
        check("账号数不为 0（未被清库）", b["accounts"] > 0)


# ───────────────────────────────────────────── G4 装配层可用
def g4() -> None:
    print("\nG4  装配层可用（CollectorServices）")
    from collector.bootstrap import CollectorServices

    ctx = CollectorServices()
    check("CollectorServices 构建成功", ctx is not None)
    check("数据库已连接", ctx.db is not None)

    accounts = ctx.account_service.list()
    check(f"账号服务可读（{len(accounts)} 个账号）", len(accounts) > 0)
    check("地区统计可用",
          len(ctx.account_service.region_stats()) > 0,
          str(ctx.account_service.region_stats()))

    rows = ctx.result_service.query_videos()
    check(f"结果服务可读（{len(rows)} 条作品）", len(rows) > 0)

    tz = ctx.timezone()
    check(f"时区解析（{tz}）", bool(tz))

    check("代理未启用时返回 None（不硬编码任何地址）",
          ctx.config.proxy_config() is None)

    # 聊天服务可构建（不实际启动浏览器）
    chat = ctx.build_chat_service()
    check("聊天采集服务可构建", chat is not None)

    # 采集引擎可构建（惰性；此处只验证装配，不启动 Playwright）
    check("采集引擎为惰性构建（未采集则未实例化）", ctx._engine is None)

    ctx.close()


if __name__ == "__main__":
    print("=" * 68)
    print("W2 验收：采集内核移植")
    print("=" * 68)
    g1()
    g2()
    g3()
    g4()
    print("\n" + "=" * 68)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
