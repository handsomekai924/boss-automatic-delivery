import { describe, expect, it } from "vitest";

import { byId, siteIdFromPath } from "../registry";
import { platformKeyFromPath, routeSegmentForKey, sharedKeyFromPath } from "../../routes/match";
import type { PlatformDescriptor } from "../types";

const BOSS = {
  site_id: "boss",
  display_name: "BOSS直聘",
  icon: "boss",
  caps: {
    auth: { sms: true, slider: true, qr: false },
    source: { expect_flow: true, tags: true, skills: true },
    catalog: { unsupported: [] },
    delivery: { apply_gate: "after_mutual_conversation", apply_channel: "onekeydeliver" },
  },
  session: { logged_in: true, phone_masked: "138****8000", busy: false },
  nav_extras: [{ key: "expect", label: "求职期望", route: "expect" }],
} as unknown as PlatformDescriptor;

const OTHER = {
  ...BOSS,
  site_id: "51job",
  display_name: "前程无忧",
  nav_extras: [],
} as unknown as PlatformDescriptor;

const LIST = [BOSS, OTHER];

describe("registry", () => {
  it("byId 只认已注册站点", () => {
    expect(byId(LIST, "boss")?.display_name).toBe("BOSS直聘");
    expect(byId(LIST, "51job")).toBeDefined();
    expect(byId(LIST, "notasite")).toBeUndefined();
    expect(byId(LIST, null)).toBeUndefined();
  });

  it("siteIdFromPath：首段是站点 id 才算平台作用域", () => {
    expect(siteIdFromPath("/boss/jobs", LIST)).toBe("boss");
    expect(siteIdFromPath("/51job", LIST)).toBe("51job");
    expect(siteIdFromPath("/overview", LIST)).toBeNull();
    expect(siteIdFromPath("/notasite/jobs", LIST)).toBeNull();
    expect(siteIdFromPath("/", LIST)).toBeNull();
  });
});

describe("routes/match", () => {
  it("共用路由 key 判定", () => {
    expect(sharedKeyFromPath("/overview", LIST)).toBe("overview");
    expect(sharedKeyFromPath("/resumes", LIST)).toBe("resumes");
    expect(sharedKeyFromPath("/settings", LIST)).toBe("settings");
    // 平台作用域不算共用
    expect(sharedKeyFromPath("/boss/jobs", LIST)).toBeNull();
    expect(sharedKeyFromPath("/nonesuch", LIST)).toBeNull();
  });

  it("平台路由 key 判定：固定项 + nav_extras", () => {
    expect(platformKeyFromPath("/boss/login", LIST)).toBe("login");
    expect(platformKeyFromPath("/boss/jobs", LIST)).toBe("jobs");
    expect(platformKeyFromPath("/boss/deliver", LIST)).toBe("deliver");
    // 站点设置板块整体移除（2026-10-29 拍板「没有这个板块」）：/settings 不再是平台路由
    expect(platformKeyFromPath("/boss/settings", LIST)).toBeNull();
    // 描述符自述的私有入口，前端不写死 expect
    expect(platformKeyFromPath("/boss/expect", LIST)).toBe("expect");
    // 51job 没有 expect
    expect(platformKeyFromPath("/51job/expect", LIST)).toBeNull();
    // 共用路由不算平台路由
    expect(platformKeyFromPath("/overview", LIST)).toBeNull();
  });

  it("routeSegmentForKey 反查路由段（切平台原地换站点用）", () => {
    expect(routeSegmentForKey("jobs", BOSS)).toBe("jobs");
    expect(routeSegmentForKey("expect", BOSS)).toBe("expect");
    expect(routeSegmentForKey("expect", OTHER)).toBeNull();
    expect(routeSegmentForKey("nonesuch", BOSS)).toBeNull();
    expect(routeSegmentForKey("jobs", null)).toBeNull();
  });
});
