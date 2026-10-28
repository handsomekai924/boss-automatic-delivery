/** 职位匹配：目录壳 + 页面级状态 + 四个子目录（工作台 / 批量发送 / 抓取任务 / 分析历史） */

import { api } from "../api.js";
import { toast, modal, escapeHtml, scoreRing } from "../ui.js";
import * as ws from "../ws.js";
import { watchTask } from "../task_watch.js";
import { openInterviewPrep } from "../interview_prep.js";
import { renderWorkbench } from "./match_workbench.js";
import { renderDeliver } from "./match_deliver.js";
import { renderCrawls } from "./match_crawls.js";
import { renderHistory } from "./match_history.js";

const PAGE_SIZE = 20;
const MIN_SCORE_DEFAULT = 70;
const GREET_COOLDOWN_DAYS = 30;

/**
 * 渲染职位匹配目录壳：页面级状态、四个子目录切换。
 * 匹配 / 投递进度都不在本页画，统一到任务中心看。
 *
 * @param {HTMLElement} root 挂载容器
 * @returns {() => void} 卸载时停掉匹配 / 投递的任务观察器
 */
export async function renderMatch(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">职位 <span class="grad">匹配</span></h1>
        <p class="hero-sub">拿你的简历和搜到的岗位逐个比对，给出匹配度、好在哪、差在哪，还有一句给 HR 的开场白。开场白可以自己改，确认后才发给对方。</p>
      </div>
    </div>
    <div class="settings-shell">
      <aside class="card settings-nav" aria-label="匹配目录">
        <button class="settings-tab active" data-tab="workbench" type="button">匹配工作台</button>
        <button class="settings-tab" data-tab="deliver" type="button">批量发送</button>
        <button class="settings-tab" data-tab="crawls" type="button">抓取任务</button>
        <button class="settings-tab" data-tab="history" type="button">分析历史</button>
      </aside>
      <section class="card settings-pane" id="match-pane"></section>
    </div>
  `;

  const state = {
    resumes: [],
    currentResumeId: "",
    jobs: [],
    totalJobs: 0,
    page: 0,
    selected: new Set(),
    matches: [],
    analysisId: null,
    resumeTitle: "",
    scope: { type: "all" },
    matchTaskId: null,
    deliverTaskId: null,
    greetDrafts: new Map(),
    detailApi: null,
    /** @type {Map<string, () => void>} 在跑匹配任务的观察器 stop 句柄（多任务并发，key = task_id） */
    matchWatches: new Map(),
    /** @type {?() => void} 投递任务观察器 stop 句柄 */
    deliverWatch: null,
  };

  const helpers = {
    /** 切到指定目录。 */
    switchTab(name) {
      const tab = root.querySelector(`.settings-tab[data-tab="${name}"]`);
      if (tab) show(name);
    },

    /** 按当前 state 渲染岗位列表与分页控件。 */
    renderJobs() {
      const pane = root.querySelector("#match-pane");
      const jobList = pane?.querySelector("#job-list");
      const jobsSub = pane?.querySelector("#jobs-sub");
      const selCount = pane?.querySelector("#sel-count");
      const pageInfo = pane?.querySelector("#page-info");
      if (!jobList) return;

      const mIds = new Set(state.matches.map((m) => m.encrypt_job_id));
      const sc = state.scope;
      if (jobsSub) {
        jobsSub.textContent = sc.type !== "all"
          ? `范围：${sc.type === "crawl_task" ? "抓取任务" : "已选"} · 共 ${state.totalJobs} 条`
          : `共 ${state.totalJobs} 条 · 第 ${state.page + 1}/${Math.max(1, Math.ceil(state.totalJobs / PAGE_SIZE))} 页`;
      }
      jobList.innerHTML = state.jobs.map((j) => {
        const id = j.encrypt_job_id;
        const on = state.selected.has(id);
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
      }).join("") || `<div class="muted">库里还没有职位，先去「职位」页抓取</div>`;

      jobList.querySelectorAll("input[type=checkbox]").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) state.selected.add(cb.dataset.id);
          else state.selected.delete(cb.dataset.id);
          helpers.updateSelCount();
        });
      });
      if (pageInfo) pageInfo.textContent = `${state.page + 1} / ${Math.max(1, Math.ceil(state.totalJobs / PAGE_SIZE))}`;
      const prevBtn = pane?.querySelector("#btn-prev");
      const nextBtn = pane?.querySelector("#btn-next");
      if (prevBtn) prevBtn.disabled = state.page <= 0;
      if (nextBtn) nextBtn.disabled = (state.page + 1) * PAGE_SIZE >= state.totalJobs;
      helpers.updateSelCount();
    },

    /** 同步各处「已选 N」计数。 */
    updateSelCount() {
      const pane = root.querySelector("#match-pane");
      const selCount = pane?.querySelector("#sel-count");
      const sendSelN = pane?.querySelector("#send-sel-n");
      if (selCount) selCount.textContent = `已选 ${state.selected.size}`;
      if (sendSelN) sendSelN.textContent = String(state.selected.size);
    },

    /** 按当前范围拉取一页岗位并刷新列表。 */
    async loadJobs() {
      try {
        const sc = state.scope;
        let url = `/api/jobs?limit=${PAGE_SIZE}&offset=${state.page * PAGE_SIZE}`;
        if (sc.type === "crawl_task" && sc.crawl_task_id) {
          const r = await api.get(`/api/crawl/tasks/${encodeURIComponent(sc.crawl_task_id)}/jobs?limit=200`);
          const ids = (r.items || []).filter((j) => !j.missing).map((j) => j.encrypt_job_id);
          state.jobs = ids.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE).map((id) => ({ encrypt_job_id: id, job_name: id, brand_name: "" }));
          state.totalJobs = ids.length;
          if (ids.length) {
            try {
              const full = await api.get(`/api/jobs?limit=${Math.min(ids.length, 200)}&offset=0`);
              const map = new Map((full.items || []).map((j) => [j.encrypt_job_id, j]));
              state.jobs = state.jobs.map((j) => map.get(j.encrypt_job_id) || j);
            } catch {}
          }
        } else if (sc.type === "jobs" && sc.job_ids?.length) {
          const ids = sc.job_ids;
          state.jobs = ids.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE).map((id) => ({ encrypt_job_id: id, job_name: id, brand_name: "" }));
          state.totalJobs = ids.length;
        } else {
          const r = await api.get(url);
          state.jobs = r.items || [];
          state.totalJobs = r.total || 0;
        }
        helpers.renderJobs();
      } catch (err) {
        toast(err.message || "加载职位失败", "bad");
      }
    },

    /** 按分数从高到低渲染匹配结果卡片，绑定点击进详情。 */
    renderMatches() {
      const pane = root.querySelector("#match-pane");
      const box = pane?.querySelector("#an-results");
      const anSub = pane?.querySelector("#an-sub");
      const clearBtn = pane?.querySelector("#btn-clear-analysis");
      if (!box) return;
      if (clearBtn) {
        clearBtn.classList.toggle("hidden", !(state.analysisId || state.matches.length));
      }
      helpers.updateSendCounts();

      if (!state.matches.length) {
        box.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>还没有匹配结果</p></div>`;
        return;
      }

      const ranked = [...state.matches].sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
      box.innerHTML = ranked.map((m) => {
        const jid = m.encrypt_job_id;
        const score = m.match_score || 0;
        const adviceLine = (m.advice || "").trim() || (m.verdict || "").trim();
        const tags = [
          m.company_blacklisted ? "公司黑名单" : "",
          m.jd_blacklisted ? "JD 关键词黑名单" : "",
          !m.jd_rule_checked ? "JD 规则未检查" : "",
          ["failed", "empty", "pending", "collecting", "unchecked"].includes(m.jd_status) ? `JD ${m.jd_status}` : "",
        ].filter(Boolean);
        const cool = helpers.greetState(m);
        const sendPill = cool.in_cooldown
          ? `<span class="pill static warn">冷却中 · ${escapeHtml(cool.cooldown_date)} 可再发</span>`
          : helpers.sendRestriction(m)
            ? `<span class="pill static bad">不可发送</span>`
            : m.deliver_status === "failed"
              ? `<span class="pill static bad">发送失败</span>`
              : m.delivered_at
                ? `<span class="pill static accent">已发送</span>`
                : m.deliver_status === "sending"
                  ? `<span class="pill static">发送中…</span>`
                  : !(m.greeting || "").trim()
                    ? `<span class="pill static warn">无招呼语</span>`
                    : `<span class="pill static ok">可发送</span>`;
        const safety = `<div class="pills mt-8">${
          tags.length
            ? tags.map((tag) => `<span class="pill static bad">${escapeHtml(tag)}</span>`).join(" ")
            : `<span class="pill static accent">安全状态已检查</span>`
        } ${sendPill}</div>`;
        return `
          <div class="card lift mb-16 match-card" style="padding:16px" data-jid="${escapeHtml(jid)}"
               role="button" tabindex="0" title="点击查看全部信息">
            <div class="match-row">
              ${scoreRing(score)}
              <div class="match-main">
                <h4 class="match-title">${escapeHtml(m.job_name)} <span class="muted" style="font-weight:400;font-size:13px">@ ${escapeHtml(m.brand_name)}</span></h4>
                <div class="muted" style="font-size:12px">${escapeHtml(m.location || "")} · ${escapeHtml(m.salary_desc || "")}</div>
                ${safety}
                ${adviceLine ? `<div class="muted" style="font-size:12.5px;margin-top:6px"><span class="muted" style="font-size:11px">建议</span> ${escapeHtml(adviceLine)}</div>` : ""}
                <div class="flex between center gap-8 mt-8">
                  <span class="muted" style="font-size:11.5px">${
                    !cool.in_cooldown && cool.last_date && m.delivered_at
                      ? `上次发送 ${escapeHtml(cool.last_date)}`
                      : ""
                  }</span>
                  <span class="muted" style="font-size:11px;flex-shrink:0">点击查看详情 ›</span>
                </div>
              </div>
            </div>
          </div>`;
      }).join("");

      box.querySelectorAll(".match-card").forEach((card) => {
        const jid = card.dataset.jid;
        card.addEventListener("click", () => helpers.openMatchDetail(jid));
        card.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            helpers.openMatchDetail(jid);
          }
        });
      });
    },

    /**
     * 算招呼冷却状态：取发送时间与上次招呼时间中较晚者，加冷却天数。
     *
     * @param {Object} m 匹配结果项（last_greeted_at / delivered_at）
     * @returns {{in_cooldown: boolean, cooldown_date: string, last_date: string, last_ts?: number}}
     */
    greetState(m) {
      const last = m.last_greeted_at || "";
      const delivered = m.delivered_at || 0;
      let lastTs = 0;
      if (delivered) lastTs = Math.max(lastTs, Number(delivered) || 0);
      if (last) {
        const t = new Date(last.replace(" ", "T")).getTime();
        if (!Number.isNaN(t)) lastTs = Math.max(lastTs, t / 1000);
      }
      if (!lastTs) return { in_cooldown: false, cooldown_date: "", last_date: "" };
      const now = Date.now() / 1000;
      const cooldownUntil = lastTs + GREET_COOLDOWN_DAYS * 86400;
      const inCooldown = cooldownUntil > now;
      const d = new Date(lastTs * 1000);
      const mmdd = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      const untilD = new Date(cooldownUntil * 1000);
      const untilMmdd = `${String(untilD.getMonth() + 1).padStart(2, "0")}-${String(untilD.getDate()).padStart(2, "0")}`;
      return {
        in_cooldown: inCooldown,
        cooldown_date: untilMmdd,
        last_date: mmdd,
        last_ts: lastTs,
      };
    },

    /** 该职位是否还在招呼冷却期。 */
    inCooldown(m) {
      return helpers.greetState(m).in_cooldown;
    },

    /** 投递状态短文案（失败原因 / 冷却 / 已发送 / 发送中）。 */
    deliverLabel(m) {
      if (m.deliver_status === "failed") return `失败：${m.deliver_error || ""}`;
      const cool = helpers.greetState(m);
      if (cool.in_cooldown) return `冷却中（${cool.cooldown_date} 后可再发）`;
      if (m.delivered_at) return "已发送";
      return m.deliver_status === "sending" ? "发送中…" : "";
    },

    /** 发送状态完整文案，含不可发送原因与「无招呼语 / 待发」。 */
    sendStatusLabel(m) {
      if (m.deliver_status === "failed") return `失败：${m.deliver_error || ""}`;
      const cool = helpers.greetState(m);
      if (cool.in_cooldown) return `冷却中（${cool.cooldown_date} 后可再发）`;
      if (m.delivered_at) return "已发送";
      if (m.deliver_status === "sending") return "发送中…";
      const restriction = helpers.sendRestriction(m);
      if (restriction) return `不可发送（${restriction}）`;
      if (!(m.greeting || "").trim()) return "无招呼语";
      return "待发";
    },

    /**
     * 按匹配时安全快照判定能否发送。
     *
     * @param {Object} m 匹配结果项
     * @returns {string} 不可发送的原因；空串表示可发送
     */
    sendRestriction(m) {
      if (!["is_blacklisted", "company_blacklisted", "jd_blacklisted", "jd_rule_checked", "jd_status", "auto_delivery_blocked"].some((key) => key in m)) {
        return "缺少匹配时安全快照";
      }
      if (m.company_blacklisted) return "公司黑名单";
      if (m.jd_blacklisted) return "JD 黑名单";
      if (!m.jd_rule_checked) return "JD 规则未检查";
      if (["failed", "pending", "collecting", "unchecked"].includes(m.jd_status || "")) return "JD 状态不完整";
      if (m.auto_delivery_blocked) return "职位状态不完整";
      return "";
    },

    /** 读输入框评分阈值并夹到 0–100；无效时用默认值。 */
    minScore(inputEl) {
      const v = parseInt(inputEl?.value, 10);
      if (Number.isNaN(v)) return MIN_SCORE_DEFAULT;
      return Math.min(100, Math.max(0, v));
    },

    /**
     * 挑出「一键发送全部」的目标：达阈值、未冷却、有招呼语、安全通过。
     *
     * @param {HTMLInputElement} [inputEl] 评分阈值输入框
     * @returns {Array<Object>} 可发送的匹配结果项
     */
    sendAllTargets(inputEl) {
      const min = helpers.minScore(inputEl);
      const sendRestriction = helpers.sendRestriction;
      return state.matches.filter(
        (m) => (m.match_score || 0) >= min && !helpers.inCooldown(m) && (m.greeting || "").trim() && !sendRestriction(m)
      );
    },

    /** 刷新「一键发送 / 发送选中」的计数与排除原因。 */
    updateSendCounts(inputEl) {
      const pane = root.querySelector("#match-pane");
      if (!inputEl) inputEl = pane?.querySelector("#send-min-score");
      const sendAllN = pane?.querySelector("#send-all-n");
      const sendExcluded = pane?.querySelector("#send-excluded");
      const sendSelN = pane?.querySelector("#send-sel-n");
      const min = helpers.minScore(inputEl);
      const eligible = helpers.sendAllTargets(inputEl);
      const ranked = state.matches.filter((m) => (m.match_score || 0) >= min && !helpers.inCooldown(m) && (m.greeting || "").trim());
      const restricted = ranked.filter((m) => helpers.sendRestriction(m)).length;
      const cooled = state.matches.filter((m) => (m.match_score || 0) >= min && helpers.inCooldown(m) && (m.greeting || "").trim()).length;
      const noGreeting = state.matches.filter((m) => (m.match_score || 0) >= min && !helpers.inCooldown(m) && !(m.greeting || "").trim()).length;
      if (sendAllN) sendAllN.textContent = String(eligible.length);
      const parts = [];
      if (restricted) parts.push(`${restricted} 条受限`);
      if (cooled) parts.push(`${cooled} 条冷却中`);
      if (noGreeting) parts.push(`${noGreeting} 条无招呼语`);
      if (sendExcluded) sendExcluded.textContent = parts.length ? `将排除 ${parts.join(" / ")}` : "";
      if (sendSelN) sendSelN.textContent = String(state.matches.filter((m) => state.selected.has(m.encrypt_job_id) && !helpers.inCooldown(m) && (m.greeting || "").trim() && !helpers.sendRestriction(m)).length);
    },

    /** 从投递配置读回评分阈值并写入输入框。 */
    async loadMinScore(inputEl) {
      try {
        const cfg = await api.get("/api/deliver/config");
        if (typeof cfg.min_score === "number" && inputEl) {
          inputEl.value = String(cfg.min_score);
          helpers.updateSendCounts(inputEl);
        }
      } catch {}
    },

    /** 校验并保存评分阈值到投递配置。 */
    async saveMinScore(inputEl) {
      const v = helpers.minScore(inputEl);
      if (inputEl) inputEl.value = String(v);
      helpers.updateSendCounts(inputEl);
      try {
        await api.put("/api/deliver/config", { min_score: v });
      } catch (err) {
        toast(err.message || "阈值保存失败", "bad");
      }
    },

    /**
     * 启动一个批量匹配任务并开始观察（后端已允许并发，可同时跑多个）。
     *
     * @param {Array<string>} [jobIds] 要匹配的职位 ID；抓取范围模式下忽略
     */
    async startMatch(jobIds) {
      if (!state.currentResumeId) return toast("先选择一份简历", "warn");
      const sc = state.scope;
      const body = {
        resume_id: state.currentResumeId,
        job_ids: jobIds || [],
      };
      if (sc.type === "crawl_task" && sc.crawl_task_id) {
        body.crawl_task_id = sc.crawl_task_id;
        body.job_ids = [];
      }
      try {
        const task = await api.post("/api/match/analyze", body);
        state.matchTaskId = task.task_id;
        state.analysisId = task.analysis_id;
        state.matches = [];
        const anSub = root.querySelector("#match-pane")?.querySelector("#an-sub");
        if (anSub) anSub.textContent = "匹配中…";
        toast(`匹配已开始（共 ${task.total || "?"} 个职位，AI 会逐个比对）`, "ok");
        helpers.watchMatch(task.task_id);
      } catch (err) {
        toast(err.message, "bad");
      }
    },

    /**
     * 把某次抓取任务设为匹配范围，切到工作台并重拉岗位。
     *
     * @param {string} taskId   抓取任务 ID
     * @param {number} jobCount 本批职位数
     */
    async setMatchScope(taskId, jobCount) {
      state.scope = { type: "crawl_task", crawl_task_id: taskId, job_count: jobCount };
      state.page = 0;
      helpers.switchTab("workbench");
      await helpers.loadJobs();
      helpers._renderScopeBar?.();
      toast("已设为匹配范围，可点击「一键匹配当前范围」开始匹配", "ok");
    },

    /**
     * 用统一 WS 事件观察一个匹配任务（替代原 1.2s setInterval 轮询）。
     * 进度不在本页画，统一到任务中心看；这里只跟到结束好收结果。
     * 结果区只聚焦最近一个任务（state.matchTaskId），互不抢写。
     *
     * @param {string} taskId 任务 id（提交响应里的 task_id）
     */
    watchMatch(taskId) {
      if (!taskId || state.matchWatches.has(taskId)) return;
      const stop = watchTask(taskId, () => api.get(`/api/match/analyze/${taskId}`), {
        taskType: "match",
        onStatus: (s) => helpers.onMatchStatus(taskId, s),
        isDone: (s) => ["done", "error", "cancelled"].includes(s.status),
      });
      state.matchWatches.set(taskId, () => {
        stop();
        state.matchWatches.delete(taskId);
      });
    },

    /**
     * 一次匹配状态回来后：聚焦任务写结果区；终态收观察器并提示。
     *
     * @param {string} taskId 任务 id
     * @param {Object}   s     业务快照（与原轮询拿到的同形状）
     */
    onMatchStatus(taskId, s) {
      const focused = taskId === state.matchTaskId;
      if (focused) {
        if (s.matches && s.matches.length) {
          state.matches = s.matches;
          helpers.renderMatches();
        }
      }
      if (!["done", "error", "cancelled"].includes(s.status)) return;

      const stopOne = state.matchWatches.get(taskId);
      if (stopOne) stopOne();

      if (!focused) {
        if (s.status === "done") toast(`后台匹配任务完成 · ${(s.matches || []).length} 条`, "ok");
        else if (s.status === "error") toast(s.error || "后台匹配任务失败", "bad");
        return;
      }
      state.matchTaskId = null;
      const anSub = root.querySelector("#match-pane")?.querySelector("#an-sub");
      if (s.status === "done") {
        if (anSub) anSub.textContent = `完成 · ${state.matches.length} 条`;
        toast("匹配完成", "ok");
      } else if (s.status === "error") {
        if (anSub) anSub.textContent = "失败";
        toast(s.error || "匹配失败", "bad");
      } else {
        if (anSub) anSub.textContent = `已停止 · ${state.matches.length} 条`;
      }
      helpers.renderMatches();
      helpers.renderJobs();
    },

    /**
     * 页面加载时恢复最近一次匹配的结果区（明细带冷却状态，走原单快照）；
     * 任务还在跑就接上观察。进度去任务中心看。
     */
    async restoreTask() {
      let s = null;
      try {
        s = await api.get("/api/match/analyze/status");
      } catch {}
      if (!s || !s.task_id) return;

      state.matchTaskId = s.task_id;
      state.analysisId = s.analysis_id || s.task_id;
      state.matches = s.matches || [];
      if (s.resume_id) {
        state.currentResumeId = s.resume_id;
        const it = state.resumes.find((x) => x.resume_id === s.resume_id);
        if (it) state.resumeTitle = it.title || s.resume_id;
      }

      if (s.status === "running") {
        helpers.renderMatches();
        helpers.renderJobs();
        helpers.watchMatch(s.task_id);
        toast("已接上进行中的匹配任务", "ok");
        return;
      }
      if (state.matches.length) {
        helpers.renderMatches();
        helpers.renderJobs();
      }
    },

    /**
     * 启动批量投递任务并开始观察。
     *
     * @param {Array<Object>} targets 要发送的匹配结果项
     * @param {{mode?: string, confirmed?: boolean, confirmationToken?: string}} [opts]
     *   mode 为 all / selected / manual；manual 需带 confirmationToken
     */
    async startDeliver(targets, { mode = "selected", confirmed = false, confirmationToken = "" } = {}) {
      if (!state.analysisId) return toast("先跑一次匹配再发送", "warn");
      if (state.deliverTaskId) return toast("已有发送任务在跑，等它结束", "warn");
      try {
        const s = await api.post("/api/deliver/start", {
          analysis_id: state.analysisId,
          encrypt_job_ids: targets.map((m) => m.encrypt_job_id),
          mode,
          confirmed,
          confirmation_token: confirmationToken,
        });
        state.deliverTaskId = s.task_id;
        toast(`开始发送 · 待发 ${s.total || 0} 条，跳过 ${s.skipped || 0} 条`, "ok");
        helpers.applyDeliverSnapshot(s);
        helpers.watchDeliver(s.task_id);
      } catch (err) {
        toast(err.message || "发送启动失败", "bad");
      }
    },

    /** 把投递快照写回 matches，并刷新列表与详情弹窗状态。 */
    applyDeliverSnapshot(s) {
      let touched = false;
      (s.items || []).forEach((it) => {
        const m = state.matches.find((x) => x.encrypt_job_id === it.encrypt_job_id);
        if (!m) return;
        m.deliver_status = it.status;
        if (it.delivered_at) m.delivered_at = it.delivered_at;
        if (it.error) m.deliver_error = it.error;
        else if (it.status !== "failed") delete m.deliver_error;
        touched = true;
      });
      if (touched) helpers.renderMatches();
      if (state.detailApi) {
        const cur = state.matches.find((x) => x.encrypt_job_id === state.detailApi.jid);
        if (cur) state.detailApi.setStatus(helpers.deliverLabel(cur));
      }
    },

    /**
     * 用统一 WS 事件观察投递任务（替代原 1s setInterval 轮询）。
     * 投递全局串行，同一时刻只有一个任务。
     *
     * @param {string} taskId 任务 id
     */
    watchDeliver(taskId) {
      if (state.deliverWatch) state.deliverWatch();
      const stop = watchTask(taskId, () => api.get("/api/deliver/status"), {
        taskType: "deliver",
        onStatus: (s) => helpers.onDeliverStatus(s),
        isDone: (s) => !s || !s.task_id || ["done", "error", "cancelled"].includes(s.status),
      });
      state.deliverWatch = () => {
        stop();
        state.deliverWatch = null;
      };
    },

    /**
     * 投递状态回来后：写回发送快照，结束后弹出成功 / 失败 / 取消提示。
     * 进度条不在这画——统一在任务中心看。
     *
     * @param {?Object} s 投递快照（与原轮询拿到的同形状）
     */
    onDeliverStatus(s) {
      if (!s || !s.task_id) {
        state.deliverTaskId = null;
        return;
      }
      state.deliverTaskId = s.task_id;
      helpers.applyDeliverSnapshot(s);
      if (["done", "error", "cancelled"].includes(s.status)) {
        state.deliverTaskId = null;
        if (s.status === "done") {
          toast(`发送完成 · 成功 ${s.ok || 0} / 失败 ${s.failed || 0} / 跳过 ${s.skipped || 0}`, "ok");
        } else if (s.status === "error") {
          toast(s.error || "发送中断", "bad");
        } else {
          toast("发送已取消", "warn");
        }
      }
    },

    /** 页面加载时接上服务端仍在跑的投递任务。 */
    async restoreDeliver() {
      let s = null;
      try {
        s = await api.get("/api/deliver/status");
      } catch {
        return;
      }
      if (!s || !s.task_id) return;
      helpers.applyDeliverSnapshot(s);
      if (s.status === "running") {
        state.deliverTaskId = s.task_id;
        helpers.watchDeliver(s.task_id);
        toast("已接上进行中的发送任务", "ok");
      }
    },

    /**
     * 弹窗确认后发送目标职位。
     *
     * @param {Array<Object>} targets   要发送的匹配结果项
     * @param {string}         [scopeNote] 弹窗里的补充说明
     * @param {{mode?: string}} [opts]  mode 为 all / selected / manual
     */
    confirmAndSend(targets, scopeNote = "", { mode = "selected" } = {}) {
      const restricted = targets.filter((m) => helpers.sendRestriction(m));
      const restrictionNote = restricted.length
        ? `<div class="banner warn mb-12">其中 ${restricted.length} 条受限职位：${escapeHtml([...new Set(restricted.map(helpers.sendRestriction))].join("、"))}。${mode === "all" ? "一键发送会自动排除它们。" : "确认后才会允许发送这些明确选中的职位。"}</div>`
        : "";
      const rows = targets.map((m) => `
        <div style="padding:10px 0;border-bottom:1px solid var(--stroke)">
          <div><strong>${escapeHtml(m.job_name)}</strong> <span class="muted">@ ${escapeHtml(m.brand_name)}</span>
            <span class="pill accent" style="margin-left:6px">评分 ${m.match_score || 0}</span>
            ${helpers.sendRestriction(m) ? `<span class="pill static bad" style="margin-left:6px">${escapeHtml(helpers.sendRestriction(m))}</span>` : ""}
          </div>
          <div class="greeting-box mt-8" style="font-size:12px">${escapeHtml(m.greeting || "（无招呼语）")}</div>
        </div>`).join("");
      const dlg = modal({
        title: `确认发送 · 共 ${targets.length} 条`,
        body: `
          <div class="banner warn mb-12">将先<strong>建立会话</strong>，再把下方的招呼语正文作为<strong>聊天消息</strong>单独发给招聘方。</div>
          ${restrictionNote}
          ${scopeNote ? `<div class="muted mb-8" style="font-size:12px">${escapeHtml(scopeNote)}</div>` : ""}
          ${rows}
          <div class="btn-row mt-16">
            <button class="btn primary" id="m-confirm">确认发送</button>
            <button class="btn ghost" id="m-cancel">取消</button>
          </div>`,
        wide: true,
      });
      const ok = dlg.box.querySelector("#m-confirm");
      const no = dlg.box.querySelector("#m-cancel");
      if (!ok || !no) return;
      no.addEventListener("click", () => dlg.close());
      ok.addEventListener("click", () => {
        dlg.close();
        helpers.startDeliver(targets, { mode, confirmed: true, confirmationToken: mode === "manual" ? `ui-${Date.now()}` : "" });
      });
    },

    /**
     * 打开单条匹配详情弹窗：分析结果、职位信息、招呼语草稿与发送。
     *
     * @param {string} jid 职位 encrypt_job_id
     */
    openMatchDetail(jid) {
      const m = state.matches.find((x) => x.encrypt_job_id === jid);
      if (!m) return;
      if (state.detailApi) {
        state.detailApi.close();
        state.detailApi = null;
      }

      const score = m.match_score || 0;
      const failed = !!(m.error || "").trim();
      const verdict = (m.verdict || "").trim();
      const advice = (m.advice || "").trim();
      const matched = (m.matched_skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");
      const missing = (m.missing_skills || []).map((s) => `<span class="pill static bad">${escapeHtml(s)}</span>`).join(" ");
      const pros = (m.pros || []).map((p) => `<div class="muted" style="font-size:12.5px">✓ ${escapeHtml(p)}</div>`).join("");
      const cons = (m.cons || []).map((c) => `<div class="muted" style="font-size:12.5px">✗ ${escapeHtml(c)}</div>`).join("");
      const draft = state.greetDrafts.has(jid) ? state.greetDrafts.get(jid) : (m.greeting || "");
      const safetyLabels = [
        m.company_blacklisted ? "公司黑名单" : "",
        m.jd_blacklisted ? "JD 关键词黑名单" : "",
        !m.jd_rule_checked ? "JD 规则未检查" : "",
        m.jd_status ? `JD 状态：${m.jd_status}` : "",
        m.auto_delivery_blocked ? "自动投递已阻断" : "",
      ].filter(Boolean);
      const snapshotRules = (m.matched_rule_snapshot || []).map((rule) =>
        `<li>${escapeHtml(rule.rule_type || "规则")} · ${escapeHtml(rule.pattern || "")} · ${escapeHtml(rule.matched_text || "")}</li>`
      ).join("");

      const cool = helpers.greetState(m);
      const sendDisabled = cool.in_cooldown ? "disabled" : "";
      const coolNote = cool.in_cooldown
        ? `<div class="banner warn mt-8">该岗位 ${escapeHtml(cool.last_date)} 已发送过，${escapeHtml(cool.cooldown_date)} 后可再次发送</div>`
        : m.delivered_at && cool.last_date
          ? `<div class="muted mt-8" style="font-size:11px">上次发送 ${escapeHtml(cool.last_date)}</div>`
          : "";

      const dlg = modal({
        wide: true,
        body: `
          <div class="flex between gap-12 mb-16 wrap" style="align-items:flex-start">
            <div>
              <div class="flex center gap-12">
                ${scoreRing(score)}
                <div>
                  <div style="font-size:17px;font-weight:650">${escapeHtml(m.brand_name)}</div>
                  <div class="muted" style="font-size:12.5px">${escapeHtml(m.location || "")}</div>
                </div>
              </div>
              <div class="md-boss muted mt-12" style="font-size:12.5px"></div>
            </div>
            <div class="job-salary">${escapeHtml(m.salary_desc || "面议")}</div>
          </div>

          <div class="card-sub mb-8">匹配分析</div>
          <div class="pills mb-8">${
            safetyLabels.length
              ? safetyLabels.map((label) => `<span class="pill static bad">${escapeHtml(label)}</span>`).join(" ")
              : `<span class="pill static accent">安全状态已检查</span>`
          } ${
            cool.in_cooldown
              ? `<span class="pill static warn">冷却中 · ${escapeHtml(cool.cooldown_date)} 可再发</span>`
              : helpers.sendRestriction(m)
                ? `<span class="pill static bad">不可发送</span>`
                : m.deliver_status === "failed"
                  ? `<span class="pill static bad">发送失败</span>`
                  : m.delivered_at
                    ? `<span class="pill static accent">已发送</span>`
                    : m.deliver_status === "sending"
                      ? `<span class="pill static">发送中…</span>`
                      : !(m.greeting || "").trim()
                        ? `<span class="pill static warn">无招呼语</span>`
                        : `<span class="pill static ok">可发送</span>`
          }</div>
          ${snapshotRules ? `<details class="acc mb-8"><summary>匹配时命中规则快照</summary><ul class="acc-body">${snapshotRules}</ul></details>` : ""}
          ${verdict ? `<div class="muted mb-8" style="font-size:12.5px"><span class="muted" style="font-size:11px">结论</span> ${escapeHtml(verdict)}</div>` : ""}
          ${advice ? `<div class="muted mb-8" style="font-size:12.5px"><span class="muted" style="font-size:11px">建议</span> ${escapeHtml(advice)}</div>` : ""}
          ${matched ? `<div class="card-sub mb-8 mt-12">技能匹配</div><div class="job-meta">${matched}</div>` : ""}
          ${missing ? `<div class="card-sub mb-8 mt-12">缺口</div><div class="pills">${missing}</div>` : ""}
          ${pros ? `<div class="card-sub mb-8 mt-12">优点</div>${pros}` : ""}
          ${cons ? `<div class="card-sub mb-8 mt-12">缺点</div>${cons}` : ""}
          ${!verdict && !advice && !matched && !missing && !pros && !cons ? `<div class="muted mb-8" style="font-size:12px">这条没有分析内容${m.error ? "（匹配失败）" : ""}</div>` : ""}

          <div class="card-sub mb-8 mt-16">职位信息</div>
          <div class="md-job-extra"><div class="muted" style="font-size:12px">加载职位详情…</div></div>

          <div class="card-sub mb-8 mt-16">招呼语 <span class="muted" style="font-size:11px">生成后先存草稿，满意再点保存</span></div>
          <textarea class="input mono greeting-edit" rows="3" style="width:100%;font-size:12.5px">${escapeHtml(draft)}</textarea>
          <div class="btn-row mt-8">
            ${failed ? `<button class="btn sm primary btn-rematch">重新匹配</button>` : ""}
            <button class="btn sm btn-regen-greet">重新生成</button>
            <button class="btn sm ghost btn-save-greet">保存招呼语</button>
            <button class="btn sm primary btn-send-one" ${sendDisabled}>发送</button>
            <button class="btn sm ghost btn-interview-prep">面试准备</button>
            <span class="muted send-status" style="font-size:11.5px">${escapeHtml(helpers.deliverLabel(m))}</span>
          </div>
          ${coolNote}
          <div class="muted draft-hint mt-8" style="font-size:11px;display:none">草稿未保存，满意后点「保存招呼语」；可反复点「重新生成」换一版</div>
          ${failed ? `<div class="banner bad mt-8">${escapeHtml(m.error)}</div>` : ""}
        `,
        /** 弹窗关闭时若还挂着本条详情句柄，就清掉。 */
        onClose: () => {
          if (state.detailApi && state.detailApi.jid === jid) state.detailApi = null;
        },
      });

      state.detailApi = {
        jid,
        close: dlg.close,
        /** 更新弹窗底部的发送状态文案。 */
        setStatus: (text) => {
          const el = dlg.box.querySelector(".send-status");
          if (el) el.textContent = text;
        },
      };

      const ta = dlg.box.querySelector(".greeting-edit");
      const draftHint = dlg.box.querySelector(".draft-hint");
      /** 把文本框内容存进草稿，并切换「未保存」提示。 */
      const syncDraft = () => {
        state.greetDrafts.set(jid, ta.value);
        const cur = state.matches.find((x) => x.encrypt_job_id === jid);
        const dirty = ta.value.trim() !== (cur?.greeting || "").trim();
        if (draftHint) draftHint.style.display = dirty ? "" : "none";
      };
      ta.addEventListener("input", syncDraft);
      syncDraft();

      const regenBtn = dlg.box.querySelector(".btn-regen-greet");
      regenBtn.addEventListener("click", async () => {
        if (!state.analysisId) return toast("先跑一次匹配再生成招呼语", "warn");
        regenBtn.disabled = true;
        regenBtn.textContent = "生成中…";
        try {
          const r = await api.post(`/api/match/${state.analysisId}/greeting/regenerate`, {
            encrypt_job_id: jid,
          });
          const text = (r.greeting || "").trim();
          if (!text) return toast("AI 没生成出招呼语，再点一次试试", "bad");
          ta.value = text;
          syncDraft();
          toast("已生成新草稿（未保存，可反复生成）", "ok");
        } catch (err) {
          toast(err.message || "生成失败", "bad");
        } finally {
          regenBtn.disabled = false;
          regenBtn.textContent = "重新生成";
        }
      });

      dlg.box.querySelector(".btn-save-greet").addEventListener("click", async () => {
        const text = dlg.box.querySelector(".greeting-edit").value;
        try {
          await api.patch(`/api/match/${state.analysisId}/greeting`, {
            encrypt_job_id: jid,
            greeting: text,
          });
          const cur = state.matches.find((x) => x.encrypt_job_id === jid);
          if (cur) cur.greeting = text;
          state.greetDrafts.delete(jid);
          syncDraft();
          toast("招呼语已保存", "ok");
        } catch (err) {
          toast(err.message || "保存失败", "bad");
        }
      });

      dlg.box.querySelector(".btn-send-one").addEventListener("click", () => {
        const cur = state.matches.find((x) => x.encrypt_job_id === jid);
        if (cur) helpers.confirmAndSend([cur], "单条手动发送需要单独确认；后端仍会按匹配时快照校验", { mode: "manual" });
      });

      dlg.box.querySelector(".btn-interview-prep").addEventListener("click", () => {
        if (!state.analysisId) return toast("先跑一次匹配再准备面试题", "warn");
        openInterviewPrep({
          analysisId: state.analysisId,
          encryptJobId: jid,
          jobName: m.job_name || "",
          brandName: m.brand_name || "",
          matchScore: m.match_score || 0,
        });
      });

      const rematchBtn = dlg.box.querySelector(".btn-rematch");
      if (rematchBtn) {
        rematchBtn.addEventListener("click", async () => {
          if (!state.analysisId) return toast("先跑一次匹配再重新匹配", "warn");
          rematchBtn.disabled = true;
          rematchBtn.textContent = "重新匹配中…";
          try {
            const r = await api.post(`/api/match/${state.analysisId}/rematch`, { encrypt_job_id: jid });
            const item = r.item || {};
            const idx = state.matches.findIndex((x) => x.encrypt_job_id === jid);
            if (idx >= 0) state.matches[idx] = item;
            state.greetDrafts.delete(jid);
            helpers.renderMatches();
            if (item.error) toast(`重新匹配又失败了：${item.error}`, "bad");
            else toast("重新匹配完成，结果已原地覆盖", "ok");
            if (state.detailApi && state.detailApi.jid === jid) helpers.openMatchDetail(jid);
          } catch (err) {
            toast(err.message || "重新匹配失败", "bad");
            if (rematchBtn.isConnected) {
              rematchBtn.disabled = false;
              rematchBtn.textContent = "重新匹配";
            }
          }
        });
      }

      api.get("/api/jobs/" + encodeURIComponent(jid))
        .then((j) => {
          const boss = dlg.box.querySelector(".md-boss");
          if (boss) {
            const who = [j.boss_name, j.boss_title].filter(Boolean).join(" · ");
            boss.innerHTML = who ? `招聘人 <span style="color:var(--text-1)">${escapeHtml(who)}</span>` : "";
          }
          const host = dlg.box.querySelector(".md-job-extra");
          if (!host) return;
          const skills = (j.skills || []).map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ");
          const labels = (j.job_labels || []).map((s) => `<span class="pill static">${escapeHtml(s)}</span>`).join(" ");
          const welfare = (j.welfare_list || []).map((s) => `<span class="pill static">${escapeHtml(s)}</span>`).join(" ");
          host.innerHTML = `
            <div class="job-meta">
              <span class="pill static">${escapeHtml(j.job_experience || "—")}</span>
              <span class="pill static">${escapeHtml(j.job_degree || "—")}</span>
              <span class="pill static">${escapeHtml(j.brand_industry || "—")}</span>
              <span class="pill static">${escapeHtml(j.brand_scale_name || "—")}</span>
              ${j.brand_stage_name ? `<span class="pill static">${escapeHtml(j.brand_stage_name)}</span>` : ""}
            </div>
            ${skills ? `<div class="card-sub mb-8 mt-12">技能</div><div class="job-meta">${skills}</div>` : ""}
            ${labels ? `<div class="card-sub mb-8 mt-12">标签</div><div class="pills">${labels}</div>` : ""}
            ${welfare ? `<div class="card-sub mb-8 mt-12">福利</div><div class="pills">${welfare}</div>` : ""}
            <div class="flex between center mb-8 mt-12">
              <div class="card-sub">职位描述</div>
              <span class="pill ${j.job_desc ? "accent" : "static"}">${j.job_desc ? "已抓描述" : "无描述"}</span>
            </div>
            ${j.job_desc
              ? `<details class="acc"><summary>展开 JD（${String(j.job_desc).length} 字）</summary><div class="acc-body" style="white-space:pre-wrap">${escapeHtml(j.job_desc)}</div></details>`
              : `<div class="muted" style="font-size:12px">列表接口不带 JD；到「职位」页点「补抓描述」。</div>`}
          `;
        })
        .catch(() => {
          const host = dlg.box.querySelector(".md-job-extra");
          if (host) host.innerHTML = `<div class="muted" style="font-size:12px">职位详情不可用（可能已从库里删除）</div>`;
        });
    },

    /** 清空当前分析结果与草稿，恢复初始状态。 */
    clearAnalysis() {
      state.analysisId = null;
      state.matches = [];
      state.greetDrafts.clear();
      if (state.detailApi) {
        state.detailApi.close();
        state.detailApi = null;
      }
      const pane = root.querySelector("#match-pane");
      const anSub = pane?.querySelector("#an-sub");
      const clearBtn = pane?.querySelector("#btn-clear-analysis");
      if (anSub) anSub.textContent = "尚未匹配";
      if (clearBtn) clearBtn.classList.add("hidden");
      helpers.renderMatches();
      helpers.renderJobs();
      helpers.updateSendCounts();
      toast("已清空分析结果", "ok");
    },

    /** 把历史分析载入工作台并刷新列表。 */
    loadAnalysisIntoWorkbench(data) {
      state.analysisId = data.analysis_id;
      state.matches = data.matches || [];
      if (state.detailApi) {
        state.detailApi.close();
        state.detailApi = null;
      }
      state.greetDrafts.clear();
      helpers.renderMatches();
      helpers.renderJobs();
    },
  };

  const pane = root.querySelector("#match-pane");
  const tabs = [...root.querySelectorAll(".settings-tab")];
  let active = "workbench";
  let loaded = { workbench: false, deliver: false, crawls: false, history: false };

  /** 高亮当前目录 tab 并更新 aria-current。 */
  function setActive(name) {
    active = name;
    tabs.forEach((tab) => {
      const on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-current", on ? "page" : "false");
    });
  }

  /** 切换并渲染指定子目录。 */
  async function show(name) {
    if (name === active && loaded[name]) return;
    setActive(name);
    loaded[name] = true;
    pane.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>加载中…</p></div>`;
    if (name === "workbench") {
      renderWorkbench(pane, state, helpers);
      return;
    }
    if (name === "deliver") {
      renderDeliver(pane, state, helpers);
      return;
    }
    if (name === "crawls") {
      await renderCrawls(pane, state, helpers);
      return;
    }
    await renderHistory(pane, state, helpers);
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

  /** 拉取简历列表，默认选中第一份。 */
  async function loadResumes() {
    try {
      const r = await api.get("/api/resume/list");
      state.resumes = r.items || [];
      if (state.resumes.length) {
        state.currentResumeId = state.resumes[0].resume_id;
        state.resumeTitle = state.resumes[0].title || state.currentResumeId;
      }
    } catch {}
  }

  await loadResumes();
  await helpers.loadJobs();
  await helpers.restoreTask();
  await helpers.restoreDeliver();
  await show("workbench");

  const offWsMatch = ws.on("task.progress", (m) => {
    if (m.task_type !== "match" || !m.task_id) return;
    if (state.matchWatches.has(m.task_id)) return;
    helpers.watchMatch(m.task_id);
  });

  /** 卸载时停掉匹配 / 投递的任务观察器。 */
  return () => {
    offWsMatch();
    [...state.matchWatches.values()].forEach((stop) => stop());
    state.matchWatches.clear();
    if (state.deliverWatch) state.deliverWatch();
  };
}
