/**
 * 简历（共用路由 `#/resumes`）：简历库 / 简历解析 / 简历优化 / 优化记录 四段。
 *
 * 旧壳 `resume.js`（简历库 + 解析）与 `resume_revise.js`（优化向导 + 修改记录）收拢到这一页；
 * 按设置页的分段范式拆分：`TABS` + 每段一个自包含组件。原先挤在清单下方的解析详情
 * 独立成「简历解析」段，首屏不再被详情顶掉半屏。
 *
 * 共用页不带站点语义。接口全部走 `repos.ts`（`resumeApi` / `interviewApi.jobs` /
 * `crawlBatchApi` / `tasksApi`），视图不拼 URL；解析任务轮询 `tasksApi.get`（解析在统一
 * 任务中心里），优化任务轮询 `resumeApi.reviseTask`，事件流水一律过 `toTaskEvents()` 再喂
 * `TaskPanel`。
 *
 * 跨段状态放页面壳（切段不丢）：简历列表 / 选中项 / 解析任务进度 / 优化任务快照 /
 * 原文侧栏 / 从优化任务跳过来待开的修改稿。各段组件切走即卸载（与设置页同口径）。
 *
 * repos.ts 与后端实际形状的出入（按后端实际形状消费，本地镜像在 `resumes/types.ts`）：
 * 1. `resumeApi.get` 声明 `{resume, content_md, llm}`，后端 `GET /api/resume/item/{id}` 实际
 *    是平铺的简历字典（原文在 `raw`，解析挂 `llm`）——本页按后端实际形状消费（`ResumeDetail`）。
 * 2. `resumeApi.sections` 声明 `{sections: string[]}`，后端实际是 `{sections: [{title, level}]}`。
 * 3. `resumeApi.revisePresets` 声明 `Record<string, string[]>`，后端每个预设是 `{name, prompt}`。
 * 4. 缺岗位列表封装：定向模式勾岗位暂用 `interviewApi.jobs`（共用接口 `/api/interview/jobs`），
 *    建议补一个 `jobsApi`。
 *
 * 相对旧页的取舍（真缺就明说）：拖拽上传、原文按标题切块 + 章节滚动联动目录、
 * 岗位详情抽屉（`/api/jobs/{id}` 平台作用域，repos 未覆盖）、LLM 模型变更提醒不迁；
 * 上传后仍自动发起解析，与旧页一致。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Box } from "@mui/material";

import {
  InlineAlert, PageHeader, SegmentedControl, Sheet, useToast,
} from "../../components";
import { fontFamilyMono, typeScale } from "../../theme/tokens";
import { resumeApi, tasksApi } from "./repos";
import { toTaskEvents } from "./taskEvents";
import {
  PARSE_TERMINAL, POLL_MS, msgOf, reviseTerminal,
  type ParseRun, type ResumeDetail, type ResumeListItem, type ReviseSnapshot,
} from "./resumes/types";
import { LibrarySection } from "./resumes/LibrarySection";
import { ParseSection } from "./resumes/ParseSection";
import { ReviseSection } from "./resumes/ReviseSection";
import { RecordsSection } from "./resumes/RecordsSection";

const TABS = [
  { value: "library", label: "简历库" },
  { value: "parse", label: "简历解析" },
  { value: "revise", label: "简历优化" },
  { value: "records", label: "优化记录" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

export function ResumesPage() {
  const toast = useToast();

  const [tab, setTab] = useState<TabValue>("library");
  const [resumes, setResumes] = useState<ResumeListItem[]>([]);
  const [resumesLoading, setResumesLoading] = useState(true);
  const [currentId, setCurrentId] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingRevision, setPendingRevision] = useState<string | null>(null);

  // 解析任务进度（列表徽章 + 解析段任务卡共用）
  const [runs, setRuns] = useState<Record<string, ParseRun>>({});
  const [detail, setDetail] = useState<ResumeDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [rawItem, setRawItem] = useState<{ title: string; raw: string } | null>(null);
  // 查看原文 / 发起解析在途（按钮「…中」+ 禁点防连发）
  const [rawBusyId, setRawBusyId] = useState<string | null>(null);
  const [parseBusyId, setParseBusyId] = useState<string | null>(null);

  // 优化任务快照（切到记录再回来还在跑）
  const [reviseTask, setReviseTask] = useState<ReviseSnapshot | null>(null);
  const reviseTaskRef = useRef(reviseTask);
  reviseTaskRef.current = reviseTask;

  const loadResumes = useCallback(async () => {
    setResumesLoading(true);
    try {
      const r = await resumeApi.list();
      setResumes((r.items ?? []) as unknown as ResumeListItem[]);
      setLoadError(null);
    } catch (e) {
      setLoadError(msgOf(e));
    } finally {
      setResumesLoading(false);
    }
  }, []);

  // 稳定引用，免得 RecordsSection 的接收 effect 因身份变化反复开详情
  const clearPendingRevision = useCallback(() => setPendingRevision(null), []);
  const openRevisionFromTask = useCallback((id: string) => {
    setPendingRevision(id);
    setTab("records");
  }, []);

  useEffect(() => { void loadResumes(); }, [loadResumes]);

  // 默认选中最新一条（后端列表已按创建时间倒序）；当前简历被删就落到下一条
  useEffect(() => {
    if (resumes.length === 0) {
      setCurrentId("");
      return;
    }
    if (!currentId || !resumes.some((r) => r.resume_id === currentId)) {
      setCurrentId(resumes[0].resume_id);
    }
  }, [resumes, currentId]);

  // 轮询读最新 runs / currentId，不用把 interval 每次重挂
  const runsRef = useRef(runs);
  runsRef.current = runs;
  const currentIdRef = useRef(currentId);
  currentIdRef.current = currentId;

  const refreshDetail = useCallback(async () => {
    const id = currentIdRef.current;
    if (!id) return;
    try {
      const d = await resumeApi.get(id);
      if (currentIdRef.current === id) setDetail(d as unknown as ResumeDetail);
    } catch {
      // 刷新失败不打扰；下次切简历会重试
    }
  }, []);

  // 选中简历 → 载入原文 + 解析结果
  useEffect(() => {
    if (!currentId) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    resumeApi.get(currentId)
      .then((d) => { if (alive) setDetail(d as unknown as ResumeDetail); })
      .catch((e) => { if (alive) toast.toast(msgOf(e), "danger"); })
      .finally(() => { if (alive) setDetailLoading(false); });
    return () => { alive = false; };
    // toast 引用稳定，不进依赖表
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentId]);

  /** 解析任务单轮跟进度；终态收结果、刷新列表。 */
  const pollParse = useCallback(async (resumeId: string, run: ParseRun) => {
    try {
      const snap = await tasksApi.get(run.taskId);
      const status = String(snap.status || "");
      setRuns((prev) => ({
        ...prev,
        [resumeId]: {
          ...run,
          status,
          percent: snap.total > 0 ? Math.round((snap.done / snap.total) * 100) : undefined,
          current: String(snap.current || ""),
          error: String(snap.error || ""),
          events: toTaskEvents(snap.events, 20),
        },
      }));
      if (!PARSE_TERMINAL.has(status)) return;
      if (status === "done") {
        toast.toast("解析完成", "ok");
        void refreshDetail();
        void loadResumes();
      } else if (status === "error") {
        toast.toast(snap.error || "解析失败", "danger");
      } else {
        toast.toast("解析已取消", "warn");
      }
    } catch (e) {
      setRuns((prev) => ({ ...prev, [resumeId]: { ...run, status: "error", error: msgOf(e) } }));
      toast.toast(msgOf(e), "danger");
    }
  }, [loadResumes, refreshDetail, toast]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      for (const [resumeId, run] of Object.entries(runsRef.current)) {
        if (PARSE_TERMINAL.has(run.status)) continue;
        void pollParse(resumeId, run);
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [pollParse]);

  /**
   * 提交 LLM 解析（异步任务，多份可并行；同名重复提交由后端 409 挡）。
   * 没拿到 task_id（测试桩简写回包）就静默跳过，不挂假进度。
   */
  const runParse = useCallback(async (item: ResumeListItem) => {
    const run = runsRef.current[item.resume_id];
    if (run && !PARSE_TERMINAL.has(run.status)) {
      toast.toast("这份简历正在解析中", "warn");
      return;
    }
    if (parseBusyId === item.resume_id) return;
    setParseBusyId(item.resume_id);
    try {
      const r = await resumeApi.startParse(item.resume_id);
      if (!r.task_id) return;
      setRuns((prev) => ({
        ...prev,
        [item.resume_id]: { taskId: r.task_id, status: "running", current: "启动…", error: "", events: [] },
      }));
      toast.toast("解析任务已提交", "ok");
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setParseBusyId(null);
    }
  }, [parseBusyId, toast]);

  const viewRaw = useCallback(async (item: ResumeListItem) => {
    if (rawBusyId === item.resume_id) return;
    setRawBusyId(item.resume_id);
    try {
      const d = await resumeApi.get(item.resume_id);
      const full = d as unknown as ResumeDetail;
      setRawItem({ title: full.title || item.title, raw: full.raw || "" });
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setRawBusyId(null);
    }
  }, [rawBusyId, toast]);

  // 挂载接管在跑的优化任务（重新生成从记录页发起时也能看到进度）
  useEffect(() => {
    let alive = true;
    resumeApi.reviseStatus()
      .then((r) => {
        const s = r as unknown as ReviseSnapshot;
        if (alive && s.task_id && !reviseTerminal(s)) setReviseTask(s);
      })
      .catch(() => { /* 快照取不到就当空闲 */ });
    return () => { alive = false; };
  }, []);

  // 优化任务轮询；无在跑任务时每拍空转不发请求
  useEffect(() => {
    const timer = window.setInterval(() => {
      const t = reviseTaskRef.current;
      if (!t || !t.task_id || reviseTerminal(t)) return;
      void (async () => {
        try {
          const snap = (await resumeApi.reviseTask(t.task_id as string)) as unknown as ReviseSnapshot;
          if (!snap.task_id) return;
          setReviseTask(snap);
          if (reviseTerminal(snap)) {
            if (snap.status === "done") toast.toast("优化稿已生成，可以查看修改稿", "ok");
            else if (snap.status === "cancelled") toast.toast("优化已取消", "warn");
            else toast.toast(snap.error || "优化失败", "danger");
          }
        } catch (e) {
          // 换了任务或记录不在了：停表不打扰
          setReviseTask((prev) => (prev && prev.task_id === t.task_id ? { ...prev, status: "error", error: msgOf(e) } : prev));
        }
      })();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, []);

  const current = resumes.find((r) => r.resume_id === currentId) ?? null;
  const currentRun = currentId ? runs[currentId] : undefined;

  return (
    <Box>
      <PageHeader
        title="简历"
        subtitle="上传简历、LLM 解析与优化修改。解析结果是人岗匹配的底稿，先在这里确认没有读错。"
      />

      {loadError ? <InlineAlert tone="danger">{loadError}</InlineAlert> : null}

      <Box sx={{ my: 2.5 }}>
        <SegmentedControl
          aria-label="简历分区"
          value={tab}
          options={[
            ...TABS.map((t) =>
              t.value === "parse" && currentRun && !PARSE_TERMINAL.has(currentRun.status)
                ? { ...t, status: "busy" as const }
                : t.value === "revise" && reviseTask && !reviseTerminal(reviseTask)
                  ? { ...t, status: "busy" as const }
                  : t,
            ),
          ]}
          onChange={setTab}
        />
      </Box>

      {tab === "library" ? (
        <LibrarySection
          resumes={resumes}
          currentId={currentId}
          runs={runs}
          loading={resumesLoading}
          rawBusyId={rawBusyId}
          parseBusyId={parseBusyId}
          onSelect={setCurrentId}
          onChanged={loadResumes}
          onRunParse={runParse}
          onViewRaw={viewRaw}
        />
      ) : null}

      {tab === "parse" ? (
        <ParseSection
          current={current}
          detail={detail}
          run={currentRun}
          loading={detailLoading}
          rawBusyId={rawBusyId}
          parseBusyId={parseBusyId}
          onRunParse={runParse}
          onViewRaw={viewRaw}
        />
      ) : null}

      {tab === "revise" ? (
        <ReviseSection
          resumes={resumes}
          currentId={currentId}
          task={reviseTask}
          onTaskChange={setReviseTask}
          onViewRevision={openRevisionFromTask}
        />
      ) : null}

      {tab === "records" ? (
        <RecordsSection openRevisionId={pendingRevision} onOpened={clearPendingRevision} />
      ) : null}

      <Sheet open={rawItem != null} onClose={() => setRawItem(null)} title={`原文 · ${rawItem?.title ?? ""}`} width={640}>
        <Box
          component="pre"
          sx={{
            m: 0, whiteSpace: "pre-wrap", wordBreak: "break-word",
            fontFamily: fontFamilyMono, ...typeScale.s,
          }}
        >
          {rawItem?.raw || "（空原文）"}
        </Box>
      </Sheet>
    </Box>
  );
}
