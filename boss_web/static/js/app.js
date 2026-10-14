/** 星舰控制台入口：hash 路由 + 舰桥状态 */

import { api } from "./api.js";
import { toast, bindThemeToggle, initTheme } from "./ui.js";
import { maybePromptUpdate, startUpdateWatcher } from "./update.js";
import { renderHome } from "./views/home.js";
import { renderLogin } from "./views/login.js";
import { renderJobs } from "./views/jobs.js";
import { renderResume } from "./views/resume.js";
import { renderMatch } from "./views/match.js";
import { renderSettings } from "./views/settings.js";

const routes = {
  "": renderHome,
  "/": renderHome,
  "/login": renderLogin,
  "/jobs": renderJobs,
  "/resume": renderResume,
  "/match": renderMatch,
  "/settings": renderSettings,
  "/llm": renderSettings,
};

let disposePrev = null;

function currentPath() {
  const h = location.hash.replace(/^#/, "") || "/";
  return h.split("?")[0];
}

function stageOf(path) {
  if (path.startsWith("/login")) return "login";
  if (path.startsWith("/jobs")) return "jobs";
  if (path.startsWith("/resume")) return "resume";
  if (path.startsWith("/match")) return "match";
  if (path.startsWith("/settings") || path.startsWith("/llm")) return "settings";
  return "";
}

function paintStages(path) {
  const active = stageOf(path);
  document.querySelectorAll("#stages a").forEach((a) => {
    a.classList.toggle("active", a.dataset.stage === active);
  });
}

export async function navigate() {
  const path = currentPath();
  paintStages(path);
  const view = document.getElementById("view");
  if (typeof disposePrev === "function") {
    try { disposePrev(); } catch { /* ignore */ }
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

/** 舰桥上的会话信号球 */
export async function refreshBridge() {
  const dot = document.getElementById("signal-session");
  const label = document.getElementById("signal-session-label");
  try {
    const st = await api.get("/api/auth/status");
    // 广播一份：首页「会话」卡片跟着舰桥一起刷新。以前它只在渲染时取一次，
    // 首屏那次请求失败/被缓存成「未登录」后就一直停在「离线」，得手动刷新页面。
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
  // 开页面就比一次版本；只弹一次（本页刷新也不再弹第二回）
  maybePromptUpdate().catch(() => { /* 检测失败不打扰 */ });
  // 挂机也别错过新版：轻提示（toast + 系统设置圆点），不重复弹窗
  startUpdateWatcher();
});

export { toast, api };
