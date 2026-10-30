/**
 * 发对话与投递（平台作用域 `#/:site/deliver`）：门槛两路径 + 配额 + 发送清单。
 *
 * 一次 `POST /deliver/start` 后端把这一站的投递行为跑完；BOSS 上那就是**打招呼**
 * （生产 `deliver_task.py`：串行发招呼语 + 冷却窗 / 每日配额 / 节流）。前端不拆两步调用：
 * `caps.delivery.apply_gate` 只决定这里**怎么解释**，不决定调哪个接口。
 * **门槛是解释性文案，不是运行时分支**——`core/flow.run_deliver` 的
 * 「发对话 → 过门槛 → 投简历」模板还没接进生产（真投简历是后续能力），
 * 见 `docs/console-migration-parity.md` §三第 9 条。
 * 配额/节流来自 `site_settings`（任务快照的 `limits`），到顶后端整批停住并落账。
 *
 * 发送进度不在这画，统一在任务中心看（与 legacy 同款分工）；本页只跟到结束好刷新状态。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Checkbox, Skeleton, Tooltip, Typography } from "@mui/material";
import { useNavigate } from "react-router-dom";

import {
  Button, EmptyState, InlineAlert, PageHeader, Pill, ScoreRing, ScrollArea, Select, Sheet, Surface,
  TextField, pageRootSx, useToast,
} from "../../components";
import { usePlatform } from "../../platform/PlatformProvider";
import { useTaskStore } from "../../stores/taskStore";
import { fmtClock, fmtNumber } from "../../utils/format";
import { radius, typeScale } from "../../theme/tokens";
import { deliverApi, jobsApi, type DeliverStartBody } from "./repos";
import { matchApi } from "../shared/repos";

/** 匹配结果行（`analysis.matches[]` + 出门补的招呼冷却字段）。 */
interface MatchRow {
  encrypt_job_id: string;
  job_name: string;
  brand_name: string;
  location?: string;
  salary_desc?: string;
  /** 职位详情接口才有；匹配行本身不带 JD */
  job_desc?: string | null;
  match_score?: number;
  greeting?: string;
  company_blacklisted?: boolean;
  jd_blacklisted?: boolean;
  jd_rule_checked?: boolean;
  jd_status?: string;
  auto_delivery_blocked?: boolean;
  matched_rule_snapshot?: unknown[];
  last_greeted_at?: string;
  greet_in_cooldown?: boolean;
  greet_cooldown_until?: number | null;
  delivered_at?: string;
  deliver_status?: string;
  deliver_error?: string;
  [k: string]: unknown;
}

interface AnalysisRow {
  analysis_id: string;
  resume_id?: string;
  resume_title?: string;
  created_at?: number;
  job_count?: number;
  top_score?: number;
  status?: string;
  model?: string;
}

interface AnalysisPayload extends AnalysisRow {
  matches?: MatchRow[];
}

/** `deliver.status` 的 `limits`（`site_settings` + 今日账 + 实际生效的条间隔秒）。 */
interface DeliverLimits {
  site?: string;
  /** 实际生效的条间隔（秒），不是设置页那个毫秒值 */
  interval?: number;
  throttle_ms?: number;
  daily_quota?: number;
  max_parallel?: number;
  sent_today?: number;
  /** 今天还能发几条；`null` = 不限（别当 0） */
  quota_left?: number | null;
  /** 门槛过了之后要不要真投简历（false = 只发对话不投） */
  apply_enabled?: boolean;
}

interface DeliverStatus {
  task_id?: string;
  status?: string;
  total?: number;
  done?: number;
  ok?: number;
  failed?: number;
  skipped?: number;
  error?: string | null;
  /** 为什么整批停住（配额用完 / 用户停 / 风控停 / 等确认）；没停就是空串 */
  stopped_reason?: string;
  items?: { encrypt_job_id?: string; status?: string; delivered_at?: string; error?: string }[];
  limits?: DeliverLimits;
}

const OPEN = new Set(["running", "pending", "waiting_human"]);
const DONE = new Set(["done", "error", "cancelled"]);

/**
 * `matchApi` 的载荷是 `Record<string, unknown>`（后端 JSON 没标型），直接 `as`
 * 强转会因结构重叠不过编译——统一从 `unknown` 收口成强类型，视图里按字段用。
 */
