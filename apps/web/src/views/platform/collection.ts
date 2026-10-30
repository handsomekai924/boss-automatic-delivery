/**
 * 采集配置的纯函数面：摘要文案、PUT 载荷拼装、期望流键解析。
 *
 * 后端 GET 回 `{search, collection}` 两块、PUT 收压平的一份（`CollectionBody`），
 * 字段名搜索块是驼峰、采集块是下划线——都在这里收口，视图别自己拼。
 * 摘要文案是 legacy `jobs.js collectionSummary` 的原样移植（含期望流/搜索流生效维度差异）。
 */

import type {
  CityNode, CollectionParams, ExpectRow, FilterConditions, SearchFilterPayload,
} from "./repos";

/** 摘要里要翻译的维度名（与搜索条件字段一一对应）。 */
export type LabelKind =
  | "city" | "jobType" | "salary" | "experience" | "degree"
  | "industry" | "scale" | "payType" | "partTime" | "stage";

/** code → 展示名；查不到就原样回（配置里存名字的场景直接过）。 */
export type LabelLookup = (kind: LabelKind, code: string) => string;

function flattenCities(nodes: CityNode[] | undefined, out: { code: string; name: string }[] = []): { code: string; name: string }[] {
  for (const n of nodes ?? []) {
    out.push({ code: n.code, name: n.name });
    flattenCities(n.children, out);
  }
  return out;
}

/**
 * 用选项表把配置里的 code 翻回原文本（缺陷「当前采集条件不要显示代码」）。
 * 选项表拿不到时回退成恒等映射，摘要照旧显示原值，不挡主流程。
 */
export function labelLookup(conditions: FilterConditions | null | undefined): LabelLookup {
  const maps: Partial<Record<LabelKind, Map<string, string>>> = {};
  const fill = (kind: LabelKind, rows: { code: string; name: string }[] | undefined) => {
    const m = new Map<string, string>();
    for (const o of rows ?? []) m.set(String(o.code), o.name);
    maps[kind] = m;
  };
  fill("city", flattenCities(conditions?.cities));
  fill("jobType", conditions?.job_types);
  fill("salary", conditions?.salaries);
  fill("experience", conditions?.experiences);
  fill("degree", conditions?.degrees);
  fill("industry", (conditions?.industries ?? []).flatMap((g) => g.options));
  fill("scale", conditions?.scales);
  fill("payType", conditions?.pay_types);
  fill("partTime", conditions?.part_times);
  fill("stage", conditions?.stages);
  return (kind, code) => maps[kind]?.get(code) ?? code;
}

/** 期望流形态（`CollectionParams.expect_kind`）。 */
export type ExpectKind = "" | "expect" | "recommend" | "part_time" | "temp_expect";

/** 采集配置两块（GET 载荷）。 */
export interface CollectionConfig {
  search: SearchFilterPayload;
  collection: CollectionParams;
}

/** 表单里的一条期望选项（采集条件 Sheet 的期望流列表行）。 */
export interface ExpectChoice {
  /** 下拉键：`recommend` / `part_time` / `expect:<id>` / `temp` */
  key: string;
  label: string;
  /** 一句明细（职位 · 城市 · 标签），列表行副文案 */
  detail: string;
  kind: ExpectKind;
  encrypt_expect_id: string;
  mix_expect_type: string;
}

/** 期望行 → 一句明细（职位 · 城市 · 标签，空段跳过）。 */
function expectDetail(r: ExpectRow): string {
  return [r.positionName, r.locationName, r.tagName].filter(Boolean).join(" · ");
}

/** 把期望列表摊平成列表行：混合推荐 + 具名期望 + 兼职；末尾挂临时期望。 */
export function expectChoices(
  lists: { mix?: ExpectRow | null; expects?: ExpectRow[]; partTime?: ExpectRow[] } | null | undefined,
  withTemp = true,
): ExpectChoice[] {
  const out: ExpectChoice[] = [];
  const mix = lists?.mix ?? null;
  const named = Array.isArray(lists?.expects) ? lists!.expects! : [];
  const partTimeRows = Array.isArray(lists?.partTime) ? lists!.partTime! : [];
  if (mix) {
    out.push({
      key: "recommend",
      label: mix.label || "推荐期望流",
      detail: expectDetail(mix),
      kind: "recommend",
      encrypt_expect_id: mix.encryptExpectId,
      mix_expect_type: mix.mixExpectType,
    });
  }
  for (const e of named) {
    out.push({
      key: `expect:${e.encryptExpectId}`,
      label: e.label || e.positionName || "未命名期望",
      detail: expectDetail(e),
      kind: "expect",
      encrypt_expect_id: e.encryptExpectId,
      mix_expect_type: e.mixExpectType,
    });
  }
  for (const e of partTimeRows) {
    out.push({
      key: `part_time`,
      label: e.label || "兼职期望流",
      detail: expectDetail(e),
      kind: "part_time",
      encrypt_expect_id: e.encryptExpectId,
      mix_expect_type: e.mixExpectType,
    });
  }
  if (withTemp) {
    out.push({
      key: "temp",
      label: "临时期望流",
      detail: "临时配的条件，只在下一次采集生效",
      kind: "temp_expect",
      encrypt_expect_id: "",
      mix_expect_type: "",
    });
  }
  return out;
}

/** 把存下来的 collection 反查成下拉键（legacy `choiceKeyOf` 语义）。 */
export function choiceKeyOf(collection: CollectionParams): string {
  const kind = (collection.expect_kind || "").trim();
  if (kind === "recommend") return "recommend";
  if (kind === "part_time") return "part_time";
  if (kind === "temp_expect") return "temp";
  if (kind === "expect" && collection.encrypt_expect_id) return `expect:${collection.encrypt_expect_id}`;
  return "";
}

