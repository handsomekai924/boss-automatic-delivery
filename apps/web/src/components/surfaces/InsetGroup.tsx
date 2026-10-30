import { Box, Typography, type SxProps, type Theme } from "@mui/material";
import type { ReactNode } from "react";
import { mergeSx } from "../../theme/sx";

export interface InsetGroupProps {
  header?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  sx?: SxProps<Theme>;
}

export interface InsetRowProps {
  label: ReactNode;
  detail?: ReactNode;
  /** 右侧控件（开关/按钮/箭头） */
  control?: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
}

/** 设置/表格的 inset-grouped 分组卡（iOS 设置风格）。 */
export function InsetGroup({ header, footer, children, sx }: InsetGroupProps) {
  return (
    <Box sx={mergeSx({ mb: 3 }, sx)}>
      {header !== undefined && (
        <Typography variant="caption" sx={{ color: "text.secondary", px: 1.5, mb: 0.75, display: "block" }}>
          {header}
        </Typography>
      )}
      <Box sx={{ bgcolor: "background.paper", borderRadius: "16px", overflow: "hidden" }}>
        {children}
      </Box>
      {footer !== undefined && (
        <Typography variant="caption" sx={{ color: "text.secondary", px: 1.5, mt: 0.75, display: "block" }}>
          {footer}
        </Typography>
      )}
    </Box>
  );
}

/** 分组内一行：左标签、右细节/控件；给 onClick 则整行可点（按压反馈）。 */
export function InsetRow({ label, detail, control, onClick, disabled = false }: InsetRowProps) {
  const interactive = Boolean(onClick) && !disabled;
  return (
    <Box
      onClick={interactive ? onClick : undefined}
      className={interactive ? "apple-press" : undefined}
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 2,
        px: 2,
        minHeight: 48,
        cursor: interactive ? "pointer" : "default",
        opacity: disabled ? 0.4 : 1,
        transition: "background-color 140ms cubic-bezier(0.16,1,0.3,1)",
        "&:hover": interactive
          ? { bgcolor: (t) => (t.palette.mode === "dark" ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)") }
          : undefined,
        "& + &": { borderTop: "1px solid", borderColor: "divider" },
      }}
    >
      <Box sx={{ flex: 1, minWidth: 0 }}>
        {typeof label === "string" ? <Typography variant="body1">{label}</Typography> : label}
      </Box>
      {detail !== undefined && (
        typeof detail === "string"
          ? <Typography variant="body2" sx={{ color: "text.secondary" }}>{detail}</Typography>
          : detail
      )}
      {control}
    </Box>
  );
}
