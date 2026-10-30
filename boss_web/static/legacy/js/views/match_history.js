/** 分析历史：历史分析列表 + 载入 / 删除 */

import { api } from "../api.js";
import { toast, escapeHtml, fmtTime } from "../ui.js";

/**
 * 渲染分析历史目录：历史列表 + 载入 / 删除。
 *
 * @param {HTMLElement} pane   内容容器
 * @param {Object}     state   页面级状态（analysisId / matches）
 * @param {Object}     helpers 共享助手（loadAnalysisIntoWorkbench 等）
 */
export async function renderHistory(pane, state, helpers) {
  pane.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">分析历史</h2>
        <div class="card-sub" id="history-sub">data/boss.db · analysis 表</div>
      </div>
      <button class="btn sm" id="btn-refresh-history">刷新</button>
    </div>
    <div id="history-list"></div>
  `;

  const $ = (id) => pane.querySelector("#" + id);
  $("btn-refresh-history").addEventListener("click", () => load());

  await load();

  /** 拉取历史分析列表并绑定载入 / 删除按钮。 */
  async function load() {
    let data;
    try {
      data = await api.get("/api/match/analyses");
    } catch (err) {
      $("history-list").innerHTML = `<div class="banner bad">${escapeHtml(err.message || "加载失败")}</div>`;
      return;
    }
    const items = data.items || [];
    $("history-sub").textContent = `共 ${items.length} 条`;

    if (!items.length) {
      $("history-list").innerHTML = `
        <div class="empty">
          <div class="empty-icon">◌</div>
          <p>还没有历史分析</p>
        </div>`;
      return;
    }

    $("history-list").innerHTML = items.map((a) => `
      <div class="flex between center gap-12" style="padding:12px 0;border-bottom:1px solid var(--stroke)">
        <div>
          <div style="font-size:13.5px">${escapeHtml(a.resume_title || a.analysis_id)}</div>
          <div class="muted" style="font-size:11.5px">
            ${escapeHtml(fmtTime(a.created_at))} · ${a.job_count} 个职位
            ${a.model ? ` · 模型 ${escapeHtml(a.model)}` : ""}
            ${a.status ? ` · ${escapeHtml(a.status)}` : ""}
          </div>
        </div>
        <div class="flex center gap-8">
          <span class="pill accent">最高 ${a.top_score}</span>
          <button class="btn sm ghost" data-load="${escapeHtml(a.analysis_id)}">载入</button>
          <button class="btn sm danger" data-del="${escapeHtml(a.analysis_id)}">删</button>
        </div>
      </div>
    `).join("");

    $("history-list").querySelectorAll("button[data-load]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          const data = await api.get("/api/match/analyses/" + b.dataset.load);
          helpers.loadAnalysisIntoWorkbench(data);
          helpers.switchTab("workbench");
          toast("已载入历史分析", "ok");
        } catch (err) {
          toast(err.message || "载入失败", "bad");
        }
      });
    });
    $("history-list").querySelectorAll("button[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        if (!confirm("删除这份分析？")) return;
        try {
          await api.del("/api/match/analyses/" + b.dataset.del);
          if (state.analysisId === b.dataset.del) {
            state.analysisId = null;
            state.matches = [];
            helpers.renderMatches();
          }
          load();
          toast("已删除", "ok");
        } catch (err) {
          toast(err.message || "删除失败", "bad");
        }
      });
    });
  }
}
