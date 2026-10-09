"""职位清洗规则的单元测试。"""

from __future__ import annotations

import json

import pytest

from boss_jobs.models import (
    Job,
    clean_page,
    clean_pages,
    clean_salary,
    clean_text,
    decode_salary_font,
    join_location,
)


def api_item(**overrides) -> dict:
    """一份形状齐全的接口 jobList 元素。"""
    item = {
        "encryptJobId": "abc123xyz",
        "jobName": "  Python 开发工程师  ",
        "brandName": "示例 科技",
        "salaryDesc": "15-25K",
        "jobExperience": "3-5年",
        "jobDegree": "本科",
        "brandIndustry": "互联网/AI",
        "brandScaleName": "100-499人",
        "brandStageName": "已上市",
        "cityName": "广州",
        "areaDistrict": "天河区",
        "businessDistrict": "棠下",
        "jobLabels": ["3-5年", "本科", "Python", "Django"],
        "skills": ["Python", "MySQL"],
        "welfareList": ["五险一金", "年终奖"],
        "bossName": "张三",
        "bossTitle": "技术总监",
        "expectId": 1174275774,
        "jobType": 0,
        "jobValidStatus": 1,
        "securityId": "sec-id",
        "lid": "lid-1",
    }
    item.update(overrides)
    return item


def ok_payload(items, *, has_more: bool = True, lid: str = "") -> dict:
    return {
        "code": 0,
        "message": "Success",
        "zpData": {"jobList": items, "hasMore": has_more, "lid": lid},
    }




@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ""),
        ("", ""),
        ("  Python   开发  ", "Python 开发"),
        (123, "123"),
        ("\t\n广州  天河 ", "广州 天河"),
    ],
)
def test_clean_text(raw, want):
    assert clean_text(raw) == want


def test_decode_salary_font_entities_and_chars():
    """S 表：``&#xe031;``→0 … ``&#xe03a;``→9，实体与裸码点同一套映射。"""
    assert decode_salary_font("&#xe039;-&#xe032;&#xe034;K") == "8-13K"
    assert decode_salary_font(f"{chr(0xE039)}-{chr(0xE032)}{chr(0xE034)}K") == "8-13K"


def test_clean_salary_normalizes_k_and_whitespace():
    assert clean_salary("  8-15k  ") == "8-15K"
    assert clean_salary("10－20K") == "10－20K"  # 全角连字符原样保留
    assert clean_salary(None) == ""


def test_join_location_skips_empty_segments():
    assert join_location("广州", "天河区", "棠下") == "广州·天河区·棠下"
    assert join_location("广州", "", "棠下") == "广州·棠下"
    assert join_location("", "", "") == ""




def test_from_api_maps_todo_fields():
    job = Job.from_api(api_item())
    assert job.job_name == "Python 开发工程师"
    assert job.brand_name == "示例 科技"
    assert job.location == "广州·天河区·棠下"
    assert job.salary_desc == "15-25K"
    assert job.job_experience == "3-5年"
    assert job.job_degree == "本科"
    assert job.brand_industry == "互联网/AI"
    assert job.brand_scale_name == "100-499人"
    assert job.is_valid


def test_from_api_falls_back_to_job_labels_for_experience_and_degree():
    job = Job.from_api(api_item(jobExperience="", jobDegree="", jobLabels=["1-3年", "大专", "Java"]))
    assert job.job_experience == "1-3年"
    assert job.job_degree == "大专"


def test_from_api_keeps_raw_json():
    item = api_item()
    job = Job.from_api(item)
    assert json.loads(job.raw_json) == item


def test_is_valid_requires_id_name_brand():
    assert not Job.from_api(api_item(encryptJobId="")).is_valid
    assert not Job.from_api(api_item(jobName="  ")).is_valid
    assert not Job.from_api(api_item(brandName="")).is_valid


def test_from_api_rejects_non_dict():
    with pytest.raises(TypeError):
        Job.from_api("not-a-dict")




