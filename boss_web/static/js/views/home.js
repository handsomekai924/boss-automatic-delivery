/** 工作台：便当格总览 + 实时任务状态 */

import { api } from "../api.js";
import { fmtTime, taskPanel, taskStateOf, renderEvents, toast } from "../ui.js";

export async function renderHome(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">自动投递<span class="grad">助手</span></h1>
        <p class="hero-sub">登录 → 找职位 → 传简历 → 自动投递。四步都在这一页里，跟着下面的卡片走就行。</p>
      </div>
      <div class="btn-row">
        <a class="btn primary" href="#/jobs">开始找工作</a>
        <a class="btn" href="#/match">去看匹配</a>
      </div>
    </div>

    <div class="bento" id="bento">
      <div class="card span-4 lift" id="card-session">
        <div class="card-head">
          <h3 class="card-title">会话</h3>
          <span class="card-sub" id="sess-sub">—</span>
        </div>
        <div class="stat-num" id="sess-num">…</div>
        <div class="stat-label" id="sess-label">登录状态</div>
        <div class="btn-row mt-16">
          <a class="btn primary" href="#/login" id="btn-login">去登录</a>
          <a class="btn ghost hidden" href="#/login" id="btn-logout">退出登录</a>
        </div>
      </div>

      <div class="card span-4 lift" id="card-jobs">
        <div class="card-head">
          <h3 class="card-title">职位库</h3>
          <span class="card-sub" id="jobs-sub">本地保存</span>
        </div>
        <div class="stat-num" id="jobs-num">…</div>
        <div class="stat-label">已入库职位</div>
        <div class="mt-12 muted mono" id="jobs-pages">流水 —</div>
        <div class="btn-row mt-16">
          <a class="btn" href="#/jobs">去找工作</a>
        </div>
      </div>

      <div class="card span-4 lift" id="card-llm">
        <div class="card-head">
          <h3 class="card-title">AI 接口</h3>
          <span class="card-sub" id="llm-sub">OpenAI 兼容</span>
        </div>
        <div class="flex center gap-12">
          <span class="signal" id="llm-signal"></span>
          <div>
            <div style="font-size:16px;font-weight:650" id="llm-model">未配置</div>
            <div class="muted mono" id="llm-base" style="font-size:12px">—</div>
          </div>
        </div>
        <div class="btn-row mt-16">
          <a class="btn" href="#/llm">去设置</a>
        </div>
      </div>

      <div class="card span-7">
        <div class="card-head">
          <h3 class="card-title">找职位进度</h3>
          <span class="card-sub" id="crawl-phase">空闲</span>
        </div>
        <div id="home-crawl-task"></div>
      </div>

      <div class="card span-5 lift" id="card-resume">
        <div class="card-head">
          <h3 class="card-title">简历匹配</h3>
          <span class="card-sub" id="an-sub">—</span>
        </div>
        <div id="an-list" class="muted">还没有分析记录</div>
        <div class="btn-row mt-16">
          <a class="btn primary" href="#/match">去看匹配</a>
          <a class="btn" href="#/resume">我的简历</a>
        </div>
      </div>

      <div class="card span-12" id="card-env">
        <div class="card-head">
          <h3 class="card-title">环境自检</h3>
          <span class="card-sub" id="env-sub">—</span>
        </div>
        <div id="env-banners"></div>
        <div class="flex between center gap-12 wrap">
          <div class="flex center gap-12">
            <span class="signal" id="chrome-signal"></span>
            <div>
              <div id="chrome-text" style="font-size:14px;font-weight:650">检测中…</div>
              <div class="muted mono" id="chrome-path" style="font-size:12px">—</div>
            </div>
          </div>
          <div class="btn-row">
            <select class="select" id="chrome-mode" title="取安全令牌时 Chrome 窗口怎么摆"></select>
            <button class="btn" id="btn-recheck" type="button">重新检测</button>
            <button class="btn" id="btn-open-chrome" type="button">打开验证窗口</button>
          </div>
        </div>
        <div class="muted mt-12" id="env-data" style="font-size:12px">数据目录 —</div>
      </div>

      <div class="card span-12">
        <div class="card-head">
          <h3 class="card-title">最近的搜索记录</h3>
          <span class="card-sub">每次翻页的结果</span>
        </div>
        <div class="ticker" id="page-log"><div class="ev muted">—</div></div>
      </div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);

  // 会话
  api.get("/api/auth/status").then((st) => {
    const name = (st.user?.name || "").trim();
    $("sess-num").textContent = st.logged_in ? "在线" : "离线";
    $("sess-label").textContent = st.logged_in
      ? name || st.phone_masked || "已登录"
      : "未登录";
    $("sess-sub").textContent = st.saved_at ? fmtTime(st.saved_at) : "—";
    const primary = $("btn-login");
    const secondary = $("btn-logout");
    if (st.logged_in) {
      primary.textContent = "重新登录";
      secondary.classList.remove("hidden");
      secondary.onclick = async (e) => {
        e.preventDefault();
        await api.post("/api/auth/logout");
        location.reload();
      };
    } else {
      primary.textContent = "去登录";
      secondary.classList.add("hidden");
    }
  }).catch(() => {});

  // 职位
  api.get("/api/jobs/stats").then((s) => {
    $("jobs-num").textContent = s.jobs ?? 0;
    $("jobs-pages").textContent = `流水 ${s.pages ?? 0} 页 · 原始 ${s.raw_seen ?? 0} 条`;
    const log = $("page-log");
    if (s.pages_log && s.pages_log.length) {
      log.innerHTML = s.pages_log
        .map((p) => {
          const t = p.fetched_at ? fmtTime(p.fetched_at) : "";
          return `<div class="ev"><time>${t}</time><span class="name">page ${p.page}</span><span>raw ${p.raw_count} / kept ${p.kept_count} / +${p.inserted}</span></div>`;
        })
        .join("");
    }
  }).catch(() => {});

  // LLM
  api.get("/api/llm/config").then((c) => {
    const sig = $("llm-signal");
    if (c.configured) {
      sig.className = "signal ok";
      $("llm-model").textContent = c.model;
      $("llm-base").textContent = c.base_url;
    } else {
      sig.className = "signal warn";
    }
  }).catch(() => {});

  // 环境自检：数据存哪 / Chrome 在不在 / 取令牌的窗口要不要显示出来
  const MODE_LABELS = {
    hidden: "隐藏窗口（默认，不打扰）",
    visible: "显示窗口（要你手动过验证时选它）",
    offscreen: "窗口放到屏幕外",
    headless: "无界面",
  };

  function addBanner(kind, text, { chrome = false } = {}) {
    const box = document.createElement("div");
    box.className = "banner " + kind;
    box.style.whiteSpace = "pre-line";
    box.textContent = text; // 一律 textContent：服务端的兜底文案可能带原始异常
    if (chrome) box.dataset.chrome = "1";
    $("env-banners").appendChild(box);
  }

  api.get("/api/system/health").then((h) => {
    $("env-sub").textContent = h.frozen ? "打包版" : "源码运行";
    $("env-data").textContent = `数据目录 ${h.data_dir}`;
    if (h.temp_run) {
      addBanner(
        "bad",
        "程序是在临时目录里运行的——多半是直接在压缩包里双击了。\n" +
          "请先把程序解压到桌面再打开，否则简历和登录状态一关就没。"
      );
    }
    if (!h.writable) {
      addBanner("warn", "程序所在目录写不进去，数据已改存到上面这个目录（备份就拷贝它）。");
    }
  }).catch(() => {});

  async function refreshChrome() {
    const sel = $("chrome-mode");
    root.querySelectorAll('[data-chrome="1"]').forEach((n) => n.remove());
    try {
      const c = await api.get("/api/system/chrome");
      $("chrome-signal").className = "signal " + (c.found ? "ok" : "bad");
      $("chrome-text").textContent = c.found ? "已找到 Chrome，可以正常抓取" : "没找到 Chrome";
      $("chrome-path").textContent = c.path || "";
      if (!c.found) addBanner("bad", c.message, { chrome: true });
      if (!sel.options.length) {
        sel.innerHTML = c.modes
          .map((m) => `<option value="${m}">${MODE_LABELS[m] || m}</option>`)
          .join("");
      }
      sel.value = c.mode;
    } catch {
      $("chrome-signal").className = "signal bad";
      $("chrome-text").textContent = "检测失败";
      $("chrome-path").textContent = "";
    }
  }
  refreshChrome();

  $("btn-recheck").onclick = async () => {
    await refreshChrome();
    toast("已重新检测", "ok");
  };

  $("chrome-mode").onchange = async (e) => {
    try {
      await api.post("/api/system/chrome/mode", { mode: e.target.value });
      toast("窗口档位已切换，下次取令牌时生效", "ok");
    } catch (err) {
      toast(err.message || "切换失败", "bad");
    }
  };

  $("btn-open-chrome").onclick = async () => {
    try {
      const r = await api.post("/api/system/chrome/open");
      toast(r.message || "已打开", "ok");
    } catch (err) {
      toast(err.message || "打不开 Chrome", "bad");
    }
  };

  // 分析
  api.get("/api/resume/analyses").then((r) => {
    const items = r.items || [];
    $("an-sub").textContent = items.length ? `${items.length} 份` : "—";
    if (!items.length) return;
    $("an-list").innerHTML = items
      .slice(0, 3)
      .map(
        (a) =>
          `<div class="flex between center gap-8" style="margin-bottom:6px">
            <span>${a.resume_title || a.analysis_id}</span>
            <span class="pill accent">最高 ${a.top_score}</span>
          </div>`
      )
      .join("");
  }).catch(() => {});

  // 抓取任务：统一进度面板
  // 后端 phase 是英文内部阶段名，直接摆在页面上等于没翻译
  const PHASE_LABELS = {
    prepare: "准备中",
    client: "连接中",
    filter: "按条件筛选",
    stoken: "验证身份",
    crawl: "翻页搜索",
  };

  const crawl = taskPanel({ title: "职位搜索", stopLabel: "停止搜索" });
  $("home-crawl-task").appendChild(crawl.el);
  crawl.onStop(async () => {
    try {
      // 取消要带 task_id，没有统一的 /api/crawl/stop
      const s = await api.get("/api/crawl/status");
      if (s.task_id) await api.post(`/api/crawl/${s.task_id}/cancel`);
    } catch { /* ignore */ }
  });
  crawl.update({ status: "idle", percent: 0 });

  let timer = setInterval(async () => {
    try {
      const s = await api.get("/api/crawl/status");
      $("crawl-phase").textContent = s.status && s.task_id
        ? PHASE_LABELS[s.phase] || PHASE_LABELS[s.status] || s.phase || s.status
        : "空闲";
      if (!s.task_id) {
        crawl.update({ status: "idle", percent: 0 });
        return;
      }
      const p = s.progress || {};
      const pages = p.pages || 0;
      const maxPages = s.params?.max_pages || 0;
      const percent = maxPages > 0 ? Math.min(100, (pages / maxPages) * 100) : (s.status === "done" ? 100 : undefined);
      crawl.update({
        status: s.status,
        percent,
        current: s.status === "running"
          ? PHASE_LABELS[s.phase] || "进行中"
          : (s.stopped_reason || s.error || ""),
        counts: [
          ["页", `${pages}${maxPages ? " / " + maxPages : ""}`],
          ["新职位", `+${p.inserted || 0}`],
          ["更新", `${p.updated || 0}`],
          ["职位描述", `${p.desc_ok || 0} 成功 / ${p.desc_failed || 0} 失败`],
        ],
        error: s.status === "error" ? s.error : undefined,
        // crawl 事件的字段名是 event，不是 name
        log: renderEvents(s.events, { limit: 12, nameKey: "event" }),
      });
    } catch { /* ignore */ }
  }, 1500);

  return () => clearInterval(timer);
}
