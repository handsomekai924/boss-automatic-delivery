"""投递配置（一键投递评分阈值）的测试：落状态库 ``doc('deliver_config')``。

这里传的 ``p`` 是**库路径**；接口层由 conftest 的 ``isolated_db`` 把库指到 tmp_path。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import boss_db
from boss_jobs import GreetingDelivery, Job, JobApiError
from boss_jobs.config import BROWSER_CHECK_GIVEUP
from boss_jobs.models import PageResult
from boss_jobs.store import JobStore
from boss_web import config as C
from boss_web import create_app
from boss_web.errors import ConflictError
from boss_web.services.deliver_config_store import (
    DeliverConfig,
    load_config,
    save_config,
    update_config,
)
from boss_web.services.deliver_task import DeliverTaskManager
from boss_web.services.resume_store import load_analysis, save_analysis


class FakeGreetClient:
    """假的 ``JobClient``：按序回脚本化结果，异常实例直接抛，并记下每次调的参数。

    按的是 :meth:`boss_web.services.deliver_task` 真正调的那个方法
    （``deliver_greeting``，建会话 + 把正文投出去那步）；响应池空了就一律成功
    ——多数用例只关心前几条怎么排。
    """

    def __init__(self, results=()) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def deliver_greeting(self, **kwargs) -> GreetingDelivery:
        self.calls.append(kwargs)
        item = self._results.pop(0) if self._results else None
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, GreetingDelivery):
            return item
        return GreetingDelivery(boss_uid=0, text=str(kwargs.get("greeting") or ""))


@pytest.fixture
def client():
    """状态库由 ``isolated_db`` 统一指到 ``tmp_path``，这里只起 app。"""
    return TestClient(create_app())




def test_default_is_70(tmp_path):
    """库里没有这份配置时，读出来是默认 70。"""
    cfg = load_config(tmp_path / "boss.db")
    assert cfg.min_score == C.DEFAULT_DELIVER_MIN_SCORE == 70


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "boss.db"
    save_config(DeliverConfig(min_score=85), p)
    assert load_config(p).min_score == 85
    assert boss_db.doc_get(boss_db.DOC_DELIVER_CONFIG, p)["min_score"] == 85


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (150, 100),  # 上越界夹到 100
        (-5, 0),  # 下越界夹到 0
        ("88", 88),  # 数字串能收
        ("abc", C.DEFAULT_DELIVER_MIN_SCORE),  # 非数字回默认
        (None, C.DEFAULT_DELIVER_MIN_SCORE),  # 空值回默认
    ],
)
def test_min_score_is_normalized(tmp_path, raw, expected):
    p = tmp_path / "boss.db"
    save_config(DeliverConfig(min_score=raw), p)
    assert load_config(p).min_score == expected


def test_corrupt_payload_falls_back(tmp_path):
    """payload 不是合法 JSON → 按默认处理，不抛。"""
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_DELIVER_CONFIG, "{不是 JSON", p)
    assert load_config(p).min_score == C.DEFAULT_DELIVER_MIN_SCORE


def test_update_config_persists(tmp_path):
    """写得进、读得出；传 ``None`` = 不改原值。"""
    p = tmp_path / "boss.db"
    assert update_config({"min_score": 60}, p).min_score == 60
    assert load_config(p).min_score == 60
    assert update_config({"min_score": None}, p).min_score == 60




def test_api_get_default(client: TestClient):
    r = client.get("/api/deliver/config")
    assert r.status_code == 200
    assert r.json()["min_score"] == C.DEFAULT_DELIVER_MIN_SCORE


def test_api_put_roundtrip(client: TestClient):
    r = client.put("/api/deliver/config", json={"min_score": 85})
    assert r.status_code == 200
    assert r.json()["min_score"] == 85
    assert client.get("/api/deliver/config").json()["min_score"] == 85


@pytest.mark.parametrize("bad", [150, -1])
def test_api_put_rejects_out_of_range(client: TestClient, bad: int):
    """越界直接 422，坏值写不进去，读出来仍是默认分。"""
    r = client.put("/api/deliver/config", json={"min_score": bad})
    assert r.status_code == 422
    assert client.get("/api/deliver/config").json()["min_score"] == C.DEFAULT_DELIVER_MIN_SCORE




def _match(jid: str, **overrides) -> dict:
    item = {
        "encrypt_job_id": jid,
        "job_name": f"岗位 {jid}",
        "brand_name": "示例科技",
        "match_score": 90,
        "greeting": f"你好，我对 {jid} 很感兴趣",
        "security_id": f"sec-{jid}",
        "lid": "L1",
    }
    item.update(overrides)
    return item


def _seed_analysis(matches, analysis_id: str = "an_t1") -> str:
    return save_analysis(
        {
            "analysis_id": analysis_id,
            "resume_id": "rs_1",
            "resume_title": "简历",
            "created_at": time.time(),
            "status": "done",
            "matches": list(matches),
        }
    )


def _wait_task(mgr, timeout: float = 5.0) -> dict:
    """等任务跑完（线程里跑的），超时就报错——正常路径秒完。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = mgr.snapshot()
        if snap.get("status") != "running":
            return snap
        time.sleep(0.01)
    raise AssertionError(f"发送任务没在 {timeout}s 内结束：{mgr.snapshot()}")


