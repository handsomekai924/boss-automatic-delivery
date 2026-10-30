/**
 * 优化记录：修改稿归档列表 + 详情侧栏（diff 对比 / 变更点清单 / 下载 / 确认保存）。
 *
 * 「查看修改稿」从「简历优化」跳过来时直接开对应详情（`openRevisionId`）。
 */

import { useCallback, useEffect, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, ConfirmSheet, EmptyState, InlineAlert, Pill, Select, Sheet, Skeleton, Surface, TextField, useToast,
  type PillTone,
} from "../../../components";
import { radius, typeScale } from "../../../theme/tokens";
import { fmtNumber, fmtTime } from "../../../utils/format";
import { resumeApi } from "../repos";
import { DiffView, SecTitle } from "./chapters";
import {
  CHANGE_TYPE_LABEL, FORMAT_OPTIONS, REV_STATUS_META, REV_PAGE_SIZE, msgOf,
  type RevisionDetail, type RevisionRow,
} from "./types";

export function RecordsSection({ openRevisionId, onOpened }: {
  openRevisionId: string | null;
  onOpened: () => void;
}) {
  const toast = useToast();
  const [items, setItems] = useState<RevisionRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detail, setDetail] = useState<RevisionDetail | null>(null);
  /** 打开详情在途的记录（行内「查看」按钮上「查看中…」防连点） */
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [dlFormat, setDlFormat] = useState<"md" | "txt">("md");
  /** 破坏性操作二次确认（缺陷清单「破坏性操作」）：确认弹窗后才删。 */
  const [removeTarget, setRemoveTarget] = useState<RevisionRow | null>(null);
  const [removing, setRemoving] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveFormat, setSaveFormat] = useState<"md" | "txt">("md");
  const [saveTitle, setSaveTitle] = useState("");
  const [saveFile, setSaveFile] = useState("");
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);

  const loadList = useCallback(async () => {
    setListLoading(true);
    try {
      const r = await resumeApi.revisions(REV_PAGE_SIZE, offset);
      setItems((r.items ?? []) as unknown as RevisionRow[]);
      setTotal(r.total ?? 0);
      setLoadError(null);
    } catch (e) {
      setLoadError(msgOf(e));
    } finally {
      setListLoading(false);
    }
  }, [offset]);

  useEffect(() => { void loadList(); }, [loadList]);

  const openDetail = useCallback(async (id: string) => {
    setOpeningId(id);
    try {
      const d = await resumeApi.revision(id);
      setDetail(d as unknown as RevisionDetail);
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setOpeningId(null);
    }
  }, [toast]);

  // 从「简历优化」的「查看修改稿」跳过来：直接开这份详情
  useEffect(() => {
    if (!openRevisionId) return;
    void openDetail(openRevisionId);
    onOpened();
  }, [openRevisionId, openDetail, onOpened]);

  const openSave = () => {
    if (!detail) return;
    const base = detail.resume_title || "resume";
    setSaveTitle(`${base}${detail.mode === "free" ? "-自由优化" : "-定向"}`);
    setSaveFile(base);
    setSaveFormat("md");
    setSaveOpen(true);
  };

  /** 确认保存：写盘 md/txt + 入库一份新简历（原简历不改）。 */
  const save = async () => {
    if (!detail) return;
    setSaving(true);
    try {
      const r = await resumeApi.saveRevision(detail.revision_id, {
        format: saveFormat,
        filename: saveFile,
        title: saveTitle,
      });
      const res = r as { saved_path?: string; saved_resume_id?: string };
      setDetail({
        ...detail,
        status: "saved",
        saved_path: res.saved_path ?? "",
        saved_resume_id: res.saved_resume_id ?? "",
      });
      toast.toast("已保存为新简历，原简历没有被覆盖", "ok");
      setSaveOpen(false);
      void loadList();
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setSaving(false);
    }
  };

  /** 重新生成：原地覆盖草稿（同一时刻只有一个生成任务，后端 409 会弹）。 */
  const regenerate = async () => {
    if (!detail || regenerating) return;
    setRegenerating(true);
    try {
      await resumeApi.regenerateRevision(detail.revision_id);
      toast.toast("已重新生成，进度到「简历优化」页看", "ok");
      setDetail(null);
      void loadList();
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setRegenerating(false);
    }
  };

  const download = (id: string) => {
    window.open(resumeApi.revisionDownloadUrl(id, dlFormat), "_blank");
  };

  /** 删除记录：先弹窗二次确认（`removeTarget`），确认后才真删；弹窗等删除回包再关。 */
  const remove = (row: RevisionRow) => setRemoveTarget(row);

  const runRemove = async () => {
    const row = removeTarget;
    if (!row || removing) return;
    setRemoving(true);
    try {
      await resumeApi.removeRevision(row.revision_id);
      toast.toast("已删除", "ok");
      if (detail?.revision_id === row.revision_id) setDetail(null);
      setRemoveTarget(null);
      void loadList();
    } catch (e) {
      toast.toast(msgOf(e), "danger");
    } finally {
      setRemoving(false);
    }
  };

  const saved = detail != null && (detail.status === "saved" || Boolean(detail.saved_resume_id));
  const revMeta = detail ? REV_STATUS_META[detail.status] ?? { label: detail.status, tone: "static" as const } : null;

  return (
    <Box>
      {loadError ? <InlineAlert tone="danger">{loadError}</InlineAlert> : null}

      {listLoading ? (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          {Array.from({ length: 3 }, (_, i) => <Skeleton key={i} height={88} />)}
        </Box>
      ) : items.length === 0 ? (
        <EmptyState
          title="还没有优化记录"
          description="到「简历优化」发起一次定向 / 自由优化，生成的修改稿会落在这里等你确认。"
        />
      ) : (
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          {items.map((row) => {
            const meta = REV_STATUS_META[row.status] ?? { label: row.status, tone: "static" as PillTone };
            const name = row.resume_title || row.revision_id;
            return (
              <Surface key={row.revision_id}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, flexWrap: "wrap" }}>
                  <Typography sx={{ ...typeScale.bodySm, fontWeight: 590, flex: 1, minWidth: 0 }} noWrap>
                    {name}
                  </Typography>
                  <Pill tone={meta.tone}>{meta.label}</Pill>
                  <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>
                    {fmtTime(row.created_at)}
                  </Typography>
                </Box>
                <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 0.5 }}>
                  底稿「{row.resume_title || "—"}」 · {row.job_count ?? 0} 个岗位 · {row.model || "—"}
                  {row.mode === "free" ? " · 自由优化" : ""}
                </Typography>
                <Box sx={{ display: "flex", gap: 1, mt: 1.5, flexWrap: "wrap" }}>
                  <Button
                    size="small"
                    variant="gray"
                    disabled={openingId === row.revision_id}
                    aria-label={`查看「${name}」`}
                    onClick={() => void openDetail(row.revision_id)}
                  >
                    {openingId === row.revision_id ? "查看中…" : "查看"}
                  </Button>
                  <Button
                    size="small"
                    variant="tinted"
                    aria-label={`下载「${name}」`}
                    onClick={() => download(row.revision_id)}
                  >
                    下载
                  </Button>
                  <Button
                    size="small"
                    variant="plain"
                    aria-label={`删除记录「${name}」`}
                    onClick={() => remove(row)}
                  >
                    删除
                  </Button>
                </Box>
              </Surface>
            );
          })}
        </Box>
      )}

      <Box sx={{ display: "flex", gap: 1, alignItems: "center", mt: 2, flexWrap: "wrap" }}>
        <Button size="small" variant="gray" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - REV_PAGE_SIZE))}>
          上一页
        </Button>
        <Button size="small" variant="gray" disabled={offset + REV_PAGE_SIZE >= total} onClick={() => setOffset(offset + REV_PAGE_SIZE)}>
          下一页
        </Button>
        <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
          共 {fmtNumber(total)} 条
        </Typography>
      </Box>

      <Sheet
        open={detail != null}
        onClose={() => setDetail(null)}
        width={720}
        title={`修改稿 · ${detail?.resume_title ?? ""}`}
        footer={
          <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
            <Select
              label="导出格式"
              options={FORMAT_OPTIONS}
              value={dlFormat}
              onChange={(v) => setDlFormat(v as "md" | "txt")}
              emptyLabel={null}
              sx={{ minWidth: 130 }}
            />
            <Button variant="gray" onClick={() => detail && download(detail.revision_id)}>下载</Button>
            {!saved && (
              <Button variant="gray" disabled={regenerating} onClick={() => void regenerate()}>
                {regenerating ? "重新生成中…" : "重新生成"}
              </Button>
            )}
            {!saved && <Button variant="filled" onClick={openSave}>确认保存</Button>}
            {saved ? <Pill tone="ok">已保存</Pill> : null}
            <Button variant="danger" onClick={() => detail && remove(detail)}>删除</Button>
          </Box>
        }
      >
        {detail ? (
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
            <Box sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}>
              {revMeta ? <Pill tone={revMeta.tone}>{revMeta.label}</Pill> : null}
              <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
                底稿「{detail.resume_title || "—"}」 · {detail.job_count ?? 0} 个岗位 · {detail.model || "—"} · {fmtTime(detail.created_at)}
              </Typography>
            </Box>

            <Box>
              <SecTitle>原文 → 修改稿（{detail.diff?.length ?? 0} 段对齐）</SecTitle>
              <Box sx={{ maxHeight: 320, overflowY: "auto", p: 1.5, bgcolor: "surface2", borderRadius: radius.s }}>
                <DiffView blocks={detail.diff ?? []} />
              </Box>
            </Box>

            <Box>
              <SecTitle>变更点清单（{detail.changes?.length ?? 0}）</SecTitle>
              {detail.changes && detail.changes.length > 0 ? (
                <Box sx={{ display: "flex", flexDirection: "column", gap: 1.25 }}>
                  {detail.changes.map((c, i) => (
                    // 每条三行定死（缺陷清单「详情」）：第 1 行章节 + 类型同行，
                    // 第 2 行原因、第 3 行摘要各占一行——混在一行时长原因会把摘要挤没
                    <Box key={i} sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}>
                      <Box sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}>
                        <Pill tone="static">{c.section || "—"}</Pill>
                        <Pill tone="accent">{CHANGE_TYPE_LABEL[c.type ?? ""] ?? c.type ?? "—"}</Pill>
                      </Box>
                      <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{c.reason || ""}</Typography>
                      <Typography sx={{ ...typeScale.bodySm }}>{c.summary || ""}</Typography>
                    </Box>
                  ))}
                </Box>
              ) : (
                <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>无变更点说明</Typography>
              )}
            </Box>
          </Box>
        ) : null}
      </Sheet>

      <Sheet
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        anchor="center"
        width={420}
        title="确认保存修改稿"
        footer={
          <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
            <Button variant="gray" disabled={saving} onClick={() => setSaveOpen(false)}>取消</Button>
            <Button variant="filled" disabled={saving} onClick={() => void save()}>
              {saving ? "保存中…" : "保存"}
            </Button>
          </Box>
        }
      >
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
            写盘为文件并入库为一份新简历，原简历不改。
          </Typography>
          <TextField label="入库标题" fullWidth value={saveTitle} onChange={(e) => setSaveTitle(e.target.value)} />
          <TextField label="文件名" fullWidth value={saveFile} onChange={(e) => setSaveFile(e.target.value)} />
          <Select
            label="导出格式"
            options={FORMAT_OPTIONS}
            value={saveFormat}
            onChange={(v) => setSaveFormat(v as "md" | "txt")}
            emptyLabel={null}
          />
        </Box>
      </Sheet>

      <ConfirmSheet
        open={removeTarget != null}
        title="删除这条修改记录"
        description="已写盘的文件会保留，删除后不可恢复。"
        confirmText={removing ? "删除中…" : "确认删除"}
        danger
        busy={removing}
        onClose={() => setRemoveTarget(null)}
        onConfirm={() => void runRemove()}
      />
    </Box>
  );
}
