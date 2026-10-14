"""历史抓取任务（``crawl_tasks`` / ``crawl_task_jobs``）与匹配范围的测试。

覆盖：档案落库、任务↔岗位归属、``fetch_pages.task_id``、``last_greeted_at``
不被重抓覆盖、``crawl_task_id`` 匹配范围、以及 ``/api/crawl/tasks…`` 路由
（``/tasks`` 不能被 ``/{task_id}`` 吃掉）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore
from boss_web import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


def make_job(encrypt_job_id: str = "id-1", **overrides) -> Job:
    defaults = dict(
        job_name="Python 开发",
        brand_name="示例科技",
        location="广州",
        salary_desc="15-25K",
        job_experience="3-5年",
        job_degree="本科",
        brand_industry="互联网",
        brand_scale_name="100-499人",
        encrypt_job_id=encrypt_job_id,
        raw_json="{}",
        city_name="广州",
        area_district="",
        business_district="",
        job_labels=(),
        skills=(),
        welfare_list=(),
        page=1,
    )
    defaults.update(overrides)
    return Job(**defaults)


def make_page(jobs=(), page: int = 1, has_more: bool = False) -> PageResult:
    jobs = tuple(jobs)
    return PageResult(page=page, jobs=jobs, has_more=has_more, raw_count=len(jobs), dropped=())


# --------------------------------------------------------------------------- #
# 档案：crawl_tasks
# --------------------------------------------------------------------------- #


def test_create_and_get_crawl_task():
    with JobStore() as store:
        store.create_crawl_task(
            "tk_1",
            source="search",
            params={"max_pages": 3, "keyword": "Python"},
        )
        item = store.get_crawl_task("tk_1")
        assert item is not None
        assert item["task_id"] == "tk_1"
        assert item["source"] == "search"
        assert item["status"] == "running"
        assert item["params"]["max_pages"] == 3
        assert item["ended_at"] == 0


def test_finish_crawl_task_computes_counts_from_links():
    """不传显式计数时，按关联表收敛（``INSERT OR IGNORE`` 保首条 action）。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("a", job_name="改过")], page=2), task_id="tk_1")
        assert store.finish_crawl_task("tk_1", status="done") is True

        item = store.get_crawl_task("tk_1")
        assert item["status"] == "done"
        assert item["job_count"] == 2  # a、b 两条归属
        assert item["inserted_count"] == 2  # 两条都是首条 inserted
        assert item["updated_count"] == 0  # a 的第二次是 INSERT OR IGNORE，不改 action
        assert item["ended_at"] > 0


def test_finish_crawl_task_accepts_explicit_counts():
    """``_archive_finish`` 会把 ``task.progress`` 的真实入库统计传进来。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("a", job_name="改过")], page=2), task_id="tk_1")
        store.finish_crawl_task(
            "tk_1", status="done", job_count=2, inserted_count=1, updated_count=1
        )
        item = store.get_crawl_task("tk_1")
        assert (item["job_count"], item["inserted_count"], item["updated_count"]) == (2, 1, 1)


def test_list_and_count_crawl_tasks():
    with JobStore() as store:
        for i in range(3):
            store.create_crawl_task(f"tk_{i}", created_at=time.time() + i)
        assert store.count_crawl_tasks() == 3
        items = store.list_crawl_tasks(limit=2, offset=0)
        assert len(items) == 2
        assert items[0]["task_id"] == "tk_2"  # 最近的在前


def test_touch_crawl_task_stats():
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.touch_crawl_task_stats("tk_1", {"pages": 2, "jobs": 5})
        item = store.get_crawl_task("tk_1")
        assert item["stats"]["pages"] == 2
        assert item["stats"]["jobs"] == 5


# --------------------------------------------------------------------------- #
# 归属：crawl_task_jobs
# --------------------------------------------------------------------------- #


def test_save_page_records_task_jobs():
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        assert store.count_crawl_task_jobs("tk_1") == 2
        assert store.crawl_task_job_ids("tk_1", only_existing=False) == ["a", "b"]


def test_task_job_action_keeps_first():
    """``INSERT OR IGNORE``：同一岗位再抓一次不覆盖归属动作（保留首条 inserted）。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("a", job_name="改过")], page=2), task_id="tk_1")
        items = store.list_crawl_task_jobs("tk_1", only_existing=False)
        assert len(items) == 1
        assert items[0]["action"] == "inserted"


def test_count_crawl_task_jobs_only_existing():
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        assert store.count_crawl_task_jobs("tk_1", only_existing=False) == 2
        assert store.count_crawl_task_jobs("tk_1", only_existing=True) == 2

        store.delete_job("b")
        assert store.count_crawl_task_jobs("tk_1", only_existing=False) == 2
        assert store.count_crawl_task_jobs("tk_1", only_existing=True) == 1


def test_crawl_task_job_ids_intersection():
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.delete_job("b")
        assert store.crawl_task_job_ids("tk_1", only_existing=True) == ["a"]
        assert store.crawl_task_job_ids("tk_1", only_existing=False) == ["a", "b"]


def test_list_crawl_task_jobs_marks_missing():
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.delete_job("b")
        items = store.list_crawl_task_jobs("tk_1", only_existing=False)
        by_id = {str(i["encrypt_job_id"]): i for i in items}
        assert by_id["a"].get("missing") is not True
        assert by_id["b"]["missing"] is True
        assert by_id["b"]["job_name"] == ""  # 职位已删，只剩归属记录


