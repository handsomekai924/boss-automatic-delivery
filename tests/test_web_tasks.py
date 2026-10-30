"""任务中心：并发占用策略 + 统一 API + 统一 WS 的冒烟测试（不打真网）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from boss_web import create_app
from boss_web.errors import ConflictError
from boss_web.services.task_runtime import (
    POLICY_GLOBAL_SINGLE,
    POLICY_PER_KEY_SINGLE,
    POLICY_UNLIMITED,
    TaskTypeSpec,
    hub,
)


class FakeTask:
    """业务任务替身：只有任务中心要用的那几个字段。"""

    def __init__(self, task_id: str, status: str = "running") -> None:
        """初始化。"""
        self.task_id = task_id
        self.status = status
        self.events: list[dict] = []
        self.cancel_flag = False

    def push(self, event: dict) -> None:
        """记一条事件。"""
        self.events.append(event)

    def snapshot(self) -> dict:
        """规范化用的业务快照。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "current": "干活中",
            "done": 1,
            "total": 2,
            "events": self.events,
        }


@pytest.fixture
def client():
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


@pytest.fixture
def hub_spec():
    """注册一组测试类型；用完把类型登记还原，不打扰别的用例。"""
    saved = dict(hub._specs)  # noqa: SLF001 - 测试就是要贴着内部状态打补丁
    hub.register(TaskTypeSpec("crawl", title="职位爬取", policy=POLICY_GLOBAL_SINGLE, mutual_group="fetch"))
    hub.register(TaskTypeSpec("desc", title="补抓JD", policy=POLICY_GLOBAL_SINGLE, mutual_group="fetch"))
    hub.register(
        TaskTypeSpec(
            "parse",
            title="简历解析",
            policy=POLICY_PER_KEY_SINGLE,
            key_fn=lambda req: str(req.get("resume_id") or ""),
        )
    )
    hub.register(TaskTypeSpec("deliver", title="批量发送", policy=POLICY_GLOBAL_SINGLE))
    hub.register(TaskTypeSpec("match", title="简历匹配", policy=POLICY_UNLIMITED, max_parallel=2))
    yield
    hub._specs.clear()  # noqa: SLF001
    hub._specs.update(saved)  # noqa: SLF001


def test_global_single_拒绝并发(hub_spec):
    """全局串行类型：跑着一个就进不来第二个。"""
    t = FakeTask("c1")
    hub.occupy("crawl", request={})
    hub.track(t, task_type="crawl")
    with pytest.raises(ConflictError):
        hub.occupy("crawl", request={})


def test_mutual_group_跨类型互斥(hub_spec):
    """爬取与补 JD 同组，互相挡。"""
    hub.occupy("crawl", request={})
    hub.track(FakeTask("c1"), task_type="crawl")
    with pytest.raises(ConflictError):
        hub.occupy("desc", request={})


def test_per_key_same_key_冲突_different_key_放行(hub_spec):
    """同简历不让重复解析；不同简历并发解析可以。"""
    hub.occupy("parse", request={"resume_id": "r1"})
    hub.track(FakeTask("p1"), task_type="parse", request={"resume_id": "r1"})
    with pytest.raises(ConflictError):
        hub.occupy("parse", request={"resume_id": "r1"})
    keys = hub.occupy("parse", request={"resume_id": "r2"})
    assert keys == ["r2"]


def test_unlimited_并发有上限(hub_spec):
    """不限并发类型也有个上限兜底。"""
    hub.occupy("match", request={})
    hub.track(FakeTask("m1"), task_type="match")
    hub.occupy("match", request={})
    hub.track(FakeTask("m2"), task_type="match")
    with pytest.raises(ConflictError):
        hub.occupy("match", request={})


# --- 任务按站点排队（docs 6.5）：占用按 (site, key) 记账 ---


def test_per_key_single_同key不同站点可并行(hub_spec):
    """同一份简历在两个站点同时解析不互相挡——通道边界是站点。"""
    hub.occupy("parse", request={"site": "boss", "resume_id": "r1"})
    hub.track(FakeTask("p1"), task_type="parse", request={"site": "boss", "resume_id": "r1"})
    keys = hub.occupy("parse", request={"site": "51job", "resume_id": "r1"})
    assert keys == ["r1"]


def test_per_key_single_同key同站点仍冲突(hub_spec):
    """站点不同才放行；同站点同 key 照旧拦。"""
    hub.occupy("parse", request={"site": "boss", "resume_id": "r1"})
    hub.track(FakeTask("p1"), task_type="parse", request={"site": "boss", "resume_id": "r1"})
    with pytest.raises(ConflictError):
        hub.occupy("parse", request={"site": "boss", "resume_id": "r1"})


def test_global_single_按站点串行(hub_spec):
    """同型任务按站点排队：boss 在发，不挡 51job 发。"""
    hub.occupy("deliver", request={"site": "boss"})
    hub.track(FakeTask("d1"), task_type="deliver", request={"site": "boss"})
    assert hub.occupy("deliver", request={"site": "51job"}) == [""]
    with pytest.raises(ConflictError):
        hub.occupy("deliver", request={"site": "boss"})


def test_mutual_group_按站点分通道(hub_spec):
    """抓取/补抓共用**站点**通道：boss 在抓不挡 51job 抓，也不挡 51job 补抓。"""
    hub.occupy("crawl", request={"site": "boss"})
    hub.track(FakeTask("c1"), task_type="crawl", request={"site": "boss"})
    with pytest.raises(ConflictError):
        hub.occupy("desc", request={"site": "boss"})
    hub.occupy("crawl", request={"site": "51job"})
    hub.occupy("desc", request={"site": "51job"})


