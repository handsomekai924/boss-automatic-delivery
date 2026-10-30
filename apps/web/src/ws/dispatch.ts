/**
 * WS 事件 → store 的唯一收口（docs 5.5「WS store」）。
 *
 * 纯函数、不碰 WebSocket，测试直接喂信封；
 * 连接生命周期在 `WsBridge.tsx`，喂进来的是同一条路上的帧。
 */

import { useTaskStore, type TaskSnapshot } from "../stores/taskStore";
import { projectSession, useSessionStore, type AuthFeedKind } from "../stores/sessionStore";
import type { PlatformSession } from "../platform/types";
import type { WsEnvelope } from "./client";

const FEED_KIND: Record<string, AuthFeedKind> = {
  "auth.need_code": "code",
  "auth.need_slider": "slider",
  "auth.need_scan": "scan",
};

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

/** `hello.data.tasks` 两种形状都收：数组，或 `list_snapshots()` 的 `{tasks: []}`。 */
function asSnapshots(v: unknown): TaskSnapshot[] {
  if (Array.isArray(v)) return v as TaskSnapshot[];
  const inner = asRecord(v).tasks;
  return Array.isArray(inner) ? (inner as TaskSnapshot[]) : [];
}

/** `hello.data.sessions` → 会话表（`{site_id: PlatformSession}`）。 */
function sessionsFrom(data: Record<string, unknown>): Record<string, PlatformSession> {
  const raw = asRecord(data.sessions);
  const out: Record<string, PlatformSession> = {};
  for (const [site, v] of Object.entries(raw)) {
    out[site] = {
      logged_in: Boolean((asRecord(v)).logged_in),
      phone_masked: String((asRecord(v)).phone_masked ?? ""),
      busy: Boolean((asRecord(v)).busy),
    };
  }
  return out;
}

/**
 * 把一帧 WS 消息落进 store。
 *
 * `hello` 是全量快照（重连后也当快照用），其余事件增量更新；
 * 认不出的 type 原样放过——新事件先落地再补 reducer，不静默丢数据也不崩。
 */
export function applyWsEvent(msg: WsEnvelope): void {
  const data = asRecord(msg.data);
  const taskId = String(msg.task_id || data.task_id || "");

  switch (msg.type) {
    case "hello": {
      useTaskStore.getState().replaceAll(asSnapshots(data.tasks));
      useSessionStore.getState().setSessions(sessionsFrom(data));
      return;
    }
    case "task.progress":
    case "task.finished": {
      if (taskId) {
        useTaskStore.getState().upsert({ ...(data as unknown as TaskSnapshot), task_id: taskId });
      }
      return;
    }
    case "task.removed": {
      if (taskId) useTaskStore.getState().remove(taskId);
      return;
    }
    case "session.changed": {
      if (msg.site) useSessionStore.getState().upsertSession(msg.site, projectSession(data));
      return;
    }
    case "auth.need_code":
    case "auth.need_slider":
    case "auth.need_scan": {
      const kind = FEED_KIND[msg.type]!;
      useSessionStore.getState().setFeed({
        task_id: taskId,
        site: msg.site,
        kind,
        phone_masked: String(data.phone_masked ?? ""),
        challenge: asRecord(kind === "code" ? data : kind === "slider" ? data.slider : data.qr),
        at: msg.ts || Date.now() / 1000,
      });
      return;
    }
    case "auth.state": {
      // done / error / cancelled 都落这一帧：喂料结束，收起表单。
      if (taskId) useSessionStore.getState().clearFeed(taskId);
      return;
    }
    default:
      return;
  }
}

/** 清空全部 store（测试 / 登出用）。 */
export function resetStores(): void {
  useTaskStore.getState().clear();
  useSessionStore.getState().clearAll();
}
