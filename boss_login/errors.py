"""异常体系。

分三层，便于调用方按粒度捕获：
    BossLoginError                 所有异常的基类
      ├── ValidationError          本地校验失败（不发请求）
      ├── TransportError           网络层失败（超时、连接错误、非法响应）
      └── ApiError                 服务端返回了失败业务码
            ├── RateLimited        发送过于频繁（带 retry_after）
            ├── RiskControlRequired 命中风控，需要人工完成安全验证
            ├── AccountBlocked     IP / 账号被封禁
            ├── CodeRejected       验证码错误 / 过期 / 次数超限
            └── SessionExpired     未登录或登录态失效
"""

from __future__ import annotations

from typing import Any


class BossLoginError(Exception):
    """所有错误的基类。"""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 直接复用 message
        return self.message


class ValidationError(BossLoginError):
    """本地校验不通过。``field`` 指明出错字段，便于 UI 定位。"""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class TransportError(BossLoginError):
    """网络层错误：超时、DNS、连接被重置、响应不是合法 JSON 等。"""


class ApiError(BossLoginError):
    """服务端返回了非成功业务码。"""

    def __init__(self, code: int, message: str, *, raw: Any = None) -> None:
        super().__init__(message or f"接口返回异常码 {code}", raw=raw)
        self.code = code


class RateLimited(ApiError):
    """发送过于频繁。``retry_after`` 为建议等待秒数（可能来自服务端）。"""

    def __init__(self, code: int, message: str, *, retry_after: int = 0, raw: Any = None) -> None:
        super().__init__(code, message, raw=raw)
        self.retry_after = retry_after


class RiskControlRequired(ApiError):
    """命中风控，需要人工完成安全验证。

    服务端在 ``SECURITY_CHECK`` 时返回 ``zpData: {seed, ts, name}``，
    前端据此渲染滑块 / 点选验证。本客户端**不实现**验证码破解，
    只把参数透出，由使用者在浏览器中亲自完成验证后，把票据回填进来。
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        seed: str = "",
        ts: str = "",
        name: str = "",
        verify_page: str = "",
        raw: Any = None,
    ) -> None:
        super().__init__(code, message, raw=raw)
        self.seed = seed
        self.ts = ts
        self.name = name
        self.verify_page = verify_page

    @property
    def is_hard_block(self) -> bool:
        """True 表示 IP / 账号已被封禁，重试无用。"""
        from .config import RISK_CODE_IP_BLOCK, RISK_CODE_UID_BLOCK

        return self.code in RISK_CODE_IP_BLOCK | RISK_CODE_UID_BLOCK


class AccountBlocked(ApiError):
    """IP 或账号被风控封禁，短时间内无法通过重试恢复。"""


class CodeRejected(ApiError):
    """验证码相关失败：错误 / 过期 / 尝试次数超限。"""


class SessionExpired(BossLoginError):
    """本地无登录态，或服务端已使登录态失效。"""


class LoginIncomplete(BossLoginError):
    """接口回了成功码，却没拿到任何可用于后续请求的凭证。

    真实站点的登录响应体里没有会话 token（登录页的成功处理只读路由字段），
    鉴权靠 ``Set-Cookie``。所以这条通常意味着登录那步**没换到登录态**——
    抛出时会带上响应 ``zpData`` 的键名和实际收到的 Cookie 名，照着适配。
    若连路由都不确定，再用 ``python -m boss_login probe``。
    """
