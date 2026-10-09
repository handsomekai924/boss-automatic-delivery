/** 通用 UI 工具：toast / modal / 主题 / 长任务面板 */

/* ---------- 主题 ---------- */

export function initTheme() {
  const saved = localStorage.getItem("orbit-theme");
  const theme = saved === "light" || saved === "dark"
    ? saved
    : (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", theme);
  return theme;
}

export function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

export function toggleTheme() {
  const next = currentTheme() === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("orbit-theme", next); } catch { /* private mode */ }
  return next;
}

export function bindThemeToggle(btn) {
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = toggleTheme();
    btn.setAttribute("aria-label", next === "light" ? "切换到黑暗模式" : "切换到光明模式");
  });
}

/* ---------- 基础 ---------- */

export function toast(message, kind = "ok", ms = 3200) {
  const root = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 220);
  }, ms);
}

export function modal({ title, body, wide = false, onClose }) {
  const root = document.getElementById("modal-root");
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.setAttribute("role", "dialog");
  back.setAttribute("aria-modal", "true");
  const box = document.createElement("div");
  box.className = "modal" + (wide ? " wide" : "");
  box.innerHTML = `
    <div class="modal-head">
      <h3>${title || ""}</h3>
      <button class="modal-close" aria-label="关闭">×</button>
    </div>
    <div class="modal-body"></div>
  `;
  const bodyEl = box.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body;
  else if (body) bodyEl.appendChild(body);

  const close = () => {
    back.remove();
    document.removeEventListener("keydown", onKey);
    if (onClose) onClose();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  box.querySelector(".modal-close").addEventListener("click", close);
  back.addEventListener("click", (e) => {
    if (e.target === back) close();
  });
  back.appendChild(box);
  root.appendChild(back);
  box.querySelector(".modal-close").focus();
  return { close, bodyEl, box };
}

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function fmtTime(ts) {
  if (!ts) return "—";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function fmtClock(ts) {
  if (!ts) return "--:--:--";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

/** 秒数 → 「3 分 12 秒」/ 「48 秒」；负数与 NaN → "…" */
export function fmtEta(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "…";
  const s = Math.round(sec);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`;
  const h = Math.floor(m / 60);
  return `${h} 小时 ${m % 60} 分`;
}

export function scoreClass(score) {
  if (score >= 75) return "";
  if (score >= 50) return "mid";
  return "low";
}

export function scoreRing(score, label = "") {
  const s = Math.max(0, Math.min(100, Number(score) || 0));
  return `<div class="score-ring ${scoreClass(s)}" style="--p:${s}">${s}${label ? `<small>${label}</small>` : ""}</div>`;
}

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

/* ---------- 长任务状态 ---------- */

export const TASK_STATUS = {
  idle:    { label: "待命",   cls: "" },
  running: { label: "进行中", cls: "is-run" },
  done:    { label: "已完成", cls: "is-ok" },
  ok:      { label: "已完成", cls: "is-ok" },
  error:   { label: "失败",   cls: "is-err" },
  cancelled: { label: "已停止", cls: "is-stop" },
  pending: { label: "排队中", cls: "is-run" },
};

/** 后端 status 字符串 → TASK_STATUS 的 key */
export function taskStateOf(status) {
  const s = String(status || "").toLowerCase();
  if (["running", "pending", "sending_sms", "sending", "need_code", "need_slider", "logging_in", "working", "process"].includes(s)) return "running";
  if (["done", "ok", "success", "finished"].includes(s)) return "done";
  if (["error", "failed", "fail"].includes(s)) return "error";
  if (["cancelled", "canceled", "stopped", "cancel"].includes(s)) return "cancelled";
  if (["idle", "none", ""].includes(s)) return "idle";
  return "running";
}

/**
 * 统一长任务面板。所有超过 2 秒的操作都用它，保证进度可视化一致。
 *
 *   const task = taskPanel({ title: "批量匹配", stopLabel: "停止" });
 *   host.appendChild(task.el);
 *   task.update({ status: "running", percent: 40, current: "…", counts: [["完成", "12 / 30"]], eta: 88 });
 */
export function taskPanel({ title = "任务", stopLabel = "停止", logTitle = "" } = {}) {
  const root = el(`
    <div class="task" role="status" aria-live="polite">
      <div class="task-head">
        <div class="task-title">${escapeHtml(title)}</div>
        <div class="task-actions">
          <span class="task-status">待命</span>
          <button type="button" class="btn sm danger hidden">${escapeHtml(stopLabel)}</button>
        </div>
      </div>
      <div class="progress" aria-hidden="true"><i style="width:0%"></i></div>
      <div class="task-meta"></div>
      <div class="task-log hidden"></div>
    </div>
  `);

  const statusEl = root.querySelector(".task-status");
  const stopBtn = root.querySelector(".btn");
  const barEl = root.querySelector(".progress");
  const fillEl = root.querySelector(".progress > i");
  const metaEl = root.querySelector(".task-meta");
  const logEl = root.querySelector(".task-log");

  let stopHandler = null;
  stopBtn.addEventListener("click", () => {
    if (stopHandler) stopHandler();
  });

  function setLog(rows) {
    if (!rows || !rows.length) {
      logEl.classList.add("hidden");
      logEl.innerHTML = "";
      return;
    }
    logEl.classList.remove("hidden");
    if (logTitle) {
      logEl.innerHTML = `<div class="ev muted">${escapeHtml(logTitle)}</div>` + rows;
    } else {
      logEl.innerHTML = rows;
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  return {
    el: root,
    onStop(fn) {
      stopHandler = fn;
      return this;
    },
    /**
     * @param {object} p
     * @param {string} p.status  后端状态或 running/done/error/cancelled/idle
     * @param {number} [p.percent] 0-100；缺省且 running 时用 indeterminate
     * @param {string} [p.current] 当前项文案
     * @param {Array<[string,string]>} [p.counts] 计数对
     * @param {string|number} [p.eta] 预估剩余（已格式化或秒）
     * @param {string} [p.error] 错误信息
     * @param {string} [p.log] 事件流水 HTML
     */
    update({ status = "idle", percent, current, counts, eta, error, log } = {}) {
      const key = taskStateOf(status);
      const meta = TASK_STATUS[key] || TASK_STATUS.idle;
      statusEl.textContent = meta.label;
      statusEl.className = `task-status ${meta.cls}`;

      const showStop = key === "running" || key === "pending";
      stopBtn.classList.toggle("hidden", !showStop);

      if (key === "running" && (percent == null || !Number.isFinite(Number(percent)))) {
        fillEl.classList.add("indeterminate");
        fillEl.style.width = "36%";
        barEl.classList.remove("ok", "bad");
      } else {
        fillEl.classList.remove("indeterminate");
        const p = Math.max(0, Math.min(100, Number(percent) || 0));
        fillEl.style.width = `${p}%`;
        barEl.classList.toggle("ok", key === "done");
        barEl.classList.toggle("bad", key === "error");
      }

      const bits = [];
      if (current) {
        bits.push(`<span class="tm"><span class="tm-k">当前</span><span class="tm-v accent">${escapeHtml(current)}</span></span>`);
      }
      for (const [k, v] of counts || []) {
        const cls = /失败|错误|error|bad/i.test(String(k)) ? "bad" :
          (/跳过|skip|warn/i.test(String(k)) ? "warn" :
            (/成功|完成|ok|done/i.test(String(k)) ? "ok" : ""));
        bits.push(`<span class="tm"><span class="tm-k">${escapeHtml(k)}</span><span class="tm-v ${cls}">${escapeHtml(String(v))}</span></span>`);
      }
      if (eta != null && eta !== "") {
        const v = typeof eta === "number" || /^\d+$/.test(String(eta)) ? fmtEta(Number(eta)) : String(eta);
        bits.push(`<span class="tm"><span class="tm-k">剩余</span><span class="tm-v">${escapeHtml(v)}</span></span>`);
      }
      if (error) {
        bits.push(`<span class="tm"><span class="tm-k">错误</span><span class="tm-v bad">${escapeHtml(error)}</span></span>`);
      }
      metaEl.innerHTML = bits.join("");

      if (log != null) setLog(log);
      return this;
    },
  };
}

/** 事件列表 → task-log 的 HTML 行（从两种事件格式提取关键信息） */
export function renderEvents(events, { limit = 30, nameKey = "name" } = {}) {
  return (events || [])
    .slice(-limit)
    .map((ev) => {
      const at = ev.at || ev.ts || ev.time;
      const name = ev[nameKey] || ev.event || ev.type || "event";
      // 两种格式：match/deliver 是 {name, payload:{...}}，crawl/desc 是平铺字段
      const flat = { ...ev };
      if (ev.payload && typeof ev.payload === "object") Object.assign(flat, ev.payload);
      const kind = /error|fail|bad|stopped/i.test(String(name))
        ? "bad"
        : /ok|done|finish|success|sent/i.test(String(name))
          ? "ok"
          : /skip|cancel/i.test(String(name))
            ? "warn"
            : "";
      const label = formatEventLabel(name, flat);
      return `<div class="ev"><time>${escapeHtml(fmtClock(at))}</time><span class="name ${kind}">${escapeHtml(label)}</span></div>`;
    })
    .join("");
}

/** 把事件名 + 字段拼成人看得懂的一行 */
function formatEventLabel(name, f) {
  const n = String(name);
  const job = f.job_name || "";
  const brand = f.brand || f.brand_name || "";
  const who = job ? (brand ? `${job} @ ${brand}` : job) : "";

  // 抓取：翻页（没有 event 字段，靠 page 计数）
  if (f.page != null && (f.inserted != null || f.kept_count != null)) {
    return `第 ${f.page} 页 · 原始 ${f.raw_count ?? "?"} · 入库 +${f.inserted ?? 0} · 更新 ${f.updated ?? 0}`;
  }
  // 抓取：JD 补抓
  if (n === "detail_done") return `✓ ${who}${f.has_desc ? "" : "（无描述）"}`;
  if (n === "detail_error") return `✗ ${who} — ${f.message || "拉取失败"}`;
  if (n === "detail_skipped") return `– ${who}（${f.reason || "已有描述"}）`;
  if (n === "detail_stopped") return `⛔ ${f.reason || "已停"}`;
  // 抓取：生命周期
  if (n === "stoken") return f.message || "正在准备搜索令牌…";
  if (n === "finished") {
    // crawl 有 stopped_reason，deliver/match 有 sent/failed/skipped 或 analysis_id
    if (f.stopped_reason) return `抓取结束（${f.stopped_reason}）`;
    if (f.sent != null || f.failed != null || f.skipped != null)
      return `完成 · 成功 ${f.sent ?? f.ok ?? 0} / 失败 ${f.failed ?? 0} / 跳过 ${f.skipped ?? 0}`;
    return "完成";
  }
  // 补抓 JD
  if (n === "item_done") return `${f.has_desc ? "✓" : "○"} ${who}`;
  if (n === "item_error") return `✗ ${who} — ${f.message || "拉取失败"}`;
  if (n === "item_skipped") return `– ${who}（${f.reason || "已有描述"}）`;
  if (n === "start") return `开始补抓 · 共 ${f.total ?? "?"} 条`;
  if (n === "stopped") return `⛔ ${f.reason || "已停"}`;
  // 匹配
  if (n === "job_start") return `→ 开始匹配 ${who}`;
  if (n === "job_done") return `✓ ${who}${f.score != null ? ` · 评分 ${f.score}` : ""}`;
  // 投递
  if (n === "send_start") return `→ 发送 ${who || f.encrypt_job_id || ""}`;
  if (n === "sent") return `✓ 已发送 ${who || f.encrypt_job_id || ""}`;
  if (n === "skipped") return `– 跳过 ${who || f.encrypt_job_id || ""}（${f.reason || "已发送"}）`;
  // 通用兜底：挑几个有意义的字段
  const bits = [who, f.message || f.reason || ""].filter(Boolean);
  return bits.length ? `${n} · ${bits.join(" · ")}` : n;
}
