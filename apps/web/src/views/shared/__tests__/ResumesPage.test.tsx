import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../../theme/ThemeModeProvider";
import { ToastProvider } from "../../../components/feedback/Toast";
import { PlatformProvider } from "../../../platform/PlatformProvider";
import { ResumesPage } from "../ResumesPage";
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
  ],
};

/** 两份简历：已解析 / 未解析，覆盖解析状态徽标。 */
const RESUME_ITEMS = [
  {
    resume_id: "r1",
    title: "前端简历-张三",
    source_name: "zhangsan.docx",
    created_at: 1700000100,
    llm: { parsed_at: 1700000200, model: "gpt-x", has_data: true },
  },
  {
    resume_id: "r2",
    title: "后端简历-李四",
    source_name: "lisi.md",
    created_at: 1700000300,
    llm: null,
  },
];

const RESUME_DETAIL = {
  resume_id: "r1",
  title: "前端简历-张三",
  created_at: 1700000100,
  raw: "# 张三\n前端工程师\n手机号 13800000000",
  llm: {
    parsed_at: 1700000200,
    model: "gpt-x",
    data: {
      basic: { name: "张三", phone: "13800000000", email: "z@x.com", city: "上海" },
      intent: { position: "前端工程师", city: "上海", salary: "25-40K" },
      work: [{ company: "某某科技", title: "前端", period: "2020-2023", highlights: ["React 重构"] }],
      project: [],
      education: [{ school: "某某大学", major: "计算机", degree: "本科", period: "2016-2020" }],
      skills: ["React", "TypeScript"],
      self_evaluation: "认真负责",
      summary: "5 年前端经验",
    },
  },
};

/** 自由模式章节：后端是 `{title, level}` 不是纯字符串。 */
const SECTIONS = {
  sections: [
    { title: "工作经历", level: 2 },
    { title: "项目经历", level: 2 },
    { title: "技能标签", level: 2 },
    { title: "求职意向", level: 1 },
  ],
};

/** 预设提示词：每个预设是 `{name, prompt}`。 */
const PRESETS = {
  categories: ["通用", "技术岗"],
  presets: {
    通用: [{ name: "精简", prompt: "请把简历压缩到一页" }],
    技术岗: [{ name: "突出项目", prompt: "请突出项目成果与技术栈" }],
  },
};

const JOBS = {
  items: [
    {
      encrypt_job_id: "j1",
      job_name: "前端工程师",
      brand_name: "某某科技",
      location: "上海",
      salary_desc: "25-40K",
      jd_status: "open",
    },
    {
      encrypt_job_id: "j2",
      job_name: "全栈工程师",
      brand_name: "另一家",
      location: "北京",
      salary_desc: "30-50K",
      jd_status: "open",
    },
  ],
  total: 2,
};

/** 一个爬取批次：批次下拉有得选，「选了批次才带 crawl_task_id」才能断言。 */
const BATCHES = {
  items: [
    {
      task_id: "ct1",
      site: "boss",
      status: "done",
      created_at: 1700000000,
      params: { title: "前端批" },
    },
  ],
  total: 1,
  limit: 100,
  offset: 0,
};

const REVISION_ITEMS = [
  {
    revision_id: "rev1",
    resume_id: "r1",
    resume_title: "前端简历-张三",
    created_at: 1700001000,
    status: "draft",
    mode: "targeted",
    job_count: 2,
    model: "gpt-x",
  },
  {
    revision_id: "rev2",
    resume_id: "r2",
    resume_title: "后端简历-李四",
    created_at: 1700001100,
    status: "running",
    mode: "free",
    job_count: 0,
    model: "gpt-x",
  },
];

const REVISION_DETAIL = {
  ...REVISION_ITEMS[0],
  original_md: "# 张三\n前端工程师",
  revised_md: "# 张三\n资深前端工程师",
  changes: [
    { section: "基本信息", type: "rewrote", summary: "标题改为资深", reason: "对齐 JD 级别" },
  ],
  diff: [
    { tag: "equal", left: "# 张三", right: "# 张三" },
    { tag: "replace", left: "前端工程师", right: "资深前端工程师" },
  ],
};

const SAVE_OK = {
  ok: true,
  saved_path: "F:/resumes/前端简历-张三-定向.md",
  saved_resume_id: "r-new",
  title: "前端简历-张三-定向",
};

