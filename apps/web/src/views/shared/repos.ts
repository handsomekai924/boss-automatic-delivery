/**
 * 共用页面的接口访问层（docs 5.3「视图不拼 URL」）。
 *
 * 只有这一层知道 URL 长什么样；视图拿类型化的函数和数据形状。
 * 共用接口（简历/匹配/面试题/任务/LLM/系统）不带站点语义——列表要按站点筛就传 `?site=`。
 */

import { api } from "../../api/client";

/* ------------------------------------------------------------------ 任务中心 */

/**
 * 任务中心统一快照形状（后端 `_normalize` 的镜像）。
 *
 * `events` 按 `stores/taskStore` 的契约收窄成 `string[]`，但后端实际下发的是
 * 事件字典数组（`{at, name, payload}`）——消费端一律过 `toTaskEvents()`（收 `unknown`），
 * 别按 `string[]` 去 map。
 */
export interface TaskSnapshot {
  task_id: string;
  task_type: string;
  site: string;
  title: string;
  status: string;
  current: string;
  done: number;
  total: number;
  error: string;
  created_at: number;
  ended_at: number | null;
  events: string[];
  raw?: Record<string, unknown>;
}

export interface TaskListParams {
  task_type?: string;
  status?: string;
  /** 空串 = 只看共用任务；不传 = 全部 */
  site?: string;
}

