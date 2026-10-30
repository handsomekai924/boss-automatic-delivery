import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../../theme/ThemeModeProvider";
import { ToastProvider } from "../../../components/feedback/Toast";
import { PlatformProvider } from "../../../platform/PlatformProvider";
import { TasksPage } from "../TasksPage";
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

/** 三条任务：在跑(boss) / 失败(共用) / 已结束(other)，覆盖三种操作分支。 */
const TASKS = {
  items: [
    {
      task_id: "t1",
      task_type: "crawl",
      site: "boss",
      title: "抓取「前端」岗位",
      status: "running",
      current: "第 2 页",
      done: 2,
      total: 10,
      error: "",
      created_at: 1700000100,
      ended_at: null,
      events: [
        { at: 1700000101, name: "page.done", payload: { page: 1, inserted: 12, updated: 3, raw_count: 15 } },
      ],
    },
    {
      task_id: "t2",
      task_type: "match",
      site: "",
      title: "人岗匹配",
      status: "error",
      current: "",
      done: 0,
      total: 0,
      error: "LLM 超时",
      created_at: 1700000200,
      ended_at: 1700000250,
      events: [{ at: 1700000240, name: "match.failed", payload: { detail: "超时" } }],
    },
    {
      task_id: "t3",
      task_type: "deliver",
      site: "other",
      title: "批量发送",
      status: "done",
      current: "",
      done: 5,
      total: 5,
      error: "",
      created_at: 1700000300,
      ended_at: 1700000360,
      events: [],
    },
  ],
};

/** 按 URL 分流的 fetch 假实现：平台描述符 + 任务列表，其余当操作回执。 */
function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/platforms")) {
      return new Response(JSON.stringify(PLATFORMS), { status: 200 });
    }
    if (url.endsWith("/api/tasks")) {
      return new Response(JSON.stringify(TASKS), { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true, cancelled: 0 }), { status: 200 });
  });
}

