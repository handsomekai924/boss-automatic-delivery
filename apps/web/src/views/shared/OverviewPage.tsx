/**
 * 总览（共用路由 `#/overview`）：各平台会话状态卡、在跑任务、快捷入口（docs 3.2）。
 *
 * 这页同时是 WS store 的验收面——`hello` 快照、`task.*`、`session.changed`、
 * `auth.need_*` 喂料三态全都在这里看得见，所以它不能只画静态壳。
 */

import { useNavigate } from "react-router-dom";
import { Box, Typography } from "@mui/material";

import {
  Button, EmptyState, InlineAlert, PageHeader, Pill, ProgressBar, ScrollArea, StatusDot, Surface,
  pageRootSx,
} from "../../components";
import { SHARED_NAV, SHEET_MERGED_NAV, type PlatformDescriptor, type PlatformSession } from "../../platform/types";
import { byId } from "../../platform/registry";
import { usePlatform } from "../../platform/PlatformProvider";
import { useFeeds, useSession, type AuthFeed } from "../../stores/sessionStore";
import { useRunningTasks, type TaskSnapshot } from "../../stores/taskStore";
import { taskStateOf, TASK_STATE_META } from "../../utils/taskState";
import { radius, typeScale } from "../../theme/tokens";
import { LibraryDataSection } from "./overview/LibraryDataSection";

const FEED_TEXT: Record<AuthFeed["kind"], string> = {
  code: "需要你输入短信验证码",
  slider: "需要你过一下滑块",
  scan: "需要你扫码确认",
};

function sessionTone(s: PlatformSession): "ok" | "busy" | "idle" {
  if (s.busy) return "busy";
  return s.logged_in ? "ok" : "idle";
}

function sessionText(s: PlatformSession): string {
  if (s.busy) return "忙碌";
  return s.logged_in ? "已登录" : "未登录";
}

/** 平台会话卡：描述符给骨架，store 给实时值（WS 到了就更）。 */
function PlatformCard({ platform, onOpen }: { platform: PlatformDescriptor; onOpen: () => void }) {
  const live = useSession(platform.site_id);
  const session = live ?? platform.session;

  return (
    <Surface hoverable onClick={onOpen} sx={{ flex: "1 1 240px", minWidth: 240 }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 1.5 }}>
        <Typography sx={{ ...typeScale.l, fontWeight: 600 }}>{platform.display_name}</Typography>
        <StatusDot tone={sessionTone(session)} label={sessionText(session)} />
      </Box>
      <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
        {session.phone_masked || "未绑定手机号"}
      </Typography>
    </Surface>
  );
}

/** 任务行：状态机 → 外观（utils/taskState 的映射表）。 */
function TaskRow({ task, onOpen }: { task: TaskSnapshot; onOpen: () => void }) {
  const meta = TASK_STATE_META[taskStateOf(task.status)];
  const pct = task.total > 0 ? Math.round((task.done / task.total) * 100) : 0;

  return (
    <Surface hoverable onClick={onOpen} sx={{ mb: 1.5 }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 1 }}>
        <Typography sx={{ ...typeScale.bodySm, fontWeight: 590, flex: 1, minWidth: 0 }}>
          {task.title || task.task_type}
        </Typography>
        {task.site ? <Pill tone="static">{task.site}</Pill> : null}
        <Pill tone={meta.tone === "ok" ? "ok" : meta.tone === "danger" ? "danger" : meta.tone === "warn" ? "warn" : "accent"}>
          {meta.label}
        </Pill>
      </Box>
      <ProgressBar value={pct} active={meta.active} />
      <Box sx={{ display: "flex", gap: 1.5, mt: 1 }}>
        <Typography sx={{ ...typeScale.s, color: "text.secondary", flex: 1, minWidth: 0 }} noWrap>
          {task.error || task.current || ""}
        </Typography>
        {task.total > 0 && (
          <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>
            {task.done}/{task.total}
          </Typography>
        )}
      </Box>
    </Surface>
  );
}

export function OverviewPage() {
  const navigate = useNavigate();
  const { platforms, loading, error } = usePlatform();
  const feeds = useFeeds();
  const running = useRunningTasks();

  return (
    <Box sx={pageRootSx}>
      <PageHeader
        title="总览"
        subtitle="各平台会话、职位库、在跑任务与常用入口"
      />

      <ScrollArea>
        {error && (
          <InlineAlert tone="danger">平台描述符取不到：{error}</InlineAlert>
        )}

        {feeds.length > 0 && (
          <Box sx={{ mb: 3, display: "flex", flexDirection: "column", gap: 1.5 }}>
            {feeds.map((f) => (
              <InlineAlert key={f.task_id} tone="warn">
                <Box component="span" sx={{ display: "flex", alignItems: "center", gap: 1.5, flexWrap: "wrap" }}>
                  <span>
                    {byId(platforms, f.site)?.display_name ?? f.site}
                    {" · "}
                    {FEED_TEXT[f.kind]}
                    {f.phone_masked ? `（${f.phone_masked}）` : ""}
                  </span>
                  <Button
                    variant="filled"
                    size="small"
                    className="apple-press"
                    onClick={() => navigate(`/${f.site}/login`)}
                  >
                    去处理
                  </Button>
                </Box>
              </InlineAlert>
            ))}
          </Box>
        )}

        <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1.5 }}>
          平台会话
        </Typography>
        {loading ? (
          <Typography sx={{ ...typeScale.bodySm, color: "text.secondary" }}>加载中…</Typography>
        ) : platforms.length === 0 ? (
          <EmptyState title="还没有平台" description="后端注册平台后，这里会列出各站点的会话状态。" />
        ) : (
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, mb: 4 }}>
            {platforms.map((p) => (
              <PlatformCard
                key={p.site_id}
                platform={p}
                onOpen={() => navigate(`/${p.site_id}/login`)}
              />
            ))}
          </Box>
        )}

        <LibraryDataSection siteIds={platforms.map((p) => p.site_id)} />

        <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1.5 }}>
          在跑任务
        </Typography>
        {running.length === 0 ? (
          <EmptyState title="当前没有在跑的任务" description="发起抓取、投递或登录后，进度会实时推到这里。" />
        ) : (
          <Box sx={{ mb: 4 }}>
            {running.map((t) => (
              <TaskRow key={t.task_id} task={t} onOpen={() => navigate("/tasks")} />
            ))}
          </Box>
        )}

        <Typography sx={{ ...typeScale.s, fontWeight: 590, color: "text.secondary", mb: 1.5 }}>
          快捷入口
        </Typography>
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1.5 }}>
          {SHARED_NAV.filter((n) => n.key !== "overview").map((n) => (
            <Surface
              key={n.key}
              interactive
              onClick={() => navigate(n.route)}
              sx={{ px: 2.5, py: 1.5, borderRadius: radius.s }}
            >
              <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>{n.label}</Typography>
            </Surface>
          ))}
          {platforms.map((p) =>
            p.nav_extras
              // 已并入「采集条件」的私有入口（如求职期望）不出快捷入口
              .filter((n) => !SHEET_MERGED_NAV.has(n.key))
              .map((n) => (
              <Surface
                key={`${p.site_id}/${n.key}`}
                interactive
                onClick={() => navigate(`/${p.site_id}/${n.route}`)}
                sx={{ px: 2.5, py: 1.5, borderRadius: radius.s }}
              >
                <Typography sx={{ ...typeScale.bodySm, fontWeight: 590 }}>
                  {p.display_name} · {n.label}
                </Typography>
              </Surface>
            )),
          )}
        </Box>
      </ScrollArea>
    </Box>
  );
}
