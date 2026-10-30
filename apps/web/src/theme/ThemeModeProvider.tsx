import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from "react";
import { CssBaseline, ThemeProvider } from "@mui/material";
import { createAppleTheme } from "./muiTheme";
import type { ThemeMode } from "./tokens";

/** 与 legacy 前端共用的主题键：共存期两套 UI 主题同步。 */
const STORAGE_KEY = "orbit-theme";

interface ThemeModeContextValue {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  toggle: () => void;
}

const ThemeModeContext = createContext<ThemeModeContextValue | null>(null);

function readInitialMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
    if (window.matchMedia?.("(prefers-color-scheme: light)").matches) return "light";
  } catch {
    /* localStorage 不可用时落 dark */
  }
  return "dark";
}

export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(readInitialMode);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", mode);
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      /* 忽略持久化失败 */
    }
  }, [mode]);

  const toggle = useCallback(() => {
    setMode((prev) => (prev === "dark" ? "light" : "dark"));
  }, []);

  const value = useMemo<ThemeModeContextValue>(
    () => ({ mode, setMode, toggle }),
    [mode, toggle],
  );
  const theme = useMemo(() => createAppleTheme(mode), [mode]);

  return (
    <ThemeModeContext.Provider value={value}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </ThemeProvider>
    </ThemeModeContext.Provider>
  );
}

export function useThemeMode(): ThemeModeContextValue {
  const ctx = useContext(ThemeModeContext);
  if (!ctx) throw new Error("useThemeMode 必须在 ThemeModeProvider 内使用");
  return ctx;
}
