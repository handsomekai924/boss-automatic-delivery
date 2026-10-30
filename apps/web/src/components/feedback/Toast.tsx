import {
  createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode,
} from "react";
import { Box, Typography } from "@mui/material";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { springs } from "../../theme/tokens";

export type ToastTone = "ok" | "warn" | "danger" | "info";

interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
}

export interface ToastApi {
  /** 与 legacy ui.js 的 toast(message, kind, ms) 语义对齐 */
  toast: (message: string, tone?: ToastTone, ms?: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const toneColor: Record<ToastTone, string> = {
  ok: "var(--ok)", warn: "var(--warn)", danger: "var(--danger)", info: "var(--accent)",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);
  const reduced = useReducedMotion();

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback((message: string, tone: ToastTone = "ok", ms = 3200) => {
    const id = nextId.current;
    nextId.current += 1;
    // 最多同时 3 条，超量丢最旧
    setItems((prev) => [...prev.slice(-2), { id, message, tone }]);
    window.setTimeout(() => dismiss(id), ms);
  }, [dismiss]);

  const api = useMemo<ToastApi>(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <Box
        sx={{
          position: "fixed",
          top: 72,
          left: 0,
          right: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 1,
          zIndex: "var(--z-toast)",
          pointerEvents: "none",
        }}
      >
        <AnimatePresence initial={false}>
          {items.map((item) => (
            <motion.div
              key={item.id}
              layout
              initial={reduced ? { opacity: 0 } : { opacity: 0, y: -8, scale: 0.98 }}
              animate={reduced ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
              exit={reduced ? { opacity: 0 } : { opacity: 0, y: -8, scale: 0.98 }}
              transition={springs.ui}
              className="material-thin"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "8px 16px",
                borderRadius: 999,
                pointerEvents: "auto",
              }}
            >
              <Box
                component="span"
                sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: toneColor[item.tone], flex: "0 0 auto" }}
              />
              <Typography variant="body2" sx={{ color: "text.primary", fontWeight: 590 }}>
                {item.message}
              </Typography>
            </motion.div>
          ))}
        </AnimatePresence>
      </Box>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 ToastProvider 内使用");
  return ctx;
}
