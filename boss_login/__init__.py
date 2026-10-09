"""BOSS直聘用户端「手机验证码登录」Python 客户端。

>>> from boss_login import create_client
>>> client = create_client()
>>> client.send_sms_code("13800138000")          # 下发验证码
SmsCodeTicket(phone_masked='138****8000', ...)
>>> client.login_by_sms("13800138000", "123456") # 人工填入收到的验证码
LoginResult(token='...', user=UserInfo(...))

配合 CLI 使用更省事::

    python -m boss_login login --phone 13800138000
"""

from __future__ import annotations

from pathlib import Path

from .client import (
    ZhipinLoginClient,
    normalize_phone,
    validate_code,
    validate_phone,
)
from .config import BASE_URL, CODE_LENGTH, ENDPOINTS, RESEND_COOLDOWN_SECONDS, SMS_SCENES
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
from .session import (
    DEFAULT_DB_PATH,
    StoredSession,
    clear_session,
    load_session,
    save_session,
)
from .verify import (
    SliderChallenge,
    SliderHelperError,
    SliderSolution,
    SliderSolver,
    build_helper_html,
    generate_trace_id,
    parse_challenge,
    parse_solution,
    solve_via_helper,
    validate_request_headers,
)

__version__ = "1.1.0"

__all__ = [
    # 核心
    "ZhipinLoginClient",
    "create_client",
    "persist_login",
    # 模型
    "ApiResponse",
    "LoginResult",
    "SmsCodeTicket",
    "SliderChallenge",
    "SliderHelperError",
    "SliderSolution",
    "StoredSession",
    "UserInfo",
    # 异常
    "AccountBlocked",
    "ApiError",
    "BossLoginError",
    "CodeRejected",
    "LoginIncomplete",
    "RateLimited",
    "RiskControlRequired",
    "SessionExpired",
    "TransportError",
    "ValidationError",
    # 工具
    "SliderSolver",
    "build_helper_html",
    "generate_trace_id",
    "normalize_phone",
    "parse_challenge",
    "parse_solution",
    "solve_via_helper",
    "validate_code",
    "validate_phone",
    "validate_request_headers",
    "mask_phone",
    "clear_session",
    "load_session",
    "save_session",
    # 常量
    "BASE_URL",
    "CODE_LENGTH",
    "DEFAULT_DB_PATH",
    "ENDPOINTS",
    "RESEND_COOLDOWN_SECONDS",
    "SMS_SCENES",
    "__version__",
]


def create_client(
    *,
    session_path: Path | str | None = None,
    load_stored: bool = True,
    **client_kwargs,
) -> ZhipinLoginClient:
    """创建客户端，并把上次保存的 Cookie / token 注入会话。

    :param session_path: 状态库路径；省略 = ``BOSS_DB`` = ``data/boss.db``
    :param load_stored: 为 ``False`` 时忽略本地登录态，强制走新的登录流程。
    """
    client = ZhipinLoginClient(**client_kwargs)
    if load_stored:
        stored = load_session(session_path)
        if not stored.is_empty:
            for name, value in stored.cookies.items():
                client.http.cookies.set(name, value)
            # 落盘的会话可能只剩 token（真实站点鉴权靠 Cookie，但假服务端
            # 之类会把 token 放响应体）。两种都得算登录态。
            if stored.token:
                client.set_auth_token(stored.token)
            # 落盘时已经认定是登录凭证的 Cookie 名，复用时也得认——名字可能
            # 不在 AUTH_COOKIES 里（真实站点 Set-Cookie 的名字会变）。
            client.set_session_cookies(stored.cookies)
    return client


def persist_login(
    client: ZhipinLoginClient,
    result: LoginResult,
    *,
    session_path: Path | str | None = None,
) -> Path:
    """把登录结果落盘到状态库（token + Cookie + 脱敏手机号），供下次复用。

    :param session_path: 状态库路径；省略 = ``BOSS_DB`` = ``data/boss.db``
    """
    cookies = result.cookies or _cookies_of(client)
    return save_session(
        StoredSession(
            token=result.token,
            cookies=cookies,
            phone_masked=result.user.phone_masked,
            user={
                "user_id": result.user.user_id,
                "name": result.user.name,
                "identity": result.user.identity,
                "is_new_user": result.is_new_user,
            },
        ),
        session_path,
    )


def _cookies_of(client: ZhipinLoginClient) -> dict[str, str]:
    jar = getattr(client.http, "cookies", None)
    if jar is None or not hasattr(jar, "get_dict"):
        return {}
    try:
        return {str(k): str(v) for k, v in jar.get_dict().items()}
    except Exception:  # noqa: BLE001
        return {}
