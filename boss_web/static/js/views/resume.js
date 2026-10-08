/** 简历航段：上传 MD → LLM 固定模板解析 → 结构化结果 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtTime } from "../ui.js";

export async function renderResume(root) {
  root.innerHTML = `
    <h1 class="hero-title">简历 <span class="grad">解析舱</span></h1>
    <p class="hero-sub">上传 Markdown 简历，自动调用你配置的 LLM 走固定模板解析，输出结构化结果；匹配分析在「匹配」页做。</p>

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

        <div class="mt-16" id="parse-state"></div>

        <div class="btn-row mt-16">
          <button class="btn primary" id="btn-parse">重新解析</button>
          <button class="btn danger" id="del-rs">删除</button>
        </div>
      </div>

      <div class="card span-7">
        <div class="card-head">
          <h3 class="card-title">解析结果</h3>
          <span class="card-sub" id="pr-sub">尚未解析</span>
        </div>
        <div id="pr-body">
          <div class="empty"><div class="empty-icon">◌</div><p>上传或载入一份简历，解析结果会显示在这里</p></div>
        </div>
      </div>
    </div>

    <div class="card mt-24" id="raw-card">
      <div class="card-head">
        <h3 class="card-title">简历原文</h3>
        <span class="card-sub">仅阅读 · 按标题轻量分段，不算解析结果</span>
      </div>
      <div id="raw-body"></div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);
  let currentResume = null;
  let parsing = false;

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
      currentResume = draft;
      toast("简历已上传，正在解析…", "ok");
      renderRaw(draft);
      loadList();
      await runParse(draft.resume_id);
    } catch (err) {
      toast(err.message || "上传失败", "bad");
    }
  }

  // ---------- 解析 ----------
  $("btn-parse").addEventListener("click", () => {
    if (!currentResume) return toast("先上传或载入一份简历", "warn");
    runParse(currentResume.resume_id);
  });

  $("del-rs").addEventListener("click", async () => {
    if (!currentResume) return toast("没有可删除的简历", "warn");
    if (!confirm("删除这份简历？")) return;
    try {
      await api.del("/api/resume/item/" + currentResume.resume_id);
      currentResume = null;
      $("pr-body").innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>上传或载入一份简历，解析结果会显示在这里</p></div>`;
      $("pr-sub").textContent = "尚未解析";
      $("raw-body").innerHTML = "";
      $("rs-sub").textContent = "—";
      $("parse-state").innerHTML = "";
      loadList();
      toast("已删除", "ok");
    } catch (err) {
      toast(err.message || "删除失败", "bad");
    }
  });

  async function runParse(resumeId) {
    if (parsing) return;
    parsing = true;
    $("btn-parse").disabled = true;
    $("pr-sub").textContent = "解析中…";
    $("parse-state").innerHTML = `<div class="banner info">◌ 正在调用 LLM 解析（单次调用，约十几秒）…</div>`;
    try {
      const r = await api.post(`/api/resume/item/${resumeId}/parse`);
      if (currentResume && currentResume.resume_id === resumeId) {
        currentResume.llm = r.llm;
        renderParsed(r.llm);
      }
      toast("解析完成", "ok");
      loadList();
    } catch (err) {
      $("pr-sub").textContent = "解析失败";
      $("parse-state").innerHTML = `<div class="banner bad">${escapeHtml(err.message || "解析失败")}</div>`;
      toast(err.message || "解析失败", "bad");
    } finally {
      parsing = false;
      $("btn-parse").disabled = false;
    }
  }

  // ---------- 展示 ----------
  function renderParsed(llm) {
    if (!llm || !llm.data) {
      $("pr-sub").textContent = "尚未解析";
      $("pr-body").innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>还没有解析结果</p></div>`;
      return;
    }
    const d = llm.data;
    $("pr-sub").textContent = `${fmtTime(llm.parsed_at)} · ${escapeHtml(llm.model || "")}`;

    const basic = d.basic || {};
    const intent = d.intent || {};
    const kv = (label, value) =>
      value
        ? `<div class="flex gap-8" style="padding:4px 0"><span class="muted" style="width:72px;flex-shrink:0">${label}</span><span>${escapeHtml(value)}</span></div>`
        : "";

    const listCard = (title, items, renderOne) =>
      items && items.length
        ? `<div class="card-sub mb-8 mt-16">${title}</div>
           ${items.map(renderOne).join("")}`
        : "";

    const workHtml = listCard("工作经历", d.work, (w) => `
      <div style="padding:10px 0;border-bottom:1px solid var(--stroke)">
        <div class="flex between center">
          <strong>${escapeHtml(w.company || "")}</strong>
          <span class="muted" style="font-size:12px">${escapeHtml(w.period || "")}</span>
        </div>
        <div class="muted" style="font-size:12.5px;margin:2px 0 6px">${escapeHtml(w.title || "")}</div>
        ${(w.highlights || []).map((h) => `<div class="muted" style="font-size:12.5px">· ${escapeHtml(h)}</div>`).join("")}
      </div>`);

    const projectHtml = listCard("项目经历", d.project, (p) => `
      <div style="padding:10px 0;border-bottom:1px solid var(--stroke)">
        <div class="flex between center">
          <strong>${escapeHtml(p.name || "")}</strong>
          <span class="muted" style="font-size:12px">${escapeHtml(p.period || "")}</span>
        </div>
        <div class="muted" style="font-size:12.5px;margin:2px 0 6px">${escapeHtml(p.role || "")}</div>
        ${(p.highlights || []).map((h) => `<div class="muted" style="font-size:12.5px">· ${escapeHtml(h)}</div>`).join("")}
      </div>`);

    const eduHtml = listCard("教育经历", d.education, (e) => `
      <div style="padding:8px 0;border-bottom:1px solid var(--stroke)">
        <div class="flex between center">
          <strong>${escapeHtml(e.school || "")}</strong>
          <span class="muted" style="font-size:12px">${escapeHtml(e.period || "")}</span>
        </div>
        <div class="muted" style="font-size:12.5px">${escapeHtml([e.major, e.degree].filter(Boolean).join(" · "))}</div>
      </div>`);

    const skills = (d.skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");

    $("pr-body").innerHTML = `
      <div class="card-sub mb-8">基本信息</div>
      ${kv("姓名", basic.name)}${kv("电话", basic.phone)}${kv("邮箱", basic.email)}${kv("城市", basic.city)}

      <div class="card-sub mb-8 mt-16">求职意向</div>
      ${kv("岗位", intent.position)}${kv("城市", intent.city)}${kv("薪资", intent.salary)}

      ${workHtml}
      ${projectHtml}
      ${eduHtml}

      <div class="card-sub mb-8 mt-16">技能标签</div>
      <div class="pills">${skills || "<span class='muted'>未提取到技能</span>"}</div>

      ${d.self_evaluation ? `
        <div class="card-sub mb-8 mt-16">自我评价</div>
        <div class="muted" style="font-size:13px;line-height:1.7">${escapeHtml(d.self_evaluation)}</div>` : ""}

      ${d.summary ? `
        <div class="card-sub mb-8 mt-16">摘要（供匹配用）</div>
        <div class="greeting-box">${escapeHtml(d.summary)}</div>` : ""}
    `;
    $("parse-state").innerHTML = "";
  }

  function renderRaw(draft) {
    $("rs-sub").textContent = draft.resume_id;
    const sections = Object.entries(draft.sections || {});
    const others = Object.entries(draft.other_sections || {});
    const block = (k, v) => `
      <details class="acc">
        <summary>${escapeHtml(k)} <span class="muted" style="font-size:11px">(${String(v || "").length} 字)</span></summary>
        <div class="acc-body">${escapeHtml((v || "").slice(0, 2000))}</div>
      </details>`;
    $("raw-body").innerHTML =
      (sections.map(([k, v]) => block(k, v)).join("") +
        others.map(([k, v]) => block(k, v)).join("")) ||
      `<div class="acc-body">${escapeHtml((draft.raw || "").slice(0, 4000))}</div>`;
  }

  async function loadList() {
    try {
      const r = await api.get("/api/resume/list");
      const items = r.items || [];
      $("rs-list").innerHTML = items
        .map((it) => {
          const parsed = it.llm && it.llm.has_data;
          return `
          <div class="flex between center gap-8" style="padding:8px 0;border-bottom:1px solid var(--stroke)">
            <span>${escapeHtml(it.title || it.resume_id)}
              ${parsed ? `<span class="pill accent">已解析</span>` : `<span class="pill">未解析</span>`}
            </span>
            <button class="btn sm ghost" data-id="${escapeHtml(it.resume_id)}">载入</button>
          </div>`;
        })
        .join("");
      $("rs-list").querySelectorAll("button[data-id]").forEach((b) => {
        b.addEventListener("click", async () => {
          const draft = await api.get("/api/resume/item/" + b.dataset.id);
          currentResume = draft;
          renderRaw(draft);
          renderParsed(draft.llm);
          if (!draft.llm) {
            $("parse-state").innerHTML = "";
            toast("这份简历还没解析，点「重新解析」开始", "warn");
          }
        });
      });
    } catch { /* ignore */ }
  }

  await loadList();
}
