/** 找工作：筛选 · 搜索 · 卡片流 · 删除 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, fmtTime, taskPanel, renderEvents, fmtEta } from "../ui.js";

export async function renderJobs(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">拉取 <span class="grad">职位</span></h1>
        <p class="hero-sub">在本地职位库浏览、筛选和补全职位描述。采集条件集中在系统配置中管理。</p>
      </div>
    </div>

    <div class="bento mb-24">
      <div class="card span-12">
        <div class="card-head">
          <h3 class="card-title">职位采集</h3>
          <span class="card-sub" id="crawl-phase">空闲</span>
        </div>
        <div class="flex between center gap-12 wrap mb-12">
          <div class="muted" id="collection-summary">正在读取当前采集条件…</div>
          <a class="btn" href="#/settings">前往采集配置</a>
        </div>
        <div class="btn-row">
          <button class="btn primary" id="btn-crawl">开始采集</button>
          <button class="btn danger hidden" id="btn-stop">停止采集</button>
        </div>
        <div id="crawl-task" class="mt-16"></div>
      </div>
    </div>

    <div class="flex column gap-8 mb-16">
      <div class="flex center gap-8">
        <input class="input flex-shrink-0" id="q" placeholder="在库中搜索岗位/公司" style="width:240px">
        <input class="input flex-shrink-0" id="city-f" placeholder="城市" style="width:120px">
        <select class="select" id="job-category" aria-label="职位状态筛选">
          <option value="">全部状态</option>
          <option value="ordinary">普通职位</option>
          <option value="company_blacklisted">公司黑名单</option>
          <option value="jd_blacklisted">JD 黑名单</option>
          <option value="both">公司与 JD 黑名单</option>
          <option value="jd_not_collected">JD 未采集</option>
          <option value="jd_failed">JD 采集失败</option>
        </select>
        <select class="select" id="jd-status-filter" aria-label="JD 状态筛选">
          <option value="">全部 JD 状态</option>
          <option value="pending">待采集</option>
          <option value="collecting">采集中</option>
          <option value="completed">已完成</option>
          <option value="empty">JD 为空</option>
          <option value="failed">采集失败</option>
          <option value="unchecked">规则未检查</option>
        </select>
      </div>
      <div class="btn-row ml-12">
        <button class="btn" id="btn-filter">筛选</button>
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
  let collectionConfig = { collection: {}, search: {} };
  let category = "";
  let jdStatus = "";
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

  // ---------- 当前采集配置 ----------
  function collectionSummary(data) {
    const search = data?.search || {};
    const collection = data?.collection || {};
    // 采集来源二选一：expect_kind 非空 = 期望流，空 = 搜索流
    const kind = (collection.expect_kind || "").trim();
    const expectMode = !!kind;
    const partTime = kind === "part_time";
    const parts = [];
    // 期望流只带筛选维度：from_search 不搬 query/stage/jobType，兼职态 salary
    // 又被 payType/partTime 顶替——摘要只列这次真会进请求串的维度，别把
    // 「配了但不生效」的也打上去，那会看成「期望 + 关键词」。
    if (search.query && !expectMode) parts.push(`关键词「${search.query}」`);
    if (search.city) parts.push(`城市 ${search.city}`);
    if (search.jobType && !expectMode) parts.push(`职位类型 ${search.jobType}`);
    if (search.salary && !partTime) parts.push(`薪资 ${search.salary}`);
    if (Array.isArray(search.experience) && search.experience.length) {
      parts.push(`经验 ${search.experience.join("、")}`);
    }
    if (Array.isArray(search.degree) && search.degree.length) {
      parts.push(`学历 ${search.degree.join("、")}`);
    }
    if (Array.isArray(search.industry) && search.industry.length) {
      parts.push(`行业 ${search.industry.join("、")}`);
    }
    if (Array.isArray(search.scale) && search.scale.length) {
      parts.push(`规模 ${search.scale.join("、")}`);
    }
    if (Array.isArray(search.payType) && search.payType.length && partTime) {
      parts.push(`薪资方式 ${search.payType.join("、")}`);
    }
    if (Array.isArray(search.partTime) && search.partTime.length && partTime) {
      parts.push(`兼职 ${search.partTime.join("、")}`);
    }
    if (Array.isArray(search.stage) && search.stage.length && !expectMode) {
      parts.push(`阶段 ${search.stage.join("、")}`);
    }
    if (kind === "part_time") {
      parts.push("兼职期望流");
    } else if (kind === "expect") {
      parts.push(`期望流「${collection.expect_name || collection.encrypt_expect_id || "未命名"}」`);
    } else if (kind === "recommend") {
      parts.push("推荐期望流");
    } else if (kind === "temp_expect") {
      parts.push(`临时期望流「${collection.expect_name || "未命名"}」`);
    } else {
      // 搜索流。选了期望 = 站点「期望 tab + 输入关键词」，
      // encryptExpectId 会进请求串——摘要得点出来，别让人以为是纯关键词。
      const ctx = collection.encrypt_expect_id || search.encryptExpectId || "";
      if (ctx) {
        parts.push(`使用搜索条件 · 期望上下文「${collection.expect_name || ctx}」`);
      } else {
        parts.push("使用搜索条件");
      }
    }
    parts.push(`最多 ${collection.max_pages ?? 5} 页`);
    parts.push(collection.fetch_details === false ? "不抓 JD" : "抓取 JD");
    parts.push(collection.store_blacklisted === false ? "不保存黑名单职位" : "保存并标记黑名单");
    return parts.join(" · ") || "不限（未设置筛选维度）";
  }

  async function loadCollectionConfig() {
    try {
      collectionConfig = await api.get("/api/collection/config");
      $("collection-summary").textContent = collectionSummary(collectionConfig);
    } catch (err) {
      $("collection-summary").textContent = "采集条件读取失败";
      toast("读采集配置失败：" + err.message, "warn");
    }
    return collectionConfig;
  }

  // ---------- 搜索 ----------
  $("btn-crawl").addEventListener("click", async () => {
    try {
      const task = await api.post("/api/crawl/start", {
        // 采集条件已集中保存到系统配置；空 body 让后端使用同一份配置快照。
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
    const qs = new URLSearchParams({
      limit,
      offset,
      ...(keyword ? { keyword } : {}),
      ...(city ? { city } : {}),
      ...(category ? { category } : {}),
      ...(jdStatus ? { jd_status: jdStatus } : {}),
    });
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
        const statusLabels = [
          j.company_blacklisted ? "公司黑名单" : "",
          j.jd_blacklisted ? "JD 关键词黑名单" : "",
          // 规则没查完就打「未检查」：covers empty/pending/failed/unchecked + completed 但没查规则
          j.jd_rule_checked ? "" : "JD 规则未检查",
          j.jd_status && j.jd_status !== "completed" && j.jd_status !== "unchecked" ? ({
            pending: "JD 待采集",
            collecting: "JD 采集中",
            empty: "JD 为空",
            failed: "JD 采集失败",
          }[j.jd_status] || `JD ${j.jd_status}`) : "",
        ].filter(Boolean);
        const statusHtml = statusLabels.map((label) => `<span class="pill ${label.includes("黑名单") || label.includes("失败") ? "warn" : "static"}">${escapeHtml(label)}</span>`).join("");
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
          <div class="job-meta">${statusHtml}</div>
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
      const matchedRules = (j.matched_rule_snapshot || []).map((rule) => `
        <div class="settings-rule">
          <strong>${escapeHtml(rule.rule_type === "company" ? "公司规则" : "JD 关键词")}</strong>
          <span class="pill static">${escapeHtml(rule.match_mode || "")}</span>
          <div>${escapeHtml(rule.pattern || "")}</div>
          <div class="muted">命中内容：${escapeHtml(rule.matched_text || "—")}${rule.source ? ` · 范围 ${escapeHtml(rule.source)}` : ""}</div>
        </div>`).join("");
      const jdStatusLabel = ({
        pending: "待采集", collecting: "采集中", completed: "已完成", empty: "JD 为空",
        failed: "采集失败", unchecked: "规则未检查",
      }[j.jd_status] || j.jd_status || "未知");
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
            ${j.company_blacklisted ? '<span class="pill warn">公司黑名单</span>' : ""}
            ${j.jd_blacklisted ? '<span class="pill warn">JD 关键词黑名单</span>' : ""}
            <span class="pill ${j.jd_status === "failed" ? "warn" : "static"}">JD：${escapeHtml(jdStatusLabel)}</span>
            ${j.jd_rule_checked ? "" : '<span class="pill warn">JD 规则未检查</span>'}
          </div>
          ${matchedRules ? `<details class="acc mb-16"><summary>查看命中规则（${(j.matched_rule_snapshot || []).length} 条）</summary><div class="acc-body">${matchedRules}</div></details>` : ""}
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
  $("job-category").addEventListener("change", (e) => {
    category = e.target.value;
    offset = 0;
    loadJobs();
  });
  $("jd-status-filter").addEventListener("change", (e) => {
    jdStatus = e.target.value;
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

  await loadCollectionConfig();
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
  };
}
