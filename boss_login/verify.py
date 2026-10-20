"""滑块验证协议层：拉挑战、把题交给本人解、组装票据。

不破解滑块；只做「挑战参数 → 官方极验组件 → 票据装回请求」。
协议细节见 `notes/boss_login/verify.md`。

Attributes:
    SliderSolver: 解题结果回调：`(挑战) -> 方案或 None`。
    EventHandler: 过程事件回调：`on_event(name, payload)`。
"""

from __future__ import annotations

import json
import logging
import random
import struct
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from . import config as C
from .errors import ApiError, BossLoginError, ValidationError

logger = logging.getLogger(__name__)

SliderSolver = Callable[["SliderChallenge"], "SliderSolution | None"]
EventHandler = Callable[[str, dict[str, Any]], None]

_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class SliderChallenge:
    """一次滑块挑战的全部参数。

    Attributes:
        captcha_type: 通道号，1=极验滑块。
        captcha_name: 通道展示名。
        gt: 极验的 gt（站点公钥）。
        challenge: 极验的一次性 challenge。
        rand_key: zppassport 族会带；zpsecureflow 族为空。
        scene: 场景码，回传时用于对账。
        start_captcha_raw: 原始 `startCaptcha` 字符串。
        raw: 原始响应字典。
    """

    captcha_type: int
    captcha_name: str
    gt: str
    challenge: str
    rand_key: str = ""
    scene: str = ""
    start_captcha_raw: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_geetest(self) -> bool:
        """是否为极验通道。"""
        return self.captcha_type == C.CAPTCHA_TYPE_JIYAN

    def require_geetest(self) -> "SliderChallenge":
        """确认是极验通道且参数完整，否则抛 `ValidationError`；返回自身便于链式调用。"""
        if not self.is_geetest:
            raise ValidationError(
                f"当前验证通道是 {self.captcha_name}（type={self.captcha_type}），"
                "本客户端只实现了极验滑块的人机协作链路",
                field="captcha_type",
            )
        if not self.gt or not self.challenge:
            raise ValidationError("挑战参数不完整：缺 gt / challenge", field="challenge")
        return self


@dataclass(frozen=True)
class SliderSolution:
    """人工拖完滑块后，极验组件给出的三个票据。

    Attributes:
        challenge: 一次性 challenge。
        validate: 求解结果票据。
        seccode: 附加校验码。
        rand_key: 随机钥匙，可选。
        captcha_type: 通道号。
    """

    challenge: str
    validate: str
    seccode: str
    rand_key: str = ""
    captcha_type: int = C.CAPTCHA_TYPE_JIYAN

    def as_headers(self, *, trace_id: str = "", zp_token: str = "") -> dict[str, str]:
        """按 captcha-sdk `onSuccess().headers` 的形状给出票据请求头。

        Args:
            trace_id: 可选 traceId。
            zp_token: 可选 `zp_token`。
        """
        headers = {
            C.CAPTCHA_HEADER_TYPE: str(self.captcha_type),
            C.CAPTCHA_HEADER_CHALLENGE: self.challenge,
            C.CAPTCHA_HEADER_VALIDATE: self.validate,
            C.CAPTCHA_HEADER_SECCODE: self.seccode,
        }
        if self.rand_key:
            headers[C.CAPTCHA_HEADER_RANDKEY] = self.rand_key
        if trace_id:
            headers["traceId"] = trace_id
        if zp_token:
            headers["zp_token"] = zp_token
        return headers

    def as_form_fields(self) -> dict[str, str]:
        """把票据铺成业务请求的表单字段（极验通道为 challenge / validate / seccode）。"""
        if self.captcha_type == C.CAPTCHA_TYPE_PICTURE:
            return {
                "captcha": self.validate,
                **({"randKey": self.rand_key} if self.rand_key else {}),
            }
        if self.captcha_type == C.CAPTCHA_TYPE_YIDUN:
            return {"validate": self.validate}
        return {
            "challenge": self.challenge,
            "validate": self.validate,
            "seccode": self.seccode,
        }


def _to_uint32(value: float | int) -> int:
    """JS 的 `>>> 0`；float 先按 ToInteger 向零截断。"""
    if isinstance(value, float):
        value = int(value)
    return value % (1 << 32)


def _js_mul_u32(multiplier: int, value: int) -> int:
    """JS 的 `multiplier * value >>> 0`，用 float64 相乘以对齐低位舍入。"""
    return _to_uint32(float(multiplier) * float(value))


