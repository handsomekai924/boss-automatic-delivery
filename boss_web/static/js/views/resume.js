/** 简历航段：一份简历两副面孔——原文与结构化解析，共用同一套章节脊柱。 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtTime, taskPanel } from "../ui.js";

//: 固定模板八段（与 boss_web/services/resume_parser.py 的 TEMPLATE_FIELDS 对齐）
const SECTIONS = [
  { key: "basic", label: "基本信息", sec: "基本信息" },
  { key: "intent", label: "求职意向", sec: "求职意向" },
  { key: "work", label: "工作经历", sec: "工作经历", kind: "list" },
  { key: "project", label: "项目经历", sec: "项目经历", kind: "list" },
  { key: "education", label: "教育经历", sec: "教育经历", kind: "list" },
  { key: "skills", label: "技能标签", sec: "技能标签", kind: "list" },
  { key: "self_evaluation", label: "自我评价", sec: "自我评价", kind: "text" },
  { key: "summary", label: "摘要", sec: "摘要", kind: "text" },
];

//: 切走再切回时别把用户选的那份简历弄丢
let lastResumeId = "";

//: 全页唯一一个画出来的图标：上传
const ICO_UPLOAD = `<svg class="rs-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M12 15.5V4.5"/><path d="m7.5 9 4.5-4.5L16.5 9"/><path d="M4.5 16v2.5a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5V16"/></svg>`;

//: 原文自己的标题 → 模板八段。整词判断，不用单字前缀
//: （后端 SECTION_ALIASES 里那个 "项目" 会把「项目管理与技术架构」也算成项目经历）
const RAW_ALIASES = {
  基本信息: ["基本信息", "个人信息", "联系信息", "联系方式"],
  求职意向: ["求职意向", "求职目标", "期望岗位", "求职期望"],
  工作经历: ["工作经历", "工作经验", "职业经历", "工作履历", "工作与实习", "实习经历"],
  项目经历: ["项目经历", "项目经验"],
  教育经历: ["教育经历", "教育背景"],
  技能标签: ["技能标签", "技能清单", "专业技能", "技术栈"],
  自我评价: ["自我评价", "个人总结", "自我介绍", "个人评价"],
};

function normHeading(text) {
  return String(text || "").replace(/[\s:：*#｜|·—–\-、,，。.]+/g, "");
}

/** 文件里的标题认到模板哪一段（认不出就空着）。 */
function rawPart(head) {
  const n = normHeading(head);
  if (!n) return "";
  for (const sec of Object.keys(RAW_ALIASES)) {
    if (RAW_ALIASES[sec].some((a) => n.includes(normHeading(a)))) return sec;
  }
  return "";
}

/** rawBlocks 的结果缓存（切面重绘很频繁） */
let blockCache = { key: "", blocks: [] };

/** 原文按它自己的标题切块：一块一个标题，正文逐字留着。 */
function rawBlocks(raw) {
  const text = String(raw || "");
  const key = `${text.length}:${text.slice(0, 32)}`;
  if (blockCache.key === key) return blockCache.blocks;

  const out = [];
  let cur = { head: "", level: 0, body: [] };
  text.split("\n").forEach((line) => {
    const m = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (m) {
      out.push(cur);
      cur = { head: m[2], level: m[1].length, body: [] };
    } else {
      cur.body.push(line);
    }
  });
  out.push(cur);
  blockCache = {
    key,
    blocks: out
      .map((b, i) => ({
        i,
        head: b.head,
        level: b.level,
        text: b.body.join("\n").replace(/^\n+|\n+$/g, ""),
      }))
      .filter((b) => b.head || b.text)
      .map((b) => ({ ...b, part: rawPart(b.head) })),
  };
  return blockCache.blocks;
}

/**
 * 简历页。LLM 解析是同步 HTTP，一次调用十几秒——用统一进度面板给可视化反馈；
 * 整条库条都是拖放落点，拖进来就传。
 */
