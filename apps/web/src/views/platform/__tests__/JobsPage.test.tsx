import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../../theme/ThemeModeProvider";
import { ToastProvider } from "../../../components/feedback/Toast";
import { PlatformProvider } from "../../../platform/PlatformProvider";
import { JobsPage } from "../JobsPage";

/** 平台描述符：`caps.source.expect_flow` / `caps.catalog.unsupported` 是这页的降级开关。 */
function platform(over: {
  expect_flow?: boolean;
  unsupported?: string[];
} = {}) {
  return {
    site_id: "boss",
    display_name: "BOSS直聘",
    icon: "boss",
    caps: {
      auth: { sms: true, slider: true, qr: true },
      source: { expect_flow: over.expect_flow ?? true, tags: true, skills: true },
      catalog: { unsupported: over.unsupported ?? [] },
      delivery: { apply_gate: "after_mutual_conversation", apply_channel: "onekeydeliver" },
    },
    session: { logged_in: true, phone_masked: "138****8000", busy: false },
    nav_extras: [{ key: "expect", label: "求职期望", route: "expect" }],
  };
}

const JOB = {
  encrypt_job_id: "j1",
  job_name: "前端工程师",
  brand_name: "某某科技",
  location: "广州·天河区",
  salary_desc: "15-25K",
  job_experience: "3-5年",
  job_degree: "本科",
  jd_status: "completed",
};

const COLLECTION = {
  search: {
    query: "前端",
    city: "广州",
    jobType: "",
    salary: "",
    experience: [],
    degree: [],
    industry: [],
    scale: [],
    payType: [],
    partTime: [],
    stage: [],
    encryptExpectId: "",
    pageSize: 15,
  },
  collection: {
    max_pages: 5,
    start_page: 1,
    page_interval: 1,
    use_search: true,
    fetch_details: true,
    detail_interval: 1,
    store_blacklisted: true,
    expect_kind: "",
    encrypt_expect_id: "",
    mix_expect_type: "",
    expect_info: "",
    expect_name: "",
  },
};

