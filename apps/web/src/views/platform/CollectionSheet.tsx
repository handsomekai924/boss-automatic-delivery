/**
 * 采集条件表单（JobsPage 的「编辑采集条件」Sheet）。
 *
 * 两块配置一次改完（搜索条件 + 采集参数 + 期望流来源），保存压平走 `toSaveBody`。
 * caps 降级（docs 3.3 / 成功标准 #3）：
 * - `caps.catalog.unsupported` 点名的维度 → 置灰并标注「该平台不支持」，不硬塞；
 * - `caps.source.expect_flow` 为假 → 期望流来源整档不出现（不是点了报错）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, Cascader, InlineAlert, MultiSelect, Pill, SegmentedControl, Select, Sheet, Switch, TextField,
  type CascaderNode, type SelectOption,
} from "../../components";
import type { PlatformCaps } from "../../platform/types";
import { radius, typeScale } from "../../theme/tokens";
import { fmtTime } from "../../utils/format";
import { filtersApi, type FilterConditions, type FilterOption, type ExpectRow } from "./repos";
import { useExpectStore } from "./expectStore";
import {
  choiceKeyOf, emptyCollectionConfig, expectChoices, toSaveBody,
  type CollectionConfig, type ExpectKind,
} from "./collection";

/** 维度标题（`CriteriaKey` → 人话）；顺序即表单顺序。 */
const DIM_LABEL: Record<string, string> = {
  keyword: "关键词",
  city: "城市",
  job_type: "职位类型",
  salary: "薪资",
  experience: "经验",
  degree: "学历",
  industry: "行业",
  scale: "规模",
  stage: "阶段",
};

type Source = "search" | "expect";

function toOptions(list: FilterOption[] | undefined): SelectOption[] {
  return (list ?? []).map((o) => ({ code: o.code, name: o.name }));
}

/** 行业分组 → 级联节点：组只导航、选项取值（选项 code 全局唯一，组码加前缀防撞）。 */
function industryNodes(conditions: FilterConditions | null): CascaderNode[] {
  return (conditions?.industries ?? [])
    .filter((g) => g.options.length > 0)
    .map((g) => ({
      code: `group:${g.name}`,
      name: g.name,
      children: g.options.map((o) => ({ code: o.code, name: o.name })),
    }));
}

/** 采集参数行：左文案、右数字（提议「采集参数分别一行」）。 */
function ParamRow({
  label, value, min, max, step, onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange: (n: number) => void;
}) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 2 }}>
      <Typography sx={{ ...typeScale.bodySm }}>{label}</Typography>
      <TextField
        type="number"
        aria-label={label}
        value={String(value)}
        onChange={(e) => onChange(Number(e.target.value) || 0)}
        inputProps={{ min, max, step }}
        sx={{ width: 110 }}
      />
    </Box>
  );
}

export interface CollectionSheetProps {
  site: string;
  caps: PlatformCaps | null;
  open: boolean;
  onClose: () => void;
  /** 当前配置（打开时的初值） */
  value: CollectionConfig;
  onSaved: (next: CollectionConfig) => void;
}

