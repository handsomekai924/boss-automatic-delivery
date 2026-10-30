import { Box, Typography } from "@mui/material";
import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}

/** 页面头：全站统一「大标题 + 一句话说明 + 右侧主操作」（docs/DESIGN.md Page head）。 */
export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "space-between",
        gap: 3,
        mb: 3,
        flexWrap: "wrap",
      }}
    >
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="h1">{title}</Typography>
        {subtitle !== undefined && (
          <Typography variant="body1" sx={{ color: "text.secondary", mt: 0.75 }}>
            {subtitle}
          </Typography>
        )}
      </Box>
      {actions !== undefined && <Box sx={{ display: "flex", gap: 1.5, alignItems: "center" }}>{actions}</Box>}
    </Box>
  );
}