def test_clean_page_keeps_valid_drops_invalid():
    payload = ok_payload(
        [
            api_item(encryptJobId="a", jobName="岗位A"),
            api_item(encryptJobId="", jobName="没主键"),
            api_item(encryptJobId="b", jobName="岗位B", brandName=""),
            api_item(encryptJobId="c", jobName="岗位C"),
        ]
    )
    result = clean_page(payload, page=1)
    assert [j.encrypt_job_id for j in result.jobs] == ["a", "c"]
    assert result.raw_count == 4
    assert result.dropped_count == 2
    assert any("字段不全" in reason for reason in result.dropped)


def test_clean_page_dedupes_within_page():
    payload = ok_payload(
        [
            api_item(encryptJobId="same", jobName="重复"),
            api_item(encryptJobId="same", jobName="重复"),
            api_item(encryptJobId="other", jobName="另一个"),
        ]
    )
    result = clean_page(payload, page=1)
    assert [j.encrypt_job_id for j in result.jobs] == ["same", "other"]
    assert result.dropped_count == 1


def test_clean_page_reads_has_more_and_lid():
    payload = ok_payload([api_item()], has_more=True, lid="page-lid")
    result = clean_page(payload, page=3)
    assert result.has_more is True
    assert result.lid == "page-lid"
    assert result.page == 3
    assert result.jobs[0].page == 3


def test_clean_page_handles_empty_or_broken_payload():
    """缺字段、空列表、``zpData`` 不是对象都只当空页，不抛。"""
    assert clean_page({"code": 0, "zpData": {}}, page=1).is_empty
    assert clean_page({"code": 0, "zpData": {"jobList": None}}, page=1).is_empty
    assert clean_page({"code": 0}, page=1).is_empty
    assert clean_page({"code": 0, "zpData": "oops"}, page=1).is_empty


def test_clean_pages_stitches_across_pages():
    pages = [
        ok_payload([api_item(encryptJobId="p1")]),
        ok_payload([api_item(encryptJobId="p2")]),
    ]
    jobs = clean_pages(pages)
    assert [j.encrypt_job_id for j in jobs] == ["p1", "p2"]
    assert [j.page for j in jobs] == [1, 2]




def test_clean_desc_strips_html_and_keeps_lines():
    from boss_jobs.models import clean_desc

    raw = "<p>岗位职责：</p><br>1. 写代码&nbsp;&nbsp;\n\n\n\n2. 测试"
    assert clean_desc(raw) == "岗位职责：\n\n1. 写代码\n\n2. 测试"


def test_clean_desc_plain_text_passthrough():
    from boss_jobs.models import clean_desc

    assert clean_desc("岗位职责：\n1. 写代码") == "岗位职责：\n1. 写代码"
    assert clean_desc(None) == ""
    assert clean_desc(123) == "123"


def test_extract_job_desc_reads_job_info():
    """实测：正文在 ``zpData.jobInfo.postDescription``，不是顶层。"""
    from boss_jobs.models import extract_job_desc

    payload = {
        "code": 0,
        "zpData": {
            "jobInfo": {"postDescription": "岗位职责：\n写代码"},
            "postDescription": "顶层这格没有",
        },
    }
    assert extract_job_desc(payload) == "岗位职责：\n写代码"


def test_extract_job_desc_falls_back_to_top_level():
    from boss_jobs.models import extract_job_desc

    assert extract_job_desc({"zpData": {"postDescription": "顶层兜底"}}) == "顶层兜底"
    assert extract_job_desc({"zpData": {}}) == ""
    assert extract_job_desc({"code": 1}) == ""


def test_job_to_dict_includes_desc():
    job = Job.from_api(
        {
            "encryptJobId": "x1",
            "jobName": "岗位",
            "brandName": "公司",
        },
        page=1,
    )
    data = job.to_dict()
    assert data["job_desc"] == ""
    assert data["detail_fetched_at"] == ""
