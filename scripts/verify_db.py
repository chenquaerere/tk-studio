"""W0b 数据库安全校验：在副本上打开用户真实数据库，确认零丢失。

用法（用测试 venv 的 3.13 解释器跑，应用代码目录需在 sys.path）：
    python scripts/verify_db.py <db_path> <应用代码目录>

其中 <应用代码目录> 合并后为程序根下的 `_app`（内含 collector / watermark / common）。
例：
    python scripts/verify_db.py data/tiktok_link_collector.db _app

判定：user_version 不变、账号数 / 作品数 / 账号名单 与原库完全一致。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


# 中文 Windows 的 cmd 默认代码页是 GBK：脚本最后一行打印 ✅/❌ 会抛
# UnicodeEncodeError —— 明明所有断言都通过了，退出码却是 1（看起来像验收失败）。
# 这里自己把输出切成 UTF-8，任何控制台都能把结果打印完。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

def raw_stats(path: str) -> dict:
    """绕过应用代码，直接用 sqlite3 读原始统计（作为基准）。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = con.cursor()
    out: dict = {}
    out["user_version"] = cur.execute("PRAGMA user_version").fetchone()[0]
    out["tables"] = sorted(
        r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    )
    for tbl, key in (("accounts", "accounts"), ("videos", "videos"),
                     ("collect_tasks", "tasks")):
        try:
            out[key] = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.Error as e:
            out[key] = f"ERR:{e}"
    try:
        out["account_list"] = sorted(
            r[0] for r in cur.execute("SELECT username FROM accounts")
        )
    except sqlite3.Error:
        out["account_list"] = []
    try:
        out["regions"] = cur.execute(
            "SELECT COALESCE(region,'') , COUNT(*) FROM accounts GROUP BY 1"
        ).fetchall()
    except sqlite3.Error:
        out["regions"] = []
    con.close()
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    db_path, src = sys.argv[1], sys.argv[2]
    sys.path.insert(0, src)

    print(f"[1/2] 原始 SQLite 直读基准: {db_path}")
    base = raw_stats(db_path)
    print(f"      user_version = {base['user_version']}")
    print(f"      tables       = {len(base['tables'])} 张")
    print(f"      accounts     = {base['accounts']}")
    print(f"      videos       = {base['videos']}")
    print(f"      tasks        = {base['tasks']}")
    print(f"      regions      = {base['regions']}")

    print("[2/2] 用采集器应用代码打开（走 _migrate/_ensure_columns）")
    from collector.db.database import SCHEMA_VERSION, Database  # type: ignore
    db = Database(db_path)
    opened = raw_stats(db_path)
    print(f"      SCHEMA_VERSION = {SCHEMA_VERSION}")
    print(f"      user_version   = {opened['user_version']}")
    print(f"      accounts       = {opened['accounts']}")
    print(f"      videos         = {opened['videos']}")
    print(f"      regions        = {opened['regions']}")

    checks = [
        ("user_version 未变", base["user_version"] == opened["user_version"]),
        ("账号数一致", base["accounts"] == opened["accounts"]),
        ("作品数一致", base["videos"] == opened["videos"]),
        ("账号名单一致", base["account_list"] == opened["account_list"]),
        ("表数量未减少", len(opened["tables"]) >= len(base["tables"])),
    ]
    print("\n=== 校验结果 ===")
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\n" + ("✅ 数据库校验通过：零丢失，可安全部署" if ok
                  else "❌ 数据库校验失败：禁止部署"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
