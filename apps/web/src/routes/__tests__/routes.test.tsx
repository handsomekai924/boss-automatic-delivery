import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../theme/ThemeModeProvider";
import { ToastProvider } from "../../components/feedback/Toast";
import { PlatformProvider } from "../../platform/PlatformProvider";
import { AppRoutes, legacyTarget } from "../index";

/** 两个站点、能力有差异：用来验「描述符驱动 + caps 降级」。 */
const PLATFORMS = {
  platforms: [
    {
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
      // expect 是真实描述符形状（求职期望流）；talent 用来验未注册私有入口的兜底
      nav_extras: [
        { key: "expect", label: "求职期望", route: "expect" },
        { key: "talent", label: "人才库", route: "talent" },
      ],
    },
    {
      site_id: "51job",
      display_name: "前程无忧",
      icon: "51job",
      caps: {
        auth: { sms: true, slider: false, qr: true },
        source: { expect_flow: false, tags: false, skills: false },
        catalog: { unsupported: ["scale", "stage"] },
        delivery: { apply_gate: "after_greeting", apply_channel: "apply" },
      },
      session: { logged_in: false, phone_masked: "", busy: false },
      nav_extras: [],
    },
  ],
};

function renderAt(path: string) {
  return render(
    <ThemeModeProvider>
      <ToastProvider>
        <MemoryRouter initialEntries={[path]}>
          <PlatformProvider>
            <AppRoutes />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

/** 页面大标题（顶栏导航项与侧栏项同名时靠 role 区分）。 */
function heading(name: string) {
  return screen.findByRole("heading", { level: 1, name });
}

beforeEach(() => {
  // 上下文平台持久化在 localStorage；清掉避免上一个用例串味（jsdom 没给就跳过）。
  globalThis.localStorage?.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(PLATFORMS), { status: 200 })),
  );
});

describe("两类布局分支", () => {
  it("共用路由不出平台侧栏，内容区撑满", async () => {
    renderAt("/overview");
    await heading("总览");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("平台作用域路由出侧栏，条目 = 固定平台路由 + nav_extras（并入采集条件的除外）", async () => {
    renderAt("/boss/jobs");
    expect(await screen.findByRole("navigation", { name: "BOSS直聘 导航" })).toBeInTheDocument();
    await heading("找工作");
    for (const label of ["登录", "找工作", "投递", "人才库"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    // 求职期望已并入「采集条件」（提议清单 1.2），不再单开侧栏入口
    expect(screen.queryByRole("button", { name: "求职期望" })).not.toBeInTheDocument();
  });

  it("求职期望旧链接落回采集条件所在处", async () => {
    renderAt("/boss/expect");
    await heading("找工作");
  });

  it("caps 降级：51job 没有 nav_extras 就不出私有入口", async () => {
    renderAt("/51job/jobs");
    expect(await screen.findByRole("navigation", { name: "前程无忧 导航" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "人才库" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "求职期望" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "找工作" })).toBeInTheDocument();
  });

  it("站点不在描述符里给回头路，不白屏", async () => {
    renderAt("/notasite/jobs");
    await screen.findByText("没这个平台");
    expect(screen.getByText(/notasite/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "回到总览" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});

describe("旧路由重定向表", () => {
  it.each([
    ["/", "/overview"],
    ["/login", "/boss/login"],
    ["/jobs", "/boss/jobs"],
    ["/resume", "/resumes"],
    ["/llm", "/settings"],
  ])("%s → %s", (from, to) => {
    expect(legacyTarget(from)).toBe(to);
  });

  it("旧 hash 落到新路由并渲染对应页面", async () => {
    renderAt("/resume");
    await heading("简历");
    // 已迁进新壳的真页面（一页三面），不是「还在旧壳里跑」占位
    expect(screen.getByRole("radio", { name: "简历库" })).toBeInTheDocument();
  });

  it("无 siteId 的平台旧路由默认落 boss", async () => {
    renderAt("/login");
    expect(await screen.findByRole("navigation", { name: "BOSS直聘 导航" })).toBeInTheDocument();
    await heading("登录与会话");
  });
});

describe("平台切换器", () => {
  it("平台作用域路由上切平台 = 原地换站点，保留当前页面", async () => {
    renderAt("/boss/jobs");
    expect(await screen.findByRole("navigation", { name: "BOSS直聘 导航" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "前程无忧" }));

    expect(await screen.findByRole("navigation", { name: "前程无忧 导航" })).toBeInTheDocument();
    await heading("找工作");
  });

  it("切到没有当前页面的平台时落回它的第一个入口，不报错", async () => {
    renderAt("/boss/talent");
    await heading("人才库");

    await userEvent.click(screen.getByRole("radio", { name: "前程无忧" }));

    // 51job 没有 talent → 落到登录
    await heading("登录与会话");
  });

  it("共用路由上切平台只换上下文不跳页", async () => {
    renderAt("/overview");
    await heading("总览");

    await userEvent.click(screen.getByRole("radio", { name: "前程无忧" }));

    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "总览" })).toBeInTheDocument());
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});

describe("nav_extras 描述符驱动", () => {
  it("平台私有入口按描述符解析标题，不写死站点名", async () => {
    renderAt("/boss/talent");
    // 标题来自描述符自述的 label，不是硬编码的站点功能名
    await heading("人才库");
    expect(screen.queryByText(/没这个平台/)).not.toBeInTheDocument();
  });
});
