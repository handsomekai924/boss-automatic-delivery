/**
 * MatchPage 测试：三段（匹配工作台 / 分析历史 / 招呼语）+ 历史载入回工作台 + 无投递假按钮。
 *
 * 骨架照抄 TasksPage.test.tsx（ThemeModeProvider + ToastProvider + MemoryRouter + PlatformProvider），
 * fetch 按 URL 全等分流——`stringContaining("/api/match/a1")` 会把 `/rematch`、`/greeting` 一起放进来，
 * 断言 URL 一律用全等再配 `objectContaining({ method })`。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../../theme/ThemeModeProvider";
import { ToastProvider } from "../../../components/feedback/Toast";
import { PlatformProvider } from "../../../platform/PlatformProvider";
import { MatchPage } from "../MatchPage";
import { resetStores } from "../../../ws/dispatch";

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
      nav_extras: [],
    },
    {
      site_id: "other",
      display_name: "另一家",
      icon: "other",
      caps: {
        auth: { sms: false, slider: false, qr: true },
        source: { expect_flow: false, tags: false, skills: false },
        catalog: { unsupported: [] },
        delivery: { apply_gate: "never", apply_channel: "im" },
      },
      session: { logged_in: false, phone_masked: "", busy: false },
      nav_extras: [],
    },
  ],
};

const RESUMES = {
  items: [
    { resume_id: "r1", title: "前端简历", source_name: "a.pdf", created_at: 1700000000, parsed: true },
    { resume_id: "r2", title: "后端简历", source_name: "b.pdf", created_at: 1700000100, parsed: true },
  ],
};

/** 两条匹配结果：j1 分高、j2 分低——列表按分数倒序，DOM 里 j1 在前。 */
const MATCH_J1 = {
  encrypt_job_id: "j1",
  job_name: "高级前端",
  brand_name: "星海科技",
  location: "上海",
  salary_desc: "25-40K",
  job_experience: "3-5年",
  job_degree: "本科",
  match_score: 88,
  matched_skills: ["React"],
  missing_skills: ["Flutter"],
  verdict: "整体匹配",
  pros: ["技术栈对口"],
  cons: ["通勤远"],
  advice: "突出 React 经验",
  greeting: "您好，我有 5 年 React 经验。",
};

const MATCH_J2 = {
  encrypt_job_id: "j2",
  job_name: "后端工程师",
  brand_name: "深蓝网络",
  location: "杭州",
  salary_desc: "20-35K",
  job_experience: "1-3年",
  job_degree: "本科",
  match_score: 60,
  matched_skills: ["Go"],
  missing_skills: ["K8s"],
  verdict: "匹配度一般",
  pros: [],
  cons: [],
  advice: "补一下云原生经验",
  greeting: "您好，我对这个岗位很感兴趣。",
};

/** 历史分析两条，a1 更新（created_at 更大）在前。 */
const ANALYSES = {
  items: [
    {
      analysis_id: "a1",
      resume_id: "r1",
      resume_title: "前端简历",
      created_at: 1700000500,
      job_count: 2,
      top_score: 88,
      status: "done",
      model: "gpt-x",
    },
    {
      analysis_id: "a2",
      resume_id: "r2",
      resume_title: "后端简历",
      created_at: 1700000400,
      job_count: 5,
      top_score: 72,
      status: "done",
      model: "gpt-x",
    },
  ],
};

/** a1 的详情：matches 故意乱序，页面要按分数排。 */
const ANALYSIS_A1 = {
  analysis_id: "a1",
  kind: "match",
  resume_id: "r1",
  resume_title: "前端简历",
  created_at: 1700000500,
  status: "done",
  matches: [MATCH_J2, MATCH_J1],
};