function stubFetch(over: { expect_flow?: boolean; unsupported?: string[] } = {}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/platforms")) {
      return new Response(JSON.stringify({ platforms: [platform(over)] }), { status: 200 });
    }
    if (url.includes("/collection/expects")) {
      return new Response(
        JSON.stringify({ mix: null, expects: [], partTime: [] }),
        { status: 200 },
      );
    }
    if (url.includes("/collection/config")) {
      return new Response(JSON.stringify(COLLECTION), { status: 200 });
    }
    if (url.includes("/filters/conditions")) {
      return new Response(
        JSON.stringify({
          cities: [], job_types: [], salaries: [], experiences: [], degrees: [],
          industries: [], scales: [], pay_types: [], stages: [], part_times: [], hot_cities: [],
          source: "api", degraded: {},
        }),
        { status: 200 },
      );
    }
    if (url.includes("/crawl/status") || url.includes("/fetch-descriptions/status")) {
      return new Response(JSON.stringify({ status: "idle" }), { status: 200 });
    }
    if (url.includes("/crawl/start") || url.includes("/fetch-descriptions")) {
      return new Response(JSON.stringify({ task_id: "t-crawl" }), { status: 200 });
    }
    if (url.includes("/api/interview/summary")) {
      return new Response(JSON.stringify({ items: { j1: { question_count: 3 } } }), { status: 200 });
    }
    if (url.includes("/api/sites/boss/jobs/j1")) {
      return new Response(JSON.stringify({ ...JOB, job_desc: "写页面", skills: ["React"] }), { status: 200 });
    }
    if (url.includes("/api/sites/boss/jobs")) {
      return new Response(
        JSON.stringify({ items: [JOB], total: 1, limit: 24, offset: 0 }),
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
        <MemoryRouter initialEntries={["/boss/jobs"]}>
          <PlatformProvider>
            <JobsPage />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

beforeEach(() => {
  globalThis.localStorage?.clear();
  vi.stubGlobal("fetch", stubFetch());
});

describe("JobsPage 列表与采集", () => {
  it("按站点作用域拉职位库，卡片出标题/公司/薪资", async () => {
    renderPage();
    expect(await screen.findByText("前端工程师")).toBeInTheDocument();
    expect(screen.getByText("某某科技")).toBeInTheDocument();
    expect(screen.getByText("15-25K")).toBeInTheDocument();

    // 走 /api/sites/{site}/…，不是 legacy 别名
    const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.startsWith("/api/sites/boss/jobs?"))).toBe(true);
    expect(urls.some((u) => u.startsWith("/api/jobs"))).toBe(false);
  });

  it("采集条件摘要带关键词与页数，期望流不出口时连提示都不出", async () => {
    vi.stubGlobal("fetch", stubFetch({ expect_flow: false }));
    renderPage();
    await screen.findByText("前端工程师");

    // 摘要是 legacy collectionSummary 同款文案
    expect(screen.getByText(/关键词「前端」/)).toBeInTheDocument();
    expect(screen.getByText(/最多 5 页/)).toBeInTheDocument();
    expect(screen.getByText(/抓取 JD/)).toBeInTheDocument();
    // 期望流的说明（与侧栏入口）都按 caps 走，不为假入口
    expect(screen.queryByText(/支持求职期望流/)).not.toBeInTheDocument();
  });

  it("caps.source.expect_flow 为真时给期望流说明", async () => {
    renderPage();
    await screen.findByText("前端工程师");
    expect(screen.getByText(/支持求职期望流/)).toBeInTheDocument();
  });

  it("开始采集打 /crawl/start，跑起来后按钮变「采集中…」", async () => {
    const fetchMock = stubFetch();
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await screen.findByText("前端工程师");

    await userEvent.click(screen.getByRole("button", { name: "开始采集" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/crawl/start",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(await screen.findByRole("button", { name: "采集中…" })).toBeDisabled();
  });

  it("开始采集请求在飞时按钮转「启动中…」并禁用，防连点重复开跑", async () => {
    // 用 deferred promise 卡住 /crawl/start，制造「请求在飞」窗口
    let resolveStart!: (r: Response) => void;
    const startGate = new Promise<Response>((res) => { resolveStart = res; });
    const base = stubFetch();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/crawl/start")) return startGate;
        return base(input);
      }),
    );
    renderPage();
    await screen.findByText("前端工程师");

    await userEvent.click(screen.getByRole("button", { name: "开始采集" }));
    const inflight = screen.getByRole("button", { name: "启动中…" });
    expect(inflight).toBeDisabled();

    resolveStart(new Response(JSON.stringify({ task_id: "t-crawl" }), { status: 200 }));
    expect(await screen.findByRole("button", { name: "采集中…" })).toBeDisabled();
  });
});

describe("JobsPage 批量与详情", () => {
  it("删除所选先弹窗二次确认，确认后才打 /jobs/delete 且 body 叫 ids", async () => {
    renderPage();
    await screen.findByText("前端工程师");

    await userEvent.click(screen.getByRole("checkbox", { name: "选择 前端工程师" }));
    await userEvent.click(screen.getByRole("button", { name: /删除所选/ }));

    // 破坏性操作先弹居中确认弹窗，没确认前不发请求
    await screen.findByRole("dialog");
    expect(globalThis.fetch).not.toHaveBeenCalledWith(
      "/api/sites/boss/jobs/delete",
      expect.anything(),
    );
    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/jobs/delete",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ ids: ["j1"] }),
        }),
      );
    });
  });

  it("点卡片开详情，删除这条也先弹窗确认，确认后走 DELETE /jobs/{id}", async () => {
    renderPage();
    await userEvent.click(await screen.findByText("前端工程师"));

    // 详情 Sheet 的标题是职位名；删完收起
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/jobs/j1",
        expect.objectContaining({ method: "GET" }),
      );
    });
    const drawer = await screen.findByRole("dialog");
    await userEvent.click(within(drawer).getByRole("button", { name: "删除这条" }));

    // 二次确认弹窗后才真删
    await userEvent.click(await screen.findByRole("button", { name: "确认删除" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/jobs/j1",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });
});

describe("JobsPage caps 降级", () => {
  it("catalog.unsupported 点名的维度在采集条件里置灰", async () => {
    vi.stubGlobal("fetch", stubFetch({ unsupported: ["salary", "industry"] }));
    renderPage();
    await screen.findByText("前端工程师");

    await userEvent.click(screen.getByRole("button", { name: "采集条件" }));

    // 置灰说明点名这两个维度，而不是把输入框硬塞出来还能填
    expect(await screen.findByText(/不支持的维度已置灰/)).toBeInTheDocument();
    expect(screen.getByText(/薪资、行业/)).toBeInTheDocument();
  });
});