function renderPage() {
  return render(
    <ThemeModeProvider>
      <ToastProvider>
        <MemoryRouter initialEntries={["/tasks"]}>
          <PlatformProvider>
            <TasksPage />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

/** MUI Select 的触发器是 `role="combobox"`，按 label 找（label 自己有 pointer-events: none，点不得）。 */
function combo(label: string) {
  return screen.getByLabelText(label);
}

beforeEach(() => {
  resetStores();
  globalThis.localStorage?.clear();
  vi.stubGlobal("fetch", stubFetch());
});

describe("TasksPage", () => {
  it("首屏走 HTTP 兜底渲染任务卡，站点角标与事件流水可读", async () => {
    renderPage();
    expect(await screen.findByText("抓取「前端」岗位")).toBeInTheDocument();
    expect(screen.getByText("人岗匹配")).toBeInTheDocument();

    // 站点角标打显示名而不是站点 id；共用任务（site 空）不出角标
    expect(screen.getByText("BOSS直聘")).toBeInTheDocument();
    expect(screen.getByText("另一家")).toBeInTheDocument();
    expect(screen.getByText("2/10")).toBeInTheDocument();

    // 展开事件：payload 平铺成一行人话。卡片按创建时间倒序，得先认出是哪张卡。
    const panel = screen
      .getAllByRole("status")
      .find((p) => within(p).queryByText("抓取「前端」岗位"));
    expect(panel).toBeDefined();
    await userEvent.click(within(panel as HTMLElement).getByRole("button", { name: "事件" }));
    expect(await screen.findByText(/第 1 页 · 原始 15 · 入库 \+12 · 更新 3/)).toBeInTheDocument();
  });

  it("按类型与搜索词过滤，计数随筛选走", async () => {
    renderPage();
    await screen.findByText("抓取「前端」岗位");

    await userEvent.click(combo("类型"));
    await userEvent.click(await screen.findByRole("option", { name: "匹配" }));
    expect(screen.queryByText("抓取「前端」岗位")).not.toBeInTheDocument();
    expect(screen.getByText("人岗匹配")).toBeInTheDocument();
    expect(screen.getByText(/共 1 条/)).toBeInTheDocument();

    await userEvent.click(combo("类型"));
    await userEvent.click(await screen.findByRole("option", { name: "全部" }));

    await userEvent.type(screen.getByRole("searchbox"), "批量");
    expect(screen.getByText("批量发送")).toBeInTheDocument();
    expect(screen.queryByText("人岗匹配")).not.toBeInTheDocument();
  });

  it("站点筛选把共用任务（site 为空）单独拎出来", async () => {
    renderPage();
    await screen.findByText("抓取「前端」岗位");

    await userEvent.click(combo("站点"));
    await userEvent.click(await screen.findByRole("option", { name: "共用任务" }));
    expect(screen.getByText("人岗匹配")).toBeInTheDocument();
    expect(screen.queryByText("抓取「前端」岗位")).not.toBeInTheDocument();
  });

  it("失败任务给「重试」，已结束任务可「删除」并从列表消失", async () => {
    renderPage();
    await screen.findByText("人岗匹配");

    // 只有失败态（t2）有「重试」
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/tasks/t2/retry",
        expect.objectContaining({ method: "POST" }),
      );
    });

    // 已结束态（t3）的「删除」：DOM 顺序里它排第一
    await userEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/tasks/t3",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
    expect(screen.queryByText("批量发送")).not.toBeInTheDocument();
  });

  it("在跑任务给「取消」→ /api/tasks/{id}/cancel", async () => {
    renderPage();
    await screen.findByText("抓取「前端」岗位");

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/tasks/t1/cancel",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("全部停止走 /api/tasks/cancel-all", async () => {
    renderPage();
    await screen.findByText("抓取「前端」岗位");
    await userEvent.click(screen.getByRole("button", { name: "全部停止" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/tasks/cancel-all",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("没有任务时给空态而不是空白", async () => {
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
    expect(await screen.findByText("还没有任何任务")).toBeInTheDocument();
  });
});

/** 筛选栏三个下拉都在——站点那个是多平台语义的关键分叉，不能只留类型/状态。 */
describe("TasksPage 筛选栏", () => {
  it("类型/状态/站点三个下拉齐全", async () => {
    renderPage();
    await screen.findByText("抓取「前端」岗位");
    for (const label of ["类型", "状态", "站点"]) {
      const box = screen.getByText(label).closest(".MuiFormControl-root") as HTMLElement;
      expect(within(box).getByRole("combobox")).toBeInTheDocument();
    }
  });
});

/** 生成任务段自 InterviewPage 迁来（2026-10-29）：面试题任务卡在这管理。 */
describe("TasksPage 面试题任务", () => {
  it("卡片带「题目」计数，管理动作走统一 /api/tasks/*", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/platforms")) {
          return new Response(JSON.stringify(PLATFORMS), { status: 200 });
        }
        if (url.endsWith("/api/tasks")) {
          return new Response(
            JSON.stringify({
              items: [
                {
                  task_id: "it1",
                  task_type: "interview",
                  site: "",
                  title: "面试题（1个岗位）",
                  status: "done",
                  current: "",
                  done: 1,
                  total: 1,
                  error: "",
                  created_at: 1700000400,
                  ended_at: 1700000500,
                  events: [],
                  raw: { question_total: 12 },
                },
              ],
            }),
            { status: 200 },
          );
        }
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }),
    );
    renderPage();
    await screen.findByText("面试题（1个岗位）");

    // 原「生成任务」卡的题目计数跟着迁过来（业务快照 raw.question_total）
    const panel = screen
      .getAllByRole("status")
      .find((p) => within(p).queryByText("面试题（1个岗位）")) as HTMLElement;
    expect(within(panel).getByText("12")).toBeInTheDocument();

    // 管理动作走任务中心统一口，不再打 /api/interview/task/*
    await userEvent.click(within(panel).getByRole("button", { name: "删除" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/tasks/it1",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });
});
