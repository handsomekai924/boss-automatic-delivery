"""APP 扫码登录 —— 复刻登录页 ``BossAppScan`` 的完整流程。

来源是按需 chunk ``user-login.5398946e.js``（zhipin-sign/v5330）里的
``BossAppScan`` 类和 ``SignLoginScanApp`` 组件，接口形状 2026-10-13 打过真实
站点核对。整条链是这样的::

    POST /wapi/zppassport/captcha/randkey
         └─ zpData.qrId = uuid          ← 二维码的内容就是这串 uuid
    ┌──────────────────────────────────────────────────────────┐
    │  页面上画二维码（content = secondUuid || uuid）            │
    │  二维码 30 秒后整段刷新（expireTime=3e4）                  │
    └──────────────────────────────────────────────────────────┘
    GET  /wapi/zppassport/qrcode/scan?uuid=          长轮询，等手机扫
         └─ {scaned:true, validate, allweb}
    GET  /wapi/zppassport/captcha/getSecondKey?uuid=
         └─ zpData.qrId = secondUuid    ← 换发第二把钥匙
    ┌── 1 秒兜底定时器 与  scanSecond 长轮询 赛跑 ──────────────┐
    │ GET /wapi/zppassport/qrcode/scanSecond?uuid=secondUuid    │
    │      └─ {scaned:true}  = App 端已确认                     │
    └──────────────────────────────────────────────────────────┘
    GET  /wapi/zppassport/qrcode/getHeadImg?qrId=    拿扫码账号的头像
         └─ zpData: {large, wxMp}
    GET  /wapi/zppassport/qrcode/scanLogin?qrId=     长轮询，等确认/取消
         └─ {scaned, login}   login=true 确认 / false 取消
    GET  /wapi/zppassport/qrcode/dispatcher?qrId=&pk=&fp=
         └─ code=0 + Set-Cookie         ← 登录态就在这一发上

几个跟短信登录不一样的点：

* **鉴权只靠 Set-Cookie**，``dispatcher`` 的响应体里照样没有会话 token，
  成功处理只读 ``zpData`` 的路由字段（``identity`` / ``toUrl`` / …）。
* ``scan`` / ``scanSecond`` / ``scanLogin`` 三个是**长轮询**，单次要挂到 100 秒
  （登录页 axios ``timeout: 1e5``）。超时是「还没轮到」，不是失败。
* ``scanLogin`` 在**还没被扫**时会立刻回 ``{scaned:true, login:false}``；
  真正进入「等 App 确认」状态后才会挂住。所以这一发只在 ``scan`` 说扫过之后
  才打——照登录页的调用顺序来，别提前问。
* ``fp``（浏览器指纹）那步在页面上是 try/catch，取不到就不带。这里同理，
  默认不带；真要带就用 :func:`boss_login.crypto.encrypt_text` 加密后传进来。
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

#: 事件回调签名：on_event(name: str, payload: dict) -> None
EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class QrTicket:
    """一次扫码会话的票据。"""

    #: 二维码内容。``getSecondKey`` 之前是 ``qrId``，之后换成 ``second_qr_id``
    #: （登录页 ``qrCodeContent = secondUuid || uuid``）。
    qr_id: str = ""
    second_qr_id: str = ""
    rand_key: str = ""
    secret_key: str = ""
    short_rand_key: str = ""
    created_at: float = field(default_factory=time.time)
    #: 本地展示层的过期时间，跟服务端票据寿命是两回事
    expires_at: float = 0.0

    @property
    def payload(self) -> str:
        """该画进二维码的字符串。"""
        if C.QR_CONTENT_PREFER_SECOND and self.second_qr_id:
            return self.second_qr_id
        return self.qr_id

    def is_expired(self, *, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True)
class QrScanOutcome:
    """``qrcode/scan`` 回执：手机已经扫过了。"""

    validate: str = ""
    allweb: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QrConfirmOutcome:
    """``qrcode/scanLogin`` 回执：App 端给了明确答复。"""

    confirmed: bool
    raw: dict[str, Any] = field(default_factory=dict)


class AppScanLogin:
    """APP 扫码登录流程。

    跟 :class:`~boss_login.client.ZhipinLoginClient` 共用同一套 HTTP 会话
    （Cookie 就靠它传递），但流程本身独立成类——扫码是「等手机那边动作」的
    异步链，跟短信那条「发码 → 人输码」的阻塞回调不是一回事。

    :param client:   已构造好的客户端，提供 ``_get`` / ``_post`` / Cookie 收集
    :param pk:       页面 ``pk``，登录页取自 ``#page_key_name``
    :param expire_seconds: 二维码展示层的存活时间（秒）
    :param poll_timeout:   长轮询单次请求超时（秒）
    :param sleeper:  休眠函数，测试时可注入空实现
    :param clock:    取时间戳的函数，测试时可注入可控时钟
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

    # ------------------------------------------------------------------ #
    # 一步一步来
    # ------------------------------------------------------------------ #

    def create(self) -> QrTicket:
        """取一把新的二维码票据（``captcha/randkey``）。

        页面那头是 ``refreshQRcode() → getRandomKey()``：请求体是**空的**——
        ``Ie`` 的构造器压根没把 ``pk`` 塞进去，别自作主张加上。
        """
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
        """长轮询 ``qrcode/scan``，直到手机真的扫了这一张。

        服务端没轮到会挂住；我们这边到点（``timeout``）就当成还没扫，
        由调用方决定刷新还是继续。页面上这张的展示寿命是 30 秒。
        """
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
            # 跟登录页一致：单次长轮询挂满 poll_timeout，整体预算在轮次之间卡。
            # 不把剩余预算折进单次超时——浏览器那头也是整发挂满的。
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
            # {scaned:false, msg:"timeout"} = 这一发没轮到，继续下一发

    def fetch_second_key(self) -> str:
        """扫过之后换发第二把钥匙（``captcha/getSecondKey``）。

        未扫码时服务端回 ``zpData:{code:1092}``（外层 ``code`` 仍是 0），
        所以要往 ``zpData`` 里再看一眼。拿到的 ``qrId`` 就是 ``secondUuid``，
        页面上二维码的内容也会跟着换成它（虽然那时 UI 已经不露二维码了）。
        """
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
        """拿扫码账号的头像（``qrcode/getHeadImg``）。拿不到不阻断登录。

        外层 ``code`` 是 0 不代表有头像——``zpData`` 里还会再套一层
        ``{code, msg}``，只有 ``zpData.large`` 才是真的图。
        """
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
        """长轮询 ``qrcode/scanLogin``，等 App 端确认或取消。

        **调用时机很重要**：``scanLogin`` 在二维码还没被扫时会立刻回
        ``{scaned:true, login:false}``（等同「取消」）。登录页只在 ``scan``
        说扫过、并且等过那 1 秒兜底之后才打这一发，这里保持一致。
        """
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
                # 登录页在这一支会继续递归轮询；少见，但别当成失败
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
        """收尾：打 ``qrcode/dispatcher`` 换登录态。

        表单形状跟登录页 ``scanJump()`` 对齐::

            {…extraForm, qrId: uuid, pk: …}
            + environment=BossZhipinBridge   （仅 App 内嵌 WebView）
            + fp=encrypt_text(fingerprint)   （可选，页面上是 try/catch）

        ``Set-Cookie`` 就在这一发的响应头上；``zpData`` 里是路由字段
        （``identity`` / ``toUrl`` / ``pcToUrl`` / …），登录页的成功处理
        只拿它们做跳转，不读 token。
        """
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
        self._cookies_before = cookies_before  # 供 build_result 用
        return response

    # ------------------------------------------------------------------ #
    # 串起来
    # ------------------------------------------------------------------ #

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
        """走完整条扫码登录链，返回 :class:`LoginResult`。

        :param on_payload: 每次二维码内容变化时的回调（``secondUuid`` 换发时
                           也会触发）。CLI 拿它重画二维码，网页拿它换图。
        :param on_event:   过程事件回调，事件名见下方 ``_emit`` 调用处。
        :param confirm_timeout: 等 App 确认的总预算（秒），默认 ``confirm_timeout``
        :param scan_timeout:    等手机扫码的总预算（秒），默认二维码存活时间
        :param should_cancel:   每步之间的取消检查，返回 ``True`` 就抛
                                :class:`BossLoginError` 退出。长轮询没法被外部
                                打断，只能在步与步之间认账。

        流程与登录页 ``BossAppScan`` 一一对应，顺序别调：

        1. ``create`` 取票据，把二维码内容交出去；
        2. ``wait_scanned`` 长轮询到手机扫了；
        3. ``fetch_second_key`` 换第二把钥匙（内容变了，再交一次）；
        4. 等 1 秒兜底（``QR_CONFIRM_DELAY_SECONDS``），或 ``scanSecond``
           先回 ``scaned``——两条路都是 ``getHeadImg`` + ``scanLogin``；
        5. ``scanLogin`` 回 ``login``：确认则 ``dispatcher``，取消则抛错；
        6. ``dispatcher`` 换到 ``Set-Cookie`` 就算登录成功。
        """

        def _check_cancel() -> None:
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
        """把 ``dispatcher`` 的回执装成 :class:`LoginResult`。

        跟短信那条一样：响应体里没有 token，凭证全在 ``Set-Cookie`` 上。
        拿不到 Cookie 就抛 :class:`LoginIncomplete`，把实到的形状打出来。
        """
        cookies = self._client._collect_cookies()
        cookies_before = getattr(self, "_cookies_before", {})
        new_cookies = {
            name: value
            for name, value in cookies.items()
            if cookies_before.get(name) != value
        }
        raw = dict(response.data)
        if scan is not None:
            # 登录页把 scan 回执的 validate/allweb 透传给成功处理，这里也留着
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
            # 交给 client 记账，is_logged_in / 落盘复用都靠这张名单
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

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _wait_confirm_window(
        self, *, on_event: EventHandler | None = None, timeout: float | None = None
    ) -> None:
        """复刻页面上「1 秒兜底定时器 与 scanSecond 赛跑」那一段。

        页面在 ``getSecondKey`` 之后起了个 1 秒定时器，到点就开始 ``scanLogin``；
        同时又打 ``scanSecond`` 长轮询，谁先到听谁的（``lock`` 防重入）。

        这里压成一发 ``scanSecond``，超时给到 1 秒（= 那个兜底定时器）：
        服务端在 App 确认前会挂住，所以正常就是「挂到 App 确认」或「1 秒超时」
        两种结果；真回了 ``{scaned:false}`` 也不重试——兜底时间已经到了，
        接下来 ``scanLogin`` 自己会挂着等确认，不会漏。
        """
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
            # 这一步是可选的加速通道，超时/断了都不该把登录掐死
            logger.debug("scanSecond 没等到回执，按兜底时间继续", exc_info=True)
            self._emit(on_event, "qr_confirm_window_done", {})
            return
        if response.flag("scaned"):
            self._emit(on_event, "qr_second_ok", dict(response.raw))
            return
        self._emit(on_event, "qr_confirm_window_done", {})

    def _require_ticket(self) -> QrTicket:
        if self.ticket is None or not self.ticket.qr_id:
            raise BossLoginError("还没有二维码票据，先调 create()")
        return self.ticket

    def _endpoint(self, key: str) -> str:
        endpoints = getattr(self._client, "endpoints", {}) or {}
        return endpoints.get(key) or C.QR_ENDPOINTS[key]

    @staticmethod
    def _raise_for_qr(response: ApiResponse, *, action: str) -> None:
        """扫码族的业务码判定。

        这一族接口的错误码跟短信登录不是一套（``1092`` = 还没扫、
        ``4563``/``4942-4950`` = 登录限额），所以**不**走
        ``_handle_response``——那张表是给发码/登录用的。
        """
        if response.ok:
            return
        code = response.code
        message = response.message or f"接口返回异常码 {code}"
        if code in C.QR_LOGIN_LIMIT_CODES or _in_limit_range(code):
            raise AccountBlocked(code, message or "本次登录受限，请稍后再试", raw=response.raw)
        raise ApiError(code, f"[{action}] {message}", raw=response.raw)

    @staticmethod
    def _shape(data: Mapping[str, Any]) -> str:
        if not data:
            return "（空）"
        return ", ".join(f"{k}:{type(v).__name__}" for k, v in sorted(data.items()))

    @staticmethod
    def _emit(handler: EventHandler | None, name: str, payload: dict[str, Any]) -> None:
        if handler is None:
            return
        try:
            handler(name, payload)
        except Exception:  # noqa: BLE001 - 事件回调不该打断登录
            logger.debug("事件回调 %s 抛了异常", name, exc_info=True)