/**
 * 把当前采集配置拼成一行人话摘要。
 *
 * 期望流与搜索流生效维度不同，只列这次真会进请求串的项，别把「配了但不生效」的也打上去。
 * `lookup` 把配置里的维度 code 翻回原文本（不传就原样显示）。
 */
export function collectionSummary(data: CollectionConfig, lookup?: LabelLookup): string {
  const search = data?.search || ({} as SearchFilterPayload);
  const collection = data?.collection || ({} as CollectionParams);
  const t = (kind: LabelKind, code: string) => (lookup ? lookup(kind, code) : code);
  const tl = (kind: LabelKind, codes: string[]) => codes.map((c) => t(kind, c)).join("、");
  const kind = (collection.expect_kind || "").trim();
  const expectMode = !!kind;
  const partTime = kind === "part_time";
  const parts: string[] = [];
  if (search.query && !expectMode) parts.push(`关键词「${search.query}」`);
  if (search.city) parts.push(`城市 ${t("city", search.city)}`);
  if (search.jobType && !expectMode) parts.push(`职位类型 ${t("jobType", search.jobType)}`);
  if (search.salary && !partTime) parts.push(`薪资 ${t("salary", search.salary)}`);
  if (Array.isArray(search.experience) && search.experience.length) {
    parts.push(`经验 ${tl("experience", search.experience)}`);
  }
  if (Array.isArray(search.degree) && search.degree.length) {
    parts.push(`学历 ${tl("degree", search.degree)}`);
  }
  if (Array.isArray(search.industry) && search.industry.length) {
    parts.push(`行业 ${tl("industry", search.industry)}`);
  }
  if (Array.isArray(search.scale) && search.scale.length) {
    parts.push(`规模 ${tl("scale", search.scale)}`);
  }
  if (Array.isArray(search.payType) && search.payType.length && partTime) {
    parts.push(`薪资方式 ${tl("payType", search.payType)}`);
  }
  if (Array.isArray(search.partTime) && search.partTime.length && partTime) {
    parts.push(`兼职 ${tl("partTime", search.partTime)}`);
  }
  if (Array.isArray(search.stage) && search.stage.length && !expectMode) {
    parts.push(`阶段 ${tl("stage", search.stage)}`);
  }
  if (kind === "part_time") {
    parts.push("兼职期望流");
  } else if (kind === "expect") {
    parts.push(`期望流「${collection.expect_name || collection.encrypt_expect_id || "未命名"}」`);
  } else if (kind === "recommend") {
    parts.push("推荐期望流");
  } else if (kind === "temp_expect") {
    parts.push(`临时期望流「${collection.expect_name || "未命名"}」`);
  } else {
    const ctx = collection.encrypt_expect_id || search.encryptExpectId || "";
    if (ctx) {
      parts.push(`使用搜索条件 · 期望上下文「${collection.expect_name || ctx}」`);
    } else {
      parts.push("使用搜索条件");
    }
  }
  parts.push(`最多 ${collection.max_pages ?? 5} 页`);
  parts.push(collection.fetch_details === false ? "不抓 JD" : "抓取 JD");
  parts.push(collection.store_blacklisted === false ? "不保存黑名单职位" : "保存并标记黑名单");
  return parts.join(" · ") || "不限（未设置筛选维度）";
}

/**
 * 两块配置压平成 PUT 载荷（`CollectionBody`）。
 *
 * 注意 `encrypt_expect_id` 是下划线名（GET 的搜索块里叫 `encryptExpectId`），
 * 后端 `data.get("encrypt_expect_id")` 搜索流与期望流共用同一份。
 */
export function toSaveBody(cfg: CollectionConfig): Record<string, unknown> {
  const s = cfg.search;
  const c = cfg.collection;
  return {
    query: s.query ?? "",
    city: s.city ?? "",
    jobType: s.jobType ?? "",
    salary: s.salary ?? "",
    experience: s.experience ?? [],
    degree: s.degree ?? [],
    industry: s.industry ?? [],
    scale: s.scale ?? [],
    payType: s.payType ?? [],
    partTime: s.partTime ?? [],
    stage: s.stage ?? [],
    pageSize: s.pageSize ?? 15,
    max_pages: c.max_pages ?? 5,
    start_page: c.start_page ?? 1,
    page_interval: c.page_interval ?? 1,
    use_search: c.use_search ?? true,
    fetch_details: c.fetch_details ?? true,
    detail_interval: c.detail_interval ?? 1,
    store_blacklisted: c.store_blacklisted ?? true,
    expect_kind: c.expect_kind ?? "",
    encrypt_expect_id: c.encrypt_expect_id ?? "",
    mix_expect_type: c.mix_expect_type ?? "",
    expect_info: c.expect_info ?? "",
    expect_name: c.expect_name ?? "",
  };
}

/** 空配置（表单初值 / reset 后的兜底）。 */
export function emptyCollectionConfig(): CollectionConfig {
  return {
    search: {
      query: "",
      city: "",
      jobType: "",
      salary: "",
      experience: [],
      degree: [],
      industry: [],
      scale: [],
      payType: [],
      partTime: [],
      stage: [],
      encryptExpectId: "",
      pageSize: 15,
    },
    collection: {
      max_pages: 5,
      start_page: 1,
      page_interval: 1,
      use_search: true,
      fetch_details: true,
      detail_interval: 1,
      store_blacklisted: true,
      expect_kind: "",
      encrypt_expect_id: "",
      mix_expect_type: "",
      expect_info: "",
      expect_name: "",
    },
  };
}
