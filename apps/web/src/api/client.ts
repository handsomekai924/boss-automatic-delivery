/**
 * fetch 封装：统一错误 → 可读 message（延续 legacy `api.js` 的约定，升级成带类型的 TS）。
 *
 * 视图不拼 URL、不判 HTTP 状态码；抛出的永远是 `ApiError`，`message` 直接能给人看。
 */

/** 接口错误：`message` 是给用户看的话，不是堆栈。 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly data: unknown;

  constructor(message: string, code = "error", status = 0, data: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.data = data;
  }
}

/** 网络不通时的话术——本地程序关了是最常见的原因。 */
const NETWORK_MESSAGE =
  "连不上本地程序。请确认那个黑色窗口还开着（关掉它就等于关掉了程序），然后刷新本页重试。";

function pickMessage(data: unknown, status: number): string {
  if (data && typeof data === "object") {
    const d = data as Record<string, unknown>;
    if (typeof d.message === "string" && d.message) return d.message;
    const detail = d.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      const inner = (detail as Record<string, unknown>).message;
      if (typeof inner === "string" && inner) return inner;
      // FastAPI 参数校验 422：`detail` 是 `[{loc, msg, type}]`，取头一条的 msg，
      // 免得越界报错退化成一句「HTTP 422」，人看不出哪儿填错了
      if (Array.isArray(detail)) {
        for (const row of detail) {
          const msg = row && typeof row === "object" ? (row as Record<string, unknown>).msg : undefined;
          if (typeof msg === "string" && msg) return msg;
        }
      }
    }
  }
  return `HTTP ${status}`;
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined && body !== null) {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let resp: Response;
  try {
    resp = await fetch(url, init);
  } catch {
    throw new ApiError(NETWORK_MESSAGE, "network");
  }

  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!resp.ok) {
    const code =
      data && typeof data === "object" && typeof (data as Record<string, unknown>).code === "string"
        ? ((data as Record<string, unknown>).code as string)
        : "error";
    throw new ApiError(pickMessage(data, resp.status), code, resp.status, data);
  }
  return data as T;
}

/** 常用 HTTP 方法的快捷封装。 */
export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body),
  put: <T>(url: string, body?: unknown) => request<T>("PUT", url, body),
  patch: <T>(url: string, body?: unknown) => request<T>("PATCH", url, body),
  del: <T>(url: string) => request<T>("DELETE", url),
  upload: <T>(url: string, formData: FormData) => request<T>("POST", url, formData),
};

/**
 * 平台作用域路径：`/api/sites/{site}/…`。
 *
 * 共用接口（简历/匹配/面试题/任务/LLM/系统）**不要**走这里——它们不带站点语义，
 * 列表用 `?site=` 过滤。旧 URL（`/api/auth/status` 等）是 legacy 专用别名，新代码禁止用。
 */
export function sitePath(site: string, path: string): string {
  const tail = path.replace(/^\/+/, "");
  return `/api/sites/${encodeURIComponent(site)}/${tail}`;
}