export function CollectionSheet({ site, caps, open, onClose, value, onSaved }: CollectionSheetProps) {
  const [draft, setDraft] = useState<CollectionConfig>(value);
  const [conditions, setConditions] = useState<FilterConditions | null>(null);
  const [expects, setExpects] = useState<{ mix: ExpectRow | null; expects: ExpectRow[]; partTime: ExpectRow[] } | null>(null);
  /** 保存 / 重置分开记在飞动作：按钮各自转文案，又互斥禁用（同时只发一个请求）。 */
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const syncedAt = useExpectStore((s) => s.bySite[site]?.at ?? 0);

  const canExpect = caps?.source.expect_flow !== false;
  const unsupported = useMemo(
    () => new Set(caps?.catalog.unsupported ?? []),
    [caps],
  );

  // 每次打开重置草稿，免得上次没保存的改动漏进下一次
  useEffect(() => {
    if (open) {
      setDraft(value);
      setError(null);
    }
  }, [open, value]);

  useEffect(() => {
    if (!open || !site) return;
    void filtersApi.conditions(site).then(setConditions).catch((e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    });
    if (canExpect) {
      // 期望列表走缓存（登录时拉好、手动同步刷新），不在每次打开都打真接口
      void useExpectStore.getState().load(site).then(setExpects).catch(() => setExpects(null));
    }
  }, [open, site, canExpect]);

  const source: Source = draft.collection.expect_kind ? "expect" : "search";
  const choices = useMemo(
    () => (expects ? expectChoices(expects) : []),
    [expects],
  );
  const choiceKey = choiceKeyOf(draft.collection);

  const patchSearch = useCallback((patch: Partial<CollectionConfig["search"]>) => {
    setDraft((d) => ({ ...d, search: { ...d.search, ...patch } }));
  }, []);
  const patchCollection = useCallback((patch: Partial<CollectionConfig["collection"]>) => {
    setDraft((d) => ({ ...d, collection: { ...d.collection, ...patch } }));
  }, []);

  /** 选中某个期望 → 回填 kind / id / 展示名。 */
  const applyChoice = useCallback((key: string) => {
    const hit = choices.find((c) => c.key === key);
    if (!hit) {
      patchCollection({ expect_kind: "", encrypt_expect_id: "", mix_expect_type: "", expect_name: "" });
      return;
    }
    patchCollection({
      expect_kind: hit.kind,
      encrypt_expect_id: hit.encrypt_expect_id,
      mix_expect_type: hit.mix_expect_type,
      expect_name: hit.label,
    });
  }, [choices, patchCollection]);

  /** 期望列表同步（求职期望并入采集条件后，「同步」从旧期望页搬到这里）。 */
  const syncExpects = useCallback(async () => {
    setSyncing(true);
    try {
      const lists = await useExpectStore.getState().load(site, { force: true });
      setExpects(lists);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  }, [site]);

  const save = useCallback(async () => {
    if (saving || resetting) return;
    setSaving(true);
    setError(null);
    try {
      const next = await filtersApi.saveCollectionConfig(site, toSaveBody(draft) as never);
      onSaved({ search: next.search, collection: next.collection });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [site, draft, onSaved, onClose, saving, resetting]);

  const reset = useCallback(async () => {
    if (saving || resetting) return;
    setResetting(true);
    setError(null);
    try {
      const next = await filtersApi.resetCollectionConfig(site);
      const cfg = { search: next.search, collection: next.collection };
      setDraft(cfg);
      onSaved(cfg);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setResetting(false);
    }
  }, [site, onSaved, saving, resetting]);

  /** 保存/重置任一在飞都锁住整排动作按钮（同时只发一个请求）。 */
  const busy = saving || resetting;

  /** 维度是否置灰（caps 点名就不给配）。 */
  const off = (key: string) => unsupported.has(key as never);

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="采集条件"
      width={560}
      footer={
        <Box sx={{ display: "flex", gap: 1, justifyContent: "space-between" }}>
          <Button variant="plain" disabled={busy} onClick={() => void reset()}>
            {resetting ? "重置中…" : "重置为默认"}
          </Button>
          <Box sx={{ display: "flex", gap: 1 }}>
            <Button variant="gray" disabled={busy} onClick={onClose}>取消</Button>
            <Button variant="filled" disabled={busy} onClick={() => void save()}>
              {saving ? "保存中…" : "保存"}
            </Button>
          </Box>
        </Box>
      }
    >
      <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
        {error && <InlineAlert tone="danger">{error}</InlineAlert>}

        {/* 期望流整档按 caps 出现；不支持就不出假入口 */}
        {canExpect ? (
          <Box>
            <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1 }}>
              采集来源
            </Typography>
            <SegmentedControl
              aria-label="采集来源"
              value={source}
              onChange={(v) => {
                if (v === "search") {
                  patchCollection({ expect_kind: "", encrypt_expect_id: "", mix_expect_type: "", expect_name: "" });
                } else if (!draft.collection.expect_kind) {
                  // 进期望流但还没选具体期望 → 落到推荐流，别停在「选了源却没参数」
                  patchCollection({ expect_kind: "recommend", expect_name: "推荐期望流" });
                }
              }}
              options={[
                { value: "search", label: "关键词搜索" },
                { value: "expect", label: "期望流" },
              ]}
            />
            {source === "expect" && (
              /* 求职期望并入采集条件（提议 1.2）：期望清单 + 同步都在这里，不再单开页 */
              <Box sx={{ mt: 1.5, display: "flex", flexDirection: "column", gap: 1 }}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <Typography sx={{ ...typeScale.xs, color: "text.secondary", flex: 1, minWidth: 0 }}>
                    {syncedAt > 0 ? `期望列表上次同步：${fmtTime(syncedAt)}` : "期望建在招聘站那边，本地只读"}
                  </Typography>
                  <Button size="small" variant="gray" disabled={syncing} onClick={() => void syncExpects()}>
                    {syncing ? "同步中…" : "同步"}
                  </Button>
                </Box>
                <Box sx={{ borderRadius: radius.s, bgcolor: "surface2", overflow: "hidden" }}>
                  {choices.map((c, i) => {
                    const active = c.key === choiceKey;
                    return (
                      <Box
                        key={c.key}
                        component="button"
                        type="button"
                        aria-pressed={active}
                        onClick={() => applyChoice(c.key)}
                        sx={{
                          display: "flex",
                          alignItems: "center",
                          gap: 1.5,
                          width: "100%",
                          px: 1.5,
                          py: 1,
                          border: 0,
                          borderTop: i === 0 ? 0 : "1px solid",
                          borderColor: "divider",
                          bgcolor: active
                            ? (t) => (t.palette.mode === "dark" ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.04)")
                            : "transparent",
                          textAlign: "left",
                          cursor: "pointer",
                          "&:hover": { bgcolor: (t) => (t.palette.mode === "dark" ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.05)") },
                        }}
                      >
                        <Box sx={{ flex: 1, minWidth: 0 }}>
                          <Typography sx={{ ...typeScale.bodySm, fontWeight: active ? 590 : 500 }}>{c.label}</Typography>
                          {c.detail && (
                            <Typography sx={{ ...typeScale.xs, color: "text.secondary" }} noWrap>{c.detail}</Typography>
                          )}
                        </Box>
                        {active && <Pill tone="accent">使用中</Pill>}
                      </Box>
                    );
                  })}
                </Box>
                {expects == null && (
                  <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>
                    期望列表要在线拉，拉不到就先用搜索条件。
                  </Typography>
                )}
              </Box>
            )}
          </Box>
        ) : (
          <InlineAlert tone="info">
            这个平台没有求职期望流（caps.source.expect_flow 为假），只走关键词搜索。
          </InlineAlert>
        )}

        <Box>
          <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1 }}>
            搜索条件
          </Typography>
          {source === "expect" && (
            <Typography sx={{ ...typeScale.xs, color: "text.secondary", mb: 1 }}>
              期望流下这些维度多数不进请求串，留着也行，切回搜索条件才生效。
            </Typography>
          )}
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
            <TextField
              label={DIM_LABEL.keyword}
              value={draft.search.query}
              onChange={(e) => patchSearch({ query: e.target.value })}
              disabled={off("keyword")}
              fullWidth
            />
            {/* 缺陷「城市仅有热门城市」：级联走全量城市树（省 → 市），叶子即市级 code */}
            <Cascader
              label={DIM_LABEL.city}
              nodes={conditions?.cities ?? []}
              value={draft.search.city}
              onChange={(v) => patchSearch({ city: typeof v === "string" ? v : (v[0] ?? "") })}
              disabled={off("city")}
              sx={{ width: "100%" }}
            />
            <Select
              label={DIM_LABEL.job_type}
              options={toOptions(conditions?.job_types)}
              value={draft.search.jobType}
              onChange={(v) => patchSearch({ jobType: v })}
              disabled={off("job_type")}
              emptyLabel="不限"
              fullWidth
            />
            <Select
              label={DIM_LABEL.salary}
              options={toOptions(conditions?.salaries)}
              value={draft.search.salary}
              onChange={(v) => patchSearch({ salary: v })}
              disabled={off("salary")}
              emptyLabel="不限"
              fullWidth
            />
            <MultiSelect
              label={DIM_LABEL.experience}
              options={toOptions(conditions?.experiences)}
              values={draft.search.experience}
              onChange={(v) => patchSearch({ experience: v })}
              disabled={off("experience")}
            />
            <MultiSelect
              label={DIM_LABEL.degree}
              options={toOptions(conditions?.degrees)}
              values={draft.search.degree}
              onChange={(v) => patchSearch({ degree: v })}
              disabled={off("degree")}
            />
            {/* 缺陷「行业无法级联选择」：组导航、选项勾选，确定才提交 */}
            <Cascader
              multiple
              label={DIM_LABEL.industry}
              nodes={industryNodes(conditions)}
              value={draft.search.industry}
              onChange={(v) => patchSearch({ industry: Array.isArray(v) ? v : v ? [v] : [] })}
              disabled={off("industry")}
              sx={{ width: "100%" }}
            />
            <MultiSelect
              label={DIM_LABEL.scale}
              options={toOptions(conditions?.scales)}
              values={draft.search.scale}
              onChange={(v) => patchSearch({ scale: v })}
              disabled={off("scale")}
            />
            <MultiSelect
              label={DIM_LABEL.stage}
              options={toOptions(conditions?.stages)}
              values={draft.search.stage}
              onChange={(v) => patchSearch({ stage: v })}
              disabled={off("stage")}
            />
            {unsupported.size > 0 && (
              <Box
                sx={{
                  p: 1.25,
                  borderRadius: radius.s,
                  bgcolor: "surface2",
                  display: "flex",
                  flexDirection: "column",
                  gap: 0.5,
                }}
              >
                <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>
                  这个平台不支持的维度已置灰：
                  {[...unsupported].map((k) => DIM_LABEL[k] ?? k).join("、")}
                </Typography>
              </Box>
            )}
            {conditions?.source === "fallback" && (
              <Typography sx={{ ...typeScale.xs, color: "warning.main" }}>
                选项表是离线兜底值，可能不是最新的。
              </Typography>
            )}
          </Box>
        </Box>

        <Box>
          <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1 }}>
            采集参数
          </Typography>
          {/* 提议「采集参数分别一行」：左文案右数字 */}
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1.25 }}>
            <ParamRow
              label="最多几页"
              value={draft.collection.max_pages ?? 5}
              min={1}
              max={10}
              onChange={(n) => patchCollection({ max_pages: n || 1 })}
            />
            <ParamRow
              label="起始页"
              value={draft.collection.start_page ?? 1}
              min={1}
              onChange={(n) => patchCollection({ start_page: n || 1 })}
            />
            <ParamRow
              label="翻页间隔（秒）"
              value={draft.collection.page_interval ?? 1}
              min={0}
              max={30}
              step={0.5}
              onChange={(n) => patchCollection({ page_interval: n })}
            />
          </Box>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1, mt: 1.5 }}>
            <Switch
              label="顺带抓职位描述（JD）"
              checked={draft.collection.fetch_details !== false}
              onChange={(e) => patchCollection({ fetch_details: e.target.checked })}
            />
            <Switch
              label="保存并标记黑名单职位"
              checked={draft.collection.store_blacklisted !== false}
              onChange={(e) => patchCollection({ store_blacklisted: e.target.checked })}
            />
          </Box>
        </Box>
      </Box>
    </Sheet>
  );
}

export { emptyCollectionConfig };
export type { CollectionConfig, ExpectKind };