/** 可变状态：上传 / 删除后列表要跟着变，才能断言「刷新列表」；职位列表可分页。 */
const state = {
  list: [] as typeof RESUME_ITEMS,
  revisions: [] as typeof REVISION_ITEMS,
  jobs: [] as typeof JOBS.items,
  jobsTotal: 0,
};

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}

/** 按 URL + method 分流的 fetch 假实现；简历列表与优化记录可被写操作改掉。 */
function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = String(init?.method || "GET").toUpperCase();

    if (url.includes("/api/platforms")) return json(PLATFORMS);

    if (url.includes("/api/resume/revise/presets")) return json(PRESETS);
    if (url.includes("/api/resume/revise/status")) return json({ task_id: null, status: "idle" });
    if (url.includes("/api/resume/revise/free-start") && method === "POST") {
      return json({ task_id: "rt1", revision_id: "rt1", status: "running", mode: "free", done: false, events: [] });
    }
    if (url.endsWith("/api/resume/revise/start") && method === "POST") {
      return json({ task_id: "rt1", revision_id: "rt1", status: "running", mode: "targeted", job_count: 1, done: false, events: [] });
    }
    if (url.includes("/api/resume/revisions") && url.includes("/save") && method === "POST") {
      return json(SAVE_OK);
    }
    if (url.includes("/api/resume/revisions") && url.includes("/regenerate") && method === "POST") {
      return json({ task_id: "rt2", status: "running" });
    }
    if (url.includes("/api/resume/revisions?") || url.endsWith("/api/resume/revisions")) {
      return json({ items: state.revisions, total: state.revisions.length });
    }
    if (url.includes("/api/resume/revisions/") && method === "DELETE") {
      return json({ deleted: true });
    }
    if (url.includes("/api/resume/revisions/")) return json(REVISION_DETAIL);

    if (url.includes("/api/resume/item/") && url.includes("/sections")) return json(SECTIONS);
    if (url.includes("/api/resume/item/") && url.includes("/parse") && method === "POST") {
      // 空 task_id：页面不会挂假进度，轮询也不会跑
      return json({ resume_id: "r1", task_id: "", status: "pending" });
    }
    if (url.includes("/api/resume/item/") && url.includes("/parse")) {
      return json({ resume_id: "r1", llm: RESUME_DETAIL.llm, parsed: true });
    }
    if (url.includes("/api/resume/item/") && method === "DELETE") {
      const id = url.split("/api/resume/item/")[1].split("/")[0];
      state.list = state.list.filter((x) => x.resume_id !== id);
      return json({ ok: true });
    }
    if (url.includes("/api/resume/item/")) {
      const id = decodeURIComponent(url.split("/api/resume/item/")[1].split("/")[0]);
      // r2 未解析：llm 只有壳没有 data
      if (id === "r2") {
        return json({ ...RESUME_DETAIL, resume_id: id, title: "后端简历-李四", raw: "# 李四", llm: null });
      }
      return json({ ...RESUME_DETAIL, resume_id: id });
    }

    if (url.includes("/api/resume/upload") && method === "POST") {
      state.list = [
        {
          resume_id: "r-up",
          title: "新上传-王五",
          source_name: "new.docx",
          created_at: 1700000900,
          llm: null,
        },
        ...state.list,
      ];
      return json({ resume_id: "r-up", title: "新上传-王五", raw: "hello" });
    }
    if (url.endsWith("/api/resume/list")) return json({ items: state.list });

    if (url.includes("/api/interview/jobs")) {
      // 按 limit/offset 整页切（分页控件翻页 = 整页替换，不是追加）
      const u = new URL(url, "http://t");
      const limit = Number(u.searchParams.get("limit") || 20);
      const offset = Number(u.searchParams.get("offset") || 0);
      return json({ items: state.jobs.slice(offset, offset + limit), total: state.jobsTotal });
    }
    if (url.includes("/api/crawl/tasks")) return json(BATCHES);

    if (url.includes("/api/tasks?") || url.endsWith("/api/tasks")) return json({ items: [] });
    if (url.includes("/api/tasks/")) {
      return json({ task_id: "pt1", status: "done", done: 1, total: 1, current: "", error: "", events: [] });
    }

    return json({ ok: true });
  });
}