def _in_limit_range(code: int) -> bool:
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
    """一步到位跑完整条 APP 扫码登录，适合脚本 / CLI 直接调。

    跟 :meth:`AppScanLogin.run` 等价，只是把 ``AppScanLogin(client, …)`` 的
    构造也一并包了。``**login_kwargs`` 会透传给 ``AppScanLogin.__init__``
    （``expire_seconds`` / ``poll_timeout`` / ``sleeper`` / ``clock`` …）。

    典型用法::

        result = run_qr_login(
            client,
            on_payload=lambda p: print(qr_terminal(p)),
            on_event=lambda n, d: print(n, d),
        )
    """
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
    """把二维码内容画成 PNG 字节，给网页 ``<img src="data:...">`` 用。

    依赖可选的 ``segno``（纯 Python，无其他依赖）。没装就抛
    :class:`RuntimeError`，让调用方自己决定降级成「只显示字符串」。
    """
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
    """同 :func:`render_qr_png`，输出 SVG 文本（矢量，缩放不糊）。"""
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
    """终端里能直接看的字符画二维码（Unicode 块元素，多数终端都认）。

    segno 的 ``terminal()`` 默认往 ``sys.stdout`` 打、返回 ``None``，所以这里
    先接进 ``StringIO`` 再交出去，免得调用方拿到 ``None``。
    """
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
