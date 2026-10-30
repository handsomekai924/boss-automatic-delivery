/**
 * 简历页共用的数据形状与常量（简历库 / 简历解析 / 简历优化 / 优化记录 四段共用）。
 */

import type { PillTone, TaskEvent } from "../../../components";

/** 简历列表行（`GET /api/resume/list` 的 item：meta 摘要，`llm` 只报解析过没有）。 */
export interface ResumeListItem {
  resume_id: string;
  title: string;
  source_name?: string;
  created_at: number;
  llm?: { parsed_at?: number; model?: string; has_data?: boolean } | null;
}

/** LLM 解析结果（`meta.llm` 整包）。 */
export interface LlmParse {
  parsed_at?: number;
  model?: string;
  data?: ParsedData | null;
}

/** 八段解析数据（后端 `resume_parser.py` 的模板字段）。 */
export interface ParsedData {
  basic?: Record<string, string>;
  intent?: Record<string, string>;
  work?: ParsedEntry[];
  project?: ParsedEntry[];
  education?: ParsedEntry[];
  skills?: string[];
  self_evaluation?: string;
  summary?: string;
}

/** 经历条目：工作 / 项目 / 教育共用（字段各取所需）。 */
export interface ParsedEntry {
  period?: string;
  company?: string;
  title?: string;
  name?: string;
  role?: string;
  school?: string;
  major?: string;
  degree?: string;
  highlights?: string[];
}

/** `GET /api/resume/item/{id}`：平铺简历字典，原文在 `raw`。 */
export interface ResumeDetail {
  resume_id: string;
  title: string;
  created_at: number;
  raw: string;
  llm: LlmParse | null;
}

/** 自由模式章节项（`extract_sections` 的输出）。 */
export interface SectionItem {
  title: string;
  level: number;
}

/** 预设提示词（`{name, prompt}`，按分类分组）。 */
export interface PresetItem {
  name: string;
  prompt: string;
}

/** 优化任务快照（`revision_tasks.snapshot()`；task_id 与 revision_id 同值）。 */
export interface ReviseSnapshot {
  task_id: string | null;
  revision_id?: string;
  status: string;
  resume_id?: string;
  mode?: string;
  job_count?: number;
  current?: string;
  error?: string;
  done?: boolean;
  events?: unknown;
}

/** 优化记录摘要行（`resume_revisions` 列表）。 */
export interface RevisionRow {
  revision_id: string;
  resume_id: string;
  resume_title: string;
  created_at: number;
  status: string;
  mode?: string;
  job_count?: number;
  model?: string;
}

export interface ChangeItem {
  section?: string;
  type?: string;
  summary?: string;
  reason?: string;
}

export interface DiffBlock {
  tag: string;
  left?: string;
  right?: string;
}

/** 优化记录详情（全文 + diff）。 */
export interface RevisionDetail extends RevisionRow {
  original_md: string;
  revised_md: string;
  changes: ChangeItem[];
  diff: DiffBlock[];
  saved_path?: string;
  saved_resume_id?: string;
}

/** 定向模式候选岗位行（`/api/interview/jobs` 的 item）。 */
export interface JobPickRow {
  encrypt_job_id: string;
  job_name: string;
  brand_name: string;
  location?: string;
  salary_desc?: string;
  jd_status?: string;
}

/** 解析任务在跑快照（简历库徽章 / 解析段进度条共用）。 */
export interface ParseRun {
  taskId: string;
  status: string;
  percent?: number;
  current: string;
  error: string;
  events: TaskEvent[];
}

/** 解析任务 / 优化任务的轮询间隔；两个都是本地任务，1.5s 足够跟手。 */
export const POLL_MS = 1500;

/** 解析任务终态（走统一任务中心的 status）。 */
export const PARSE_TERMINAL = new Set(["done", "error", "cancelled"]);

/** 定向模式最多勾几个岗位——后端 `_pick_target_jobs` 也只吃前 5 个。 */
export const MAX_TARGET = 5;

export const JOB_PAGE_SIZE = 20;

export const REV_PAGE_SIZE = 20;

/** 变更点类型文案（legacy `CHANGE_TYPE_LABEL`）。 */
export const CHANGE_TYPE_LABEL: Record<string, string> = {
  rewrote: "改写",
  reordered: "重排",
  trimmed: "精简",
  expanded: "扩写",
  keyword: "关键词对齐",
};

/** 优化记录状态徽标（legacy `statusCls`）。 */
export const REV_STATUS_META: Record<string, { label: string; tone: PillTone }> = {
  running: { label: "生成中", tone: "warn" },
  draft: { label: "草稿", tone: "accent" },
  saved: { label: "已保存", tone: "ok" },
  error: { label: "出错", tone: "danger" },
  cancelled: { label: "已取消", tone: "static" },
  discarded: { label: "已放弃", tone: "static" },
};

export const FORMAT_OPTIONS = [
  { code: "md", name: "Markdown" },
  { code: "txt", name: "纯文本" },
];

export const MODE_OPTIONS = [
  { value: "targeted", label: "定向优化" },
  { value: "free", label: "自由优化" },
] as const;

export type ReviseMode = (typeof MODE_OPTIONS)[number]["value"];

/** 错误 → 给人看的一句话（ApiError.message 本来就是话术）。 */
export function msgOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 优化任务快照 → 是否已终态（后端 done 标志或 status 三态）。 */
export function reviseTerminal(s: ReviseSnapshot | null): boolean {
  if (!s) return true;
  return Boolean(s.done) || ["done", "error", "cancelled"].includes(String(s.status || ""));
}
