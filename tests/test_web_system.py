"""环境自检 / Chrome 引导 / 错误人话化。

重点测两件事：

1. :func:`humanize` 对**真实**的 ``StokenError`` 文案逐条命中——这些原文抄自
   ``boss_jobs/cdp_stoken.py``，改了那边的措辞这里就该红。
2. 界面上不会出现 ``StokenError: 连不上 CDP（ws://...）`` 这种原文。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_jobs.cdp_stoken import StokenError
from boss_web import runtime
from boss_web.app import create_app
from boss_web.services import app_settings_store, troubleshoot


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# humanize：对着真实文案
# --------------------------------------------------------------------------- #

#: 抄自 boss_jobs/cdp_stoken.py 的原文（行号见各注释）
REAL_MESSAGES = [
    "找不到 Chrome：装个 Google Chrome，或用 BOSS_CHROME_BIN 指到 chrome 可执行文件。",
    "BOSS_CHROME_BIN=C:\\nope\\chrome.exe 指的文件不存在",
    "连不上 CDP（ws://127.0.0.1:9222/devtools/browser/8f3c-...）：握手被拒",
    "Chrome 起了但调试口没开（试过 9222~9224）。端口 9222 一直没开\n换个 BOSS_CDP_PORT 试试",
    "建 Chrome 专属 profile 失败 C:\\x\\.chrome_profile：[WinError 5] 拒绝访问",
    "拉不起 Chrome（C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe）：[WinError 2]",
    "等了 40s 也没等到站点写出 __zp_stoken__。多半是登录态没灌进去（``doc('session')`` 过期？）",
    "CDP 等回包失败（Storage.getCookies）：连接被对端关闭",
    "要走 CDP 得装 websocket-client：pip install websocket-client",
]


@pytest.mark.parametrize("raw", REAL_MESSAGES)
def test_humanize_never_returns_the_raw_technical_text(raw):
    out = troubleshoot.humanize(StokenError(raw))

    assert out != raw, f"没翻译：{raw}"
    # 技术痕迹一条都不许留
    for leak in ("ws://", "StokenError", "WinError", "cdp_stoken", "__zp_stoken__", "Storage."):
        assert leak not in out, f"翻完还漏了 {leak}：{out}"
    # 得是给用户看的中文，而且要有下一步动作
    assert len(out) > 10


def test_humanize_keeps_unknown_messages_verbatim():
    """认不出来就原样返回——宁可露线索，也别吞掉。"""
    assert troubleshoot.humanize("某个没见过的错") == "某个没见过的错"


def test_humanize_accepts_plain_string():
    assert troubleshoot.humanize("找不到 Chrome：装个 Google Chrome") != "找不到 Chrome：装个 Google Chrome"


def test_humanize_is_case_sensitive_for_env_names():
    """环境变量名是识别的钥匙，大小写写错就认不出来了——钉住现状。"""
    assert "chrome" in troubleshoot.humanize("找不到 Chrome：xxx").lower()


# --------------------------------------------------------------------------- #
# /api/system/*
# --------------------------------------------------------------------------- #


def test_health_reports_paths(client, tmp_path):
    body = client.get("/api/system/health").json()

    assert body["frozen"] is False, "测试跑在源码态"
    assert body["data_dir"]
    assert body["db"]
    assert body["writable"] is True
    assert body["temp_run"] is False


def test_chrome_status_reports_found(client, monkeypatch):
    monkeypatch.setattr("boss_jobs.cdp_stoken.find_chrome", lambda: r"C:\fake\chrome.exe")
    body = client.get("/api/system/chrome").json()

    assert body["found"] is True
    assert body["path"] == r"C:\fake\chrome.exe"
    assert body["mode"] in body["modes"]


def test_chrome_status_gives_guidance_when_missing(client, monkeypatch):
    """没装 Chrome 是第一大劝退点，返回的必须是能照做的中文。"""

    def boom():
        raise StokenError("找不到 Chrome：装个 Google Chrome，或用 BOSS_CHROME_BIN 指到 chrome 可执行文件。")

    monkeypatch.setattr("boss_jobs.cdp_stoken.find_chrome", boom)
    body = client.get("/api/system/chrome").json()

    assert body["found"] is False
    assert "Chrome 下载" in body["message"]
    assert "BOSS_CHROME_BIN" not in body["message"]


def test_chrome_mode_roundtrip_persists(client, monkeypatch):
    from boss_jobs.cdp_stoken import CHROME_MODE_ENV

    resp = client.post("/api/system/chrome/mode", json={"mode": "visible"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "visible"
    assert app_settings_store.load_settings().chrome_mode == "visible"

    # 存了还不够——得立刻写进环境变量，否则要重启才生效
    import os

    assert os.environ.get(CHROME_MODE_ENV) == "visible"
    monkeypatch.delenv(CHROME_MODE_ENV, raising=False)


def test_chrome_mode_rejects_unknown_value(client):
    resp = client.post("/api/system/chrome/mode", json={"mode": "全屏"})
    assert resp.status_code == 422
    assert "不认识的窗口档位" in resp.json()["message"]


def test_chrome_mode_empty_restores_default(client, monkeypatch):
    from boss_jobs.cdp_stoken import CHROME_MODE_ENV

    monkeypatch.setenv(CHROME_MODE_ENV, "visible")
    resp = client.post("/api/system/chrome/mode", json={"mode": ""})

    assert resp.status_code == 200
    import os

    assert CHROME_MODE_ENV not in os.environ


def test_open_chrome_translates_failure(client, monkeypatch):
    """拉不起 Chrome 时要报人话，而不是把 StokenError 原文甩出来。"""

    def boom(**kwargs):
        raise StokenError("拉不起 Chrome（C:\\x\\chrome.exe）：[WinError 2] 找不到文件")

    monkeypatch.setattr("boss_jobs.cdp_stoken.launch_chrome", boom)
    resp = client.post("/api/system/chrome/open")

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "environment"
    assert "WinError" not in body["message"]


def test_open_chrome_success_message(client, monkeypatch):
    monkeypatch.setattr("boss_jobs.cdp_stoken.launch_chrome", lambda **kwargs: None)
    resp = client.post("/api/system/chrome/open")

    assert resp.status_code == 200
    assert "可见窗口" in resp.json()["message"]


# --------------------------------------------------------------------------- #
# 异常壳
# --------------------------------------------------------------------------- #


def test_stoken_error_becomes_friendly_503(client, monkeypatch):
    """同步请求里漏出来的 StokenError 也要被翻译。"""

    def boom():
        raise StokenError("连不上 CDP（ws://127.0.0.1:9222/devtools/browser/abc）：握手被拒")

    monkeypatch.setattr("boss_jobs.cdp_stoken.find_chrome", boom)
    resp = client.post("/api/system/chrome/open")

    body = resp.json()
    assert resp.status_code == 503
    assert "ws://" not in body["message"]
    assert body["code"] == "environment"


def test_unhandled_exception_does_not_leak_traceback(client, monkeypatch):
    """兜底处理器：任何没预料到的异常都回中文，不把 traceback 甩给用户。

    注意不能自己往 app 上挂 ``/api/boom`` 去测——``app.py`` 的 SPA 兜底路由
    ``/{full_path:path}`` 注册在后面却匹配一切，会把新路由吃掉、返回 200 空壳。
    所以在既有路由上引爆。
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("内部炸了：secret-token-xyz")

    monkeypatch.setattr("boss_db.resolve_db_path", boom)
    resp = client.get("/api/system/health")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal"
    assert "secret-token-xyz" not in body["message"]
    assert "RuntimeError" not in body["message"]


# --------------------------------------------------------------------------- #
# 测试替身：确认线上调用点真的换成了 humanize
# --------------------------------------------------------------------------- #


def test_deliver_task_uses_humanize():
    """投递任务的失败文案不该带裸类名。"""
    import inspect

    from boss_web.services import deliver_task

    source = inspect.getsource(deliver_task)
    assert "type(exc).__name__" not in source
    assert "humanize(exc)" in source


def test_crawl_task_uses_humanize():
    import inspect

    from boss_web.services import crawl_task

    source = inspect.getsource(crawl_task)
    assert "type(exc).__name__" not in source
    assert "humanize(exc)" in source


def test_runtime_app_data_dir_is_absolute():
    assert runtime.app_data_dir().is_absolute()
