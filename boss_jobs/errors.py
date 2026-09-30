"""职位获取模块的异常。

分两层：
    JobError        基类
      ├── JobTransportError  网络层失败（超时、连接错误、响应不是 JSON）
      ├── JobApiError        服务端返回了失败业务码（含登录态失效、浏览器校验）
      └── JobDataError       响应是成功码但结构对不上，解析不出来
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
