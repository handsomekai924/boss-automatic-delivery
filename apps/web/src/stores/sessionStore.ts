/**
 * 会话 + 登录喂料 store（docs 5.5 的另半边）。
 *
 * `hello.data.sessions` 全量、`session.changed` 单站增量都归一到 `PlatformSession`；
 * `auth.need_code/need_slider/need_scan` 登记待喂料，`auth.state`（done/error/cancelled）清掉。
 */

import { useMemo } from "react";
import { create } from "zustand";

import type { PlatformSession } from "../platform/types";

/** 登录喂料三态（waiting_human 状态机的 UI 半边，docs 5.5）。 */
export type AuthFeedKind = "code" | "slider" | "scan";

export interface AuthFeed {
  task_id: string;
  site: string;
  kind: AuthFeedKind;
  phone_masked: string;
  /** `need_slider` 的挑战（gt/challenge/image_b64…）、`need_scan` 的二维码，原样透传给表单 */
  challenge: Record<string, unknown>;
  /** 进入等待态的时间戳（秒），表单按它排序 */
  at: number;
}

interface SessionStore {
  /** site_id → 会话摘要 */
  sessions: Record<string, PlatformSession>;
  /** task_id → 待喂料；同一任务同时只会等一种料 */
  feeds: Record<string, AuthFeed>;
  setSessions: (next: Record<string, PlatformSession>) => void;
  upsertSession: (site: string, session: PlatformSession) => void;
  setFeed: (feed: AuthFeed) => void;
  clearFeed: (taskId: string) => void;
  clearAll: () => void;
}

export const useSessionStore = create<SessionStore>((set) => ({
  sessions: {},
  feeds: {},

  setSessions: (next) => set({ sessions: next ?? {} }),

  upsertSession: (site, session) =>
    set((s) => ({ sessions: { ...s.sessions, [site]: session } })),

  setFeed: (feed) => set((s) => ({ feeds: { ...s.feeds, [feed.task_id]: feed } })),

  clearFeed: (taskId) =>
    set((s) => {
      if (!(taskId in s.feeds)) return s;
      const next = { ...s.feeds };
      delete next[taskId];
      return { feeds: next };
    }),

  clearAll: () => set({ sessions: {}, feeds: {} }),
}));

const TERMINAL = new Set(["done", "error", "cancelled", ""]);

/**
 * `session.changed` / `login_tasks.status_payload()` → `PlatformSession`。
 *
 * 与后端 `SiteAdapter.session_summary()` 逐字段对齐：busy 看当前登录任务有没有跑完。
 */
export function projectSession(data: Record<string, unknown>): PlatformSession {
  const task = data.task as Record<string, unknown> | null | undefined;
  const busy = Boolean(task) && !TERMINAL.has(String((task ?? {}).status ?? ""));
  return {
    logged_in: Boolean(data.logged_in),
    phone_masked: typeof data.phone_masked === "string" ? data.phone_masked : "",
    busy,
  };
}

/** 待喂料列表（按时间倒序，最新的在前）。 */
export function selectFeeds(feeds: Record<string, AuthFeed>): AuthFeed[] {
  return Object.values(feeds).sort((a, b) => b.at - a.at);
}

/** 某站点的会话摘要；没这个站点回 undefined。 */
export function selectSession(
  sessions: Record<string, PlatformSession>,
  site: string | null | undefined,
): PlatformSession | undefined {
  return site ? sessions[site] : undefined;
}

/**
 * 组件取待喂料列表。
 *
 * zustand v5 选择器无默认浅比较，直接回新数组会无限重渲染；
 * 选稳定引用再 memo，收口在这里（同 `useTasks`）。
 */
export function useFeeds(): AuthFeed[] {
  const map = useSessionStore((s) => s.feeds);
  return useMemo(() => selectFeeds(map), [map]);
}

/** 组件取某站点会话（描述符兜底在调用方做）。 */
export function useSession(site: string | null | undefined): PlatformSession | undefined {
  const sessions = useSessionStore((s) => s.sessions);
  return selectSession(sessions, site);
}
