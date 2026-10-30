/**
 * 面试题页共用的数据形状与小工具（出题 / 生成记录 两段共用）。
 */

import type { TaskSnapshot } from "../../../stores/taskStore";

/** 与旧页一致的分页大小。 */
export const PAGE_SIZE = 20;

/** 补题题型（后端 `QUESTION_TYPES` 的顺序）。 */
export const QUESTION_TYPES = ["技术深度", "项目经历", "场景", "软技能"] as const;

/** 补题单次上限（后端 `SUPPLEMENT_MAX`）。 */
export const SUPPLEMENT_MAX = 10;

/** 出题页岗位条目（`GET /api/interview/jobs` 的 items）。 */
export type JobRow = {
  encrypt_job_id: string;
  job_name: string;
  brand_name: string;
  location: string;
  salary_desc: string;
  job_experience: string;
  job_degree: string;
  jd_status: string;
  question_count: number;
};

/** 生成记录条目（`GET /api/interview/records` 的 items，一岗一套题）。 */
export type RecordRow = {
  encrypt_job_id: string;
  analysis_id: string;
  job_name: string;
  brand_name: string;
  question_count: number;
  created_at: number;
  updated_at: number;
};

/** 单条题目（`GET /api/interview/list` 的 questions）。 */
export type QuestionItem = {
  id: number;
  question_text: string;
  question_type: string;
  difficulty: string;
  is_frequent: boolean;
  answer_hints?: {
    approach?: string;
    key_points?: string[];
    reference_answer?: string;
  };
};

/** 岗位出题上下文（`GET /api/interview/context`）。 */
export type CtxInfo = {
  analysis_id?: string;
  match_score?: number;
  question_count?: number;
  task?: { status?: string } | null;
};

/** 生成任务业务快照（`interviewApi.tasks` / `task()` 的形状，任务中心 `_normalize` 的 `raw`）。 */
export type InterviewTaskSnap = {
  task_id: string;
  status: string;
  current?: string;
  done?: number;
  total?: number;
  error?: string | null;
  created_at?: number;
  ended_at?: number | null;
  question_total?: number;
  job_ids?: string[];
  events?: unknown[];
  [k: string]: unknown;
};

/** 题目侧栏目标岗位（analysisId 可空 = 纯 JD 出题的记录）。 */
export type PrepTarget = {
  encryptJobId: string;
  jobName: string;
  brandName: string;
  analysisId?: string;
};

/** 错误 → 给人看的一句话（toast 文案统一走这）。 */
export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * 业务任务快照 → 任务中心统一形状（与 WS 推的 `_normalize` 同口径，`raw` 原样挂回）。
 * `events` 后端是字典数组，TaskSnapshot 契约收窄成 string[]——照 repos 的口径透传给 `toTaskEvents()`。
 */
export function toTaskSnapshot(s: InterviewTaskSnap): TaskSnapshot {
  const jobIds = Array.isArray(s.job_ids) ? s.job_ids.map(String) : [];
  return {
    task_id: String(s.task_id ?? ""),
    task_type: "interview",
    site: "",
    title: `面试题（${jobIds.length}个岗位）`,
    status: String(s.status ?? ""),
    current: String(s.current ?? ""),
    done: Number(s.done ?? 0),
    total: Number(s.total ?? 0),
    error: s.error ?? "",
    created_at: Number(s.created_at ?? 0),
    ended_at: s.ended_at == null ? null : Number(s.ended_at),
    events: (s.events ?? []) as unknown as string[],
    raw: s,
  };
}

/** 在跑任务占用的岗位 id（失败不占坑，可以直接重提）——这些岗位的「生成」按钮变「任务中」。 */
export function busyJobIds(tasks: TaskSnapshot[]): Set<string> {
  const out = new Set<string>();
  for (const t of tasks) {
    if (t.status !== "running") continue;
    const raw = (t.raw ?? {}) as { job_ids?: unknown };
    for (const jid of Array.isArray(raw.job_ids) ? raw.job_ids : []) out.add(String(jid));
  }
  return out;
}
