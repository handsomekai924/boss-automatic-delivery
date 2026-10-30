/**
 * 路由表（docs 3.2）：共用分支不出平台侧栏，平台分支出。
 *
 * 旧 hash 路由在同一张表里重定向，旧书签不失效；
 * 平台私有入口（`nav_extras`）走兜底分支按描述符解析，不写死站点名。
 */

import type { ComponentType } from "react";
import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { usePlatform } from "../platform/PlatformProvider";
import { byId } from "../platform/registry";
import { PLATFORM_NAV, SHEET_MERGED_NAV } from "../platform/types";
import { platformKeyFromPath } from "./match";
import { PlatformLayout, SharedLayout, ShellLayout } from "./layouts";
import { OverviewPage } from "../views/shared/OverviewPage";
import { TasksPage } from "../views/shared/TasksPage";
import { SettingsPage } from "../views/shared/SettingsPage";
import { LoginPage } from "../views/platform/LoginPage";
import { JobsPage } from "../views/platform/JobsPage";
import { DeliverPage } from "../views/platform/DeliverPage";
import { ResumesPage } from "../views/shared/ResumesPage";
import { InterviewPage } from "../views/shared/InterviewPage";
import { MatchPage } from "../views/shared/MatchPage";
import { PendingPage } from "../views/PendingPage";

/**
 * 旧 hash → 新路由（docs 3.2「旧路由兼容」）。
 *
 * 只收会歧义的那些：`/match`、`/interview`、`/tasks`、`/settings` 本来就是共用路由，
 * 不用重定向；`/login`、`/jobs` 无 `siteId`，按拍板落 `boss`。
 */
export const LEGACY_REDIRECTS: readonly [string, string][] = [
  ["/", "/overview"],
  ["/login", "/boss/login"],
  ["/jobs", "/boss/jobs"],
  ["/resume", "/resumes"],
  ["/llm", "/settings"],
];

/** 平台路由 key → 旧壳 hash（`nav_extras` 占位页的「打开旧界面」按钮用）。发对话/投递/期望在旧壳都在「匹配」页里。 */
const PLATFORM_LEGACY: Record<string, string> = {
  login: "/login",
  jobs: "/jobs",
  deliver: "/match",
  expect: "/match",
};

/**
 * 平台私有页注册表：`nav_extras` 的 key → 页面组件。
 *
 * 共享路由表不认识这些名字，入口由描述符自述（成功标准 #3/#10「平台私有功能不该进共享抽象」）；
 * 第二平台要么复用这张表里的条目，要么再加一行——`views/**` 不该因此改。
 * 求职期望（`expect`）已并入「采集条件」Sheet，不再单开页——旧链接落到找工作。
 */
const PLATFORM_EXTRAS: Record<string, ComponentType> = {};

/** 平台作用域兜底：`nav_extras` 等描述符自述的入口按 key 解析标题。 */
function PlatformExtraPage() {
  const { siteId = "" } = useParams();
  const location = useLocation();
  const { platforms } = usePlatform();

  const platform = byId(platforms, siteId);
  const key = platform ? platformKeyFromPath(location.pathname, platforms) : null;
  const extra = platform?.nav_extras.find((n) => n.key === key);
  const title = extra?.label ?? "平台功能";

  // 已并入「采集条件」的入口（如求职期望）：旧链接直接落回功能所在地
  if (key && SHEET_MERGED_NAV.has(key)) {
    return <Navigate to={`/${siteId}/jobs`} replace />;
  }

  const Page = key ? PLATFORM_EXTRAS[key] : undefined;
  if (Page) return <Page />;

  return (
    <PendingPage
      title={title}
      scope={platform?.display_name ?? siteId}
      legacyHash={PLATFORM_LEGACY[key ?? ""] ?? PLATFORM_LEGACY.expect}
    />
  );
}

/** 平台根路径落到第一个固定入口（登录），与切平台的回落一致。 */
function PlatformIndexRedirect() {
  return <Navigate to={PLATFORM_NAV[0].route} replace />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<ShellLayout />}>
        {/* 共用分支：不出侧栏 */}
        <Route element={<SharedLayout />}>
          <Route index element={<Navigate to="/overview" replace />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="resumes" element={<ResumesPage />} />
          <Route path="match" element={<MatchPage />} />
          <Route path="interview" element={<InterviewPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>

        {/* 平台作用域分支：出侧栏 */}
        <Route path=":siteId" element={<PlatformLayout />}>
          <Route index element={<PlatformIndexRedirect />} />
          <Route path="login" element={<LoginPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="deliver" element={<DeliverPage />} />
          <Route path="*" element={<PlatformExtraPage />} />
        </Route>

        {/* 旧路由重定向表：`/` 已由上面的 index 兜掉，其余按表走 */}
        {LEGACY_REDIRECTS.filter(([from]) => from !== "/").map(([from, to]) => (
          <Route key={from} path={from.replace(/^\//, "")} element={<Navigate to={to} replace />} />
        ))}
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Route>
    </Routes>
  );
}

/** 旧 hash 重定向目标（测试用）；不是旧路由回 `null`。 */
export function legacyTarget(path: string): string | null {
  return LEGACY_REDIRECTS.find(([from]) => from === path)?.[1] ?? null;
}
