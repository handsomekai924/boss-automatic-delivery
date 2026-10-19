/** 通用 UI 工具：toast / modal / 主题 / 长任务面板 */

/* ---------- 主题 ---------- */

export function initTheme() {
  const saved = localStorage.getItem("orbit-theme");
  const theme = saved === "light" || saved === "dark"
    ? saved
    : (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", theme);
  return theme;
}

export function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

export function toggleTheme() {
  const next = currentTheme() === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("orbit-theme", next); } catch { /* private mode */ }
  return next;
}

export function bindThemeToggle(btn) {
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = toggleTheme();
    btn.setAttribute("aria-label", next === "light" ? "切换到黑暗模式" : "切换到光明模式");
  });
}

/* ---------- 基础 ---------- */

export function toast(message, kind = "ok", ms = 3200) {
  const root = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 220);
  }, ms);
}

export function modal({ title, body, wide = false, onClose }) {
  const root = document.getElementById("modal-root");
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.setAttribute("role", "dialog");
  back.setAttribute("aria-modal", "true");
  const box = document.createElement("div");
  box.className = "modal" + (wide ? " wide" : "");
  box.innerHTML = `
    <div class="modal-head">
      <h3>${title || ""}</h3>
      <button class="modal-close" aria-label="关闭">×</button>
    </div>
    <div class="modal-body"></div>
  `;
  const bodyEl = box.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body;
  else if (body) bodyEl.appendChild(body);

  const close = () => {
    back.remove();
    document.removeEventListener("keydown", onKey);
    if (onClose) onClose();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  box.querySelector(".modal-close").addEventListener("click", close);
  back.addEventListener("click", (e) => {
    if (e.target === back) close();
  });
  back.appendChild(box);
  root.appendChild(back);
  box.querySelector(".modal-close").focus();
  return { close, bodyEl, box };
}

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function fmtTime(ts) {
  if (!ts) return "—";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function fmtClock(ts) {
  if (!ts) return "--:--:--";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

/** 秒数 → 「3 分 12 秒」/ 「48 秒」；负数与 NaN → "…" */
export function fmtEta(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "…";
  const s = Math.round(sec);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`;
  const h = Math.floor(m / 60);
  return `${h} 小时 ${m % 60} 分`;
}

export function scoreClass(score) {
  if (score >= 75) return "";
  if (score >= 50) return "mid";
  return "low";
}

export function scoreRing(score, label = "") {
  const s = Math.max(0, Math.min(100, Number(score) || 0));
  return `<div class="score-ring ${scoreClass(s)}" style="--p:${s}">${s}${label ? `<small>${label}</small>` : ""}</div>`;
}

export function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function debounce(fn, ms = 300) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

/* ---------- 弹层通用（多选下拉 / 级联共用） ---------- */

//: 弹层挂 body + fixed 定位：采集配置那栏是 overflow 滚动容器，贴在字段上会被裁掉。
//: 同一时刻只开一个，全局只挂一份监听——组件再多个也不会漏，页面切换也不用手工拆。
let openPop = null;

function closeOpenPop() {
  if (!openPop) return;
  openPop.close();
  openPop = null;
}

document.addEventListener("click", (e) => {
  if (!openPop || openPop.inside(e)) return;
  closeOpenPop();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeOpenPop();
});
window.addEventListener("resize", () => openPop && openPop.place());
document.addEventListener("scroll", () => openPop && openPop.place(), true);

/**
 * 把弹层挂到 body 并贴着 trigger 定位，自动翻边、超出右边界往左收。
 *
 * @param {HTMLElement} trigger 触发按钮
 * @param {HTMLElement} pop     弹层本体（初始 hidden）
 * @param {{minWidth?: number, onOpen?: () => void, onClose?: () => void}} [opts]
 */
function attachPop(trigger, pop, { minWidth = 200, onOpen, onClose } = {}) {
  function place() {
    const r = trigger.getBoundingClientRect();
    const h = Math.min(pop.scrollHeight || 280, 360);
    const w = Math.max(r.width, minWidth);
    let left = r.left;
    if (left + w > window.innerWidth - 8) left = Math.max(8, window.innerWidth - w - 8);
    pop.style.left = `${left}px`;
    pop.style.width = `${w}px`;
    // 默认向下；贴到窗口底部就翻上去，免得被截断
    const roomBelow = window.innerHeight - r.bottom;
    if (roomBelow < h + 12 && r.top > roomBelow) {
      pop.style.top = "auto";
      pop.style.bottom = `${window.innerHeight - r.top + 6}px`;
      pop.style.maxHeight = `${Math.max(200, r.top - 12)}px`;
    } else {
      pop.style.bottom = "auto";
      pop.style.top = `${r.bottom + 6}px`;
      pop.style.maxHeight = `${Math.max(200, roomBelow - 12)}px`;
    }
  }

  function open() {
    closeOpenPop();
    document.body.appendChild(pop);
    pop.hidden = false;
    if (onOpen) onOpen();
    place();
    trigger.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    // 点击后组件常会重绘（innerHTML 重写），e.target 已脱离文档，contains() 判不出来。
    // 事件路径是派发那一刻算好的，照样包含弹层，用它认「点在弹层里」。
    openPop = {
      close,
      place,
      inside(e) {
        const path = typeof e.composedPath === "function" ? e.composedPath() : [e.target];
        return path.includes(trigger) || path.includes(pop);
      },
    };
  }

  function close() {
    pop.hidden = true;
    pop.remove();
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    if (onClose) onClose();
    if (openPop && openPop.close === close) openPop = null;
  }

  return { open, close, place, isOpen: () => !pop.hidden };
}

/* ---------- 多选下拉 ---------- */

//: 原生 `<select multiple>` 是列表框，选项一直摊开占半屏。
//: 这里做成「点开才弹」的勾选面板，值仍然是 code 数组。
/**
 * 多选下拉。外观对齐 `.select`，点开才显示选项。
 *
 *   const ms = multiSelect({ onChange: (values) => … });
 *   host.replaceWith(ms.el);
 *   ms.setOptions([{ code: "1", name: "…", … }]);
 *   ms.setValues(["1"]);
 *   ms.getValues(); // ["1"]
 *
 * @param {object} o
 * @param {Array<{code: string, name?: string}>} [o.options]
 * @param {string[]} [o.values]
 * @param {string} [o.placeholder] 一个都没选时的文案
 * @param {(values: string[]) => void} [o.onChange] 用户勾选/清空时才回调，setValues 不触发
 */
export function multiSelect({ options = [], values = [], placeholder = "不限", onChange } = {}) {
  const root = el(`
    <div class="multi-select">
      <button type="button" class="select multi-select-trigger" aria-haspopup="listbox" aria-expanded="false">
        <span class="multi-select-label"></span>
      </button>
    </div>
  `);
  const pop = el(`
    <div class="multi-select-pop" role="listbox" aria-multiselectable="true" hidden>
      <div class="multi-select-list"></div>
      <div class="multi-select-foot">
        <button type="button" class="btn sm ghost multi-select-clear">清空</button>
      </div>
    </div>
  `);
  const trigger = root.querySelector(".multi-select-trigger");
  const labelEl = root.querySelector(".multi-select-label");
  const listEl = pop.querySelector(".multi-select-list");

  let opts = [];
  let selected = new Set();

  function paintLabel() {
    const names = opts.filter((o) => selected.has(o.code)).map((o) => o.name);
    if (!names.length) {
      labelEl.textContent = placeholder;
      labelEl.classList.add("is-placeholder");
      return;
    }
    labelEl.classList.remove("is-placeholder");
    labelEl.textContent = names.length <= 2 ? names.join("、") : `${names[0]} 等 ${names.length} 项`;
  }

  function paintList() {
    if (!opts.length) {
      listEl.innerHTML = `<div class="muted multi-select-empty">暂无选项</div>`;
      return;
    }
    listEl.innerHTML = opts
      .map(
        (o) => `
        <label class="multi-select-item">
          <input type="checkbox" value="${escapeHtml(o.code)}" ${selected.has(o.code) ? "checked" : ""}>
          <span>${escapeHtml(o.name)}</span>
        </label>`
      )
      .join("");
    listEl.querySelectorAll("input").forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) selected.add(input.value);
        else selected.delete(input.value);
        paintLabel();
        if (onChange) onChange(getValues());
      });
    });
  }

  function getValues() {
    return opts.filter((o) => selected.has(o.code)).map((o) => o.code);
  }

  function setOptions(next) {
    const keep = [...selected];
    opts = (next || []).map((o) => ({ code: String(o.code), name: o.name || String(o.code) }));
    selected = new Set(keep.filter((c) => opts.some((o) => o.code === c)));
    paintList();
    paintLabel();
  }

  function setValues(v) {
    selected = new Set((v || []).map(String));
    paintList();
    paintLabel();
  }

  const popApi = attachPop(trigger, pop);
  trigger.addEventListener("click", () => (popApi.isOpen() ? popApi.close() : popApi.open()));
  pop.querySelector(".multi-select-clear").addEventListener("click", () => {
    selected = new Set();
    paintList();
    paintLabel();
    if (onChange) onChange(getValues());
  });

  setOptions(options);
  setValues(values);

  return { el: root, getValues, setValues, setOptions, close: popApi.close };
}

/* ---------- 级联选择 ---------- */

function eachNode(nodes, fn) {
  for (const n of nodes || []) {
    fn(n);
    if (n.children?.length) eachNode(n.children, fn);
  }
}

function findNode(nodes, code) {
  let hit = null;
  eachNode(nodes, (n) => {
    if (!hit && n.code === code) hit = n;
  });
  return hit;
}

/** 到某个 code 的祖先链（不含自己）+ 自己；找不到返回 [] */
function pathToNode(nodes, code) {
  const chain = [];
  function go(list) {
    for (const n of list) {
      chain.push(n);
      if (n.code === code) return true;
      if (n.children?.length && go(n.children)) return true;
      chain.pop();
    }
    return false;
  }
  return go(nodes) ? chain : [];
}

/**
 * 级联选择：每列只渲染当前路径的一层，几千个城市节点也不卡。
 *
 * 单选（城市）：点任意节点即取值；有子级的同时展开下一级。点「确定」或点外面才收起。
 * 多选（行业）：父级只导航，点叶子勾选/取消，勾完点「确定」。
 *
 *   const c = cascader({ nodes: [{ code, name, children }], onChange: (v) => … });
 *   host.replaceWith(c.el);
 *   c.setNodes(nodes); c.setValue("101280100"); c.getValue();
 *
 * @param {object} o
 * @param {Array<{code: string, name: string, children?: array}>} [o.nodes] 顶层节点
 * @param {boolean} [o.multiple] 多选模式（叶子才可勾）
 * @param {string|string[]} [o.value]
 * @param {string} [o.placeholder]
 * @param {(value: string|string[]) => void} [o.onChange] 点选/清空时才回调，setValue 不触发
 */
export function cascader({ nodes = [], value, multiple = false, placeholder = "不限", onChange } = {}) {
  const root = el(`
    <div class="cascader-field">
      <button type="button" class="cascader-trigger" aria-haspopup="listbox" aria-expanded="false">
        <span class="cascader-path"></span>
        <span class="cascader-caret">▾</span>
      </button>
    </div>
  `);
  const pop = el(`
    <div class="cascader-pop" hidden>
      <div class="cascader-cols"></div>
      <div class="cascader-foot">
        <button type="button" class="btn sm ghost cascader-clear">清空</button>
        <button type="button" class="btn sm primary cascader-ok">确定</button>
      </div>
    </div>
  `);
  const trigger = root.querySelector(".cascader-trigger");
  const labelEl = root.querySelector(".cascader-path");
  const colsEl = pop.querySelector(".cascader-cols");

  let tree = [];
  /** 已展开的节点链：drill[i] 的 children 是第 i+1 列 */
  let drill = [];
  /** 多选：已勾的 code 集合；单选：pickedCode */
  let picked = new Set();
  let pickedCode = "";

  function visibleColumns() {
    const cols = [tree];
    for (const node of drill) {
      if (!node.children?.length) break;
      cols.push(node.children);
    }
    return cols;
  }

  function paintLabel() {
    if (multiple) {
      const names = [];
      eachNode(tree, (n) => {
        if (picked.has(n.code)) names.push(n.name);
      });
      if (!names.length) {
        labelEl.textContent = placeholder;
        labelEl.classList.add("is-placeholder");
      } else {
        labelEl.classList.remove("is-placeholder");
        labelEl.textContent = names.length <= 2 ? names.join("、") : `${names[0]} 等 ${names.length} 项`;
      }
      return;
    }
    const hit = pickedCode ? findNode(tree, pickedCode) : null;
    if (!hit && !pickedCode) {
      labelEl.textContent = placeholder;
      labelEl.classList.add("is-placeholder");
      return;
    }
    labelEl.classList.remove("is-placeholder");
    // 找不到就先把 code 亮出来，别让已存的值看起来像没配
    labelEl.textContent = hit ? hit.name : pickedCode;
  }

  /**
   * @param {number} [resetFrom] 该列起（含）滚动重置为 0——换父级时子级要回顶部；
   *   不传 = 全部保留（勾选/清空不打断浏览位置）。
   */
  function paintCols(resetFrom = Infinity) {
    const cols = visibleColumns();
    colsEl.style.setProperty("--cascader-n", String(Math.max(cols.length, 1)));
    const scrolls = [...colsEl.querySelectorAll(".cascader-col")].map((c, i) =>
      i < resetFrom ? c.scrollTop : 0,
    );
    if (!tree.length) {
      colsEl.innerHTML = `<div class="cascader-empty">暂无选项</div>`;
      return;
    }
    colsEl.innerHTML = cols
      .map((list, i) => {
        const items = list
          .map((n) => {
            const hasKids = Boolean(n.children?.length);
            const isActive = drill[i]?.code === n.code;
            const isPicked = multiple ? picked.has(n.code) : pickedCode === n.code;
            const cls = `cascader-item${isActive ? " active" : ""}${isPicked ? " picked" : ""}`;
            const code = escapeHtml(n.code);
            const name = escapeHtml(n.name);
            // 多选：只有叶子能勾，父级点开下一级；单选：任意节点都能取值
            if (multiple && !hasKids) {
              return `<label class="${cls}" data-code="${code}">
                <input type="checkbox" data-code="${code}" ${isPicked ? "checked" : ""}>
                <span class="cascader-name">${name}</span>
              </label>`;
            }
            return `<button type="button" class="${cls}" data-code="${code}">
              <span class="cascader-name">${name}</span>
              ${hasKids ? '<span class="caret">›</span>' : ""}
            </button>`;
          })
          .join("");
        return `<div class="cascader-col">${items}</div>`;
      })
      .join("");

    colsEl.querySelectorAll("button.cascader-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const code = btn.dataset.code;
        const node = findNode(tree, code);
        if (!node) return;
        const colIndex = [...colsEl.children].indexOf(btn.closest(".cascader-col"));
        const prevKey = drill.map((d) => d.code).join("/");
        drill = drill.slice(0, colIndex);
        if (node.children?.length) drill.push(node);
        const resetFrom = drill.map((d) => d.code).join("/") === prevKey ? Infinity : colIndex + 1;
        if (multiple) {
          // 多选的父级只负责展开
          paintCols(resetFrom);
          return;
        }
        pickedCode = node.code;
        paintCols(resetFrom);
        paintLabel();
        if (onChange) onChange(pickedCode);
        // 选完不关窗：还能继续改主意 / 往下点别的路径，点「确定」或点外面才收
      });
    });
    colsEl.querySelectorAll("input[data-code]").forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) picked.add(input.dataset.code);
        else picked.delete(input.dataset.code);
        paintCols();
        paintLabel();
        if (onChange) onChange(getValue());
      });
    });

    colsEl.querySelectorAll(".cascader-col").forEach((col, i) => {
      col.scrollTop = scrolls[i] ?? 0;
    });
  }

  function getValue() {
    if (!multiple) return pickedCode || "";
    const out = [];
    eachNode(tree, (n) => {
      if (picked.has(n.code)) out.push(n.code);
    });
    return out;
  }

  function setValue(v) {
    if (multiple) picked = new Set((v || []).map(String));
    else pickedCode = v ? String(v) : "";
    paintCols();
    paintLabel();
  }

  function setNodes(next) {
    tree = normalizeNodes(next || []);
    drill = [];
    paintCols(0);
    paintLabel();
  }

  /** code 统一成字符串，子级递归；调用方给嵌套 DTO 也能用 */
  function normalizeNodes(list) {
    return (list || []).map((n) => {
      const node = { code: String(n.code), name: n.name || String(n.code) };
      if (n.children?.length) node.children = normalizeNodes(n.children);
      return node;
    });
  }

  const popApi = attachPop(trigger, pop, {
    minWidth: 320,
    // 打开时把路径还原到当前值那一层（含自己，好让它的子级也亮出来），不用每次从头点
    onOpen() {
      const target = multiple ? getValue()[0] : pickedCode;
      drill = target ? pathToNode(tree, target) : [];
      paintCols();
    },
  });
  trigger.addEventListener("click", () => (popApi.isOpen() ? popApi.close() : popApi.open()));
  pop.querySelector(".cascader-clear").addEventListener("click", () => {
    picked = new Set();
    pickedCode = "";
    paintCols();
    paintLabel();
    if (onChange) onChange(getValue());
  });
  pop.querySelector(".cascader-ok").addEventListener("click", () => popApi.close());

  setNodes(nodes);
  setValue(value ?? (multiple ? [] : ""));

  return { el: root, getValue, setValue, setNodes, close: popApi.close };
}

/* ---------- 长任务状态 ---------- */

export const TASK_STATUS = {
  idle:    { label: "待命",   cls: "" },
  running: { label: "进行中", cls: "is-run" },
  done:    { label: "已完成", cls: "is-ok" },
  ok:      { label: "已完成", cls: "is-ok" },
  error:   { label: "失败",   cls: "is-err" },
  cancelled: { label: "已停止", cls: "is-stop" },
  pending: { label: "排队中", cls: "is-run" },
};

/** 后端 status 字符串 → TASK_STATUS 的 key */
export function taskStateOf(status) {
  const s = String(status || "").toLowerCase();
  if (["running", "pending", "sending_sms", "sending", "need_code", "need_slider", "logging_in", "working", "process"].includes(s)) return "running";
  if (["done", "ok", "success", "finished"].includes(s)) return "done";
  if (["error", "failed", "fail"].includes(s)) return "error";
  if (["cancelled", "canceled", "stopped", "cancel"].includes(s)) return "cancelled";
  if (["idle", "none", ""].includes(s)) return "idle";
  return "running";
}

/**
 * 统一长任务面板。所有超过 2 秒的操作都用它，保证进度可视化一致。
 *
 *   const task = taskPanel({ title: "批量匹配", stopLabel: "停止" });
 *   host.appendChild(task.el);
 *   task.update({ status: "running", percent: 40, current: "…", counts: [["完成", "12 / 30"]], eta: 88 });
 */
export function taskPanel({ title = "任务", stopLabel = "停止", logTitle = "" } = {}) {
  const root = el(`
    <div class="task" role="status" aria-live="polite">
      <div class="task-head">
        <div class="task-title">${escapeHtml(title)}</div>
        <div class="task-actions">
          <span class="task-status">待命</span>
          <button type="button" class="btn sm danger hidden">${escapeHtml(stopLabel)}</button>
        </div>
      </div>
      <div class="progress" aria-hidden="true"><i style="width:0%"></i></div>
      <div class="task-meta"></div>
      <div class="task-log hidden"></div>
    </div>
  `);

  const statusEl = root.querySelector(".task-status");
  const stopBtn = root.querySelector(".btn");
  const barEl = root.querySelector(".progress");
  const fillEl = root.querySelector(".progress > i");
  const metaEl = root.querySelector(".task-meta");
  const logEl = root.querySelector(".task-log");

  let stopHandler = null;
  stopBtn.addEventListener("click", () => {
    if (stopHandler) stopHandler();
  });

  function setLog(rows) {
    if (!rows || !rows.length) {
      logEl.classList.add("hidden");
      logEl.innerHTML = "";
      return;
    }
    logEl.classList.remove("hidden");
    if (logTitle) {
      logEl.innerHTML = `<div class="ev muted">${escapeHtml(logTitle)}</div>` + rows;
    } else {
      logEl.innerHTML = rows;
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  return {
    el: root,
    onStop(fn) {
      stopHandler = fn;
      return this;
    },
    /**
     * @param {object} p
     * @param {string} p.status  后端状态或 running/done/error/cancelled/idle
     * @param {number} [p.percent] 0-100；缺省且 running 时用 indeterminate
     * @param {string} [p.current] 当前项文案
     * @param {Array<[string,string]>} [p.counts] 计数对
     * @param {string|number} [p.eta] 预估剩余（已格式化或秒）
     * @param {string} [p.error] 错误信息
     * @param {string} [p.log] 事件流水 HTML
     */
    update({ status = "idle", percent, current, counts, eta, error, log } = {}) {
      const key = taskStateOf(status);
      const meta = TASK_STATUS[key] || TASK_STATUS.idle;
      statusEl.textContent = meta.label;
      statusEl.className = `task-status ${meta.cls}`;

      const showStop = key === "running" || key === "pending";
      stopBtn.classList.toggle("hidden", !showStop);

      if (key === "running" && (percent == null || !Number.isFinite(Number(percent)))) {
        fillEl.classList.add("indeterminate");
        fillEl.style.width = "36%";
        barEl.classList.remove("ok", "bad");
      } else {
        fillEl.classList.remove("indeterminate");
        const p = Math.max(0, Math.min(100, Number(percent) || 0));
        fillEl.style.width = `${p}%`;
        barEl.classList.toggle("ok", key === "done");
        barEl.classList.toggle("bad", key === "error");
      }

      const bits = [];
      if (current) {
        bits.push(`<span class="tm"><span class="tm-k">当前</span><span class="tm-v accent">${escapeHtml(current)}</span></span>`);
      }
      for (const [k, v] of counts || []) {
        const cls = /失败|错误|error|bad/i.test(String(k)) ? "bad" :
          (/跳过|skip|warn/i.test(String(k)) ? "warn" :
            (/成功|完成|ok|done/i.test(String(k)) ? "ok" : ""));
        bits.push(`<span class="tm"><span class="tm-k">${escapeHtml(k)}</span><span class="tm-v ${cls}">${escapeHtml(String(v))}</span></span>`);
      }
      if (eta != null && eta !== "") {
        const v = typeof eta === "number" || /^\d+$/.test(String(eta)) ? fmtEta(Number(eta)) : String(eta);
        bits.push(`<span class="tm"><span class="tm-k">剩余</span><span class="tm-v">${escapeHtml(v)}</span></span>`);
      }
      if (error) {
        bits.push(`<span class="tm"><span class="tm-k">错误</span><span class="tm-v bad">${escapeHtml(error)}</span></span>`);
      }
      metaEl.innerHTML = bits.join("");

      if (log != null) setLog(log);
      return this;
    },
  };
}

/** 事件列表 → task-log 的 HTML 行（从两种事件格式提取关键信息） */
export function renderEvents(events, { limit = 30, nameKey = "name" } = {}) {
  return (events || [])
    .slice(-limit)
    .map((ev) => {
      const at = ev.at || ev.ts || ev.time;
      const name = ev[nameKey] || ev.event || ev.type || "event";
      // 两种格式：match/deliver 是 {name, payload:{...}}，crawl/desc 是平铺字段
      const flat = { ...ev };
      if (ev.payload && typeof ev.payload === "object") Object.assign(flat, ev.payload);
      const kind = /error|fail|bad|stopped/i.test(String(name))
        ? "bad"
        : /ok|done|finish|success|sent/i.test(String(name))
          ? "ok"
          : /skip|cancel/i.test(String(name))
            ? "warn"
            : "";
      const label = formatEventLabel(name, flat);
      return `<div class="ev"><time>${escapeHtml(fmtClock(at))}</time><span class="name ${kind}">${escapeHtml(label)}</span></div>`;
    })
    .join("");
}

/** 把事件名 + 字段拼成人看得懂的一行 */
function formatEventLabel(name, f) {
  const n = String(name);
  const job = f.job_name || "";
  const brand = f.brand || f.brand_name || "";
  const who = job ? (brand ? `${job} @ ${brand}` : job) : "";

  // 抓取：翻页（没有 event 字段，靠 page 计数）
  if (f.page != null && (f.inserted != null || f.kept_count != null)) {
    return `第 ${f.page} 页 · 原始 ${f.raw_count ?? "?"} · 入库 +${f.inserted ?? 0} · 更新 ${f.updated ?? 0}`;
  }
  // 抓取：JD 补抓
  if (n === "detail_done") return `✓ ${who}${f.has_desc ? "" : "（无描述）"}`;
  if (n === "detail_error") return `✗ ${who} — ${f.message || "拉取失败"}`;
  if (n === "detail_skipped") return `– ${who}（${f.reason || "已有描述"}）`;
  if (n === "detail_stopped") return `⛔ ${f.reason || "已停"}`;
  // 抓取：生命周期
  if (n === "stoken") return f.message || "正在准备搜索令牌…";
  if (n === "finished") {
    // crawl 有 stopped_reason，deliver/match 有 sent/failed/skipped 或 analysis_id
    if (f.stopped_reason) return `抓取结束（${f.stopped_reason}）`;
    if (f.sent != null || f.failed != null || f.skipped != null)
      return `完成 · 成功 ${f.sent ?? f.ok ?? 0} / 失败 ${f.failed ?? 0} / 跳过 ${f.skipped ?? 0}`;
    return "完成";
  }
  // 补抓 JD
  if (n === "item_done") return `${f.has_desc ? "✓" : "○"} ${who}`;
  if (n === "item_error") return `✗ ${who} — ${f.message || "拉取失败"}`;
  if (n === "item_skipped") return `– ${who}（${f.reason || "已有描述"}）`;
  if (n === "start") return `开始补抓 · 共 ${f.total ?? "?"} 条`;
  if (n === "stopped") return `⛔ ${f.reason || "已停"}`;
  // 匹配
  if (n === "job_start") return `→ 开始匹配 ${who}`;
  if (n === "job_done") return `✓ ${who}${f.score != null ? ` · 评分 ${f.score}` : ""}`;
  // 投递
  if (n === "send_start") return `→ 发送 ${who || f.encrypt_job_id || ""}`;
  if (n === "sent") return `✓ 已发送 ${who || f.encrypt_job_id || ""}`;
  if (n === "skipped") return `– 跳过 ${who || f.encrypt_job_id || ""}（${f.reason || "已发送"}）`;
  // 通用兜底：挑几个有意义的字段
  const bits = [who, f.message || f.reason || ""].filter(Boolean);
  return bits.length ? `${n} · ${bits.join(" · ")}` : n;
}
