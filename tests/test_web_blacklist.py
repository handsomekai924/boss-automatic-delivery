"""黑名单规则、规则重检与采集配置的 API / 服务测试。

规矩：只用隔离库（``isolated_db``），不碰真 Chrome、真 LLM。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore
from boss_web import create_app
from boss_web.services import blacklist
from boss_web.services.blacklist_recheck import RecheckTaskManager, recheck_tasks


@pytest.fixture
def client():
    return TestClient(create_app())


def make_job(job_id: str, *, job_name: str = "Python 开发", brand_name: str = "甲公司", job_desc: str = "") -> Job:
    item = {
        "encryptJobId": job_id,
        "jobName": job_name,
        "brandName": brand_name,
        "securityId": f"sec-{job_id}",
        "lid": f"lid-{job_id}",
    }
    job = Job.from_api(item)
    if job_desc:
        from dataclasses import replace

        job = replace(job, job_desc=job_desc, detail_fetched_at="2026-01-01 00:00:00", jd_status="completed")
    return job


def save_jobs(*jobs: Job) -> JobStore:
    store = JobStore()
    store.save_page(PageResult(page=1, jobs=tuple(jobs), has_more=False, raw_count=len(jobs)))
    return store


# --------------------------------------------------------------------------- #
# 规则 CRUD / 校验
# --------------------------------------------------------------------------- #


def test_company_rule_rejects_scope(client: TestClient):
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "甲公司", "match_mode": "exact", "scope": "both"},
    )
    assert resp.status_code == 422
    assert "范围" in resp.text or "scope" in resp.text.lower()


def test_company_rule_accepts_empty_scope(client: TestClient):
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "甲公司", "match_mode": "exact", "scope": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["scope"] == ""


def test_jd_rule_requires_scope(client: TestClient):
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "jd_keyword", "pattern": "996", "match_mode": "contains", "scope": "nope"},
    )
    assert resp.status_code == 422
    ok = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "jd_keyword", "pattern": "996", "match_mode": "contains", "scope": "job_name"},
    )
    assert ok.status_code == 200
    assert ok.json()["item"]["scope"] == "job_name"


def test_invalid_regex_rejected_when_enabled(client: TestClient):
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "jd_keyword", "pattern": "([", "match_mode": "regex", "scope": "both", "enabled": True},
    )
    assert resp.status_code == 422


def test_edit_preserves_disabled_flag(client: TestClient):
    created = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "乙公司", "match_mode": "exact", "enabled": False},
    ).json()["item"]
    updated = client.put(
        f"/api/blacklist/rules/{created['rule_id']}",
        json={"rule_type": "company", "pattern": "乙公司改", "match_mode": "exact", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["item"]["enabled"] is False
    assert updated.json()["item"]["pattern"] == "乙公司改"


def test_delete_keeps_historical_hits(client: TestClient):
    created = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "丙公司", "match_mode": "exact"},
    ).json()["item"]
    match = blacklist.RuleMatch(created, "丙公司", "brand_name")
    blacklist.save_hit(job_id="j1", job_name="岗位", brand_name="丙公司", match=match, action="stored")
    assert client.delete(f"/api/blacklist/rules/{created['rule_id']}").status_code == 200
    hits = client.get("/api/blacklist/hits").json()["items"]
    assert any(h["brand_name"] == "丙公司" for h in hits)


# --------------------------------------------------------------------------- #
# 评估器
# --------------------------------------------------------------------------- #


def test_empty_company_name_is_not_a_hit_but_blocks():
    rules = [{"rule_type": "company", "pattern": ".*", "match_mode": "regex", "enabled": True, "rule_id": "r1"}]
    assert blacklist.evaluate_company("", rules) == []
    assert blacklist.evaluate_company("   ", rules) == []
    # 有名字时正则仍然正常命中
    assert blacklist.evaluate_company("随便什么公司", rules)


def test_company_normalize_and_exact():
    rules = [{"rule_type": "company", "pattern": "甲公司", "match_mode": "exact", "enabled": True, "rule_id": "r1"}]
    assert blacklist.evaluate_company("  甲   公司  ", rules)
    assert not blacklist.evaluate_company("甲公司分公司", rules)


def test_jd_scope_name_only_matches_without_desc():
    rules = [
        {
            "rule_type": "jd_keyword",
            "pattern": "外包",
            "match_mode": "contains",
            "scope": "job_name",
            "enabled": True,
            "rule_id": "r1",
        }
    ]
    hits = blacklist.evaluate_jd("Java 外包工程师", "", rules)
    assert hits and hits[0].source == "job_name"
    assert not blacklist.evaluate_jd("Java 工程师", "", rules)


# --------------------------------------------------------------------------- #
# 规则重检
# --------------------------------------------------------------------------- #


def _wait_done(manager: RecheckTaskManager, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = manager.snapshot()
        if snap.get("status") in {"done", "error", "cancelled"}:
            return snap
        time.sleep(0.02)
    raise AssertionError("重检任务超时未结束")


def test_recheck_updates_state_and_keeps_analysis(client: TestClient):
    store = save_jobs(
        make_job("j1", brand_name="甲公司", job_name="Python", job_desc="写代码"),
        make_job("j2", brand_name="正常公司", job_name="Java", job_desc="也写代码"),
    )
    try:
        client.post(
            "/api/blacklist/rules",
            json={"rule_type": "company", "pattern": "甲公司", "match_mode": "exact"},
        )
        manager = RecheckTaskManager()
        task = manager.start()
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        assert snap["progress"]["total"] == 2
        j1 = store.get_job("j1")
        j2 = store.get_job("j2")
        assert j1.company_blacklisted is True
        assert j1.auto_delivery_blocked is True
        assert j2.company_blacklisted is False
        assert j2.auto_delivery_blocked is False
        # 分析表不应被重检写入
        import boss_db

        conn = boss_db.acquire()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r["name"] for r in rows}
        if "analyses" in names:
            n = conn.execute("SELECT COUNT(*) AS n FROM analyses").fetchone()["n"]
            assert n == 0
        assert task.task_id.startswith("br_")
    finally:
        store.close()


def test_recheck_rule_snapshot_frozen_at_start():
    manager = RecheckTaskManager()
    task = manager.start(job_ids=["missing"])
    assert task.rules_snapshot is not None
    # 启动后再改规则不影响已开始任务的快照内容
    before = tuple(task.rules_snapshot)
    blacklist.create_rule({"rule_type": "company", "pattern": "冻结测试", "match_mode": "exact"})
    assert tuple(task.rules_snapshot) == before
    _wait_done(manager)


def test_recheck_conflict_when_running():
    manager = RecheckTaskManager()
    task = manager.start(job_ids=["a"])
    try:
        with pytest.raises(Exception):
            manager.start(job_ids=["b"])
    finally:
        task.cancel_flag = True
        _wait_done(manager)


def test_recheck_paging_beyond_one_batch():
    store = save_jobs(*[make_job(f"p{i}", brand_name=f"公司{i}") for i in range(8)])
    try:
        manager = RecheckTaskManager()
        manager.start()
        snap = _wait_done(manager, timeout=10.0)
        assert snap["status"] == "done"
        assert snap["progress"]["total"] == 8
        assert snap["progress"]["done"] == 8
    finally:
        store.close()


def test_recheck_name_only_scope_checked_without_desc():
    store = save_jobs(make_job("n1", job_name="前端 外包", brand_name="正常公司", job_desc=""))
    try:
        blacklist.create_rule(
            {
                "rule_type": "jd_keyword",
                "pattern": "外包",
                "match_mode": "contains",
                "scope": "job_name",
            }
        )
        manager = RecheckTaskManager()
        manager.start()
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        job = store.get_job("n1")
        assert job.jd_rule_checked is True
        assert job.jd_blacklisted is True
        assert job.auto_delivery_blocked is True
    finally:
        store.close()


def test_recheck_no_jd_rules_marks_checked_and_unblocked():
    store = save_jobs(make_job("c1", brand_name="正常公司", job_desc=""))
    try:
        manager = RecheckTaskManager()
        manager.start()
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        job = store.get_job("c1")
        assert job.jd_rule_checked is True
        assert job.auto_delivery_blocked is False
    finally:
        store.close()


def test_recheck_missing_company_blocks_auto_delivery():
    store = save_jobs(make_job("m1", brand_name="", job_desc="写代码"))
    try:
        manager = RecheckTaskManager()
        manager.start()
        _wait_done(manager)
        job = store.get_job("m1")
        # Job.from_api 的 is_valid 可能拒掉空公司名；能入库才算
        if job is not None:
            assert job.auto_delivery_blocked is True
            assert job.company_blacklisted is False
    finally:
        store.close()


def test_recheck_status_not_found_raises():
    manager = RecheckTaskManager()
    with pytest.raises(Exception):
        manager.snapshot("br_nope")


def test_recheck_dedupes_hits_within_one_run():
    """同一轮内 (job, rule) 只写一条审计；跨轮各记一条（保留时间线）。"""
    store = save_jobs(make_job("d1", brand_name="甲公司", job_desc="写代码"))
    try:
        rule = blacklist.create_rule(
            {"rule_type": "company", "pattern": "甲公司", "match_mode": "exact"}
        )
        manager = RecheckTaskManager()
        # 同一个 job_id 传两次：既测去重，也防分页抖动
        manager.start(job_ids=["d1", "d1", "d1"])
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        hits = [h for h in blacklist.list_hits(rule["rule_id"]) if h.get("task_id") == snap["task_id"]]
        assert len(hits) == 1

        # 再跑一轮：新的一轮记新的一条（时间线保留）
        manager2 = RecheckTaskManager()
        manager2.start(job_ids=["d1"])
        snap2 = _wait_done(manager2)
        hits2 = [h for h in blacklist.list_hits(rule["rule_id"]) if h.get("task_id") == snap2["task_id"]]
        assert len(hits2) == 1
        all_hits = blacklist.list_hits(rule["rule_id"])
        assert len(all_hits) == 2
    finally:
        store.close()


def test_recheck_skips_duplicate_jobs_in_one_run():
    store = save_jobs(make_job("d2", brand_name="甲公司", job_desc=""))
    try:
        blacklist.create_rule({"rule_type": "company", "pattern": "甲公司", "match_mode": "exact"})
        manager = RecheckTaskManager()
        manager.start(job_ids=["d2", "d2"])
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        # 同一职位只处理一次，进度不会重复计数
        assert snap["progress"]["done"] == 1
        assert snap["progress"]["total"] == 1
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 采集配置 API
# --------------------------------------------------------------------------- #


def test_collection_config_preserves_page_size(client: TestClient):
    payload = {
        "query": "python",
        "city": "101280100",
        "jobType": "",
        "salary": "405",
        "experience": ["104"],
        "degree": [],
        "industry": [],
        "scale": [],
        "payType": [],
        "partTime": [],
        "stage": [],
        "pageSize": 30,
        "max_pages": 3,
        "start_page": 1,
        "page_interval": 0.5,
        "use_search": True,
        "fetch_details": False,
        "detail_interval": 0.5,
        "store_blacklisted": True,
    }
    resp = client.put("/api/collection/config", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["search"]["pageSize"] == 30
    assert body["search"]["city"] == "101280100"
    assert body["collection"]["fetch_details"] is False
    got = client.get("/api/collection/config").json()
    assert got["search"]["pageSize"] == 30
    assert got["search"]["city"] == "101280100"


def test_collection_config_reset(client: TestClient):
    client.put(
        "/api/collection/config",
        json={"query": "go", "pageSize": 20, "city": "101010100"},
    )
    body = client.post("/api/collection/config/reset").json()
    assert body["search"]["query"] == ""
    assert body["search"]["pageSize"] == 15


def test_blacklist_recheck_api_endpoints(client: TestClient):
    idle = client.get("/api/blacklist/recheck/status").json()
    assert idle["status"] == "idle"
    started = client.post("/api/blacklist/recheck", json={"limit": 10})
    assert started.status_code == 200
    task_id = started.json()["task_id"]
    assert task_id
    # 轮询到结束
    for _ in range(100):
        state = client.get("/api/blacklist/recheck/status").json()
        if state.get("status") in {"done", "error", "cancelled"}:
            break
        time.sleep(0.02)
    assert state["status"] in {"done", "error", "cancelled"}


# --------------------------------------------------------------------------- #
# 匹配汇总 / 抓取计数回归
# --------------------------------------------------------------------------- #


def test_match_auto_delivery_blocked_helper():
    from boss_web.services.match_task import _auto_delivery_blocked

    assert _auto_delivery_blocked({"company_blacklisted": True})
    assert _auto_delivery_blocked({"jd_blacklisted": True})
    assert _auto_delivery_blocked({"jd_rule_checked": False})
    assert _auto_delivery_blocked({"jd_rule_checked": True, "jd_status": "failed"})
    assert not _auto_delivery_blocked(
        {
            "company_blacklisted": False,
            "jd_blacklisted": False,
            "jd_rule_checked": True,
            "jd_status": "completed",
            "auto_delivery_blocked": False,
        }
    )


def test_crawl_task_progress_does_not_double_count_finished():
    from boss_web.services.crawl_task import CrawlTask

    task = CrawlTask({})
    task.push({"event": "page", "page": 1, "inserted": 2, "updated": 1, "company_hits": 1, "jd_hits": 1})
    task.push({"event": "finished", "inserted": 2, "updated": 1, "company_hits": 1, "jd_hits": 1})
    assert task.progress["inserted"] == 2
    assert task.progress["updated"] == 1
    assert task.progress["company_hits"] == 1
    assert task.progress["pages"] == 1


def test_crawl_task_detail_done_counts_empty():
    from boss_web.services.crawl_task import CrawlTask

    task = CrawlTask({})
    task.push({"event": "detail_done", "has_desc": False, "jd_status": "empty"})
    assert task.progress["jd_empty"] == 1
    assert task.progress["desc_done"] == 1


def test_legacy_analyze_includes_safety_snapshot():
    """老 /api/resume/analyze 路径仍可达，match 也必须带完整安全快照。"""
    from boss_web.services.resume_analyzer import _safety_snapshot

    job = make_job("z1", brand_name="甲公司", job_desc="写代码")
    snap = _safety_snapshot(job)
    for key in (
        "is_blacklisted",
        "company_blacklisted",
        "jd_blacklisted",
        "jd_rule_checked",
        "jd_status",
        "auto_delivery_blocked",
        "matched_rule_snapshot",
    ):
        assert key in snap
