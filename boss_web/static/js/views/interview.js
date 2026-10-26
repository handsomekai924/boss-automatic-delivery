/** 面试题生成页：岗位列表（批量出题）+ 生成记录（详情弹窗）。 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtTime, debounce, modal } from "../ui.js";
import { openQuestionDetail } from "../interview_prep.js";

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
      <div class="btn-row">
        <button class="btn ghost" id="iv-task-center">任务中心<span class="pill accent hidden" id="iv-tc-badge" style="margin-left:6px"></span></button>
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
    /** 在跑 / 失败任务占用的岗位 id → task_id（这些岗位不允许再点生成）。 */
    busyJobIds: new Map(),
    runningCount: 0,
    /** 上一轮在跑的任务 id，用来判断谁刚结束。 */
    knownRunning: new Set(),
  };
  /** @type {ReturnType<typeof setTimeout>|null} */
  let pollTimer = null;
  let disposed = false;
  /** 任务中心弹窗开着时的重画回调；空 = 没开。 */
  let taskCenterRender = null;

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
   * 画任务中心弹窗内容：在跑 + 失败任务（进度 / 停止 / 全部停止 / 失败删除重试）。
   *
   * @param {HTMLElement} host 弹窗内容节点
   * @param {Array<object>} tasks 任务快照列表
   */
  function paintTaskCenter(host, tasks) {
    const rows = (tasks || []).filter(
      (t) => t.status === "running" || t.status === "error"
    );
    const runningCount = rows.filter((t) => t.status === "running").length;
    if (!rows.length) {
      host.innerHTML = `
        <div class="empty"><div class="empty-icon">◎</div><p>当前没有进行中或失败的任务</p></div>`;
      return;
    }
    const list = rows
      .map((t) => {
        const pct = t.total ? Math.round(((t.done || 0) / t.total) * 100) : 0;
        return `
        <div class="iv-task-row" data-id="${escapeHtml(t.task_id)}">
          <div class="flex between center gap-8">
            <div class="card-sub" style="min-width:0">
              ${escapeHtml(t.current || (t.status === "error" ? "生成失败" : "正在生成面试题…"))}
              <span class="pill ${t.status === "running" ? "accent" : "bad"}">${
                t.status === "running" ? "进行中" : "失败"
              }</span>
            </div>
            <div class="btn-row">
              ${
                t.status === "running"
                  ? `<button class="btn sm danger iv-tc-cancel" data-id="${escapeHtml(t.task_id)}">停止</button>`
                  : `<button class="btn sm ghost iv-tc-del" data-id="${escapeHtml(t.task_id)}">删除</button>
                     <button class="btn sm primary iv-tc-retry" data-id="${escapeHtml(t.task_id)}">重试</button>`
              }
            </div>
          </div>
          <div class="progress ${t.status === "error" ? "bad" : ""} mt-8"><i style="width:${pct}%"></i></div>
          <div class="muted mt-8" style="font-size:12px">
            ${t.done || 0} / ${t.total || 0} 个岗位 · ${(t.job_ids || []).length} 个目标
            ${t.status === "running" ? "" : t.status === "error" && t.error ? ` · ${escapeHtml(t.error)}` : ""}
          </div>
        </div>`;
      })
      .join("");
    host.innerHTML = `
      <div class="flex between center mb-12">
        <div class="card-sub">生成任务 <span class="pill accent">${runningCount} 个进行中</span></div>
        ${
          runningCount
            ? '<button class="btn sm ghost" id="iv-cancel-all">全部停止</button>'
            : ""
        }
      </div>
      <div class="iv-task-list">${list}</div>`;
  }

  /** 打开任务中心弹窗：在跑任务进度 + 全部停止；失败的可删除 / 重试。弹窗开着时随轮询刷新。 */
  function openTaskCenter() {
    const dlg = modal({
      title: "任务中心",
      wide: true,
      body: `<div class="muted">正在读取任务…</div>`,
      onClose: () => {
        if (taskCenterRender === myRender) taskCenterRender = null;
      },
    });
    // 同一时刻只留一个任务中心；旧的关掉时别把新的回调清掉。
    const myRender = (tasks) => paintTaskCenter(dlg.bodyEl, tasks);

    /** 拉一次任务并画。 */
    async function render() {
      try {
        const data = await api.get("/api/interview/tasks");
        const tasks = data.tasks || [];
        state.runningCount = data.running_count || 0;
        state.busyJobIds = new Map(Object.entries(data.busy_job_ids || {}));
        paintTaskCenter(dlg.bodyEl, tasks);
        updateTcBadge();
      } catch (err) {
        dlg.bodyEl.innerHTML = `<div class="muted">读取失败：${escapeHtml(err.message)}</div>`;
      }
    }

    dlg.bodyEl.addEventListener("click", async (e) => {
      const cancel = e.target.closest?.(".iv-tc-cancel");
      if (cancel) {
        try {
          await api.post(`/api/interview/task/${encodeURIComponent(cancel.dataset.id)}/cancel`);
          toast("已请求停止", "warn");
          await render();
        } catch (err) {
          toast(err.message, "bad");
        }
        return;
      }
      if (e.target.closest?.("#iv-cancel-all")) {
        try {
          await api.post("/api/interview/tasks/cancel");
          toast("已请求停止全部", "warn");
          await render();
        } catch (err) {
          toast(err.message, "bad");
        }
        return;
      }
      const del = e.target.closest?.(".iv-tc-del");
      if (del) {
        try {
          await api.del(`/api/interview/task/${encodeURIComponent(del.dataset.id)}`);
          toast("已删除", "ok");
          await render();
        } catch (err) {
          toast(err.message, "bad");
        }
        return;
      }
      const retry = e.target.closest?.(".iv-tc-retry");
      if (retry) {
        try {
          await api.post(
            `/api/interview/task/${encodeURIComponent(retry.dataset.id)}/retry`
          );
          toast("已重新提交", "ok");
          await render();
        } catch (err) {
          toast(err.message, "bad");
        }
      }
    });

    // 轮询拿到快照后直接重画弹窗，不再另拉接口。
    taskCenterRender = myRender;
    render();
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

  /** 画页头「任务中心」角标（在跑任务数）。 */
  function updateTcBadge() {
    const badge = root.querySelector("#iv-tc-badge");
    if (!badge) return;
    const n = state.runningCount || 0;
    if (n > 0) {
      badge.classList.remove("hidden");
      badge.textContent = String(n);
    } else {
      badge.classList.add("hidden");
    }
  }

  /** 拉全部任务并重画；有在跑的就继续轮询。 */
  async function refreshTasks() {
    if (disposed) return;
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    try {
      const data = await api.get("/api/interview/tasks");
      const tasks = data.tasks || [];
      const runningCount = data.running_count || 0;
      const prevBusy = state.busyJobIds;
      state.runningCount = runningCount;
      state.busyJobIds = new Map(Object.entries(data.busy_job_ids || {}));
      updateTcBadge();
      if (taskCenterRender) taskCenterRender(tasks);

      // 占用岗位变了 → 重画表格（生成按钮禁用态跟着变）
      const busyChanged =
        prevBusy.size !== state.busyJobIds.size ||
        [...state.busyJobIds.keys()].some((k) => !prevBusy.has(k));
      if (busyChanged) {
        if (state.tab === "jobs") renderJobsTab();
        else renderRecordsTab();
      }

      // 刚从「在跑」变「结束」的任务：提示 + 刷列表
      const nowRunning = new Set(
        tasks.filter((t) => t.status === "running").map((t) => t.task_id)
      );
      let justFinished = false;
      for (const t of tasks) {
        if (state.knownRunning.has(t.task_id) && !nowRunning.has(t.task_id)) {
          justFinished = true;
          if (t.status === "done") toast(`生成完成：${t.question_total || 0} 道题`, "ok");
          else if (t.status === "error") toast(t.error || "生成失败", "bad");
        }
      }
      state.knownRunning = nowRunning;
      if (justFinished) {
        if (state.tab === "jobs") await loadJobs();
        else await loadRecords();
      }
    } catch {
      /* 拿不到就下一轮再试 */
    }
    if (disposed) return;
    pollTimer = setTimeout(refreshTasks, 1500);
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
  root.querySelector("#iv-task-center")?.addEventListener("click", () => openTaskCenter());
  pane.addEventListener("change", onPaneChange);
  pane.addEventListener("click", onPaneClick);

  // 首屏：抓取批次 + 简历 + 任务角标 + 岗位列表
  try {
    const [ct, rs] = await Promise.all([
      api.get("/api/crawl/tasks?limit=100"),
      api.get("/api/resume/list"),
    ]);
    state.crawlTasks = ct.items || [];
    state.resumes = rs.items || [];
  } catch {
    /* 列表拉不到就空着，主表还能看 */
  }
  await loadJobs();
  refreshTasks();

  return () => {
    disposed = true;
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  };
}
