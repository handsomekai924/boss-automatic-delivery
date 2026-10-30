/**
 * 题目侧栏：上下文/摘要 + 导出 + 补题 + 题型筛选浏览（旧 `interview_prep.js` 弹窗）。
 *
 * 补题语义（UI 文案必须体现）：只补所选题型、追加到已有题目后面、避开已有题目，不覆盖旧题。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Typography } from "@mui/material";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

import {
  Button, EmptyState, Pill, SegmentedControl, Select, Sheet, Skeleton, Surface, TextField, useToast,
} from "../../../components";
import { radius, springs, typeScale } from "../../../theme/tokens";
import { interviewApi } from "../repos";
import {
  errMsg, QUESTION_TYPES, SUPPLEMENT_MAX,
  type CtxInfo, type PrepTarget, type QuestionItem, type InterviewTaskSnap,
} from "./types";

export function PrepSheet({
  target, onClose, onChanged, onTaskStarted,
}: {
  target: PrepTarget | null;
  onClose: () => void;
  /** 题目有增删改后通知外层刷新徽章 */
  onChanged: () => void;
  /** 生成任务起动后让外层对账任务列表 */
  onTaskStarted: () => void;
}) {
  const toast = useToast();
  const [questions, setQuestions] = useState<QuestionItem[]>([]);
  const [questionTotal, setQuestionTotal] = useState(0);
  const [ctx, setCtx] = useState<CtxInfo | null>(null);
  const [summaryCount, setSummaryCount] = useState<number | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [supOpen, setSupOpen] = useState(false);
  const [supType, setSupType] = useState<string>(QUESTION_TYPES[0]);
  const [supCount, setSupCount] = useState("3");
  const [genBusy, setGenBusy] = useState(false);
  /** 一次只展示一题（缺陷清单提议）：当前题 index，配合上一题 / 下一题翻页 */
  const [qIndex, setQIndex] = useState(0);
  /** 在盯的生成任务 id（null = 没在盯） */
  const [watchId, setWatchId] = useState<string | null>(null);
  /** 加载完成标记（= 哪个岗位的题）：换岗自动作废回骨架，别拿上一岗的题冒充这一岗。 */
  const [loadedFor, setLoadedFor] = useState("");
  const reduced = useReducedMotion();

  const jobId = target?.encryptJobId ?? "";
  /** 题目/摘要是拉回来的内容：拉取窗口铺骨架，别先甩「还没有面试题」空态。 */
  const loading = jobId !== "" && loadedFor !== jobId;

  /** 题目 + 上下文 + 摘要一起拉（摘要题数是出题状态徽章的权威来源之一）。 */
  const load = useCallback(async () => {
    if (!jobId) return;
    try {
      const [l, c, s] = await Promise.all([
        interviewApi.list({ encrypt_job_id: jobId }),
        interviewApi.context({ encrypt_job_id: jobId }),
        interviewApi.summary({ encrypt_job_ids: jobId }),
      ]);
      const list = l as unknown as { questions?: QuestionItem[]; stats?: { question_count?: number } };
      setQuestions(list.questions ?? []);
      setQuestionTotal(Number(list.stats?.question_count ?? 0));
      setCtx(c as unknown as CtxInfo);
      const items =
        (s as unknown as { items?: Record<string, { question_count?: number }> }).items ?? {};
      setSummaryCount(items[jobId]?.question_count ?? null);
    } catch (e) {
      toast.toast(`读面试题失败：${errMsg(e)}`, "danger");
    } finally {
      // 失败也收骨架（空态 + toast 说清），别一直转
      setLoadedFor(jobId);
    }
  }, [jobId, toast]);

  useEffect(() => {
    if (!jobId) return;
    setTypeFilter("");
    setSupOpen(false);
    setWatchId(null);
    setQIndex(0);
    void load();
  }, [jobId, load]);

  // 换题型筛选回到第一题
  useEffect(() => {
    setQIndex(0);
  }, [typeFilter]);

  // 生成是异步任务：轮询盯到终态再刷题（WS 也会推进度，这里保证断线照样收尾）
  useEffect(() => {
    if (!watchId) return undefined;
    const timer = setInterval(() => {
      void (async () => {
        try {
          const snap = (await interviewApi.task(watchId)) as unknown as InterviewTaskSnap;
          const st = String(snap?.status ?? "");
          if (!st || st === "idle" || st === "running") return;
          setWatchId(null);
          if (st === "done") toast.toast(`已生成 ${Number(snap.question_total ?? 0)} 道题`, "ok");
          else if (st === "error") toast.toast(String(snap.error || "生成失败"), "danger");
          await load();
          onChanged();
        } catch {
          /* 这一轮拉不到就等下一轮 */
        }
      })();
    }, 2000);
    return () => clearInterval(timer);
  }, [watchId, load, onChanged, toast]);

  const analysisId = target?.analysisId || ctx?.analysis_id || "";

  /** 出整套 / 补题都走 generate：带 question_type 就是补题（追加、避开已有题）。 */
  const startGenerate = async (extra?: { questionType?: string; questionCount?: number }) => {
    if (!jobId) return;
    if (genBusy || watchId) {
      toast.toast("生成任务已在跑", "warn");
      return;
    }
    setGenBusy(true);
    try {
      const resp = await interviewApi.generate({
        analysis_id: analysisId,
        encrypt_job_ids: [jobId],
        question_type: extra?.questionType,
        question_count: extra?.questionCount,
      });
      toast.toast(
        extra?.questionType
          ? `已开始补充「${extra.questionType}」${extra.questionCount ?? 0} 条`
          : "已开始生成面试题",
        "ok",
      );
      if (resp.task_id) setWatchId(resp.task_id);
      onTaskStarted();
    } catch (e) {
      toast.toast(errMsg(e), "danger");
    } finally {
      setGenBusy(false);
    }
  };

  const submitSupplement = () => {
    const n = Number(supCount);
    if (!supType) {
      toast.toast("请选择题型", "warn");
      return;
    }
    if (!Number.isInteger(n) || n < 1) {
      toast.toast("条数至少 1", "warn");
      return;
    }
    if (n > SUPPLEMENT_MAX) {
      toast.toast(`单次最多 ${SUPPLEMENT_MAX} 条`, "warn");
      return;
    }
    void startGenerate({ questionType: supType, questionCount: n });
  };

  const visible = useMemo(
    () => (typeFilter ? questions.filter((q) => q.question_type === typeFilter) : questions),
    [questions, typeFilter],
  );

  // 列表缩短（换筛选 / 重生成）时把 index 夹回范围内
  const safeIndex = visible.length ? Math.min(qIndex, visible.length - 1) : 0;
  const q = visible[safeIndex];

  return (
    <Sheet
      open={target !== null}
      onClose={onClose}
      width={640}
      title={target ? `面试题 · ${[target.jobName, target.brandName].filter(Boolean).join(" · ")}` : ""}
    >
      {target && loading && (
        /* 题目/摘要拉取窗口：摘要块 + 题卡两块骨架占位，数据到了整块替换不跳版 */
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          <Skeleton height={72} sx={{ borderRadius: radius.s }} />
          <Skeleton height={40} sx={{ borderRadius: radius.s }} />
          <Skeleton height={180} sx={{ borderRadius: radius.s }} />
        </Box>
      )}
      {target && !loading && (
        <>
          {/* 上下文与摘要：题数 / 匹配度 / 归属分析（旧弹窗的摘要区） */}
          <Surface sx={{ mb: 2 }}>
            <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1 }}>
              上下文与摘要
            </Typography>
            <Box sx={{ display: "flex", gap: 2.5, flexWrap: "wrap" }}>
              {(
                [
                  ["已有题数", String(questionTotal || summaryCount || 0)],
                  ["摘要题数", summaryCount != null ? String(summaryCount) : "—"],
                  ["匹配度", ctx?.match_score ? String(ctx.match_score) : "—"],
                  ["关联分析", ctx?.analysis_id ? "有" : "无（按 JD 出题）"],
                ] as [string, string][]
              ).map(([k, v]) => (
                <Box key={k} sx={{ display: "flex", gap: 1, alignItems: "baseline" }}>
                  <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{k}</Typography>
                  <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>{v}</Typography>
                </Box>
              ))}
            </Box>
          </Surface>

          <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", mb: 2, alignItems: "center" }}>
            {/* 导出必须是真链接（新窗口拿附件），不发 fetch 拿二进制；
                用 Button component="a"——尺寸和其它按钮一致（缺陷清单「导出按钮大小」）。 */}
            <Button
              variant="gray"
              component="a"
              href={interviewApi.exportUrl({ encrypt_job_id: target.encryptJobId })}
              target="_blank"
              rel="noreferrer"
              sx={{ textDecoration: "none" }}
            >
              导出 Markdown
            </Button>
            <Button variant={supOpen ? "tinted" : "gray"} onClick={() => setSupOpen((v) => !v)}>
              {supOpen ? "收起补题" : "补题"}
            </Button>
            <Button
              variant="filled"
              disabled={genBusy || watchId !== null}
              onClick={() => void startGenerate()}
            >
              {genBusy ? "提交中…" : watchId ? "生成中…" : questionTotal ? "重新生成" : "生成面试题"}
            </Button>
          </Box>

          {supOpen && (
            <Surface sx={{ mb: 2, display: "flex", flexDirection: "column", gap: 1.5 }}>
              <Select
                label="题型"
                options={QUESTION_TYPES.map((t) => ({ code: t, name: t }))}
                value={supType}
                onChange={setSupType}
                emptyLabel={null}
                sx={{ maxWidth: 240 }}
              />
              <TextField
                label="补几条"
                type="number"
                value={supCount}
                onChange={(e) => setSupCount(e.target.value)}
                sx={{ maxWidth: 160 }}
              />
              <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
                只补所选题型，追加到已有题目后面、自动避开已有题目；不覆盖旧题，单次最多{" "}
                {SUPPLEMENT_MAX} 条。
              </Typography>
              <Box sx={{ display: "flex", gap: 1 }}>
                <Button
                  variant="filled"
                  disabled={genBusy || watchId !== null}
                  onClick={submitSupplement}
                >
                  {genBusy ? "补题中…" : "开始补题"}
                </Button>
                <Button variant="plain" onClick={() => setSupOpen(false)}>收起</Button>
              </Box>
            </Surface>
          )}

          <Box sx={{ mb: 1.5 }}>
            <SegmentedControl<string>
              aria-label="题型筛选"
              size="sm"
              value={typeFilter}
              onChange={setTypeFilter}
              options={[
                { value: "", label: "全部" },
                ...QUESTION_TYPES.map((t) => ({ value: t as string, label: t })),
              ]}
            />
          </Box>

          {visible.length === 0 ? (
            <EmptyState
              title={questions.length ? "没有符合筛选条件的题目" : "这个岗位还没有面试题"}
              description={
                questions.length
                  ? "换个题型再看看。"
                  : "点「生成面试题」出一套，或用「补题」按题型追加。"
              }
            />
          ) : (
            <>
              <Box sx={{ display: "flex", gap: 1, alignItems: "center", mb: 1.5 }}>
                <Typography
                  sx={{ ...typeScale.s, color: "text.secondary", flex: 1, fontVariantNumeric: "tabular-nums" }}
                >
                  第 {safeIndex + 1} / {visible.length} 题
                </Typography>
                <Button
                  size="small"
                  variant="gray"
                  disabled={safeIndex <= 0}
                  onClick={() => setQIndex(safeIndex - 1)}
                >
                  上一题
                </Button>
                <Button
                  size="small"
                  variant="gray"
                  disabled={safeIndex >= visible.length - 1}
                  onClick={() => setQIndex(safeIndex + 1)}
                >
                  下一题
                </Button>
              </Box>

              {/* 换题走短交叉淡入（reduced-motion 友好）；题卡只渲染当前这道 */}
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={q.id}
                  initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
                  animate={reduced ? { opacity: 1 } : { opacity: 1, y: 0 }}
                  exit={reduced ? { opacity: 0 } : { opacity: 0, y: -8 }}
                  transition={springs.ui}
                >
                  <Surface>
                    <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center", mb: 1 }}>
                      <Pill tone="static">{q.question_type}</Pill>
                      <Pill tone={q.difficulty === "高级" ? "warn" : "static"}>{q.difficulty}</Pill>
                      {q.is_frequent && <Pill tone="accent">高频</Pill>}
                    </Box>
                    <Typography sx={{ ...typeScale.m, fontWeight: 590, whiteSpace: "pre-wrap", mb: 1 }}>
                      {q.question_text}
                    </Typography>
                    {q.answer_hints?.approach && (
                      <Box sx={{ mb: 1 }}>
                        <Typography sx={{ ...typeScale.xs, color: "text.secondary", mb: 0.5 }}>
                          答题思路
                        </Typography>
                        <Typography sx={{ ...typeScale.bodySm, whiteSpace: "pre-wrap" }}>
                          {q.answer_hints.approach}
                        </Typography>
                      </Box>
                    )}
                    {(q.answer_hints?.key_points ?? []).length > 0 && (
                      <Box sx={{ mb: 1 }}>
                        <Typography sx={{ ...typeScale.xs, color: "text.secondary", mb: 0.5 }}>
                          关键知识点
                        </Typography>
                        {(q.answer_hints?.key_points ?? []).map((p, i) => (
                          <Typography key={i} sx={{ ...typeScale.bodySm }}>· {p}</Typography>
                        ))}
                      </Box>
                    )}
                    {q.answer_hints?.reference_answer && (
                      <Box>
                        <Typography sx={{ ...typeScale.xs, color: "text.secondary", mb: 0.5 }}>
                          参考答案
                        </Typography>
                        <Typography sx={{ ...typeScale.bodySm, whiteSpace: "pre-wrap" }}>
                          {q.answer_hints.reference_answer}
                        </Typography>
                      </Box>
                    )}
                  </Surface>
                </motion.div>
              </AnimatePresence>
            </>
          )}
        </>
      )}
    </Sheet>
  );
}
