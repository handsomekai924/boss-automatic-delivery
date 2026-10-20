"""APP 扫码登录：取票据 → 等手机扫 → App 确认 → 换登录态。

鉴权只靠 `Set-Cookie`；长轮询接口的超时是「还没轮到」，不是失败。
协议细节见 `notes/boss_login/qrcode.md`。

Attributes:
    EventHandler: 过程事件回调：`on_event(name, payload)`。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import config as C
from .errors import (
    AccountBlocked,
    ApiError,
    BossLoginError,
    LoginIncomplete,
    TransportError,
)
from .models import ApiResponse, LoginResult, UserInfo

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class QrTicket:
    """一次扫码会话的票据。

    Attributes:
        qr_id: 首把钥匙（uuid），二维码最初的内容。
        second_qr_id: 第二把钥匙；换发后优先画进二维码。
        rand_key: 随机钥匙。
        secret_key: 密钥。
        short_rand_key: 短随机钥匙。
        created_at: 创建时刻。
        expires_at: 本地展示层过期时间，与服务端票据寿命无关。
    """

    qr_id: str = ""
    second_qr_id: str = ""
    rand_key: str = ""
    secret_key: str = ""
    short_rand_key: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def payload(self) -> str:
        """该画进二维码的字符串。"""
        if C.QR_CONTENT_PREFER_SECOND and self.second_qr_id:
            return self.second_qr_id
        return self.qr_id

    def is_expired(self, *, now: float | None = None) -> bool:
        """展示层是否已过期；未设过期时间则永不过期。"""
        if not self.expires_at:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True)
class QrScanOutcome:
    """`qrcode/scan` 回执：手机已经扫过了。

    Attributes:
        validate: 验证票据。
        allweb: 是否全站通行。
        raw: 原始响应。
    """

    validate: str = ""
    allweb: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QrConfirmOutcome:
    """`qrcode/scanLogin` 回执：App 端给了明确答复。

    Attributes:
        confirmed: 是否确认登录。
        raw: 原始响应。
    """

    confirmed: bool
    raw: dict[str, Any] = field(default_factory=dict)


class AppScanLogin:
    """APP 扫码登录流程，与短信客户端共用 HTTP 会话。

    Args:
        client: 已构造好的客户端，提供 `_get` / `_post` / Cookie 收集。
        pk: 页面 `pk`。
        expire_seconds: 二维码展示层存活时间（秒）。
        poll_timeout: 长轮询单次请求超时（秒）。
        confirm_timeout: 等 App 确认的总预算（秒）。
        sleeper: 休眠函数，测试时可注入。
        clock: 取时间戳的函数，测试时可注入。
    """

    def __init__(
        self,
        client: Any,
        *,
        pk: str = C.QR_DEFAULT_PK,
        expire_seconds: int = C.QR_EXPIRE_SECONDS,
        poll_timeout: float = C.QR_POLL_TIMEOUT,
        confirm_timeout: float = C.QR_CONFIRM_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化。"""
        self._client = client
        self.pk = pk
        self.expire_seconds = expire_seconds
        self.poll_timeout = poll_timeout
        self.confirm_timeout = confirm_timeout
        self._sleep = sleeper
        self._clock = clock

        self.ticket: QrTicket | None = None
        self.head_img: str = ""
        self.watched_mp: bool = False

    def create(self) -> QrTicket:
        """取一把新的二维码票据（`captcha/randkey`）；缺 `qrId` 时抛 `LoginIncomplete`。"""
        response = self._client._post(self._endpoint("qr_randkey"), {}, timeout=15.0)
        self._raise_for_qr(response, action="qr_randkey")
        data = response.data
        qr_id = str(data.get("qrId") or "")
        if not qr_id:
            raise LoginIncomplete(
                "randkey 回了成功码，但 zpData 里没有 qrId，拿不到二维码内容。\n"
                f"  zpData 的键：{sorted(data) or '（空）'}\n"
                f"  原始载荷：{response.raw}",
                raw=response.raw,
            )

        now = self._clock()
        self.ticket = QrTicket(
            qr_id=qr_id,
            rand_key=str(data.get("randKey") or ""),
            secret_key=str(data.get("secretKey") or ""),
            short_rand_key=str(data.get("shortRandKey") or ""),
            created_at=now,
            expires_at=now + self.expire_seconds,
        )
        self.head_img = ""
        self.watched_mp = False
        logger.info("扫码票据已创建 qrId=%s", qr_id)
        return self.ticket

    @property
    def payload(self) -> str:
        """当前该画进二维码的字符串。"""
        return self.ticket.payload if self.ticket else ""

    def wait_scanned(self, *, timeout: float | None = None) -> QrScanOutcome:
        """长轮询 `qrcode/scan` 直到手机扫了；超时抛 `BossLoginError`。"""
        ticket = self._require_ticket()
        deadline = self._clock() + (
            self.expire_seconds if timeout is None else timeout
        )
        while True:
            if ticket.is_expired(now=self._clock()) and timeout is None:
                raise BossLoginError("二维码已过期，请刷新后重新扫码")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise BossLoginError("等待扫码超时，请刷新二维码后重试")
            response = self._client._get(
                self._endpoint("qr_scan"),
                {"uuid": ticket.qr_id},
                timeout=self.poll_timeout,
            )
            self._raise_for_qr(response, action="qr_scan")
            if response.flag("scaned"):
                outcome = QrScanOutcome(
                    validate=str(response.raw.get("validate") or ""),
                    allweb=bool(response.raw.get("allweb")),
                    raw=dict(response.raw),
                )
                logger.info("二维码已被扫码 validate=%s", bool(outcome.validate))
                return outcome

    def fetch_second_key(self) -> str:
        """扫过之后换发第二把钥匙（`captcha/getSecondKey`）；失败抛 `ApiError`。"""
        ticket = self._require_ticket()
        response = self._client._get(
            self._endpoint("qr_second_key"),
            {"uuid": ticket.qr_id},
            timeout=15.0,
        )
        self._raise_for_qr(response, action="qr_second_key")
        second = str(response.data.get("qrId") or "")
        inner_code = response.data.get("code")
        if not second:
            raise ApiError(
                int(inner_code) if str(inner_code).isdigit() else response.code,
                "getSecondKey 没换到第二把钥匙"
                f"（zpData={self._shape(response.data)}）",
                raw=response.raw,
            )
        ticket.second_qr_id = second
        logger.debug("第二把钥匙已换发 secondUuid=%s", second)
        return second

    def fetch_head_img(self) -> str:
        """拿扫码账号头像（`qrcode/getHeadImg`）；拿不到不阻断登录。"""
        ticket = self._require_ticket()
        response = self._client._get(
            self._endpoint("qr_head_img"),
            {"qrId": ticket.qr_id},
            timeout=15.0,
        )
        self._raise_for_qr(response, action="qr_head_img")
        data = response.data
        large = str(data.get("large") or "")
        self.watched_mp = bool(data.get("wxMp"))
        if large:
            self.head_img = large
        else:
            logger.debug("拿不到头像：%s", self._shape(data))
        return self.head_img

    def wait_confirmed(self, *, timeout: float | None = None) -> QrConfirmOutcome:
        """长轮询 `qrcode/scanLogin` 等 App 确认或取消；须在手机扫过之后再调。"""
        ticket = self._require_ticket()
        budget = self.confirm_timeout if timeout is None else timeout
        deadline = self._clock() + budget
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise BossLoginError("等待 App 端确认超时，请重新扫码")
            response = self._client._get(
                self._endpoint("qr_scan_login"),
                {"qrId": ticket.qr_id},
                timeout=self.poll_timeout,
            )
            self._raise_for_qr(response, action="qr_scan_login")
            if not response.flag("scaned"):
                continue
            confirmed = bool(response.raw.get("login"))
            logger.info("App 端已答复：%s", "确认登录" if confirmed else "取消登录")
            return QrConfirmOutcome(confirmed=confirmed, raw=dict(response.raw))

    def dispatch(
        self,
        *,
        fingerprint: str = "",
        extra_params: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        """打 `qrcode/dispatcher` 换登录态；`Set-Cookie` 在这一发的响应头上。"""
        ticket = self._require_ticket()
        params: dict[str, Any] = {
            "qrId": ticket.qr_id,
            "pk": self.pk,
        }
        if extra_params:
            params.update({k: v for k, v in extra_params.items() if v not in (None, "")})
        if fingerprint:
            from .crypto import encrypt_text

            params["fp"] = encrypt_text(fingerprint)

        cookies_before = self._client._collect_cookies()
        response = self._client._get(
            self._endpoint("qr_dispatcher"), params, timeout=15.0
        )
        if not response.ok:
            self._raise_for_qr(response, action="qr_dispatcher")
        self._cookies_before = cookies_before
        return response

    def run(
        self,
        *,
        on_event: EventHandler | None = None,
        on_payload: Callable[[str], None] | None = None,
        fingerprint: str = "",
        confirm_timeout: float | None = None,
        scan_timeout: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> LoginResult:
        """走完整条扫码登录链，返回 `LoginResult`。

        顺序为 create → wait_scanned → fetch_second_key → 确认窗口 →
        head_img → wait_confirmed → dispatcher；取消或超时抛 `BossLoginError`。

        Args:
            on_event: 过程事件回调。
            on_payload: 二维码内容变化时的回调。
            fingerprint: 可选浏览器指纹，加密后作为 `fp`。
            confirm_timeout: 等 App 确认的总预算（秒）。
            scan_timeout: 等手机扫码的总预算（秒）。
            should_cancel: 每步之间的取消检查，返回 `True` 则退出。
        """

        def _check_cancel() -> None:
            """按 `should_cancel` 请求取消。"""
            if should_cancel is not None and should_cancel():
                raise BossLoginError("已取消登录")

        ticket = self.create()
        _check_cancel()
        if on_payload:
            on_payload(ticket.payload)
        self._emit(on_event, "qr_created", {"payload": ticket.payload, "pk": self.pk})

        try:
            scan = self.wait_scanned(timeout=scan_timeout)
        except BossLoginError as exc:
            self._emit(on_event, "qr_expired", {"message": str(exc)})
            raise

        _check_cancel()
        self.fetch_second_key()
        if on_payload:
            on_payload(ticket.payload)
        self._emit(
            on_event,
            "qr_scanned",
            {
                "validate": scan.validate,
                "allweb": scan.allweb,
                "payload": ticket.payload,
            },
        )

        self._wait_confirm_window(on_event=on_event, timeout=confirm_timeout)
        _check_cancel()
        self.fetch_head_img()
        self._emit(
            on_event,
            "qr_head",
            {"head_img": self.head_img, "wx_mp": self.watched_mp},
        )

        confirm = self.wait_confirmed(timeout=confirm_timeout)
        _check_cancel()
        if not confirm.confirmed:
            self._emit(on_event, "qr_cancelled", dict(confirm.raw))
            raise BossLoginError("已在 App 端取消登录")
        self._emit(on_event, "qr_confirmed", dict(confirm.raw))

        result = self.dispatch(fingerprint=fingerprint)
        login = self.build_result(result, scan=scan)
        self._emit(
            on_event,
            "success",
            {"user": login.user.name or login.user.user_id},
        )
        return login

    def build_result(
        self, response: ApiResponse, *, scan: QrScanOutcome | None = None
    ) -> LoginResult:
        """把 `dispatcher` 的回执装成 `LoginResult`；没换到凭证时抛 `LoginIncomplete`。"""
        cookies = self._client._collect_cookies()
        cookies_before = getattr(self, "_cookies_before", {})
        new_cookies = {
            name: value
            for name, value in cookies.items()
            if cookies_before.get(name) != value
        }
        raw = dict(response.data)
        if scan is not None:
            raw.setdefault("validate", scan.validate)
            raw.setdefault("allweb", scan.allweb)

        user = UserInfo.from_data(raw)
        result = LoginResult(
            token=self._client._extract_token(response.data),
            user=user,
            is_new_user=bool(response.data.get("isNewUser") or response.data.get("newUser")),
            cookies=cookies,
            raw=raw,
            new_cookies=new_cookies,
        )
        if not result.logged_in:
            raise LoginIncomplete(
                "dispatcher 回了成功码，但既没有 token，响应也没种下任何 Cookie，"
                "拿不到可用于后续请求的凭证。\n"
                f"  响应 zpData 的键：{self._shape(response.data)}\n"
                f"  Cookie 里现有的键：{sorted(cookies) or '（空）'}\n"
                "  扫码登录的登录态本来就在 Set-Cookie 上，这里没收到 Cookie，"
                "说明 dispatcher 那一发没换到登录态。",
                raw=response.raw,
            )

        names = set(new_cookies) or {n for n in cookies}
        if names:
            self._client._session_cookie_names |= set(new_cookies) or {
                n for n in cookies if n in C.AUTH_COOKIES
            }
        token = result.token
        if token:
            self._client.set_auth_token(token)
        logger.info(
            "扫码登录成功：%s", user.name or user.user_id or "(未取到昵称)"
        )
        return result

    def _wait_confirm_window(
        self, *, on_event: EventHandler | None = None, timeout: float | None = None
    ) -> None:
        """用一发 `scanSecond` 复刻「1 秒兜底 vs 二次确认」赛跑，失败不阻断登录。"""
        ticket = self._require_ticket()
        if not ticket.second_qr_id:
            return

        delay = C.QR_CONFIRM_DELAY_SECONDS
        self._emit(on_event, "qr_waiting_confirm", {"delay": delay})
        try:
            response = self._client._get(
                self._endpoint("qr_scan_second"),
                {"uuid": ticket.second_qr_id},
                timeout=delay if timeout is None else timeout,
            )
        except TransportError:
            logger.debug("scanSecond 没等到回执，按兜底时间继续", exc_info=True)
            self._emit(on_event, "qr_confirm_window_done", {})
            return
        if response.flag("scaned"):
            self._emit(on_event, "qr_second_ok", dict(response.raw))
            return
        self._emit(on_event, "qr_confirm_window_done", {})

    def _require_ticket(self) -> QrTicket:
        """取出当前票据；尚未创建时抛 `BossLoginError`。"""
        if self.ticket is None or not self.ticket.qr_id:
            raise BossLoginError("还没有二维码票据，先调 create()")
        return self.ticket

    def _endpoint(self, key: str) -> str:
        """按 key 取接口路径，客户端可覆盖。"""
        endpoints = getattr(self._client, "endpoints", {}) or {}
        return endpoints.get(key) or C.QR_ENDPOINTS[key]

    @staticmethod
    def _raise_for_qr(response: ApiResponse, *, action: str) -> None:
        """扫码族业务码判定：限额码抛 `AccountBlocked`，其余抛 `ApiError`。"""
        if response.ok:
            return
        code = response.code
        message = response.message or f"接口返回异常码 {code}"
        if code in C.QR_LOGIN_LIMIT_CODES or _in_limit_range(code):
            raise AccountBlocked(code, message or "本次登录受限，请稍后再试", raw=response.raw)
        raise ApiError(code, f"[{action}] {message}", raw=response.raw)

    @staticmethod
    def _shape(data: Mapping[str, Any]) -> str:
        """把字典收成 `键:类型` 列表，便于错误信息里展示形状。"""
        if not data:
            return "（空）"
        return ", ".join(f"{k}:{type(v).__name__}" for k, v in sorted(data.items()))

    @staticmethod
    def _emit(handler: EventHandler | None, name: str, payload: dict[str, Any]) -> None:
        """发事件；回调异常只记日志，不打断登录。"""
        if handler is None:
            return
        try:
            handler(name, payload)
        except Exception:  # noqa: BLE001 - 事件回调不该打断登录
            logger.debug("事件回调 %s 抛了异常", name, exc_info=True)


def _in_limit_range(code: int) -> bool:
    """业务码是否落在登录限额区间。"""
    low, high = C.QR_LOGIN_LIMIT_RANGE
    return low <= code <= high


def run_qr_login(
    client: Any,
    *,
    pk: str = C.QR_DEFAULT_PK,
    on_event: EventHandler | None = None,
    on_payload: Callable[[str], None] | None = None,
    fingerprint: str = "",
    confirm_timeout: float | None = None,
    scan_timeout: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    **login_kwargs: Any,
) -> LoginResult:
    """一步到位跑完整条 APP 扫码登录，适合脚本 / CLI 直接调。"""
    login = AppScanLogin(client, pk=pk, **login_kwargs)
    return login.run(
        on_event=on_event,
        on_payload=on_payload,
        fingerprint=fingerprint,
        confirm_timeout=confirm_timeout,
        scan_timeout=scan_timeout,
        should_cancel=should_cancel,
    )


def render_qr_png(payload: str, *, scale: int = 8, border: int = 2) -> bytes:
    """把二维码内容画成 PNG 字节；未装 `segno` 时抛 `RuntimeError`。"""
    if not payload:
        raise ValueError("二维码内容为空")
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "生成二维码图片需要 segno：pip install segno"
        ) from exc

    import io

    qr = segno.make(payload, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border)
    return buf.getvalue()


def render_qr_svg(payload: str, *, scale: int = 8, border: int = 2) -> str:
    """同 `render_qr_png`，输出 SVG 文本；未装 `segno` 时抛 `RuntimeError`。"""
    if not payload:
        raise ValueError("二维码内容为空")
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "生成二维码图片需要 segno：pip install segno"
        ) from exc

    qr = segno.make(payload, error="m")
    return qr.svg_inline(scale=scale, border=border)


def render_qr_terminal(payload: str, *, border: int = 2) -> str:
    """终端字符画二维码；未装 `segno` 时抛 `RuntimeError`。"""
    if not payload:
        raise ValueError("二维码内容为空")
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "生成二维码图片需要 segno：pip install segno"
        ) from exc

    import io

    qr = segno.make(payload, error="m")
    buf = io.StringIO()
    qr.terminal(out=buf, compact=True, border=border)
    return buf.getvalue()
