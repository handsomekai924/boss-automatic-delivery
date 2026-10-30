/**
 * 平台作用域页面的接口访问层（docs 5.3「视图不拼 URL」+ 6.3 站点化约定）。
 *
 * 全部走 `/api/sites/{site}/…`（`sitePath`），**不用**旧的 `/api/auth/…` 那套
 * 兼容别名——那是给 legacy 壳的，M3 收尾要一起删。视图只拿类型化的函数和数据形状，
 * 不知道 URL 长什么样，也不知道某个站点是哪家。
 */

import { api, sitePath } from "../../api/client";

/** 站点作用域下的路径；集中收口，视图别自己调 `sitePath`。 */
const at = (site: string, path: string) => sitePath(site, path);

/* ------------------------------------------------------------------ 登录与会话 */

/** 登录任务快照（后端 `login_tasks.status_payload()` 的镜像）。 */
export interface AuthTask {
  task_id: string;
  status: string;
  phone_masked?: string;
  /** 扫码任务的二维码元信息；`revision` 变了才要重取图 */
  qr?: {
    active?: boolean;
    scanned?: boolean;
    confirmed?: boolean;
    revision?: number;
    head_img?: string;
    payload?: string;
    pk?: string;
    [k: string]: unknown;
  } | null;
  /** 滑块挑战（gt/challenge/image_b64…），喂给 `SliderForm` */
  slider?: Record<string, unknown> | null;
  error?: string | null;
  [k: string]: unknown;
}

/** `/auth/status` 出门的总览：当前任务 + 会话摘要。 */
export interface AuthStatus {
  logged_in: boolean;
  phone_masked: string;
  task: AuthTask | null;
  [k: string]: unknown;
}

/**
 * 极验票据三键（后端 `SliderSolutionBody` 长短名都认）。
 * 走官方组件回来的是长名，自己接的是短名，这里两种都收。
 */
export interface SliderTicket {
  geetest_challenge?: string;
  geetest_validate?: string;
  geetest_seccode?: string;
  challenge?: string;
  validate?: string;
  seccode?: string;
  rand_key?: string;
}

export const authApi = {
  status: (site: string) => api.get<AuthStatus>(at(site, "/auth/status")),

  /** 发短信验证码：起一条登录任务，回任务快照（`task_id` 在里面） */
  sendSms: (site: string, body: { phone: string; dial_code?: string }) =>
    api.post<AuthTask>(at(site, "/auth/sms"), body),

  /**
   * 提交短信验证码。
   * 后端实参是 `{task_id, code}`（`LoginRequest`），不是 `sms_code`——照代码来。
   */
  submitCode: (site: string, body: { task_id: string; code: string }) =>
    api.post<AuthTask>(at(site, "/auth/login"), body),

  /** 起一条 APP 扫码登录；二维码图单独取 */
  startQr: (site: string, pk = "") =>
    api.post<AuthTask>(at(site, "/auth/qr"), { pk }),

  /** 二维码 PNG 的地址（`<img src>` 用，不是 fetch）；`v` 递增破缓存 */
  qrImageUrl: (site: string, taskId: string, revision = 0) =>
    at(site, `/auth/qr/${encodeURIComponent(taskId)}/image?v=${revision}`),

  task: (site: string, taskId: string) =>
    api.get<AuthTask>(at(site, `/auth/tasks/${encodeURIComponent(taskId)}`)),

  cancel: (site: string, taskId: string) =>
    api.post<{ ok: boolean }>(at(site, "/auth/cancel"), { task_id: taskId }),

  /** 登出当前会话；会话卡上的「退出登录」用 */
  logout: (site: string) => api.post<{ ok: boolean }>(at(site, "/auth/logout")),

  /** 滑块帮助页（后端渲染的极验官方组件页）；票据由它自己 POST 回 `/solution` */
  sliderPageUrl: (site: string, taskId: string) =>
    at(site, `/auth/slider/${encodeURIComponent(taskId)}`),

  /** 「重新加载」：换一张新的极验挑战；回 `{page_url}`（新壳自己拼 sitePath，不看这个） */
  sliderRefresh: (site: string, taskId: string) =>
    api.post<{ page_url: string }>(
      at(site, `/auth/slider/${encodeURIComponent(taskId)}/refresh`),
    ),

  /** 喂滑块票据。长短名都认（`validate`→`validate_token`、`randKey`→`rand_key`），`task_id` 在路径里 */
  sliderSolve: (site: string, taskId: string, ticket: SliderTicket) =>
    api.post<AuthTask>(at(site, `/auth/slider/${encodeURIComponent(taskId)}/solution`), ticket),
};

