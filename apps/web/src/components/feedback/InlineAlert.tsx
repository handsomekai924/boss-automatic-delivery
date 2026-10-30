import { Box, Typography } from "@mui/material";
import type { ReactNode } from "react";

export type InlineAlertTone = "ok" | "warn" | "danger" | "info";

export interface InlineAlertProps {
  tone?: InlineAlertTone;
  children: ReactNode;
}

const toneStyle: Record<InlineAlertTone, { icon: string; color: string }> = {
  ok: { icon: "✓", color: "var(--ok)" },
  warn: { icon: "!", color: "var(--warn)" },
  danger: { icon: "✕", color: "var(--danger)" },
  info: { icon: "i", color: "var(--accent)" },
};

/** 行内告警：反馈四类（状态/完成/警告/错误）里的警告与错误载体。 */
export function InlineAlert({ tone = "info", children }: InlineAlertProps) {
  const t = toneStyle[tone];
  return (
    <Box
      role={tone === "danger" ? "alert" : "status"}
      sx={{
        display: "flex",
        alignItems: "flex-start",
        gap: 1.25,
        p: 1.5,
        borderRadius: "12px",
        bgcolor: (th) => (th.palette.mode === "dark" ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.04)"),
      }}
    >
      <Box
        aria-hidden="true"
        sx={{
          width: 20, height: 20, borderRadius: "50%",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          bgcolor: t.color, color: "#fff", fontSize: 12, fontWeight: 700, mt: "1px",
          flex: "0 0 auto",
        }}
      >
        {t.icon}
      </Box>
      <Typography variant="body2" sx={{ color: "text.primary", pt: "2px" }}>
        {children}
      </Typography>
    </Box>
  );
}
