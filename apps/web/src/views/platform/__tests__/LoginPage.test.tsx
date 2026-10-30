import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../../theme/ThemeModeProvider";
import { ToastProvider } from "../../../components/feedback/Toast";
import { PlatformProvider } from "../../../platform/PlatformProvider";
import { LoginPage } from "../LoginPage";
import { applyWsEvent, resetStores } from "../../../ws/dispatch";
import type { WsEnvelope } from "../../../ws/client";

/** 平台描述符：`caps.auth` 是这页的降级开关，所以两个站点各给一份不同的。 */
function platform(auth: { sms: boolean; slider: boolean; qr: boolean }) {
  return {
    site_id: "boss",
    display_name: "BOSS直聘",
    icon: "boss",
    caps: {
      auth,
      source: { expect_flow: true, tags: true, skills: true },
      catalog: { unsupported: [] },
      delivery: { apply_gate: "after_mutual_conversation", apply_channel: "onekeydeliver" },
    },
    session: { logged_in: false, phone_masked: "", busy: false },
    nav_extras: [],
  };
}

const ALL_CAPS = { sms: true, slider: true, qr: true };
const NO_QR = { sms: true, slider: true, qr: false };

function frame(partial: Partial<WsEnvelope> & { type: string }): WsEnvelope {
  return { ts: 1700000000, task_type: "login", task_id: "", site: "", data: {}, ...partial };
}

function stubFetch(auth: { sms: boolean; slider: boolean; qr: boolean } = ALL_CAPS) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/platforms")) {
      return new Response(JSON.stringify({ platforms: [platform(auth)] }), { status: 200 });
    }
    if (url.includes("/auth/sms") || url.includes("/auth/login") || url.includes("/auth/qr")) {
      return new Response(
        JSON.stringify({ task_id: "t1", status: "need_code", phone_masked: "138****8000" }),
        { status: 200 },
      );
    }
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  });
}

function renderPage() {
  return render(
    <ThemeModeProvider>
      <ToastProvider>
        <MemoryRouter initialEntries={["/boss/login"]}>
          <PlatformProvider>
            <LoginPage />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

beforeEach(() => {
  resetStores();
  globalThis.localStorage?.clear();
  vi.stubGlobal("fetch", stubFetch());
  vi.spyOn(window, "open").mockImplementation(() => null);
});

describe("LoginPage 主动登录", () => {
  it("未登录给会话空态，两种登录方式都能切", async () => {
    renderPage();
    // 空态是一整句，别用精确匹配去找「还没登录」
    expect(await screen.findByText(/还没登录/)).toBeInTheDocument();
    // SegmentedControl 的选项是 role=radio，不是 button
    expect(screen.getByRole("radio", { name: /手机号 \+ 验证码/ })).toBeEnabled();
    // 默认落在短信档，发码按钮在
    expect(screen.getByRole("button", { name: "发送验证码" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: /App 扫码/ }));
    expect(await screen.findByRole("button", { name: "生成二维码" })).toBeInTheDocument();
  });

  it("发码打到站点作用域路径，不是 legacy 别名", async () => {
    renderPage();
    await screen.findByText(/还没登录/);

    await userEvent.type(screen.getByLabelText("手机号"), "13800008000");
    await userEvent.click(screen.getByRole("button", { name: "发送验证码" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/auth/sms",
        expect.objectContaining({ method: "POST" }),
      );
    });
    // 旧别名一个都不该被碰到
    for (const call of (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls) {
      expect(String(call[0])).not.toMatch(/^\/api\/auth\//);
    }
  });

  it("发码请求在飞时按钮转「发送中…」并禁用，防连点重复发", async () => {
    // 用 deferred promise 卡住 /auth/sms，制造「请求在飞」窗口
    let resolveSms!: (r: Response) => void;
    const smsGate = new Promise<Response>((res) => { resolveSms = res; });
    const base = stubFetch();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/auth/sms")) return smsGate;
        return base(input);
      }),
    );
    renderPage();
    await screen.findByText(/还没登录/);

    await userEvent.type(screen.getByLabelText("手机号"), "13800008000");
    await userEvent.click(screen.getByRole("button", { name: "发送验证码" }));
    expect(screen.getByRole("button", { name: "发送中…" })).toBeDisabled();

    resolveSms(new Response(JSON.stringify({ task_id: "t1", status: "need_code" }), { status: 200 }));
    expect(await screen.findByRole("button", { name: "发送验证码" })).toBeEnabled();
  });
});

describe("LoginPage 喂料三态", () => {
  it("need_code 出验证码表单，提交打 /auth/login 且字段叫 code", async () => {
    renderPage();
    applyWsEvent(
      frame({
        type: "auth.need_code",
        task_id: "t9",
        site: "boss",
        data: { task_id: "t9", status: "need_code", phone_masked: "138****8000" },
      }),
    );

    expect(await screen.findByText("需要你输入短信验证码")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("短信验证码"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "提交" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/auth/login",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ task_id: "t9", code: "123456" }),
        }),
      );
    });
  });

  it("need_slider 只给「打开滑块页面」，不在壳里造滑块", async () => {
    renderPage();
    applyWsEvent(
      frame({
        type: "auth.need_slider",
        task_id: "t7",
        site: "boss",
        data: {
          task_id: "t7",
          status: "need_slider",
          phone_masked: "",
          slider: { needed: true, solved: false },
        },
      }),
    );

    expect(await screen.findByText("需要你过一下滑块")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "打开滑块页面" }));
    // 走站点作用域路径（legacy 别名 `/api/auth/slider/…` 会把站点钉死成 boss）
    expect(window.open).toHaveBeenCalledWith(
      "/api/sites/boss/auth/slider/t7",
      "_blank",
      "noreferrer",
    );
  });

  it("auth.state 一到就收起喂料表单", async () => {
    renderPage();
    applyWsEvent(
      frame({
        type: "auth.need_code",
        task_id: "t9",
        site: "boss",
        data: { task_id: "t9", status: "need_code", phone_masked: "138****8000" },
      }),
    );
    expect(await screen.findByText("需要你输入短信验证码")).toBeInTheDocument();

    applyWsEvent(frame({ type: "auth.state", task_id: "t9", site: "boss", data: { status: "done" } }));
    await waitFor(() => {
      expect(screen.queryByText("需要你输入短信验证码")).not.toBeInTheDocument();
    });
  });

  it("别的站点的喂料不串台，只显示当前站点的", async () => {
    renderPage();
    applyWsEvent(
      frame({
        type: "auth.need_code",
        task_id: "t1",
        site: "boss",
        data: { task_id: "t1", status: "need_code", phone_masked: "" },
      }),
    );
    applyWsEvent(
      frame({
        type: "auth.need_code",
        task_id: "t2",
        site: "other",
        data: { task_id: "t2", status: "need_code", phone_masked: "" },
      }),
    );

    await screen.findByText("需要你输入短信验证码");
    // 只有一张卡：另一个站点的料归它自己的登录页
    expect(screen.getAllByText("需要你输入短信验证码")).toHaveLength(1);
  });
});

describe("LoginPage caps 降级", () => {
  it("caps.auth.qr 为假时扫码档禁用，不出假入口", async () => {
    vi.stubGlobal("fetch", stubFetch(NO_QR));
    renderPage();
    await screen.findByText(/还没登录/);

    const qr = screen.getByRole("radio", { name: /App 扫码/ });
    expect(qr).toBeDisabled();
  });
});