export async function renderResume(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">简历 <span class="grad">解析舱</span></h1>
        <p class="hero-sub">上传 Markdown 简历，LLM 按固定模板抠成结构化结果。「匹配」页取的就是这份结果，所以这里先把要交出去的东西看准。</p>
      </div>
    </div>

    <section class="card rs-bar" id="rs-bar">
      <div class="rs-lib" id="rs-lib"></div>
      <div class="rs-bar-act">
        <span class="rs-fresh" id="rs-fresh"></span>
        <button class="btn" id="btn-upload">${ICO_UPLOAD}上传简历</button>
        <button class="btn primary" id="btn-parse">重新解析</button>
        <button class="btn danger" id="btn-del">删除</button>
      </div>
      <input type="file" id="file" accept=".md,.markdown,.txt" hidden>
    </section>

    <div id="rs-task" class="mb-16"></div>

    <div class="rs-bench" id="rs-bench">
      <aside class="rs-rail" id="rs-rail" aria-label="章节目录">
        <div class="rs-rail-head mono" id="rs-rail-head"></div>
        <nav class="rs-toc" id="rs-toc"></nav>
      </aside>

      <section class="card rs-face" id="rs-face">
        <div class="rs-face-head">
          <div class="rs-switch" role="tablist" aria-label="查看原文或解析结果">
            <button class="rs-switch-btn is-on" id="sw-parsed" role="tab" aria-selected="true">解析结果</button>
            <button class="rs-switch-btn" id="sw-source" role="tab" aria-selected="false">原文</button>
          </div>
          <div class="rs-face-meta mono" id="rs-meta"></div>
        </div>
        <div class="rs-face-body scroll-y" id="rs-body" tabindex="-1"></div>
      </section>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);
  const bar = $("rs-bar");
  const body = $("rs-body");
  const fileInput = $("file");

  let resumes = [];       // /api/resume/list 的 items
  let current = null;     // 载入中的整份（含 raw / sections / llm）
  let llmCfg = null;      // { model, configured }
  let face = "parsed";    // parsed | source
  let parsing = false;
  let activeSec = SECTIONS[0].sec;
  let rawHas = new Set(); // 原文里认得出模板段的那些章节
  const scrollTops = { parsed: 0, source: 0 };
  let spyFrame = 0;

  const parsePanel = taskPanel({ title: "LLM 解析", stopLabel: "" });
  $("rs-task").appendChild(parsePanel.el);
  parsePanel.update({ status: "idle", percent: 0 });

  function paintParse(state, detail, percent) {
    parsePanel.update({
      status: state,
      percent,
      current: detail,
      counts: [],
    });
  }

  function relTime(ts) {
    if (!ts) return "—";
    const d = Math.floor(Date.now() / 1000 - ts);
    if (d < 60) return "刚刚";
    if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
    return fmtTime(ts).slice(0, 10);
  }

  /** 这一段的捕获情况：目录上那个点与右侧读数的唯一来源。 */
  function coverage(s) {
    const d = (current && current.llm && current.llm.data) || null;
    if (!d) return { state: "none", note: "未解析" };
    const v = d[s.key];
    if (s.kind === "list" || Array.isArray(v)) {
      const n = Array.isArray(v) ? v.length : 0;
      return { state: n ? "on" : "off", note: n ? `${n} 条` : "空" };
    }
    if (s.key === "basic") {
      const filled = ["name", "phone", "email", "city"].filter((k) => (v || {})[k]).length;
      return { state: filled ? "on" : "off", note: filled ? `${filled}/4` : "空" };
    }
    if (s.key === "intent") {
      const filled = ["position", "city", "salary"].filter((k) => (v || {})[k]).length;
      return { state: filled ? "on" : "off", note: filled ? `${filled}/3` : "空" };
    }
    const text = String(v || "");
    return { state: text ? "on" : "off", note: text ? `${text.length} 字` : "空" };
  }

  /**
   * 这一面里有没有这一段的落点——没有就不给点，免得点了没反应。
   * 姓名与意向那两段恒在场，其余段有内容才渲染。
   */
  function targetExists(sec, onFace) {
    if (onFace === "source") return rawHas.has(sec);
    const s = SECTIONS.find((x) => x.sec === sec);
    if (!s || !(current && current.llm && current.llm.data)) return false;
    return s.key === "basic" || s.key === "intent" || coverage(s).state === "on";
  }

  /** 目录（导航 + 解析审计）。模板没接住的原文章节也列进来：文件自己的一级章节（标题那层不算，它已经变成了上面的姓名） */
  function renderToc() {
    const d = current && current.llm && current.llm.data;
    const captured = SECTIONS.filter((s) => coverage(s).state === "on").length;
    const head = $("rs-rail-head");
    head.textContent = !current ? "8 段模板" : !d ? "尚未解析" : `${captured}/8 段已捕获`;
    head.classList.toggle("is-ok", !!d && captured === SECTIONS.length);

    const rows = SECTIONS.map((s) => {
      const c = coverage(s);
      const on = s.sec === activeSec && face === "parsed";
      return `
        <button class="rs-toc-row${on ? " is-active" : ""}"
                data-sec="${escapeHtml(s.sec)}" data-face="parsed"
                data-has="${targetExists(s.sec, "parsed") ? 1 : 0}">
          <i class="rs-dot" data-state="${c.state}"></i>
          <span class="rs-toc-label" title="${escapeHtml(s.label)}">${escapeHtml(s.label)}</span>
          <span class="rs-toc-note mono">${escapeHtml(c.note)}</span>
        </button>`;
    }).join("");

    const orphans = rawBlocks(current && current.raw).filter((b) => !b.part && b.level === 2);
    const orphanRows = orphans.length
      ? `<div class="rs-toc-sep">模板没接住</div>
         ${orphans
           .map(
             (b) => `
          <button class="rs-toc-row is-quiet" data-sec="raw-${b.i}" data-face="source" data-has="1">
            <i class="rs-dot" data-state="none"></i>
            <span class="rs-toc-label" title="${escapeHtml(b.head)}">${escapeHtml(b.head)}</span>
          </button>`
           )
           .join("")}`
      : "";

    $("rs-toc").innerHTML = rows + orphanRows;
    $("rs-toc").querySelectorAll(".rs-toc-row").forEach((b) => {
      b.addEventListener("click", () => {
        activeSec = b.dataset.sec;
        if (b.dataset.face !== face) flipTo(b.dataset.face, activeSec);
        else jumpTo(activeSec);
        paintToc();
      });
    });
    paintToc();
  }

  /** 两个面共用同一套锚点：原文面里高亮的也是「这一段」那一行；在自己那一面里没有落点的段安静下来，别让人点了没反应 */
  function paintToc() {
    root.querySelectorAll(".rs-toc-row").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.sec === activeSec);
      b.classList.toggle("is-dead", b.dataset.has === "0");
    });
  }

  /** 把某个章节滚到阅读区顶部（两个面共用同一套锚点，翻面落回同一段）。 */
  function jumpTo(sec, smooth = true) {
    const target = body.querySelector(`[data-sec="${sec}"], [data-part="${sec}"]`);
    if (target) target.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
  }

  /** 原文里比章节更深的那些块没有自己的行，保持上一段高亮不闪 */
  function spy() {
    const marks = [...body.querySelectorAll("[data-sec]")];
    if (!marks.length) return;
    const top = body.getBoundingClientRect().top + 12;
    let hit = marks[0];
    for (const m of marks) {
      if (m.getBoundingClientRect().top <= top) hit = m;
    }
    const key = hit.dataset.part || hit.dataset.sec;
    if (key !== activeSec && root.querySelector(`.rs-toc-row[data-sec="${key}"]`)) {
      activeSec = key;
      paintToc();
    }
  }

  body.addEventListener("scroll", () => {
    if (spyFrame) return;
    spyFrame = requestAnimationFrame(() => {
      spyFrame = 0;
      spy();
    });
  });

  function flipTo(next, sec) {
    if (next === face) return;
    scrollTops[face] = body.scrollTop;
    face = next;
    $("sw-parsed").classList.toggle("is-on", face === "parsed");
    $("sw-source").classList.toggle("is-on", face === "source");
    $("sw-parsed").setAttribute("aria-selected", String(face === "parsed"));
    $("sw-source").setAttribute("aria-selected", String(face === "source"));
    renderBody();
    body.classList.remove("is-flip");
    void body.offsetWidth; // 让动画重跑
    body.classList.add("is-flip");
    if (sec) {
      jumpTo(sec, false);
      activeSec = sec; // 点哪一行就高亮哪一行，滚起来再由 spy 接手
    } else {
      body.scrollTop = scrollTops[face] || 0;
      activeSec = firstKey();
    }
    paintToc();
  }

  $("sw-parsed").addEventListener("click", () => flipTo("parsed"));
  $("sw-source").addEventListener("click", () => flipTo("source"));

  function entryHtml(e, opts) {
    const when = escapeHtml(e.period || "");
    const title = opts.titleOf(e);
    const sub = opts.subOf(e);
    const lines = (e.highlights || [])
      .map((h) => `<li>${escapeHtml(h)}</li>`)
      .join("");
    return `
      <div class="rs-entry">
        <div class="rs-when mono">${when || "—"}</div>
        <div class="rs-what">
          <div class="rs-what-head">
            <strong>${escapeHtml(title)}</strong>
            ${sub ? `<span class="rs-role">${escapeHtml(sub)}</span>` : ""}
          </div>
          ${lines ? `<ul class="rs-hl">${lines}</ul>` : ""}
        </div>
      </div>`;
  }

  function renderParsedFace(d) {
    const basic = d.basic || {};
    const intent = d.intent || {};
    const who = basic.name || (current && current.title) || "未识别到姓名";
    const contact = [basic.phone, basic.email, basic.city].filter(Boolean).join(" · ");

    const intentPills = [
      intent.position && `<span class="pill accent static">${escapeHtml(intent.position)}</span>`,
      intent.city && `<span class="pill static">${escapeHtml(intent.city)}</span>`,
      intent.salary && `<span class="pill static mono">${escapeHtml(intent.salary)}</span>`,
    ]
      .filter(Boolean)
      .join("");

    const missing = SECTIONS.filter((s) => coverage(s).state === "off").map((s) => s.label);

    const sec = (s) => `<section class="rs-sec" data-sec="${escapeHtml(s.sec)}">`;

    const work = (d.work || []).length
      ? `${sec(SECTIONS[2])}<h3 class="rs-h3">工作经历</h3><div class="rs-timeline">
           ${d.work
             .map((w) =>
               entryHtml(w, {
                 titleOf: (x) => x.company || "—",
                 subOf: (x) => x.title || "",
               })
             )
             .join("")}
         </div></section>`
      : "";

    const project = (d.project || []).length
      ? `${sec(SECTIONS[3])}<h3 class="rs-h3">项目经历</h3><div class="rs-timeline">
           ${d.project
             .map((p) =>
               entryHtml(p, {
                 titleOf: (x) => x.name || "—",
                 subOf: (x) => x.role || "",
               })
             )
             .join("")}
         </div></section>`
      : "";

    const education = (d.education || []).length
      ? `${sec(SECTIONS[4])}<h3 class="rs-h3">教育经历</h3><div class="rs-timeline is-flat">
           ${d.education
             .map((e) =>
               entryHtml(
                 { ...e, highlights: [] },
                 {
                   titleOf: (x) => x.school || "—",
                   subOf: (x) => [x.major, x.degree].filter(Boolean).join(" · "),
                 }
               )
             )
             .join("")}
         </div></section>`
      : "";

    const skills = (d.skills || []).length
      ? `${sec(SECTIONS[5])}<h3 class="rs-h3">技能标签</h3>
         <div class="pills">${d.skills.map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join("")}</div></section>`
      : "";

    const selfEval = d.self_evaluation
      ? `${sec(SECTIONS[6])}<h3 class="rs-h3">自我评价</h3>
         <p class="rs-prose">${escapeHtml(d.self_evaluation)}</p></section>`
      : "";

    const summary = d.summary
      ? `${sec(SECTIONS[7])}<h3 class="rs-h3">摘要 <span class="rs-h3-note">匹配页读这段</span></h3>
         <div class="rs-summary">${escapeHtml(d.summary)}</div></section>`
      : "";

    return `
      <header class="rs-masthead" data-sec="基本信息">
        <h2 class="rs-name">${escapeHtml(who)}</h2>
        ${contact ? `<div class="rs-contact mono">${escapeHtml(contact)}</div>` : ""}
        ${
          missing.length
            ? `<div class="rs-gap mono" title="模板里这些段落这次是空的">这次空着：${escapeHtml(missing.join(" / "))}</div>`
            : `<div class="rs-gap is-ok mono">8/8 段全部捕获</div>`
        }
      </header>

      <section class="rs-sec" data-sec="求职意向">
        <h3 class="rs-h3">求职意向</h3>
        ${
          intentPills
            ? `<div class="pills">${intentPills}</div>`
            : `<div class="muted" style="font-size:12.5px">没有期望岗位 / 城市 / 薪资——去「原文」看看是不是没写在简历里。</div>`
        }
      </section>

      ${work}${project}${education}${skills}${selfEval}${summary}

      <footer class="rs-foot">
        <div>
          <div class="rs-foot-title">这份解析可以拿去匹配了</div>
          <div class="muted" style="font-size:12.5px">匹配页选这份简历，对库里的岗位跑人岗匹配。</div>
        </div>
        <a class="btn primary" href="#/match">去匹配这份简历</a>
      </footer>
    `;
  }

  /** 解析结果还没出来时的空骨架：把「会抠出哪八段」先摆给用户看。 */
  function renderSkeleton(note) {
    return `
      <div class="rs-skel">
        <div class="rs-skel-top">
          <div class="rs-sk rs-sk-name"></div>
          <div class="rs-sk rs-sk-line" style="width:38%"></div>
        </div>
        ${SECTIONS.slice(2)
          .map(
            (s) => `
          <div class="rs-skel-sec">
            <div class="rs-sk rs-sk-h"></div>
            <div class="rs-sk rs-sk-line"></div>
            <div class="rs-sk rs-sk-line" style="width:72%"></div>
          </div>`
          )
          .join("")}
      </div>
      <div class="rs-skel-note" id="rs-cta">
        <p>${escapeHtml(note || "这份简历还没解析。解析会用你配的模型抠出八段结构化字段：基本信息 / 求职意向 / 工作经历 / 项目经历 / 教育经历 / 技能标签 / 自我评价 / 摘要。")}</p>
        <button class="btn primary" id="btn-parse-here">开始解析</button>
      </div>`;
  }

  /** 原文面：一个字没改，只把文件自己的标题提上来 */
  function renderSourceFace() {
    const blocks = rawBlocks(current && current.raw);
    const head = `
      <header class="rs-masthead" data-sec="__top">
        <h2 class="rs-name rs-name-src">原文</h2>
        <div class="rs-contact mono">${escapeHtml((current && current.title) || "")} · ${escapeHtml(
          String((current && current.raw && current.raw.length) || 0)
        )} 字 · 按文件自己的标题分段，正文一字未改</div>
      </header>`;

    if (!blocks.length) {
      return (
        head +
        `<section class="rs-sec" data-sec="__raw">
           <pre class="rs-raw">${escapeHtml(((current && current.raw) || "").slice(0, 20000))}</pre>
         </section>`
      );
    }

    return (
      head +
      blocks
        .map(
          (b) => `
        <section class="rs-sec" data-sec="raw-${b.i}"${b.part ? ` data-part="${escapeHtml(b.part)}"` : ""}>
          <div class="rs-src-head">
            <h3 class="rs-h3${b.level >= 3 ? " is-sub" : ""}">${escapeHtml(b.head || "（标题前）")}</h3>
            ${b.part ? `<span class="rs-h3-note">→ ${escapeHtml(b.part)}</span>` : ""}
            <span class="rs-h3-note mono">${escapeHtml(String(b.text.length))} 字</span>
          </div>
          <pre class="rs-raw">${escapeHtml(b.text)}</pre>
        </section>`
        )
        .join("")
    );
  }

  function renderBody(animate = false) {
    let html;
    if (!current) {
      html = renderEmptyState();
    } else if (face === "source") {
      html = renderSourceFace();
    } else if (parsing) {
      html = renderSkeleton("正在调用 LLM 解析，单次调用，十几秒…");
    } else if (current.llm && current.llm.data) {
      html = renderParsedFace(current.llm.data);
    } else {
      html = renderSkeleton();
    }
    body.innerHTML = html;
    body.classList.toggle("is-anim", animate);
    if (animate) setTimeout(() => body.classList.remove("is-anim"), 800);
    const cta = body.querySelector("#btn-parse-here");
    if (cta) cta.addEventListener("click", () => runParse(current.resume_id));
    const drop = body.querySelector("#rs-drop");
    if (drop) {
      drop.addEventListener("click", () => fileInput.click());
      ["dragenter", "dragover"].forEach((ev) =>
        drop.addEventListener(ev, (e) => {
          e.preventDefault();
          drop.classList.add("is-over");
        })
      );
      ["dragleave", "drop"].forEach((ev) =>
        drop.addEventListener(ev, (e) => {
          e.preventDefault();
          drop.classList.remove("is-over");
        })
      );
      drop.addEventListener("drop", (e) => {
        const f = e.dataTransfer && e.dataTransfer.files[0];
        if (f) upload(f);
      });
    }
    rawHas = new Set(rawBlocks(current && current.raw).map((b) => b.part).filter(Boolean));
    renderToc();
    renderMeta();
    renderBar();
    body.scrollTop = animate ? 0 : scrollTops[face] || 0;
    activeSec = firstKey();
    paintToc();
  }

  /** 这一面第一个「目录里点得到」的段落。 */
  function firstKey() {
    for (const m of body.querySelectorAll("[data-sec]")) {
      const k = m.dataset.part || m.dataset.sec;
      if (root.querySelector(`.rs-toc-row[data-sec="${k}"]`)) return k;
    }
    return "";
  }

  function renderEmptyState() {
    return `
      <div class="rs-empty">
        <div class="rs-drop" id="rs-drop">
          <div class="rs-drop-ico">${ICO_UPLOAD}</div>
          <div class="rs-drop-t">把 Markdown 简历拖进来，或点击选择</div>
          <div class="muted" style="font-size:12px">.md / .markdown / .txt · 最大 2MB</div>
        </div>
        <div class="rs-empty-note">
          <div class="rs-toc-sep">解析会抠出这八段</div>
          <div class="rs-chips">${SECTIONS.map((s) => `<span class="pill static">${escapeHtml(s.label)}</span>`).join("")}</div>
        </div>
      </div>`;
  }

  function renderMeta() {
    const m = $("rs-meta");
    if (!current) {
      m.textContent = "";
      return;
    }
    if (face === "source") {
      const n = rawBlocks(current && current.raw).length;
      m.innerHTML = `<span class="muted">原文 · ${escapeHtml(String(n))} 段 · 只读</span>`;
      return;
    }
    const llm = current.llm;
    m.innerHTML = llm
      ? `<span title="${escapeHtml(fmtTime(llm.parsed_at))}">${escapeHtml(llm.model || "—")} · ${escapeHtml(
          relTime(llm.parsed_at)
        )}</span>`
      : `<span class="muted">尚未解析</span>`;
  }

  function renderBar() {
    const lib = $("rs-lib");
    if (!resumes.length) {
      lib.innerHTML = `<span class="muted" style="font-size:12.5px">库里还没有简历</span>`;
    } else {
      lib.innerHTML = resumes
        .map((it) => {
          const parsed = !!(it.llm && it.llm.has_data);
          const on = it.resume_id === (current && current.resume_id);
          return `<button class="rs-chip${on ? " is-on" : ""}" data-id="${escapeHtml(it.resume_id)}"
                          title="${escapeHtml(it.title || it.resume_id)}">
                    <i class="rs-dot" data-state="${parsed ? "on" : "off"}"></i>
                    <span class="rs-chip-t">${escapeHtml(it.title || it.resume_id)}</span>
                  </button>`;
        })
        .join("");
      lib.querySelectorAll(".rs-chip").forEach((b) => {
        b.addEventListener("click", () => selectResume(b.dataset.id));
      });
    }

    const f = $("rs-fresh");
    const hasCur = !!current;
    const llm = current && current.llm;
    if (!llmCfg) {
      f.innerHTML = ""; // 还没读到配置，先别下结论
    } else if (!llmCfg.configured) {
      f.innerHTML = `<span class="pill warn static" title="解析需要先配好 API Key / Base URL / 模型名">LLM 未配置</span>`;
    } else if (llm && llm.data && llm.model && llm.model !== llmCfg.model) {
      f.innerHTML = `<span class="pill warn static" title="解析用的是 ${escapeHtml(
        llm.model
      )}，当前配置是 ${escapeHtml(llmCfg.model)}">模型已变更 · 建议重新解析</span>`;
    } else if (llm && llm.data) {
      f.innerHTML = `<span class="pill ok static">解析与当前模型一致</span>`;
    } else {
      f.innerHTML = "";
    }

    $("btn-upload").disabled = parsing;
    $("btn-parse").disabled = !hasCur || parsing;
    $("btn-parse").textContent = parsing ? "解析中…" : "重新解析";
    $("btn-del").disabled = !hasCur || parsing;
    // 空态那枚 ico 只在按钮内部，disabled 时不用管
  }

  async function loadLlmCfg() {
    try {
      llmCfg = await api.get("/api/llm/config");
    } catch {
      llmCfg = null;
    }
  }

  async function loadList() {
    try {
      const r = await api.get("/api/resume/list");
      resumes = r.items || [];
    } catch {
      resumes = [];
    }
    renderBar();
  }

  async function selectResume(id) {
    if (parsing) return toast("解析中，等它跑完再切", "warn");
    if (current && current.resume_id === id) return;
    try {
      current = await api.get("/api/resume/item/" + id);
      lastResumeId = id;
      scrollTops.parsed = 0;
      scrollTops.source = 0;
      renderBody();
    } catch (err) {
      toast(err.message || "载入失败", "bad");
    }
  }

  async function upload(file) {
    if (parsing) return toast("解析中，等它跑完再传", "warn");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const draft = await api.upload("/api/resume/upload", fd);
      current = draft;
      lastResumeId = draft.resume_id;
      face = "parsed";
      await loadList();
      renderBody();
      toast("简历已上传，开始解析…", "ok");
      await runParse(draft.resume_id);
    } catch (err) {
      toast(err.message || "上传失败", "bad");
    }
  }

  /**
   * 调 LLM 解析一份简历。后端不会覆盖原有解析结果——失败了也要把它留在屏幕上。
   */
  async function runParse(resumeId) {
    if (parsing) return;
    const prev = current && current.llm;
    parsing = true;
    face = "parsed";
    renderBody();
    paintParse("running", "正在调用 LLM 解析，单次调用，十几秒…", 40);
    try {
      const r = await api.post(`/api/resume/item/${resumeId}/parse`);
      if (current && current.resume_id === resumeId) {
        current.llm = r.llm;
      }
      parsing = false;
      paintParse("done", "解析完成", 100);
      renderBody(true);
      toast("解析完成", "ok");
      await loadList();
    } catch (err) {
      parsing = false;
      paintParse("error", err.message || "解析失败", 100);
      renderBody();
      const banner = `
        <div class="banner bad">
          <div><strong>解析失败：</strong>${escapeHtml(err.message || "未知错误")}</div>
          ${
            prev
              ? `<div class="muted mt-8" style="font-size:12px">下面是上一次的解析结果（${escapeHtml(
                  relTime(prev.parsed_at)
                )} · ${escapeHtml(prev.model || "—")}），没有被覆盖。</div>`
              : `<div class="muted mt-8" style="font-size:12px">检查「模型」页的 Key / Base URL / 模型名是否可用。</div>`
          }
        </div>`;
      body.insertAdjacentHTML("afterbegin", banner);
      body.scrollTop = 0;
      toast(err.message || "解析失败", "bad");
    }
  }

  async function remove() {
    if (!current) return toast("没有可删除的简历", "warn");
    if (!confirm(`删除「${current.title || current.resume_id}」？`)) return;
    try {
      await api.del("/api/resume/item/" + current.resume_id);
      if (lastResumeId === current.resume_id) lastResumeId = "";
      current = null;
      face = "parsed";
      renderBody();
      await loadList();
      toast("已删除", "ok");
    } catch (err) {
      toast(err.message || "删除失败", "bad");
    }
  }

  $("btn-upload").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) upload(fileInput.files[0]);
    fileInput.value = "";
  });
  $("btn-parse").addEventListener("click", () => {
    if (!current) return toast("先上传或载入一份简历", "warn");
    runParse(current.resume_id);
  });
  $("btn-del").addEventListener("click", remove);

  ["dragenter", "dragover"].forEach((ev) =>
    bar.addEventListener(ev, (e) => {
      e.preventDefault();
      bar.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    bar.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragleave" && bar.contains(e.relatedTarget)) return;
      bar.classList.remove("is-over");
    })
  );
  bar.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) upload(f);
  });

  renderBody();
  await Promise.all([loadLlmCfg(), loadList()]);
  renderBar();

  if (lastResumeId && resumes.some((x) => x.resume_id === lastResumeId)) {
    await selectResume(lastResumeId);
  } else if (resumes.length) {
    await selectResume(resumes[0].resume_id);
  } else {
    renderBody();
  }

  return () => {
    if (spyFrame) cancelAnimationFrame(spyFrame);
  };
}