# --------------------------------------------------------------------------- #
# fetch_pages.task_id
# --------------------------------------------------------------------------- #


def test_fetch_pages_records_task_id():
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("b")], page=2))  # 不带 task_id

        rows = store._conn.execute(
            "SELECT page, task_id FROM fetch_pages ORDER BY page"
        ).fetchall()
        assert rows[0]["task_id"] == "tk_1"
        assert rows[1]["task_id"] == ""


# --------------------------------------------------------------------------- #
# last_greeted_at：重抓不覆盖
# --------------------------------------------------------------------------- #


def test_last_greeted_at_survives_resave():
    """重抓 UPSERT **不许** 覆盖发送史（``last_greeted_at`` 不在 INSERT/UPDATE 列里）。"""
    with JobStore() as store:
        store.save_page(make_page(jobs=[make_job("a")]))
        store.update_last_greeted_at("a", "2026-10-01 12:00:00")
        assert store.get_last_greeted_at("a") == "2026-10-01 12:00:00"

        store.save_page(make_page(jobs=[make_job("a", job_name="重抓后的岗位")], page=2))
        job = store.get_job("a")
        assert job.job_name == "重抓后的岗位"  # 其他字段照常更新
        assert store.get_last_greeted_at("a") == "2026-10-01 12:00:00"


# --------------------------------------------------------------------------- #
# 匹配范围：crawl_task_id
# --------------------------------------------------------------------------- #


def test_pick_jobs_crawl_task_scope():
    from boss_web.services import match_task as mt

    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("c")], page=2))  # 不属于本批

        task = mt.MatchTask("rs_1", [], crawl_task_id="tk_1")
        jobs = mt._pick_jobs(task)
        assert {j.encrypt_job_id for j in jobs} == {"a", "b"}

        store.delete_job("b")
        jobs = mt._pick_jobs(task)
        assert {j.encrypt_job_id for j in jobs} == {"a"}  # 交集


def test_pick_jobs_job_ids_scope_wins_over_all():
    from boss_web.services import match_task as mt

    with JobStore() as store:
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]))
        task = mt.MatchTask("rs_1", ["a"])
        assert [j.encrypt_job_id for j in mt._pick_jobs(task)] == ["a"]


def test_match_task_scope_info():
    from boss_web.services import match_task as mt

    task = mt.MatchTask("rs_1", ["a", "b"], crawl_task_id="tk_9")
    scope = task.scope_info()
    assert scope["type"] == "crawl_task"
    assert scope["crawl_task_id"] == "tk_9"

    snap = task.snapshot()
    assert snap["crawl_task_id"] == "tk_9"
    assert snap["scope"]["type"] == "crawl_task"


# --------------------------------------------------------------------------- #
# API：/api/crawl/tasks…（/tasks 不能被 /{task_id} 吃掉）
# --------------------------------------------------------------------------- #


def test_api_list_tasks_empty(client: TestClient):
    r = client.get("/api/crawl/tasks")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_api_list_tasks_not_eaten_by_task_id_route(client: TestClient):
    """``GET /api/crawl/tasks`` 必须走列表路由，不是 ``/{task_id}`` 的 ``tasks``。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1", params={"keyword": "Python"})
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.finish_crawl_task("tk_1", status="done")

    r = client.get("/api/crawl/tasks")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["task_id"] == "tk_1"
    assert items[0]["job_count_existing"] == 1
    assert items[0]["params"]["keyword"] == "Python"


def test_api_get_task_detail(client: TestClient):
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.finish_crawl_task("tk_1", status="done", error="")

    r = client.get("/api/crawl/tasks/tk_1")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "tk_1"
    assert body["status"] == "done"
    assert body["job_count_existing"] == 2


def test_api_get_task_missing_404(client: TestClient):
    r = client.get("/api/crawl/tasks/nope")
    assert r.status_code == 404


def test_api_task_jobs_marks_missing(client: TestClient):
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.delete_job("b")

    r = client.get("/api/crawl/tasks/tk_1/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["existing"] == 1
    by_id = {str(i["encrypt_job_id"]): i for i in body["items"]}
    assert by_id["b"]["missing"] is True


def test_api_match_task_batch(client: TestClient, monkeypatch):
    from boss_web.services import match_task as mt

    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.finish_crawl_task("tk_1", status="done")

    started = []
    monkeypatch.setattr(
        mt.match_tasks,
        "start",
        lambda **kw: started.append(kw) or mt.MatchTask("rs_1", [], crawl_task_id=kw.get("crawl_task_id", "")),
    )

    r = client.post("/api/crawl/tasks/tk_1/match", json={"resume_id": "rs_1"})
    assert r.status_code == 200
    assert started and started[0]["crawl_task_id"] == "tk_1"
    assert started[0]["resume_id"] == "rs_1"
    assert r.json()["scope"]["type"] == "crawl_task"


def test_api_match_task_batch_empty_intersection(client: TestClient):
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.delete_job("a")

    r = client.post("/api/crawl/tasks/tk_1/match", json={"resume_id": "rs_1"})
    assert r.status_code == 422
    assert "本批职位已不在库中" in r.json()["message"]


def test_api_match_task_batch_requires_resume(client: TestClient):
    r = client.post("/api/crawl/tasks/tk_1/match", json={"resume_id": ""})
    assert r.status_code == 422
    assert "简历" in r.json()["message"]
