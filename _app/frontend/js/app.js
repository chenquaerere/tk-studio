/* TK去水印高清程序 v4.0 · 前端逻辑（第 3 期）
   无框架、无构建步骤：原生 JS + fetch + EventSource(SSE)。
   后端契约见 server/api.py。                                          */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  // 转义必须连引号一起处理：esc() 同时用在文本位置和 value="${...}" 属性位置，
  // 昵称/地区里有一个 `"` 就能把属性撑破（表单显示不全，甚至注入 onfocus 之类的属性）。
  const esc = (v) => String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const fmtSize = (n) => {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 ** 3) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 ** 3).toFixed(2) + " GB";
  };

  // ------------------------------------------------------------ HTTP
  async function api(path, { method = "GET", body = null, raw = false } = {}) {
    const opt = { method, headers: {} };
    if (body !== null) {
      if (raw) { opt.body = body; }
      else {
        opt.headers["Content-Type"] = "application/json";
        opt.body = JSON.stringify(body);
      }
    }
    const res = await fetch(path, opt);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || res.statusText;
      throw new Error(msg);
    }
    return data;
  }

  function toast(msg, ms = 3500) {
    const t = $("toast");
    t.textContent = msg;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.textContent = ""; }, ms);
  }

  // -------------------------------------------------------- 应用内通知卡片
  /**
   * 界面右下角的通知卡片栈（2026-10-10）。
   *
   * 背景：采集完成的提醒原来一律弹**桌面**浮层（server/notifier.py），
   * 用户反馈"通知跑到程序外面去了"。现在**只有这里一条通道**：
   * 采集链接 / 去水印 / 高清修复的完成消息都画成程序界面右下角的卡片
   * （宽度/缩放由设置驱动：notify_card_width / notify_card_scale）；
   * 停留时长由设置里的 notify_duration 决定（0 = 不自动关闭）；
   * 窗口最小化时倒计时挂起，回来自动接着走。不再弹桌面浮层。
   * 卡片样式全部在 css/app.css（#notify-stack / .notify-card）。
   */
  const Notify = (() => {
    const ICONS = { info: "ℹ", ok: "✔", warn: "⚠", error: "✖" };
    const MAX_CARDS = 3;            // 再多就把最老的挤掉，避免糊住半个屏幕
    const DEFAULT_TIMEOUT = 9000;
    let host = null;
    let onKey = null;
    // 自动消失时长（毫秒）；0 = 常驻，等用户点掉。由设置 notify_duration 驱动。
    let duration = DEFAULT_TIMEOUT;

    /** 设置里的秒数 → 毫秒（0 = 不自动关闭；下限 3 秒，太短看不清）。 */
    function setDuration(sec) {
      const s = Number(sec);
      if (!Number.isFinite(s) || s < 0) return;
      duration = s === 0 ? 0 : Math.max(3000, Math.round(s * 1000));
    }

    function stack() {
      if (host && host.isConnected) return host;
      host = document.createElement("div");
      host.id = "notify-stack";
      host.setAttribute("role", "status");
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
      return host;
    }

    function unbindKey() {
      if (onKey) {
        document.removeEventListener("keydown", onKey, true);
        onKey = null;
      }
    }

    function close(card) {
      if (!card || card.dataset.closing) return;
      card.dataset.closing = "1";
      clearTimeout(Number(card.dataset.timer || 0));
      card.classList.add("notify-out");
      setTimeout(() => {
        card.remove();
        if (host && !host.children.length) unbindKey();
      }, 180);
    }

    /** 进度条：从 from（0~1）走完 ms。改 `--notify-from` 让暂停后接着走。 */
    function runBar(card, from, ms) {
      const bar = card.querySelector(".notify-progress > i");
      if (!bar) return;
      bar.style.setProperty("--notify-from", String(Math.max(0, Math.min(1, from))));
      bar.style.animation = "none";     // 重置动画，否则不会重新播放
      void bar.offsetWidth;
      bar.style.animation = "";
      bar.style.animationDuration = ms + "ms";
    }

    function schedule(card, ms, from = 1) {
      card.dataset.total = String(card.dataset.total || ms);
      card.dataset.left = String(ms);
      card.dataset.startedAt = String(Date.now());
      delete card.dataset.paused;     // 重新计时：暂停标记同步清掉
      clearTimeout(Number(card.dataset.timer || 0));
      card.dataset.timer = String(setTimeout(() => close(card), ms));
      runBar(card, from, ms);
    }

    function pause(card) {
      if (card.dataset.closing) return;
      if (card.classList.contains("notify-sticky")) return;   // 常驻卡片没有倒计时
      if (card.dataset.paused === "1") return;   // 已经在暂停：别再扣一次时间
      card.dataset.paused = "1";
      const total = Number(card.dataset.total || DEFAULT_TIMEOUT);
      const used = Date.now() - Number(card.dataset.startedAt || Date.now());
      const left = Math.max(400, Number(card.dataset.left || total) - used);
      card.dataset.left = String(left);
      clearTimeout(Number(card.dataset.timer || 0));
      card.classList.add("notify-paused");   // CSS 暂停进度条动画
    }

    function resume(card) {
      if (card.dataset.closing) return;
      if (card.classList.contains("notify-sticky")) return;   // 别把常驻卡片叫醒成限时卡
      if (card.dataset.paused !== "1") return;   // 没在暂停就别重启动画
      delete card.dataset.paused;
      card.classList.remove("notify-paused");
      const total = Math.max(1, Number(card.dataset.total || DEFAULT_TIMEOUT));
      const left = Math.max(400, Number(card.dataset.left || total));
      schedule(card, left, left / total);
    }

    /** 窗口被切走/最小化：挂起倒计时（记下是被谁挂起的）。 */
    function holdCard(card) {
      if (!card || card.dataset.closing) return;
      if (card.classList.contains("notify-sticky")) return;
      card.dataset.held = "1";
      pause(card);
    }

    /** 只续上被「窗口不可见」挂起的卡片；鼠标悬停暂停的不受影响。 */
    function resumeHeld() {
      if (!host) return;
      Array.from(host.children).forEach((card) => {
        if (card.dataset.held !== "1") return;
        delete card.dataset.held;
        resume(card);
      });
    }

    // 最小化/切到别的程序时不让卡片偷偷走完：回来自动接着走（用户不用手点，
    // 也不会错过提醒）。
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { resumeHeld(); return; }
      if (!host) return;
      Array.from(host.children).forEach(holdCard);
    });

    function show({ title, message, kind = "info", timeout = DEFAULT_TIMEOUT } = {}) {
      const k = ICONS[kind] ? kind : "info";
      const box = stack();
      // 卡片出现时把旧的单行提示条清空：同一件事不再在两处各说一遍。
      // 样式里 #toast 也会给卡片让位，这里顺手清内容，免得卡片退场后
      // 那句话又冒出来闪一下。
      const staleToast = $("toast");
      if (staleToast) staleToast.textContent = "";
      const card = el("div", "notify-card");
      card.dataset.kind = k;

      const head = el("div", "notify-head");
      const icon = el("span", "notify-icon", ICONS[k]);
      const ttl = el("span", "notify-title", title || "通知");
      const x = el("button", "notify-close", "×");
      x.type = "button";
      x.title = "关闭";
      x.setAttribute("aria-label", "关闭通知");
      head.append(icon, ttl, x);

      const msg = el("div", "notify-msg", message || "");
      const foot = el("div", "notify-foot");
      const okBtn = el("button", "notify-ok", "知道了");
      okBtn.type = "button";
      foot.appendChild(okBtn);

      const prog = el("div", "notify-progress");
      prog.appendChild(document.createElement("i"));

      card.append(head, msg, foot, prog);
      box.appendChild(card);
      // 超出上限：同步摘掉最老的（close() 有退场动画，会让计数滞后）
      while (box.children.length > MAX_CARDS) {
        const old = box.firstElementChild;
        clearTimeout(Number(old.dataset.timer || 0));
        old.remove();
      }

      x.addEventListener("click", (ev) => { ev.stopPropagation(); close(card); });
      okBtn.addEventListener("click", (ev) => { ev.stopPropagation(); close(card); });
      card.addEventListener("click", () => close(card));
      card.addEventListener("mouseenter", () => pause(card));
      card.addEventListener("mouseleave", () => resume(card));

      if (!onKey) {
        onKey = (ev) => {
          if (ev.key !== "Escape") return;
          const last = host && host.lastElementChild;
          if (!last) return;
          ev.stopPropagation();
          close(last);
        };
        document.addEventListener("keydown", onKey, true);
      }
      // timeout === 0：常驻卡片，不自动消失（设置里选了「不自动关闭」）
      if (Number(timeout) === 0) card.classList.add("notify-sticky");
      else schedule(card, Math.max(1200, Number(timeout) || DEFAULT_TIMEOUT));
      // 弹出时窗口本来就不可见（最小化/切走）：直接挂起倒计时，
      // 等用户回来看见再走完。
      if (document.hidden) holdCard(card);
      return card;
    }

    /** 应用卡片外观（来自采集 meta：notify_card_width / notify_card_scale）。 */
    function configure(meta) {
      const m = meta || {};
      const w = Number(m.notify_card_width);
      const s = Number(m.notify_card_scale);
      const root = document.documentElement;
      root.style.setProperty("--notify-w",
        (Number.isFinite(w) && w >= 200 ? Math.round(w) : 420) + "px");
      root.style.setProperty("--notify-scale",
        String(Number.isFinite(s) && s > 0 ? s : 1));
      const d = Number(m.notify_duration);
      if (Number.isFinite(d)) setDuration(d);
    }

    return { show, close, configure, setDuration,
             durationMs: () => duration };
  })();

  function setDisabled(ids, busy) {
    ids.forEach((id) => { const n = $(id); if (n) n.disabled = busy; });
  }

  function setBar(id, frac) {
    const n = $(id);
    if (n) n.style.width = Math.max(0, Math.min(100, Math.round((frac || 0) * 100))) + "%";
  }

  function writeLog(id, line) {
    const n = $(id);
    if (!n) return;
    n.textContent += (n.textContent && n.textContent !== "等待任务…" ? "\n" : "") + line;
    if (n.textContent.startsWith("等待任务…")) n.textContent = line;
    n.scrollTop = n.scrollHeight;
  }

  function clearLog(id, placeholder = "等待任务…") {
    const n = $(id);
    if (n) n.textContent = placeholder;
  }

  // ------------------------------------------------------------ 导航
  // 视图清单（与 index.html 侧栏一一对应）。
  // 新增视图 = 在这里加一个 id + 在 index.html 加一个 <section id="page-xx">
  //           + 往 PAGE_LOADERS 注册懒加载函数。collector.js 也走这套。
  const PAGES = [
    "dashboard",                                                // 总览
    "accounts", "chat", "collect", "results", "daily", "history", // 采集工作流
    "tiktok", "hd", "library", "models",                        // 素材制作
    "jobs", "tools", "logs", "settings", "about",                // 系统
  ];

  const pageLoaded = {};
  const PAGE_LOADERS = {};      // id -> () => void（首次进入时调用一次）

  function activatePage(target) {
    const btn = document.querySelector(`.nav-item[data-page="${target}"]`);
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b === btn));
    PAGES.forEach((p) => {
      const node = $("page-" + p);
      if (node) node.classList.toggle("hidden", p !== target);
    });
    const content = document.querySelector(".content");
    if (content) content.scrollTop = 0;
    if (target && location.hash.slice(1) !== target) location.hash = target;
    if (!pageLoaded[target]) {
      const loader = PAGE_LOADERS[target];
      if (loader) {
        pageLoaded[target] = true;
        try { loader(); }
        catch (err) { toast(target + " 视图加载失败：" + err.message); }
      }
    }
  }

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => activatePage(btn.dataset.page));
  });

  window.addEventListener("hashchange", () => {
    const t = location.hash.slice(1);
    if (t && PAGES.includes(t) && t !== document.querySelector(".nav-item.active")?.dataset.page) {
      activatePage(t);
    }
  });

  // 素材制作 / 系统视图的懒加载（函数声明已提升，这里只是登记）
  PAGE_LOADERS.models = () => loadModels();
  PAGE_LOADERS.library = () => loadLibrary();
  PAGE_LOADERS.about = () => loadAbout();
  PAGE_LOADERS.settings = () => loadSettings();

  // 首屏视图：优先 URL hash，其次侧栏标记为 active 的那一项。
  // 同步执行，避免等 boot() 的异步请求回来之前出现空白外壳。
  (function bootInitialPage() {
    const initial = location.hash.slice(1);
    const fallback = document.querySelector(".nav-item.active")?.dataset.page;
    activatePage(PAGES.includes(initial) ? initial : (fallback || "tiktok"));
  })();

  // ------------------------------------------------------------ SSE 任务
  const activeJobs = {};

  function watchJob(jobId, opts) {
    if (!jobId) return null;
    const { bar, log: logId, buttons = [], onDone = null, label = "任务" } = opts || {};
    const state = { id: jobId, label, es: null, done: false };
    activeJobs[jobId] = state;

    const es = new EventSource(`/api/jobs/${jobId}/events`);
    state.es = es;

    es.addEventListener("snapshot", (e) => {
      const s = JSON.parse(e.data);
      if (bar) setBar(bar, s.frac);
    });

    ["stage", "progress", "item"].forEach((kind) => {
      es.addEventListener(kind, (e) => {
        const ev = JSON.parse(e.data);
        if (bar && typeof ev.frac === "number") setBar(bar, ev.frac);
        const tag = ev.stage_label ? `[${ev.stage_label}] ` : "";
        if (ev.message) writeLog(logId, tag + ev.message);
      });
    });

    es.addEventListener("log", (e) => {
      const ev = JSON.parse(e.data);
      if (ev.message) writeLog(logId, ev.message);
    });

    // 记录最近一次收到的结果数据。若后端把结果事件错发在 finish 之后，
    // 这里还能兜住（v4.0.2 之前就踩过这个坑：解析完成但一直没有结果）。
    const lastData = {};
    lastData.items = null;
    lastData.failed = null;
    lastData.outputs = null;
    // 本任务收到过的全部 state 字段（accounts / summary / task_id …）。
    // 收尾事件若没带 data，就用它补齐 —— 这是「采集完成显示 0 个账号」的兜底。
    const lastState = {};

    const applyData = (d) => {
      if (!d) return;
      if (Array.isArray(d.items) && opts.onItems) {
        lastData.items = d.items;
        try { opts.onItems(d.items, d); } catch (err) {
          writeLog(logId, "结果渲染出错：" + err.message);
        }
      }
      if (Array.isArray(d.failed) && opts.onFailed) {
        lastData.failed = d.failed;
        try { opts.onFailed(d.failed); } catch { /* 忽略 */ }
      }
      if (Array.isArray(d.outputs) && opts.onOutputs) {
        lastData.outputs = d.outputs;
        try { opts.onOutputs(d.outputs, d); } catch (err) {
          writeLog(logId, "结果渲染出错：" + err.message);
        }
      }
    };

    es.addEventListener("state", (e) => {
      let ev = {};
      try { ev = JSON.parse(e.data); } catch { return; }
      const d = ev.data || {};
      // 记住本任务 state 里出现过的字段：收尾事件偶发不带 data 时兜底，
      // 否则「采集完成」会统计成 0 个账号 0 条。
      Object.keys(d).forEach((k) => { lastState[k] = d[k]; });
      applyData(d);
    });

    es.addEventListener("notify", (e) => {
      let ev = {};
      try { ev = JSON.parse(e.data); } catch { return; }
      const kind = ["ok", "warn", "error"].includes(ev.level) ? ev.level : "info";
      const msg = ev.message || (kind === "error" ? "任务失败" : "任务完成");
      // 没有正文的纯 info 事件（旧后端）不打扰用户。
      if (!ev.message && kind === "info") return;
      notifyCompletion(msg, kind);
    });

    const finish = (kind) => (e) => {
      let ev = {};
      try { ev = JSON.parse(e.data || "{}"); } catch { /* 忽略 */ }
      state.done = true;
      if (bar && kind === "job_done") setBar(bar, 1);
      const cn = { job_done: "完成", job_failed: "失败", job_cancelled: "已取消" }[kind];
      writeLog(logId, `—— ${cn} ——`);
      if (ev.message) writeLog(logId, ev.message);
      // 收尾事件的结果：以事件自带的 data 为准，缺的字段用本任务的 state 补齐。
      // （结果回调仍按事件原始 data 派发，保持既有行为不变。）
      const payload = Object.assign({}, lastState, ev.data || {});
      ev = Object.assign({}, ev, { data: payload });
      // 兜底：把结果重放一次，确保「没有结果」这类问题不再出现
      applyData(ev.data || {});
      if (lastData.items && opts.onItems) {
        try { opts.onItems(lastData.items, {}); } catch { /* 忽略 */ }
      }
      if (lastData.outputs && opts.onOutputs) {
        try { opts.onOutputs(lastData.outputs, {}); } catch { /* 忽略 */ }
      }
      setDisabled(buttons, false);
      try { es.close(); } catch { /* 忽略 */ }
      delete activeJobs[jobId];
      refreshTaskBar();
      if (onDone) onDone(kind, ev);
    };
    es.addEventListener("job_done", finish("job_done"));
    es.addEventListener("job_failed", finish("job_failed"));
    es.addEventListener("job_cancelled", finish("job_cancelled"));
    es.onerror = () => { /* EventSource 自动重连 */ };

    setDisabled(buttons, true);
    return state;
  }

  async function jobAction(jobId, action) {
    if (!jobId) { toast("当前没有进行中的任务"); return; }
    try {
      const r = await api(`/api/jobs/${jobId}/${action}`, { method: "POST" });
      toast(r.message || action);
    } catch (e) { toast("操作失败：" + e.message); }
  }

  async function refreshTaskBar() {
    try {
      const { jobs } = await api("/api/jobs");
      const act = (jobs || []).filter((j) =>
        ["running", "paused", "pending"].includes(j.state));
      $("task-summary").textContent = act.length
        ? `进行中 ${act.length} 项 · ${act[0].label || act[0].kind} ${Math.round((act[0].frac || 0) * 100)}%`
        : "就绪";
    } catch { $("task-summary").textContent = "服务未连接"; }
  }
  setInterval(refreshTaskBar, 2500);

  // -------------------------------------- 任务完成的统一通知（唯一出口）
  // 水印侧（高清修复 / 下载 / 处理）的完成消息由后端通过任务的 notify 字段
  // 以 SSE 事件（jobs.py 的 NOTIFY）送出，采集侧走 bus.notify 轮询。
  // 2026-10-11 用户第三次反馈：「还是在桌面右下角弹」—— 只要按「窗口有没有焦点」
  // 分流，pywebview 下 document.hasFocus() 经常是 false，最后几乎总会走桌面浮层；
  // 而且采集页和去水印页各有一套判断，看着就像两个不一样的弹窗。
  // 现在只有一条通道：三个板块（采集链接 / 去水印 / 高清修复）全部调用
  // notifyCompletion()，卡片画在**程序界面右下角**（css/app.css 的 #notify-stack），
  // 桌面浮层不再被任何完成消息使用。没打标的消息（进度类）仍是 toast。
  let beepOnDone = true;
  async function syncBeepOnDone() {
    try {
      const d = await api("/api/settings");
      const raw = (d && d.raw) || {};
      if (typeof raw.beep_on_done === "boolean") beepOnDone = raw.beep_on_done;
    } catch { /* 读取失败：保持默认开启 */ }
  }
  syncBeepOnDone();

  /** 完成提醒的停留时长（采集设置 notify_duration；0 = 不自动关闭）。
   *
   *  设置页保存后 collector.js 会重拉 meta 并 configure()；这里再兜一次
   *  启动读取，保证一进来就是去水印/高清页也有正确时长。
   */
  async function syncNotifyDuration() {
    try {
      const d = await api("/api/collector/settings");
      const values = (d && d.values) || {};
      const v = Number(values.notify_duration);
      if (Number.isFinite(v)) Notify.setDuration(v);
    } catch { /* 读取失败：用默认 9 秒 */ }
  }
  syncNotifyDuration();

  /** 只播提示音，不弹窗（desktop:false 明确告诉后端：别弹桌面浮层）。 */
  function beepOnce() {
    api("/api/system/notify", {
      method: "POST",
      body: { title: "", message: "", kind: "info", timeout_ms: 0,
              sound: true, desktop: false },
    }).catch(() => { /* 提示音失败不影响结果 */ });
  }

  /** 任务完成类消息的**唯一**出口：程序界面右下角卡片（+ 可选提示音）。
   *
   *  停留时长 = 采集设置里的 notify_duration（默认 9 秒；0 = 不自动关闭）；
   *  窗口最小化 / 被切走时倒计时挂起，回来自动接着走完 —— 不会漏消息，
   *  也不用每次手动点掉（2026-10-11 用户反馈：以前窗口没焦点就常驻，
   *  每张卡片都得手点）。任何情况下都不再弹桌面浮层。
   *  opts.sound：本次是否出声（采集侧用 notify_sound，水印侧用 beep_on_done）。 */
  function notifyCompletion(msg, kind = "info", title = "任务完成", opts = {}) {
    if (!msg) return;
    // 时长由设置决定；不再看窗口有没有焦点 —— pywebview 下 hasFocus() 常为
    // false，那条分支会让每张卡片都变成常驻，用户每次都得起手点掉。
    Notify.show({ title, message: msg, kind, timeout: Notify.durationMs() });
    const sound = opts.sound === undefined ? beepOnDone : !!opts.sound;
    if (sound) beepOnce();
  }

  function notifyItem(n) {
    const msg = (n && n.message) || "";
    if (!msg) return;
    if (!n.card) { toast(msg, 4200); return; }
    notifyCompletion(msg, n.level || "info");
  }

  setInterval(async () => {
    try {
      const { items } = await api("/api/notifications");
      (items || []).forEach(notifyItem);
    } catch { /* 忽略 */ }
  }, 2800);

  // ------------------------------------------------------------ 渲染助手

  /** 自动表头：中文名优先，否则用列序号。 */
  const HDR_CN = {
    0: "序号", 1: "平台/时间", 2: "类型", 3: "标题", 4: "作者",
    5: "画质/大小", 6: "时长/路径", 7: "状态",
  };

  /**
   * 渲染表格。两种调用都支持：
   *   renderTable(id, rows)              —— 自动表头（后端返回的是二维数组）
   *   renderTable(id, headers, rows)     —— 指定表头
   * 之前只支持后者，导致「下载历史 / 素材库」把数据当成了表头，
   * 表格显示成一片空白。
   */
  function renderTable(containerId, headers, rows) {
    const box = $(containerId);
    if (!box) return;
    if (rows === undefined) {          // 两参形式
      rows = headers;
      headers = null;
    }
    if (!rows || !rows.length) {
      box.className = "table-wrap muted";
      box.textContent = "暂无数据";
      return;
    }
    const ncol = Math.max(...rows.map((r) => (r ? r.length : 0)), 0);
    const useHeaders = (headers && headers.length)
      ? headers
      : Array.from({ length: ncol }, (_, i) => HDR_CN[i] || `列${i + 1}`);
    const thead = "<tr>" + useHeaders.map((h) => `<th>${esc(h)}</th>`).join("") + "</tr>";
    const tbody = rows.map((r) =>
      "<tr>" + (r || []).map((c) => `<td>${esc(c)}</td>`).join("") + "</tr>").join("");
    box.className = "table-wrap";
    box.innerHTML = `<table><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
  }

  // 从一次任务的输出里挑出「可以送进高清」的图片路径。
  // v4.0 修复：勾了「下载后自动送入高清（仅图片）」之前根本没人执行——
  // 后端只把图片数量写进 job["auto_hd"]，前端没有任何代码读它。
  function pickAutoHdImages(outputs) {
    return (outputs || [])
      .filter((o) => o && o.is_image && o.path)
      .map((o) => o.path);
  }

  // 单个画廊卡片：图片/视频 + 名称 + 操作行（打开 / 下载）。
  // 手机端取素材的最短路径是「长按图片 → 存储到照片」，为此做了三件事：
  //  1) <img src> 直接指向原图 —— 后端 /api/file 返回的就是原文件，
  //     没有缩略图服务，所以长按存下来的是原图而不是小图；
  //  2) 不在图片上禁 user-select / -webkit-touch-callout，保住 iOS 长按菜单；
  //  3) 「打开」走 preview_url（Content-Disposition: inline）—— 若用 url
  //     （attachment），iOS 会直接下载进「文件」App，相册不在那条路径上。
  function galleryFigure(f) {
    const fig = el("figure");
    if (f.is_video) {
      const wrap = el("div", "video-thumb");
      const v = el("video");
      v.src = f.url; v.controls = true; v.preload = "metadata";
      wrap.appendChild(v);
      fig.appendChild(wrap);
    } else {
      const img = el("img");
      img.src = f.url; img.loading = "lazy"; img.alt = f.name; img.title = f.name;
      img.addEventListener("click", () => openLightbox(f));
      fig.appendChild(img);
    }
    fig.appendChild(el("figcaption", null, f.name));

    const bar = el("div", "gallery-actions");
    const open = el("a", "btn btn-ghost btn-sm", "打开");
    open.href = f.preview_url || f.url;
    open.target = "_blank";
    open.rel = "noopener";
    open.dataset.ico = "window";
    open.title = "在新标签打开原图（手机上可长按保存到相册）";
    const dl = el("a", "btn btn-ghost btn-sm", "下载");
    dl.href = f.url;
    dl.download = f.name || "";
    dl.dataset.ico = "download";
    dl.title = "下载到本机（安卓下载后会自动进相册）";
    bar.appendChild(open);
    bar.appendChild(dl);
    fig.appendChild(bar);
    return fig;
  }

  function renderOutputs(galleryId, filesId, outputs, opts = {}) {
    const gal = $(galleryId);
    const list = $(filesId);
    if (gal) gal.innerHTML = "";
    if (list) list.innerHTML = "";
    const items = outputs || [];
    if (!items.length) return;

    items.forEach((f) => {
      if (gal && (f.is_image || f.is_video)) gal.appendChild(galleryFigure(f));
      if (list) {
        const row = el("div", "file-row");
        row.appendChild(el("span", "fname", f.name));
        if (f.size) row.appendChild(el("span", "fsize", fmtSize(f.size)));
        const a = el("a", null, "打开");
        a.href = f.url; a.target = "_blank"; a.download = f.name;
        row.appendChild(a);
        list.appendChild(row);
      }
    });
  }

  // 「保存到相册」只有在**安全上下文**里才可能存在。
  // Web Share API（navigator.share / canShare）带 [SecureContext] 标记：
  // 非 localhost 的 http:// 源上 navigator.share 直接是 undefined ——
  // 手机访问本程序用的正是 http://192.168.x.x:端口，所以必须先开 HTTPS。
  // 这里做特性检测而不是 UA 判断：不支持就整块不渲染，绝不出现
  // 「点了没反应 / 报错」的按钮。
  function canShareFiles() {
    if (!window.isSecureContext) return false;
    if (typeof navigator === "undefined") return false;
    // 只给触屏设备（手机 / 平板）：桌面端本来就有「下载」，
    // 而 Windows 上 Chrome 也实现了 navigator.share —— 不限制的话
    // 桌面 WebView2 里会多出一个点了弹 Windows 分享面板、
    // 却并不能「存进相册」的按钮。桌面界面保持原样不动。
    const touch = (navigator.maxTouchPoints || 0) > 0;
    if (!touch) return false;
    if (typeof navigator.share !== "function"
        || typeof navigator.canShare !== "function") return false;
    try {
      const probe = new File([new Blob([])], "t.jpg", { type: "image/jpeg" });
      return navigator.canShare({ files: [probe] });
    } catch (e) {
      return false;
    }
  }

  // 先取回原文件再交给系统分享面板（navigator.share 不能直接分享 URL）。
  // 失败时兜底：把原图开在新标签，提示长按保存 —— 至少不会卡死。
  async function saveToAlbum(f, btn) {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "准备中…";
    try {
      const resp = await fetch(f.url || f.preview_url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      const type = blob.type || (f.is_video ? "video/mp4" : "image/jpeg");
      const file = new File([blob], f.name || "素材", { type });
      await navigator.share({ files: [file], title: f.name || "素材" });
      toast("已交给系统保存 / 分享面板");
    } catch (err) {
      // 用户自己取消不算失败，不要弹提示吓人
      if (!err || err.name !== "AbortError") {
        toast("一键保存没成功，已为你打开原图 —— 长按图片选「存储到照片」");
        window.open(f.preview_url || f.url, "_blank", "noopener");
      }
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  function openLightbox(f) {
    const url = typeof f === "string" ? f : (f.preview_url || f.url);
    const box = el("div", "lightbox");
    const img = el("img");
    img.src = url;
    if (typeof f !== "string" && f.name) img.alt = f.name;
    // 只有点「图片以外的背景」才关闭。之前是整个灯箱任意点击都关 ——
    // 手机上长按图片松手时的 click 会把灯箱一起关掉，
    // 用户根本来不及在弹出菜单里选「存储到照片」。
    box.addEventListener("click", (ev) => { if (ev.target === box) box.remove(); });
    const tip = el("div", "lightbox-tip", "长按图片可「存储到照片」");
    box.appendChild(img);
    let shareable = false;
    if (typeof f !== "string") {
      shareable = canShareFiles();
      if (shareable) {
        const bar = el("div", "lightbox-actions");
        const save = el("button", "btn btn-primary btn-sm", "保存到相册");
        save.dataset.ico = "save";
        save.type = "button";
        save.title = "调用系统保存/分享面板，可直接存进相册或分享到抖音、微信";
        save.addEventListener("click", () => saveToAlbum(f, save));
        bar.appendChild(save);
        box.appendChild(bar);
        tip.textContent = "也可以长按图片选「存储到照片」";
      }
    }
    box.appendChild(tip);
    document.body.appendChild(box);
  }

  // ============================================================ 去水印
  const TK_BTNS = ["tk-download", "tk-pause", "tk-resume", "tk-cancel"];
  let tkParseJob = null;
  let tkDownloadJob = null;
  let tkItems = [];
  const tkSelected = new Set();      // 勾选的序号（1-based）

  function updateSelCount() {
    const n = tkSelected.size;
    $("tk-sel-count").textContent = tkItems.length
      ? `已选 ${n} / 共 ${tkItems.length} 项`
      : "已选 0 项";
    const btn = $("tk-download");
    btn.disabled = !(tkItems.length && n);
    // 说清楚为什么不能点，避免用户只看到「禁止」图标却不知道为什么
    btn.title = tkItems.length
      ? (n ? `下载选中的 ${n} 项` : "请先在上面勾选要下载的项目")
      : "请先解析链接";
    // 同步勾选框状态（全选 / 全不选 / 默认全选都走这里）
    document.querySelectorAll('#tk-table input[type="checkbox"]').forEach((cb) => {
      const idx = Number(cb.dataset.idx);
      const should = tkSelected.has(idx);
      if (cb.checked !== should) {
        cb.checked = should;
        const tr = cb.closest("tr");
        if (tr) tr.classList.toggle("picked", should);
      }
    });
  }

  function renderParseResults(items, data) {
    tkItems = items || [];
    tkSelected.clear();
    // v4.0.2：解析完默认「全选」。
    // 之前默认不选，下载按钮一直是禁用状态（鼠标移上去是禁止符号），
    // 用户会以为「下载不了」。现在解析完直接可点「下载选中」。
    tkItems.forEach((it) => tkSelected.add(it.index));

    const box = $("tk-table");
    if (!tkItems.length) {
      box.className = "table-wrap muted";
      box.textContent = "暂无数据";
      $("tk-count").textContent = "";
      updateSelCount();
      return;
    }

    // 用可勾选的列表代替只读表格 —— 手机上点一行就能选中，比勾选框更好按
    box.className = "table-wrap";
    box.innerHTML = "";
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    ["选择", "序号", "平台", "类型", "标题", "作者", "画质", "时长"].forEach((h) =>
      hr.appendChild(el("th", null, h)));
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = el("tbody");
    tkItems.forEach((it) => {
      const tr = el("tr");
      tr.className = "pick-row";

      const tdChk = el("td");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.dataset.idx = String(it.index);
      cb.addEventListener("change", () => {
        if (cb.checked) tkSelected.add(it.index); else tkSelected.delete(it.index);
        tr.classList.toggle("picked", cb.checked);
        updateSelCount();
      });
      tdChk.appendChild(cb);
      tr.appendChild(tdChk);

      [
        it.index,
        it.platform,
        it.kind === "video" ? "视频" : `图集(${it.image_count})`,
        (it.title || "").slice(0, 42),
        it.author || "-",
        it.quality || "-",
        it.duration ? `${it.duration} 秒` : "-",
      ].forEach((v) => tr.appendChild(el("td", null, v)));

      // 点整行也能切换勾选（手机上更好操作）
      tr.addEventListener("click", (e) => {
        if (e.target === cb) return;
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event("change"));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);

    $("tk-count").textContent = `成功 ${tkItems.length} 条`
      + (data && data.urls ? ` / 共 ${data.urls} 条` : "");
    updateSelCount();

    const strip = $("tk-covers");
    strip.innerHTML = "";
    tkItems.forEach((it) => {
      if (!it.cover) return;
      const img = el("img");
      img.src = it.cover; img.loading = "lazy"; img.title = it.title;
      img.addEventListener("error", () => img.remove());
      strip.appendChild(img);
    });
  }

  function renderFailed(failed) {
    const wrap = $("tk-failed-wrap");
    const box = $("tk-failed");
    if (!failed || !failed.length) { if (wrap) wrap.style.display = "none"; return; }
    if (wrap) wrap.style.display = "";
    box.textContent = failed.map((f) => `#${f.index} ${f.reason}`).join("\n");
  }

  $("tk-parse").addEventListener("click", async () => {
    const text = $("tk-urls").value.trim();
    if (!text) { toast("请先粘贴链接"); return; }
    clearLog("tk-log");
    setBar("tk-parse-bar", 0);
    $("tk-parse-msg").textContent = "解析中…";
    setDisabled(["tk-parse"], true);
    try {
      const r = await api("/api/tiktok/parse", { method: "POST", body: { text } });
      tkParseJob = r.job_id;
      watchJob(r.job_id, {
        bar: "tk-parse-bar",
        log: "tk-log",
        label: "解析",
        buttons: ["tk-parse"],
        onItems: (items, data) => {
          renderParseResults(items, data);
          if (data && data.message) $("tk-parse-msg").textContent = data.message;
          toast(`解析完成：成功 ${items.length} 条`);
        },
        onFailed: renderFailed,
        onDone: () => setDisabled(["tk-parse"], false),
      });
    } catch (e) {
      $("tk-parse-msg").textContent = "解析失败：" + e.message;
      setDisabled(["tk-parse"], false);
    }
  });

  $("tk-paste").addEventListener("click", async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (!t) { toast("剪贴板是空的"); return; }
      $("tk-urls").value = ($("tk-urls").value ? $("tk-urls").value + "\n" : "") + t;
      toast("已粘贴");
    } catch {
      toast("浏览器未授权读取剪贴板，请手动 Ctrl+V");
    }
  });

  $("tk-clear").addEventListener("click", () => {
    $("tk-urls").value = "";
    tkItems = [];
    renderParseResults([], {});
    renderTable("tk-table", [], []);
    $("tk-covers").innerHTML = "";
    $("tk-parse-msg").textContent = "未解析";
    $("tk-count").textContent = "";
    setBar("tk-parse-bar", 0);
    $("tk-failed-wrap").style.display = "none";
    $("tk-result-card").style.display = "none";
    clearLog("tk-log");
  });

  $("tk-download").addEventListener("click", async () => {
    if (!tkItems.length) { toast("请先解析链接"); return; }
    const selIdx = $("tk-sel").value.trim();
    const picked = Array.from(tkSelected);
    if (!picked.length && !selIdx) {
      toast("请先勾选要下载的项目（或填写序号）");
      return;
    }
    clearLog("tk-log");
    setBar("tk-bar", 0);
    $("tk-result-card").style.display = "none";
    const wantAutoHd = $("tk-auto-hd").checked;
    let autoHdImages = [];          // 本次下载到的图片（勾了自动高清才有值）
    try {
      const r = await api("/api/tiktok/download", {
        method: "POST",
        body: {
          selected: picked,        // 勾选的序号（1-based）
          sel_idx: selIdx,         // 手填序号优先由后端处理
          out_dir: "",
          auto_hd: wantAutoHd,
        },
      });
      tkDownloadJob = r.job_id;
      watchJob(r.job_id, {
        bar: "tk-bar",
        log: "tk-log",
        buttons: TK_BTNS,
        label: "下载",
        onOutputs: (outputs, data) => {
          $("tk-result-card").style.display = "";
          renderOutputs("tk-gallery", "tk-files", outputs);
          if (wantAutoHd) autoHdImages = pickAutoHdImages(outputs);
          const zipUrl = (data && data.zip_url) || "";
          if (zipUrl) {
            $("tk-zip-row").style.display = "";
            const a = $("tk-zip");
            a.href = zipUrl;
            a.setAttribute("download", "TK下载.zip");
          } else {
            $("tk-zip-row").style.display = "none";
          }
        },
        onDone: (kind) => {
          // 下载成功后，把本次的图片真的送进高清页并按既有流程开始处理
          // （后端不能替前端开高清任务，见 app.js 顶部 pickAutoHdImages 注释）
          if (!wantAutoHd || kind !== "job_done" || !autoHdImages.length) return;
          const paths = autoHdImages.filter((p) => !hdServerFiles.includes(p));
          hdServerFiles = hdServerFiles.concat(paths);
          renderPicked();
          const n = autoHdImages.length;
          autoHdImages = [];
          toast(`已自动送入高清修复：${n} 张图片`);
          activatePage("hd");
          if (hdBusy) {
            toast("高清任务正在运行，已加入列表，稍后点「开始处理」");
            return;
          }
          const btn = $("hd-start");
          if (btn) btn.click();     // 复用高清页既有的开始流程，避免两套逻辑分叉
        },
      });
      toast(picked.length ? `已开始下载 ${picked.length} 项` : "已开始下载");
    } catch (e) { toast("无法开始：" + e.message); }
  });

  $("tk-sel-all").addEventListener("click", () => {
    tkItems.forEach((it) => tkSelected.add(it.index));
    document.querySelectorAll('#tk-table input[type="checkbox"]').forEach((cb) => {
      cb.checked = true;
      cb.closest("tr").classList.add("picked");
    });
    updateSelCount();
  });

  $("tk-sel-none").addEventListener("click", () => {
    tkSelected.clear();
    document.querySelectorAll('#tk-table input[type="checkbox"]').forEach((cb) => {
      cb.checked = false;
      cb.closest("tr").classList.remove("picked");
    });
    updateSelCount();
  });

  $("tk-pause").addEventListener("click", () => jobAction(tkDownloadJob, "pause"));
  $("tk-resume").addEventListener("click", () => jobAction(tkDownloadJob, "resume"));
  $("tk-cancel").addEventListener("click", () => jobAction(tkDownloadJob, "cancel"));
  $("tk-open-dir").addEventListener("click", () => api("/api/tiktok/open-dir").catch(() => {}));

  // --------------------------------------------------------- 素材保存目录
  // 去水印与高清各有一个「保存目录」设置，语义不同（别混淆）：
  //   去水印 → tk_config.output_dir（去水印自己的下载根）
  //   高清   → 全局 output_root（原本页面留空即用它，这里直接改它）
  // 「选择」走后端子进程弹原生目录框 —— 浏览器模式下同样可用，
  // 不依赖 window.pywebview；改动即时保存，后端会同步把新目录
  // 加进访问白名单并重建下载器。
  let DEFAULT_OUT_DIR = "";

  async function pickFolder(initial) {
    const r = await api("/api/system/pick-folder", {
      method: "POST", body: { initial: initial || "" },
    });
    return (r && r.path) || "";        // 用户取消 → 空串
  }

  async function loadOutputDirs() {
    try {
      const r = await api("/api/tiktok/settings");
      DEFAULT_OUT_DIR = r.default_dir || "";
      $("tk-out").value = (r.values && r.values.output_dir) || "";
    } catch { /* 非关键，保留输入框原状 */ }
    try {
      const s = await api("/api/settings");
      $("hd-out").value = (s.values && s.values.output_root) || "";
    } catch { /* 非关键 */ }
  }

  async function saveTkOutputDir(value) {
    try {
      const r = await api("/api/tiktok/settings", {
        method: "POST", body: { output_dir: value },
      });
      toast(r.message || "去水印保存目录已更新", "success");
    } catch (e) { toast("保存失败：" + e.message, "error"); }
  }

  async function saveHdOutputDir(value) {
    try {
      await api("/api/settings", { method: "POST", body: { output_root: value } });
      toast("高清保存目录已更新", "success");
    } catch (e) { toast("保存失败：" + e.message, "error"); }
  }

  function wireOutputDir(cfg) {
    const input = $(cfg.input);
    if (!input) return;
    $(cfg.pick).addEventListener("click", async () => {
      try {
        const p = await pickFolder(input.value.trim());
        if (!p) return;                 // 用户取消：安静返回
        input.value = p;
        cfg.save(p);
      } catch (e) { toast("无法打开目录选择器：" + e.message, "error"); }
    });
    $(cfg.reset).addEventListener("click", () => {
      input.value = DEFAULT_OUT_DIR;
      cfg.save(DEFAULT_OUT_DIR);
    });
  }

  wireOutputDir({ input: "tk-out", pick: "tk-out-pick",
                  reset: "tk-out-reset", save: saveTkOutputDir });
  wireOutputDir({ input: "hd-out", pick: "hd-out-pick",
                  reset: "hd-out-reset", save: saveHdOutputDir });
  loadOutputDirs();

  $("tk-hist-refresh").addEventListener("click", async () => {
    try {
      const { rows } = await api("/api/tiktok/history");
      renderTable("tk-hist",
        ["下载时间", "平台", "类型", "标题", "作者", "大小", "文件路径"], rows || []);
    } catch (e) { $("tk-hist").textContent = "读取失败：" + e.message; }
  });

  $("tk-hist-clear").addEventListener("click", async () => {
    try {
      const r = await api("/api/tiktok/history/clear", { method: "POST" });
      toast(r.message || "已清空");
      renderTable("tk-hist", [], []);
    } catch (e) { toast("清空失败：" + e.message); }
  });

  // ============================================================ 高清修复
  const HD_BTNS = ["hd-start", "hd-pause", "hd-resume", "hd-cancel"];
  let hdJob = null;
  let hdBusy = false;          // 是否有任务真正在跑（hdJob 结束后不会自动清空，不能拿它判断）
  let hdFiles = [];            // 本地待上传的 File 对象
  let hdServerFiles = [];      // 已在服务器磁盘上的路径（最近下载 / 素材库导入）
  let hdPresetKind = "image";
  let hdMeta = { models: [], scales: [] };

  const HD_HINT_DEFAULT = "支持 jpg / png / webp / heic，mp4 / mov / mkv 等";

  function renderPicked() {
    const box = $("hd-picked");
    box.innerHTML = "";
    const rows = [];
    hdFiles.forEach((f, i) => rows.push({
      name: f.name, size: f.size, tag: "待上传",
      remove: () => { hdFiles.splice(i, 1); renderPicked(); },
    }));
    hdServerFiles.forEach((p, i) => rows.push({
      name: String(p).split(/[\\/]/).pop(), size: 0, tag: "服务器上",
      remove: () => { hdServerFiles.splice(i, 1); renderPicked(); },
    }));
    // 顶部工具条：数量统计 + 一键清空（逐个点「移除」太累）
    if (rows.length) {
      const bar = el("div", "filelist-bar");
      const nUp = hdFiles.length, nSrv = hdServerFiles.length;
      const parts = [];
      if (nUp) parts.push(`${nUp} 个待上传`);
      if (nSrv) parts.push(`${nSrv} 个已在服务器`);
      bar.appendChild(el("span", "fl-count", `共 ${rows.length} 个文件（${parts.join("，")}）`));
      const clr = el("button", "btn btn-ghost btn-sm", "🗑 清空");
      clr.type = "button";
      clr.title = "清空全部已选文件（不影响本地文件）";
      clr.addEventListener("click", clearPicked);
      bar.appendChild(clr);
      box.appendChild(bar);
    }
    rows.forEach((r) => {
      const row = el("div", "file-row");
      row.appendChild(el("span", "fname", r.name));
      if (r.tag) row.appendChild(el("span", "fsize", r.tag));
      if (r.size) row.appendChild(el("span", "fsize", fmtSize(r.size)));
      const rm = el("a", null, "移除");
      rm.href = "javascript:void(0)";
      rm.addEventListener("click", r.remove);
      row.appendChild(rm);
      box.appendChild(row);
    });
    if (rows.length) {
      $("hd-drop-hint").textContent =
        `共 ${rows.length} 个文件（${hdFiles.length} 个待上传，${hdServerFiles.length} 个已在服务器）`;
    } else {
      $("hd-drop-hint").textContent = HD_HINT_DEFAULT;
    }
  }

  function clearPicked() {
    if (hdBusy) { toast("任务进行中，请先取消或等待结束再清空列表"); return; }
    if (!hdFiles.length && !hdServerFiles.length) { toast("列表已经是空的"); return; }
    hdFiles = [];
    hdServerFiles = [];
    $("hd-files").value = "";
    renderPicked();
    toast("已清空文件列表（文件夹路径未清空）");
  }

  // 自动化验收用的只读/调试入口（手工操作不需要它）。
  // scripts/verify_frontend.py 会通过它在真实窗口里核对文件清单行为。
  window.__TK = {
    getHdFiles: () => hdFiles.slice(),
    setHdFiles: (arr) => { hdFiles = arr || []; renderPicked(); },
    getHdServerFiles: () => hdServerFiles.slice(),
    setHdServerFiles: (arr) => { hdServerFiles = arr || []; renderPicked(); },
    renderPicked,
    clearPicked,
    getHdJob: () => hdJob,
    isHdBusy: () => hdBusy,
  };

  function setupDropzone() {
    const zone = $("hd-drop");
    const input = $("hd-files");
    $("hd-browse").addEventListener("click", (e) => { e.stopPropagation(); input.click(); });
    zone.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      hdFiles = hdFiles.concat(Array.from(input.files || []));
      input.value = "";
      renderPicked();
    });
    ["dragenter", "dragover"].forEach((ev) =>
      zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) =>
      zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
    zone.addEventListener("drop", (e) => {
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      if (!files.length) return;
      hdFiles = hdFiles.concat(files);
      renderPicked();
    });
  }

  function renderPresets(presets) {
    const box = $("hd-presets");
    box.innerHTML = "";
    (presets || []).forEach((p) => {
      const b = el("button", "capsule", `${p.icon || ""} ${p.label || p.key}`);
      b.type = "button";
      b.title = p.desc || "";
      b.addEventListener("click", async () => {
        box.querySelectorAll(".capsule").forEach((n) => n.classList.remove("active"));
        b.classList.add("active");
        try {
          const r = await api("/api/hd/preset", {
            method: "POST",
            body: {
              key: p.key, kind: hdPresetKind,
              image_model: $("hd-img-model").value,
              video_model: $("hd-vid-model").value,
              img_scale: $("hd-scale").value,
              vid_scale: $("hd-scale").value,
              vid_side: $("hd-side").value,
              img_max: $("hd-max").value,
              face: $("hd-face").checked,
              interpolate: $("hd-interp").checked,
            },
          });
          if (r.image_model) $("hd-img-model").value = r.image_model;
          if (r.video_model) $("hd-vid-model").value = r.video_model;
          if (r.img_scale) $("hd-scale").value = r.img_scale;
          if (r.vid_scale) $("hd-scale").value = r.vid_scale;
          if (r.vid_side) $("hd-side").value = r.vid_side;
          if (typeof r.face === "boolean") $("hd-face").checked = r.face;
          if (typeof r.interpolate === "boolean") $("hd-interp").checked = r.interpolate;
          $("hd-preset-msg").textContent = `已套用预设：${p.label}`;
        } catch (e) { toast("套用预设失败：" + e.message); }
      });
      box.appendChild(b);
    });
  }

  $("hd-start").addEventListener("click", async () => {
    const mode = $("hd-mode").value;
    const folder = $("hd-folder").value.trim();
    if (!hdFiles.length && !hdServerFiles.length && !folder) {
      toast("请选择文件、导入最近下载，或填写文件夹路径");
      return;
    }
    clearLog("hd-log");
    setBar("hd-bar", 0);
    $("hd-result-card").style.display = "none";
    setDisabled(["hd-start"], true);
    try {
      // 已在服务器磁盘上的文件直接复用，不重复上传
      let paths = hdServerFiles.slice();
      if (hdFiles.length) {
        writeLog("hd-log", `正在上传 ${hdFiles.length} 个文件…`);
        const fd = new FormData();
        hdFiles.forEach((f) => fd.append("files", f, f.name));
        const up = await api("/api/upload", { method: "POST", body: fd, raw: true });
        paths = paths.concat(up.files || []);
        clearLog("hd-log");
        writeLog("hd-log", `已就绪 ${paths.length} 个文件`);
      } else if (paths.length) {
        writeLog("hd-log", `直接使用服务器上的 ${paths.length} 个文件`);
      }
      const r = await api("/api/hd/start", {
        method: "POST",
        body: {
          mode, files: paths, folder,
          image_model: $("hd-img-model").value,
          video_model: $("hd-vid-model").value,
          img_scale: $("hd-scale").value,
          vid_scale: $("hd-scale").value,
          vid_side: $("hd-side").value,
          img_max: $("hd-max").value,
          face: $("hd-face").checked,
          interpolate: $("hd-interp").checked,
          out_root: $("hd-out").value.trim() || undefined,
        },
      });
      hdJob = r.job_id;
      hdBusy = true;
      watchJob(r.job_id, {
        bar: "hd-bar",
        log: "hd-log",
        buttons: HD_BTNS,
        label: "高清",
        onOutputs: (outputs) => {
          $("hd-result-card").style.display = "";
          renderOutputs("hd-gallery", "hd-videos", outputs);
        },
        onDone: () => {
          hdBusy = false;
          setDisabled(["hd-start"], false);
          toast("处理结束");
        },
      });
      toast("已开始处理");
    } catch (e) {
      hdBusy = false;
      writeLog("hd-log", "启动失败：" + e.message);
      toast("启动失败：" + e.message);
      setDisabled(["hd-start"], false);
    }
  });

  $("hd-pause").addEventListener("click", () => jobAction(hdJob, "pause"));
  $("hd-resume").addEventListener("click", () => jobAction(hdJob, "resume"));
  $("hd-cancel").addEventListener("click", () => jobAction(hdJob, "cancel"));
  $("hd-open").addEventListener("click", () => api("/api/output-dir").catch(() => {}));

  $("hd-recent").addEventListener("click", async () => {
    try {
      const r = await api("/api/hd/from-recent", { method: "POST", body: {} });
      const paths = r.paths || [];
      $("hd-drop-hint").textContent = r.message;
      if (!paths.length) { toast(r.message || "暂无最近下载的素材"); return; }
      // 这些文件已在服务器磁盘上，直接走 hdFilesServer，无需再上传
      hdServerFiles = hdServerFiles.concat(paths);
      renderPicked();
      toast(r.message || `已导入 ${paths.length} 个素材`);
    } catch (e) { toast("导入失败：" + e.message); }
  });

  $("hd-clear").addEventListener("click", clearPicked);

  // ============================================================ 模型中心
  async function loadModels() {
    try {
      const r = await api("/api/models/status");
      const cat = $("md-cat");
      if (cat.options.length <= 1) {
        (r.categories || []).forEach((c) => {
          const o = el("option", null, c);
          o.value = c;
          cat.appendChild(o);
        });
      }
      renderModelCards(r.items);
      cat.onchange = () => renderModelCards(r.items);
    } catch (e) { $("md-grid").textContent = "加载失败：" + e.message; }
    try {
      const p = await api("/api/models/presets");
      const box = $("md-presets");
      box.innerHTML = "";
      (p.presets || []).forEach((x) => {
        const card = el("div", "preset-card");
        card.appendChild(el("div", "p-name", `${x.icon || ""} ${x.label || x.key}`));
        card.appendChild(el("div", "p-desc", x.desc || ""));
        const img = x.image ? `图片：${x.image.model || "-"} / ${x.image.scale || "-"}` : "";
        const vid = x.video ? `视频：${x.video.model || "-"} / ${x.video.scale || "-"}` : "";
        if (img || vid) card.appendChild(el("div", "m-meta", [img, vid].filter(Boolean).join("　")));
        box.appendChild(card);
      });
    } catch (e) { $("md-presets").textContent = "加载失败：" + e.message; }
  }

  function renderModelCards(items) {
    const box = $("md-grid");
    const filter = $("md-cat").value;
    box.innerHTML = "";
    const list = (items || []).filter((m) => !filter || m.category === filter);
    if (!list.length) { box.textContent = "没有匹配的模型"; return; }
    list.forEach((m) => {
      const card = el("div", "model-card");
      card.appendChild(el("div", "m-name", m.name || m.key));
      card.appendChild(el("div", "m-meta",
        `${m.category || "-"} · ${m.size || "-"} · 显存 ${m.vram || "-"} · ${m.weights || ""}`));
      const stage = m.stage || "";
      const cls = stage.includes("已接入") ? "tag-ready"
        : stage.includes("规划") ? "tag-planned" : "tag-optional";
      card.appendChild(el("span", "tag " + cls, stage));
      box.appendChild(card);
    });
  }

  $("md-refresh").addEventListener("click", () => { $("md-cat").value = ""; loadModels(); });

  // ============================================================ 素材库
  async function loadLibrary() {
    try {
      const r = await api("/api/library");
      renderTable("lib-table",
        ["类型", "名称", "大小", "修改时间", "路径"], r.rows || []);
      const gal = $("lib-gallery");
      gal.innerHTML = "";
      // 与结果区共用同一个卡片渲染（图片/视频 + 名称 + 打开/下载操作行）
      (r.items || []).forEach((f) => gal.appendChild(galleryFigure(f)));
      $("lib-msg").textContent = `共 ${(r.rows || []).length} 项`;
      const roots = await api("/api/library/roots");
      $("lib-roots").textContent = "扫描目录：" + (roots.roots || []).join("　");
    } catch (e) { $("lib-msg").textContent = "加载失败：" + e.message; }
  }

  $("lib-refresh").addEventListener("click", loadLibrary);
  $("lib-open").addEventListener("click", () => api("/api/output-dir").catch(() => {}));
  $("lib-cleanup").addEventListener("click", async () => {
    try {
      const r = await api("/api/tools/cleanup", { method: "POST", body: {} });
      $("lib-msg").textContent = r.message || "已清理";
    } catch (e) { $("lib-msg").textContent = "清理失败：" + e.message; }
  });

  // ============================================================ 工具箱
  function toolButton(btnId, name, outId, fields) {
    $(btnId).addEventListener("click", async () => {
      const body = {};
      (fields || []).forEach((f) => {
        const node = $(f.id);
        body[f.key] = f.num ? Number(node.value) : node.value;
      });
      $(outId).textContent = "运行中…";
      try {
        const r = await api(`/api/tools/${name}`, { method: "POST", body });
        $(outId).textContent = r.message || "完成";
      } catch (e) { $(outId).textContent = "失败：" + e.message; }
    });
  }
  toolButton("tl-diag", "diagnose", "tl-diag-out", []);
  toolButton("gif-run", "gif", "gif-msg", [
    { id: "gif-video", key: "video" },
    { id: "gif-fps", key: "fps", num: true },
    { id: "gif-scale", key: "scale", num: true },
  ]);
  toolButton("fr-run", "frames", "fr-msg", [
    { id: "fr-video", key: "video" },
    { id: "fr-fps", key: "fps", num: true },
    { id: "fr-dir", key: "out_dir" },
  ]);
  toolButton("rn-run", "rename", "rn-msg", [
    { id: "rn-pattern", key: "pattern" },
    { id: "rn-start", key: "start", num: true },
  ]);

  // ============================================================ 设置
  async function loadSettings() {
    try {
      const r = await api("/api/settings");
      const v = r.values || {};
      $("set-out").value = v.output_root || "";
      $("set-date").checked = !!v.date_folder;
      $("set-skip").checked = !!v.skip_existing;
      $("set-keep").checked = !!v.keep_frames;
      $("set-beep").checked = !!v.beep_on_done;
      $("set-face").checked = !!v.face_enhance;
      $("set-half").checked = !!v.half;
      $("set-interp").checked = !!v.interpolate;
      $("set-skipsim").checked = v.video_skip_similar !== false;
      $("set-encoder").value = v.video_encoder || "auto";
      $("set-crf").value = v.video_crf ?? 18;
      $("set-tile").value = v.tile ?? 0;
      $("set-facework").value = v.face_work_side ?? 4096;
      $("set-maxsize").value = v.max_output_size ?? 4096;
      $("set-vidmax").value = v.video_max_side ?? 1920;
      $("set-lan").checked = !!v.lan_access;
      $("set-https").checked = !!v.https_enabled;
      loadLanInfo();
      loadHttpsInfo();
    } catch (e) { $("set-msg").textContent = "读取设置失败：" + e.message; }
  }

  async function loadLanInfo() {
    try {
      const r = await api("/api/lan-info");
      if (r.enabled && r.listening_lan && r.url) {
        $("set-lan-addr").textContent =
          `手机访问地址：${r.url}\n${r.note}`;
      } else if (r.enabled) {
        $("set-lan-addr").textContent =
          `已勾选，但当前进程仍在监听 ${r.host || "127.0.0.1"}。\n请重启程序后生效。`;
      } else {
        $("set-lan-addr").textContent = r.note;
      }
    } catch {
      $("set-lan-addr").textContent = "读取失败";
    }
  }

  $("set-lan-info").addEventListener("click", loadLanInfo);

  // ---- HTTPS（v1.5.0）----
  // 状态分「配置想要什么」与「进程实际提供了什么」：开关改完要重启才生效，
  // 界面必须如实显示这层差别，否则用户会以为勾一下就通了。
  async function loadHttpsInfo() {
    const box = $("set-https-addr");
    try {
      const r = await api("/api/https-info");
      const cert = r.cert || {};
      const lines = [];
      if (r.running) {
        lines.push(`HTTPS 已生效：${r.https_url || "（未识别到局域网 IP）"}`);
      } else if (r.desired) {
        lines.push("已勾选但尚未生效 —— 重启程序后开始监听 HTTPS。");
      } else {
        lines.push("未启用：手机端的一键「保存到相册 / 分享」需要 HTTPS。");
      }
      if (cert.exists) {
        lines.push(`证书：${cert.fingerprint_short || "?"}　`
          + `有效期至 ${cert.not_after || "?"}（还剩 ${cert.days_left} 天）`);
        lines.push(cert.ok ? `状态：${cert.reason}`
          : `⚠ ${cert.reason}`);
      } else {
        lines.push(`证书：${cert.reason}`);
      }
      if (r.desired && !r.lan_access) {
        lines.push("⚠ 还没开「允许局域网访问」—— 手机连不上，请一并勾选。");
      }
      lines.push(`安装地址（手机用 http 打开即可下载证书）：${r.http_url || "（未识别到局域网 IP）"}`);
      lines.push(`装好证书后改用：${r.https_url || "（重启后显示）"}`);
      box.textContent = lines.join("\n");
    } catch (e) {
      box.textContent = "读取 HTTPS 状态失败：" + e.message;
    }
  }

  $("set-https-info").addEventListener("click", loadHttpsInfo);
  // 「下载证书」是普通链接（href 指向 /api/https/cert），不拦点击 ——
  // 手机上由系统处理「下载描述文件」的流程，比 window.open 可靠。
  $("set-https-regen").addEventListener("click", () => {
    // 全工程禁原生二次确认框与提示框（verify_w4 门禁），走 collector.js 的
    // 同一套内联二次确认：隐藏原按钮 + 原位插确认条。
    const btn = $("set-https-regen");
    const run = async () => {
      try {
        const r = await api("/api/https/regenerate", { method: "POST" });
        toast(r.message || "证书已重新生成");
        loadHttpsInfo();
      } catch (e) { toast("生成失败：" + e.message); }
    };
    const ic = window.TKCollector && window.TKCollector.inlineConfirm;
    if (ic) ic(btn, run, "重新生成证书？");
    else run();
  });

  $("set-save").addEventListener("click", async () => {
    try {
      const r = await api("/api/settings", {
        method: "POST",
        body: {
          output_root: $("set-out").value.trim(),
          date_folder: $("set-date").checked,
          skip_existing: $("set-skip").checked,
          keep_frames: $("set-keep").checked,
          beep_on_done: $("set-beep").checked,
          face_enhance: $("set-face").checked,
          half: $("set-half").checked,
          interpolate: $("set-interp").checked,
          video_skip_similar: $("set-skipsim").checked,
          video_encoder: $("set-encoder").value,
          video_crf: Number($("set-crf").value || 18),
          tile: Number($("set-tile").value || 0),
          face_work_side: Number($("set-facework").value || 4096),
          max_output_size: Number($("set-maxsize").value || 4096),
          video_max_side: Number($("set-vidmax").value || 1920),
          lan_access: $("set-lan").checked,
          https_enabled: $("set-https").checked,
        },
      });
      $("set-msg").textContent = r.message || "已保存";
      toast("设置已保存");
      loadLanInfo();
      loadHttpsInfo();
    } catch (e) { $("set-msg").textContent = "保存失败：" + e.message; }
  });

  $("set-open").addEventListener("click", () => api("/api/output-dir").catch(() => {}));

  // ============================================================ 关于
  async function loadAbout() {
    try {
      const e = await api("/api/env");
      $("about-env").textContent = e.text || "";
    } catch { /* 忽略 */ }
    try {
      const c = await api("/api/changelog");
      $("about-changelog").textContent = c.markdown || "";
    } catch { /* 忽略 */ }
  }

  $("about-open").addEventListener("click", () => api("/api/output-dir").catch(() => {}));
  $("about-quit").addEventListener("click", async () => {
    try { await api("/api/shutdown", { method: "POST" }); } catch { /* 正在退出 */ }
    document.body.innerHTML =
      "<div style='padding:48px;font-family:Segoe UI;color:#f9fafb;background:#0e0e13;height:100vh'>" +
      "<h2>程序正在退出…</h2><p style='color:#9ca3af'>可以关闭此窗口。</p></div>";
  });

  // ============================================================ 初始化
  async function boot() {
    try {
      const p = await api("/api/ping");
      $("ver").textContent = "v" + p.version;
      $("svc-pill").classList.add("ok");
      $("svc-pill").textContent = "服务就绪";
      document.title = `${p.title} v${p.version}`;
    } catch {
      $("svc-pill").textContent = "服务未连接";
      return;
    }
    try {
      const env = await api("/api/health");
      // 顶栏只放短标签（显卡），完整环境信息在「关于」页
      $("env-pill").textContent = env.gpu || "引擎就绪";
      $("env-pill").title = env.env || "";
    } catch { /* 非关键 */ }

    // 高清下拉项
    try {
      const m = await api("/api/hd/models");
      hdMeta = { models: m.models || [], scales: m.scales || [] };

      // 关键修复（v4.0）：option 的 value 必须是「模型 key」，
      // 显示文本才是中文名。此前把 (名称,key) 元组整个塞进 value，
      // 提交上来的变成了 "Real-ESRGAN 通用轻量 x4（快速）,realesr-general-x4v3"，
      // 后端 MODELS[key] 直接 KeyError —— 高清处理全部失败。
      const fillOpts = (sel, opts, preferred) => {
        sel.innerHTML = "";
        (opts || []).forEach((o) => {
          const opt = document.createElement("option");
          opt.value = o.value;
          opt.textContent = o.label || o.value;
          if (o.value === preferred) opt.selected = true;
          sel.appendChild(opt);
        });
      };
      const modelOpts = m.models || [];
      const fallback = (modelOpts.find((x) => x.value === "realesr-general-x4v3")
        || modelOpts[0] || {}).value;

      // v4.0 修复：把「上次用的参数」真的填回表单。
      // services._remember_choices() 一直在存 image_model / video_model /
      // image_scale / video_scale / video_sharp / video_max_side / face_enhance /
      // interpolate，但界面从来没读过它们 —— 重启后表单还是写死的默认值
      // （「存了不生效」，用户上次选的模型/档位白选）。
      // 数据来源：GET /api/settings 的 raw（就是完整配置文件）。
      let pref = {};
      try { pref = (await api("/api/settings")).raw || {}; } catch (_) { pref = {}; }
      const pickModel = (want) =>
        (modelOpts.some((x) => x.value === want) ? want : fallback);
      fillOpts($("hd-img-model"), modelOpts, pickModel(pref.image_model));
      fillOpts($("hd-vid-model"), modelOpts, pickModel(pref.video_model));

      // 档位：配置里存的是 2 / 4（数字），下拉项是「2x」「4x」「自动（推荐）」
      const scaleLabel = (v) =>
        (Number(v) === 2 ? "2x" : Number(v) === 4 ? "4x" : "自动（推荐）");
      fillOpts($("hd-scale"), m.scales || [],
        scaleLabel(pref.image_scale != null ? pref.image_scale : pref.video_scale));
      if (m.sides && m.sides.length) {
        // 视频档位：存的是 video_sharp（是否只锐化）+ video_max_side（数字）
        const wantSide = pref.video_sharp === true
          ? "sharp" : String(pref.video_max_side != null ? pref.video_max_side : "1920");
        fillOpts($("hd-side"), m.sides,
          (m.sides.some((o) => String(o.value) === wantSide) ? wantSide : "1920"));
      }
      if (typeof pref.face_enhance === "boolean") {
        $("hd-face").checked = pref.face_enhance;
      }
      if (typeof pref.interpolate === "boolean") {
        $("hd-interp").checked = pref.interpolate;
      }
      // 输出长边上限的 option value 是「4096（推荐）」这种带说明的文字，
      // 按前缀匹配，避免把中文说明写死在 JS 里。
      const maxOpt = Array.from($("hd-max").options).find(
        (o) => o.value.startsWith(String(pref.max_output_size != null
          ? pref.max_output_size : "")));
      if (maxOpt) $("hd-max").value = maxOpt.value;
      renderPresets(m.presets || []);
    } catch (e) {
      writeLog("hd-log", "模型列表加载失败：" + e.message);
    }

    // 模式切换决定预设按图片还是视频套用
    $("hd-mode").addEventListener("change", (e) => {
      hdPresetKind = e.target.value === "video" ? "video" : "image";
    });

    setupDropzone();
    renderPicked();
    refreshTaskBar();
    pageLoaded.settings = false;   // 首次点开设置时再加载
  }

  // 供 collector.js 使用的公共能力（同一份 DOM / 同一套请求与提示逻辑）
  window.TKStudio = {
    $, el, esc, fmtSize, api, toast, setDisabled, setBar, writeLog, clearLog,
    notify: Notify,
    notifyCompletion,
    watchJob, jobAction, activatePage, PAGE_LOADERS, pageLoaded, PAGES,
    tabSummary: (t) => { const n = $("task-summary"); if (n) n.textContent = t; },
  };

  boot();
})();
