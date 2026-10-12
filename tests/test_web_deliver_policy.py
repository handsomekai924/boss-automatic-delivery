"""投递策略：mode=all 排除、selected/manual 确认、匹配时快照不可变。

规矩：假发送客户端，不打真网络。策略读的是 **analysis 里那份匹配时快照**，
不在发送中途重算当前规则。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from boss_jobs.models import PageResult
from boss_jobs.store import JobStore
from boss_web import create_app
from boss_web.services.deliver_task import DeliverTaskManager
from boss_web.services.resume_store import load_analysis, save_analysis

# 匹配时安全快照的必备字段（缺一就当旧分析，自动投递一律拒）
SAFETY_FIELDS = (
    "is_blacklisted",
    "company_blacklisted",
    "jd_blacklisted",
    "jd_rule_checked",
    "jd_status",
    "auto_delivery_blocked",
    "matched_rule_snapshot",
)


class FakeGreetClient:
    """假 ``JobClient``：只记录 ``deliver_greeting`` 调用，不打网络。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def deliver_greeting(self, **kwargs):
        from boss_jobs import GreetingDelivery

        self.calls.append(kwargs)
        return GreetingDelivery(boss_uid=0, text=str(kwargs.get("greeting") or ""))


@pytest.fixture
def client():
    return TestClient(create_app())


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


def _safe(jid: str, **overrides) -> dict:
    """完整的匹配时安全快照 + 无限制状态。"""
    item = _match(jid)
    item.update(
        {
            "is_blacklisted": False,
            "company_blacklisted": False,
            "jd_blacklisted": False,
            "jd_rule_checked": True,
            "jd_status": "completed",
            "auto_delivery_blocked": False,
            "matched_rule_snapshot": [],
        }
    )
    item.update(overrides)
    return item


def _seed(matches, analysis_id: str = "an_p1") -> str:
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


