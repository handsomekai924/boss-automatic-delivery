/** AI 设置：OpenAI 兼容接口配置 · 模型名在线下拉 · 第一次配置的分步引导 */

import { api } from "../api.js";
import { toast, escapeHtml, taskPanel, modal } from "../ui.js";

//: DeepSeek 的「一键预设」。用户自己去找 Base URL 是最容易卡住的一步，
//: 直接给填好。Key 必须他自己去平台拿——程序碰不到，也不该碰。
const PRESET = {
  label: "DeepSeek",
  site: "https://platform.deepseek.com",
  keysPage: "https://platform.deepseek.com/api_keys",
  baseUrl: "https://api.deepseek.com/v1",
  model: "deepseek-chat",
};

//: 引导截图。有图更好懂，没有也不影响——onerror 直接摘掉，卡片退化成纯文字。
//: 图放 ``boss_web/static/guide/``，随 static 目录一起进包。
const GUIDE_SHOTS = {
  login: { src: "/static/guide/deepseek-login.png", alt: "DeepSeek 开放平台的登录页" },
  recharge: { src: "/static/guide/deepseek-recharge.png", alt: "左侧菜单里的「充值」入口" },
  keys: { src: "/static/guide/deepseek-api-keys.png", alt: "「API keys」页面右上角的创建按钮" },
  copy: { src: "/static/guide/deepseek-copy-key.png", alt: "创建后弹窗里的复制按钮" },
};

function shot(key) {
  const s = GUIDE_SHOTS[key];
  if (!s) return "";
  return `<img class="guide-shot" src="${s.src}" alt="${s.alt}" loading="lazy" onerror="this.remove()">`;
}

//: 配置指引正文。整段塞进弹窗里，页面上只留一个入口按钮——
//: 步骤带截图，展开收起会把表单顶到很下面，弹窗关掉就完事。
function guideBodyHtml() {
  return `
    <ol class="guide-steps">
      <li>
        <div class="guide-n">1</div>
        <div class="guide-body">
          <b>打开 DeepSeek 开放平台，登录</b>
          <p>
            网址
            <a class="mono" href="${PRESET.site}" target="_blank" rel="noopener">platform.deepseek.com</a>，
            手机号收验证码或微信扫码都行，没注册过会自动帮你注册。
            登录后建议顺手做一下<strong>实名认证</strong>，否则建不了 Key。
          </p>
          ${shot("login")}
        </div>
      </li>
      <li>
        <div class="guide-n">2</div>
        <div class="guide-body">
          <b>先充一点钱</b>
          <p>
            左侧菜单点「<strong>充值</strong>」，充 10 元就够试很久（余额不会过期）。
            不充值也能建 Key，但一调用就会报「余额不足」。
          </p>
          ${shot("recharge")}
        </div>
      </li>
      <li>
        <div class="guide-n">3</div>
        <div class="guide-body">
          <b>建一个 API Key，然后复制走</b>
          <p>
            左侧菜单点「<a class="mono" href="${PRESET.keysPage}" target="_blank" rel="noopener">API keys</a>」，
            再点右上角「<strong>创建 API key</strong>」，随便起个名字（比如「自动投递」）。
          </p>
          <p>
            创建完会弹出一串 <span class="mono">sk-</span> 开头的字符——
            <strong>这就是钥匙，只显示这一次</strong>，请立刻点「复制」存下来，
            关掉窗口就再也看不到了（丢了只能删掉重新建一个）。
          </p>
          ${shot("keys")}
          ${shot("copy")}
        </div>
      </li>
      <li>
        <div class="guide-n">4</div>
        <div class="guide-body">
          <b>粘贴到表单里</b>
          <p>
            点下面的「<strong>一键填入 DeepSeek 预设</strong>」把接口地址填好（会自动关掉本窗口）→
            在「API Key」里粘贴刚才复制的那串 →
            点「<strong>拉取模型</strong>」→ 选中 <span class="mono">${PRESET.model}</span> →
            点「<strong>保存配置</strong>」。保存后程序会自己测一下通不通。
          </p>
        </div>
      </li>
    </ol>

    <div class="banner info mb-16">
      DeepSeek 的页面按钮偶尔会改名字，认准带「API」字样的入口就对了。
      用别的也行——任何「OpenAI 兼容」的服务（Kimi、通义、本地部署的模型）都是填三个东西：
      接口地址、Key、模型名。
    </div>

    <div class="btn-row">
      <button class="btn primary btn-preset" type="button">一键填入 DeepSeek 预设</button>
      <a class="btn" href="${PRESET.keysPage}" target="_blank" rel="noopener">去拿 Key（新窗口打开）</a>
    </div>
  `;
}

