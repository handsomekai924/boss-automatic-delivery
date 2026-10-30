/**
 * 设计 token —— docs/DESIGN.md（Apple 设计语言）的数值形态。
 * MUI theme 与 CSS 变量同源于此，禁止两处各写一份。
 */

export type ThemeMode = "light" | "dark";

/** 4pt 节奏刻度（--sp-1..8），间距一律 rem 友好的 px 基准。 */
export const spacing = {
  1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32, 7: 48, 8: 64,
} as const;

/**
 * 圆角一律 px 字符串——MUI 的 sx 对数字 `borderRadius` 会乘 `theme.shape.borderRadius`
 * （写 12 实渲染 12×12=144px，缺陷清单「修改稿原文 144px 异常圆弧」即此坑）；
 * 字符串原样落地，主题 overrides 与 sx 两处才一致。
 */
export const radius = {
  xs: "8px", s: "12px", m: "16px", l: "20px", pill: "999px",
} as const;

export const zIndex = {
  page: 1, sticky: 40, dropdown: 50, topbar: 60, overlay: 100, modal: 110, toast: 130,
} as const;

export const fontFamily =
  '-apple-system, "SF Pro Text", "SF Pro Display", "PingFang SC", "HarmonyOS Sans", "Microsoft YaHei", sans-serif';
export const fontFamilyMono =
  '"SF Mono", "JetBrains Mono", "Cascadia Code", Consolas, monospace';

/**
 * 字阶：字距随字号——大字收紧、小字放松。
 * 一个固定 letter-spacing 全站通用 = 错误（DESIGN.md Banned Patterns）。
 */
export const typeScale = {
  hero:   { fontSize: "clamp(28px, 3.2vw, 34px)", letterSpacing: "-0.4px", lineHeight: 1.15 },
  xl:     { fontSize: "26px", letterSpacing: "-0.3px", lineHeight: 1.2 },
  l:      { fontSize: "19px", letterSpacing: "-0.2px", lineHeight: 1.3 },
  m:      { fontSize: "16px", letterSpacing: "-0.1px", lineHeight: 1.4 },
  body:   { fontSize: "14px", letterSpacing: "0", lineHeight: 1.5 },
  bodySm: { fontSize: "13px", letterSpacing: "0.1px", lineHeight: 1.5 },
  s:      { fontSize: "12px", letterSpacing: "0.1px", lineHeight: 1.45 },
  xs:     { fontSize: "11px", letterSpacing: "0.12px", lineHeight: 1.4 },
} as const;

/** 弹簧动效（motion 库参数）：手势驱动场景一律弹簧，禁关键帧锁死。 */
export const springs = {
  /** 默认 UI：临界阻尼，不回弹（面板/页面/开关） */
  ui: { type: "spring", bounce: 0, duration: 0.35 },
  /** 动量交互：轻微回弹（flick / 拖拽释放 / 分段切换） */
  momentum: { type: "spring", bounce: 0.2, duration: 0.4 },
} as const;

export const microMotion = {
  ease: "cubic-bezier(0.16, 1, 0.3, 1)",
  duration: { fast: 140, base: 220, slow: 320 },
  /** 按压反馈：pointer-down 即 scale，不等 click */
  pressScale: 0.97,
  pressDuration: 100,
} as const;

export interface SemanticPalette {
  bg0: string; bg1: string; bg2: string;
  surface: string; surface2: string;
  separator: string;
  text1: string; text2: string; text3: string;
  accent: string; ok: string; warn: string; danger: string;
  onAccent: string;
}

/** Apple system colors 原值，不自行调色温（docs/DESIGN.md Color）。 */
export const palette: Record<ThemeMode, SemanticPalette> = {
  dark: {
    bg0: "#000000", bg1: "#0D0D0F", bg2: "#1C1C1E",
    surface: "#1C1C1E", surface2: "rgba(120,120,128,0.36)",
    separator: "rgba(84,84,88,0.65)",
    text1: "#FFFFFF", text2: "rgba(235,235,245,0.60)", text3: "rgba(235,235,245,0.30)",
    accent: "#0A84FF", ok: "#30D158", warn: "#FF9F0A", danger: "#FF453A",
    onAccent: "#FFFFFF",
  },
  light: {
    bg0: "#F2F2F7", bg1: "#E9E9EB", bg2: "#FFFFFF",
    surface: "#FFFFFF", surface2: "rgba(120,120,128,0.12)",
    separator: "rgba(60,60,67,0.29)",
    text1: "#000000", text2: "rgba(60,60,67,0.60)", text3: "rgba(60,60,67,0.30)",
    accent: "#0071E3", ok: "#34C759", warn: "#FF9500", danger: "#FF3B30",
    onAccent: "#FFFFFF",
  },
};

export interface MaterialSpec {
  background: string;
  backdropFilter: string;
}

/** 玻璃材质：轻材质（顶栏/浮层）/ 重材质（侧栏）。禁止浅材质叠浅材质。 */
export const materials: Record<ThemeMode, { thin: MaterialSpec; heavy: MaterialSpec; edge: string }> = {
  dark: {
    thin: { background: "rgba(28,28,30,0.72)", backdropFilter: "blur(20px) saturate(180%)" },
    heavy: { background: "rgba(22,22,24,0.82)", backdropFilter: "blur(30px) saturate(180%)" },
    edge: "rgba(255,255,255,0.08)",
  },
  light: {
    thin: { background: "rgba(255,255,255,0.72)", backdropFilter: "blur(20px) saturate(180%)" },
    heavy: { background: "rgba(242,242,247,0.82)", backdropFilter: "blur(30px) saturate(180%)" },
    edge: "rgba(255,255,255,0.4)",
  },
};

/** 反馈语义色取值（tone → 主题色），组件不许自造颜色。 */
export const toneColor = {
  ok: "ok", warn: "warn", danger: "danger", accent: "accent",
} as const;