def _run_deliver(matches, job_ids, client, *, interval: float = 0.0):
    """起一个发送任务（假客户端 + no-op sleep）并等它跑完，回 ``(snapshot, analysis_id)``。"""
    aid = _seed_analysis(matches)
    mgr = DeliverTaskManager(
        client_factory=lambda: client, sleeper=lambda _s: None, interval=interval
    )
    mgr.start(analysis_id=aid, job_ids=job_ids)
    return _wait_task(mgr), aid


def _match_of(analysis_id: str, jid: str) -> dict:
    return next(m for m in load_analysis(analysis_id)["matches"] if m["encrypt_job_id"] == jid)


def test_deliver_happy_path_sends_and_writes_back():
    """正常：逐条发完 → 任务 done，结果写回 payload（status ok + delivered_at）。

    打招呼用 payload 里的 securityId / lid（库里没有这条职位时的兜底）。
    """
    client = FakeGreetClient()
    snap, aid = _run_deliver([_match("j1"), _match("j2")], ["j1", "j2"], client)

    assert snap["status"] == "done"
    assert (snap["total"], snap["done"], snap["ok"], snap["failed"]) == (2, 2, 2, 0)
    assert [c["encrypt_job_id"] for c in client.calls] == ["j1", "j2"]
    assert client.calls[0]["security_id"] == "sec-j1"
    assert client.calls[0]["lid"] == "L1"
    assert client.calls[0]["greeting"] == "你好，我对 j1 很感兴趣"

    row = _match_of(aid, "j1")
    assert row["deliver_status"] == "ok"
    assert row["delivered_at"] > 0
    assert row["deliver_error"] == ""


def test_deliver_skips_already_delivered():
    """已成功的不重发：delivered_at 有值直接跳过，也不打请求。"""
    client = FakeGreetClient()
    matches = [_match("j1", delivered_at=123.0), _match("j2")]
    snap, aid = _run_deliver(matches, ["j1", "j2"], client)

    assert snap["status"] == "done"
    assert snap["skipped"] == 1
    assert (snap["total"], snap["ok"]) == (1, 1)
    assert [c["encrypt_job_id"] for c in client.calls] == ["j2"]
    assert [i["status"] for i in snap["items"] if i["encrypt_job_id"] == "j1"] == ["skipped"]
    assert _match_of(aid, "j1")["delivered_at"] == 123.0


def test_deliver_all_skipped_is_done_not_error():
    """一整个清单都发过了 → 空跑一场，照样算 done，不算错。"""
    client = FakeGreetClient()
    snap, _ = _run_deliver([_match("j1", delivered_at=1.0)], ["j1"], client)
    assert snap["status"] == "done"
    assert (snap["total"], snap["skipped"]) == (0, 1)
    assert client.calls == []


def test_deliver_code36_stops_whole_batch():
    """code 36（账号异常）→ 立刻停整批，后面的不再发，等人工处理；j3 从头到尾不被碰。"""
    client = FakeGreetClient([None, JobApiError(36, "您的账户存在异常行为")])
    matches = [_match("j1"), _match("j2"), _match("j3")]
    snap, aid = _run_deliver(matches, ["j1", "j2", "j3"], client)

    assert snap["status"] == "error"
    assert "账号异常" in snap["error"]
    assert [c["encrypt_job_id"] for c in client.calls] == ["j1", "j2"]
    assert (snap["ok"], snap["failed"]) == (1, 1)
    assert _match_of(aid, "j2")["deliver_status"] == "failed"
    assert "账号异常" in _match_of(aid, "j2")["deliver_error"]
    assert "deliver_status" not in _match_of(aid, "j3")


