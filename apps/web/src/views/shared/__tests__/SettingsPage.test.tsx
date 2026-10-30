import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { ThemeModeProvider } from "../../../theme/ThemeModeProvider";
import { ToastProvider } from "../../../components/feedback/Toast";
import { PlatformProvider } from "../../../platform/PlatformProvider";
import { SettingsPage } from "../SettingsPage";
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

/** 配置快照：返回的是打码 Key（`has_key`/`fixed` 是 masked() 的附加字段）。 */
const LLM_CONFIG = {
  api_key: "sk-***abcd",
  has_key: true,
  base_url: "https://api.deepseek.com/v1",
  model: "deepseek-chat",
  configured: true,
  fixed: { temperature: 0.7, max_tokens: 2048, resume_parse_max_tokens: 8192, match_max_tokens: 4096, timeout: 120 },
};

const MODELS = {
  models: ["deepseek-chat", "deepseek-reasoner"],
  source: "https://api.deepseek.com/v1/models",
  selected: "deepseek-chat",
};

const LLM_TEST_OK = { ok: true, latency_ms: 88, model: "deepseek-chat", message: "正常，一切就绪" };

const UPDATE_STATUS = {
  ok: true,
  current: "1.2.3",
  latest: "1.2.4",
  has_update: true,
  should_prompt: true,
  ignored: null,
  notes: "修复了一批问题",
  can_self_update: true,
  zip_asset: { name: "app.zip", url: "https://example.com/app.zip" },
  checked_at: 1700000000,
  error: null,
};

const UPDATE_LOG = {
  ok: true,
  hint: "（没有日志）",
  files: [{ name: "update.log", exists: true, size: 1024, truncated: false, content: "[2026-01-01] 下载完成" }],
  diagnostics: {
    current: "1.2.3",
    latest: "1.2.4",
    has_update: true,
    should_prompt: true,
    ignored: null,
    error: null,
    checked_at: 1700000000,
    install_mode_label: "安装版（可自动更新）",
    can_self_update: true,
    zip_asset: { name: "app.zip", url: "https://example.com/app.zip" },
    last_stage: "",
    last_stage_text: "",
  },
};

/** 两条规则：r1 启用（全站 `site=''`）、r2 停用（本站专属），启停与归属徽标各自可认。 */
const RULES = {
  items: [
    {
      rule_id: "r1",
      rule_type: "company",
      pattern: "某外包公司",
      match_mode: "exact",
      scope: "",
      description: "全是外包",
      enabled: true,
      hit_count: 3,
      last_error: "",
      site: "",
      created_at: "2026-01-01 00:00:00",
      updated_at: "2026-01-01 00:00:00",
    },
    {
      rule_id: "r2",
      rule_type: "jd_keyword",
      pattern: "996",
      match_mode: "contains",
      scope: "both",
      description: "",
      enabled: false,
      hit_count: 0,
      last_error: "",
      site: "boss",
      created_at: "2026-01-02 00:00:00",
      updated_at: "2026-01-02 00:00:00",
    },
  ],
};

const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });

/** 按 URL 分流的 fetch 假实现；URL 全等分流，别用 includes 误匹配子路径。 */
function stubFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/platforms") return json(PLATFORMS);
    if (url === "/api/llm/models") return json(MODELS);
    if (url === "/api/llm/test") return json(LLM_TEST_OK);
    if (url === "/api/llm/config") return json(LLM_CONFIG);
    if (url === "/api/system/update/log") return json(UPDATE_LOG);
    if (url === "/api/system/update" || url.startsWith("/api/system/update?")) return json(UPDATE_STATUS);
    if (url === "/api/sites/boss/blacklist/rules") return json(RULES);
    if (url.includes("/blacklist/rules") && method === "POST") {
      return json({ item: { ...RULES.items[0], enabled: false } });
    }
    if (url.includes("/blacklist/rules") && method === "DELETE") return json({ deleted: true });
    return json({ ok: true });
  });
}

function renderPage() {
  return render(
    <ThemeModeProvider>
      <ToastProvider>
        <MemoryRouter initialEntries={["/settings"]}>
          <PlatformProvider>
            <SettingsPage />
          </PlatformProvider>
        </MemoryRouter>
      </ToastProvider>
    </ThemeModeProvider>,
  );
}

/** 取 fetch 假实现的调用流水（URL + init），断言请求体用。 */
function fetchCalls(): [string, RequestInit | undefined][] {
  return (globalThis.fetch as unknown as Mock).mock.calls.map(([u, init]) => [
    String(u),
    init as RequestInit | undefined,
  ]);
}

beforeEach(() => {
  resetStores();
  globalThis.localStorage?.clear();
  vi.stubGlobal("fetch", stubFetch());
});

