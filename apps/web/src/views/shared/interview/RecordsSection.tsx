/**
 * 生成记录：一岗一套题的归档列表，可开题目侧栏（浏览 / 补题 / 导出）或重新生成。
 *
 * `title` 后端支持职位名模糊（`GET /api/interview/records?title=`），这里直接透传。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, CellTitle, DataTable, ScrollArea, Surface, TextField, pageRootSx, useToast, type Column,
} from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtNumber, fmtTime } from "../../../utils/format";
import { interviewApi } from "../repos";
import { errMsg, PAGE_SIZE, type PrepTarget, type RecordRow } from "./types";
import { Pager } from "./bits";
import { PrepSheet } from "./PrepSheet";

export function RecordsSection({
  busy, onTaskStarted,
}: {
  busy: Set<string>;
  onTaskStarted: () => void;
}) {
  const toast = useToast();

  const [recTitle, setRecTitle] = useState("");
  const [title, setTitle] = useState("");
  const [recsPage, setRecsPage] = useState(0);
  const [records, setRecords] = useState<RecordRow[]>([]);
  const [recsTotal, setRecsTotal] = useState(0);
  const [recsLoading, setRecsLoading] = useState(false);
  const [prep, setPrep] = useState<PrepTarget | null>(null);
  /** 「重新生成」请求在飞的记录 id：按钮转文案并禁用，防连点重复出题。 */
  const [regenId, setRegenId] = useState<string | null>(null);

  const loadRecords = useCallback(async () => {
    setRecsLoading(true);
    try {
      const data = await interviewApi.records({
        title: title || undefined,
        limit: PAGE_SIZE,
        offset: recsPage * PAGE_SIZE,
      });
      setRecords((data.items ?? []) as unknown as RecordRow[]);
      setRecsTotal(Number(data.total ?? 0));
    } catch (e) {
      toast.toast(`读生成记录失败：${errMsg(e)}`, "danger");
    } finally {
      setRecsLoading(false);
    }
  }, [title, recsPage, toast]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords]);

  const startGenerate = async (ids: string[]) => {
    if (regenId) return;
    setRegenId(ids[0] ?? "");
    try {
      await interviewApi.generate({ encrypt_job_ids: ids });
      toast.toast(`已开始生成 ${ids.length} 个岗位的面试题`, "ok");
      onTaskStarted();
      await loadRecords();
    } catch (e) {
      toast.toast(errMsg(e), "danger");
    } finally {
      setRegenId(null);
    }
  };

  const recordColumns: Column<RecordRow>[] = useMemo(
    () => [
      {
        key: "job_name",
        header: "职位名称",
        render: (r) => <CellTitle>{r.job_name || r.encrypt_job_id}</CellTitle>,
      },
      { key: "brand_name", header: "公司", render: (r) => r.brand_name || "—" },
      { key: "question_count", header: "题目数", render: (r) => fmtNumber(r.question_count) },
      { key: "created_at", header: "生成时间", render: (r) => fmtTime(r.created_at) },
      { key: "updated_at", header: "更新时间", render: (r) => fmtTime(r.updated_at) },
    ],
    [],
  );

  const recsPageCount = Math.max(1, Math.ceil(recsTotal / PAGE_SIZE));

  return (
    <Box sx={pageRootSx}>
      <Surface sx={{ mb: 2, flex: "0 0 auto", display: "flex", gap: 1.5, flexWrap: "wrap", alignItems: "center" }}>
        <TextField
          label="记录搜索"
          type="search"
          placeholder="按职位名模糊匹配…"
          value={recTitle}
          onChange={(e) => setRecTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setTitle(recTitle.trim());
              setRecsPage(0);
            }
          }}
          sx={{ flex: "1 1 220px" }}
        />
        <Button
          variant="gray"
          onClick={() => {
            setTitle(recTitle.trim());
            setRecsPage(0);
          }}
        >
          查询
        </Button>
        <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
          一岗一套题；点「详情」进题目侧栏浏览、补题、导出。
        </Typography>
      </Surface>

      <ScrollArea>
        <DataTable
          columns={recordColumns}
          rows={records}
          rowKey={(r) => r.encrypt_job_id}
          loading={recsLoading}
          emptyText="还没有生成记录"
          rowActions={(r) => (
            <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
              {/* 没题可看就禁用（与出题页「查看」同规则） */}
              <Button
                size="small"
                variant="plain"
                disabled={r.question_count === 0}
                onClick={() =>
                  setPrep({
                    encryptJobId: r.encrypt_job_id,
                    jobName: r.job_name,
                    brandName: r.brand_name,
                    analysisId: r.analysis_id || undefined,
                  })
                }
              >
                详情
              </Button>
              <Button
                size="small"
                variant="gray"
                sx={{ whiteSpace: "nowrap" }}
                disabled={busy.has(r.encrypt_job_id) || regenId !== null}
                onClick={() => void startGenerate([r.encrypt_job_id])}
              >
                {busy.has(r.encrypt_job_id)
                  ? "任务中"
                  : regenId === r.encrypt_job_id ? "生成中…" : "重新生成"}
              </Button>
            </Box>
          )}
        />
      </ScrollArea>
      <Pager
        total={recsTotal}
        page={recsPage}
        pageCount={recsPageCount}
        onPrev={() => setRecsPage((p) => Math.max(0, p - 1))}
        onNext={() => setRecsPage((p) => p + 1)}
      />

      <PrepSheet
        target={prep}
        onClose={() => setPrep(null)}
        onChanged={() => void loadRecords()}
        onTaskStarted={onTaskStarted}
      />
    </Box>
  );
}