def _mix_u32(value: int, mul1: int, shift1: int, mul2: int, shift2: int) -> int:
    """Murmur 风格收尾混合，对应 verify.html 的嵌套赋值。"""
    value = _js_mul_u32(mul1, value)
    value = (value ^ (value >> shift1)) & 0xFFFFFFFF
    value = _js_mul_u32(mul2, value)
    value = (value ^ (value >> shift2)) & 0xFFFFFFFF
    return value


def _i32(value: int) -> int:
    """截成 JS 的 int32（有符号）。"""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def _compute_checksum(seed: str) -> str:
    """3 位校验符，复刻 verify.html 的浏览器分支。"""
    t = 0
    for ch in seed:
        t = _i32((t << 5) - t + ord(ch))
    n = 0
    for i in range(len(seed) - 1, -1, -1):
        n = _i32((n << 7) - n + ord(seed[i]) * (i + 1))
    mid = len(seed) // 2
    o = 0
    for i, ch in enumerate(seed):
        o = _i32((o << 3) - o + ord(ch) * (abs(i - mid) + 1))

    a = abs(_i32(t ^ n))
    s = _CHARS[_mix_u32(a, 2654435761, 16, 2246822507, 13) % len(_CHARS)]

    f = abs(_i32(n ^ o))
    l = _CHARS[_mix_u32(f, 3266489909, 16, 2654435761, 13) % len(_CHARS)]

    d = abs(_i32(o ^ t))
    r = _CHARS[_mix_u32(d, 668265261, 16, 2246822507, 13) % len(_CHARS)]
    return s + l + r


def generate_trace_id(
    *,
    now: Callable[[], float] = time.time,
    rand: Callable[[], float] = random.random,
) -> str:
    """生成 `F-<13 位十六进制毫秒时间戳><6 位随机字符><3 位校验符>` 的 traceId。"""
    stamp = format(int(now() * 1000), "x").lower().rjust(13, "0")[-13:]
    suffix = "".join(_CHARS[int(rand() * len(_CHARS)) % len(_CHARS)] for _ in range(6))
    seed = stamp + suffix
    return f"F-{seed}{_compute_checksum(seed)}"


def parse_challenge(data: Mapping[str, Any], *, scene: str = "") -> SliderChallenge:
    """把 gettype / getTypeV2 的 `zpData` 拍成 `SliderChallenge`；非法 JSON 抛 `ValidationError`。"""
    start_raw = str(data.get("startCaptcha") or "")
    start: dict[str, Any] = {}
    if start_raw:
        try:
            parsed = json.loads(start_raw)
        except ValueError as exc:
            raise ValidationError(f"startCaptcha 不是合法 JSON：{exc}", field="startCaptcha") from exc
        if isinstance(parsed, dict):
            start = parsed

    captcha_type = data.get("captchaType")
    try:
        captcha_type = int(captcha_type)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        captcha_type = C.CAPTCHA_TYPE_JIYAN

    return SliderChallenge(
        captcha_type=captcha_type,
        captcha_name=str(data.get("captchaName") or C.CAPTCHA_TYPE_NAMES.get(captcha_type, "")),
        gt=str(start.get("gt") or data.get("gt") or ""),
        challenge=str(start.get("challenge") or data.get("challenge") or ""),
        rand_key=str(data.get("randKey") or ""),
        scene=scene,
        start_captcha_raw=start_raw,
        raw=dict(data),
    )


def parse_solution(payload: Mapping[str, Any]) -> SliderSolution:
    """把浏览器回传的求解结果拍成 `SliderSolution`；缺字段抛 `ValidationError`。"""
    def pick(*names: str) -> str:
        """按候选键名取第一个非空字符串。"""
        for name in names:
            value = payload.get(name)
            if value:
                return str(value)
        return ""

    challenge = pick("geetest_challenge", "challenge")
    validate = pick("geetest_validate", "validate")
    seccode = pick("geetest_seccode", "seccode")
    if not (challenge and validate and seccode):
        raise ValidationError(
            "求解结果不完整，需要 challenge / validate / seccode 三项", field="solution"
        )
    return SliderSolution(
        challenge=challenge,
        validate=validate,
        seccode=seccode,
        rand_key=pick("randKey", "rand_key"),
        captcha_type=int(payload.get("type") or payload.get("captchaType") or C.CAPTCHA_TYPE_JIYAN),
    )


