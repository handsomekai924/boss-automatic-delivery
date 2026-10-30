/**
 * 纯函数工具（utils/）——对应 5.3「工具」行：
 * formatters（fmtTime/fmtClock/fmtEta）、debounce、escapeHtml→sanitize。
 * 从 legacy ui.js 迁移语义，补单测；组件/业务样式不得自带第二份实现。
 */

/** 秒级时间戳或日期 → 本地化日期时间；无效值 → "—"。 */
export function fmtTime(ts: number | string | Date | null | undefined): string {
  if (!ts) return "—";
  const d = ts instanceof Date ? ts : typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

/** 秒级时间戳或日期 → `HH:MM:SS`；无效值 → "--:--:--"。 */
export function fmtClock(ts: number | string | Date | null | undefined): string {
  if (!ts) return "--:--:--";
  const d = ts instanceof Date ? ts : typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

/** 秒数 → 「3 分 12 秒」/「48 秒」；负数与 NaN → "…"。 */
export function fmtEta(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "…";
  const s = Math.round(sec);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`;
  const h = Math.floor(m / 60);
  return `${h} 小时 ${m % 60} 分`;
}

/** 数字千分位；无效值 → "—"。 */
export function fmtNumber(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString("zh-CN");
}

/** 数字紧凑写法（图表轴刻度）：<1000 原样，≥1000 → 1.2K / 2K；无效值 → "—"。 */
export function fmtCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const trim = (x: number) => (Number.isInteger(x) ? String(x) : x.toFixed(1));
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  if (abs < 1_000_000) return `${trim(n / 1000)}K`;
  return `${trim(n / 1_000_000)}M`;
}

/** 转义 HTML 特殊字符，防注入（React 里用于 dangerouslySetInnerHTML 前的收口）。 */
export function sanitize(str: unknown): string {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 防抖：`ms` 毫秒内重复调用只留最后一次。返回的函数带 cancel()。 */
export function debounce<A extends unknown[]>(
  fn: (...args: A) => void,
  ms = 300,
): ((...args: A) => void) & { cancel: () => void } {
  let t: ReturnType<typeof setTimeout> | undefined;
  const wrapped = (...args: A) => {
    if (t !== undefined) clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => {
    if (t !== undefined) clearTimeout(t);
    t = undefined;
  };
  return wrapped;
}
