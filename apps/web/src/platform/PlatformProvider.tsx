/**
 * 当前平台上下文：路由参数为准，共用路由回落到持久化的上下文（docs 5.4）。
 *
 * 切换平台 = 导航（平台作用域路由原地换站点），共用路由只换上下文不跳页（docs 3.3）。
 * Context 只做注入不做状态——真状态在 `stores/`，这里负责把两者接上。
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { fetchPlatforms, byId, siteIdFromPath } from "./registry";
import { platformKeyFromPath, sharedKeyFromPath, routeSegmentForKey } from "../routes/match";
import { PLATFORM_NAV, type PlatformDescriptor } from "./types";

const STORAGE_KEY = "console.siteId";

export interface PlatformContextValue {
  /** 全量描述符（`GET /api/platforms`） */
  platforms: PlatformDescriptor[];
  loading: boolean;
  error: string | null;
  /** 当前平台作用域路由的站点 id；共用路由为 `null` */
  routeSiteId: string | null;
  /** 当前共用路由 key；平台作用域为 `null` */
  routeSharedKey: string | null;
  /** 当前平台作用域路由 key；共用路由为 `null` */
  routePlatformKey: string | null;
  /** 切换器认的「当前平台」：平台路由看路由，共用路由看持久化上下文 */
  activeSiteId: string;
  activePlatform: PlatformDescriptor | null;
  /** 切平台：平台路由原地换站点；共用路由只换上下文 */
  selectPlatform: (siteId: string) => void;
  refresh: () => Promise<void>;
}

const PlatformContext = createContext<PlatformContextValue | null>(null);

function readStored(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function store(siteId: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, siteId);
  } catch {
    /* 隐私模式等：上下文只在本次会话有效，不拦主流程 */
  }
}

export function PlatformProvider({ children }: { children: ReactNode }) {
  const [platforms, setPlatforms] = useState<PlatformDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [contextSiteId, setContextSiteId] = useState(readStored);
  const location = useLocation();
  const navigate = useNavigate();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const list = await fetchPlatforms();
      setPlatforms(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const routeSiteId = useMemo(
    () => siteIdFromPath(location.pathname, platforms),
    [location.pathname, platforms],
  );
  const routeSharedKey = useMemo(
    () => sharedKeyFromPath(location.pathname, platforms),
    [location.pathname, platforms],
  );
  const routePlatformKey = useMemo(
    () => platformKeyFromPath(location.pathname, platforms),
    [location.pathname, platforms],
  );

  const activeSiteId = useMemo(() => {
    if (routeSiteId) return routeSiteId;
    if (contextSiteId && byId(platforms, contextSiteId)) return contextSiteId;
    return platforms[0]?.site_id ?? "";
  }, [routeSiteId, contextSiteId, platforms]);

  const selectPlatform = useCallback(
    (siteId: string) => {
      const target = byId(platforms, siteId);
      if (!target) return;
      if (routeSiteId) {
        // 平台作用域路由：原地换站点（#/boss/jobs 切 51job → #/51job/jobs）；
        // 目标站点没有这一项就落回它的第一个入口，不给「点了报错」。
        const seg = routeSegmentForKey(routePlatformKey ?? "", target) ?? PLATFORM_NAV[0].route;
        navigate(`/${siteId}/${seg}`);
        return;
      }
      setContextSiteId(siteId);
      store(siteId);
    },
    [platforms, routeSiteId, routePlatformKey, navigate],
  );

  const value = useMemo<PlatformContextValue>(
    () => ({
      platforms,
      loading,
      error,
      routeSiteId,
      routeSharedKey,
      routePlatformKey,
      activeSiteId,
      activePlatform: byId(platforms, activeSiteId) ?? null,
      selectPlatform,
      refresh,
    }),
    [platforms, loading, error, routeSiteId, routeSharedKey, routePlatformKey,
      activeSiteId, selectPlatform, refresh],
  );

  return <PlatformContext.Provider value={value}>{children}</PlatformContext.Provider>;
}

/** 取平台上下文；不在 Provider 里用会直接抛——漏包一层必须当场炸，不给静默降级。 */
export function usePlatform(): PlatformContextValue {
  const ctx = useContext(PlatformContext);
  if (!ctx) throw new Error("usePlatform 必须在 <PlatformProvider> 里用");
  return ctx;
}
