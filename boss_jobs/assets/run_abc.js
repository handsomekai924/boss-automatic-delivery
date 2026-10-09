/*
 * 跑 security-js 的 ABC.z(seed, ts)，把 __zp_stoken__ 打到 stdout。
 *
 * 这是 BOSS 直聘安全网关令牌的**生成端**，与站点前端 app~2.1a6c0514.js 的
 * 写法一一对应：
 *
 *     token = new ABC().z(seed, parseInt(ts) + (480 + getTimezoneOffset()) * 60000)
 *
 * 用法:
 *     node run_abc.js <security.js> <seed> <ts> [jsTimezoneOffsetMinutes]
 *
 * stdout 只写 token 本身；诊断信息走 stderr。
 */
"use strict";

const fs = require("fs");

/**
 * 塞一个够用的浏览器外壳——ABC.z 会做环境指纹（canvas / plugins /
 * screen / localStorage 等），缺了它生成的 token 服务端不认。
 * canvas 的 getImageData 挡掉指纹读回，返回一张稳定的假图。
 */
function installDom() {
  const nav = {
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    appVersion:
      "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    platform: "Win32",
    language: "zh-CN",
    languages: ["zh-CN", "zh", "en"],
    webdriver: false,
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    cookieEnabled: true,
    onLine: true,
    vendor: "Google Inc.",
    vendorSub: "",
    productSub: "20030107",
    appCodeName: "Mozilla",
    appName: "Netscape",
    doNotTrack: null,
    pdfViewerEnabled: true,
    plugins: {
      length: 3,
      item: () => null,
      namedItem: () => null,
      refresh: () => {},
    },
    mimeTypes: {
      length: 4,
      item: () => null,
      namedItem: () => null,
    },
    webkitTemporaryStorage: {
      queryUsageAndQuota: (cb) => cb(0, 1024 * 1024 * 1024),
      requestQuota: (n, cb) => cb(n),
    },
    getBattery: () =>
      Promise.resolve({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 }),
  };

  const loc = {
    hostname: "www.zhipin.com",
    host: "www.zhipin.com",
    href: "https://www.zhipin.com/web/geek/jobs",
    origin: "https://www.zhipin.com",
    pathname: "/web/geek/jobs",
    protocol: "https:",
    search: "",
    hash: "",
    reload() {},
    replace() {},
    assign() {},
  };

  const canvasCtx = {
    canvas: { width: 300, height: 150 },
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    font: "14px Arial",
    textAlign: "start",
    textBaseline: "alphabetic",
    globalAlpha: 1,
    globalCompositeOperation: "source-over",
    fillRect() {},
    strokeRect() {},
    clearRect() {},
    fillText() {},
    strokeText() {},
    measureText(t) {
      return { width: 7.2 * String(t).length };
    },
    beginPath() {},
    closePath() {},
    moveTo() {},
    lineTo() {},
    bezierCurveTo() {},
    quadraticCurveTo() {},
    arc() {},
    arcTo() {},
    rect() {},
    fill() {},
    stroke() {},
    clip() {},
    translate() {},
    rotate() {},
    scale() {},
    save() {},
    restore() {},
    createLinearGradient() {
      return { addColorStop() {} };
    },
    createRadialGradient() {
      return { addColorStop() {} };
    },
    getImageData(x, y, w, h) {
      const data = new Uint8ClampedArray(Math.max(1, w * h * 4));
      for (let i = 0; i < data.length; i++) data[i] = (i * 37) % 256;
      return { data, width: w, height: h };
    },
    putImageData() {},
    drawImage() {},
  };

  const el = (tag) => ({
    style: {},
    tagName: (tag || "").toUpperCase(),
    width: tag === "canvas" ? 300 : 0,
    height: tag === "canvas" ? 150 : 0,
    setAttribute() {},
    getAttribute() {
      return null;
    },
    appendChild() {},
    removeChild() {},
    insertBefore() {},
    getContext(type) {
      return type === "2d" ? canvasCtx : null;
    },
    toDataURL() {
      return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAASwAAACWCAYAAABkW7XSAAAAEUlEQVR42u3BAQ0AAADCoPdPbQ8HFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwJcGJTcAAe0RzL0AAAAASUVORK5CYII=";
    },
    innerHTML: "",
    outerHTML: "",
    textContent: "",
    children: [],
    childNodes: [],
    nodeType: 1,
    parentNode: null,
    addEventListener() {},
    removeEventListener() {},
    getElementsByTagName() {
      return [];
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
  });

  const storage = (name) => {
    const data = new Map();
    return {
      getItem(k) {
        return data.has(k) ? data.get(k) : null;
      },
      setItem(k, v) {
        data.set(String(k), String(v));
      },
      removeItem(k) {
        data.delete(k);
      },
      clear() {
        data.clear();
      },
      key(i) {
        return Array.from(data.keys())[i] ?? null;
      },
      get length() {
        return data.size;
      },
      __name: name,
    };
  };

  const def = (key, value) => {
    try {
      Object.defineProperty(global, key, {
        configurable: true,
        writable: true,
        enumerable: true,
        value,
      });
    } catch (_) {
      try {
        global[key] = value;
      } catch (__) {
        /* 只读全局，放弃覆盖 */
      }
    }
  };

  def("window", global);
  def("self", global);
  def("top", global);
  def("parent", global);
  def("frames", global);
  def("navigator", nav);
  def("location", loc);
  global.document = {
    createElement: el,
    getElementsByTagName: (t) => [el("head")],
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    documentElement: el("html"),
    body: el("body"),
    head: el("head"),
    cookie: "",
    addEventListener() {},
    removeEventListener() {},
    createTextNode: (t) => ({ textContent: t }),
    createComment: () => ({}),
    referrer: "",
    title: "BOSS直聘",
    hidden: false,
    visibilityState: "visible",
    readyState: "complete",
    compatMode: "CSS1Compat",
    characterSet: "UTF-8",
    documentURI: "https://www.zhipin.com/web/geek/jobs",
    domain: "www.zhipin.com",
    location: loc,
    implementation: { hasFeature: () => true },
  };
  global.screen = {
    width: 1920,
    height: 1080,
    availWidth: 1920,
    availHeight: 1040,
    colorDepth: 24,
    pixelDepth: 24,
    orientation: { type: "landscape-primary", angle: 0 },
  };
  global.history = {
    length: 1,
    state: null,
    scrollRestoration: "auto",
    back() {},
    forward() {},
    go() {},
    pushState() {},
    replaceState() {},
  };
  global.performance = {
    now: () => Date.now(),
    timeOrigin: Date.now() - 1234,
    timing: {
      navigationStart: Date.now() - 1234,
      loadEventEnd: Date.now() - 200,
      domContentLoadedEventEnd: Date.now() - 400,
    },
    getEntriesByType: () => [],
    getEntriesByName: () => [],
  };
  global.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 16);
  global.cancelAnimationFrame = clearTimeout;
  global.getComputedStyle = () =>
    new Proxy(
      {},
      {
        get(t, p) {
          return p === "getPropertyValue" ? () => "" : "";
        },
      }
    );
  global.matchMedia = (q) => ({
    matches: false,
    media: q,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    onchange: null,
  });
  global.localStorage = storage("localStorage");
  global.sessionStorage = storage("sessionStorage");
  global.HTMLElement = class HTMLElement {};
  global.HTMLCanvasElement = class HTMLCanvasElement {};
  global.Element = class Element {};
  global.Node = class Node {};
  global.Event = class Event {
    constructor(t) {
      this.type = t;
    }
  };
  global.MessageEvent = class MessageEvent {};
  global.MutationObserver = class MutationObserver {
    observe() {}
    disconnect() {}
  };
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  global.IntersectionObserver = class IntersectionObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  global.XMLHttpRequest = class XMLHttpRequest {
    open() {}
    send() {}
    setRequestHeader() {}
    addEventListener() {}
  };
  global.fetch = () => Promise.reject(new Error("offline"));
  global.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
  global.outerWidth = 1920;
  global.outerHeight = 1080;
  global.innerWidth = 1920;
  global.innerHeight = 1080;
  global.devicePixelRatio = 1;
  global.screenX = 0;
  global.screenY = 0;
  global.pageXOffset = 0;
  global.pageYOffset = 0;
  global.scrollX = 0;
  global.scrollY = 0;
  global.closed = false;
  global.isSecureContext = true;
  global.crossOriginIsolated = false;
  try {
    Object.defineProperty(global, "crypto", {
      configurable: true,
      writable: true,
      value: {
        getRandomValues(arr) {
          for (let i = 0; i < arr.length; i++) arr[i] = (Math.random() * 256) | 0;
          return arr;
        },
        randomUUID: () => "00000000-0000-4000-8000-000000000000",
        subtle: {
          digest: async () => new Uint8Array(32),
        },
      },
    });
  } catch (_) {
    /* Node 自带 crypto 就够用 */
  }
  global.TextEncoder = require("util").TextEncoder;
  global.TextDecoder = require("util").TextDecoder;
  global.btoa = (s) => Buffer.from(String(s), "binary").toString("base64");
  global.atob = (s) => Buffer.from(String(s), "base64").toString("binary");
  global.URL = require("url").URL;
  global.URLSearchParams = require("url").URLSearchParams;
  global.Worker = class Worker {
    postMessage() {}
    terminate() {}
  };
  global.Blob = class Blob {};
  global.FormData = class FormData {};
  global.WebSocket = class WebSocket {};
  global.Notification = class Notification {};
  global.Plugin = class Plugin {};
  global.MimeType = class MimeType {};
  global.Audio = class Audio {};
  global.Image = class Image {};
  global.Option = class Option {};
}

/** 跑 security-js，把 token 打到 stdout。`getTimezoneOffset()` 是「UTC 比本地慢多少分钟」（北京 = -480） */
function main() {
  const [, , scriptPath, seed, tsStr, tzStr] = process.argv;
  if (!scriptPath || seed === undefined || tsStr === undefined) {
    process.stderr.write("usage: node run_abc.js <security.js> <seed> <ts> [tzOffsetMin]\n");
    process.exit(2);
  }
  installDom();
  const code = fs.readFileSync(scriptPath, "utf8");
  (0, eval)(code);

  const ABC = global.ABC;
  if (typeof ABC !== "function") {
    process.stderr.write("security-js 里没有 window.ABC\n");
    process.exit(3);
  }

  const ts = parseInt(tsStr, 10);
  if (!Number.isFinite(ts)) {
    process.stderr.write("ts 不是数字: " + tsStr + "\n");
    process.exit(2);
  }
  const tz =
    tzStr === undefined || tzStr === ""
      ? new Date().getTimezoneOffset()
      : parseInt(tzStr, 10);
  const adjusted = ts + (480 + tz) * 60000;

  const token = new ABC().z(seed, adjusted);
  if (!token) {
    process.stderr.write("ABC.z 返回空\n");
    process.exit(4);
  }
  process.stdout.write(String(token));
}

main();
