/** 登录航段：手机号 → 短信码 → （滑块人机协作）→ 会话 */

import { api } from "../api.js";
import { toast, modal, el, escapeHtml } from "../ui.js";
import { refreshBridge } from "../app.js";

export async function renderLogin(root) {
  root.innerHTML = `
    <h1 class="hero-title">接入 <span class="grad">BOSS 会话</span></h1>
    <p class="hero-sub">短信验证码 + 极验滑块都在这个页面完成。滑块由<strong>你本人拖动</strong>官方组件，系统只做票据管道，不认缺口、不伪造轨迹。</p>

    <div class="bento">
      <div class="card span-7 glow" id="gate">
        <div class="card-head">
          <h3 class="card-title">登录门</h3>
          <span class="card-sub" id="gate-state">idle</span>
        </div>

        <div class="track" id="track">
          <div class="node on" data-step="1"><i></i><span>手机号</span></div>
          <div class="node" data-step="2"><i></i><span>验证码</span></div>
          <div class="node" data-step="3"><i></i><span>滑块</span></div>
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
            <span class="muted" style="font-size:12px">触发滑块时会自动弹出验证层</span>
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
            <button class="btn ghost" id="btn-cancel">取消本次</button>
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

  function setStep(n) {
    root.querySelectorAll("#track .node").forEach((node) => {
      const s = Number(node.dataset.step);
      node.classList.toggle("on", s === n);
      node.classList.toggle("done", s < n);
    });
    $("step-phone").classList.toggle("hidden", n !== 1);
    $("step-code").classList.toggle("hidden", n !== 2);
    $("step-done").classList.toggle("hidden", n !== 4);
    if (n === 3) {
      $("step-code").classList.remove("hidden");
    }
  }

  function renderEvents(events) {
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

  function openSlider(task) {
    const url = `/api/auth/slider/${task.task_id}`;
    if (sliderModal) sliderModal.close();
    const body = el(`
      <div>
        <div class="banner warn">请拖动下面的滑块完成验证 —— 答案在你手里，系统不会替你算。</div>
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
      },
    });
    body.querySelector("#btn-retry-slider").addEventListener("click", () => {
      const f = body.querySelector("iframe");
      f.src = url;
    });
  }

  function applyTask(task) {
    if (!task || !task.task_id) return;
    taskId = task.task_id;
    $("gate-state").textContent = task.status;
    renderEvents(task.events);

    const st = task.status;
    if (st === "need_code") {
      setStep(2);
      $("code-hint").textContent = task.error
        ? task.error
        : `验证码已发送到 ${task.phone_masked || "你的手机"}`;
    } else if (st === "need_slider") {
      setStep(3);
      if (!sliderModal) openSlider(task);
    } else if (st === "done") {
      setStep(4);
      if (sliderModal) { sliderModal.close(); sliderModal = null; }
      toast("登录成功，会话已保存", "ok");
      refreshSession();
      refreshBridge();
    } else if (st === "error") {
      setStep(1);
      if (sliderModal) { sliderModal.close(); sliderModal = null; }
      toast(task.error || "登录失败", "bad");
    } else if (st === "cancelled") {
      setStep(1);
      toast("已取消本次登录", "warn");
    } else if (st === "logging_in") {
      setStep(3);
    }
  }

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
      setStep(2);
      applyTask(task);
      clearInterval(pollTimer);
      pollTimer = setInterval(poll, 900);
      poll();
      toast("已发起发送短信", "ok");
    } catch (err) {
      toast(err.message || "发送失败", "bad");
    }
  });

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

  $("btn-cancel").addEventListener("click", async () => {
    if (!taskId) return;
    try {
      await api.post("/api/auth/cancel", { task_id: taskId });
      setStep(1);
      toast("已取消", "warn");
    } catch (err) {
      toast(err.message, "bad");
    }
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

  refreshSession();

  // 若已有进行中的任务，接上
  try {
    const st = await api.get("/api/auth/status");
    if (st.task && !["done", "error", "cancelled"].includes(st.task.status)) {
      applyTask(st.task);
      pollTimer = setInterval(poll, 900);
    }
  } catch { /* ignore */ }

  return () => {
    clearInterval(pollTimer);
    if (sliderModal) sliderModal.close();
  };
}
