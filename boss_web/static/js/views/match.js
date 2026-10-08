/** 匹配舱：全库/勾选岗位匹配 · 优缺点 · 招呼语手改 · 三种粒度发送（确认门槛） */

import { api } from "../api.js";
import { toast, modal, escapeHtml, scoreRing, fmtTime } from "../ui.js";

const PAGE_SIZE = 20;

//: 「一键发送全部」的评分阈值兜底值（读不到 /api/deliver/config 时用它，
//: 正常情况以落库的 doc('deliver_config') 为准）
const MIN_SCORE_DEFAULT = 70;

export async function renderMatch(root) {
  root.innerHTML = `
    <h1 class="hero-title">匹配 <span class="grad">舱</span></h1>
    <p class="hero-sub">用 LLM 解析过的简历对库里的岗位做人岗匹配，出匹配度、优缺点与个性化招呼语；可手改招呼语，确认后发送。</p>

    <div class="bento mb-24">
      <div class="card span-12">
        <div class="flex between center gap-12 wrap">
          <div class="flex center gap-12 wrap">
            <label class="muted" style="font-size:12.5px">简历</label>
            <select class="select" id="resume-select" style="min-width:220px"></select>
            <span class="pill" id="llm-badge">LLM —</span>
          </div>
          <div class="btn-row">
            <button class="btn primary" id="btn-match-all">一键匹配全部</button>
            <button class="btn" id="btn-match-sel">匹配选中</button>
            <button class="btn danger hidden" id="btn-stop">停止</button>
          </div>
        </div>
        <div class="progress mt-16"><i id="mt-bar" style="width:0%"></i></div>
        <div class="muted mono mt-8" id="mt-msg" style="font-size:12px">待命</div>
      </div>
    </div>

    <div class="card mb-24">
      <div class="card-head">
        <h3 class="card-title">发送工具条</h3>
        <span class="card-sub">任何粒度发送前都会弹确认 · 已成功的不会重发 · 发送 = 建会话 + 单独发招呼语正文</span>
      </div>
      <div class="flex between center wrap gap-12">
        <div class="btn-row">
          <button class="btn primary" id="btn-send-all">一键发送全部 (<span id="send-all-n">0</span>)</button>
          <button class="btn" id="btn-send-sel">发送选中 (<span id="send-sel-n">0</span>)</button>
        </div>
        <label class="flex center gap-8">
          <span class="muted" style="font-size:12.5px">评分阈值</span>
          <input type="number" class="input" id="send-min-score" value="70" min="0" max="100" step="1" style="width:76px">
          <span class="muted" style="font-size:11.5px">≥ 该分数才进「一键发送全部」，改完自动保存</span>
        </label>
        <span class="muted" style="font-size:12px">在下方「匹配结果」里展开单条可单独改招呼语 / 发送</span>
      </div>
    </div>

    <div class="card mb-24">
      <div class="card-head">
        <h3 class="card-title">历史分析</h3>
        <span class="card-sub">data/boss.db · analysis 表</span>
      </div>
      <div id="history-list" class="scroll-y" style="max-height:200px"></div>
    </div>

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
          <span class="card-sub" id="an-sub">尚未匹配</span>
        </div>
        <div id="an-results" class="scroll-y">
          <div class="empty"><div class="empty-icon">◌</div><p>启动匹配后，结果会按分数从高到低排在这里</p></div>
        </div>
      </div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);
  let resumes = [];
  let currentResumeId = "";
  let jobs = [];
  let totalJobs = 0;
  let page = 0;
  const selected = new Set(); // encrypt_job_id
  let matches = [];           // 当前展示的 match 条目
  let analysisId = null;
  let pollTimer = null;
  let taskId = null;
  let deliverTimer = null;    // 发送任务轮询
  let deliverTaskId = null;
  const expanded = new Set();      // 展开中的 match（encrypt_job_id）
  const greetDrafts = new Map();   // 手改中的招呼语草稿，轮询重绘不丢

  // ---------- 简历选择 ----------
  async function loadResumes() {
    try {
      const r = await api.get("/api/resume/list");
      resumes = r.items || [];
      const sel = $("resume-select");
      sel.innerHTML =
        `<option value="">— 选择简历 —</option>` +
        resumes
          .map((it) => {
            const parsed = it.llm && it.llm.has_data ? " · 已解析" : " · 未解析";
            return `<option value="${escapeHtml(it.resume_id)}">${escapeHtml(it.title || it.resume_id)}${parsed}</option>`;
          })
          .join("");
      if (resumes.length) {
        currentResumeId = resumes[0].resume_id;
        sel.value = currentResumeId;
      }
      updateLlmBadge();
    } catch { /* ignore */ }
  }

  function updateLlmBadge() {
    const it = resumes.find((x) => x.resume_id === currentResumeId);
    const parsed = it && it.llm && it.llm.has_data;
    $("llm-badge").className = parsed ? "pill accent" : "pill";
    $("llm-badge").textContent = parsed ? "简历已解析" : "简历未解析（匹配将用规则摘要）";
  }

  $("resume-select").addEventListener("change", () => {
    currentResumeId = $("resume-select").value;
    updateLlmBadge();
  });

  // ---------- 岗位列表 ----------
  async function loadJobs() {
    try {
      const r = await api.get(`/api/jobs?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`);
      jobs = r.items || [];
      totalJobs = r.total || 0;
      renderJobs();
    } catch (err) {
      toast(err.message || "加载职位失败", "bad");
    }
  }

  function matchedIds() {
    return new Set(matches.map((m) => m.encrypt_job_id));
  }

  function renderJobs() {
    const mIds = matchedIds();
    $("jobs-sub").textContent = `共 ${totalJobs} 条 · 第 ${page + 1}/${Math.max(1, Math.ceil(totalJobs / PAGE_SIZE))} 页`;
    $("job-list").innerHTML = jobs
      .map((j) => {
        const id = j.encrypt_job_id;
        const on = selected.has(id);
        const done = mIds.has(id);
        return `
        <label class="flex center gap-8" style="padding:8px 0;border-bottom:1px solid var(--stroke);cursor:pointer">
          <input type="checkbox" data-id="${escapeHtml(id)}" ${on ? "checked" : ""}>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              ${escapeHtml(j.job_name)} <span class="muted">@ ${escapeHtml(j.brand_name)}</span>
              ${done ? `<span class="pill accent">已匹配</span>` : ""}
            </div>
            <div class="muted" style="font-size:11px">${escapeHtml(j.location || "")} · ${escapeHtml(j.salary_desc || "")}</div>
          </div>
        </label>`;
      })
      .join("") || `<div class="muted">库里还没有职位，先去「职位」页抓取</div>`;

    $("job-list").querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(cb.dataset.id);
        else selected.delete(cb.dataset.id);
        updateSelCount();
      });
    });
    $("page-info").textContent = `${page + 1} / ${Math.max(1, Math.ceil(totalJobs / PAGE_SIZE))}`;
    updateSelCount();
  }

  function updateSelCount() {
    $("sel-count").textContent = `已选 ${selected.size}`;
    $("send-sel-n").textContent = String(selected.size);
  }

  // ---------- 一键发送的评分阈值 ----------
  function minScore() {
    const v = parseInt($("send-min-score").value, 10);
    if (Number.isNaN(v)) return MIN_SCORE_DEFAULT;
    return Math.min(100, Math.max(0, v));
  }

  /** 「一键发送全部」的目标：评分 ≥ 阈值 + 有招呼语 + 未发送。 */
  function sendAllTargets() {
    const min = minScore();
    return matches.filter(
      (m) => (m.match_score || 0) >= min && !m.delivered_at && (m.greeting || "").trim()
    );
  }

  function updateSendCounts() {
    $("send-all-n").textContent = String(sendAllTargets().length);
  }

  $("send-min-score").addEventListener("input", updateSendCounts);
  $("send-min-score").addEventListener("change", saveMinScore);

  /** 阈值落状态库 doc('deliver_config')：刷新 / 换机器都跟着走。 */
  async function loadMinScore() {
    try {
      const cfg = await api.get("/api/deliver/config");
      if (typeof cfg.min_score === "number") {
        $("send-min-score").value = String(cfg.min_score);
        updateSendCounts();
      }
    } catch { /* 读不到就用默认 70 */ }
  }

  async function saveMinScore() {
    const v = minScore();
    $("send-min-score").value = String(v);  // 越界 / 空值收回合法区间
    updateSendCounts();
    try {
      await api.put("/api/deliver/config", { min_score: v });
    } catch (err) {
      toast(err.message || "阈值保存失败", "bad");
    }
  }

  $("btn-select-page").addEventListener("click", () => {
    jobs.forEach((j) => selected.add(j.encrypt_job_id));
    renderJobs();
  });
  $("btn-clear-sel").addEventListener("click", () => {
    selected.clear();
    renderJobs();
  });
  $("btn-prev").addEventListener("click", () => {
    if (page > 0) {
      page -= 1;
      loadJobs();
    }
  });
  $("btn-next").addEventListener("click", () => {
    if ((page + 1) * PAGE_SIZE < totalJobs) {
      page += 1;
      loadJobs();
    }
  });

  // ---------- 匹配 ----------
  $("btn-match-all").addEventListener("click", () => startMatch([]));
  $("btn-match-sel").addEventListener("click", () => {
    if (!selected.size) return toast("先在左侧勾选岗位", "warn");
    startMatch([...selected]);
  });
  $("btn-stop").addEventListener("click", async () => {
    if (!taskId) return;
    try {
      await api.post(`/api/match/analyze/${taskId}/cancel`);
      toast("已请求停止", "warn");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  async function startMatch(jobIds) {
    if (!currentResumeId) return toast("先选择一份简历", "warn");
    try {
      const task = await api.post("/api/match/analyze", {
        resume_id: currentResumeId,
        job_ids: jobIds,
      });
      taskId = task.task_id;
      analysisId = task.analysis_id;
      matches = [];
      $("btn-match-all").classList.add("hidden");
      $("btn-match-sel").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
      $("an-sub").textContent = "匹配中…";
      toast(`匹配已启动（共 ${task.total || "?"} 个职位，并行调 LLM）`, "ok");
      startPoll();
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  /** 页面回来 / 刷新后，接上还在跑（或刚跑完）的匹配任务 */
  async function restoreTask() {
    let s = null;
    try {
      s = await api.get("/api/match/analyze/status");
    } catch { /* ignore */ }
    if (!s || !s.task_id) return;

    taskId = s.task_id;
    analysisId = s.analysis_id || s.task_id;
    matches = s.matches || [];
    if (s.resume_id) {
      currentResumeId = s.resume_id;
      $("resume-select").value = s.resume_id;
      updateLlmBadge();
    }

    if (s.status === "running") {
      $("btn-match-all").classList.add("hidden");
      $("btn-match-sel").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
      $("an-sub").textContent = `匹配中 · ${s.done || 0}/${s.total || "?"}`;
      $("mt-bar").style.width =
        (s.total ? Math.round(((s.done || 0) / s.total) * 100) : 0) + "%";
      $("mt-msg").textContent = `${s.done || 0}/${s.total || "?"} · ${s.current_job || ""}`;
      renderMatches();
      renderJobs();
      startPoll();
      toast("已接上进行中的匹配任务", "ok");
      return;
    }

    if (matches.length) {
      $("an-sub").textContent = `${s.status === "done" ? "已完成" : s.status === "cancelled" ? "已停止" : "失败"} · ${matches.length} 条`;
      $("mt-bar").style.width = "100%";
      $("mt-msg").textContent = s.error || s.status;
      renderMatches();
      renderJobs();
    }
  }

  function startPoll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      if (!taskId) return;
      try {
        const s = await api.get(`/api/match/analyze/${taskId}`);
        const done = s.done || 0;
        const total = s.total || 1;
        $("mt-bar").style.width = (s.status === "running" ? Math.round((done / total) * 100) : 100) + "%";
        $("mt-msg").textContent =
          s.status === "running" ? `${done}/${total} · ${s.current_job || ""}` : s.error || s.status;
        if (s.matches && s.matches.length) {
          matches = s.matches;
          renderMatches();
        }
        if (["done", "error", "cancelled"].includes(s.status)) {
          clearInterval(pollTimer);
          pollTimer = null;
          taskId = null;
          $("btn-match-all").classList.remove("hidden");
          $("btn-match-sel").classList.remove("hidden");
          $("btn-stop").classList.add("hidden");
          if (s.status === "done") {
            $("an-sub").textContent = `完成 · ${matches.length} 条`;
            toast("匹配完成", "ok");
            loadHistory();
          } else if (s.status === "error") {
            $("an-sub").textContent = "失败";
            toast(s.error || "匹配失败", "bad");
          } else {
            $("an-sub").textContent = `已停止 · ${matches.length} 条`;
          }
          renderMatches();
          renderJobs();
        }
      } catch { /* ignore */ }
    }, 1200);
  }

  // ---------- 匹配结果（默认收起：评分 / 岗位 / 公司 / 地址 / 薪资 / 建议） ----------
  function renderMatches() {
    const box = $("an-results");
    const keepScroll = box.scrollTop;
    updateSendCounts();  // 「一键发送全部」的待发条数随匹配结果走

    if (!matches.length) {
      box.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>还没有匹配结果</p></div>`;
      return;
    }

    // 轮询重绘前把在改的招呼语草稿存下来
    box.querySelectorAll(".match-card").forEach((card) => {
      const ta = card.querySelector(".greeting-edit");
      if (ta) greetDrafts.set(card.dataset.jid, ta.value);
    });

    const ranked = [...matches].sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
    box.innerHTML = ranked
      .map((m) => {
        const jid = m.encrypt_job_id;
        const score = m.match_score || 0;
        const open = expanded.has(jid);
        const advice = (m.advice || "").trim();
        const verdict = (m.verdict || "").trim();
        const adviceLine = advice || verdict;
        const matched = (m.matched_skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");
        const missing = (m.missing_skills || []).map((s) => `<span class="pill static bad">${escapeHtml(s)}</span>`).join(" ");
        const pros = (m.pros || []).map((p) => `<div class="muted" style="font-size:12.5px">✓ ${escapeHtml(p)}</div>`).join("");
        const cons = (m.cons || []).map((c) => `<div class="muted" style="font-size:12.5px">✗ ${escapeHtml(c)}</div>`).join("");
        const draft = greetDrafts.has(jid) ? greetDrafts.get(jid) : (m.greeting || "");
        return `
          <div class="card lift mb-16 match-card" style="padding:16px" data-jid="${escapeHtml(jid)}">
            <div class="match-row">
              ${scoreRing(score)}
              <div class="match-main">
                <div class="flex between center gap-8">
                  <h4 class="match-title">${escapeHtml(m.job_name)} <span class="muted" style="font-weight:400;font-size:13px">@ ${escapeHtml(m.brand_name)}</span></h4>
                  <button class="btn sm ghost btn-toggle-more flex-shrink-0">${open ? "▾ 收起" : "▸ 展开"}</button>
                </div>
                <div class="muted" style="font-size:12px">${escapeHtml(m.location || "")} · ${escapeHtml(m.salary_desc || "")}</div>
                <div class="muted" style="font-size:12.5px;margin-top:6px"><span class="muted" style="font-size:11px">建议</span> ${escapeHtml(adviceLine)}</div>
                ${open ? `
                  <div class="match-more">
                    ${verdict && verdict !== adviceLine ? `<div class="card-sub mb-8">结论</div><div class="muted" style="font-size:12.5px">${escapeHtml(verdict)}</div>` : ""}
                    ${matched ? `<div class="card-sub mb-8 mt-12">技能匹配</div><div class="job-meta">${matched}</div>` : ""}
                    ${missing ? `<div class="card-sub mb-8 mt-12">缺口</div><div class="pills">${missing}</div>` : ""}
                    ${pros ? `<div class="card-sub mb-8 mt-12">优点</div>${pros}` : ""}
                    ${cons ? `<div class="card-sub mb-8 mt-12">缺点</div>${cons}` : ""}
                    <div class="card-sub mb-8 mt-12">招呼语 <span class="muted" style="font-size:11px">可直接改，改完点保存</span></div>
                    <textarea class="input mono greeting-edit" rows="2" style="width:100%;font-size:12.5px">${escapeHtml(draft)}</textarea>
                    <div class="btn-row mt-8">
                      <button class="btn sm ghost btn-save-greet">保存招呼语</button>
                      <button class="btn sm primary btn-send-one">发送</button>
                      <span class="muted send-status" style="font-size:11.5px">${escapeHtml(deliverLabel(m))}</span>
                    </div>
                    ${m.error ? `<div class="banner bad mt-8">${escapeHtml(m.error)}</div>` : ""}
                  </div>` : ""}
              </div>
            </div>
          </div>`;
      })
      .join("");

    box.querySelectorAll(".match-card").forEach((card) => {
      const jid = card.dataset.jid;

      card.querySelector(".btn-toggle-more").addEventListener("click", () => {
        if (expanded.has(jid)) expanded.delete(jid);
        else expanded.add(jid);
        renderMatches();
      });

      const ta = card.querySelector(".greeting-edit");
      if (ta) {
        ta.addEventListener("input", () => greetDrafts.set(jid, ta.value));
      }
      const saveBtn = card.querySelector(".btn-save-greet");
      if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
          const text = card.querySelector(".greeting-edit").value;
          try {
            await api.patch(`/api/match/${analysisId}/greeting`, {
              encrypt_job_id: jid,
              greeting: text,
            });
            const m = matches.find((x) => x.encrypt_job_id === jid);
            if (m) m.greeting = text;
            greetDrafts.delete(jid);
            toast("招呼语已保存", "ok");
          } catch (err) {
            toast(err.message || "保存失败", "bad");
          }
        });
      }
      const sendBtn = card.querySelector(".btn-send-one");
      if (sendBtn) {
        sendBtn.addEventListener("click", () => {
          const m = matches.find((x) => x.encrypt_job_id === jid);
          if (m) confirmAndSend([m]);
        });
      }
    });

    box.scrollTop = keepScroll;
  }

  function deliverLabel(m) {
    if (m.deliver_status === "failed") return `失败：${m.deliver_error || ""}`;
    if (m.delivered_at) return "已发送";
    return m.deliver_status === "sending" ? "发送中…" : "";
  }

  // ---------- 发送：三种粒度共用一个任务（C6） ----------
  async function startDeliver(targets) {
    if (!analysisId) return toast("先跑一次匹配再发送", "warn");
    if (deliverTaskId) return toast("已有发送任务在跑，等它结束", "warn");
    try {
      const s = await api.post("/api/deliver/start", {
        analysis_id: analysisId,
        encrypt_job_ids: targets.map((m) => m.encrypt_job_id),
      });
      deliverTaskId = s.task_id;
      toast(`开始发送 · 待发 ${s.total || 0} 条，跳过 ${s.skipped || 0} 条已发送`, "ok");
      applyDeliverSnapshot(s);
      startDeliverPoll();
    } catch (err) {
      toast(err.message || "发送启动失败", "bad");
    }
  }

  /** 把发送任务的每条状态贴回对应的匹配结果行。 */
  function applyDeliverSnapshot(s) {
    let touched = false;
    (s.items || []).forEach((it) => {
      if (it.status === "skipped") return;  // 已发送过，保持原样
      const m = matches.find((x) => x.encrypt_job_id === it.encrypt_job_id);
      if (!m) return;
      m.deliver_status = it.status;
      if (it.delivered_at) m.delivered_at = it.delivered_at;
      if (it.error) m.deliver_error = it.error;
      else if (it.status !== "failed") delete m.deliver_error;
      touched = true;
    });
    if (touched) renderMatches();
  }

  function startDeliverPoll() {
    clearInterval(deliverTimer);
    deliverTimer = setInterval(async () => {
      let s = null;
      try {
        s = await api.get("/api/deliver/status");
      } catch {
        return;
      }
      if (!s || !s.task_id) {
        stopDeliverPoll();
        return;
      }
      deliverTaskId = s.task_id;
      applyDeliverSnapshot(s);
      if (["done", "error", "cancelled"].includes(s.status)) {
        stopDeliverPoll();
        if (s.status === "done") {
          toast(`发送完成 · 成功 ${s.ok || 0} / 失败 ${s.failed || 0} / 跳过 ${s.skipped || 0}`, "ok");
        } else if (s.status === "error") {
          toast(s.error || "发送中断", "bad");
        } else {
          toast("发送已取消", "warn");
        }
      }
    }, 1000);
  }

  function stopDeliverPoll() {
    clearInterval(deliverTimer);
    deliverTimer = null;
    deliverTaskId = null;
  }

  /** 刷新回来接上还在跑的发送任务（跟匹配任务一样的待遇）。 */
  async function restoreDeliver() {
    let s = null;
    try {
      s = await api.get("/api/deliver/status");
    } catch {
      return;
    }
    if (!s || !s.task_id) return;
    applyDeliverSnapshot(s);
    if (s.status === "running") {
      deliverTaskId = s.task_id;
      startDeliverPoll();
      toast("已接上进行中的发送任务", "ok");
    }
  }

  // ---------- 发送入口（确认弹窗 → startDeliver） ----------
  $("btn-send-all").addEventListener("click", () => {
    const min = minScore();
    const targets = sendAllTargets();
    if (!targets.length) return toast(`没有「评分 ≥ ${min} 且有招呼语且未发送」的条目`, "warn");
    confirmAndSend(targets, `筛选条件：评分 ≥ ${min}`);
  });
  $("btn-send-sel").addEventListener("click", () => {
    const targets = matches.filter((m) => selected.has(m.encrypt_job_id) && !m.delivered_at && (m.greeting || "").trim());
    if (!targets.length) return toast("选中的条目里没有「有招呼语且未发送」的", "warn");
    confirmAndSend(targets);
  });

  function confirmAndSend(targets, scopeNote = "") {
    const rows = targets
      .map(
        (m) => `
        <div style="padding:10px 0;border-bottom:1px solid var(--stroke)">
          <div><strong>${escapeHtml(m.job_name)}</strong> <span class="muted">@ ${escapeHtml(m.brand_name)}</span>
            <span class="pill accent" style="margin-left:6px">评分 ${m.match_score || 0}</span></div>
          <div class="greeting-box mt-8" style="font-size:12px">${escapeHtml(m.greeting || "（无招呼语）")}</div>
        </div>`
      )
      .join("");
    modal({
      title: `确认发送 · 共 ${targets.length} 条`,
      body: `
        <div class="banner warn mb-12">将先<strong>建立会话</strong>，再把下方的招呼语正文作为<strong>聊天消息</strong>单独发给招聘方。</div>
        ${scopeNote ? `<div class="muted mb-8" style="font-size:12px">${escapeHtml(scopeNote)}</div>` : ""}
        ${rows}
        <div class="btn-row mt-16">
          <button class="btn primary" id="m-confirm">确认发送</button>
          <button class="btn ghost" id="m-cancel">取消</button>
        </div>`,
      wide: true,
    });
    setTimeout(() => {
      const ok = document.getElementById("m-confirm");
      const no = document.getElementById("m-cancel");
      if (!ok || !no) return;
      no.addEventListener("click", () => document.querySelector(".modal-close")?.click());
      ok.addEventListener("click", () => {
        document.querySelector(".modal-close")?.click();
        startDeliver(targets);
      });
    }, 0);
  }

  // ---------- 历史 ----------
  async function loadHistory() {
    try {
      const r = await api.get("/api/match/analyses");
      const items = (r.items || []).filter((a) => true);
      $("history-list").innerHTML = items
        .map(
          (a) => `
          <div class="flex between center gap-12" style="padding:10px 0;border-bottom:1px solid var(--stroke)">
            <div>
              <div>${escapeHtml(a.resume_title || a.analysis_id)}</div>
              <div class="muted" style="font-size:11px">${fmtTime(a.created_at)} · ${a.job_count} 个职位</div>
            </div>
            <div class="flex center gap-8">
              <span class="pill accent">最高 ${a.top_score}</span>
              <button class="btn sm ghost" data-load="${escapeHtml(a.analysis_id)}">载入</button>
              <button class="btn sm danger" data-del="${escapeHtml(a.analysis_id)}">删</button>
            </div>
          </div>`
        )
        .join("") || `<div class="muted">还没有历史分析</div>`;

      $("history-list").querySelectorAll("button[data-load]").forEach((b) => {
        b.addEventListener("click", async () => {
          try {
            const data = await api.get("/api/match/analyses/" + b.dataset.load);
            analysisId = data.analysis_id;
            matches = data.matches || [];
            expanded.clear();
            greetDrafts.clear();
            $("an-sub").textContent = `历史 · ${fmtTime(data.created_at)} · ${matches.length} 条`;
            renderMatches();
            renderJobs();
            $("results-card").scrollIntoView({ behavior: "smooth", block: "start" });
            toast("已载入历史分析", "ok");
          } catch (err) {
            toast(err.message || "载入失败", "bad");
          }
        });
      });
      $("history-list").querySelectorAll("button[data-del]").forEach((b) => {
        b.addEventListener("click", async () => {
          if (!confirm("删除这份分析？")) return;
          try {
            await api.del("/api/match/analyses/" + b.dataset.del);
            if (analysisId === b.dataset.del) {
              analysisId = null;
              matches = [];
              expanded.clear();
              greetDrafts.clear();
              renderMatches();
            }
            loadHistory();
            toast("已删除", "ok");
          } catch (err) {
            toast(err.message || "删除失败", "bad");
          }
        });
      });
    } catch { /* ignore */ }
  }

  await loadResumes();
  await loadMinScore();
  await loadJobs();
  await loadHistory();
  await restoreTask();
  await restoreDeliver();
  updateSendCounts();

  return () => {
    clearInterval(pollTimer);
    clearInterval(deliverTimer);
  };
}
