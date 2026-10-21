/** 批量发送：上下文条 + 发送工具条 + 投递任务面板 + 发送清单 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtTime } from "../ui.js";

const MIN_SCORE_DEFAULT = 70;

/**
 * 渲染批量发送目录：发送工具条、投递任务面板、发送清单。
 *
 * @param {HTMLElement} pane   内容容器
 * @param {Object}     state   页面级状态（matches / analysisId / selected 等）
 * @param {Object}     helpers 共享助手（confirmAndSend、sendAllTargets 等）
 */
export function renderDeliver(pane, state, helpers) {
  const { matches, analysisId, selected, resumeTitle } = state;
  const { deliverPanelHost, sendRestriction, sendAllTargets, updateSendCounts,
          confirmAndSend, paintDeliver, deliverLabel, startDeliverPoll } = helpers;

  const hasAnalysis = !!analysisId && matches.length > 0;
  const ctxLabel = hasAnalysis
    ? `当前分析：${escapeHtml(resumeTitle || "")} · ${matches.length} 条结果`
    : "还没有分析结果";

  pane.innerHTML = `
    <div class="mb-16">
      <div class="flex between center gap-12 wrap mb-8">
        <div class="card-sub">${ctxLabel}</div>
        ${!hasAnalysis ? `<button class="btn sm" id="btn-goto-workbench">去匹配工作台</button>` : ""}
      </div>
      ${!hasAnalysis ? `<div class="empty"><div class="empty-icon">◌</div><p>先在「匹配工作台」跑一次匹配，结果会出现在这里</p></div>` : ""}
    </div>
    ${hasAnalysis ? `
    <div class="card mb-16">
      <div class="card-head">
        <h3 class="card-title">发送工具条</h3>
        <span class="card-sub">每次发送前都会弹窗让你确认 · 30 天内已发过的会跳过 · 发出去的就是招呼语正文</span>
      </div>
      <div class="flex between center wrap gap-12 mb-16">
        <div class="btn-row">
          <button class="btn primary" id="btn-send-all">一键发送全部 (<span id="send-all-n">0</span>)</button>
          <button class="btn" id="btn-send-sel">发送选中 (<span id="send-sel-n">0</span>)</button>
        </div>
        <label class="flex center gap-8">
          <span class="muted" style="font-size:12.5px">评分阈值</span>
          <input type="number" class="input" id="send-min-score" value="70" min="0" max="100" step="1" style="width:76px">
          <span class="muted" style="font-size:11.5px">≥ 该分数才进「一键发送全部」，改完自动保存</span>
        </label>
        <span class="muted" id="send-excluded" style="font-size:12px"></span>
      </div>
      <div id="deliver-task"></div>
    </div>
    <div class="card">
      <div class="card-head">
        <h3 class="card-title">发送清单</h3>
        <span class="card-sub" id="send-list-sub">共 ${matches.length} 条</span>
      </div>
      <div id="send-list" class="scroll-y"></div>
    </div>
    ` : ""}
  `;

  const $ = (id) => pane.querySelector("#" + id);

  if (!hasAnalysis) {
    $("btn-goto-workbench")?.addEventListener("click", () => helpers.switchTab("workbench"));
    return;
  }

  if (deliverPanelHost) $("deliver-task").appendChild(deliverPanelHost);

  const minInput = $("send-min-score");
  helpers.loadMinScore(minInput);
  minInput.addEventListener("input", () => helpers.updateSendCounts(minInput));
  minInput.addEventListener("change", () => helpers.saveMinScore(minInput));

  $("btn-send-all").addEventListener("click", () => {
    const min = helpers.minScore(minInput);
    const targets = helpers.sendAllTargets(minInput);
    if (!targets.length) return toast(`没有「评分 ≥ ${min}、安全状态完整、未在冷却中且有招呼语」的条目`, "warn");
    helpers.confirmAndSend(targets, `筛选条件：评分 ≥ ${min}`, { mode: "all" });
  });
  $("btn-send-sel").addEventListener("click", () => {
    const targets = matches.filter((m) => selected.has(m.encrypt_job_id) && !helpers.inCooldown(m) && (m.greeting || "").trim());
    if (!targets.length) return toast("选中的条目里没有「有招呼语、未发送且不在冷却中」的", "warn");
    helpers.confirmAndSend(targets, "选中的职位将按匹配时安全快照执行策略", { mode: "selected" });
  });

  renderSendList();

  /** 按分数从高到低渲染发送清单，绑定勾选与详情按钮。 */
  function renderSendList() {
    const box = $("send-list");
    if (!box) return;
    const ranked = [...matches].sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
    box.innerHTML = ranked.map((m) => {
      const jid = m.encrypt_job_id;
      const cool = helpers.greetState(m);
      const disabled = cool.in_cooldown ? "disabled" : "";
      const statusText = helpers.sendStatusLabel(m);
      return `
        <div class="flex center gap-8" style="padding:8px 0;border-bottom:1px solid var(--stroke)">
          <input type="checkbox" data-jid="${escapeHtml(jid)}" ${selected.has(jid) ? "checked" : ""} ${disabled}>
          <span class="pill accent" style="flex-shrink:0">${m.match_score || 0}</span>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              ${escapeHtml(m.job_name)} <span class="muted">@ ${escapeHtml(m.brand_name)}</span>
            </div>
            <div class="muted" style="font-size:11px">${escapeHtml(statusText)}</div>
          </div>
          <button class="btn sm ghost" data-detail="${escapeHtml(jid)}" style="flex-shrink:0">详情</button>
        </div>`;
    }).join("") || `<div class="muted">没有匹配结果</div>`;

    box.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(cb.dataset.jid);
        else selected.delete(cb.dataset.jid);
        helpers.updateSelCount();
        helpers.updateSendCounts(minInput);
      });
    });
    box.querySelectorAll("[data-detail]").forEach((btn) => {
      btn.addEventListener("click", () => helpers.openMatchDetail(btn.dataset.detail));
    });
  }

  helpers.updateSendCounts(minInput);
}
