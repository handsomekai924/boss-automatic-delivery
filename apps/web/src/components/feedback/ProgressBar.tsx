import { Box } from "@mui/material";
import type { Theme } from "@mui/material/styles";
import { microMotion, palette, type ThemeMode } from "../../theme/tokens";

export interface ProgressBarProps {
  /** 0–100；undefined = 不确定态（活动条） */
  value?: number;
  tone?: "accent" | "ok" | "warn" | "danger";
  height?: number;
  /** 活动态：条缓慢流动（仅 running） */
  active?: boolean;
}

const barColor: Record<NonNullable<ProgressBarProps["tone"]>, (m: ThemeMode) => string> = {
  accent: (m) => palette[m].accent,
  ok: (m) => palette[m].ok,
  warn: (m) => palette[m].warn,
  danger: (m) => palette[m].danger,
};

/** 任务进度条（DESIGN.md Long-Task Panel：宽度过渡 80ms，reduce-motion 也保留）。 */
export function ProgressBar({ value, tone = "accent", height = 6, active = false }: ProgressBarProps) {
  const pct = value == null ? 100 : Math.max(0, Math.min(100, value));
  return (
    <Box
      role="progressbar"
      aria-valuenow={value == null ? undefined : Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      sx={{
        position: "relative",
        width: "100%",
        height,
        borderRadius: "999px",
        overflow: "hidden",
        bgcolor: (t: Theme) => (t.palette.mode === "dark" ? "rgba(120,120,128,0.36)" : "rgba(120,120,128,0.16)"),
      }}
    >
      <Box
        className="task-progress-bar"
        sx={{
          height: "100%",
          width: `${pct}%`,
          borderRadius: "999px",
          bgcolor: (t: Theme) => barColor[tone](t.palette.mode as ThemeMode),
          transition: `width 80ms linear`,
          opacity: active ? 1 : 0.9,
        }}
      />
    </Box>
  );
}

export { microMotion };
