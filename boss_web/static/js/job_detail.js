/** 职位详情弹窗（复用给「找工作」页与「简历优化」选岗位清单）。 */

import { api } from "./api.js";
import { toast, modal, escapeHtml } from "./ui.js";

const JD_STATUS_LABEL = {
  pending: "待采集",
  collecting: "采集中",
  completed: "已完成",
  empty: "JD 为空",
  failed: "采集失败",
  unchecked: "规则未检查",
};

/**
 * 打开职位详情弹窗。
 *
 * @param {string} id 加密职位 ID
 * @param {{ onDeleted?: (id: string) => void }} opts 删除后回调（示例：重刷列表）
 */
export async function openJobDetail(id, opts = {}) {
  const onDeleted = opts.onDeleted;
  try {
    const j = await api.get("/api/jobs/" + encodeURIComponent(id));
    const skills = (j.skills || [])
      .map((s) => `<span class="chip">${escapeHtml(s)}</span>`)
      .join(" ");
    const welfare = (j.welfare_list || [])
      .map((s) => `<span class="pill static">${escapeHtml(s)}</span>`)
      .join(" ");
    const matchedRules = (j.matched_rule_snapshot || [])
      .map(
        (rule) => `
        <div class="settings-rule">
          <strong>${escapeHtml(rule.rule_type === "company" ? "公司规则" : "JD 关键词")}</strong>
          <span class="pill static">${escapeHtml(rule.match_mode || "")}</span>
          <div>${escapeHtml(rule.pattern || "")}</div>
          <div class="muted">命中内容：${escapeHtml(rule.matched_text || "—")}${rule.source ? ` · 范围 ${escapeHtml(rule.source)}` : ""}</div>
        </div>`
      )
      .join("");
    const jdStatusLabel = JD_STATUS_LABEL[j.jd_status] || j.jd_status || "未知";
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
            if (onDeleted) onDeleted(id);
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
