/** 简历优化：四步向导（选简历 → 选岗位 → 生成 → 对比保存）+ 修改记录列表。 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, fmtTime, taskPanel, taskStateOf, renderEvents } from "../ui.js";
import { openJobDetail } from "../job_detail.js";

const MAX_TARGET = 5;
const JOB_PAGE_SIZE = 20;
const CHANGE_TYPE_LABEL = {
  rewrote: "改写",
  reordered: "重排",
  trimmed: "精简",
  expanded: "扩写",
  keyword: "关键词对齐",
};

/**
 * 简历优化向导主入口：整块替换简历页的双面工作区。
 *
 * @param {HTMLElement} root   挂载容器
 * @param {{resumeId: string}} ctx 初始上下文（当前简历 id）
 * @returns {() => void}       卸载函数
 */
export function renderRevise(root, ctx = {}) {
  const $ = (id) => root.querySelector("#" + id);

  if (window.__reviseCleanup) {
    try { window.__reviseCleanup(); } catch {}
  }

  root.innerHTML = `
    <div class="revise" id="revise-app">
      <div id="revise-stage"></div>
    </div>
  `;

  /**
   * 向导内保留的选择。
   *
   * @property {string} mode 优化模式：`targeted` | `free`
   * @property {string} targetKind 目标范围：`jobs` | `crawl_task` | `all`
   * @property {?string} taskId    任务 id，同时也是 revision_id
   */
  const state = {
    mode: ctx.mode || "targeted",
    resumeId: ctx.resumeId || "",
    resumeTitle: "",
    resumeRaw: "",
    jobIds: new Set(),
    targetKind: "jobs",
    crawlTaskId: "",
    allRepo: false,
    format: "md",
    taskId: null,
    revision: null,
    timer: 0,
    completed: false,
    sections: [],
    selectedSections: new Set(),
    presets: null,
    optimizationPrompt: "",
  };

  const panel = taskPanel({ title: "简历优化", stopLabel: "停止" });
  panel.update({ status: "idle", percent: 0 });

  /** 步骤指示轨高亮（与登录页 `track` 同一套视觉：on/done）。 */
  function setStep(n) {
    root.querySelectorAll("#track .node").forEach((node) => {
      const s = Number(node.dataset.step);
      node.classList.toggle("on", s === n);
      node.classList.toggle("done", s < n);
    });
  }

  /** 事件流水 → 面板日志 HTML。 */
  function logHtml(events) {
    return renderEvents(events || [], { limit: 20 });
  }

  /** 每个阶段卡片的公共头部（header）：返回 / 步骤轨 / 修改记录。 */
  function header(step = 1, mode = "targeted") {
    const stateClass = (s) => step > s ? "done" : step === s ? "on" : "";
    const steps = mode === "free"
      ? ['选简历', '选章节', '生成', '对比保存']
      : ['选简历', '选岗位', '生成', '对比保存'];
    const node = steps.map((s, i) => `<div class="node ${stateClass(i+1)}" data-step="${i + 1}"><i></i><span>${s}</span></div>`).join("")
    return `
      <div class="rv-top column">
        <div class="button-row">
          <button class="btn sm ghost" id="revise-back">← 返回简历</button>
          <button class="btn sm" id="rv-history-btn">修改记录</button>
        </div>
        <div class="track revise-track" id="track">
          ${node}
        </div>
      </div>
    `;
  }

  /** 取简历详情；失败回 `null`。 */
  async function loadResumeDetail(id) {
    try {
      return await api.get("/api/resume/item/" + id);
    } catch {
      return null;
    }
  }

  /** 第 1 步：选简历和优化模式。 */
  function renderStage1() {
    const stage = $("revise-stage");
    stage.innerHTML = `
      <div class="card rv-card">
        <div class="rv-top">${header(1, state.mode)}</div>
        <div class="rv-label">第 1 步 · 选简历与优化模式</div>
        <div class="flex center gap-8 mb-12">
          <span class="muted">优化模式</span>
          <label class="flex center gap-4"><input type="radio" name="rv-mode" value="targeted" ${state.mode === "targeted" ? "checked" : ""}><span>定向优化（基于目标岗位）</span></label>
          <label class="flex center gap-4"><input type="radio" name="rv-mode" value="free" ${state.mode === "free" ? "checked" : ""}><span>自由优化（选择章节）</span></label>
        </div>
        <select class="select" id="rv-resume" style="width:320px"></select>
        <div class="rv-raw"><pre class="rs-raw" id="rv-raw-text">…</pre></div>
        <div class="muted" style="font-size:12.5px">修改以这份【原文】为底稿，不是解析结果。原简历永不覆盖。</div>
        <div class="btn-row mt-12"><button class="btn primary" id="rv-next1">下一步</button></div>
      </div>
    `;

    root.querySelectorAll("input[name=rv-mode]").forEach((radio) => {
      radio.addEventListener("change", () => {
        state.mode = radio.value;
        const btn = $("rv-next1");
        btn.textContent = state.mode === "free" ? "下一步：选章节" : "下一步：选岗位";
        setStep(1);
        const track = root.querySelector("#track");
        if (track) {
          const steps = state.mode === "free" ? ['选简历', '选章节', '生成', '对比保存'] : ['选简历', '选岗位', '生成', '对比保存'];
          const nodes = track.querySelectorAll(".node span");
          nodes.forEach((n, i) => { if (steps[i]) n.textContent = steps[i]; });
        }
      });
    });

    (async () => {
      const items = await api.get("/api/resume/list").then((r) => r.items || []).catch(() => []);
      const sel = $("rv-resume");
      sel.innerHTML = items
        .map((it) => `<option value="${escapeHtml(it.resume_id)}">${escapeHtml(it.title || it.resume_id)}</option>`)
        .join("");
      if (state.resumeId && items.some((x) => x.resume_id === state.resumeId)) sel.value = state.resumeId;
      else if (items.length) sel.value = items[0].resume_id;
      const pick = async () => {
        state.resumeId = sel.value;
        state.jobIds.clear();
        state.selectedSections.clear();
        state.revision = null;
        const d = await loadResumeDetail(sel.value);
        state.resumeRaw = (d && d.raw) || "";
        state.resumeTitle = (d && d.title) || sel.value;
        $("rv-raw-text").textContent = state.resumeRaw || "（空简历）";
      };
      sel.addEventListener("change", pick);
      if (sel.value) await pick();
    })();
    $("rv-next1").addEventListener("click", () => goStep(2));
  }

  /** 第 2 步：根据模式显示岗位选择或章节选择。 */
  function renderStage2() {
    if (state.mode === "free") {
      renderStage2Free();
    } else {
      renderStage2Targeted();
    }
  }

  /** 第 2 步（自由模式）：选择章节和提示词。 */
  function renderStage2Free() {
    const stage = $("revise-stage");
    stage.innerHTML = `
      <div class="card rv-card">
        <div class="rv-top">${header(2, state.mode)}</div>
        <div class="rv-label">第 2 步 · 选择章节与优化提示词</div>

        <div class="mb-12">
          <div class="muted mb-8">选择需要优化的章节（留空 = 全部）</div>
          <div id="rv-sections" class="scroll-y" style="max-height:200px;border:1px solid var(--border);border-radius:4px;padding:8px"></div>
        </div>

        <div class="mb-12">
          <div class="flex between center mb-8">
            <span class="muted">优化提示词</span>
            <select class="select" id="rv-preset-cat" style="width:160px"><option value="">选择预设分类…</option></select>
          </div>
          <div id="rv-preset-list" class="mb-8" style="display:none"></div>
          <textarea class="input" id="rv-prompt" rows="4" placeholder="输入自定义提示词或选择上方预设"></textarea>
        </div>

        <div class="btn-row mt-12">
          <button class="btn" id="rv-back2">上一步</button>
          <button class="btn primary" id="rv-next2">下一步：生成</button>
        </div>
      </div>
    `;

    // 加载章节列表
    (async () => {
      try {
        const data = await api.get(`/api/resume/item/${state.resumeId}/sections`);
        state.sections = data.sections || [];
        const host = $("rv-sections");
        if (!state.sections.length) {
          host.innerHTML = `<div class="muted">未检测到章节标题</div>`;
          return;
        }
        host.innerHTML = state.sections.map((s, i) => `
          <label class="flex center gap-4" style="padding:4px 0">
            <input type="checkbox" data-idx="${i}" ${state.selectedSections.has(s.title) ? "checked" : ""}>
            <span style="font-size:13px">${escapeHtml(s.title)}</span>
            <span class="muted" style="font-size:11px">H${s.level}</span>
          </label>
        `).join("");
        host.querySelectorAll("input[type=checkbox]").forEach(cb => {
          cb.addEventListener("change", () => {
            const idx = Number(cb.dataset.idx);
            const title = state.sections[idx]?.title;
            if (cb.checked) state.selectedSections.add(title);
            else state.selectedSections.delete(title);
          });
        });
      } catch (err) {
        $("rv-sections").innerHTML = `<div class="muted">加载章节失败</div>`;
      }
    })();

    // 加载预设提示词
    (async () => {
      try {
        const data = await api.get("/api/resume/revise/presets");
        state.presets = data.presets || {};
        const categories = data.categories || [];
        const catSel = $("rv-preset-cat");
        catSel.innerHTML = `<option value="">选择预设分类…</option>` +
          categories.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");

        catSel.addEventListener("change", () => {
          const cat = catSel.value;
          const list = $("rv-preset-list");
          if (!cat) {
            list.style.display = "none";
            return;
          }
          const items = state.presets[cat] || [];
          list.style.display = "block";
          list.innerHTML = items.map((p, i) => `
            <button class="btn sm ghost" data-cat="${escapeHtml(cat)}" data-idx="${i}" style="margin:2px">
              ${escapeHtml(p.name)}
            </button>
          `).join("");
          list.querySelectorAll("button").forEach(btn => {
            btn.addEventListener("click", () => {
              const idx = Number(btn.dataset.idx);
              const prompt = state.presets[btn.dataset.cat][idx]?.prompt || "";
              $("rv-prompt").value = prompt;
              state.optimizationPrompt = prompt;
            });
          });
        });
      } catch (err) {
        toast("加载预设失败", "warn");
      }
    })();

    $("rv-prompt").addEventListener("input", (e) => {
      state.optimizationPrompt = e.target.value;
    });

    $("rv-back2").addEventListener("click", () => goStep(1));
    $("rv-next2").addEventListener("click", () => {
      if (!state.optimizationPrompt.trim()) {
        return toast("请输入或选择优化提示词", "warn");
      }
      goStep(3);
    });
  }

  /** 第 2 步（定向模式）：勾选目标岗位（手动 / 批次 / 全库范围）。 */
  function renderStage2Targeted() {
    const stage = $("revise-stage");
    stage.innerHTML = `
      <div class="card rv-card">
        <div class="rv-top">${header(2)}</div>
        <div class="rv-label" id="rv-label-n">第 2 步 · 选目标岗位（0/${MAX_TARGET}）</div>
        <div class="flex center gap-8 wrap mb-12">
          <select class="select" id="rv-batch" style="width:280px"><option value="">载入爬取批次…</option></select>
          <button class="btn sm ghost" id="rv-clear">清空</button>
        </div>
        <div id="rv-joblist" class="scroll-y rv-joblist"></div>
        <div class="btn-row mt-12">
          <button class="btn" id="rv-back2">上一步</button>
          <button class="btn primary" id="rv-next2">下一步：生成</button>
        </div>
      </div>
    `;

    let jobs = [];
    let totalJobs = 0;
    let page = 0;
    let loadingMore = false;
    let dead = false;
    /** @type {{type: "jobs"|"crawl_task"|"all", crawl_task_id?: string}|null} 候选职位范围。 */
    let scope = null;
    const setLabel = (n) => {
      const el = $("rv-label-n");
      if (el) el.textContent = `第 2 步 · 选目标岗位（${n}/${MAX_TARGET}）`;
    };

    const renderList = () => {
      const host = $("rv-joblist");
      host.innerHTML = jobs
        .map((j) => {
          const id = j.encrypt_job_id;
          const on = state.jobIds.has(id);
          const noJd = state.targetKind === "jobs" && !(j.job_desc || "") && j.jd_status !== "completed";
          return `
            <label class="flex center gap-8 rv-jrow${noJd ? " is-nojd" : ""}" data-id="${escapeHtml(id)}">
              <input type="checkbox" data-id="${escapeHtml(id)}" ${on ? "checked" : ""} ${state.jobIds.size >= MAX_TARGET && !on ? "disabled" : ""}>
              <span style="flex:1;min-width:0">
                <span style="font-size:13px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                  ${escapeHtml(j.job_name)} <span class="muted">@ ${escapeHtml(j.brand_name)}</span>
                </span>
                <span class="muted" style="font-size:11px">${escapeHtml(j.salary_desc || "")} · ${escapeHtml(j.location || "")}</span>
                ${noJd ? `<span class="pill warn static">无 JD，只按职位名 / 技能参与</span>` : ""}
              </span>
              <button type="button" class="btn sm ghost rv-detail-btn">详情</button>
            </label>`;
        })
        .join("") ||
        (dead ? `<div class="muted">没有更多职位了</div>` : `<div class="muted">没匹配的职位</div>`);

      host.querySelectorAll("input[type=checkbox]").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) {
            if (state.jobIds.size >= MAX_TARGET) return toast(`最多选 ${MAX_TARGET} 个`, "warn");
            state.jobIds.add(cb.dataset.id);
          } else {
            state.jobIds.delete(cb.dataset.id);
          }
          setLabel(state.jobIds.size);
          renderList();
        });
      });
      host.querySelectorAll(".rv-detail-btn").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          const row = btn.closest(".rv-jrow");
          const id = row && row.dataset.id;
          if (!id) return;
          openJobDetail(id, {
            onDeleted: () => {
              state.jobIds.delete(id);
              setLabel(state.jobIds.size);
              renderList();
            },
          });
        });
      });
    };

    /** 按当前 scope 拉一页职位，追加到列表（滚动触底分页）。 */
    const loadMore = async () => {
      if (loadingMore || dead) return;
      loadingMore = true;
      try {
        let items = [];
        let total = 0;
        if (scope && scope.type === "crawl_task") {
          const d = await api.get(`/api/crawl/tasks/${scope.crawl_task_id}/jobs?limit=${JOB_PAGE_SIZE}&offset=${page * JOB_PAGE_SIZE}`);
          items = (d.items || []).filter((x) => !x.missing);
          total = d.existing ?? d.total ?? 0;
        } else if (scope && scope.type === "all") {
          const d = await api.get(`/api/jobs?limit=${JOB_PAGE_SIZE}&offset=${page * JOB_PAGE_SIZE}`);
          items = d.items || [];
          total = d.total ?? 0;
        } else {
          const d = await api.get(`/api/jobs?limit=${JOB_PAGE_SIZE}&offset=${page * JOB_PAGE_SIZE}`);
          items = (d.items || []).map((j) => ({ ...j }));
          total = d.total ?? 0;
        }
        jobs.push(...items);
        totalJobs = total;
        page += 1;
        if (totalJobs > 0 && jobs.length >= totalJobs) dead = true;
        renderList();
      } catch (err) {
        toast(err.message || "加载岗位失败", "bad");
      } finally {
        loadingMore = false;
      }
    };

    const host = $("rv-joblist");
    host.addEventListener("scroll", () => {
      if (loadingMore || dead) return;
      if (host.scrollTop + host.clientHeight >= host.scrollHeight - 40) loadMore();
    });

    (async () => {
      dead = false;
      await loadMore();
    })();

    (async () => {
      const sel = $("rv-batch");
      try {
        const b = await api.get("/api/crawl/tasks?limit=100");
        const items = b.items || [];
        sel.innerHTML =
          `<option value="__all__">全部职位</option>` +
          items
            .slice(0, 50)
            .map(
              (t) =>
                `<option value="${escapeHtml(t.task_id)}">批次 ${escapeHtml(fmtTime(t.created_at))} #${escapeHtml(
                  String(t.task_id).slice(-6)
                )} · ${t.job_count_existing ?? t.job_count ?? "?"} 个职位</option>`
            )
            .join("");
        sel.addEventListener("change", async () => {
          const v = sel.value;
          if (v === "__all__") {
            scope = { type: "all" };
            state.crawlTaskId = "";
            state.targetKind = "all";
          } else if (v) {
            scope = { type: "crawl_task", crawl_task_id: v };
            state.crawlTaskId = v;
            state.targetKind = "crawl_task";
          } else {
            scope = null;
            state.crawlTaskId = "";
            state.targetKind = "jobs";
          }
          jobs = [];
          page = 0;
          totalJobs = 0;
          dead = false;
          host.scrollTop = 0;
          await loadMore();
        });
      } catch {
        sel.innerHTML = `<option value="__all__">全部职位（从库中所有岗位里挑选）</option><option value="">不限批次（手动挑选，最多 ${MAX_TARGET} 个）</option>`;
      }
    })();

    $("rv-clear").addEventListener("click", () => {
      state.jobIds.clear();
      setLabel(0);
      renderList();
    });
    $("rv-back2").addEventListener("click", () => goStep(1));
    $("rv-next2").addEventListener("click", () => {
      if (!state.jobIds.size) return toast(`最多选 ${MAX_TARGET} 个岗位`, "warn");
      goStep(3);
    });
  }

  /** 第 3 步：启动简历优化长任务并轮询进度。 */
  function renderStage3() {
    const stage = $("revise-stage");
    const labelText = state.mode === "free"
      ? "第 3 步 · 生成优化修改"
      : `第 3 步 · 生成针对性修改（${state.jobIds.size} 个岗位）`;
    stage.innerHTML = `
      <div class="card rv-card">
        <div class="rv-top">${header(3, state.mode)}</div>
        <div class="rv-label">${labelText}</div>
        <div id="rv-task"></div>
      </div>
    `;
    $("rv-task").appendChild(panel.el);
    panel.update({ status: "idle", percent: 0 });

    const start = async () => {
      panel.update({ status: "running", percent: 0, current: "启动…" });
      try {
        let r;
        if (state.mode === "free") {
          r = await api.post("/api/resume/revise/free-start", {
            resume_id: state.resumeId,
            selected_sections: [...state.selectedSections],
            optimization_prompt: state.optimizationPrompt,
          });
        } else {
          r = await api.post("/api/resume/revise/start", {
            resume_id: state.resumeId,
            job_ids: [...state.jobIds],
            crawl_task_id: state.targetKind === "crawl_task" ? state.crawlTaskId : "",
            all_repo: state.targetKind === "all",
          });
        }
        state.taskId = r.task_id;
        toast("已启动，正在生成…", "ok");
        poll();
      } catch (err) {
        panel.update({ status: "error", error: err.message || "启动失败" });
        toast(err.message || "启动失败", "bad");
      }
    };

    const poll = async () => {
      clearInterval(state.timer);
      state.timer = setInterval(async () => {
        if (!state.taskId) return clearInterval(state.timer);
        try {
          const s = await api.get("/api/resume/revise/status");
          if (s.task_id && s.task_id !== state.taskId) {
            panel.update({ status: "idle", error: "有另一个简历优化任务在跑", current: "" });
            return clearInterval(state.timer);
          }
          if (!s.task_id) return clearInterval(state.timer);
          const st = taskStateOf(s.status);
          panel.update({
            status: st,
            percent: s.done ? 100 : undefined,
            current: s.current || "",
            error: s.error || undefined,
            counts: [["岗位", String(s.job_count ?? "")]],
            log: logHtml(s.events),
          });
          if (s.done || s.status === "done") {
            clearInterval(state.timer);
            loadRevisionResult();
          } else if (/error|cancelled/.test(s.status)) {
            clearInterval(state.timer);
            setStep(3);
          }
        } catch {}
      }, 1200);
    };

    panel.onStop(async () => {
      if (!state.taskId) return;
      try {
        await api.post(`/api/resume/revise/${state.taskId}/cancel`);
        toast("已请求停止", "warn");
      } catch (err) {
        toast(err.message || "停止失败", "bad");
      }
    });

    // 自动开始生成
    start();
  }

  /** 载入草稿详情进对比。 */
  async function loadRevisionResult() {
    try {
      const d = await api.get(`/api/resume/revisions/${state.taskId}`);
      state.revision = d;
      state.fetchedDiff = d.diff || [];
      state.completed = false;
      goStep(4);
    } catch (err) {
      toast(err.message || "载入草稿失败", "bad");
      setStep(3);
    }
  }

  /** 第 4 步：对比修改稿并确认保存 / 导出。 */
  function renderStage4() {
    const stage = $("revise-stage");
    const rev = state.revision || {};
    const changes = rev.changes || [];
    const diff = (state.fetchedDiff || rev.diff) || [];
    const saved = rev.status === "saved" || rev.saved_resume_id;

    stage.innerHTML = `
      <div class="card rv-card">
        <div class="rv-top">${header(4)}</div>
        <div class="rv-label">第 4 步 · 对比确认</div>
        <div class="rv-diffview">${diffHtml(diff)}</div>
        <div class="rv-changes">
          <div class="muted mb-8">变更点清单（${changes.length}）</div>
          ${changes
            .map(
              (c) => `
            <div class="rv-change">
              <span class="pill static">${escapeHtml(c.section || "—")}</span>
              <span class="pill accent">${escapeHtml(CHANGE_TYPE_LABEL[c.type] || c.type)}</span>
              <span>${escapeHtml(c.summary || "")}</span>
              <span class="muted" style="font-size:12px">${escapeHtml(c.reason || "")}</span>
            </div>`
            )
            .join("") || `<div class="muted">无变更点说明</div>`}
        </div>
        <div class="flex center gap-8 wrap mt-12">
          <span class="muted" style="font-size:12.5px">导出格式</span>
          <select class="select" id="rv-format" style="width:110px">
            <option value="md" ${state.format === "md" ? "selected" : ""}>Markdown</option>
            <option value="txt" ${state.format === "txt" ? "selected" : ""}>纯文本</option>
          </select>
          ${saved ? "" : `<button class="btn" id="rv-regenerate">重新生成</button>`}
          <button class="btn" id="rv-export">导出</button>
          ${saved ? "" : `<button class="btn danger" id="rv-discard">放弃</button>`}
          ${saved ? "" : `<button class="btn primary" id="rv-save">确认保存</button>`}
          ${saved ? `<span class="pill ok">已保存</span>` : ""}
        </div>
      </div>
    `;

    $("rv-format").addEventListener("change", (e) => {
      state.format = e.target.value;
    });

    $("rv-export").addEventListener("click", () => {
      const fmt = state.format === "txt" ? "txt" : "md";
      window.open(`/api/resume/revisions/${state.taskId}/download?format=${fmt}`, "_blank");
    });

    if (!saved) {
      $("rv-regenerate").addEventListener("click", async () => {
        try {
          const r = await api.post(`/api/resume/revisions/${state.taskId}/regenerate`);
          state.taskId = r.task_id;
          toast("已重新生成，正在处理…", "ok");
          goStep(3);
          renderStage3();
          await poll();
        } catch (err) {
          toast(err.message || "重新生成失败", "bad");
        }
      });

      $("rv-discard").addEventListener("click", async () => {
        if (!confirm("放弃这份修改稿？")) return;
        try {
          await api.del(`/api/resume/revisions/${state.taskId}`);
          toast("已放弃", "ok");
          state.taskId = null;
          state.revision = null;
          state.completed = false;
          goStep(2);
        } catch (err) {
          toast(err.message || "放弃失败", "bad");
        }
      });

      $("rv-save").addEventListener("click", () => {
        const m = modal({
          title: "确认保存修改稿",
          body: `
            <div class="muted mb-12">将写盘为文件，并入库为一份新简历。原简历不改。</div>
            <label class="rv-save-row">
              <span>入库标题</span>
              <input class="input" id="rv-save-title" value="${escapeHtml((rev.resume_title || "简历") + "-定向")}">
            </label>
            <label class="rv-save-row">
              <span>文件名</span>
              <input class="input" id="rv-save-fname" value="${escapeHtml(rev.resume_title || "resume")}">
            </label>
            <div class="btn-row mt-12 justify-end">
              <button class="btn primary" id="rv-save-ok">保存</button>
              <button class="btn ghost" id="rv-save-cancel">取消</button>
            </div>
          `,
        });
        m.bodyEl.querySelector("#rv-save-ok").addEventListener("click", async () => {
          const title = m.bodyEl.querySelector("#rv-save-title").value;
          const fname = m.bodyEl.querySelector("#rv-save-fname").value;
          try {
            const r = await api.post(`/api/resume/revisions/${state.taskId}/save`, {
              format: state.format,
              filename: fname,
              title,
            });
            m.close();
            toast("已保存", "ok");
            if (state.revision) {
              state.revision.saved_path = r.saved_path;
              state.revision.saved_resume_id = r.saved_resume_id;
              state.revision.status = "saved";
            }
            renderStage4();
          } catch (err) {
            toast(err.message || "保存失败：文件写入或入库出错", "bad");
          }
        });
        m.bodyEl.querySelector("#rv-save-cancel").addEventListener("click", () => m.close());
      });
    }
  }

  const app = $("revise-app");
  app.addEventListener("click", (e) => {
    const back = e.target.closest("#revise-back");
    if (back) {
      window.location.hash = "#/resume";
      setTimeout(() => {
        import("../app.js").then((m) => m.navigate && m.navigate());
      }, 0);
      return;
    }
    const hist = e.target.closest("#rv-history-btn");
    if (hist) openHistoryModal();
  });

  /** 切到第 n 步并渲染对应阶段；对比完成后只准重开第 1 步。 */
  function goStep(n) {
    if (state.completed) {
      if (n !== 1) return;
      state.completed = false;
      state.taskId = null;
      state.revision = null;
    }
    setStep(n);
    if (n === 1) renderStage1();
    else if (n === 2) renderStage2();
    else if (n === 3) renderStage3();
    else renderStage4();
  }

  /** 打开「修改记录」弹窗。 */
  function openHistoryModal() {
    const m = modal({
      title: "修改记录",
      wide: true,
      body: `<div id="rv-history-list" class="scroll-y" style="max-height:60vh"></div>`,
    });
    renderHistoryIn(m.bodyEl);
  }

  /** 往 `host` 渲染修改记录列表，并绑载入 / 下载 / 删除。 */
  async function renderHistoryIn(host) {
    host.querySelectorAll("[data-load],[data-dl],[data-del]").forEach((el) => el.remove());
    try {
      const r = await api.get("/api/resume/revisions?limit=50");
      const items = r.items || [];
      const statusCls = (s) => (s === "saved" ? "ok" : s === "draft" ? "accent" : s === "error" ? "warn" : "static");
      const list = host.querySelector("#rv-history-list") || host;
      list.innerHTML = items
        .map(
          (it) => `
        <div class="rv-rec">
          <div class="flex between center">
            <span class="mono">${escapeHtml(String(it.created_at ? fmtTime(it.created_at) : ""))}</span>
            <span class="pill ${statusCls(it.status)}">${escapeHtml(it.status)}</span>
          </div>
          <div class="muted" style="font-size:12px">底稿「${escapeHtml(it.resume_title || "")}」 · ${it.job_count ?? 0} 个岗位 · ${escapeHtml(it.model || "")}</div>
          <div class="btn-row">
            <button class="btn sm ghost" data-load="${escapeHtml(it.revision_id)}">载入</button>
            <button class="btn sm ghost" data-dl="${escapeHtml(it.revision_id)}">下载</button>
            <button class="btn sm ghost danger" data-del="${escapeHtml(it.revision_id)}">删</button>
          </div>
        </div>`
        )
        .join("") || `<div class="muted">还没有修改记录</div>`;

      list.querySelectorAll("[data-load]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            const d = await api.get(`/api/resume/revisions/${b.dataset.load}`);
            state.revision = d;
            state.taskId = d.revision_id;
            state.resumeId = d.resume_id;
            state.resumeRaw = d.original_md || "";
            state.resumeTitle = d.resume_title || "";
            modalCloseAll();
            goToCompareOnly();
          } catch (err) {
            toast(err.message || "载入失败", "bad");
          }
        })
      );
      list.querySelectorAll("[data-dl]").forEach((b) =>
        b.addEventListener("click", () => {
          window.open(`/api/resume/revisions/${b.dataset.dl}/download?format=${state.format}`, "_blank");
        })
      );
      list.querySelectorAll("[data-del]").forEach((b) =>
        b.addEventListener("click", async () => {
          if (!confirm("删除这条修改记录？已写盘文件会保留。")) return;
          try {
            await api.del(`/api/resume/revisions/${b.dataset.del}`);
            renderHistoryIn(host);
          } catch (err) {
            toast(err.message || "删除失败", "bad");
          }
        })
      );
    } catch {
      const list = host.querySelector("#rv-history-list") || host;
      list.innerHTML = `<div class="muted">修改记录加载失败</div>`;
    }
  }

  /** 从历史载入后只展示对比，不再回上一步。 */
  function goToCompareOnly() {
    state.completed = true;
    setStep(4);
    renderStage4();
  }

  goStep(1);

  const cleanup = () => {
    if (state.timer) clearInterval(state.timer);
    window.__reviseCleanup = null;
  };
  window.__reviseCleanup = cleanup;
  return cleanup;
}

