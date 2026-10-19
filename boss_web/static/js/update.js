/** 版本更新提示：开页面时比一次 Gitee 发行版，有新的就弹窗；点「立即更新」走应用内自更新 */

import { api } from "./api.js";
import { modal, escapeHtml, toast } from "./ui.js";

/** 轮询新服务的间隔。swap 期服务在换自己，这期间一律按「还没起来」等 */
const RECONNECT_MS = 1500;
/** 等新服务起来的总时长上限。超过就当更新没跑完，让用户自己看日志 */
const WAIT_LIMIT_MS = 10 * 60 * 1000;
/** 后台轻提示的轮询间隔。检测本身是后端每 5 分钟刷缓存，这里只管读 */
const WATCHER_POLL_MS = 60 * 1000;
/** 「更新完成」提示停留多久再刷新——够看清更新到了哪一版，又不至于干等 */
const RELOAD_DELAY_MS = 1500;
/** 记「这个标签页已经处理过更新回执」的键（必须跟着标签页活过刷新，所以用 sessionStorage） */
const DONE_SEEN_KEY = "boss:update-done-seen";

/** 已经 toast 过的版本号——同一个版本不许反复提醒 */
let notifiedVersion = "";

/**
 * 最近一次 ``/api/system/update`` 的结果。
 *
 * 「版本更新」页签的圆点要**一直亮着**（有新版且没忽略就不灭），所以不能只靠
 * 60s 一轮的 ``boss:update`` 事件碰运气——关掉弹窗进设置页那会儿没事件，
 * 圆点就是灭的。状态进缓存，页面一挂载就能画上。
 */
let lastStatus = null;

/** 把检测结果记进缓存并广播出去，页签圆点和设置页跟着刷 */
export function applyUpdateStatus(status) {
  if (!status) return;
  lastStatus = status;
  window.dispatchEvent(new CustomEvent("boss:update", { detail: status }));
}

/** 进设置页先按这个画圆点；还没测过回 ``null``，调用方自己决定要不要现取 */
export function peekUpdateStatus() {
  return lastStatus;
}

/**
 * 有新版本就弹「发现新版本」窗口。出口：
 *
 * - **立即更新**——应用内自更新：下载 zip → 停服换文件 → 同端口重启，全程 WS 进度
 * - **前往 Gitee / GitHub 下载**——打开对应发行版页（版本判断只用 Gitee）
 * - **忽略此版本**——记进后端，这一版不再弹；发了更**新**的版本照样弹
 * - **取消 / 关掉**——什么都不记，下次打开页面还会弹；圆点**也不灭**，
 *   「关掉」≠「忽略」，只要还有没被忽略的新版就一直挂着
 */
export async function maybePromptUpdate() {
  let status;
  try {
    status = await api.get("/api/system/update");
  } catch {
    // 检测接口本身挂了（本地服务问题）不该再弹一个错框
    return;
  }
  if (!status) return;
  applyUpdateStatus(status);
  if (status.update_done) {
    // 更新完了：刷一下页面就回新版本，**不关标签页**（见 showUpdateDone）
    showUpdateDone(status.update_done);
    return;
  }
  if (!status.should_prompt) return;
  // 弹窗马上要展示这一版了，后台 watcher 别再 toast 一次
  markNotified(status.latest);
  showUpdateModal(status);
}

/** 记下「这版已经提醒过了」，同版本不再 toast */
function markNotified(version) {
  if (version) notifiedVersion = String(version);
}

/**
 * 后台轻提示：每隔一阵读一次检测结果，发现**没提醒过**的新版就 toast 一下。
 *
 * 跟首屏弹窗的分工：弹窗只在打开页面时出现一次；挂机靠这里。**绝不弹窗**，
 * 也**不重复 toast 同一版**。忽略水位线由 ``should_prompt`` 天然压住——被
 * 忽略的版本连 toast 都不会有。
 *
 * 每次拿到状态都会广播 ``boss:update``，系统设置的「版本更新」页签跟着刷。
 */
export function startUpdateWatcher(options) {
  const intervalMs = (options && options.intervalMs) || WATCHER_POLL_MS;
  const poll = async () => {
    let status;
    try {
      status = await api.get("/api/system/update");
    } catch {
      return;
    }
    if (!status) return;
    applyUpdateStatus(status);
    if (status.should_prompt && status.latest && status.latest !== notifiedVersion) {
      markNotified(status.latest);
      toast(`发现新版本 v${status.latest}，到「系统设置 → 版本更新」看看`, "ok", 6000);
    }
  };
  setInterval(poll, Math.max(5000, intervalMs));
  // 立刻对一次，免得刚打开页面要干等一整轮
  poll();
}

/** 系统设置里「打开更新窗口 / 立即更新」用的入口，复用同一套弹窗 */
export function openUpdateDialog(status) {
  if (!status) return;
  markNotified(status.latest);
  showUpdateModal(status);
}

