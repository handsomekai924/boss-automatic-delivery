import { Button as MuiButton, type ButtonProps as MuiButtonProps, type SxProps, type Theme } from "@mui/material";
import { alpha } from "@mui/material/styles";
import type { ReactNode } from "react";
import { mergeSx } from "../../theme/sx";

export type ButtonVariant = "filled" | "gray" | "tinted" | "plain" | "danger";

export interface ButtonProps extends Omit<MuiButtonProps, "variant" | "color"> {
  /** Apple 按钮语义：filled 主操作 / gray 次操作 / tinted 弱调 / plain 纯文字 / danger 破坏 */
  variant?: ButtonVariant;
  startIcon?: ReactNode;
  /** `component="a"` 时的链接属性——MUI 多态类型在包装层会丢，显式补回，尺寸仍和其它按钮一致 */
  href?: string;
  target?: string;
  rel?: string;
}

const variantSx: Record<ButtonVariant, SxProps<Theme>> = {
  filled: (t) => ({
    backgroundColor: t.palette.primary.main,
    color: t.palette.primary.contrastText,
    "&:hover:not(:disabled)": { backgroundColor: t.palette.primary.main, filter: "brightness(0.9)" },
    "&:active:not(:disabled)": { filter: "brightness(0.82)" },
  }),
  gray: (t) => ({
    backgroundColor: t.palette.surface2,
    color: t.palette.text.primary,
    "&:hover:not(:disabled)": {
      backgroundColor: t.palette.mode === "dark" ? "rgba(120,120,128,0.46)" : "rgba(120,120,128,0.2)",
    },
  }),
  tinted: (t) => ({
    backgroundColor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.22 : 0.12),
    color: t.palette.primary.main,
    "&:hover:not(:disabled)": {
      backgroundColor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.3 : 0.18),
    },
  }),
  plain: (t) => ({
    backgroundColor: "transparent",
    color: t.palette.primary.main,
    "&:hover:not(:disabled)": {
      backgroundColor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.12 : 0.08),
    },
  }),
  danger: (t) => ({
    backgroundColor: t.palette.error.main,
    color: "#FFFFFF",
    "&:hover:not(:disabled)": { backgroundColor: t.palette.error.main, filter: "brightness(0.9)" },
    "&:active:not(:disabled)": { filter: "brightness(0.82)" },
  }),
};

/** Apple 风格按钮：胶囊圆角、pointer-down 即 scale(0.97)（按下反馈在 theme 根样式）。 */
export function Button({ variant = "gray", sx, ...rest }: ButtonProps) {
  return <MuiButton {...rest} sx={mergeSx(variantSx[variant], sx)} />;
}
