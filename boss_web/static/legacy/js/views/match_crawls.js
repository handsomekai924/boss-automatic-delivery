/** 抓取任务：历史任务列表 + 详情弹窗 + 「匹配本批职位」入口 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, fmtTime } from "../ui.js";

/**
 * 渲染抓取任务目录：任务列表、详情弹窗、设为匹配范围。
 *
 * @param {HTMLElement} pane   内容容器
 * @param {Object}     state   页面级状态（本目录未直接改写）
 * @param {Object}     helpers 共享助手，用到 `setMatchScope`
 */
export async function renderCrawls(pane, state, helpers) {
  pane.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">抓取任务</h2>
        <div class="card-sub" id="crawl-sub">加载中…</div>
      </div>
      <button class="btn sm" id="btn-refresh-crawls">刷新</button>
    </div>
    <div id="crawl-list"></div>
  `;

  const $ = (id) => pane.querySelector("#" + id);
  $("btn-refresh-crawls").addEventListener("click", () => load());

  await load();

  /** 拉取最近 50 条抓取任务并渲染列表。 */
  async function load() {
    let data;
    try {
      data = await api.get("/api/crawl/tasks?limit=50");
    } catch (err) {
      $("crawl-sub").textContent = err.message || "加载失败";
      $("crawl-list").innerHTML = `<div class="banner bad">${escapeHtml(err.message || "加载失败")}</div>`;
      return;
    }
    const items = data.items || [];
    const running = items.filter((t) => t.status === "running" || t.live).length;
    $("crawl-sub").textContent = `共 ${data.total ?? items.length} 次${running ? ` · 进行中 ${running}` : ""}`;

    if (!items.length) {
      $("crawl-list").innerHTML = `
        <div class="empty">
          <div class="empty-icon">◌</div>
          <p>还没有抓取记录，去「拉取职位」开始第一次采集</p>
          <button class="btn sm mt-8" id="btn-goto-jobs">去拉取职位</button>
        </div>`;
      $("btn-goto-jobs")?.addEventListener("click", () => { location.hash = "#/jobs"; });
      return;
    }

    $("crawl-list").innerHTML = items.map((t) => {
      const params = t.params || {};
      const stats = t.stats || {};
      const sourceLabel = t.source === "expect" ? "期望流" : "搜索流";
      const cond = summarizeParams(params);
      const statusLabel = { running: "进行中", done: "已完成", error: "失败", cancelled: "已中断" }[t.status] || t.status;
      const statusPill = t.status === "running" ? "accent" : t.status === "error" ? "bad" : "static";
      const pages = stats.pages ?? params.max_pages ?? "—";
      const jobCount = t.job_count_existing ?? t.job_count ?? 0;
      const totalJobs = t.job_count ?? 0;
      return `
        <div class="card mb-12 lift" style="padding:14px 16px" data-tid="${escapeHtml(t.task_id)}">
          <div class="flex between center gap-12 wrap">
            <div>
              <div class="flex center gap-8">
                <strong>${escapeHtml(fmtTime(t.created_at))}</strong>
                <span class="pill static">${sourceLabel}</span>
                <span class="pill ${statusPill}">${statusLabel}</span>
              </div>
              <div class="muted" style="font-size:12px;margin-top:4px">${escapeHtml(cond)}</div>
              <div class="muted" style="font-size:11.5px;margin-top:2px">
                ${escapeHtml(String(pages))} 页 · 职位 ${totalJobs}（新增 ${t.inserted_count ?? 0} / 更新 ${t.updated_count ?? 0}）
                · 库中仍存 ${jobCount}
              </div>
              ${t.error ? `<div class="banner bad mt-8" style="font-size:12px">${escapeHtml(t.error)}</div>` : ""}
            </div>
            <div class="btn-row">
              <button class="btn sm primary" data-match="${escapeHtml(t.task_id)}" ${jobCount === 0 ? "disabled" : ""}>设为匹配范围</button>
              <button class="btn sm" data-detail="${escapeHtml(t.task_id)}">查看详情</button>
            </div>
          </div>
        </div>`;
    }).join("");

    $("crawl-list").querySelectorAll("[data-detail]").forEach((b) => {
      b.addEventListener("click", () => openDetail(b.dataset.detail));
    });
    $("crawl-list").querySelectorAll("[data-match]").forEach((b) => {
      b.addEventListener("click", () => openMatchConfirm(b.dataset.match));
    });
  }

  /**
   * 把任务启动参数拼成一行条件摘要。
   *
   * @param {Object} params 任务启动参数（keyword / city_name 等）
   * @returns {string} 形如「关键词 · 城市 · 最多 N 页」；无条件时为「默认条件」
   */
  function summarizeParams(params) {
    const parts = [];
    if (params.expect_kind || params.expect_name) {
      parts.push(`期望 ${params.expect_name || params.expect_kind}`);
    }
    if (params.keyword) parts.push(`关键词「${params.keyword}」`);
    if (params.city_name || params.city) parts.push(params.city_name || params.city);
    if (params.area_name) parts.push(params.area_name);
    if (params.max_pages) parts.push(`最多 ${params.max_pages} 页`);
    return parts.join(" · ") || "默认条件";
  }

  /**
   * 打开任务详情弹窗：启动参数快照、抓取统计、本批职位预览。
   *
   * @param {string} taskId 任务 ID
   */
  async function openDetail(taskId) {
    let t;
    try {
      t = await api.get(`/api/crawl/tasks/${encodeURIComponent(taskId)}`);
    } catch (err) {
      toast(err.message || "加载任务详情失败", "bad");
      return;
    }
    const params = t.params || {};
    const stats = t.stats || {};
    const paramsLines = Object.entries(params)
      .map(([k, v]) => `<li><span class="muted">${escapeHtml(k)}</span> ${escapeHtml(JSON.stringify(v))}</li>`)
      .join("");
    const statsLines = Object.entries(stats)
      .map(([k, v]) => `<li><span class="muted">${escapeHtml(k)}</span> ${escapeHtml(String(v))}</li>`)
      .join("");

    const dlg = modal({
      title: `抓取任务详情`,
      wide: true,
      body: `
        <div class="muted mb-8" style="font-size:12px">${escapeHtml(fmtTime(t.created_at))} · ${t.source === "expect" ? "期望流" : "搜索流"} · ${escapeHtml(t.status)}</div>
        <div class="card-sub mb-8">启动参数快照</div>
        <ul class="acc-body" style="font-size:12px">${paramsLines || "<li class='muted'>无</li>"}</ul>
        <div class="card-sub mb-8 mt-12">抓取统计</div>
        <ul class="acc-body" style="font-size:12px">${statsLines || "<li class='muted'>无</li>"}</ul>
        <div class="card-sub mb-8 mt-12">本批职位预览</div>
        <div id="task-jobs-preview" class="muted" style="font-size:12px">加载中…</div>
        <div class="btn-row mt-16">
          <button class="btn sm primary" id="dlg-match-batch">设为匹配范围</button>
          <button class="btn sm" id="dlg-copy-cond">复制条件摘要</button>
          <button class="btn sm ghost" id="dlg-close">关闭</button>
        </div>
      `,
    });

    dlg.box.querySelector("#dlg-close").addEventListener("click", () => dlg.close());
    dlg.box.querySelector("#dlg-match-batch").addEventListener("click", () => {
      dlg.close();
      openMatchConfirm(taskId);
    });
    dlg.box.querySelector("#dlg-copy-cond").addEventListener("click", () => {
      const text = summarizeParams(params);
      navigator.clipboard?.writeText(text).then(
        () => toast("条件摘要已复制", "ok"),
        () => toast("复制失败", "bad"),
      );
    });

    try {
      const jobs = await api.get(`/api/crawl/tasks/${encodeURIComponent(taskId)}/jobs?limit=10`);
      const host = dlg.box.querySelector("#task-jobs-preview");
      if (!host) return;
      const items = jobs.items || [];
      host.innerHTML = items.length
        ? items.map((j) => `<div style="padding:4px 0">${escapeHtml(j.job_name)} <span class="muted">@ ${escapeHtml(j.brand_name)}</span>${j.missing ? ` <span class="pill static bad">已删除</span>` : ""}</div>`).join("")
          + `<div class="muted mt-8">共 ${jobs.total ?? items.length} 条${jobs.existing != null ? `（库中仍存 ${jobs.existing}）` : ""}</div>`
        : `<div class="muted">本批没有职位记录</div>`;
    } catch {
      const host = dlg.box.querySelector("#task-jobs-preview");
      if (host) host.innerHTML = `<div class="banner bad">职位预览加载失败</div>`;
    }
  }

  /**
   * 确认后把本批职位设为匹配范围并切到工作台。
   *
   * @param {string} taskId 任务 ID
   */
  async function openMatchConfirm(taskId) {
    let t;
    try {
      t = await api.get(`/api/crawl/tasks/${encodeURIComponent(taskId)}`);
    } catch (err) {
      return toast(err.message || "加载任务失败", "bad");
    }
    const jobCount = t.job_count_existing ?? t.job_count ?? 0;
    if (jobCount === 0) return toast("本批职位已不在库中", "warn");

    const dlg = modal({
      title: "设为匹配范围",
      body: `
        <div class="muted mb-12" style="font-size:12.5px">
          将本次抓取的 <strong>${jobCount}</strong> 个职位设为匹配范围，切换到「匹配工作台」。<br>
          到工作台后可自行选择简历并点击「一键匹配当前范围」开始匹配。
        </div>
        <div class="btn-row mt-16">
          <button class="btn primary" id="batch-confirm">设为范围并切换</button>
          <button class="btn ghost" id="batch-cancel">取消</button>
        </div>
      `,
    });

    dlg.box.querySelector("#batch-cancel").addEventListener("click", () => dlg.close());
    dlg.box.querySelector("#batch-confirm").addEventListener("click", () => {
      dlg.close();
      helpers.setMatchScope(taskId, jobCount);
    });
  }
}
