/**
 * 简历解析：选中简历的原文摘要 + LLM 八段解析结果 / 解析进度。
 *
 * 从旧「简历库」下半截拆出来——清单和解析详情各占一段，首屏不再被详情顶掉半屏。
 * 解析任务轮询在页面壳（列表徽章也要用），这里只吃 `run` / `detail` 展示。
 */

import { Box, Typography } from "@mui/material";

import {
  Button, EmptyState, ScrollArea, Skeleton, Surface, TaskPanel, pageRootSx,
} from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtTime } from "../../../utils/format";
import { ParsedChapters } from "./chapters";
import { PARSE_TERMINAL, type ParseRun, type ResumeDetail, type ResumeListItem } from "./types";

export function ParseSection({
  current, detail, run, loading, rawBusyId, parseBusyId, onRunParse, onViewRaw,
}: {
  current: ResumeListItem | null;
  detail: ResumeDetail | null;
  run: ParseRun | undefined;
  /** 详情在途：骨架屏预占解析结果，别闪空态 */
  loading: boolean;
  /** 查看原文 / 发起解析在途（按钮「…中」+ 禁点防连发） */
  rawBusyId: string | null;
  parseBusyId: string | null;
  onRunParse: (item: ResumeListItem) => void;
  onViewRaw: (item: ResumeListItem) => void;
}) {
  if (!current) {
    return (
      <Box sx={pageRootSx}>
        <ScrollArea>
          <EmptyState
            title="还没有选中简历"
            description="到「简历库」上传一份简历，再回来看解析结果。"
          />
        </ScrollArea>
      </Box>
    );
  }

  const parsedData = detail?.llm?.data ?? null;
  const running = run != null && !PARSE_TERMINAL.has(run.status);

  return (
    <Box sx={pageRootSx}>
      {/* 白底圆角容器撑满剩余高度，滚动条落在它内部：标题行固定，正文自滚 */}
      <Surface sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, flexWrap: "wrap", flex: "0 0 auto" }}>
          <Typography sx={{ ...typeScale.l, fontWeight: 650, flex: 1, minWidth: 0 }}>
            {current.title || current.resume_id}
          </Typography>
          {detail?.llm?.parsed_at ? (
            <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>
              {detail.llm.model || "—"} · {fmtTime(detail.llm.parsed_at)}
            </Typography>
          ) : null}
          <Button
            size="small"
            variant="gray"
            disabled={rawBusyId === current.resume_id}
            onClick={() => onViewRaw(current)}
          >
            {rawBusyId === current.resume_id ? "查看中…" : "查看原文"}
          </Button>
        </Box>

        <ScrollArea sx={{ p: 0, pt: 2 }}>
          {running ? (
            <Box sx={{ mt: 2 }}>
              <TaskPanel
                title={`解析「${current.title || current.resume_id}」`}
                status={run.status}
                percent={run.percent}
                current={run.current || undefined}
                error={run.error || null}
                events={run.events}
              />
            </Box>
          ) : loading ? (
            <Box sx={{ mt: 2, display: "flex", flexDirection: "column", gap: 1 }}>
              {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} height={28} />)}
            </Box>
          ) : parsedData ? (
            <ParsedChapters data={parsedData} />
          ) : (
            <EmptyState
              title="这份简历还没解析"
              description="解析会识别出八段：基本信息 / 求职意向 / 工作经历 / 项目经历 / 教育经历 / 技能标签 / 自我评价 / 摘要。"
              action={
                <Button
                  variant="filled"
                  disabled={parseBusyId === current.resume_id}
                  onClick={() => onRunParse(current)}
                >
                  {parseBusyId === current.resume_id ? "解析中…" : detail?.llm ? "重新解析" : "开始解析"}
                </Button>
              }
            />
          )}
        </ScrollArea>
      </Surface>
    </Box>
  );
}
