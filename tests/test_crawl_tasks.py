"""历史抓取任务（`crawl_tasks` / `crawl_task_jobs`）与匹配范围的测试。

覆盖：档案落库、任务↔岗位归属、`fetch_pages.task_id`、`last_greeted_at`
不被重抓覆盖、`crawl_task_id` 匹配范围、以及 `/api/crawl/tasks…` 路由
（`/tasks` 不能被 `/{task_id}` 吃掉）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import boss_db
from boss_jobs.models import Job, PageResult
from boss_jobs.store import DEFAULT_SITE, JobStore
from boss_web import create_app


def _task_site(task_id: str) -> str:
    """直接读 `crawl_tasks.site`（不走 `_row_to_crawl_task`，免得被它的兜底骗了）。"""
    conn = boss_db.connect(boss_db.resolve_db_path(), migrate=False)
    try:
        row = conn.execute(
            "SELECT site FROM crawl_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


@pytest.fixture
def client():
    """提供指向假服务端的登录客户端。"""
    return TestClient(create_app())


def make_job(encrypt_job_id: str = "id-1", **overrides) -> Job:
    """构造一条职位记录。"""
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
    """构造一页职位数据。"""
    jobs = tuple(jobs)
    return PageResult(page=page, jobs=jobs, has_more=has_more, raw_count=len(jobs), dropped=())


def test_create_and_get_crawl_task():
    """验证建任务并读回。"""
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
    """不传显式计数时，按关联表收敛（`INSERT OR IGNORE` 保首条 action）。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("a", job_name="改过")], page=2), task_id="tk_1")
        assert store.finish_crawl_task("tk_1", status="done") is True

        item = store.get_crawl_task("tk_1")
        assert item["status"] == "done"
        assert item["job_count"] == 2
        assert item["inserted_count"] == 2
        assert item["updated_count"] == 0
        assert item["ended_at"] > 0


def test_finish_crawl_task_accepts_explicit_counts():
    """`_archive_finish` 会把 `task.progress` 的真实入库统计传进来。"""
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
    """验证列出并计数任务。"""
    with JobStore() as store:
        for i in range(3):
            store.create_crawl_task(f"tk_{i}", created_at=time.time() + i)
        assert store.count_crawl_tasks() == 3
        items = store.list_crawl_tasks(limit=2, offset=0)
        assert len(items) == 2
        assert items[0]["task_id"] == "tk_2"


def test_touch_crawl_task_stats():
    """验证更新任务统计。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.touch_crawl_task_stats("tk_1", {"pages": 2, "jobs": 5})
        item = store.get_crawl_task("tk_1")
        assert item["stats"]["pages"] == 2
        assert item["stats"]["jobs"] == 5


def test_save_page_records_task_jobs():
    """验证存页记录任务岗位。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        assert store.count_crawl_task_jobs("tk_1") == 2
        assert store.crawl_task_job_ids("tk_1", only_existing=False) == ["a", "b"]


def test_task_job_action_keeps_first():
    """`INSERT OR IGNORE`：同一岗位再抓一次不覆盖归属动作（保留首条 inserted）。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("a", job_name="改过")], page=2), task_id="tk_1")
        items = store.list_crawl_task_jobs("tk_1", only_existing=False)
        assert len(items) == 1
        assert items[0]["action"] == "inserted"


def test_count_crawl_task_jobs_only_existing():
    """验证只计仍存在的岗位。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        assert store.count_crawl_task_jobs("tk_1", only_existing=False) == 2
        assert store.count_crawl_task_jobs("tk_1", only_existing=True) == 2

        store.delete_job("b")
        assert store.count_crawl_task_jobs("tk_1", only_existing=False) == 2
        assert store.count_crawl_task_jobs("tk_1", only_existing=True) == 1


