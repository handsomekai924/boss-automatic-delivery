"""网页层的错误类型，统一映射成 HTTP 状态码。"""

from __future__ import annotations

from typing import Any


class WebError(Exception):
    """业务可读错误，前端直接把 ``message`` 摆出来。"""

    status_code = 400
    code = "web_error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class NotFoundError(WebError):
    status_code = 404
    code = "not_found"


class ConflictError(WebError):
    status_code = 409
    code = "conflict"


class ValidationWebError(WebError):
    status_code = 422
    code = "invalid"


class UpstreamError(WebError):
    """上游（BOSS 站点 / LLM 接口）报错，502 透传可读原因。"""

    status_code = 502
    code = "upstream"


class EnvironmentWebError(WebError):
    """本机环境不满足（没装 Chrome、端口被占、目录不可写），503。

    和 :class:`UpstreamError` 分开是因为**该谁去修不一样**：上游错了等一会儿
    就好，环境错了得让用户照提示去装/去关。
    """

    status_code = 503
    code = "environment"
