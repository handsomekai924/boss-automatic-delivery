"""LLM 配置 + 在线模型列表 + 连通性测试。

可配置的只有 api_key / base_url / model；温度、max_tokens、超时是系统固定值。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .. import config as C
from ..errors import UpstreamError
from ..services.llm_client import LLMClient
from ..services.llm_config_store import LLMConfig, load_config, update_config

router = APIRouter()


class ConfigBody(BaseModel):
    """只有这三项能改；采样参数即使传进来也会被丢掉。"""

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class ModelsBody(BaseModel):
    """拉模型列表用的临时凭据（不落盘）。

    两项都留空就用已保存的配置——这样用户改完 Key 先「拉取」再「保存」也行。
    """

    base_url: str | None = None
    api_key: str | None = None


@router.get("/config")
def get_config() -> dict[str, Any]:
    """读 LLM 配置（key 打码）。"""
    return load_config().masked()


@router.put("/config")
def put_config(body: ConfigBody) -> dict[str, Any]:
    """写 LLM 配置。"""
    cfg = update_config(body.model_dump(exclude_none=True))
    return cfg.masked()


@router.post("/models")
def list_models(body: ModelsBody | None = None) -> dict[str, Any]:
    """代理 `GET {base_url}/models`，拉可用模型名给下拉框用。

    走 POST 是为了不把 API Key 塞进 URL。
    """
    stored = load_config()
    body = body or ModelsBody()
    cfg = LLMConfig(
        api_key=(body.api_key or "").strip() or stored.api_key,
        base_url=(body.base_url or "").strip() or stored.base_url,
        model=stored.model,
        temperature=C.LLM_TEMPERATURE,
        max_tokens=C.LLM_MAX_TOKENS,
        timeout=C.LLM_TIMEOUT,
    )
    client = LLMClient(cfg)
    models = client.list_models()
    return {
        "models": models,
        "source": cfg.base_url.rstrip("/") + "/models",
        "selected": stored.model,
    }


@router.post("/test")
def test_connection() -> dict[str, Any]:
    """用已保存配置做一次连通性测试；失败也回 200 带人话。"""
    client = LLMClient(load_config())
    try:
        return client.test()
    except UpstreamError as exc:
        return {"ok": False, "message": exc.message}
