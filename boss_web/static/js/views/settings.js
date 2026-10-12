/** 系统配置：AI、采集参数、搜索条件和黑名单规则。 */

import { api } from "../api.js";
import { toast, escapeHtml, modal, multiSelect, cascader } from "../ui.js";
import { renderLLM } from "./llm.js";

export async function renderSettings(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">系统 <span class="grad">配置</span></h1>
        <p class="hero-sub">AI 接口、BOSS 搜索条件、采集参数和黑名单规则集中管理。修改后先保存，当前采集任务使用启动时的配置快照。</p>
      </div>
    </div>
    <div class="settings-shell">
      <aside class="card settings-nav" aria-label="系统配置目录">
        <button class="settings-tab active" data-tab="ai" type="button">AI 设置</button>
        <button class="settings-tab" data-tab="collection" type="button">采集配置</button>
      </aside>
      <section class="card settings-pane" id="settings-pane"></section>
    </div>
  `;
  const pane = root.querySelector("#settings-pane");
  const tabs = [...root.querySelectorAll(".settings-tab")];
  let active = "ai";
  let dirty = false;

  function setDirty(value) {
    dirty = Boolean(value);
    const title = root.querySelector(".hero-sub");
    if (title) title.textContent = dirty
      ? "有未保存修改。切换目录前请先保存或确认放弃修改。"
      : "AI 接口、BOSS 搜索条件、采集参数和黑名单规则集中管理。修改后先保存，当前采集任务使用启动时的配置快照。";
  }

  function setActive(name) {
    active = name;
    tabs.forEach((tab) => {
      const on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-current", on ? "page" : "false");
    });
  }

  async function show(name) {
    if (name === active && pane.childElementCount) return;
    if (dirty && !window.confirm("当前目录有未保存修改，确定放弃并切换吗？")) return;
    setDirty(false);
    setActive(name);
    pane.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>加载中…</p></div>`;
    if (name === "ai") {
      await renderLLM(pane, setDirty);
      return;
    }
    await renderCollection(pane, setDirty);
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => show(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        show(tab.dataset.tab);
      }
    });
  });
  await show("ai");
  return () => {};
}

