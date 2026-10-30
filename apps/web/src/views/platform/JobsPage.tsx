/**
 * 找工作（平台作用域 `#/:site/jobs`）：采集入口 + 库内检索 + 卡片流 + 详情。
 *
 * 采集条件是站点作用域配置（`/api/sites/{site}/collection/config`），就地编辑
 * （`CollectionSheet`），不在共用设置页里配——搜索维度每个平台都不一样。
 * caps 降级（docs 3.3）：`catalog.unsupported` 维度置灰、`source.expect_flow`
 * 为假就不出期望流入口；差异只走描述符，视图里不写 `site ===`。
 *
 * 采集/补 JD 的细进度统一在任务中心看（本页只跟到结束好收结果），与 legacy 同款分工。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Checkbox, Typography } from "@mui/material";
import { useNavigate } from "react-router-dom";

import {
  Button, ConfirmSheet, EmptyState, InlineAlert, JobCard, JobDetailDrawer, PageHeader, Pill,
  ScrollArea, SearchFilterBar, Select, Skeleton, Surface, TextField, pageRootSx, useToast,
} from "../../components";
import type { JobDetail, JobSummary } from "../../api/dto";
import { usePlatform } from "../../platform/PlatformProvider";
import { useTaskStore, type TaskSnapshot } from "../../stores/taskStore";
import { fmtNumber } from "../../utils/format";
import { radius, typeScale } from "../../theme/tokens";
import { crawlApi, filtersApi, jobsApi, type FilterConditions, type JobItem } from "./repos";
// 面试题摘要是共用能力（跨站点），不在这层站点作用域的 repos 里
import { interviewApi } from "../shared/repos";
import { CollectionSheet } from "./CollectionSheet";
import {
  collectionSummary, emptyCollectionConfig, labelLookup, type CollectionConfig,
} from "./collection";

const PAGE_SIZE = 24;

const CATEGORY_OPTIONS = [
  { code: "ordinary", name: "普通职位" },
  { code: "company_blacklisted", name: "公司黑名单" },
  { code: "jd_blacklisted", name: "JD 黑名单" },
  { code: "both", name: "公司与 JD 黑名单" },
  { code: "jd_not_collected", name: "JD 未采集" },
  { code: "jd_failed", name: "JD 采集失败" },
];

const JD_STATUS_OPTIONS = [
  { code: "pending", name: "待采集" },
  { code: "collecting", name: "采集中" },
  { code: "completed", name: "已完成" },
  { code: "empty", name: "JD 为空" },
  { code: "failed", name: "采集失败" },
  { code: "unchecked", name: "规则未检查" },
];

/** 还没收口的原始 status（补 JD / 采集共用）。 */
const OPEN_STATUS = new Set(["running", "pending", "waiting_human"]);
const DONE_STATUS = new Set(["done", "error", "cancelled"]);

/**
 * 后端职位主键叫 `encrypt_job_id`，公用组件的 DTO 叫 `job_id`——
 * 在这层换名，组件库保持跨平台中立。
 */
function toSummary(j: JobItem): JobSummary {
  return { ...(j as unknown as JobSummary), job_id: j.encrypt_job_id };
}

function toDetail(j: JobItem): JobDetail {
  return { ...(j as unknown as JobDetail), job_id: j.encrypt_job_id };
}

