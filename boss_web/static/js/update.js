/** 版本更新提示：开页面时比一次 Gitee 发行版，有新的就弹窗 */

import { api } from "./api.js";
import { modal, escapeHtml, toast } from "./ui.js";

/**
 * 有新版本就弹「前往更新」窗口。三个出口：
 *
 * - **前往 Gitee / GitHub 下载**——打开对应发行版页（版本判断只用 Gitee）
 * - **忽略此版本**——记进后端，这一版不再弹；发了更**新**的版本照样弹
 * - **取消 / 关掉**——什么都不记，下次打开页面还会弹
 */
export async function maybePromptUpdate() {
  let status;
  try {
    status = await api.get("/api/system/update");
  } catch {
    // 检测接口本身挂了（本地服务问题）不该再弹一个错框
    return;
  }
  if (!status || !status.should_prompt) return;
  showUpdateModal(status);
}

function showUpdateModal(status) {
  const current = escapeHtml(status.current || "—");
  const latest = escapeHtml(status.latest || "—");
  const notes = String(status.notes || "").trim();
  const notesHtml = notes
    ? `<details class="acc mb-16" open><summary>更新说明</summary><div class="acc-body update-notes">${escapeHtml(notes)}</div></details>`
    : `<div class="muted mb-16">这版没附更新说明。</div>`;

  const box = modal({
    title: "发现新版本",
    body: `
      <div class="mb-16">
        <div class="update-versions">
          <span class="pill static">当前 v${current}</span>
          <span class="update-arrow" aria-hidden="true">→</span>
          <span class="pill on">最新 v${latest}</span>
        </div>
      </div>
      ${notesHtml}
      <div class="muted mb-8" style="font-size:12px">
        点「取消」下次还会提醒；点「忽略此版本」则这一版不再弹，等有更新的版本再提。
      </div>
      <div class="btn-row">
        <button class="btn primary" type="button" data-act="gitee">前往 Gitee 下载</button>
        <button class="btn" type="button" data-act="github">前往 GitHub 下载</button>
        <button class="btn ghost" type="button" data-act="ignore">忽略此版本</button>
        <button class="btn ghost" type="button" data-act="cancel">取消</button>
      </div>
    `,
    onClose() {
      // 取消 / Esc / 点外面 = 下次仍提示，什么都不写
    },
  });

  box.bodyEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "gitee" || act === "github") {
      const url = act === "gitee" ? status.gitee_url : status.github_url;
      if (url) window.open(url, "_blank", "noopener");
      return;
    }
    if (act === "cancel") {
      box.close();
      return;
    }
    if (act === "ignore") {
      btn.disabled = true;
      try {
        await api.post("/api/system/update/ignore", { version: status.latest });
        toast(`已忽略 v${status.latest}`, "ok");
        box.close();
      } catch (err) {
        btn.disabled = false;
        toast(err.message || "记不住这个选择，请重试", "bad");
      }
    }
  });
}
