/**
 * 人岗匹配（共用路由 `#/match`）：匹配工作台 / 分析历史 / 招呼语 三段。
 *
 * 迁自旧壳 `match.js` + `match_workbench.js` + `match_history.js` + `greeting_prompt.js`。
 * 旧「匹配」页混进来的「发对话 / 投递」是平台作用域功能（要登录态、发消息走各家通道），
 * 归 M3 的 `deliver` 页——这页只留一条说明，不摆假按钮。
 *
 * 按设置页的分段范式拆分：`TABS` + 每段一个自包含组件，切段只换右侧内容。
 * 页面级状态（简历 / 当前分析 / matches / 草稿 / 任务快照）挂在壳上，切段不丢；
 * 各段自己的表单与列表状态跟设置页一样，切走即卸、回来重拉。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box } from "@mui/material";

import {
  InlineAlert, PageHeader, pageRootSx, ScrollArea, SegmentedControl, useToast, ViewFade,
} from "../../components";
import { useTasks } from "../../stores/taskStore";
import {
  matchApi, resumeApi, type ResumeItem,
} from "./repos";
import { rank, TASK_TERMINAL, type AnalysisRow, type MatchItem, type MatchTaskSnap } from "./match/types";
import { WorkbenchSection } from "./match/WorkbenchSection";
import { HistorySection } from "./match/HistorySection";
import { GreetingSection } from "./match/GreetingSection";

const TABS = [
  { value: "workbench", label: "匹配工作台" },
  { value: "history", label: "分析历史" },
  { value: "greeting", label: "招呼语" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

export function MatchPage() {
  const toast = useToast();
  /** WS 推的统一任务快照：任务进度优先看它（比轮询快），断线时下面的轮询兜底。 */
  const tasks = useTasks();

  const [tab, setTab] = useState<TabValue>("workbench");

  /* —— 页面级状态：切段不丢 —— */
  const [resumes, setResumes] = useState<ResumeItem[]>([]);
  const [resumeId, setResumeId] = useState("");
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [taskSnap, setTaskSnap] = useState<MatchTaskSnap | null>(null);
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [matches, setMatches] = useState<MatchItem[]>([]);
  /** 历史详情在途：结果区摆骨架屏占位（缺陷清单「匹配结果展开预占空间」）。 */
  const [resultsLoading, setResultsLoading] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busyJid, setBusyJid] = useState<string | null>(null);
  const [expandedJid, setExpandedJid] = useState<string | null>(null);
  /** 历史摘要行：壳上留一份，任务跑完要刷；历史段自己也会进段时拉一次。 */
  const [rows, setRows] = useState<AnalysisRow[]>([]);

  /* ---------------- 首屏：简历 / 接回上次任务 ---------------- */

  useEffect(() => {
    void (async () => {
      try {
        const data = await resumeApi.list();
        setResumes(data.items ?? []);
        // 默认选第一份，省一次点击（legacy 同款）
        if (data.items?.length) setResumeId((prev) => prev || data.items[0].resume_id);
      } catch {
        /* 简历列表拿不到不挡整页；下拉空了再说 */
      }
    })();
    // 刷新/断线重开后接回最近一次匹配（legacy restoreTask）
    void (async () => {
      try {
        const snap = (await matchApi.status()) as unknown as MatchTaskSnap;
        if (snap?.task_id) adoptTask(snap);
      } catch {
        /* 恢复现场失败不打扰：历史列表里还能手动载入 */
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 把一份任务快照接进工作台（启动 / 恢复现场共用）。 */
  const adoptTask = useCallback((snap: MatchTaskSnap) => {
    if (!snap?.task_id) return;
    setTaskSnap(snap);
    setAnalysisId(snap.analysis_id || snap.task_id);
    setMatches(Array.isArray(snap.matches) ? rank(snap.matches) : []);
    setDrafts({});
    setActiveTaskId(TASK_TERMINAL.has(snap.status) ? null : snap.task_id);
    setTab("workbench");
  }, []);

  /* ---------------- 任务进度：轮询业务快照（WS 走 useTasks） ---------------- */

  const watchId = taskSnap?.task_id ?? activeTaskId ?? "";
  const liveTask = useMemo(
    () => tasks.find((t) => t.task_id === watchId) ?? null,
    [tasks, watchId],
  );

  useEffect(() => {
    if (!activeTaskId) return undefined;
    let dead = false;
    const tick = async () => {
      try {
        const snap = (await matchApi.task(activeTaskId)) as unknown as MatchTaskSnap;
        if (dead) return;
        setTaskSnap(snap);
        if (snap.analysis_id) setAnalysisId(snap.analysis_id);
        if (Array.isArray(snap.matches) && snap.matches.length) setMatches(rank(snap.matches));
        if (TASK_TERMINAL.has(snap.status)) {
          setActiveTaskId(null);
          // 已落库，顺手刷新历史摘要（top_score 会重收敛）
          void (async () => {
            try {
              const data = await matchApi.analyses();
              setRows(() => (data.items ?? []) as unknown as AnalysisRow[]);
            } catch { /* 历史刷不到不打断收尾 */ }
          })();
          if (snap.status === "done") toast.toast("匹配完成", "ok");
          else if (snap.status === "error") toast.toast(snap.error || "匹配失败", "danger");
          else toast.toast("匹配已取消", "warn");
        }
      } catch {
        /* 轮询失败不弹窗（2s 一发会刷屏），下一轮再试 */
      }
    };
    void tick();
    const timer = setInterval(() => { void tick(); }, 2000);
    return () => { dead = true; clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTaskId]);

  /* ---------------- 历史载入：先跳工作台，拉包在背后补 ---------------- */

  const loadAnalysis = useCallback(async (id: string) => {
    // 点「载入」立即切走——等拉完再跳会卡几秒（缺陷清单「分析历史载入」）
    setTab("workbench");
    setActiveTaskId(null);
    setTaskSnap(null);
    setAnalysisId(null);
    setMatches([]);
    setDrafts({});
    // 详情在途先摆骨架屏：结果区高度先占住，回来再换真卡，布局不跳
    setResultsLoading(true);
    try {
      const data = (await matchApi.analysis(id)) as unknown as { matches?: MatchItem[] };
      setAnalysisId(id);
      setMatches(Array.isArray(data.matches) ? rank(data.matches) : []);
    } finally {
      setResultsLoading(false);
    }
  }, []);

  const onDeleteAnalysis = useCallback((id: string) => {
    // 删的正是当前工作台那份——结果跟着清掉，免得对着不存在的 analysis 改招呼语
    setAnalysisId((prev) => {
      if (prev !== id) return prev;
      setMatches([]);
      setDrafts({});
      return null;
    });
  }, []);

  const clearResults = useCallback(() => {
    setAnalysisId(null);
    setMatches([]);
    setDrafts({});
    setTaskSnap(null);
    setActiveTaskId(null);
    setResultsLoading(false);
  }, []);

  // 传给工作台的 setter 保持稳定引用：MatchCard 靠 memo 挡重渲染，回调一变全列表跟着渲
  const setMatchesFn = useCallback((fn: (prev: MatchItem[]) => MatchItem[]) => setMatches(fn), []);
  const setDraftsFn = useCallback(
    (fn: (prev: Record<string, string>) => Record<string, string>) => setDrafts(fn),
    [],
  );
  const setExpandedJidFn = useCallback((fn: (prev: string | null) => string | null) => setExpandedJid(fn), []);

  return (
    <Box sx={pageRootSx}>
      <PageHeader
        title="匹配"
        subtitle="拿你的简历和搜到的岗位逐个比对：给出匹配度、好在哪、差在哪，还有一句给 HR 的开场白（可自己改）。"
      />

      {/* 发对话/投递是平台作用域（各家登录态、各家通道），归 M3 的 deliver 页——这里只留路标 */}
      <Box sx={{ flex: "0 0 auto" }}>
        <InlineAlert tone="info">
          发对话与投递已移到平台侧『发对话与投递』页——那页按平台登录态操作，这页只管匹配、历史分析和招呼语文案。
        </InlineAlert>
      </Box>

      <Box sx={{ my: 2.5, flex: "0 0 auto" }}>
        <SegmentedControl<TabValue>
          aria-label="匹配分区"
          value={tab}
          options={[...TABS]}
          onChange={setTab}
        />
      </Box>

      {/* 工作台双栏：ViewFade 自带撑满，两栏各自内部滚动（不套整块 ScrollArea） */}
      {tab === "workbench" && (
        <ViewFade slotKey={tab}>
          <WorkbenchSection
            resumes={resumes as unknown as { resume_id: string; title: string; parsed?: boolean }[]}
            resumeId={resumeId}
            onResumeId={setResumeId}
            taskSnap={taskSnap}
            liveTask={liveTask as unknown as { title?: string; status: string; done: number; total: number; current?: string; error?: string | null; events?: unknown[] } | null}
            activeTaskId={activeTaskId}
            resultsLoading={resultsLoading}
            analysisId={analysisId}
            matches={matches}
            drafts={drafts}
            busyJid={busyJid}
            expandedJid={expandedJid}
            onAdoptTask={adoptTask}
            onSetMatches={setMatchesFn}
            onSetDrafts={setDraftsFn}
            onSetBusyJid={setBusyJid}
            onSetExpandedJid={setExpandedJidFn}
            onClearResults={clearResults}
          />
        </ViewFade>
      )}

      {/* 历史段同工作台：ViewFade 直挂，HistorySection 自己 fill（标题固定 + 列表局部滚动） */}
      {tab === "history" && (
        <ViewFade slotKey={tab}>
          <HistorySection
            rows={rows}
            setRows={(fn) => setRows(fn)}
            onLoadAnalysis={loadAnalysis}
            onDelete={onDeleteAnalysis}
          />
        </ViewFade>
      )}

      {tab === "greeting" && (
        <ScrollArea>
          <ViewFade slotKey={tab}>
            <GreetingSection />
          </ViewFade>
        </ScrollArea>
      )}
    </Box>
  );
}
