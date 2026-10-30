/**
 * 顶栏右侧全局状态：任务角标 + 会话信号（docs 3.1 顶栏右槽）。
 *
 * 数据来自 WS store，不自己发请求；点角标进任务中心，点信号进总览。
 */

import { useNavigate } from "react-router-dom";
import { Box, Typography } from "@mui/material";

import { useSessionStore } from "../../stores/sessionStore";
import { useRunningTasks } from "../../stores/taskStore";
import { usePlatform } from "../../platform/PlatformProvider";
import { typeScale } from "../../theme/tokens";
import { StatusDot } from "../data/StatusDot";

/** 任务角标：在跑条数；0 条时不渲染数字，只留入口。 */
export function TaskBadge() {
  const navigate = useNavigate();
  const running = useRunningTasks();

  return (
    <Box
      component="button"
      type="button"
      className="apple-press"
      aria-label={`任务中心，${running.length} 条在跑`}
      onClick={() => navigate("/tasks")}
      sx={{
        border: 0, cursor: "pointer", borderRadius: "999px",
        px: 1.5, py: 0.75, ...typeScale.s, fontWeight: 590,
        bgcolor: running.length ? "var(--accent)" : "transparent",
        color: running.length ? "#fff" : "text.secondary",
        display: "inline-flex", alignItems: "center", gap: 0.75,
      }}
    >
      <span aria-hidden="true">◎</span>
      {running.length > 0 ? running.length : "任务"}
    </Box>
  );
}

/** 会话信号：全部平台的登录/忙碌一眼看得出；点进总览看分卡。 */
export function SessionSignal() {
  const navigate = useNavigate();
  const { platforms } = usePlatform();
  const sessions = useSessionStore((s) => s.sessions);

  const busy = platforms.some((p) => (sessions[p.site_id] ?? p.session).busy);
  const loggedIn = platforms.filter((p) => (sessions[p.site_id] ?? p.session).logged_in).length;
  const tone = busy ? "busy" : loggedIn > 0 ? "ok" : "idle";
  const label = busy ? "有任务在登录" : loggedIn > 0 ? `${loggedIn} 个平台已登录` : "没有平台登录";

  return (
    <Box
      component="button"
      type="button"
      className="apple-press"
      aria-label={`会话：${label}`}
      onClick={() => navigate("/overview")}
      sx={{
        border: 0, cursor: "pointer", borderRadius: "999px",
        px: 1.5, py: 0.75, background: "transparent",
        display: "inline-flex", alignItems: "center", gap: 1,
      }}
    >
      <StatusDot tone={tone} />
      <Typography sx={{ ...typeScale.s, color: "text.secondary", whiteSpace: "nowrap" }}>
        {label}
      </Typography>
    </Box>
  );
}
