/** 简历航段：上传 MD · 章节解析 · 职位匹配分析 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, scoreRing, fmtTime } from "../ui.js";

export async function renderResume(root) {
  root.innerHTML = `
    <h1 class="hero-title">简历 <span class="grad">匹配舱</span></h1>
    <p class="hero-sub">上传 Markdown 简历，规则切出七大章节，再用你配置的 LLM 对库里的职位做匹配度、技能缺口与招呼语生成。</p>

    <div class="bento">
      <div class="card span-5">
        <div class="card-head">
          <h3 class="card-title">简历</h3>
          <span class="card-sub" id="rs-sub">—</span>
        </div>

        <div class="dropzone" id="dropzone">
          <div class="dz-icon">⇪</div>
          <div>拖进来，或点击选择 <span class="mono">.md</span> 文件</div>
          <div class="muted" style="font-size:12px;margin-top:6px">最大 2MB · 也支持 .txt</div>
          <input type="file" id="file" accept=".md,.markdown,.txt" hidden>
        </div>

        <div id="rs-list" class="mt-16"></div>

        <div id="rs-preview" class="mt-16"></div>
      </div>

      <div class="card span-7">
        <div class="card-head">
          <h3 class="card-title">匹配分析</h3>
          <span class="card-sub" id="an-state">idle</span>
        </div>

        <div class="field">
          <label>分析职位数 top_k</label>
          <input class="input mono" id="top-k" type="number" value="10" min="1" max="50">
          <span class="hint">从库里按最近抓取取 N 条；也可先去职位舱勾选（进阶）</span>
        </div>

        <div class="btn-row mb-16">
          <button class="btn primary" id="btn-analyze">开始匹配</button>
          <button class="btn danger hidden" id="btn-cancel-an">停止</button>
        </div>

        <div class="progress mb-8"><i id="an-bar" style="width:0%"></i></div>
        <div class="muted mono mb-16" id="an-msg" style="font-size:12px">待命</div>

        <div id="an-results"></div>
      </div>
    </div>

    <div class="card mt-24 hidden" id="history-card">
      <div class="card-head">
        <h3 class="card-title">历史分析</h3>
        <span class="card-sub">data/boss.db</span>
      </div>
      <div id="history-list"></div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);
  let currentResume = null;
  let pollTimer = null;
  let analyzeTaskId = null;

  // ---------- 上传 ----------
  const dz = $("dropzone");
  const fileInput = $("file");
  dz.addEventListener("click", () => fileInput.click());
  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("over");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("over");
    const f = e.dataTransfer.files[0];
    if (f) upload(f);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) upload(fileInput.files[0]);
  });

  async function upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    try {
      const draft = await api.upload("/api/resume/upload", fd);
      toast("简历已上传并解析", "ok");
      currentResume = draft;
      renderPreview(draft);
      loadList();
    } catch (err) {
      toast(err.message || "上传失败", "bad");
    }
  }

  function renderPreview(draft) {
    $("rs-sub").textContent = draft.resume_id;
    const skills = (draft.skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");
    const sections = Object.entries(draft.sections || {})
      .map(
        ([k, v]) => `
        <details class="acc">
          <summary>${escapeHtml(k)} <span class="muted" style="font-size:11px">(${String(v || "").length} 字)</span></summary>
          <div class="acc-body">${escapeHtml((v || "").slice(0, 1200))}</div>
        </details>`
      )
      .join("");
    const others = Object.entries(draft.other_sections || {})
      .map(
        ([k, v]) => `
        <details class="acc">
          <summary class="muted">${escapeHtml(k)}</summary>
          <div class="acc-body">${escapeHtml((v || "").slice(0, 800))}</div>
        </details>`
      )
      .join("");

    $("rs-preview").innerHTML = `
      <div class="flex between center mb-8">
        <strong>${escapeHtml(draft.title)}</strong>
        <button class="btn sm danger" id="del-rs">删除</button>
      </div>
      <div class="card-sub mb-8">技能标签</div>
      <div class="pills mb-16">${skills || "<span class='muted'>未识别到技能标签章节</span>"}</div>
      <div class="card-sub mb-8">章节</div>
      ${sections || "<div class='muted'>没有识别到标准章节，原文仍会用于分析</div>"}
      ${others}
    `;
    $("del-rs").addEventListener("click", async () => {
      if (!confirm("删除这份简历？")) return;
      await api.del("/api/resume/item/" + draft.resume_id);
      currentResume = null;
      $("rs-preview").innerHTML = "";
      $("rs-sub").textContent = "—";
      loadList();
      toast("已删除", "ok");
    });
  }

  async function loadList() {
    try {
      const r = await api.get("/api/resume/list");
      const items = r.items || [];
      $("rs-list").innerHTML = items
        .map(
          (it) => `
          <div class="flex between center gap-8" style="padding:8px 0;border-bottom:1px solid var(--stroke)">
            <span>${escapeHtml(it.title || it.resume_id)}</span>
            <button class="btn sm ghost" data-id="${escapeHtml(it.resume_id)}">载入</button>
          </div>`
        )
        .join("");
      $("rs-list").querySelectorAll("button[data-id]").forEach((b) => {
        b.addEventListener("click", async () => {
          const draft = await api.get("/api/resume/item/" + b.dataset.id);
          currentResume = draft;
          renderPreview(draft);
        });
      });
    } catch { /* ignore */ }
  }

  // ---------- 分析 ----------
  $("btn-analyze").addEventListener("click", async () => {
    if (!currentResume) return toast("先上传或载入一份简历", "warn");
    try {
      const task = await api.post("/api/resume/analyze", {
        resume_id: currentResume.resume_id,
        top_k: Number($("top-k").value) || 10,
        job_ids: [],
      });
      analyzeTaskId = task.task_id;
      $("btn-analyze").classList.add("hidden");
      $("btn-cancel-an").classList.remove("hidden");
      toast("分析已启动（串行调 LLM，请稍候）", "ok");
      startPoll();
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  $("btn-cancel-an").addEventListener("click", async () => {
    if (!analyzeTaskId) return;
    try {
      await api.post(`/api/resume/analyze/${analyzeTaskId}/cancel`);
      toast("已请求停止", "warn");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  function startPoll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      if (!analyzeTaskId) return;
      try {
        const s = await api.get(`/api/resume/analyze/${analyzeTaskId}`);
        const done = s.done || 0;
        const total = s.total || 1;
        $("an-bar").style.width = (s.status === "running" ? Math.round((done / total) * 100) : 100) + "%";
        $("an-state").textContent = s.status;
        $("an-msg").textContent =
          s.status === "running"
            ? `${done}/${total} · ${s.current_job || ""}`
            : s.error || s.status;
        if (s.matches && s.matches.length) renderMatches(s.matches);
        if (["done", "error", "cancelled"].includes(s.status)) {
          clearInterval(pollTimer);
          pollTimer = null;
          $("btn-analyze").classList.remove("hidden");
          $("btn-cancel-an").classList.add("hidden");
          if (s.status === "done") {
            toast("分析完成", "ok");
            loadHistory();
          } else if (s.status === "error") {
            toast(s.error || "分析失败", "bad");
          }
        }
      } catch { /* ignore */ }
    }, 1200);
  }

  function renderMatches(matches) {
    const ranked = [...matches].sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
    $("an-results").innerHTML = ranked
      .map((m) => {
        const score = m.match_score || 0;
        const matched = (m.matched_skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");
        const missing = (m.missing_skills || []).map((s) => `<span class="pill static bad">${escapeHtml(s)}</span>`).join(" ");
        return `
          <div class="card lift mb-16" style="padding:16px">
            <div class="match-row">
              ${scoreRing(score)}
              <div class="match-main">
                <h4>${escapeHtml(m.job_name)} <span class="muted" style="font-weight:400;font-size:13px">@ ${escapeHtml(m.brand_name)}</span></h4>
                <div class="muted" style="font-size:12px">${escapeHtml(m.location || "")} · ${escapeHtml(m.salary_desc || "")}</div>
                <div class="muted" style="font-size:12.5px;margin-top:6px">${escapeHtml(m.verdict || "")}</div>
                <div class="job-meta">${matched}</div>
                ${missing ? `<div class="card-sub mb-8">缺口</div><div class="pills">${missing}</div>` : ""}
                ${m.greeting ? `<div class="greeting-box">💬 ${escapeHtml(m.greeting)}</div>` : ""}
                ${m.error ? `<div class="banner bad mt-8">${escapeHtml(m.error)}</div>` : ""}
              </div>
            </div>
          </div>`;
      })
      .join("");
  }

  async function loadHistory() {
    try {
      const r = await api.get("/api/resume/analyses");
      const items = r.items || [];
      if (!items.length) return;
      $("history-card").classList.remove("hidden");
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
              <button class="btn sm ghost" data-id="${escapeHtml(a.analysis_id)}">查看</button>
            </div>
          </div>`
        )
        .join("");
      $("history-list").querySelectorAll("button[data-id]").forEach((b) => {
        b.addEventListener("click", async () => {
          const data = await api.get("/api/resume/analyses/" + b.dataset.id);
          modal({
            title: "分析结果 · " + escapeHtml(data.resume_title || data.analysis_id),
            body: `<div id="m-matches"></div>`,
            wide: true,
          });
          setTimeout(() => {
            const box = document.getElementById("m-matches");
            if (box) renderMatchesInto(box, data.matches || []);
          }, 0);
        });
      });
    } catch { /* ignore */ }
  }

  function renderMatchesInto(box, matches) {
    const ranked = [...matches].sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
    box.innerHTML = ranked
      .map((m) => {
        const score = m.match_score || 0;
        return `
        <div class="match-row mb-16">
          ${scoreRing(score)}
          <div class="match-main">
            <h4>${escapeHtml(m.job_name)} <span class="muted" style="font-weight:400;font-size:13px">@ ${escapeHtml(m.brand_name)}</span></h4>
            <div class="muted" style="font-size:12.5px">${escapeHtml(m.verdict || "")}</div>
            ${m.greeting ? `<div class="greeting-box">💬 ${escapeHtml(m.greeting)}</div>` : ""}
          </div>
        </div>`;
      })
      .join("");
  }

  // 技能缺口条（历史详情里附带）
  function renderGaps(container, gaps) {
    if (!gaps || !gaps.length) return;
    const max = Math.max(...gaps.map((g) => g.count || 0), 1);
    container.innerHTML = gaps
      .map(
        (g) => `
        <div class="skill-bar">
          <span class="sb-name">${escapeHtml(g.skill)}</span>
          <span class="sb-track"><i style="width:${Math.round(((g.count || 0) / max) * 100)}%"></i></span>
          <span class="sb-n">${g.count}</span>
        </div>`
      )
      .join("");
  }
  // renderGaps 供历史详情用
  window.__renderGaps = renderGaps;

  await loadList();
  await loadHistory();

  return () => clearInterval(pollTimer);
}
