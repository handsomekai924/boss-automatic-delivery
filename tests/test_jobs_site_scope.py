"""跨站点职位库视图：`jobs.site` 的过滤与切片（todo3 Stage 3 第三条）。

多平台之后 `jobs` 表是所有站点共用的一张表，站点维度靠 `jobs.site` 分账：
列表/统计/清空按站点切片，别家站点的职位不串进来。这里的规矩是**查询层过滤**，
不是把库拆成 N 张表。
"""

from __future__ import annotations

import pytest

import boss_db
from boss_jobs.models import Job, PageResult
from boss_jobs.store import DEFAULT_SITE, JobStore


@pytest.fixture(autouse=True)
def _clean_cache():
    """每条用例前后关掉连接缓存。"""
    boss_db.close_all()
    yield
    boss_db.close_all()


def _seed(rows: list[tuple[str, str, str]]) -> None:
    """按 `(encrypt_job_id, site, job_name)` 直接落几行职位。

    `_INSERT_JOB` 故意不写 `site`（沿用列默认，单站点旧账全归 BOSS），
    测试要造多站点行就得显式写。
    """
    conn = boss_db.connect(boss_db.resolve_db_path(), migrate=False)
    try:
        for jid, site, name in rows:
            conn.execute(
                "INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at, site) "
                "VALUES (?, ?, '示例科技', '2026-10-28 08:00:00', ?)",
                (jid, name, site),
            )
        conn.commit()
    finally:
        conn.close()


# --- 查询层过滤 ---


def test_list_jobs_按站点切片():
    """传了 `site` 就只列这个站点的职位。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端"), ("j3", "boss", "测试")])
    with JobStore() as store:
        names = [j.job_name for j in store.list_jobs(site="boss")]
        assert sorted(names) == ["前端", "测试"]
        assert [j.job_name for j in store.list_jobs(site="51job")] == ["后端"]


def test_list_jobs_不传站点就是全站():
    """`site=None` = 不切片；老调用方行为不变。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端")])
    with JobStore() as store:
        assert len(store.list_jobs()) == 2


def test_count_jobs_matching_与列表同款切片():
    """翻页前的总数跟列表必须同一个口径，否则分页对不上。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端"), ("j3", "boss", "测试")])
    with JobStore() as store:
        assert store.count_jobs_matching(site="boss") == 2
        assert store.count_jobs_matching(site="51job") == 1
        assert store.count_jobs_matching() == 3


def test_count_jobs_按站点数():
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端")])
    with JobStore() as store:
        assert store.count_jobs(site="boss") == 1
        assert store.count_jobs() == 2


def test_summary_按站点汇总():
    """汇总里的总条数与 top_cities 也跟着切片。"""
    _seed([
        ("j1", "boss", "前端"),
        ("j2", "boss", "测试"),
        ("j3", "51job", "后端"),
    ])
    with JobStore() as store:
        s = store.summary(site="boss")
    assert s["jobs"] == 2
    assert sum(c["count"] for c in s["top_cities"]) == 2


def test_clear_jobs_只清本站点():
    """清空是破坏性操作，绝不能跨站点端掉别家的职位。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端")])
    with JobStore() as store:
        assert store.clear_jobs(site="boss") == 1
        assert store.count_jobs() == 1
        assert [j.job_name for j in store.list_jobs()] == ["后端"]


# --- 抓取落库：站点归属要写进去，别只吃列默认 ---


def _job(jid: str, site: str = "") -> Job:
    """一条最省的职位对象。"""
    return Job(
        job_name=f"岗位 {jid}", brand_name="示例科技", location="", salary_desc="",
        job_experience="", job_degree="", brand_industry="", brand_scale_name="",
        encrypt_job_id=jid, site=site,
    )


def _site_of(jid: str) -> str:
    """直接读 `jobs.site`（不经过查询层，免得被过滤口径骗了）。"""
    conn = boss_db.connect(boss_db.resolve_db_path(), migrate=False)
    try:
        row = conn.execute(
            "SELECT site FROM jobs WHERE encrypt_job_id = ?", (jid,)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


def _page_sites() -> list[str]:
    """`fetch_pages` 流水的站点归属。"""
    conn = boss_db.connect(boss_db.resolve_db_path(), migrate=False)
    try:
        return [str(r[0]) for r in conn.execute("SELECT site FROM fetch_pages ORDER BY id")]
    finally:
        conn.close()


def test_save_page_站点归属写进两张表():
    """跨站点视图的底账：`jobs.site` 与 `fetch_pages.site` 都得跟着调用方写。"""
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(_job("p1"),)), site="51job")
    assert _site_of("p1") == "51job"
    assert _page_sites() == ["51job"]


