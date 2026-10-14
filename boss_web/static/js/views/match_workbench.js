/** 匹配工作台：简历选择 + 范围条 + 匹配按钮 + 双栏（岗位列表｜匹配结果）+ 详情弹窗 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, scoreRing, taskPanel, renderEvents } from "../ui.js";

const PAGE_SIZE = 20;

export function renderWorkbench(pane, state, helpers) {
  const {
    resumes, currentResumeId, jobs, totalJobs, page, selected,
    matches, analysisId, scope,
  } = state;
  const {
    setMatchButtons, paintMatch, renderMatches, renderJobs, updateSelCount,
    startMatch, openMatchDetail, deliverLabel, loadResumes, updateLlmBadge,
    matchPanelHost,
  } = helpers;

  pane.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div class="flex center gap-12 wrap">
        <label class="muted" style="font-size:12.5px">简历</label>
        <select class="select" id="resume-select" style="width:220px"></select>
      </div>
      <div class="btn-row">
        <button class="btn primary" id="btn-match-all">一键匹配当前范围</button>
        <button class="btn" id="btn-match-sel">匹配选中</button>
      </div>
    </div>
    <div id="scope-bar" class="flex center gap-8 wrap mb-12"></div>
    <div id="match-task" class="mb-16"></div>
    <div class="bento">
      <div class="card span-5 panel-col">
        <div class="card-head">
          <h3 class="card-title">岗位列表</h3>
          <span class="card-sub" id="jobs-sub">—</span>
        </div>
        <div class="flex between center mb-12">
          <div class="btn-row">
            <button class="btn sm ghost" id="btn-select-page">全选本页</button>
            <button class="btn sm ghost" id="btn-clear-sel">清空</button>
          </div>
          <span class="pill accent" id="sel-count">已选 0</span>
        </div>
        <div id="job-list" class="scroll-y"></div>
        <div class="flex between center mt-12">
          <button class="btn sm ghost" id="btn-prev">上一页</button>
          <span class="muted mono" id="page-info" style="font-size:12px">—</span>
          <button class="btn sm ghost" id="btn-next">下一页</button>
        </div>
      </div>
      <div class="card span-7 panel-col" id="results-card">
        <div class="card-head">
          <h3 class="card-title">匹配结果</h3>
          <div class="flex center gap-8">
            <span class="card-sub" id="an-sub">尚未匹配</span>
            <button class="btn sm ghost" id="btn-clear-analysis" ${state.analysisId || state.matches.length ? "" : "hidden"}>清空</button>
          </div>
        </div>
        <div id="an-results" class="scroll-y">
          <div class="empty"><div class="empty-icon">◌</div><p>启动匹配后，结果会按分数从高到低排在这里</p></div>
        </div>
      </div>
    </div>
  `;

  const $ = (id) => pane.querySelector("#" + id);

  // 挂载匹配任务面板
  if (matchPanelHost) $("match-task").appendChild(matchPanelHost);

  // 简历下拉
  const sel = $("resume-select");
  sel.innerHTML =
    `<option value="">— 选择简历 —</option>` +
    resumes.map((it) => {
      const parsed = it.llm && it.llm.has_data ? " · 已解析" : " · 未解析";
      return `<option value="${escapeHtml(it.resume_id)}">${escapeHtml(it.title || it.resume_id)}${parsed}</option>`;
    }).join("");
  sel.value = currentResumeId || "";
  sel.addEventListener("change", () => {
    state.currentResumeId = sel.value;
  });

  // 范围条
  renderScopeBar();

  // 岗位列表
  renderJobs();

  // 匹配结果
  renderMatches();

  // 按钮
  $("btn-match-all").addEventListener("click", () => startMatch([]));
  $("btn-match-sel").addEventListener("click", () => {
    if (!selected.size) return toast("先在左侧勾选岗位", "warn");
    startMatch([...selected]);
  });
  $("btn-clear-analysis")?.addEventListener("click", () => helpers.clearAnalysis());
  $("btn-select-page").addEventListener("click", () => {
    jobs.forEach((j) => selected.add(j.encrypt_job_id));
    renderJobs();
  });
  $("btn-clear-sel").addEventListener("click", () => {
    selected.clear();
    renderJobs();
  });
  $("btn-prev").addEventListener("click", () => {
    if (state.page > 0) {
      state.page -= 1;
      helpers.loadJobs();
    }
  });
  $("btn-next").addEventListener("click", () => {
    if ((state.page + 1) * PAGE_SIZE < state.totalJobs) {
      state.page += 1;
      helpers.loadJobs();
    }
  });
  // 初始翻页按钮状态
  const prevBtn = $("btn-prev");
  const nextBtn = $("btn-next");
  if (prevBtn) prevBtn.disabled = state.page <= 0;
  if (nextBtn) nextBtn.disabled = (state.page + 1) * PAGE_SIZE >= state.totalJobs;

  function renderScopeBar() {
    const bar = $("scope-bar");
    if (!bar) return;
    const sc = state.scope;
    if (!sc || sc.type === "all") {
      bar.innerHTML = `<span class="pill static">全部职位</span>`;
      return;
    }
    const label = sc.type === "crawl_task"
      ? `抓取任务 #${escapeHtml(String(sc.crawl_task_id || "").slice(-6))} · ${sc.job_count ?? "?"} 个职位`
      : `已选 ${sc.job_ids?.length || 0} 个职位`;
    bar.innerHTML = `
      <span class="pill accent">${label}</span>
      <button class="btn sm ghost" id="btn-clear-scope">清除范围</button>
    `;
    bar.querySelector("#btn-clear-scope")?.addEventListener("click", () => {
      state.scope = { type: "all" };
      helpers.loadJobs();
      renderScopeBar();
    });
  }

  // 暴露给壳层：外部（匹配本批职位）改了 scope 后可以重绘范围条
  helpers._renderScopeBar = renderScopeBar;
}
