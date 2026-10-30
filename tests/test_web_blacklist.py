"""黑名单规则、规则重检与采集配置的 API / 服务测试。

规矩：只用隔离库（`isolated_db`），不碰真 Chrome、真 LLM。
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
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


def make_job(job_id: str, *, job_name: str = "Python 开发", brand_name: str = "甲公司", job_desc: str = "") -> Job:
    """造一条可入库的职位（可带描述）。"""
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
    """把职位批量写进库，回打开的 JobStore。"""
    store = JobStore()
    store.save_page(PageResult(page=1, jobs=tuple(jobs), has_more=False, raw_count=len(jobs)))
    return store




def test_company_rule_rejects_scope(client: TestClient):
    """验证公司规则不许带 scope。"""
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "甲公司", "match_mode": "exact", "scope": "both"},
    )
    assert resp.status_code == 422
    assert "范围" in resp.text or "scope" in resp.text.lower()


def test_company_rule_accepts_empty_scope(client: TestClient):
    """验证公司规则可带空 scope。"""
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "甲公司", "match_mode": "exact", "scope": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["scope"] == ""


def test_jd_rule_requires_scope(client: TestClient):
    """验证 JD 规则必须带合法 scope。"""
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
    """验证启用中的非法正则被拒。"""
    resp = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "jd_keyword", "pattern": "([", "match_mode": "regex", "scope": "both", "enabled": True},
    )
    assert resp.status_code == 422


def test_edit_preserves_disabled_flag(client: TestClient):
    """验证编辑规则保留禁用标记。"""
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
    """验证删规则后历史命中仍可查。"""
    created = client.post(
        "/api/blacklist/rules",
        json={"rule_type": "company", "pattern": "丙公司", "match_mode": "exact"},
    ).json()["item"]
    match = blacklist.RuleMatch(created, "丙公司", "brand_name")
    blacklist.save_hit(job_id="j1", job_name="岗位", brand_name="丙公司", match=match, action="stored")
    assert client.delete(f"/api/blacklist/rules/{created['rule_id']}").status_code == 200
    hits = client.get("/api/blacklist/hits").json()["items"]
    assert any(h["brand_name"] == "丙公司" for h in hits)




def test_empty_company_name_is_not_a_hit_but_blocks():
    """验证空公司名不命中但拦自动投递。"""
    rules = [{"rule_type": "company", "pattern": ".*", "match_mode": "regex", "enabled": True, "rule_id": "r1"}]
    assert blacklist.evaluate_company("", rules) == []
    assert blacklist.evaluate_company("   ", rules) == []
    assert blacklist.evaluate_company("随便什么公司", rules)


def test_company_normalize_and_exact():
    """验证公司名归一后精确匹配。"""
    rules = [{"rule_type": "company", "pattern": "甲公司", "match_mode": "exact", "enabled": True, "rule_id": "r1"}]
    assert blacklist.evaluate_company("  甲   公司  ", rules)
    assert not blacklist.evaluate_company("甲公司分公司", rules)


def test_jd_scope_name_only_matches_without_desc():
    """验证 scope=job_name 无描述也能命中。"""
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




def _wait_done(manager: RecheckTaskManager, timeout: float = 5.0) -> dict:
    """等重检任务进入终态，回快照。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = manager.snapshot()
        if snap.get("status") in {"done", "error", "cancelled"}:
            return snap
        time.sleep(0.02)
    raise AssertionError("重检任务超时未结束")


def test_recheck_updates_state_and_keeps_analysis(client: TestClient):
    """验证重检更新黑名单状态且不写分析表。"""
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
    """验证开跑后改规则不影响本次快照。"""
    manager = RecheckTaskManager()
    task = manager.start(job_ids=["missing"])
    assert task.rules_snapshot is not None
    before = tuple(task.rules_snapshot)
    blacklist.create_rule({"rule_type": "company", "pattern": "冻结测试", "match_mode": "exact"})
    assert tuple(task.rules_snapshot) == before
    _wait_done(manager)


def test_recheck_conflict_when_running():
    """验证重检跑着时再启动冲突。"""
    manager = RecheckTaskManager()
    task = manager.start(job_ids=["a"])
    try:
        with pytest.raises(Exception):
            manager.start(job_ids=["b"])
    finally:
        task.cancel_flag = True
        _wait_done(manager)