export function JobsPage() {
  const toast = useToast();
  const navigate = useNavigate();
  const { activeSiteId, activePlatform } = usePlatform();
  const site = activeSiteId;
  const caps = activePlatform?.caps ?? null;
  const tasks = useTaskStore((s) => s.tasks);

  const [items, setItems] = useState<JobItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [keyword, setKeyword] = useState("");
  const [city, setCity] = useState("");
  const [category, setCategory] = useState("");
  const [jdStatus, setJdStatus] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  // 首屏必拉一页：初值 true，别让「库还是空的」空态在拉取前闪一下
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [collection, setCollection] = useState<CollectionConfig>(emptyCollectionConfig);
  /** 采集条件摘要也是拉回来的内容——拉取窗口用骨架占位，免得空摘要先闪一下。 */
  const [collectionLoading, setCollectionLoading] = useState(true);
  const [conditions, setConditions] = useState<FilterConditions | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  /** 破坏性操作的二次确认（弹窗，不再用 window.confirm）。 */
  const [confirmAction, setConfirmAction] = useState<{
    title: string;
    description: string;
    confirmText: string;
    run: () => Promise<void>;
  } | null>(null);
  /** 确认弹窗里的请求在飞：弹窗不收、按钮转文案并禁用，防连点重复删。 */
  const [confirmBusy, setConfirmBusy] = useState(false);
  /** 本地「在跑」旗标：start 一按就亮，不等 WS 快照（那边可能晚一拍）。 */
  const [crawlActive, setCrawlActive] = useState(false);
  const [crawlTaskId, setCrawlTaskId] = useState<string | null>(null);
  /** 开始/停止采集的请求在飞旗标：任务跑没跑是 crawlActive，这里只管按钮防连点。 */
  const [crawlStarting, setCrawlStarting] = useState(false);
  const [crawlStopping, setCrawlStopping] = useState(false);
  const [descActive, setDescActive] = useState(false);
  const [descTaskId, setDescTaskId] = useState<string | null>(null);
  const [descStarting, setDescStarting] = useState(false);

  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [interviewCounts, setInterviewCounts] = useState<Record<string, number>>({});

  const crawlTask: TaskSnapshot | null = crawlTaskId ? tasks[crawlTaskId] ?? null : null;
  const descTask: TaskSnapshot | null = descTaskId ? tasks[descTaskId] ?? null : null;

  /** 按当前筛选拉一页库内职位。 */
  const loadJobs = useCallback(async () => {
    if (!site) return;
    setLoading(true);
    try {
      const data = await jobsApi.list(site, {
        limit: PAGE_SIZE,
        offset,
        keyword: keyword.trim() || undefined,
        city: city.trim() || undefined,
        category: category || undefined,
        jd_status: jdStatus || undefined,
      });
      setItems(data.items ?? []);
      setTotal(Number(data.total) || 0);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [site, offset, keyword, city, category, jdStatus]);

  const loadCollection = useCallback(async () => {
    if (!site) {
      setCollectionLoading(false);
      return;
    }
    setCollectionLoading(true);
    try {
      const data = await filtersApi.collectionConfig(site);
      setCollection({ search: data.search, collection: data.collection });
    } catch (e) {
      toast.toast(`读采集条件失败：${e instanceof Error ? e.message : String(e)}`, "danger");
    } finally {
      setCollectionLoading(false);
    }
    // 选项表只用来把摘要里的 code 翻回原文本，拿不到就原样显示，不挡主流程
    void filtersApi.conditions(site).then(setConditions).catch(() => setConditions(null));
  }, [site, toast]);

  /** 本页岗位的面试题数量（卡片徽章 / 按钮文案）。 */
  const loadInterviewSummary = useCallback(async (list: JobItem[]) => {
    const ids = list.map((j) => j.encrypt_job_id).filter(Boolean);
    if (!ids.length) {
      setInterviewCounts({});
      return;
    }
    try {
      const data = await interviewApi.summary({ encrypt_job_ids: ids.join(",") });
      const raw = data.items ?? {};
      const counts: Record<string, number> = {};
      for (const [id, row] of Object.entries(raw)) counts[id] = Number(row?.question_count ?? 0);
      setInterviewCounts(counts);
    } catch {
      setInterviewCounts({});
    }
  }, []);

  // 站点一换，表单与列表全清，免得把上个平台的筛选带给下一个
  useEffect(() => {
    setItems([]);
    setTotal(0);
    setOffset(0);
    setKeyword("");
    setCity("");
    setCategory("");
    setJdStatus("");
    setSelected(new Set());
    setDetailId(null);
    setDetail(null);
    setCrawlTaskId(null);
    setDescTaskId(null);
  }, [site]);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    void loadCollection();
  }, [loadCollection]);

  useEffect(() => {
    void loadInterviewSummary(items);
  }, [items, loadInterviewSummary]);

  // 进页对账一次在跑的采集 / 补 JD（可能是在别的页发起的）
  useEffect(() => {
    if (!site) return;
    void crawlApi.status(site).then((s) => {
      const id = String((s as { task_id?: string }).task_id ?? "");
      const st = String((s as { status?: string }).status ?? "");
      if (id && st && OPEN_STATUS.has(st)) {
        setCrawlTaskId(id);
        setCrawlActive(true);
      }
    }).catch(() => undefined);
    void jobsApi.fetchDescriptionsStatus(site).then((s) => {
      const id = String((s as { task_id?: string }).task_id ?? "");
      const st = String((s as { status?: string }).status ?? "");
      if (id && st && OPEN_STATUS.has(st)) {
        setDescTaskId(id);
        setDescActive(true);
      }
    }).catch(() => undefined);
  }, [site]);

  // 任务收口就复位按钮 + 重载列表（细节去任务中心看）
  useEffect(() => {
    if (crawlTask && DONE_STATUS.has(crawlTask.status)) {
      const st = crawlTask.status;
      setCrawlTaskId(null);
      setCrawlActive(false);
      void loadJobs();
      toast.toast(st === "error" ? `搜索出错：${crawlTask.error ?? ""}` : "搜索结束", st === "error" ? "danger" : "ok");
    }
  }, [crawlTask, loadJobs, toast]);

  useEffect(() => {
    if (descTask && DONE_STATUS.has(descTask.status)) {
      const st = descTask.status;
      setDescTaskId(null);
      setDescActive(false);
      void loadJobs();
      toast.toast(st === "error" ? `补全出错：${descTask.error ?? ""}` : "补全结束", st === "error" ? "danger" : "ok");
    }
  }, [descTask, loadJobs, toast]);

  const openDetail = useCallback(async (id: string) => {
    setDetailId(id);
    // 详情是异步拉的：先拿列表行撑起抽屉（标题/薪资即点即见，不空壳），
    // JD 槽先放「加载中…」占位，拉到整份详情就地替换。
    const row = items.find((j) => j.encrypt_job_id === id);
    setDetail(row ? { ...toDetail(row), job_desc: "加载中…" } : null);
    try {
      const j = await jobsApi.get(site, id);
      setDetail(toDetail(j));
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
      setDetailId(null);
    }
  }, [site, toast, items]);

  const closeDetail = useCallback(() => {
    setDetailId(null);
    setDetail(null);
  }, []);

  const runRemoveOne = useCallback(async (id: string) => {
    try {
      await jobsApi.remove(site, id);
      toast.toast("已删除", "ok");
      closeDetail();
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      await loadJobs();
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    }
  }, [site, toast, closeDetail, loadJobs]);

  /** 破坏性操作先弹窗二次确认（缺陷清单「破坏性操作」），确认后才真删。 */
  const removeOne = useCallback((id: string) => {
    setConfirmAction({
      title: "删除这条职位",
      description: "这条职位会从本地库删掉，删除后不可恢复。",
      confirmText: "确认删除",
      run: () => runRemoveOne(id),
    });
  }, [runRemoveOne]);

  const removeSelected = useCallback(() => {
    if (!selected.size) {
      toast.toast("先勾选要删的职位", "warn");
      return;
    }
    setConfirmAction({
      title: "删除所选职位",
      description: `确认删除选中的 ${selected.size} 条职位？删除后不可恢复。`,
      confirmText: "确认删除",
      run: async () => {
        try {
          const r = await jobsApi.removeMany(site, [...selected]);
          toast.toast(`已删除 ${fmtNumber(r.deleted ?? 0)} 条`, "ok");
          setSelected(new Set());
          await loadJobs();
        } catch (e) {
          toast.toast(e instanceof Error ? e.message : String(e), "danger");
        }
      },
    });
  }, [site, selected, toast, loadJobs]);

  const clearByCondition = useCallback(() => {
    const scope = keyword.trim() || city.trim()
      ? `条件（${[keyword.trim(), city.trim()].filter(Boolean).join(" / ")}）`
      : "全部";
    setConfirmAction({
      title: "按条件清空职位",
      description: `确认清空 ${scope} 的职位？此操作不可恢复。`,
      confirmText: "确认清空",
      run: async () => {
        try {
          await jobsApi.clear(site, true, {
            keyword: keyword.trim() || undefined,
            city: city.trim() || undefined,
          });
          toast.toast("已清空", "ok");
          setSelected(new Set());
          await loadJobs();
        } catch (e) {
          toast.toast(e instanceof Error ? e.message : String(e), "danger");
        }
      },
    });
  }, [site, keyword, city, toast, loadJobs]);

  const startCrawl = useCallback(async () => {
    if (crawlStarting || crawlActive) return;
    setCrawlStarting(true);
    try {
      const r = await crawlApi.start(site, {});
      setCrawlTaskId(r.task_id);
      setCrawlActive(true);
      toast.toast("搜索已开始，进度去任务中心看", "ok");
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setCrawlStarting(false);
    }
  }, [site, toast, crawlStarting, crawlActive]);

  const stopCrawl = useCallback(async () => {
    if (!crawlTaskId || crawlStopping) return;
    setCrawlStopping(true);
    try {
      await crawlApi.cancel(site, crawlTaskId);
      toast.toast("已请求停止，翻完当前页就收手", "warn");
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setCrawlStopping(false);
    }
  }, [site, crawlTaskId, crawlStopping, toast]);

  const fetchDescriptions = useCallback(async () => {
    if (descActive || descStarting) {
      if (descActive) toast.toast("补全任务已在跑", "warn");
      return;
    }
    setDescStarting(true);
    try {
      const r = await jobsApi.fetchDescriptions(site, { limit: 0 });
      setDescTaskId(r.task_id);
      setDescActive(true);
      toast.toast("补全已开始", "ok");
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setDescStarting(false);
    }
  }, [site, descActive, descStarting, toast]);

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectPage = useCallback(() => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const j of items) next.add(j.encrypt_job_id);
      return next;
    });
  }, [items]);

  const pageFrom = total === 0 ? 0 : offset + 1;
  const pageTo = Math.min(offset + PAGE_SIZE, total);

  const summaryText = useMemo(
    () => collectionSummary(collection, labelLookup(conditions)),
    [collection, conditions],
  );

  return (
    <Box sx={pageRootSx}>
      <PageHeader
        title="找工作"
        subtitle="在本地职位库浏览、筛选和补全职位描述。采集条件就地配，细进度去任务中心。"
        actions={
          <Box sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}>
            <Pill tone="static">共 {fmtNumber(total)} 条</Pill>
            <Button variant="gray" onClick={() => setSheetOpen(true)}>采集条件</Button>
            <Button
              variant="filled"
              disabled={crawlActive || crawlStarting}
              onClick={() => void startCrawl()}
            >
              {crawlStarting ? "启动中…" : crawlActive ? "采集中…" : "开始采集"}
            </Button>
            {crawlActive && (
              <Button variant="danger" disabled={crawlStopping} onClick={() => void stopCrawl()}>
                {crawlStopping ? "停止中…" : "停止采集"}
              </Button>
            )}
          </Box>
        }
      />

      <Surface sx={{ flex: "0 0 auto", mb: 2.5 }}>
        <Box sx={{ display: "flex", gap: 1.5, alignItems: "flex-start", flexWrap: "wrap" }}>
          <Box sx={{ flex: "1 1 320px", minWidth: 0 }}>
            <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 0.5 }}>
              当前采集条件
            </Typography>
            {collectionLoading ? (
              // 摘要拉取窗口：两行短骨架占位，文案到了直接替换不跳版
              <Box sx={{ display: "flex", flexDirection: "column", gap: 0.75 }}>
                <Skeleton width="70%" height={16} />
                <Skeleton width="45%" height={16} />
              </Box>
            ) : (
              <Typography sx={{ ...typeScale.bodySm }}>{summaryText}</Typography>
            )}
            {caps?.source.expect_flow && (
              <>
                <Typography sx={{ ...typeScale.xs, color: "text.secondary", mt: 0.5 }}>
                  这个平台支持求职期望流——求职期望已并入「采集条件」：来源切换、期望清单、
                  同步都在里面，改完即时生效。
                </Typography>
                <Box sx={{ mt: 1 }}>
                  <Button size="small" variant="gray" onClick={() => setSheetOpen(true)}>
                    打开采集条件
                  </Button>
                </Box>
              </>
            )}
          </Box>
        </Box>
      </Surface>

      <Surface sx={{ flex: "0 0 auto", mb: 2, display: "flex", flexDirection: "column", gap: 1.5 }}>
        <SearchFilterBar
          query={keyword}
          onQueryChange={setKeyword}
          label="在库中搜索岗位 / 公司"
          placeholder="岗位名 / 公司名，模糊匹配"
          actions={
            <>
              <TextField
                label="城市"
                value={city}
                onChange={(e) => setCity(e.target.value)}
                sx={{ width: 120 }}
              />
              <Select
                label="职位状态"
                options={CATEGORY_OPTIONS}
                value={category}
                onChange={(v) => {
                  setCategory(v);
                  setOffset(0);
                }}
                emptyLabel="全部状态"
                sx={{ minWidth: 150 }}
              />
              <Select
                label="JD 状态"
                options={JD_STATUS_OPTIONS}
                value={jdStatus}
                onChange={(v) => {
                  setJdStatus(v);
                  setOffset(0);
                }}
                emptyLabel="全部 JD 状态"
                sx={{ minWidth: 150 }}
              />
              <Button variant="gray" disabled={loading} onClick={() => setOffset(0)}>筛选</Button>
            </>
          }
        />
        <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
          <Button variant="plain" size="small" onClick={selectPage}>全选本页</Button>
          <Button variant="plain" size="small" disabled={!selected.size} onClick={removeSelected}>
            删除所选{selected.size ? `（${selected.size}）` : ""}
          </Button>
          <Button variant="plain" size="small" onClick={clearByCondition}>按条件清空</Button>
          <Button
            variant="plain"
            size="small"
            disabled={descActive || descStarting}
            onClick={() => void fetchDescriptions()}
          >
            {descStarting ? "启动中…" : descActive ? "补全中…" : "补全描述"}
          </Button>
        </Box>
      </Surface>

      {/* 内部滚动：载入错误 + 骨架 / 空态 / 职位列表 */}
      <ScrollArea>
        {loadError && <InlineAlert tone="danger">{loadError}</InlineAlert>}
        {loading && items.length === 0 ? (
          // 拉取窗口：3 行卡片骨架（约等于 JobCard 高度），数据到了直接替换不跳版
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5, mb: 2 }}>
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} height={96} sx={{ borderRadius: radius.s }} />
            ))}
          </Box>
        ) : items.length === 0 ? (
          <EmptyState
            title="库还是空的"
            description="点右上「开始采集」拉一批，或放宽上面的筛选条件。"
          />
        ) : (
          <Box
            sx={{
              display: "flex",
              flexDirection: "column",
              gap: 1.5,
              mb: 2,
              // 翻页/重筛时旧列表压暗占位（与 LibraryDataSection 同款），别整块抽走
              opacity: loading ? 0.55 : 1,
              transition: "opacity 160ms ease",
            }}
          >
            {items.map((j) => {
              const id = j.encrypt_job_id;
              const count = interviewCounts[id] ?? 0;
              return (
                <JobCard
                  key={id}
                  job={toSummary(j)}
                  onClick={() => void openDetail(id)}
                  leading={
                    <Checkbox
                      checked={selected.has(id)}
                      onChange={() => toggleSelect(id)}
                      inputProps={{ "aria-label": `选择 ${j.job_name}` }}
                    />
                  }
                  actions={
                    <Button
                      size="small"
                      variant="gray"
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate("/interview");
                      }}
                    >
                      {count ? `面试题 ${count}` : "面试准备"}
                    </Button>
                  }
                />
              );
            })}
          </Box>
        )}
      </ScrollArea>

      {/* 固定底：分页器 */}
      <Box sx={{ display: "flex", gap: 1.5, alignItems: "center", justifyContent: "center", mt: 2, flex: "0 0 auto" }}>
        <Button
          variant="gray"
          disabled={offset <= 0 || loading}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          上一页
        </Button>
        <Typography sx={{ ...typeScale.bodySm, color: "text.secondary", fontVariantNumeric: "tabular-nums" }}>
          {pageFrom} - {pageTo} / {fmtNumber(total)}
        </Typography>
        <Button
          variant="gray"
          disabled={offset + PAGE_SIZE >= total || loading}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          下一页
        </Button>
      </Box>

      <JobDetailDrawer
        open={detailId != null}
        onClose={closeDetail}
        job={detail}
        onDelete={removeOne}
      />

      <ConfirmSheet
        open={confirmAction != null}
        title={confirmAction?.title ?? ""}
        description={confirmAction?.description}
        confirmText={confirmBusy ? "执行中…" : confirmAction?.confirmText ?? "确认"}
        danger
        busy={confirmBusy}
        onClose={() => {
          if (!confirmBusy) setConfirmAction(null);
        }}
        onConfirm={() => {
          const run = confirmAction?.run;
          if (!run || confirmBusy) return;
          // 破坏性操作在弹窗里跑完再收：期间按钮禁用转文案，防连点重复删
          setConfirmBusy(true);
          void run().finally(() => {
            setConfirmBusy(false);
            setConfirmAction(null);
          });
        }}
      />

      <CollectionSheet
        site={site}
        caps={caps}
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        value={collection}
        onSaved={(next) => {
          setCollection(next);
          toast.toast("采集条件已保存", "ok");
        }}
      />
    </Box>
  );
}