def test_crawl_task_job_ids_intersection():
    """验证任务岗位 id 交集。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.delete_job("b")
        assert store.crawl_task_job_ids("tk_1", only_existing=True) == ["a"]
        assert store.crawl_task_job_ids("tk_1", only_existing=False) == ["a", "b"]


def test_list_crawl_task_jobs_marks_missing():
    """验证列表标记缺失岗位。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.delete_job("b")
        items = store.list_crawl_task_jobs("tk_1", only_existing=False)
        by_id = {str(i["encrypt_job_id"]): i for i in items}
        assert by_id["a"].get("missing") is not True
        assert by_id["b"]["missing"] is True
        assert by_id["b"]["job_name"] == ""


def test_fetch_pages_records_task_id():
    """验证抓页写入 task_id。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("b")], page=2))

        rows = store._conn.execute(
            "SELECT page, task_id FROM fetch_pages ORDER BY page"
        ).fetchall()
        assert rows[0]["task_id"] == "tk_1"
        assert rows[1]["task_id"] == ""


def test_last_greeted_at_survives_resave():
    """重抓 UPSERT 不许 覆盖发送史（`last_greeted_at` 不在 INSERT/UPDATE 列里）。"""
    with JobStore() as store:
        store.save_page(make_page(jobs=[make_job("a")]))
        store.update_last_greeted_at("a", "2026-10-01 12:00:00")
        assert store.get_last_greeted_at("a") == "2026-10-01 12:00:00"

        store.save_page(make_page(jobs=[make_job("a", job_name="重抓后的岗位")], page=2))
        job = store.get_job("a")
        assert job.job_name == "重抓后的岗位"
        assert store.get_last_greeted_at("a") == "2026-10-01 12:00:00"


def test_pick_jobs_crawl_task_scope():
    """验证按任务范围挑岗位。"""
    from boss_web.services import match_task as mt

    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]), task_id="tk_1")
        store.save_page(make_page(jobs=[make_job("c")], page=2))

        task = mt.MatchTask("rs_1", [], crawl_task_id="tk_1")
        jobs = mt._pick_jobs(task)
        assert {j.encrypt_job_id for j in jobs} == {"a", "b"}

        store.delete_job("b")
        jobs = mt._pick_jobs(task)
        assert {j.encrypt_job_id for j in jobs} == {"a"}


def test_pick_jobs_job_ids_scope_wins_over_all():
    """验证 job_ids 优先于全量。"""
    from boss_web.services import match_task as mt

    with JobStore() as store:
        store.save_page(make_page(jobs=[make_job("a"), make_job("b")]))
        task = mt.MatchTask("rs_1", ["a"])
        assert [j.encrypt_job_id for j in mt._pick_jobs(task)] == ["a"]


def test_match_task_scope_info():
    """验证匹配范围说明。"""
    from boss_web.services import match_task as mt

    task = mt.MatchTask("rs_1", ["a", "b"], crawl_task_id="tk_9")
    scope = task.scope_info()
    assert scope["type"] == "crawl_task"
    assert scope["crawl_task_id"] == "tk_9"

    snap = task.snapshot()
    assert snap["crawl_task_id"] == "tk_9"
    assert snap["scope"]["type"] == "crawl_task"


def test_api_list_tasks_empty(client: TestClient):
    """验证任务列表为空。"""
    r = client.get("/api/crawl/tasks")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_api_list_tasks_not_eaten_by_task_id_route(client: TestClient):
    """`GET /api/crawl/tasks` 必须走列表路由，不是 `/{task_id}` 的 `tasks`。"""
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
    """验证任务详情接口。"""
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
    """验证缺任务 404。"""
    r = client.get("/api/crawl/tasks/nope")
    assert r.status_code == 404


def test_api_task_jobs_marks_missing(client: TestClient):
    """验证任务岗位接口标记缺失。"""
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
    """验证批量匹配任务。"""
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
    """验证空交集批量匹配。"""
    with JobStore() as store:
        store.create_crawl_task("tk_1")
        store.save_page(make_page(jobs=[make_job("a")]), task_id="tk_1")
        store.delete_job("a")

    r = client.post("/api/crawl/tasks/tk_1/match", json={"resume_id": "rs_1"})
    assert r.status_code == 422
    assert "本批职位已不在库中" in r.json()["message"]


def test_api_match_task_batch_requires_resume(client: TestClient):
    """验证批量匹配需要简历。"""
    r = client.post("/api/crawl/tasks/tk_1/match", json={"resume_id": ""})
    assert r.status_code == 422
    assert "简历" in r.json()["message"]


# --- 站点归属（多平台）：批次账按站点分账 ---


def test_create_crawl_task_站点归属落库():
    """`crawl_tasks.site` 是批次账的分账键，跟 `jobs.site` 同一套口径。"""
    with JobStore() as store:
        store.create_crawl_task("tk_site", site="51job")
    assert _task_site("tk_site") == "51job"


def test_create_crawl_task_不传站点吃兜底():
    """单站点旧账口径：没传就跟列默认同值，**不是空串**。"""
    with JobStore() as store:
        store.create_crawl_task("tk_fallback")
    assert _task_site("tk_fallback") == DEFAULT_SITE == "boss"


def test_list_crawl_tasks_按站点切片():
    """`site=None` = 跨站点全量（共用口）；传了就只列本站批次（站点口）。"""
    with JobStore() as store:
        store.create_crawl_task("tk_a", created_at=1.0, site="boss")
        store.create_crawl_task("tk_b", created_at=2.0, site="51job")
        store.create_crawl_task("tk_c", created_at=3.0, site="boss")

        assert [t["task_id"] for t in store.list_crawl_tasks(site="boss")] == ["tk_c", "tk_a"]
        assert [t["task_id"] for t in store.list_crawl_tasks(site="51job")] == ["tk_b"]
        assert len(store.list_crawl_tasks()) == 3

        assert store.count_crawl_tasks(site="boss") == 2
        assert store.count_crawl_tasks(site="51job") == 1
        assert store.count_crawl_tasks() == 3


def test_批次回包带site():
    """共用页要标来源，`site` 得随行出门。"""
    with JobStore() as store:
        store.create_crawl_task("tk_row", site="51job")
        assert store.get_crawl_task("tk_row")["site"] == "51job"


def test_api_共用批次列表跨站点(client: TestClient):
    """共用口**不切片**：匹配/出题/简历优化选批次要看得见所有来源。

    这条也是别名的守门员——`/api/crawl/tasks` 一旦被 legacy 别名接走，
    `resolve_site` 会给它套上 `site="boss"`，51job 的批次立刻从列表里消失。
    """
    with JobStore() as store:
        store.create_crawl_task("tk_boss", created_at=1.0, site="boss")
        store.create_crawl_task("tk_51", created_at=2.0, site="51job")

    body = client.get("/api/crawl/tasks").json()
    assert body["total"] == 2
    assert {i["site"] for i in body["items"]} == {"boss", "51job"}
    assert {i["task_id"] for i in body["items"]} == {"tk_boss", "tk_51"}


def test_api_站点口批次列表只列本站(client: TestClient):
    """`/api/sites/{site}/crawl/tasks` 是切片口，别家站点的批次不串进来。"""
    with JobStore() as store:
        store.create_crawl_task("tk_boss", created_at=1.0, site="boss")
        store.create_crawl_task("tk_51", created_at=2.0, site="51job")

    body = client.get("/api/sites/boss/crawl/tasks").json()
    assert [i["task_id"] for i in body["items"]] == ["tk_boss"]
    assert body["total"] == 1


def test_api_站点口批次详情照样可读(client: TestClient):
    """详情是共用读法（跟 `jobs.py` 的 `get_job` 同一条先例）：路径只切列表。

    平台页可能要跳去看一个跨站点批次的 JD，那里没有站点语义要守。
    """
    with JobStore() as store:
        store.create_crawl_task("tk_51", site="51job")
    assert client.get("/api/sites/boss/crawl/tasks/tk_51").status_code == 200
