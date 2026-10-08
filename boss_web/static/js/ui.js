/** 通用 UI 工具：toast / modal / 小组件 */

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
    if (onClose) onClose();
  };
  box.querySelector(".modal-close").addEventListener("click", close);
  back.addEventListener("click", (e) => {
    if (e.target === back) close();
  });
  back.appendChild(box);
  root.appendChild(back);
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
