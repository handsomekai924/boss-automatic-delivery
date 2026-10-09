"""数据传输对象。

对外只暴露 dataclass，屏蔽服务端返回结构的差异（``zpData`` / ``data`` / 裸字段）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def mask_phone(phone: str) -> str:
    """脱敏：13800138000 -> 138****8000。日志与本地存储都只用脱敏后的号码。"""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[7:]}"
    if len(digits) > 4:
        return f"{digits[:2]}****{digits[-2:]}"
    return digits


@dataclass(frozen=True)
class ApiResponse:
    """把服务端响应拍平成统一结构。"""

    code: int
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200

    @property
    def ok(self) -> bool:
        return self.code == 0

    @classmethod
    def from_payload(cls, payload: Any, http_status: int = 200) -> "ApiResponse":
        """兼容多种包裹形式：{code,message,zpData} / {code,message,data} / 裸 dict。"""
        if not isinstance(payload, dict):
            raise TypeError(f"响应不是 JSON 对象: {type(payload).__name__}")

        code = payload.get("code", payload.get("status", 0))
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -1

        message = str(payload.get("message") or payload.get("msg") or "")
        data = payload.get("zpData") or payload.get("data") or {}
        if not isinstance(data, dict):
            data = {"value": data}

        return cls(code=code, message=message, data=data, http_status=http_status)


@dataclass(frozen=True)
class SmsCodeTicket:
    """一次成功的「发送验证码」回执。"""

    phone_masked: str
    dial_code: str
    sent_at: float
    retry_after: int

    @property
    def expires_at(self) -> float:
        """本地认为可以重发的时间点。"""
        return self.sent_at + self.retry_after

    def remaining_cooldown(self, *, now: float | None = None) -> int:
        """距离可重发还剩多少秒，向上取整，已过则返回 0。"""
        remain = self.expires_at - (now if now is not None else time.time())
        return max(0, int(remain + 0.999))


@dataclass(frozen=True)
class UserInfo:
    """登录用户信息。"""

    user_id: str = ""
    name: str = ""
    phone_masked: str = ""
    identity: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "UserInfo":
        return cls(
            user_id=str(data.get("userId") or data.get("uid") or data.get("encryptUserId") or ""),
            name=str(data.get("name") or data.get("geekName") or data.get("nickName") or ""),
            phone_masked=str(data.get("phone") or data.get("mobile") or ""),
            identity=str(data.get("identity") or data.get("type") or ""),
            raw=data,
        )


@dataclass(frozen=True)
class LoginResult:
    """一次成功的登录回执。"""

    token: str
    user: UserInfo
    is_new_user: bool = False
    cookies: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    #: 这次登录**新种**的 Cookie（前后快照的差集）。真实站点登录响应体里没有
    #: 会话 token，鉴权完全靠 ``Set-Cookie``；名字也可能不在 ``AUTH_COOKIES`` 里。
    #: 只要真种了 Cookie 就先当换到了登录态，由 ``user_info`` 验真伪。
    new_cookies: dict[str, str] = field(default_factory=dict)

    @property
    def logged_in(self) -> bool:
        """有 token，或者拿到了鉴权 Cookie，都算登录成功。"""
        from .config import AUTH_COOKIES

        return (
            bool(self.token)
            or any(name in self.cookies for name in AUTH_COOKIES)
            or bool(self.new_cookies)
        )
