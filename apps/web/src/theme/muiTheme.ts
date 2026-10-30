import { createTheme, type Components, type Shadows, type Theme, type CSSObject } from "@mui/material/styles";
import type { ThemeMode } from "./tokens";
import {
  fontFamily, fontFamilyMono, materials, microMotion, palette, radius, spacing, typeScale, zIndex,
} from "./tokens";

declare module "@mui/material/styles" {
  interface Palette {
    /** 卡片内嵌面 / 次级填充（tokens.surface2） */
    surface2: string;
  }
  interface PaletteOptions {
    surface2: string;
  }
}

/** Apple 阴影：轻柔分层，不用 MUI 默认的生硬 elevation。 */
function appleShadows(mode: ThemeMode): Shadows {
  const soft = mode === "dark"
    ? "0 2px 8px rgba(0,0,0,0.45)"
    : "0 1px 2px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.05)";
  const strong = mode === "dark"
    ? "0 8px 28px rgba(0,0,0,0.55)"
    : "0 2px 6px rgba(0,0,0,0.08), 0 12px 32px rgba(0,0,0,0.10)";
  const list: string[] = ["none"];
  for (let i = 1; i <= 24; i += 1) list.push(i >= 8 ? strong : soft);
  return list as unknown as Shadows;
}

/** token → CSS 变量（global.css 与业务样式只许用变量，不许裸写色值）。 */
function cssVars(mode: ThemeMode): CSSObject {
  const c = palette[mode];
  const m = materials[mode];
  return {
    "--bg-0": c.bg0, "--bg-1": c.bg1, "--bg-2": c.bg2,
    "--surface": c.surface, "--surface-2": c.surface2,
    "--separator": c.separator,
    "--text-1": c.text1, "--text-2": c.text2, "--text-3": c.text3,
    "--accent": c.accent, "--ok": c.ok, "--warn": c.warn, "--danger": c.danger,
    "--text-on-accent": c.onAccent,
    "--material-thin-bg": m.thin.background, "--material-thin-blur": m.thin.backdropFilter,
    "--material-heavy-bg": m.heavy.background, "--material-heavy-blur": m.heavy.backdropFilter,
    "--material-edge": m.edge,
    "--sp-1": `${spacing[1]}px`, "--sp-2": `${spacing[2]}px`, "--sp-3": `${spacing[3]}px`,
    "--sp-4": `${spacing[4]}px`, "--sp-5": `${spacing[5]}px`, "--sp-6": `${spacing[6]}px`,
    "--sp-7": `${spacing[7]}px`, "--sp-8": `${spacing[8]}px`,
    "--r-xs": radius.xs, "--r-s": radius.s, "--r-m": radius.m,
    "--r-l": radius.l, "--r-pill": radius.pill,
    "--z-page": zIndex.page, "--z-sticky": zIndex.sticky, "--z-dropdown": zIndex.dropdown,
    "--z-topbar": zIndex.topbar, "--z-overlay": zIndex.overlay, "--z-modal": zIndex.modal,
    "--z-toast": zIndex.toast,
  };
}

