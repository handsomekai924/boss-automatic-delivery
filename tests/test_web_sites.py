"""平台作用域 API 前缀 / 登录态按站点隔离 / WS 信封带 site（docs §6.3）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_web import create_app
from boss_web.services.task_runtime import TaskTypeSpec, hub


class FakeTask:
    """业务任务替身：只有任务中心要用的那几个字段。"""

    def __init__(self, task_id: str, status: str = "running") -> None:
        """初始化。"""
        self.task_id = task_id
        self.status = status

    def push(self, event: dict) -> None:
        """记一条事件。"""

    def snapshot(self) -> dict:
        """规范化用的业务快照。"""
        return {"task_id": self.task_id, "status": self.status, "done": 0, "total": 1}


@pytest.fixture
def client():
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


@pytest.fixture
def hub_spec():
    """登记一条测试任务类型；用完还原，不打扰别的用例。"""
    saved = dict(hub._specs)  # noqa: SLF001 - 测试就是要贴着内部状态打补丁
    hub.register(TaskTypeSpec("crawl", title="职位爬取", site="boss"))
    hub.register(TaskTypeSpec("parse", title="简历解析"))
    yield
    hub._specs.clear()  # noqa: SLF001
    hub._specs.update(saved)  # noqa: SLF001


def test_sites_前缀走站点守卫(client: TestClient):
    """验 `/api/sites/{site}/…` 只认注册过的站点，没注册的直接 404。"""
    assert client.get("/api/sites/boss/auth/status").status_code == 200
    res = client.get("/api/sites/51job/auth/status")
    assert res.status_code == 404
    assert "51job" in res.json()["message"]


def test_旧端点留作兼容别名(client: TestClient):
    """验共存期旧 URL 原样可用（legacy 前端专供，语义 = site "boss"）。"""
    new = client.get("/api/sites/boss/auth/status").json()
    old = client.get("/api/auth/status").json()
    assert old == new


def test_别名不吃未知站点(client: TestClient):
    """验别名面上的路径参数不会被当成站点——`{site}` 段不存在，恒落 boss。"""
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/sites/boss/jobs").status_code == 200


def test_ws_信封带_site(client: TestClient, hub_spec):
    """验每帧都带 `site`：共用事件空串，站点事件带站点 id。"""
    hub.track(FakeTask("c1"), task_type="crawl", site="boss")
    with client.websocket_connect("/api/ws") as ws:
        hello = ws.receive_json()
        assert hello["site"] == ""
        assert hello["type"] == "hello"

        hub.broadcast("task.progress", {"x": 1}, task_type="crawl", task_id="c1", site="boss")
        frame = ws.receive_json()
        assert frame["site"] == "boss"


def test_ws_hello_带_sessions(client: TestClient):
    """验 `hello.sessions` 是全站会话摘要，形状与 `/api/platforms` 的 session 同构。"""
    with client.websocket_connect("/api/ws") as ws:
        hello = ws.receive_json()
    sessions = hello["data"]["sessions"]
    assert set(sessions) == {"boss"}
    assert set(sessions["boss"]) == {"logged_in", "phone_masked", "busy"}
    # 旧壳（legacy）认的单站摘要继续给，M3 随 legacy 一起删
    assert set(hello["data"]["session"]) >= {"logged_in", "phone_masked"}


def test_task_snapshot_带_site(client: TestClient, hub_spec):
    """验任务快照带 `site`，共用任务是空串。"""
    hub.track(FakeTask("c1"), task_type="crawl", site="boss")
    hub.track(FakeTask("p1"), task_type="parse")
    snap = client.get("/api/tasks/c1").json()
    assert snap["site"] == "boss"
    assert client.get("/api/tasks/p1").json()["site"] == ""


def test_任务列表按站点过滤(client: TestClient, hub_spec):
    """验 `?site=` 过滤：给了只看该站，不给全看，空串只看共用。"""
    hub.track(FakeTask("c1"), task_type="crawl", site="boss")
    hub.track(FakeTask("p1"), task_type="parse")

    def ids(qs: str) -> set[str]:
        body = client.get(f"/api/tasks{qs}").json()
        return {t["task_id"] for t in body["tasks"]}

    assert ids("") == {"c1", "p1"}
    assert ids("?site=boss") == {"c1"}
    assert ids("?site=") == {"p1"}
    assert ids("?site=51job") == set()


def test_任务类型自述站点也进快照(client: TestClient, hub_spec):
    """验启动参数没写 site 时按任务类型自述兜底（发对话/抓取这类站点通道）。"""
    hub.track(FakeTask("c1"), task_type="crawl")
    assert client.get("/api/tasks/c1").json()["site"] == "boss"
