/** 系统配置 → 招呼语：改写打招呼语生成约束，空值即回到内置默认。 */

import { api } from "../api.js";
import { toast, escapeHtml } from "../ui.js";

/**
 * 「招呼语」面板：编辑 `greeting_prompt.GREETING_REQUIREMENT` 的用户自定义版。
 *
 * 只开这一段约束；匹配 / 重生成 / 简历解析的其他提示词固定在代码里。
 *
 * @param {HTMLElement} root 挂载容器
 * @param {(v: boolean) => void} markDirty 标/清未保存修改
 * @returns {Promise<void>}
 */
export async function renderGreetingPrompt(root, markDirty) {
  root.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">招呼语</h2>
        <div class="card-sub">只改打招呼语的生成约束（字数、语气、禁语）；匹配与简历解析的其他提示词固定</div>
      </div>
      <div class="btn-row">
        <button class="btn" id="greet-restore" type="button">恢复默认</button>
        <button class="btn primary" id="greet-save" type="button">保存</button>
      </div>
    </div>
    <div class="settings-scroll">
      <div class="field">
        <label for="greet-req">打招呼语约束</label>
        <textarea class="input textarea" id="greet-req" rows="8" maxlength="500" placeholder="留空 = 用内置默认"></textarea>
        <span class="hint">留空 = 用内置默认；最多 500 字。保存后下次生成 / 重新生成立即生效。</span>
      </div>
      <div class="card-sub" id="greet-status"></div>
      <div class="divider"></div>
      <div class="card-sub">内置默认</div>
      <pre class="update-log-view" id="greet-default">加载中…</pre>
    </div>
  `;

  const $ = (id) => root.querySelector(`#${id}`);
  const input = $("greet-req");
  let baseline = "";

  /** 回包 → 文本框、状态行、默认值预览。 */
  function fill(data) {
    const custom = (data && data.custom) || "";
    input.value = custom;
    baseline = custom;
    $("greet-status").textContent = custom
      ? "当前状态：使用自定义"
      : "当前状态：使用内置默认";
    $("greet-default").textContent = (data && data.default) || "";
    markDirty(false);
  }

  /** 读当前设置。 */
  async function load() {
    try {
      fill(await api.get("/api/system/greeting-prompt"));
    } catch (err) {
      toast("读招呼语设置失败：" + (err.message || ""), "bad");
    }
  }

  input.addEventListener("input", () => {
    markDirty(input.value.trim() !== baseline.trim());
  });

  $("greet-save").addEventListener("click", async () => {
    try {
      const saved = await api.put("/api/system/greeting-prompt", {
        requirement: input.value,
      });
      fill(saved);
      toast("招呼语约束已保存", "ok");
    } catch (err) {
      toast(err.message || "保存失败", "bad");
    }
  });

  $("greet-restore").addEventListener("click", async () => {
    if (input.value.trim() && !window.confirm("确定恢复成内置默认？自定义内容会丢。")) return;
    try {
      fill(await api.put("/api/system/greeting-prompt", { requirement: "" }));
      toast("已恢复内置默认", "ok");
    } catch (err) {
      toast(err.message || "恢复默认失败", "bad");
    }
  });

  await load();
}
