/** 工作台：便当格总览 */

import { api } from "../api.js";
import { fmtTime } from "../ui.js";

export async function renderHome(root) {
  root.innerHTML = `
    <h1 class="hero-title">星舰<span class="grad">控制台</span></h1>
    <p class="hero-sub">登录 · 抓取职位 · 简历匹配 · 模型调用 —— 四条航段，一张面板。</p>

    <div class="bento" id="bento">
      <div class="card span-5 lift" id="card-session">
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
        <div class="mt-16 muted mono" id="jobs-pages">流水 —</div>
        <div class="btn-row mt-16">
          <a class="btn" href="#/jobs">进入职位舱</a>
        </div>
      </div>

      <div class="card span-3 lift" id="card-crawl">
        <div class="card-head"><h3 class="card-title">抓取</h3></div>
        <div class="orb-ring" id="crawl-ring" style="--p:0"><div>0<small>PAGES</small></div></div>
        <div class="mt-16 muted" id="crawl-msg">待命</div>
      </div>

      <div class="card span-6 lift" id="card-llm">
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

      <div class="card span-6 lift" id="card-resume">
        <div class="card-head">
          <h3 class="card-title">简历匹配</h3>
          <span class="card-sub" id="an-sub">—</span>
        </div>
        <div id="an-list" class="muted">还没有分析记录</div>
        <div class="btn-row mt-16">
          <a class="btn primary" href="#/resume">上传简历开始分析</a>
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

  // 抓取进度
  let timer = setInterval(async () => {
    try {
      const s = await api.get("/api/crawl/status");
      const ring = $("crawl-ring");
      if (!s.task_id) {
        ring.innerHTML = `<div>0<small>PAGES</small></div>`;
        ring.style.setProperty("--p", 0);
        $("crawl-msg").textContent = "待命";
        return;
      }
      const pages = s.progress?.pages || 0;
      const maxPages = s.params?.max_pages || 10;
      const p = Math.min(100, Math.round((pages / maxPages) * 100));
      ring.style.setProperty("--p", s.status === "running" ? p : 100);
      ring.innerHTML = `<div>${pages}<small>${s.status.toUpperCase()}</small></div>`;
      $("crawl-msg").textContent =
        s.status === "running" ? `抓取中 · ${s.phase}` : s.stopped_reason || s.error || s.status;
    } catch { /* ignore */ }
  }, 1500);

  return () => clearInterval(timer);
}