/** Apple 风格 MUI 主题：MUI 只出组件骨架，外观全部经这里定制（docs/DESIGN.md）。 */
export function createAppleTheme(mode: ThemeMode): Theme {
  const c = palette[mode];
  const m = materials[mode];

  const components: Components = {
    MuiCssBaseline: {
      styleOverrides: {
        ":root": cssVars(mode),
        body: {
          background: c.bg0,
          color: c.text1,
          fontFamily,
          WebkitFontSmoothing: "antialiased",
          MozOsxFontSmoothing: "grayscale",
        },
        "::selection": {
          background: mode === "dark" ? "rgba(10,132,255,0.35)" : "rgba(0,113,227,0.2)",
        },
        "*:focus-visible": {
          outline: `2px solid ${c.accent}`,
          outlineOffset: 2,
          borderRadius: 4,
        },
        "::placeholder": { color: c.text3 },
      },
    },

    MuiButton: {
      defaultProps: { disableRipple: true, disableElevation: true, variant: "text" },
      styleOverrides: {
        root: {
          textTransform: "none",
          borderRadius: radius.pill,
          fontWeight: 590,
          letterSpacing: "-0.1px",
          transition: `transform ${microMotion.pressDuration}ms ease-out, background-color ${microMotion.duration.fast}ms ${microMotion.ease}, color ${microMotion.duration.fast}ms ${microMotion.ease}`,
          // pointer-down 即反馈，不等 click（DESIGN.md 硬约束 #1）
          "&:active:not(:disabled)": { transform: `scale(${microMotion.pressScale})` },
          "&:disabled": { opacity: 0.4 },
        },
        sizeSmall: { minHeight: 32, padding: "6px 14px", fontSize: 13 },
        sizeMedium: { minHeight: 40, padding: "8px 18px", fontSize: 15 },
        sizeLarge: { minHeight: 48, padding: "10px 22px", fontSize: 16 },
      },
    },

    MuiIconButton: {
      defaultProps: { disableRipple: true },
      styleOverrides: {
        root: {
          borderRadius: radius.pill,
          transition: `transform ${microMotion.pressDuration}ms ease-out`,
          "&:active:not(:disabled)": { transform: `scale(${microMotion.pressScale})` },
        },
      },
    },

    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: "none",
          backgroundColor: c.surface,
          borderRadius: radius.m,
        },
      },
    },

    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: radius.m,
          boxShadow: appleShadows(mode)[2],
        },
      },
    },

    MuiDialog: {
      styleOverrides: {
        paper: {
          borderRadius: radius.l,
          backgroundColor: c.surface,
          boxShadow: appleShadows(mode)[10],
        },
      },
    },

    MuiDrawer: {
      styleOverrides: {
        paper: {
          backgroundColor: mode === "dark" ? "rgba(22,22,24,0.92)" : "rgba(250,250,252,0.92)",
          backdropFilter: m.heavy.backdropFilter,
          borderRight: `1px solid ${c.separator}`,
        },
      },
    },

    MuiAppBar: {
      defaultProps: { elevation: 0, color: "transparent" },
      styleOverrides: {
        root: {
          background: m.thin.background,
          backdropFilter: m.thin.backdropFilter,
          // 亮边 = 光打在材质上（docs/DESIGN.md Materials）
          boxShadow: `inset 0 -1px 0 ${m.edge}, inset 0 1px 0 ${m.edge}`,
        },
      },
    },

    MuiTableCell: {
      styleOverrides: {
        root: {
          borderColor: c.separator,
          padding: "12px 16px",
          fontSize: typeScale.bodySm.fontSize,
          letterSpacing: typeScale.bodySm.letterSpacing,
        },
        head: {
          color: c.text2,
          fontWeight: 590,
          fontSize: typeScale.s.fontSize,
          letterSpacing: typeScale.s.letterSpacing,
        },
      },
    },

    MuiTableRow: {
      styleOverrides: {
        root: {
          transition: `background-color ${microMotion.duration.fast}ms ${microMotion.ease}`,
          "&:hover": {
            backgroundColor: mode === "dark" ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)",
          },
          "&:last-child td": { borderBottom: 0 },
        },
      },
    },

    MuiChip: {
      styleOverrides: {
        root: {
          borderRadius: radius.pill,
          fontWeight: 590,
          fontSize: typeScale.s.fontSize,
          letterSpacing: typeScale.s.letterSpacing,
        },
      },
    },

    // iOS 风格开关：51×31 轨道、27 圆钮
    MuiSwitch: {
      styleOverrides: {
        root: { width: 51, height: 31, padding: 0 },
        switchBase: {
          padding: 2,
          "&.Mui-checked": {
            transform: "translateX(20px)",
            color: "#FFFFFF",
            "& + .MuiSwitch-track": {
              backgroundColor: c.ok,
              opacity: 1,
              border: 0,
            },
          },
        },
        thumb: { width: 27, height: 27, boxShadow: "0 1px 3px rgba(0,0,0,0.25)" },
        track: {
          borderRadius: radius.pill,
          backgroundColor: mode === "dark" ? "rgba(120,120,128,0.5)" : "rgba(120,120,128,0.32)",
          opacity: 1,
        },
      },
    },

    MuiTextField: {
      defaultProps: { variant: "filled", margin: "dense" },
      styleOverrides: {
        root: {
          "& .MuiFilledInput-root": {
            backgroundColor: c.surface2,
            borderRadius: radius.s,
            overflow: "hidden",
            border: `1px solid transparent`,
            transition: `border-color ${microMotion.duration.fast}ms ${microMotion.ease}`,
            "&:before, &:after": { display: "none" },
            "&.Mui-focused": {
              backgroundColor: c.surface2,
              borderColor: c.accent,
            },
            "&:hover:not(.Mui-focused)": {
              backgroundColor: mode === "dark" ? "rgba(120,120,128,0.42)" : "rgba(120,120,128,0.16)",
            },
          },
          "& .MuiInputLabel-root": { color: c.text2 },
        },
      },
    },

    MuiMenu: {
      styleOverrides: {
        paper: {
          borderRadius: radius.m,
          backgroundColor: m.thin.background,
          backdropFilter: m.thin.backdropFilter,
          border: `1px solid ${c.separator}`,
          boxShadow: appleShadows(mode)[8],
        },
      },
    },

    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          borderRadius: radius.xs,
          backgroundColor: c.bg2,
          color: c.text1,
          fontSize: typeScale.s.fontSize,
          letterSpacing: typeScale.s.letterSpacing,
          boxShadow: appleShadows(mode)[4],
          border: `1px solid ${c.separator}`,
        },
      },
    },

    MuiSkeleton: {
      styleOverrides: {
        root: {
          borderRadius: radius.s,
          backgroundColor: c.surface2,
        },
      },
    },

    MuiLinearProgress: {
      styleOverrides: {
        root: {
          height: 6,
          borderRadius: radius.pill,
          backgroundColor: c.surface2,
        },
        bar: { borderRadius: radius.pill },
      },
    },

    MuiTab: {
      defaultProps: { disableRipple: true },
      styleOverrides: {
        root: {
          textTransform: "none",
          fontWeight: 590,
          letterSpacing: "-0.1px",
          minHeight: 44,
          "&.Mui-selected": { color: c.text1 },
        },
      },
    },

    MuiTabs: {
      styleOverrides: {
        indicator: {
          height: 3,
          borderRadius: radius.pill,
          backgroundColor: c.accent,
        },
      },
    },
  };

  return createTheme({
    palette: {
      mode,
      primary: { main: c.accent, contrastText: c.onAccent },
      success: { main: c.ok },
      warning: { main: c.warn },
      error: { main: c.danger },
      background: { default: c.bg0, paper: c.surface },
      text: { primary: c.text1, secondary: c.text2, disabled: c.text3 },
      divider: c.separator,
      surface2: c.surface2,
    },
    shape: { borderRadius: radius.s },
    shadows: appleShadows(mode),
    typography: {
      fontFamily,
      fontSize: 14,
      h1: { ...typeScale.hero, fontWeight: 700 },
      h2: { ...typeScale.xl, fontWeight: 700 },
      h3: { ...typeScale.l, fontWeight: 600 },
      h4: { ...typeScale.m, fontWeight: 600 },
      h5: { ...typeScale.m, fontWeight: 600 },
      h6: { ...typeScale.body, fontWeight: 600 },
      subtitle1: { ...typeScale.m, fontWeight: 590 },
      subtitle2: { ...typeScale.bodySm, fontWeight: 590 },
      body1: { ...typeScale.body },
      body2: { ...typeScale.bodySm },
      button: { fontSize: "15px", fontWeight: 590, letterSpacing: "-0.1px", lineHeight: 1.2 },
      caption: { ...typeScale.s },
      overline: { ...typeScale.xs, fontWeight: 590 },
    },
    components,
  });
}

export { fontFamilyMono };
