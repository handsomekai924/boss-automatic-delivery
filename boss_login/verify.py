"""滑块验证协议层 —— 拉挑战、把题交给本人解、组装票据。

业务请求被 400061 拦下后的那条链（passport 族）::

    GET  /wapi/zppassport/captcha/getTypeV2  ──►  {gt, challenge, randKey}
                                        │
                                        ▼
                          本地帮助页加载官方极验组件（gt.0.5.0）
                          ┌──────────────────────────────────────┐
                          │  你本人拖动滑块，极验在浏览器里出结果  │
                          └──────────────────────────────────────┘
                                        │  {challenge, validate, seccode}
                                        ▼
                     重试 —— 打 **send/smsCodeV2**，票据在**表单**里：
                       challenge / validate / seccode
                     （极验通道就这三个键；user-login.js 的 Ce 写死了）

**这条链上没有 validate，也没有 Zp-Captcha 请求头**。passport 族根本没有
validate 路由（实测 404）。那套请求头是 zpsecureflow validate 的形状；登录页
chunk 里 ``Zp-Captcha`` 出现 0 次——业务请求只把票据放在表单上。极验票据是
一次性凭证——先拿去 ``zpsecureflow/validate`` 会把它烧掉，业务请求再带同一张
就只剩 400061（真机踩过）。

``zpsecureflow/captcha/gettype + validate`` 是 verify.html 独立验证页那条链，
下发的是**另一个 gt**，它的票据打不动 zppassport 的业务接口。见
:func:`validate_request_headers` / :meth:`ZhipinLoginClient.run_slider_verify`。

设计要点
    * **不破解滑块**：缺口在哪、要拖多远、拖动轨迹怎么伪造，这里一概不管。
      解题永远是浏览器里的官方极验组件 + 你的手。
    * 协议层只做三件事：把挑战参数取出来、把官方组件架起来、把结果装回请求。
    * 网络层 / 时钟 / 随机数全部可注入，``generate_trace_id`` 等纯函数可离线对拍。

关于 traceId
    verify.html 的 ``getCommonHeaders()`` 会给每个请求带一个 ``traceId``，
    形状是 ``F-<13 位十六进制时间戳 + 6 位随机字符><3 位校验符>``。校验符有两条
    实现：Node 环境走 HMAC-SHA256，浏览器走一套 32 位位运算。真实站点是浏览器
    发的请求，所以这里复刻**浏览器那条**（见 :func:`_compute_checksum`）。
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

#: 解题结果回调签名：(挑战) -> 方案或 None；None 表示放弃
SliderSolver = Callable[["SliderChallenge"], "SliderSolution | None"]
#: 过程事件回调：on_event(name, payload)
EventHandler = Callable[[str, dict[str, Any]], None]

# 复刻 verify.html 里的 CHARS / SECRET_KEY
_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SliderChallenge:
    """一次滑块挑战的全部参数。

    :param captcha_type: 通道号，1=极验滑块（实测返回的就是这个）
    :param gt:           极验的 gt（站点公钥）
    :param challenge:    极验的一次性 challenge
    :param rand_key:     zppassport 族会带；zpsecureflow 族为 None
    :param scene:        场景码，回传时用于对账
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
        return self.captcha_type == C.CAPTCHA_TYPE_JIYAN

    def require_geetest(self) -> "SliderChallenge":
        """目前只实现了极验滑块的求解链路，别的通道直接说清楚。

        返回的实测样本里 ``captchaType`` 恒为 1，所以这里不是假想的防御——
        真换了通道（阿里 / 易盾 / 图片），得先看清它的协议再补。
        """
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

    字段名跟 ``captchaObj.getValidate()`` 对齐：
    ``geetest_challenge`` / ``geetest_validate`` / ``geetest_seccode``。
    """

    challenge: str
    validate: str
    seccode: str
    rand_key: str = ""
    captcha_type: int = C.CAPTCHA_TYPE_JIYAN

    def as_headers(self, *, trace_id: str = "", zp_token: str = "") -> dict[str, str]:
        """按 captcha-sdk ``onSuccess().headers`` 的形状给出票据请求头。

        键名取自 captcha-sdk@5.1.3 的 onSuccess，别改名字。这套请求头挂到
        **被拦下的业务请求本身**上就是通行证——passport 族没有 validate 路由，
        也不需要先登记。

        Jiyan 通道的 onSuccess 只给 Type/Challenge/Validate/Seccode 四个头，
        **不带** Randkey（Picture 通道才带）。这里在有 randKey 时顺手补上，
        多一个头服务端不认识就忽略。
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
        """把票据铺成 **业务请求的表单字段**。

        键名不是猜的：登录页 chunk ``user-login.js`` 的 ``Ce`` 中间件按通道挂载::

            1==verifyType ? {challenge, validate, seccode}   # 极验（本项目走这条）
            3==verifyType ? {captcha, randKey}               # 图片
            4==verifyType ? {validate}                       # 易盾

        **没有** ``verifyToken`` / ``captchaToken``，极验通道也**不带** ``randKey``
        （那是图片通道的）。那份 chunk 里 ``Zp-Captcha`` 出现 0 次——业务请求不带
        那套头，它们是 zpsecureflow validate 的形状。
        """
        if self.captcha_type == C.CAPTCHA_TYPE_PICTURE:
            # 图片通道：票据本体在 captcha 字段里，randKey 跟着走。
            return {
                "captcha": self.validate,
                **({"randKey": self.rand_key} if self.rand_key else {}),
            }
        if self.captcha_type == C.CAPTCHA_TYPE_YIDUN:
            return {"validate": self.validate}
        # 极验（默认）以及未知通道：与 user-login.js 的 1==verifyType 分支一致
        return {
            "challenge": self.challenge,
            "validate": self.validate,
            "seccode": self.seccode,
        }


