/** 工作台：便当格总览 + 实时任务状态 */

import { api } from "../api.js";
import { fmtTime, taskPanel, taskStateOf, renderEvents } from "../ui.js";

export async function renderHome(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">星舰<span class="grad">控制台</span></h1>
        <p class="hero-sub">登录 · 抓取职位 · 简历匹配 · 模型调用 —— 四条航段，一张面板。</p>
      </div>
      <div class="btn-row">
        <a class="btn primary" href="#/jobs">开始抓取</a>
        <a class="btn" href="#/match">去匹配</a>
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
          <span class="card-sub" id="jobs-sub">data/boss.db</span>
        </div>
        <div class="stat-num" id="jobs-num">…</div>
        <div class="stat-label">已入库职位</div>
        <div class="mt-12 muted mono" id="jobs-pages">流水 —</div>
        <div class="btn-row mt-16">
          <a class="btn" href="#/jobs">进入职位舱</a>
        </div>
      </div>

      <div class="card span-4 lift" id="card-llm">
        <div class="card-head">
          <h3 class="card-title">模型</h3>
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
          <a class="btn" href="#/llm">配置接口</a>
        </div>
      </div>

      <div class="card span-7">
        <div class="card-head">
          <h3 class="card-title">抓取任务</h3>
          <span class="card-sub" id="crawl-phase">idle</span>
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
          <a class="btn primary" href="#/match">进入匹配舱</a>
          <a class="btn" href="#/resume">简历库</a>
        </div>
      </div>

      <div class="card span-12">
        <div class="card-head">
          <h3 class="card-title">最近抓取流水</h3>
          <span class="card-sub">fetch_pages</span>
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
  const crawl = taskPanel({ title: "职位抓取", stopLabel: "停止抓取" });
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
      $("crawl-phase").textContent = s.status && s.task_id ? s.phase || s.status : "idle";
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
        current: s.status === "running" ? (s.phase || "抓取中") : (s.stopped_reason || s.error || ""),
        counts: [
          ["页", `${pages}${maxPages ? " / " + maxPages : ""}`],
          ["入库", `+${p.inserted || 0}`],
          ["更新", `${p.updated || 0}`],
          ["JD", `${p.desc_ok || 0} 成功 / ${p.desc_failed || 0} 失败`],
        ],
        error: s.status === "error" ? s.error : undefined,
        // crawl 事件的字段名是 event，不是 name
        log: renderEvents(s.events, { limit: 12, nameKey: "event" }),
      });
    } catch { /* ignore */ }
  }, 1500);

  return () => clearInterval(timer);
}
