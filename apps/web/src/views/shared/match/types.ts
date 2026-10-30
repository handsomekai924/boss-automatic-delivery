/**
 * 匹配页共用的数据形状与小工具（工作台 / 历史 / 招呼语三个分段共用）。
 *
 * 接口形状按后端来（repos 的类型面有几处窄了，读的时候再放宽，见原 MatchPage 文件头）。
 */

/** 一条匹配结果（`MatchTask._match_job` 的 item；详见 boss_web/services/match_task.py）。 */
export interface MatchItem {
  encrypt_job_id: string;
  job_name: string;
  brand_name: string;
  location?: string;
  salary_desc?: string;
  job_experience?: string;
  job_degree?: string;
  match_score: number;
  matched_skills: string[];
  missing_skills: string[];
  verdict: string;
  pros: string[];
  cons: string[];
  advice: string;
  greeting: string;
  error?: string;
  /* 安全快照（只读展示，投递判定在平台侧） */
  is_blacklisted?: boolean;
  company_blacklisted?: boolean;
  jd_blacklisted?: boolean;
  jd_status?: string;
  jd_rule_checked?: boolean;
  auto_delivery_blocked?: boolean;
}

/** 匹配任务业务快照（`MatchTask.snapshot()` + API 层补的 greet 状态）。 */
export interface MatchTaskSnap {
  task_id: string | null;
  analysis_id?: string;
  status: string;
  resume_id?: string;
  created_at?: number;
  ended_at?: number | null;
  error?: string | null;
  done?: number;
  total?: number;
  current_job?: string;
  matches?: MatchItem[];
  events?: unknown[];
}

/** 历史分析摘要行（`resume_store.list_analyses`）。 */
export interface AnalysisRow {
  analysis_id: string;
  resume_id: string;
  resume_title: string;
  created_at: number;
  job_count: number;
  top_score: number;
  status: string;
  model: string;
}

/** 历史分析详情（`GET /api/match/analyses/{id}` 出门的整份 payload）。 */
export interface AnalysisPayload {
  analysis_id: string;
  resume_id?: string;
  resume_title?: string;
  created_at?: number;
  status?: string;
  matches?: MatchItem[];
}

/** 勾选面板里的职位行（借 `interviewApi.jobs` 的字段面）。 */
export interface JobPick {
  encrypt_job_id: string;
  job_name: string;
  brand_name: string;
  location?: string;
  salary_desc?: string;
}

/** 抓取批次行（借任务中心的 crawl 任务快照）。 */
export interface CrawlPick {
  task_id: string;
  title: string;
  created_at: number;
  site?: string;
}

/** 结果按匹配分从高到低（legacy `renderMatches` 同款排序）。 */
export function rank(items: MatchItem[]): MatchItem[] {
  return [...items].sort((a, b) => (b.match_score ?? 0) - (a.match_score ?? 0));
}

/** 接口错误统一转人话（client 抛的都是带 message 的 ApiError）。 */
export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 招呼语长度上限（后端 `GreetingBody` 的 max_length）。 */
export const GREETING_MAX = 500;

/** 职位勾选面板一页几个（legacy PAGE_SIZE）。 */
export const PAGE_SIZE = 20;

/** 匹配任务终态；到这些状态就停轮询。 */
export const TASK_TERMINAL = new Set(["done", "error", "cancelled"]);

/** 历史行的状态文案（后端 status 原值兜底）。 */
export function analysisStatusLabel(status: string): string {
  if (status === "done") return "已完成";
  if (status === "error") return "失败";
  if (status === "cancelled") return "已取消";
  return status || "—";
}
