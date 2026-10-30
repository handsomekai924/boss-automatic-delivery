/**
 * 路由分类：共用 vs 平台作用域（docs 3.2）。
 *
 * 判定只认描述符驱动的导航表（`PLATFORM_NAV` + `nav_extras` / `SHARED_NAV`），
 * 视图里不出现 `if (site === "boss")`。
 */

import { PLATFORM_NAV, SHARED_NAV } from "../platform/types";
import { siteIdFromPath } from "../platform/registry";
import type { PlatformDescriptor } from "../platform/types";

/** 共用路由 key（`overview` / `resumes` / …）；平台作用域返回 `null`。 */
export function sharedKeyFromPath(
  pathname: string,
  list: readonly PlatformDescriptor[],
): string | null {
  if (siteIdFromPath(pathname, list)) return null;
  const seg = "/" + (pathname.split("/").filter(Boolean)[0] ?? "");
  return SHARED_NAV.find((n) => n.route === seg)?.key ?? null;
}

/**
 * 平台作用域路由 key（`login` / `jobs` / … / `nav_extras` 里的私有 key）；
 * 共用路由返回 `null`。
 */
export function platformKeyFromPath(
  pathname: string,
  list: readonly PlatformDescriptor[],
): string | null {
  const siteId = siteIdFromPath(pathname, list);
  if (!siteId) return null;
  const platform = list.find((p) => p.site_id === siteId);
  const seg = pathname.split("/").filter(Boolean)[1] ?? "";
  const fixed = PLATFORM_NAV.find((n) => n.route === seg);
  if (fixed) return fixed.key;
  const extra = platform?.nav_extras.find((n) => n.route === seg);
  return extra ? extra.key : null;
}

/** 由路由 key 反查路由段（切平台时原地换站点用）；没有平台就没有可拼的路径。 */
export function routeSegmentForKey(
  key: string,
  platform: PlatformDescriptor | null,
): string | null {
  if (!platform) return null;
  const fixed = PLATFORM_NAV.find((n) => n.key === key);
  if (fixed) return fixed.route;
  const extra = platform.nav_extras.find((n) => n.key === key);
  return extra ? extra.route : null;
}
