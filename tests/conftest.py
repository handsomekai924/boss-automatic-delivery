"""共享夹具：给集成测试和 CLI 测试提供同一个假服务端。

一条规矩：**测试绝不碰真状态库、也绝不真拉 Chrome**。``isolated_db`` 把
``BOSS_DB`` 指到临时目录，整套 ``load_*/save_*/clear_*`` 自动跟着走（它们
都是调用时才解析路径），顺手把 CDP 换新换成假令牌。要真跑端到端，用
``tools/wire_search.py``。
"""

from __future__ import annotations

import threading

import pytest

from boss_login import ZhipinLoginClient
from tools.mock_server import BURNED_TICKETS, LAST_SMS_REQUEST, STORE, serve


@pytest.fixture(scope="module")
def server():
    """起一台假服务端；``port=0`` 交给系统分配空闲端口，免得测试互相抢。"""
    httpd = serve(port=0)
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
def isolated_db(monkeypatch, tmp_path):
    """一处隔离：整个测试跑在 ``tmp_path/boss.db`` 上，收尾把连接都关掉。

    ``fetch`` 默认会为 ``__zp_stoken__`` 起一台带调试口的 Chrome。那条路要真
    浏览器，所以统一把换新动作换成假令牌（落盘仍然走真 ``StokenStore``）。
    """
    import boss_db

    monkeypatch.setenv(boss_db.DB_ENV, str(tmp_path / "boss.db"))
    monkeypatch.setattr(
        "boss_jobs.cdp_stoken.CdpStokenProvider._acquire_from_chrome",
        lambda self: "0138FAKECDP",
    )
    yield
    boss_db.close_all()


@pytest.fixture
def client(server):
    """登录客户端；``retries=0`` 避免重试把集成问题掩盖掉。"""
    return ZhipinLoginClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None)