/* ------------------------------------------------------------------ 职位 */

export interface JobItem {
  encrypt_job_id: string;
  job_name: string;
  brand_name: string;
  location: string;
  salary_desc: string;
  job_experience: string;
  job_degree: string;
  jd_status?: string;
  fetched_at?: number;
  [k: string]: unknown;
}

export const jobsApi = {
  /** 职位库分页；筛选维度是 `city`/`keyword`/`category`/`jd_status`，不是自由文本 q */
  list: (
    site: string,
    p: {
      limit?: number;
      offset?: number;
      city?: string;
      keyword?: string;
      category?: string;
      jd_status?: string;
    } = {},
  ) => api.get<{ items: JobItem[]; total: number; limit: number; offset: number }>(at(site, `/jobs${qs(p)}`)),
  stats: (site: string) => api.get<Record<string, unknown>>(at(site, "/jobs/stats")),
  get: (site: string, id: string) =>
    api.get<JobItem>(at(site, `/jobs/${encodeURIComponent(id)}`)),
  remove: (site: string, id: string) =>
    api.del<{ ok: boolean }>(at(site, `/jobs/${encodeURIComponent(id)}`)),
  /** 批量删；后端字段就叫 `ids`（`DeleteBody`），空列表会抛「没有选中任何职位」 */
  removeMany: (site: string, ids: string[]) =>
    api.post<{ deleted: number }>(at(site, "/jobs/delete"), { ids }),
  /** 清空职位库；`confirm` 是布尔不是字符串（`ClearBody`），可按城市/关键词收窄 */
  clear: (site: string, confirm = true, scope: { city?: string; keyword?: string } = {}) =>
    api.post<Record<string, unknown>>(at(site, "/jobs/clear"), { confirm, ...scope }),

  /**
   * 批量补 JD（还没抓到描述的职位）；异步任务。
   * 只控数量与间隔（`FetchDescBody`），不按 id 挑——要挑就先删掉不要的。
   */
  fetchDescriptions: (site: string, body: { limit?: number; interval?: number } = {}) =>
    api.post<{ task_id: string }>(at(site, "/jobs/fetch-descriptions"), body),
  fetchDescriptionsStatus: (site: string) =>
    api.get<Record<string, unknown>>(at(site, "/jobs/fetch-descriptions/status")),
  fetchDescriptionsCancel: (site: string) =>
    api.post<{ ok: boolean }>(at(site, "/jobs/fetch-descriptions/cancel")),
};

/* ------------------------------------------------------------------ 抓取 */

/** 启动抓取（`CrawlBody`）；`use_search` 是兼容字段，现在一律走搜索流。 */
export interface CrawlStartBody {
  max_pages?: number;
  interval?: number;
  start_page?: number;
  fetch_details?: boolean;
  detail_interval?: number;
  store_blacklisted?: boolean;
}

export const crawlApi = {
  tasks: (site: string, p: { limit?: number; offset?: number } = {}) =>
    api.get<{ items: Record<string, unknown>[]; total: number; limit: number; offset: number }>(
      at(site, `/crawl/tasks${qs(p)}`),
    ),
  task: (site: string, taskId: string) =>
    api.get<Record<string, unknown>>(at(site, `/crawl/tasks/${encodeURIComponent(taskId)}`)),
  taskJobs: (site: string, taskId: string, p: { limit?: number; offset?: number } = {}) =>
    api.get<{ items: JobItem[]; total: number; existing: number; limit: number; offset: number }>(
      at(site, `/crawl/tasks/${encodeURIComponent(taskId)}/jobs${qs(p)}`),
    ),
  status: (site: string) => api.get<Record<string, unknown>>(at(site, "/crawl/status")),
  start: (site: string, body: CrawlStartBody = {}) =>
    api.post<{ task_id: string }>(at(site, "/crawl/start"), body),
  cancel: (site: string, taskId: string) =>
    api.post<{ ok: boolean }>(at(site, `/crawl/tasks/${encodeURIComponent(taskId)}/cancel`)),
  /** 糖：一批岗位直接进匹配（等价 `match.analyze` + `crawl_task_id`） */
  matchTask: (site: string, taskId: string, body: { resume_id: string }) =>
    api.post<{ task_id: string }>(at(site, `/crawl/tasks/${encodeURIComponent(taskId)}/match`), body),
};

