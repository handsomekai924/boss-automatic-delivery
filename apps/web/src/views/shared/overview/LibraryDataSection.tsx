/**
 * 总览「职位库」数据区（缺陷清单「提议：可以放置一些可视化图表」）。
 *
 * 图表遵守 dataviz 规范：单值放 stat tile（不画单柱图）；采集保留率是单一比例 →
 * 同色系 meter（不是两片饼图）；城市分布是名义类目，所有条同用 slot-1 蓝
 * （不按数值渐变）；采集流水只标极值，其余靠 tooltip 与表格孪生。单系列不出
 * 图例（标题就是系列名）。颜色只用主题的 `--accent`/文本 token，明暗两套都是
 * validate_palette 过检的单色系（蓝 #0071E3 / #0A84FF）。
 */

import { useEffect, useState } from "react";
import { Box, Typography } from "@mui/material";

import { BarChart, ChartCard, ColumnChart, InlineAlert, StatTile, Surface } from "../../../components";
import type { BarDatum, ChartTable, ColumnDatum } from "../../../components";
import { useRunningTasks } from "../../../stores/taskStore";
import { jobsApi } from "../../platform/repos";
import { fmtNumber } from "../../../utils/format";
import { typeScale } from "../../../theme/tokens";
import { mergeStats, type LibraryStats } from "./stats";

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function LibraryDataSection({ siteIds }: { siteIds: string[] }) {
  const running = useRunningTasks();
  const [stats, setStats] = useState<LibraryStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const sitesKey = siteIds.join(",");
  // 任务起停（不管进度）就重取一轮——抓完一轮数字不能一直旧着
  const runningKey = running
    .map((t) => t.task_id)
    .sort()
    .join(",");

  useEffect(() => {
    const sites = sitesKey ? sitesKey.split(",") : [];
    if (sites.length === 0) {
      setStats(null);
      setLoadError(null);
      return;
    }
    let alive = true;
    setLoading(true);
    void (async () => {
      const parts = await Promise.all(
        sites.map((site) =>
          jobsApi
            .stats(site)
            .then((data) => ({ site, ok: true as const, data }))
            .catch((e: unknown) => ({ site, ok: false as const, err: errText(e) })),
        ),
      );
      if (!alive) return;
      const ok = parts.flatMap((p) => (p.ok ? [{ site: p.site, data: p.data }] : []));
      const errs = parts.flatMap((p) => (p.ok ? [] : [p.err]));
      if (ok.length > 0) {
        setStats(mergeStats(ok));
        setLoadError(null);
      } else {
        setLoadError(errs[0] ?? "职位数据取不到");
      }
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [sitesKey, runningKey]);

  if (sitesKey.length === 0) return null;

  return (
    <>
      <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1.5 }}>
        职位库
      </Typography>
      {loadError && !stats ? (
        <Box sx={{ mb: 2 }}>
          <InlineAlert tone="danger">职位数据取不到：{loadError}</InlineAlert>
        </Box>
      ) : null}
      {stats ? (
        <Box sx={{ opacity: loading ? 0.55 : 1, transition: "opacity 160ms ease" }}>
          <LibraryCharts stats={stats} />
        </Box>
      ) : loading ? (
        <Typography sx={{ ...typeScale.bodySm, color: "text.secondary", mb: 2 }}>加载中…</Typography>
      ) : null}
    </>
  );
}

function LibraryCharts({ stats }: { stats: LibraryStats }) {
  const keepRate = stats.raw > 0 ? stats.kept / stats.raw : 0;

  const cityBars: BarDatum[] = stats.cities.map((c) => ({
    label: c.city || "未知",
    value: c.count,
  }));
  const cityTable: ChartTable = {
    head: ["城市", "职位数"],
    rows: stats.cities.map((c) => [c.city || "未知", fmtNumber(c.count)]),
  };

  const pageBars: ColumnDatum[] = stats.pageLog.map((r) => ({
    label: r.at.slice(11, 16) || `第${r.page}页`,
    value: r.kept,
    hint: `第 ${r.page} 页 · 原始 ${fmtNumber(r.raw)} · 丢弃 ${fmtNumber(r.dropped)} · 新增 ${fmtNumber(r.inserted)} · 更新 ${fmtNumber(r.updated)}`,
  }));
  const pageTable: ChartTable = {
    head: ["时间", "站点", "页", "原始", "保留", "丢弃", "新增", "更新"],
    rows: stats.pageLog.map((r) => [
      r.at.slice(11, 16),
      r.site,
      String(r.page),
      fmtNumber(r.raw),
      fmtNumber(r.kept),
      fmtNumber(r.dropped),
      fmtNumber(r.inserted),
      fmtNumber(r.updated),
    ]),
  };

  return (
    <>
      <Surface sx={{ mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
          <StatTile label="在库职位" value={fmtNumber(stats.jobs)} />
          <StatTile label="采集页数" value={fmtNumber(stats.pages)} />
          <StatTile
            label="采集保留率"
            value={`${(keepRate * 100).toFixed(1)}%`}
            caption={`保留 ${fmtNumber(stats.kept)} · 丢弃 ${fmtNumber(stats.dropped)}`}
            meter={keepRate}
          />
          <StatTile
            label="缺失描述"
            value={fmtNumber(stats.missingDesc)}
            caption="还没抓到 JD 的在库职位"
          />
        </Box>
      </Surface>

      <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, mb: 4 }}>
        <ChartCard
          sx={{ flex: "1 1 320px", minWidth: 280 }}
          title="城市分布"
          caption={`在库职位按城市计数（前 ${stats.cities.length} 名）`}
          table={cityTable}
        >
          <BarChart data={cityBars} unit=" 个" />
        </ChartCard>
        <ChartCard
          sx={{ flex: "1 1 320px", minWidth: 280 }}
          title="采集流水"
          caption="近 10 页 · 每页保留职位数"
          table={pageTable}
        >
          <ColumnChart data={pageBars} unit=" 个" />
        </ChartCard>
      </Box>
    </>
  );
}
