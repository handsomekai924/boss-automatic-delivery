/**
 * 任务快照 store（docs 5.5「WS store」的任务半边）。
 *
 * WS 事件 → 这里，组件按 `task_type` / `site` 订阅切片；
 * 重连后的 `hello` 是全量快照，直接 `replaceAll`，不累加。
 */

import { useMemo } from "react";
import { create } from "zustand";

/** 任务中心统一快照形状（后端 `_normalize` 的镜像）。 */
export interface TaskSnapshot {
  task_id: string;
  task_type: string;
  /** 站点 id；空 = 共用任务 */
  site: string;
  title: string;
  status: string;
  current: string;
  done: number;
  total: number;
  error?: string | null;
  created_at?: number | null;
  ended_at?: number | null;
  events: string[];
  /** 业务任务自带的原始快照，视图按需读 */
  raw?: Record<string, unknown>;
}

interface TaskStore {
  /** task_id → 快照；用对象不用数组，`task.progress` 高频 upsert 才不 O(n) */
  tasks: Record<string, TaskSnapshot>;
  upsert: (snap: TaskSnapshot) => void;
  replaceAll: (snaps: TaskSnapshot[]) => void;
  remove: (taskId: string) => void;
  clear: () => void;
}

const EMPTY: Record<string, TaskSnapshot> = {};

export const useTaskStore = create<TaskStore>((set) => ({
  tasks: EMPTY,

  upsert: (snap) =>
    set((s) => ({ tasks: { ...s.tasks, [snap.task_id]: { ...s.tasks[snap.task_id], ...snap } } })),

  replaceAll: (snaps) =>
    set(() => {
      const next: Record<string, TaskSnapshot> = {};
      for (const t of snaps) if (t?.task_id) next[t.task_id] = t;
      return { tasks: next };
    }),

  remove: (taskId) =>
    set((s) => {
      if (!(taskId in s.tasks)) return s;
      const next = { ...s.tasks };
      delete next[taskId];
      return { tasks: next };
    }),

  clear: () => set({ tasks: EMPTY }),
}));

/** 全量快照数组（按创建时间倒序，新的在前）。 */
export function selectTasks(tasks: Record<string, TaskSnapshot>): TaskSnapshot[] {
  return Object.values(tasks).sort(
    (a, b) => (b.created_at ?? 0) - (a.created_at ?? 0) || a.task_id.localeCompare(b.task_id),
  );
}

/**
 * 按站点过滤；`site === undefined` 不过滤，`site === ""` 只留共用任务。
 * 与后端 `list_snapshots(site=…)` 同一套语义。
 */
export function selectTasksBySite(
  tasks: Record<string, TaskSnapshot>,
  site: string | undefined,
): TaskSnapshot[] {
  const all = selectTasks(tasks);
  return site === undefined ? all : all.filter((t) => (t.site ?? "") === site);
}

/** 在跑中的任务（顶栏角标用）。 */
export function selectRunning(tasks: Record<string, TaskSnapshot>): TaskSnapshot[] {
  return selectTasks(tasks).filter((t) => !t.ended_at && !["done", "error", "cancelled", ""].includes(String(t.status)));
}

/**
 * 组件取任务列表。
 *
 * zustand v5 的选择器没有默认浅比较，直接 `(s) => selectTasks(s.tasks)` 每次回新数组
 * 就会无限重渲染；所以固定选 `s.tasks`（稳定引用）再 memo 一次，收口在这里不留给调用方。
 */
export function useTasks(): TaskSnapshot[] {
  const map = useTaskStore((s) => s.tasks);
  return useMemo(() => selectTasks(map), [map]);
}

/** 同上，只留未结束的（顶栏角标用）。 */
export function useRunningTasks(): TaskSnapshot[] {
  const map = useTaskStore((s) => s.tasks);
  return useMemo(() => selectRunning(map), [map]);
}