export async function renderLLM(root, onDirty = () => {}) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">AI <span class="grad">设置</span></h1>
        <p class="hero-sub">填一次就行。Key 只存在你自己电脑上，页面上不会明文回显；请求直接从这台电脑发出去，不经过任何中转。</p>
      </div>
    </div>

    <div class="bento">
      <div class="card span-12" id="guide-card">
        <div class="card-head">
          <h3 class="card-title" id="guide-title">还没有 Key？照着做一次就好</h3>
          <span class="card-sub">第一次配置约 3 分钟</span>
        </div>
        <div class="btn-row">
          <button class="btn sm primary btn-preset" id="btn-preset" type="button">一键填入 DeepSeek 预设</button>
          <a class="btn sm" href="${PRESET.keysPage}" target="_blank" rel="noopener">去拿 Key（新窗口打开）</a>
          <button class="btn sm" id="guide-open" type="button">查看配置指引</button>
        </div>
      </div>

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
          <label>接口地址（Base URL）</label>
          <input class="input mono" id="base-url" placeholder="${PRESET.baseUrl}">
          <span class="hint">DeepSeek 就用上面那个默认值；别的服务填它给你的地址</span>
        </div>

        <div class="field">
          <label>模型</label>
          <div class="flex center gap-8">
            <select class="select" id="model" style="flex:1">
              <option value="">— 先点右边按钮拉取模型 —</option>
            </select>
            <button class="btn" id="btn-models" type="button">拉取模型</button>
          </div>
          <span class="hint" id="model-hint">点「拉取模型」会从上面的接口地址读出可用模型，选一个再保存</span>
        </div>

        <div class="banner info mb-16" id="fixed-params">
          程序已经定好 AI 的生成参数了（温度 0.7 · 通用 2048 tokens · 简历解析 8192 tokens · 人岗匹配 4096 tokens · 超时 120 秒）。
        </div>

        <div class="btn-row mb-16">
          <button class="btn primary" id="btn-save">保存配置</button>
          <button class="btn" id="btn-test">测试连接</button>
          <span class="pill" id="test-result">还没测过</span>
        </div>

        <div id="llm-task"></div>
      </div>

      <div class="card span-5">
        <div class="card-head">
          <h3 class="card-title">连接状态</h3>
          <span class="card-sub">随时可重新测试</span>
        </div>
        <div class="flex center gap-12 mb-24">
          <div class="orb-ring" id="llm-ring" style="--p:0;width:96px;height:96px;font-size:18px">
            <div>—<small>未测</small></div>
          </div>
          <div>
            <div style="font-size:18px;font-weight:650" id="llm-title">还没配置</div>
            <div class="muted" id="llm-desc" style="font-size:12px">填好 Key 和接口地址 → 拉取模型 → 保存 → 测试连接</div>
          </div>
        </div>
        <div class="divider"></div>
        <div class="card-sub mb-8">这些地方会用到 AI</div>
        <p class="muted" style="font-size:13px;line-height:1.8">
          · 把你的简历整理成固定格式<br>
          · 逐个岗位算匹配度、说明好在哪差在哪<br>
          · 给 HR 写一句开场白
        </p>
        <div class="banner info mt-16">
          Key 只存在你电脑上的 data/boss.db 里，请求直接从本机发到你填的地址。
        </div>
      </div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);

  /** 拉到的模型列表（用于下拉框） */
  let modelList = [];

  // 同步 HTTP 也可能要几十秒（LLM 测试超时 30s），用统一面板给进度
  const linkPanel = taskPanel({ title: "测试连接", stopLabel: "" });
  $("llm-task").appendChild(linkPanel.el);
  linkPanel.update({ status: "idle", percent: 0 });

  function paintLink(state, detail, percent) {
    linkPanel.update({ status: state, percent, current: detail, counts: [] });
  }

  // ---------- 引导：正文进弹窗，页面只留入口按钮 ----------
  // 配好了只换标题，不再做展开/收起——弹窗自己会关，页面上不需要记状态
  function paintGuide(configured) {
    $("guide-title").textContent = configured
      ? "配置指引"
      : "还没有 Key？照着做一次就好";
  }

  /** 一键填入 DeepSeek 预设：填接口地址，光标丢到 Key 框 */
  function applyPreset() {
    $("base-url").value = PRESET.baseUrl;
    onDirty(true);
    const key = $("api-key");
    key.focus();
    toast(`已填好 ${PRESET.label} 的接口地址，粘贴 Key 后点「拉取模型」`, "ok");
  }

  /** 打开配置指引弹窗。宽一点，截图才看得清 */
  function openGuide() {
    const dlg = modal({ title: "API Key 配置指引", body: guideBodyHtml(), wide: true });
    // 弹窗里也有「一键填入」——先关窗再填，焦点才落到 Key 框上
    dlg.bodyEl.querySelector(".btn-preset").addEventListener("click", () => {
      dlg.close();
      applyPreset();
    });
  }

  $("guide-open").addEventListener("click", openGuide);
  $("btn-preset").addEventListener("click", applyPreset);

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
    $("fixed-params").innerHTML =
      `程序已经定好 AI 的生成参数了（temperature ${f.temperature ?? 0.7} · ` +
      `通用 ${f.max_tokens ?? 2048} tokens · ` +
      `简历解析 ${f.resume_parse_max_tokens ?? 8192} tokens · ` +
      `人岗匹配 ${f.match_max_tokens ?? 4096} tokens · ` +
      `超时 ${f.timeout ?? 120} 秒）。`;
  }

  function paintReady(configured, model, baseUrl) {
    $("cfg-state").textContent = configured ? "已配置" : "没配置";
    if (!configured) {
      $("llm-title").textContent = "还没配置";
      $("llm-desc").textContent = "填好 Key 和接口地址 → 拉取模型 → 保存 → 测试连接";
      $("llm-ring").style.setProperty("--p", 0);
      $("llm-ring").innerHTML = `<div>—<small>未测</small></div>`;
      return;
    }
    $("llm-title").textContent = model;
    $("llm-desc").textContent = baseUrl;
    $("llm-ring").style.setProperty("--p", 100);
    $("llm-ring").innerHTML = `<div>已就绪<small>待测试</small></div>`;
  }

  async function load() {
    try {
      const c = await api.get("/api/llm/config");
      $("base-url").value = c.base_url || "";
      setFixedBanner(c.fixed);
      $("key-hint").textContent = c.has_key
        ? `已保存 Key：${c.api_key}（留空则不修改）`
        : "还没有保存过 Key";
      fillModelSelect(c.model || "");
      paintReady(c.configured, c.model, c.base_url);
      paintGuide(c.configured);
      // 已有 Key 和接口地址就顺手把模型列表拉下来
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
    paintLink("running", "正在拉取模型列表…", 30);
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
      $("model-hint").innerHTML = `拉到 <span class="mono">${modelList.length}</span> 个可用模型，选一个再保存`;
      paintLink("done", `拉到 ${modelList.length} 个模型`, 100);
      if (!opts.silent) toast(`已拉到 ${modelList.length} 个模型`, "ok");
      return true;
    } catch (err) {
      $("model-hint").textContent = "拉取失败：" + err.message;
      paintLink("error", err.message || "拉取失败", 100);
      if (!opts.silent) toast("拉模型失败：" + err.message, "bad");
      return false;
    } finally {
      btn.disabled = false;
      btn.textContent = "拉取模型";
    }
  }

  $("btn-models").addEventListener("click", () => fetchModels());
  ["api-key", "base-url", "model"].forEach((id) => {
    $(id).addEventListener("input", () => onDirty(true));
    $(id).addEventListener("change", () => onDirty(true));
  });

  $("btn-save").addEventListener("click", async () => {
    const model = $("model").value.trim();
    if (!model) {
      toast("请先点「拉取模型」，再选一个", "warn");
      return;
    }
    const key = $("api-key").value.trim();
    try {
      const payload = {
        base_url: $("base-url").value.trim(),
        model,
      };
      if (key) payload.api_key = key;
      const saved = await api.put("/api/llm/config", payload);
      $("key-hint").textContent = saved.has_key
        ? `已保存 Key：${saved.api_key}（留空则不修改）`
        : "还没有保存过 Key";
      $("api-key").value = "";
      onDirty(false);
      setFixedBanner(saved.fixed);
      paintReady(saved.configured, saved.model, saved.base_url);
      paintGuide(true);
      toast("配置已保存", "ok");
      // 刚填了新 Key 就顺手验一下——不然用户以为配好了，到匹配那步才发现 Key 是错的
      if (key) await testConnection({ auto: true });
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  /**
   * 测试连接。
   * @param {{auto?: boolean}} opts auto=保存后自动跑，失败不弹 toast
   */
  async function testConnection(opts = {}) {
    const pill = $("test-result");
    pill.className = "pill warn";
    pill.textContent = "测试中…";
    $("llm-ring").style.setProperty("--p", 40);
    $("llm-ring").innerHTML = `<div>…<small>测试中</small></div>`;
    paintLink("running", "正在测试连接…", 40);
    try {
      const r = await api.post("/api/llm/test");
      if (r.ok) {
        pill.className = "pill ok";
        pill.textContent = `正常 · ${r.latency_ms} 毫秒`;
        $("llm-ring").style.setProperty("--p", 100);
        $("llm-ring").innerHTML = `<div>已就绪<small>${r.latency_ms} 毫秒</small></div>`;
        $("llm-title").textContent = r.model || "连接正常";
        $("llm-desc").textContent = `AI 回了一句：${escapeHtml(r.message || "")}`;
        paintLink("done", `连接正常 · ${r.latency_ms} 毫秒`, 100);
        toast("AI 连接正常，可以用了", "ok");
      } else {
        pill.className = "pill bad";
        pill.textContent = "没通";
        $("llm-ring").style.setProperty("--p", 100);
        $("llm-ring").innerHTML = `<div>!<small>没通</small></div>`;
        $("llm-desc").textContent = r.message || "";
        paintLink("error", r.message || "测试没通过", 100);
        toast(r.message || "测试没通过", "bad");
      }
    } catch (err) {
      pill.className = "pill bad";
      pill.textContent = "没通";
      paintLink("error", err.message || "测试没通过", 100);
      if (!opts.auto) toast(err.message, "bad");
    }
  }

  $("btn-test").addEventListener("click", () => testConnection());

  await load();
}
