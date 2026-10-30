/**
 * 统一 WebSocket 客户端：全应用只开一条 `/api/ws`（docs 5.5）。
 *
 * 连接后服务端先推 `hello` 快照，之后按事件推送。断线自动重连；
 * 连不上时 `status` 为 `"down"`，`wsBridge` 靠合成帧 `ws.status` 起停降级轮询。
 * 与 legacy `static/legacy/js/ws.js` 同一套行为，升级成带信封类型的 TS。
 */

/** `/api/ws` 事件信封：每帧都带 `site`（空 = 共用事件）。 */
export interface WsEnvelope {
  type: string;
  ts: number;
  task_type: string;
  task_id: string;
  site: string;
  data: Record<string, unknown>;
}

export type WsStatus = "connecting" | "open" | "down";

export type WsHandler = (msg: WsEnvelope) => void;

const listeners = new Map<string, Set<WsHandler>>();
const anyListeners = new Set<WsHandler>();

let socket: WebSocket | null = null;
let status: WsStatus = "connecting";
let retryMs = 1000;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let pingTimer: ReturnType<typeof setInterval> | null = null;

function dispatch(msg: WsEnvelope): void {
  if (msg.type === "pong") return;
  for (const h of anyListeners) {
    try {
      h(msg);
    } catch (e) {
      console.error("[ws] handler error", e);
    }
  }
  const set = listeners.get(msg.type);
  if (set) {
    for (const h of set) {
      try {
        h(msg);
      } catch (e) {
        console.error("[ws] handler error", e);
      }
    }
  }
}

function setStatus(next: WsStatus): void {
  if (status === next) return;
  status = next;
  // 合成帧：各面板靠它启动/停掉降级轮询（沿用 legacy 约定）。
  dispatch({ type: "ws.status", ts: Date.now() / 1000, task_type: "", task_id: "", site: "", data: { status: next } });
}

/**
 * 订阅某类消息。
 *
 * @param type 消息 type，`"*"` 表示全部
 * @param handler 回调
 * @returns 退订函数
 */
export function on(type: string, handler: WsHandler): () => void {
  if (type === "*") {
    anyListeners.add(handler);
    return () => anyListeners.delete(handler);
  }
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type)!.add(handler);
  return () => {
    listeners.get(type)?.delete(handler);
  };
}

/** @returns 连接状态：connecting / open / down */
export function getStatus(): WsStatus {
  return status;
}

/** 发一条客户端消息（如 ping / resync）；没连上就静默丢弃。 */
export function send(payload: Record<string, unknown>): void {
  if (socket && socket.readyState === WebSocket.OPEN) {
    try {
      socket.send(JSON.stringify(payload));
    } catch {
      /* 忽略 */
    }
  }
}

/** 请求服务端重推全量快照。 */
export function resync(): void {
  send({ type: "resync" });
}

function scheduleReconnect(): void {
  if (retryTimer) return;
  console.warn(`[ws] ${retryMs}ms 后重连`);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void connect();
    retryMs = Math.min(retryMs * 2, 15000);
  }, retryMs);
}

function startPing(): void {
  stopPing();
  pingTimer = setInterval(() => send({ type: "ping" }), 25000);
}

function stopPing(): void {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

/**
 * 建立连接（幂等）。
 *
 * @returns 连上或失败后都 resolve，调用方不必等
 */
export async function connect(): Promise<void> {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  status = "connecting";
  return new Promise((resolve) => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let settled = false;
    // 旧 socket 无论半死不活都先补一刀关掉，不留服务端挂着的僵尸连接。
    if (socket) {
      try {
        socket.close();
      } catch {
        /* 忽略 */
      }
      socket = null;
    }
    try {
      socket = new WebSocket(`${proto}://${location.host}/api/ws`);
    } catch {
      setStatus("down");
      scheduleReconnect();
      resolve();
      return;
    }
    socket.onopen = () => {
      console.info("[ws] 已连接");
      setStatus("open");
      retryMs = 1000;
      startPing();
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    socket.onmessage = (e) => {
      let msg: WsEnvelope | null = null;
      try {
        msg = JSON.parse(String(e.data)) as WsEnvelope;
      } catch {
        return;
      }
      dispatch(msg);
    };
    const down = (): void => {
      stopPing();
      setStatus("down");
      scheduleReconnect();
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    socket.onerror = () => {
      console.warn("[ws] 连接出错");
      down();
    };
    socket.onclose = (e) => {
      // code/reason 是排查「反复重连」的关键现场：1006=异常断开，1000=正常关。
      console.warn(`[ws] 连接关闭 code=${e.code} reason=${e.reason || "-"}`);
      down();
    };
    setTimeout(() => {
      if (!settled) {
        settled = true;
        resolve();
      }
    }, 3000);
  });
}

/** 主动断开（测试用）。 */
export function disconnect(): void {
  stopPing();
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  try {
    socket?.close();
  } catch {
    /* 忽略 */
  }
  socket = null;
  setStatus("down");
}

/** 仅供测试：重置模块级单例状态。 */
export function __reset(): void {
  listeners.clear();
  anyListeners.clear();
  socket = null;
  status = "connecting";
  retryMs = 1000;
  retryTimer = null;
  pingTimer = null;
}

export default { on, connect, disconnect, send, resync, getStatus };
