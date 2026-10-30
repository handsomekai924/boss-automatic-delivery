import { describe, expect, it } from "vitest";

import { mergeStats, normalizeStats } from "../overview/stats";

describe("normalizeStats", () => {
  it("归一 stats 形状；缺 inserted 别名时读 inserted_count", () => {
    const s = normalizeStats("boss", {
      jobs: 10,
      pages: 2,
      raw_seen: 20,
      kept_seen: 15,
      dropped_seen: 5,
      missing_desc: 3,
      top_cities: [{ city: "广州", count: 7 }],
      pages_log: [
        {
          page: 1,
          raw_count: 20,
          kept_count: 15,
          dropped_count: 5,
          inserted_count: 12,
          updated_count: 3,
          fetched_at: "2026-10-28 14:02:03",
        },
      ],
    });
    expect(s).not.toBeNull();
    expect(s?.cities).toEqual([{ city: "广州", count: 7 }]);
    expect(s?.pageLog[0]).toMatchObject({ site: "boss", at: "2026-10-28 14:02:03", inserted: 12, updated: 3 });
  });

  it("不是 stats 形状（如平台描述符 JSON）→ null，不画成全 0", () => {
    expect(normalizeStats("boss", { platforms: [] })).toBeNull();
    expect(normalizeStats("boss", "oops")).toBeNull();
    expect(normalizeStats("boss", null)).toBeNull();
  });
});

describe("mergeStats", () => {
  it("计数求和、城市合并取前 N、页流水按时间取最近 N 条且时间正序", () => {
    const merged = mergeStats([
      {
        site: "boss",
        data: {
          jobs: 10,
          pages: 1,
          raw_seen: 20,
          kept_seen: 15,
          dropped_seen: 5,
          missing_desc: 3,
          top_cities: [
            { city: "广州", count: 7 },
            { city: "深圳", count: 2 },
          ],
          pages_log: [
            { page: 1, kept_count: 5, fetched_at: "2026-10-28 10:00:00" },
            { page: 2, kept_count: 6, fetched_at: "2026-10-28 12:00:00" },
          ],
        },
      },
      {
        site: "other",
        data: {
          jobs: 5,
          pages: 1,
          raw_seen: 8,
          kept_seen: 6,
          dropped_seen: 2,
          // missing_desc 是全库计数：合并取 max，不求和
          missing_desc: 4,
          top_cities: [{ city: "广州", count: 3 }],
          pages_log: [{ page: 1, kept_count: 9, fetched_at: "2026-10-28 11:00:00" }],
        },
      },
    ]);

    expect(merged.jobs).toBe(15);
    expect(merged.pages).toBe(2);
    expect(merged.missingDesc).toBe(4);
    expect(merged.cities).toEqual([
      { city: "广州", count: 10 },
      { city: "深圳", count: 2 },
    ]);
    // 最近 3 条按时间正序
    expect(merged.pageLog.map((r) => r.at)).toEqual([
      "2026-10-28 10:00:00",
      "2026-10-28 11:00:00",
      "2026-10-28 12:00:00",
    ]);
    expect(merged.pageLog.map((r) => r.site)).toEqual(["boss", "other", "boss"]);
  });

  it("全空输入 → 全 0，不炸", () => {
    const merged = mergeStats([]);
    expect(merged.jobs).toBe(0);
    expect(merged.cities).toEqual([]);
    expect(merged.pageLog).toEqual([]);
    expect(mergeStats([{ site: "boss", data: { platforms: [] } }]).missingDesc).toBe(0);
  });
});
