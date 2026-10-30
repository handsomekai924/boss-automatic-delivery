/** 任务中心页：全局任务列表（筛选 / 卡片进度 / 停止 / 重试 / 删除）。 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtClock, renderEvents, taskStateOf, TASK_STATUS } from "../ui.js";
import * as ws from "../ws.js";

/** 类型徽标文案。 */
const TYPE_LABELS = {
  crawl: "爬取",
  desc: "补JD",
  parse: "解析",
  revision: "优化",
  match: "匹配",
  deliver: "发送",
  interview: "面试题",
  login: "登录",
  recheck: "重检",
  update: "更新",
};

/** 状态点配色（卡片左侧圆点）。 */
const DOT_CLS = {
  running: "dot-run",
  pending: "dot-run",
  waiting_human: "dot-warn",
  error: "dot-err",
  done: "dot-ok",
  cancelled: "dot-stop",
};

/**
 * 渲染「任务中心」独立页。
 *
 * 数据源以统一 WS 为主；WS 断了或首屏就用 `GET /api/tasks` 兜底轮询。
 * 支持 `#/tasks?type=match&status=running` 预过滤。
 *
 * @param {HTMLElement} root 挂载点
 * @returns {Promise<() => void>} 离开页面时的清理函数
 */
export async function renderTasks(root) {
  const params = new URLSearchParams((location.hash.split("?")[1] || ""));
  const state = {
    type: params.get("type") || "",
    status: params.get("status") || "",
    q: "",
    /** @type {Map<string, object>} task_id → 规范化快照 */
    tasks: new Map(),
    expanded: new Set(),
  };
  let disposed = false;
  let fallbackTimer = null;

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">任务<span class="grad">中心</span></h1>
        <p class="hero-sub">所有异步任务都在这里：抓取、补 JD、解析、优化、匹配、发送、出题……可以随时停止、重试、清理。</p>
      </div>
      <div class="btn-row">
        <span class="pill" id="tc-counts">在跑 0 · 失败 0</span>
        <button class="btn ghost" id="tc-cancel-all" type="button">全部停止</button>
        <button class="btn ghost" id="tc-clear" type="button">清理已结束</button>
      </div>
    </div>
    <div class="card filter-bar" id="tc-filters">
      <label class="filter-item">类型
        <select class="select" id="tc-type">
          <option value="">全部</option>
          ${Object.entries(TYPE_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
        </select>
      </label>
      <label class="filter-item">状态
        <select class="select" id="tc-status">
          <option value="">全部</option>
          <option value="running">在跑</option>
          <option value="waiting_human">等待人工</option>
          <option value="error">失败</option>
          <option value="finished">已结束</option>
        </select>
      </label>
      <label class="filter-item grow">搜索
        <input class="input" id="tc-q" type="search" placeholder="按标题 / 类型过滤…">
      </label>
    </div>
    <div id="tc-list" class="task-list"></div>
  `;

  const listEl = root.querySelector("#tc-list");
  const countsEl = root.querySelector("#tc-counts");
  const typeSel = root.querySelector("#tc-type");
  const statusSel = root.querySelector("#tc-status");
  const qInput = root.querySelector("#tc-q");

  typeSel.value = state.type;
  statusSel.value = state.status;

  // ── 数据进出 ────────────────────────────────────────────────

  /** 用一份快照列表整体替换本地缓存。 */
  function ingest(snapshots) {
    state.tasks.clear();
    for (const s of snapshots || []) state.tasks.set(s.task_id, s);
    paint();
  }

  /** 合入单个任务快照（task.progress / task.finished）。 */
  function upsert(snap) {
    if (!snap || !snap.task_id) return;
    state.tasks.set(snap.task_id, snap);
    paint();
  }

  /** 从 HTTP 拉全量（首屏 / WS 降级）。 */
  async function loadHttp() {
    try {
      const data = await api.get("/api/tasks");
      if (disposed) return;
      ingest(data.tasks || []);
    } catch {
      /* 降级轮询下一轮再试 */
    }
  }

  /** WS 断线时的 5s 兜底轮询；WS 回来就停。 */
  function syncFallback() {
    if (fallbackTimer) {
      clearInterval(fallbackTimer);
      fallbackTimer = null;
    }
    if (ws.getStatus() !== "open" && !fallbackTimer) {
      fallbackTimer = setInterval(() => {
        if (ws.getStatus() === "open") {
          clearInterval(fallbackTimer);
          fallbackTimer = null;
          return;
        }
        loadHttp();
      }, 5000);
    }
  }

  // ── 渲染 ────────────────────────────────────────────────────

  /** 当前筛选条件下的任务列表（新到旧）。 */
  function visibleTasks() {
    const q = state.q.trim().toLowerCase();
    return [...state.tasks.values()]
      .filter((t) => {
        if (state.type && t.task_type !== state.type) return false;
        if (state.status === "running") return ["running", "pending", "waiting_human", "sending_sms", "need_code", "need_slider", "logging_in", "need_scan"].includes(t.status);
        if (state.status === "waiting_human") return ["waiting_human", "need_code", "need_slider", "need_scan"].includes(t.status);
        if (state.status === "finished") return ["done", "cancelled", "error"].includes(t.status);
        if (state.status && t.status !== state.status) return false;
        if (q && !(`${t.title} ${t.task_type} ${TYPE_LABELS[t.task_type] || ""}`.toLowerCase().includes(q))) return false;
        return true;
      })
      .sort((a, b) => (Number(b.created_at) || 0) - (Number(a.created_at) || 0));
  }

  /** 单张任务卡 HTML。 */
  function cardHtml(t) {
    const key = taskStateOf(t.status);
    const meta = TASK_STATUS[key] || TASK_STATUS.idle;
    const dot = DOT_CLS[t.status] || DOT_CLS[key] || "dot-stop";
    const running = ["running", "pending", "waiting_human", "sending_sms", "need_code", "need_slider", "logging_in", "need_scan"].includes(t.status);
    const pct = t.total > 0 ? Math.round((t.done / t.total) * 100) : null;
    const bar = running && pct == null
      ? `<div class="progress"><i class="indeterminate" style="width:36%"></i></div>`
      : `<div class="progress ${key === "done" ? "ok" : key === "error" ? "bad" : ""}"><i style="width:${Math.max(0, Math.min(100, pct ?? (key === "done" ? 100 : 0)))}%"></i></div>`;
    const counts = t.total > 0 ? `${t.done}/${t.total}` : t.done > 0 ? String(t.done) : "";
    const timeBits = [];
    if (t.created_at) timeBits.push(`开始 ${fmtClock(t.created_at)}`);
    if (t.ended_at) timeBits.push(`结束 ${fmtClock(t.ended_at)}`);

    const actions = [];
    if (running) {
      actions.push(`<button class="btn sm danger" data-act="cancel" data-id="${escapeHtml(t.task_id)}">取消</button>`);
      if (t.task_type === "login") {
        actions.push(`<a class="btn sm ghost" href="#/login">去喂料</a>`);
      }
    } else {
      if (t.status === "error") {
        actions.push(`<button class="btn sm" data-act="retry" data-id="${escapeHtml(t.task_id)}">重试</button>`);
      }
      actions.push(`<button class="btn sm ghost" data-act="delete" data-id="${escapeHtml(t.task_id)}">删除</button>`);
    }
    actions.push(`<button class="btn sm ghost" data-act="expand" data-id="${escapeHtml(t.task_id)}">${state.expanded.has(t.task_id) ? "收起事件" : "事件"}</button>`);

    const expanded = state.expanded.has(t.task_id);
    const events = expanded && (t.events || []).length
      ? `<div class="task-log">${renderEvents(t.events, { limit: 20 })}</div>`
      : expanded
        ? `<div class="task-log"><div class="ev muted">暂无事件流水</div></div>`
        : "";

    return `
      <div class="task-card ${meta.cls}" data-id="${escapeHtml(t.task_id)}">
        <div class="task-card-main">
          <div class="task-card-title">
            <span class="dot ${dot}" aria-hidden="true"></span>
            <span class="pill mono">${escapeHtml(TYPE_LABELS[t.task_type] || t.task_type)}</span>
            <span class="title">${escapeHtml(t.title || t.task_id)}</span>
            <span class="task-status ${meta.cls}">${escapeHtml(meta.label)}</span>
          </div>
          ${bar}
          <div class="task-card-meta">
            ${counts ? `<span class="tm"><span class="tm-k">进度</span><span class="tm-v">${escapeHtml(counts)}</span></span>` : ""}
            ${t.current ? `<span class="tm"><span class="tm-k">当前</span><span class="tm-v accent">${escapeHtml(t.current)}</span></span>` : ""}
            ${timeBits.map((s) => `<span class="tm muted mono">${escapeHtml(s)}</span>`).join("")}
          </div>
          ${t.error ? `<div class="task-card-error">⚠ ${escapeHtml(t.error)}</div>` : ""}
        </div>
        <div class="task-card-actions">${actions.join("")}</div>
        ${events}
      </div>
    `;
  }

  /** 重画列表与计数。 */
  function paint() {
    if (disposed) return;
    const all = [...state.tasks.values()];
    const running = all.filter((t) => ["running", "pending", "waiting_human", "sending_sms", "need_code", "need_slider", "logging_in", "need_scan"].includes(t.status)).length;
    const failed = all.filter((t) => t.status === "error").length;
    countsEl.textContent = `在跑 ${running} · 失败 ${failed}`;
    const list = visibleTasks();
    if (!list.length) {
      listEl.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>${all.length ? "没有符合筛选条件的任务" : "还没有任何任务"}</p></div>`;
      return;
    }
    listEl.innerHTML = list.map(cardHtml).join("");
  }

  // ── 操作 ────────────────────────────────────────────────────

  /** 对一个任务发操作请求并刷新。 */
  async function act(kind, id) {
    try {
      if (kind === "cancel") {
        await api.post(`/api/tasks/${encodeURIComponent(id)}/cancel`);
        toast("已请求停止");
      } else if (kind === "retry") {
        await api.post(`/api/tasks/${encodeURIComponent(id)}/retry`);
        toast("已重新提交");
      } else if (kind === "delete") {
        await api.del(`/api/tasks/${encodeURIComponent(id)}`);
        state.tasks.delete(id);
        paint();
        toast("已删除");
        return;
      }
      await loadHttp();
    } catch (err) {
      toast(err.message || String(err), "bad");
    }
  }

  listEl.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const { act: kind, id } = btn.dataset;
    if (kind === "expand") {
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      paint();
      return;
    }
    act(kind, id);
  });

  root.querySelector("#tc-cancel-all").addEventListener("click", async () => {
    try {
      const r = await api.post("/api/tasks/cancel-all");
      toast(`已停止 ${r.cancelled ?? 0} 个任务`);
      await loadHttp();
    } catch (err) {
      toast(err.message || String(err), "bad");
    }
  });

  root.querySelector("#tc-clear").addEventListener("click", async () => {
    const finished = [...state.tasks.values()].filter((t) => ["done", "cancelled", "error"].includes(t.status));
    if (!finished.length) {
      toast("没有可清理的已结束任务");
      return;
    }
    try {
      for (const t of finished) {
        await api.del(`/api/tasks/${encodeURIComponent(t.task_id)}`);
        state.tasks.delete(t.task_id);
      }
      paint();
      toast(`已清理 ${finished.length} 条`);
    } catch (err) {
      toast(err.message || String(err), "bad");
      await loadHttp();
    }
  });

  typeSel.addEventListener("change", () => {
    state.type = typeSel.value;
    paint();
  });
  statusSel.addEventListener("change", () => {
    state.status = statusSel.value;
    paint();
  });
  qInput.addEventListener("input", () => {
    state.q = qInput.value;
    paint();
  });

  // ── 数据源：WS 优先，HTTP 兜底 ─────────────────────────────

  const offs = [
    ws.on("hello", (m) => ingest((m.data && m.data.tasks && m.data.tasks.tasks) || [])),
    ws.on("task.snapshot", (m) => ingest((m.data && m.data.tasks && m.data.tasks.tasks) || (m.data && m.data.tasks) || [])),
    ws.on("task.progress", (m) => upsert(m.data)),
    ws.on("task.finished", (m) => upsert(m.data)),
    ws.on("task.removed", (m) => {
      if (m.task_id) {
        state.tasks.delete(m.task_id);
        paint();
      }
    }),
    ws.on("ws.status", () => syncFallback()),
  ];

  ws.connect().then(() => {
    if (disposed) return;
    if (ws.getStatus() === "open") ws.resync();
    else loadHttp();
    syncFallback();
  });
  await loadHttp();

  paint();

  return () => {
    disposed = true;
    offs.forEach((off) => off());
    if (fallbackTimer) clearInterval(fallbackTimer);
  };
}
