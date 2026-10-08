"""职位获取模块的异常。

分两层：
    JobError        基类
      ├── JobTransportError  网络层失败（超时、连接错误、响应不是 JSON）
      ├── JobApiError        服务端返回了失败业务码（含登录态失效、浏览器校验）
      ├── JobDataError       响应是成功码但结构对不上，解析不出来
      └── ChatSendError      聊天通道（MQTT over WebSocket）发消息失败
"""

from __future__ import annotations

from typing import Any

from . import config as C


class JobError(Exception):
    """职位获取失败的基类。"""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 直接复用 message
        return self.message


class JobTransportError(JobError):
    """网络层错误：超时、DNS、连接被重置、响应不是合法 JSON 等。"""


class JobApiError(JobError):
    """服务端返回了非成功业务码。``code`` 为业务码，非 HTTP 状态码。"""

    def __init__(self, code: int, message: str, *, raw: Any = None) -> None:
        super().__init__(message or f"接口返回异常码 {code}", raw=raw)
        self.code = code

    @property
    def is_session_expired(self) -> bool:
        """True 表示登录态失效（接口回了「当前登录状态已失效」这类话术/码）。"""
        return self.code in C.CODE_SESSION_EXPIRED or "登录" in self.message

    @property
    def is_browser_check(self) -> bool:
        """True 表示撞上安全网关（code 37「浏览器环境异常」）。

        缺的是 ``__zp_stoken__`` 安全网关令牌。令牌的来源与算法见
        :mod:`boss_jobs.stoken`——本项目按站点前端那条链路**自动算**，
        算完重试即可，不用人工回浏览器。
        """
        return self.code == C.CODE_BROWSER_CHECK

    @property
    def is_risk_control(self) -> bool:
        """True 表示账号被风控（code 36「您的账户存在异常行为」）。

        这不是缺令牌，是账号维度的风险状态，通常要走人机验证
        （GeeTest）后才恢复。客户端不去绕，把话术说清楚。
        """
        return self.code == C.CODE_RISK_CONTROL


class JobDataError(JobError):
    """响应业务码是成功，但字段形状不对，没法映射成职位。"""


class ChatSendError(JobError):
    """聊天通道（MQTT over WebSocket）没连上 / 没发出去。

    跟 :class:`JobApiError` 分开，是因为它**没有业务码**：失败在传输层
    （握手被拒、CONNACK 非成功、PUBLISH 直接抛 / ``rc != 0``），重试策略也不一样——
    见 :mod:`boss_jobs.chat`。上层（发送任务）把它当「这条发送失败」记流水，
    **不重试**（避免重复打扰招聘方）。

    ⚠️ **没等到 PUBACK 不算失败**：这条网关对文本帧不回 PUBACK，发完就把连接
    关掉是常态（见 :mod:`boss_jobs.chat` 模块头）。本异常只在**握手/连接没成、
    或 PUBLISH 本身报错**时抛。
    """
