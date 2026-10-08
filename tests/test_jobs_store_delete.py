"""JobStore 删除方法的单元测试。"""

from __future__ import annotations

import pytest

from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore


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


def make_page(jobs) -> PageResult:
    jobs = tuple(jobs)
    return PageResult(page=1, jobs=jobs, has_more=True, raw_count=len(jobs), dropped=())


@pytest.fixture
def store(tmp_path):
    with JobStore(tmp_path / "jobs.db") as s:
        s.save_page(
            make_page(
                [
                    make_job("a", job_name="前端工程师", city_name="广州"),
                    make_job("b", job_name="Python 开发", city_name="深圳"),
                    make_job("c", job_name="产品经理", brand_name="前端科技", city_name="广州"),
                ]
            )
        )
        yield s


def test_delete_job(store: JobStore):
    assert store.delete_job("a") is True
    assert store.count_jobs() == 2
    assert store.get_job("a") is None
    assert store.delete_job("a") is False


def test_delete_jobs_bulk(store: JobStore):
    assert store.delete_jobs(["a", "c", "ghost"]) == 2
    assert store.count_jobs() == 1
    assert store.delete_jobs([]) == 0


def test_count_jobs_matching(store: JobStore):
    assert store.count_jobs_matching() == 3
    assert store.count_jobs_matching(city="广州") == 2
    assert store.count_jobs_matching(keyword="前端") == 2  # 岗位名 + 公司名
    assert store.count_jobs_matching(city="广州", keyword="产品") == 1


def test_clear_jobs_with_condition(store: JobStore):
    deleted = store.clear_jobs(city="广州")
    assert deleted == 2
    assert store.count_jobs() == 1
    assert store.get_job("b") is not None


def test_clear_jobs_all(store: JobStore):
    assert store.clear_jobs() == 3
    assert store.count_jobs() == 0
    # 抓取流水保留
    assert store.count_pages() == 1
