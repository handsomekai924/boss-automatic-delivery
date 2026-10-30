/**
 * WS 桥：一条 `/api/ws` 连接 → `applyWsEvent` → store（docs 5.5）。
 *
 * 断线降级：`ws.status` 变 `"down"` 就起 60s 慢轮询兜底（沿用 legacy `app.js` 行为），
 * 连回来即停。挂载一次就够，全局只此一条连接。
 */

import { useEffect } from "react";

import { api } from "../api/client";
import { useTaskStore, type TaskSnapshot } from "../stores/taskStore";
import * as ws from "./client";
import { applyWsEvent } from "./dispatch";

/** 断线兜底轮询间隔（毫秒）。 */
const FALLBACK_POLL_MS = 60_000;

async function pollTasks(): Promise<void> {
  try {
    const body = await api.get<{ tasks?: TaskSnapshot[] }>("/api/tasks");
    useTaskStore.getState().replaceAll(Array.isArray(body.tasks) ? body.tasks : []);
  } catch {
    /* 连不上就等下一轮，不打断界面 */
  }
}

export function WsBridge(): null {
  useEffect(() => {
    const off = ws.on("*", applyWsEvent);
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const stopPoll = (): void => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };
    const startPoll = (): void => {
      if (pollTimer) return;
      void pollTasks();
      pollTimer = setInterval(() => void pollTasks(), FALLBACK_POLL_MS);
    };

    const offStatus = ws.on("ws.status", (msg) => {
      const status = String((msg.data as Record<string, unknown>).status ?? "");
      if (status === "down") startPoll();
      else stopPoll();
    });

    void ws.connect();

    return () => {
      off();
      offStatus();
      stopPoll();
      ws.disconnect();
    };
  }, []);

  return null;
}
