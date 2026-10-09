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




def test_job_desc_defaults_empty(store: JobStore):
    store.save_page(make_page(jobs=[make_job("a")]))
    job = store.get_job("a")
    assert job is not None
    assert job.job_desc == ""
    assert job.detail_fetched_at == ""


def test_update_job_desc_writes_and_stamps(store: JobStore):
    store.save_page(make_page(jobs=[make_job("a")]))
    assert store.update_job_desc("a", "岗位职责：\n写代码", fetched_at="2026-10-08 12:00:00")
    job = store.get_job("a")
    assert job is not None
    assert job.job_desc == "岗位职责：\n写代码"
    assert job.detail_fetched_at == "2026-10-08 12:00:00"


def test_update_job_desc_marks_empty_too(store: JobStore):
    """详情回来没 JD 也要盖时间戳，免得下次补抓又翻它一遍。"""
    store.save_page(make_page(jobs=[make_job("a")]))
    assert store.update_job_desc("a", "", fetched_at="2026-10-08 12:00:00")
    job = store.get_job("a")
    assert job is not None
    assert job.job_desc == ""
    assert job.detail_fetched_at == "2026-10-08 12:00:00"


def test_update_job_desc_missing_job_returns_false(store: JobStore):
    assert not store.update_job_desc("nope", "x")


def test_resave_does_not_wipe_job_desc(store: JobStore):
    """列表接口不回 JD：重抓一页不能拿空串把已抓到的描述抹掉。"""
    store.save_page(make_page(jobs=[make_job("a")]))
    store.update_job_desc("a", "已抓到的 JD", fetched_at="2026-10-08 12:00:00")
    store.save_page(make_page(page=2, jobs=[make_job("a", job_name="改过名", page=2)]))
    job = store.get_job("a")
    assert job is not None
    assert job.job_name == "改过名"
    assert job.job_desc == "已抓到的 JD"
    assert job.detail_fetched_at == "2026-10-08 12:00:00"


def test_list_jobs_missing_desc(store: JobStore):
    store.save_page(make_page(jobs=[make_job("a"), make_job("b"), make_job("c")]))
    store.update_job_desc("b", "有描述", fetched_at="2026-10-08 12:00:00")
    assert store.count_jobs_missing_desc() == 2
    missing = store.list_jobs_missing_desc()
    assert {j.encrypt_job_id for j in missing} == {"a", "c"}
    assert store.list_jobs_missing_desc(limit=1) and len(store.list_jobs_missing_desc(limit=1)) == 1


def test_list_jobs_missing_desc_已有描述不再重复获取(store: JobStore):
    """已有 JD 的一条都不回；抓过但回空 JD 的也跳过（别每次撞同一批）。"""
    store.save_page(
        make_page(jobs=[make_job("a"), make_job("b"), make_job("c"), make_job("d")])
    )
    store.update_job_desc("b", "已经有 JD 了", fetched_at="2026-10-08 12:00:00")
    store.update_job_desc("c", "", fetched_at="2026-10-08 12:00:00")
    assert store.count_jobs_missing_desc() == 2
    assert {j.encrypt_job_id for j in store.list_jobs_missing_desc()} == {"a", "d"}


def test_fetched_desc_ids(store: JobStore):
    """批量问「哪些已经抓过详情」——抓取流程顺带补 JD 时跳过已有的；空 JD 也算抓过。"""
    store.save_page(make_page(jobs=[make_job("a"), make_job("b"), make_job("c")]))
    store.update_job_desc("b", "有描述", fetched_at="2026-10-08 12:00:00")
    store.update_job_desc("c", "", fetched_at="2026-10-08 12:00:00")

    assert store.fetched_desc_ids(["a", "b", "c", "不在库里的"]) == {"b", "c"}
    assert store.fetched_desc_ids([]) == set()
    assert store.fetched_desc_ids(["a"]) == set()


def test_column_migration_adds_desc_columns(tmp_path):
    """老库（建表时还没有 job_desc 列）打开后要能自动补列；再开一次也幂等。"""
    import sqlite3

    import boss_db

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE jobs (
            encrypt_job_id     TEXT PRIMARY KEY,
            job_name           TEXT NOT NULL,
            brand_name         TEXT NOT NULL,
            location           TEXT NOT NULL DEFAULT '',
            salary_desc        TEXT NOT NULL DEFAULT '',
            job_experience     TEXT NOT NULL DEFAULT '',
            job_degree         TEXT NOT NULL DEFAULT '',
            brand_industry     TEXT NOT NULL DEFAULT '',
            brand_scale_name   TEXT NOT NULL DEFAULT '',
            city_name          TEXT NOT NULL DEFAULT '',
            area_district      TEXT NOT NULL DEFAULT '',
            business_district  TEXT NOT NULL DEFAULT '',
            brand_stage_name   TEXT NOT NULL DEFAULT '',
            job_labels         TEXT NOT NULL DEFAULT '[]',
            skills             TEXT NOT NULL DEFAULT '[]',
            welfare_list       TEXT NOT NULL DEFAULT '[]',
            boss_name          TEXT NOT NULL DEFAULT '',
            boss_title         TEXT NOT NULL DEFAULT '',
            expect_id          TEXT NOT NULL DEFAULT '',
            job_type           INTEGER NOT NULL DEFAULT 0,
            job_valid_status   INTEGER NOT NULL DEFAULT 1,
            security_id        TEXT NOT NULL DEFAULT '',
            lid                TEXT NOT NULL DEFAULT '',
            page               INTEGER NOT NULL DEFAULT 0,
            raw_json           TEXT NOT NULL DEFAULT '',
            fetched_at         TEXT NOT NULL
        );
        INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at)
        VALUES ('legacy-1', '老岗位', '老公司', '2026-01-01 00:00:00');
        """
    )
    conn.commit()
    conn.close()

    with JobStore(path) as store:
        job = store.get_job("legacy-1")
        assert job is not None
        assert job.job_desc == ""
        assert job.detail_fetched_at == ""
        assert store.update_job_desc("legacy-1", "补上的 JD")
        job = store.get_job("legacy-1")
        assert job is not None
        assert job.job_desc == "补上的 JD"

    with JobStore(path) as store:
        assert store.count_jobs() == 1
