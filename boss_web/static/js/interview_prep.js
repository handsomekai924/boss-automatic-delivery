/** 面试题弹窗：左侧题目类型目录 + 右侧单题详情，上一题 / 下一题切换。 */

import { api } from "./api.js";
import { toast, modal, escapeHtml } from "./ui.js";

const TYPES = ["技术深度", "项目经历", "场景", "软技能"];

/**
 * 打开题目详情弹窗（左类型目录 / 右单题，上一题下一题）。
 *
 * @param {{analysisId?: string, encryptJobId: string, jobName?: string, brandName?: string, matchScore?: number, onChanged?: () => void}} opts
 *   岗位与归属分析；analysisId 可空（纯 JD 出题的记录）；onChanged 在题目有增删改后回调
 * @returns {{close: () => void}} 弹窗句柄
 */
export function openInterviewPrep(opts) {
  const analysisId = opts.analysisId || "";
  const jobId = opts.encryptJobId || "";
  const title = [opts.jobName || "岗位", opts.brandName].filter(Boolean).join(" · ");
  const onChanged = typeof opts.onChanged === "function" ? opts.onChanged : null;

  const dlg = modal({
    title: `面试题 · ${escapeHtml(title)}`,
    wide: true,
    body: `<div class="muted">正在读取面试题…</div>`,
    onClose: () => {
      stopPoll();
      document.removeEventListener("keydown", onKey);
    },
  });

  /** @type {ReturnType<typeof setTimeout>|null} */
  let pollTimer = null;
  /** @type {Array<object>} */
  let questions = [];
  let stats = { question_count: 0 };
  let generating = false;
  /** 当前游标（在 visibleQuestions() 里下标）。 */
  let cursor = 0;
  /** 左侧选中的题型；空 = 全部。 */
  let activeType = "";
  /** 展开中的题型（二级目录显示其下题目）。 */
  let expanded = new Set(TYPES);
  /** 编辑弹窗是否开着（开着时不响应左右翻页键）。 */
  let editOpen = false;

  const body = dlg.bodyEl;

  /** 停掉任务轮询。 */
  function stopPoll() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  /** 当前选中题型下的题目。 */
  function visibleQuestions() {
    return activeType ? questions.filter((q) => q.question_type === activeType) : questions;
  }

  /** 画左侧题目类型目录：一级题型 / 二级题目标题。 */
  function renderCatalog() {
    const nav = body.querySelector("#ip-catalog");
    if (!nav) return;
    const rows = questions;
    const visible = visibleQuestions();
    const currentId = visible[cursor]?.id;
    const counts = Object.fromEntries(TYPES.map((t) => [t, 0]));
    for (const q of rows) counts[q.question_type] = (counts[q.question_type] || 0) + 1;

    /** 二级：某题型下的题目标题。 */
    const questionList = (list) =>
      `<div class="iqd-group">${list
        .map(
          (q) =>
            `<button type="button" class="iqd-q ${q.id === currentId ? "active" : ""}" data-id="${q.id}">
            ${escapeHtml((q.question_text || "").slice(0, 28))}
          </button>`
        )
        .join("")}</div>`;

    /** 一级「全部」（无子级）。 */
    const allBtn = `
      <button type="button" class="iqd-type ${activeType === "" ? "active" : ""}" data-type="">
        <span class="iqd-type-label"><span class="iqd-caret-spacer"></span>全部</span>
        <span class="iqd-n">${rows.length}</span>
      </button>`;

    /** 一级题型（点选中 + 侧边箭头展开/收起），下挂二级题目。 */
    const typeSection = (t) => {
      const list = rows.filter((q) => q.question_type === t);
      const open = expanded.has(t);
      return `
        <div class="iqd-l1">
          <button type="button" class="iqd-type ${activeType === t ? "active" : ""}" data-type="${escapeHtml(t)}">
            <span class="iqd-type-label">
              <span class="iqd-caret ${open ? "open" : ""}" data-toggle="${escapeHtml(t)}" title="${open ? "收起" : "展开"}">▸</span>
              ${escapeHtml(t)}
            </span>
            <span class="iqd-n">${counts[t] || 0}</span>
          </button>
          ${open && list.length ? questionList(list) : ""}
        </div>`;
    };

    nav.innerHTML = `
      <div class="card-sub mb-8">题目类型目录</div>
      ${allBtn}
      ${TYPES.filter((t) => (counts[t] || 0) > 0).map(typeSection).join("")}`;
  }

  /** 画右侧单题详情。 */
  function renderDetail() {
    const stage = body.querySelector("#ip-stage");
    const nav = body.querySelector("#ip-nav");
    if (!stage || !nav) return;
    const rows = visibleQuestions();
    if (cursor >= rows.length) cursor = rows.length - 1;
    if (cursor < 0) cursor = 0;

    if (!questions.length) {
      stage.innerHTML = `
        <div class="empty">
          <div class="empty-icon">◎</div>
          <p>这个岗位还没有面试题。</p>
        </div>`;
      nav.classList.add("hidden");
      return;
    }
    if (!rows.length) {
      stage.innerHTML = `<div class="empty"><div class="empty-icon">◎</div><p>没有符合筛选条件的题目</p></div>`;
      nav.classList.add("hidden");
      return;
    }
    nav.classList.remove("hidden");

    const q = rows[cursor];
    const hints = q.answer_hints || {};
    const points = (hints.key_points || []).map((p) => `<li>${escapeHtml(String(p))}</li>`).join("");
    const tags = (hints.tags || [])
      .map((t) => `<span class="chip">${escapeHtml(String(t))}</span>`)
      .join(" ");
    stage.innerHTML = `
      <div class="card ip-q" data-id="${q.id}">
        <div class="flex between center gap-8 wrap mb-12">
          <div class="job-meta">
            <span class="pill static">${escapeHtml(q.question_type)}</span>
            <span class="pill ${q.difficulty === "高级" ? "warn" : "static"}">${escapeHtml(q.difficulty)}</span>
            ${q.is_frequent ? '<span class="pill accent">⭐ 高频</span>' : ""}
            ${tags}
          </div>
          <div class="btn-row">
            <button class="btn sm ghost ip-edit">编辑</button>
          </div>
        </div>
        <div class="ip-q-text">${escapeHtml(q.question_text)}</div>
        <div class="ip-hints mt-16">
          ${hints.approach ? `<div class="mb-12"><div class="card-sub mb-8">答题思路</div><div>${escapeHtml(hints.approach)}</div></div>` : ""}
          ${
            points
              ? `<div class="mb-12"><div class="card-sub mb-8">关键知识点</div><ul class="ip-points">${points}</ul></div>`
              : ""
          }
          ${
            hints.reference_answer
              ? `<div class="mb-12"><div class="card-sub mb-8">参考答案</div><div style="white-space:pre-wrap">${escapeHtml(hints.reference_answer)}</div></div>`
              : ""
          }
          ${!hints.approach && !points && !hints.reference_answer ? `<div class="muted">这道题没有提示内容，可点「编辑」补一份。</div>` : ""}
        </div>
      </div>`;

    body.querySelector("#ip-pos").textContent = `${cursor + 1} / ${rows.length}${
      rows.length !== questions.length ? `（筛选自 ${questions.length} 道）` : ""
    }`;
    const prev = body.querySelector("#ip-prev");
    const next = body.querySelector("#ip-next");
    if (prev) prev.disabled = cursor <= 0;
    if (next) next.disabled = cursor >= rows.length - 1;
  }

  /** 目录 + 详情一起重画。 */
  function renderStage() {
    renderCatalog();
    renderDetail();
  }

  /** 画顶部状态（题数 / 按钮态）。 */
  function renderHead() {
    const head = body.querySelector("#ip-head");
    if (!head) return;
    const total = stats.question_count || 0;
    head.innerHTML = `
      <div class="flex between center gap-12 wrap mb-8">
        <div class="card-sub">
          ${total ? `共 ${total} 道` : "还没有面试题"}
          ${opts.matchScore ? ` · 匹配度 ${escapeHtml(String(opts.matchScore))}` : ""}
        </div>
        <div class="btn-row">
          ${
            total
              ? `<button class="btn ghost" id="ip-export-md">导出 Markdown</button>
                 <button class="btn ghost" id="ip-export-json">导出 JSON</button>`
              : ""
          }
          <button class="btn ghost" id="ip-sup-open" ${generating ? "disabled" : ""}>补题</button>
          <button class="btn primary" id="ip-generate" ${generating ? "disabled" : ""}>${
            generating ? "生成中…" : total ? "重新生成" : "生成面试题"
          }</button>
        </div>
      </div>`;

    head.querySelector("#ip-generate")?.addEventListener("click", () => startGenerate());
    head.querySelector("#ip-sup-open")?.addEventListener("click", () => openSupplement());
    head.querySelector("#ip-export-md")?.addEventListener("click", () => exportAs("markdown"));
    head.querySelector("#ip-export-json")?.addEventListener("click", () => exportAs("json"));
  }

  /** 画生成中进度条 + 事件。 */
  function renderTask(task) {
    const box = body.querySelector("#ip-task");
    if (!box) return;
    if (!task || task.status === "idle") {
      box.innerHTML = "";
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    const pct = task.total ? Math.round(((task.done || 0) / task.total) * 100) : 0;
    const events = (task.events || [])
      .slice(-6)
      .map((e) => {
        const p = e.payload || {};
        return `<div class="muted" style="font-size:12px">${escapeHtml(e.name)} ${escapeHtml(
          p.job_name || p.reason || p.error || ""
        )}</div>`;
      })
      .join("");
    box.innerHTML = `
      <div class="card mb-16">
        <div class="flex between center mb-8">
          <div class="card-sub">${escapeHtml(task.current || "正在生成面试题…")}</div>
          ${
            task.status === "running"
              ? '<button class="btn sm danger" id="ip-cancel">停止</button>'
              : ""
          }
        </div>
        <div class="progress ${task.status === "error" ? "bad" : ""}"><i style="width:${pct}%"></i></div>
        <div class="muted mt-8" style="font-size:12px">${
          task.status === "running"
            ? `${task.done || 0} / ${task.total || 0} 个岗位`
            : task.status === "error"
              ? `失败：${escapeHtml(task.error || "")}`
              : task.status === "cancelled"
                ? "已取消"
                : `完成，共 ${task.question_total || 0} 道题`
        }</div>
        <div class="mt-8">${events}</div>
      </div>`;
    box.querySelector("#ip-cancel")?.addEventListener("click", async () => {
      try {
        await api.post(`/api/interview/task/${encodeURIComponent(task.task_id)}/cancel`);
        toast("已请求停止", "warn");
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  }

  /** 拉列表并整页重画。 */
  async function loadList() {
    try {
      const data = await api.get(
        `/api/interview/list?encrypt_job_id=${encodeURIComponent(jobId)}`
      );
      questions = data.questions || [];
      stats = data.stats || { question_count: 0 };
      renderHead();
      renderStage();
    } catch (err) {
      toast("读面试题失败：" + err.message, "bad");
    }
  }

  /** 题目有增删改后通知外层（刷新卡片徽章）。 */
  function notifyChanged() {
    if (onChanged) onChanged();
  }

  /** 轮询生成任务；跑完就重拉列表。 */
  function watchTask(taskId) {
    stopPoll();
    generating = true;
    renderHead();
    const tick = async () => {
      try {
        const task = await api.get(`/api/interview/task/${encodeURIComponent(taskId)}`);
        renderTask(task);
        if (task.status === "running") {
          pollTimer = setTimeout(tick, 1500);
          return;
        }
        generating = false;
        cursor = 0;
        await loadList();
        if (task.status === "done") {
          toast(`已生成 ${task.question_total || 0} 道题`, "ok");
          notifyChanged();
        } else if (task.status === "error") toast(task.error || "生成失败", "bad");
      } catch (err) {
        generating = false;
        renderHead();
        toast(err.message, "bad");
      }
    };
    tick();
  }

  /** 触发生成（只出这个岗位的题，整套替换）。 */
  async function startGenerate() {
    if (generating) return toast("生成任务已在跑", "warn");
    try {
      const task = await api.post("/api/interview/generate", {
        analysis_id: analysisId,
        encrypt_job_ids: [jobId],
      });
      renderTask(task);
      watchTask(task.task_id);
      toast("已开始生成面试题", "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  /** 补题：弹窗选题型 + 条数，只补这一型、追加到已有题后面，不覆盖。 */
  function openSupplement() {
    if (generating) return toast("生成任务已在跑", "warn");
    editOpen = true;
    const sup = modal({
      title: "补题面试题",
      body: `
        <div class="field mb-12">
          <label>题型</label>
          <select class="select" id="ip-sup-type">
            ${TYPES.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("")}
          </select>
        </div>
        <div class="field mb-12">
          <label>补几条（1-10）</label>
          <input class="input" id="ip-sup-count" type="number" min="1" max="10" value="3">
        </div>
        <div class="muted mb-12" style="font-size:12px">
          只补所选题型，追加到已有题目后面；自动避开重复，单次最多 10 条。
        </div>
        <div class="btn-row">
          <button class="btn primary" id="ip-sup-go">开始补题</button>
          <button class="btn ghost" id="ip-sup-cancel">取消</button>
        </div>`,
      onClose: () => {
        editOpen = false;
      },
    });
    const box = sup.box;
    box.querySelector("#ip-sup-cancel").addEventListener("click", () => sup.close());
    box.querySelector("#ip-sup-go").addEventListener("click", async () => {
      const type = box.querySelector("#ip-sup-type").value || "";
      const count = Number(box.querySelector("#ip-sup-count").value || 0);
      if (!type) return toast("请选择题型", "warn");
      if (!Number.isInteger(count) || count < 1) return toast("条数至少 1", "warn");
      if (count > 10) return toast("单次最多 10 条", "warn");
      sup.close();
      try {
        const task = await api.post("/api/interview/generate", {
          analysis_id: analysisId,
          encrypt_job_ids: [jobId],
          question_type: type,
          question_count: count,
        });
        renderTask(task);
        watchTask(task.task_id);
        toast(`已开始补充「${type}」${count} 条`, "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  }

  /**
   * 导出下载。
   *
   * @param {"markdown"|"json"} format 导出格式
   */
  function exportAs(format) {
    window.open(
      `/api/interview/export?encrypt_job_id=${encodeURIComponent(jobId)}&format=${format}`,
      "_blank"
    );
  }

  /**
   * 打开单题编辑。
   *
   * @param {object} q 题目
   */
  function openEdit(q) {
    const hints = q.answer_hints || {};
    editOpen = true;
    const edit = modal({
      title: "编辑题目",
      body: `
        <div class="field mb-12">
          <label>题目内容</label>
          <textarea class="textarea" id="ie-text" rows="3">${escapeHtml(q.question_text)}</textarea>
        </div>
        <div class="field mb-12">
          <label>答题思路</label>
          <textarea class="textarea" id="ie-approach" rows="3">${escapeHtml(hints.approach || "")}</textarea>
        </div>
        <div class="field mb-12">
          <label>关键知识点（一行一个）</label>
          <textarea class="textarea" id="ie-points" rows="3">${escapeHtml(
            (hints.key_points || []).join("\n")
          )}</textarea>
        </div>
        <div class="field mb-12">
          <label>参考答案</label>
          <textarea class="textarea" id="ie-answer" rows="3">${escapeHtml(
            hints.reference_answer || ""
          )}</textarea>
        </div>
        <div class="btn-row">
          <button class="btn primary" id="ie-save">保存</button>
          <button class="btn ghost" id="ie-cancel">取消</button>
        </div>`,
      onClose: () => {
        editOpen = false;
      },
    });
    edit.box.querySelector("#ie-cancel").addEventListener("click", () => edit.close());
    edit.box.querySelector("#ie-save").addEventListener("click", async () => {
      const text = edit.box.querySelector("#ie-text").value.trim();
      if (!text) return toast("题目内容不能为空", "warn");
      const points = edit.box
        .querySelector("#ie-points")
        .value.split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      try {
        await api.patch(`/api/interview/${q.id}`, {
          question_text: text,
          answer_hints: {
            ...hints,
            approach: edit.box.querySelector("#ie-approach").value.trim(),
            key_points: points,
            reference_answer: edit.box.querySelector("#ie-answer").value.trim(),
          },
        });
        edit.close();
        await loadList();
        notifyChanged();
        toast("已保存", "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  }

  /** 左右方向键翻页（编辑弹窗打开时 / 光标在输入框时不抢键）。 */
  function onKey(e) {
    if (editOpen) return;
    const tag = (e.target?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      cursor -= 1;
      renderStage();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      cursor += 1;
      renderStage();
    }
  }
  document.addEventListener("keydown", onKey);

  body.addEventListener("click", async (e) => {
    // 侧边箭头：只展开/收起二级题目，不改选中题型。
    const caret = e.target.closest?.(".iqd-caret");
    if (caret) {
      e.preventDefault();
      e.stopPropagation();
      const t = caret.dataset.toggle || "";
      if (t) {
        if (expanded.has(t)) expanded.delete(t);
        else expanded.add(t);
        renderCatalog();
      }
      return;
    }
    const typeBtn = e.target.closest?.(".iqd-type");
    if (typeBtn) {
      activeType = typeBtn.dataset.type || "";
      if (activeType && !expanded.has(activeType)) expanded.add(activeType);
      cursor = 0;
      renderStage();
      return;
    }
    const jump = e.target.closest?.(".iqd-q");
    if (jump) {
      const id = Number(jump.dataset.id);
      const q = questions.find((x) => x.id === id);
      if (q) {
        activeType = q.question_type;
        expanded.add(q.question_type);
        cursor = Math.max(0, visibleQuestions().findIndex((x) => x.id === id));
        renderStage();
      }
      return;
    }
    const card = e.target.closest?.(".ip-q");
    if (!card) return;
    const id = Number(card.dataset.id);
    const q = questions.find((x) => x.id === id);
    if (!q) return;
    if (e.target.closest(".ip-edit")) openEdit(q);
  });

  body.innerHTML = `
    <div id="ip-head"></div>
    <div id="ip-task"></div>
    <div class="iqd-wrap">
      <aside class="iqd-nav" id="ip-catalog"></aside>
      <section class="iqd-detail">
        <div id="ip-stage"></div>
        <div class="flex between center gap-12 mt-16 hidden" id="ip-nav">
          <button class="btn ghost" id="ip-prev">← 上一题</button>
          <div class="muted" id="ip-pos" style="font-size:13px"></div>
          <button class="btn ghost" id="ip-next">下一题 →</button>
        </div>
      </section>
    </div>`;
  renderHead();
  body.querySelector("#ip-prev")?.addEventListener("click", () => {
    cursor -= 1;
    renderStage();
  });
  body.querySelector("#ip-next")?.addEventListener("click", () => {
    cursor += 1;
    renderStage();
  });

  (async () => {
    // 先看有没有在跑的任务，再拉题。
    try {
      const task = await api.get("/api/interview/task/current");
      if (task && task.status === "running" && task.analysis_id === analysisId) {
        renderTask(task);
        watchTask(task.task_id);
      }
    } catch {
      /* 拿不到任务状态就当空闲 */
    }
    await loadList();
  })();

  return dlg;
}

/** 生成记录详情的别名（出题页「生成记录」用）。 */
export const openQuestionDetail = openInterviewPrep;