/** 按 URL 全等 + method 分流的 fetch 假实现（错误兜底：一律 `{ok:true}`）。 */
function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });

    if (url.includes("/api/platforms")) return json(PLATFORMS);
    if (url === "/api/resume/list") return json(RESUMES);
    if (url === "/api/match/analyses") return json(ANALYSES);
    if (url === "/api/match/analyses/a1") {
      return method === "DELETE" ? json({ deleted: true }) : json(ANALYSIS_A1);
    }
    if (url === "/api/match/analyze/status") return json({ task_id: null, status: "idle" });
    if (url === "/api/match/analyze") {
      // 启动回包就是整份 MatchTask.snapshot()（repos 收窄成了 `{task_id}`）
      return json({
        task_id: "mt_1",
        analysis_id: "mt_1",
        status: "running",
        resume_id: "r1",
        created_at: 1700001000,
        done: 0,
        total: 2,
        matches: [],
        events: [],
      });
    }
    if (url === "/api/match/analyze/mt_1") {
      return json({
        task_id: "mt_1",
        analysis_id: "mt_1",
        status: "done",
        resume_id: "r1",
        created_at: 1700001000,
        ended_at: 1700001100,
        done: 2,
        total: 2,
        matches: [MATCH_J1, MATCH_J2],
        events: [{ at: 1700001100, name: "finished", payload: { analysis_id: "mt_1" } }],
      });
    }
    if (url === "/api/match/a1/greeting" && method === "PATCH") {
      const body = JSON.parse(String(init?.body ?? "{}")) as { greeting?: string };
      return json({ ok: true, item: { ...MATCH_J2, greeting: body.greeting ?? "" } });
    }
    if (url === "/api/match/a1/greeting/regenerate") {
      return json({ ok: true, greeting: "重打的招呼语草稿" });
    }
    if (url === "/api/match/a1/rematch") {
      // 只改不增：回的是「原地替换那条」的新 item
      return json({ ok: true, item: { ...MATCH_J1, verdict: "重跑后的结论", match_score: 91 } });
    }
    if (url === "/api/system/greeting-prompt") {
      // 后端真实形状（repos 类型写的是 `{prompt}`，见 MatchPage 文件头注释）
      return json({ requirement: "", custom: "", is_default: true, default: "内置默认招呼语要求" });
    }
    if (url.startsWith("/api/crawl/tasks")) {
      return json({
        items: [
          { task_id: "c1", site: "boss", status: "done", created_at: 1700000200, params: { title: "前端批" } },
        ],
        total: 1,
        limit: 100,
        offset: 0,
      });
    }
    if (url.startsWith("/api/interview/jobs")) {
      return json({
        items: [
          { encrypt_job_id: "j1", job_name: "高级前端", brand_name: "星海科技", location: "上海", salary_desc: "25-40K" },
        ],
        total: 1,
      });
    }
    return json({ ok: true });
  });
}

let fetchMock: ReturnType<typeof stubFetch>;

/** 个别用例要拖慢某一个 URL 的回包（骨架屏在途断言），独立 stub 覆盖。 */
function stubFetchWithDelay(delayUrl: string, delayMs: number) {
  const base = stubFetch();
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === delayUrl) {
      await new Promise((r) => setTimeout(r, delayMs));
    }
    return base(input, init);
  });
}

function renderPage() {
  return render(
    <ThemeModeProvider>
      <ToastProvider>
        <MemoryRouter initialEntries={["/match"]}>
          <PlatformProvider>
            <MatchPage />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

/** 切分段（SegmentedControl 是 radiogroup）。 */
function clickTab(name: string) {
  return userEvent.click(screen.getByRole("radio", { name }));
}

/** 载入一份历史分析：进「分析历史」→ 点「载入」→ 自动回工作台。 */
async function loadHistoryA1() {
  await clickTab("分析历史");
  const row = await screen.findByRole("group", { name: "历史分析 a1" });
  await userEvent.click(within(row).getByRole("button", { name: "载入" }));
  await clickTab("匹配工作台");
}

beforeEach(() => {
  resetStores();
  globalThis.localStorage?.clear();
  fetchMock = stubFetch();
  vi.stubGlobal("fetch", fetchMock);
});

describe("MatchPage 分段", () => {
  it("首屏在匹配工作台，三个分段可切换", async () => {
    renderPage();
    expect(await screen.findByText("开始匹配")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "匹配工作台" })).toHaveAttribute("aria-checked", "true");

    await clickTab("分析历史");
    expect(await screen.findByRole("group", { name: "历史分析 a1" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "分析历史" })).toHaveAttribute("aria-checked", "true");

    await clickTab("招呼语");
    expect(await screen.findByLabelText("招呼语要求")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "招呼语" })).toHaveAttribute("aria-checked", "true");
  });
});

