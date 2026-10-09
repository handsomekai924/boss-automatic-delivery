"""OpenAI 兼容的 Chat Completions 客户端（requests，无新依赖）。"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Mapping, Sequence

import requests

from ..errors import UpstreamError, ValidationWebError
from .llm_config_store import LLMConfig

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 把上游的技术错误翻成人话
# --------------------------------------------------------------------------- #
#
# ``requests`` 的异常原文长这样：::
#
#   HTTPSConnectionPool(host='api.deepseek.com', port=443): Max retries exceeded
#   with url: /v1/chat/completions (Caused by ConnectTimeoutError(...))
#
# 上游的响应体里还会带两三百字英文报错。**这些一律不进界面**：原文写日志，
# 用户看到的必须是「哪儿错了 + 怎么改」。
#
# 报错文本会被 ``tests/test_web_llm.py`` 钉住，改了这里就该同步改测试。

#: 状态码 → ``(怎么了, 怎么办)``
_STATUS_HINTS: dict[int, tuple[str, str]] = {
    400: ("对方没接受这次请求", "多半是模型名不对，回「AI 设置」重新拉一次模型列表再选。"),
    401: ("API Key 不对", "Key 可能复制漏了字符，或者已经被删掉。请重新复制一遍，注意别带上空格和换行。"),
    402: ("账户余额不足", "AI 服务那边没钱了，去充值后再试。"),
    403: ("这个 Key 没有调用权限", "账号可能还没实名认证，或者没开通这个模型。"),
    404: ("接口地址填错了", "检查「接口地址」这一栏，DeepSeek 要填 https://api.deepseek.com/v1。"),
    429: ("请求太频繁或超出额度", "等一两分钟再试。"),
}

#: 状态码认不出来时，从上游原文里嗅一嗅更准的原因
_SNIPPET_HINTS: tuple[tuple[str, str], ...] = (
    ("insufficient balance", "账户余额不足，去 AI 服务那边充值后再试。"),
    ("invalid api key", "API Key 不对，请重新复制一次。"),
    ("authentication", "API Key 不对，请重新复制一次。"),
    ("model not exist", "模型名不对，「AI 设置」里重新拉一次模型列表再选。"),
    ("rate limit", "请求太频繁，等一两分钟再试。"),
)


def _friendly_http_error(status: int, snippet: str) -> str:
    """上游回 4xx / 5xx 时给用户看的话。**不含**上游响应体原文。"""
    low = (snippet or "").lower()
    # 先嗅原文：同一个状态码不同服务商给的原因不一样，原文比状态码更准
    for needle, text in _SNIPPET_HINTS:
        if needle in low:
            return text
    hint = _STATUS_HINTS.get(status)
    if hint:
        return f"{hint[0]}。{hint[1]}"
    if 500 <= status < 600:
        return f"AI 服务商那边出故障了（{status}），不是你的配置问题。过一会儿再试。"
    return f"AI 接口没接受这次请求（返回 {status}）。换个模型，或过一会儿再试。"


def _friendly_network_error(exc: BaseException) -> str:
    """连不上 / 超时 / TLS 失败——区分开，因为用户要做的事不一样。"""
    text = str(exc).lower()
    if "certificate" in text or "ssl" in text:
        return "和 AI 服务的加密连接没建立起来。换一条网络（比如手机热点）再试。"
    if "timed out" in text or "timeout" in text:
        return "连 AI 服务超时了。检查网络后重试；一直超时就换一个「接口地址」。"
    return "连不上 AI 服务。请先确认这台电脑能正常上网。"


#: 注入用的网络层签名，测试里替换成假的
HttpSender = Callable[..., Any]


class LLMClient:
    def __init__(self, config: LLMConfig, *, http: HttpSender | None = None) -> None:
        self.config = config
        self._http = http or requests

    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        cfg = self.config
        if not cfg.configured:
            raise ValidationWebError("AI 还没配置好——API Key / 接口地址 / 模型，三样都要填。")

        url = cfg.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": cfg.model,
            "messages": list(messages),
            "temperature": cfg.temperature if temperature is None else temperature,
            "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._http.post(
                url,
                headers=headers,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=timeout or cfg.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("调用 AI 接口失败（%s）：%s", url, exc)
            raise UpstreamError(_friendly_network_error(exc)) from exc

        status = int(getattr(resp, "status_code", 200) or 200)
        if status >= 400:
            snippet = str(getattr(resp, "text", ""))[:300]
            logger.warning("AI 接口返回 %s：%s", status, snippet)
            raise UpstreamError(_friendly_http_error(status, snippet))
        try:
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("AI 响应解析失败：%s", exc)
            raise UpstreamError(
                "AI 返回的内容看不懂——「接口地址」可能不是 OpenAI 兼容协议的，换个地址试试。"
            ) from exc
        return str(content or "")

    def test(self) -> dict[str, Any]:
        started = time.time()
        reply = self.chat(
            [{"role": "user", "content": "请只回复两个字：正常"}],
            max_tokens=16,
            timeout=30.0,
        )
        return {
            "ok": True,
            "latency_ms": int((time.time() - started) * 1000),
            "model": self.config.model,
            "message": reply.strip()[:80],
        }

    def list_models(self) -> list[str]:
        """打 ``GET /models`` 拉可用模型列表（OpenAI 兼容）。

        顺序按接口返回；重名去重，空 id 丢掉。
        """
        cfg = self.config
        if not cfg.base_url:
            raise ValidationWebError("先填「接口地址」再拉模型列表。")
        if not cfg.api_key:
            raise ValidationWebError("先填「API Key」再拉模型列表。")

        url = cfg.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
        try:
            resp = self._http.get(url, headers=headers, timeout=min(cfg.timeout, 30.0))
        except requests.RequestException as exc:
            logger.warning("拉取模型列表失败（%s）：%s", url, exc)
            raise UpstreamError(_friendly_network_error(exc)) from exc

        status = int(getattr(resp, "status_code", 200) or 200)
        if status >= 400:
            snippet = str(getattr(resp, "text", ""))[:300]
            logger.warning("模型列表接口返回 %s：%s", status, snippet)
            raise UpstreamError(_friendly_http_error(status, snippet))
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning("模型列表不是 JSON：%s", exc)
            raise UpstreamError(
                "这个接口地址没有返回标准格式的模型列表——「接口地址」可能填错了。"
            ) from exc

        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise UpstreamError(
                "这个接口地址不像是 OpenAI 兼容的（模型列表格式对不上），换个地址试试。"
            )

        seen: set[str] = set()
        models: list[str] = []
        for item in raw:
            mid = item.get("id") if isinstance(item, dict) else item
            mid = str(mid or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            models.append(mid)
        if not models:
            raise UpstreamError("这个接口一个可用模型都没返回，检查 Key 和接口地址填对没有。")
        return models


def extract_json(text: str) -> dict[str, Any] | None:
    """从 LLM 回复里抠出第一段 JSON 对象（容忍 ```json 围栏和闲话）。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for raw in candidates:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None