def test_deliver_session_expired_stops_batch():
    """登录态失效（code 1）→ 后面每一条都会失败，停批提示先重新登录。"""
    client = FakeGreetClient([JobApiError(1, "当前登录状态已失效")])
    snap, _ = _run_deliver([_match("j1"), _match("j2")], ["j1", "j2"], client)
    assert snap["status"] == "error"
    assert "登录" in snap["error"]
    assert len(client.calls) == 1


def _chat_remind_exc(content: str) -> JobApiError:
    """按真实弹窗结构造一个「开聊提醒」异常，让 ``is_chat_remind`` 靠结构认出来。"""
    from boss_jobs.errors import format_api_message

    raw = {
        "code": 1,
        "message": "开聊提醒",
        "zpData": {
            "bizData": {
                "chatRemindDialog": {
                    "title": "温馨提示",
                    "content": content,
                    "remindType": 524288,
                    "blockLevel": 0,
                }
            }
        },
    }
    return JobApiError(1, format_api_message(raw), raw=raw)


def test_deliver_chat_remind_continues_batch_without_login_hint():
    """「开聊提醒」是**提示弹窗**，不是停批信号；更别叫人去重新登录。

    实测（2026-10-09）：``friend/add.json`` 回 code 1 + chatRemindDialog，
    话术是「您今天已与120位BOSS沟通，还剩30次沟通机会哦」——那 30 是**还能再
    发的次数**。以前 code 1 被一律当登录态失效，界面提示「请先到登录页重新
    登录」；后来又改成整批停手，剩下的 30 次白白浪费。

    ``JobClient.greet`` 里已经做过「模拟点击确认」（cid=1）；确认完还被拦
    （这条用例）只记这一条，**继续发剩下的**。
    """
    exc = _chat_remind_exc("您今天已与120位BOSS沟通，还剩30次沟通机会哦")
    client = FakeGreetClient([exc, None])
    snap, aid = _run_deliver([_match("j1"), _match("j2")], ["j1", "j2"], client)

    assert snap["status"] == "done"
    assert snap["error"] is None
    assert (snap["ok"], snap["failed"]) == (1, 1)
    assert len(client.calls) == 2
    assert "还剩30次沟通机会" in _match_of(aid, "j1")["deliver_error"]
    assert "重新登录" not in _match_of(aid, "j1")["deliver_error"]
    assert _match_of(aid, "j2")["deliver_status"] == "ok"


def test_deliver_chat_remind_tomorrow_message_stops_batch():
    """达到每日沟通上限并提示明天再来时，当前失败后立即停整批。"""
    message = "您今天已与150位BOSS沟通，休息一下，明天再来吧"
    client = FakeGreetClient([_chat_remind_exc(message)])
    snap, aid = _run_deliver(
        [_match("j1"), _match("j2"), _match("j3")], ["j1", "j2", "j3"], client
    )

    assert snap["status"] == "error"
    assert "沟通配额" in snap["error"]
    assert "明天再来" in snap["error"]
    assert (snap["done"], snap["failed"], snap["ok"]) == (1, 1, 0)
    assert [call["encrypt_job_id"] for call in client.calls] == ["j1"]
    assert "明天再来" in _match_of(aid, "j1")["deliver_error"]
    assert "deliver_status" not in _match_of(aid, "j2")
    assert "deliver_status" not in _match_of(aid, "j3")


def test_deliver_chat_remind_remaining_zero_stops_batch():
    """话术明说「还剩 0 次」= 今天的量真见底了，停批（但仍不是登录失效）。"""
    exc = _chat_remind_exc("您今天已与150位BOSS沟通，还剩0次沟通机会哦")
    client = FakeGreetClient([exc])
    snap, aid = _run_deliver([_match("j1"), _match("j2")], ["j1", "j2"], client)

    assert snap["status"] == "error"
    assert "沟通配额" in snap["error"]
    assert "还剩0次沟通机会" in snap["error"]
    assert "重新登录" not in snap["error"]
    assert len(client.calls) == 1
    assert "开聊提醒" in _match_of(aid, "j1")["deliver_error"]


