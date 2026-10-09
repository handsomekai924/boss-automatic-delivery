"""LLM 配置与客户端的单元测试（离线，网络层可注入）。

配置落状态库 ``doc('llm_config')``；这里传的 ``p`` 是**库路径**。
"""

from __future__ import annotations

import json

import pytest

import boss_db
from boss_web import config as C
from boss_web.errors import UpstreamError
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
    p = tmp_path / "boss.db"
    cfg = LLMConfig(api_key="sk-secret", base_url="https://x/v1", model="m1", temperature=0.3)
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.api_key == "sk-secret"
    assert loaded.model == "m1"
    assert loaded.configured
    assert loaded.masked()["api_key"] == "sk-***cret"
    assert loaded.masked()["has_key"] is True
    # 采样参数被拉回系统常量，0.3 存不进去
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
    """温度 / max_tokens / 超时是系统固定值，传进来也要被丢掉。"""
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
    # 落库的也是常量
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
    # 采样参数来自系统常量
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


# --------------------------------------------------------------------------- #
# 上游报错的人话化
#
# 这里的花样最多：requests 的异常原文是「HTTPSConnectionPool(host=…)」，
# 上游响应体是两三百字英文。**一个都不许原样出现在界面上**——
# 原文只进日志。这几条测试就是拿真实原文来钉这件事。
# --------------------------------------------------------------------------- #

#: 抄自真实 requests 异常的文本
REQUESTS_ERRORS = [
    "HTTPSConnectionPool(host='api.deepseek.com', port=443): Max retries exceeded "
    "with url: /v1/chat/completions (Caused by ConnectTimeoutError("
    "<urllib3.connection.HTTPSConnection object at 0x1>, "
    "'Connection to api.deepseek.com timed out. (connect timeout=120)'))",
    "HTTPSConnectionPool(host='api.x.com', port=443): Max retries exceeded with url: "
    "/v1/chat/completions (Caused by SSLError(SSLCertVerificationError(1, "
    "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')))",
    "HTTPSConnectionPool(host='127.0.0.1', port=9999): Max retries exceeded with url: "
    "/v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection "
    "object at 0x2>: Failed to establish a new connection: [WinError 10061] 由于目标计算机积极拒绝'))",
]


def _client(resp=None, exc=None):
    import requests

    kwargs = {}
    if exc is not None:
        kwargs["exc"] = exc
    else:
        kwargs["resp"] = resp
    return LLMClient(
        LLMConfig(api_key="sk-1", base_url="https://api.x/v1", model="m1"),
        http=FakeHttp(**kwargs),
    )


@pytest.mark.parametrize("raw", REQUESTS_ERRORS)
def test_network_error_never_shows_requests_internals(raw):
    import requests

    exc = requests.ConnectionError(raw)
    with pytest.raises(UpstreamError) as ei:
        _client(exc=exc).chat([{"role": "user", "content": "hi"}])

    message = str(ei.value)
    assert message != raw
    for leak in ("HTTPSConnectionPool", "urllib3", "WinError", "SSLError", "0x1", "url:"):
        assert leak not in message, f"漏了 {leak}：{message}"
    assert "网" in message or "网络" in message or "连接" in message


@pytest.mark.parametrize(
    "status,hint",
    [
        (401, "API Key"),
        (402, "余额"),
        (404, "接口地址"),
        (429, "频繁"),
    ],
)
def test_upstream_status_becomes_actionable(status, hint):
    resp = FakeResp({"error": {"message": "some upstream english text"}}, status=status)
    with pytest.raises(UpstreamError) as ei:
        _client(resp=resp).chat([{"role": "user", "content": "hi"}])

    message = str(ei.value)
    assert hint in message, message
    assert "some upstream english text" not in message, "上游响应体不许出现在界面上"


def test_upstream_5xx_blames_the_server_not_the_user():
    resp = FakeResp({"error": "internal"}, status=503)
    with pytest.raises(UpstreamError) as ei:
        _client(resp=resp).chat([{"role": "user", "content": "hi"}])
    assert "不是你" in str(ei.value) or "服务商" in str(ei.value)


def test_keyword_in_body_wins_over_plain_status_table():
    """同一个 400，有的服务商说余额不足、有的说模型不存在——按原文判更准。"""
    from boss_web.services.llm_client import _friendly_http_error

    assert "模型" in _friendly_http_error(400, '{"error":"Model Not Exist"}')
    assert "余额" in _friendly_http_error(400, '{"error":"Insufficient Balance"}')


def test_unknown_status_keeps_the_code_but_not_the_body():
    from boss_web.services.llm_client import _friendly_http_error

    out = _friendly_http_error(418, "<html>teapot 内部堆栈 xxx</html>")
    assert "418" in out, "认不出来至少要把状态码给出来，方便用户报障"
    assert "teapot" not in out
    assert "<html>" not in out


def test_bad_json_response_is_explained_not_dumped():
    class NotJson(FakeResp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    resp = NotJson({"x": 1})
    with pytest.raises(UpstreamError) as ei:
        _client(resp=resp).chat([{"role": "user", "content": "hi"}])
    message = str(ei.value)
    assert "Expecting value" not in message
    assert "接口地址" in message


def test_models_error_paths_also_humanized():
    resp = FakeResp({"error": "Unauthorized"}, status=401)
    with pytest.raises(UpstreamError) as ei:
        _client(resp=resp).list_models()
    assert "API Key" in str(ei.value)


def test_models_rejects_non_openai_shape_with_advice():
    with pytest.raises(UpstreamError) as ei:
        _client(resp=FakeResp({"oops": 1})).list_models()
    assert "OpenAI" in str(ei.value)

