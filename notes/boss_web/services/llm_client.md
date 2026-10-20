# `boss_web.services.llm_client` 协议笔记

> 摘自 `boss_web/services/llm_client.py` 的模块文档字符串 / `#` 注释。

## 一句话

OpenAI 兼容的 Chat Completions 客户端。

## 细节

把上游的技术错误翻成人话：

`requests` 的异常原文长这样：::

    HTTPSConnectionPool(host='api.deepseek.com', port=443): Max retries exceeded
    with url: /v1/chat/completions (Caused by ConnectTimeoutError(...))

上游的响应体里还会带两三百字英文报错。**这些一律不进界面**：原文写日志，
用户看到的必须是「哪儿错了 + 怎么改」。

报错文本会被 `tests/test_web_llm.py` 钉住，改了这里就该同步改测试。

### `_friendly_http_error`

先嗅原文：同一个状态码不同服务商给的原因不一样，原文比状态码更准。