function renderPage() {
  return render(
    <ThemeModeProvider>
      <ToastProvider>
        <MemoryRouter initialEntries={["/resumes"]}>
          <PlatformProvider>
            <ResumesPage />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

/** 切到某一段（SegmentedControl 的 radio）。 */
async function clickTab(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("radio", { name }));
}

beforeEach(() => {
  resetStores();
  globalThis.localStorage?.clear();
  state.list = [...RESUME_ITEMS];
  state.revisions = [...REVISION_ITEMS];
  state.jobs = [...JOBS.items];
  state.jobsTotal = JOBS.total;
  vi.stubGlobal("fetch", stubFetch());
  vi.stubGlobal("confirm", () => true);
});

describe("ResumesPage 分段", () => {
  it("四段标签齐全，默认进简历库", async () => {
    renderPage();
    expect(await screen.findByRole("radio", { name: "简历库" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "简历解析" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "简历优化" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "优化记录" })).toBeInTheDocument();
    // 默认在简历库：清单在，解析详情不在
    expect(await screen.findByRole("button", { name: "查看原文「前端简历-张三」" })).toBeInTheDocument();
    expect(screen.queryByText("这份简历还没解析")).not.toBeInTheDocument();
  });
});

describe("ResumesPage 简历库", () => {
  it("列表渲染两份简历，解析状态与来源可读", async () => {
    renderPage();
    // 标题会同时出现在卡片和解析详情头，用卡片按钮认卡（唯一）
    expect(await screen.findByRole("button", { name: "查看原文「前端简历-张三」" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看原文「后端简历-李四」" })).toBeInTheDocument();
    expect(screen.getByText("已解析")).toBeInTheDocument();
    expect(screen.getByText("未解析")).toBeInTheDocument();
    expect(screen.getByText(/zhangsan\.docx/)).toBeInTheDocument();
    // 清单段不带解析详情
    expect(screen.queryByText("某某科技")).not.toBeInTheDocument();
  });

  it("空库给空态而不是空白", async () => {
    state.list = [];
    renderPage();
    expect(await screen.findByText("还没有简历")).toBeInTheDocument();
  });

  it("上传成功后刷新列表并出现新简历", async () => {
    const { container } = renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    const file = new File(["hello"], "new.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    // 隐藏的 file input 不能走 userEvent.upload（会做 pointer-events 断言），直接 change
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByRole("button", { name: "查看原文「新上传-王五」" })).toBeInTheDocument();
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/resume/upload",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("查看原文在 Sheet 里展示 raw", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await user.click(screen.getByRole("button", { name: "查看原文「前端简历-张三」" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/手机号 13800000000/)).toBeInTheDocument();
  });

  it("删除要二次确认，确认后从列表消失", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await user.click(screen.getByRole("button", { name: "删除「前端简历-张三」" }));
    // 先弹二次确认（ConfirmSheet），点「确认删除」才真发 DELETE
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("删除「前端简历-张三」？")).toBeInTheDocument();
    expect(globalThis.fetch).not.toHaveBeenCalledWith(
      "/api/resume/item/r1",
      expect.objectContaining({ method: "DELETE" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/resume/item/r1",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
    // 删除回包后弹窗才关、列表才刷新
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "查看原文「前端简历-张三」" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "查看原文「后端简历-李四」" })).toBeInTheDocument();
  });
});

describe("ResumesPage 简历解析", () => {
  it("切过去按八段展示解析结果", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "简历解析");
    expect(await screen.findByText("某某科技")).toBeInTheDocument();
    expect(screen.getByText("React 重构")).toBeInTheDocument();
    expect(screen.getByText(/5 年前端经验/)).toBeInTheDocument();
    expect(screen.getByText("前端工程师")).toBeInTheDocument();
  });

  it("未解析的简历给开始解析而不是空白", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「后端简历-李四」" });

    // 点标题选中未解析那份（列表行 Surface 的 onClick；按钮会 stopPropagation）
    await user.click(screen.getByText("后端简历-李四"));
    await clickTab(user, "简历解析");

    expect(await screen.findByText("这份简历还没解析")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始解析" })).toBeInTheDocument();
  });
});