def test_deliver_single_failure_does_not_stop_batch():
    """单条业务码失败只记这条，下一条继续（失败不自动重试）。"""
    client = FakeGreetClient([JobApiError(1001, "参数错误"), None])
    snap, aid = _run_deliver([_match("j1"), _match("j2")], ["j1", "j2"], client)

    assert snap["status"] == "done"
    assert (snap["ok"], snap["failed"]) == (1, 1)
    assert len(client.calls) == 2
    assert _match_of(aid, "j1")["deliver_status"] == "failed"
    assert _match_of(aid, "j1")["deliver_error"] == "参数错误"
    assert "delivered_at" not in _match_of(aid, "j1")
    assert _match_of(aid, "j2")["deliver_status"] == "ok"


def test_deliver_generic_exception_does_not_stop_batch():
    """网络层等非业务异常也只拖垮这一条，整批照跑。"""
    client = FakeGreetClient([RuntimeError("连接被重置"), None])
    snap, _ = _run_deliver([_match("j1"), _match("j2")], ["j1", "j2"], client)
    assert snap["status"] == "done"
    assert (snap["ok"], snap["failed"]) == (1, 1)
    assert len(client.calls) == 2


def test_deliver_stops_after_repeated_37():
    """连续撞 code 37 到 BROWSER_CHECK_GIVEUP 次就停批——别撞着墙继续敲。"""
    client = FakeGreetClient([JobApiError(37, "您的环境存在异常.")] * 5)
    matches = [_match(f"j{i}") for i in range(1, 6)]
    snap, _ = _run_deliver(matches, [f"j{i}" for i in range(1, 6)], client)

    assert snap["status"] == "error"
    assert "37" in snap["error"]
    assert len(client.calls) == BROWSER_CHECK_GIVEUP


def test_deliver_37_然后成功_连击清零():
    """37 之后成功一次，连击计数清零——不会攒够次数就误停批。"""
    client = FakeGreetClient(
        [
            JobApiError(37, "您的环境存在异常."),
            None,
            JobApiError(37, "您的环境存在异常."),
            None,
        ]
    )
    snap, _ = _run_deliver(
        [_match(f"j{i}") for i in range(1, 5)], [f"j{i}" for i in range(1, 5)], client
    )
    assert snap["status"] == "done"
    assert (snap["ok"], snap["failed"]) == (2, 2)


def test_deliver_missing_security_id_fails_that_item():
    """职位、payload 都没有 securityId → 这条记失败并写明原因，不白打一发。"""
    client = FakeGreetClient()
    matches = [_match("j1"), _match("j2", security_id="")]
    snap, aid = _run_deliver(matches, ["j1", "j2"], client)

    assert [c["encrypt_job_id"] for c in client.calls] == ["j1"]
    assert "securityId" in _match_of(aid, "j2")["deliver_error"]
    assert snap["failed"] == 1


def test_deliver_uses_job_row_when_available():
    """库里有这条职位 → 用库里的 securityId / lid / encryptBossId（优先级高于 payload）。"""
    with JobStore() as store:
        store.save_page(
            PageResult(
                page=1,
                jobs=(
                    Job.from_api(
                        {
                            "encryptJobId": "j1",
                            "jobName": "岗位 j1",
                            "brandName": "示例科技",
                            "securityId": "SEC-DB",
                            "lid": "LID-DB",
                            "encryptBossId": "BOSS-DB",
                        }
                    ),
                ),
                has_more=False,
                raw_count=1,
            )
        )

    client = FakeGreetClient()
    _, aid = _run_deliver([_match("j1", security_id="SEC-PAYLOAD")], ["j1"], client)
    call = client.calls[0]
    assert call["security_id"] == "SEC-DB"
    assert call["lid"] == "LID-DB"
    assert call["encrypt_boss_id"] == "BOSS-DB"
    assert _match_of(aid, "j1")["deliver_status"] == "ok"


