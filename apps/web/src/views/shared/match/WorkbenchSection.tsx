/**
 * 匹配工作台：发起匹配（搜索范围 + 可选勾选）+ 任务进度 + 匹配结果。
 *
 * 布局（缺陷清单「匹配工作台布局」）：左栏搜索/配置（固定宽），右栏任务进度 + 结果（flex 1），
 * 窄屏（xs）才落回上下堆叠。
 *
 * 搜索范围（缺陷清单「搜索范围」）：只留「爬取批次下拉 + 搜索框」同一行——
 * 不选批次 = 搜全部职位；勾具体岗位 = 只匹配这几个，不勾 = 范围内全部。
 * 不再有「全部岗位 / 抓取批次」互斥分段。
 *
 * 结果/任务状态是页面级的（切到历史、招呼语再回来还在），这层只管发起与展示；
 * 保存招呼语 / 重打草稿 / 单条重跑的动作也在这层（跟结果卡走）。
 */

import { useEffect, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, EmptyState, Pill, Select, Skeleton, Surface, TaskPanel, TextField,
} from "../../../components";
import { useToast } from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtNumber, fmtTime } from "../../../utils/format";
import {
  errText, rank, PAGE_SIZE, TASK_TERMINAL, type CrawlPick, type JobPick, type MatchItem, type MatchTaskSnap,
} from "./types";
import { MatchCard } from "./MatchCard";
import { crawlBatchApi, interviewApi, matchApi } from "../repos";
import { toTaskEvents } from "../taskEvents";

/** 结果卡骨架：几何对齐 MatchCard（圆环 + 两行文案 + 招呼语框 + 按钮行），预占最终空间免得回弹。 */
function MatchCardSkeleton() {
  return (
    <Surface sx={{ mb: 1.5, display: "flex", flexDirection: "column", gap: 1.5 }} aria-hidden>
      <Box sx={{ display: "flex", gap: 1.5, alignItems: "flex-start" }}>
        <Skeleton variant="circular" width={56} height={56} sx={{ flex: "0 0 auto" }} />
        <Box sx={{ flex: 1, minWidth: 0, pt: 0.5 }}>
          <Skeleton variant="text" width="55%" height={18} />
          <Skeleton variant="text" width="38%" height={14} sx={{ mt: 0.5 }} />
          <Skeleton variant="text" width="72%" height={14} sx={{ mt: 0.5 }} />
        </Box>
      </Box>
      <Skeleton variant="rectangular" height={64} />
      <Box sx={{ display: "flex", gap: 1 }}>
        <Skeleton variant="rectangular" width={88} height={28} />
        <Skeleton variant="rectangular" width={88} height={28} />
        <Skeleton variant="rectangular" width={56} height={28} />
      </Box>
    </Surface>
  );
}

