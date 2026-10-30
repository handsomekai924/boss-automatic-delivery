import type { ReactNode } from "react";
import type { SxProps, Theme } from "@mui/material/styles";
import Box from "@mui/material/Box";

import { mergeSx } from "../../theme/sx";

/**
 * 整页固定 + 内部滚动（布局契约）：
 * 外层（ShellLayout / CONTENT_SX）占满视口且 overflow: hidden，页面整体不滚动；
 * 页面根用 `pageRootSx` 撑满内容区并裁掉溢出，滚动只发生在 `ScrollArea` 里。
 */
export const pageRootSx = {
  flex: 1,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
} as const;

/**
 * 页面内部滚动区：滚动条只出现在这里（列表区 / 内容区）。
 * 默认内边距给卡片阴影留白——滚动区是裁剪边界，padding 在裁剪区内，
 * 阴影伸进留白仍可见（appleShadows 软阴影约 12px）。
 */
export function ScrollArea({ children, sx }: { children: ReactNode; sx?: SxProps<Theme> }) {
  return (
    <Box sx={mergeSx({ flex: 1, minHeight: 0, overflowY: "auto", p: 1.5 }, sx)}>{children}</Box>
  );
}
