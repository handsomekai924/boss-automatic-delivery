"""职位入库（SQLite）的单元测试。"""

from __future__ import annotations

import pytest

from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore, open_store


def make_job(encrypt_job_id: str = "id-1", **overrides) -> Job:
    defaults = dict(
        job_name="Python 开发",
        brand_name="示例科技",
        location="广州·天河区·棠下",
        salary_desc="15-25K",
        job_experience="3-5年",
        job_degree="本科",
        brand_industry="互联网/AI",
        brand_scale_name="100-499人",
        encrypt_job_id=encrypt_job_id,
        raw_json="{}",
        city_name="广州",
        area_district="天河区",
        business_district="棠下",
        job_labels=("3-5年", "本科", "Python"),
        skills=("Python",),
        welfare_list=("五险一金",),
        page=1,
    )
    defaults.update(overrides)
    return Job(**defaults)


def make_page(page: int = 1, jobs=(), has_more: bool = True, raw_count: int | None = None) -> PageResult:
    jobs = tuple(jobs)
    return PageResult(
        page=page,
        jobs=jobs,
        has_more=has_more,
        raw_count=raw_count if raw_count is not None else len(jobs),
        dropped=(),
    )


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "jobs.db") as s:
        yield s


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #


def test_save_page_inserts_and_counts(store: JobStore):
    outcome = store.save_page(make_page(jobs=[make_job("a"), make_job("b")]))
    assert outcome.inserted == 2
    assert outcome.updated == 0
    assert store.count_jobs() == 2
    assert store.count_pages() == 1


def test_save_page_upserts_same_job_id(store: JobStore):
    store.save_page(make_page(jobs=[make_job("a", job_name="旧岗位")]))
    outcome = store.save_page(
        make_page(page=2, jobs=[make_job("a", job_name="新岗位", page=2)], raw_count=1),
    )
    assert outcome.inserted == 0
    assert outcome.updated == 1
    assert store.count_jobs() == 1

    job = store.get_job("a")
    assert job is not None
    assert job.job_name == "新岗位"
    assert job.page == 2


def test_save_page_records_drop_reason(store: JobStore):
    page = PageResult(
        page=1,
        jobs=(make_job("a"),),
        has_more=True,
        raw_count=3,
        dropped=("第 2 条字段不全", "第 3 条同页重复"),
    )
    outcome = store.save_page(page)
    assert outcome.inserted == 1
    assert outcome.skipped == 2

    rows = store.list_pages()
    assert rows[0]["raw_count"] == 3
    assert rows[0]["kept_count"] == 1
    assert rows[0]["dropped_count"] == 2
    assert "字段不全" in rows[0]["note"]


def test_save_pages_batch(store: JobStore):
    outcomes = store.save_pages(
        [
            make_page(jobs=[make_job("a")]),
            make_page(page=2, jobs=[make_job("b")]),
        ]
    )
    assert [o.total for o in outcomes] == [1, 1]
    assert store.count_jobs() == 2
    assert store.count_pages() == 2


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def test_roundtrip_preserves_fields(store: JobStore):
    store.save_page(make_page(jobs=[make_job("a")]))
    job = store.get_job("a")
    assert job is not None
    assert job.job_name == "Python 开发"
    assert job.location == "广州·天河区·棠下"
    assert job.salary_desc == "15-25K"
    assert job.job_experience == "3-5年"
    assert job.job_degree == "本科"
    assert job.brand_industry == "互联网/AI"
    assert job.brand_scale_name == "100-499人"
    assert job.job_labels == ("3-5年", "本科", "Python")
    assert job.city_name == "广州"


def test_list_jobs_filters(store: JobStore):
    store.save_page(
        make_page(
            jobs=[
                make_job("a", job_name="Python 开发", city_name="广州"),
                make_job("b", job_name="Java 开发", city_name="深圳"),
                make_job("c", job_name="前端", brand_name="广州小厂", city_name="广州"),
            ]
        )
    )
    assert {j.encrypt_job_id for j in store.list_jobs(city="广州")} == {"a", "c"}
    assert {j.encrypt_job_id for j in store.list_jobs(keyword="开发")} == {"a", "b"}
    assert {j.encrypt_job_id for j in store.list_jobs(keyword="小厂")} == {"c"}
    assert len(store.list_jobs(limit=1)) == 1


def test_summary_counts(store: JobStore):
    store.save_page(make_page(jobs=[make_job("a", city_name="广州"), make_job("b", city_name="广州")]))
    store.save_page(make_page(page=2, jobs=[make_job("c", city_name="深圳")], raw_count=3))
    summary = store.summary()
    assert summary["jobs"] == 3
    assert summary["pages"] == 2
    assert summary["raw_seen"] == 2 + 3  # 第一页 2 条原始，第二页声称 3 条
    assert summary["kept_seen"] == 3
    assert summary["top_cities"][0] == {"city": "广州", "count": 2}


def test_get_job_missing_returns_none(store: JobStore):
    assert store.get_job("nope") is None


def test_memory_db_works():
    with JobStore(":memory:") as store:
        store.save_page(make_page(jobs=[make_job("a")]))
        assert store.count_jobs() == 1


def test_open_store_uses_path(tmp_path):
    path = tmp_path / "sub" / "jobs.db"
    with open_store(path) as store:
        store.save_page(make_page(jobs=[make_job("a")]))
    assert path.exists()