async function renderCollection(root, markDirty) {
  root.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div><h2 class="card-title">采集配置</h2><div class="card-sub">完整搜索条件和采集行为</div></div>
      <div class="btn-row"><button class="btn" id="collection-reset">重置</button><button class="btn primary" id="collection-save">保存全部</button></div>
    </div>
    <div class="settings-scroll">
      <div class="row-4">
        <div class="field"><label>最大页数</label><input class="input" id="c-max-pages" type="number" min="1" max="200"></div>
        <div class="field"><label>起始页</label><input class="input" id="c-start-page" type="number" min="1"></div>
        <div class="field"><label>页间隔（秒）</label><input class="input" id="c-page-interval" type="number" min="0" max="30" step="0.1"></div>
        <div class="field"><label>详情间隔（秒）</label><input class="input" id="c-detail-interval" type="number" min="0" max="30" step="0.1"></div>
      </div>
      <div class="row-3">
        <label class="pill on"><input type="checkbox" id="c-use-search"> 使用搜索条件</label>
        <label class="pill on"><input type="checkbox" id="c-fetch-details"> 抓取职位详情 / JD</label>
        <label class="pill on"><input type="checkbox" id="c-store-blacklisted"> 入库并标记黑名单职位</label>
      </div>
      <div class="divider"></div>
      <div class="card-sub mb-8">BOSS 搜索条件 <span class="muted" style="font-size:12px">（筛选值请从下拉里选，提交的是接口要的 code）</span></div>
      <div id="search-source" class="muted mb-8"></div>
      <div class="row-2">
        <div class="field"><label>关键词</label><input class="input" id="s-query" autocomplete="off"></div>
        <div class="field"><label>每页条数</label><input class="input" id="s-pageSize" type="number" min="1" max="50"></div>
        <div class="field"><label>城市</label><div id="s-city"></div></div>
        <div class="field"><label>职位类型</label><select class="select" id="s-jobType"></select></div>
        <div class="field"><label>薪资</label><select class="select" id="s-salary"></select></div>
        <div class="field"><label>经验</label><div id="s-experience"></div></div>
        <div class="field"><label>学历</label><div id="s-degree"></div></div>
        <div class="field"><label>行业</label><div id="s-industry"></div></div>
        <div class="field"><label>规模</label><div id="s-scale"></div></div>
        <div class="field"><label>薪资方式</label><div id="s-payType"></div></div>
        <div class="field"><label>兼职</label><div id="s-partTime"></div></div>
        <div class="field"><label>融资阶段</label><div id="s-stage"></div></div>
      </div>
      <div class="divider"></div>
      <div class="flex between center wrap gap-8 mb-8"><div class="card-sub">黑名单规则</div><div class="btn-row"><button class="btn sm" id="rule-add-company">新增公司规则</button><button class="btn sm" id="rule-add-jd">新增 JD 规则</button><button class="btn sm ghost" id="rules-recheck">重新检查已入库职位</button></div></div>
      <div id="recheck-status" class="muted mb-8" role="status"></div>
      <div id="rules-list"></div>
    </div>
  `;
  const $ = (id) => root.querySelector("#" + id);
  const fields = ["c-max-pages", "c-start-page", "c-page-interval", "c-detail-interval", "s-query", "s-pageSize"];
  fields.forEach((id) => $(id).addEventListener("input", () => markDirty(true)));
  ["c-use-search", "c-fetch-details", "c-store-blacklisted"].forEach((id) => $(id).addEventListener("change", () => markDirty(true)));
  ["s-jobType", "s-salary"].forEach((id) => $(id).addEventListener("change", () => markDirty(true)));

  // 城市/行业是级联（省→市→区 / 分组→叶子），树很大，摊成 option 会卡死初始化
  const cityPicker = cascader({ onChange: () => markDirty(true) });
  cityPicker.el.id = "s-city";
  $("s-city").replaceWith(cityPicker.el);
  const industryPicker = cascader({ multiple: true, onChange: () => markDirty(true) });
  industryPicker.el.id = "s-industry";
  $("s-industry").replaceWith(industryPicker.el);

  // 其余多选维度用勾选下拉，不再用原生 select multiple（那是列表框，选项一直摊开）
  const multiIds = ["s-experience", "s-degree", "s-scale", "s-payType", "s-partTime", "s-stage"];
  const multis = {};
  multiIds.forEach((id) => {
    const ms = multiSelect({ onChange: () => markDirty(true) });
    ms.el.id = id;
    $(id).replaceWith(ms.el);
    multis[id] = ms;
  });

  function fillSelect(select, options, { blankLabel = "不限" } = {}) {
    const current = select.value;
    select.innerHTML = `<option value="">${blankLabel}</option>`;
    options.forEach((opt) => {
      const el = document.createElement("option");
      el.value = opt.code;
      el.textContent = opt.name || opt.code;
      select.appendChild(el);
    });
    if (current && [...select.options].some((o) => o.value === current)) select.value = current;
  }

  /** 行业 DTO 是分组 + options，转成级联的两层树；只有叶子是提交值 */
  function industryNodes(groups) {
    return (groups || []).map((g) => ({
      code: `g:${g.name}`,
      name: g.name,
      children: (g.options || []).map((o) => ({ code: String(o.code), name: o.name || String(o.code) })),
    }));
  }

  async function loadConditions() {
    const cond = await api.get("/api/filters/conditions");
    const sourceEl = $("search-source");
    if (sourceEl) {
      const degraded = cond.degraded && Object.keys(cond.degraded).length
        ? `（部分降级：${Object.entries(cond.degraded).map(([k, v]) => `${k}=${v}`).join(", ")}）`
        : "";
      sourceEl.textContent = `筛选选项来源：${cond.source || "api"}${degraded}`;
    }
    cityPicker.setNodes(cond.city?.length ? cond.city : (cond.hotCity || []));
    industryPicker.setNodes(industryNodes(cond.industry || []));
    fillSelect($("s-jobType"), cond.jobType || []);
    fillSelect($("s-salary"), cond.salary || []);
    multis["s-experience"].setOptions(cond.experience || []);
    multis["s-degree"].setOptions(cond.degree || []);
    multis["s-scale"].setOptions(cond.scale || []);
    multis["s-payType"].setOptions(cond.payType || []);
    multis["s-partTime"].setOptions(cond.partTime || []);
    multis["s-stage"].setOptions(cond.stage || []);
    return cond;
  }

  function multiValues(id) {
    return multis[id].getValues();
  }
  function fill(data) {
    const c = data.collection || {};
    $("c-max-pages").value = c.max_pages ?? 5;
    $("c-start-page").value = c.start_page ?? 1;
    $("c-page-interval").value = c.page_interval ?? 1;
    $("c-detail-interval").value = c.detail_interval ?? 1;
    $("c-use-search").checked = c.use_search !== false;
    $("c-fetch-details").checked = c.fetch_details !== false;
    $("c-store-blacklisted").checked = c.store_blacklisted !== false;
    const s = data.search || {};
    $("s-query").value = s.query || "";
    $("s-pageSize").value = s.pageSize ?? 15;
    // code 值直接落到控件（选项已由 loadConditions 填好）
    cityPicker.setValue(s.city || "");
    industryPicker.setValue(s.industry || []);
    if ($("s-jobType")) $("s-jobType").value = s.jobType || "";
    if ($("s-salary")) $("s-salary").value = s.salary || "";
    const multiMap = {
      "s-experience": s.experience || [],
      "s-degree": s.degree || [],
      "s-scale": s.scale || [],
      "s-payType": s.payType || [],
      "s-partTime": s.partTime || [],
      "s-stage": s.stage || [],
    };
    Object.entries(multiMap).forEach(([id, values]) => {
      multis[id].setValues(values || []);
    });
  }
  function payload() {
    return {
      query: $("s-query").value.trim(),
      city: cityPicker.getValue(),
      jobType: $("s-jobType").value,
      salary: $("s-salary").value,
      experience: multiValues("s-experience"),
      degree: multiValues("s-degree"),
      industry: industryPicker.getValue(),
      scale: multiValues("s-scale"),
      payType: multiValues("s-payType"),
      partTime: multiValues("s-partTime"),
      stage: multiValues("s-stage"),
      pageSize: Number($("s-pageSize").value) || 15,
      max_pages: Number($("c-max-pages").value) || 5, start_page: Number($("c-start-page").value) || 1, page_interval: Number($("c-page-interval").value) || 0,
      use_search: $("c-use-search").checked, fetch_details: $("c-fetch-details").checked, detail_interval: Number($("c-detail-interval").value) || 0, store_blacklisted: $("c-store-blacklisted").checked,
    };
  }
  async function load() {
    try {
      await loadConditions();
      fill(await api.get("/api/collection/config"));
      await loadRules();
      markDirty(false);
    } catch (err) { toast(err.message || "读取采集配置失败", "bad"); }
  }
  async function loadRules() {
    const data = await api.get("/api/blacklist/rules");
    $("rules-list").innerHTML = (data.items || []).map((r) => `
      <div class="settings-rule flex between center gap-8 wrap" data-rule="${escapeHtml(r.rule_id)}">
        <div><strong>${escapeHtml(r.pattern)}</strong> <span class="pill static">${escapeHtml(r.rule_type)} / ${escapeHtml(r.match_mode)}</span>
          <div class="muted" style="font-size:12px">${escapeHtml(r.description || "无描述")} · 命中 ${r.hit_count || 0} 次${r.last_error ? ` · 规则错误：${escapeHtml(r.last_error)}` : ""}</div></div>
        <div class="btn-row"><button class="btn sm ghost rule-test">测试</button><button class="btn sm ghost rule-hits">命中详情</button><button class="btn sm ghost rule-edit">编辑</button><button class="btn sm ghost rule-toggle">${r.enabled ? "停用" : "启用"}</button><button class="btn sm danger rule-delete">删除</button></div>
      </div>`).join("") || `<div class="muted">还没有黑名单规则</div>`;
    $("rules-list").querySelectorAll(".settings-rule").forEach((row) => {
      const id = row.dataset.rule;
      const rule = (data.items || []).find((item) => item.rule_id === id);
      row.querySelector(".rule-toggle").addEventListener("click", async () => { try { await api.post(`/api/blacklist/rules/${id}/toggle`, { enabled: !rule.enabled }); await loadRules(); markDirty(false); } catch (err) { toast(err.message, "bad"); } });
      row.querySelector(".rule-delete").addEventListener("click", async () => { if (!window.confirm("删除当前规则？历史命中记录不会删除。")) return; try { await api.del(`/api/blacklist/rules/${id}`); await loadRules(); toast("规则已删除", "ok"); } catch (err) { toast(err.message, "bad"); } });
      row.querySelector(".rule-hits").addEventListener("click", async () => {
        try {
          const hits = await api.get(`/api/blacklist/rules/${id}/hits?limit=100`);
          const body = document.createElement("div");
          body.innerHTML = (hits.items || []).map((hit) => `<div class="settings-rule"><strong>${escapeHtml(hit.job_name || "未知职位")}</strong><div>${escapeHtml(hit.brand_name || "")} · ${escapeHtml(hit.matched_text || "")}</div><div class="muted">${escapeHtml(hit.action || "")} · ${escapeHtml(hit.created_at || "")}</div></div>`).join("") || `<div class="muted">暂无命中记录</div>`;
          modal({ title: `规则命中详情：${escapeHtml(rule.pattern)}`, body, wide: true });
        } catch (err) { toast(err.message, "bad"); }
      });
      row.querySelector(".rule-test").addEventListener("click", () => openRuleModal(rule, true));
      row.querySelector(".rule-edit").addEventListener("click", () => openRuleModal(rule, false));
    });
  }

  function openRuleModal(existing = {}, testOnly = false) {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="field hidden"><label>规则类型</label><select class="select" id="rule-type"><option value="company">公司黑名单</option><option value="jd_keyword">JD 关键词</option></select></div>
      <div class="field"><label>规则内容</label><input class="input" id="rule-pattern" autocomplete="off"></div>
      <div class="field"><label>匹配方式</label><select class="select" id="rule-mode"><option value="exact">精确匹配</option><option value="contains">包含匹配</option><option value="regex">正则匹配</option></select>
        <div class="muted" id="rule-mode-help" style="font-size:12px"></div>
      </div>
      <div class="field" id="rule-scope-field"><label>匹配范围</label><select class="select" id="rule-scope"><option value="both">职位名 + JD 正文</option><option value="job_name">职位名</option><option value="job_desc">JD 正文</option></select></div>
      <div class="field"><label>说明</label><input class="input" id="rule-description"></div>
      <div class="field ${testOnly ? "" : "hidden"}" id="rule-test-fields"><label>测试职位名</label><input class="input" id="rule-job-name"><label>测试 JD 正文</label><textarea class="input" id="rule-job-desc" rows="4"></textarea><label>测试公司名</label><input class="input" id="rule-brand"></div>
      <div class="btn-row mt-16"><button class="btn" id="rule-validate" type="button">${testOnly ? "测试匹配" : "验证规则"}</button>${testOnly ? "" : `<button class="btn primary" id="rule-submit" type="button">保存规则</button>`}</div>
      <div class="banner info mt-12" id="rule-result">正则规则保存或启用前会在服务端验证。</div>`;
    const get = (id) => body.querySelector("#" + id);
    get("rule-type").value = existing.rule_type || "company";
    get("rule-pattern").value = existing.pattern || "";
    get("rule-mode").value = existing.match_mode || (existing.rule_type === "company" ? "exact" : "contains");
    get("rule-scope").value = existing.scope || "both";
    get("rule-description").value = existing.description || "";
    const ruleModeHelpContent = () => {
      const ruleType = get("rule-type").value;
      const ruleMode = get("rule-mode").value;
      if (ruleType === "company") {
        return ruleMode === "exact"
          ? "精确匹配：公司名标准化后全等才算命中。"
          : "正则匹配：按正则搜索公司名，保存/启用前会校验正则。";
      } else {
        return ruleMode === "contains"
          ? "包含匹配：文本里出现规则内容即命中。"
          : "正则匹配：按正则搜索职位名/JD 正文，保存/启用前会校验正则。";
      }
    }
    const updateMode = () => {
      const company = get("rule-type").value === "company";
      get("rule-scope-field").classList.toggle("hidden", company);
      get("rule-mode").innerHTML = company
        ? `<option value="exact">精确匹配</option><option value="regex">正则匹配</option>`
        : `<option value="contains">包含匹配</option><option value="regex">正则匹配</option>`;
      if (![...get("rule-mode").options].some((option) => option.value === get("rule-mode").value)) get("rule-mode").value = company ? "exact" : "contains";
      get("rule-mode-help").textContent = ruleModeHelpContent();
      get("rule-mode").addEventListener("change", () => {
        get("rule-mode-help").textContent = ruleModeHelpContent();
      });
    };
    get("rule-type").addEventListener("change", updateMode);
    updateMode();
    const dialog = modal({ title: testOnly ? "测试黑名单规则" : (existing.rule_id ? "编辑黑名单规则" : "新增黑名单规则"), body, wide: true });
    get("rule-validate").addEventListener("click", async () => {
      try {
        const ruleType = get("rule-type").value;
        const isCompany = ruleType === "company";
        const payload = {
          rule_type: ruleType,
          pattern: get("rule-pattern").value.trim(),
          match_mode: get("rule-mode").value,
          scope: isCompany ? "" : get("rule-scope").value,
          description: get("rule-description").value.trim(),
          brand_name: get("rule-brand")?.value || "",
          job_name: get("rule-job-name")?.value || "",
          job_desc: get("rule-job-desc")?.value || "",
        };
        if (testOnly) {
          const result = await api.post("/api/blacklist/test", payload);
          get("rule-result").textContent = result.matched ? `命中 ${result.matches.length} 条` : "未命中";
        } else {
          await api.post("/api/blacklist/test", payload);
          get("rule-result").textContent = "规则格式有效，可以保存";
        }
      } catch (err) { get("rule-result").textContent = err.message || "规则无效"; }
    });
    if (!testOnly) get("rule-submit").addEventListener("click", async () => {
      try {
        const ruleType = get("rule-type").value;
        const isCompany = ruleType === "company";
        const payload = {
          rule_type: ruleType,
          pattern: get("rule-pattern").value.trim(),
          match_mode: get("rule-mode").value,
          // 公司规则不接受匹配范围；JD 规则必须带 scope
          scope: isCompany ? "" : get("rule-scope").value,
          description: get("rule-description").value.trim(),
          // 编辑时保留原启用状态，避免误把停用规则重新打开
          enabled: existing.rule_id ? existing.enabled !== false : true,
        };
        const url = existing.rule_id ? `/api/blacklist/rules/${existing.rule_id}` : "/api/blacklist/rules";
        const saved = existing.rule_id ? await api.put(url, payload) : await api.post(url, payload);
        dialog.close(); await loadRules(); toast(saved.item ? "规则已保存" : "规则已保存", "ok");
      } catch (err) { get("rule-result").textContent = err.message || "保存失败"; }
    });
  }
  $("collection-save").addEventListener("click", async () => { try { fill(await api.put("/api/collection/config", payload())); markDirty(false); toast("采集配置已保存", "ok"); } catch (err) { toast(err.message, "bad"); } });
  $("collection-reset").addEventListener("click", async () => { if (!window.confirm("确定重置搜索条件和采集参数？")) return; try { fill(await api.post("/api/collection/config/reset")); markDirty(false); toast("已重置", "ok"); } catch (err) { toast(err.message, "bad"); } });
  $("rule-add-company").addEventListener("click", () => openRuleModal({ rule_type: "company", match_mode: "exact", scope: "" }));
  $("rule-add-jd").addEventListener("click", () => openRuleModal({ rule_type: "jd_keyword", match_mode: "contains", scope: "both" }));
  $("rules-recheck").addEventListener("click", async () => {
    try {
      const task = await api.post("/api/blacklist/recheck", {});
      $("recheck-status").textContent = `规则重检已开始：${task.task_id}`;
      const timer = setInterval(async () => {
        try {
          const state = await api.get("/api/blacklist/recheck/status");
          const p = state.progress || {};
          $("recheck-status").textContent = state.task_id ? `规则重检：${state.status} · ${p.done || 0}/${p.total || 0}` : "";
          if (["done", "error", "cancelled"].includes(state.status)) { clearInterval(timer); await loadRules(); }
        } catch { clearInterval(timer); }
      }, 1000);
    } catch (err) { toast(err.message || "重检启动失败", "bad"); }
  });
  await load();
}