describe("MatchPage 历史分析", () => {
  it("列表渲染：简历、职位数、最高分、模型都可读", async () => {
    renderPage();
    await clickTab("分析历史");
    const row = await screen.findByRole("group", { name: "历史分析 a1" });
    expect(within(row).getByText("前端简历")).toBeInTheDocument();
    expect(within(row).getByText(/2 个职位/)).toBeInTheDocument();
    expect(within(row).getByText(/最高 88/)).toBeInTheDocument();
    expect(within(row).getByText(/gpt-x/)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "历史分析 a2" })).toBeInTheDocument();
  });

  it("没有历史分析时给空态而不是空白", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/platforms")) {
          return new Response(JSON.stringify(PLATFORMS), { status: 200 });
        }
        return new Response(JSON.stringify({ items: [] }), { status: 200 });
      }),
    );
    renderPage();
    await clickTab("分析历史");
    expect(await screen.findByText("还没有历史分析")).toBeInTheDocument();
  });

  it("删除带确认，确认后 DELETE 并从列表消失", async () => {
    renderPage();
    await clickTab("分析历史");
    const row = await screen.findByRole("group", { name: "历史分析 a1" });
    await userEvent.click(within(row).getByRole("button", { name: "删除" }));
    expect(await screen.findByText(/删除这份分析/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/match/analyses/a1",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
    expect(screen.queryByRole("group", { name: "历史分析 a1" })).not.toBeInTheDocument();
  });
});

describe("MatchPage 匹配结果", () => {
  it("载入历史分析后结果按分数倒序，每条带分数与招呼语", async () => {
    renderPage();
    await loadHistoryA1();

    const cards = await screen.findAllByRole("group", { name: /匹配结果/ });
    expect(cards).toHaveLength(2);
    // 详情回包里 j2 在前，DOM 里必须按分数把 j1 排到第一
    expect(cards[0]).toHaveAttribute("aria-label", "匹配结果 高级前端 星海科技");
    expect(within(cards[0]).getByText("88")).toBeInTheDocument();
    expect(within(cards[1]).getByText("60")).toBeInTheDocument();
    expect(within(cards[1]).getByLabelText("招呼语")).toHaveValue("您好，我对这个岗位很感兴趣。");
  });

  it("「重跑」调 /api/match/{analysis_id}/rematch 且只改不增（原地替换那条）", async () => {
    renderPage();
    await loadHistoryA1();
    const card1 = await screen.findByRole("group", { name: "匹配结果 高级前端 星海科技" });
    expect(within(card1).getByText("整体匹配")).toBeInTheDocument();

    await userEvent.click(within(card1).getByRole("button", { name: "重跑" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/match/a1/rematch",
        expect.objectContaining({ method: "POST" }),
      );
    });

    // 原地替换：旧结论没了、新结论来了、条数没变
    expect(await screen.findByText("重跑后的结论")).toBeInTheDocument();
    expect(screen.queryByText("整体匹配")).not.toBeInTheDocument();
    expect(within(card1).getByText("91")).toBeInTheDocument();
    expect(screen.getAllByRole("group", { name: /匹配结果/ })).toHaveLength(2);
  });

  it("招呼语编辑保存调 PATCH /api/match/{analysis_id}/greeting，带职位 id 与草稿文本", async () => {
    renderPage();
    await loadHistoryA1();
    const card2 = await screen.findByRole("group", { name: "匹配结果 后端工程师 深蓝网络" });

    const ta = within(card2).getByLabelText("招呼语");
    await userEvent.clear(ta);
    await userEvent.type(ta, "您好，我做过五年后端");
    await userEvent.click(within(card2).getByRole("button", { name: "保存招呼语" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/match/a1/greeting",
        expect.objectContaining({ method: "PATCH" }),
      );
    });
    const call = fetchMock.mock.calls.find((c) => String(c[0]) === "/api/match/a1/greeting") as
      | [string, RequestInit]
      | undefined;
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      encrypt_job_id: "j2",
      greeting: "您好，我做过五年后端",
    });
  });
});

