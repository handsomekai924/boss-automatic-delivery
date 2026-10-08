/** 职位航段：筛选 · 抓取 · 卡片流 · 删除 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, fmtTime } from "../ui.js";

export async function renderJobs(root) {
  root.innerHTML = `
    <h1 class="hero-title">职位 <span class="grad">舱库</span></h1>
    <p class="hero-sub">按条件抓取、卡片化浏览、选中即删。抓取是后台任务，翻页硬间隔 1 秒防风控，随时可停。</p>

    <div class="bento mb-24">
      <div class="card span-5">
        <div class="card-head">
          <h3 class="card-title">抓取控制台</h3>
          <span class="card-sub" id="crawl-phase">idle</span>
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
          <label class="pill ${"on"}" id="use-search">
            <input type="checkbox" checked hidden> 使用搜索流（走库里的搜索条件）
          </label>
        </div>
        <div class="progress mb-8"><i id="crawl-bar" style="width:0%"></i></div>
        <div class="flex between center">
          <span class="muted mono" id="crawl-msg" style="font-size:12px">待命</span>
          <div class="btn-row">
            <button class="btn primary" id="btn-crawl">开始抓取</button>
            <button class="btn danger hidden" id="btn-stop">停止</button>
          </div>
        </div>
        <div class="ticker mt-16" id="crawl-log"><div class="ev muted">—</div></div>
      </div>

      <div class="card span-7">
        <div class="card-head">
          <h3 class="card-title">搜索条件</h3>
          <span class="card-sub" id="filter-sub">—</span>
        </div>
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
          <button class="btn primary" id="btn-save-filter">保存条件</button>
          <button class="btn ghost" id="btn-reset-filter">重置</button>
          <span class="muted" id="filter-summary" style="font-size:12px"></span>
        </div>
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
        <button class="btn" id="btn-fetch-desc" title="对还没抓到 JD 的职位逐条拉详情">补抓描述</button>
      </div>
    </div>

    <div class="card mb-16 hidden" id="desc-panel">
      <div class="card-head">
        <h3 class="card-title">补抓职位描述</h3>
        <span class="card-sub" id="desc-phase">idle</span>
      </div>
      <div class="progress mb-8"><i id="desc-bar" style="width:0%"></i></div>
      <div class="flex between center gap-12" style="flex-wrap:wrap">
        <div class="muted mono" id="desc-msg" style="font-size:12px">待命</div>
        <div class="btn-row">
          <button class="btn danger hidden" id="btn-desc-stop">停止</button>
          <button class="btn ghost" id="btn-desc-hide">收起</button>
        </div>
      </div>
      <div class="ticker mt-12" id="desc-log" style="max-height:120px"><div class="ev muted">—</div></div>
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

  // ---------- 抓取 ----------
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
      toast("抓取任务已启动", "ok");
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
        $("crawl-bar").style.width = (s.status === "running" ? pct : 100) + "%";
        $("crawl-phase").textContent = s.phase || s.status;
        $("crawl-msg").textContent =
          s.status === "running"
            ? `第 ${p.pages || 0} 页 · 入库 +${p.inserted || 0} / 改 ${p.updated || 0}` +
              (p.desc_done || p.desc_skipped
                ? ` · JD ${p.desc_ok || 0}/${p.desc_done || 0}` + (p.desc_skipped ? `（跳过已有 ${p.desc_skipped}）` : "")
                : "")
            : s.error || s.stopped_reason || s.status;
        const log = $("crawl-log");
        if (s.events && s.events.length) {
          log.innerHTML = s.events
            .slice(-20)
            .map((e) => {
              const t = e.at ? new Date(e.at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
              return `<div class="ev"><time>${t}</time><span class="name">p${e.page ?? e.event ?? ""}</span><span>raw ${e.raw_count ?? "-"} kept ${e.kept_count ?? "-"} +${e.inserted ?? "-"}</span></div>`;
            })
            .join("");
          log.scrollTop = log.scrollHeight;
        }
        if (["done", "error", "cancelled"].includes(s.status)) {
          clearInterval(pollTimer);
          pollTimer = null;
          $("btn-crawl").classList.remove("hidden");
          $("btn-stop").classList.add("hidden");
          loadJobs();
          toast(s.status === "error" ? "抓取出错：" + (s.error || "") : "抓取结束", s.status === "error" ? "bad" : "ok");
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
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="empty-icon">◎</div><p>库还是空的，先去左边抓一批</p></div>`;
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
            <span class="pill ${j.job_desc ? "accent" : "static"}">${j.job_desc ? "已抓描述" : "无描述"}</span>
          </div>
          ${j.job_desc
            ? `<details class="acc mb-16"><summary>展开 JD（${String(j.job_desc).length} 字）</summary><div class="acc-body" style="white-space:pre-wrap">${escapeHtml(j.job_desc)}</div></details>`
            : `<div class="muted mb-16" style="font-size:12px">列表接口不带 JD；点右上「补抓描述」或在抓取时顺带补。</div>`}
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

  // ---------- 手动补抓 JD ----------
  let descPollTimer = null;

  function fmtEta(sec) {
    if (!isFinite(sec) || sec <= 0) return "—";
    const s = Math.round(sec);
    if (s < 60) return `${s} 秒`;
    return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  }

  function renderDesc(s) {
    const p = s.progress || {};
    const total = p.total || 0;
    const done = p.done || 0;
    const interval = Number(p.interval || 1);
    const running = s.status === "running";
    const panel = $("desc-panel");
    panel.classList.remove("hidden");
    $("desc-phase").textContent = s.status;
    $("desc-bar").style.width = (total ? Math.min(100, Math.round((done / total) * 100)) : running ? 5 : 100) + "%";
    $("btn-desc-stop").classList.toggle("hidden", !running);
    $("btn-fetch-desc").disabled = running;
    $("btn-fetch-desc").textContent = running ? `补抓 ${done}/${total}` : "补抓描述";

    const counts =
      `已抓 ${done}/${total} · 有描述 ${p.ok || 0} · 空 ${p.skipped || 0} · 失败 ${p.failed || 0}` +
      (p.already ? ` · 跳过已有 ${p.already}` : "");
    // 撞安全网关停批时，从事件里捞出原因展示
    const stopEvent = (s.events || []).find((e) => e.event === "stopped");
    if (running) {
      // 请求本身也要一点时间，按 interval + 0.6s 估剩余
      const eta = fmtEta((total - done) * (interval + 0.6));
      $("desc-msg").textContent =
        `${counts} · 间隔 ${interval}s · 约剩 ${eta}` + (p.current ? ` · 当前：${p.current}` : "");
    } else {
      $("desc-msg").textContent =
        counts +
        (stopEvent
          ? ` · ${stopEvent.reason || "已停"}`
          : s.error
            ? ` · ${s.error}`
            : s.status === "cancelled"
              ? " · 已取消"
              : " · 完成");
    }

    const log = $("desc-log");
    if (s.events && s.events.length) {
      log.innerHTML = s.events
        .slice(-20)
        .map((e) => {
          const t = e.at
            ? new Date(e.at * 1000).toLocaleTimeString("zh-CN", { hour12: false })
            : "";
          const label =
            e.event === "item_error"
              ? `✗ ${e.job_name || ""} ${e.message || ""}`
              : e.event === "item_done"
                ? `${e.has_desc ? "✓" : "○"} ${e.job_name || ""}`
                : e.event === "item_skipped"
                  ? `– ${e.job_name || ""} ${e.reason || "跳过"}`
                  : e.event === "stopped"
                    ? `⛔ ${e.reason || "已停"}`
                    : e.event || "";
          return `<div class="ev"><time>${t}</time><span class="name">${escapeHtml(String(label))}</span></div>`;
        })
        .join("");
      log.scrollTop = log.scrollHeight;
    }
  }

  function stopDescPoll() {
    clearInterval(descPollTimer);
    descPollTimer = null;
    $("btn-fetch-desc").disabled = false;
    $("btn-fetch-desc").textContent = "补抓描述";
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
                : `补抓完成：有描述 ${p.ok || 0} / 空 ${p.skipped || 0} / 失败 ${p.failed || 0}`
              : s.status === "error"
                ? "补抓出错：" + (s.error || "")
                : "补抓已取消",
            s.status === "error" ? "bad" : stopEv ? "warn" : "ok"
          );
          loadJobs();
        }
      } catch { /* ignore */ }
    }, 1200);
  }

  $("btn-fetch-desc").addEventListener("click", async () => {
    if (descPollTimer) return toast("补抓任务已在跑", "warn");
    try {
      // interval 不传 = 服务端默认 1s（防风控）
      const task = await api.post("/api/jobs/fetch-descriptions", { limit: 0 });
      toast(`补抓已启动（${task.progress?.total ?? "?"} 条）`, "ok");
      renderDesc(task);
      startDescPoll();
    } catch (err) {
      stopDescPoll();
      toast(err.message, "bad");
    }
  });

  $("btn-desc-stop").addEventListener("click", async () => {
    try {
      const s = await api.post("/api/jobs/fetch-descriptions/cancel");
      renderDesc(s);
      toast("已请求停止，抓完当前这条就收手", "warn");
    } catch (err) {
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

  // 恢复进行中的抓取
  try {
    const s = await api.get("/api/crawl/status");
    if (s.task_id && s.status === "running") {
      crawlTaskId = s.task_id;
      $("btn-crawl").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
      startPoll();
    }
  } catch { /* ignore */ }

  // 恢复进行中的补抓
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
