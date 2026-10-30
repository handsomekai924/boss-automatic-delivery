import { Box, type BoxProps } from "@mui/material";
import type { ReactNode } from "react";
import { mergeSx } from "../../theme/sx";

export interface SurfaceProps extends Omit<BoxProps, "children"> {
  /** 默认带内距 */
  padded?: boolean;
  /** 可点击卡片：按压反馈 + 手型 */
  interactive?: boolean;
  children?: ReactNode;
}

/** 卡片面：实底（禁止玻璃，玻璃只在顶栏/侧栏/浮层——DESIGN.md Surfaces）。 */
export function Surface({ padded = true, interactive = false, sx, children, ...rest }: SurfaceProps) {
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
          cursor: interactive ? "pointer" : "inherit",
        },
        sx,
      )}
    >
      {children}
    </Box>
  );
}