describe("MatchPage 发起匹配", () => {
  it("不选批次 = 全部职位：POST /api/match/analyze 只带 resume_id，进度接上后出结果", async () => {
    renderPage();
    await screen.findByText("开始匹配");

    await userEvent.click(screen.getByRole("button", { name: "开始匹配" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/match/analyze",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const call = fetchMock.mock.calls.find((c) => String(c[0]) === "/api/match/analyze") as
      | [string, RequestInit]
      | undefined;
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ resume_id: "r1" });

    // 轮询业务快照收尾 → 结果落到工作台
    expect(await screen.findByRole("group", { name: "匹配结果 高级前端 星海科技" })).toBeInTheDocument();
  });

  it("选了抓取批次才带 crawl_task_id", async () => {
    renderPage();
    await screen.findByText("开始匹配");

    await userEvent.click(screen.getByLabelText("抓取批次"));
    await userEvent.click(await screen.findByRole("option", { name: /前端批/ }));
    await userEvent.click(screen.getByRole("button", { name: "开始匹配" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/match/analyze",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const call = fetchMock.mock.calls.find((c) => String(c[0]) === "/api/match/analyze") as
      | [string, RequestInit]
      | undefined;
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ resume_id: "r1", crawl_task_id: "c1" });
  });
});

describe("MatchPage 工作台布局与搜索范围", () => {
  it("左右分栏：左「匹配设置」右「匹配结果区」，窄屏才堆叠（flexDirection 响应式）", async () => {
    renderPage();
    await screen.findByText("开始匹配");
    const cols = screen.getByTestId("match-workbench-cols");
    const setup = within(cols).getByTestId("match-setup-col");
    const results = within(cols).getByTestId("match-results-col");
    expect(setup).toHaveAttribute("aria-label", "匹配设置");
    expect(results).toHaveAttribute("aria-label", "匹配结果区");
    // 两栏是同一行容器下的兄弟节点（md+ 左右、xs 上下由 sx 断点控制）
    expect(setup.parentElement).toBe(cols);
    expect(results.parentElement).toBe(cols);
  });

  it("搜索范围只剩「抓取批次 + 搜索框」同一行，没有「全部岗位」分段", async () => {
    renderPage();
    await screen.findByText("开始匹配");
    // 「全部岗位 / 抓取批次」互斥分段整个拿掉
    expect(screen.queryByText("全部岗位")).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "岗位搜索范围" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "全部岗位" })).not.toBeInTheDocument();

    // 批次下拉 + 搜索框必须同一行容器
    const scopeRow = screen.getByTestId("match-scope-row");
    expect(within(scopeRow).getByLabelText("抓取批次")).toBeInTheDocument();
    expect(within(scopeRow).getByLabelText("搜职位名")).toBeInTheDocument();
  });
});

describe("MatchPage 匹配结果骨架屏", () => {
  it("载入历史分析在途用骨架屏预占结果区，回来后换成真卡", async () => {
    // 详情回包拖慢一点，才能截到「在途」那一帧
    vi.stubGlobal("fetch", stubFetchWithDelay("/api/match/analyses/a1", 150));
    fetchMock = globalThis.fetch as unknown as ReturnType<typeof stubFetch>;

    renderPage();
    await clickTab("分析历史");
    const row = await screen.findByRole("group", { name: "历史分析 a1" });
    await userEvent.click(within(row).getByRole("button", { name: "载入" }));

    // 在途：结果区是骨架屏占位（不是空态），布局高度先占住
    expect(await screen.findByRole("status", { name: "匹配结果加载中" })).toBeInTheDocument();
    expect(screen.getByTestId("match-results-skeleton")).toBeInTheDocument();
    expect(screen.queryByText("启动匹配后，结果会按分数从高到低排在这里")).not.toBeInTheDocument();

    // 落地：真卡来了，骨架撤掉
    expect(await screen.findByRole("group", { name: "匹配结果 高级前端 星海科技" })).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "匹配结果加载中" })).not.toBeInTheDocument();
  });
});

describe("MatchPage 发对话/投递的归属", () => {
  it("页面上没有投递/发对话的假按钮，只有一条搬家说明", async () => {
    renderPage();
    expect(await screen.findByText(/发对话与投递已移到平台侧/)).toBeInTheDocument();
    const names = screen.getAllByRole("button").map((b) => b.textContent ?? "");
    expect(names.join(" ")).not.toMatch(/投递|发对话|发送|打招呼/);
  });
});

describe("MatchPage 招呼语", () => {
  it("表头左侧标题「打招呼语」+ 右侧动作（对齐分析历史表头）", async () => {
    renderPage();
    await clickTab("招呼语");
    // 左标题回来了：以前只有右侧两个按钮，标题空缺
    const title = await screen.findByText("打招呼语");
    expect(title).toBeInTheDocument();
    // 同一表头行里：标题占左、按钮靠右（和「历史分析」同款 flex 行）
    const header = title.parentElement as HTMLElement;
    expect(header).toHaveTextContent("恢复默认");
    expect(header).toHaveTextContent("保存");
  });

  it("分段里读内置默认、改自定义、保存走 PUT /api/system/greeting-prompt", async () => {
    renderPage();
    await clickTab("招呼语");
    expect(await screen.findByText("内置默认招呼语要求")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("招呼语要求"), "突出项目经验");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/system/greeting-prompt",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    // 找 PUT 那一发——GET（进段时读）也在同一个 URL 上，别拿错
    const call = fetchMock.mock.calls.find(
      (c) => String(c[0]) === "/api/system/greeting-prompt" && String(c[1]?.method ?? "") === "PUT",
    ) as [string, RequestInit] | undefined;
    expect(String(call?.[1]?.body)).toContain("突出项目经验");
  });
});
