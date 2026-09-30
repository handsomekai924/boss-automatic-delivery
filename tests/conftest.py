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


@pytest.fixture
def client(server):
    # retries=0：集成测试里不希望被重试掩盖问题
    return ZhipinLoginClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None)