export function WorkbenchSection({
  resumes, resumeId, onResumeId,
  taskSnap, liveTask, activeTaskId, resultsLoading,
  analysisId, matches, drafts, busyJid, expandedJid,
  onAdoptTask, onSetMatches, onSetDrafts,
  onSetBusyJid, onSetExpandedJid, onClearResults,
}: {
  resumes: { resume_id: string; title: string; parsed?: boolean }[];
  resumeId: string;
  onResumeId: (id: string) => void;
  taskSnap: MatchTaskSnap | null;
  liveTask: { title?: string; status: string; done: number; total: number; current?: string; error?: string | null; events?: unknown[] } | null;
  activeTaskId: string | null;
  /** 历史载入详情在途：结果区先摆骨架屏占位，别用空态把布局压扁 */
  resultsLoading: boolean;
  analysisId: string | null;
  matches: MatchItem[];
  drafts: Record<string, string>;
  busyJid: string | null;
  expandedJid: string | null;
  onAdoptTask: (snap: MatchTaskSnap) => void;
  onSetMatches: (fn: (prev: MatchItem[]) => MatchItem[]) => void;
  onSetDrafts: (fn: (prev: Record<string, string>) => Record<string, string>) => void;
  onSetBusyJid: (id: string | null) => void;
  onSetExpandedJid: (fn: (prev: string | null) => string | null) => void;
  onClearResults: () => void;
}) {
  const toast = useToast();

  const [starting, setStarting] = useState(false);
  // 候选岗位面板（搜索范围内的勾选，随时可用）
  const [jobs, setJobs] = useState<JobPick[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobPage, setJobPage] = useState(0);
  const [jobQuery, setJobQuery] = useState("");
  const [jobApplied, setJobApplied] = useState("");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  // 抓取批次下拉：空 = 全部职位
  const [crawls, setCrawls] = useState<CrawlPick[]>([]);
  const [crawlId, setCrawlId] = useState("");

  /* ---------------- 发起匹配 ---------------- */

  // 搜索词防抖：每敲一个字都打后端会把职位表翻烂，静默 300ms 再查
  useEffect(() => {
    const t = setTimeout(() => {
      setJobApplied((prev) => {
        const next = jobQuery.trim();
        return prev === next ? prev : next;
      });
      setJobPage(0);
    }, 300);
    return () => clearTimeout(t);
  }, [jobQuery]);

  // 候选岗位：查词 / 翻页 / 批次变化都重拉；不带 crawl_task_id = 全库搜
  useEffect(() => {
    let dead = false;
    void (async () => {
      try {
        const data = await interviewApi.jobs({
          title: jobApplied || undefined,
          crawl_task_id: crawlId || undefined,
          limit: PAGE_SIZE,
          offset: jobPage * PAGE_SIZE,
        });
        if (dead) return;
        setJobs((data.items ?? []) as unknown as JobPick[]);
        setJobsTotal(data.total ?? 0);
      } catch (e) {
        if (!dead) toast.toast(errText(e), "danger");
      }
    })();
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [crawlId, jobPage, jobApplied]);

  // 抓取批次下拉：进工作台就拉（空选项 = 全部职位，所以列表随时要备着）
  useEffect(() => {
    let dead = false;
    void (async () => {
      try {
        const data = await crawlBatchApi.tasks({ limit: 100 });
        if (dead) return;
        setCrawls(
          (data.items ?? []).map((t) => ({
            task_id: t.task_id,
            title: String((t.params as { title?: string } | undefined)?.title ?? "抓取任务"),
            created_at: t.created_at,
            site: t.site,
          })),
        );
      } catch (e) {
        if (!dead) toast.toast(errText(e), "danger");
      }
    })();
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startMatch = async () => {
    if (!resumeId) {
      toast.toast("先选择一份简历", "warn");
      return;
    }
    setStarting(true);
    try {
      const body: { resume_id: string; job_ids?: string[]; crawl_task_id?: string } = { resume_id: resumeId };
      // 勾了岗位 = 只匹配这几个；没勾 = 搜索范围内全部（空批次 = 全库）
      if (selected.size) body.job_ids = [...selected];
      else if (crawlId) body.crawl_task_id = crawlId;
      // repos 把回包收窄成 `{task_id}`，实际是整份 MatchTask.snapshot()（boss_web/api/match.py start_match）
      const snap = (await matchApi.analyze(body)) as unknown as MatchTaskSnap;
      onAdoptTask(snap);
      toast.toast("匹配已启动", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      setStarting(false);
    }
  };

  const cancelTask = async () => {
    const id = activeTaskId || taskSnap?.task_id;
    if (!id) return;
    try {
      await matchApi.cancel(id);
      toast.toast("已请求停止", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    }
  };

  const toggleSelected = (jid: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(jid)) next.delete(jid);
      else next.add(jid);
      return next;
    });

  /* ---------------- 单条操作：保存招呼语 / 重打草稿 / 重跑（只改不增） ---------------- */

  const saveGreeting = async (jid: string) => {
    if (!analysisId) return;
    const text = drafts[jid] ?? matches.find((m) => m.encrypt_job_id === jid)?.greeting ?? "";
    onSetBusyJid(jid);
    try {
      const r = (await matchApi.patchGreeting(analysisId, jid, text)) as { item?: MatchItem };
      onSetMatches((prev) => rank(prev.map((m) => (m.encrypt_job_id === jid ? (r.item ?? { ...m, greeting: text }) : m))));
      onSetDrafts((prev) => {
        const next = { ...prev };
        delete next[jid];
        return next;
      });
      toast.toast("招呼语已保存", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      onSetBusyJid(null);
    }
  };

  const regenGreeting = async (jid: string) => {
    if (!analysisId) return;
    onSetBusyJid(jid);
    try {
      // 只回草稿不落库：先放草稿框里，确认后再点保存（legacy 语义）
      const r = await matchApi.regenGreeting(analysisId, jid);
      onSetDrafts((prev) => ({ ...prev, [jid]: r.greeting }));
      toast.toast("已生成新草稿，确认后记得保存", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      onSetBusyJid(null);
    }
  };

  const rematchOne = async (jid: string) => {
    if (!analysisId) return;
    onSetBusyJid(jid);
    try {
      // 只改不增：后端原地改库里那条 match，这里也只替换这一条，不往列表塞新行
      const r = (await matchApi.rematch(analysisId, jid)) as { item?: MatchItem };
      if (r.item) onSetMatches((prev) => rank(prev.map((m) => (m.encrypt_job_id === jid ? r.item! : m))));
      toast.toast("已重跑这一条", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      onSetBusyJid(null);
    }
  };

  /* ---------------- 派生展示 ---------------- */

  // 任务进度：WS 统一快照优先（推得快），轮询业务快照兜底
  const panelStatus = liveTask?.status ?? taskSnap?.status ?? "running";
  const panelDone = liveTask?.done ?? taskSnap?.done ?? 0;
  const panelTotal = liveTask?.total ?? taskSnap?.total ?? 0;
  const panelPercent = panelTotal > 0 ? Math.round((panelDone / panelTotal) * 100) : undefined;
  const panelCurrent = liveTask?.current ?? taskSnap?.current_job ?? "";
  const panelError = liveTask?.error ?? taskSnap?.error ?? null;
  const panelEvents = toTaskEvents(taskSnap?.events ?? liveTask?.events, 20);
  const panelCounts: [string, string][] | undefined =
    panelTotal > 0
      ? [["进度", `${fmtNumber(panelDone)}/${fmtNumber(panelTotal)}`]]
      : panelDone > 0
        ? [["已处理", fmtNumber(panelDone)]]
        : undefined;
  const taskRunning = !!activeTaskId && !TASK_TERMINAL.has(panelStatus);
  const pageCount = Math.max(1, Math.ceil(jobsTotal / PAGE_SIZE));
  // 结果在途（历史详情拉取 / 匹配任务还没吐行）→ 骨架屏预占空间，别用空态让布局塌一下又弹回
  const showResultSkeletons = resultsLoading || (taskRunning && matches.length === 0);

  return (
    <Box
      data-testid="match-workbench-cols"
      sx={{
        display: "flex",
        flexDirection: { xs: "column", md: "row" },
        gap: 2.5,
        alignItems: "flex-start",
      }}
    >
      {/* ---------------- 左栏：搜索 / 配置 ---------------- */}
      <Box
        role="region"
        aria-label="匹配设置"
        data-testid="match-setup-col"
        sx={{
          width: { xs: "100%", md: 360 },
          flex: { xs: "1 1 auto", md: "0 0 360px" },
          maxWidth: { md: 380 },
          minWidth: 0,
        }}
      >
        <Surface>
          <Box sx={{ display: "flex", gap: 1.5, flexWrap: "wrap", alignItems: "center" }}>
            <Select
              label="简历"
              options={resumes.map((r) => ({
                code: r.resume_id,
                name: r.parsed ? r.title : `${r.title}（未解析）`,
              }))}
              value={resumeId}
              onChange={onResumeId}
              emptyLabel={null}
              sx={{ minWidth: 0, flex: "1 1 160px" }}
            />
            <Button variant="filled" onClick={() => void startMatch()} disabled={starting}>
              {starting ? "启动中…" : selected.size ? `匹配选中 ${selected.size}` : "开始匹配"}
            </Button>
          </Box>

          {/* 搜索范围一行：批次下拉 + 搜索框（不选批次 = 全部职位） */}
          <Box
            data-testid="match-scope-row"
            sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center", mt: 1.5 }}
          >
            <Select
              label="抓取批次"
              options={crawls.map((c) => ({
                code: c.task_id,
                name: `${c.title || "抓取任务"} · ${fmtTime(c.created_at)}`,
              }))}
              value={crawlId}
              onChange={(v) => { setCrawlId(v); setJobPage(0); }}
              emptyLabel="不限"
              sx={{ flex: "1 1 140px", minWidth: 140 }}
            />
            <TextField
              label="搜职位名"
              type="search"
              placeholder="按职位名模糊匹配…"
              value={jobQuery}
              onChange={(e) => setJobQuery(e.target.value)}
              sx={{ flex: "1 1 140px", minWidth: 140 }}
            />
          </Box>

          <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 1 }}>
            不选批次 = 搜全部职位；勾具体岗位 = 只匹配这几个，不勾 = 范围内全部（量大时会比较久，进度在右边实时更新）。
          </Typography>

          <Box sx={{ mt: 2, display: "flex", flexDirection: "column", gap: 1 }}>
            <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
              <Pill tone={selected.size ? "accent" : "static"}>已选 {selected.size}</Pill>
              <Button size="small" variant="gray" onClick={() => setSelected(new Set(jobs.map((j) => j.encrypt_job_id)))}>
                全选本页
              </Button>
              <Button size="small" variant="plain" onClick={() => setSelected(new Set())}>清空</Button>
            </Box>
            {jobs.length === 0 ? (
              <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>没有可勾选的职位</Typography>
            ) : (
              <>
                <Box sx={{ maxHeight: 280, overflowY: "auto", display: "flex", flexDirection: "column", gap: 0.5 }}>
                  {jobs.map((j) => (
                    <Box
                      key={j.encrypt_job_id}
                      component="label"
                      sx={{
                        display: "flex", gap: 1, alignItems: "center", cursor: "pointer",
                        p: 1, borderRadius: "10px",
                        "&:hover": { bgcolor: "action.hover" },
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(j.encrypt_job_id)}
                        onChange={() => toggleSelected(j.encrypt_job_id)}
                      />
                      <Typography sx={{ ...typeScale.bodySm, flex: 1, minWidth: 0 }}>
                        {j.job_name} @ {j.brand_name}
                      </Typography>
                      <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
                        {[j.location, j.salary_desc].filter(Boolean).join(" · ")}
                      </Typography>
                    </Box>
                  ))}
                </Box>
                <Box sx={{ display: "flex", gap: 1, alignItems: "center" }}>
                  <Button size="small" variant="gray" disabled={jobPage <= 0} onClick={() => setJobPage((p) => p - 1)}>
                    上一页
                  </Button>
                  <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
                    第 {jobPage + 1} / {pageCount} 页 · 共 {fmtNumber(jobsTotal)} 个职位
                  </Typography>
                  <Button
                    size="small" variant="gray"
                    disabled={jobPage + 1 >= pageCount}
                    onClick={() => setJobPage((p) => p + 1)}
                  >
                    下一页
                  </Button>
                </Box>
              </>
            )}
          </Box>
        </Surface>
      </Box>

      {/* ---------------- 右栏：任务进度 + 匹配结果 ---------------- */}
      <Box
        role="region"
        aria-label="匹配结果区"
        data-testid="match-results-col"
        sx={{ flex: 1, minWidth: 0, width: { xs: "100%" } }}
      >
        {/* ---------------- 任务进度 ---------------- */}
        {(taskSnap || liveTask) && (
          <Box sx={{ mb: 2.5 }}>
            <TaskPanel
              title={liveTask?.title || "人岗匹配"}
              status={panelStatus}
              percent={panelPercent}
              current={panelCurrent || undefined}
              counts={panelCounts}
              error={panelError}
              events={panelEvents}
              stopLabel="取消"
              onStop={taskRunning ? () => void cancelTask() : undefined}
              extra={
                <Button size="small" variant="plain" onClick={onClearResults}>收起</Button>
              }
            />
          </Box>
        )}

        {/* ---------------- 匹配结果 ---------------- */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1.5 }}>
          <Typography sx={{ ...typeScale.m, fontWeight: 600, flex: 1 }}>匹配结果</Typography>
          {matches.length > 0 && (
            <>
              <Pill tone="static">{fmtNumber(matches.length)} 条</Pill>
              <Button size="small" variant="plain" onClick={onClearResults}>清空</Button>
            </>
          )}
        </Box>

        {showResultSkeletons ? (
          // 骨架屏预占最终卡位：历史详情在途 / 任务刚起步还没吐行时先撑住高度
          <Box role="status" aria-label="匹配结果加载中" data-testid="match-results-skeleton">
            <MatchCardSkeleton />
            <MatchCardSkeleton />
          </Box>
        ) : matches.length === 0 ? (
          <EmptyState
            title="启动匹配后，结果会按分数从高到低排在这里"
            description="每条给匹配度、匹配理由和一句招呼语；招呼语可以改、可以重打草稿，确认后才落库。"
          />
        ) : (
          <Box>
            {matches.map((m) => (
              <MatchCard
                key={m.encrypt_job_id}
                item={m}
                draft={drafts[m.encrypt_job_id]}
                busy={busyJid === m.encrypt_job_id}
                expanded={expandedJid === m.encrypt_job_id}
                onDraft={(text) => onSetDrafts((prev) => ({ ...prev, [m.encrypt_job_id]: text }))}
                onToggleDetail={() =>
                  onSetExpandedJid((prev) => (prev === m.encrypt_job_id ? null : m.encrypt_job_id))
                }
                onSave={() => void saveGreeting(m.encrypt_job_id)}
                onRegen={() => void regenGreeting(m.encrypt_job_id)}
                onRematch={() => void rematchOne(m.encrypt_job_id)}
              />
            ))}
          </Box>
        )}
      </Box>
    </Box>
  );
}
