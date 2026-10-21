/** 登录页：手机号 → 短信码 → （滑块你本人拖）→ 存下登录状态 */

import { api } from "../api.js";
import { toast, modal, el, escapeHtml } from "../ui.js";
import { refreshBridge } from "../app.js";

/**
 * 模块级状态：整页刷新/新开标签会重新求值为 `false`，页内路由来回切保持 `true`。
 * 用来区分「刷新页面」（放弃原流程）与「页内切回」（接上原有流程）。
 */
let bootstrapped = false;

/**
 * 渲染登录页：短信验证码与 APP 扫码两条登录流，以及会话信息侧栏。
 *
 * 已有进行中的任务时：整页刷新视为放弃并清残留；页内切回则接上原任务。
 *
 * @param {HTMLElement} root 挂载容器
 * @returns {() => void} 离开页面时的清理（停轮询、关滑块）
 */
export async function renderLogin(root) {
  const freshPageLoad = !bootstrapped;
  bootstrapped = true;

  root.innerHTML = `
    <div class="page-head">
      <div class="page-head-text">
        <h1 class="hero-title">登录 <span class="grad">BOSS 直聘</span></h1>
        <p class="hero-sub">短信验证码和滑块验证都在这个页面完成。
      </div>
    </div>

    <div class="bento">
      <div class="card span-7 glow" id="gate">
        <div class="card-head">
          <h3 class="card-title">登录门</h3>
          <span class="card-sub" id="gate-state">idle</span>
        </div>

        <div class="btn-row" id="mode-row" style="margin-bottom:12px">
          <button class="btn sm primary" id="mode-sms">短信登录</button>
          <button class="btn sm ghost" id="mode-qr">APP 扫码登录</button>
        </div>

        <div id="mode-sms-panel">
        <div class="track" id="track">
          <div class="node on" data-step="1"><i></i><span>手机号</span></div>
          <div class="node" data-step="2"><i></i><span>滑块</span></div>
          <div class="node" data-step="3"><i></i><span>验证码</span></div>
          <div class="node" data-step="4"><i></i><span>完成</span></div>
        </div>

        <div id="step-phone">
          <div class="row-2">
            <div class="field">
              <label>国际区号</label>
              <input class="input mono" id="dial" value="86">
            </div>
            <div class="field">
              <label>手机号</label>
              <input class="input mono" id="phone" placeholder="13800138000" maxlength="11" autocomplete="tel">
            </div>
          </div>
          <div class="btn-row">
            <button class="btn primary" id="btn-send">发送验证码</button>
          </div>
        </div>

        <div id="step-slider" class="hidden">
          <div class="banner warn">人机验证中：请在弹窗里拖动滑块，完成后弹窗会自动关闭并进入下一步。</div>
          <div class="btn-row">
            <button class="btn primary" id="btn-reopen-slider">打开滑块验证</button>
            <button class="btn ghost" id="btn-abandon">放弃</button>
          </div>
        </div>

        <div id="step-code" class="hidden">
          <div class="field">
            <label>短信验证码</label>
            <input class="input mono" id="code" placeholder="6 位数字" maxlength="8" inputmode="numeric">
            <span class="hint" id="code-hint">验证码已发到你的手机</span>
          </div>
          <div class="btn-row">
            <button class="btn primary" id="btn-login">登录</button>
            <button class="btn ghost" id="btn-cancel">放弃本次</button>
          </div>
        </div>

        <div id="step-done" class="hidden">
          <div class="banner info">✓ 登录成功，会话已写入 <span class="mono">data/boss.db</span>（与命令行 <span class="mono">python -m boss_login</span> 共用）。</div>
          <div class="btn-row">
            <a class="btn primary" href="#/jobs">去抓职位</a>
            <button class="btn danger" id="btn-logout">退出登录</button>
          </div>
        </div>
        </div>

        <div id="mode-qr-panel" class="hidden">
          <div id="qr-idle">
            <div class="banner info">用手机上的 <b>BOSS 直聘 APP</b> 扫下面的二维码，然后在 App 里点「确认登录」。</div>
            <div class="btn-row">
              <button class="btn primary" id="btn-qr-start">生成二维码</button>
            </div>
          </div>

          <div id="qr-wait" class="hidden" style="text-align:center">
            <div id="qr-status" class="muted mb-8">二维码已生成，请用 BOSS APP 扫码</div>
            <img id="qr-img" alt="登录二维码" style="width:220px;height:220px;border-radius:12px;background:#fff;padding:10px;box-sizing:content-box">
            <div id="qr-img-error" class="banner warn hidden" style="text-align:left;max-width:300px;margin:8px auto 0"></div>
            <div class="muted mt-8" id="qr-payload" style="font-size:11px;word-break:break-all"></div>
            <div class="btn-row" style="justify-content:center">
              <button class="btn ghost" id="btn-qr-refresh">刷新二维码</button>
              <button class="btn ghost" id="btn-qr-cancel">放弃</button>
            </div>
          </div>

          <div id="qr-scanned" class="hidden" style="text-align:center">
            <div class="banner warn">✓ 手机已扫码，请在 App 里点「确认登录」</div>
            <img id="qr-head" alt="" style="width:64px;height:64px;border-radius:50%;margin:8px 0">
            <div class="btn-row" style="justify-content:center">
              <button class="btn ghost" id="btn-qr-cancel-2">放弃</button>
            </div>
          </div>

          <div id="qr-done" class="hidden">
            <div class="banner info">✓ 扫码登录成功，会话已写入 <span class="mono">data/boss.db</span>。</div>
            <div class="btn-row">
              <a class="btn primary" href="#/jobs">去抓职位</a>
              <button class="btn danger" id="btn-logout-qr">退出登录</button>
            </div>
          </div>
        </div>
      </div>

      <div class="card span-5">
        <div class="card-head">
          <h3 class="card-title">当前会话</h3>
          <span class="card-sub" id="sess-path">—</span>
        </div>
        <div id="sess-info" class="muted">检测中…</div>
        <div class="divider"></div>
        <div class="card-sub mb-8">事件流</div>
        <div class="ticker" id="events"><div class="ev muted">暂无</div></div>
      </div>
    </div>
  `;

  const $ = (id) => root.querySelector("#" + id);
  let taskId = null;
  let pollTimer = null;
  let sliderModal = null;
  let sliderAutoClose = false;
  let currentTask = null;
  let currentStep = 1;
  let lastStatus = null;

  /**
   * 短信流步骤条与四步面板的显隐。
   *
   * @param {number} n 1 手机号 / 2 滑块 / 3 验证码 / 4 完成
   */
  function goStep(n) {
    currentStep = n;
    root.querySelectorAll("#track .node").forEach((node) => {
      const s = Number(node.dataset.step);
      node.classList.toggle("on", s === n);
      node.classList.toggle("done", s < n);
    });
    $("step-phone").classList.toggle("hidden", n !== 1);
    $("step-slider").classList.toggle("hidden", n !== 2);
    $("step-code").classList.toggle("hidden", n !== 3);
    $("step-done").classList.toggle("hidden", n !== 4);
  }

  let mode = "sms";
  let qrRevision = -1;

  /**
   * 切换短信/扫码页签的面板与按钮样式。
   *
   * @param {"sms"|"qr"} next 目标页签
   */
  function setMode(next) {
    if (next === mode) return;
    mode = next;
    const sms = mode === "sms";
    $("mode-sms-panel").classList.toggle("hidden", !sms);
    $("mode-qr-panel").classList.toggle("hidden", sms);
    $("mode-sms").classList.toggle("primary", sms);
    $("mode-sms").classList.toggle("ghost", !sms);
    $("mode-qr").classList.toggle("primary", !sms);
    $("mode-qr").classList.toggle("ghost", sms);
  }

  /**
   * 只显示扫码流的某一块面板，其余隐藏。
   *
   * @param {"qr-idle"|"qr-wait"|"qr-scanned"|"qr-done"} name 目标面板 id
   */
  function showQrPane(name) {
    for (const id of ["qr-idle", "qr-wait", "qr-scanned", "qr-done"]) {
      $(id).classList.toggle("hidden", id !== name);
    }
  }

  /**
   * 二维码图片加载失败时改显示响应里的 message。
   *
   * @param {HTMLImageElement} img 裂图的 `<img>`
   */
  async function onQrImgError(img) {
    img.onerror = null;
    img.classList.add("hidden");
    let msg = "二维码图片加载失败，请点「刷新二维码」重试";
    try {
      const res = await fetch(img.src);
      const data = await res.json();
      if (data && data.message) msg = data.message;
    } catch { }
    const box = $("qr-img-error");
    box.textContent = msg;
    box.classList.remove("hidden");
    toast(msg, "bad");
  }

  /**
   * 按任务里的 qr 快照刷新二维码图/状态；`revision` 变了才重新取图。
   *
   * @param {Object} task 任务快照，取 `task.qr`
   */
  function applyQr(task) {
    const qr = task.qr || {};
    if (!qr.payload) return;
    if (qr.revision !== qrRevision) {
      qrRevision = qr.revision;
      const img = $("qr-img");
      const err = $("qr-img-error");
      err.classList.add("hidden");
      err.textContent = "";
      img.classList.remove("hidden");
      img.onerror = () => onQrImgError(img);
      img.src = `${qr.image_url || `/api/auth/qr/${task.task_id}/image`}`;
      $("qr-payload").textContent = qr.payload;
    }
    if (qr.head_img) $("qr-head").src = qr.head_img;
    if (qr.scanned) {
      showQrPane("qr-scanned");
      $("qr-status").textContent = "已扫码，等 App 确认…";
    } else {
      showQrPane("qr-wait");
      $("qr-status").textContent = "二维码已生成，请用 BOSS APP 扫码（30 秒内有效）";
    }
  }

  /**
   * 把最近事件画进右侧事件流（最多 25 条）。
   *
   * @param {Array<{at?: number, name: string, payload?: Object}>} events 任务事件列表
   */
  function renderTaskEvents(events) {
    const box = $("events");
    if (!events || !events.length) return;
    box.innerHTML = events
      .slice(-25)
      .map((e) => {
        const t = e.at ? new Date(e.at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
        return `<div class="ev"><time>${t}</time><span class="name">${escapeHtml(e.name)}</span><span>${escapeHtml(
          JSON.stringify(e.payload || {}).slice(0, 80)
        )}</span></div>`;
      })
      .join("");
    box.scrollTop = box.scrollHeight;
  }

  /** 读 `/api/auth/status` 刷新「当前会话」卡片。 */
  async function refreshSession() {
    try {
      const st = await api.get("/api/auth/status");
      $("sess-path").textContent = st.session_path || "—";
      const name = (st.user?.name || "").trim();
      $("sess-info").innerHTML = st.logged_in
        ? `<div style="font-size:20px;font-weight:650">${escapeHtml(name || "已登录")}</div>
           <div class="muted mono" style="font-size:12px">${escapeHtml(st.phone_masked || "")}${name ? " · " + escapeHtml(st.user?.user_id || "") : ""}</div>`
        : `<div>本地没有登录态</div><div class="muted" style="font-size:12px">用上面的门发码登录</div>`;
    } catch {
      $("sess-info").textContent = "读不到会话";
    }
  }

  /** 关掉滑块弹窗；置 `sliderAutoClose`，免得被 `onClose` 当成用户放弃。 */
  function closeSlider() {
    if (!sliderModal) return;
    sliderAutoClose = true;
    sliderModal.close();
    sliderModal = null;
  }

  /**
   * 打开极验官方滑块弹窗。challenge 一次性，重载前必须先 `/refresh` 换新。
   *
   * @param {Object} task 任务快照，取 `task.task_id`
   */
  function openSlider(task) {
    const url = `/api/auth/slider/${task.task_id}`;
    if (sliderModal) return;
    const body = el(`
      <div>
        <div class="banner warn">请拖动下面的滑块完成验证，拖完后弹窗会自动关闭</div>
        <iframe class="slider-frame" src="${url}" title="滑块验证"></iframe>
        <div class="muted mt-8" style="font-size:12px">
          弹层里的页面会自动把票据回传。若卡住，可
          <button class="btn sm ghost" id="btn-retry-slider">重新加载</button>
        </div>
      </div>
    `);
    sliderModal = modal({
      title: "人机验证 · 极验官方组件",
      body,
      wide: true,
      onClose: () => {
        sliderModal = null;
        if (sliderAutoClose) {
          sliderAutoClose = false;
          return;
        }
        abandonLogin("已关闭滑块验证，本次登录已放弃。");
      },
    });
    body.querySelector("#btn-retry-slider").addEventListener("click", async () => {
      const f = body.querySelector("iframe");
      try {
        await api.post(`/api/auth/slider/${task.task_id}/refresh`);
        f.src = `${url}?t=${Date.now()}`;
      } catch (err) {
        toast(err.message || "刷新滑块失败", "bad");
      }
    });
  }

  /**
   * 放弃当前登录流：停轮询、关滑块、把本条流的界面归位，并通知后端取消任务。
   *
   * @param {string} [message] 放弃原因，非空则弹 toast
   */
  async function abandonLogin(message) {
    const id = taskId;
    const wasQr = currentTask?.kind === "qr";
    taskId = null;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    closeSlider();
    if (wasQr) {
      showQrPane("qr-idle");
    } else {
      goStep(1);
    }
    if (id) {
      try {
        await api.post("/api/auth/cancel", { task_id: id });
      } catch { }
    }
    if (message) toast(message, "warn");
  }

  /**
   * 按任务快照分派到短信或扫码处理器。必须按 `task.kind` 分——两条流的
   * status 字符串重叠（pending / logging_in / done），共用分派器会串台。
   *
   * @param {Object} task 任务快照
   */
  function applyTask(task) {
    if (!task || !task.task_id) return;
    taskId = task.task_id;
    currentTask = task;
    $("gate-state").textContent = task.status;
    renderTaskEvents(task.events);

    if (task.kind === "qr") {
      applyQrTask(task);
    } else {
      applySmsTask(task);
    }
  }

  /** 短信登录：只推进短信那条 4 步轨，绝不碰扫码面板。 */
  function applySmsTask(task) {
    const st = task.status;
    const prev = lastStatus;
    lastStatus = st;

    if (st === "need_slider") {
      goStep(2);
      if (!sliderModal) openSlider(task);
    } else if (st === "need_code") {
      closeSlider();
      goStep(3);
      $("code-hint").textContent = task.error
        ? task.error
        : `验证码已发送到 ${task.phone_masked || "你的手机"}`;
    } else if (st === "logging_in") {
      closeSlider();
      goStep(3);
    } else if (st === "pending" || st === "sending_sms") {
      if (prev === "need_slider") {
        closeSlider();
        goStep(3);
      } else if (currentStep < 2) {
        goStep(2);
      }
    } else if (st === "done") {
      closeSlider();
      goStep(4);
      toast("登录成功，会话已保存", "ok");
      refreshSession();
      refreshBridge();
    } else if (st === "error") {
      closeSlider();
      goStep(1);
      toast(task.error || "登录失败", "bad");
    } else if (st === "cancelled") {
      closeSlider();
      goStep(1);
      toast("已取消本次登录", "warn");
    }
  }

  /**
   * APP 扫码登录：只推进扫码那几块面板，绝不碰短信的步骤条，也绝不切页签。
   * 轮询每 900ms 一发，这里若调过 `setMode`，用户点页签会在一秒内被拽回来。
   *
   * @param {Object} task 任务快照
   */
  function applyQrTask(task) {
    const st = task.status;
    lastStatus = st;
    if (task.qr && (task.qr.payload || task.qr.scanned)) applyQr(task);

    if (st === "pending") {
      if (mode === "qr") showQrPane("qr-idle");
    } else if (st === "need_scan") {
      showQrPane("qr-wait");
    } else if (st === "logging_in") {
      showQrPane("qr-scanned");
    } else if (st === "done") {
      showQrPane("qr-done");
      toast("登录成功，会话已保存", "ok");
      refreshSession();
      refreshBridge();
    } else if (st === "error") {
      showQrPane("qr-idle");
      toast(task.error || "扫码登录失败", "bad");
    } else if (st === "cancelled") {
      showQrPane("qr-idle");
      toast("已取消本次扫码登录", "warn");
    }
  }

  /** 拉一次任务状态并分派；终态（done/error/cancelled）时停掉轮询。 */
  async function poll() {
    if (!taskId) return;
    try {
      const task = await api.get(`/api/auth/tasks/${taskId}`);
      applyTask(task);
      if (["done", "error", "cancelled"].includes(task.status)) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    } catch (err) {
      console.warn(err);
    }
  }

  $("btn-send").addEventListener("click", async () => {
    const phone = $("phone").value.trim();
    try {
      const task = await api.post("/api/auth/sms", { phone, dial_code: $("dial").value.trim() || "86" });
      taskId = task.task_id;
      lastStatus = null;
      goStep(2);
      applyTask(task);
      clearInterval(pollTimer);
      pollTimer = setInterval(poll, 900);
      poll();
      toast("已发起发送短信", "ok");
    } catch (err) {
      toast(err.message || "发送失败", "bad");
    }
  });

  /** 请求生成二维码，切到扫码页签并开始轮询。 */
  async function startQr() {
    try {
      qrRevision = -1;
      const task = await api.post("/api/auth/qr", {});
      taskId = task.task_id;
      lastStatus = null;
      setMode("qr");
      applyTask(task);
      clearInterval(pollTimer);
      pollTimer = setInterval(poll, 900);
      poll();
    } catch (err) {
      toast(err.message || "生成二维码失败", "bad");
    }
  }

  $("btn-qr-start").addEventListener("click", startQr);
  $("btn-qr-refresh").addEventListener("click", async () => {
    if (taskId) {
      try {
        await api.post("/api/auth/cancel", { task_id: taskId });
      } catch { }
    }
    await startQr();
  });

  /**
   * 放弃扫码登录：停轮询、回 idle、通知后端取消。
   *
   * @param {string} [message] 放弃原因，非空则弹 toast
   */
  async function cancelQr(message) {
    const id = taskId;
    taskId = null;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    showQrPane("qr-idle");
    if (id) {
      try {
        await api.post("/api/auth/cancel", { task_id: id });
      } catch { }
    }
    if (message) toast(message, "warn");
  }
  $("btn-qr-cancel").addEventListener("click", () => cancelQr("已放弃本次扫码登录"));
  $("btn-qr-cancel-2").addEventListener("click", () => cancelQr("已放弃本次扫码登录"));

  /**
   * 切页签 = 换一条登录流。旧流当场作废（旧二维码留着也只会挡住发码，后端 409），
   * 两条流的界面都归位，旧码作废。
   *
   * @param {"sms"|"qr"} next 目标页签
   */
  async function switchFlow(next) {
    setMode(next);
    const alive = taskId && currentTask && !["done", "error", "cancelled"].includes(currentTask.status);
    if (!alive) return;
    const wasQr = currentTask.kind === "qr";
    await abandonLogin(
      wasQr ? "已放弃本次扫码登录，要扫码请重新生成二维码" : "已放弃短信登录，请重新发送验证码"
    );
    showQrPane("qr-idle");
    goStep(1);
    qrRevision = -1;
  }
  $("mode-sms").addEventListener("click", () => switchFlow("sms"));
  $("mode-qr").addEventListener("click", () => switchFlow("qr"));

  $("btn-login").addEventListener("click", async () => {
    if (!taskId) return toast("请先发送验证码", "warn");
    try {
      const task = await api.post("/api/auth/login", { task_id: taskId, code: $("code").value.trim() });
      applyTask(task);
      if (!pollTimer) pollTimer = setInterval(poll, 900);
    } catch (err) {
      toast(err.message || "登录失败", "bad");
    }
  });

  $("btn-cancel").addEventListener("click", () => {
    abandonLogin("已放弃本次登录");
  });

  $("btn-abandon").addEventListener("click", () => {
    abandonLogin("已放弃本次登录");
  });

  $("btn-reopen-slider").addEventListener("click", () => {
    if (sliderModal) return;
    if (!currentTask?.slider?.needed) return toast("当前没有待完成的滑块验证", "warn");
    openSlider(currentTask);
  });

  const logoutBtn = $("btn-logout");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      await api.post("/api/auth/logout");
      toast("已退出", "ok");
      refreshSession();
      refreshBridge();
    });
  }
  const logoutQrBtn = $("btn-logout-qr");
  if (logoutQrBtn) {
    logoutQrBtn.addEventListener("click", async () => {
      await api.post("/api/auth/logout");
      toast("已退出", "ok");
      showQrPane("qr-idle");
      refreshSession();
      refreshBridge();
    });
  }

  refreshSession();

  try {
    const st = await api.get("/api/auth/status");
    const running = st.task && !["done", "error", "cancelled"].includes(st.task.status);
    if (running) {
      if (freshPageLoad) {
        taskId = st.task.task_id;
        await abandonLogin("页面已刷新，上一次登录已放弃。");
      } else {
        applyTask(st.task);
        setMode(st.task.kind === "qr" ? "qr" : "sms");
        pollTimer = setInterval(poll, 900);
      }
    }
  } catch { }

  return () => {
    clearInterval(pollTimer);
    closeSlider();
  };
}
