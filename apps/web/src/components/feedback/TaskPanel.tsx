import type { ReactNode } from "react";
import { Box, Typography } from "@mui/material";
import { typeScale } from "../../theme/tokens";
import { fmtEta } from "../../utils/format";
import { TASK_STATE_META, taskStateOf, type TaskState } from "../../utils/taskState";
import { Button } from "../controls/Button";
import { StatusDot } from "../data/StatusDot";
import { Surface } from "../surfaces/Surface";
import { ProgressBar } from "./ProgressBar";
import { EventStream, type TaskEvent } from "./EventStream";

export interface TaskPanelProps {
  title: string;
  /** 后端原始 status（走 taskStateOf 归一）或已归一状态 */
  status: string | TaskState;
  /** 0–100；undefined = 不确定态 */
  percent?: number;
  /** 当前处理对象（如职位名） */
  current?: string;
  /** 计数行：[标签, 值] */
  counts?: [string, string][];
  /** 剩余秒数或已格式化文案 */
  eta?: number | string;
  error?: string | null;
  events?: TaskEvent[];
  eventsTitle?: string;
  stopLabel?: string;
  onStop?: () => void;
  /** 站点角标（多平台：任务属于哪个 site） */
  siteBadge?: ReactNode;
  extra?: ReactNode;
}

/** 统一长任务面板（>2s 操作一律走它——DESIGN.md Long-Task Panel）：状态机 → 外观映射表。 */
export function TaskPanel({
  title, status, percent, current, counts, eta, error,
  events, eventsTitle, stopLabel = "停止", onStop, siteBadge, extra,
}: TaskPanelProps) {
  const state: TaskState = taskStateOf(status);
  const meta = TASK_STATE_META[state];
  const running = state === "running";

  return (
    <Surface padded sx={{ display: "flex", flexDirection: "column", gap: 2 }} role="status" aria-live="polite">
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
        <StatusDot tone={meta.tone} />
        <Typography sx={{ ...typeScale.m, fontWeight: 600, flex: 1, minWidth: 0 }}>{title}</Typography>
        {siteBadge}
        <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>{meta.label}</Typography>
        {running && onStop !== undefined && (
          <Button variant="danger" size="small" onClick={onStop}>{stopLabel}</Button>
        )}
      </Box>

      <ProgressBar value={percent} active={running} tone={state === "error" ? "danger" : state === "done" ? "ok" : "accent"} />

      {(current !== undefined || counts?.length || eta !== undefined || error) && (
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, alignItems: "baseline" }}>
          {current !== undefined && (
            <Box sx={{ display: "flex", gap: 1, alignItems: "baseline", minWidth: 0, flex: "1 1 180px" }}>
              <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>当前</Typography>
              <Typography sx={{ ...typeScale.bodySm, color: "primary.main", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {current}
              </Typography>
            </Box>
          )}
          {counts?.map(([k, v]) => (
            <Box key={k} sx={{ display: "flex", gap: 1, alignItems: "baseline" }}>
              <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{k}</Typography>
              <Typography sx={{ ...typeScale.bodySm, fontWeight: 590, fontVariantNumeric: "tabular-nums" }}>{v}</Typography>
            </Box>
          ))}
          {eta !== undefined && (
            <Box sx={{ display: "flex", gap: 1, alignItems: "baseline" }}>
              <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>剩余</Typography>
              <Typography sx={{ ...typeScale.bodySm }}>{typeof eta === "number" ? fmtEta(eta) : eta}</Typography>
            </Box>
          )}
          {error != null && error !== "" && (
            <Typography sx={{ ...typeScale.bodySm, color: "error.main" }}>错误：{error}</Typography>
          )}
        </Box>
      )}

      {extra}
      {events !== undefined && <EventStream events={events} title={eventsTitle} />}
    </Surface>
  );
}
