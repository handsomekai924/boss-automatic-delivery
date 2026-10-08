/** 模型航段：OpenAI 兼容接口配置 · 模型名在线下拉 */

import { api } from "../api.js";
import { toast, escapeHtml } from "../ui.js";

export async function renderLLM(root) {
  root.innerHTML = `
    <h1 class="hero-title">模型 <span class="grad">链路</span></h1>
    <p class="hero-sub">OpenAI 兼容协议（DeepSeek / Kimi / 本地 vLLM 都行）。Key 只存本地 <span class="mono">data/boss.db</span>，回显时打码。模型名从接口在线拉取。</p>

    <div class="bento">
      <div class="card span-7 glow">
        <div class="card-head">
          <h3 class="card-title">接口配置</h3>
          <span class="card-sub" id="cfg-state">—</span>
        </div>

        <div class="field">
          <label>API Key</label>
          <input class="input mono" id="api-key" type="password" placeholder="sk-…（留空 = 不修改）" autocomplete="off">
          <span class="hint" id="key-hint">已保存的 Key 不会明文回传</span>
        </div>

        <div class="field">
          <label>Base URL</label>
          <input class="input mono" id="base-url" placeholder="https://api.deepseek.com/v1">
          <span class="hint">结尾带 /v1 的兼容网关都可以</span>
        </div>

        <div class="field">
          <label>模型名（在线获取）</label>
          <div class="flex center gap-8">
            <select class="select" id="model" style="flex:1">
              <option value="">— 先拉取模型列表 —</option>
            </select>
            <button class="btn" id="btn-models" type="button">拉取模型</button>
          </div>
          <span class="hint" id="model-hint">列表来自 <span class="mono">GET {base_url}/models</span>，选一个再保存</span>
        </div>

        <div class="banner info mb-16" id="fixed-params">
          系统固定参数：温度 <span class="mono">0.7</span> · max_tokens <span class="mono">2048</span> · 超时 <span class="mono">120s</span>
          <span class="muted">（不可改）</span>
        </div>

        <div class="btn-row">
          <button class="btn primary" id="btn-save">保存配置</button>
          <button class="btn" id="btn-test">测试连接</button>
          <span class="pill" id="test-result">未测试</span>
        </div>
      </div>

      <div class="card span-5">
        <div class="card-head">
          <h3 class="card-title">链路状态</h3>
          <span class="card-sub">signal</span>
        </div>
        <div class="flex center gap-12 mb-24">
          <div class="orb-ring" id="llm-ring" style="--p:0;width:96px;height:96px;font-size:18px">
            <div>—<small>STATUS</small></div>
          </div>
          <div>
            <div style="font-size:18px;font-weight:650" id="llm-title">尚未配置</div>
            <div class="muted" id="llm-desc" style="font-size:12px">填好 Key 与 Base URL → 拉取模型 → 保存 → 测试连接</div>
          </div>
        </div>
        <div class="divider"></div>
        <div class="card-sub mb-8">用途</div>
        <p class="muted" style="font-size:13px;line-height:1.8">
          · 简历章节结构化提取<br>
          · 简历 vs 职位的匹配度评估<br>
          · 生成给招聘方的打招呼语
        </p>
        <div class="banner info mt-16">
          接口请求直接从本机发到你配置的 Base URL，不经过第三方中转。
        </div>
      </div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);

  /** 拉到的模型列表（用于下拉框） */
  let modelList = [];

  function fillModelSelect(selected = "") {
    const sel = $("model");
    const ids = [...modelList];
    // 已保存但不在列表里的模型也保留，别把用户的选择弄丢
    if (selected && !ids.includes(selected)) ids.unshift(selected);
    sel.innerHTML =
      `<option value="">${ids.length ? "请选择模型" : "先拉取模型列表"}</option>` +
      ids
        .map(
          (id) =>
            `<option value="${escapeHtml(id)}" ${id === selected ? "selected" : ""}>${escapeHtml(id)}</option>`
        )
        .join("");
  }

  function setFixedBanner(fixed) {
    const f = fixed || {};
    $("fixed-params").innerHTML = `系统固定参数：温度 <span class="mono">${f.temperature ?? 0.7}</span> · max_tokens <span class="mono">${f.max_tokens ?? 2048}</span> · 超时 <span class="mono">${f.timeout ?? 120}s</span> <span class="muted">（不可改）</span>`;
  }

  async function load() {
    try {
      const c = await api.get("/api/llm/config");
      $("base-url").value = c.base_url || "";
      setFixedBanner(c.fixed);
      $("cfg-state").textContent = c.configured ? "已配置" : "未配置";
      $("key-hint").textContent = c.has_key
        ? `已保存 Key：${c.api_key}（留空则不修改）`
        : "还没有保存过 Key";
      fillModelSelect(c.model || "");
      if (c.configured) {
        $("llm-title").textContent = c.model;
        $("llm-desc").textContent = c.base_url;
        $("llm-ring").style.setProperty("--p", 100);
        $("llm-ring").innerHTML = `<div>OK<small>READY</small></div>`;
      }
      // 已有 Key 和 Base URL 就顺手把模型列表拉下来
      if (c.has_key && c.base_url) {
        await fetchModels({ silent: true });
        fillModelSelect(c.model || "");
      }
    } catch (err) {
      toast("读配置失败：" + err.message, "bad");
    }
  }

  /**
   * 拉模型列表。
   * @param {{silent?: boolean}} opts silent=失败不弹 toast
   */
  async function fetchModels(opts = {}) {
    const btn = $("btn-models");
    btn.disabled = true;
    btn.textContent = "拉取中…";
    $("model-hint").innerHTML = `正在向 <span class="mono">${escapeHtml($("base-url").value.trim() || "…")}/models</span> 请求`;
    try {
      const payload = {
        base_url: $("base-url").value.trim(),
        // 未保存的 Key 也带上，改完先拉再存
        api_key: $("api-key").value.trim() || undefined,
      };
      const r = await api.post("/api/llm/models", payload);
      modelList = r.models || [];
      const current = $("model").value;
      fillModelSelect(current || r.selected || "");
      $("model-hint").innerHTML = `拉到 <span class="mono">${modelList.length}</span> 个模型 · 来自 <span class="mono">${escapeHtml(r.source || "")}</span>`;
      if (!opts.silent) toast(`已拉到 ${modelList.length} 个模型`, "ok");
      return true;
    } catch (err) {
      $("model-hint").textContent = "拉取失败：" + err.message;
      if (!opts.silent) toast("拉模型失败：" + err.message, "bad");
      return false;
    } finally {
      btn.disabled = false;
      btn.textContent = "拉取模型";
    }
  }

  $("btn-models").addEventListener("click", () => fetchModels());

  $("btn-save").addEventListener("click", async () => {
    const model = $("model").value.trim();
    if (!model) {
      toast("请先拉取模型列表并选一个", "warn");
      return;
    }
    try {
      const payload = {
        base_url: $("base-url").value.trim(),
        model,
      };
      const key = $("api-key").value.trim();
      if (key) payload.api_key = key;
      const saved = await api.put("/api/llm/config", payload);
      $("cfg-state").textContent = saved.configured ? "已配置" : "未配置";
      $("key-hint").textContent = saved.has_key
        ? `已保存 Key：${saved.api_key}（留空则不修改）`
        : "还没有保存过 Key";
      $("api-key").value = "";
      setFixedBanner(saved.fixed);
      if (saved.configured) {
        $("llm-title").textContent = saved.model;
        $("llm-desc").textContent = saved.base_url;
        $("llm-ring").style.setProperty("--p", 100);
        $("llm-ring").innerHTML = `<div>OK<small>READY</small></div>`;
      }
      toast("配置已保存", "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  $("btn-test").addEventListener("click", async () => {
    const pill = $("test-result");
    pill.className = "pill warn";
    pill.textContent = "测试中…";
    $("llm-ring").style.setProperty("--p", 40);
    $("llm-ring").innerHTML = `<div>…<small>TEST</small></div>`;
    try {
      const r = await api.post("/api/llm/test");
      if (r.ok) {
        pill.className = "pill ok";
        pill.textContent = `正常 · ${r.latency_ms}ms`;
        $("llm-ring").style.setProperty("--p", 100);
        $("llm-ring").innerHTML = `<div>OK<small>${r.latency_ms}MS</small></div>`;
        $("llm-title").textContent = r.model || "连接正常";
        $("llm-desc").textContent = `回显：${escapeHtml(r.message || "")}`;
        toast("LLM 连接正常", "ok");
      } else {
        pill.className = "pill bad";
        pill.textContent = "失败";
        $("llm-ring").style.setProperty("--p", 100);
        $("llm-ring").innerHTML = `<div>!<small>FAIL</small></div>`;
        $("llm-desc").textContent = r.message || "";
        toast(r.message || "测试失败", "bad");
      }
    } catch (err) {
      pill.className = "pill bad";
      pill.textContent = "失败";
      toast(err.message, "bad");
    }
  });

  await load();
}
