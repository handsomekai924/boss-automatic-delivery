"""短信登录客户端：发码 → 人工输码 → 登录 → 失败重试。

鉴权主要靠 `Set-Cookie`；滑块由本人在浏览器里拖官方组件完成。
流程与协议细节见 `notes/boss_login/client.md`。

Attributes:
    EventHandler: 事件回调：`on_event(name, payload)`。
    CodeProvider: 验证码输入回调：`(尝试次数, 票据) -> 验证码或 None`。
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

EventHandler = Callable[[str, dict[str, Any]], None]
CodeProvider = Callable[[int, "SmsCodeTicket | None"], "str | None"]

_CN_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_INTL_PHONE_RE = re.compile(r"^\d{5,15}$")
_NON_DIGIT_RE = re.compile(r"\D")


def normalize_phone(phone: str) -> str:
    """去掉空格、横线、括号等非数字字符。"""
    return _NON_DIGIT_RE.sub("", str(phone or ""))


def validate_phone(phone: str, dial_code: str = "86") -> None:
    """校验手机号格式，不合法则抛 `ValidationError`。"""
    if not phone:
        raise ValidationError("请输入手机号", field="phone")
    if dial_code == "86":
        if not _CN_PHONE_RE.match(phone):
            raise ValidationError("手机号格式不正确", field="phone")
    elif not _INTL_PHONE_RE.match(phone):
        raise ValidationError("手机号格式不正确，请检查国家区号", field="phone")


def validate_code(code: str, code_length: int = C.CODE_LENGTH) -> None:
    """校验验证码格式，不合法则抛 `ValidationError`。"""
    code = normalize_phone(code)
    if not code:
        raise ValidationError("请输入短信验证码", field="code")
    if len(code) != code_length:
        raise ValidationError(f"验证码为 {code_length} 位数字", field="code")


class ZhipinLoginClient:
    """BOSS直聘用户端短信登录客户端。

    Args:
        base_url: 站点根地址。
        endpoints: 覆盖 `config.ENDPOINTS` 中的接口路径。
        http: 具备 `request()` 的会话对象，默认新建 `requests.Session`。
        timeout: 单次请求超时（秒）。
        retries: 网络层失败后的重试次数（仅超时 / 5xx）。
        backoff: 重试退避基数（秒），按 2 的幂增长。
        cooldown_seconds: 本地重发冷却（秒）。
        code_length: 验证码位数。
        max_code_attempts: 同一号码连续输错几次后放弃。
        sleeper: 休眠函数，测试时可注入。
        clock: 取时间戳的函数，测试时可注入。
        headers: 叠加到默认请求头之上。

    Attributes:
        base_url: 站点根地址。
        endpoints: 合并后的接口路径表。
        timeout: 单次请求超时（秒）。
        retries: 网络层重试次数。
        backoff: 重试退避基数。
        cooldown_seconds: 本地重发冷却（秒）。
        code_length: 验证码位数。
        max_code_attempts: 验证码最大尝试次数。
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
        """初始化。"""
        self.base_url = base_url.rstrip("/")
        self.endpoints = {
            **C.ENDPOINTS,
            **C.VERIFY_ENDPOINTS,
            **C.QR_ENDPOINTS,
            **(endpoints or {}),
        }
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
        if hasattr(self._http, "headers"):
            self._http.headers.update(merged_headers)

        self._tickets: dict[str, SmsCodeTicket] = {}
        self._slider_solutions: dict[str, SliderSolution] = {}
        self._sms_tokens: dict[str, str] = {}
        self._auth_token: str = ""
        self._session_cookie_names: set[str] = set()

    @property
    def http(self) -> Any:
        """底层会话，便于外部直接复用 Cookie。"""
        return self._http

    def last_ticket(self, phone: str) -> SmsCodeTicket | None:
        """取该手机号最近一次发送回执。"""
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

        手机号不合法抛 `ValidationError`；冷却中抛 `RateLimited`；
        风控抛 `RiskControlRequired` / `AccountBlocked`。

        Args:
            phone: 手机号（自动清洗格式）。
            dial_code: 国家区号，默认 `86`。
            scene: `login` / `register` / `bind`。
            identity: `0` 求职者 / `1` 招聘者。
            verify_token: 命中风控后回传的人工验证票据。
            force: 忽略本地冷却强制发送。
            slider_solver: 命中滑块时的解题回调。
            on_event: 过程事件回调。
        """
        phone = normalize_phone(phone)
        validate_phone(phone, dial_code)
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
        """本地冷却拦截，仍在冷却期时抛 `RateLimited`。"""
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
        """用手机号 + 短信验证码登录。

        格式不合法抛 `ValidationError`；验证码失败抛 `CodeRejected`；
        风控抛 `RiskControlRequired` / `AccountBlocked`。

        Args:
            phone: 手机号。
            code: 短信验证码。
            dial_code: 国家区号。
            scene: 短信场景。
            identity: `0` 求职者 / `1` 招聘者。
            verify_token: 人工验证票据。
            slider_solver: 命中滑块时的解题回调。
            solution: 现成滑块票据；缺省先复用发码那次解出的那张。
            on_event: 过程事件回调。
        """
        phone = normalize_phone(phone)
        code = normalize_phone(code)
        validate_phone(phone, dial_code)
        validate_code(code, self.code_length)
        sms_type = self._scene_code(scene)

        if solution is None:
            solution = self._slider_solutions.get(phone)

        self._call_login_suggest(phone, code, dial_code=dial_code)
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
        self._tickets.pop(phone, None)
        self._slider_solutions.pop(phone, None)
        self._sms_tokens.pop(phone, None)
        logger.info("登录成功：%s", user.name or user.user_id or result.user.phone_masked)
        return result

    def _call_login_suggest(self, phone: str, code: str, *, dial_code: str = "86") -> None:
        """登录前置检查 `getLoginSuggest`，失败不阻断登录。"""
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
        """串起完整登录流程：发码 → 人工输码 → 登录 → 失败重试。

        Args:
            phone: 手机号。
            code_provider: 索要验证码的回调；返回 `None` 表示放弃。
            dial_code: 国家区号。
            scene: 短信场景。
            verify_token: 人工验证票据。
            slider_solver: 命中滑块时的解题回调。
            on_event: 过程事件回调。
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
        """验证码过期后的重发：先等到冷却结束，避免又撞限流。"""
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
        """拉取当前登录用户信息；未登录抛 `SessionExpired`。"""
        response = self._post(self.endpoints["user_info"], {}, method="GET")
        if not response.ok:
            raise SessionExpired(response.message or "未登录或登录态已失效", raw=response.data)
        return UserInfo.from_data(response.data)

    def is_logged_in(self) -> bool:
        """本地是否持有登录态（轻量判断，不发请求）。"""
        if self._auth_token:
            return True
        cookies = self._collect_cookies()
        if any(name in cookies for name in C.AUTH_COOKIES):
            return True
        return any(name in cookies for name in self._session_cookie_names)

    def set_auth_token(self, token: str) -> None:
        """注入或清空会话 token。"""
        self._auth_token = token or ""

    def set_session_cookies(self, cookies: Mapping[str, str]) -> None:
        """登记这些 Cookie 名是登录凭证，落盘复用时也要认。"""
        self._session_cookie_names = {str(name) for name in cookies}

    @property
    def auth_token(self) -> str:
        """当前会话 token。"""
        return self._auth_token

    def logout(self) -> None:
        """退出登录并清空本地登录态；服务端失败不阻断本地清理。"""
        try:
            self._post(self.endpoints["logout"], {})
        except BossLoginError as exc:
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
        """向服务端要一次滑块挑战；失败抛 `ApiError`。

        Args:
            scene: 场景码。
            endpoint_key: 挑战接口 key，须与业务接口同族。
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
        """拉挑战并由本人解题，不调 validate。

        Args:
            solver: 解题回调；缺省拉起本地帮助页。
            scene: 场景码。
            endpoint_key: 挑战接口 key。
            on_event: 过程事件回调。
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
            solution = SliderSolution(
                challenge=solution.challenge,
                validate=solution.validate,
                seccode=solution.seccode,
                rand_key=challenge.rand_key,
                captcha_type=solution.captcha_type or challenge.captcha_type,
            )
        return solution

    def submit_slider_solution(self, solution: SliderSolution) -> ApiResponse:
        """把票据交给 zpsecureflow 的 validate；业务链不要调它，失败抛 `ApiError`。"""
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
        """verify.html 那条链：拉挑战 → 人机解题 → 交 zpsecureflow validate。"""
        solution = self.solve_slider(
            solver=solver,
            scene=scene,
            endpoint_key="get_type",
            on_event=on_event,
        )
        self.submit_slider_solution(solution)
        self._emit(on_event, "slider_passed", {"challenge": solution.challenge})
        return solution

    def _is_slider_challenge(self, code: int, message: str, data: Mapping[str, Any]) -> bool:
        """区分「滑块」与其余风控；滑块可人机协作，其余交给人处理。"""
        if code in C.RISK_CODE_IP_BLOCK | C.RISK_CODE_UID_BLOCK:
            return False
        if code in C.RISK_CODE_SLIDER:
            return True
        return any(keyword in message for keyword in C.SLIDER_MESSAGE_KEYWORDS)

    def probe_endpoints(self, *, keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """逐个试探候选接口，帮助确认线上真实路径。"""
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
        timeout: float | None = None,
    ) -> ApiResponse:
        """发请求 + 网络层重试 + 解析 JSON。业务码判定交给 `_handle_response`。"""
        body = urlencode(payload) if method == "POST" else None
        params = payload if method == "GET" else None
        return self._request(
            method,
            path,
            body=body,
            params=params,
            extra_headers=extra_headers,
            timeout=timeout,
        )

    def _sms_v2_form(
        self,
        phone: str,
        *,
        dial_code: str = "86",
        identity: int = C.IDENTITY_JOB_SEEKER,
        solution: SliderSolution | None = None,
    ) -> dict[str, Any]:
        """按登录页 `Ce(!0) + je()` 的形状造出 `send/smsCodeV2` 的表单。"""
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
        """带着极验票据打 `send/smsCodeV2`，把被拦下的发码请求续上。"""
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
        """按登录页 `Ce() + je()` 的形状造出 `login/phoneV2` 的表单。"""
        region = dial_code if str(dial_code).startswith("+") else f"+{dial_code}"
        form: dict[str, Any] = {
            "regionCode": region,
            "pk": C.DEFAULT_PK,
            "smsType": C.SMS_TYPE_V2,
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
        """带着极验票据打 `login/phoneV2`，把被拦下的登录请求续上。"""
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

    def _get(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> ApiResponse:
        """发 GET 请求并解析响应。"""
        return self._request("GET", path, params=params, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        action: str = "",
        timeout: float | None = None,
    ) -> ApiResponse:
        """发请求 + 网络层重试 + 解析 JSON；业务码判定交给 `_handle_response`。

        Args:
            extra_headers: 叠在默认请求头之上。
            timeout: 覆盖实例级超时，长轮询接口会挂 100 秒。
        """
        url = self.base_url + path
        headers = {**C.DEFAULT_HEADERS, **(extra_headers or {})}
        last_error: Exception | None = None
        request_timeout = self.timeout if timeout is None else timeout

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
                    timeout=request_timeout,
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

        if code == 1001:
            retry_after = self._parse_int(data.get("retryAfter"), self.cooldown_seconds)
            self._tickets[phone] = SmsCodeTicket(
                phone_masked=mask_phone(phone),
                dial_code="",
                sent_at=self._clock(),
                retry_after=retry_after,
            )
            raise RateLimited(code, message, retry_after=retry_after, raw=data)

        if code in (2001, 2002, 2003):
            raise CodeRejected(code, message, raw=data)

        if code == 2004:
            raise AccountBlocked(code, message, raw=data)

        raise ApiError(code, f"[{action}] {message}", raw=data)

    @staticmethod
    def _scene_code(scene: str) -> int:
        """场景名转 smsType；未知场景抛 `ValidationError`。"""
        if scene not in C.SMS_SCENES:
            raise ValidationError(f"不支持的短信场景：{scene}", field="scene")
        return C.SMS_SCENES[scene]

    @staticmethod
    def _is_risk_control(code: int, message: str) -> bool:
        """先认码表，再认提示语，判断是否风控拦下。"""
        if code in C.ALL_RISK_CODES:
            return True
        return any(keyword in message for keyword in C.RISK_MESSAGE_KEYWORDS)

    @staticmethod
    def _verify_token_payload(token: str) -> dict[str, Any]:
        """把人工验证票据塞进请求体的常见键位。"""
        return {
            "verifyToken": token,
            "validate": token,
            "NECaptchaValidate": token,
            "captchaToken": token,
        }

    @staticmethod
    def _parse_int(value: Any, default: int) -> int:
        """转成正整数；失败或非正数时用默认值。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _extract_token(data: Mapping[str, Any]) -> str:
        """从响应体里抠会话 token；取不到是常态（真实站点靠 Cookie）。"""
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
        """把响应体的键名和值类型压成一行，方便排查契约差异。"""
        if not data:
            return "（空）"
        return ", ".join(f"{key}:{type(value).__name__}" for key, value in sorted(data.items()))

    def _collect_cookies(self) -> dict[str, str]:
        """取出当前 Cookie 罐里的全部 Cookie。"""
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
        """按名取单个 Cookie 值。"""
        return self._collect_cookies().get(name, "")

    @staticmethod
    def _emit(handler: EventHandler | None, name: str, payload: dict[str, Any]) -> None:
        """发过程事件。"""
        if handler is not None:
            handler(name, payload)