/* ------------------------------------------------------------------ 发对话与投递 */

/**
 * 一次投递请求（`DeliverBody`）。
 *
 * `mode` 三档：`all` 全量 / `selected` 勾选 / `manual` 逐条；
 * 受限职位在 `selected`/`manual` 下要 `confirmed`（配 `confirmation_token`）才发。
 */
export interface DeliverStartBody {
  analysis_id: string;
  mode?: "all" | "selected" | "manual";
  confirmed?: boolean;
  confirmation_token?: string;
  encrypt_job_ids?: string[];
}

export const deliverApi = {
  /** 投递评分阈值（`min_score`，0–100） */
  config: (site: string) => api.get<{ min_score: number }>(at(site, "/deliver/config")),
  saveConfig: (site: string, body: { min_score: number }) =>
    api.put<{ min_score: number }>(at(site, "/deliver/config"), body),
  start: (site: string, body: DeliverStartBody) =>
    api.post<{ task_id: string }>(at(site, "/deliver/start"), body),
  status: (site: string) => api.get<Record<string, unknown>>(at(site, "/deliver/status")),
  cancel: (site: string, taskId: string) =>
    api.post<{ ok: boolean }>(at(site, `/deliver/${encodeURIComponent(taskId)}/cancel`)),
};

/* ------------------------------------------------------------------ 搜索条件与期望流 */

export const filtersApi = {
  /** 选项表；`caps.catalog.unsupported` 里点名的维度要置灰，别硬塞 */
  conditions: (site: string) =>
    api.get<Record<string, unknown>>(at(site, "/filters/conditions")).then(normalizeConditions),
  search: (site: string) => api.get<SearchFilterPayload>(at(site, "/filters/search")),
  saveSearch: (site: string, body: Partial<SearchFilterPayload>) =>
    api.put<SearchFilterPayload>(at(site, "/filters/search"), body),
  resetSearch: (site: string) =>
    api.post<SearchFilterPayload>(at(site, "/filters/search/reset")),

  /**
   * 采集配置（搜索条件 + 抓取参数 + 期望流）。
   * GET 回 `{search, collection}` 两块，PUT 收**压平**的一份（`CollectionBody`）——
   * 拼装见 `collection.ts` 的 `toSaveBody`，别在视图里现拼。
   */
  collectionConfig: (site: string) =>
    api.get<CollectionConfigPayload>(at(site, "/collection/config")),
  saveCollectionConfig: (site: string, body: CollectionSaveBody) =>
    api.put<CollectionConfigPayload & { saved: boolean }>(at(site, "/collection/config"), body),
  resetCollectionConfig: (site: string) =>
    api.post<CollectionConfigPayload & { saved: boolean }>(at(site, "/collection/config/reset")),

  /**
   * 在线拉当前账号的求职期望列表。
   * 只有 `caps.source.expect_flow` 为真的平台才该调——调用方按 caps 决定出不出入口，
   * 失败后端抛 `UpstreamError` 带人话，直接 `toast(msg, "danger")`。
   */
  expects: (site: string) =>
    api.get<ExpectLists>(at(site, "/collection/expects")),
};

/* ------------------------------------------------------------------ 采集配置 DTO */

/** 选项（后端 `FilterOption.to_dict`）；`lowSalary`/`highSalary` 只有薪资档有。 */
export interface FilterOption {
  code: string;
  name: string;
  lowSalary?: number;
  highSalary?: number;
}

/** 城市树节点（省 → 市 → 区）。 */
export interface CityNode {
  code: string;
  name: string;
  children?: CityNode[];
}

/** 行业分组（后端 `IndustryGroup`）。 */
export interface IndustryGroup {
  name: string;
  options: FilterOption[];
}