function showUpdateModal(status) {
  const current = escapeHtml(status.current || "—");
  const latest = escapeHtml(status.latest || "—");
  const notes = String(status.notes || "").trim();
  const notesHtml = notes
    ? `<details class="acc mb-16" open><summary>更新说明</summary><div class="acc-body update-notes">${escapeHtml(notes)}</div></details>`
    : `<div class="muted mb-16">这版没附更新说明。</div>`;
  // 源码跑 / 压缩包预览跑没法自动换文件，只留手动下载入口
  const canApply = Boolean(status.can_self_update && status.zip_asset && status.zip_asset.url);
  const applyBtn = canApply
    ? `<button class="btn primary" type="button" data-act="apply">立即更新</button>`
    : "";

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
      <div class="update-progress" hidden>
        <div class="mb-8" data-role="stage-text">正在准备…</div>
        <div class="progress"><i data-role="bar" style="width:0%"></i></div>
      </div>
      <div class="btn-row">
        ${applyBtn}
        <button class="btn" type="button" data-act="gitee">前往 Gitee 下载</button>
        <button class="btn" type="button" data-act="github">前往 GitHub 下载</button>
        <button class="btn ghost" type="button" data-act="ignore">忽略此版本</button>
        <button class="btn ghost" type="button" data-act="cancel">取消</button>
      </div>
    `,
    onClose() {
      // 取消 / Esc / 点外面 = 下次仍提示，什么都不写
    },
  });

  const progressEl = box.bodyEl.querySelector(".update-progress");
  const stageEl = box.bodyEl.querySelector('[data-role="stage-text"]');
  const barEl = box.bodyEl.querySelector('[data-role="bar"]');
  let applying = false;
  let stopped = false;

  const setStage = (text, percent, kind) => {
    if (stageEl) stageEl.textContent = text;
    if (barEl && barEl.parentElement) {
      barEl.style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
      barEl.parentElement.classList.toggle("ok", kind === "ok");
      barEl.parentElement.classList.toggle("bad", kind === "bad");
    }
  };
  const lockButtons = (locked) => {
    box.bodyEl.querySelectorAll("button[data-act]").forEach((b) => {
      b.disabled = locked;
    });
    const closeBtn = box.box.querySelector(".modal-close");
    if (closeBtn) closeBtn.disabled = locked;
  };

  box.bodyEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn || btn.disabled) return;
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
        // 忽略成功 = 这一版不再提醒，圆点**立刻**熄掉（别等下一轮轮询才灭）
        applyUpdateStatus({ ...status, should_prompt: false, ignored: status.latest });
        toast(`已忽略 v${status.latest}`, "ok");
        box.close();
      } catch (err) {
        btn.disabled = false;
        toast(err.message || "记不住这个选择，请重试", "bad");
      }
      return;
    }
    if (act === "apply" && !applying) {
      applying = true;
      lockButtons(true);
      if (progressEl) progressEl.hidden = false;
      setStage("正在启动更新…", 2, null);

      // 一开跑就盯着 update_done：swap 期 WS 必断，进度接不上但完成回执能接上
      watchForNewService();

      // 先挂 WS 再发 apply：下载一开跑就有百分比，不会错过开头
      const socket = await connectProgressSocket({
        onEvent: (ev) => {
          if (!ev || stopped) return;
          if (ev.stage === "error") {
            stopped = true;
            applying = false;
            setStage(ev.text || "更新失败", 100, "bad");
            toast(ev.text || "更新失败", "bad", 6000);
            lockButtons(false);
            return;
          }
          if (ev.stage === "stopping" || ev.stage === "swapping" || ev.stage === "done") {
            setStage("正在替换并重启，请不要关闭电脑…", ev.percent ?? 90, null);
            return;
          }
          setStage(ev.text || "正在处理…", ev.percent ?? 0, null);
        },
        onDown: () => {
          // swap 期断开是预期的，只换一句提示；完成靠轮询接手
          if (applying && !stopped) {
            setStage("正在替换并重启，请不要关闭电脑…", 90, null);
          }
        },
      });

      try {
        await api.post("/api/system/update/apply", { version: status.latest || "" });
      } catch (err) {
        stopped = true;
        applying = false;
        if (socket) {
          try { socket.close(); } catch { /* 已经断了 */ }
        }
        lockButtons(false);
        setStage("", 0, null);
        if (progressEl) progressEl.hidden = true;
        toast(err.message || "启动更新失败，请重试", "bad");
      }
    }
  });

  /** 轮询同 host:port，等新进程回 `update_done`，然后把这个标签页刷到新版本上 */
  async function watchForNewService() {
    const deadline = Date.now() + WAIT_LIMIT_MS;
    while (!stopped && applying && Date.now() < deadline) {
      let done = null;
      try {
        const s = await api.get("/api/system/update");
        done = s && s.update_done;
      } catch {
        // 新服务还没起来（或刚好在换文件），继续等
      }
      if (done) {
        applying = false;
        stopped = true;
        try { box.close(); } catch { /* 已经被关了 */ }
        // 只刷新，不关标签页
        showUpdateDone(done);
        return;
      }
      await sleep(RECONNECT_MS);
    }
    if (!stopped && applying) {
      applying = false;
      setStage(
        "等了很久也没等到新服务起来。请到「系统设置 → 版本更新」看更新日志，或把诊断信息复制给开发者。",
        100,
        "bad",
      );
      lockButtons(false);
    }
  }
}

/**
 * 更新完成：**只刷新页面**，绝不关标签页。
 *
 * 这里踩过三个坑，改回去之前先看清楚：
 *
 * - **绝不 ``window.close()``**。本应用的标签页是用户自己开出来的、没有历史记录
 *   （``history.length === 1``），Chrome / Edge / Firefox 对这种情况**照关不误**
 *   ——``try/catch`` 也兜不住（被拦时只打一条 console 警告，不抛异常）。旧版本
 *   一句 ``window.close()`` 把页面直接干掉，而重启的 exe 带 ``--no-browser``，
 *   不会自己再开一个，用户手上就一个页面都不剩了。刷新就没有这个问题。
 * - **刷之前必须记住「这页处理过」**。回执在服务端是**进程内缓存**的
 *   （见 ``services/updater.py`` 的 ``consume_done_marker``），同一个进程会一直回同一份；
 *   不记标记就会「看到回执 → 刷新 → 又看到回执 → 再刷新」无限刷。
 *   标记跟着标签页活过刷新，所以每个标签页最多刷一次。
 * - **每个标签页都刷**（不只是发起更新的那个）。更新后重新打开的页面虽然本来就是
 *   新服务发的，但这里不做区分：拿到回执就统一刷一遍，保证手上这页一定不跑旧代码。
 */
function showUpdateDone(done) {
  const version = String((done && (done.version || done.new_version)) || "").replace(/^v/i, "");
  const key = version || "unknown";
  if (doneAlreadyShown(key)) return;
  // 标记存不进去（浏览器禁了存储）就当刷不动：宁可让用户手动按 F5，
  // 也不能刷不完。所以「能不能刷」跟着「标没标上」走。
  const canReload = rememberDone(key);

  const box = modal({
    title: "更新完成",
    body: `
      <div class="mb-16">
        ${version ? `<div class="mb-8">已更新到 <strong>v${escapeHtml(version)}</strong>。</div>` : ""}
        <div class="muted">${
          canReload
            ? "页面正在重新加载，马上回到新版本。要是没动静，点下面的按钮或自己按 F5。"
            : "请手动按 F5 刷新页面，回到新版本。"
        }</div>
      </div>
      <div class="btn-row">
        <button class="btn primary" type="button" data-act="reload">立即刷新</button>
      </div>
    `,
    onClose() {},
  });

  const reloadNow = () => {
    try {
      location.reload();
    } catch {
      /* 刷不动就把页面留着，上面那句「手动按 F5」兜底 */
    }
  };
  box.bodyEl.addEventListener("click", (e) => {
    if (e.target.closest('[data-act="reload"]')) reloadNow();
  });

  if (canReload) setTimeout(reloadNow, RELOAD_DELAY_MS);
}

/** 这个标签页是不是已经为这一版弹过「更新完成」了（sessionStorage 活着跨过刷新，见 showUpdateDone） */
function doneAlreadyShown(key) {
  try {
    return sessionStorage.getItem(DONE_SEEN_KEY) === key;
  } catch {
    return false;
  }
}

/** 记下「这版处理过了」。存不进去回 ``false``——调用方据此放弃自动刷新 */
function rememberDone(key) {
  try {
    sessionStorage.setItem(DONE_SEEN_KEY, key);
    return true;
  } catch {
    return false;
  }
}

function connectProgressSocket({ onEvent, onDown }) {
  return new Promise((resolve) => {
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/system/update/ws`;
    let settled = false;
    let socket;
    try {
      socket = new WebSocket(url);
    } catch {
      onDown && onDown();
      resolve(null);
      return;
    }
    const fail = () => {
      if (!settled) {
        settled = true;
        resolve(null);
      }
      onDown && onDown();
    };
    socket.onopen = () => {
      if (!settled) {
        settled = true;
        resolve(socket);
      }
    };
    socket.onmessage = (msg) => {
      let ev = null;
      try {
        ev = JSON.parse(msg.data);
      } catch {
        return;
      }
      onEvent && onEvent(ev);
    };
    socket.onerror = () => fail();
    socket.onclose = () => fail();
    // 3 秒连不上就别吊着按钮了，让上层走轮询兜底
    setTimeout(() => {
      if (!settled) {
        settled = true;
        try { socket.close(); } catch { /* noop */ }
        resolve(null);
        onDown && onDown();
      }
    }, 3000);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
