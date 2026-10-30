/**
 * 分析历史：跑过的匹配归档列表，可载回工作台 / 删除（带确认）。
 *
 * 载入会把结果写回页面级状态并切去工作台——历史是归档，工作台才是改招呼语的地方。
 *
 * 布局（缺陷「分析历史顶部栏随内容滚动」）：根 fill，「历史分析 + 刷新」标题行固定不滚，
 * 只有下方历史项列表在 ScrollArea 里滚（与全站「容器头部固定 + 列表局部滚动」一致）。
 */

import { useEffect, useState } from "react";
import { Box, Typography } from "@mui/material";

import { Button, EmptyState, pageRootSx, Pill, ScrollArea, Sheet, Surface, useToast } from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtNumber, fmtTime } from "../../../utils/format";
import { matchApi } from "../repos";
import {
  analysisStatusLabel, errText, type AnalysisRow,
} from "./types";

export function HistorySection({
  rows, setRows, onLoadAnalysis, onDelete,
}: {
  rows: AnalysisRow[];
  setRows: (fn: (prev: AnalysisRow[]) => AnalysisRow[]) => void;
  /** 载入一份历史分析到工作台（壳层负责写结果状态并切页签）。 */
  onLoadAnalysis: (id: string) => Promise<void>;
  /** 删除后若删的是当前工作台那份，壳层会顺手清结果。 */
  onDelete: (id: string) => void;
}) {
  const toast = useToast();
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // 进这层才拉一次（懒加载，与设置页各段同款）；壳层跑完任务也会刷 rows，这里跟着显示最新
  useEffect(() => {
    void (async () => {
      try {
        const data = await matchApi.analyses();
        setRows(() => (data.items ?? []) as unknown as AnalysisRow[]);
      } catch (e) {
        toast.toast(errText(e), "danger");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadAnalysis = async (id: string) => {
    setLoadingId(id);
    try {
      await onLoadAnalysis(id);
      toast.toast("已载入历史分析", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      setLoadingId(null);
    }
  };

  const removeAnalysis = async (id: string) => {
    try {
      await matchApi.removeAnalysis(id);
      setRows((prev) => prev.filter((r) => r.analysis_id !== id));
      onDelete(id);
      toast.toast("已删除", "ok");
    } catch (e) {
      toast.toast(errText(e), "danger");
    } finally {
      setConfirmDeleteId(null);
    }
  };

  return (
    <Box sx={pageRootSx}>
      {/* 头部标题固定在顶，不随列表滚 */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1.5, flex: "0 0 auto" }}>
        <Typography sx={{ ...typeScale.m, fontWeight: 600, flex: 1 }}>历史分析</Typography>
        <Button
          size="small" variant="gray"
          onClick={() => {
            void (async () => {
              try {
                const data = await matchApi.analyses();
                setRows(() => (data.items ?? []) as unknown as AnalysisRow[]);
              } catch (e) {
                toast.toast(errText(e), "danger");
              }
            })();
          }}
        >
          刷新
        </Button>
      </Box>

      {/* 只有历史项列表局部滚动 */}
      <ScrollArea>
        {rows.length === 0 ? (
          <EmptyState
            title="还没有历史分析"
            description="跑过一次匹配后，结果会归档到这里，随时可以载回来看或删掉。"
          />
        ) : (
          <Box>
            {rows.map((row) => (
              <Surface
                key={row.analysis_id}
                role="group"
                aria-label={`历史分析 ${row.analysis_id}`}
                sx={{ mb: 1.5, display: "flex", gap: 1.5, alignItems: "center", flexWrap: "wrap" }}
              >
                <Box sx={{ flex: 1, minWidth: 200 }}>
                  <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>
                    {row.resume_title || row.analysis_id}
                  </Typography>
                  <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
                    {fmtTime(row.created_at)} · {fmtNumber(row.job_count)} 个职位 · 模型 {row.model || "—"} · {analysisStatusLabel(row.status)}
                  </Typography>
                </Box>
                <Pill tone="accent">最高 {fmtNumber(row.top_score)}</Pill>
                <Button
                  size="small" variant="gray"
                  disabled={loadingId === row.analysis_id}
                  onClick={() => void loadAnalysis(row.analysis_id)}
                >
                  {loadingId === row.analysis_id ? "载入中…" : "载入"}
                </Button>
                <Button size="small" variant="plain" onClick={() => setConfirmDeleteId(row.analysis_id)}>
                  删除
                </Button>
              </Surface>
            ))}
          </Box>
        )}
      </ScrollArea>

      {/* 删除确认留在滚动区外 */}
      <Sheet
        open={confirmDeleteId !== null}
        onClose={() => setConfirmDeleteId(null)}
        title="删除分析"
        anchor="center"
        width={380}
        footer={
          <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
            <Button variant="gray" onClick={() => setConfirmDeleteId(null)}>取消</Button>
            <Button variant="danger" onClick={() => confirmDeleteId && void removeAnalysis(confirmDeleteId)}>
              确认删除
            </Button>
          </Box>
        }
      >
        <Typography sx={{ ...typeScale.bodySm }}>
          删除这份分析？删除后不可恢复。
        </Typography>
      </Sheet>
    </Box>
  );
}
