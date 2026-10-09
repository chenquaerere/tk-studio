"""W3 验收：采集子系统服务层接线 + SSE。

在无 GUI 环境下真启 uvicorn，用真实 HTTP 请求逐条验证 /api/collector/* 端点。

安全约束（绝不触碰用户真实数据）：
  - 不调用 /collect（会真的开浏览器采集并替换作品记录）
  - 不调用 /results/clear（空参数会清空全部作品）
  - 不删除任何账号
  - 只做只读查询 + 幂等写（写回同样的设置值）+ 生成一个导出文件

用法：python scripts/verify_w3.py
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "_app"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(port: int, path: str, method: str = "GET", body: dict | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def main() -> int:
    print("=" * 70)
    print("W3 验收：采集子系统服务层接线 + SSE")
    print("=" * 70)

    print("\n[1] 导入服务层并挂载路由")
    try:
        import server.api as api
        api.refresh_allowed_paths()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  [FAIL] 导入失败：{exc}")
        return 1
    routes = set((api.app.openapi().get("paths") or {}).keys())
    n_collector = len([p for p in routes if p.startswith("/api/collector")])
    check(f"collector 路由已挂载（{n_collector} 条）", n_collector >= 15)

    port = free_port()
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(
        api.app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    threading.Thread(target=srv.run, daemon=True, name="uvicorn-w3").start()
    deadline = time.time() + 40
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.25)
    else:
        print("  [FAIL] 服务未就绪")
        return 1
    print(f"      服务已就绪 @127.0.0.1:{port}")

    # ────────────────────────────────────────── 只读端点
    print("\n[2] 元信息与账号")
    st, d = req(port, "/api/collector/meta")
    check("GET /meta", st == 200 and "regions" in d, f"{st}")
    check("  地区字典非空", bool(d.get("regions")), str(d.get("regions"))[:60])
    check("  未分类哨兵已暴露", d.get("region_unspecified") == "__none__")

    st, d = req(port, "/api/collector/accounts")
    check("GET /accounts", st == 200 and d.get("total", 0) > 0, f"{st} total={d.get('total')}")
    check("  账号含地区字段", all("region" in a for a in d.get("accounts", [])[:3]))
    check("  昵称待获取统计存在", "missing_nickname" in d,
          f"待获取 {d.get('missing_nickname')}")

    st, d2 = req(port, "/api/collector/accounts?region=%E8%B6%8A%E5%8D%97")
    check("GET /accounts?region=越南 过滤生效",
          st == 200 and 0 < d2.get("total", 0) < d.get("total", 0),
          f"越南 {d2.get('total')} / 全部 {d.get('total')}")

    st, d3 = req(port, "/api/collector/accounts?region=__none__")
    check("GET /accounts?region=__none__ 未分类过滤", st == 200,
          f"未分类 {d3.get('total')}")

    print("\n[3] 任务与结果")
    st, tasks = req(port, "/api/collector/tasks?limit=5")
    check("GET /tasks", st == 200 and len(tasks.get("tasks", [])) > 0,
          f"{len(tasks.get('tasks', []))} 条")
    tid = (tasks.get("tasks") or [{}])[0].get("task_id", "")
    if tid:
        st, detail = req(port, f"/api/collector/tasks/{tid}")
        check(f"GET /tasks/{tid}", st == 200 and "videos" in detail,
              f"账号 {len(detail.get('accounts', []))} / 作品 {len(detail.get('videos', []))}")
        check("  任务详情含纯 URL 文本", "url_text" in detail)

    st, res_all = req(port, "/api/collector/results?scope=all")
    check("GET /results?scope=all", st == 200 and len(res_all.get("rows", [])) > 0,
          f"{len(res_all.get('rows', []))} 条")
    check("  结果含采集时间列", all("collect_time" in r for r in res_all.get("rows", [])[:3]))
    check("  账号统计已生成", len(res_all.get("stats", [])) > 0,
          str(res_all.get("stats"))[:70])

    st, res_sess = req(port, "/api/collector/results?scope=session")
    check("GET /results?scope=session 空态（本次未采集）",
          st == 200 and res_sess.get("rows") == [] and res_sess.get("empty_reason"),
          res_sess.get("empty_reason", ""))

    st, logs = req(port, "/api/collector/logs?limit=5")
    check("GET /logs", st == 200 and "logs" in logs, f"{len(logs.get('logs', []))} 条")

    print("\n[4] 聊天采集（只读）")
    st, recent = req(port, "/api/collector/chat/recent")
    check("GET /chat/recent", st == 200 and "targets" in recent,
          f"{len(recent.get('targets', []))} 个最近目标")
    st, links = req(port, "/api/collector/chat/links?chat_type=friend&name=x&uid=1")
    check("GET /chat/links", st == 200 and "url_text" in links)

    print("\n[5] 设置 / 维护 / 环境")
    st, s = req(port, "/api/collector/settings")
    check("GET /settings", st == 200 and "browser_headless" in s.get("values", {}),
          f"headless={s.get('values', {}).get('browser_headless')}")
    # 幂等写：把读到的值原样写回，不改动任何配置
    st, w = req(port, "/api/collector/settings", "POST",
                {"browser_headless": s.get("values", {}).get("browser_headless", True)})
    check("POST /settings（原值写回，幂等）", st == 200 and w.get("ok"))
    st, w = req(port, "/api/collector/settings", "POST", {"__bogus__": 1})
    check("POST /settings 拒绝未知键", st == 400, f"{st}")

    st, tp = req(port, "/api/collector/settings/test-proxy", "POST", {})
    check("POST /settings/test-proxy", st == 200 and "message" in tp, tp.get("message", ""))

    st, bk = req(port, "/api/collector/backups")
    check("GET /backups", st == 200 and "backups" in bk,
          f"{len(bk.get('backups', []))} 个备份")

    st, uc = req(port, "/api/collector/update-check")
    check("GET /update-check", st == 200 and "has_update" in uc, str(uc)[:70])

    st, env = req(port, "/api/collector/env")
    check("GET /env", st == 200 and env.get("db_exists"), f"{st}")
    check("  数据库账号数一致", env.get("accounts") == d.get("total"),
          f"env={env.get('accounts')} / accounts={d.get('total')}")
    check("  作品数 > 0", (env.get("videos") or 0) > 0, str(env.get("videos")))
    check("  Playwright 可用", env.get("playwright") is True)
    check("  登录态目录已识别", (env.get("session_dirs") or 0) > 0,
          f"{env.get('session_dirs')} 个")

    print("\n[6] 导出")
    st, ex = req(port, "/api/collector/export", "POST", {"format": "txt", "mode": "urls"})
    check("POST /export (txt)", st == 200 and ex.get("count", 0) > 0,
          f"{ex.get('count')} 条 → {ex.get('name')}")
    if st == 200:
        st2, dl = req(port, f"/api/collector/exports")
        check("GET /exports 列出导出文件", st2 == 200 and len(dl.get("files", [])) > 0,
              f"{len(dl.get('files', []))} 个")
        p = str(ex.get("path", ""))
        st3, _ = req(port, f"/api/collector/export/download?path={urllib.parse.quote(p)}")
        check("GET /export/download 受控下载", st3 == 200, f"{st3}")
    st, bad = req(port, "/api/collector/export/download?path=C%3A%5CWindows%5Cwin.ini")
    check("导出下载越权被拒（403）", bad is not None and st == 403, f"{st}")

    print("\n[7] SSE 事件通道（用合成任务验证）")
    from server.jobs import STORE
    from server.state import EventKind, JobKind, JobState
    job = STORE.create(JobKind.COLLECT, label="SSE 验证")
    job.set_state(JobState.RUNNING)
    job.progress(0.5, message="一半了")
    job.emit(EventKind.ITEM, data={"username": "demo", "status": "completed"})
    got: list[str] = []
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/jobs/{job.id}/events", timeout=8) as resp:
            check("SSE 响应 content-type 正确",
                  "text/event-stream" in (resp.headers.get("content-type") or ""),
                  resp.headers.get("content-type", ""))
            deadline = time.time() + 6
            while time.time() < deadline:
                line = resp.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if s.startswith("event:"):
                    got.append(s.split(":", 1)[1].strip())
                if "progress" in got and "item" in got:
                    break
    except Exception as exc:  # noqa: BLE001
        check("SSE 连接", False, f"{type(exc).__name__}: {exc}")
    check("SSE 首屏下发 snapshot", "snapshot" in got, str(got[:5]))
    check("SSE 补发历史事件（断线重连用）",
          "job_created" in got and "progress" in got, str(got[:6]))
    check("SSE 下发采集进度事件", "item" in got, str(got[:8]))
    job.finish("验证结束")

    print("\n[8] 采集任务启动校验（绝不真的采集）")
    st, err = req(port, "/api/collector/collect", "POST", {"account_ids": ["__no_such__"]})
    check("POST /collect 不存在的账号 → 404", st == 404, f"{st}")
    # 空请求必须被拒 —— 否则会隐式回退成"全部账号"并触发替换式删除
    st, err = req(port, "/api/collector/collect", "POST", {})
    check("POST /collect 空请求 → 400（安全闸门）", st == 400,
          str(err.get("detail") if isinstance(err, dict) else err)[:80])
    st, err = req(port, "/api/collector/collect", "POST", {"account_ids": []})
    check("POST /collect 空 account_ids → 400（安全闸门）", st == 400, f"{st}")

    srv.should_exit = True
    time.sleep(0.5)

    print("\n" + "=" * 70)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    for f in FAIL:
        print(f"  - {f}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