def test_save_page_不传站点吃兜底():
    """单站点旧账口径：没传站点就跟列默认同值，**不是空串**。

    空串会让这条职位从 `/api/sites/boss/jobs` 里消失——`site = ?` 是精确匹配。
    """
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(_job("p2"),)))
    assert _site_of("p2") == DEFAULT_SITE == "boss"


def test_save_page_重抓不改站点归属():
    """来了空值就留原值——归属不是重抓能随便改的（跟 `last_greeted_at` 同一条道理）。"""
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(_job("p3"),)), site="51job")
        store.save_page(PageResult(page=1, jobs=(_job("p3"),)))  # 空站点重抓
    assert _site_of("p3") == "51job"


def test_save_page_职位自带站点也认():
    """`Job.site` 有值就用它；调用方不用重复传。"""
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(_job("p4", site="51job"),)))
    assert _site_of("p4") == "51job"


def test_list_pages_按站点切片():
    """抓取流水也是站点账，别把别家的翻页记录混进本站统计。"""
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(_job("g1"),)), site="boss")
        store.save_page(PageResult(page=1, jobs=(_job("g2"),)), site="51job")
        assert [p["page"] for p in store.list_pages(site="51job")] == [1]
        assert len(store.list_pages()) == 2
        assert len(store.list_pages(site="boss")) == 1


def test_summary_翻页账也按站点汇():
    """`summary(site=…)` 的 `pages` / `raw_seen` 得跟着切片，不然站点统计是假的。"""
    with JobStore() as store:
        store.save_page(PageResult(page=1, jobs=(_job("g3"),), raw_count=5), site="51job")
        mine = store.summary(site="boss")
        theirs = store.summary(site="51job")
    assert (mine["pages"], mine["raw_seen"]) == (0, 0)
    assert (theirs["pages"], theirs["raw_seen"]) == (1, 5)


# --- 模型带得动站点 ---


def test_job_to_dict_带site():
    """跨站点视图要能标出每条是哪儿来的，`site` 得出门。"""
    _seed([("j1", "51job", "后端")])
    with JobStore() as store:
        job = store.list_jobs()[0]
    assert job.site == "51job"
    assert job.to_dict()["site"] == "51job"


def test_清洗出来的职位还没落库时_site为空():
    """抓下来还没入库的清洗对象不预设站点，落库由列默认补齐。"""
    assert Job(job_name="n", brand_name="b", location="", salary_desc="",
               job_experience="", job_degree="", brand_industry="",
               brand_scale_name="", encrypt_job_id="x").site == ""


# --- 接口层：路径里的 {site} 是切片口 ---


@pytest.fixture
def client():
    """起一个不带假服务端的测试客户端。"""
    from fastapi.testclient import TestClient

    from boss_web import create_app

    return TestClient(create_app())


def test_api_list_只回本站点(client):
    """`/api/sites/{site}/jobs` 不许串别家站点的职位。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端")])
    body = client.get("/api/sites/boss/jobs").json()
    assert body["total"] == 1
    assert [i["job_name"] for i in body["items"]] == ["前端"]
    assert body["items"][0]["site"] == "boss"


def test_api_stats_只汇本站点(client):
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端"), ("j3", "boss", "测试")])
    body = client.get("/api/sites/boss/jobs/stats").json()
    assert body["jobs"] == 2


def test_api_clear_只清本站点(client):
    """清空带站点安全绳，别把别家的职位一起端了。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端")])
    body = client.post("/api/sites/boss/jobs/clear", json={"confirm": True}).json()
    assert body["deleted"] == 1
    with JobStore() as store:
        assert [j.job_name for j in store.list_jobs()] == ["后端"]


def test_api_legacy别名也归boss(client):
    """共存期别名 `/api/jobs` 走 `site="boss"`，行为跟新面一致。"""
    _seed([("j1", "boss", "前端"), ("j2", "51job", "后端")])
    assert client.get("/api/jobs").json()["total"] == 1
