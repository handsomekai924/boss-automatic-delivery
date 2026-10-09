"""LLM 配置与客户端的单元测试（离线，网络层可注入）。

配置落状态库 ``doc('llm_config')``；这里传的 ``p`` 是**库路径**。
"""

from __future__ import annotations

import json

import pytest

import boss_db
from boss_web import config as C
from boss_web.services.llm_client import LLMClient, extract_json
from boss_web.services.llm_config_store import (
    LLMConfig,
    load_config,
    mask_key,
    save_config,
    update_config,
)


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = []

    def _hit(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.exc:
            raise self.exc
        return self.resp

    def post(self, url, **kwargs):
        return self._hit("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._hit("GET", url, **kwargs)


def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("sk-abcdef123456").startswith("sk-")
    assert mask_key("sk-abcdef123456").endswith("3456")


def test_save_load_roundtrip(tmp_path):
    """温度 / max_tokens / 超时是系统常量，存进去的 0.3 等值会被拉回。"""
    p = tmp_path / "boss.db"
    cfg = LLMConfig(api_key="sk-secret", base_url="https://x/v1", model="m1", temperature=0.3)
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.api_key == "sk-secret"
    assert loaded.model == "m1"
    assert loaded.configured
    assert loaded.masked()["api_key"] == "sk-***cret"
    assert loaded.masked()["has_key"] is True
    assert loaded.temperature == C.LLM_TEMPERATURE
    assert loaded.max_tokens == C.LLM_MAX_TOKENS
    assert loaded.timeout == C.LLM_TIMEOUT


def test_update_keeps_old_key(tmp_path):
    p = tmp_path / "boss.db"
    save_config(LLMConfig(api_key="sk-old", model="m"), p)
    cfg = update_config({"api_key": "", "model": "m2"}, p)
    assert cfg.api_key == "sk-old"
    assert cfg.model == "m2"


def test_update_ignores_sampling_params(tmp_path):
    """温度 / max_tokens / 超时是系统固定值，传进来也要被丢掉；落库同样写常量。"""
    p = tmp_path / "boss.db"
    save_config(LLMConfig(api_key="sk-a", model="m1"), p)
    cfg = update_config(
        {
            "model": "m2",
            "temperature": 1.9,
            "max_tokens": 16,
            "timeout": 5.0,
        },
        p,
    )
    assert cfg.model == "m2"
    assert cfg.temperature == C.LLM_TEMPERATURE
    assert cfg.max_tokens == C.LLM_MAX_TOKENS
    assert cfg.timeout == C.LLM_TIMEOUT
    raw = boss_db.doc_get(boss_db.DOC_LLM_CONFIG, p)
    assert raw["temperature"] == C.LLM_TEMPERATURE
    assert raw["max_tokens"] == C.LLM_MAX_TOKENS


def test_load_normalizes_legacy_values(tmp_path):
    """历史配置里若写过采样参数，读出来也要被拉齐。"""
    p = tmp_path / "boss.db"
    boss_db.doc_set(
        boss_db.DOC_LLM_CONFIG,
        {
            "api_key": "sk-x",
            "base_url": "https://x/v1",
            "model": "m",
            "temperature": 1.5,
            "max_tokens": 99,
            "timeout": 7.0,
        },
        p,
    )
    cfg = load_config(p)
    assert cfg.temperature == C.LLM_TEMPERATURE
    assert cfg.max_tokens == C.LLM_MAX_TOKENS
    assert cfg.timeout == C.LLM_TIMEOUT


def test_masked_exposes_fixed_params():
    cfg = LLMConfig(api_key="sk-12345678", model="m").masked()
    assert cfg["fixed"]["temperature"] == C.LLM_TEMPERATURE
    assert cfg["fixed"]["max_tokens"] == C.LLM_MAX_TOKENS
    assert cfg["fixed"]["timeout"] == C.LLM_TIMEOUT


def test_chat_posts_to_completions():
    http = FakeHttp(
        FakeResp({"choices": [{"message": {"content": "你好"}}], "model": "m1"})
    )
    client = LLMClient(LLMConfig(api_key="sk-1", base_url="https://api.x/v1", model="m1"), http=http)
    out = client.chat([{"role": "user", "content": "hi"}])
    assert out == "你好"
    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == "https://api.x/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-1"
    body = json.loads(kwargs["data"].decode("utf-8"))
    assert body["model"] == "m1"
    assert body["temperature"] == C.LLM_TEMPERATURE
    assert body["max_tokens"] == C.LLM_MAX_TOKENS


def test_chat_requires_config():
    client = LLMClient(LLMConfig())
    with pytest.raises(Exception):
        client.chat([{"role": "user", "content": "hi"}])


def test_list_models():
    http = FakeHttp(
        FakeResp(
            {
                "data": [
                    {"id": "deepseek-chat"},
                    {"id": "deepseek-reasoner"},
                    {"id": "deepseek-chat"},  # 重复，去重
                    {"id": ""},
                ]
            }
        )
    )
    client = LLMClient(LLMConfig(api_key="sk-1", base_url="https://api.x/v1"), http=http)
    assert client.list_models() == ["deepseek-chat", "deepseek-reasoner"]
    method, url, kwargs = http.calls[0]
    assert method == "GET"
    assert url == "https://api.x/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-1"


def test_list_models_requires_key():
    client = LLMClient(LLMConfig(base_url="https://api.x/v1"), http=FakeHttp())
    with pytest.raises(Exception):
        client.list_models()


def test_list_models_rejects_bad_payload():
    client = LLMClient(
        LLMConfig(api_key="sk-1", base_url="https://api.x/v1"),
        http=FakeHttp(FakeResp({"oops": 1})),
    )
    with pytest.raises(Exception):
        client.list_models()


def test_extract_json_variants():
    assert extract_json('{"a":1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('前言 {"match_score": 80} 后语')["match_score"] == 80
    assert extract_json("没有 JSON") is None
