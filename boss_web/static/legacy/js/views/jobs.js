/** 找工作：筛选 · 搜索 · 卡片流 · 删除 */

import { api } from "../api.js";
import { toast, escapeHtml } from "../ui.js";
import { watchTask } from "../task_watch.js";
import { openJobDetail } from "../job_detail.js";
import { openInterviewPrep } from "../interview_prep.js";

/**
 * 渲染找工作页：采集入口、筛选、职位卡片流、补全描述。
 *
 * 采集进度不在这画，统一在任务中心看（本页只跟到结束好收结果）。
 *
 * @param {HTMLElement} root 挂载容器
 * @returns {Promise<() => void>} 卸载时停掉搜索 / 补全两个任务观察器
 */
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
  let totalJobs = 0;
  const limit = 24;
  /** @type {?() => void} 搜索任务观察器的停止句柄（watchTask 返回），null = 没在观察。 */
  let crawlStop = null;
  let crawlTaskId = null;
  let collectionConfig = { collection: {}, search: {} };
  let category = "";
  let jdStatus = "";
  let interviewSummary = {};

  /**
   * 把当前采集配置拼成一行人话摘要。
   *
   * 期望流与搜索流生效维度不同，只列这次真会进请求串的项，别把「配了但不生效」的也打上去。
   *
   * @param {{collection?: Object<string, any>, search?: Object<string, any>}} data 采集配置
   * @returns {string} 用「 · 」连接的摘要；没设维度时给兜底文案
   */
  function collectionSummary(data) {
    const search = data?.search || {};
    const collection = data?.collection || {};
    const kind = (collection.expect_kind || "").trim();
    const expectMode = !!kind;
    const partTime = kind === "part_time";
    const parts = [];
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

  /** 读系统里的采集配置并刷新摘要；失败给出提示。 */
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

  $("btn-crawl").addEventListener("click", async () => {
    try {
      const task = await api.post("/api/crawl/start", {
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

  /**
   * 观察搜索任务直到结束，结束后重载列表。
   *
   * 进度条 / 日志 / 停止都在任务中心看（入口在顶栏），本页只跟到
   * 结束好收结果。原来是 1s setInterval 轮询，现在交给 watchTask：WS 有
   * `crawl` 任务事件才拉 status（400ms 节流），断线由它内部降级轮询。
   */
  function startPoll() {
    const taskId = crawlTaskId;
    if (!taskId) return;
    if (crawlStop) crawlStop();
    crawlStop = watchTask(taskId, () => api.get(`/api/crawl/${taskId}`), {
      taskType: "crawl",
      isDone: (s) => ["done", "error", "cancelled"].includes(s.status),
      onStatus: (s) => {
        $("crawl-phase").textContent = s.status === "running" ? (s.current || "进行中") : "空闲";
        if (["done", "error", "cancelled"].includes(s.status)) {
          if (crawlStop) { crawlStop(); crawlStop = null; }
          $("btn-crawl").classList.remove("hidden");
          $("btn-stop").classList.add("hidden");
          loadJobs();
          toast(s.status === "error" ? "搜索出错：" + (s.error || "") : "搜索结束", s.status === "error" ? "bad" : "ok");
        }
      },
    });
  }

  /** 按当前筛选条件拉一页职位并渲染。 */
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
      totalJobs = Number(data.total) || 0;
      $("total-label").textContent = `共 ${data.total} 条`;
      $("page-label").textContent = `${offset + 1} - ${Math.min(offset + limit, totalJobs)} / ${totalJobs}`;
      const prevBtn = $("btn-prev");
      const nextBtn = $("btn-next");
      if (prevBtn) prevBtn.disabled = offset <= 0;
      if (nextBtn) nextBtn.disabled = offset + limit >= totalJobs;
      const items = data.items || [];
      interviewSummary = await loadInterviewSummary(items);
      renderGrid(items);
    } catch (err) {
      toast("读职位失败：" + err.message, "bad");
    }
  }

  /**
   * 拉本页岗位的面试题状态（卡片徽章 / 按钮文案用）。
   *
   * @param {Array<Object<string, any>>} items 当页职位
   * @returns {Promise<Object<string, any>>} 职位 ID → {question_count}
   */
  async function loadInterviewSummary(items) {
    const ids = items.map((j) => j.encrypt_job_id).filter(Boolean);
    if (!ids.length) return {};
    try {
      const data = await api.get(
        "/api/interview/summary?encrypt_job_ids=" + encodeURIComponent(ids.join(","))
      );
      return data.items || {};
    } catch {
      return {};
    }
  }

  /**
   * 画职位卡片流（含黑名单 / JD 状态 pill 与勾选）。
   *
   * @param {Array<Object<string, any>>} items 当页职位
   */
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
          j.jd_rule_checked ? "" : "JD 规则未检查",
          j.jd_status && j.jd_status !== "completed" && j.jd_status !== "unchecked" ? ({
            pending: "JD 待采集",
            collecting: "JD 采集中",
            empty: "JD 为空",
            failed: "JD 采集失败",
          }[j.jd_status] || `JD ${j.jd_status}`) : "",
        ].filter(Boolean);
        const statusHtml = statusLabels.map((label) => `<span class="pill ${label.includes("黑名单") || label.includes("失败") ? "warn" : "static"}">${escapeHtml(label)}</span>`).join("");
        const iq = interviewSummary[j.encrypt_job_id] || { question_count: 0 };
        const interviewBadge = iq.question_count
          ? `<span class="pill accent">面试题 ${iq.question_count}</span>`
          : "";
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
          <div class="job-meta">${statusHtml}${interviewBadge}</div>
          <div class="muted" style="font-size:11px">${escapeHtml(j.brand_industry || "")} ${escapeHtml(j.brand_scale_name || "")}</div>
          <button class="btn sm ghost btn-interview" data-id="${escapeHtml(j.encrypt_job_id)}">${
            iq.question_count ? "查看面试题" : "面试准备"
          }</button>
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
        if (e.target.closest(".btn-interview")) {
          openInterviewFor(id);
          return;
        }
        openDetail(id);
      });
    });
  }

  /**
   * 面试题入口：已有题就开详情弹窗；还没题就去「面试题生成」页出题。
   *
   * @param {string} id 加密职位 ID
   */
  async function openInterviewFor(id) {
    try {
      const ctx = await api.get("/api/interview/context?encrypt_job_id=" + encodeURIComponent(id));
      if (ctx.question_count) {
        openInterviewPrep({
          analysisId: ctx.analysis_id,
          encryptJobId: id,
          jobName: ctx.job_name,
          brandName: ctx.brand_name,
          matchScore: ctx.match_score,
          onChanged: () => loadJobs(),
        });
        return;
      }
      location.hash = "#/interview";
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  /**
   * 打开职位详情弹窗（技能 / 福利 / 命中规则 / JD），并挂上删除。
   *
   * @param {string} id 加密职位 ID
   */
  async function openDetail(id) {
    openJobDetail(id, { onDeleted: () => loadJobs() });
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

  /** @type {?string} 当前补抓任务 id——只用来对上 WS 的 `desc` 事件，status 仍读全局端点。 */
  let descTaskId = null;
  /** @type {?() => void} 补抓任务观察器的停止句柄（watchTask 返回），null = 没在观察。 */
  let descStop = null;

  /** 停掉补全描述的观察并恢复按钮。 */
  function stopDescPoll() {
    if (descStop) { descStop(); descStop = null; }
    $("btn-fetch-desc").disabled = false;
    $("btn-fetch-desc").textContent = "补全描述";
  }

  /**
   * 观察补全描述任务；结束后 toast 汇总并重载列表。
   *
   * 进度不在这画——统一在任务中心看；这里只留「结束 → 提示 + 重载 + 按钮复位」。
   */
  function startDescPoll() {
    if (descStop) { descStop(); descStop = null; }
    if (!descTaskId) return;
    descStop = watchTask(descTaskId, () => api.get("/api/jobs/fetch-descriptions/status"), {
      taskType: "desc",
      isDone: (s) => ["done", "error", "cancelled"].includes(s.status),
      onStatus: (s) => {
        const running = s.status === "running" || s.status === "pending";
        $("btn-fetch-desc").disabled = running;
        $("btn-fetch-desc").textContent = running ? "补全中…" : "补全描述";
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
      },
    });
  }

  $("btn-fetch-desc").addEventListener("click", async () => {
    if (descStop) return toast("补全任务已在跑", "warn");
    try {
      const task = await api.post("/api/jobs/fetch-descriptions", { limit: 0 });
      toast(`补全已开始（${task.progress?.total ?? "?"} 条）`, "ok");
      descTaskId = task.task_id || null;
      $("btn-fetch-desc").disabled = true;
      $("btn-fetch-desc").textContent = "补全中…";
      startDescPoll();
    } catch (err) {
      stopDescPoll();
      toast(err.message, "bad");
    }
  });

  $("btn-prev").addEventListener("click", () => {
    if (offset > 0) {
      offset = Math.max(0, offset - limit);
      loadJobs();
    }
  });
  $("btn-next").addEventListener("click", () => {
    if (offset + limit < totalJobs) {
      offset += limit;
      loadJobs();
    }
  });

  await loadCollectionConfig();
  await loadJobs();

  try {
    const s = await api.get("/api/crawl/status");
    if (s.task_id && s.status === "running") {
      crawlTaskId = s.task_id;
      $("btn-crawl").classList.add("hidden");
      $("btn-stop").classList.remove("hidden");
      startPoll();
    }
  } catch { }

  try {
    const s = await api.get("/api/jobs/fetch-descriptions/status");
    if (s.task_id && s.status === "running") {
      descTaskId = s.task_id;
      $("btn-fetch-desc").disabled = true;
      $("btn-fetch-desc").textContent = "补全中…";
      startDescPoll();
    }
  } catch { }

  return () => {
    if (crawlStop) { crawlStop(); crawlStop = null; }
    if (descStop) { descStop(); descStop = null; }
  };
}
