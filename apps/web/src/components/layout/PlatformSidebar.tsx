import { Box, Typography } from "@mui/material";
import type { SxProps, Theme } from "@mui/material/styles";
import { typeScale } from "../../theme/tokens";
import { mergeSx } from "../../theme/sx";
import type { PlatformDescriptor } from "../../platform/types";
import { PLATFORM_NAV, SHEET_MERGED_NAV } from "../../platform/types";
import { StatusDot } from "../data/StatusDot";

export interface PlatformSidebarProps {
  platform: PlatformDescriptor;
  /** 当前平台作用域路由 key */
  activeKey: string;
  onSelect: (key: string) => void;
  sx?: SxProps<Theme>;
}

/** 平台作用域侧栏（仅 `/:siteId/*` 路由展示；共用路由不渲染它）。 */
export function PlatformSidebar({ platform, activeKey, onSelect, sx }: PlatformSidebarProps) {
  const items = [
    ...PLATFORM_NAV.map((n) => ({ key: n.key, label: n.label })),
    // 已并入「采集条件」的私有入口（如求职期望）不再单开一栏
    ...platform.nav_extras.filter((n) => !SHEET_MERGED_NAV.has(n.key)).map((n) => ({ key: n.key, label: n.label })),
  ];

  return (
    <Box
      component="nav"
      aria-label={`${platform.display_name} 导航`}
      sx={mergeSx(
        {
          width: 200,
          flex: "0 0 auto",
          display: "flex",
          flexDirection: "column",
          gap: 0.5,
          p: 1.5,
        },
        sx,
      )}
    >
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, px: 1, pb: 1.5, flex: "0 0 auto" }}>
        <Typography sx={{ ...typeScale.s, color: "text.secondary" }}>{platform.display_name}</Typography>
        <StatusDot
          tone={platform.session.busy ? "busy" : platform.session.logged_in ? "ok" : "idle"}
          label={platform.session.busy ? "忙碌" : platform.session.logged_in ? "已登录" : "未登录"}
        />
      </Box>

      {/* 矮窗口时导航自己滚，侧栏整体不塌（整页固定 + 内部滚动） */}
      <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        {items.map((item) => {
          const active = item.key === activeKey;
          return (
            <Box
              key={item.key}
              component="button"
              type="button"
              onClick={() => onSelect(item.key)}
              className="apple-press"
              sx={{
                display: "block",
                width: "100%",
                border: 0,
                textAlign: "left",
                px: 1.5,
                py: 1,
                mb: 0.5,
                borderRadius: "12px",
                cursor: "pointer",
                ...typeScale.bodySm,
                fontWeight: active ? 590 : 400,
                color: "text.primary",
                bgcolor: active ? "surface2" : "transparent",
                "&:hover": { bgcolor: (t: Theme) => (t.palette.mode === "dark" ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.04)") },
              }}
            >
              {item.label}
            </Box>
          );
        })}
      </Box>
    </Box>
  );
}