def test_deliver_ignores_ids_not_in_analysis():
    """清单里混进分析结果没有的 id → 忽略它，不炸。"""
    client = FakeGreetClient()
    snap, _ = _run_deliver([_match("j1")], ["j1", "不存在"], client)
    assert snap["status"] == "done"
    assert snap["ok"] == 1
    assert [c["encrypt_job_id"] for c in client.calls] == ["j1"]


def test_deliver_unknown_analysis_fails_task():
    """分析结果不存在 → 整批起不来，任务报错。"""
    client = FakeGreetClient()
    mgr = DeliverTaskManager(
        client_factory=lambda: client, sleeper=lambda _s: None, interval=0.0
    )
    mgr.start(analysis_id="an_不存在", job_ids=["j1"])
    snap = _wait_task(mgr)
    assert snap["status"] == "error"
    assert "不存在" in snap["error"]


def test_deliver_cancel_stops_between_items():
    """取消：发完手上这条就停，状态置 cancelled；已发的不回滚。

    sleep 留出窗口，让测试在第一条发完前按下取消。
    """
    started = threading.Event()

    class BlockingClient(FakeGreetClient):
        def deliver_greeting(self, **kwargs):
            started.set()
            time.sleep(0.15)
            return super().deliver_greeting(**kwargs)

    client = BlockingClient()
    aid = _seed_analysis([_match("j1"), _match("j2")])
    mgr = DeliverTaskManager(
        client_factory=lambda: client, sleeper=lambda _s: None, interval=0.0
    )
    mgr.start(analysis_id=aid, job_ids=["j1", "j2"])
    assert started.wait(2.0)

    mgr.current().cancel_flag = True
    snap = _wait_task(mgr)

    assert snap["status"] == "cancelled"
    assert [c["encrypt_job_id"] for c in client.calls] == ["j1"]
    assert _match_of(aid, "j1")["deliver_status"] == "ok"


def test_deliver_refuses_second_task_while_running():
    """同一时刻只跑一个：跑着的时候再 start → 409。"""
    gate = threading.Event()

    class BlockingClient(FakeGreetClient):
        def greet(self, **kwargs):
            gate.wait(2.0)
            return super().greet(**kwargs)

    client = BlockingClient()
    aid = _seed_analysis([_match("j1")])
    mgr = DeliverTaskManager(
        client_factory=lambda: client, sleeper=lambda _s: None, interval=0.0
    )
    mgr.start(analysis_id=aid, job_ids=["j1"])
    try:
        with pytest.raises(ConflictError):
            mgr.start(analysis_id=aid, job_ids=["j1"])
    finally:
        gate.set()
    assert _wait_task(mgr)["status"] == "done"




@pytest.fixture
def fake_client() -> FakeGreetClient:
    return FakeGreetClient()


@pytest.fixture
def patched_manager(monkeypatch, fake_client):
    """把接口层用的全局任务管理器换成注入假客户端的——绝不打真网络。"""
    import boss_web.api.deliver as api_deliver

    mgr = DeliverTaskManager(
        client_factory=lambda: fake_client, sleeper=lambda _s: None, interval=0.0
    )
    monkeypatch.setattr(api_deliver, "deliver_tasks", mgr)
    return mgr


def test_api_status_idle_before_any_task(client: TestClient, patched_manager):
    assert client.get("/api/deliver/status").json() == {"task_id": None, "status": "idle"}


def test_api_start_rejects_empty_ids(client: TestClient, patched_manager):
    r = client.post("/api/deliver/start", json={"analysis_id": "an_x", "encrypt_job_ids": []})
    assert r.status_code == 422
    assert "没有要发送的岗位" in r.json()["message"]


def test_api_start_then_status(client: TestClient, fake_client, patched_manager):
    aid = _seed_analysis([_match("j1"), _match("j2")])
    r = client.post(
        "/api/deliver/start", json={"analysis_id": aid, "encrypt_job_ids": ["j1", "j2"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"].startswith("dv_")
    assert body["status"] == "running"

    snap = _wait_task(patched_manager)
    assert snap["status"] == "done"
    assert snap["ok"] == 2
    assert client.get("/api/deliver/status").json()["ok"] == 2


def test_api_cancel_without_task_is_404(client: TestClient, patched_manager):
    r = client.post("/api/deliver/dv_不存在/cancel")
    assert r.status_code == 404
