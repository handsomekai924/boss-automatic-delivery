/**
 * 一条匹配结果卡：分数圆环 + 职位/公司 + 匹配理由 + 招呼语（可编辑/重打草稿/单条重跑）。
 *
 * 旧版这里是「卡片 + 详情弹窗」两层；招呼语本来就在卡上改最顺手，这轮把详情收成卡内展开，
 * 少一次弹窗跳转。重跑语义是「只改不增」——`matchApi.rematch` 原地替换这一条，列表不长行。
 *
 * 性能（缺陷「展开收起卡顿」）：`memo` 包一层，回调 props 一律「带 jid 的稳定引用」（id 由本卡
 * 自己传出去），父层列表状态变化时只有 props 真变了的卡才重渲染。展开区不做高度动画——
 * height 逐帧整页 reflow 是卡顿根因，改成即时布局 + 仅 opacity 短淡入，收起即卸（点了立刻变）。
 */

import { memo } from "react";
import { Box, Typography } from "@mui/material";
import { motion } from "motion/react";

import { Button, Pill, ScoreRing, Surface, TextField } from "../../../components";
import { typeScale } from "../../../theme/tokens";
import { GREETING_MAX, type MatchItem } from "./types";

/** 技能标签行（匹配 / 欠缺）。 */
function SkillLine({ label, values, tone }: { label: string; values: string[]; tone: "ok" | "warn" }) {
  if (!values?.length) return null;
  return (
    <Box sx={{ display: "flex", gap: 0.75, alignItems: "baseline", flexWrap: "wrap" }}>
      <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{label}</Typography>
      {values.map((v) => (
        <Pill key={v} tone={tone}>{v}</Pill>
      ))}
    </Box>
  );
}

/** 优点 / 风险列表行。 */
function ListLine({ label, values }: { label: string; values: string[] }) {
  if (!values?.length) return null;
  return (
    <Box>
      <Typography sx={{ ...typeScale.xs, color: "text.secondary" }}>{label}</Typography>
      {values.map((v) => (
        <Typography key={v} sx={{ ...typeScale.s }}>· {v}</Typography>
      ))}
    </Box>
  );
}

export const MatchCard = memo(function MatchCard({
  item, draft, busy, expanded,
  onDraft, onToggleDetail, onSave, onRegen, onRematch,
}: {
  item: MatchItem;
  /** 招呼语草稿；undefined = 还没动过，显示库里那份 */
  draft?: string;
  /** 该条是否正在重跑/生成（按钮转圈禁用） */
  busy: boolean;
  expanded: boolean;
  /** 稳定回调（带 id）：组件自己把 `item.encrypt_job_id` 传出去，父层共享同一函数引用 */
  onDraft: (jid: string, text: string) => void;
  onToggleDetail: (jid: string) => void;
  onSave: (jid: string) => void;
  onRegen: (jid: string) => void;
  onRematch: (jid: string) => void;
}) {
  const jid = item.encrypt_job_id;
  const meta = [item.location, item.salary_desc, item.job_experience, item.job_degree]
    .filter(Boolean).join(" · ");
  const dirty = draft !== undefined && draft !== item.greeting;

  return (
    <Surface
      role="group"
      aria-label={`匹配结果 ${item.job_name} ${item.brand_name}`}
      sx={{ mb: 1.5, display: "flex", flexDirection: "column", gap: 1.5 }}
    >
      <Box sx={{ display: "flex", gap: 1.5, alignItems: "flex-start" }}>
        <ScoreRing score={item.match_score ?? 0} />
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ ...typeScale.bodySm, fontWeight: 600 }}>
            {item.job_name} @ {item.brand_name}
          </Typography>
          {meta && (
            <Typography sx={{ ...typeScale.s, color: "text.secondary", mt: 0.25 }}>{meta}</Typography>
          )}
          {item.verdict && (
            <Typography sx={{ ...typeScale.s, mt: 0.5 }}>{item.verdict}</Typography>
          )}
          {item.advice && (
            <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>建议：{item.advice}</Typography>
          )}
          {(item.company_blacklisted || item.jd_blacklisted) && (
            <Box sx={{ display: "flex", gap: 0.5, mt: 0.5, flexWrap: "wrap" }}>
              {item.company_blacklisted && <Pill tone="danger">公司黑名单</Pill>}
              {item.jd_blacklisted && <Pill tone="danger">JD 黑名单</Pill>}
              {item.jd_rule_checked === false && <Pill tone="warn">JD 规则未检查</Pill>}
            </Box>
          )}
        </Box>
      </Box>

      <Box>
        <TextField
          label="招呼语"
          multiline
          rows={2}
          fullWidth
          inputProps={{ maxLength: GREETING_MAX }}
          value={draft ?? item.greeting ?? ""}
          onChange={(e) => onDraft(jid, e.target.value)}
          placeholder="给 HR 的开场白，可自己改"
        />
        {dirty && (
          <Typography sx={{ ...typeScale.xs, color: "warning.main", mt: 0.5 }}>
            草稿未保存，点「保存招呼语」才落库
          </Typography>
        )}
      </Box>

      <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap", alignItems: "center" }}>
        <Button size="small" variant="filled" onClick={() => onSave(jid)} disabled={busy}>保存招呼语</Button>
        <Button size="small" variant="gray" onClick={() => onRegen(jid)} disabled={busy}>
          {busy ? "生成中…" : "重新生成"}
        </Button>
        <Button size="small" variant="gray" onClick={() => onRematch(jid)} disabled={busy}>重跑</Button>
        <Button size="small" variant="plain" onClick={() => onToggleDetail(jid)}>
          {expanded ? "收起详情" : "匹配详情"}
        </Button>
      </Box>

      {/* 展开区：即时布局变化 + 仅 opacity 短淡入（缺陷「展开收起卡顿」）——
          height 动画每帧整页 reflow 是卡顿根因，已去掉；收起直接卸载，点了立刻变。 */}
      {expanded && (
        <motion.div
          key="detail"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.15, ease: "easeOut" }}
        >
          <Surface sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
            <SkillLine label="匹配技能" values={item.matched_skills} tone="ok" />
            <SkillLine label="欠缺技能" values={item.missing_skills} tone="warn" />
            <ListLine label="优势" values={item.pros} />
            <ListLine label="风险" values={item.cons} />
            {item.error && (
              <Typography sx={{ ...typeScale.s, color: "error.main" }}>错误：{item.error}</Typography>
            )}
          </Surface>
        </motion.div>
      )}
    </Surface>
  );
});
