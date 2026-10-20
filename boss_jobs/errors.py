"""职位获取模块的异常。

分两层：
    JobError        基类
      ├── JobTransportError  网络层失败（超时、连接错误、响应不是 JSON）
      ├── JobApiError        服务端返回了失败业务码（含登录态失效、浏览器校验）
      ├── JobDataError       响应是成功码但结构对不上，解析不出来
      └── ChatSendError      聊天通道（MQTT over WebSocket）发消息失败

Attributes:
    _CHAT_REMAIN_RE: 弹窗话术里「还剩 N 次」的提取正则。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import config as C

_CHAT_REMAIN_RE = re.compile(r"还剩\s*(\d+)\s*次")
_CHAT_RATE_LIMIT_RE = re.compile(
    r"(?:操作过于频繁|请求过于频繁|发送过于频繁|操作太快|请求太快|稍后再试|休息.{0,8}再试)"
)
_CHAT_LIMIT_EXHAUSTED_RE = re.compile(
    r"(?:"
    r"(?:今天|今日).{0,40}沟通.{0,40}(?:休息.{0,12}明天再来|明天再来)"
    r"|休息一下"
    r"|明天再来"
    r"|沟通.{0,12}(?:次数|额度).{0,8}(?:已达|达到|用完|上限)"
    r")"
)


def dig_chat_remind(payload: Any) -> Mapping[str, Any] | None:
    """从响应包里抠出 `zpData.bizData.chatRemindDialog`（没有回 `None`）。

    顶层 `message` 只是「开聊提醒」这个短标签，真话在 dialog.content 里。
    """
    if not isinstance(payload, Mapping):
        return None
    zp = payload.get("zpData")
    if not isinstance(zp, Mapping):
        return None
    biz = zp.get("bizData")
    if not isinstance(biz, Mapping):
        return None
    dialog = biz.get("chatRemindDialog")
    return dialog if isinstance(dialog, Mapping) else None


def format_api_message(payload: Mapping[str, Any]) -> str:
    """从响应包里抠出给人看的话术。

    顶层 `message` 常常只是短标签；有 `dig_chat_remind` 那种弹窗时优先用 `content`。
    """
    dialog = dig_chat_remind(payload)
    if dialog:
        content = str(dialog.get("content") or "").strip()
        if content:
            return content
        title = str(dialog.get("title") or "").strip()
        if title:
            return title
    for key in ("message", "msg"):
        val = payload.get(key)
        if val:
            return str(val)
    return ""


def chat_remind_remaining(payload: Any) -> int | None:
    """从「开聊提醒」话术里抠「还剩 N 次」；抠不出来回 `None`。

    传响应包或直接传话术字符串都行。剩 N 次表示还能主动沟通的次数，
    大于 0 时发送任务该继续用掉，别一见弹窗就整批停手。
    """
    if isinstance(payload, Mapping):
        text = format_api_message(payload)
    else:
        text = str(payload or "")
    m = _CHAT_REMAIN_RE.search(text)
    return int(m.group(1)) if m else None


class JobError(Exception):
    """职位获取失败的基类。

    Args:
        message: 错误文案。
        raw: 原始响应（排障用）。
    """

    def __init__(self, message: str, *, raw: Any = None) -> None:
        """初始化。"""
        super().__init__(message)
        self.message = message
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 直接复用 message
        """返回 message。"""
        return self.message


class JobTransportError(JobError):
    """网络层错误：超时、DNS、连接被重置、响应不是合法 JSON 等。"""


class JobApiError(JobError):
    """服务端返回了非成功业务码。`code` 为业务码，非 HTTP 状态码。

    Args:
        code: 业务码。
        message: 错误文案。
        raw: 原始响应（排障用）。
    """

    def __init__(self, code: int, message: str, *, raw: Any = None) -> None:
        """初始化。"""
        super().__init__(message or f"接口返回异常码 {code}", raw=raw)
        self.code = code

    @property
    def is_session_expired(self) -> bool:
        """True 表示登录态失效。

        code 7 或话术里明说了登录问题才算；code 1 是业务失败的通用码，不算。
        """
        return self.code in C.CODE_SESSION_EXPIRED or "登录" in self.message

    @property
    def chat_remind(self) -> Mapping[str, Any] | None:
        """`chatRemindDialog` 那个弹窗（没有回 `None`）。见 `dig_chat_remind`。"""
        return dig_chat_remind(self.raw)

    @property
    def is_chat_remind(self) -> bool:
        """True 表示撞上「开聊提醒」——每日沟通配额的提示弹窗。

        不是登录失效也不是账号风控；剩余次数大于 0 时仍可继续发送。
        """
        return self.chat_remind is not None or "开聊提醒" in self.message

    @property
    def chat_remind_remaining(self) -> int | None:
        """弹窗话术里的「还剩 N 次」；抠不出来回 `None`。见 `chat_remind_remaining`。"""
        return chat_remind_remaining(self.raw if self.raw is not None else self.message)

    @property
    def is_chat_rate_limited(self) -> bool:
        """True 表示沟通请求触发服务端的频率限制文案。"""
        return bool(_CHAT_RATE_LIMIT_RE.search(self.message))

    @property
    def is_chat_limit_exhausted(self) -> bool:
        """True 表示文案明确指向每日沟通次数耗尽，不包含一般操作频率限制。"""
        remaining = self.chat_remind_remaining
        if remaining is not None:
            return remaining == 0
        return bool(_CHAT_LIMIT_EXHAUSTED_RE.search(self.message))

    @property
    def is_browser_check(self) -> bool:
        """True 表示撞上安全网关（code 37「浏览器环境异常」）。

        缺的是 `__zp_stoken__` 令牌；令牌由 `boss_jobs.stoken` / `boss_jobs.cdp_stoken`
        自动补，算完重试即可。
        """
        return self.code == C.CODE_BROWSER_CHECK

    @property
    def is_risk_control(self) -> bool:
        """True 表示账号被风控（code 36「您的账户存在异常行为」）。

        要人机验证才恢复，客户端不去绕，把话术说清楚。
        """
        return self.code == C.CODE_RISK_CONTROL


class JobDataError(JobError):
    """响应业务码是成功，但字段形状不对，没法映射成职位。"""


class ChatSendError(JobError):
    """聊天通道（MQTT over WebSocket）没连上 / 没发出去。

    跟 `JobApiError` 分开，是因为它没有业务码：失败在传输层
    （握手被拒、CONNACK 非成功、PUBLISH 报错），上层记「这条发送失败」流水后不重试。
    没等到 PUBACK 不算失败——该网关对文本帧不回 PUBACK。
    """