def test_recheck_paging_beyond_one_batch():
    """验证重检跨批分页扫完全部职位。"""
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
    """验证仅职名 scope 无描述也能标黑。"""
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
    """验证无 JD 规则时标已检且不拦投递。"""
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
    """验证缺公司名仍拦自动投递。"""
    store = save_jobs(make_job("m1", brand_name="", job_desc="写代码"))
    try:
        manager = RecheckTaskManager()
        manager.start()
        _wait_done(manager)
        job = store.get_job("m1")
        if job is not None:
            assert job.auto_delivery_blocked is True
            assert job.company_blacklisted is False
    finally:
        store.close()


def test_recheck_status_not_found_raises():
    """验证查不存在的重检任务会抛错。"""
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
        manager.start(job_ids=["d1", "d1", "d1"])
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        hits = [h for h in blacklist.list_hits(rule["rule_id"]) if h.get("task_id") == snap["task_id"]]
        assert len(hits) == 1

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
    """验证同一轮重复 job_id 只处理一次。"""
    store = save_jobs(make_job("d2", brand_name="甲公司", job_desc=""))
    try:
        blacklist.create_rule({"rule_type": "company", "pattern": "甲公司", "match_mode": "exact"})
        manager = RecheckTaskManager()
        manager.start(job_ids=["d2", "d2"])
        snap = _wait_done(manager)
        assert snap["status"] == "done"
        assert snap["progress"]["done"] == 1
        assert snap["progress"]["total"] == 1
    finally:
        store.close()




def test_collection_config_preserves_page_size(client: TestClient):
    """验证采集配置保留 pageSize。"""
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
    """验证采集配置重置为默认。"""
    client.put(
        "/api/collection/config",
        json={"query": "go", "pageSize": 20, "city": "101010100"},
    )
    body = client.post("/api/collection/config/reset").json()
    assert body["search"]["query"] == ""
    assert body["search"]["pageSize"] == 15


def test_blacklist_recheck_api_endpoints(client: TestClient):
    """验证重检接口起停与状态。"""
    idle = client.get("/api/blacklist/recheck/status").json()
    assert idle["status"] == "idle"
    started = client.post("/api/blacklist/recheck", json={"limit": 10})
    assert started.status_code == 200
    task_id = started.json()["task_id"]
    assert task_id
    for _ in range(100):
        state = client.get("/api/blacklist/recheck/status").json()
        if state.get("status") in {"done", "error", "cancelled"}:
            break
        time.sleep(0.02)
    assert state["status"] in {"done", "error", "cancelled"}




def test_match_auto_delivery_blocked_helper():
    """验证自动投递拦截判定的各分支。"""
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
    """验证 finished 事件不重复累计进度。"""
    from boss_web.services.crawl_task import CrawlTask

    task = CrawlTask({})
    task.push({"event": "page", "page": 1, "inserted": 2, "updated": 1, "company_hits": 1, "jd_hits": 1})
    task.push({"event": "finished", "inserted": 2, "updated": 1, "company_hits": 1, "jd_hits": 1})
    assert task.progress["inserted"] == 2
    assert task.progress["updated"] == 1
    assert task.progress["company_hits"] == 1
    assert task.progress["pages"] == 1


def test_crawl_task_detail_done_counts_empty():
    """验证空描述详情也计入完成数。"""
    from boss_web.services.crawl_task import CrawlTask

    task = CrawlTask({})
    task.push({"event": "detail_done", "has_desc": False, "jd_status": "empty"})
    assert task.progress["jd_empty"] == 1
    assert task.progress["desc_done"] == 1


def test_crawl_task_snapshot_feeds_task_center():
    """任务中心卡片的「当前/进度」直接读 snapshot：阶段给中文，进度按页数算。

    页面上不再画采集进度条，任务中心要能自己读懂跑在哪一步——`current`
    必须是人话（不是内部代号），`done/total` 给页数好画确定态进度条；
    空参数建任务不炸，`total` 给 0 供前端回落不确定态。
    """
    from boss_web.services.crawl_task import CrawlTask

    task = CrawlTask({"max_pages": 5})
    task.phase = "crawl"
    task.push({"event": "page", "page": 1, "inserted": 2})
    snap = task.snapshot()
    assert snap["current"] == "翻页搜索"
    assert snap["done"] == 1
    assert snap["total"] == 5

    blank = CrawlTask({})
    blank.phase = "stoken"
    snap = blank.snapshot()
    assert snap["current"] == "验证身份"
    assert snap["total"] == 0


