/* ============================================================================
   TK 素材工坊 · 采集模块前端 v1.0.0
   承接原「TikTok 作品链接采集器」的 6 个视图：
     采集任务 / 账号管理 / 采集结果 / 每日归档 / 好友群聊 / 任务中心

   设计约定（沿用旧版 UX 契约，见 docs/MERGE_PLAN.md 第 6.5 节）：
     · 不用模态框打断操作 —— 破坏性动作走「内联二次确认」
     · 列表整行可点 —— 但输入框 / 链接 / 按钮上的点击不触发选中
     · 采集结果默认只显示「本次采集」，程序启动时列表为空
     · 破坏性动作（清空/删除）必须人明确点第二次

   依赖 app.js 暴露的 window.TKStudio（同一份 DOM、同一套请求与提示）。
   ========================================================================== */

(() => {
  "use strict";

  const S = window.TKStudio;
  if (!S) { console.error("[collector] TKStudio 未初始化，脚本顺序有误"); return; }

  const { $, el, esc, fmtSize, api, toast, writeLog, clearLog, setBar, watchJob,
          jobAction, PAGE_LOADERS, notify: Notify } = S;

  const API = "/api/collector";

  /** 把采集 meta 里的卡片外观设置同步给界面通知组件（app.js 的 Notify）。 */
  const syncNotifyCfg = () => {
    if (Notify && Notify.configure) Notify.configure(S_.meta);
  };

  // ------------------------------------------------------------ 小工具

  /** YYYY-MM-DD HH:MM:SS（或 epoch 秒）→ 短格式 */
  const shortTime = (v) => {
    if (v == null || v === "") return "—";
    if (typeof v === "number" || /^\d{9,}(\.\d+)?$/.test(String(v))) {
      const d = new Date(Number(v) * 1000);
      if (Number.isNaN(d.getTime())) return "—";
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    const s = String(v);
    return s.length >= 16 ? s.slice(5, 16) : s;
  };

  const nz = (v) => (v == null || v === "" ? "—" : v);

  function copyText(text, okMsg = "已复制") {
    const t = String(text || "");
    if (!t) { toast("没有可复制的内容"); return; }
    const done = () => toast(okMsg);
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(t).then(done).catch(() => fallback());
    } else { fallback(); }
    function fallback() {
      const ta = document.createElement("textarea");
      ta.value = t;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); }
      catch { toast("复制失败，请手动选择文本"); }
      document.body.removeChild(ta);
    }
  }

  function saveText(filename, text) {
    const blob = new Blob([String(text || "")], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 400);
  }

  /** 内联二次确认：把按钮替换成「确定/取消」，不再弹模态框。 */
  function inlineConfirm(btn, onConfirm, question = "确定？") {
    if (!btn || btn.dataset.confirming === "1") return;
    const parent = btn.parentNode;
    const holder = el("span", "inline-confirm");
    holder.appendChild(document.createTextNode(question + " "));
    const yes = el("button", "btn btn-danger btn-sm", "确定");
    const no = el("button", "btn btn-ghost btn-sm", "取消");
    holder.appendChild(yes);
    holder.appendChild(no);

    btn.dataset.confirming = "1";
    btn.style.display = "none";
    parent.insertBefore(holder, btn);
    const restore = () => {
      delete btn.dataset.confirming;
      btn.style.display = "";
      if (holder.parentNode) holder.parentNode.removeChild(holder);
    };
    yes.addEventListener("click", (e) => { e.stopPropagation(); restore(); onConfirm(); });
    no.addEventListener("click", (e) => { e.stopPropagation(); restore(); });
  }

  function emptyState(icon, title, desc) {
    const box = el("div", "empty");
    box.appendChild(el("div", "empty-ico", icon));
    box.appendChild(el("div", "empty-title", title));
    if (desc) box.appendChild(el("div", "empty-desc", desc));
    return box;
  }

  function loadingState(text = "加载中…") {
    const box = el("div", "empty");
    box.appendChild(el("div", "spinner"));
    box.appendChild(el("div", "empty-desc", text));
    return box;
  }

  function fillTable(wrap, headers, rows, opts = {}) {
    wrap.innerHTML = "";
    if (!rows.length) {
      wrap.appendChild(opts.empty || emptyState("◇", "暂无数据"));
      return;
    }
    const table = el("table");
    const thead = el("thead");
    const htr = el("tr");
    headers.forEach((h) => htr.appendChild(el("th", null, h)));
    thead.appendChild(htr);
    table.appendChild(thead);
    const tb = el("tbody");
    rows.forEach((cells, i) => {
      const tr = el("tr");
      if (opts.rowClass) tr.className = opts.rowClass(cells, i);
      (Array.isArray(cells) ? cells : [cells]).forEach((c) => {
        if (c instanceof Node) { const td = el("td"); td.appendChild(c); tr.appendChild(td); }
        else {
          const td = el("td");
          if (c && typeof c === "object" && c.__html != null) td.innerHTML = c.__html;
          else td.textContent = c == null ? "—" : String(c);
          tr.appendChild(td);
        }
      });
      if (opts.onRowClick) tr.addEventListener("click", (e) => {
        if (e.target.closest("input, button, a, select")) return;
        opts.onRowClick(tr, i, e);
      });
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
  }

  const H = (html) => ({ __html: html });

  // 状态中文名（状态筛选下拉与标签共用同一份，避免两处各写而对不上）
  const STATUS_CN = {
    completed: "完成", partial: "不足", empty: "无作品", failed: "失败",
    skipped: "已跳过", running: "采集中", pending: "等待", stopped: "已停止",
    paused: "已暂停", ok: "正常",
    // 登录态：`unknown` 是「这台机器还没探活过」，不是「一定没登录」——
    // 程序目录里有 data/sessions/<账号> 就说明登录态还在。文案必须区分，
    // 否则会让人误以为要重新登录 21 个账号。
    unknown: "未检测", valid: "已登录",
    expired: "登录过期", logging_in: "登录中",
  };

  const statusTag = (status, label) => {
    const map = {
      completed: "tag-ready", partial: "tag-planned", empty: "tag-bad",
      failed: "tag-bad", skipped: "tag-optional", running: "tag-running",
      pending: "tag-optional", stopped: "tag-optional", paused: "tag-planned",
    };
    const cls = map[status] || "tag-optional";
    return `<span class="tag ${cls}">${esc(label || STATUS_CN[status] || status || "—")}</span>`;
  };

  const regionTag = (region, noneLabel = "未分类") =>
    region ? `<span class="tag tag-info">${esc(region)}</span>`
           : `<span class="tag tag-optional">${esc(noneLabel)}</span>`;

  // ------------------------------------------------------------ 共享状态

  const S_ = {
    meta: null,
    accounts: [],
    picked: new Set(),          // 跨筛选保留的勾选
    countTouched: false,        // 用户是否改过「每账号数量」
    collectInited: false,       // 采集页配置行（数量/日期/间隔）是否已初始化过
                                // —— 用它而不是 `!meta` 来判断，见 loadCollect 注释
    lastFailedIds: [],          // 最近一次采集中失败的账号（供「重试失败账号」）
    region: "",
    search: "",
    enabledFilter: "",          // "" 全部 / "1" 仅启用 / "0" 仅停用
    nickJobId: "",
    lastTaskId: "",
    results: {
      scope: "session", mode: "urls", rows: [], rawRows: [],
      urls: "", grouped: "", accountId: "", date: "", status: "",
    },
    // 聊天链接采集（按原程序布局：左列会话列表 + 右列操作区）
    chat: {
      accountId: "", target: null, recent: [], jobId: null,
      convs: [], filter: "", type: "", loading: false,
    },
    daily: { files: [], current: "", text: "", day: "" },
  };

  async function fetchAccounts(opts = {}) {
    const params = new URLSearchParams();
    if (S_.region) params.set("region", S_.region);
    const q = params.toString();
    const d = await api(`${API}/accounts${q ? "?" + q : ""}`);
    S_.accounts = d.accounts || [];
    if (opts.render !== false) {
      renderCollectAccounts();
      renderAccountsTable();
      fillAccountSelects();
    }
    return d;
  }

  /**
   * 地区下拉项。
   *
   * ⚠️ /meta 的 regions 是**字符串数组**（["越南","缅甸",...]），
   *    不是 {value,label} 对象 —— 早期按对象读会把选项值写成字面量 "undefined"，
   *    结果「按地区采集」实际是按一个不存在的地区去筛，永远筛出 0 个账号。
   *    这里做归一化，并把 region_stats 的计数拼进文案。
   */
  function regionOptions() {
    const stats = new Map(
      (S_.meta?.region_stats || []).map((s) => [s.region, s]));
    const list = [];
    (S_.meta?.regions || []).forEach((r) => {
      const name = typeof r === "string" ? r : (r.value || r.label || "");
      if (!name) return;
      const st = stats.get(name);
      list.push({
        value: name,
        label: (typeof r === "object" && r.label) || st?.label || name,
        count: (typeof r === "object" && r.count) || st?.count || 0,
      });
    });
    // 有未分类账号时补一个哨兵项（后端认 "__none__"）
    const none = stats.get("");
    if (none && none.count) {
      list.push({ value: S_.meta.region_unspecified || "__none__",
                  label: S_.meta.region_none || "未分类", count: none.count });
    }
    return list;
  }

  /** 把地区选项灌进一个 <select>，首项固定是「全部地区」。 */
  function fillRegionSelect(sel) {
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = "";
    const all = el("option", null, "全部地区");
    all.value = "";
    sel.appendChild(all);
    regionOptions().forEach((r) => {
      const o = el("option", null, r.count ? `${r.label}（${r.count}）` : r.label);
      o.value = r.value;
      sel.appendChild(o);
    });
    // 尽量恢复原选择，恢复不了就回到「全部地区」
    sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "";
  }

  /** 填充「添加账号」行的地区下拉（首项为「未分类」= 空值）。 */
  function fillQuickRegion(sel) {
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = "";
    const none = el("option", null, "未分类");
    none.value = "";
    sel.appendChild(none);
    regionOptions().forEach((r) => {
      const o = el("option", null, r.label);
      o.value = r.value;
      sel.appendChild(o);
    });
    sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "";
  }

  /** 添加行：输入主页链接或 @用户名后回车（或点「添加」）即入库（原版 addbar）。 */
  async function quickAddAccount() {
    const box = $("ac-quick");
    const raw = (box.value || "").trim();
    if (!raw) { toast("请输入主页链接或 @用户名"); box.focus(); return; }
    const btn = $("ac-quick-add");
    btn.disabled = true;
    try {
      const r = await api(`${API}/accounts`, {
        method: "POST",
        body: { raws: [raw], region: ($("ac-quick-region") || {}).value || "" },
      });
      if (r.added) toast(`已添加 ${r.added} 个账号`);
      else if (r.skipped) toast("该账号已存在，已跳过");
      else toast(`添加失败${r.failed ? `（${r.failed} 个）` : ""}`);
      box.value = "";
      await fetchAccounts();
      renderAccountsTable();
    } catch (e) {
      toast("添加失败：" + e.message);
    } finally {
      btn.disabled = false;
    }
  }


  // ==========================================================================
  //  让某视图「每次进入都重新加载」
  //  仪表盘 / 历史记录展示的是统计快照，缓存首屏数字会过期；
  //  app.js 的 pageLoaded 标记是幂等缓存的开关，这里加载完就复位。
  // ==========================================================================

  function alwaysReload(pageId, fn) {
    PAGE_LOADERS[pageId] = () => {
      try { return fn(); }
      finally { if (S.pageLoaded) S.pageLoaded[pageId] = false; }
    };
  }

  // ==========================================================================
  //  视图零：仪表盘
  // ==========================================================================

  function statText(id, v) {
    const n = $(id);
    if (n) n.textContent = v == null || v === "" ? "—" : String(v);
  }

  async function loadDashboard() {
    const wrap = $("db-tasks");
    if (!wrap) return;
    try {
      const d = await api(`${API}/overview`);
      const a = d.accounts || {};
      const v = d.videos || {};
      const t = d.tasks || {};
      const ch = d.chat || {};

      statText("db-a-total", a.total);
      statText("db-a-enabled", a.enabled);
      statText("db-a-disabled", a.disabled);
      statText("db-v-total", v.total);
      statText("db-v-today", v.today);
      statText("db-t-total", t.total);
      statText("db-chat", ch.targets);
      statText("db-date", d.today ? `· 统计日期 ${d.today}（${d.timezone || ""}）` : "");

      // ---- 未完成任务横幅（可一键跳去恢复） ----
      const uf = t.unfinished;
      const banner = $("db-unfinished");
      if (uf && uf.task_id) {
        $("db-unfinished-text").innerHTML =
          `⚠ 有<strong>未完成</strong>的采集任务 <span class="mono">${esc(uf.task_id)}</span>` +
          `（${esc(uf.status_label || uf.status)} · 创建于 ${esc(shortTime(uf.created_at))}）。` +
          `可到「采集任务」页点「恢复未完成」继续跑完。`;
        banner.style.display = "";
      } else if (banner) {
        banner.style.display = "none";
      }

      // ---- 首屏引导（账号为 0 时才出现） ----
      const guide = $("db-guide");
      if (guide) guide.style.display = (a.total ? "none" : "");

      // ---- 最近任务 ----
      const rows = t.recent || [];
      $("db-task-note").textContent = t.total
        ? `共 ${t.total} 个任务，显示最近 ${rows.length} 个` : "";
      wrap.innerHTML = "";
      if (!rows.length) {
        wrap.appendChild(emptyState("◷", "还没有采集任务",
          "去「采集任务」页选中账号跑一次，任务就会出现在这里。"));
      } else {
        fillTable(wrap, ["任务", "记录日期", "状态", "账号", "实际 / 目标", "创建时间"],
          rows.map((r) => [
            H(`<span class="mono">${esc(r.task_id)}</span>`),
            esc(r.target_date || "—"),
            H(statusTag(r.status)),
            String(r.account_count || 0),
            `${r.actual_total || 0} / ${r.target_total || 0}`,
            H(`<span class="time">${esc(shortTime(r.created_at))}</span>`),
          ]),
          { onRowClick: (tr, i) => showTaskDetail("db", rows[i].task_id) });
      }
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "统计加载失败", e.message));
    }
  }

  function bindDashboardOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindDashboardOnce._done) return;
    bindDashboardOnce._done = true;
    on("db-refresh", "click", loadDashboard);
    on("db-go-collect", "click", () => S.activatePage("collect"));
    on("db-go-accounts", "click", () => S.activatePage("accounts"));
    bindTaskDetailOnce("db");
    startDashboardAutoRefresh();
  }

  /** 仪表盘自动刷新 —— 原桌面版每 3 秒重算一次统计卡与最近任务。
   *
   *  用单例定时器 + 三重守卫，避免 alwaysReload 每次进页面重复创建定时器：
   *  · 窗口切到后台 / 最小化时不刷（省电，也不会刷到不可见的 DOM）
   *  · 当前页不是仪表盘时不刷（避免与其它页抢渲染）
   *  · 有弹窗打开时不刷（否则会打断用户正在操作的弹窗）
   */
  let dashTimer = null;
  function startDashboardAutoRefresh() {
    if (dashTimer) return;
    dashTimer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (location.hash.slice(1) !== "dashboard") return;
      if (document.querySelector(".modal-mask")) return;
      loadDashboard().catch(() => {});
    }, 3000);
  }

  // ==========================================================================
  //  任务详情（仪表盘 / 历史记录共用；prefix = 容器 id 前缀 db|hi）
  // ==========================================================================

  const taskDetailCache = {};      // prefix -> 当前展示的 url_text

  async function showTaskDetail(prefix, taskId) {
    const card = $(`${prefix}-detail`);
    if (!card) return;
    const kv = $(`${prefix}-detail-kv`);
    const accts = $(`${prefix}-detail-accts`);
    const links = $(`${prefix}-detail-links`);
    card.style.display = "";
    $(`${prefix}-detail-id`).textContent = taskId;
    kv.innerHTML = "";
    accts.innerHTML = "";
    links.textContent = "加载中…";
    try {
      const d = await api(`${API}/tasks/${encodeURIComponent(taskId)}`);
      const t = d.task || {};
      [
        ["任务 ID", t.task_id], ["记录日期", t.target_date || "—"],
        ["状态", t.status_label || t.status], ["账号数", t.account_count],
        ["目标 / 实际", `${t.target_total || 0} / ${t.actual_total || 0}`],
        ["成功 / 失败", `${t.success_count || 0} / ${t.fail_count || 0}`],
        ["创建时间", shortTime(t.created_at)],
        ["完成时间", t.completed_at ? shortTime(t.completed_at) : "—"],
      ].forEach(([k, val]) => {
        const row = el("div", "kv-row");
        row.appendChild(el("div", "kv-k", k));
        row.appendChild(el("div", "kv-v", val == null ? "—" : String(val)));
        kv.appendChild(row);
      });

      const accounts = d.accounts || [];
      if (accounts.length) {
        fillTable(accts, ["账号", "目标", "实际", "状态", "备注"], accounts.map((r) => [
          esc(r.username || r.account_id),
          String(r.target_count || 0),
          String(r.actual_count || 0),
          H(statusTag(r.status)),
          H(`<span class="muted">${esc(r.error || "—")}</span>`),
        ]));
      } else {
        accts.appendChild(emptyState("◇", "该任务没有账号明细"));
      }

      taskDetailCache[prefix] = d.url_text || "";
      links.textContent = d.url_text || "（该任务没有采到链接）";
      const note = $(`${prefix}-detail-note`);
      if (note) {
        note.textContent = `${(d.videos || []).length} 条作品链接`;
      }
      card.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      links.textContent = "加载失败：" + e.message;
    }
  }

  function bindTaskDetailOnce(prefix) {
    on(`${prefix}-detail-close`, "click", () => {
      const card = $(`${prefix}-detail`);
      if (card) card.style.display = "none";
    });
    on(`${prefix}-detail-copy`, "click", () => {
      copyText(taskDetailCache[prefix] || "", "该任务链接已复制");
    });
    on(`${prefix}-detail-export`, "click", () => {
      const id = $(`${prefix}-detail-id`).textContent || "task";
      const text = taskDetailCache[prefix] || "";
      if (!text.trim()) { toast("没有可导出的链接"); return; }
      saveText(`${id}_links.txt`, text);
      toast("已导出 TXT");
    });
  }

  // ==========================================================================
  //  视图一：采集任务
  // ==========================================================================

  function renderCollectAccounts() {
    const wrap = $("col-accts");
    if (!wrap) return;
    const kw = S_.search.trim().toLowerCase();
    const list = S_.accounts.filter((a) => {
      if (!kw) return true;
      return String(a.display_name || "").toLowerCase().includes(kw)
          || String(a.username || "").toLowerCase().includes(kw);
    });

    $("col-acct-count").textContent =
      `共 ${S_.accounts.length} 个${kw ? `，匹配 ${list.length} 个` : ""}（只采集启用中的账号）`;

    wrap.innerHTML = "";
    if (!list.length) {
      wrap.appendChild(emptyState("◎", "该地区下没有启用账号",
        "请切换「地区」筛选，或到「账号管理」新增账号。"));
      updatePicked();
      return;
    }

    list.forEach((a) => {
      const row = el("div", "acct-row");
      row.dataset.id = a.account_id;
      if (!a.enabled) row.style.opacity = ".45";

      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = S_.picked.has(a.account_id);
      cb.disabled = !a.enabled;
      cb.addEventListener("change", () => {
        if (cb.checked) S_.picked.add(a.account_id);
        else S_.picked.delete(a.account_id);
        updatePicked();
      });

      const name = el("div", "acct-name");
      name.innerHTML =
        `<span class="nick">${esc(a.display_name || "（昵称待获取）")}</span>` +
        `<span class="user">@${esc(a.username)}</span>`;
      // ⚠️ 地区 / 状态标签走**独立**的 .acct-meta 列，不能塞进 .acct-name：
      //    .acct-name 是 nowrap + 省略号，标签在里面会和用户名抢同一行，
      //    实测两者间距只有 0~1px，「越南」看着像压在 @账号 上。
      const meta = el("span", "acct-meta");
      const regWrap = el("span");
      regWrap.innerHTML = regionTag(a.region, S_.meta?.region_none || "未分类");
      meta.appendChild(regWrap);
      if (!a.enabled) meta.appendChild(el("span", "tag tag-optional", "已停用"));

      // 每账号独立数量（原桌面版就有）：改完立即落库，
      // 这样「统一数量」和「个别调整」互不冲突。
      const qtyWrap = el("span", "acct-qty-wrap");
      const qty = el("input", "acct-qty");
      qty.type = "number";
      qty.min = "1"; qty.max = "50";
      qty.value = String(a.collect_count ?? 4);
      qty.title = "该账号的采集数量（改完立即保存）";
      qty.addEventListener("click", (e) => e.stopPropagation());
      qty.addEventListener("change", async () => {
        const v = Math.max(1, Math.min(50, parseInt(qty.value, 10) || 4));
        qty.value = String(v);
        try {
          await api(`${API}/accounts/${encodeURIComponent(a.account_id)}`,
            { method: "PATCH", body: { collect_count: v } });
          a.collect_count = v;
          toast(`@${a.username} 采集数量 → ${v}`);
        } catch (err) { toast("保存失败：" + err.message); }
      });

      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(meta);
      row.appendChild(el("span", "muted nowrap acct-last", "最后 " + shortTime(a.last_collect_time)));
      qtyWrap.appendChild(qty);
      qtyWrap.appendChild(el("span", "muted nowrap", "条"));
      row.appendChild(qtyWrap);
      row.addEventListener("click", (e) => {
        if (e.target.closest("input")) return;
        if (!a.enabled) return;
        if (S_.picked.has(a.account_id)) S_.picked.delete(a.account_id);
        else S_.picked.add(a.account_id);
        cb.checked = S_.picked.has(a.account_id);
        updatePicked();
      });

      wrap.appendChild(row);
    });
    updatePicked();
  }

  function updatePicked() {
    document.querySelectorAll("#col-accts .acct-row").forEach((r) => {
      r.classList.toggle("picked", S_.picked.has(r.dataset.id));
    });
    const n = S_.picked.size;
    const total = (S_.accounts || []).length;
    $("col-picked").textContent = n
      ? `已选 ${n}/${total}`
      : (S_.region ? `未勾选账号 → 将按地区「${S_.region}」采集` : `已选 0/${total}`);
    $("col-start").disabled = n === 0 && !S_.region;
    $("col-warn-replace").style.display = n || S_.region ? "" : "none";
  }

  // 本地日期 YYYY-MM-DD。
  // ⚠️ 不能用 toISOString()：那是 UTC，东八区凌晨会得到「昨天」。
  function todayStr(offsetDays = 0) {
    const d = new Date();
    d.setDate(d.getDate() + (offsetDays || 0));
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  // 失败账号重试栏：采集结束后若有失败项，列出来并给一键重试。
  function renderRetryBar(failed) {
    const bar = $("col-retry-bar");
    const msg = $("col-retry-msg");
    if (!bar || !msg) return;
    const list = (failed || []).filter((a) => a && a.username);
    if (!list.length) { bar.style.display = "none"; return; }
    const needLogin = list.filter((a) =>
      /登录|login/i.test(a.error_reason || "")).length;
    msg.innerHTML = `有 <strong>${list.length}</strong> 个账号采集失败`
      + (needLogin ? `（其中 ${needLogin} 个需先登录）` : "")
      + `：${list.slice(0, 6).map((a) => "@" + esc(a.username)).join("、")}`
      + (list.length > 6 ? " …" : "");
    bar.style.display = "";
  }

  // ==========================================================================
  //  采集结束统一收尾
  //  ① 让「采集结果」下次进入必定重拉 —— 它是**数据**视图，缓存住就永远
  //     看不到新采到的东西（这正是「采集完了结果页还是空的」的根因）
  //  ② 用户若正停在结果页 → 立刻就地刷新，不用手动切页再切回来
  //  ③ 弹系统通知（**桌面居中弹窗** + 提示音）—— 切到别的窗口也不会漏掉「采完了」
  // ==========================================================================

  /** 一行话概括本次采集（优先用后端给的 summary，回落为本地统计）。 */
  function summarizeJob(j) {
    const s = (j && j.summary) || "";
    if (s) return s;
    const accts = (j && j.accounts) || [];
    // 统计缺失时**不要**报「共 0 个账号：完成 0」—— 那看起来像「一条都没采到」，
    // 实际上只是收尾事件没带数据（2026-10-08 用户反馈的假 0）。
    if (!accts.length) return "本次采集已结束，明细请见采集日志与「采集结果」";
    const ok = accts.filter(
      (a) => ["completed", "partial", "empty"].includes(String(a.status))).length;
    const bad = accts.filter((a) => a.status === "failed").length;
    return `共 ${accts.length} 个账号：完成 ${ok}${bad ? `，失败 ${bad}` : ""}`;
  }

  /** 发一条采集通知。失败只吞掉 —— 通知不该打断采集主流程。
   *
   *  2026-10-11 起**和去水印 / 高清修复共用同一个出口**：app.js 的
   *  window.TKStudio.notifyCompletion（程序界面右下角卡片）。
   *  以前这里自己判断窗口焦点、自己 POST /api/system/notify，结果
   *  pywebview 下 hasFocus() 常为 false → 变成桌面浮层，而且和水印侧
   *  的弹窗长得不一样。现在只有一个实现、一个位置。
   *  提示音仍按采集设置里的 notify_sound 走。 */
  function notifySystem(title, message, kind) {
    const meta = S_.meta || {};
    const sound = meta.notify_sound !== false;
    const shared = window.TKStudio && window.TKStudio.notifyCompletion;
    if (shared) {
      shared(message, kind, title, { sound });
      return;
    }
    // 兜底（理论上到不了）：app.js 没加载时至少画个界面内卡片
    if (Notify) Notify.show({ title, message, kind, timeout: 9000 });
  }

  function afterCollectEnd(kind, j, label) {
    j = j || {};
    S_.lastTaskId = j.task_id || S_.lastTaskId || "";
    S_.results.scope = "session";

    // ① 失效结果页缓存
    if (S.pageLoaded) S.pageLoaded.results = false;
    // ② 正停在结果页 → 立刻刷新
    const active = document.querySelector(".nav-item.active");
    if (active && active.dataset.page === "results") {
      loadResults({ silent: true }).catch(() => {});
    }

    // ③ 系统通知
    const stopped = kind === "job_cancelled";
    const failedN = (j.accounts || []).filter((a) => a.status === "failed").length;
    let uk = "ok";
    let title = `${label}完成`;
    if (stopped) { uk = "warn"; title = `${label}已停止`; }
    else if (kind === "job_error") { uk = "error"; title = `${label}失败`; }
    else if (failedN) { uk = "warn"; title = `${label}结束（${failedN} 个账号失败）`; }
    notifySystem(title, summarizeJob(j), uk);
  }

  // 重试最近一次失败的账号（沿用它们各自设定的数量）
  async function retryFailedAccounts() {
    const ids = S_.lastFailedIds || [];
    if (!ids.length) { toast("没有需要重试的账号"); return; }
    $("col-start").disabled = true;
    $("col-run-card").style.display = "";
    clearLog("col-log", `正在重试 ${ids.length} 个失败账号…`);
    setBar("col-bar", 0);
    try {
      const r = await api(`${API}/collect`,
        { method: "POST", body: { account_ids: ids } });
      toast(`已重新启动：${r.account_count} 个账号`);
      $("col-run-state").textContent =
        `重试 · 任务 ${r.job_id.slice(0, 8)} · ${r.target_date}`;
      watchJob(r.job_id, {
        bar: "col-bar", log: "col-log", label: "重试",
        onDone: (kind, ev) => {
          $("col-start").disabled = false;
          $("col-cancel").disabled = true;
          $("col-pause").disabled = true;
          $("col-resume").disabled = true;
          const j = ev.data || {};
          renderAccountResults(j.accounts || [], j.summary || "");
          const failed = (j.accounts || []).filter((a) => a.status === "failed");
          S_.lastFailedIds = failed.map((a) => a.account_id).filter(Boolean);
          renderRetryBar(failed);
          // 提示同样交给 afterCollectEnd 的卡片，不再另发 toast
          afterCollectEnd(kind, j, "重试");
          fetchAccounts({ render: true }).catch(() => {});
        },
      });
      $("col-cancel").disabled = false;
      $("col-pause").disabled = false;
      $("col-resume").disabled = true;
    } catch (e) {
      $("col-start").disabled = false;
      toast("重试失败：" + e.message);
    }
  }

  // 检测上次被中断、尚未跑完的采集任务，提示并勾选待采账号
  async function resumeUnfinished() {
    try {
      const d = await api(`${API}/tasks?limit=10`);
      const done = ["completed", "done"];
      const unfinished = (d.tasks || []).find(
        (t) => t.status && !done.includes(String(t.status)) && !t.finished_at);
      if (!unfinished) { toast("没有未完成的采集任务"); return; }

      const detail = await api(
        `${API}/tasks/${encodeURIComponent(unfinished.task_id)}`);
      const okStatus = ["completed", "partial", "empty"];
      const pending = (detail.accounts || [])
        .filter((a) => !okStatus.includes(String(a.status)))
        .map((a) => a.account_id).filter(Boolean);
      if (!pending.length) { toast("该任务已无待采账号"); return; }

      S_.picked = new Set(pending);
      renderCollectAccounts();
      toast(`任务 ${unfinished.task_id} 还有 ${pending.length} 个账号未完成，`
            + `已勾选，点「开始采集」继续`);
    } catch (e) { toast("查询未完成任务失败：" + e.message); }
  }

  async function loadCollect() {
    const wrap = $("col-accts");
    wrap.innerHTML = "";
    wrap.appendChild(loadingState("加载采集配置…"));
    try {
      // ⚠️ 这个 if 里**只允许放"取 meta"本身**。
      //    曾经把「填充地区下拉 / 时区 / 采集日期 / 账号间隔」整块塞在
      //    `if (!S_.meta)` 下面 —— 而 meta 是**全局共享**的：侧栏里「账号管理」
      //    排在「采集任务」前面，用户先去过账号管理，S_.meta 就已有值，
      //    于是整块被跳过 → 地区下拉永远只剩「全部地区」（用户报的正是这个），
      //    采集日期、账号间隔、时区标签也一起变空。
      //    教训：**共享缓存的"有没有" 绝不能当作"我该不该初始化自己的控件"的判据。**
      if (!S_.meta) S_.meta = await api(`${API}/meta`);
      syncNotifyCfg();   // 卡片外观跟着 meta 走（宽度/缩放）

      // 时区标签：只读展示，无条件刷新（幂等）
      const tz = $("col-tz");
      if (tz) tz.textContent = "时区 " + (S_.meta.timezone || "—");

      // 地区下拉：无条件重建。fillRegionSelect 内部会先记住再恢复当前选择，
      // 所以反复调用是安全的（幂等），也不会把用户选的地区弹回"全部"。
      fillRegionSelect($("col-region"));

      // 下面三个是**用户可编辑**的输入框：只在"本次会话还没初始化过 +
      // 当前为空"时补默认值，绝不覆盖用户已经改过的内容。
      if (!S_.collectInited) {
        S_.collectInited = true;
        const cnt = $("col-count");
        if (cnt) cnt.value = String(S_.meta.default_collect_count || 4);
        const dateEl = $("col-date");
        if (dateEl && !dateEl.value) dateEl.value = todayStr();
        const ivEl = $("col-interval");
        if (ivEl && !ivEl.value) {
          ivEl.value = String(S_.meta.account_interval_seconds ?? 4);
        }
      }
      const d = await fetchAccounts();
      const missing = d.missing_nickname || 0;
      const tip = $("ac-nick-tip");
      if (tip && missing) {
        tip.style.display = "";
        tip.innerHTML = `有 <strong>${missing}</strong> 个账号还没有昵称，` +
          `到「账号管理」点「刷新昵称」即可自动补齐（约 1 个账号 3～5 秒）。`;
      }
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "加载失败", e.message));
    }
  }

  async function startCollect() {
    const ids = [...S_.picked];
    const payload = {};
    if (ids.length) {
      payload.account_ids = ids;
    } else if (S_.region) {
      payload.region = S_.region;
    } else {
      toast("请先勾选账号，或选择一个地区");
      return;
    }
    // 「每账号数量」只要被用户改过，就作为本次统一覆盖下发；
    // 没改过则不下发 —— 各账号沿用自己在「账号管理」里设的数量。
    if (S_.countTouched) {
      const n = Math.max(1, Math.min(50, parseInt($("col-count").value, 10) || 4));
      payload.collect_count = n;
    }
    const dateEl = $("col-date");
    if (dateEl && dateEl.value.trim()) payload.target_date = dateEl.value.trim();

    // 账号间隔随任务写回配置（与旧桌面版一致：改了就生效）
    const ivEl = $("col-interval");
    if (ivEl) {
      const iv = Math.max(0, Math.min(120, parseFloat(ivEl.value) || 0));
      api(`${API}/settings`,
        { method: "POST", body: { account_interval_seconds: iv } })
        .catch(() => { /* 非关键，不阻断采集 */ });
    }

    S_.lastFailedIds = [];
    renderRetryBar([]);
    $("col-start").disabled = true;
    $("col-run-card").style.display = "";
    clearLog("col-log", "正在启动采集…");
    setBar("col-bar", 0);
    try {
      const r = await api(`${API}/collect`, { method: "POST", body: payload });
      toast(`已启动：${r.account_count} 个账号`);
      $("col-run-state").textContent = `任务 ${r.job_id.slice(0, 8)} · ${r.target_date}`;
      watchJob(r.job_id, {
        bar: "col-bar", log: "col-log",
        buttons: [],
        label: "采集",
        onItems: null,
        onDone: (kind, ev) => {
          $("col-start").disabled = false;
          $("col-cancel").disabled = true;
          $("col-pause").disabled = true;
          $("col-resume").disabled = true;
          const j = ev.data || {};
          renderAccountResults(j.accounts || [], j.summary || "");
          // 记录本次失败账号，供「重试失败账号」一键重来
          const failed = (j.accounts || []).filter((a) => a.status === "failed");
          S_.lastFailedIds = failed.map((a) => a.account_id).filter(Boolean);
          renderRetryBar(failed);
          // 完成 / 停止 / 失败的提示统一由 afterCollectEnd → notifySystem 的
          // 界面内卡片负责（2026-10-11 去掉了这里的 toast：同一件事说两遍，
          // 而且卡片出现时会清空 toast 文字，只剩一条空壳）。
          afterCollectEnd(kind, j, "采集");
          fetchAccounts({ render: true }).catch(() => {});
        },
      });
      $("col-cancel").disabled = false;
      $("col-pause").disabled = false;
      $("col-resume").disabled = true;
    } catch (e) {
      $("col-start").disabled = false;
      toast("启动失败：" + e.message);
      writeLog("col-log", "启动失败：" + e.message);
    }
  }

  function renderAccountResults(accounts, summary) {
    const card = $("col-acc-card");
    const wrap = $("col-acc-table");
    card.style.display = "";
    $("col-summary").textContent = summary || "";
    const rows = accounts.map((a) => [
      a.username ? "@" + a.username : a.account_id,
      H(statusTag(a.status, a.status_label)),
      `${a.actual_count}/${a.target_count}`,
      (a.error ? H(`<span class="bad">${esc(a.error)}</span>`) : "") ||
        ((a.diagnostics && (a.diagnostics.pinned_collected || a.diagnostics.pinned_skipped))
          ? H(`<span class="muted">置顶：额外采 ${a.diagnostics.pinned_collected || 0} / 跳过 ${a.diagnostics.pinned_skipped || 0}</span>`)
          : ""),
    ]);
    fillTable(wrap, ["账号", "状态", "实际/目标", "备注"], rows,
      { empty: emptyState("◇", "本次没有账号结果") });
  }

  // ==========================================================================
  //  视图二：账号管理
  // ==========================================================================

  // 当前筛选条件下的账号（搜索 + 启用状态）。
  // 单独抽出来是因为「全选 / 反选 / 批量启停」都必须只作用于**看得见的**这些，
  // 否则用户会误操作到被筛掉的账号（旧桌面版就是这么定义的）。
  function visibleAccounts() {
    const kw = S_.search.trim().toLowerCase();
    return S_.accounts.filter((a) => {
      if (S_.enabledFilter === "1" && !a.enabled) return false;
      if (S_.enabledFilter === "0" && a.enabled) return false;
      if (!kw) return true;
      return String(a.display_name || "").toLowerCase().includes(kw)
          || String(a.username || "").toLowerCase().includes(kw);
    });
  }

  function renderAccountsTable() {
    const wrap = $("ac-table");
    if (!wrap) return;
    const list = visibleAccounts();

    const stat = $("ac-stat");
    const enabled = S_.accounts.filter((a) => a.enabled).length;
    const missing = S_.accounts.filter((a) => !a.display_name).length;
    stat.textContent =
      `${S_.accounts.length} 个账号 · 启用 ${enabled}` + (missing ? ` · 昵称待获取 ${missing}` : "");

    wrap.innerHTML = "";
    if (!list.length) {
      wrap.appendChild(emptyState("☰", "没有账号",
        "点右上角「＋ 新增账号」粘贴 TikTok 主页链接或 @用户名批量导入。"));
      updateAccountButtons();
      return;
    }

    const table = el("table");
    const heads = ["选择", "昵称", "账号", "地区", "采集数量", "登录状态", "最后采集", "状态", "备注", "操作"];
    const thead = el("thead");
    const htr = el("tr");
    heads.forEach((h) => htr.appendChild(el("th", null, h)));
    thead.appendChild(htr);
    table.appendChild(thead);

    const tb = el("tbody");
    list.forEach((a) => {
      const tr = el("tr", "pick-row");
      tr.dataset.id = a.account_id;
      tr.classList.toggle("picked", S_.picked.has(a.account_id));

      const cbTd = el("td");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = S_.picked.has(a.account_id);
      cb.addEventListener("change", () => {
        if (cb.checked) S_.picked.add(a.account_id);
        else S_.picked.delete(a.account_id);
        tr.classList.toggle("picked", cb.checked);
        updateAccountButtons();
      });
      cbTd.appendChild(cb);
      tr.appendChild(cbTd);

      const nickTd = el("td");
      nickTd.innerHTML = a.display_name
        ? esc(a.display_name)
        : `<span class="muted">昵称待获取</span>`;
      tr.appendChild(nickTd);

      const userTd = el("td", "mono");
      userTd.textContent = "@" + a.username;
      tr.appendChild(userTd);

      const regionTd = el("td");
      regionTd.innerHTML = regionTag(a.region, S_.meta?.region_none || "未分类");
      tr.appendChild(regionTd);

      const qtyTd = el("td");
      const qty = el("input", "acct-qty");
      qty.type = "number";
      qty.min = "1"; qty.max = "50";
      qty.value = String(a.collect_count ?? 4);
      qty.dataset.orig = String(a.collect_count ?? 4);
      qty.addEventListener("change", async () => {
        const v = Math.max(1, Math.min(50, parseInt(qty.value, 10) || 4));
        qty.value = String(v);
        try {
          await api(`${API}/accounts/${encodeURIComponent(a.account_id)}`,
            { method: "PATCH", body: { collect_count: v } });
          a.collect_count = v;
          qty.dataset.orig = String(v);
          toast(`@${a.username} 采集数量 → ${v}`);
        } catch (e) { toast("保存失败：" + e.message); qty.value = qty.dataset.orig; }
      });
      qtyTd.appendChild(qty);
      tr.appendChild(qtyTd);

      const loginTd = el("td");
      loginTd.innerHTML = statusTag(a.login_status);
      tr.appendChild(loginTd);

      const timeTd = el("td", "time");
      timeTd.textContent = shortTime(a.last_collect_time);
      tr.appendChild(timeTd);

      const enTd = el("td");
      enTd.innerHTML = a.enabled
        ? '<span class="tag tag-ready">启用</span>'
        : '<span class="tag tag-optional">停用</span>';
      tr.appendChild(enTd);

      const remarkTd = el("td", "muted");
      remarkTd.textContent = nz(a.remark);
      tr.appendChild(remarkTd);

      const opTd = el("td");
      const btnEdit = el("button", "btn btn-ghost btn-sm", "编辑");
      btnEdit.addEventListener("click", (e) => { e.stopPropagation(); openAccountDialog(a); });
      // 「重新登录」：后端一直有 POST /accounts/{id}/reset-login（清掉这台机器上
      // 保存的登录态），界面却没有入口 —— 登录态失效时用户只能干等。
      const btnRelogin = el("button", "btn btn-ghost btn-sm", "重新登录");
      btnRelogin.title = "清除这台机器保存的登录态，下次采集会重新打开浏览器登录";
      btnRelogin.addEventListener("click", (e) => {
        e.stopPropagation();
        inlineConfirm(btnRelogin, async () => {
          try {
            await api(`${API}/accounts/${encodeURIComponent(a.account_id)}/reset-login`,
              { method: "POST", body: {} });
            a.login_status = "unknown";
            toast(`已清除 @${a.username} 的登录态，下次采集会重新登录`);
            renderAccountsTable();
          } catch (err) { toast("清除登录态失败：" + err.message); }
        }, `清除 @${a.username} 的登录态？下次采集需要重新登录。`);
      });
      const btnDel = el("button", "btn btn-soft-danger btn-sm", "删除");
      btnDel.addEventListener("click", (e) => {
        e.stopPropagation();
        inlineConfirm(btnDel, async () => {
          try {
            await api(`${API}/accounts/${encodeURIComponent(a.account_id)}`, { method: "DELETE" });
            S_.picked.delete(a.account_id);
            toast(`已删除 @${a.username}`);
            await fetchAccounts();
          } catch (err) { toast("删除失败：" + err.message); }
        }, `删除 @${a.username}？`);
      });
      opTd.appendChild(btnEdit);
      opTd.appendChild(document.createTextNode(" "));
      opTd.appendChild(btnRelogin);
      opTd.appendChild(document.createTextNode(" "));
      opTd.appendChild(btnDel);
      tr.appendChild(opTd);

      // 整行点击 = 切换启用状态（这是用户明确要求的「整行可点」）
      tr.addEventListener("click", async (e) => {
        if (e.target.closest("input, button, a, select")) return;
        const next = !a.enabled;
        try {
          await api(`${API}/accounts/${encodeURIComponent(a.account_id)}`,
            { method: "PATCH", body: { enabled: next } });
          a.enabled = next;
          toast(`@${a.username} ${next ? "已启用" : "已停用"}`);
          renderAccountsTable();
          renderCollectAccounts();
        } catch (err) { toast("操作失败：" + err.message); }
      });

      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.innerHTML = "";
    wrap.appendChild(table);
    updateAccountButtons();
  }

  function updateAccountButtons() {
    const n = S_.picked.size;
    const setR = $("ac-set-region");
    const del = $("ac-delete");
    const en = $("ac-enable");
    const dis = $("ac-disable");
    const cnt = $("ac-edit-count");
    if (setR) setR.disabled = n === 0;
    if (cnt) { cnt.disabled = n === 0; cnt.textContent = n ? `编辑数量（${n}）` : "编辑数量"; }
    if (del) { del.disabled = n === 0; del.textContent = n ? `删除（${n}）` : "删除"; }
    if (en) { en.disabled = n === 0; en.textContent = n ? `启用（${n}）` : "启用"; }
    if (dis) { dis.disabled = n === 0; dis.textContent = n ? `停用（${n}）` : "停用"; }
    const picked = $("ac-picked");
    if (picked) picked.textContent = `已选 ${n} 项`;
  }

  // 对已勾选的账号批量启用 / 停用。
  // 后端没有批量端点，逐个 PATCH —— 账号量级只有几十个，
  // 且某个失败不影响其它（末尾汇总成功 / 失败数）。
  async function batchSetEnabled(next) {
    const ids = [...S_.picked];
    if (!ids.length) return;
    const byId = new Map(S_.accounts.map((a) => [a.account_id, a]));
    let ok = 0, fail = 0;
    for (const id of ids) {
      try {
        await api(`${API}/accounts/${encodeURIComponent(id)}`,
          { method: "PATCH", body: { enabled: next } });
        const a = byId.get(id);
        if (a) a.enabled = next;
        ok += 1;
      } catch { fail += 1; }
    }
    toast(`已${next ? "启用" : "停用"} ${ok} 个账号`
          + (fail ? `，失败 ${fail}` : ""));
    renderAccountsTable();
    renderCollectAccounts();
  }

  function openAccountDialog(account) {
    const mask = el("div", "modal-mask");
    const box = el("div", "modal");
    const head = el("div", "modal-head");
    head.appendChild(el("div", "modal-title", account ? `编辑 @${account.username}` : "新增账号"));
    const close = el("button", "modal-close", "✕");
    head.appendChild(close);
    const body = el("div", "modal-body");
    const foot = el("div", "modal-foot");
    box.appendChild(head); box.appendChild(body); box.appendChild(foot);
    mask.appendChild(box);
    document.body.appendChild(mask);

    const done = () => mask.remove();
    close.addEventListener("click", done);
    mask.addEventListener("click", (e) => { if (e.target === mask) done(); });

    const regionOpts = regionOptions().map((r) => r.label);

    if (account) {
      body.innerHTML = `
        <div class="row" style="margin-top:0">
          <label class="field grow">昵称<input type="text" id="dlg-nick" value="${esc(account.display_name || "")}"></label>
        </div>
        <div class="row">
          <label class="field grow">地区<input type="text" id="dlg-region" list="dlg-regions" value="${esc(account.region || "")}" placeholder="留空 = 未分类"></label>
          <label class="field">采集数量<input type="number" id="dlg-count" min="1" max="50" value="${esc(account.collect_count ?? 4)}"></label>
        </div>
        <div class="row">
          <label class="field grow">备注<input type="text" id="dlg-remark" value="${esc(account.remark || "")}"></label>
        </div>
        <datalist id="dlg-regions">${regionOpts.map((r) => `<option value="${esc(r)}">`).join("")}</datalist>
        <p class="muted">提示：只提交你改过的字段，不会覆盖其它数据。</p>`;
      const save = el("button", "btn btn-primary", "保存");
      save.addEventListener("click", async () => {
        const payload = {};
        const nick = $("dlg-nick").value.trim();
        const region = $("dlg-region").value.trim();
        const count = parseInt($("dlg-count").value, 10);
        const remark = $("dlg-remark").value;
        if (nick !== (account.display_name || "")) payload.display_name = nick;
        if (region !== (account.region || "")) payload.region = region;
        if (count && count !== account.collect_count) payload.collect_count = count;
        if (remark !== (account.remark || "")) payload.remark = remark;
        if (!Object.keys(payload).length) { toast("没有改动"); done(); return; }
        try {
          await api(`${API}/accounts/${encodeURIComponent(account.account_id)}`,
            { method: "PATCH", body: payload });
          toast("已保存");
          done();
          await fetchAccounts();
        } catch (e) { toast("保存失败：" + e.message); }
      });
      const cancel = el("button", "btn btn-ghost", "取消");
      cancel.addEventListener("click", done);
      foot.appendChild(cancel); foot.appendChild(save);
    } else {
      body.innerHTML = `
        <div class="row" style="margin-top:0">
          <span class="muted">每行一个主页链接或 @用户名</span>
          <span class="spacer"></span>
          <button class="btn btn-ghost btn-sm" id="dlg-file-btn">📄 从文件读取</button>
          <input type="file" id="dlg-file" accept=".txt,.csv,.tsv,.xlsx,.xlsm" style="display:none" />
        </div>
        <div class="row">
          <label class="field field-stack grow">账号（每行一条，支持 @用户名 或 主页链接）
            <textarea id="dlg-raws" rows="8" placeholder="@username&#10;https://www.tiktok.com/@username"></textarea>
          </label>
        </div>
        <div class="row">
          <label class="field grow">导入到地区：
            <input type="text" id="dlg-region" list="dlg-regions" placeholder="留空 = 未分类" />
          </label>
          <label class="field">采集数量<input type="number" id="dlg-count" min="1" max="50" value="${esc(S_.meta?.default_collect_count || 4)}"></label>
        </div>
        <datalist id="dlg-regions">${regionOpts.map((r) => `<option value="${esc(r)}">`).join("")}</datalist>
        <p class="muted">（可留「未分类」，导入后再批量归类）已存在的账号会自动跳过，不会重复导入。</p>`;

      // 从文件读取：只解析并回填到文本框，由用户过目后再点「导入」
      // （沿用旧桌面程序的节奏，避免选错文件就直接改库）
      $("dlg-file-btn").addEventListener("click", () => $("dlg-file").click());
      $("dlg-file").addEventListener("change", async () => {
        const f = $("dlg-file").files && $("dlg-file").files[0];
        if (!f) return;
        const btn = $("dlg-file-btn");
        btn.disabled = true;
        btn.textContent = "读取中…";
        try {
          const qs = new URLSearchParams({ name: f.name, dry_run: "1" });
          const res = await fetch(`${API}/accounts/import-file?${qs}`,
                                  { method: "POST", body: f });
          const r = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(r.detail || `HTTP ${res.status}`);
          $("dlg-raws").value = (r.raws || []).join("\n");
          toast(`已从「${f.name}」读取 ${r.parsed} 行，确认无误后点「导入」`);
        } catch (e) {
          toast("读取失败：" + e.message);
        } finally {
          btn.disabled = false;
          btn.textContent = "📄 从文件读取";
          $("dlg-file").value = "";
        }
      });

      const save = el("button", "btn btn-primary", "导入");
      save.addEventListener("click", async () => {
        const raws = $("dlg-raws").value.split("\n").map((s) => s.trim()).filter(Boolean);
        if (!raws.length) { toast("请至少输入一个账号"); return; }
        try {
          const r = await api(`${API}/accounts`, {
            method: "POST",
            body: {
              raws,
              collect_count: parseInt($("dlg-count").value, 10) || undefined,
              region: $("dlg-region").value.trim(),
            },
          });
          notifySystem("导入完成",
                       `新增 ${r.added}，跳过 ${r.skipped}，失败 ${r.failed}`,
                       r.failed ? "warn" : "ok");
          done();
          await fetchAccounts();
        } catch (e) { toast("导入失败：" + e.message); }
      });
      const cancel = el("button", "btn btn-ghost", "取消");
      cancel.addEventListener("click", done);
      foot.appendChild(cancel); foot.appendChild(save);
    }
  }

  function openRegionDialog() {
    const ids = [...S_.picked];
    if (!ids.length) return;
    const mask = el("div", "modal-mask");
    const box = el("div", "modal");
    box.style.width = "min(460px, 94vw)";
    box.innerHTML = `
      <div class="modal-head"><div class="modal-title">设置地区（${ids.length} 个账号）</div>
        <button class="modal-close">✕</button></div>
      <div class="modal-body">
        <p class="muted">可从下拉选择，也可直接输入新的地区名。</p>
        <div class="row" style="margin-top:0">
          <label class="field grow">地区名称<input type="text" id="rg-name" placeholder="例如 越南" autofocus></label>
        </div>
        <div class="capsules" id="rg-quick" style="margin-top:12px"></div>
        <p class="muted" style="margin-top:12px">留空并保存 = 设为「未分类」。</p>
      </div>
      <div class="modal-foot">
        <button class="btn btn-ghost" data-act="cancel">取消</button>
        <button class="btn btn-primary" data-act="ok">保存</button>
      </div>`;
    mask.appendChild(box);
    document.body.appendChild(mask);
    const done = () => mask.remove();

    const quick = box.querySelector("#rg-quick");
    const used = [...new Set(S_.accounts.map((a) => a.region).filter(Boolean))];
    (used.length ? used : regionOptions().map((r) => r.label))
      .slice(0, 12).forEach((name) => {
        const c = el("button", "capsule", name);
        c.addEventListener("click", () => { box.querySelector("#rg-name").value = name; });
        quick.appendChild(c);
      });

    box.querySelector(".modal-close").addEventListener("click", done);
    box.querySelector('[data-act="cancel"]').addEventListener("click", done);
    mask.addEventListener("click", (e) => { if (e.target === mask) done(); });
    box.querySelector('[data-act="ok"]').addEventListener("click", async () => {
      const region = box.querySelector("#rg-name").value.trim();
      try {
        const r = await api(`${API}/accounts/regions`,
          { method: "POST", body: { account_ids: ids, region } });
        toast(`已设置 ${r.updated} 个账号 → ${r.region}`);
        done();
        await fetchAccounts();
      } catch (e) { toast("设置失败：" + e.message); }
    });
  }

  /** 批量设置「每账号采集数量」（原桌面版账号页「编辑数量」按钮）。
   *
   *  只改数量，不动地区/备注/启用状态 —— 后端逐账号 PATCH，
   *  避免整对象覆盖把地区清空（历史踩坑）。
   */
  function openCountDialog() {
    const ids = [...S_.picked];
    if (!ids.length) return;
    const byId = new Map(S_.accounts.map((a) => [a.account_id, a]));
    const counts = ids.map((id) => Number(byId.get(id)?.collect_count) || 0);
    const same = counts.every((c) => c === counts[0]);
    const mask = el("div", "modal-mask");
    const box = el("div", "modal");
    box.style.width = "min(440px, 94vw)";
    box.innerHTML = `
      <div class="modal-head"><div class="modal-title">批量设置采集数量</div>
        <button class="modal-close">✕</button></div>
      <div class="modal-body">
        <p class="muted">为选中的 ${ids.length} 个账号统一设置采集数量：</p>
        <p class="muted">当前值：${same ? counts[0] : `不一致（${counts.join(" / ")}）`}</p>
        <div class="row" style="margin-top:0">
          <label class="field grow">每账号采集数量
            <input type="number" id="cn-value" min="0" max="200"
                   value="${same ? counts[0] : ""}" placeholder="0 ~ 200" autofocus>
          </label>
        </div>
        <div class="capsules" id="cn-quick" style="margin-top:10px"></div>
        <p class="muted" style="margin-top:10px">
          0 = 该账号不采作品（仅保留记录）。采集时「每账号数量」输入框的值会覆盖此处设置。
        </p>
      </div>
      <div class="modal-foot">
        <button class="btn btn-ghost" data-act="cancel">取消</button>
        <button class="btn btn-primary" data-act="ok">应用</button>
      </div>`;
    mask.appendChild(box);
    document.body.appendChild(mask);
    const done = () => mask.remove();

    const quick = box.querySelector("#cn-quick");
    [1, 2, 4, 6, 8, 10, 15, 20].forEach((n) => {
      const c = el("button", "capsule", String(n));
      c.addEventListener("click", () => { box.querySelector("#cn-value").value = String(n); });
      quick.appendChild(c);
    });

    box.querySelector(".modal-close").addEventListener("click", done);
    box.querySelector('[data-act="cancel"]').addEventListener("click", done);
    mask.addEventListener("click", (e) => { if (e.target === mask) done(); });
    box.querySelector('[data-act="ok"]').addEventListener("click", async () => {
      const raw = box.querySelector("#cn-value").value.trim();
      const n = Number(raw);
      if (!raw || !Number.isFinite(n) || n < 0 || n > 200) {
        toast("请输入 0 ~ 200 之间的整数");
        return;
      }
      try {
        const r = await api(`${API}/accounts/collect-count`,
          { method: "POST", body: { account_ids: ids, collect_count: Math.trunc(n) } });
        toast(`已为 ${r.updated} 个账号设置采集数量 = ${r.collect_count}`);
        done();
        await fetchAccounts();
      } catch (e) { toast("设置失败：" + e.message); }
    });
  }

  async function refreshNicknames() {
    const ids = [...S_.picked];
    const label = ids.length ? `选中的 ${ids.length} 个账号` : "全部缺昵称的账号";
    const btn = $("ac-refresh-nick");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "刷新中…";
    try {
      const r = await api(`${API}/accounts/refresh-nicknames`,
        { method: "POST", body: ids.length ? { account_ids: ids } : {} });
      $("ac-nick-tip").style.display = "";
      $("ac-nick-tip").innerHTML = `正在获取昵称（${label}）…`;
      watchJob(r.job_id, {
        bar: null, log: null, buttons: [],
        label: "昵称",
        onDone: async (kind) => {
          btn.disabled = false;
          btn.textContent = old;
          if (kind === "job_done") notifySystem("昵称刷新完成", "账号昵称已更新", "ok");
          await fetchAccounts();
          $("ac-nick-tip").style.display = "none";
        },
      });
    } catch (e) {
      btn.disabled = false;
      btn.textContent = old;
      toast("刷新失败：" + e.message);
    }
  }

  async function loadAccounts() {
    const wrap = $("ac-table");
    wrap.innerHTML = "";
    wrap.appendChild(loadingState("加载账号…"));
    try {
      if (!S_.meta) S_.meta = await api(`${API}/meta`);
      syncNotifyCfg();
      const sel = $("ac-region");
      if (sel && sel.options.length <= 1) fillRegionSelect(sel);
      const qsel = $("ac-quick-region");
      if (qsel && qsel.options.length <= 1) fillQuickRegion(qsel);
      await fetchAccounts();
      renderAccountsTable();
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "加载失败", e.message));
    }
  }

  // ==========================================================================
  //  视图三：采集结果
  // ==========================================================================

  async function loadResults(opts = {}) {
    const wrap = $("rs-table");
    if (opts.silent !== true) {
      wrap.innerHTML = "";
      wrap.appendChild(loadingState("查询结果…"));
    }
    try {
      const params = new URLSearchParams({ scope: S_.results.scope });
      if (S_.results.accountId) params.set("account_id", S_.results.accountId);
      if (S_.results.date) params.set("publish_date", S_.results.date);
      const d = await api(`${API}/results?${params}`);
      S_.results.rawRows = d.rows || [];   // 服务端结果（未做状态过滤）
      S_.results.urls = d.url_text || "";
      S_.results.grouped = d.grouped_text || "";
      fillStatusSelect(S_.results.rawRows);
      renderResults(d);
      fillAccountSelects();
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "查询失败", e.message));
    }
  }

  // 状态下拉按「本次查询实际出现过的状态」动态重建（旧桌面版同做法），
  // 并尽量保住用户已选的值。
  function fillStatusSelect(rows) {
    const sel = $("rs-status");
    if (!sel) return;
    const keep = sel.value;
    const seen = [...new Set((rows || []).map((r) => r.status).filter(Boolean))];
    sel.innerHTML = '<option value="">全部状态</option>';
    seen.forEach((s) => {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = STATUS_CN[s] || s;
      sel.appendChild(o);
    });
    sel.value = keep && seen.includes(keep) ? keep : "";
  }

  function renderResults(d) {
    d = d || {};
    const wrap = $("rs-table");
    const raw = S_.results.rawRows || [];
    // 状态筛选在前端做（后端 /results 不支持 status 参数）；
    // 注意 urls / grouped 文本仍来自服务端**未过滤**结果 —— 与旧桌面版一致。
    const st = S_.results.status;
    const rows = st ? raw.filter((r) => String(r.status) === st) : raw;
    S_.results.rows = rows;

    // 统计卡
    const stats = $("rs-stats");
    const mk = (label, value, cls, vcls) => {
      const c = el("div", "stat" + (cls ? " " + cls : ""));
      c.appendChild(el("div", "stat-value" + (vcls ? " " + vcls : ""), String(value)));
      c.appendChild(el("div", "stat-label", label));
      return c;
    };
    const accts = new Set(rows.map((r) => r.username));
    const latest = rows.reduce((m, r) => (r.collect_time > m ? r.collect_time : m), "");
    stats.innerHTML = "";
    // 空的时候不摆一排「0」统计卡 —— 那是纯噪音，空态说明已经足够
    stats.style.display = rows.length ? "" : "none";
    if (rows.length) {
      stats.appendChild(mk("作品总数", rows.length, "primary"));
      stats.appendChild(mk("覆盖账号", accts.size));
      // 最近采集是个「日期时间」不是纯数字：用同号等宽的次级尺寸，
      // 否则 28px 的数字字体会把它撑满整张卡。
      stats.appendChild(mk("最近采集", shortTime(latest), "accent", "stat-value-sm"));
    }

    $("rs-count").textContent = rows.length
      ? (st ? `共 ${rows.length} 条（筛选自 ${raw.length} 条）` : `共 ${rows.length} 条`)
      : "共 0 条";
    // 最近采集时间（原桌面版用绿色高亮，让「刚采完」一眼可见）
    const latestEl = $("rs-latest");
    if (latestEl) {
      latestEl.textContent = latest ? `最近采集 ${shortTime(latest)}` : "";
      latestEl.style.color = latest ? "var(--success)" : "";
    }

    // 注意：空态必须放在**可见**的卡片里，否则用户看到的就是一片空白。
    // 链接文本区（#rs-link-card）无条件可见 —— 原桌面版的下半屏就是这个
    // LinkTextPanel，「复制选中 / 复制全部」永远点得到（铁律 17）。
    $("rs-card").style.display = "";

    if (!rows.length) {
      wrap.innerHTML = "";
      wrap.appendChild(st
        ? emptyState("🔎", "当前筛选没有结果",
            `「${STATUS_CN[st] || st}」状态下没有链接。把状态改回「全部状态」再点「查询」。`)
        : (S_.results.scope === "session"
          ? emptyState("🔗", "本次采集还没有结果",
              d.empty_reason === "本次运行尚未采集"
                ? "程序刚启动时这里是空的 —— 这是有意的。到「采集任务」选账号跑一次，结果会立刻出现在这里。"
                : "本次采集没有抓到任何作品，去「任务中心」看这次任务的运行日志。")
          : emptyState("🔗", "历史里也没有作品记录",
              "要么还没采集过，要么结果被清空了。可以切到「本次采集」看这一次的结果。")));
      renderUrlBox();
      return;
    }

    const heads = ["序号", "昵称", "账号", "作品 ID", "采集时间", "发布时间", "链接"];
    const trs = rows.map((r, i) => {
      const link = el("a", null, "打开");
      link.href = r.video_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.style.color = "var(--accent)";
      const acc = S_.accounts.find((a) => a.account_id === r.account_id);
      return [
        String(i + 1),
        acc?.display_name || "—",
        H(`<span class="mono">@${esc(r.username)}</span>`),
        H(`<span class="mono">${esc(r.video_id)}</span>`),
        H(`<span class="time">${esc(shortTime(r.collect_time))}</span>`),
        H(`<span class="muted">${esc(r.publish_time ? shortTime(r.publish_time) : "—")}</span>`),
        link,
      ];
    });
    // 整行可点 = 复制该条链接（沿用旧桌面版；点「打开」链接本身不受影响）
    fillTable(wrap, heads, trs, {
      onRowClick: (tr, i) => {
        const r = rows[i];
        if (r) copyText(r.video_url, "已复制该条链接");
      },
    });
    renderUrlBox();
  }

  /** 把链接文本铺进文本区（纯链接 / 按账号分组 两种模式），并刷新「共 N 条」。 */
  function renderUrlBox() {
    const box = $("rs-urls");
    if (!box) return;
    const text = S_.results.mode === "grouped" ? S_.results.grouped : S_.results.urls;
    box.textContent = text || "暂无数据";
    const cnt = $("rs-url-count");
    if (cnt) {
      // 只数真正的链接行（分组模式下还有 @账号 这样的标题行）
      const n = String(text || "").split("\n").filter((x) => /^https?:\/\//.test(x.trim())).length;
      cnt.textContent = `共 ${n} 条链接`;
    }
  }

  /** 「复制选中」：有选区就复制选区，没有就退化成复制全部（原桌面版同语义）。
   *
   * 只认「选区落在该链接文本区里」的情况 —— 页面上别处（比如表格）选中了
   * 一段文字时，点这个按钮不该把它复制走。
   */
  function copySelectedFrom(box) {
    let picked = "";
    try {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && box && sel.anchorNode && box.contains(sel.anchorNode)) {
        picked = String(sel);
      }
    } catch { /* 选区不可用时一律按「复制全部」处理 */ }
    if (picked.trim()) {
      const n = picked.split("\n").filter((x) => x.trim()).length;
      copyText(picked, `已复制选中 ${n} 行`);
      return;
    }
    copyAllResults();
  }

  /** 全选链接文本区（对标原桌面版 `LinkTextPanel.select_all`）。 */
  function selectAllIn(box) {
    if (!box) return;
    const range = document.createRange();
    range.selectNodeContents(box);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    box.focus();
  }

  /** 给一个链接文本区挂上「Ctrl+A / Ctrl+C / 右键菜单」三件套。
   *  原桌面版 LinkTextPanel 用的是 tk.Text + tk.Menu，Web 里行为等价。 */
  function attachUrlBox(box, allGetter) {
    if (!box || box._urlBound) return;
    box._urlBound = true;
    box.addEventListener("keydown", (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const k = e.key.toLowerCase();
      if (k === "a") { e.preventDefault(); selectAllIn(box); }
      else if (k === "c") { e.preventDefault(); copySelectedFrom(box); }
    });
    box.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const sel = () => copySelectedFrom(box);
      const all = () => copyText(allGetter(), "链接已复制");
      const menu = openCtxMenu(e.clientX, e.clientY, [
        ["复制选中", sel], ["复制全部", all], ["全选", () => selectAllIn(box)],
      ]);
      return menu;
    });
  }

  /** 通用右键菜单（等价 tk.Menu）。 */
  let _ctxMenu = null;
  function openCtxMenu(x, y, items) {
    if (_ctxMenu) _ctxMenu.remove();
    const menu = el("div", "ctx-menu");
    items.forEach(([label, fn]) => {
      const b = el("button", null, label);
      b.addEventListener("click", () => {
        menu.remove();
        _ctxMenu = null;
        fn();
      });
      menu.appendChild(b);
    });
    menu.style.left = Math.min(x, window.innerWidth - 150) + "px";
    menu.style.top = Math.min(y, window.innerHeight - 130) + "px";
    document.body.appendChild(menu);
    _ctxMenu = menu;
    const away = (ev) => {
      if (!menu.contains(ev.target)) {
        menu.remove();
        _ctxMenu = null;
        document.removeEventListener("mousedown", away, true);
      }
    };
    setTimeout(() => document.addEventListener("mousedown", away, true), 0);
    return menu;
  }

  async function doExport(fmt, mode) {
    try {
      const body = { format: fmt, mode: mode || S_.results.mode };
      if (S_.results.scope === "all") body.task_id = "";
      if (S_.results.accountId) body.account_id = S_.results.accountId;
      const r = await api(`${API}/export`, { method: "POST", body });
      toast(`已导出 ${r.count} 条 → ${r.name}`);
      window.open(`${API}/export/download?path=${encodeURIComponent(r.path)}`, "_blank");
    } catch (e) { toast("导出失败：" + e.message); }
  }

  // ==========================================================================
  //  视图四：每日归档
  // ==========================================================================

  /** YYYYMMDD → YYYY-MM-DD（只用于展示）。 */
  const dayLabel = (day) => `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6)}`;

  /** 归档正文里的「真链接行」条数（跳过空行/备注行）。 */
  const countLinks = (text) => String(text || "")
    .split(/\r?\n/).filter((x) => /^https?:\/\//i.test(x.trim())).length;

  async function loadDaily() {
    const wrap = $("dl-list");
    wrap.innerHTML = "";
    wrap.appendChild(loadingState("加载归档…"));
    try {
      const d = await api(`${API}/daily-links`);
      S_.daily.files = d.files || [];
      $("dl-count").textContent = S_.daily.files.length
        ? `${S_.daily.files.length} 个归档文件` : "";
      wrap.innerHTML = "";
      if (!S_.daily.files.length) {
        S_.daily.current = "";
        S_.daily.text = "";
        S_.daily.day = "";
        $("dl-title").textContent = "选择一个日期";
        $("dl-content").textContent = "—";
        $("dl-link-count").textContent = "";
        wrap.appendChild(emptyState("▤", "暂无归档",
          "每完成一次采集，会把当日链接写入 links_log/YYYYMMDD.txt。"));
        return;
      }
      S_.daily.files.forEach((f) => {
        const row = el("div", "acct-row");
        row.style.padding = "9px 10px";
        row.dataset.day = f.day;
        row.innerHTML = `<span class="acct-name"><span class="nick">${esc(dayLabel(f.day))}</span>` +
          `<span class="user">${(f.size / 1024).toFixed(1)} KB</span></span>`;
        if (S_.daily.current === f.day) row.classList.add("picked");
        row.addEventListener("click", () => openDaily(f.day));
        wrap.appendChild(row);
      });
      // ⚠️ 关键：以前这里只在「首次进入」才打开正文（`if (!S_.daily.current)`），
      //    而本视图又不走 alwaysReload —— 结果看完一次后，无论采集多少次、
      //    归档文件被替换成多少条，页面都还显示上次那份旧内容，
      //    点「复制」自然拿不到新采到的链接（用户反馈的「复制不全」根因）。
      //    现在：每次进入都重拉列表 + 重拉正文。
      const days = S_.daily.files.map((f) => f.day);
      const keep = days.includes(S_.daily.current) ? S_.daily.current : days[0];
      await openDaily(keep);
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "加载失败", e.message));
    }
  }

  async function openDaily(day) {
    if (!day) return;
    S_.daily.current = day;
    document.querySelectorAll("#dl-list .acct-row").forEach((r) => {
      r.classList.toggle("picked", r.dataset.day === day);
    });
    $("dl-title").textContent = `${dayLabel(day)} 归档内容`;
    const box = $("dl-content");
    box.textContent = "加载中…";
    $("dl-link-count").textContent = "";
    try {
      const d = await api(`${API}/daily-links/${day}`);
      // 正文只认服务端返回的完整文本 —— 复制 / 保存都从这里取，
      // 与展示区的高度、滚动位置完全无关（避免「只复制到看得见的部分」）。
      S_.daily.text = d.content || "";
      S_.daily.day = day;
      box.textContent = S_.daily.text || "（空文件）";
      box.dataset.day = day;
      const n = countLinks(S_.daily.text);
      $("dl-link-count").textContent = n ? `共 ${n} 条链接` : "（无链接）";
    } catch (e) {
      S_.daily.text = "";
      S_.daily.day = day;
      box.textContent = "加载失败：" + e.message;
      $("dl-link-count").textContent = "";
    }
  }

  // ==========================================================================
  //  视图五：好友 / 群聊采集
  // ==========================================================================

  /** 账号下拉显示名（原程序同款）：有地区则「越南 · @user」，否则「@user」。 */
  function accountLabel(a) {
    const region = String((a && a.region) || "").trim();
    const base = `@${(a && a.username) || ""}`;
    return region ? `${region} · ${base}` : base;
  }

  function fillAccountSelects() {
    const sel = $("ch-account");
    if (sel) {
      const cur = sel.value;
      sel.innerHTML = "";
      S_.accounts.filter((a) => a.enabled).forEach((a) => {
        const o = el("option", null, accountLabel(a));
        o.value = a.account_id;
        sel.appendChild(o);
      });
      if (cur) sel.value = cur;
    }
    const rsel = $("rs-account");
    if (rsel) {
      const cur = rsel.value;
      rsel.innerHTML = "";
      const all = el("option", null, "全部账号");
      all.value = "";
      rsel.appendChild(all);
      S_.accounts.forEach((a) => {
        const o = el("option", null, accountLabel(a));
        o.value = a.account_id;
        rsel.appendChild(o);
      });
      rsel.value = cur || S_.results.accountId;
    }
  }

  // -------------------------------------------------- 目标身份 / 链接回填

  /** 两个候选（或目标）是否指向同一对象：优先用最稳定的标识比对。 */
  function sameTarget(a, b) {
    if (!a || !b) return false;
    if (a.conversation_id && b.conversation_id) {
      return a.conversation_id === b.conversation_id;
    }
    if (a.sec_uid && b.sec_uid) return a.sec_uid === b.sec_uid;
    return !!a.uid && !!b.uid && String(a.uid) === String(b.uid);
  }

  /** ChatTarget → query string（只带非空字段，后端按稳定标识定位存档）。 */
  function chatTargetQuery(t) {
    const p = new URLSearchParams();
    ["chat_type", "name", "handle", "uid", "sec_uid", "conversation_id"]
      .forEach((k) => { if (t && t[k]) p.set(k, String(t[k])); });
    return p.toString();
  }

  /** 渲染「采集到的链接」文本区（原版：一行一条，可直接粘到去水印）。 */
  function renderChatLinks(links) {
    const arr = (links || []).map((x) => String(x || "").trim()).filter(Boolean);
    $("ch-links").textContent = arr.join("\n");
    const c = $("ch-result-count");
    if (c) c.textContent = arr.length ? `${arr.length} 条` : "";
  }

  /** 会话行的稳定 key（优先 conversation_id，退化到 sec_uid / uid / 名称）。 */
  function convKey(c) {
    if (!c) return "";
    return String(c.conversation_id || c.sec_uid || c.uid || c.stable_key
      || `${c.chat_type}:${c.name || ""}`);
  }

  /** 在左列会话列表里高亮已选目标，并滚动到可视区。 */
  function highlightConv(t) {
    const key = convKey(t);
    let hit = null;
    document.querySelectorAll("#ch-convs .conv-row").forEach((r) => {
      const on = !!key && r.dataset.key === key;
      r.classList.toggle("picked", on);
      if (on) hit = r;
    });
    if (hit) hit.scrollIntoView({ block: "nearest" });
  }

  function setChatStatus(text) {
    const n = $("ch-status");
    if (n) n.textContent = text;
  }

  /** 选定目标：写状态 + 更新右列标签 + 高亮左列行 + 回填该目标**已存档**的链接。 */
  async function applyChatTarget(t) {
    if (!t) return;
    S_.chat.target = t;
    const label = t.name || t.detail_label || t.type_label || t.chat_type || "（无名）";
    const detail = t.detail_label ? `  ${t.detail_label}` : "";
    const box = $("ch-target-label");
    box.textContent = `✓ 已选择：${label}${detail}`;
    box.classList.add("ok");
    $("ch-start").disabled = false;
    highlightConv(t);
    setChatStatus(`已选定目标：${label}（${t.type_label || t.chat_type || ""}），`
      + `点「开始采集」或双击该行直接采集`);
    try {
      const d = await api(`${API}/chat/links?${chatTargetQuery(t)}`);
      renderChatLinks(d.links || []);
    } catch { /* 回填失败不影响目标选择本身 */ }
  }

  /** 渲染「最近使用」胶囊；点击即切换到该目标（原版：无记录时显示「暂无」）。 */
  async function loadChatRecent() {
    const box = $("ch-recent");
    if (!box) return;
    const empty = () => { box.innerHTML = ""; box.appendChild(el("span", "muted", "暂无")); };
    try {
      const d = await api(`${API}/chat/recent?limit=5`);
      const targets = d.targets || [];
      S_.chat.recent = targets;
      box.innerHTML = "";
      if (!targets.length) { empty(); return; }
      targets.forEach((t) => {
        const txt = `${t.type_label || ""}｜${t.name || t.detail_label || "（无名）"}`;
        const b = el("button", "capsule", txt);
        b.title = [t.type_label, t.name, t.handle].filter(Boolean).join(" · ");
        if (sameTarget(t, S_.chat.target)) b.classList.add("active");
        b.addEventListener("click", () => {
          box.querySelectorAll(".capsule").forEach((n) => n.classList.remove("active"));
          b.classList.add("active");
          applyChatTarget(t);
        });
        box.appendChild(b);
      });
    } catch { empty(); }
  }

  /** 本地实时过滤（不发网络请求）：关键词 + 类型分段。 */
  function filteredConvs() {
    const q = String(S_.chat.filter || "").trim().toLowerCase().replace(/^@/, "");
    const t = S_.chat.type || "";
    return (S_.chat.convs || []).filter((c) => {
      if (t && c.chat_type !== t) return false;
      if (!q) return true;
      const hay = [c.name, c.handle, c.subtitle, c.display]
        .map((x) => String(x || "").toLowerCase()).join(" ");
      return hay.includes(q);
    });
  }

  /** 渲染左列会话列表：单选点 + 名称 + 类型徽标 + 摘要；整行可点、双击直接采集。 */
  function renderConvs() {
    const box = $("ch-convs");
    if (!box) return;
    const list = filteredConvs();
    box.innerHTML = "";
    if (!list.length) {
      const tip = (S_.chat.convs || []).length
        ? "无匹配会话\n（清空过滤条件或切换类型后重试）"
        : "暂无会话数据\n点击右上角「刷新会话列表」加载";
      const d = el("div", "empty");
      d.style.cssText = "padding:40px 16px;text-align:center;white-space:pre-line;color:var(--text-3)";
      d.textContent = tip;
      box.appendChild(d);
      return;
    }
    list.forEach((c) => {
      const row = el("div", "conv-row");
      row.dataset.key = convKey(c);
      if (S_.chat.target && sameTarget(c, S_.chat.target)) row.classList.add("picked");

      row.appendChild(el("span", "conv-radio"));
      row.appendChild(el("span", "conv-name", c.name || "（无名）"));

      const isGroup = c.chat_type === "group";
      row.appendChild(el("span", `conv-badge ${isGroup ? "g" : "f"}`, isGroup ? "群" : "友"));

      // 好友显示 @用户名；群聊显示消息摘要（原版同款规则）
      const raw = String(c.handle || "").replace(/^@/, "");
      const sub = isGroup ? String(c.subtitle || "").slice(0, 40)
        : (raw ? `@${raw}` : String(c.subtitle || "").slice(0, 40));
      if (sub) row.appendChild(el("span", "conv-sub", sub));

      row.addEventListener("click", () => applyChatTarget(c));
      row.addEventListener("dblclick", () => { applyChatTarget(c); chatCollect(); });
      box.appendChild(row);
    });
  }

  /** 一次拉取该账号消息页的**完整会话列表**（原版：登录成功后自动加载）。 */
  async function loadConvs() {
    const accountId = $("ch-account").value;
    if (!accountId) { toast("请先在「账号管理」添加账号"); return; }
    if (S_.chat.loading) return;
    S_.chat.loading = true;
    const btn = $("ch-conv");
    btn.disabled = true;
    setChatStatus("正在从消息页拉取会话列表…（约需 15~40 秒，请勿重复点击）");
    clearLog("ch-log", "正在加载会话列表，请稍候（约 15~40 秒）…");
    setBar("ch-bar", 0);
    const finish = () => { S_.chat.loading = false; btn.disabled = false; };
    try {
      const r = await api(`${API}/chat/search`, {
        method: "POST",
        body: { account_id: accountId, mode: "all" },
      });
      watchJob(r.job_id, {
        bar: "ch-bar", log: "ch-log", buttons: [], label: "会话列表",
        onItems: (items) => { S_.chat.convs = items || []; renderConvs(); },
        onDone: (kind) => {
          finish();
          const convs = S_.chat.convs || [];
          const nf = convs.filter((c) => c.chat_type === "friend").length;
          const ng = convs.length - nf;
          if (kind === "job_done") {
            $("ch-conv-count").textContent =
              `好友 ${nf} · 群聊 ${ng} · 共 ${convs.length} 条，点选即选定`;
            setChatStatus(`已加载会话列表：好友 ${nf} · 群聊 ${ng}`);
            if (!convs.length) toast("没拉到会话，请确认该账号已登录且消息页可访问");
          } else {
            $("ch-conv-count").textContent = "尚未加载，点右上角「刷新会话列表」";
            setChatStatus("会话列表获取失败：请先点「登录账号」完成登录后重试");
            writeLog("ch-log", "会话列表获取失败：请先在浏览器里完成登录");
          }
        },
      });
    } catch (e) {
      finish();
      setChatStatus("会话列表获取失败：" + e.message);
      writeLog("ch-log", "会话列表获取失败：" + e.message);
      toast("会话列表获取失败：" + e.message);
    }
  }

  // （原「搜索目标 → 候选表格」已被左列会话列表取代，见 renderConvs / loadConvs）


  async function chatCollect() {
    if (!S_.chat.target) { toast("请先选择目标"); return; }
    const accountId = $("ch-account").value;
    if (!accountId) { toast("请先选择采集账号"); return; }
    const count = Math.max(1, parseInt($("ch-count").value, 10) || 22);
    const showBrowser = !!($("ch-show-browser") && $("ch-show-browser").checked);
    clearLog("ch-log", showBrowser
      ? "正在采集…（已开启浏览器窗口，可直接观察进度）"
      : "正在采集…");
    setBar("ch-bar", 0);
    $("ch-start").disabled = true;
    S_.chat.jobId = null;
    if ($("ch-stop")) $("ch-stop").disabled = false;   // 采集期间可随时停止
    try {
      const r = await api(`${API}/chat/collect`, {
        method: "POST",
        body: {
          account_id: accountId, target: S_.chat.target, count,
          show_browser: showBrowser,
        },
      });
      S_.chat.jobId = r.job_id;
      watchJob(r.job_id, {
        bar: "ch-bar", log: "ch-log", buttons: [],
        label: "聊天采集",
        onDone: async (kind) => {
          $("ch-start").disabled = false;
          S_.chat.jobId = null;
          if ($("ch-stop")) $("ch-stop").disabled = true;
          let links = null;
          try {
            const j = await api(`/api/jobs/${r.job_id}`);
            links = (j.values && j.values.links) || [];
          } catch { /* 详情拿不到就回读存档 */ }
          if (links === null) {
            try {
              const d = await api(`${API}/chat/links?${chatTargetQuery(S_.chat.target)}`);
              links = d.links || [];
            } catch { links = []; }
          }
          renderChatLinks(links);
          loadChatRecent();          // 采过的目标会冒到「最近使用」最前
          if (kind === "job_done") {
            notifySystem("采集完成", `成功拿到 ${links.length} 条链接`,
                         links.length ? "ok" : "warn");
          }
          else if (links.length) toast(`已拿到 ${links.length} 条链接`);
        },
      });
    } catch (e) {
      $("ch-start").disabled = false;
      S_.chat.jobId = null;
      if ($("ch-stop")) $("ch-stop").disabled = true;
      toast("采集失败：" + e.message);
      writeLog("ch-log", "采集失败：" + e.message);
    }
  }

  /** 弹浏览器窗口等人工登录（消息页），登录态落 data/sessions/ 长期复用。 */
  async function chatLogin() {
    const accountId = $("ch-account").value;
    if (!accountId) { toast("请先选择采集账号"); return; }
    const btn = $("ch-login");
    const tip = $("ch-login-tip");
    btn.disabled = true;
    if (tip) tip.textContent = "正在打开浏览器…请在窗口中完成登录";
    clearLog("ch-log",
      "正在打开浏览器窗口，请在里面登录 TikTok 消息页。\n" +
      "登录成功后本任务会自动结束（最多等 5 分钟），之后长期免登录。");
    setBar("ch-bar", 0);
    try {
      const r = await api(`${API}/chat/login`, {
        method: "POST",
        body: { account_id: accountId, timeout: 300 },
      });
      watchJob(r.job_id, {
        bar: "ch-bar", log: "ch-log", buttons: [],
        label: "登录",
        onDone: (kind) => {
          btn.disabled = false;
          if (kind === "job_done") {
            if (tip) tip.textContent = "✓ 登录成功，已保存登录态";
            toast("登录成功，正在加载会话列表…");
            loadConvs();            // 原版行为：登录成功后自动拉会话列表
          } else {
            if (tip) tip.textContent = "登录未完成（超时或取消了登录）";
            toast("登录未完成，请重试");
          }
        },
      });
    } catch (e) {
      btn.disabled = false;
      if (tip) tip.textContent = "打开登录失败：" + e.message;
      toast("打开登录失败：" + e.message);
      writeLog("ch-log", "登录失败：" + e.message);
    }
  }

  function chatExport() {
    const text = $("ch-links").textContent || "";
    if (!text.trim()) { toast("没有可导出的链接"); return; }
    const t = S_.chat.target || {};
    const safe = String(t.name || t.detail_label || t.chat_type || "chat")
      .replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 40);
    saveText(`${todayStr(0)}_${safe}_链接.txt`, text);
    toast("已导出 TXT");
  }

  function chatClear() {
    const t = S_.chat.target;
    if (!t) { toast("请先选择目标"); return; }
    const label = t.name || t.detail_label || t.chat_type || "该目标";
    inlineConfirm($("ch-clear"), async () => {
      try {
        const d = await api(`${API}/chat/links?${chatTargetQuery(t)}`, { method: "DELETE" });
        renderChatLinks([]);
        toast(`已清空「${label}」的 ${d.cleared || 0} 条链接`);
        writeLog("ch-log", `已清空「${label}」存档链接 ${d.cleared || 0} 条`);
      } catch (e) { toast("清空失败：" + e.message); }
    }, `清空「${label}」已存档链接？`);
  }

  async function loadChat() {
    try {
      if (!S_.accounts.length) await fetchAccounts({ render: false });
      fillAccountSelects();
    } catch (e) { writeLog("ch-log", "加载账号失败：" + e.message); }

    // 采集数量沿用上次保存的值（原版 config: chat_last_count）
    try {
      const s = await api(`${API}/settings`);
      const n = parseInt((s.values || {}).chat_last_count, 10);
      if (n >= 1 && $("ch-count")) $("ch-count").value = Math.min(200, n);
    } catch { /* 读不到就用默认值 */ }

    await loadChatRecent();

    // 会话列表不自动拉取（每次换账号/换时间结果都不同，且耗时 15~40 秒）——
    // 与原版一致：由「刷新会话列表」按钮或登录成功后触发。
    if (!(S_.chat.convs || []).length) {
      const cc = $("ch-conv-count");
      if (cc) cc.textContent = "尚未加载，点右上角「刷新会话列表」";
      renderConvs();
      clearLog("ch-log",
        "① 选采集账号 → ② 点「登录账号」完成一次人工登录（之后免登录）\n" +
        "→ ③ 点「刷新会话列表」加载好友 / 群聊 → ④ 在左列点选目标 → ⑤ 开始采集。");
    }
  }

  // ==========================================================================
  //  视图六：任务中心
  // ==========================================================================

  const JOB_KIND_CN = {
    collect: "采集", chat_collect: "好友群聊", tiktok_download: "去水印下载",
    tiktok_parse: "链接解析", hd_image: "高清·图片", hd_video: "高清·视频",
    hd_mixed: "高清·混合", model_download: "模型下载", tool: "工具",
  };

  async function loadJobs() {
    const wrap = $("jb-table");
    try {
      const d = await api("/api/jobs");
      const jobs = (d.jobs || []).slice().reverse();
      const filter = $("jb-filter").value;
      const list = jobs.filter((j) => {
        if (!filter) return true;
        if (filter === "running") return ["running", "paused", "pending"].includes(j.state);
        if (filter === "done") return j.state === "done" || j.state === "completed";
        if (filter === "failed") return j.state === "failed" || j.state === "cancelled";
        return true;
      });
      $("jb-count").textContent = `${list.length} / ${jobs.length} 个任务`;

      if (!list.length) {
        wrap.innerHTML = "";
        wrap.appendChild(emptyState("◷", "暂无任务", "采集 / 下载 / 高清任务都会出现在这里。"));
        return;
      }
      const rows = list.map((j) => {
        const detail = el("button", "btn btn-ghost btn-sm", "详情");
        detail.addEventListener("click", (e) => { e.stopPropagation(); showJobDetail(j.id); });
        const canStop = ["running", "paused", "pending"].includes(j.state);
        const stop = el("button", "btn btn-soft-danger btn-sm", "停止");
        stop.disabled = !canStop;
        stop.addEventListener("click", (e) => { e.stopPropagation(); jobAction(j.id, "cancel"); });
        return [
          H(`<span class="mono">${esc(j.id.slice(0, 8))}</span>`),
          JOB_KIND_CN[j.kind] || j.kind,
          esc(j.label || "—"),
          H(statusTag(j.state)),
          `${Math.round((j.frac || 0) * 100)}%`,
          H(`<span class="time">${esc(shortTime(j.created_at))}</span>`),
          detail,
          stop,
        ];
      });
      fillTable(wrap, ["任务", "类型", "说明", "状态", "进度", "创建时间", "查看", "操作"], rows,
        { onRowClick: (tr, i) => showJobDetail(list[i].id) });
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "加载失败", e.message));
    }
  }

  async function showJobDetail(jobId) {
    const card = $("jb-detail-card");
    const kv = $("jb-detail");
    const log = $("jb-log");
    card.style.display = "";
    $("jb-detail-title").textContent = jobId.slice(0, 8);
    kv.innerHTML = "";
    log.textContent = "加载中…";
    try {
      const j = await api(`/api/jobs/${jobId}`);
      const v = j.values || {};
      const items = [
        ["任务 ID", j.id], ["类型", JOB_KIND_CN[j.kind] || j.kind],
        ["说明", j.label], ["状态", j.state],
        ["进度", `${Math.round((j.frac || 0) * 100)}%`],
        ["当前阶段", j.stage_label || j.stage || "—"],
        ["创建时间", shortTime(j.created_at)],
        ["结束时间", j.finished_at ? shortTime(j.finished_at) : "—"],
        ["耗时", j.elapsed ? j.elapsed.toFixed(1) + " 秒" : "—"],
        ["结果提示", j.message || "—"],
        ["错误", j.error || "—"],
      ];
      // 业务自有的键（task_id / 成功数 / 链接数 …）一并列出
      Object.entries(v).forEach(([k, val]) => {
        if (["links", "url_text", "accounts"].includes(k)) return;   // 太长的单独展示
        items.push([k, Array.isArray(val) ? `[${val.length} 项]` : val]);
      });
      items.forEach(([k, val]) => {
        const row = el("div", "kv-row");
        row.appendChild(el("div", "kv-k", k));
        row.appendChild(el("div", "kv-v",
          val == null || val === "" ? "—" : String(val)));
        kv.appendChild(row);
      });

      // 采集类任务：把采集日志拉出来（/api/jobs 不带日志，日志在采集库里）
      const taskId = v.task_id || "";
      if (j.kind === "collect" && taskId) {
        try {
          const ld = await api(`${API}/logs?task_id=${encodeURIComponent(taskId)}&limit=400`);
          const lines = (ld.logs || []).reverse();
          log.textContent = lines.length
            ? lines.map((x) => `[${shortTime(x.created_at)}] ${x.level.toUpperCase()} ${x.message}`).join("\n")
            : "（该任务没有采集日志）";
          return;
        } catch { /* 落到下面的兜底 */ }
      }
      if (Array.isArray(v.links) && v.links.length) {
        log.textContent = v.links.join("\n");
        return;
      }
      log.textContent = "（该任务没有留存日志）";
    } catch (e) { log.textContent = "加载失败：" + e.message; }
  }

  async function refreshJobsBadge() {
    const badge = $("jobs-badge");
    if (!badge) return;
    try {
      const { jobs } = await api("/api/jobs");
      const n = (jobs || []).filter((j) =>
        ["running", "paused", "pending"].includes(j.state)).length;
      badge.textContent = String(n);
      badge.classList.toggle("hot", n > 0);
    } catch { /* 忽略 */ }
  }

  // ==========================================================================
  //  视图七：历史记录（任务级台账）
  // ==========================================================================

  /** 简易防抖：搜索框连续输入只触发一次查询。 */
  function debounce(fn, ms = 250) {
    let t = null;
    return (...args) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => { t = null; fn(...args); }, ms);
    };
  }

  async function loadHistory() {
    const wrap = $("hi-table");
    if (!wrap) return;
    const limit = parseInt($("hi-limit").value, 10) || 100;
    const status = $("hi-status").value || "";
    const query = $("hi-query").value.trim().toLowerCase();
    $("hi-count").textContent = "加载中…";
    try {
      const d = await api(`${API}/tasks?limit=${limit}`);
      const all = d.tasks || [];
      let rows = all;
      if (status) rows = rows.filter((r) => r.status === status);
      if (query) {
        rows = rows.filter((r) =>
          [r.task_id, r.target_date, r.status, r.status_label]
            .join(" ").toLowerCase().includes(query));
      }
      const total = d.grand_total || all.length;
      $("hi-count").textContent = rows.length === all.length
        ? `共 ${total} 个任务，已载入最近 ${all.length} 个`
        : `筛选命中 ${rows.length} 条（最近 ${all.length} 个里）`;
      wrap.innerHTML = "";
      if (!rows.length) {
        wrap.appendChild(emptyState("🗂", "选择任务查看详情",
          all.length ? "换一个状态或关键词再试。" : "还没有任何采集任务记录。"));
        return;
      }
      fillTable(wrap,
        ["任务", "记录日期", "状态", "账号", "成功 / 失败", "实际 / 目标", "创建时间"],
        rows.map((r) => [
          H(`<span class="mono">${esc(r.task_id)}</span>`),
          esc(r.target_date || "—"),
          H(statusTag(r.status)),
          String(r.account_count || 0),
          `${r.success_count || 0} / ${r.fail_count || 0}`,
          `${r.actual_total || 0} / ${r.target_total || 0}`,
          H(`<span class="time">${esc(shortTime(r.created_at))}</span>`),
        ]),
        { onRowClick: (tr, i) => showTaskDetail("hi", rows[i].task_id) });
    } catch (e) {
      $("hi-count").textContent = "";
      wrap.innerHTML = "";
      wrap.appendChild(emptyState("!", "加载失败", e.message));
    }
  }

  function bindHistoryOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindHistoryOnce._done) return;
    bindHistoryOnce._done = true;
    on("hi-refresh", "click", loadHistory);
    on("hi-status", "change", loadHistory);
    on("hi-limit", "change", loadHistory);
    on("hi-query", "input", debounce(loadHistory, 280));
    bindTaskDetailOnce("hi");
  }

  // ==========================================================================
  //  视图八：运行日志
  //  后端只按「白名单 key」取文件（见 server/api.py::_resolve_log），
  //  前端拿到的 path 仅用于展示，不作为参数回传。
  // ==========================================================================

  const LV_CLASS = {
    ERROR: "lv-error", CRITICAL: "lv-error", FATAL: "lv-error",
    WARNING: "lv-warning", DEBUG: "lv-debug",
    INFO: "lv-info", OTHER: "lv-other",
  };

  function fillLogFiles(files, current) {
    const sel = $("lg-file");
    if (!sel) return;
    const keep = current || sel.value;
    sel.innerHTML = "";
    if (!files.length) {
      const o = el("option", null, "（还没有日志文件）");
      o.value = "";
      sel.appendChild(o);
      return;
    }
    files.forEach((f) => {
      const o = el("option", null, `${f.name} · ${fmtSize(f.size)} · ${f.mtime}`);
      o.value = f.key;
      sel.appendChild(o);
    });
    if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  }

  async function loadLogs() {
    const view = $("lg-view");
    if (!view) return;
    const params = new URLSearchParams();
    const file = $("lg-file").value;
    if (file) params.set("file", file);
    params.set("lines", $("lg-lines").value || "500");
    if ($("lg-level").value) params.set("level", $("lg-level").value);
    if ($("lg-query").value.trim()) params.set("query", $("lg-query").value.trim());
    $("lg-stat").textContent = "加载中…";
    try {
      const d = await api(`/api/system/logs?${params.toString()}`);
      fillLogFiles(d.files || [], d.file || "");
      const rows = d.lines || [];
      view.innerHTML = "";
      if (!rows.length) {
        view.textContent = d.message || "（没有匹配的日志行）";
        $("lg-stat").textContent = "";
        return;
      }
      const frag = document.createDocumentFragment();
      rows.forEach((r) => {
        const div = el("div", "log-line " + (LV_CLASS[r.level] || "lv-other"));
        div.textContent = r.text;
        frag.appendChild(div);
      });
      view.appendChild(frag);
      view.scrollTop = view.scrollHeight;

      const counts = {};
      rows.forEach((r) => { counts[r.level] = (counts[r.level] || 0) + 1; });
      const brief = ["ERROR", "WARNING", "INFO", "DEBUG"]
        .filter((k) => counts[k]).map((k) => `${k} ${counts[k]}`).join(" · ");
      $("lg-stat").textContent =
        `显示 ${rows.length} / 读取 ${d.read || 0} 行` +
        (brief ? ` · ${brief}` : "") + (d.truncated ? " · 文件较大，只取尾部" : "");
    } catch (e) {
      view.textContent = "加载失败：" + e.message;
      $("lg-stat").textContent = "";
    }
  }

  function bindLogsOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindLogsOnce._done) return;
    bindLogsOnce._done = true;
    on("lg-refresh", "click", loadLogs);
    on("lg-file", "change", loadLogs);
    on("lg-level", "change", loadLogs);
    on("lg-lines", "change", loadLogs);
    on("lg-query", "input", debounce(loadLogs, 300));
    on("lg-wrap", "change", () => {
      const view = $("lg-view");
      if (view) view.classList.toggle("wrap", $("lg-wrap").checked);
    });
    on("lg-open", "click", async () => {
      try {
        const r = await api("/api/system/open-log-dir", { method: "POST" });
        toast("已打开日志目录：" + (r.path || ""));
      } catch (e) { toast("打开目录失败：" + e.message); }
    });
    on("lg-copy", "click", () => copyText($("lg-view").innerText, "日志已复制"));
    on("lg-export", "click", () => {
      const text = $("lg-view").innerText || "";
      if (!text.trim()) { toast("没有可导出的日志"); return; }
      saveText(`${todayStr(0)}_日志.txt`, text);
      toast("已导出 TXT");
    });
  }

  // ==========================================================================
  //  设置页 · 采集分区
  // ==========================================================================

  const CSET = {
    count: "cset-count", headless: "cset-headless", notify: "cset-notify",
    // 通知卡片外观（2026-10-10 新增；2026-10-11 起通道固定为界面内卡片，
    // 只剩尺寸可调）
    notifyWidth: "cset-notify-width", notifyScale: "cset-notify-scale",
    // 完成提醒停留秒数（0 = 不自动关闭；窗口最小化时倒计时挂起）
    notifyDuration: "cset-notify-duration",
    timeout: "cset-timeout", retry: "cset-retry", interval: "cset-interval",
    proxyOn: "cset-proxy-on", proxyType: "cset-proxy-type",
    proxyHost: "cset-proxy-host", proxyPort: "cset-proxy-port",
    logLevel: "cset-loglevel", tz: "cset-tz",
    chatCount: "cset-chat-count", histKeep: "cset-histkeep",
    logKeep: "cset-logkeep", autoUpdate: "cset-autoupdate",
    // 原桌面版「数据 / 日志 / 更新」分组里可编辑的三项
    exportDir: "cset-export-dir", logDir: "cset-log-dir",
    repo: "cset-repo", updateUrl: "cset-update-url",
  };

  async function loadCollectorSettings() {
    try {
      const { values } = await api(`${API}/settings`);
      const setV = (id, v) => { const n = $(id); if (n) n.value = v == null ? "" : v; };
      const setC = (id, v) => { const n = $(id); if (n) n.checked = !!v; };
      setV(CSET.count, values.default_collect_count);
      setC(CSET.headless, values.browser_headless);
      setC(CSET.notify, values.notify_sound);
      setV(CSET.notifyWidth, values.notify_card_width);
      setV(CSET.notifyScale, values.notify_card_scale);
      // 下拉框：value 必须是字符串；老配置缺这个键时回退默认 9 秒，
      // 否则会跳到第一个选项（3 秒）上。
      const durSel = $(CSET.notifyDuration);
      if (durSel) durSel.value = String(values.notify_duration == null ? 9 : values.notify_duration);
      setV(CSET.timeout, values.page_load_timeout_ms);
      setV(CSET.retry, values.max_retry);
      setV(CSET.interval, values.account_interval_seconds);
      setV(CSET.chatCount, values.chat_last_count);
      setV(CSET.histKeep, values.history_keep_days);
      setV(CSET.logKeep, values.log_keep_days);
      setC(CSET.autoUpdate, values.auto_check_update);
      setC(CSET.proxyOn, values.proxy_enabled);
      setV(CSET.proxyType, values.proxy_type || "http");
      setV(CSET.proxyHost, values.proxy_host);
      setV(CSET.proxyPort, values.proxy_port);
      setV(CSET.logLevel, values.log_level || "INFO");
      setV(CSET.exportDir, values.export_dir);
      setV(CSET.logDir, values.log_dir);
      setV(CSET.repo, values.update_repo);
      setV(CSET.updateUrl, values.update_url);

      // 时区下拉：常见候选项 + 当前值
      const tzSel = $(CSET.tz);
      if (tzSel) {
        const cur = values.timezone || "Asia/Ho_Chi_Minh";
        const opts = ["Asia/Ho_Chi_Minh", "Asia/Yangon", "Asia/Bangkok",
          "Asia/Jakarta", "Asia/Manila", "Asia/Kuala_Lumpur",
          "Asia/Singapore", "Asia/Phnom_Penh", "Asia/Vientiane", "Asia/Shanghai"];
        if (!opts.includes(cur)) opts.unshift(cur);
        tzSel.innerHTML = "";
        opts.forEach((t) => {
          const o = el("option", null, t);
          o.value = t;
          if (t === cur) o.selected = true;
          tzSel.appendChild(o);
        });
      }
    } catch (e) {
      const n = $(CSET.count);
      if (n) n.placeholder = "读取失败：" + e.message;
    }
  }

  async function saveCollectorSettings() {
    const num = (id) => {
      const n = $(id);
      const v = n ? Number(n.value) : NaN;
      return Number.isFinite(v) ? v : undefined;
    };
    const body = {
      default_collect_count: num(CSET.count),
      browser_headless: $(CSET.headless).checked,
      notify_sound: $(CSET.notify).checked,
      notify_card_width: num(CSET.notifyWidth),
      notify_card_scale: num(CSET.notifyScale),
      notify_duration: num(CSET.notifyDuration),
      page_load_timeout_ms: num(CSET.timeout),
      max_retry: num(CSET.retry),
      account_interval_seconds: num(CSET.interval),
      proxy_enabled: $(CSET.proxyOn).checked,
      proxy_type: $(CSET.proxyType).value,
      proxy_host: $(CSET.proxyHost).value.trim(),
      proxy_port: $(CSET.proxyPort).value.trim(),
      log_level: $(CSET.logLevel).value,
      timezone: $(CSET.tz).value,
      chat_last_count: num(CSET.chatCount),
      history_keep_days: num(CSET.histKeep),
      log_keep_days: num(CSET.logKeep),
      auto_check_update: $(CSET.autoUpdate).checked,
    };
    // 目录类字段留空 = 沿用当前值（不提交空串，避免日志/导出落到程序根目录）；
    // 想回到出厂值请点「恢复默认」。
    [[CSET.exportDir, "export_dir"], [CSET.logDir, "log_dir"],
     [CSET.repo, "update_repo"], [CSET.updateUrl, "update_url"]].forEach(([id, key]) => {
      const n = $(id);
      if (n && n.value.trim()) body[key] = n.value.trim();
    });
    Object.keys(body).forEach((k) => { if (body[k] === undefined) delete body[k]; });
    try {
      await api(`${API}/settings`, { method: "POST", body });
      $("cset-msg").textContent = "已保存，采集配置即时生效（日志保留天数需重启）";
      toast("采集设置已保存");
      if (S_.meta) S_.meta = null;
      S_.meta = await api(`${API}/meta`).catch(() => S_.meta);
      syncNotifyCfg();
    } catch (e) {
      $("cset-msg").textContent = "保存失败：" + e.message;
      toast("保存失败：" + e.message);
    }
  }

  // ==========================================================================
  //  关于页 · 采集环境
  // ==========================================================================

  async function loadCollectorEnv() {
    const kv = $("about-collect");
    if (!kv) return;
    kv.innerHTML = "";
    try {
      const d = await api(`${API}/env`);
      const rows = [
        ["数据库", d.db_path],
        ["账号 / 作品", `${d.accounts} / ${d.videos}`],
        ["数据目录", d.data_dir],
        ["登录态目录", d.sessions],
        ["导出目录", d.exports_dir],
        ["日志目录", d.logs_dir],
        ["每日归档目录", d.links_log_dir],
        ["备份目录", d.backups_dir],
        ["时区", d.timezone],
        ["无头模式", d.headless ? "开启" : "关闭"],
        ["Playwright", d.playwright ? "已安装" : "缺失"],
        ["浏览器内核", d.chromium ? "可用" : `不可用 ${d.chromium_error || ""}`],
      ];
      rows.forEach(([k, v]) => {
        const row = el("div", "kv-row");
        row.appendChild(el("div", "kv-k", k));
        const val = el("div", "kv-v", v == null ? "—" : String(v));
        if ((k === "Playwright" || k === "浏览器内核") && !d.chromium) val.style.color = "var(--danger-2)";
        row.appendChild(val);
        kv.appendChild(row);
      });
    } catch (e) {
      kv.appendChild(el("div", "kv-v", "读取失败：" + e.message));
    }
  }

  // ==========================================================================
  //  视图注册
  // ==========================================================================

  PAGE_LOADERS.collect = () => { loadCollect(); };
  PAGE_LOADERS.accounts = () => { loadAccounts(); };
  PAGE_LOADERS.chat = () => { loadChat(); };
  PAGE_LOADERS.jobs = () => { loadJobs(); };

  // 仪表盘 / 历史记录 / 运行日志是实时快照：每次进入都要重新拉，不能吃首屏缓存。
  alwaysReload("dashboard", () => { loadDashboard(); bindDashboardOnce(); });
  alwaysReload("history", () => { loadHistory(); bindHistoryOnce(); });
  alwaysReload("logs", () => { loadLogs(); bindLogsOnce(); });
  // 采集结果同理：它是**数据**视图，缓存住就永远看不到新采到的东西 ——
  // 这正是「采集完结果页不刷新、还是空的」的根因。
  alwaysReload("results", () => { loadResults(); bindResultsOnce(); });
  // 每日归档同属**数据**视图：归档文件是「当天最后一次采集整体替换」的，
  // 缓存住就会出现「列表刷新了、正文还是上一份」→ 复制拿不到新链接。
  alwaysReload("daily", () => { loadDaily(); bindDailyOnce(); });

  // 设置 / 关于 两个视图已有旧版加载器，这里追加采集分区（不覆盖）
  const prevSettings = PAGE_LOADERS.settings;
  PAGE_LOADERS.settings = () => {
    if (prevSettings) prevSettings();
    loadCollectorSettings();
    bindCollectorSettingsOnce();
  };
  const prevAbout = PAGE_LOADERS.about;
  PAGE_LOADERS.about = () => {
    if (prevAbout) prevAbout();
    loadCollectorEnv();
    bindAboutOnce();
  };

  // 首屏补触发：app.js 在 collector.js 之前就执行了 activatePage，
  // 那时这些 loader 还没注册，所以默认视图不会被加载。这里补一次。
  (function retriggerActive() {
    const active = document.querySelector(".nav-item.active")?.dataset.page;
    if (active && PAGE_LOADERS[active] && !S.pageLoaded[active]) {
      S.activatePage(active);
    }
  })();

  // ==========================================================================
  //  事件绑定
  // ==========================================================================

  function on(id, ev, fn) {
    const n = $(id);
    if (n) n.addEventListener(ev, fn);
  }

  function bindCollectOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindCollectOnce._done) return;
    bindCollectOnce._done = true;
    on("col-search", "input", (e) => { S_.search = e.target.value; renderCollectAccounts(); });
    on("col-region", "change", async (e) => {
      S_.region = e.target.value;
      // 切换地区 = 自动勾选该地区全部账号（沿用旧版行为）
      try {
        await fetchAccounts({ render: false });
        if (S_.region) {
          S_.accounts.filter((a) => a.enabled).forEach((a) => S_.picked.add(a.account_id));
        }
        renderCollectAccounts();
      } catch (err) { toast("切换地区失败：" + err.message); }
    });
    on("col-sel-all", "click", () => {
      S_.accounts.filter((a) => a.enabled).forEach((a) => S_.picked.add(a.account_id));
      renderCollectAccounts();
    });
    on("col-count", "input", () => { S_.countTouched = true; });
    on("col-today", "click", () => { $("col-date").value = todayStr(); });
    on("col-yesterday", "click", () => { $("col-date").value = todayStr(-1); });

    // 「应用到已选」：把统一数量逐个写进已勾选账号（落库，非本次覆盖）
    on("col-apply-count", "click", async () => {
      const ids = [...S_.picked];
      if (!ids.length) { toast("请先勾选账号"); return; }
      const n = Math.max(1, Math.min(50, parseInt($("col-count").value, 10) || 4));
      let ok = 0;
      for (const id of ids) {
        try {
          await api(`${API}/accounts/${encodeURIComponent(id)}`,
            { method: "PATCH", body: { collect_count: n } });
          const a = S_.accounts.find((x) => x.account_id === id);
          if (a) a.collect_count = n;
          ok += 1;
        } catch { /* 单个失败不打断整体 */ }
      }
      S_.countTouched = false;   // 已落到各账号上，不必再整批覆盖
      renderCollectAccounts();
      toast(`已将「每账号数量 = ${n}」应用到 ${ok} 个账号`);
    });

    // 暂停 / 继续：后端 Job 早已支持，引擎在**账号边界**响应暂停
    const runningCollect = async () => {
      const d = await api("/api/jobs");
      return (d.jobs || []).find((x) => x.kind === "collect"
        && ["running", "pending", "paused"].includes(x.state));
    };
    on("col-pause", "click", async () => {
      try {
        const j = await runningCollect();
        if (!j) { toast("没有正在运行的采集任务"); return; }
        await jobAction(j.id, "pause");
        $("col-pause").disabled = true;
        $("col-resume").disabled = false;
        writeLog("col-log", "已暂停：当前账号采完后停下，已采数据保留");
      } catch (e) { toast("暂停失败：" + e.message); }
    });
    on("col-resume", "click", async () => {
      try {
        const j = await runningCollect();
        if (!j) { toast("没有可继续的采集任务"); return; }
        await jobAction(j.id, "resume");
        $("col-resume").disabled = true;
        $("col-pause").disabled = false;
        writeLog("col-log", "已继续采集");
      } catch (e) { toast("继续失败：" + e.message); }
    });
    on("col-retry", "click", retryFailedAccounts);
    on("col-resume-unfinished", "click", resumeUnfinished);
    on("col-sel-none", "click", () => { S_.picked.clear(); renderCollectAccounts(); });
    on("col-invert", "click", () => {
      S_.accounts.forEach((a) => {
        if (!a.enabled) return;
        if (S_.picked.has(a.account_id)) S_.picked.delete(a.account_id);
        else S_.picked.add(a.account_id);
      });
      renderCollectAccounts();
    });
    on("col-start", "click", startCollect);
    on("col-cancel", "click", () => {
      // 取消当前运行中的采集任务（从任务注册表里找）
      api("/api/jobs").then((d) => {
        const j = (d.jobs || []).find((x) => x.kind === "collect"
          && ["running", "pending", "paused"].includes(x.state));
        if (j) jobAction(j.id, "cancel");
        else toast("没有正在运行的采集任务");
      }).catch(() => toast("取消失败"));
    });
  }

  function bindAccountsOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindAccountsOnce._done) return;
    bindAccountsOnce._done = true;
    on("ac-search", "input", (e) => {
      S_.search = e.target.value;
      renderAccountsTable();
      renderCollectAccounts();
    });
    on("ac-region", "change", async (e) => {
      S_.region = e.target.value;
      try { await fetchAccounts(); } catch (err) { toast("筛选失败：" + err.message); }
    });
    on("ac-add", "click", () => openAccountDialog(null));
    on("ac-quick-add", "click", quickAddAccount);
    on("ac-quick", "keydown", (e) => { if (e.key === "Enter") quickAddAccount(); });
    on("ac-enabled", "change", (e) => {
      S_.enabledFilter = e.target.value;
      renderAccountsTable();
    });

    // 「全选 / 全不选 / 反选」只作用于**当前筛选出来**的账号，
    // 不碰被筛掉的 —— 否则用户会误伤看不见的行。
    on("ac-sel-all", "click", () => {
      visibleAccounts().forEach((a) => S_.picked.add(a.account_id));
      renderAccountsTable();
    });
    on("ac-sel-none", "click", () => {
      visibleAccounts().forEach((a) => S_.picked.delete(a.account_id));
      renderAccountsTable();
    });
    on("ac-invert", "click", () => {
      visibleAccounts().forEach((a) => {
        if (S_.picked.has(a.account_id)) S_.picked.delete(a.account_id);
        else S_.picked.add(a.account_id);
      });
      renderAccountsTable();
    });

    on("ac-enable", "click", () => batchSetEnabled(true));
    on("ac-disable", "click", () => batchSetEnabled(false));
    on("ac-set-region", "click", openRegionDialog);
    on("ac-edit-count", "click", openCountDialog);
    on("ac-refresh-nick", "click", refreshNicknames);
    on("ac-delete", "click", () => {
      const ids = [...S_.picked];
      if (!ids.length) return;
      const btn = $("ac-delete");
      inlineConfirm(btn, async () => {
        let ok = 0, fail = 0;
        for (const id of ids) {
          try {
            await api(`${API}/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
            ok += 1;
          } catch { fail += 1; }
        }
        S_.picked.clear();
        toast(`已删除 ${ok} 个${fail ? `，失败 ${fail}` : ""}`);
        await fetchAccounts();
      }, `删除选中的 ${ids.length} 个账号？`);
    });
  }

  /** 当前结果集对应的链接文本（随「纯链接 / 按账号分组」切换）。 */
  function resultText() {
    return S_.results.mode === "grouped"
      ? (S_.results.grouped || "") : (S_.results.urls || "");
  }

  /** 「复制全部」：原桌面版放在页头，永远可见（没数据时给出提示而不是静默）。 */
  function copyAllResults() {
    const text = resultText();
    if (!text.trim()) { toast("没有可复制的链接，请先采集或点「查询」"); return; }
    const n = text.split("\n").filter((x) => x.trim()).length;
    copyText(text, `已复制 ${n} 条链接到剪贴板`);
  }

  /** 「链接窗口」：与原桌面版一致的独立链接面板（纯链接 / 按账号分组）。 */
  function openLinkWindow() {
    // 与原桌面版一致：没有链接时只提示、不开空窗口
    if (!resultText().trim()) {
      toast("没有可展示的链接，请先查询或采集");
      return;
    }
    const mask = el("div", "modal-mask");
    const box = el("div", "modal modal-wide");
    box.innerHTML = `
      <div class="modal-head">
        <div class="modal-title">链接窗口</div>
        <button class="modal-close">✕</button>
      </div>
      <div class="modal-body">
        <div class="row" style="margin-top:0">
          <div class="seg" id="lw-mode">
            <button data-mode="urls" class="active">纯链接</button>
            <button data-mode="grouped">按账号分组</button>
          </div>
          <span class="spacer" style="flex:1"></span>
          <span class="muted" id="lw-count"></span>
          <button class="btn btn-ghost btn-sm" id="lw-copy-sel">复制选中</button>
          <button class="btn btn-ghost btn-sm" id="lw-copy">复制全部</button>
        </div>
        <pre class="url-box" id="lw-text" tabindex="0"></pre>
      </div>`;
    const close = () => mask.remove();
    mask.appendChild(box);
    document.body.appendChild(mask);

    let mode = S_.results.mode || "urls";
    const paint = () => {
      const text = mode === "grouped"
        ? (S_.results.grouped || "") : (S_.results.urls || "");
      // 只数真正的链接行（分组模式下还有 @账号 标题行）
      const n = text.split("\n").filter((x) => /^https?:\/\//.test(x.trim())).length;
      $("lw-text").textContent = text || "暂无数据";
      $("lw-count").textContent = `共 ${n} 条链接`;
    };
    box.querySelectorAll("#lw-mode button").forEach((b) => {
      b.addEventListener("click", () => {
        box.querySelectorAll("#lw-mode button").forEach((x) =>
          x.classList.toggle("active", x === b));
        mode = b.dataset.mode;
        S_.results.mode = mode;      // 与原版一致：窗口里切模式，结果页也跟着切
        paint();
      });
    });
    const lwGetter = () => (mode === "grouped"
      ? (S_.results.grouped || "") : (S_.results.urls || ""));
    $("lw-copy").addEventListener("click", () =>
      copyText(lwGetter(), "链接已复制"));
    $("lw-copy-sel").addEventListener("click", () =>
      copySelectedFrom($("lw-text")));
    attachUrlBox($("lw-text"), lwGetter);
    box.querySelector(".modal-close").addEventListener("click", close);
    mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
    paint();
    $("lw-text").focus();
  }

  function bindResultsOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindResultsOnce._done) return;
    bindResultsOnce._done = true;
    document.querySelectorAll("#rs-scope button").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#rs-scope button").forEach((x) =>
          x.classList.toggle("active", x === b));
        S_.results.scope = b.dataset.scope;
        loadResults();
      });
    });
    document.querySelectorAll("#rs-mode button").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#rs-mode button").forEach((x) =>
          x.classList.toggle("active", x === b));
        S_.results.mode = b.dataset.mode;
        renderUrlBox();
      });
    });
    on("rs-account", "change", (e) => {
      S_.results.accountId = e.target.value;
      loadResults();
    });
    on("rs-date", "change", (e) => { S_.results.date = e.target.value; loadResults(); });
    on("rs-status", "change", (e) => {
      S_.results.status = e.target.value;
      renderResults();          // 状态过滤在前端，不必再请求一次
    });
    on("rs-query", "click", () => loadResults());
    // 「导出…」下拉（原桌面版就是 OptionMenu，不是一排四个按钮）
    const expBtn = $("rs-export-btn");
    const expMenu = $("rs-export-menu");
    if (expBtn && expMenu) {
      const closeMenu = () => {
        expMenu.classList.add("hidden");
        expBtn.setAttribute("aria-expanded", "false");
      };
      expBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const willOpen = expMenu.classList.contains("hidden");
        expMenu.classList.toggle("hidden", !willOpen);
        expBtn.setAttribute("aria-expanded", String(willOpen));
      });
      expMenu.querySelectorAll("button").forEach((b) => {
        b.addEventListener("click", () => {
          closeMenu();
          doExport(b.dataset.fmt, b.dataset.mode);
        });
      });
      if (!bindResultsOnce._docBound) {
        bindResultsOnce._docBound = true;
        document.addEventListener("click", closeMenu);
        document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
      }
    }
    on("rs-copy-all", "click", copyAllResults);
    on("rs-link-window", "click", openLinkWindow);
    on("rs-clear", "click", () => {
      const btn = $("rs-clear");
      // 清空范围 = 当前筛选的**日期 + 账号**（不含状态，与旧桌面版一致）
      const bits = [];
      if (S_.results.accountId) {
        const a = S_.accounts.find((x) => x.account_id === S_.results.accountId);
        bits.push(`账号 @${a ? a.username : S_.results.accountId}`);
      }
      if (S_.results.date) bits.push(`发布日期 ${S_.results.date}`);
      const where = bits.length ? bits.join(" · ") : "全部历史结果";
      inlineConfirm(btn, async () => {
        try {
          const body = {};
          if (S_.results.accountId) body.account_id = S_.results.accountId;
          if (S_.results.date) body.publish_date = S_.results.date;
          const r = await api(`${API}/results/clear`, { method: "POST", body });
          toast(`已清空 ${r.deleted} 条`);
          loadResults();
        } catch (e) { toast("清空失败：" + e.message); }
      }, `清空「${where}」的链接？`);
    });
    // 链接文本区：复制选中 / 复制全部 + 快捷键 + 右键菜单
    // （对齐原桌面版 app/ui/link_window.py::LinkTextPanel）
    on("rs-copy", "click", copyAllResults);
    on("rs-copy-sel", "click", () => copySelectedFrom($("rs-urls")));
    attachUrlBox($("rs-urls"), resultText);
  }

  function bindDailyOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindDailyOnce._done) return;
    bindDailyOnce._done = true;
    on("dl-refresh", "click", loadDaily);

    // 复制一律从 S_.daily.text（服务端完整正文）取，不从 DOM 取 ——
    // 展示区有滚动条，从 DOM 取容易让人怀疑「只复制了看得见的部分」。
    const archiveText = () => S_.daily.text || $("dl-content").textContent;
    const copyAll = () => {
      const t = archiveText();
      const n = countLinks(t);
      if (!String(t || "").trim()) { toast("没有可复制的内容"); return; }
      copyText(t, n ? `已复制 ${n} 条链接` : "归档内容已复制");
    };
    on("dl-copy", "click", copyAll);
    on("dl-copy-sel", "click", () => {
      let picked = "";
      const box = $("dl-content");
      try {
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed && box &&
            sel.anchorNode && box.contains(sel.anchorNode)) {
          picked = String(sel);
        }
      } catch { /* 选区不可用时按「复制全部」处理 */ }
      if (picked.trim()) {
        const n = countLinks(picked);
        copyText(picked, n ? `已复制选中 ${n} 条链接` : "已复制选中内容");
        return;
      }
      copyAll();
    });
    on("dl-download", "click", () => {
      const day = S_.daily.day || S_.daily.current || "archive";
      saveText(`${day}.txt`, archiveText());
    });

    // 归档正文区：与原「采集结果」链接区同一套交互（框选 / Ctrl+A / Ctrl+C / 右键菜单）
    const box = $("dl-content");
    if (box) {
      box.setAttribute("tabindex", "0");
      box.addEventListener("keydown", (e) => {
        if (!(e.ctrlKey || e.metaKey)) return;
        const k = e.key.toLowerCase();
        if (k === "a") { e.preventDefault(); selectAllIn(box); }
        else if (k === "c") { e.preventDefault(); copyAll(); }
      });
      box.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openCtxMenu(e.clientX, e.clientY,
          [["复制全部", copyAll],
           ["全选", () => selectAllIn(box)]]);
      });
    }
  }

  let chatBound = false;
  function bindChatOnce() {
    // 幂等守卫：alwaysReload 的视图**每次进入**都会重跑本函数，
    // 不加这道闸就会重复 addEventListener（按钮点一次跑两遍、
    // 下拉菜单点一次被 toggle 两次＝看起来没反应）。
    if (bindChatOnce._done) return;
    bindChatOnce._done = true;
    if (chatBound) return;
    chatBound = true;
    on("ch-conv", "click", loadConvs);
    on("ch-login", "click", chatLogin);
    on("ch-start", "click", chatCollect);
    on("ch-stop", "click", () => {
      if (!S_.chat.jobId) { toast("当前没有进行中的聊天采集"); return; }
      jobAction(S_.chat.jobId, "cancel");
    });
    on("ch-copy", "click", () => copyText($("ch-links").textContent, "链接已复制"));
    on("ch-export", "click", chatExport);
    on("ch-clear", "click", chatClear);

    // 关键词输入即过滤（纯本地，无网络请求 —— 原版设计）
    on("ch-filter", "input", () => {
      S_.chat.filter = $("ch-filter").value;
      renderConvs();
    });
    on("ch-filter", "keydown", (e) => {
      if (e.key === "Escape") { $("ch-filter").value = ""; S_.chat.filter = ""; renderConvs(); }
    });

    // 类型分段：全部 / 好友 / 群聊
    const seg = $("ch-type-seg");
    if (seg) {
      seg.querySelectorAll("button").forEach((b) => {
        b.addEventListener("click", () => {
          seg.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
          b.classList.add("active");
          S_.chat.type = b.dataset.type || "";
          renderConvs();
        });
      });
    }

    // 切换账号：清空已选目标与会话列表
    // （⚠️ 否则会用 A 账号的 conversation_id 去 B 账号的会话列表里点选，
    //   结果是「采不到」或采到别的会话 —— 原程序 v6 之前踩过目标串号的坑）
    on("ch-account", "change", () => {
      S_.chat.target = null;
      S_.chat.convs = [];
      S_.chat.filter = "";
      S_.chat.type = "";
      S_.chat.jobId = null;
      if ($("ch-filter")) $("ch-filter").value = "";
      if (seg) seg.querySelectorAll("button").forEach((x, i) => x.classList.toggle("active", i === 0));
      $("ch-start").disabled = true;
      $("ch-stop").disabled = true;
      const box = $("ch-target-label");
      box.textContent = "未选择目标（在左侧列表点选）";
      box.classList.remove("ok");
      $("ch-conv-count").textContent = "尚未加载，点右上角「刷新会话列表」";
      renderConvs();
      renderChatLinks([]);
      setChatStatus("已切换账号，请重新点「刷新会话列表」加载该账号的会话");
      setBar("ch-bar", 0);
    });
  }

  function bindJobsOnce() {
    on("jb-refresh", "click", loadJobs);
    on("jb-filter", "change", loadJobs);
  }

  let settingsBound = false;
  function bindCollectorSettingsOnce() {
    if (settingsBound) return;
    settingsBound = true;
    on("cset-save", "click", saveCollectorSettings);
    on("cset-reset", "click", () => {
      const btn = $("cset-reset");
      inlineConfirm(btn, async () => {
        try {
          const r = await api(`${API}/settings/reset`, { method: "POST", body: {} });
          toast(`已恢复默认（${(r.reset || []).length} 项）`);
          $("cset-msg").textContent = "已恢复默认采集设置";
          await loadCollectorSettings();
          if (S_.meta) S_.meta = null;
          S_.meta = await api(`${API}/meta`).catch(() => S_.meta);
          syncNotifyCfg();
        } catch (e) {
          $("cset-msg").textContent = "恢复默认失败：" + e.message;
          toast("恢复默认失败：" + e.message);
        }
      }, "恢复全部采集设置为默认值？（不影响账号与作品数据）");
    });
    on("cset-proxy-test", "click", async () => {
      $("cset-proxy-msg").textContent = "测试中…";
      try {
        const r = await api(`${API}/settings/test-proxy`, { method: "POST" });
        $("cset-proxy-msg").textContent = r.message || (r.ok ? "可达" : "不可达");
        $("cset-proxy-msg").style.color = r.ok ? "var(--success)" : "var(--danger-2)";
      } catch (e) {
        $("cset-proxy-msg").textContent = "测试失败：" + e.message;
        $("cset-proxy-msg").style.color = "var(--danger-2)";
      }
    });
    on("cset-backup", "click", async () => {
      $("cset-maint-msg").textContent = "备份中…";
      try {
        const r = await api(`${API}/backup`, { method: "POST" });
        $("cset-maint-msg").textContent = "已备份：" + r.name;
        toast("数据库已备份");
      } catch (e) { $("cset-maint-msg").textContent = "备份失败：" + e.message; }
    });
    on("cset-backups", "click", loadBackupList);
    on("cset-paths", "click", loadCollectorPaths);
    on("cset-open-logs", "click", async () => {
      try {
        const r = await api("/api/system/open-log-dir", { method: "POST" });
        toast("已打开日志目录：" + (r.path || ""));
      } catch (e) { toast("打开目录失败：" + e.message); }
    });
  }

  async function loadBackupList() {
    const wrap = $("cset-backup-list");
    if (!wrap) return;
    wrap.textContent = "加载中…";
    try {
      const d = await api(`${API}/backups`);
      const list = d.backups || [];
      wrap.innerHTML = "";
      if (!list.length) { wrap.textContent = "暂无备份"; return; }
      wrap.appendChild(el("div", "muted",
        `共 ${list.length} 个备份（显示最近 10 个）。「恢复」会覆盖当前数据库，请谨慎。`));
      list.slice(0, 10).forEach((b) => {
        const row = el("div", "file-row");
        row.appendChild(el("span", "fname", b.name));
        row.appendChild(el("span", "fsize", (b.size / 1024).toFixed(0) + " KB"));
        row.appendChild(el("span", "muted", b.mtime));
        const btn = el("button", "btn btn-ghost btn-sm", "恢复");
        // 恢复是不可逆写操作：必须由用户点第二次（内联确认，不弹模态框）
        btn.addEventListener("click", () => {
          inlineConfirm(btn, async () => {
            const msg = $("cset-maint-msg");
            msg.textContent = "恢复中…";
            try {
              await api(`${API}/restore`, { method: "POST", body: { path: b.path } });
              msg.textContent = `已从 ${b.name} 恢复（建议重启程序）`;
              toast("数据库已恢复");
            } catch (e) {
              msg.textContent = "恢复失败：" + e.message;
              toast("恢复失败：" + e.message);
            }
          }, `用 ${b.name} 覆盖当前数据库？`);
        });
        row.appendChild(btn);
        wrap.appendChild(row);
      });
    } catch (e) { wrap.textContent = "加载失败：" + e.message; }
  }

  async function loadCollectorPaths() {
    const kv = $("cset-path-list");
    if (!kv) return;
    kv.innerHTML = "";
    try {
      const d = await api(`${API}/env`);
      [
        ["数据库", d.db_path],
        ["导出目录", d.exports_dir],
        ["日志目录", d.logs_dir],
        ["每日归档目录", d.links_log_dir],
        ["备份目录", d.backups_dir],
        ["登录态目录", d.sessions],
      ].forEach(([k, v]) => {
        const row = el("div", "kv-row");
        row.appendChild(el("div", "kv-k", k));
        const val = el("div", "kv-v", v == null ? "—" : String(v));
        row.appendChild(val);
        kv.appendChild(row);
      });
    } catch (e) {
      kv.appendChild(el("div", "kv-v", "读取失败：" + e.message));
    }
  }

  let aboutBound = false;
  function bindAboutOnce() {
    if (aboutBound) return;
    aboutBound = true;
    on("about-check-update", "click", async () => {
      $("about-update-msg").textContent = "检查中…";
      try {
        const r = await api(`${API}/update-check`);
        $("about-update-msg").textContent = r.has_update
          ? `发现新版本 ${r.version}${r.url ? " · " + r.url : ""}`
          : "已是最新版本";
      } catch (e) { $("about-update-msg").textContent = "检查失败：" + e.message; }
    });
  }

  // 「启动时检查更新」（设置项 auto_check_update）——2026-10-10 之前这个勾选框
  // 没有任何代码读它：勾了不会检查，取消也无人理会。现在启动后静默查一次，
  // 只有**真的发现新版本**才提示（不打扰）；手动按钮的文案行为保持不变。
  async function autoCheckUpdate() {
    try {
      const { values } = await api(`${API}/settings`);
      if (!values || values.auto_check_update === false) return;
      const r = await api(`${API}/update-check`);
      if (!r || !r.has_update) return;
      if (Notify) {
        Notify.show({
          title: "发现新版本",
          message: `采集模块 ${r.version}${r.url ? " · " + r.url : ""}`,
          kind: "info",
          timeout: 15000,
        });
      }
    } catch (_) {
      // 离线 / 未配置更新源 / 接口异常都不该在启动时打扰用户
    }
  }

  // 首屏即可绑定的（这些元素一直在 DOM 里，与视图懒加载无关）
  function bindAll() {
    bindCollectOnce();
    bindAccountsOnce();
    bindResultsOnce();
    bindDailyOnce();
    bindChatOnce();
    bindJobsOnce();
    refreshJobsBadge();
    setInterval(refreshJobsBadge, 3000);
    autoCheckUpdate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindAll);
  } else { bindAll(); }

  // 给外部调试留个口子
  window.TKCollector = { S: S_, loadAccounts, loadCollect, loadResults, loadDaily, loadJobs };
})();
