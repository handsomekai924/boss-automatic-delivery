"""短信登录客户端 —— 完整流程的核心实现。

    校验手机号 → 本地冷却 → 发验证码 → 人工输码 → getLoginSuggest → 登录 → 收 Set-Cookie

发码分支：限流 → :class:`RateLimited`；滑块 → 解题后带票打 **smsCodeV2**（不调
validate，票据一次性）；风控 → :class:`RiskControlRequired`；封禁 → :class:`AccountBlocked`。
登录分支：滑块 → 先复用发码那张票打 **phoneV2**，不认就重新解一次；验证码错误/
过期 → :class:`CodeRejected`；成功 → 落盘登录态。``zpData.token`` 是 smsToken。

* 网络、休眠、时钟可注入，便于离线单测（见 tests/）。
* 服务端返回码 → 异常类型集中在 ``_handle_response``，按类型捕获即可。
* **真实站点的登录响应体里没有会话 token**，鉴权完全靠 ``Set-Cookie``；Cookie 名
  可能不在 ``AUTH_COOKIES`` 里，「这次登录新种的 Cookie」本身就当凭证。
* **不实现验证码破解**：滑块由本人在浏览器里拖官方极验组件，本模块只递挑战参数、
  收回票据；其余风控抛 ``RiskControlRequired`` 交使用者处理。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode

import requests

from . import config as C
from .crypto import encrypt_account
from .errors import (
    AccountBlocked,
    ApiError,
    BossLoginError,
    CodeRejected,
    LoginIncomplete,
    RateLimited,
    RiskControlRequired,
    SessionExpired,
    TransportError,
    ValidationError,
)
from .models import ApiResponse, LoginResult, SmsCodeTicket, UserInfo, mask_phone
from .verify import (
    SliderChallenge,
    SliderSolution,
    SliderSolver,
    generate_trace_id,
    parse_challenge,
    solve_via_helper,
    validate_request_headers,
)

logger = logging.getLogger(__name__)

#: 事件回调签名：on_event(name: str, payload: dict) -> None
EventHandler = Callable[[str, dict[str, Any]], None]
#: 验证码输入回调签名：(尝试次数, 票据或 None) -> 验证码字符串；返回 None 表示放弃
CodeProvider = Callable[[int, "SmsCodeTicket | None"], "str | None"]

_CN_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_INTL_PHONE_RE = re.compile(r"^\d{5,15}$")
_NON_DIGIT_RE = re.compile(r"\D")


def normalize_phone(phone: str) -> str:
    """去掉空格、横线、括号等非数字字符（用户常从通讯录粘贴带格式的号码）。"""
    return _NON_DIGIT_RE.sub("", str(phone or ""))


def validate_phone(phone: str, dial_code: str = "86") -> None:
    """校验手机号格式，不合法则抛 :class:`ValidationError`。"""
    if not phone:
        raise ValidationError("请输入手机号", field="phone")
    if dial_code == "86":
        if not _CN_PHONE_RE.match(phone):
            raise ValidationError("手机号格式不正确", field="phone")
    elif not _INTL_PHONE_RE.match(phone):
        raise ValidationError("手机号格式不正确，请检查国家区号", field="phone")


def validate_code(code: str, code_length: int = C.CODE_LENGTH) -> None:
    """校验验证码格式。"""
    code = normalize_phone(code)
    if not code:
        raise ValidationError("请输入短信验证码", field="code")
    if len(code) != code_length:
        raise ValidationError(f"验证码为 {code_length} 位数字", field="code")


class ZhipinLoginClient:
    """BOSS直聘用户端短信登录客户端。

    :param base_url:      站点根地址
    :param endpoints:     覆盖 :data:`config.ENDPOINTS` 中的接口路径
    :param http:          具备 ``request()`` 方法的会话对象（默认新建 ``requests.Session``）
    :param timeout:       单次请求超时（秒）
    :param retries:       网络层失败后的重试次数（仅针对超时 / 5xx）
    :param backoff:       重试退避基数（秒），按 2 的幂增长
    :param cooldown_seconds: 本地重发冷却，默认与服务端一致（60s）
    :param sleeper:       休眠函数，测试时可注入空实现
    :param clock:         取时间戳的函数，测试时可注入可控时钟
    """

    def __init__(
        self,
        *,
        base_url: str = C.BASE_URL,
        endpoints: Mapping[str, str] | None = None,
        http: Any | None = None,
        timeout: float = 10.0,
        retries: int = 2,
        backoff: float = 0.8,
        cooldown_seconds: int = C.RESEND_COOLDOWN_SECONDS,
        code_length: int = C.CODE_LENGTH,
        max_code_attempts: int = C.MAX_CODE_ATTEMPTS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoints = {**C.ENDPOINTS, **C.VERIFY_ENDPOINTS, **(endpoints or {})}
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.cooldown_seconds = cooldown_seconds
        self.code_length = code_length
        self.max_code_attempts = max_code_attempts
        self._sleep = sleeper
        self._clock = clock

        self._http = http or requests.Session()
        merged_headers = {**C.DEFAULT_HEADERS, **(headers or {})}
        # 注入的假会话可能没有 headers 属性，因此做一次防御性赋值
        if hasattr(self._http, "headers"):
            self._http.headers.update(merged_headers)

        #: phone -> 最近一次发送回执
        self._tickets: dict[str, SmsCodeTicket] = {}
        #: phone -> 发码那次解出来的滑块票据。登录页 ``verifyInfo`` 跨发码 / 登录
        #: 共用，登录会先试着复用这张，服务端不认再重新解一次。
        self._slider_solutions: dict[str, SliderSolution] = {}
        #: phone -> 发码响应 ``zpData.token``（smsToken）。登录前置 ``getLoginSuggest``
        #: 要用；**不是**登录态，别拿它当鉴权凭证。
        self._sms_tokens: dict[str, str] = {}
        #: 落盘的会话 token（真实站点登录响应体里通常没有，假服务端会给）
        self._auth_token: str = ""
        #: 登录那次种下的 Cookie 名。真实站点鉴权靠 Set-Cookie，名字可能不在
        #: ``AUTH_COOKIES`` 里——记下实到的名字，``is_logged_in`` 才认得出它们。
        self._session_cookie_names: set[str] = set()


    @property
    def http(self) -> Any:
        """底层会话，便于外部直接复用 Cookie。"""
        return self._http

    def last_ticket(self, phone: str) -> SmsCodeTicket | None:
        return self._tickets.get(normalize_phone(phone))


    def send_sms_code(
        self,
        phone: str,
        *,
        dial_code: str = "86",
        scene: str = "login",
        identity: int = C.IDENTITY_JOB_SEEKER,
        verify_token: str | None = None,
        force: bool = False,
        slider_solver: SliderSolver | None = None,
        on_event: EventHandler | None = None,
    ) -> SmsCodeTicket:
        """请求下发短信验证码。

        :param phone:        手机号（自动清洗格式）
        :param dial_code:    国家区号，默认 ``86``
        :param scene:        ``login`` / ``register`` / ``bind``，决定 V1 的 ``smsType``
        :param identity:     ``0`` 求职者 / ``1`` 招聘者；滑块重试走 V2 时进 ``purpose``
        :param verify_token: 命中风控后，由人工验证取得并回传的票据
        :param force:        忽略本地冷却强制发送（一般只用于调试）
        :param slider_solver: 命中滑块时的解题回调；给了就会人机协作过一次再重试
        :raises ValidationError:    手机号不合法
        :raises RateLimited:        仍在冷却期
        :raises RiskControlRequired: 需要人工完成安全验证
        :raises AccountBlocked:     IP / 账号被封禁

        命中滑块且给了 ``slider_solver`` 时人机协作过一次，再带票打 **V2** 重试
        （走 :meth:`solve_slider`，**不调 validate**，票据一次性）；登录步会复用
        这张票。
        """
        phone = normalize_phone(phone)
        validate_phone(phone, dial_code)
        # 参数校验放在最前：非法场景码应当在冷却检查等有状态判断之前就失败
        sms_type = self._scene_code(scene)

        if not force:
            self._guard_local_cooldown(phone)

        payload: dict[str, Any] = {
            "phone": phone,
            "regionCode": dial_code,
            "smsType": sms_type,
        }
        if verify_token:
            payload.update(self._verify_token_payload(verify_token))

        response = self._post(self.endpoints["send_sms_code"], payload)
        try:
            self._handle_response(response, phone=phone, action="send_sms_code")
        except RiskControlRequired as exc:
            if slider_solver is None or not self._is_slider_challenge(
                exc.code, exc.message, exc.raw or {}
            ):
                raise
            self._emit(on_event, "slider_required", {"code": exc.code, "message": exc.message})
            solution = self.solve_slider(solver=slider_solver, on_event=on_event)
            self._slider_solutions[phone] = solution
            response = self._retry_send_sms_v2(
                phone, dial_code=dial_code, identity=identity, solution=solution, on_event=on_event
            )
            try:
                self._handle_response(response, phone=phone, action="send_sms_code")
            except RiskControlRequired as retry_exc:
                # 票据挂在请求上了还被拦，说明还缺东西（键名不对、票据过期……）。
                # 别再抛一句「请完成滑块验证」把人绕回去——那条路刚走完。
                if self._is_slider_challenge(
                    retry_exc.code, retry_exc.message, retry_exc.raw or {}
                ):
                    raise RiskControlRequired(
                        retry_exc.code,
                        "滑块已经拖过了，票据也挂在 smsCodeV2 的表单上了，但服务端仍不认"
                        "（可能票据已过期）。请用 -v 重跑一次，看重试请求的路径和表单。",
                        raw=retry_exc.raw,
                    ) from retry_exc
                raise

        retry_after = self._parse_int(response.data.get("retryAfter"), self.cooldown_seconds)
        ticket = SmsCodeTicket(
            phone_masked=mask_phone(phone),
            dial_code=dial_code,
            sent_at=self._clock(),
            retry_after=retry_after,
        )
        self._tickets[phone] = ticket
        sms_token = str(response.data.get(C.SMS_TOKEN_FIELD) or "")
        if sms_token:
            self._sms_tokens[phone] = sms_token
        logger.info("验证码已发送至 %s，%s 秒后可重发", ticket.phone_masked, retry_after)
        return ticket

    def _guard_local_cooldown(self, phone: str) -> None:
        """本地冷却拦截，避免明知会被拒还去打服务端。"""
        ticket = self._tickets.get(phone)
        if ticket is None:
            return
        remain = ticket.remaining_cooldown(now=self._clock())
        if remain > 0:
            raise RateLimited(
                1001,
                f"发送太频繁了，请 {remain} 秒后再试",
                retry_after=remain,
            )


    def login_by_sms(
        self,
        phone: str,
        code: str,
        *,
        dial_code: str = "86",
        scene: str = "login",
        identity: int = C.IDENTITY_JOB_SEEKER,
        verify_token: str | None = None,
        slider_solver: SliderSolver | None = None,
        solution: SliderSolution | None = None,
        on_event: EventHandler | None = None,
    ) -> LoginResult:
        """用手机号 + 短信验证码登录（首次验证通过即注册）。

        :param identity:     ``0`` 求职者 / ``1`` 招聘者；滑块重试走 V2 时进 ``purpose``
        :param slider_solver: 命中滑块时的解题回调；缺省不解题，直接抛出
                              :class:`RiskControlRequired`。
        :param solution:      现成的滑块票据。缺省会先复用发码那次解出来的那张
                              （登录页的 ``verifyInfo`` 就是跨发码 / 登录共用的），
                              服务端不认再重新解一次。

        :raises ValidationError: 手机号或验证码格式不合法
        :raises CodeRejected:    验证码错误 / 过期 / 错误次数超限
        :raises RiskControlRequired / AccountBlocked: 风控

        登录前置 ``getLoginSuggest`` 失败不阻断。有票据就直接打 V2；没有先按 V1
        探一次，命中滑块再解题重试。命中滑块后**不调 validate**（票据一次性）。
        """
        phone = normalize_phone(phone)
        code = normalize_phone(code)
        validate_phone(phone, dial_code)
        validate_code(code, self.code_length)
        sms_type = self._scene_code(scene)

        if solution is None:
            solution = self._slider_solutions.get(phone)

        self._call_login_suggest(phone, code, dial_code=dial_code)
        # 真实站点登录响应体里没有会话 token，鉴权靠 Set-Cookie。拍快照好把
        # 「这次登录新种的 Cookie」单独认出来——名字可能不在 AUTH_COOKIES 里。
        cookies_before = self._collect_cookies()

        if solution is not None:
            response = self._retry_login_v2(
                phone,
                code,
                dial_code=dial_code,
                identity=identity,
                solution=solution,
                on_event=on_event,
            )
        else:
            payload: dict[str, Any] = {
                "phone": phone,
                "phoneCode": code,
                "regionCode": dial_code,
                "smsType": sms_type,
            }
            if verify_token:
                payload.update(self._verify_token_payload(verify_token))
            response = self._post(self.endpoints["login_by_sms"], payload)

        try:
            self._handle_response(response, phone=phone, action="login_by_sms")
        except RiskControlRequired as exc:
            if not self._is_slider_challenge(exc.code, exc.message, exc.raw or {}):
                raise
            if slider_solver is None:
                raise
            self._emit(on_event, "slider_required", {"code": exc.code, "message": exc.message})
            # 复用失败 = 票据已烧/过期，或刚才那发 V1 压根没带票；重新解一张。
            solution = self.solve_slider(solver=slider_solver, on_event=on_event)
            self._slider_solutions[phone] = solution
            cookies_before = self._collect_cookies()
            response = self._retry_login_v2(
                phone,
                code,
                dial_code=dial_code,
                identity=identity,
                solution=solution,
                on_event=on_event,
            )
            try:
                self._handle_response(response, phone=phone, action="login_by_sms")
            except RiskControlRequired as retry_exc:
                if self._is_slider_challenge(retry_exc.code, retry_exc.message, retry_exc.raw or {}):
                    raise RiskControlRequired(
                        retry_exc.code,
                        "滑块已经拖过了，票据也挂在 phoneV2 的表单上了，但服务端仍不认"
                        "（可能票据已过期）。请用 -v 重跑一次，看重试请求的路径和表单。",
                        raw=retry_exc.raw,
                    ) from retry_exc
                raise

        token = self._extract_token(response.data)
        cookies = self._collect_cookies()
        new_cookies = {
            name: value
            for name, value in cookies.items()
            if cookies_before.get(name) != value
        }
        user = UserInfo.from_data({**response.data, "phone": mask_phone(phone)})
        result = LoginResult(
            token=token,
            user=user,
            is_new_user=bool(response.data.get("isNewUser") or response.data.get("newUser")),
            cookies=cookies,
            raw=response.data,
            new_cookies=new_cookies,
        )
        if not result.logged_in:
            raise LoginIncomplete(
                "接口返回成功，但既没有 token，登录响应也没种下任何 Cookie，"
                "拿不到可用于后续请求的凭证。\n"
                f"  响应 zpData 的键：{self._shape_of(response.data)}\n"
                f"  Cookie 里现有的键：{sorted(cookies) or '（空）'}\n"
                "  真实站点的登录响应体本来就没有 token，鉴权靠 Set-Cookie；"
                "这里没收到 Cookie，说明登录那步没换到登录态。\n"
                "  若连路由都不确定，用 `python -m boss_login probe` 再确认一次。",
                raw=response.data,
            )
        if not token and not any(name in cookies for name in C.AUTH_COOKIES):
            # 种了 Cookie 但名字都不认识——先放行，让 user_info 验真伪，并打出名字。
            logger.warning(
                "登录响应种了 Cookie，但名字都不在已知鉴权名单里：%s",
                sorted(new_cookies) or sorted(cookies),
            )

        self._auth_token = token or self._auth_token
        if new_cookies:
            self._session_cookie_names |= set(new_cookies)
        elif any(name in cookies for name in C.AUTH_COOKIES):
            self._session_cookie_names |= {
                name for name in cookies if name in C.AUTH_COOKIES
            }
        # 登录成功后验证码即失效，清掉本地冷却，允许下次立即发起
        self._tickets.pop(phone, None)
        self._slider_solutions.pop(phone, None)
        self._sms_tokens.pop(phone, None)
        logger.info("登录成功：%s", user.name or user.user_id or result.user.phone_masked)
        return result

    def _call_login_suggest(self, phone: str, code: str, *, dial_code: str = "86") -> None:
        """登录前置检查 ``validate/getLoginSuggest``，失败不阻断登录。

        登录页 ``loginOrRegister`` 的中间件链**末位固定是它**（``Re``），每次登录
        都打，请求体 ``{token: smsToken, encryptedAccount, code, regionCode}``。
        整个调用裹在 try/catch 里，失败只上报埋点，登录照常往下走——所以这里也
        绝不能让它抛出去。

        ``smsToken`` 是发码响应的 ``zpData.token``。``send`` / ``login`` 若是两条
        命令（两个进程），这一步可能拿不到它，那就照登录页那样传空——总比不打好。
        """
        path = self.endpoints.get("login_suggest") or C.ENDPOINTS["login_suggest"]
        region = dial_code if str(dial_code).startswith("+") else f"+{dial_code}"
        payload = {
            "token": self._sms_tokens.get(phone, ""),
            "encryptedAccount": encrypt_account(phone),
            "code": code,
            "regionCode": region,
        }
        try:
            self._post(path, payload)
        except BossLoginError as exc:
            logger.debug("getLoginSuggest 失败（登录页也会吞掉，不阻断）：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 这一发绝不拦登录
            logger.debug("getLoginSuggest 异常（忽略）：%s", exc)


    def run_sms_login(
        self,
        phone: str,
        code_provider: CodeProvider,
        *,
        dial_code: str = "86",
        scene: str = "login",
        verify_token: str | None = None,
        slider_solver: SliderSolver | None = None,
        on_event: EventHandler | None = None,
    ) -> LoginResult:
        """串起完整登录流程，适合脚本 / CLI 直接调用。

        :param code_provider: 回调 ``(attempt, ticket) -> code``；
                              返回 ``None`` 表示使用者主动放弃本次登录。
        :param slider_solver: 命中滑块时的解题回调；缺省不解题，直接抛出
                              :class:`RiskControlRequired`，由调用方决定。
        :param on_event:      过程事件回调，事件名见下方 ``_emit`` 调用处。

        处理策略：
          * 冷却中不重复发码，直接进入输码环节；
          * 命中滑块且给了 ``slider_solver`` → 人机协作过掉后自动重试；
          * 验证码错误 → 回调再次索要，直到 ``max_code_attempts``；
          * 验证码过期 → 自动重发一次再来；
          * 其他异常直接抛出，交由调用方决定是否重试。
        """
        phone = normalize_phone(phone)
        validate_phone(phone, dial_code)

        ticket = self._ensure_code_sent(
            phone,
            dial_code=dial_code,
            scene=scene,
            verify_token=verify_token,
            slider_solver=slider_solver,
            on_event=on_event,
        )

        last_error: BossLoginError | None = None
        attempt = 0
        while attempt < self.max_code_attempts:
            attempt += 1
            self._emit(on_event, "need_code", {"attempt": attempt, "phone": ticket.phone_masked})
            code = code_provider(attempt, ticket)
            if code is None:
                raise BossLoginError("已取消登录")

            try:
                result = self.login_by_sms(
                    phone,
                    code,
                    dial_code=dial_code,
                    scene=scene,
                    verify_token=verify_token,
                    slider_solver=slider_solver,
                    on_event=on_event,
                )
            except CodeRejected as exc:
                last_error = exc
                self._emit(on_event, "code_rejected", {"attempt": attempt, "message": exc.message})
                if exc.code in (2002, 2003) and attempt < self.max_code_attempts:
                    ticket = self._resend_after_failure(
                        phone,
                        dial_code=dial_code,
                        scene=scene,
                        slider_solver=slider_solver,
                        on_event=on_event,
                    )
                continue

            self._emit(on_event, "success", {"attempt": attempt, "user": result.user.name})
            return result

        raise last_error or CodeRejected(2003, "验证码错误次数过多，请重新获取验证码")

    def _ensure_code_sent(
        self,
        phone: str,
        *,
        dial_code: str,
        scene: str,
        verify_token: str | None,
        slider_solver: SliderSolver | None = None,
        on_event: EventHandler | None = None,
    ) -> SmsCodeTicket:
        """发码；若处于冷却期且有票据，则复用而不报错。"""
        try:
            ticket = self.send_sms_code(
                phone,
                dial_code=dial_code,
                scene=scene,
                verify_token=verify_token,
                slider_solver=slider_solver,
                on_event=on_event,
            )
        except RateLimited as exc:
            existing = self._tickets.get(phone)
            if existing is None:
                raise
            logger.info("处于冷却期，复用上一次已发送的验证码（%s 秒后可重发）", exc.retry_after)
            self._emit(on_event, "cooldown_reused", {"retry_after": exc.retry_after})
            return existing

        self._emit(
            on_event,
            "code_sent",
            {"phone": ticket.phone_masked, "retry_after": ticket.retry_after},
        )
        return ticket

    def _resend_after_failure(
        self,
        phone: str,
        *,
        dial_code: str,
        scene: str,
        slider_solver: SliderSolver | None = None,
        on_event: EventHandler | None = None,
    ) -> SmsCodeTicket:
        """验证码过期后的重发：先等到冷却结束，避免又撞限流。

        本地与服务端的冷却可能对不上（时钟漂移，或另一台设备刚发过），以服务端
        给的剩余秒数为准再等一次；超过上限就放弃，避免无意义的长眠。
        """
        ticket = self._tickets.get(phone)
        wait = ticket.remaining_cooldown(now=self._clock()) if ticket else 0
        if wait > 0:
            logger.info("等待 %s 秒冷却结束后重发验证码", wait)
            self._sleep(wait)
        self._tickets.pop(phone, None)

        try:
            new_ticket = self.send_sms_code(
                phone, dial_code=dial_code, scene=scene,
                slider_solver=slider_solver, on_event=on_event,
            )
        except RateLimited as exc:
            if exc.retry_after <= 0 or exc.retry_after > C.MAX_RESEND_WAIT_SECONDS:
                raise
            logger.info("服务端要求 %s 秒后才可重发，等待后重试一次", exc.retry_after)
            self._sleep(exc.retry_after)
            self._tickets.pop(phone, None)
            new_ticket = self.send_sms_code(
                phone, dial_code=dial_code, scene=scene,
                slider_solver=slider_solver, on_event=on_event,
            )

        self._emit(
            on_event,
            "code_resent",
            {"phone": new_ticket.phone_masked, "retry_after": new_ticket.retry_after},
        )
        return new_ticket


    def fetch_user_info(self) -> UserInfo:
        """拉取当前登录用户信息；未登录抛 :class:`SessionExpired`。"""
        response = self._post(self.endpoints["user_info"], {}, method="GET")
        if not response.ok:
            raise SessionExpired(response.message or "未登录或登录态已失效", raw=response.data)
        return UserInfo.from_data(response.data)

    def is_logged_in(self) -> bool:
        """本地是否持有登录态（轻量判断，不发请求）。

        会话 token 和鉴权 Cookie 都算——真实站点登录响应体里往往没有 token，
        但落盘的会话可能只剩 token（或只剩 Cookie），两种都得认。
        Cookie 名可能不在 ``AUTH_COOKIES`` 里，所以登录那次实到的名字也算。
        """
        if self._auth_token:
            return True
        cookies = self._collect_cookies()
        if any(name in cookies for name in C.AUTH_COOKIES):
            return True
        return any(name in cookies for name in self._session_cookie_names)

    def set_auth_token(self, token: str) -> None:
        """注入/清空会话 token（``create_client`` 载入落盘登录态时用）。"""
        self._auth_token = token or ""

    def set_session_cookies(self, cookies: Mapping[str, str]) -> None:
        """登记「这些 Cookie 名是登录凭证」（``create_client`` 载入落盘登录态时用）。

        不能只认 ``AUTH_COOKIES``：真实站点鉴权靠 Set-Cookie，名字会变。
        落盘时已经认定是登录态的 Cookie，复用时也得认。
        """
        self._session_cookie_names = {str(name) for name in cookies}

    @property
    def auth_token(self) -> str:
        return self._auth_token

    def logout(self) -> None:
        """退出登录并清空本地登录态。"""
        try:
            self._post(self.endpoints["logout"], {})
        except BossLoginError as exc:  # 退出失败不应阻断本地清理
            logger.warning("调用登出接口失败：%s", exc)
        finally:
            self._tickets.clear()
            self._auth_token = ""
            self._session_cookie_names.clear()
            cookies = getattr(self._http, "cookies", None)
            if cookies is not None and hasattr(cookies, "clear"):
                cookies.clear()


    def fetch_slider_challenge(
        self,
        *,
        scene: str = "",
        endpoint_key: str = C.SLIDER_CHALLENGE_ENDPOINT_KEY,
    ) -> SliderChallenge:
        """向服务端要一次滑块挑战。

        默认走 **passport 族**（与 ``send/smsCode`` 同族）::

            GET /wapi/zppassport/captcha/getTypeV2
            → {"code":0,"zpData":{"captchaType":1,"captchaName":"极验验证",
               "startCaptcha":"{\\"gt\\":\\"…\\",\\"challenge\\":\\"…\\"}",
               "randKey":"…"}}

        同族很重要：两边共用同一个极验 gt 和同一套服务端会话。verify.html 用的
        zpsecureflow ``gettype`` 下发的是**另一个 gt**，拿它的票据去打 zppassport
        的业务接口，服务端不认（真机踩过）。要走那条链，把 ``endpoint_key``
        传成 ``"get_type"``。

        :raises ApiError: 服务端没给出可用的挑战
        """
        path = self.endpoints.get(endpoint_key) or C.VERIFY_ENDPOINTS[endpoint_key]
        params = {"scene": scene} if scene else {}
        response = self._get(path, params)
        if not response.ok:
            raise ApiError(
                response.code,
                response.message or "拉取滑块挑战失败",
                raw=response.data,
            )
        challenge = parse_challenge(response.data, scene=scene).require_geetest()
        logger.info(
            "已取到滑块挑战：gt=%s challenge=%s… randKey=%s",
            challenge.gt[:12],
            challenge.challenge[:12],
            "有" if challenge.rand_key else "无",
        )
        logger.debug("挑战原始返回：%s", response.data)
        return challenge

    def solve_slider(
        self,
        *,
        solver: SliderSolver | None = None,
        scene: str = "",
        endpoint_key: str = C.SLIDER_CHALLENGE_ENDPOINT_KEY,
        on_event: EventHandler | None = None,
    ) -> SliderSolution:
        """拉挑战 → 你本人解题，**不调 validate**。

        业务请求的自动衔接就用这条。passport 族没有 validate 路由（实测 404）；
        captcha-sdk 的 onSuccess 也只把 ``Zp-Captcha-*`` 交回调用方，由调用方挂到
        被拦下的那个请求上。极验票据是一次性凭证，先交去 validate 会把它烧掉。

        :param solver: 解题回调 ``(challenge) -> solution``；
                       缺省用 :func:`boss_login.verify.solve_via_helper`，
                       也就是拉起本地帮助页、由你在浏览器里拖官方极验滑块。
        """
        challenge = self.fetch_slider_challenge(scene=scene, endpoint_key=endpoint_key)
        self._emit(on_event, "slider_challenge", {"gt": challenge.gt, "challenge": challenge.challenge})

        if solver is None:
            solution = solve_via_helper(challenge, on_event=on_event)
        else:
            solution = solver(challenge)
        if solution is None:
            raise BossLoginError("已放弃滑块验证")
        if not solution.rand_key:
            # getTypeV2 特意下发 randKey，多半要跟着业务请求走
            solution = SliderSolution(
                challenge=solution.challenge,
                validate=solution.validate,
                seccode=solution.seccode,
                rand_key=challenge.rand_key,
                captcha_type=solution.captcha_type or challenge.captcha_type,
            )
        return solution

    def submit_slider_solution(self, solution: SliderSolution) -> ApiResponse:
        """把票据交给 **zpsecureflow** 的 validate 接口（verify.html 页面那条链）。

        请求体是空的，票据全在请求头上——这是 verify.html 的 ``validate`` 调用方式，
        不是自创的。

        **不要**在业务请求的自动衔接里调它：passport 族没有这个路由，而且极验
        票据一次性，先交在这里会把票据烧掉，业务请求再带同一张就只剩 400061。
        业务链请用 :meth:`solve_slider` + :meth:`_retry_send_sms_v2`。

        :raises ApiError: 服务端不认这张票据
        """
        path = self.endpoints.get("validate") or C.VERIFY_ENDPOINTS["validate"]
        headers = validate_request_headers(
            solution,
            trace_id=generate_trace_id(now=self._clock),
            zp_token=self._cookie("bst") or "",
        )
        logger.debug("validate 请求头 %s", sorted(headers))
        response = self._request(
            "POST", path, body="", extra_headers=headers, action="submit_slider_solution"
        )
        logger.debug(
            "validate 返回：code=%s message=%s data=%s",
            response.code,
            response.message,
            response.data,
        )
        if not response.ok:
            raise ApiError(
                response.code,
                response.message or "滑块票据校验失败",
                raw=response.data,
            )
        logger.info("zpsecureflow validate 已接受票据（code=%s）", response.code)
        return response

    def run_slider_verify(
        self,
        *,
        solver: SliderSolver | None = None,
        scene: str = "passport-verify",
        on_event: EventHandler | None = None,
    ) -> SliderSolution:
        """verify.html 页面那条链：拉挑战 → 人机解题 → 交 zpsecureflow validate。

        这条是**独立验证页**的流程，跟业务请求的自动衔接不是一回事。业务请求
        请用 :meth:`solve_slider`——那条链不该调 validate（票据一次性）。

        :raises SliderHelperError / ApiError: 求解或提交失败
        """
        solution = self.solve_slider(
            solver=solver,
            scene=scene,
            endpoint_key="get_type",  # verify.html 族，和 validate 同族
            on_event=on_event,
        )
        self.submit_slider_solution(solution)
        self._emit(on_event, "slider_passed", {"challenge": solution.challenge})
        return solution

    def _is_slider_challenge(self, code: int, message: str, data: Mapping[str, Any]) -> bool:
        """区分「滑块」与其余风控：滑块可以人机协作过掉，其余得交给人去处理。

        刻意比 :meth:`_is_risk_control` 窄：SECURITY_CHECK(37) 那种「跳去
        security.html」的安全验证带 ``{seed,ts,name}``，协议完全不同，
        不能套滑块链路。滑块只认 400061 及其话术近亲。
        """
        if code in C.RISK_CODE_IP_BLOCK | C.RISK_CODE_UID_BLOCK:
            return False
        if code in C.RISK_CODE_SLIDER:
            return True
        return any(keyword in message for keyword in C.SLIDER_MESSAGE_KEYWORDS)


    def probe_endpoints(self, *, keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """逐个试探候选接口，帮助确认线上真实路径。

        对每个候选地址发一次**空表单** POST，只关心「有没有这个路由」：
        404/403 视为不可用，其余状态视为存活候选。
        """
        report: list[dict[str, Any]] = []
        targets = keys or C.CANDIDATE_ENDPOINTS.keys()
        for key in targets:
            for path in C.CANDIDATE_ENDPOINTS.get(key, ()):
                try:
                    response = self._http.request(
                        "POST",
                        self.base_url + path,
                        data={},
                        headers=dict(C.DEFAULT_HEADERS),
                        timeout=self.timeout,
                        allow_redirects=False,
                    )
                    snippet = (response.text or "")[:120].replace("\n", " ")
                    report.append(
                        {
                            "key": key,
                            "path": path,
                            "status": response.status_code,
                            "alive": response.status_code not in (403, 404, 405),
                            "snippet": snippet,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - 探测阶段需要吞掉所有异常
                    report.append(
                        {"key": key, "path": path, "status": None, "alive": False, "error": str(exc)}
                    )
        return report


    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        method: str = "POST",
        extra_headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        """发请求 + 网络层重试 + 解析 JSON。业务码判定交给 ``_handle_response``。"""
        body = urlencode(payload) if method == "POST" else None
        params = payload if method == "GET" else None
        return self._request(method, path, body=body, params=params, extra_headers=extra_headers)

    def _sms_v2_form(
        self,
        phone: str,
        *,
        dial_code: str = "86",
        identity: int = C.IDENTITY_JOB_SEEKER,
        solution: SliderSolution | None = None,
    ) -> dict[str, Any]:
        """按登录页 ``Ce(!0) + je()`` 的形状造出 ``send/smsCodeV2`` 的表单。

        来源是按需 chunk ``user-login.js``，不是猜的::

            Ce(t=true):  {...formData除 identity/phoneCode, pk, smsType:7,
                         purpose:identity, version:1}  + 票据
            je():       phone → encryptedAccount（AES，见 boss_login.crypto）

        所以表单里**没有** ``phone``，也**没有** ``verifyToken`` / ``captchaToken``。
        极验通道的票据只有三个键：``challenge`` / ``validate`` / ``seccode``。
        """
        region = dial_code if str(dial_code).startswith("+") else f"+{dial_code}"
        form: dict[str, Any] = {
            "regionCode": region,
            "pk": C.DEFAULT_PK,
            "smsType": C.SMS_TYPE_V2,
            "purpose": int(identity),
            "version": C.SMS_VERSION_V2,
            "encryptedAccount": encrypt_account(phone),
        }
        if solution is not None:
            form.update(solution.as_form_fields())
        return form

    def _retry_send_sms_v2(
        self,
        phone: str,
        *,
        dial_code: str = "86",
        identity: int = C.IDENTITY_JOB_SEEKER,
        solution: SliderSolution,
        on_event: EventHandler | None = None,
    ) -> ApiResponse:
        """带着极验票据，打 ``send/smsCodeV2`` 把被拦下的发码请求续上。

        三处都跟登录页对齐，错一处服务端就还是 400061：

        1. **路径**：极验通道走 ``/wapi/zppassport/send/smsCodeV2``（``B[1].smsCode``），
           不是 V1 的 ``send/smsCode``。
        2. **表单**：票据是 ``challenge`` / ``validate`` / ``seccode`` 三个键，
           叠在 V2 的表单形状上（``encryptedAccount`` / ``smsType=7`` / ``version=1`` …）。
        3. **不带 Zp-Captcha 请求头**：那套是 zpsecureflow validate 的形状；
           登录页的 ``requestHeaders`` 是空的，业务请求只把票据放表单。

        这里**不**调 validate——passport 族没有那条路由，而且极验票据一次性，
        交出去就没了。
        """
        path = self.endpoints.get("send_sms_code_v2") or C.ENDPOINTS["send_sms_code_v2"]
        body = self._sms_v2_form(
            phone, dial_code=dial_code, identity=identity, solution=solution
        )
        self._emit(
            on_event,
            "slider_retry",
            {"path": path, "has_ticket": bool(solution.validate)},
        )
        logger.debug("带滑块票据重试 %s", path)
        logger.debug("  表单键 %s", sorted(body))
        return self._post(path, body)

    def _login_v2_form(
        self,
        phone: str,
        code: str,
        *,
        dial_code: str = "86",
        identity: int = C.IDENTITY_JOB_SEEKER,
        solution: SliderSolution | None = None,
    ) -> dict[str, Any]:
        """按登录页 ``Ce() + je()`` 的形状造出 ``login/phoneV2`` 的表单。

        跟发码那张（``Ce(!0)``）只有两处差别，来源同样是 ``user-login.js``::

            Ce(t=false): {...formData除 identity/phoneCode, pk, smsType:7,
                          purpose:identity} + phoneCode   ← 没有 version:1
            je():        phone → encryptedAccount

        所以登录表单里**没有** ``version``、**没有** ``phone``，但多一个
        ``phoneCode``。票据仍是 ``challenge`` / ``validate`` / ``seccode`` 三键。
        ``fp``（浏览器指纹）那步在登录页是 try/catch，取不到就跳过——这里先不带。
        """
        region = dial_code if str(dial_code).startswith("+") else f"+{dial_code}"
        form: dict[str, Any] = {
            "regionCode": region,
            "pk": C.DEFAULT_PK,
            "smsType": C.SMS_TYPE_V2,  # Ce() 写死 7，登录也一样
            "purpose": int(identity),
            "phoneCode": code,
            "encryptedAccount": encrypt_account(phone),
        }
        if solution is not None:
            form.update(solution.as_form_fields())
        return form

    def _retry_login_v2(
        self,
        phone: str,
        code: str,
        *,
        dial_code: str = "86",
        identity: int = C.IDENTITY_JOB_SEEKER,
        solution: SliderSolution,
        on_event: EventHandler | None = None,
    ) -> ApiResponse:
        """带着极验票据，打 ``login/phoneV2`` 把被拦下的登录请求续上。

        路径 / 表单 / 请求头三处都跟登录页对齐，跟 :meth:`_retry_send_sms_v2`
        同一套规则：极验通道走 ``B[1].sms = login/phoneV2``，票据只以
        ``challenge`` / ``validate`` / ``seccode`` 挂在表单上，**不带**
        Zp-Captcha 请求头，**不调** validate。
        """
        path = self.endpoints.get("login_by_sms_v2") or C.ENDPOINTS["login_by_sms_v2"]
        body = self._login_v2_form(
            phone, code, dial_code=dial_code, identity=identity, solution=solution
        )
        self._emit(
            on_event,
            "slider_retry",
            {"path": path, "has_ticket": bool(solution.validate), "kind": "login"},
        )
        logger.debug("带滑块票据重试 %s", path)
        logger.debug("  表单键 %s", sorted(body))
        return self._post(path, body)

    def _get(self, path: str, params: Mapping[str, Any]) -> ApiResponse:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        action: str = "",
    ) -> ApiResponse:
        """发请求 + 网络层重试 + 解析 JSON。业务码判定交给 ``_handle_response``。

        :param extra_headers: 叠在默认请求头之上（滑块票据就走这里）。
        """
        url = self.base_url + path
        headers = {**C.DEFAULT_HEADERS, **(extra_headers or {})}
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            if attempt:
                delay = self.backoff * (2 ** (attempt - 1))
                logger.debug("第 %s 次重试 %s，等待 %.1fs", attempt, path, delay)
                self._sleep(delay)
            try:
                raw = self._http.request(
                    method,
                    url,
                    data=body,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = TransportError(f"网络请求失败：{exc}")
                continue

            status = getattr(raw, "status_code", 0)
            if status >= 500:
                last_error = TransportError(f"服务端错误：HTTP {status}")
                continue

            try:
                parsed = json.loads(raw.text)
            except (ValueError, AttributeError):
                snippet = (getattr(raw, "text", "") or "")[:160].replace("\n", " ")
                last_error = TransportError(
                    f"响应不是合法 JSON（HTTP {status}）：{snippet}"
                )
                continue

            return ApiResponse.from_payload(parsed, http_status=status)

        raise last_error or TransportError("请求失败")

    def _handle_response(self, response: ApiResponse, *, phone: str, action: str) -> None:
        """把服务端业务码翻译成对应的异常类型。"""
        if response.ok:
            return

        code = response.code
        message = response.message or C.CODE_MESSAGES.get(code, f"接口返回异常码 {code}")
        data = response.data

        # 风控：SECURITY_CHECK 会带出滑块参数 {seed, ts, name}
        if self._is_risk_control(code, message):
            if code in C.RISK_CODE_IP_BLOCK | C.RISK_CODE_UID_BLOCK:
                raise AccountBlocked(
                    code, message or "当前 IP 或账号已被封禁，请稍后再试", raw=data
                )
            raise RiskControlRequired(
                code,
                message or "请先完成安全验证",
                seed=str(data.get("seed") or ""),
                ts=str(data.get("ts") or ""),
                name=str(data.get("name") or ""),
                verify_page=f"{C.BASE_URL}/web/passport/zp/verify.html",
                raw=data,
            )

        # 限流：优先采用服务端给出的剩余秒数
        if code == 1001:
            retry_after = self._parse_int(data.get("retryAfter"), self.cooldown_seconds)
            self._tickets[phone] = SmsCodeTicket(
                phone_masked=mask_phone(phone),
                dial_code="",
                sent_at=self._clock(),
                retry_after=retry_after,
            )
            raise RateLimited(code, message, retry_after=retry_after, raw=data)

        # 验证码相关失败（仅登录接口会返回）
        if code in (2001, 2002, 2003):
            raise CodeRejected(code, message, raw=data)

        if code == 2004:
            raise AccountBlocked(code, message, raw=data)

        raise ApiError(code, f"[{action}] {message}", raw=data)


    @staticmethod
    def _scene_code(scene: str) -> int:
        if scene not in C.SMS_SCENES:
            raise ValidationError(f"不支持的短信场景：{scene}", field="scene")
        return C.SMS_SCENES[scene]

    @staticmethod
    def _is_risk_control(code: int, message: str) -> bool:
        """判断一次业务失败是不是风控拦下来的。

        先认码表，再认提示语。之所以要认提示语：滑块验证的码位不止一个（实测到
        400061，同类还有若干近亲），离线拿不到完整码表，而这类响应的话术是稳定的。
        关键字在 config 里收得很窄，不会误伤「验证码错误」这种普通业务失败。
        """
        if code in C.ALL_RISK_CODES:
            return True
        return any(keyword in message for keyword in C.RISK_MESSAGE_KEYWORDS)

    @staticmethod
    def _verify_token_payload(token: str) -> dict[str, Any]:
        """把人工验证票据塞进请求体。

        不同验证通道字段名不同，这里同时带上常见键，服务端按需取用。
        """
        return {
            "verifyToken": token,
            "validate": token,
            "NECaptchaValidate": token,
            "captchaToken": token,
        }

    @staticmethod
    def _parse_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _extract_token(data: Mapping[str, Any]) -> str:
        """从响应体里抠会话 token；再往里看一层，免得藏在 ``zpData.user.token``。

        ⚠️ 真实站点的 ``login/phoneV2`` 响应体里**没有** token，鉴权靠
        Set-Cookie——这里取到空是常态，不是失败。
        """
        for field in C.TOKEN_FIELDS:
            value = data.get(field)
            if value:
                return str(value)
        for value in data.values():
            if isinstance(value, Mapping):
                for field in C.TOKEN_FIELDS:
                    inner = value.get(field)
                    if inner:
                        return str(inner)
        return ""

    @staticmethod
    def _shape_of(data: Mapping[str, Any]) -> str:
        """把响应体的键名和值类型压成一行，方便远程排查契约差异。"""
        if not data:
            return "（空）"
        return ", ".join(f"{key}:{type(value).__name__}" for key, value in sorted(data.items()))

    def _collect_cookies(self) -> dict[str, str]:
        jar = getattr(self._http, "cookies", None)
        if jar is None:
            return {}
        if hasattr(jar, "get_dict"):
            try:
                return {str(k): str(v) for k, v in jar.get_dict().items()}
            except Exception:  # noqa: BLE001 - CookieJar 形态各异，取不到就算了
                return {}
        return {}

    def _cookie(self, name: str) -> str:
        return self._collect_cookies().get(name, "")

    @staticmethod
    def _emit(handler: EventHandler | None, name: str, payload: dict[str, Any]) -> None:
        if handler is not None:
            handler(name, payload)