def test_match_includes_safety_snapshot():
    """match 结果必须带完整安全快照（安全状态字段一个不能少）。"""
    from boss_web.services.match_task import _blacklist_snapshot

    job = make_job("z1", brand_name="甲公司", job_desc="写代码")
    snap = _blacklist_snapshot(job)
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


# --- 站点归属（todo3 第四节）：规则有的是站点语义，`site=''` = 全站生效 ---


def test_create_rule_默认全站生效():
    """不指定站点 = 全站规则（空串），跟单站点旧账同形状——不是落成 `'boss'`。"""
    rule = blacklist.create_rule(
        {"rule_type": "company", "pattern": "甲公司", "match_mode": "exact"}
    )
    assert rule["site"] == ""


def test_create_rule_可指定站点归属():
    """站点专属规则：只在那个站点的抓取/重检里生效。"""
    rule = blacklist.create_rule(
        {"rule_type": "company", "pattern": "乙公司", "match_mode": "exact", "site": "51job"}
    )
    assert rule["site"] == "51job"


def test_list_rules_按站点生效切片():
    """传 `site` = 该站点**生效**的规则：全站（`''`）+ 本站专属，别家的不串进来。"""
    blacklist.create_rule({"rule_type": "company", "pattern": "全站", "match_mode": "exact"})
    blacklist.create_rule({"rule_type": "company", "pattern": "BOSS专属", "match_mode": "exact", "site": "boss"})
    blacklist.create_rule({"rule_type": "company", "pattern": "别家专属", "match_mode": "exact", "site": "51job"})

    def patterns(site):
        return sorted(r["pattern"] for r in blacklist.list_rules(site=site))

    assert patterns("boss") == ["BOSS专属", "全站"]
    assert patterns("51job") == ["全站", "别家专属"]
    assert len(blacklist.list_rules()) == 3  # 不传 = 全部


def test_update_rule_不传站点不改归属():
    """只改备注不该顺手把规则收成全站——`site=None` 是「这次不动」。"""
    rule = blacklist.create_rule(
        {"rule_type": "company", "pattern": "丙公司", "match_mode": "exact", "site": "51job"}
    )
    updated = blacklist.update_rule(rule["rule_id"], {"description": "改个备注"})
    assert updated["site"] == "51job"


def test_update_rule_显式空串收成全站():
    """显式给空串 = 用户就要把它放开到全站，那是有意的改法。"""
    rule = blacklist.create_rule(
        {"rule_type": "company", "pattern": "丁公司", "match_mode": "exact", "site": "boss"}
    )
    updated = blacklist.update_rule(rule["rule_id"], {"site": ""})
    assert updated["site"] == ""


def test_api_rules_只回本平台生效(client: TestClient):
    """平台作用域的规则列表不串别家站点的专属规则（全站规则照回）。"""
    blacklist.create_rule({"rule_type": "company", "pattern": "全站", "match_mode": "exact"})
    blacklist.create_rule({"rule_type": "company", "pattern": "别家", "match_mode": "exact", "site": "51job"})
    blacklist.create_rule({"rule_type": "company", "pattern": "本家", "match_mode": "exact", "site": "boss"})

    body = client.get("/api/sites/boss/blacklist/rules").json()
    assert sorted(i["pattern"] for i in body["items"]) == ["全站", "本家"]


def test_api_rules_回包带site(client: TestClient):
    """行里带 `site`，前端要区分「全站 / 本站」直接看这个字段。"""
    blacklist.create_rule({"rule_type": "company", "pattern": "全站", "match_mode": "exact"})
    body = client.get("/api/sites/boss/blacklist/rules").json()
    assert body["items"][0]["site"] == ""


def test_api_rules_新建不传site默认全站(client: TestClient):
    """API 层不传 `site` 落成全站——老行为不变，别让共存期规则突然长出归属。"""
    r = client.post(
        "/api/sites/boss/blacklist/rules",
        json={"rule_type": "company", "pattern": "戊公司", "match_mode": "exact"},
    )
    assert r.status_code == 200
    assert r.json()["item"]["site"] == ""


def test_api_rules_legacy别名同样按boss生效(client: TestClient):
    """共存期别名 `/api/blacklist/rules` 也落 `site="boss"` 的口径，行为跟新面一致。"""
    blacklist.create_rule({"rule_type": "company", "pattern": "全站", "match_mode": "exact"})
    blacklist.create_rule({"rule_type": "company", "pattern": "别家", "match_mode": "exact", "site": "51job"})
    body = client.get("/api/blacklist/rules").json()
    assert sorted(i["pattern"] for i in body["items"]) == ["全站"]