function toRows(v: unknown): AnalysisRow[] {
  return Array.isArray(v) ? (v as AnalysisRow[]) : [];
}
function toPayload(v: unknown): AnalysisPayload {
  return (v ?? {}) as AnalysisPayload;
}

/**
 * 按匹配时安全快照判能不能发（legacy `sendRestriction`）。
 * 返回不可发送的原因；空串 = 可发。门槛判定在后端，这只是给人看的预检。
 */
function restriction(m: MatchRow): string {
  const keys = [
    "is_blacklisted", "company_blacklisted", "jd_blacklisted",
    "jd_rule_checked", "jd_status", "auto_delivery_blocked",
  ];
  if (!keys.some((k) => k in m)) return "缺少匹配时安全快照";
  if (m.company_blacklisted) return "公司黑名单";
  if (m.jd_blacklisted) return "JD 黑名单";
  if (m.jd_rule_checked === false) return "JD 规则未检查";
  if (["failed", "pending", "collecting", "unchecked"].includes(m.jd_status || "")) return "JD 状态不完整";
  if (m.auto_delivery_blocked) return "职位状态不完整";
  return "";
}

function cooldownDate(m: MatchRow): string {
  const until = Number(m.greet_cooldown_until ?? 0);
  return until ? fmtClock(until) : "";
}

/** 发送状态完整文案（含不可发送原因与「无招呼语 / 待发」）。 */
function statusLabel(m: MatchRow): string {
  if (m.deliver_status === "failed") return `失败：${m.deliver_error || ""}`;
  if (m.greet_in_cooldown) return `冷却中（${cooldownDate(m)} 后可再发）`;
  if (m.delivered_at) return "已发送";
  if (m.deliver_status === "sending") return "发送中…";
  const r = restriction(m);
  if (r) return `不可发送（${r}）`;
  if (!(m.greeting || "").trim()) return "无招呼语";
  return "待发";
}

/** 门槛两路径的人话（`caps.delivery.apply_gate` 驱动，不写站点名）。 */
function gateText(applyGate: string | undefined, applyEnabled: boolean | undefined): string {
  if (applyEnabled === false) {
    return "站点没开投递（site_settings.apply_enabled=0）：只会发对话，不投简历。";
  }
  if (applyGate === "after_greeting") {
    return "这个平台发完对话即可投简历，不需要等对方回话。";
  }
  return "先发对话建立会话；对方回话（双方对话成立）后才会投简历。没回话的只发对话、不投。";
}

/**
 * 匹配分容器：悬浮出岗位名 / 薪资 / JD（提议「投递 2.1」）。
 * JD 不在匹配行里，第一次悬浮才按需拉详情，拉到就地缓存。
 */
function ScoreHoverCard({ site, row }: { site: string; row: MatchRow }) {
  const [jd, setJd] = useState<string | null>(row.job_desc ?? null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const loadJd = useCallback(() => {
    if (jd != null || loading) return;
    setLoading(true);
    jobsApi
      .get(site, row.encrypt_job_id)
      .then((j) => setJd(typeof j.job_desc === "string" ? j.job_desc : ""))
      .catch(() => setFailed(true))
      .finally(() => setLoading(false));
  }, [jd, loading, site, row.encrypt_job_id]);

  return (
    <Tooltip
      enterDelay={180}
      onOpen={loadJd}
      slotProps={{ tooltip: { sx: { maxWidth: 360 } } }}
      title={
        <Box sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}>
          <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>{row.job_name}</Typography>
          <Typography sx={{ ...typeScale.xs, opacity: 0.75 }}>
            {row.salary_desc || "面议"}
            {row.location ? ` · ${row.location}` : ""}
          </Typography>
          {loading ? (
            <Box sx={{ display: "flex", flexDirection: "column", gap: 0.5, mt: 0.5 }}>
              <Skeleton width="80%" height={12} sx={{ bgcolor: "rgba(255,255,255,0.25)" }} />
              <Skeleton width="60%" height={12} sx={{ bgcolor: "rgba(255,255,255,0.25)" }} />
            </Box>
          ) : failed ? (
            <Typography sx={{ ...typeScale.xs, opacity: 0.75 }}>JD 拉不到，去职位详情里看。</Typography>
          ) : (
            <Typography
              sx={{
                ...typeScale.xs,
                opacity: 0.85,
                maxHeight: 200,
                overflowY: "auto",
                whiteSpace: "pre-wrap",
              }}
            >
              {jd ? jd : "这个职位还没抓到 JD。"}
            </Typography>
          )}
        </Box>
      }
    >
      <Box
        role="img"
        tabIndex={0}
        aria-label={`匹配 ${Number(row.match_score) || 0} 分，悬浮或聚焦查看岗位详情`}
        sx={{
          display: "inline-flex",
          borderRadius: "50%",
          cursor: "default",
          "&:focus-visible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: 2 },
        }}
      >
        <ScoreRing score={Number(row.match_score) || 0} size={40} label="分" />
      </Box>
    </Tooltip>
  );
}

export function DeliverPage() {
  const toast = useToast();
  const navigate = useNavigate();
  const { activeSiteId, activePlatform } = usePlatform();
  const site = activeSiteId;
  const applyGate = activePlatform?.caps.delivery.apply_gate;
  const tasks = useTaskStore((s) => s.tasks);

  const [analyses, setAnalyses] = useState<AnalysisRow[]>([]);
  const [analysisId, setAnalysisId] = useState("");
  const [analysis, setAnalysis] = useState<AnalysisPayload | null>(null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [minScore, setMinScore] = useState(70);
  const [minDraft, setMinDraft] = useState("70");
  const [limits, setLimits] = useState<DeliverLimits | null>(null);
  const [status, setStatus] = useState<DeliverStatus | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pending, setPending] = useState<{ rows: MatchRow[]; mode: "all" | "selected" | "manual"; note: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** 分析下拉 / 匹配清单都是拉回来的内容：拉取窗口用骨架占位，别先甩「还没有分析结果」空态。 */
  const [analysesLoading, setAnalysesLoading] = useState(true);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  /** 停止发送 / 阈值自动保存的在飞旗标（按钮防连点 + 文案反馈）。 */
  const [cancelling, setCancelling] = useState(false);
  const [minSaving, setMinSaving] = useState(false);

  const deliverTaskId = status?.task_id ?? "";
  const deliverTask = deliverTaskId ? tasks[deliverTaskId] ?? null : null;
  const sending = status != null && OPEN.has(status.status ?? "");

  const matches = useMemo(() => analysis?.matches ?? [], [analysis]);

  /** 可发候选：有招呼语、未发过、不在冷却里、安全快照没拦。 */
  const sendable = useCallback(
    (rows: MatchRow[]) =>
      rows.filter((m) => !restriction(m) && !m.greet_in_cooldown && !m.delivered_at && (m.greeting || "").trim()),
    [],
  );

  const allTargets = useMemo(
    () => sendable(matches.filter((m) => (Number(m.match_score) || 0) >= minScore)),
    [matches, minScore, sendable],
  );
  const selTargets = useMemo(
    () => sendable(matches.filter((m) => selected.has(m.encrypt_job_id))),
    [matches, selected, sendable],
  );

  const loadAnalyses = useCallback(async () => {
    setAnalysesLoading(true);
    try {
      const data = await matchApi.analyses();
      const items = toRows(data.items);
      setAnalyses(items);
      setAnalysisId((prev) => prev || items[0]?.analysis_id || "");
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setAnalysesLoading(false);
    }
  }, []);

  const loadAnalysis = useCallback(async (id: string) => {
    if (!id) {
      setAnalysis(null);
      setAnalysisLoading(false);
      return;
    }
    setAnalysisLoading(true);
    try {
      setAnalysis(toPayload(await matchApi.analysis(id)));
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
      setAnalysis(null);
    } finally {
      setAnalysisLoading(false);
    }
  }, [toast]);

  const loadConfigAndStatus = useCallback(async () => {
    if (!site) return;
    try {
      const cfg = await deliverApi.config(site);
      setMinScore(cfg.min_score);
      setMinDraft(String(cfg.min_score));
    } catch { /* 阈值读不到就用默认 70，别挡着发送 */ }
    try {
      const s = await deliverApi.status(site) as DeliverStatus;
      setStatus(s);
      if (s.limits) setLimits(s.limits);
    } catch { /* 无任务时后端回空快照，读不到就当没有 */ }
  }, [site]);

  useEffect(() => {
    void loadAnalyses();
  }, [loadAnalyses]);

  useEffect(() => {
    void loadAnalysis(analysisId);
    setSelected(new Set());
  }, [analysisId, loadAnalysis]);

  useEffect(() => {
    void loadConfigAndStatus();
  }, [loadConfigAndStatus]);

  // WS 里的投递任务一动就刷新状态；收口后重读一次分析（写回 delivered_at）
  useEffect(() => {
    if (!deliverTask) return;
    if (DONE.has(deliverTask.status)) {
      void loadConfigAndStatus();
      void loadAnalysis(analysisId);
    }
  }, [deliverTask, loadConfigAndStatus, loadAnalysis, analysisId]);

  const saveMinScore = useCallback(async (raw: string) => {
    const v = Math.min(100, Math.max(0, parseInt(raw, 10) || 0));
    setMinScore(v);
    setMinDraft(String(v));
    // 阈值失焦自动保存：给个在飞文案（别无声无息），重叠保存也让后端最后一次生效
    setMinSaving(true);
    try {
      await deliverApi.saveConfig(site, { min_score: v });
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setMinSaving(false);
    }
  }, [site, toast]);

  const openConfirm = useCallback((rows: MatchRow[], mode: "all" | "selected" | "manual", note: string) => {
    if (!rows.length) {
      toast.toast(
        mode === "all"
          ? `没有「评分 ≥ ${minScore}、安全状态完整、未在冷却中且有招呼语」的条目`
          : "选中的条目里没有「有招呼语、未发送且不在冷却中」的",
        "warn",
      );
      return;
    }
    setPending({ rows, mode, note });
    setConfirmOpen(true);
  }, [minScore, toast]);

  const startDeliver = useCallback(async () => {
    if (!pending || !analysisId) return;
    setBusy(true);
    try {
      const body: DeliverStartBody = {
        analysis_id: analysisId,
        mode: pending.mode,
        // 受限职位在 selected/manual 下要显式确认；一键发送后端会自动排除
        confirmed: true,
        confirmation_token: pending.mode === "manual" ? `ui-${Date.now()}` : "",
        encrypt_job_ids: pending.rows.map((m) => m.encrypt_job_id),
      };
      const s = await deliverApi.start(site, body) as DeliverStatus;
      setStatus(s);
      if (s.limits) setLimits(s.limits);
      setConfirmOpen(false);
      setPending(null);
      toast.toast(`开始发送 · 待发 ${fmtNumber(s.total ?? 0)} 条，跳过 ${fmtNumber(s.skipped ?? 0)} 条`, "ok");
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setBusy(false);
    }
  }, [pending, analysisId, site, toast]);

  const cancelDeliver = useCallback(async () => {
    if (!deliverTaskId || cancelling) return;
    setCancelling(true);
    try {
      await deliverApi.cancel(site, deliverTaskId);
      toast.toast("已请求停止", "warn");
    } catch (e) {
      toast.toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      setCancelling(false);
    }
  }, [site, deliverTaskId, cancelling, toast]);

  const toggle = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const ranked = useMemo(
    () => [...matches].sort((a, b) => (Number(b.match_score) || 0) - (Number(a.match_score) || 0)),
    [matches],
  );

  const excluded = matches.length - allTargets.length;
  const quotaLeft = limits?.quota_left;
  const quotaText = limits
    ? limits.daily_quota
      ? `今日配额 ${fmtNumber(limits.sent_today ?? 0)} / ${fmtNumber(limits.daily_quota)} · 还剩 ${quotaLeft == null ? "—" : fmtNumber(quotaLeft)}`
      : "每日配额不限"
    : "配额信息读不到时按不限处理";

  return (
    <Box sx={pageRootSx}>
      <PageHeader
        title="发对话与投递"
        subtitle="发出去的是招呼语正文；投不投简历由门槛决定，两路径都在这里说清楚。"
        actions={
          <Box sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}>
            <Pill tone={limits?.apply_enabled === false ? "warn" : "static"}>{quotaText}</Pill>
            {sending ? (
              <Button variant="danger" disabled={cancelling} onClick={() => void cancelDeliver()}>
                {cancelling ? "停止中…" : "停止发送"}
              </Button>
            ) : null}
          </Box>
        }
      />

      {/* 固定区：门槛 / 停发 / 错误横幅（InlineAlert 不吃 sx，包一层定住不随清单滚） */}
      <Box sx={{ flex: "0 0 auto" }}>
        <InlineAlert tone={applyGate === "after_greeting" ? "info" : "warn"}>
          {gateText(applyGate, limits?.apply_enabled)}
        </InlineAlert>

        {/* 没停就是空串，别渲染成「未知原因」 */}
        {status?.stopped_reason ? (
          <InlineAlert tone="warn">发送停住了：{status.stopped_reason}</InlineAlert>
        ) : null}

        {loadError && <InlineAlert tone="danger">{loadError}</InlineAlert>}
      </Box>

      {/* 固定区：控制行 */}
      <Surface sx={{ flex: "0 0 auto", mt: 2, mb: 2.5 }}>
        <Box sx={{ display: "flex", gap: 1.5, flexWrap: "wrap", alignItems: "flex-end" }}>
          <Select
            label="用哪次匹配"
            options={analyses.map((a) => ({
              code: a.analysis_id,
              name: `${a.resume_title || a.resume_id || "分析"} · ${a.job_count ?? 0} 条 · ${fmtClock(a.created_at ?? null)}`,
            }))}
            value={analysisId}
            onChange={setAnalysisId}
            emptyLabel={analyses.length ? "选一次分析" : "还没有分析"}
            sx={{ flex: "1 1 280px", minWidth: 240 }}
          />
          <TextField
            label="评分阈值"
            type="number"
            value={minDraft}
            onChange={(e) => setMinDraft(e.target.value)}
            onBlur={() => void saveMinScore(minDraft)}
            inputProps={{ min: 0, max: 100, step: 1 }}
            sx={{ width: 120 }}
          />
          <Button variant="filled" disabled={sending} onClick={() => openConfirm(allTargets, "all", `筛选条件：评分 ≥ ${minScore}`)}>
            一键发送全部（{allTargets.length}）
          </Button>
          <Button variant="gray" disabled={sending || !selTargets.length} onClick={() => openConfirm(selTargets, "selected", "选中的职位将按匹配时安全快照执行策略")}>
            发送选中（{selTargets.length}）
          </Button>
        </Box>
        <Typography sx={{ ...typeScale.xs, color: "text.secondary", mt: 1 }}>
          每次发送前都会弹窗让你确认 · 30 天内已发过的会跳过 · ≥ 阈值才进「一键发送全部」，改完自动保存
          {minSaving ? " · 保存中…" : ""}
          {excluded > 0 ? ` · 当前清单有 ${excluded} 条不满足发送条件` : ""}
        </Typography>
      </Surface>

      {/* 局部滚动：发送清单内容区（容器头固定，行卡自滚）；骨架 / 空态同占这块 */}
      <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        {(analysesLoading || analysisLoading) && analysis == null ? (
          // 首拉窗口：4 行行卡骨架（约等于发送行高度）；已有数据时刷新走原地替换，不回骨架
          <ScrollArea>
            <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5, mb: 2 }}>
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} height={64} sx={{ borderRadius: radius.s }} />
              ))}
            </Box>
          </ScrollArea>
        ) : !analysisId ? (
          <ScrollArea>
            <EmptyState
              title="还没有分析结果"
              description="先在「匹配」跑一次匹配，结果会出现在这里。"
            />
          </ScrollArea>
        ) : ranked.length === 0 ? (
          <ScrollArea>
            <EmptyState title="这次分析没有匹配结果" description="换一份简历或放宽职位范围再跑一次。" />
          </ScrollArea>
        ) : (
          <Surface sx={{ p: 0, flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
            {/* 容器头部标题固定在卡顶，不随清单滚 */}
            <Box sx={{ px: 2.5, py: 1.5, display: "flex", gap: 1.5, alignItems: "center", flex: "0 0 auto" }}>
              <Typography sx={{ ...typeScale.s, fontWeight: 590, flex: 1, minWidth: 0 }}>
                发送清单
              </Typography>
              <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>
                共 {fmtNumber(ranked.length)} 条
              </Typography>
              {/* 全选跳过冷却中的（复选框也是禁用的），别让人勾了发不出去 */}
              <Button
                size="small"
                variant="plain"
                onClick={() => setSelected(new Set(ranked.filter((m) => m.greet_in_cooldown !== true).map((m) => m.encrypt_job_id)))}
              >
                全选
              </Button>
              <Button size="small" variant="plain" onClick={() => setSelected(new Set())}>清空</Button>
            </Box>
            <ScrollArea sx={{ p: 0 }}>
            {ranked.map((m) => {
              const jid = m.encrypt_job_id;
              const cool = m.greet_in_cooldown === true;
              const r = restriction(m);
              return (
                <Box
                  key={jid}
                  sx={{
                    display: "flex",
                    alignItems: "center",
                    gap: 1.5,
                    px: 2.5,
                    py: 1.25,
                    borderTop: (t) => `1px solid ${t.palette.divider}`,
                  }}
                >
                  <Checkbox
                    checked={selected.has(jid)}
                    disabled={cool}
                    onChange={() => toggle(jid)}
                    inputProps={{ "aria-label": `选择 ${m.job_name}` }}
                  />
                  <ScoreHoverCard site={site} row={m} />
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Typography sx={{ ...typeScale.bodySm, fontWeight: 590, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {m.job_name} <Box component="span" sx={{ color: "text.secondary", fontWeight: 400 }}>@ {m.brand_name}</Box>
                    </Typography>
                    <Typography sx={{ ...typeScale.xs, color: r ? "warning.main" : "text.secondary" }}>
                      {statusLabel(m)}
                    </Typography>
                    {(m.greeting || "").trim() && (
                      <Typography sx={{ ...typeScale.xs, color: "text.secondary", mt: 0.25 }} noWrap>
                        {m.greeting}
                      </Typography>
                    )}
                  </Box>
                  {r && <Pill tone="warn">{r}</Pill>}
                </Box>
              );
            })}
            </ScrollArea>
          </Surface>
        )}
      </Box>

      {/* 固定底：任务中心路标，不随清单滚 */}
      <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 2, flex: "0 0 auto" }}>
        发送进度、失败重试都在任务中心
        <Box
          component="button"
          onClick={() => navigate("/tasks")}
          sx={{
            border: 0, bgcolor: "transparent", color: "primary.main", cursor: "pointer",
            font: "inherit", textDecoration: "underline", ml: 0.5, p: 0,
          }}
        >
          去看
        </Box>
      </Typography>

      <Sheet
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        anchor="center"
        width={640}
        title={pending ? `确认发送 · 共 ${pending.rows.length} 条` : "确认发送"}
        footer={
          <Box sx={{ display: "flex", gap: 1, justifyContent: "flex-end" }}>
            <Button variant="gray" disabled={busy} onClick={() => setConfirmOpen(false)}>取消</Button>
            <Button variant="filled" disabled={busy} onClick={() => void startDeliver()}>
              {busy ? "发送中…" : "确认发送"}
            </Button>
          </Box>
        }
      >
        <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
          <InlineAlert tone="warn">
            将先<strong>建立会话</strong>，再把招呼语正文作为<strong>聊天消息</strong>发给招聘方。
            投不投简历按门槛走：{gateText(applyGate, limits?.apply_enabled)}
          </InlineAlert>
          {pending?.note && (
            <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{pending.note}</Typography>
          )}
          {pending?.rows.map((m) => {
            const r = restriction(m);
            return (
              <Box
                key={m.encrypt_job_id}
                sx={{
                  p: 1.5,
                  borderRadius: radius.s,
                  bgcolor: "surface2",
                  display: "flex",
                  flexDirection: "column",
                  gap: 0.75,
                }}
              >
                <Box sx={{ display: "flex", gap: 1, alignItems: "center", flexWrap: "wrap" }}>
                  <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>{m.job_name}</Typography>
                  <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>@ {m.brand_name}</Typography>
                  <Pill tone="accent">评分 {Number(m.match_score) || 0}</Pill>
                  {r && <Pill tone="warn">{r}</Pill>}
                </Box>
                <Typography sx={{ ...typeScale.xs, whiteSpace: "pre-wrap" }}>
                  {m.greeting || "（无招呼语）"}
                </Typography>
              </Box>
            );
          })}
          {pending && pending.rows.some((m) => restriction(m)) && (
            <InlineAlert tone="warn">
              其中 {pending.rows.filter((m) => restriction(m)).length} 条受限职位。
              {pending.mode === "all"
                ? "一键发送会自动排除它们。"
                : "确认后才会允许发送这些明确选中的职位。"}
            </InlineAlert>
          )}
        </Box>
      </Sheet>
    </Box>
  );
}
