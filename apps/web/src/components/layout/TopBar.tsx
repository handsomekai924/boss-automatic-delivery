import { useState } from "react";
import { Box, MenuItem, Menu, Typography } from "@mui/material";
import { useThemeMode } from "../../theme/ThemeModeProvider";
import { typeScale } from "../../theme/tokens";
import type { PlatformDescriptor } from "../../platform/types";
import { SHARED_NAV } from "../../platform/types";
import { SegmentedControl, type SegmentedOption } from "../controls/SegmentedControl";
import { Button } from "../controls/Button";
import { StatusDot } from "../data/StatusDot";

export interface TopBarProps {
  /** 平台描述符列表（/api/platforms） */
  platforms: PlatformDescriptor[];
  /** 当前平台；null = 处于共用路由 */
  activeSiteId: string | null;
  /** 当前共用路由 key；null = 处于平台作用域 */
  activeSharedKey: string | null;
  onSelectPlatform: (siteId: string) => void;
  onSelectShared: (key: string) => void;
  /** 顶栏右侧额外内容（任务铃等） */
  rightSlot?: React.ReactNode;
}

/** 顶栏：平台切换器 + 真共用路由入口 + 主题开关（玻璃材质见 theme 根样式）。 */
export function TopBar({
  platforms, activeSiteId, activeSharedKey, onSelectPlatform, onSelectShared, rightSlot,
}: TopBarProps) {
  const { mode, toggle } = useThemeMode();
  const [moreAnchor, setMoreAnchor] = useState<HTMLElement | null>(null);

  // ≤3 平台内联分段；>3 收进下拉（拍板 #3）
  const inline = platforms.length > 0 && platforms.length <= 3;
  const platformOptions: SegmentedOption[] = platforms.map((p) => ({
    value: p.site_id,
    label: p.display_name,
    status: p.session.logged_in ? "ok" : "warn",
  }));

  return (
    <Box
      component="header"
      sx={{
        display: "flex",
        alignItems: "center",
        gap: 2,
        px: 2.5,
        height: 56,
        flex: "0 0 auto",
      }}
    >
      <Typography sx={{ ...typeScale.m, fontWeight: 700, letterSpacing: "-0.2px", mr: 1 }}>
        招聘中枢
      </Typography>

      {inline ? (
        <SegmentedControl
          options={platformOptions}
          value={activeSiteId ?? ""}
          onChange={(v) => onSelectPlatform(v)}
          aria-label="平台切换"
        />
      ) : (
        <>
          <Button
            variant="gray"
            size="small"
            onClick={(e: React.MouseEvent<HTMLElement>) => setMoreAnchor(e.currentTarget)}
            aria-haspopup="menu"
          >
            {platforms.find((p) => p.site_id === activeSiteId)?.display_name ?? "选择平台"}
            {" ▾"}
          </Button>
          <Menu
            anchorEl={moreAnchor}
            open={moreAnchor != null}
            onClose={(_event, reason) => {
              // 缺陷清单「下拉框」：鼠标收起不把焦点留在触发器上（蓝框不留），Esc 保留焦点
              if (reason === "escapeKeyDown") moreAnchor?.focus();
              else moreAnchor?.blur();
              setMoreAnchor(null);
            }}
            slotProps={{ root: { disableRestoreFocus: true } }}
          >
            {platforms.map((p) => (
              <MenuItem
                key={p.site_id}
                selected={p.site_id === activeSiteId}
                onClick={() => { onSelectPlatform(p.site_id); moreAnchor?.blur(); setMoreAnchor(null); }}
              >
                <StatusDot tone={p.session.logged_in ? "ok" : "idle"} />
                <Box component="span" sx={{ ml: 1 }}>{p.display_name}</Box>
              </MenuItem>
            ))}
          </Menu>
        </>
      )}

      <Box sx={{ display: "flex", gap: 0.5, ml: 2, overflowX: "auto" }} className="scroll-edges-x">
        {SHARED_NAV.map((item) => {
          const active = activeSharedKey === item.key;
          return (
            <Box
              key={item.key}
              component="button"
              type="button"
              onClick={() => onSelectShared(item.key)}
              className="apple-press"
              sx={{
                border: 0,
                background: active ? "var(--material-thin-bg)" : "transparent",
                color: active ? "text.primary" : "text.secondary",
                ...typeScale.bodySm,
                fontWeight: active ? 590 : 400,
                px: 1.5,
                py: 0.75,
                borderRadius: "999px",
                cursor: "pointer",
                whiteSpace: "nowrap",
              }}
            >
              {item.label}
            </Box>
          );
        })}
      </Box>

      <Box sx={{ ml: "auto", display: "flex", alignItems: "center", gap: 1 }}>
        {rightSlot}
        <Button variant="plain" size="small" onClick={toggle} aria-label="切换主题">
          {mode === "dark" ? "☀︎" : "☾"}
        </Button>
      </Box>
    </Box>
  );
}