describe("SettingsPage LLM 配置", () => {
  it("三项输入都在，保存走 PUT /api/llm/config（Key 留空不覆盖）", async () => {
    renderPage();

    // type=password 也是 role=textbox，别去拿 searchbox
    expect(screen.getByLabelText("API Key")).toBeInTheDocument();
    expect(screen.getByLabelText("接口地址")).toBeInTheDocument();
    expect(screen.getByLabelText("模型")).toBeInTheDocument();

    // 等配置回填完再存，否则 model 还是空的（空模型不让保存）
    expect(await screen.findByDisplayValue("https://api.deepseek.com/v1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/llm/config",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    const put = fetchCalls().find(([u, init]) => u === "/api/llm/config" && init?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({
      base_url: "https://api.deepseek.com/v1",
      model: "deepseek-chat",
    });
  });

  it("「拉取模型」走 POST /api/llm/models，api_key 只进请求体不进 URL", async () => {
    renderPage();
    expect(await screen.findByDisplayValue("https://api.deepseek.com/v1")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("API Key"), "sk-test-12345678");
    await userEvent.click(screen.getByRole("button", { name: "拉取模型" }));

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/llm/models",
        expect.objectContaining({ method: "POST" }),
      );
    });

    // 全等断言：一次都不许把 Key 拼进 URL（连 query 都不该有）
    const modelCalls = fetchCalls().filter(([u]) => u.includes("llm/models"));
    expect(modelCalls.length).toBeGreaterThan(0);
    for (const [u] of modelCalls) expect(u).toBe("/api/llm/models");

    const withKey = modelCalls.find(([, init]) => String(init?.body ?? "").includes("sk-test-12345678"));
    expect(withKey).toBeDefined();
    expect(JSON.parse(String(withKey?.[1]?.body)).api_key).toBe("sk-test-12345678");

    // 拉回来的模型进了下拉
    await userEvent.click(screen.getByLabelText("模型"));
    expect(await screen.findByRole("option", { name: "deepseek-reasoner" })).toBeInTheDocument();
  });

  it("「测试连通」把返回的 message 原样显示出来", async () => {
    renderPage();
    expect(await screen.findByDisplayValue("https://api.deepseek.com/v1")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "测试连通" }));
    expect(await screen.findByText("正常，一切就绪")).toBeInTheDocument();
  });

  it("「测试连通」请求在飞时按钮转「测试中…」并禁用，防连点重复测", async () => {
    // 用 deferred promise 卡住 /api/llm/test，制造「请求在飞」窗口
    let resolveTest!: (r: Response) => void;
    const testGate = new Promise<Response>((res) => { resolveTest = res; });
    const base = stubFetch();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/llm/test") return testGate;
        return base(input);
      }),
    );
    renderPage();
    expect(await screen.findByDisplayValue("https://api.deepseek.com/v1")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "测试连通" }));
    expect(screen.getByRole("button", { name: "测试中…" })).toBeDisabled();

    resolveTest(json({ ok: true, message: "正常，一切就绪" }));
    expect(await screen.findByText("正常，一切就绪")).toBeInTheDocument();
  });

  it("测试失败也是 200 带人话：message 走 danger 行内提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/platforms") return json(PLATFORMS);
        if (url === "/api/llm/test") return json({ ok: false, message: "余额不足，请充值后再试" });
        if (url === "/api/llm/config") return json(LLM_CONFIG);
        return json({ ok: true });
      }),
    );
    renderPage();
    expect(await screen.findByDisplayValue("https://api.deepseek.com/v1")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "测试连通" }));
    expect(await screen.findByText("余额不足，请充值后再试")).toBeInTheDocument();
  });
});

describe("SettingsPage 程序更新", () => {
  it("检查更新走 GET /api/system/update", async () => {
    renderPage();
    await screen.findByDisplayValue("https://api.deepseek.com/v1");

    await userEvent.click(screen.getByRole("radio", { name: "程序更新" }));
    expect(await screen.findByText("当前版本")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "检查更新" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/system/update?force=true",
        expect.objectContaining({ method: "GET" }),
      );
    });

    // 进页面就查一次更新状态（页签小圆点用），走同一支接口不带 force
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/system/update",
      expect.objectContaining({ method: "GET" }),
    );
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/system/update/log",
      expect.objectContaining({ method: "GET" }),
    );
  });
});

describe("SettingsPage 黑名单规则", () => {
  it("规则列表按当前平台渲染，「停用」走 toggle 接口", async () => {
    renderPage();
    await screen.findByDisplayValue("https://api.deepseek.com/v1");

    await userEvent.click(screen.getByRole("radio", { name: "黑名单规则" }));
    expect(await screen.findByText("某外包公司")).toBeInTheDocument();
    expect(screen.getByText("996")).toBeInTheDocument();
    // 归属徽标：`site` 空 = 全站，非空 = 本站专属（后端 2026-10-28 起回传）
    expect(screen.getByText("全站")).toBeInTheDocument();
    expect(screen.getByText("本站")).toBeInTheDocument();

    // 站点作用域来自 usePlatform().activeSiteId，不写站点判断
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/sites/boss/blacklist/rules",
      expect.objectContaining({ method: "GET" }),
    );

    await userEvent.click(screen.getByRole("button", { name: "停用" }));
    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        "/api/sites/boss/blacklist/rules/r1/toggle",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const toggle = fetchCalls().find(([u]) => u === "/api/sites/boss/blacklist/rules/r1/toggle");
    expect(JSON.parse(String(toggle?.[1]?.body))).toEqual({ enabled: false });
  });
});

describe("SettingsPage 外观", () => {
  it("浅色/深色切换走 setMode（主题根节点跟着换）", async () => {
    renderPage();
    await screen.findByDisplayValue("https://api.deepseek.com/v1");

    await userEvent.click(screen.getByRole("radio", { name: "外观" }));

    // 先点深色再点浅色：不依赖初始模式，两次都看得见效果。
    // 持久化（localStorage orbit-theme）由 ThemeModeProvider 负责，
    // 但这个 jsdom 环境里 localStorage 整个不可用，只能验 data-theme 这条同样走它 effect 的线。
    await userEvent.click(await screen.findByRole("radio", { name: "深色" }));
    await waitFor(() => {
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    });
    await userEvent.click(screen.getByRole("radio", { name: "浅色" }));
    await waitFor(() => {
      expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    });
  });
});
