/**
 * 任务中心（共用路由 `#/tasks`）：全局任务列表（筛选 / 卡片进度 / 停止 / 重试 / 清理）。
 *
 * 数据源以统一 WS store 为主；首屏与 WS 断线时用 `GET /api/tasks` 兜底（legacy 同款 5s 轮询）。
 * 多平台语义：`site` 是**筛选器**不是作用域——这页看得到所有站点的任务，
 * 空选项 = 只看共用任务，与后端 `list_snapshots(site=…)` 同一套语义。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Typography } from "@mui/material";

import {
  Button, EmptyState, InlineAlert, PageHeader, Pill, ScrollArea, Select, Skeleton, Surface, TaskPanel, TextField,
  pageRootSx, useToast,
} from "../../components";
import { usePlatform } from "../../platform/PlatformProvider";
import { useTaskStore, type TaskSnapshot } from "../../stores/taskStore";
import { getStatus } from "../../ws/client";
import { fmtClock, fmtNumber } from "../../utils/format";
import { radius, typeScale } from "../../theme/tokens";
import { tasksApi } from "./repos";
import { toTaskEvents } from "./taskEvents";

/** 类型徽标文案（legacy `TYPE_LABELS`）。 */
const TYPE_LABELS: Record<string, string> = {
  crawl: "爬取",
  desc: "补JD",
  parse: "解析",
  revision: "优化",
  match: "匹配",
  deliver: "发送",
  interview: "面试题",
  login: "登录",
  recheck: "重检",
  update: "更新",
};

/** 还没结束的原始 status（含登录喂料中间态，`taskStateOf` 会把它们归到 running）。 */
const UNFINISHED = new Set([
  "running", "pending", "waiting_human", "sending_sms",
  "need_code", "need_slider", "logging_in", "need_scan",
]);
const FINISHED = new Set(["done", "cancelled", "error"]);

type StatusFilter = "" | "running" | "waiting_human" | "finished";

const STATUS_OPTIONS = [
  { code: "running", name: "在跑" },
  { code: "waiting_human", name: "等待人工" },
  { code: "finished", name: "已结束" },
];

/** 站点筛选：空 = 共用任务，`__all__` = 全部（默认）。 */
const SITE_ALL = "__all__";

function matchesStatus(t: TaskSnapshot, f: StatusFilter): boolean {
  if (!f) return true;
  if (f === "running") return UNFINISHED.has(t.status);
  if (f === "waiting_human") {
    return ["waiting_human", "need_code", "need_slider", "need_scan"].includes(t.status);
  }
  return FINISHED.has(t.status);
}

/** 单张任务卡：状态机外观走 TaskPanel，操作按钮挂在 `extra`。 */
function TaskCard({
  task, siteName, expanded, onToggle, onCancel, onRetry, onDelete, action,
}: {
  task: TaskSnapshot;
  /** 站点显示名（`siteLabel` 的产物）；空串 = 共用任务，不出角标 */
  siteName: string;
  expanded: boolean;
  onToggle: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onDelete: () => void;
  /** 这张卡哪个动作在飞（请求防连点 + 按钮转文案）；null = 都没在飞 */
  action?: "cancel" | "retry" | "delete" | null;
}) {
  const running = UNFINISHED.has(task.status);
  const busy = action ?? null;
  const pct = task.total > 0 ? Math.round((task.done / task.total) * 100) : undefined;
  // 业务快照里的专属计数（如出题任务的「题目」数）跟着卡片走——生成任务段迁任务中心（2026-10-29）
  const raw = (task.raw ?? {}) as { question_total?: number };
  const counts: [string, string][] = [];
  if (task.total > 0) counts.push(["进度", `${fmtNumber(task.done)}/${fmtNumber(task.total)}`]);
  else if (task.done > 0) counts.push(["已处理", fmtNumber(task.done)]);
  if (typeof raw.question_total === "number") counts.push(["题目", fmtNumber(raw.question_total)]);
  if (task.created_at) counts.push(["开始", fmtClock(task.created_at)]);
  if (task.ended_at) counts.push(["结束", fmtClock(task.ended_at)]);

  return (
    <Box sx={{ mb: 1.5 }}>
      <TaskPanel
        title={task.title || task.task_id}
        status={task.status}
        percent={pct}
        current={task.current || undefined}
        counts={counts}
        error={task.error || null}
        siteBadge={siteName ? <Pill tone="static">{siteName}</Pill> : undefined}
        stopLabel={busy === "cancel" ? "取消中…" : "取消"}
        onStop={running ? onCancel : undefined}
        extra={
          <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
            <Pill tone="static">{TYPE_LABELS[task.task_type] || task.task_type}</Pill>
            {!running && task.status === "error" && (
              <Button size="small" variant="gray" disabled={busy !== null} onClick={onRetry}>
                {busy === "retry" ? "重试中…" : "重试"}
              </Button>
            )}
            {!running && (
              <Button size="small" variant="plain" disabled={busy !== null} onClick={onDelete}>
                {busy === "delete" ? "删除中…" : "删除"}
              </Button>
            )}
            <Button size="small" variant="plain" onClick={onToggle}>
              {expanded ? "收起事件" : "事件"}
            </Button>
          </Box>
        }
      />
      {expanded && (
        <Surface sx={{ mt: 0.5, maxHeight: 260, overflowY: "auto" }}>
          {task.events?.length ? (
            <Box sx={{ display: "flex", flexDirection: "column", gap: 0.75 }}>
              {toTaskEvents(task.events, 20).map((ev, i) => (
                <Box key={i} sx={{ display: "flex", gap: 1.5, alignItems: "baseline" }}>
                  <Typography sx={{ ...typeScale.xs, color: "text.secondary", fontVariantNumeric: "tabular-nums" }}>
                    {fmtClock(ev.at ?? null)}
                  </Typography>
                  <Typography
                    sx={{
                      ...typeScale.s,
                      color:
                        ev.kind === "error" ? "error.main"
                          : ev.kind === "ok" ? "success.main"
                          : ev.kind === "warn" ? "warning.main"
                          : "text.primary",
                    }}
                  >
                    {ev.name}
                  </Typography>
                </Box>
              ))}
            </Box>
          ) : (
            <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>暂无事件流水</Typography>
          )}
        </Surface>
      )}
    </Box>
  );
}