/** `GET /filters/conditions`：7 类筛选维度 + 来源标记。 */
export interface FilterConditions {
  cities: CityNode[];
  job_types: FilterOption[];
  salaries: FilterOption[];
  experiences: FilterOption[];
  degrees: FilterOption[];
  industries: IndustryGroup[];
  scales: FilterOption[];
  pay_types: FilterOption[];
  stages: FilterOption[];
  part_times: FilterOption[];
  hot_cities: FilterOption[];
  /** `api` / `fallback` / `html` */
  source?: string;
  /** 被降级的来源，如 `{cities: "fallback"}` */
  degraded?: Record<string, string>;
}

/**
 * 后端 `FilterConditions.to_dict()` 回的是驼峰单数键（`city`/`hotCity`/`jobType`…），
 * 前端统一用字段名复数键——在这层换名，视图只认 `FilterConditions` 的形状。
 * 两套键都认：后端哪天换键这里不用跟着改。
 */
export function normalizeConditions(raw: Record<string, unknown> | null | undefined): FilterConditions {
  const r = raw ?? {};
  const pick = (snake: string, camel: string): unknown => r[snake] ?? r[camel];
  return {
    cities: (pick("cities", "city") as CityNode[]) ?? [],
    hot_cities: (pick("hot_cities", "hotCity") as FilterOption[]) ?? [],
    job_types: (pick("job_types", "jobType") as FilterOption[]) ?? [],
    salaries: (pick("salaries", "salary") as FilterOption[]) ?? [],
    experiences: (pick("experiences", "experience") as FilterOption[]) ?? [],
    degrees: (pick("degrees", "degree") as FilterOption[]) ?? [],
    industries: (pick("industries", "industry") as IndustryGroup[]) ?? [],
    scales: (pick("scales", "scale") as FilterOption[]) ?? [],
    pay_types: (pick("pay_types", "payType") as FilterOption[]) ?? [],
    stages: (pick("stages", "stage") as FilterOption[]) ?? [],
    part_times: (pick("part_times", "partTime") as FilterOption[]) ?? [],
    source: typeof r.source === "string" ? r.source : undefined,
    degraded: (r.degraded as Record<string, string>) ?? {},
  };
}

/**
 * 搜索条件（后端 `_filter_payload`）。
 * 维度用接口的驼峰名（`jobType`/`payType`…），与 `CollectionBody` 对齐。
 */
export interface SearchFilterPayload {
  query: string;
  city: string;
  jobType: string;
  salary: string;
  experience: string[];
  degree: string[];
  industry: string[];
  scale: string[];
  payType: string[];
  partTime: string[];
  stage: string[];
  /** 期望上下文 id——注意 GET 回的是驼峰，PUT 要的是 `encrypt_expect_id` */
  encryptExpectId: string;
  pageSize: number;
  is_blank?: boolean;
  summary?: string;
  [k: string]: unknown;
}

/** 采集参数（后端 `CollectionConfig`）。 */
export interface CollectionParams {
  max_pages: number;
  start_page: number;
  page_interval: number;
  use_search: boolean;
  fetch_details: boolean;
  detail_interval: number;
  store_blacklisted: boolean;
  /** `""` 搜索流 / `expect` / `recommend` / `part_time` / `temp_expect` */
  expect_kind: string;
  encrypt_expect_id: string;
  mix_expect_type: string;
  expect_info: string;
  /** 仅展示（摘要用），不进请求参数 */
  expect_name: string;
  [k: string]: unknown;
}

/** `GET /collection/config` 回两块；`PUT` 收压平的一份。 */
export interface CollectionConfigPayload {
  search: SearchFilterPayload;
  collection: CollectionParams;
}

/** `PUT /collection/config` 的载荷 = 搜索条件字段 + 采集参数字段，压平一份。 */
export interface CollectionSaveBody extends Partial<SearchFilterPayload>, Partial<CollectionParams> {}

/** 求职期望列表行（后端 `_row`）。 */
export interface ExpectRow {
  kind: string;
  encryptExpectId: string;
  positionName: string;
  locationName: string;
  tagName: string;
  mixExpectType: string;
  label: string;
}

export interface ExpectLists {
  mix: ExpectRow | null;
  expects: ExpectRow[];
  partTime: ExpectRow[];
}

/** 查询串：跳过 `undefined`/`null`/空串。 */
function qs(params: object): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}
