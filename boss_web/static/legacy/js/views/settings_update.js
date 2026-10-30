/** 系统配置 → 版本更新：手动检测、就地看更新日志、出错一键复制诊断信息。 */

import { api } from "../api.js";
import { toast, escapeHtml, modal, fmtTime } from "../ui.js";
import { applyUpdateStatus, openUpdateDialog } from "../update.js";

/** 日志读失败 / 更新失败时横幅里要能看到版本号，一张截图就是一份反馈 */
export async function renderSettingsUpdate(root) {
  root.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">版本更新</h2>
        <div class="card-sub">检查新版本、查看更新日志；出问题可一键复制诊断信息发给开发者</div>
      </div>
      <div class="btn-row">
        <button class="btn" id="upd-check" type="button">检查更新</button>
        <button class="btn primary" id="upd-apply" type="button" hidden>立即更新</button>
        <button class="btn" id="upd-open" type="button" hidden>打开更新窗口</button>
        <button class="btn" id="upd-refresh-log" type="button">刷新日志</button>
        <button class="btn ghost" id="upd-copy" type="button">复制诊断信息</button>
      </div>
    </div>
    <div id="upd-error-banner"></div>
    <div class="settings-scroll">
      <div class="update-summary" id="upd-summary"></div>
      <div class="divider"></div>
      <div class="card-sub mb-8">更新日志 <span class="muted">（%TEMP%\\BossAutoDelivery-update\\，不用自己打开）</span></div>
      <div class="card-sub" id="lbl-update-log">update.log</div>
      <pre class="update-log-view" id="log-update">加载中…</pre>
      <div class="card-sub" id="lbl-progress-log">progress.log</div>
      <pre class="update-log-view" id="log-progress">加载中…</pre>
    </div>
  `;

  /** 最近一次接口回来的原始载荷，「复制诊断信息」直接从这拼 */
  let payload = null;

  /** 拉日志 + 诊断，成功整页重画，失败只出错误横幅。 */
  async function loadAll() {
    try {
      payload = await api.get("/api/system/update/log");
    } catch (err) {
      payload = null;
      renderBanner(null, err.message || "读不到更新日志，请重试");
      return;
    }
    renderAll();
  }

  /** 按当前 payload 刷摘要、日志、横幅和按钮。 */
  function renderAll() {
    if (!payload) return;
    renderSummary(payload);
    renderLogs(payload);
    renderBanner(payload, null);
    syncButtons(payload);
  }

  /**
   * 画顶部版本摘要：当前 / 最新 / 安装方式 / 上次检查结果 / 忽略版本。
   *
   * @param {Object} p 日志接口载荷，用 `p.diagnostics`
   */
  function renderSummary(p) {
    const d = p.diagnostics || {};
    const current = d.current || "—";
    const latest = d.latest ? `v${d.latest}` : "未测到";
    const checked = d.checked_at ? fmtTime(d.checked_at) : "还没测过";
    const result = d.error
      ? `检测失败：${d.error}`
      : d.has_update
        ? `有新版本 v${d.latest}${d.should_prompt ? "" : "（已忽略，不再提醒）"}`
        : "已是最新";
    const ignored = d.ignored ? `v${d.ignored}` : "无";
    root.querySelector("#upd-summary").innerHTML = `
      <div class="update-versions mb-8">
        <span class="pill static">当前 v${escapeHtml(current)}</span>
        <span class="pill ${d.has_update ? "on" : "static"}">最新 ${escapeHtml(latest)}</span>
        <span class="pill static">${escapeHtml(d.install_mode_label || "")}</span>
      </div>
      <div class="muted mb-8">上次检查：${escapeHtml(checked)} · 结果：${escapeHtml(result)}</div>
      <div class="muted">忽略版本：${escapeHtml(ignored)}${d.can_self_update ? "" : "　·　当前环境不支持应用内更新"}</div>
    `;
  }

  /**
   * 错误横幅：版本 + 原因 + 「日志就在下面」——拍一张就够反馈。
   *
   * @param {?Object} p          日志接口载荷；`null` 表示只有 `extraError`
   * @param {?string} extraError 额外错误文案（接口本身挂了时用）
   */
  function renderBanner(p, extraError) {
    const box = root.querySelector("#upd-error-banner");
    const d = (p && p.diagnostics) || {};
    const logReadError = p ? (p.files || []).map((f) => f.read_error).filter(Boolean)[0] : null;
    const failed = d.last_stage === "error" ? d.last_stage_text || "更新失败" : "";
    const reason = extraError || d.error || failed || logReadError;
    if (!reason) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = `
      <div class="banner bad mb-12 column">
        <div class="mb-8"><strong>当前版本 v${escapeHtml(d.current || "?")} · ${escapeHtml(d.install_mode_label || "未知环境")}</strong></div>
        <div class="mb-8">${escapeHtml(reason)}</div>
        <div class="muted">完整日志见下方；方便的话点「复制诊断信息」把内容发给开发者。</div>
      </div>
    `;
  }

  /**
   * 把 update.log / progress.log 画进对应的 pre。
   *
   * @param {Object} p 日志接口载荷
   */
  function renderLogs(p) {
    const byName = {};
    for (const f of p.files || []) byName[f.name] = f;
    paintLog("#log-update", byName["update.log"], p.hint);
    paintLog("#log-progress", byName["progress.log"], p.hint);
  }

  /**
   * 画单个日志文件内容；缺文件 / 读失败 / 截断各有占位文案。
   *
   * @param {string}  sel  目标元素选择器
   * @param {?Object} file 文件项（`exists` / `read_error` / `truncated` / `content`）
   * @param {?string} hint 缺文件时的提示
   */
  function paintLog(sel, file, hint) {
    const el = root.querySelector(sel);
    if (!el) return;
    if (!file || !file.exists) {
      el.classList.add("empty");
      el.textContent = hint || "（没有日志）";
      return;
    }
    el.classList.remove("empty");
    if (file.read_error) {
      el.textContent = `（读取失败：${file.read_error}）\n更新结束后点「刷新日志」再看。`;
      return;
    }
    const truncated = file.truncated ? `（只显示最后 ${fmtBytes(file.size)} 的尾部）\n` : "";
    el.textContent = truncated + (file.content || "（空文件）");
    el.scrollTop = el.scrollHeight;
  }

  /**
   * 按诊断显隐「立即更新 / 打开更新窗口」。
   *
   * @param {Object} p 日志接口载荷
   */
  function syncButtons(p) {
    const d = p.diagnostics || {};
    const canApply = Boolean(d.has_update && d.can_self_update && d.zip_asset && d.zip_asset.url);
    root.querySelector("#upd-apply").hidden = !canApply;
    root.querySelector("#upd-open").hidden = !d.has_update;
  }

  /** 强制检测一次，顺手刷新日志区，结果用 toast 汇报。 */
  async function checkNow() {
    const btn = root.querySelector("#upd-check");
    btn.disabled = true;
    btn.textContent = "检测中…";
    try {
      const status = await api.get("/api/system/update?force=true");
      try {
        payload = await api.get("/api/system/update/log");
        renderAll();
      } catch {
        renderSummary({ diagnostics: status });
        renderBanner({ diagnostics: status }, null);
        syncButtons({ diagnostics: status });
      }
      if (status.error) {
        toast(status.error, "bad", 6000);
      } else if (status.has_update) {
        toast(`发现新版本 v${status.latest}`, "ok");
      } else {
        toast("已是最新", "ok");
      }
      applyUpdateStatus(status);
    } catch (err) {
      renderBanner(null, err.message || "检测失败，请重试");
      toast(err.message || "检测失败", "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = "检查更新";
    }
  }

  /** 打开现有「发现新版本」窗（复用 apply / WS / 忽略那一套），先拿完整状态带上更新说明 */
  async function openDialog() {
    let status = null;
    try {
      status = await api.get("/api/system/update");
    } catch {
    }
    const d = (payload && payload.diagnostics) || {};
    openUpdateDialog(
      status || {
        current: d.current,
        latest: d.latest,
        notes: "",
        has_update: d.has_update,
        should_prompt: d.should_prompt,
        ignored: d.ignored,
        gitee_url: "https://gitee.com/w-kev/boss-automatic-delivery/releases",
        github_url: "https://github.com/handsomekai924/boss-automatic-delivery/releases",
        zip_asset: d.zip_asset,
        can_self_update: d.can_self_update,
      },
    );
  }

  /** 诊断信息 = 一份能直接贴进微信/邮件的纯文本，版本号和日志都在里面 */
  function buildDiagnosticsText() {
    const p = payload || {};
    const d = p.diagnostics || {};
    const lines = [];
    const push = (k, v) => lines.push(`${k}：${v == null || v === "" ? "无" : v}`);
    lines.push("=== BossAutoDelivery 诊断信息 ===");
    push("导出时间", new Date().toLocaleString("zh-CN"));
    push("当前版本", d.current ? `v${d.current}` : "未知");
    push("最新版本", d.latest ? `v${d.latest}` : "（未测到）");
    push("安装方式", d.install_mode_label || "未知");
    push("安装目录", d.install_dir);
    push("数据目录", d.data_dir);
    push("上次检查", d.checked_at ? fmtTime(d.checked_at) : "还没测过");
    push(
      "检查结果",
      d.error
        ? `失败：${d.error}`
        : d.has_update
          ? `有新版本 v${d.latest}${d.should_prompt ? "" : "（已忽略）"}`
          : "已是最新",
    );
    push("忽略版本", d.ignored ? `v${d.ignored}` : "无");
    push("自更新", d.can_self_update ? "可以" : "不可以");
    push("最近更新阶段", d.last_stage ? `${d.last_stage} — ${d.last_stage_text || ""}` : "无");
    push("日志目录", p.work_dir);
    for (const f of p.files || []) {
      lines.push("");
      lines.push(`--- ${f.name}（最后 ${fmtBytes(f.size || 0)}）---`);
      if (!f.exists) lines.push("（文件不存在）");
      else if (f.read_error) lines.push(`（读取失败：${f.read_error}）`);
      else lines.push(f.content || "（空文件）");
    }
    return lines.join("\n");
  }

  /** 复制诊断信息；剪贴板被拦就弹手动复制框。 */
  async function copyDiagnostics() {
    const text = buildDiagnosticsText();
    const ok = await copyText(text);
    if (ok) {
      toast("诊断信息已复制", "ok");
      return;
    }
    const box = modal({
      title: "复制诊断信息",
      body: `
        <div class="muted mb-8">浏览器不让自动复制。请点进下面的框，全选后 Ctrl+C 发给开发者。</div>
        <textarea class="input" rows="14" readonly style="width:100%;font-family:var(--mono);white-space:pre-wrap">${escapeHtml(text)}</textarea>
      `,
    });
    const ta = box.bodyEl.querySelector("textarea");
    if (ta) {
      ta.focus();
      ta.select();
    }
  }

  root.querySelector("#upd-check").addEventListener("click", checkNow);
  root.querySelector("#upd-refresh-log").addEventListener("click", loadAll);
  root.querySelector("#upd-copy").addEventListener("click", copyDiagnostics);
  root.querySelector("#upd-apply").addEventListener("click", openDialog);
  root.querySelector("#upd-open").addEventListener("click", openDialog);

  /** `boss:update` 广播进来时，就地改诊断字段并重画摘要/横幅/按钮。 */
  const onUpdate = (e) => {
    const status = (e && e.detail) || {};
    if (!payload) payload = {};
    payload.diagnostics = {
      ...(payload.diagnostics || {}),
      current: status.current,
      latest: status.latest,
      has_update: status.has_update,
      should_prompt: status.should_prompt,
      ignored: status.ignored,
      error: status.error,
      checked_at: status.checked_at,
      can_self_update: status.can_self_update,
      zip_asset: status.zip_asset,
      install_mode_label: (payload.diagnostics || {}).install_mode_label,
    };
    renderSummary(payload);
    renderBanner(payload, null);
    syncButtons(payload);
  };
  window.addEventListener("boss:update", onUpdate);

  await loadAll();

  return () => {
    window.removeEventListener("boss:update", onUpdate);
  };
}

/** 复制到剪贴板；被拒/不可用回 false，让上层弹手动复制框 */
async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/**
 * 字节数 → 「1.2 KB」这类可读大小。
 *
 * @param {number} n 字节数
 * @returns {string} 可读大小
 */
function fmtBytes(n) {
  const size = Number(n) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