export function TasksPage() {
  const toast = useToast();
  const { platforms } = usePlatform();
  const tasks = useTaskStore((s) => s.tasks);
  const replaceAll = useTaskStore((s) => s.replaceAll);
  const remove = useTaskStore((s) => s.remove);

  const [type, setType] = useState("");
  const [status, setStatus] = useState<StatusFilter>("");
  const [site, setSite] = useState(SITE_ALL);
  const [q, setQ] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // 首屏必拉一次：初值 true，别让「还没有任何任务」空态在拉取前闪一下
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** 每张卡在飞的动作（task_id → 动作）：按钮转文案并禁用，防连点重复提交。 */
  const [busyIds, setBusyIds] = useState<Record<string, "cancel" | "retry" | "delete">>({});
  /** 「全部停止」「清理已结束」的在飞旗标。 */
  const [allStopping, setAllStopping] = useState(false);
  const [clearing, setClearing] = useState(false);

  /** HTTP 兜底拉全量（首屏 / WS 断线轮询 / 操作后对账）。 */
  const loadHttp = useCallback(async () => {
    setLoading(true);
    try {
      const data = await tasksApi.list();
      replaceAll(data.items ?? []);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [replaceAll]);

  useEffect(() => {
    void loadHttp();
    // WS 断线才轮询；连着就让 WS 推，省得两边打架
    const timer = setInterval(() => {
      if (getStatus() !== "open") void loadHttp();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadHttp]);

  const all = useMemo(() => Object.values(tasks), [tasks]);
  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((t) => {
        if (type && t.task_type !== type) return false;
        if (site !== SITE_ALL && (t.site ?? "") !== site) return false;
        if (!matchesStatus(t, status)) return false;
        if (needle) {
          const hay = `${t.title} ${t.task_type} ${TYPE_LABELS[t.task_type] ?? ""}`.toLowerCase();
          if (!hay.includes(needle)) return false;
        }
        return true;
      })
      .sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
  }, [all, type, status, site, q]);

  const runningCount = all.filter((t) => UNFINISHED.has(t.status)).length;
  const failedCount = all.filter((t) => t.status === "error").length;

  /** 站点 id → 显示名；角标上打 id（`other`）对人没意义，回落到描述符的名字。 */
  const siteNames = useMemo(
    () => new Map(platforms.map((p) => [p.site_id, p.display_name])),
    [platforms],
  );
  const siteLabel = useCallback(
    (site: string) => (site ? siteNames.get(site) ?? site : ""),
    [siteNames],
  );

  const siteOptions = useMemo(
    () => [
      { code: SITE_ALL, name: "全部站点" },
      { code: "", name: "共用任务" },
      ...platforms.map((p) => ({ code: p.site_id, name: p.display_name })),
    ],
    [platforms],
  );

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const act = async (kind: "cancel" | "retry" | "delete", id: string) => {
    if (busyIds[id]) return; // 同一张卡的动作在飞就不重入
    setBusyIds((prev) => ({ ...prev, [id]: kind }));
    try {
      if (kind === "cancel") {
        await tasksApi.cancel(id);
        toast.toast("已请求停止", "ok");
      } else if (kind === "retry") {
        await tasksApi.retry(id);
        toast.toast("已重新提交", "ok");
      } else {
        await tasksApi.remove(id);
        remove(id);
        toast.toast("已删除", "ok");
        return;
      }
      await loadHttp();
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setBusyIds((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
    }
  };

  const cancelAll = async () => {
    if (allStopping) return;
    setAllStopping(true);
    try {
      const r = await tasksApi.cancelAll();
      toast.toast(`已停止 ${fmtNumber((r as { cancelled?: number }).cancelled ?? 0)} 个任务`, "ok");
      await loadHttp();
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setAllStopping(false);
    }
  };

  const clearFinished = async () => {
    if (clearing) return;
    const finished = all.filter((t) => FINISHED.has(t.status));
    if (!finished.length) {
      toast.toast("没有可清理的已结束任务", "info");
      return;
    }
    setClearing(true);
    try {
      await Promise.all(finished.map((t) => tasksApi.remove(t.task_id)));
      for (const t of finished) remove(t.task_id);
      toast.toast(`已清理 ${finished.length} 条`, "ok");
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
      await loadHttp();
    } finally {
      setClearing(false);
    }
  };

  return (
    <Box sx={pageRootSx}>
      <PageHeader
        title="任务中心"
        subtitle="所有异步任务都在这里：抓取、补 JD、解析、优化、匹配、发送、出题……可以随时停止、重试、清理。"
        actions={
          <Box sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}>
            <Pill tone={failedCount > 0 ? "danger" : "static"}>
              在跑 {runningCount} · 失败 {failedCount}
            </Pill>
            <Button variant="gray" disabled={allStopping} onClick={() => void cancelAll()}>
              {allStopping ? "停止中…" : "全部停止"}
            </Button>
            <Button variant="gray" disabled={clearing} onClick={() => void clearFinished()}>
              {clearing ? "清理中…" : "清理已结束"}
            </Button>
          </Box>
        }
      />

      {loadError && <InlineAlert tone="danger">{loadError}</InlineAlert>}

      <Surface sx={{ flex: "0 0 auto", mb: 2.5, display: "flex", gap: 1.5, flexWrap: "wrap", alignItems: "center" }}>
        <Select
          label="类型"
          options={Object.entries(TYPE_LABELS).map(([code, name]) => ({ code, name }))}
          value={type}
          onChange={setType}
          emptyLabel="全部"
          sx={{ minWidth: 140 }}
        />
        <Select
          label="状态"
          options={STATUS_OPTIONS}
          value={status}
          onChange={(v) => setStatus(v as StatusFilter)}
          emptyLabel="全部"
          sx={{ minWidth: 140 }}
        />
        <Select
          label="站点"
          options={siteOptions}
          value={site}
          onChange={setSite}
          emptyLabel={null}
          sx={{ minWidth: 160 }}
        />
        <TextField
          label="搜索"
          type="search"
          placeholder="按标题 / 类型过滤…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          sx={{ flex: "1 1 220px" }}
        />
      </Surface>

      <ScrollArea>
        {loading && all.length === 0 ? (
          // 首屏拉取窗口：3 行任务卡骨架（约等于 TaskPanel 高度），数据到了直接替换不跳版
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} height={96} sx={{ borderRadius: radius.s }} />
            ))}
          </Box>
        ) : visible.length === 0 ? (
          <EmptyState
            title={all.length ? "没有符合筛选条件的任务" : "还没有任何任务"}
            description={all.length ? "换个筛选条件再看看。" : "发起抓取、投递或登录后，进度会实时推到这里。"}
          />
        ) : (
          <Box>
            {visible.map((t) => (
              <TaskCard
                key={t.task_id}
                task={t}
                siteName={siteLabel(t.site)}
                expanded={expanded.has(t.task_id)}
                action={busyIds[t.task_id] ?? null}
                onToggle={() => toggleExpand(t.task_id)}
                onCancel={() => void act("cancel", t.task_id)}
                onRetry={() => void act("retry", t.task_id)}
                onDelete={() => void act("delete", t.task_id)}
              />
            ))}
          </Box>
        )}

        <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 2 }}>
          共 {visible.length} 条
        </Typography>
      </ScrollArea>
    </Box>
  );
}
