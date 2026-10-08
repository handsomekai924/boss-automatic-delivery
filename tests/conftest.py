"""共享夹具：给集成测试和 CLI 测试提供同一个假服务端。"""

from __future__ import annotations

import threading

import pytest

from boss_login import ZhipinLoginClient
from tools.mock_server import BURNED_TICKETS, LAST_SMS_REQUEST, STORE, serve


@pytest.fixture(scope="module")
def server():
    httpd = serve(port=0)  # 交给系统分配空闲端口
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def clean_store():
    import tools.mock_server as mock_server

    STORE.clear()
    LAST_SMS_REQUEST.clear()
    BURNED_TICKETS.clear()
    mock_server.FILTER_API_DOWN = False
    yield
    STORE.clear()
    LAST_SMS_REQUEST.clear()
    BURNED_TICKETS.clear()
    mock_server.FILTER_API_DOWN = False


@pytest.fixture(autouse=True)
def no_real_chrome(monkeypatch, tmp_path):
    """测试里绝不真拉 Chrome，也绝不写项目里的真 ``stoken.json`` / ``session.json``。

    ``fetch`` 默认会为 ``__zp_stoken__`` 起一台带调试口的 Chrome，并把令牌落到
    项目根（见 :mod:`boss_jobs.cdp_stoken`）。那条路要真浏览器，还会污染真账本，
    所以统一把换新动作换成假令牌、把落盘路径挪进临时目录。要真跑端到端，用
    ``tools/wire_search.py``。
    """
    monkeypatch.setattr(
        "boss_jobs.cdp_stoken.CdpStokenProvider._acquire_from_chrome",
        lambda self: "0138FAKECDP",
    )
    monkeypatch.setenv("BOSS_STOKEN_STORE", str(tmp_path / "stoken.json"))
    monkeypatch.setattr(
        "boss_jobs.cdp_stoken.DEFAULT_STORE_PATH", tmp_path / "stoken.json"
    )
    # CLI 默认 --session 指向项目根 session.json；测试里挪走，免得把假令牌镜像进去
    fake_session = tmp_path / "session.json"
    monkeypatch.setattr("boss_jobs.config.DEFAULT_SESSION_PATH", fake_session)
    monkeypatch.setattr("boss_jobs.cli.DEFAULT_SESSION_PATH", fake_session)


@pytest.fixture
def client(server):
    # retries=0：集成测试里不希望被重试掩盖问题
    return ZhipinLoginClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None)
