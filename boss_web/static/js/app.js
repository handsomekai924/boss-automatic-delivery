/** 星舰控制台入口：hash 路由 + 舰桥状态 */

import { api } from "./api.js";
import { toast, bindThemeToggle, initTheme } from "./ui.js";
import { maybePromptUpdate, startUpdateWatcher } from "./update.js";
import * as ws from "./ws.js";
import { renderHome } from "./views/home.js";
import { renderLogin } from "./views/login.js";
import { renderJobs } from "./views/jobs.js";
import { renderResume } from "./views/resume.js";
import { renderMatch } from "./views/match.js";
import { renderInterview } from "./views/interview.js";
import { renderTasks } from "./views/tasks.js";
import { renderSettings } from "./views/settings.js";

/** hash 路径 → 视图渲染函数。 */
const routes = {
  "": renderHome,
  "/": renderHome,
  "/login": renderLogin,
  "/jobs": renderJobs,
  "/resume": renderResume,
  "/match": renderMatch,
  "/interview": renderInterview,
  "/tasks": renderTasks,
  "/settings": renderSettings,
  "/llm": renderSettings,
};

/** 上一视图的清理函数，换页前调用。 */
let disposePrev = null;

/** 从 `location.hash` 取路径（去掉 query）。 */
function currentPath() {
  const h = location.hash.replace(/^#/, "") || "/";
  return h.split("?")[0];
}

/** 路径前缀 → 舰桥阶段 key；不在流程里返回 ""。 */
function stageOf(path) {
  if (path.startsWith("/login")) return "login";
  if (path.startsWith("/jobs")) return "jobs";
  if (path.startsWith("/resume")) return "resume";
  if (path.startsWith("/match")) return "match";
  if (path.startsWith("/interview")) return "interview";
  if (path.startsWith("/tasks")) return "tasks";
  if (path.startsWith("/settings") || path.startsWith("/llm")) return "settings";
  return "";
}

/** 把 `#stages` 里当前阶段的链接标成 `.active`。 */
function paintStages(path) {
  const active = stageOf(path);
  document.querySelectorAll("#stages a").forEach((a) => {
    a.classList.toggle("active", a.dataset.stage === active);
  });
}

/** 按 hash 渲染当前视图，换页前先调用上一视图的 dispose。 */
export async function navigate() {
  const path = currentPath();
  paintStages(path);
  const view = document.getElementById("view");
  if (typeof disposePrev === "function") {
    try { disposePrev(); } catch { }
    disposePrev = null;
  }
  view.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>正在接入…</p></div>`;
  const render = routes[path] || renderHome;
  try {
    disposePrev = await render(view);
  } catch (err) {
    console.error(err);
    view.innerHTML = `<div class="empty"><div class="empty-icon">⚠</div><p>${err.message || err}</p></div>`;
  }
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
}

/** 刷新舰桥会话信号球，并广播 `boss:session` 给首页卡片（首屏 / WS 降级用）。 */
export async function refreshBridge() {
  try {
    const st = await api.get("/api/auth/status");
    window.dispatchEvent(new CustomEvent("boss:session", { detail: st }));
    paintSession(st);
  } catch {
    const dot = document.getElementById("signal-session");
    const label = document.getElementById("signal-session-label");
    if (dot) dot.className = "signal bad";
    if (label) label.textContent = "离线";
  }
}

/** 顶栏任务角标：task_id → 是否未结束（WS 事件维护）。 */
const liveTasks = new Map();

/** 未结束状态集合，与后端 `task_runtime.UNFINISHED` 对齐。 */
const UNFINISHED = new Set([
  "pending", "running", "waiting_human", "sending_sms",
  "need_code", "need_slider", "logging_in", "need_scan",
]);

/** 按 `liveTasks` 重画顶栏在跑角标。 */
function paintTaskBadge() {
  const badge = document.getElementById("bridge-tasks-badge");
  if (!badge) return;
  let n = 0;
  for (const st of liveTasks.values()) if (UNFINISHED.has(st)) n += 1;
  badge.textContent = String(n);
  badge.classList.toggle("hidden", n === 0);
}

/** 合入一条任务快照（hello / task.* 通用）。 */
function ingestTask(snap) {
  if (!snap || !snap.task_id) return;
  liveTasks.set(snap.task_id, snap.status || "running");
  paintTaskBadge();
}

/** 建统一 WS：会话信号球、顶栏角标都吃它的推送。 */
function startUnifiedWs() {
  ws.on("hello", (m) => {
    const tasks = (m.data && m.data.tasks && m.data.tasks.tasks) || [];
    liveTasks.clear();
    tasks.forEach(ingestTask);
    const session = (m.data && m.data.session) || {};
    window.dispatchEvent(new CustomEvent("boss:session", { detail: session }));
    paintSession(session);
  });
  ws.on("task.snapshot", (m) => {
    const tasks = (m.data && m.data.tasks && m.data.tasks.tasks) || m.data?.tasks || [];
    liveTasks.clear();
    (Array.isArray(tasks) ? tasks : []).forEach(ingestTask);
  });
  ws.on("task.progress", (m) => ingestTask(m.data));
  ws.on("task.finished", (m) => ingestTask(m.data));
  ws.on("task.removed", (m) => {
    if (m.task_id) {
      liveTasks.delete(m.task_id);
      paintTaskBadge();
    }
  });
  ws.on("session.changed", (m) => {
    const session = (m.data && m.data.session) || m.data || {};
    window.dispatchEvent(new CustomEvent("boss:session", { detail: session }));
    paintSession(session);
  });
  ws.on("ws.status", () => syncBridgeFallback());
  ws.connect();
}

/** 按会话摘要画信号球（WS 推送与 HTTP 兜底共用一套画法）。 */
function paintSession(st) {
  const dot = document.getElementById("signal-session");
  const label = document.getElementById("signal-session-label");
  if (!dot || !label) return;
  if (st && st.logged_in) {
    dot.className = "signal ok";
    label.textContent = st.phone_masked || "已登录";
  } else {
    dot.className = "signal warn";
    label.textContent = "未登录";
  }
  if (st && st.task && ["pending", "sending_sms", "need_code", "need_slider", "logging_in"].includes(st.task.status)) {
    dot.className = "signal busy";
    label.textContent = "登录进行中";
  }
}

/** WS 断线时的会话兜底轮询定时器（连上就停）。 */
let bridgeFallbackTimer = null;

/** WS 断线才起 60s 慢轮询拉会话；正常全靠 hello / `session.changed` 推送。 */
function syncBridgeFallback() {
  if (bridgeFallbackTimer) {
    clearInterval(bridgeFallbackTimer);
    bridgeFallbackTimer = null;
  }
  if (ws.getStatus() !== "open") {
    bridgeFallbackTimer = setInterval(() => {
      if (ws.getStatus() === "open") {
        clearInterval(bridgeFallbackTimer);
        bridgeFallbackTimer = null;
        return;
      }
      refreshBridge();
    }, 60000);
  }
}

window.addEventListener("hashchange", navigate);
window.addEventListener("load", () => {
  initTheme();
  bindThemeToggle(document.getElementById("theme-toggle"));
  navigate();
  refreshBridge();
  startUnifiedWs();
  syncBridgeFallback();
  maybePromptUpdate().catch(() => { });
  startUpdateWatcher();
});

export { toast, api };
