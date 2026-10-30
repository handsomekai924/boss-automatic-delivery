"""站点限速/配额（`site_settings`）：仓储口 + 共享执行规则 + 投递队列落地。

限速是站点属性、执行是共享规则——这里验三层各自守好自己那段：
`boss_db` 只管存取，`boss_web.core.limits` 只管形状与熔断判据，
`deliver_task` 才是把它们落到发送循环上的地方。
"""

from __future__ import annotations

import time

import pytest

import boss_db
from boss_web.core.contracts import DeliverFault
from boss_web.core.limits import (
    BREAKER_TRIP,
    Breaker,
    FaultAction,
    SiteLimits,
    fault_action,
    throttle_hook,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """每条用例前后关掉连接缓存。"""
    boss_db.close_all()
    yield
    boss_db.close_all()


# ---------------------------------------------------------------------------
# boss_db：site_settings 的仓储口
# ---------------------------------------------------------------------------


def test_get_没写过这一行_回全默认(tmp_path):
    """库里没有这一行也算有值：节流 0 / 配额 0 / 并发 1 / 不投。"""
    s = boss_db.get_site_settings("boss", tmp_path / "boss.db")
    assert s == boss_db.SiteSettings(site="boss")


def test_set_写进表_读得回(tmp_path):
    """写完读回原值。"""
    p = tmp_path / "boss.db"
    boss_db.set_site_settings("boss", p, throttle_ms=1200, daily_quota=30, apply_enabled=True)
    s = boss_db.get_site_settings("boss", p)
    assert (s.throttle_ms, s.daily_quota, s.max_parallel, s.apply_enabled) == (1200, 30, 1, True)
    assert s.updated_at > 0


def test_set_传None的字段不动(tmp_path):
    """跟 `update_config` 同规矩：`None` = 保持原值。"""
    p = tmp_path / "boss.db"
    boss_db.set_site_settings("boss", p, throttle_ms=800, daily_quota=10)
    boss_db.set_site_settings("boss", p, daily_quota=25)
    s = boss_db.get_site_settings("boss", p)
    assert (s.throttle_ms, s.daily_quota) == (800, 25)


def test_set_坏值夹回合理区间(tmp_path):
    """负数顶到下限、并发至少 1、开关认真假。"""
    p = tmp_path / "boss.db"
    s = boss_db.set_site_settings(
        "boss", p, throttle_ms=-5, daily_quota=-1, max_parallel=0, apply_enabled=1
    )
    assert (s.throttle_ms, s.daily_quota, s.max_parallel, s.apply_enabled) == (0, 0, 1, True)
    s = boss_db.set_site_settings("boss", p, apply_enabled=0)
    assert s.apply_enabled is False


def test_list_只列写过的(tmp_path):
    """没写过行的站点不在列表里（用默认值即可）。"""
    p = tmp_path / "boss.db"
    boss_db.set_site_settings("boss", p, throttle_ms=500)
    boss_db.set_site_settings("51job", p, daily_quota=3)
    rows = boss_db.list_site_settings(p)
    assert [r.site for r in rows] == ["51job", "boss"]


def test_get_站点名不能为空(tmp_path):
    """站点名是主键，空名不许落。"""
    with pytest.raises(ValueError):
        boss_db.get_site_settings("", tmp_path / "boss.db")


def test_count_greeted_today_只数今天且只数本站(tmp_path):
    """账记在 `jobs.last_greeted_at`：昨天的不算，别的站点不算。"""
    p = tmp_path / "boss.db"
    conn = boss_db.connect(p, migrate=False)
    today = time.strftime("%Y-%m-%d", time.localtime())
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    rows = [
        ("j1", "boss", f"{today} 09:00:00"),
        ("j2", "boss", f"{today} 09:30:00"),
        ("j3", "boss", f"{yesterday} 23:00:00"),
        ("j4", "51job", f"{today} 09:00:00"),
        ("j5", "boss", ""),
    ]
    try:
        for jid, site, greeted in rows:
            conn.execute(
                "INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at, "
                "site, last_greeted_at) VALUES (?, 'n', 'b', '2026-10-28 08:00:00', ?, ?)",
                (jid, site, greeted),
            )
        conn.commit()
    finally:
        conn.close()
    assert boss_db.count_greeted_today("boss", path=p) == 2
    assert boss_db.count_greeted_today("51job", path=p) == 1


# ---------------------------------------------------------------------------
# boss_web.core.limits：形状 + 熔断判据
# ---------------------------------------------------------------------------


def test_from_mapping_认仓储行也认dict():
    """`boss_db.SiteSettings`、裸 dict、`None` 都收得住。"""
    row = boss_db.SiteSettings(site="boss", throttle_ms=700, daily_quota=5, apply_enabled=True)
    s = SiteLimits.from_mapping(row)
    assert (s.site, s.throttle_ms, s.daily_quota, s.apply_enabled) == ("boss", 700, 5, True)
    assert SiteLimits.from_mapping({"throttle_ms": "300"}).throttle_ms == 300
    assert SiteLimits.from_mapping(None) == SiteLimits()
    assert SiteLimits.from_mapping({"max_parallel": -2}).max_parallel == 1


def test_throttle_seconds_与配额余量():
    """毫秒换算秒；配额 0 = 不限。"""
    assert SiteLimits(throttle_ms=1500).throttle_seconds == 1.5
    assert SiteLimits().quota_left(99) is None
    assert SiteLimits(daily_quota=10).quota_left(7) == 3
    assert SiteLimits(daily_quota=10).quota_left(99) == 0
    assert SiteLimits(daily_quota=10).has_daily_quota is True


def test_fault_action_分类到动作():
    """停/退避/继续三档是共享规则，站点只管分类。"""
    assert fault_action(DeliverFault.QUOTA) == FaultAction.STOP
    assert fault_action(DeliverFault.SESSION_CLOSED) == FaultAction.STOP
    assert fault_action(DeliverFault.RISK_CONTROL) == FaultAction.STOP
    assert fault_action(DeliverFault.RATE_LIMIT) == FaultAction.COOLOFF
    assert fault_action(DeliverFault.UNKNOWN) == FaultAction.CONTINUE


def test_breaker_停类一发就断():
    """账号异常这类不该攒次数。"""
    b = Breaker()
    assert b.record(DeliverFault.RISK_CONTROL) == FaultAction.STOP
    assert b.tripped and b.reason


def test_breaker_退避类攒到阈值断():
    """限频攒满才断，中途每次只退避。"""
    b = Breaker(trip=3)
    assert b.record(DeliverFault.RATE_LIMIT) == FaultAction.COOLOFF
    assert b.record(DeliverFault.RATE_LIMIT) == FaultAction.COOLOFF
    assert b.record(DeliverFault.RATE_LIMIT) == FaultAction.STOP
    assert b.tripped and "3" in b.reason


def test_breaker_成功一次清零():
    """退避之后真发出去一条，计数归零。"""
    b = Breaker(trip=2)
    b.record(DeliverFault.RATE_LIMIT)
    b.ok()
    assert b.record(DeliverFault.RATE_LIMIT) == FaultAction.COOLOFF
    assert not b.tripped


def test_breaker_默认阈值是三():
    assert Breaker().trip == BREAKER_TRIP == 3


def test_throttle_hook_没配就不睡():
    """`throttle_ms=0` / 没传 limits = 不节流，站点默认另说。"""
    slept: list[float] = []
    throttle_hook(None, slept.append)()
    throttle_hook(SiteLimits(throttle_ms=0), slept.append)()
    assert slept == []


def test_throttle_hook_按站点节流():
    """配了就照 `throttle_ms` 睡。"""
    slept: list[float] = []
    tick = throttle_hook(SiteLimits(throttle_ms=1500), slept.append)
    tick()
    tick()
    assert slept == [1.5, 1.5]


# ---------------------------------------------------------------------------
# deliver_task：把 site_settings 落到发送循环上
# ---------------------------------------------------------------------------


def _safe(jid: str, **overrides) -> dict:
    """一条带完整安全快照、不受限的 match 条目。"""
    item = {
        "encrypt_job_id": jid,
        "job_name": f"岗位 {jid}",
        "brand_name": "示例科技",
        "match_score": 90,
        "greeting": f"你好，我对 {jid} 很感兴趣",
        "security_id": f"sec-{jid}",
        "lid": "L1",
        "is_blacklisted": False,
        "company_blacklisted": False,
        "jd_blacklisted": False,
        "jd_rule_checked": True,
        "jd_status": "completed",
        "auto_delivery_blocked": False,
        "matched_rule_snapshot": [],
    }
    item.update(overrides)
    return item


class FakeGreetClient:
    """假 `JobClient`：只记 `deliver_greeting`，不打网络。"""

    def __init__(self) -> None:
        """初始化。"""
        self.calls: list[dict] = []

    def deliver_greeting(self, **kwargs):
        """记一次招呼发送并回成功。"""
        from boss_jobs import GreetingDelivery

        self.calls.append(kwargs)
        return GreetingDelivery(boss_uid=0, text=str(kwargs.get("greeting") or ""))


def _run_deliver(matches: list[dict]):
    """落 analysis → 起发送任务 → 等结束，回 `(快照, 假客户端, 睡过的间隔)`。"""
    from boss_web.services.deliver_task import DeliverTaskManager
    from boss_web.services.resume_store import save_analysis

    aid = save_analysis(
        {
            "analysis_id": "an_limits",
            "resume_id": "rs_1",
            "resume_title": "简历",
            "created_at": time.time(),
            "status": "done",
            "matches": list(matches),
        }
    )
    fake = FakeGreetClient()
    slept: list[float] = []
    mgr = DeliverTaskManager(
        client_factory=lambda: fake, sleeper=slept.append, interval=0.0
    )
    mgr.start(
        analysis_id=aid,
        job_ids=[m["encrypt_job_id"] for m in matches],
        mode="selected",
        confirmed=True,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        snap = mgr.snapshot()
        if snap.get("status") != "running":
            return snap, fake, slept
        time.sleep(0.01)
    raise AssertionError(f"发送任务没在 5s 内结束：{mgr.snapshot()}")


def test_节流间隔_站点配了就盖掉全局默认():
    """`site_settings.throttle_ms` 是站点属性，盖掉 `DELIVER_INTERVAL`。"""
    boss_db.set_site_settings("boss", throttle_ms=900)
    snap, fake, slept = _run_deliver([_safe("j1"), _safe("j2"), _safe("j3")])
    assert snap["status"] == "done"
    assert len(fake.calls) == 3
    # 3 条 → 条间 2 次节流（最后一条不再睡）
    assert slept == [0.9, 0.9]
    assert snap["limits"]["throttle_ms"] == 900
    assert snap["limits"]["interval"] == 0.9


def test_节流没配_用调用方自带的默认():
    """没配 `throttle_ms` 时 `interval=0.0` 就是不睡，别自己发明延迟。"""
    snap, _fake, slept = _run_deliver([_safe("j1"), _safe("j2")])
    assert snap["status"] == "done"
    assert slept == []
    assert snap["limits"]["throttle_ms"] == 0


def test_每日配额_到顶就不再发():
    """配额按发对话条数计，到顶的那些记跳过，不浪费站点调用。"""
    boss_db.set_site_settings("boss", daily_quota=2)
    snap, fake, _slept = _run_deliver([_safe(f"j{i}") for i in range(1, 5)])
    assert len(fake.calls) == 2
    assert snap["skipped"] == 2
    by_id = {i["encrypt_job_id"]: i for i in snap["items"]}
    assert by_id["j3"]["status"] == "skipped"
    assert "配额" in by_id["j3"]["error"]
    assert any(e["name"] == "quota_exhausted" for e in snap["events"])
    assert "配额" in snap["stopped_reason"], "投递页要能直接显示「为什么剩下的没发」"


def test_每日配额_算上今天已经发过的():
    """配额余量 = 今日上限 - 今天已发数，不是只看这批。"""
    p = boss_db.resolve_db_path()
    conn = boss_db.connect(p, migrate=False)
    today = time.strftime("%Y-%m-%d", time.localtime())
    try:
        for jid in ("old1", "old2"):
            conn.execute(
                "INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at, "
                "site, last_greeted_at) VALUES (?, 'n', 'b', '2026-10-28 08:00:00', "
                "'boss', ?)",
                (jid, f"{today} 08:30:00"),
            )
        conn.commit()
    finally:
        conn.close()
    boss_db.set_site_settings("boss", daily_quota=3)
    snap, fake, _slept = _run_deliver([_safe("j1"), _safe("j2"), _safe("j3")])
    assert snap["limits"]["sent_today"] == 2
    assert snap["limits"]["quota_left"] == 1
    assert len(fake.calls) == 1
    assert snap["skipped"] == 2


# ---------------------------------------------------------------------------
# 平台设置接口：`/api/sites/{site}/settings`
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """起一个不带假服务端的测试客户端。"""
    from fastapi.testclient import TestClient

    from boss_web import create_app

    return TestClient(create_app())


def test_api_读默认(client):
    """没配过也回完整视图，前端不用判空。"""
    r = client.get("/api/sites/boss/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["site"] == "boss"
    assert (body["throttle_ms"], body["daily_quota"], body["max_parallel"]) == (0, 0, 1)
    assert body["apply_enabled"] is False
    assert body["sent_today"] == 0
    assert body["quota_left"] is None, "0 = 不限，不是「一条都不能发」"


def test_api_写读往返(client):
    """写完读回新值。"""
    r = client.put(
        "/api/sites/boss/settings",
        json={"throttle_ms": 1500, "daily_quota": 20, "max_parallel": 2, "apply_enabled": True},
    )
    assert r.status_code == 200
    assert r.json() == {
        "site": "boss",
        "throttle_ms": 1500,
        "daily_quota": 20,
        "max_parallel": 2,
        "apply_enabled": True,
        "sent_today": 0,
        "quota_left": 20,
    }
    assert client.get("/api/sites/boss/settings").json()["throttle_ms"] == 1500


def test_api_不传的字段不动(client):
    """跟 `PUT /deliver/config` 同规矩：`None` = 保持原值。"""
    client.put("/api/sites/boss/settings", json={"throttle_ms": 800, "daily_quota": 10})
    r = client.put("/api/sites/boss/settings", json={"daily_quota": 25})
    body = r.json()
    assert (body["throttle_ms"], body["daily_quota"]) == (800, 25)


def test_api_越界直接422(client):
    """负数不许落库。"""
    assert client.put("/api/sites/boss/settings", json={"throttle_ms": -1}).status_code == 422
    assert client.put("/api/sites/boss/settings", json={"max_parallel": 0}).status_code == 422


def test_api_没这个平台404(client):
    """站点清单来自 `/api/platforms`，没注册的站点进不来。"""
    assert client.get("/api/sites/51job/settings").status_code == 404


def test_api_legacy别名归boss(client):
    """共存期别名 `/api/settings` 当 `site="boss"`；新代码走 `/api/sites/{site}/…`。"""
    client.put("/api/sites/boss/settings", json={"throttle_ms": 700})
    assert client.get("/api/settings").json()["throttle_ms"] == 700


def test_api_今日余量随账走(client):
    """`sent_today` / `quota_left` 跟发送账本同一口径。"""
    p = boss_db.resolve_db_path()
    conn = boss_db.connect(p, migrate=False)
    today = time.strftime("%Y-%m-%d", time.localtime())
    try:
        conn.execute(
            "INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at, "
            "site, last_greeted_at) VALUES ('j1', 'n', 'b', '2026-10-28 08:00:00', "
            "'boss', ?)",
            (f"{today} 08:30:00",),
        )
        conn.commit()
    finally:
        conn.close()
    client.put("/api/sites/boss/settings", json={"daily_quota": 5})
    body = client.get("/api/sites/boss/settings").json()
    assert (body["sent_today"], body["quota_left"]) == (1, 4)
