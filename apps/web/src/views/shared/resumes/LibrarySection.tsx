/**
 * 简历库：上传 + 简历清单。
 *
 * 清单行带解析状态徽章、查看原文 / 解析 / 删除；解析进度的任务卡在「简历解析」段，
 * 这里只保留徽章（解析中 / 已解析 / 未解析），避免清单和详情挤一屏。
 */

import { useRef, useState } from "react";
import { Box, Typography } from "@mui/material";

import { Button, ConfirmSheet, EmptyState, Pill, Skeleton, Surface, useToast } from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { fmtTime } from "../../../utils/format";
import { resumeApi } from "../repos";
import { PARSE_TERMINAL, msgOf, type ParseRun, type ResumeListItem } from "./types";

export function LibrarySection({
  resumes, currentId, runs, loading, rawBusyId, parseBusyId, onSelect, onChanged, onRunParse, onViewRaw,
}: {
  resumes: ResumeListItem[];
  currentId: string;
  runs: Record<string, ParseRun>;
  /** 列表在途：骨架屏预占，别闪一下空态 */
  loading: boolean;
  /** 查看原文 / 发起解析在途的简历（按钮「…中」+ 禁点防连发） */
  rawBusyId: string | null;
  parseBusyId: string | null;
  onSelect: (id: string) => void;
  onChanged: () => void;
  onRunParse: (item: ResumeListItem) => void;
  onViewRaw: (item: ResumeListItem) => void;
}) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  /** 破坏性操作二次确认（缺陷清单「破坏性操作」）：确认弹窗后才删。 */
  const [removeTarget, setRemoveTarget] = useState<ResumeListItem | null>(null);
  const [removing, setRemoving] = useState(false);
  const [uploading, setUploading] = useState(false);

  /** 上传简历；成功后刷新列表、选中新简历并自动发起解析（与旧页一致）。 */
  const upload = async (file: File) => {
    if (uploading) return;
    setUploading(true);
    try {
      const draft = await resumeApi.upload(file);
      toast.toast("简历已上传，开始解析…", "ok");
      onChanged();
      onSelect(draft.resume_id);
      onRunParse({ resume_id: draft.resume_id, title: draft.resume_id, created_at: 0 });
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setUploading(false);
    }
  };

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) void upload(f);
    e.target.value = "";
  };

  /** 删除简历：先弹窗二次确认（`removeTarget`），确认后才真删；弹窗等删除回包再关。 */
  const remove = (item: ResumeListItem) => setRemoveTarget(item);

  const runRemove = async () => {
    const item = removeTarget;
    if (!item || removing) return;
    setRemoving(true);
    try {
      await resumeApi.remove(item.resume_id);
      toast.toast("已删除", "ok");
      if (item.resume_id === currentId) onSelect("");
      setRemoveTarget(null);
      onChanged();
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setRemoving(false);
    }
  };

  return (
    <Box>
      <Surface sx={{ mb: 2.5, display: "flex", gap: 1.5, alignItems: "center", flexWrap: "wrap" }}>
        <Typography sx={{ ...typeScale.s, color: "text.secondary", flex: 1, minWidth: 220 }}>
          Word（.docx）/ PDF 最大 8MB；Markdown（.md）/ 纯文本（.txt）最大 2MB。上传后自动发起解析。
        </Typography>
        <Button variant="filled" disabled={uploading} onClick={() => fileRef.current?.click()}>
          {uploading ? "上传中…" : "上传简历"}
        </Button>
        {/* 隐藏 input 收口文件选择；拖拽上传不迁（见文件头） */}
        <input
          ref={fileRef}
          type="file"
          accept=".docx,.pdf,.md,.markdown,.txt"
          hidden
          onChange={onFileChange}
        />
      </Surface>

      {loading ? (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          {Array.from({ length: 3 }, (_, i) => <Skeleton key={i} height={88} />)}
        </Box>
      ) : resumes.length === 0 ? (
        <EmptyState
          title="还没有简历"
          description="上传一份简历（Word / PDF / Markdown / 纯文本），AI 会整理成固定八段，后面人岗匹配用的就是这份结果。"
          action={
            <Button variant="filled" disabled={uploading} onClick={() => fileRef.current?.click()}>
              {uploading ? "上传中…" : "上传简历"}
            </Button>
          }
        />
      ) : (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          {resumes.map((item) => {
            const run = runs[item.resume_id];
            const busy = Boolean(run && !PARSE_TERMINAL.has(run.status));
            const parsed = item.llm?.has_data === true;
            const name = item.title || item.resume_id;
            return (
              <Surface
                key={item.resume_id}
                hoverable
                onClick={() => onSelect(item.resume_id)}
                sx={{ outline: item.resume_id === currentId ? "2px solid var(--accent)" : "none" }}
              >
                <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
                  <Typography sx={{ ...typeScale.bodySm, fontWeight: 590, flex: 1, minWidth: 0 }} noWrap>
                    {name}
                  </Typography>
                  {busy ? <Pill tone="accent">解析中</Pill> : parsed ? <Pill tone="ok">已解析</Pill> : <Pill tone="static">未解析</Pill>}
                </Box>
                <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 0.5 }}>
                  {item.source_name || "—"} · {fmtTime(item.created_at)}
                </Typography>
                <Box sx={{ display: "flex", gap: 1, mt: 1.5, flexWrap: "wrap" }}>
                  <Button
                    size="small"
                    variant="gray"
                    disabled={rawBusyId === item.resume_id}
                    aria-label={`查看原文「${name}」`}
                    onClick={(e) => { e.stopPropagation(); onViewRaw(item); }}
                  >
                    {rawBusyId === item.resume_id ? "查看中…" : "查看原文"}
                  </Button>
                  <Button
                    size="small"
                    variant="tinted"
                    disabled={busy || parseBusyId === item.resume_id}
                    aria-label={`${parsed ? "重新解析" : "解析"}「${name}」`}
                    onClick={(e) => { e.stopPropagation(); onRunParse(item); }}
                  >
                    {busy || parseBusyId === item.resume_id ? "解析中…" : parsed ? "重新解析" : "解析"}
                  </Button>
                  <Button
                    size="small"
                    variant="plain"
                    disabled={busy}
                    aria-label={`删除「${name}」`}
                    onClick={(e) => { e.stopPropagation(); remove(item); }}
                  >
                    删除
                  </Button>
                </Box>
              </Surface>
            );
          })}
        </Box>
      )}

      <ConfirmSheet
        open={removeTarget != null}
        title={`删除「${removeTarget ? removeTarget.title || removeTarget.resume_id : ""}」？`}
        description="这份简历和它的解析结果一起删，删除后不可恢复。"
        confirmText={removing ? "删除中…" : "确认删除"}
        danger
        busy={removing}
        onClose={() => setRemoveTarget(null)}
        onConfirm={() => void runRemove()}
      />
    </Box>
  );
}
