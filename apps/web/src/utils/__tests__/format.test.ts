import { describe, expect, it, vi } from "vitest";
import { debounce, fmtClock, fmtCompact, fmtEta, fmtNumber, fmtTime, sanitize } from "../format";

describe("fmtTime / fmtClock", () => {
  it("秒级时间戳 → 本地化；无效 → 兜底", () => {
    const ts = 1700000000; // 2023-11-14 22:13:20 UTC
    expect(fmtTime(ts)).toContain("2023");
    expect(fmtTime(null)).toBe("—");
    expect(fmtTime("not-a-date")).toBe("—");
    expect(fmtClock(ts)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(fmtClock(undefined)).toBe("--:--:--");
  });

  it("Date 对象可用", () => {
    const d = new Date(2026, 0, 1, 8, 30, 0);
    expect(fmtTime(d)).toContain("2026");
  });
});

describe("fmtEta", () => {
  it("秒/分/时三段文案", () => {
    expect(fmtEta(48)).toBe("48 秒");
    expect(fmtEta(192)).toBe("3 分 12 秒");
    expect(fmtEta(180)).toBe("3 分");
    expect(fmtEta(3720)).toBe("1 小时 2 分");
    expect(fmtEta(-1)).toBe("…");
    expect(fmtEta(NaN)).toBe("…");
    expect(fmtEta(null)).toBe("…");
  });
});

describe("fmtNumber", () => {
  it("千分位；无效 → —", () => {
    expect(fmtNumber(12345)).toBe("12,345");
    expect(fmtNumber(null)).toBe("—");
    expect(fmtNumber(Infinity)).toBe("—");
  });
});

describe("fmtCompact", () => {
  it("<1000 原样，≥1000 紧凑；无效 → —", () => {
    expect(fmtCompact(0)).toBe("0");
    expect(fmtCompact(42)).toBe("42");
    expect(fmtCompact(1000)).toBe("1K");
    expect(fmtCompact(1200)).toBe("1.2K");
    expect(fmtCompact(2000)).toBe("2K");
    expect(fmtCompact(1_500_000)).toBe("1.5M");
    expect(fmtCompact(null)).toBe("—");
    expect(fmtCompact(NaN)).toBe("—");
  });
});

describe("sanitize", () => {
  it("转义 HTML 特殊字符", () => {
    expect(sanitize('<img src=x onerror="a">')).toBe("&lt;img src=x onerror=&quot;a&quot;&gt;");
    expect(sanitize("a & b")).toBe("a &amp; b");
    expect(sanitize(null)).toBe("");
  });
});

describe("debounce", () => {
  it("只留最后一次调用；cancel 可取消", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const d = debounce(fn, 100);
    d(1);
    d(2);
    vi.advanceTimersByTime(200);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith(2);

    d(3);
    d.cancel();
    vi.advanceTimersByTime(200);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