/** 拼查询串；跳过 `undefined`/`null`/空串（`= ` 留空对后端等于没传）。 */
function query(params: object): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const tasksApi = {
  list: (p: TaskListParams = {}) =>
    api.get<{ items: TaskSnapshot[] }>(`/api/tasks${query(p)}`),
  get: (taskId: string) => api.get<TaskSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}`),
  cancel: (taskId: string) =>
    api.post<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`),
  retry: (taskId: string) =>
    api.post<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(taskId)}/retry`),
  remove: (taskId: string) => api.del<{ ok: boolean }>(`/api/tasks/${encodeURIComponent(taskId)}`),
  cancelAll: () => api.post<{ ok: boolean }>("/api/tasks/cancel-all"),
};

/* ------------------------------------------------------------------ 简历 */

export interface ResumeItem {
  resume_id: string;
  title: string;
  source_name: string;
  created_at: number;
  parsed: boolean;
  [k: string]: unknown;
}

export interface ResumeRevision {
  revision_id: string;
  resume_id: string;
  title: string;
  created_at: number;
  status: string;
  [k: string]: unknown;
}

export const resumeApi = {
  list: () => api.get<{ items: ResumeItem[] }>("/api/resume/list"),
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return api.upload<{ resume_id: string }>("/api/resume/upload", fd);
  },
  get: (id: string) =>
    api.get<{ resume: ResumeItem; content_md: string; llm: unknown }>(
      `/api/resume/item/${encodeURIComponent(id)}`,
    ),
  remove: (id: string) => api.del<{ ok: boolean }>(`/api/resume/item/${encodeURIComponent(id)}`),
  sections: (id: string) =>
    api.get<{ sections: string[] }>(`/api/resume/item/${encodeURIComponent(id)}/sections`),
  startParse: (id: string) =>
    api.post<{ task_id: string }>(`/api/resume/item/${encodeURIComponent(id)}/parse`),
  getParse: (id: string) =>
    api.get<{ llm: unknown }>(`/api/resume/item/${encodeURIComponent(id)}/parse`),

  analyses: () => api.get<{ items: Record<string, unknown>[] }>("/api/resume/analyses"),
  getAnalysis: (id: string) =>
    api.get<Record<string, unknown>>(`/api/resume/analyses/${encodeURIComponent(id)}`),
  removeAnalysis: (id: string) =>
    api.del<{ ok: boolean }>(`/api/resume/analyses/${encodeURIComponent(id)}`),

  /** 定向优化：以岗位为目标改简历 */
  startRevise: (body: {
    resume_id: string;
    job_ids?: string[];
    crawl_task_id?: string;
    all_repo?: boolean;
  }) => api.post<{ task_id: string }>("/api/resume/revise/start", body),
  /** 自由优化：按章节 + 提示词改简历 */
  startFreeRevise: (body: {
    resume_id: string;
    selected_sections?: string[];
    optimization_prompt: string;
  }) => api.post<{ task_id: string }>("/api/resume/revise/free-start", body),
  revisePresets: () =>
    api.get<{ categories: string[]; presets: Record<string, string[]> }>(
      "/api/resume/revise/presets",
    ),
  reviseStatus: () => api.get<Record<string, unknown>>("/api/resume/revise/status"),
  reviseTask: (taskId: string) =>
    api.get<Record<string, unknown>>(`/api/resume/revise/${encodeURIComponent(taskId)}`),
  cancelRevise: (taskId: string) =>
    api.post<{ ok: boolean }>(`/api/resume/revise/${encodeURIComponent(taskId)}/cancel`),

  revisions: (limit = 20, offset = 0) =>
    api.get<{ items: ResumeRevision[]; total: number }>(
      `/api/resume/revisions${query({ limit, offset })}`,
    ),
  revision: (id: string) =>
    api.get<Record<string, unknown>>(`/api/resume/revisions/${encodeURIComponent(id)}`),
  saveRevision: (
    id: string,
    body: { format?: "md" | "txt"; filename?: string; title?: string },
  ) => api.post<Record<string, unknown>>(`/api/resume/revisions/${encodeURIComponent(id)}/save`, body),
  regenerateRevision: (id: string) =>
    api.post<Record<string, unknown>>(`/api/resume/revisions/${encodeURIComponent(id)}/regenerate`),
  removeRevision: (id: string) =>
    api.del<{ ok: boolean }>(`/api/resume/revisions/${encodeURIComponent(id)}`),
  /** 生成下载链接（新窗口打开，不是 fetch） */
  revisionDownloadUrl: (id: string, format: "md" | "txt" = "md") =>
    `/api/resume/revisions/${encodeURIComponent(id)}/download?format=${format}`,
};

/* ------------------------------------------------------------------ 匹配 */

export const matchApi = {
  analyze: (body: { resume_id: string; job_ids?: string[]; crawl_task_id?: string }) =>
    api.post<{ task_id: string }>("/api/match/analyze", body),
  status: () => api.get<Record<string, unknown>>("/api/match/analyze/status"),
  task: (taskId: string) =>
    api.get<Record<string, unknown>>(`/api/match/analyze/${encodeURIComponent(taskId)}`),
  cancel: (taskId: string) =>
    api.post<{ ok: boolean }>(`/api/match/analyze/${encodeURIComponent(taskId)}/cancel`),
  patchGreeting: (analysisId: string, encryptJobId: string, greeting: string) =>
    api.patch<Record<string, unknown>>(
      `/api/match/${encodeURIComponent(analysisId)}/greeting`,
      { encrypt_job_id: encryptJobId, greeting },
    ),
  regenGreeting: (analysisId: string, encryptJobId: string) =>
    api.post<{ greeting: string }>(
      `/api/match/${encodeURIComponent(analysisId)}/greeting/regenerate`,
      { encrypt_job_id: encryptJobId },
    ),
  /** 单条重跑：只改库里那条，不新增 analysis（只改不增） */
  rematch: (analysisId: string, encryptJobId: string) =>
    api.post<Record<string, unknown>>(`/api/match/${encodeURIComponent(analysisId)}/rematch`, {
      encrypt_job_id: encryptJobId,
    }),
  analyses: () => api.get<{ items: Record<string, unknown>[] }>("/api/match/analyses"),
  analysis: (id: string) =>
    api.get<Record<string, unknown>>(`/api/match/analyses/${encodeURIComponent(id)}`),
  removeAnalysis: (id: string) =>
    api.del<{ ok: boolean }>(`/api/match/analyses/${encodeURIComponent(id)}`),
};

/* ------------------------------------------------------------------ 面试题 */

export const interviewApi = {
  generate: (body: {
    analysis_id?: string;
    encrypt_job_id?: string;
    encrypt_job_ids?: string[];
    resume_id?: string;
    question_count?: number;
    question_type?: string;
  }) => api.post<{ task_id: string }>("/api/interview/generate", body),
  tasks: () => api.get<{ items: Record<string, unknown>[] }>("/api/interview/tasks"),
  current: () => api.get<Record<string, unknown>>("/api/interview/task/current"),
  task: (id: string) => api.get<Record<string, unknown>>(`/api/interview/task/${encodeURIComponent(id)}`),
  jobs: (p: { crawl_task_id?: string; title?: string; limit?: number; offset?: number } = {}) =>
    api.get<{ items: Record<string, unknown>[]; total: number }>(
      `/api/interview/jobs${query(p)}`,
    ),
  records: (p: { encrypt_job_id?: string; title?: string; limit?: number; offset?: number } = {}) =>
    api.get<Record<string, unknown>>(`/api/interview/records${query(p)}`),
  context: (p: { encrypt_job_id?: string } = {}) =>
    api.get<Record<string, unknown>>(`/api/interview/context${query(p)}`),
  /**
   * 一批岗位的出题状态（jobs 卡片徽章用）。
   * 参数是 `encrypt_job_ids`（复数、逗号分隔），不是 `encrypt_job_id`——照后端来。
   */
  summary: (p: { encrypt_job_ids?: string } = {}) =>
    api.get<{ items: Record<string, { question_count: number }> }>(
      `/api/interview/summary${query(p)}`,
    ),
  list: (p: { encrypt_job_id?: string; limit?: number; offset?: number } = {}) =>
    api.get<Record<string, unknown>>(`/api/interview/list${query(p)}`),
  exportUrl: (p: { encrypt_job_id?: string; format?: string } = {}) =>
    `/api/interview/export${query(p)}`,
};

/* ------------------------------------------------------------------ 抓取批次（共用账，跨站点） */

/**
 * 抓取批次档案（`crawl_tasks` 表）。共用读法——批量匹配 / 出题 / 简历优化都拿它当职位来源，
 * 跟「全库」「勾选」并列，不是站点操作。站点作用域的抓取执行在 `platform/repos.ts` 的 `crawlApi`。
 */
export interface CrawlTaskRow {
  task_id: string;
  site: string;
  source?: string;
  status: string;
  created_at: number;
  started_at?: number | null;
  ended_at?: number | null;
  /** 启动参数快照（含 config_snapshot），条件摘要前端压一行 */
  params?: Record<string, unknown>;
  stats?: Record<string, unknown>;
  /** 本批去重后职位数 */
  job_count?: number;
  /** 库里还存在的（「匹配本批」用这个） */
  job_count_existing?: number;
  inserted_count?: number;
  updated_count?: number;
  error?: string | null;
  stopped_reason?: string;
  live?: boolean;
}

export const crawlBatchApi = {
  /** 跨站点全量批次账（共用口 `/api/crawl/tasks`，不带 `{site}`）。 */
  tasks: (p: { limit?: number; offset?: number } = {}) =>
    api.get<{ items: CrawlTaskRow[]; total: number; limit: number; offset: number }>(
      `/api/crawl/tasks${query(p)}`,
    ),
  task: (taskId: string) =>
    api.get<CrawlTaskRow>(`/api/crawl/tasks/${encodeURIComponent(taskId)}`),
  /** 本批职位（`total` 含已删归属，`existing` 是库里还在的，行里 `missing: true` 标已删）。 */
  jobs: (taskId: string, p: { limit?: number; offset?: number } = {}) =>
    api.get<{ items: Record<string, unknown>[]; total: number; existing: number; limit: number; offset: number }>(
      `/api/crawl/tasks/${encodeURIComponent(taskId)}/jobs${query(p)}`,
    ),
};

/* ------------------------------------------------------------------ 设置 */

export interface LlmConfig {
  api_key: string;
  base_url: string;
  model: string;
  [k: string]: unknown;
}

/** 招呼语提示词（后端 `_greeting_prompt_payload`）。 */
export interface GreetingPrompt {
  /** 生效值（自定义没有就回落到内置默认） */
  requirement: string;
  /** 自定义原文；空串 = 没设过 */
  custom: string;
  is_default: boolean;
  /** 内置默认原文 */
  default: string;
}

export const systemApi = {
  health: () => api.get<{ ok: boolean; [k: string]: unknown }>("/api/system/health"),
  chrome: () => api.get<Record<string, unknown>>("/api/system/chrome"),
  chromeMode: (body: { mode: string }) =>
    api.post<Record<string, unknown>>("/api/system/chrome/mode", body),
  chromeOpen: () => api.post<Record<string, unknown>>("/api/system/chrome/open"),
  /**
   * 招呼语提示词：生效值 + 自定义原文 + 内置默认（`is_default` = 没设过自定义）。
   * 三个键照后端 `_greeting_prompt_payload` 来，别缩成一个 `prompt`。
   */
  greetingPrompt: () => api.get<GreetingPrompt>("/api/system/greeting-prompt"),
  /** 空串 = 恢复出厂默认；回包是写完之后的完整状态。 */
  saveGreetingPrompt: (requirement: string) =>
    api.put<GreetingPrompt>("/api/system/greeting-prompt", { requirement }),
  update: (force = false) =>
    api.get<Record<string, unknown>>(`/api/system/update${force ? "?force=true" : ""}`),
  updateIgnore: (body: { version?: string }) =>
    api.post<Record<string, unknown>>("/api/system/update/ignore", body),
  updateLog: () => api.get<{ lines?: string[]; [k: string]: unknown }>("/api/system/update/log"),
  updateApply: () => api.post<Record<string, unknown>>("/api/system/update/apply"),
};

export const llmApi = {
  config: () => api.get<LlmConfig>("/api/llm/config"),
  saveConfig: (body: { api_key?: string; base_url?: string; model?: string }) =>
    api.put<LlmConfig>("/api/llm/config", body),
  models: (body: { base_url?: string; api_key?: string } = {}) =>
    api.post<{ models: string[]; source: string; selected: string }>("/api/llm/models", body),
  test: () => api.post<{ ok: boolean; message: string }>("/api/llm/test"),
};

/* ------------------------------------------------------------------ 黑名单（平台作用域，但共用设置页要能管） */

export interface BlacklistRule {
  rule_id: string;
  rule_type: "company" | "jd_keyword";
  pattern: string;
  match_mode: string;
  scope: string;
  enabled: boolean;
  [k: string]: unknown;
}

export const blacklistApi = {
  /** 规则挂在平台作用域下；设置页用当前平台上下文的 site 调它。 */
  rules: (site: string, type?: "company" | "jd_keyword") =>
    api.get<{ items: BlacklistRule[] }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/rules${query({ type })}`,
    ),
  createRule: (site: string, body: Partial<BlacklistRule>) =>
    api.post<{ item: BlacklistRule }>(`/api/sites/${encodeURIComponent(site)}/blacklist/rules`, body),
  updateRule: (site: string, id: string, body: Partial<BlacklistRule>) =>
    api.put<{ item: BlacklistRule }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/rules/${encodeURIComponent(id)}`,
      body,
    ),
  toggleRule: (site: string, id: string, enabled: boolean) =>
    api.post<{ item: BlacklistRule }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/rules/${encodeURIComponent(id)}/toggle`,
      { enabled },
    ),
  removeRule: (site: string, id: string) =>
    api.del<{ deleted: boolean }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/rules/${encodeURIComponent(id)}`,
    ),
  testRule: (site: string, body: Record<string, unknown>) =>
    api.post<{ matched: boolean; matches: unknown[] }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/test`,
      body,
    ),
  ruleHits: (site: string, id: string, limit = 100) =>
    api.get<{ items: unknown[] }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/rules/${encodeURIComponent(id)}/hits${query({ limit })}`,
    ),
  hits: (site: string, limit = 100) =>
    api.get<{ items: unknown[] }>(
      `/api/sites/${encodeURIComponent(site)}/blacklist/hits${query({ limit })}`,
    ),
  recheck: (site: string) =>
    api.post<{ task_id: string }>(`/api/sites/${encodeURIComponent(site)}/blacklist/recheck`, {}),
  recheckStatus: (site: string) =>
    api.get<Record<string, unknown>>(`/api/sites/${encodeURIComponent(site)}/blacklist/recheck/status`),
  recheckCancel: (site: string) =>
    api.post<Record<string, unknown>>(`/api/sites/${encodeURIComponent(site)}/blacklist/recheck/cancel`),
};