describe("ResumesPage 简历优化", () => {
  it("切到自由模式给章节勾选和预设提示词", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "简历优化");
    await user.click(await screen.findByRole("radio", { name: "自由优化" }));

    // 章节勾选（标签文本带 H 级别后缀，按前缀认）
    expect(await screen.findByRole("checkbox", { name: /工作经历/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /项目经历/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /技能标签/ })).toBeInTheDocument();

    // 预设提示词按钮（默认选中第一个分类）
    expect(screen.getByRole("button", { name: "精简" })).toBeInTheDocument();

    // 点预设填进提示词框
    await user.click(screen.getByRole("button", { name: "精简" }));
    expect(screen.getByLabelText("优化提示词")).toHaveValue("请把简历压缩到一页");
  });

  it("定向模式勾岗位后启动，打到 /api/resume/revise/start", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "简历优化");
    const jobBox = await screen.findByRole("checkbox", { name: /前端工程师/ });
    await user.click(jobBox);

    await user.click(screen.getByRole("button", { name: "启动优化" }));

    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find((c) => String(c[0]).endsWith("/api/resume/revise/start"));
      expect(hit).toBeDefined();
      const body = JSON.parse(String(hit?.[1]?.body));
      expect(body.job_ids).toEqual(["j1"]);
      expect(body.all_repo).toBe(false);
      expect(body.crawl_task_id).toBe("");
      expect(body.resume_id).toBeTruthy();
    });
  });

  it("自由模式点预设后启动，打到 /api/resume/revise/free-start", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "简历优化");
    await user.click(await screen.findByRole("radio", { name: "自由优化" }));
    await user.click(await screen.findByRole("button", { name: "精简" }));
    await user.click(screen.getByRole("button", { name: "启动优化" }));

    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find((c) => String(c[0]).endsWith("/api/resume/revise/free-start"));
      expect(hit).toBeDefined();
      const body = JSON.parse(String(hit?.[1]?.body));
      expect(body.optimization_prompt).toBe("请把简历压缩到一页");
      expect(body.resume_id).toBeTruthy();
    });
  });

  it("目标范围只剩「爬取批次 + 搜索框」同一行，没有「全部职位」分段", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });
    await clickTab(user, "简历优化");

    // 「全部职位 / 爬取批次」互斥分段整个拿掉
    expect(screen.queryByText("全部职位")).not.toBeInTheDocument();
    expect(screen.queryByText("全部岗位")).not.toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "岗位搜索范围" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "全部职位" })).not.toBeInTheDocument();

    // 批次下拉 + 搜索框必须同一行容器
    const scopeRow = screen.getByTestId("revise-scope-row");
    expect(within(scopeRow).getByLabelText("爬取批次")).toBeInTheDocument();
    expect(within(scopeRow).getByLabelText("搜索职位")).toBeInTheDocument();

    // 不选批次 = 搜全库：候选请求不带 crawl_task_id
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find((c) => String(c[0]).includes("/api/interview/jobs"));
      expect(hit).toBeDefined();
      expect(String(hit?.[0])).not.toContain("crawl_task_id");
    });
  });

  it("不选批次启动（未勾岗位）= 全库目标：all_repo 收口", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });
    await clickTab(user, "简历优化");
    await screen.findByRole("checkbox", { name: /前端工程师/ });

    await user.click(screen.getByRole("button", { name: "启动优化" }));

    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find((c) => String(c[0]).endsWith("/api/resume/revise/start"));
      expect(hit).toBeDefined();
      const body = JSON.parse(String(hit?.[1]?.body));
      expect(body.job_ids).toEqual([]);
      expect(body.crawl_task_id).toBe("");
      expect(body.all_repo).toBe(true);
      expect(body.resume_id).toBeTruthy();
    });
  });

  it("选了爬取批次（未勾岗位）启动才带 crawl_task_id", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });
    await clickTab(user, "简历优化");

    const batchSelect = await screen.findByLabelText("爬取批次");
    await waitFor(() => expect(batchSelect).toBeEnabled());
    await user.click(batchSelect);
    await user.click(await screen.findByRole("option", { name: /前端批/ }));

    await user.click(screen.getByRole("button", { name: "启动优化" }));

    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find((c) => String(c[0]).endsWith("/api/resume/revise/start"));
      expect(hit).toBeDefined();
      const body = JSON.parse(String(hit?.[1]?.body));
      expect(body.job_ids).toEqual([]);
      expect(body.crawl_task_id).toBe("ct1");
      expect(body.all_repo).toBe(false);
    });
  });

  it("候选岗位列表走分页控件（整页替换），没有「加载更多」", async () => {
    // 25 个候选：第 1 页 20 个、第 2 页 5 个
    state.jobs = Array.from({ length: 25 }, (_, i) => ({
      encrypt_job_id: `jp${i}`,
      job_name: `测试职位 ${i}`,
      brand_name: "某公司",
      location: "上海",
      salary_desc: "20-30K",
      jd_status: "open",
    }));
    state.jobsTotal = 25;

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });
    await clickTab(user, "简历优化");

    // 第 1 页整页展示；「加载更多」不再有
    expect(await screen.findByText("测试职位 0")).toBeInTheDocument();
    expect(screen.getByText("测试职位 19")).toBeInTheDocument();
    expect(screen.queryByText("测试职位 20")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加载更多" })).not.toBeInTheDocument();

    const prev = screen.getByRole("button", { name: "上一页" });
    const next = screen.getByRole("button", { name: "下一页" });
    expect(prev).toBeDisabled();
    expect(next).toBeEnabled();

    // 翻页 = 按 offset 拉下一页并整页替换（第 1 页的行不留着）
    await user.click(next);
    expect(await screen.findByText("测试职位 20")).toBeInTheDocument();
    expect(screen.getByText("测试职位 24")).toBeInTheDocument();
    expect(screen.queryByText("测试职位 0")).not.toBeInTheDocument();
    expect(screen.queryByText("测试职位 19")).not.toBeInTheDocument();
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find(
        (c) => String(c[0]).includes("/api/interview/jobs") && String(c[0]).includes("offset=20"),
      );
      expect(hit).toBeDefined();
    });
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });
});

