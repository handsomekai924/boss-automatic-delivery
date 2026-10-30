/** fetch 封装：统一错误 → 可读 message */

/**
 * 发请求并把失败收成可读错误对象抛出。
 *
 * @param {string}          method HTTP 方法
 * @param {string}          url    路径
 * @param {object|FormData} [body] JSON 对象或 FormData
 * @param {object}          [opts] 额外 fetch init 项
 * @returns {Promise<*>} 响应 JSON；网络不通或非 2xx 时抛 `{code, message, …}`
 */
async function request(method, url, body, opts = {}) {
  const init = {
    method,
    headers: {},
    ...opts,
  };
  if (body !== undefined && body !== null && !(body instanceof FormData)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  } else if (body instanceof FormData) {
    init.body = body;
  }
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (err) {
    console.error("请求发不出去", method, url, err);
    throw {
      code: "network",
      message:
        "连不上本地程序。请确认那个黑色窗口还开着（关掉它就等于关掉了程序），然后刷新本页重试。",
    };
  }
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!resp.ok) {
    const msg = (data && (data.message || (data.detail && data.detail.message))) || `HTTP ${resp.status}`;
    throw { code: (data && data.code) || "error", message: msg, status: resp.status, data };
  }
  return data;
}

/** 常用 HTTP 方法的快捷封装。 */
export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body),
  put: (url, body) => request("PUT", url, body),
  patch: (url, body) => request("PATCH", url, body),
  del: (url) => request("DELETE", url),
  upload: (url, formData) => request("POST", url, formData),
};
