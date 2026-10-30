import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { Box, useTheme } from "@mui/material";
import { motion, useReducedMotion } from "motion/react";
import { springs, typeScale } from "../../theme/tokens";

export interface SegmentedOption<T extends string = string> {
  value: T;
  label: string;
  icon?: ReactNode;
  /** 状态点（如平台登录状态）：ok / warn / danger / busy */
  status?: "ok" | "warn" | "danger" | "busy";
  disabled?: boolean;
}

export interface SegmentedControlProps<T extends string = string> {
  value: T;
  options: SegmentedOption<T>[];
  onChange: (value: T) => void;
  size?: "sm" | "md";
  fullWidth?: boolean;
  "aria-label"?: string;
}

const statusColor: Record<string, string> = {
  ok: "var(--ok)", warn: "var(--warn)", danger: "var(--danger)", busy: "var(--accent)",
};

/**
 * Apple 风格分段控件（平台切换器）：选中块弹簧滑动、可键盘左右切换。
 * 动量交互用带轻微回弹的弹簧；reduced-motion 时改为瞬时切换。
 */
export function SegmentedControl<T extends string = string>({
  value, options, onChange, size = "md", fullWidth = false, "aria-label": ariaLabel,
}: SegmentedControlProps<T>) {
  const theme = useTheme();
  const reduced = useReducedMotion();
  const layoutId = useId();
  const listRef = useRef<HTMLDivElement | null>(null);

  const enabled = options.filter((o) => !o.disabled);
  const move = (dir: 1 | -1) => {
    const idx = enabled.findIndex((o) => o.value === value);
    if (idx < 0 || enabled.length === 0) return;
    const next = enabled[(idx + dir + enabled.length) % enabled.length];
    onChange(next.value);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    }
  };

  return (
    <Box
      ref={listRef}
      role="radiogroup"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      sx={{
        display: "inline-flex",
        alignItems: "center",
        p: "2px",
        borderRadius: "999px",
        bgcolor: "surface2",
        width: fullWidth ? "100%" : "auto",
      }}
    >
      {options.map((opt) => {
        const selected = opt.value === value;
        return (
          <Box
            key={opt.value}
            component="button"
            role="radio"
            aria-checked={selected}
            disabled={opt.disabled}
            onClick={() => onChange(opt.value)}
            className="apple-press"
            sx={{
              position: "relative",
              flex: fullWidth ? 1 : "0 0 auto",
              minWidth: size === "sm" ? 56 : 72,
              height: size === "sm" ? 28 : 32,
              px: size === "sm" ? 1.5 : 2.25,
              border: 0,
              borderRadius: "999px",
              bgcolor: "transparent",
              color: selected ? "text.primary" : "text.secondary",
              cursor: opt.disabled ? "default" : "pointer",
              opacity: opt.disabled ? 0.4 : 1,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 0.75,
              ...(size === "sm" ? typeScale.s : typeScale.bodySm),
              fontWeight: 590,
              WebkitTapHighlightColor: "transparent",
            }}
          >
            {selected && (
              <motion.span
                layoutId={layoutId}
                transition={reduced ? { duration: 0 } : springs.momentum}
                style={{
                  position: "absolute",
                  inset: 0,
                  borderRadius: 999,
                  background: theme.palette.background.paper,
                  boxShadow: theme.shadows[2],
                }}
              />
            )}
            <Box sx={{ position: "relative", zIndex: 1, display: "inline-flex", alignItems: "center", gap: 0.75 }}>
              {opt.icon}
              {opt.label}
              {opt.status && (
                <Box
                  component="span"
                  sx={{
                    width: 6, height: 6, borderRadius: "50%",
                    bgcolor: statusColor[opt.status],
                    display: "inline-block",
                  }}
                />
              )}
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
