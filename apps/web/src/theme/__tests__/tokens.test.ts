import { describe, expect, it } from "vitest";
import { materials, palette, springs, typeScale, radius } from "../tokens";
import { createAppleTheme } from "../muiTheme";

describe("设计 token（docs/DESIGN.md 的数值形态）", () => {
  it("字距随字号：大字收紧、小字放松（禁止全站固定字距）", () => {
    expect(parseFloat(typeScale.hero.letterSpacing)).toBeLessThan(0);
    expect(typeScale.body.letterSpacing).toBe("0");
    expect(parseFloat(typeScale.xs.letterSpacing)).toBeGreaterThan(0);
  });

  it("深浅两套语义色齐全且强调色不同", () => {
    for (const mode of ["dark", "light"] as const) {
      const c = palette[mode];
      expect(c.accent).toBeTruthy();
      expect(c.text1).toBeTruthy();
      expect(c.separator).toBeTruthy();
    }
    expect(palette.dark.accent).not.toBe(palette.light.accent);
  });

  it("材质分轻重两层且带 blur", () => {
    for (const mode of ["dark", "light"] as const) {
      expect(materials[mode].thin.backdropFilter).toContain("blur");
      expect(materials[mode].heavy.backdropFilter).toContain("blur");
    }
  });

  it("弹簧参数：默认临界阻尼不回弹，动量才带回弹", () => {
    expect(springs.ui.bounce).toBe(0);
    expect(springs.momentum.bounce).toBeGreaterThan(0);
    expect(springs.ui.type).toBe("spring");
  });

  it("createAppleTheme 产出双主题且按钮根样式含按压反馈", () => {
    const dark = createAppleTheme("dark");
    const light = createAppleTheme("light");
    expect(dark.palette.mode).toBe("dark");
    expect(light.palette.mode).toBe("light");
    expect(dark.palette.primary.main).toBe(palette.dark.accent);

    const root = dark.components?.MuiButton?.styleOverrides?.root;
    expect(JSON.stringify(root)).toContain("scale(0.97)");
    expect(dark.shape.borderRadius).toBe(radius.s);
  });
});
