/**
 * 平台描述符注册表：只吃 `GET /api/platforms`，前端不许写死平台清单（docs 5.4）。
 *
 * 切换器、侧边栏、caps 降级全部由描述符生成；新增站点后端注册了，前端一个字不改。
 */

import { api } from "../api/client";
import type { PlatformDescriptor } from "./types";

interface PlatformsResponse {
  platforms: PlatformDescriptor[];
}

/** 拉全量描述符。 */
export async function fetchPlatforms(): Promise<PlatformDescriptor[]> {
  const body = await api.get<PlatformsResponse>("/api/platforms");
  return body.platforms ?? [];
}

/** 按 site_id 取描述符；没这个站点回 `undefined`。 */
export function byId(
  list: readonly PlatformDescriptor[],
  siteId: string | null | undefined,
): PlatformDescriptor | undefined {
  if (!siteId) return undefined;
  return list.find((p) => p.site_id === siteId);
}

/**
 * 从 URL 首段认站点 id：认得出就是平台作用域路由，认不出就是共用路由。
 *
 * 站点清单来自描述符，所以别处的旧书签（`#/boss/jobs`）与未来的新站点同一条判定路径。
 */
export function siteIdFromPath(
  pathname: string,
  list: readonly PlatformDescriptor[],
): string | null {
  const seg = pathname.split("/").filter(Boolean)[0] ?? "";
  return byId(list, seg) ? seg : null;
}
