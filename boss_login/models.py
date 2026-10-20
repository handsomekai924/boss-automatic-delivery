"""数据传输对象，屏蔽服务端返回结构的差异。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def mask_phone(phone: str) -> str:
    """脱敏：`13800138000` → `138****8000`。"""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[7:]}"
    if len(digits) > 4:
        return f"{digits[:2]}****{digits[-2:]}"
    return digits


@dataclass(frozen=True)
class ApiResponse:
    """把服务端响应拍平成统一结构。

    Attributes:
        code: 业务码。
        message: 服务端文案。
        data: 业务数据（`zpData` / `data`）。
        http_status: HTTP 状态码。
        raw: 原始 JSON 载荷；扫码那族接口把 `scaned` / `login` 摊在顶层，留在这里。
    """

    code: int
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """业务码是否为成功。"""
        return self.code == 0

    def flag(self, name: str, default: bool = False) -> bool:
        """读顶层的布尔旗标（`scaned` / `login` 之类）。"""
        value = self.raw.get(name, default)
        return bool(value) if value is not None else default

    @classmethod
    def from_payload(cls, payload: Any, http_status: int = 200) -> "ApiResponse":
        """兼容多种包裹形式构造；payload 不是 JSON 对象时抛 `TypeError`。"""
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

        return cls(
            code=code,
            message=message,
            data=data,
            http_status=http_status,
            raw=dict(payload),
        )


@dataclass(frozen=True)
class SmsCodeTicket:
    """一次成功的「发送验证码」回执。

    Attributes:
        phone_masked: 脱敏手机号。
        dial_code: 国家区号。
        sent_at: 发送时刻（epoch 秒）。
        retry_after: 建议重发间隔（秒）。
    """

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
    """登录用户信息。

    Attributes:
        user_id: 用户 ID。
        name: 昵称。
        phone_masked: 脱敏手机号。
        identity: 身份（求职者 / 招聘者）。
        raw: 原始用户数据。
    """

    user_id: str = ""
    name: str = ""
    phone_masked: str = ""
    identity: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "UserInfo":
        """从用户信息字典构造，兼容多种字段名。"""
        return cls(
            user_id=str(data.get("userId") or data.get("uid") or data.get("encryptUserId") or ""),
            name=str(data.get("name") or data.get("geekName") or data.get("nickName") or ""),
            phone_masked=str(data.get("phone") or data.get("mobile") or ""),
            identity=str(data.get("identity") or data.get("type") or ""),
            raw=data,
        )


@dataclass(frozen=True)
class LoginResult:
    """一次成功的登录回执。

    Attributes:
        token: 会话 token；真实站点通常为空，鉴权靠 Cookie。
        user: 用户信息。
        is_new_user: 是否新注册用户。
        cookies: 登录时收到的全部 Cookie。
        raw: 原始响应。
        new_cookies: 这次登录新种的 Cookie（前后快照差集）；有就先当换到了登录态。
    """

    token: str
    user: UserInfo
    is_new_user: bool = False
    cookies: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
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