_HELPER_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>滑块验证 · BOSS直聘</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background:#f5f7fa; color:#1a1a1a; margin:0; padding:40px 16px; }}
  .card {{ max-width:420px; margin:0 auto; background:#fff; border-radius:16px;
           box-shadow:0 2px 16px rgba(0,0,0,.08); padding:32px 28px; }}
  h1 {{ font-size:18px; margin:0 0 8px; }}
  p  {{ font-size:13px; color:#666; line-height:1.7; margin:0 0 20px; }}
  #geetest {{ min-height:44px; }}
  .ok {{ display:none; margin-top:20px; padding:14px 16px; border-radius:8px;
         background:#e6f7f7; color:#0a7a7a; font-size:13px; line-height:1.7; }}
  .tokens {{ display:none; margin-top:16px; }}
  .tokens label {{ display:block; font-size:12px; color:#999; margin:10px 0 4px; }}
  .tokens input {{ width:100%; box-sizing:border-box; padding:8px 10px; font-size:12px;
                   border:1px solid #e0e0e0; border-radius:6px; font-family:monospace; }}
  .meta {{ margin-top:22px; font-size:11px; color:#bbb; line-height:1.8; }}
</style>
</head>
<body>
  <div class="card">
    <h1>网站访客身份验证</h1>
    <p>下面这块滑块由<strong>极验官方组件</strong>渲染，答案在你手里。<br>
       拖过去即视为本人完成验证，本页面不会替你算缺口、不会伪造轨迹。</p>
    <div id="geetest"></div>
    <div class="ok" id="ok"></div>
    <div class="tokens" id="tokens">
      <label>geetest_challenge</label><input id="t1" readonly>
      <label>geetest_validate</label><input id="t2" readonly>
      <label>geetest_seccode</label><input id="t3" readonly>
    </div>
    <div class="meta">
      challenge：<code id="meta-c"></code><br>
      本页监听 <code>http://{host}:{port}/</code>，拖完自动回传。
    </div>
  </div>

<script src="{loader_url}"></script>
<script>
(function () {{
  var gt = {gt_json};
  var challenge = {challenge_json};
  var postUrl = {post_url_json};

  function finish(validate) {{
    var ok = document.getElementById("ok");
    ok.style.display = "block";
    ok.innerHTML = "✓ 验证通过，已回传给本机客户端，可以关闭这个页面了。";

    // 票据也摊在页面上：万一回传失败（端口被占用、脚本挂了），
    // 你还能手抄回终端，不至于白拖一次。
    document.getElementById("t1").value = validate.geetest_challenge || validate.challenge || "";
    document.getElementById("t2").value = validate.geetest_validate || validate.validate || "";
    document.getElementById("t3").value = validate.geetest_seccode || validate.seccode || "";
    document.getElementById("tokens").style.display = "block";

    var body = JSON.stringify({{
      geetest_challenge: validate.geetest_challenge || validate.challenge,
      geetest_validate: validate.geetest_validate || validate.validate,
      geetest_seccode: validate.geetest_seccode || validate.seccode
    }});
    if (window.fetch) {{
      fetch(postUrl, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: body}})
        .then(function (r) {{
          if (!r.ok) throw new Error("HTTP " + r.status);
          ok.innerHTML = "✓ 验证通过，结果已送达本机客户端，可以关闭这个页面了。";
        }})
        .catch(function (e) {{
          ok.innerHTML = "⚠ 验证已通过，但回传失败（" + e + "）。请把下面三行票据抄回终端。";
        }});
    }} else {{
      ok.innerHTML = "⚠ 浏览器过旧不支持自动回传，请把下面三行票据抄回终端。";
    }}
  }}

  function boot() {{
    if (typeof initGeetest !== "function") {{
      document.getElementById("ok").style.display = "block";
      document.getElementById("ok").innerHTML = "✗ 极验组件没能加载，请检查网络后刷新。";
      return;
    }}
    initGeetest({{
      gt: gt,
      challenge: challenge,
      api_server_v3: {api_servers_json},
      offline: false,
      new_captcha: true,
      product: "float",
      width: "100%"
    }}, function (captchaObj) {{
      captchaObj.onReady(function () {{}});
      captchaObj.onSuccess(function () {{
        finish(captchaObj.getValidate());
      }});
      captchaObj.onError(function () {{
        document.getElementById("ok").style.display = "block";
        document.getElementById("ok").innerHTML = "✗ 极验组件报错，请刷新页面重试。";
      }});
      captchaObj.appendTo(document.getElementById("geetest"));
    }});
  }}

  document.getElementById("meta-c").textContent = challenge;
  boot();
}})();
</script>
</body>
</html>
"""


def build_helper_html(
    challenge: SliderChallenge,
    *,
    host: str = C.SLIDER_HELPER_HOST,
    port: int = C.SLIDER_HELPER_PORT,
    loader_url: str = C.GEETEST_LOADER_URL,
    post_url: str = "/solution",
) -> str:
    """渲染本地求解帮助页；纯函数，便于离线核对输出。

    Args:
        challenge: 挑战参数，内嵌进页面。
        host: 帮助页监听地址。
        port: 帮助页监听端口。
        loader_url: 极验加载器地址。
        post_url: 票据回传地址，默认本地帮助服务的 `/solution`。
    """
    return _HELPER_HTML.format(
        host=host,
        port=port,
        loader_url=loader_url,
        post_url_json=json.dumps(post_url),
        gt_json=json.dumps(challenge.gt, ensure_ascii=False),
        challenge_json=json.dumps(challenge.challenge, ensure_ascii=False),
        api_servers_json=json.dumps(list(C.GEETEST_API_SERVERS), ensure_ascii=False),
    )


class SliderHelperError(BossLoginError):
    """本地帮助页没能把解题结果送回来（超时 / 被占用 / 页面被关掉）。"""


class _HelperHandler(BaseHTTPRequestHandler):
    """只干两件事：吐帮助页、收求解结果。"""

    html: str = ""
    result_box: "dict[str, Any]" = {}
    ready: threading.Event = threading.Event()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """把访问日志降到 debug。"""
        logger.debug("[slider-helper] %s", fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        """返回帮助页 HTML。"""
        body = self.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        """收下求解结果并置位 ready。"""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        try:
            solution = parse_solution(payload)
        except ValidationError:
            body = json.dumps({"ok": False, "message": "票据不完整"}, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        type(self).result_box["solution"] = solution
        type(self).ready.set()

        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def solve_via_helper(
    challenge: SliderChallenge,
    *,
    host: str = C.SLIDER_HELPER_HOST,
    port: int = C.SLIDER_HELPER_PORT,
    timeout: float = C.SLIDER_HELPER_TIMEOUT,
    open_browser: bool = True,
    loader_url: str = C.GEETEST_LOADER_URL,
    on_event: EventHandler | None = None,
) -> SliderSolution:
    """起一个仅本机可访问的帮助页，等你拖完滑块把票据收回来；超时抛 `SliderHelperError`。

    Args:
        challenge: 挑战参数。
        host: 监听地址。
        port: 监听端口；0 表示交给系统分配。
        timeout: 等人工求解的上限（秒）。
        open_browser: 是否自动拉起默认浏览器。
        loader_url: 极验加载器地址。
        on_event: 过程事件回调。
    """
    challenge.require_geetest()
    html = build_helper_html(challenge, host=host, port=port, loader_url=loader_url)

    ready = threading.Event()
    result_box: dict[str, Any] = {}
    handler = type(
        "_BoundHelperHandler",
        (_HelperHandler,),
        {"html": html, "result_box": result_box, "ready": ready},
    )
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        raise SliderHelperError(f"本地帮助页起不来（{host}:{port} 被占用？）：{exc}") from exc

    bound_port = httpd.server_address[1]
    url = f"http://{host}:{bound_port}/"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    if on_event:
        on_event("slider_helper_started", {"url": url, "gt": challenge.gt})
    logger.info("滑块帮助页已就绪：%s（请在浏览器里拖动滑块）", url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - 拉不起浏览器不算失败，页面还在
            logger.warning("自动打开浏览器失败（%s），请手动访问 %s", exc, url)

    try:
        if not ready.wait(timeout):
            raise SliderHelperError(
                f"等了 {timeout:.0f} 秒还没收到滑块结果，本次验证作废"
                f"（挑战已过期，下次会重新拉取）。如需手动粘贴，票据在帮助页下方。"
            )
        solution = result_box.get("solution")
        if solution is None:  # pragma: no cover - ready 置位时必然写入
            raise SliderHelperError("帮助页说成功了，却没给出票据")
        if on_event:
            on_event("slider_solved", {"challenge": solution.challenge})
        return solution
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


def validate_request_headers(
    solution: SliderSolution,
    *,
    trace_id: str = "",
    zp_token: str = "",
) -> dict[str, str]:
    """组装 `zpsecureflow/captcha/validate` 要带的请求头（请求体为空）。

    只给 verify.html 那条链用；业务请求走 `SliderSolution.as_headers`。
    """
    headers = solution.as_headers(trace_id=trace_id, zp_token=zp_token)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers
