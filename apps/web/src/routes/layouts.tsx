/**
 * 上下 + 左右壳（docs 3.1）：顶栏两分支共享，平台作用域才出侧栏。
 *
 * 共用路由（`SharedLayout`）内容区撑满整行；平台作用域（`PlatformLayout`）
 * 左侧栏 + 右内容。两套布局都挂在 `ShellLayout` 下，顶栏只渲染一次。
 */

import { Outlet, useNavigate, useParams } from "react-router-dom";
import { Box, Typography } from "@mui/material";

import { Button, PageTransition, PlatformSidebar, ScrollArea, SessionSignal, TaskBadge, TopBar, pageRootSx } from "../components";
import { usePlatform } from "../platform/PlatformProvider";
import { byId } from "../platform/registry";
import { SHARED_NAV } from "../platform/types";
import { routeSegmentForKey } from "./match";
import { typeScale } from "../theme/tokens";

/**
 * 整页固定 + 内部滚动：内容区自己不滚（overflow: hidden），滚动只发生在
 * 页面内部的 `ScrollArea`（列表区 / 内容区）。顶栏 + 侧栏 + 页头都不随内容滚走。
 */
const CONTENT_SX = {
  flex: 1,
  minWidth: 0,
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
  px: { xs: 2.5, md: 4 },
  py: 3,
} as const;

/** 顶栏 + 主体横排；切换器/共用入口/全局状态都在这层，下层两分支共用。 */
export function ShellLayout() {
  const navigate = useNavigate();
  const { platforms, activeSiteId, routeSharedKey, selectPlatform } = usePlatform();

  return (
    <Box sx={{ height: "100dvh", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <TopBar
        platforms={platforms}
        activeSiteId={activeSiteId}
        activeSharedKey={routeSharedKey}
        onSelectPlatform={selectPlatform}
        onSelectShared={(key) => {
          const item = SHARED_NAV.find((n) => n.key === key);
          if (item) navigate(item.route);
        }}
        rightSlot={
          <>
            <SessionSignal />
            <TaskBadge />
          </>
        }
      />
      <Box sx={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
        <Outlet />
      </Box>
    </Box>
  );
}

/** 共用路由分支：不渲染平台侧栏，内容区自适应（docs 3.1）。 */
export function SharedLayout() {
  return (
    <Box sx={CONTENT_SX}>
      <PageTransition>
        <Outlet />
      </PageTransition>
    </Box>
  );
}

/** 站点不在描述符里：给回头路，不给白屏。 */
function UnknownSite({ siteId }: { siteId: string }) {
  const navigate = useNavigate();
  return (
    <Box sx={{ ...CONTENT_SX, ...pageRootSx }}>
      <ScrollArea>
        <Typography sx={{ ...typeScale.xl, fontWeight: 700, mb: 1 }}>没这个平台</Typography>
        <Typography sx={{ ...typeScale.bodySm, color: "text.secondary", mb: 2.5 }}>
          路径里的「{siteId}」不是已注册的站点。可能是旧书签，也可能后端没加载这个平台。
        </Typography>
        <Button variant="filled" onClick={() => navigate("/overview")}>回到总览</Button>
      </ScrollArea>
    </Box>
  );
}

/** 平台作用域分支：左侧栏 + 右内容；条目由描述符驱动（`PLATFORM_NAV` + `nav_extras`）。 */
export function PlatformLayout() {
  const navigate = useNavigate();
  const { siteId = "" } = useParams();
  const { platforms, routePlatformKey } = usePlatform();
  const platform = byId(platforms, siteId);

  if (!platform) return <UnknownSite siteId={siteId} />;

  return (
    <>
      <PlatformSidebar
        platform={platform}
        activeKey={routePlatformKey ?? ""}
        onSelect={(key) => {
          // 换页不换站点：#/boss/jobs → #/boss/deliver
          const seg = routeSegmentForKey(key, platform);
          if (seg) navigate(`/${siteId}/${seg}`);
        }}
      />
      <Box sx={CONTENT_SX}>
        <PageTransition>
          <Outlet />
        </PageTransition>
      </Box>
    </>
  );
}
