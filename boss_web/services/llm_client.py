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
            raise ValidationWebError("LLM 还没配置完整（API Key / Base URL / 模型名）")

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
            raise UpstreamError(f"连不上 LLM 接口：{exc}") from exc

        if getattr(resp, "status_code", 200) >= 400:
            snippet = str(getattr(resp, "text", ""))[:300]
            raise UpstreamError(
                f"LLM 接口返回 {getattr(resp, 'status_code', '?')}：{snippet}"
            )
        try:
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise UpstreamError(f"LLM 响应格式不对：{exc}") from exc
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
            raise ValidationWebError("先填 Base URL 再拉模型列表")
        if not cfg.api_key:
            raise ValidationWebError("先填 API Key 再拉模型列表")

        url = cfg.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
        try:
            resp = self._http.get(url, headers=headers, timeout=min(cfg.timeout, 30.0))
        except requests.RequestException as exc:
            raise UpstreamError(f"连不上模型列表接口：{exc}") from exc

        if getattr(resp, "status_code", 200) >= 400:
            snippet = str(getattr(resp, "text", ""))[:300]
            raise UpstreamError(
                f"模型列表接口返回 {getattr(resp, 'status_code', '?')}：{snippet}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamError(f"模型列表不是 JSON：{exc}") from exc

        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise UpstreamError("模型列表响应里没有 data 数组")

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
            raise UpstreamError("接口返回的模型列表是空的")
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
