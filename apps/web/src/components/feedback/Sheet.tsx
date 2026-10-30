import { useEffect, type ReactNode } from "react";
import { Box, Typography } from "@mui/material";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { springs, zIndex } from "../../theme/tokens";

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  /** 进出方向：右侧面板 / 底部抽屉 / 居中弹窗（同路进出——DESIGN.md 硬约束 #7） */
  anchor?: "right" | "bottom" | "center";
  width?: number | string;
  footer?: ReactNode;
  children: ReactNode;
}

/** Sheet/Modal：dim 聚焦 + 材质化进入（blur+scale，不是纯淡入）。 */
export function Sheet({
  open, onClose, title, anchor = "right", width = 420, footer, children,
}: SheetProps) {
  const reduced = useReducedMotion();

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 居中弹窗用 x/y 的 -50% 做位移（motion 写 transform，静态 translate(-50%,-50%) 会被吃掉，
  // 这就是「弹窗不居中」的根因）；两侧/底部面板用小位移进出（同路进出）。
  const centered = anchor === "center";
  const baseX = centered ? "-50%" : 0;
  const baseY = centered ? "-50%" : 0;
  const offX = anchor === "right" ? 16 : 0;
  const offY = anchor === "bottom" ? 16 : 0;

  const enter = reduced
    ? { opacity: 0, x: baseX, y: baseY }
    : { opacity: 0, scale: 0.98, filter: "blur(8px)", x: baseX || offX, y: baseY || offY };
  const shown = { opacity: 1, scale: 1, filter: "blur(0px)", x: baseX, y: baseY };
  const exit = reduced
    ? { opacity: 0, x: baseX, y: baseY }
    : { opacity: 0, scale: 0.98, filter: "blur(8px)", x: baseX || offX, y: baseY || offY };

  const positionSx =
    anchor === "right"
      ? { top: 0, right: 0, bottom: 0, borderRadius: "20px 0 0 20px", width, maxWidth: "92vw", height: "100%" }
      : anchor === "bottom"
        ? { left: 0, right: 0, bottom: 0, borderRadius: "20px 20px 0 0", width: "100%", maxHeight: "85vh" }
        : { top: "50%", left: "50%", borderRadius: "20px", width, maxWidth: "92vw", maxHeight: "85vh" };

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="sheet-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduced ? 0.15 : 0.25 }}
            onClick={onClose}
            style={{
              position: "fixed",
              inset: 0,
              background: "rgba(0,0,0,0.4)",
              zIndex: zIndex.overlay,
            }}
          />
          <motion.div
            key="sheet-panel"
            role="dialog"
            aria-modal="true"
            initial={enter}
            animate={shown}
            exit={exit}
            transition={reduced ? { duration: 0.15 } : springs.ui}
            style={{
              position: "fixed",
              zIndex: zIndex.modal,
              background: "var(--surface)",
              boxShadow: "0 12px 48px rgba(0,0,0,0.28)",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
              ...positionSx,
            }}
          >
            {title !== undefined && (
              <Box
                sx={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 2,
                  px: 2.5,
                  py: 2,
                  flex: "0 0 auto",
                }}
              >
                <Typography variant="h4" sx={{ fontWeight: 600 }}>{title}</Typography>
                <Box
                  component="button"
                  aria-label="关闭"
                  onClick={onClose}
                  className="apple-press"
                  sx={{
                    border: 0,
                    width: 30,
                    height: 30,
                    borderRadius: "50%",
                    bgcolor: "surface2",
                    color: "text.secondary",
                    cursor: "pointer",
                    fontSize: 15,
                    lineHeight: 1,
                  }}
                >
                  ✕
                </Box>
              </Box>
            )}
            <Box sx={{ flex: 1, overflowY: "auto", px: 2.5, pb: 2.5 }}>{children}</Box>
            {footer !== undefined && (
              <Box sx={{ px: 2.5, py: 2, borderTop: "1px solid", borderColor: "divider", flex: "0 0 auto" }}>
                {footer}
              </Box>
            )}
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
