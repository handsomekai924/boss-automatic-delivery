/**
 * 出题：按抓取批次 / 职位名挑一批岗位 → 批量生成。
 *
 * 题量不给手填（2026-10-29）：按匹配度自动 10–15，请求不带 `question_count`；
 * 出题按钮并进筛选条容器，不再单独占一块。在跑任务占用的岗位标「任务中」并跳过；
 * 行内也能单个生成 / 查看题目（开 PrepSheet）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, CellTitle, DataTable, Pill, Select, Surface, TextField, useToast, type Column,
} from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtTime } from "../../../utils/format";
import type { TaskSnapshot } from "../../../stores/taskStore";
import { crawlBatchApi, interviewApi } from "../repos";
import { errMsg, PAGE_SIZE, type JobRow, type PrepTarget } from "./types";
import { Pager, RowCheck } from "./bits";
import { PrepSheet } from "./PrepSheet";

export function GenerateSection({
  busy, interviewTasks, onTaskStarted,
}: {
  /** 在跑任务占用的岗位 id（按钮转「任务中」） */
  busy: Set<string>;
  /** 任务收尾后要对账「已生成」徽章 */
  interviewTasks: TaskSnapshot[];
  onTaskStarted: () => void;
}) {
  const toast = useToast();

  const [crawlTaskId, setCrawlTaskId] = useState("");
  const [titleInput, setTitleInput] = useState("");
  const [title, setTitle] = useState("");
  const [jobsPage, setJobsPage] = useState(0);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [prep, setPrep] = useState<PrepTarget | null>(null);
  /** 出题请求在飞（"batch" = 批量，否则是单岗 id）：按钮转文案并禁用，防连点重复出题。 */
  const [genKey, setGenKey] = useState<string | null>(null);

  // 抓取批次下拉：共用口 `/api/crawl/tasks`（跨站点全量，批次只是职位来源）
  const [batchOptions, setBatchOptions] = useState<{ code: string; name: string }[]>([]);
  useEffect(() => {
    let dead = false;
    void (async () => {
      try {
        const data = await crawlBatchApi.tasks({ limit: 100 });
        if (dead) return;
        setBatchOptions(
          (data.items ?? []).map((t) => ({
            code: t.task_id,
            name: `批次 ${fmtTime(t.created_at)} #${t.task_id.slice(-6)}`,
          })),
        );
      } catch {
        // 批次下拉拿不到就只剩「全部抓取记录」，不挡出题
      }
    })();
    return () => { dead = true; };
  }, []);

  const loadJobs = useCallback(async () => {
    setJobsLoading(true);
    try {
      const data = await interviewApi.jobs({
        crawl_task_id: crawlTaskId,
        title,
        limit: PAGE_SIZE,
        offset: jobsPage * PAGE_SIZE,
      });
      setJobs((data.items ?? []) as unknown as JobRow[]);
      setJobsTotal(Number(data.total ?? 0));
    } catch (e) {
      toast.toast(`读岗位列表失败：${errMsg(e)}`, "danger");
    } finally {
      setJobsLoading(false);
    }
  }, [crawlTaskId, title, jobsPage, toast]);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  // 生成任务收尾后，岗位列表的「已生成」徽章要对账一次（WS 推终态时触发）
  const seenStatus = useRef<Record<string, string>>({});
  useEffect(() => {
    const next: Record<string, string> = {};
    let justFinished = false;
    for (const t of interviewTasks) {
      next[t.task_id] = t.status;
      if (seenStatus.current[t.task_id] === "running" && t.status !== "running") justFinished = true;
    }
    seenStatus.current = next;
    if (justFinished) void loadJobs();
  }, [interviewTasks, loadJobs]);

  const toggleSelect = useCallback((id: string, on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  /** 触发生成（单个或批量，可与其它任务并行）。题量按匹配度自动（10–15），不带 question_count。 */
  const startGenerate = async (ids: string[], key: string) => {
    if (genKey) return;
    setGenKey(key);
    try {
      await interviewApi.generate({ encrypt_job_ids: ids });
      toast.toast(`已开始生成 ${ids.length} 个岗位的面试题`, "ok");
      onTaskStarted();
      await loadJobs();
    } catch (e) {
      toast.toast(errMsg(e), "danger");
    } finally {
      setGenKey(null);
    }
  };

  const onBatchGenerate = () => {
    const ids = [...selected].filter((id) => !busy.has(id));
    const skipped = selected.size - ids.length;
    if (!ids.length) {
      toast.toast(
        skipped ? "选中的岗位都已在任务里，请先在任务中心处理" : "请先勾选要生成的岗位",
        "warn",
      );
      return;
    }
    if (skipped) toast.toast(`跳过 ${skipped} 个已在任务里的岗位`, "warn");
    void startGenerate(ids, "batch");
  };

  const jobColumns: Column<JobRow>[] = useMemo(
    () => [
      {
        key: "sel",
        width: 44,
        header: (
          <RowCheck
            label="全选本页"
            checked={jobs.length > 0 && jobs.every((j) => selected.has(j.encrypt_job_id))}
            onChange={(on) =>
              setSelected((prev) => {
                const next = new Set(prev);
                for (const j of jobs) {
                  if (on) next.add(j.encrypt_job_id);
                  else next.delete(j.encrypt_job_id);
                }
                return next;
              })
            }
          />
        ),
        render: (j) => (
          <RowCheck
            label={`选择 ${j.job_name}`}
            checked={selected.has(j.encrypt_job_id)}
            onChange={(on) => toggleSelect(j.encrypt_job_id, on)}
          />
        ),
      },
      {
        key: "job_name",
        header: "职位名称",
        render: (j) => (
          <Box>
            <CellTitle>{j.job_name || "—"}</CellTitle>
            <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>
              {[j.job_experience, j.job_degree].filter(Boolean).join(" · ")}
            </Typography>
          </Box>
        ),
      },
      { key: "brand_name", header: "公司", render: (j) => j.brand_name || "—" },
      {
        key: "salary_desc",
        header: "薪资 / 城市",
        render: (j) => (
          <Box>
            <Typography sx={{ ...typeScale.bodySm }}>{j.salary_desc || "—"}</Typography>
            <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{j.location}</Typography>
          </Box>
        ),
      },
      {
        key: "jd_status",
        header: "JD 状态",
        render: (j) => <Pill tone="static">{j.jd_status || "—"}</Pill>,
      },
      {
        key: "question_count",
        header: "面试题",
        render: (j) =>
          j.question_count > 0 ? <Pill tone="ok">已生成</Pill> : <Pill tone="static">未生成</Pill>,
      },
    ],
    [jobs, selected, toggleSelect],
  );

  const jobsPageCount = Math.max(1, Math.ceil(jobsTotal / PAGE_SIZE));

  return (
    <Box>
      <Surface sx={{ mb: 2, display: "flex", gap: 1.5, flexWrap: "wrap", alignItems: "center" }}>
        <Select
          label="抓取批次"
          options={batchOptions}
          value={crawlTaskId}
          onChange={(v) => {
            setCrawlTaskId(v);
            setJobsPage(0);
          }}
          emptyLabel="全部抓取记录"
          sx={{ minWidth: 220 }}
        />
        <TextField
          label="职位名称"
          type="search"
          placeholder="模糊匹配…"
          value={titleInput}
          onChange={(e) => setTitleInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setTitle(titleInput.trim());
              setJobsPage(0);
            }
          }}
          sx={{ flex: "1 1 200px" }}
        />
        <Button
          variant="gray"
          onClick={() => {
            setTitle(titleInput.trim());
            setJobsPage(0);
          }}
        >
          查询
        </Button>
        {/* 出题动作并进筛选条（2026-10-29），不再单独占一个容器 */}
        <Button
          variant="filled"
          sx={{ ml: "auto" }}
          disabled={genKey !== null}
          onClick={onBatchGenerate}
        >
          {genKey === "batch" ? "出题中…" : `出题（已选 ${selected.size}）`}
        </Button>
        <Typography sx={{ ...typeScale.s, color: "text.secondary", flexBasis: "100%" }}>
          勾选岗位后批量出题；题量按匹配度自动（10–15 题），不手动指定；已在任务里的岗位会自动跳过。
        </Typography>
      </Surface>

      <DataTable
        columns={jobColumns}
        rows={jobs}
        rowKey={(j) => j.encrypt_job_id}
        loading={jobsLoading}
        emptyText="没有符合条件的岗位"
        rowActions={(j) => (
          <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
            {/* 没生成过题就没有可看的——禁用，别开了个空侧栏（缺陷清单「查看按钮」） */}
            <Button
              size="small"
              variant="plain"
              disabled={j.question_count === 0}
              onClick={() =>
                setPrep({
                  encryptJobId: j.encrypt_job_id,
                  jobName: j.job_name,
                  brandName: j.brand_name,
                })
              }
            >
              查看
            </Button>
            <Button
              size="small"
              variant="gray"
              disabled={busy.has(j.encrypt_job_id) || genKey !== null}
              onClick={() => void startGenerate([j.encrypt_job_id], j.encrypt_job_id)}
            >
              {busy.has(j.encrypt_job_id) ? "任务中" : genKey === j.encrypt_job_id ? "生成中…" : "生成"}
            </Button>
          </Box>
        )}
      />
      <Pager
        total={jobsTotal}
        page={jobsPage}
        pageCount={jobsPageCount}
        onPrev={() => setJobsPage((p) => Math.max(0, p - 1))}
        onNext={() => setJobsPage((p) => p + 1)}
      />

      <PrepSheet
        target={prep}
        onClose={() => setPrep(null)}
        onChanged={() => void loadJobs()}
        onTaskStarted={onTaskStarted}
      />
    </Box>
  );
}