/** diff 行块 → HTML（markdown diff：只展示增删改动，省略未改动行）。 */
function diffHtml(blocks) {
  if (!blocks || !blocks.length) return `<div class="muted">无对比内容</div>`;
  const parts = [];
  for (const b of blocks) {
    const tag = b.tag || "equal";
    const left = escapeHtml(b.left || "");
    const right = escapeHtml(b.right || "");
    if (tag === "delete") {
      parts.push(`<div class="d-row d-del"><span class="d-marker">−</span><span class="d-text">${left}</span></div>`);
    } else if (tag === "equal") {
      parts.push(`<div class="d-row d-equal"><span class="d-marker">&nbsp;</span><span class="d-text">${left}</span></div>`);
    } else if (tag === "insert") {
      parts.push(`<div class="d-row d-ins"><span class="d-marker">+</span><span class="d-text">${right}</span></div>`);
    } else {
      if (left) parts.push(`<div class="d-row d-del"><span class="d-marker">−</span><span class="d-text">${left}</span></div>`);
      if (right) parts.push(`<div class="d-row d-ins"><span class="d-marker">+</span><span class="d-text">${right}</span></div>`);
    }
  }
  if (!parts.length) return `<div class="muted">无改动</div>`;
  return parts.join("");
}

/** 关掉页面上所有弹窗。 */
function modalCloseAll() {
  document.querySelectorAll(".modal-backdrop").forEach((b) => b.remove());
  document.querySelectorAll(".modal").forEach((m) => m.remove());
}