def _wait(mgr, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = mgr.snapshot()
        if snap.get("status") != "running":
            return snap
        time.sleep(0.01)
    raise AssertionError(f"发送任务没在 {timeout}s 内结束：{mgr.snapshot()}")


def _run(matches, *, mode="selected", confirmed=False, confirmation_token="", job_ids=None, min_score=None):
    """起任务、等结束，回 ``(snapshot, fake_client, analysis_id)``。"""
    aid = _seed(matches)
    fake = FakeGreetClient()
    mgr = DeliverTaskManager(client_factory=lambda: fake, sleeper=lambda _s: None, interval=0.0)
    if min_score is not None:
        from boss_web.services.deliver_config_store import save_config, DeliverConfig

        save_config(DeliverConfig(min_score=min_score))
    ids = job_ids if job_ids is not None else [m["encrypt_job_id"] for m in matches]
    mgr.start(
        analysis_id=aid,
        job_ids=ids,
        mode=mode,
        confirmed=confirmed,
        confirmation_token=confirmation_token,
    )
    return _wait(mgr), fake, aid


def _items_by_id(snap: dict) -> dict[str, dict]:
    return {i["encrypt_job_id"]: i for i in snap.get("items") or []}


# --------------------------------------------------------------------------- #
# mode=all：服务端排除受限项，skipped 不是 failed，且不打发送客户端
# --------------------------------------------------------------------------- #


def test_all_排除公司黑名单且不调客户端():
    snap, fake, _ = _run(
        [_safe("j1"), _safe("j2", company_blacklisted=True, is_blacklisted=True)],
        mode="all",
    )
    assert snap["status"] == "done"
    assert snap["ok"] == 1
    assert snap["skipped"] == 1
    assert snap["failed"] == 0
    assert len(fake.calls) == 1
    items = _items_by_id(snap)
    assert items["j2"]["status"] == "skipped"
    assert "公司黑名单" in items["j2"]["error"]


def test_all_排除JD黑名单():
    snap, fake, _ = _run(
        [_safe("j1"), _safe("j2", jd_blacklisted=True, is_blacklisted=True)],
        mode="all",
    )
    assert snap["ok"] == 1 and snap["skipped"] == 1
    assert len(fake.calls) == 1
    assert "JD" in _items_by_id(snap)["j2"]["error"]


def test_all_排除JD未检查():
    snap, fake, _ = _run(
        [_safe("j1"), _safe("j2", jd_rule_checked=False, auto_delivery_blocked=True)],
        mode="all",
    )
    assert snap["ok"] == 1 and snap["skipped"] == 1
    assert "未检查" in _items_by_id(snap)["j2"]["error"]


def test_all_排除JD状态不完整():
    snap, fake, _ = _run(
        [_safe("j1"), _safe("j2", jd_status="failed")],
        mode="all",
    )
    assert snap["ok"] == 1 and snap["skipped"] == 1
    assert "JD 状态不完整" in _items_by_id(snap)["j2"]["error"]


def test_all_排除缺少招呼语():
    snap, fake, _ = _run(
        [_safe("j1"), _safe("j2", greeting="   ")],
        mode="all",
    )
    assert snap["ok"] == 1 and snap["skipped"] == 1
    assert "招呼语" in _items_by_id(snap)["j2"]["error"]


def test_all_排除低于阈值():
    snap, fake, _ = _run(
        [_safe("j1"), _safe("j2", match_score=50)],
        mode="all",
        min_score=70,
    )
    assert snap["ok"] == 1 and snap["skipped"] == 1
    assert "阈值" in _items_by_id(snap)["j2"]["error"]


def test_all_旧分析缺安全快照一律拒():
    """没有安全快照字段的旧匹配：自动投递一律拒，但计 skipped 不是 failed。"""
    snap, fake, _ = _run([_match("j1")], mode="all")
    assert snap["status"] == "done"
    assert snap["ok"] == 0
    assert snap["skipped"] == 1
    assert snap["failed"] == 0
    assert fake.calls == []
    assert "安全快照" in _items_by_id(snap)["j1"]["error"]


def test_all_快照缺一个字段也不算完整():
    item = _safe("j1")
    item.pop("auto_delivery_blocked")
    snap, fake, _ = _run([item], mode="all")
    assert snap["skipped"] == 1
    assert fake.calls == []
    assert "安全快照" in _items_by_id(snap)["j1"]["error"]


def test_all_跳过已发送的():
    snap, fake, _ = _run(
        [_safe("j1", delivered_at=time.time() - 10), _safe("j2")],
        mode="all",
    )
    assert snap["ok"] == 1
    assert snap["skipped"] == 1
    assert snap["failed"] == 0
    assert len(fake.calls) == 1
    assert _items_by_id(snap)["j1"]["error"] == "已发送"


def test_all_全部受限时零发送且任务done():
    snap, fake, _ = _run(
        [
            _safe("j1", company_blacklisted=True),
            _safe("j2", jd_blacklisted=True),
            _safe("j3", jd_rule_checked=False),
        ],
        mode="all",
    )
    assert snap["status"] == "done"
    assert snap["ok"] == 0
    assert snap["skipped"] == 3
    assert snap["failed"] == 0
    assert fake.calls == []


def test_all_混杂时只发合格的():
    snap, fake, _ = _run(
        [
            _safe("ok1"),
            _safe("bad-co", company_blacklisted=True),
            _safe("ok2"),
            _safe("bad-jd", jd_blacklisted=True),
        ],
        mode="all",
    )
    assert snap["ok"] == 2
    assert snap["skipped"] == 2
    assert snap["status"] == "done"
    sent = sorted(str(c.get("security_id") or "") for c in fake.calls)
    assert sent == ["sec-ok1", "sec-ok2"]


# --------------------------------------------------------------------------- #
# mode=selected：受限项要汇总确认
# --------------------------------------------------------------------------- #


def test_selected_受限未确认_整批拒发():
    snap, fake, _ = _run(
        [_safe("ok1"), _safe("bad", company_blacklisted=True)],
        mode="selected",
        confirmed=False,
    )
    assert snap["status"] == "error"
    assert "确认" in (snap.get("error") or "")
    assert fake.calls == []
    assert snap["failed"] == 0
    items = _items_by_id(snap)
    assert items["bad"]["status"] == "skipped"
    assert "未确认" in items["bad"]["error"]


def test_selected_受限已确认_按清单发():
    snap, fake, _ = _run(
        [_safe("ok1"), _safe("bad", company_blacklisted=True)],
        mode="selected",
        confirmed=True,
    )
    assert snap["status"] == "done"
    assert snap["ok"] == 2
    assert len(fake.calls) == 2


def test_selected_无不受限项_不需确认直接发():
    snap, fake, _ = _run([_safe("j1"), _safe("j2")], mode="selected", confirmed=False)
    assert snap["status"] == "done"
    assert snap["ok"] == 2
    assert len(fake.calls) == 2


# --------------------------------------------------------------------------- #
# mode=manual：受限单条要二次确认（token）
# --------------------------------------------------------------------------- #


def test_manual_受限只有confirmed还不够():
    snap, fake, _ = _run(
        [_safe("bad", company_blacklisted=True)],
        mode="manual",
        confirmed=True,
        confirmation_token="",
    )
    assert snap["status"] == "error"
    assert "确认" in (snap.get("error") or "")
    assert fake.calls == []


def test_manual_受限带token才发():
    snap, fake, _ = _run(
        [_safe("bad", company_blacklisted=True)],
        mode="manual",
        confirmed=True,
        confirmation_token="ui-123",
    )
    assert snap["status"] == "done"
    assert snap["ok"] == 1
    assert len(fake.calls) == 1


def test_manual_普通职位不需要token():
    snap, fake, _ = _run([_safe("j1")], mode="manual", confirmed=False, confirmation_token="")
    assert snap["status"] == "done"
    assert snap["ok"] == 1


# --------------------------------------------------------------------------- #
# 快照不可变：发送只认匹配时那份，不读当前库/当前规则
# --------------------------------------------------------------------------- #


def test_规则事后改掉_仍按匹配时快照拦(tmp_path):
    """匹配时记了公司黑名单；库里的当前状态后来改成干净的，一键发送仍拒。"""
    aid = _seed([_safe("j1", company_blacklisted=True, is_blacklisted=True)])
    # 当前库状态：干净（规则删了 / 重检翻案了）
    store = JobStore(tmp_path / "boss.db")
    try:
        from boss_jobs.models import Job

        job = Job.from_api(
            {"encryptJobId": "j1", "jobName": "岗位 j1", "brandName": "示例科技", "securityId": "sec-j1", "lid": "L1"}
        )
        store.save_page(PageResult(page=1, jobs=(job,), has_more=False, raw_count=1))
        store.update_blacklist_state(
            "j1",
            company_blacklisted=False,
            jd_blacklisted=False,
            matched_rules=[],
            jd_rule_checked=True,
            jd_status="completed",
            auto_delivery_blocked=False,
        )
    finally:
        store.close()

    fake = FakeGreetClient()
    mgr = DeliverTaskManager(client_factory=lambda: fake, sleeper=lambda _s: None, interval=0.0)
    mgr.start(analysis_id=aid, job_ids=["j1"], mode="all", confirmed=False)
    snap = _wait(mgr)

    assert snap["status"] == "done"
    assert snap["skipped"] == 1
    assert snap["ok"] == 0
    assert fake.calls == []
    assert "公司黑名单" in _items_by_id(snap)["j1"]["error"]


def test_当前库被标黑_匹配快照干净_仍按快照发(tmp_path):
    """反向：匹配时是干净快照；库里后来被规则标黑，本批按快照放行。"""
    aid = _seed([_safe("j1")])
    store = JobStore(tmp_path / "boss.db")
    try:
        from boss_jobs.models import Job

        job = Job.from_api(
            {"encryptJobId": "j1", "jobName": "岗位 j1", "brandName": "示例科技", "securityId": "sec-j1", "lid": "L1"}
        )
        store.save_page(PageResult(page=1, jobs=(job,), has_more=False, raw_count=1))
        store.update_blacklist_state(
            "j1",
            company_blacklisted=True,
            jd_blacklisted=False,
            matched_rules=[{"rule_id": "r-late", "rule_type": "company"}],
            jd_rule_checked=True,
            jd_status="completed",
            auto_delivery_blocked=True,
        )
    finally:
        store.close()

    fake = FakeGreetClient()
    mgr = DeliverTaskManager(client_factory=lambda: fake, sleeper=lambda _s: None, interval=0.0)
    mgr.start(analysis_id=aid, job_ids=["j1"], mode="all", confirmed=False)
    snap = _wait(mgr)

    assert snap["status"] == "done"
    assert snap["ok"] == 1
    assert snap["skipped"] == 0
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# API 契约
# --------------------------------------------------------------------------- #


def test_api_start_接受mode与确认字段(client: TestClient, monkeypatch):
    import boss_web.api.deliver as api_deliver

    fake = FakeGreetClient()
    mgr = DeliverTaskManager(client_factory=lambda: fake, sleeper=lambda _s: None, interval=0.0)
    monkeypatch.setattr(api_deliver, "deliver_tasks", mgr)
    aid = _seed([_safe("j1", company_blacklisted=True)])

    r = client.post(
        "/api/deliver/start",
        json={
            "analysis_id": aid,
            "encrypt_job_ids": ["j1"],
            "mode": "manual",
            "confirmed": True,
            "confirmation_token": "tok-1",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "manual"
    assert body["confirmed"] is True
    assert body["confirmation_token"] == "tok-1"
    snap = _wait(mgr)
    assert snap["ok"] == 1


def test_api_拒绝未知mode(client: TestClient, monkeypatch):
    import boss_web.api.deliver as api_deliver

    mgr = DeliverTaskManager(client_factory=FakeGreetClient, sleeper=lambda _s: None, interval=0.0)
    monkeypatch.setattr(api_deliver, "deliver_tasks", mgr)
    aid = _seed([_safe("j1")])
    r = client.post(
        "/api/deliver/start",
        json={"analysis_id": aid, "encrypt_job_ids": ["j1"], "mode": "auto"},
    )
    assert r.status_code == 422


def test_api_旧客户端默认selected拿不到all权限(client: TestClient, monkeypatch):
    """老请求不带 mode → 按 selected 处理，不能白拿一键全部发送。"""
    import boss_web.api.deliver as api_deliver

    fake = FakeGreetClient()
    mgr = DeliverTaskManager(client_factory=lambda: fake, sleeper=lambda _s: None, interval=0.0)
    monkeypatch.setattr(api_deliver, "deliver_tasks", mgr)
    # 三条匹配，老客户端只交了其中一条 id；mode 默认 selected，不会自动把另外两条拉进来
    aid = _seed([_safe("j1"), _safe("j2"), _safe("j3")])
    r = client.post("/api/deliver/start", json={"analysis_id": aid, "encrypt_job_ids": ["j1"]})
    assert r.status_code == 200
    snap = _wait(mgr)
    assert snap["ok"] == 1
    assert len(fake.calls) == 1
