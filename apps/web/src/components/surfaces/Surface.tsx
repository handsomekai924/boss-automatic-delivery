import { Box, type BoxProps } from "@mui/material";
import type { Theme } from "@mui/material/styles";
import type { ReactNode } from "react";
import { mergeSx } from "../../theme/sx";

export interface SurfaceProps extends Omit<BoxProps, "children"> {
  /** 默认带内距 */
  padded?: boolean;
  /** 可点击卡片：按压反馈 + 手型 */
  interactive?: boolean;
  /** 可点击列表项：只悬浮高亮、无按压缩放（JobCard 同款）——勾选/整卡点击的手感不分裂 */
  hoverable?: boolean;
  children?: ReactNode;
}

/** 卡片面：实底（禁止玻璃，玻璃只在顶栏/侧栏/浮层——DESIGN.md Surfaces）。 */
export function Surface({
  padded = true, interactive = false, hoverable = false, sx, children, ...rest
}: SurfaceProps) {
  // 悬浮高亮（无按压缩放）：列表项整卡可点但带内部控件时用，避免误触感
  const hoverSx = hoverable
    ? {
        transition: "box-shadow 140ms ease, background-color 140ms ease",
        "&:hover": {
          boxShadow: (t: Theme) => t.shadows[5],
          bgcolor: (t: Theme) => (t.palette.mode === "dark" ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.02)"),
        },
      }
    : null;
  return (
    <Box
      {...rest}
      className={interactive ? "apple-press" : rest.className}
      sx={mergeSx(
        {
          bgcolor: "background.paper",
          borderRadius: "16px",
          boxShadow: (t) => t.shadows[2],
          p: padded ? 2.5 : 0,
          cursor: interactive || hoverable ? "pointer" : "inherit",
        },
        hoverSx,
        sx,
      )}
    >
      {children}
    </Box>
  );
}
