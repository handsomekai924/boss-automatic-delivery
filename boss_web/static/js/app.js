/** 星舰控制台入口：hash 路由 + 舰桥状态 */

import { api } from "./api.js";
import { toast, bindThemeToggle, initTheme } from "./ui.js";
import { maybePromptUpdate, startUpdateWatcher } from "./update.js";
import { renderHome } from "./views/home.js";
import { renderLogin } from "./views/login.js";
import { renderJobs } from "./views/jobs.js";
import { renderResume } from "./views/resume.js";
import { renderMatch } from "./views/match.js";
import { renderInterview } from "./views/interview.js";
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

/** 刷新舰桥会话信号球，并广播 `boss:session` 给首页卡片。 */
export async function refreshBridge() {
  const dot = document.getElementById("signal-session");
  const label = document.getElementById("signal-session-label");
  try {
    const st = await api.get("/api/auth/status");
    window.dispatchEvent(new CustomEvent("boss:session", { detail: st }));
    if (st.logged_in) {
      dot.className = "signal ok";
      label.textContent = st.phone_masked || "已登录";
    } else {
      dot.className = "signal warn";
      label.textContent = "未登录";
    }
    if (st.task && ["pending", "sending_sms", "need_code", "need_slider", "logging_in"].includes(st.task.status)) {
      dot.className = "signal busy";
      label.textContent = "登录进行中";
    }
  } catch {
    dot.className = "signal bad";
    label.textContent = "离线";
  }
}

window.addEventListener("hashchange", navigate);
window.addEventListener("load", () => {
  initTheme();
  bindThemeToggle(document.getElementById("theme-toggle"));
  navigate();
  refreshBridge();
  setInterval(refreshBridge, 8000);
  maybePromptUpdate().catch(() => { });
  startUpdateWatcher();
});

export { toast, api };
