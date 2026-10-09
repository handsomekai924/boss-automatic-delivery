/** 找工作：筛选 · 搜索 · 卡片流 · 删除 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, fmtTime, taskPanel, renderEvents, fmtEta } from "../ui.js";

export async function renderJobs(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">拉取 <span class="grad">职位</span></h1>
        <p class="hero-sub">按条件搜索职位、一张张看、不要的直接删。搜索在后台跑，每翻一页停 1 秒（避免被网站当成机器人），随时可以停。</p>
      </div>
    </div>

    <div class="bento mb-24">
      <div class="card span-12">
        <div class="card-head">
          <h3 class="card-title">搜索设置</h3>
          <span class="card-sub" id="crawl-phase">空闲</span>
        </div>

        <div class="row-3">
          <div class="field">
            <label>最大页数</label>
            <input class="input mono" id="max-pages" type="number" value="5" min="1" max="200">
          </div>
          <div class="field">
            <label>间隔(秒)</label>
            <input class="input mono" id="interval" type="number" value="1" min="0" step="0.5">
          </div>
          <div class="field">
            <label>起始页</label>
            <input class="input mono" id="start-page" type="number" value="1" min="1">
          </div>
        </div>

        <div class="flex center gap-12 mb-16">
          <label class="pill on" id="use-search">
            <input type="checkbox" checked hidden> 使用搜索流（走库里的搜索条件）
          </label>
          <span class="muted" id="filter-sub" style="font-size:12px">—</span>
        </div>

        <div class="divider mb-16"></div>
        <div class="card-sub mb-8">搜索条件</div>

        <div class="row-2">
          <div class="field">
            <label>关键词</label>
            <input class="input" id="f-query" placeholder="如：前端 / Python / 产品经理">
          </div>
          <div class="field">
            <label>薪资</label>
            <select class="select" id="f-salary">
              <option value="">不限</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>城市（省 / 市 / 区 级联）</label>
          <div class="cascader-field" id="city-field">
            <button type="button" class="cascader-trigger" id="city-trigger" aria-haspopup="true" aria-expanded="false">
              <span class="cascader-path" id="city-path">未选择 = 站点当前城市</span>
              <span class="cascader-caret">▾</span>
            </button>
            <div class="cascader-pop hidden" id="city-pop">
              <div class="hot-row" id="city-hot"></div>
              <div class="cascader">
                <div class="cascader-cols">
                  <div class="cascader-col" id="city-col-0"></div>
                  <div class="cascader-col" id="city-col-1"></div>
                  <div class="cascader-col" id="city-col-2"></div>
                </div>
              </div>
              <div class="cascader-foot">
                <button class="btn sm ghost" id="city-clear" type="button">清除</button>
                <button class="btn sm primary" id="city-done" type="button">完成</button>
              </div>
            </div>
          </div>
        </div>
        <div id="filter-pills" class="mb-16"></div>
        <div class="btn-row">
          <button class="btn" id="btn-save-filter">保存条件</button>
          <button class="btn ghost" id="btn-reset-filter">重置</button>
          <span class="muted" id="filter-summary" style="font-size:12px"></span>
          <span class="flex-shrink-0" style="flex:1"></span>
          <button class="btn primary" id="btn-crawl">开始搜索</button>
          <button class="btn danger hidden" id="btn-stop">停止搜索</button>
        </div>
        <div id="crawl-task" class="mt-16"></div>
      </div>
    </div>

    <div class="flex between center mb-16">
      <div class="flex center gap-8">
        <input class="input" id="q" placeholder="在库中搜索岗位/公司" style="width:240px">
        <input class="input" id="city-f" placeholder="城市" style="width:120px">
        <button class="btn" id="btn-filter">筛选</button>
      </div>
      <div class="btn-row">
        <span class="muted" id="total-label"></span>
        <button class="btn" id="btn-select-all">全选本页</button>
        <button class="btn danger" id="btn-del-selected">删除所选</button>
        <button class="btn danger ghost" id="btn-clear">按条件清空</button>
        <button class="btn" id="btn-fetch-desc" title="对还没取到职位描述的职位，逐条打开详情页取回">补全描述</button>
      </div>
    </div>

    <div class="card mb-16 hidden" id="desc-panel">
      <div class="card-head">
        <h3 class="card-title">补全职位描述</h3>
        <span class="card-sub" id="desc-phase">空闲</span>
      </div>
      <div id="desc-task"></div>
      <div class="btn-row mt-12">
        <button class="btn ghost" id="btn-desc-hide">收起</button>
      </div>
    </div>

    <div class="job-grid" id="job-grid"></div>
    <div class="flex center gap-8 mt-24" style="justify-content:center">
      <button class="btn ghost" id="btn-prev">上一页</button>
      <span class="mono muted" id="page-label"></span>
      <button class="btn ghost" id="btn-next">下一页</button>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);
  const selected = new Set();
  let offset = 0;
  const limit = 24;
  let pollTimer = null;
  let crawlTaskId = null;

  // 后端 phase 是英文内部阶段名，页面上得说人话
  const PHASE_LABELS = {
    prepare: "准备中",
    client: "连接中",
    filter: "按条件筛选",
    stoken: "验证身份",
    crawl: "翻页搜索",
  };

  // 统一长任务面板：搜索 + 补全职位描述
  const crawlPanel = taskPanel({ title: "职位搜索", stopLabel: "停止搜索" });
  $("crawl-task").appendChild(crawlPanel.el);
  crawlPanel.onStop(async () => {
    if (!crawlTaskId) return;
    try {
      await api.post(`/api/crawl/${crawlTaskId}/cancel`);
      toast("已请求停止，翻完当前页就收手", "warn");
    } catch (err) {
      toast(err.message, "bad");
    }
  });
  crawlPanel.update({ status: "idle", percent: 0 });

  const descPanelUi = taskPanel({ title: "补全职位描述", stopLabel: "停止补全" });
  $("desc-task").appendChild(descPanelUi.el);
  descPanelUi.onStop(async () => {
    try {
      const s = await api.post("/api/jobs/fetch-descriptions/cancel");
      renderDesc(s);
      toast("已请求停止，取完当前这条就收手", "warn");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  // ---------- 筛选 ----------
  /** @type {{code:string,name:string,children?:any[]}|null} */
  let cityTree = [];
  let hotCities = [];
  /** 级联已选路径：[省, 市, 区?] */
  let cityPath = [];
  let salaryOptions = [];

  function findCityPath(nodes, code, path = []) {
    for (const n of nodes || []) {
      const next = [...path, n];
      if (String(n.code) === String(code)) return next;
      const hit = findCityPath(n.children || [], code, next);
      if (hit) return hit;
    }
    return null;
  }

  function findCityByName(nodes, name, path = []) {
    for (const n of nodes || []) {
      const next = [...path, n];
      if (n.name === name) return next;
      const hit = findCityByName(n.children || [], name, next);
      if (hit) return hit;
    }
    return null;
  }

  function renderCityCascader() {
    const cols = [$("city-col-0"), $("city-col-1"), $("city-col-2")];
    const levels = [
      cityTree,
      cityPath[0]?.children || [],
      cityPath[1]?.children || [],
    ];
    cols.forEach((col, idx) => {
      const items = levels[idx] || [];
      if (!items.length) {
        const hint =
          idx === 0
            ? "选项表为空"
            : idx === 1
              ? (cityPath[0] ? "无下级" : "先选省份")
              : (cityPath[1] ? "无下级" : "先选城市");
        col.innerHTML = `<div class="cascader-empty">${hint}</div>`;
        return;
      }
      col.innerHTML = items
        .map((n) => {
          const picked = cityPath[idx]?.code === n.code;
          const hasKids = (n.children || []).length > 0;
          return `<button type="button" class="cascader-item ${picked ? "picked" : ""}" data-level="${idx}" data-code="${escapeHtml(n.code)}">
            <span>${escapeHtml(n.name)}</span>
            ${hasKids ? '<span class="caret">▸</span>' : ""}
          </button>`;
        })
        .join("");
      col.querySelectorAll(".cascader-item").forEach((btn) => {
        btn.addEventListener("click", () => {
          const level = Number(btn.dataset.level);
          const code = btn.dataset.code;
          const pool = levels[level] || [];
          const node = pool.find((n) => String(n.code) === String(code));
          if (!node) return;
          cityPath = [...cityPath.slice(0, level), node];
          renderCityCascader();
          updateFilterSummary();
          // 选到叶子（没有下级）就算选完，自动收起；省/市还得继续往下钻
          if (!(node.children || []).length) setCityOpen(false);
        });
      });
      // 回填已选项时滚进可视区，别让高亮落在折下
      const picked = col.querySelector(".cascader-item.picked");
      if (picked) picked.scrollIntoView({ block: "nearest" });
    });

    const pathEl = $("city-path");
    if (!cityPath.length) {
      pathEl.textContent = "未选择 = 站点当前城市";
    } else {
      // 直辖市常见「北京 / 北京」，同名只留一层
      const names = [];
      for (const n of cityPath) {
        if (!names.length || names[names.length - 1] !== n.name) names.push(n.name);
      }
      pathEl.textContent = names.join(" / ");
    }
  }

  function renderSalarySelect(selectedCode = "") {
    const sel = $("f-salary");
    sel.innerHTML =
      `<option value="">不限</option>` +
      salaryOptions
        .map(
          (o) =>
            `<option value="${escapeHtml(o.code)}" ${String(o.code) === String(selectedCode) ? "selected" : ""}>${escapeHtml(o.name)}</option>`
        )
        .join("");
  }

  function renderHotCities() {
    $("city-hot").innerHTML = hotCities
      .map((c) => `<span class="pill" data-code="${escapeHtml(c.code)}" data-name="${escapeHtml(c.name)}">${escapeHtml(c.name)}</span>`)
      .join("");
    $("city-hot").querySelectorAll(".pill").forEach((p) => {
      p.addEventListener("click", () => {
        const code = p.dataset.code;
        const byCode = findCityPath(cityTree, code) || findCityByName(cityTree, p.dataset.name);
        // 热点城市可能不在省→市树里（如直辖市拍平），直接构造单节点路径
        cityPath = byCode || [{ code, name: p.dataset.name, children: [] }];
        renderCityCascader();
        updateFilterSummary();
        setCityOpen(false);
      });
    });
  }

  function currentCityCode() {
    return cityPath.length ? String(cityPath[cityPath.length - 1].code) : "";
  }

  function updateFilterSummary() {
    const parts = [];
    const q = $("f-query").value.trim();
    if (q) parts.push(`关键词「${q}」`);
    if (cityPath.length) {
      const names = [];
      for (const n of cityPath) {
        if (!names.length || names[names.length - 1] !== n.name) names.push(n.name);
      }
      parts.push(`城市 ${names.join("/")}`);
    }
    const sal = salaryOptions.find((o) => String(o.code) === String($("f-salary").value));
    if (sal) parts.push(`薪资 ${sal.name}`);
    $("filter-pills").querySelectorAll(".pill.on").forEach((p) => {
      parts.push(p.textContent.trim());
    });
    $("filter-summary").textContent = parts.join(" · ") || "不限（推荐流）";
  }

  async function loadFilter() {
    let cond = null;
    try {
      cond = await api.get("/api/filters/conditions");
      cityTree = cond.city || [];
      hotCities = cond.hotCity || [];
      salaryOptions = cond.salary || [];
      renderHotCities();
      renderSalarySelect();
      renderCityCascader();
      renderPills(cond);
    } catch (err) {
      $("filter-pills").innerHTML = `<div class="banner warn">选项表拿不到：${escapeHtml(err.message)}</div>`;
    }
    try {
      const f = await api.get("/api/filters/search");
      $("f-query").value = f.query || "";
      renderSalarySelect(f.salary || "");
      if (f.city) {
        cityPath = findCityPath(cityTree, f.city) || [{ code: f.city, name: f.city, children: [] }];
      } else {
        cityPath = [];
      }
      renderCityCascader();
      applyPillSelection(f);
      $("filter-sub").textContent = f.is_blank ? "不限" : "搜索流";
      updateFilterSummary();
    } catch (err) {
      toast("读筛选条件失败：" + err.message, "warn");
    }
  }

  function applyPillSelection(f) {
    const wanted = {
      experience: new Set((f.experience || []).map(String)),
      degree: new Set((f.degree || []).map(String)),
      scale: new Set((f.scale || []).map(String)),
    };
    $("filter-pills").querySelectorAll(".pill").forEach((p) => {
      const set = wanted[p.dataset.key];
      if (!set) return;
      p.classList.toggle("on", set.has(String(p.dataset.code)));
    });
  }

  $("f-query").addEventListener("input", updateFilterSummary);
  $("f-salary").addEventListener("change", updateFilterSummary);

  // 城市级联平时只占一行，点击触发按钮才弹出浮层
  const cityField = $("city-field");
  const cityTrigger = $("city-trigger");
  const cityPop = $("city-pop");

  function setCityOpen(open) {
    cityPop.classList.toggle("hidden", !open);
    cityTrigger.classList.toggle("open", open);
    cityTrigger.setAttribute("aria-expanded", open ? "true" : "false");
  }
  const isCityOpen = () => !cityPop.classList.contains("hidden");

  cityTrigger.addEventListener("click", () => setCityOpen(!isCityOpen()));
  cityPop.addEventListener("click", (e) => e.stopPropagation());
  function onDocClick(e) {
    if (isCityOpen() && !cityField.contains(e.target)) setCityOpen(false);
  }
  function onDocKey(e) {
    if (e.key === "Escape") setCityOpen(false);
  }
  document.addEventListener("click", onDocClick);
  document.addEventListener("keydown", onDocKey);

  $("city-done").addEventListener("click", () => setCityOpen(false));
  $("city-clear").addEventListener("click", () => {
    cityPath = [];
    renderCityCascader();
    updateFilterSummary();
    setCityOpen(false);
  });

  function renderPills(cond) {
    const groups = [
      ["experience", "经验", cond.experience],
      ["degree", "学历", cond.degree],
      ["scale", "规模", cond.scale],
    ];
    $("filter-pills").innerHTML = groups
      .map(([key, label, options]) => {
        if (!options || !options.length) return "";
        const pills = options
          .slice(0, 12)
          .map((o) => `<span class="pill" data-key="${key}" data-code="${escapeHtml(o.code ?? o)}">${escapeHtml(o.name ?? o)}</span>`)
          .join("");
        return `<div class="mb-8"><div class="card-sub mb-8">${label}</div><div class="pills">${pills}</div></div>`;
      })
      .join("") + (cond.source && cond.source !== "api" ? `<div class="banner warn">当前选项来自离线兜底（source=${escapeHtml(cond.source)}）</div>` : "");

    // 多选暂存在 data，保存时写进 filter
    $("filter-pills").querySelectorAll(".pill").forEach((p) => {
      p.addEventListener("click", () => {
        p.classList.toggle("on");
        updateFilterSummary();
      });
    });
  }

  function collectFilter() {
    const extras = { experience: [], degree: [], scale: [] };
    $("filter-pills").querySelectorAll(".pill.on").forEach((p) => {
      const k = p.dataset.key;
      if (extras[k]) extras[k].push(p.dataset.code);
    });
    return {
      query: $("f-query").value.trim(),
      city: currentCityCode(),
      jobType: "",
      salary: $("f-salary").value,
      experience: extras.experience,
      degree: extras.degree,
      industry: [],
      scale: extras.scale,
      payType: [],
      partTime: [],
      stage: [],
    };
  }

  $("btn-save-filter").addEventListener("click", async () => {
    try {
      const saved = await api.put("/api/filters/search", collectFilter());
      $("filter-summary").textContent = saved.summary || "";
      toast("筛选条件已保存", "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  $("btn-reset-filter").addEventListener("click", async () => {
    try {
      await api.post("/api/filters/search/reset");
      await loadFilter();
      toast("已重置", "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  // ---------- 搜索 ----------
  $("use-search").addEventListener("click", (e) => {
    const pill = e.currentTarget;
    const cb = pill.querySelector("input");
    cb.checked = !cb.checked;
    pill.classList.toggle("on", cb.checked);
  });

  $("btn-crawl").addEventListener("click", async () => {
    try {
      const task = await api.post("/api/crawl/start", {
        max_pages: Number($("max-pages").value) || 5,
        interval: Number($("interval").value) || 1,
        start_page: Number($("start-page").value) || 1,
        use_search: $("use-search").querySelector("input").checked,
      });
      crawlTaskId = task.task_id;
      $("btn-crawl").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
      toast("搜索已开始", "ok");
      startPoll();
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  $("btn-stop").addEventListener("click", async () => {
    if (!crawlTaskId) return;
    try {
      await api.post(`/api/crawl/${crawlTaskId}/cancel`);
      toast("已请求停止，翻完当前页就收手", "warn");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  function startPoll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      if (!crawlTaskId) return;
      try {
        const s = await api.get(`/api/crawl/${crawlTaskId}`);
        const p = s.progress || {};
        const maxPages = s.params?.max_pages || 5;
        const pct = Math.min(100, Math.round(((p.pages || 0) / maxPages) * 100));
        $("crawl-phase").textContent = PHASE_LABELS[s.phase] || PHASE_LABELS[s.status] || s.phase || "空闲";

        const counts = [
          ["页", `${p.pages || 0} / ${maxPages}`],
          ["新职位", `+${p.inserted || 0}`],
          ["更新", `${p.updated || 0}`],
        ];
        if (p.desc_done || p.desc_skipped) {
          counts.push(["职位描述", `${p.desc_ok || 0}/${p.desc_done || 0} 成功`]);
          if (p.desc_skipped) counts.push(["跳过", `${p.desc_skipped}`]);
          if (p.desc_failed) counts.push(["失败", `${p.desc_failed}`]);
        }

        crawlPanel.update({
          status: s.status,
          percent: s.status === "running" ? pct : 100,
          current: s.status === "running"
            ? (PHASE_LABELS[s.phase] || `第 ${p.pages || 0} 页`)
            : (s.error || s.stopped_reason || ""),
          counts,
          error: s.status === "error" ? s.error : undefined,
          log: renderEvents(s.events, {
            limit: 20,
            nameKey: "event",
          }),
        });

        if (["done", "error", "cancelled"].includes(s.status)) {
          clearInterval(pollTimer);
          pollTimer = null;
          $("btn-crawl").classList.remove("hidden");
          $("btn-stop").classList.add("hidden");
          loadJobs();
          toast(s.status === "error" ? "搜索出错：" + (s.error || "") : "搜索结束", s.status === "error" ? "bad" : "ok");
        }
      } catch { /* ignore */ }
    }, 1000);
  }

  // ---------- 职位列表 ----------
  async function loadJobs() {
    const keyword = $("q").value.trim();
    const city = $("city-f").value.trim();
    const qs = new URLSearchParams({ limit, offset, ...(keyword ? { keyword } : {}), ...(city ? { city } : {}) });
    try {
      const data = await api.get("/api/jobs?" + qs.toString());
      $("total-label").textContent = `共 ${data.total} 条`;
      $("page-label").textContent = `${offset + 1} - ${Math.min(offset + limit, data.total)} / ${data.total}`;
      renderGrid(data.items || []);
    } catch (err) {
      toast("读职位失败：" + err.message, "bad");
    }
  }

  function renderGrid(items) {
    const grid = $("job-grid");
    if (!items.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">◎</div><p>库还是空的，先去上面搜一批</p></div>`;
      return;
    }
    grid.innerHTML = items
      .map((j) => {
        const sel = selected.has(j.encrypt_job_id) ? "selected" : "";
        const labels = (j.job_labels || []).slice(0, 3).map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join("");
        return `
        <div class="card job-card lift ${sel}" data-id="${escapeHtml(j.encrypt_job_id)}">
          <div class="job-check"></div>
          <h3 class="job-title">${escapeHtml(j.job_name)}</h3>
          <div class="job-brand">${escapeHtml(j.brand_name)}</div>
          <div class="job-salary">${escapeHtml(j.salary_desc || "面议")}</div>
          <div class="job-meta">
            <span class="pill static">${escapeHtml(j.location || "—")}</span>
            <span class="pill static">${escapeHtml(j.job_experience || "—")}</span>
            <span class="pill static">${escapeHtml(j.job_degree || "—")}</span>
          </div>
          <div class="job-meta">${labels}</div>
          <div class="muted" style="font-size:11px">${escapeHtml(j.brand_industry || "")} ${escapeHtml(j.brand_scale_name || "")}</div>
        </div>`;
      })
      .join("");

    grid.querySelectorAll(".job-card").forEach((card) => {
      card.addEventListener("click", (e) => {
        const id = card.dataset.id;
        if (e.target.classList.contains("job-check")) {
          if (selected.has(id)) selected.delete(id);
          else selected.add(id);
          card.classList.toggle("selected");
          return;
        }
        openDetail(id);
      });
    });
  }

  async function openDetail(id) {
    try {
      const j = await api.get("/api/jobs/" + encodeURIComponent(id));
      const skills = (j.skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");
      const welfare = (j.welfare_list || []).map((s) => `<span class="pill static">${escapeHtml(s)}</span>`).join(" ");
      modal({
        title: escapeHtml(j.job_name),
        body: `
          <div class="flex between center mb-16">
            <div>
              <div style="font-size:17px;font-weight:650">${escapeHtml(j.brand_name)}</div>
              <div class="muted">${escapeHtml(j.location || "")} · ${escapeHtml(j.brand_industry || "")}</div>
            </div>
            <div class="job-salary">${escapeHtml(j.salary_desc || "面议")}</div>
          </div>
          <div class="job-meta mb-16">
            <span class="pill static">${escapeHtml(j.job_experience || "—")}</span>
            <span class="pill static">${escapeHtml(j.job_degree || "—")}</span>
            <span class="pill static">${escapeHtml(j.brand_scale_name || "—")}</span>
          </div>
          <div class="card-sub mb-8">技能 / 标签</div>
          <div class="pills mb-16">${skills || "<span class='muted'>—</span>"}</div>
          <div class="card-sub mb-8">福利</div>
          <div class="pills mb-16">${welfare || "<span class='muted'>—</span>"}</div>
          <div class="flex between center mb-8">
            <div class="card-sub">职位描述</div>
            <span class="pill ${j.job_desc ? "accent" : "static"}">${j.job_desc ? "已有描述" : "无描述"}</span>
          </div>
          ${j.job_desc
            ? `<details class="acc mb-16"><summary>展开职位描述（${String(j.job_desc).length} 字）</summary><div class="acc-body" style="white-space:pre-wrap">${escapeHtml(j.job_desc)}</div></details>`
            : `<div class="muted mb-16" style="font-size:12px">搜索列表里不带职位描述；点右上「补全描述」，或下次搜索时顺带取回。</div>`}
          <div class="card-sub mb-8">Boss</div>
          <div>${escapeHtml(j.boss_name || "—")} · ${escapeHtml(j.boss_title || "")}</div>
          <div class="btn-row mt-24">
            <button class="btn danger" id="del-one">删除这条</button>
          </div>
        `,
        wide: true,
      });
      setTimeout(() => {
        const btn = document.getElementById("del-one");
        if (btn) {
          btn.addEventListener("click", async () => {
            try {
              await api.del("/api/jobs/" + encodeURIComponent(id));
              toast("已删除", "ok");
              document.querySelector(".modal-backdrop")?.remove();
              loadJobs();
            } catch (err) {
              toast(err.message, "bad");
            }
          });
        }
      }, 0);
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  $("btn-filter").addEventListener("click", () => {
    offset = 0;
    loadJobs();
  });
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { offset = 0; loadJobs(); }
  });

  $("btn-select-all").addEventListener("click", () => {
    root.querySelectorAll(".job-card").forEach((card) => {
      selected.add(card.dataset.id);
      card.classList.add("selected");
    });
  });

  $("btn-del-selected").addEventListener("click", async () => {
    if (!selected.size) return toast("先点卡片右上角的勾选", "warn");
    if (!confirm(`确认删除选中的 ${selected.size} 条职位？`)) return;
    try {
      const r = await api.post("/api/jobs/delete", { ids: [...selected] });
      toast(`已删除 ${r.deleted} 条`, "ok");
      selected.clear();
      loadJobs();
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  $("btn-clear").addEventListener("click", async () => {
    const keyword = $("q").value.trim();
    const city = $("city-f").value.trim();
    const scope = keyword || city ? `条件（${[keyword, city].filter(Boolean).join(" / ")}）` : "全部";
    if (!confirm(`确认清空 ${scope} 的职位？此操作不可恢复。`)) return;
    try {
      const r = await api.post("/api/jobs/clear", {
        confirm: true,
        keyword: keyword || null,
        city: city || null,
      });
      toast(`已清空 ${r.deleted} 条`, "ok");
      loadJobs();
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  // ---------- 手动补全职位描述 ----------
  let descPollTimer = null;

  function renderDesc(s) {
    const p = s.progress || {};
    const total = p.total || 0;
    const done = p.done || 0;
    const interval = Number(p.interval || 1);
    const running = s.status === "running";
    const panel = $("desc-panel");
    panel.classList.remove("hidden");
    $("desc-phase").textContent = running ? "进行中" : "空闲";
    $("btn-fetch-desc").disabled = running;
    $("btn-fetch-desc").textContent = running ? `补全 ${done}/${total}` : "补全描述";

    // 撞安全网关停批时，从事件里捞出原因展示
    const stopEvent = (s.events || []).find((e) => e.event === "stopped");
    const eta = running ? fmtEta((total - done) * (interval + 0.6)) : undefined;

    descPanelUi.update({
      status: s.status,
      percent: total ? (done / total) * 100 : running ? 5 : 100,
      current: running ? (p.current || "拉取中") : (stopEvent?.reason || s.error || ""),
      counts: [
        ["进度", `${done}/${total}`],
        ["有描述", `${p.ok || 0}`],
        ["空", `${p.skipped || 0}`],
        ["失败", `${p.failed || 0}`],
        ...(p.already ? [["跳过已有", `${p.already}`]] : []),
        ["间隔", `${interval}s`],
      ],
      eta,
      error: s.status === "error" ? s.error : undefined,
      log: renderEvents(s.events, { limit: 20, nameKey: "event" }),
    });
  }

  function stopDescPoll() {
    clearInterval(descPollTimer);
    descPollTimer = null;
    $("btn-fetch-desc").disabled = false;
    $("btn-fetch-desc").textContent = "补全描述";
  }

  function startDescPoll() {
    clearInterval(descPollTimer);
    descPollTimer = setInterval(async () => {
      try {
        const s = await api.get("/api/jobs/fetch-descriptions/status");
        renderDesc(s);
        if (["done", "error", "cancelled"].includes(s.status)) {
          stopDescPoll();
          const p = s.progress || {};
          const stopEv = (s.events || []).find((e) => e.event === "stopped");
          toast(
            s.status === "done"
              ? stopEv
                ? stopEv.reason || "已停"
                : `补全完成：有描述 ${p.ok || 0} / 空 ${p.skipped || 0} / 失败 ${p.failed || 0}`
              : s.status === "error"
                ? "补全出错：" + (s.error || "")
                : "补全已取消",
            s.status === "error" ? "bad" : stopEv ? "warn" : "ok"
          );
          loadJobs();
        }
      } catch { /* ignore */ }
    }, 1200);
  }

  $("btn-fetch-desc").addEventListener("click", async () => {
    if (descPollTimer) return toast("补全任务已在跑", "warn");
    try {
      // interval 不传 = 服务端默认 1s（防风控）
      const task = await api.post("/api/jobs/fetch-descriptions", { limit: 0 });
      toast(`补全已开始（${task.progress?.total ?? "?"} 条）`, "ok");
      renderDesc(task);
      startDescPoll();
    } catch (err) {
      stopDescPoll();
      toast(err.message, "bad");
    }
  });

  $("btn-desc-hide").addEventListener("click", () => {
    if (descPollTimer) return toast("还在跑，先停掉再收起", "warn");
    $("desc-panel").classList.add("hidden");
  });

  $("btn-prev").addEventListener("click", () => {
    offset = Math.max(0, offset - limit);
    loadJobs();
  });
  $("btn-next").addEventListener("click", () => {
    offset += limit;
    loadJobs();
  });

  await loadFilter();
  await loadJobs();

  // 恢复进行中的搜索
  try {
    const s = await api.get("/api/crawl/status");
    if (s.task_id && s.status === "running") {
      crawlTaskId = s.task_id;
      $("btn-crawl").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
      startPoll();
    }
  } catch { /* ignore */ }

  // 恢复进行中的补全
  try {
    const s = await api.get("/api/jobs/fetch-descriptions/status");
    if (s.task_id && s.status === "running") {
      renderDesc(s);
      startDescPoll();
    } else if (s.task_id) {
      renderDesc(s); // 上一轮结果留着看
    }
  } catch { /* ignore */ }

  return () => {
    clearInterval(pollTimer);
    clearInterval(descPollTimer);
    document.removeEventListener("click", onDocClick);
    document.removeEventListener("keydown", onDocKey);
  };
}
