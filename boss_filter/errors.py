"""筛选条件模块的异常。

分两层：
    FilterError        基类
      ├── FilterTransportError  网络层失败（超时、连接错误、响应不是 JSON）
      ├── FilterApiError        服务端返回了失败业务码（含登录态失效）
      └── FilterDataError       响应是成功码但结构对不上，解析不出来
"""

from __future__ import annotations

from typing import Any


class FilterError(Exception):
    """筛选条件获取失败的基类。"""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 直接复用 message
        return self.message


class FilterTransportError(FilterError):
    """网络层错误：超时、DNS、连接被重置、响应不是合法 JSON 等。"""


class FilterApiError(FilterError):
    """服务端返回了非成功业务码。``code`` 为业务码，非 HTTP 状态码。"""

    def __init__(self, code: int, message: str, *, raw: Any = None) -> None:
        super().__init__(message or f"接口返回异常码 {code}", raw=raw)
        self.code = code

    @property
    def is_session_expired(self) -> bool:
        """True 表示登录态失效（接口回了「当前登录状态已失效」这类话术/码）。

        **code 1 不算**——它是业务失败的通用码，只有 code 7（实测的
        「当前登录状态已失效」）或话术里明说了登录问题才算。跟
        :attr:`boss_jobs.errors.JobApiError.is_session_expired` 同判据。
        """
        return self.code in (7,) or "登录" in self.message


class FilterDataError(FilterError):
    """响应业务码是成功，但字段形状不对，没法映射成筛选项。"""
