/**
 * 任务观察器：把「1.5s 轮询 status」换成「统一 WS 事件驱动」。
 *
 * 用法（各业务页替换原 setInterval 轮询）：
 *
 *   const stop = watchTask(taskId, fetchStatus, {
 *     onStatus: (payload) => paint(payload),
 *     isDone: (payload) => ["done", "error", "cancelled"].includes(payload.status),
 *   });
 *   // 离开页面 / 任务结束时 stop()
 *
 * WS 任务帧自带业务快照（`data.raw` 与各页原 HTTP status 同形状），收帧直接画，
 * **不再回源打 HTTP**——事件密集时回源会在服务端日志里刷出一串 GET，和轮询没两样。
 * 只有首帧与 WS 断线兜底才走 `fetchStatus`；断线回落 5s 轮询，连上即停。
 */

import * as ws from "./ws.js";

/** 降级轮询间隔（毫秒）——仅 WS 断线时启用。 */
const FALLBACK_POLL_MS = 5000;
/** 进度帧合帧窗口（毫秒）：窗口内只画最后一帧；终态不排队，立刻画。 */
const COALESCE_MS = 200;

/**
 * WS 帧 → 业务快照。
 *
 * `data.raw` 是业务对象 `snapshot()` 的原样副本，和各页原 HTTP status 同形状；
 * 规范化帧补上 `done` / `total` / `current` 的兜底计算，业务字段盖在上面。
 *
 * @param {Object} m WS 消息（或任何带 `data` 的包装）
 * @returns {?Object} 业务快照；空帧回 `null`
 */
export function payloadOf(m) {
  const d = m && m.data;
  if (!d || typeof d !== "object") return null;
  const raw = d.raw && typeof d.raw === "object" ? d.raw : {};
  return { ...d, ...raw };
}

/**
 * 开始观察一个任务。
 *
 * @param {string} taskId 任务 id
 * @param {() => Promise<Object>} fetchStatus 拉业务 status 的函数（首帧 / 断线兜底用）
 * @param {Object} handlers
 * @param {(payload: Object) => void} handlers.onStatus 每次拿到 status 回调
 * @param {(payload: Object) => boolean} [handlers.isDone] 判断任务是否已结束
 * @param {string} [handlers.taskType] 任务类型提示，用于更快过滤事件
 * @returns {() => void} 停止观察
 */
export function watchTask(taskId, fetchStatus, { onStatus, isDone, taskType = "" } = {}) {
  let stopped = false;
  /** @type {?ReturnType<typeof setInterval>} 降级轮询 */
  let fallbackTimer = null;
  /** @type {?ReturnType<typeof setTimeout>} 进度帧合帧定时器 */
  let coalesceTimer = null;
  /** @type {?Object} 合帧窗口内攒下的最新快照 */
  let pending = null;

  /** 画一帧；判定结束就停。 */
  function paint(payload) {
    if (stopped || payload == null) return;
    try {
      onStatus(payload);
    } catch (e) {
      console.error("[task_watch] onStatus error", e);
    }
    if (isDone && isDone(payload)) stop();
  }

  /**
   * 收一帧快照。终态 / 显式 immediate 立刻画；进度帧按窗口合帧，
   * 只保证画到最新一帧，全程不回源 HTTP。
   *
   * @param {?Object} payload 业务快照
   * @param {{immediate?: boolean}} [opts] immediate 时跳过合帧
   */
  function push(payload, { immediate = false } = {}) {
    if (stopped || payload == null) return;
    const terminal = ["done", "error", "cancelled"].includes(String(payload.status || ""));
    if (immediate || terminal) {
      if (coalesceTimer) {
        clearTimeout(coalesceTimer);
        coalesceTimer = null;
      }
      pending = null;
      paint(payload);
      return;
    }
    pending = payload;
    if (coalesceTimer) return;
    coalesceTimer = setTimeout(() => {
      coalesceTimer = null;
      const p = pending;
      pending = null;
      if (p != null) paint(p);
    }, COALESCE_MS);
  }

  /** HTTP 拉一次业务 status——只给首帧与 WS 断线兜底用。 */
  async function tick() {
    if (stopped) return;
    let payload = null;
    try {
      payload = await fetchStatus();
    } catch {
      return;
    }
    if (stopped || payload == null) return;
    push(payload, { immediate: true });
  }

  /** WS 断线兜底轮询；连上就停。 */
  function ensureFallback() {
    if (ws.getStatus() === "open") {
      if (fallbackTimer) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
      return;
    }
    if (!fallbackTimer) {
      fallbackTimer = setInterval(() => {
        if (ws.getStatus() === "open") {
          clearInterval(fallbackTimer);
          fallbackTimer = null;
          return;
        }
        tick();
      }, FALLBACK_POLL_MS);
    }
  }

  /** 事件过滤：只关心本任务。 */
  function onTaskEvent(m) {
    if (stopped) return;
    if (taskType && m.task_type && m.task_type !== taskType) return;
    if (m.task_id && m.task_id !== taskId) return;
    push(payloadOf(m));
  }

  /**
   * hello / task.snapshot 是全量快照：从中挑出本任务补一帧，
   * 覆盖「订阅前事件就发完了」或「断线重连」的空窗，不必回源 HTTP。
   */
  function onSnapshot(m) {
    ensureFallback();
    if (stopped) return;
    const list =
      (m.data && m.data.tasks && m.data.tasks.tasks) || (m.data && m.data.tasks) || [];
    const hit = list.find((t) => t && t.task_id === taskId);
    if (hit) push(payloadOf({ data: hit }), { immediate: true });
  }

  const offs = [
    ws.on("task.progress", onTaskEvent),
    ws.on("task.finished", onTaskEvent),
    ws.on("hello", onSnapshot),
    ws.on("task.snapshot", onSnapshot),
    ws.on("ws.status", () => ensureFallback()),
    ws.on("session.changed", () => ensureFallback()),
  ];

  ws.connect().then(() => {
    if (stopped) return;
    ensureFallback();
  });
  tick();

  function stop() {
    if (stopped) return;
    stopped = true;
    offs.forEach((off) => off());
    if (fallbackTimer) {
      clearInterval(fallbackTimer);
      fallbackTimer = null;
    }
    if (coalesceTimer) {
      clearTimeout(coalesceTimer);
      coalesceTimer = null;
    }
    pending = null;
  }
  return stop;
}

export default { watchTask, payloadOf };
