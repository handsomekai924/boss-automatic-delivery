/** 工作台：便当格总览 */

import { api } from "../api.js";
import { fmtTime, toast } from "../ui.js";

/**
 * 渲染工作台：会话 / 职位 / AI / 简历卡片 + 环境自检。
 *
 * 会话卡片订阅舰桥的 `boss:session` 广播，避免只在渲染时取一次状态就卡在「离线」。
 * 采集等任务的进度不在这画，也不放跳转按钮——任务中心的唯一入口在顶栏。
 *
 * @param {HTMLElement} root 挂载容器
 * @returns {Promise<() => void>} 卸载时退订广播
 */
export async function renderHome(root) {
  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">自动投递<span class="grad">助手</span></h1>
        <p class="hero-sub">登录 → 找职位 → 传简历 → 自动投递。四步都在这一页里，跟着下面的卡片走就行。</p>
      </div>
      <div class="btn-row">
        <a class="btn primary" href="#/jobs">开始找工作</a>
        <a class="btn" href="#/match">去看匹配</a>
      </div>
    </div>

    <div class="bento" id="bento">
      <div class="card span-4 lift" id="card-session">
        <div class="card-head">
          <h3 class="card-title">会话</h3>
          <span class="card-sub" id="sess-sub">—</span>
        </div>
        <div class="stat-num" id="sess-num">…</div>
        <div class="stat-label" id="sess-label">登录状态</div>
        <div class="btn-row mt-16">
          <a class="btn primary" href="#/login" id="btn-login">去登录</a>
          <a class="btn ghost hidden" href="#/login" id="btn-logout">退出登录</a>
        </div>
      </div>

      <div class="card span-4 lift" id="card-jobs">
        <div class="card-head">
          <h3 class="card-title">职位库</h3>
          <span class="card-sub" id="jobs-sub">本地保存</span>
        </div>
        <div class="stat-num" id="jobs-num">…</div>
        <div class="stat-label">已入库职位</div>
        <div class="mt-12 muted mono" id="jobs-pages">流水 —</div>
        <div class="btn-row mt-16">
          <a class="btn" href="#/jobs">去找工作</a>
        </div>
      </div>

      <div class="card span-4 lift" id="card-llm">
        <div class="card-head">
          <h3 class="card-title">AI 接口</h3>
          <span class="card-sub" id="llm-sub">OpenAI 兼容</span>
        </div>
        <div class="flex center gap-12">
          <span class="signal" id="llm-signal"></span>
          <div>
            <div style="font-size:16px;font-weight:650" id="llm-model">未配置</div>
            <div class="muted mono" id="llm-base" style="font-size:12px">—</div>
          </div>
        </div>
        <div class="btn-row mt-16">
          <a class="btn" href="#/settings">系统配置</a>
        </div>
      </div>

      <div class="card span-7">
        <div class="card-head">
          <h3 class="card-title">任务中心</h3>
          <span class="card-sub">顶栏入口</span>
        </div>
        <div class="muted">采集、匹配、投递等任务的进度、当前状态、停止和重试都挪到任务中心了；跑着的任务会在顶栏「任务」角标上亮起。</div>
      </div>

      <div class="card span-5 lift" id="card-resume">
        <div class="card-head">
          <h3 class="card-title">简历匹配</h3>
          <span class="card-sub" id="an-sub">—</span>
        </div>
        <div id="an-list" class="muted">还没有分析记录</div>
        <div class="btn-row mt-16">
          <a class="btn primary" href="#/match">去看匹配</a>
          <a class="btn" href="#/resume">我的简历</a>
        </div>
      </div>

      <div class="card span-12" id="card-env">
        <div class="card-head">
          <h3 class="card-title">环境自检</h3>
          <span class="card-sub" id="env-sub">—</span>
        </div>
        <div id="env-banners"></div>
        <div class="flex between center gap-12 wrap">
          <div class="flex center gap-12">
            <span class="signal" id="chrome-signal"></span>
            <div>
              <div id="chrome-text" style="font-size:14px;font-weight:650">检测中…</div>
              <div class="muted mono" id="chrome-path" style="font-size:12px">—</div>
            </div>
          </div>
          <div class="btn-row">
            <select class="select" id="chrome-mode" title="取安全令牌时 Chrome 窗口怎么摆"></select>
            <button class="btn" id="btn-recheck" type="button">重新检测</button>
            <button class="btn" id="btn-open-chrome" type="button">打开验证窗口</button>
          </div>
        </div>
        <div class="muted mt-12" id="env-data" style="font-size:12px">数据目录 —</div>
      </div>

      <div class="card span-12">
        <div class="card-head">
          <h3 class="card-title">最近的搜索记录</h3>
          <span class="card-sub">每次翻页的结果</span>
        </div>
        <div class="ticker" id="page-log"><div class="ev muted">—</div></div>
      </div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);

  /**
   * 按会话状态刷会话卡片，并接上登录 / 退出按钮。
   *
   * @param {Object<string, any>} st 会话状态（logged_in / user / phone_masked / saved_at）
   */
  function paintSession(st) {
    if (!st) return;
    const name = (st.user?.name || "").trim();
    $("sess-num").textContent = st.logged_in ? "在线" : "离线";
    $("sess-label").textContent = st.logged_in
      ? name || st.phone_masked || "已登录"
      : "未登录";
    $("sess-sub").textContent = st.saved_at ? fmtTime(st.saved_at) : "—";
    const primary = $("btn-login");
    const secondary = $("btn-logout");
    if (st.logged_in) {
      primary.textContent = "重新登录";
      secondary.classList.remove("hidden");
      secondary.onclick = async (e) => {
        e.preventDefault();
        await api.post("/api/auth/logout");
        location.reload();
      };
    } else {
      primary.textContent = "去登录";
      secondary.classList.add("hidden");
    }
  }

  const onSession = (e) => paintSession(e.detail);
  window.addEventListener("boss:session", onSession);
  api.get("/api/auth/status").then(paintSession).catch(() => {});

  api.get("/api/jobs/stats").then((s) => {
    $("jobs-num").textContent = s.jobs ?? 0;
    $("jobs-pages").textContent = `流水 ${s.pages ?? 0} 页 · 原始 ${s.raw_seen ?? 0} 条`;
    const log = $("page-log");
    if (s.pages_log && s.pages_log.length) {
      log.innerHTML = s.pages_log
        .map((p) => {
          const t = p.fetched_at ? fmtTime(p.fetched_at) : "";
          return `<div class="ev"><time>${t}</time><span class="name">page ${p.page}</span><span>raw ${p.raw_count} / kept ${p.kept_count} / +${p.inserted}</span></div>`;
        })
        .join("");
    }
  }).catch(() => {});

  api.get("/api/llm/config").then((c) => {
    const sig = $("llm-signal");
    if (c.configured) {
      sig.className = "signal ok";
      $("llm-model").textContent = c.model;
      $("llm-base").textContent = c.base_url;
    } else {
      sig.className = "signal warn";
    }
  }).catch(() => {});

  /** Chrome 取令牌窗口档位 → 下拉文案。 */
  const MODE_LABELS = {
    hidden: "隐藏窗口（默认，不打扰）",
    visible: "显示窗口（要你手动过验证时选它）",
    offscreen: "窗口放到屏幕外",
    headless: "无界面",
  };

  /**
   * 往环境自检区加一条横幅。
   *
   * @param {string} kind 样式类名（bad / warn / ok）
   * @param {string} text 文案；用 textContent 写入，防服务端兜底文案带出标记
   * @param {{chrome?: boolean}} [opts] chrome=true 时打上 data-chrome，便于重检时清掉
   */
  function addBanner(kind, text, { chrome = false } = {}) {
    const box = document.createElement("div");
    box.className = "banner " + kind;
    box.style.whiteSpace = "pre-line";
    box.textContent = text;
    if (chrome) box.dataset.chrome = "1";
    $("env-banners").appendChild(box);
  }

  api.get("/api/system/health").then((h) => {
    $("env-sub").textContent = h.frozen ? "打包版" : "源码运行";
    $("env-data").textContent = `当前版本：v${h.version || "?"} · 数据目录 ${h.data_dir}`;
    if (h.temp_run) {
      addBanner(
        "bad",
        "程序是在临时目录里运行的——多半是直接在压缩包里双击了。\n" +
          "请先把程序解压到桌面再打开，否则简历和登录状态一关就没。"
      );
    }
    if (!h.writable) {
      addBanner("warn", "程序所在目录写不进去，数据已改存到上面这个目录（备份就拷贝它）。");
    }
  }).catch(() => {});

  /** 检测 Chrome 并刷新信号灯 / 路径 / 窗口档位选项。 */
  async function refreshChrome() {
    const sel = $("chrome-mode");
    root.querySelectorAll('[data-chrome="1"]').forEach((n) => n.remove());
    try {
      const c = await api.get("/api/system/chrome");
      $("chrome-signal").className = "signal " + (c.found ? "ok" : "bad");
      $("chrome-text").textContent = c.found ? "已找到 Chrome，可以正常抓取" : "没找到 Chrome";
      $("chrome-path").textContent = c.path || "";
      if (!c.found) addBanner("bad", c.message, { chrome: true });
      if (!sel.options.length) {
        sel.innerHTML = c.modes
          .map((m) => `<option value="${m}">${MODE_LABELS[m] || m}</option>`)
          .join("");
      }
      sel.value = c.mode;
    } catch {
      $("chrome-signal").className = "signal bad";
      $("chrome-text").textContent = "检测失败";
      $("chrome-path").textContent = "";
    }
  }
  refreshChrome();

  $("btn-recheck").onclick = async () => {
    await refreshChrome();
    toast("已重新检测", "ok");
  };

  $("chrome-mode").onchange = async (e) => {
    try {
      await api.post("/api/system/chrome/mode", { mode: e.target.value });
      toast("窗口档位已切换，下次取令牌时生效", "ok");
    } catch (err) {
      toast(err.message || "切换失败", "bad");
    }
  };

  $("btn-open-chrome").onclick = async () => {
    try {
      const r = await api.post("/api/system/chrome/open");
      toast(r.message || "已打开", "ok");
    } catch (err) {
      toast(err.message || "打不开 Chrome", "bad");
    }
  };

  api.get("/api/resume/analyses").then((r) => {
    const items = r.items || [];
    $("an-sub").textContent = items.length ? `${items.length} 份` : "—";
    if (!items.length) return;
    $("an-list").innerHTML = items
      .slice(0, 3)
      .map(
        (a) =>
          `<div class="flex between center gap-8" style="margin-bottom:6px">
            <span>${a.resume_title || a.analysis_id}</span>
            <span class="pill accent">最高 ${a.top_score}</span>
          </div>`
      )
      .join("");
  }).catch(() => {});

  return () => {
    window.removeEventListener("boss:session", onSession);
  };
}