# --------------------------------------------------------------------------- #
# traceId（复刻 verify.html 的浏览器实现）
# --------------------------------------------------------------------------- #


def _to_uint32(value: float | int) -> int:
    """JS 的 ``>>> 0``。传进来的可能是 float64 乘积，先按 JS 的 ToInteger 截断。"""
    if isinstance(value, float):
        # float.is_integer() 之前先截断向零，与 JS ToInteger 一致
        value = int(value)
    return value % (1 << 32)


def _js_mul_u32(multiplier: int, value: int) -> int:
    """JS 的 ``multiplier * value >>> 0``。

    注意这一步在 JS 里是 **Number 乘法**（float64），乘积超过 2**53 时低位会被
    舍掉。Python 的 int 是精确的，所以这里先转 float 相乘，才能和浏览器逐位对齐。
    """
    return _to_uint32(float(multiplier) * float(value))


def _mix_u32(value: int, mul1: int, shift1: int, mul2: int, shift2: int) -> int:
    """一段 Murmur 风格的收尾混合，对应 verify.html 里那串嵌套赋值。"""
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
    """3 位校验符，逐行对应 verify.html 的 ``computeChecksum`` 浏览器分支。"""
    # 累加器 t：((t<<5)-t+charCode) | 0
    t = 0
    for ch in seed:
        t = _i32((t << 5) - t + ord(ch))
    # 累加器 n：((n<<7)-n+charCode*(i+1)) | 0，倒序
    n = 0
    for i in range(len(seed) - 1, -1, -1):
        n = _i32((n << 7) - n + ord(seed[i]) * (i + 1))
    # 累加器 o：((o<<3)-o+charCode*(|i-mid|+1)) | 0
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
    """生成 ``F-<13 位十六进制毫秒时间戳><6 位随机字符><3 位校验符>``。

    与 verify.html 的 ``generateBossTraceID()`` 同构；校验符走浏览器那条分支。
    """
    stamp = format(int(now() * 1000), "x").lower().rjust(13, "0")[-13:]
    suffix = "".join(_CHARS[int(rand() * len(_CHARS)) % len(_CHARS)] for _ in range(6))
    seed = stamp + suffix
    return f"F-{seed}{_compute_checksum(seed)}"


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #


def parse_challenge(data: Mapping[str, Any], *, scene: str = "") -> SliderChallenge:
    """把 gettype / getTypeV2 的 ``zpData`` 拍成 :class:`SliderChallenge`。

    实测形状::

        {"captchaType":1,"captchaName":"极验验证","wyCaptchaId":null,
         "wyCaptchaType":null,
         "startCaptcha":"{\\"success\\":1,\\"challenge\\":\\"...\\",\\"gt\\":\\"...\\"}",
         "randKey":null}

    ``startCaptcha`` 是**字符串化的 JSON**，这点不看实测根本猜不到。
    """
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
    """把浏览器回传的求解结果拍成 :class:`SliderSolution`。

    同时认两套键名：极验原始的 ``geetest_*`` 与帮助页收的短键，免得调用方记混。
    """
    def pick(*names: str) -> str:
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


# --------------------------------------------------------------------------- #
# 本地帮助页
# --------------------------------------------------------------------------- #

#: 页面里内嵌了挑战参数，打开就能拖，不需要再回服务端取。
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
  var postUrl = "/solution";

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
) -> str:
    """渲染本地求解帮助页。纯函数，便于离线核对输出。"""
    return _HELPER_HTML.format(
        host=host,
        port=port,
        loader_url=loader_url,
        gt_json=json.dumps(challenge.gt, ensure_ascii=False),
        challenge_json=json.dumps(challenge.challenge, ensure_ascii=False),
        api_servers_json=json.dumps(list(C.GEETEST_API_SERVERS), ensure_ascii=False),
    )


# --------------------------------------------------------------------------- #
# 本地帮助服务
# --------------------------------------------------------------------------- #


class SliderHelperError(BossLoginError):
    """本地帮助页没能把解题结果送回来（超时 / 被占用 / 页面被关掉）。"""


class _HelperHandler(BaseHTTPRequestHandler):
    """只干两件事：吐帮助页、收求解结果。"""

    # 由 SliderHelper 在启动前塞进来
    html: str = ""
    result_box: "dict[str, Any]" = {}
    ready: threading.Event = threading.Event()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("[slider-helper] %s", fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        body = self.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
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
    """起一个仅本机可访问的帮助页，等你拖完滑块把票据收回来。

    :param port:        监听端口；0 表示交给系统分配
    :param timeout:     等人工求解的上限（秒）
    :param open_browser: 是否自动拉起默认浏览器
    :raises SliderHelperError: 超时或没能收下结果

    页面里跑的是极验**官方**组件，答案由你本人拖出来。这里只是把
    「挑战参数 → 浏览器 → 票据 → 客户端」这段管道接好。
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


# --------------------------------------------------------------------------- #
# 顺手给出的请求头
# --------------------------------------------------------------------------- #


def validate_request_headers(
    solution: SliderSolution,
    *,
    trace_id: str = "",
    zp_token: str = "",
) -> dict[str, str]:
    """组装 ``POST zpsecureflow/captcha/validate`` 要带的请求头。

    verify.html 里是 ``c.send("")``：请求体为空，票据全在请求头上。
    另外还带 ``traceId`` / ``zp_token``（取自 ``bst`` Cookie），这里一并给你。

    **只给 verify.html 那条链用**。业务请求的自动衔接走
    :meth:`SliderSolution.as_headers`，别把票据先交来这里——极验票据一次性。
    """
    headers = solution.as_headers(trace_id=trace_id, zp_token=zp_token)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers
