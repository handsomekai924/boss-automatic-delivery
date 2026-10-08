/** fetch 封装：统一错误 → 可读 message */

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
    throw { code: "network", message: "网络不通或服务未启动：" + err };
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

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body),
  put: (url, body) => request("PUT", url, body),
  del: (url) => request("DELETE", url),
  upload: (url, formData) => request("POST", url, formData),
};