def test_unlimited_max_parallel_按站点计(hub_spec):
    """并发上限分站点算，不跨站点挤兑。"""
    hub.occupy("match", request={"site": "boss"})
    hub.track(FakeTask("m1"), task_type="match", request={"site": "boss"})
    hub.occupy("match", request={"site": "boss"})
    hub.track(FakeTask("m2"), task_type="match", request={"site": "boss"})
    with pytest.raises(ConflictError):
        hub.occupy("match", request={"site": "boss"})
    # 另一个站点还有额度
    hub.occupy("match", request={"site": "51job"})


def test_站点取值顺序_启动参数优先于类型自述(hub_spec):
    """`track()` 与 `occupy()` 算站点必须同一条规则，否则记账对不上。"""
    hub.register(TaskTypeSpec("login", title="登录", policy=POLICY_GLOBAL_SINGLE, site="boss"))
    try:
        hub.occupy("login", request={})
        hub.track(FakeTask("l1"), task_type="login", request={})
        assert hub.snapshot("l1")["site"] == "boss"  # spec 自述兜底
        with pytest.raises(ConflictError):
            hub.occupy("login", request={})
        # 启动参数里的 site 覆盖 spec 自述
        assert hub.occupy("login", request={"site": "51job"}) == [""]
    finally:
        hub._specs.pop("login", None)  # noqa: SLF001


def test_结束任务不占坑(hub_spec):
    """任务到终态后自动放行下一个。"""
    t = FakeTask("c1")
    hub.occupy("crawl", request={})
    hub.track(t, task_type="crawl")
    t.status = "done"
    assert hub.occupy("crawl", request={}) == [""]


def test_api_list_and_cancel(client: TestClient, hub_spec):
    """列表 / 取消 / 全停 / 删除走统一 API。"""
    t = FakeTask("c1")
    hub.occupy("crawl", request={})
    hub.track(t, task_type="crawl", title="抓「前端」")

    r = client.get("/api/tasks", params={"task_type": "crawl"})
    assert r.status_code == 200
    data = r.json()
    assert data["running_count"] == 1
    assert data["tasks"][0]["title"] == "抓「前端」"

    r = client.post("/api/tasks/c1/cancel")
    assert r.status_code == 200
    assert t.cancel_flag is True

    r = client.post("/api/tasks/cancel-all")
    assert r.status_code == 200

    t.status = "cancelled"
    r = client.delete("/api/tasks/c1")
    assert r.status_code == 200
    assert client.get("/api/tasks").json()["tasks"] == []


def test_api_list_items与tasks是同一个列表(client: TestClient, hub_spec):
    """`items` 是列表接口的通用键，`tasks` 留给 WS `hello` 与 legacy。

    两把钥匙开同一扇门，别只回一个键把另一侧晾着——前端按 `items` 取，
    WS 快照按 `tasks` 取，缺哪个都有人拿不到东西。
    """
    t = FakeTask("c1")
    hub.occupy("crawl", request={})
    hub.track(t, task_type="crawl", title="抓「前端」")
    data = client.get("/api/tasks").json()
    assert data["items"] == data["tasks"]
    assert [i["task_id"] for i in data["items"]] == ["c1"]


def test_api_delete_拒绝未结束(client: TestClient, hub_spec):
    """跑着的任务不能删。"""
    hub.occupy("crawl", request={})
    hub.track(FakeTask("c1"), task_type="crawl")
    assert client.delete("/api/tasks/c1").status_code == 409


def test_ws_hello_带全量快照(client: TestClient, hub_spec):
    """连接先给一帧 hello：任务列表 + 会话 + 更新进度。"""
    hub.occupy("crawl", request={})
    hub.track(FakeTask("c1"), task_type="crawl", title="抓「前端」")
    with client.websocket_connect("/api/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "hello"
        tasks = msg["data"]["tasks"]["tasks"]
        assert any(t["task_id"] == "c1" for t in tasks)
        assert "session" in msg["data"]
        assert "update" in msg["data"]


def test_ws_ping_resync(client: TestClient):
    """客户端 ping 回 pong；resync 重推任务快照。"""
    with client.websocket_connect("/api/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "resync"})
        assert ws.receive_json()["type"] == "task.snapshot"


def test_ws_断开不留未取回异常(client: TestClient, caplog):
    """客户端直接断开不能打 "Task exception was never retrieved"。

    `_consume` 是独立 task，断开时它带着 `WebSocketDisconnect` 死掉；
    端点必须把它的异常取回来，否则 asyncio 在 GC 时报 ERROR。
    """
    import gc
    import logging

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        for _ in range(3):
            with client.websocket_connect("/api/ws") as ws:
                ws.receive_json()
        gc.collect()
    leaked = [r for r in caplog.records if "never retrieved" in r.getMessage()]
    assert not leaked


def test_ws_坏载荷不炸连接(client: TestClient):
    """业务快照混进日期/字节也不许掀桌：降级成字符串照推，连接活着。

    一帧坏 JSON 曾能把整条连接炸掉（`json.dumps` 抛 TypeError 直接把
    端点带走），客户端随即重连，服务端日志里就剩一串
    `[accepted]` / `connection open`。
    """
    from datetime import datetime

    with client.websocket_connect("/api/ws") as ws:
        ws.receive_json()
        hub.broadcast(
            "task.progress",
            {"blob": b"\x00\x01", "at": datetime(2026, 10, 27)},
            task_type="login",
            task_id="t1",
        )
        msg = ws.receive_json()
        assert msg["type"] == "task.progress"
        assert isinstance(msg["data"]["blob"], str)
        assert isinstance(msg["data"]["at"], str)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
