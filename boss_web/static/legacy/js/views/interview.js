/** 面试题生成页：岗位列表（批量出题）+ 生成记录（详情弹窗）。 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtTime, debounce } from "../ui.js";
import { openQuestionDetail } from "../interview_prep.js";
import * as ws from "../ws.js";

const PAGE_SIZE = 20;

/**
 * 渲染「面试题生成」独立页。
 *
 * Tab 1 岗位列表：表格 + 抓取记录筛选 + 职位名模糊匹配 + 批量勾选出题。
 * Tab 2 生成记录：表格，详情进弹窗（左题型目录 / 右单题，上一题下一题）。
 *
 * @param {HTMLElement} root 挂载点
 */
export async function renderInterview(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">面试题 <span class="grad">生成</span></h1>
        <p class="hero-sub">按抓取批次或职位名挑一批岗位，批量出面试准备题；生成记录里可以逐题浏览、导出。</p>
      </div>
    </div>
    <div class="settings-shell">
      <aside class="card settings-nav" aria-label="面试题目录">
        <button class="settings-tab active" data-tab="jobs" type="button">岗位列表</button>
        <button class="settings-tab" data-tab="records" type="button">生成记录</button>
      </aside>
      <section class="card settings-pane" id="interview-pane"></section>
    </div>
  `;

  const pane = root.querySelector("#interview-pane");
  const state = {
    tab: "jobs",
    crawlTaskId: "",
    title: "",
    jobs: [],
    jobsTotal: 0,
    jobsPage: 0,
    selected: new Set(),
    crawlTasks: [],
    resumes: [],
    resumeId: "",
    records: [],
    recordsTotal: 0,
    recordsPage: 0,
    recordTitle: "",
    /** 在跑任务占用的岗位 id → task_id（这些岗位不允许再点生成；失败不占坑）。 */
    busyJobIds: new Map(),
    /** 上一轮在跑的任务 id，用来判断谁刚结束。 */
    knownRunning: new Set(),
  };
  let disposed = false;
  /** @type {?ReturnType<typeof setTimeout>} WS 事件触发刷新的节流排队。 */
  let refreshTimer = null;
  /** 上次刷新时刻，WS 事件节流用（500ms 内最多拉一次）。 */
  let lastRefreshAt = 0;

  /** 切 tab。 */
  function show(name) {
    state.tab = name;
    for (const btn of root.querySelectorAll(".settings-tab")) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    }
    if (name === "jobs") {
      renderJobsTab();
      loadJobs();
    } else {
      renderRecordsTab();
      loadRecords();
    }
  }

  /** 抓取批次下拉选项。 */
  function crawlOptions() {
    return (
      `<option value="">全部抓取记录</option>` +
      state.crawlTasks
        .map(
          (t) =>
            `<option value="${escapeHtml(t.task_id)}" ${
              state.crawlTaskId === t.task_id ? "selected" : ""
            }>批次 ${escapeHtml(fmtTime(t.created_at))} #${escapeHtml(
              String(t.task_id).slice(-6)
            )} · ${t.job_count_existing ?? t.job_count ?? "?"} 个职位</option>`
        )
        .join("")
    );
  }

  /** 简历下拉（没做匹配分析的岗位出题用）。 */
  function resumeOptions() {
    return (
      `<option value="">自动（优先用匹配分析的简历）</option>` +
      state.resumes
        .map(
          (r) =>
            `<option value="${escapeHtml(r.resume_id)}" ${
              state.resumeId === r.resume_id ? "selected" : ""
            }>${escapeHtml(r.title || r.resume_id)}</option>`
        )
        .join("")
    );
  }

  /** Tab 1：岗位列表表格。 */
  function renderJobsTab() {
    const pageCount = Math.max(1, Math.ceil(state.jobsTotal / PAGE_SIZE));
    const rows = state.jobs
      .map((j) => {
        const on = state.selected.has(j.encrypt_job_id);
        const qn = j.question_count || 0;
        return `
        <tr>
          <td class="col-check">
            <input type="checkbox" class="iv-check" data-id="${escapeHtml(j.encrypt_job_id)}" ${on ? "checked" : ""}>
          </td>
          <td>
            <div class="iv-title">${escapeHtml(j.job_name || "-")}</div>
            <div class="muted" style="font-size:12px">${escapeHtml(j.job_experience || "")}${
              j.job_degree ? " · " + escapeHtml(j.job_degree) : ""
            }</div>
          </td>
          <td>${escapeHtml(j.brand_name || "-")}</td>
          <td>
            <div>${escapeHtml(j.salary_desc || "-")}</div>
            <div class="muted" style="font-size:12px">${escapeHtml(j.location || "")}</div>
          </td>
          <td><span class="pill static">${escapeHtml(j.jd_status || "-")}</span></td>
          <td>
            ${
              qn
                ? '<span class="pill ok">已生成</span>'
                : '<span class="pill static">未生成</span>'
            }
          </td>
          <td class="col-actions">
            <button class="btn sm ghost iv-view" data-id="${escapeHtml(j.encrypt_job_id)}"
                    data-name="${escapeHtml(j.job_name || "")}"
                    data-brand="${escapeHtml(j.brand_name || "")}"
                    ${qn ? "" : "disabled"}>查看</button>
            <button class="btn sm iv-gen-one" data-id="${escapeHtml(j.encrypt_job_id)}"
                    data-name="${escapeHtml(j.job_name || "")}"
                    data-brand="${escapeHtml(j.brand_name || "")}"
                    ${state.busyJobIds.has(j.encrypt_job_id) ? "disabled" : ""}>${
                      state.busyJobIds.has(j.encrypt_job_id)
                        ? "任务中"
                        : "生成"
                    }</button>
          </td>
        </tr>`;
      })
      .join("");

    pane.innerHTML = `
      <div class="flex between center gap-12 wrap mb-12">
        <div class="flex gap-8 wrap" style="flex:1;min-width:220px">
          <select class="select" id="iv-crawl">${crawlOptions()}</select>
          <input class="input" id="iv-title" placeholder="职位名称模糊匹配" value="${escapeHtml(
            state.title
          )}" style="flex:1;min-width:160px">
          <button class="btn ghost" id="iv-search">查询</button>
        </div>
        <div class="btn-row">
          <select class="select" id="iv-resume">${resumeOptions()}</select>
          <button class="btn primary" id="iv-batch">批量生成面试题（<span id="iv-sel-count">${
            state.selected.size
          }</span>）</button>
        </div>
      </div>
      <div class="data-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th class="col-check"><input type="checkbox" id="iv-check-all"></th>
              <th>职位名称</th>
              <th>公司</th>
              <th>薪资 / 城市</th>
              <th>JD 状态</th>
              <th>面试题</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>${rows || `<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">没有符合条件的岗位</td></tr>`}</tbody>
        </table>
      </div>
      <div class="flex between center mt-12">
        <div class="muted" style="font-size:12px">共 ${state.jobsTotal} 条 · 第 ${
          state.jobsPage + 1
        }/${pageCount} 页</div>
        <div class="btn-row">
          <button class="btn sm ghost" id="iv-prev" ${state.jobsPage <= 0 ? "disabled" : ""}>上一页</button>
          <button class="btn sm ghost" id="iv-next" ${
            state.jobsPage + 1 >= pageCount ? "disabled" : ""
          }>下一页</button>
        </div>
      </div>`;

    const $ = (id) => pane.querySelector(id);
    $("#iv-crawl")?.addEventListener("change", (e) => {
      state.crawlTaskId = e.target.value;
      state.jobsPage = 0;
      loadJobs();
    });
    $("#iv-search")?.addEventListener("click", () => {
      state.title = $("#iv-title")?.value.trim() || "";
      state.jobsPage = 0;
      loadJobs();
    });
    $("#iv-title")?.addEventListener(
      "input",
      debounce(() => {
        state.title = $("#iv-title")?.value.trim() || "";
        state.jobsPage = 0;
        loadJobs();
      }, 400)
    );
    $("#iv-resume")?.addEventListener("change", (e) => {
      state.resumeId = e.target.value;
    });
    $("#iv-prev")?.addEventListener("click", () => {
      state.jobsPage = Math.max(0, state.jobsPage - 1);
      loadJobs();
    });
    $("#iv-next")?.addEventListener("click", () => {
      state.jobsPage += 1;
      loadJobs();
    });
    $("#iv-check-all")?.addEventListener("change", (e) => {
      if (e.target.checked) {
        for (const j of state.jobs) state.selected.add(j.encrypt_job_id);
      } else {
        for (const j of state.jobs) state.selected.delete(j.encrypt_job_id);
      }
      renderJobsTab();
    });
    $("#iv-batch")?.addEventListener("click", () => {
      const ids = [...state.selected].filter((id) => !state.busyJobIds.has(id));
      const skipped = state.selected.size - ids.length;
      if (!ids.length) {
        return toast(
          skipped ? "选中的岗位都已在任务列表里，请先到「任务中心」处理" : "请先勾选要生成的岗位",
          "warn"
        );
      }
      if (skipped) toast(`跳过 ${skipped} 个已在任务里的岗位`, "warn");
      startGenerate(ids);
    });

    const all = pane.querySelector("#iv-check-all");
    if (all) {
      all.checked =
        state.jobs.length > 0 && state.jobs.every((j) => state.selected.has(j.encrypt_job_id));
    }
  }
  /** Tab 2：生成记录表格。 */
  function renderRecordsTab() {
    const pageCount = Math.max(1, Math.ceil(state.recordsTotal / PAGE_SIZE));
    const rows = state.records
      .map(
        (r) => `
        <tr>
          <td>
            <div class="iv-title">${escapeHtml(r.job_name || r.encrypt_job_id)}</div>
          </td>
          <td>${escapeHtml(r.brand_name || "-")}</td>
          <td>${r.question_count}</td>
          <td>${r.question_count}</td>
          <td class="muted" style="font-size:12px">${escapeHtml(fmtTime(r.created_at))}</td>
          <td class="muted" style="font-size:12px">${escapeHtml(fmtTime(r.updated_at))}</td>
          <td class="col-actions">
            <button class="btn sm ghost iv-detail" data-id="${escapeHtml(r.encrypt_job_id)}"
                    data-name="${escapeHtml(r.job_name || "")}"
                    data-brand="${escapeHtml(r.brand_name || "")}"
                    data-analysis="${escapeHtml(r.analysis_id || "")}"
                    data-score="${escapeHtml(String(r.match_score ?? ""))}">详情</button>
            <button class="btn sm ghost iv-replay" data-id="${escapeHtml(r.encrypt_job_id)}"
                    data-name="${escapeHtml(r.job_name || "")}"
                    ${state.busyJobIds.has(r.encrypt_job_id) ? "disabled" : ""}>${
                      state.busyJobIds.has(r.encrypt_job_id) ? "任务中" : "重新生成"
                    }</button>
          </td>
        </tr>`
      )
      .join("");

    pane.innerHTML = `
      <div class="flex between center gap-12 wrap mb-12">
        <div class="flex gap-8 wrap" style="flex:1;min-width:220px">
          <input class="input" id="iv-rec-title" placeholder="职位名称模糊匹配" value="${escapeHtml(
            state.recordTitle
          )}" style="flex:1;min-width:160px">
          <button class="btn ghost" id="iv-rec-search">查询</button>
        </div>
      </div>
      <div class="data-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>职位名称</th>
              <th>公司</th>
              <th>题目数</th>
              <th>生成时间</th>
              <th>更新时间</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>${rows || `<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">还没有生成记录</td></tr>`}</tbody>
        </table>
      </div>
      <div class="flex between center mt-12">
        <div class="muted" style="font-size:12px">共 ${state.recordsTotal} 条 · 第 ${
          state.recordsPage + 1
        }/${pageCount} 页</div>
        <div class="btn-row">
          <button class="btn sm ghost" id="iv-rec-prev" ${
            state.recordsPage <= 0 ? "disabled" : ""
          }>上一页</button>
          <button class="btn sm ghost" id="iv-rec-next" ${
            state.recordsPage + 1 >= pageCount ? "disabled" : ""
          }>下一页</button>
        </div>
      </div>`;

    const $ = (id) => pane.querySelector(id);
    $("#iv-rec-search")?.addEventListener("click", () => {
      state.recordTitle = $("#iv-rec-title")?.value.trim() || "";
      state.recordsPage = 0;
      loadRecords();
    });
    $("#iv-rec-title")?.addEventListener(
      "input",
      debounce(() => {
        state.recordTitle = $("#iv-rec-title")?.value.trim() || "";
        state.recordsPage = 0;
        loadRecords();
      }, 400)
    );
    $("#iv-rec-prev")?.addEventListener("click", () => {
      state.recordsPage = Math.max(0, state.recordsPage - 1);
      loadRecords();
    });
    $("#iv-rec-next")?.addEventListener("click", () => {
      state.recordsPage += 1;
      loadRecords();
    });
  }

  /** 表格勾选（委托，只绑一次）。 */
  function onPaneChange(e) {
    const box = e.target.closest?.(".iv-check");
    if (!box) return;
    const id = box.dataset.id;
    if (box.checked) state.selected.add(id);
    else state.selected.delete(id);
    const cnt = pane.querySelector("#iv-sel-count");
    if (cnt) cnt.textContent = String(state.selected.size);
    const all = pane.querySelector("#iv-check-all");
    if (all) {
      all.checked =
        state.jobs.length > 0 && state.jobs.every((j) => state.selected.has(j.encrypt_job_id));
    }
  }

  /** 表格操作按钮（委托，只绑一次）。 */
  function onPaneClick(e) {
    const view = e.target.closest?.(".iv-view");
    if (view) {
      openDetail({
        encryptJobId: view.dataset.id,
        jobName: view.dataset.name,
        brandName: view.dataset.brand,
      });
      return;
    }
    const gen = e.target.closest?.(".iv-gen-one");
    if (gen) {
      startGenerate([gen.dataset.id]);
      return;
    }
    const d = e.target.closest?.(".iv-detail");
    if (d) {
      openDetail({
        encryptJobId: d.dataset.id,
        jobName: d.dataset.name,
        brandName: d.dataset.brand,
        analysisId: d.dataset.analysis,
      });
      return;
    }
    const r = e.target.closest?.(".iv-replay");
    if (r) startGenerate([r.dataset.id]);
  }

  /**
   * 打开题目详情弹窗。
   *
   * @param {{encryptJobId: string, jobName?: string, brandName?: string, analysisId?: string}} opts
   */
  function openDetail(opts) {
    openQuestionDetail({
      analysisId: opts.analysisId || "",
      encryptJobId: opts.encryptJobId,
      jobName: opts.jobName,
      brandName: opts.brandName,
      onChanged: () => {
        if (state.tab === "jobs") loadJobs();
        else loadRecords();
      },
    });
  }

  /**
   * 触发生成（单个或批量，可与其它任务并行）。
   *
   * @param {string[]} ids 职位 ID 列表
   */
  async function startGenerate(ids) {
    try {
      const task = await api.post("/api/interview/generate", {
        encrypt_job_ids: ids,
        resume_id: state.resumeId || "",
      });
      state.knownRunning.add(task.task_id);
      toast(`已开始生成 ${ids.length} 个岗位的面试题`, "ok");
      await refreshTasks();
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  /**
   * 拉一次任务并刷新占用态；任务从「在跑」变「结束」时提示并刷列表。
   * 由挂载、startGenerate 与 WS 事件（queueRefresh）触发，不再自己定时轮询。
   */
  async function refreshTasks() {
    if (disposed) return;
    lastRefreshAt = Date.now();
    try {
      const data = await api.get("/api/tasks?task_type=interview");
      const tasks = data.tasks || [];
      const busy = new Map();
      for (const t of tasks) {
        if (t.status !== "running") continue;
        for (const jid of (t.raw && t.raw.job_ids) || []) busy.set(String(jid), t.task_id);
      }
      const prevBusy = state.busyJobIds;
      state.busyJobIds = busy;

      const busyChanged =
        prevBusy.size !== state.busyJobIds.size ||
        [...state.busyJobIds.keys()].some((k) => !prevBusy.has(k));
      if (busyChanged) {
        if (state.tab === "jobs") renderJobsTab();
        else renderRecordsTab();
      }

      const nowRunning = new Set(
        tasks.filter((t) => t.status === "running").map((t) => t.task_id)
      );
      let justFinished = false;
      for (const t of tasks) {
        if (state.knownRunning.has(t.task_id) && !nowRunning.has(t.task_id)) {
          justFinished = true;
          const raw = t.raw || {};
          if (t.status === "done") toast(`生成完成：${raw.question_total || 0} 道题`, "ok");
          else if (t.status === "error") toast(t.error || "生成失败", "bad");
        }
      }
      state.knownRunning = nowRunning;
      if (justFinished) {
        if (state.tab === "jobs") await loadJobs();
        else await loadRecords();
      }
    } catch {
    }
  }

  /**
   * WS 事件触发的节流刷新：500ms 内最多拉一次。
   * 生成任务事件很密（每个岗位一条 progress），不节流会把 /api/tasks 打爆。
   */
  function queueRefresh() {
    if (disposed) return;
    const wait = 500 - (Date.now() - lastRefreshAt);
    if (wait <= 0) {
      refreshTasks();
      return;
    }
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      refreshTasks();
    }, wait);
  }

  /** 统一 WS 的任务事件：只认 interview 类型，重拉一次拿全量再重画。 */
  function onTaskEvent(m) {
    if (m.task_type !== "interview") return;
    queueRefresh();
  }

  /** 拉岗位列表。 */
  async function loadJobs() {
    try {
      const q = new URLSearchParams({
        crawl_task_id: state.crawlTaskId || "",
        title: state.title || "",
        limit: String(PAGE_SIZE),
        offset: String(state.jobsPage * PAGE_SIZE),
      });
      const data = await api.get(`/api/interview/jobs?${q}`);
      state.jobs = data.items || [];
      state.jobsTotal = data.total || 0;
    } catch (err) {
      toast("读岗位列表失败：" + err.message, "bad");
    }
    if (state.tab === "jobs") renderJobsTab();
  }

  /** 拉生成记录。 */
  async function loadRecords() {
    try {
      const q = new URLSearchParams({
        title: state.recordTitle || "",
        limit: String(PAGE_SIZE),
        offset: String(state.recordsPage * PAGE_SIZE),
      });
      const data = await api.get(`/api/interview/records?${q}`);
      state.records = data.items || [];
      state.recordsTotal = data.total || 0;
    } catch (err) {
      toast("读生成记录失败：" + err.message, "bad");
    }
    if (state.tab === "records") renderRecordsTab();
  }

  for (const btn of root.querySelectorAll(".settings-tab")) {
    btn.addEventListener("click", () => show(btn.dataset.tab));
  }
  pane.addEventListener("change", onPaneChange);
  pane.addEventListener("click", onPaneClick);

  const wsOffs = [
    ws.on("task.progress", onTaskEvent),
    ws.on("task.finished", onTaskEvent),
  ];
  ws.connect();

  try {
    const [ct, rs] = await Promise.all([
      api.get("/api/crawl/tasks?limit=100"),
      api.get("/api/resume/list"),
    ]);
    state.crawlTasks = ct.items || [];
    state.resumes = rs.items || [];
  } catch {
  }
  await loadJobs();
  refreshTasks();

  return () => {
    disposed = true;
    wsOffs.forEach((off) => off());
    if (refreshTimer) {
      clearTimeout(refreshTimer);
      refreshTimer = null;
    }
  };
}
