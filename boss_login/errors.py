"""异常体系：本地校验、网络层、业务码三层错误。

`BossLoginError` 是基类；`ApiError` 及其子类对应服务端业务失败码。
"""

from __future__ import annotations

from typing import Any


class BossLoginError(Exception):
    """所有错误的基类。

    Args:
        message: 对外展示的错误信息。
        raw: 原始响应或上下文，便于排查。
    """

    def __init__(self, message: str, *, raw: Any = None) -> None:
        """初始化。"""
        super().__init__(message)
        self.message = message
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 直接复用 message
        """返回 `message`。"""
        return self.message


class ValidationError(BossLoginError):
    """本地校验不通过。

    Args:
        message: 错误信息。
        field: 出错字段名，便于 UI 定位。
    """

    def __init__(self, message: str, *, field: str = "") -> None:
        """初始化。"""
        super().__init__(message)
        self.field = field


class TransportError(BossLoginError):
    """网络层错误：超时、DNS、连接被重置、响应不是合法 JSON 等。"""


class ApiError(BossLoginError):
    """服务端返回了非成功业务码。

    Args:
        code: 业务码。
        message: 服务端文案。
        raw: 原始响应。
    """

    def __init__(self, code: int, message: str, *, raw: Any = None) -> None:
        """初始化。"""
        super().__init__(message or f"接口返回异常码 {code}", raw=raw)
        self.code = code


class RateLimited(ApiError):
    """发送过于频繁。

    Args:
        code: 业务码。
        message: 服务端文案。
        retry_after: 建议等待秒数，可能来自服务端。
        raw: 原始响应。
    """

    def __init__(self, code: int, message: str, *, retry_after: int = 0, raw: Any = None) -> None:
        """初始化。"""
        super().__init__(code, message, raw=raw)
        self.retry_after = retry_after


class RiskControlRequired(ApiError):
    """命中风控，需要人工完成安全验证。

    本客户端不实现验证码破解，只透出服务端参数，由使用者在浏览器完成后回填票据。

    Args:
        code: 业务码。
        message: 服务端文案。
        seed: 安全验证种子。
        ts: 时间戳。
        name: 验证名称。
        verify_page: 验证页地址。
        raw: 原始响应。
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
        """初始化。"""
        super().__init__(code, message, raw=raw)
        self.seed = seed
        self.ts = ts
        self.name = name
        self.verify_page = verify_page

    @property
    def is_hard_block(self) -> bool:
        """IP / 账号是否已被封禁，重试无用。"""
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

    抛出时会带上响应 `zpData` 的键名和实际收到的 Cookie 名，照着适配。
    """