describe("ResumesPage 优化记录", () => {
  it("记录列表渲染，查看给 diff 与变更点", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "优化记录");
    expect(await screen.findByRole("button", { name: "查看「前端简历-张三」" })).toBeInTheDocument();
    expect(screen.getByText("草稿")).toBeInTheDocument();
    expect(screen.getByText("生成中")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看「前端简历-张三」" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/资深前端工程师/)).toBeInTheDocument();
    expect(within(dialog).getByText("标题改为资深")).toBeInTheDocument();
  });

  it("确认保存走 /api/resume/revisions/{id}/save 且入库为新简历", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "优化记录");
    await user.click(await screen.findByRole("button", { name: "查看「前端简历-张三」" }));
    await screen.findByRole("dialog");

    await user.click(screen.getByRole("button", { name: "确认保存" }));
    expect(await screen.findByText("确认保存修改稿")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const hit = calls.find((c) => String(c[0]).endsWith("/api/resume/revisions/rev1/save"));
      expect(hit).toBeDefined();
      const body = JSON.parse(String(hit?.[1]?.body));
      expect(body.format).toBe("md");
      expect(body.title).toBeTruthy();
      expect(body.filename).toBeTruthy();
    });
  });

  it("变更点每条三行：章节 + 类型同行，原因、摘要各占一行", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "查看原文「前端简历-张三」" });

    await clickTab(user, "优化记录");
    await user.click(await screen.findByRole("button", { name: "查看「前端简历-张三」" }));
    await screen.findByRole("dialog");

    const section = await screen.findByText("基本信息");
    const type = screen.getByText("改写");
    const reason = screen.getByText("对齐 JD 级别");
    const summary = screen.getByText("标题改为资深");

    // 第 1 行：章节 + 类型同一行容器
    const line1 = section.parentElement as HTMLElement;
    expect(line1).toContainElement(type);
    expect(within(line1).queryByText("对齐 JD 级别")).not.toBeInTheDocument();
    expect(within(line1).queryByText("标题改为资深")).not.toBeInTheDocument();

    // 第 2、3 行：原因、摘要各占一行（条目竖排的直接子节点，互不同行）
    const item = line1.parentElement as HTMLElement;
    expect(item.children).toHaveLength(3);
    expect(reason.parentElement).toBe(item);
    expect(summary.parentElement).toBe(item);
  });
});
