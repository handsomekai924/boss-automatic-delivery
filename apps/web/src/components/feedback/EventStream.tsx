import { Box, Typography } from "@mui/material";
import { AnimatePresence, motion } from "motion/react";
import { springs, typeScale } from "../../theme/tokens";
import { fmtClock } from "../../utils/format";
import { Pill, type PillTone } from "../data/Pill";

export type EventKind = "info" | "ok" | "warn" | "error" | "muted";

export interface TaskEvent {
  /** 事件名/消息 */
  name: string;
  /** 秒级时间戳或 ISO */
  at?: number | string | Date | null;
  kind?: EventKind;
}

export interface EventStreamProps {
  events: TaskEvent[];
  /** 最多显示条数（默认 30，旧件 renderEvents 行为） */
  limit?: number;
  title?: string;
  /** 事件条高度上限（滚动区） */
  maxHeight?: number;
}

const kindTone: Record<EventKind, PillTone | "static"> = {
  info: "accent",
  ok: "ok",
  warn: "warn",
  error: "danger",
  muted: "static",
};

/** 任务事件流（renderEvents 迁移）：新事件从顶部进入，弹簧滑入。 */
export function EventStream({ events, limit = 30, title, maxHeight = 260 }: EventStreamProps) {
  const rows = events.slice(-limit);
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
      {title !== undefined && (
        <Typography variant="caption" sx={{ color: "text.secondary" }}>{title}</Typography>
      )}
      <Box
        sx={{
          maxHeight,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 0.75,
        }}
      >
        <AnimatePresence initial={false}>
          {rows.map((ev, i) => {
            const kind = ev.kind ?? "info";
            return (
              <motion.div
                key={`${ev.at ?? ""}-${ev.name}-${i}`}
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={springs.ui}
                style={{ display: "flex", alignItems: "center", gap: 8 }}
              >
                <Typography
                  component="time"
                  sx={{ ...typeScale.xs, color: "text.disabled", flex: "0 0 auto", width: 64, fontVariantNumeric: "tabular-nums" }}
                >
                  {fmtClock(ev.at ?? null)}
                </Typography>
                {kind !== "info" && (
                  <Pill tone={kindTone[kind] as PillTone} sx={{ flex: "0 0 auto" }}>
                    {kind === "ok" ? "成功" : kind === "warn" ? "警告" : kind === "error" ? "错误" : "提示"}
                  </Pill>
                )}
                <Typography
                  sx={{
                    ...typeScale.bodySm,
                    color: kind === "error" ? "error.main" : kind === "muted" ? "text.secondary" : "text.primary",
                    minWidth: 0,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {ev.name}
                </Typography>
              </motion.div>
            );
          })}
        </AnimatePresence>
        {rows.length === 0 && (
          <Typography sx={{ ...typeScale.bodySm, color: "text.secondary", py: 1 }}>暂无事件</Typography>
        )}
      </Box>
    </Box>
  );
}
