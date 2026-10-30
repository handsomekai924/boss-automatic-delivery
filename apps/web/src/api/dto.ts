/** 跨平台共享 DTO（与后端响应同构；站点私有字段用 index signature 扩展）。 */

/** 职位列表卡片最小面（jobs 表 / 搜索流共用） */
export interface JobSummary {
  /** 站点加密职位 ID */
  job_id: string;
  /** 来源站点（多平台必填） */
  site?: string;
  job_name: string;
  brand_name: string;
  salary_desc?: string | null;
  location?: string | null;
  job_experience?: string | null;
  job_degree?: string | null;
  /** 匹配分（0–100），无则不展示 */
  score?: number | null;
  /** JD 采集状态 */
  jd_status?: JdStatus | string | null;
}

export type JdStatus = "pending" | "collecting" | "completed" | "empty" | "failed" | "unchecked";

export interface MatchedRule {
  rule_type: string;
  match_mode?: string | null;
  pattern?: string | null;
  matched_text?: string | null;
  source?: string | null;
}

/** 职位详情（GET /api/jobs/{id}，legacy job_detail.js 消费面） */
export interface JobDetail extends JobSummary {
  brand_industry?: string | null;
  brand_scale_name?: string | null;
  skills?: string[];
  welfare_list?: string[];
  job_desc?: string | null;
  boss_name?: string | null;
  boss_title?: string | null;
  company_blacklisted?: boolean;
  jd_blacklisted?: boolean;
  jd_rule_checked?: boolean;
  matched_rule_snapshot?: MatchedRule[];
}

export const JD_STATUS_LABEL: Record<JdStatus, string> = {
  pending: "待采集",
  collecting: "采集中",
  completed: "已完成",
  empty: "JD 为空",
  failed: "采集失败",
  unchecked: "规则未检查",
};
