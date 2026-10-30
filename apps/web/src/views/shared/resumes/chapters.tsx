/** 解析八段展示 + 修改稿 diff 对比（简历解析段 / 优化记录段共用）。 */

import { Box, Typography } from "@mui/material";

import { Pill } from "../../../components";
import { fontFamilyMono, typeScale } from "../../../theme/tokens";
import type { DiffBlock, ParsedData } from "./types";

/** 段落小标题（解析结果 / 对比面板统一用它分段）。 */
export function SecTitle({ children }: { children: React.ReactNode }) {
  return (
    <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1 }}>
      {children}
    </Typography>
  );
}

/** 经历条目：时间段 + 标题/副标题 + 亮点列表（工作/项目/教育共用）。 */
function EntryRow({ when, title, sub, highlights }: {
  when?: string;
  title: string;
  sub?: string;
  highlights?: string[];
}) {
  return (
    <Box sx={{ display: "flex", gap: 2, py: 1, borderTop: "1px solid", borderColor: "divider" }}>
      <Typography sx={{ ...typeScale.xs, color: "text.secondary", width: 96, flex: "0 0 auto", fontFamily: fontFamilyMono, pt: 0.25 }}>
        {when || "—"}
      </Typography>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Box sx={{ display: "flex", gap: 1, alignItems: "baseline", flexWrap: "wrap" }}>
          <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>{title}</Typography>
          {sub ? (
            <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>{sub}</Typography>
          ) : null}
        </Box>
        {highlights && highlights.length > 0 && (
          <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2, display: "flex", flexDirection: "column", gap: 0.25 }}>
            {highlights.map((h, i) => (
              <Typography component="li" key={i} sx={{ ...typeScale.s, color: "text.primary" }}>{h}</Typography>
            ))}
          </Box>
        )}
      </Box>
    </Box>
  );
}

/** 解析结果按模板八段平铺展示（旧页的章节目录 + 滚动联动不迁，见文件头取舍）。 */
export function ParsedChapters({ data }: { data: ParsedData }) {
  const basic = data.basic ?? {};
  const intent = data.intent ?? {};
  const contact = [basic.phone, basic.email, basic.city].filter(Boolean).join(" · ");
  const work = data.work ?? [];
  const project = data.project ?? [];
  const education = data.education ?? [];
  const skills = data.skills ?? [];

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3, mt: 2 }}>
      <Box>
        <SecTitle>基本信息</SecTitle>
        <Typography sx={{ ...typeScale.l, fontWeight: 650 }}>{basic.name || "未识别到姓名"}</Typography>
        {contact ? (
          <Typography sx={{ ...typeScale.bodySm, color: "text.secondary", fontFamily: fontFamilyMono }}>{contact}</Typography>
        ) : null}
      </Box>

      <Box>
        <SecTitle>求职意向</SecTitle>
        <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
          {intent.position ? <Pill tone="accent">{intent.position}</Pill> : null}
          {intent.city ? <Pill tone="static">{intent.city}</Pill> : null}
          {intent.salary ? <Pill tone="static">{intent.salary}</Pill> : null}
          {!intent.position && !intent.city && !intent.salary ? (
            <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
              没有期望岗位 / 城市 / 薪资——去「查看原文」看看是不是没写在简历里
            </Typography>
          ) : null}
        </Box>
      </Box>

      {work.length > 0 && (
        <Box>
          <SecTitle>工作经历</SecTitle>
          {work.map((w, i) => (
            <EntryRow key={i} when={w.period} title={w.company || "—"} sub={w.title} highlights={w.highlights} />
          ))}
        </Box>
      )}

      {project.length > 0 && (
        <Box>
          <SecTitle>项目经历</SecTitle>
          {project.map((p, i) => (
            <EntryRow key={i} when={p.period} title={p.name || "—"} sub={p.role} highlights={p.highlights} />
          ))}
        </Box>
      )}

      {education.length > 0 && (
        <Box>
          <SecTitle>教育经历</SecTitle>
          {education.map((e, i) => (
            <EntryRow
              key={i}
              when={e.period}
              title={e.school || "—"}
              sub={[e.major, e.degree].filter(Boolean).join(" · ")}
            />
          ))}
        </Box>
      )}

      {skills.length > 0 && (
        <Box>
          <SecTitle>技能标签</SecTitle>
          <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
            {skills.map((s, i) => <Pill key={i} tone="static">{s}</Pill>)}
          </Box>
        </Box>
      )}

      {data.self_evaluation ? (
        <Box>
          <SecTitle>自我评价</SecTitle>
          <Typography sx={{ ...typeScale.body, whiteSpace: "pre-wrap" }}>{data.self_evaluation}</Typography>
        </Box>
      ) : null}

      {data.summary ? (
        <Box>
          <SecTitle>摘要（匹配页读这段）</SecTitle>
          <Typography sx={{ ...typeScale.body, whiteSpace: "pre-wrap" }}>{data.summary}</Typography>
        </Box>
      ) : null}
    </Box>
  );
}

/**
 * diff 行块 → 对比视图（markdown diff：只标增删改，等同行也留着当上下文）。
 * 标记沿用 legacy `diffHtml`：`−` 删除 / `+` 新增 / 空白 等同。
 */
export function DiffView({ blocks }: { blocks: DiffBlock[] }) {
  if (!blocks.length) {
    return <Typography sx={{ ...typeScale.bodySm, color: "text.secondary" }}>无对比内容</Typography>;
  }
  const rows: { mark: string; text: string; color: string }[] = [];
  for (const b of blocks) {
    const left = b.left ?? "";
    const right = b.right ?? "";
    if (b.tag === "equal") {
      rows.push({ mark: " ", text: left, color: "text.secondary" });
    } else if (b.tag === "insert") {
      rows.push({ mark: "+", text: right, color: "success.main" });
    } else if (b.tag === "delete") {
      rows.push({ mark: "−", text: left, color: "error.main" });
    } else {
      // replace：先删后增
      if (left) rows.push({ mark: "−", text: left, color: "error.main" });
      if (right) rows.push({ mark: "+", text: right, color: "success.main" });
    }
  }
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}>
      {rows.filter((r) => r.text !== "").map((r, i) => (
        <Box key={i} sx={{ display: "flex", gap: 1, alignItems: "flex-start" }}>
          <Box component="span" sx={{ width: 14, flex: "0 0 auto", fontFamily: fontFamilyMono, ...typeScale.s, color: r.color }}>
            {r.mark}
          </Box>
          <Box
            component="pre"
            sx={{
              m: 0, flex: 1, minWidth: 0, whiteSpace: "pre-wrap", wordBreak: "break-word",
              fontFamily: fontFamilyMono, ...typeScale.s, color: r.color,
            }}
          >
            {r.text}
          </Box>
        </Box>
      ))}
    </Box>
  );
}
