/**
 * 平台抽象（前端半边）——与后端 `GET /api/platforms` 同构（docs/multiplatform-console-redesign.md 5.4）。
 * 视图内不出现 `if (site === "boss")`：差异只能来自 caps / nav_extras / 后端站点私有数据形状。
 */

/** 搜索维度 key（后端 SearchCriteria 的键） */
export type CriteriaKey =
  | "keyword" | "city" | "industry" | "experience" | "degree"
  | "salary" | "job_type" | "scale" | "stage";

export interface PlatformCaps {
  auth: {
    sms: boolean;
    slider: boolean;
    qr: boolean;
  };
  source: {
    /** 求职期望流（BOSS 私有） */
    expect_flow: boolean;
    tags: boolean;
    skills: boolean;
  };
  catalog: {
    /** 不支持的搜索维度：UI 置灰不硬塞 */
    unsupported: CriteriaKey[];
  };
  delivery: {
    /** 投递门槛：发对话 → 过门槛 → 投递 */
    apply_gate: "after_mutual_conversation" | "after_greeting";
    apply_channel: string;
  };
}

export interface PlatformSession {
  logged_in: boolean;
  phone_masked?: string;
  busy?: boolean;
}

export interface NavExtra {
  key: string;
  label: string;
  /** 相对路由段（拼 `/:siteId/{route}`） */
  route: string;
}

export interface PlatformDescriptor {
  site_id: string;
  display_name: string;
  icon: string;
  caps: PlatformCaps;
  session: PlatformSession;
  /** 平台私有入口（如 BOSS 求职期望流），注册进 `/:siteId/:route` */
  nav_extras: NavExtra[];
}

/** 平台作用域基础导航（各平台都有；nav_extras 追加私有入口） */
export const PLATFORM_NAV = [
  { key: "login", label: "登录", route: "login" },
  { key: "jobs", label: "找工作", route: "jobs" },
  { key: "deliver", label: "投递", route: "deliver" },
] as const;

/**
 * 已并入「采集条件」Sheet 的私有入口（提议「将求职期望并入采集条件」）。
 * 描述符照旧自述这些 key，但侧栏/快捷入口不再单开——管理面在「找工作 → 采集条件」里。
 */
export const SHEET_MERGED_NAV: ReadonlySet<string> = new Set(["expect"]);

/** 真正共用的路由（顶栏入口；共用路由不展示平台侧栏） */
export const SHARED_NAV = [
  { key: "overview", label: "总览", route: "/overview" },
  { key: "resumes", label: "简历", route: "/resumes" },
  { key: "match", label: "匹配", route: "/match" },
  { key: "interview", label: "面试题", route: "/interview" },
  { key: "tasks", label: "任务", route: "/tasks" },
  { key: "settings", label: "设置", route: "/settings" },
] as const;
