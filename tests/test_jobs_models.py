"""职位清洗规则的单元测试。"""

from __future__ import annotations

import json

import pytest

from boss_jobs.models import (
    ExpectItem,
    ExpectList,
    Job,
    clean_expects,
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
    """构造成功响应体。

    Args:
        items (list): jobList 元素列表。
        has_more (bool): 是否还有下一页。
        lid (str): 分页追踪标识。

    Returns:
        带 `code` / `zpData` 的完整响应 dict。
    """
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
    """验 `clean_text` 对空白与非字符串输入都归一成规范文本。"""
    assert clean_text(raw) == want


def test_decode_salary_font_entities_and_chars():
    """验字体实体与私有区字符都能解出薪资数字。"""
    assert decode_salary_font("&#xe039;-&#xe032;&#xe034;K") == "8-13K"
    assert decode_salary_font(f"{chr(0xE039)}-{chr(0xE032)}{chr(0xE034)}K") == "8-13K"


def test_clean_salary_normalizes_k_and_whitespace():
    """验 `clean_salary` 规范化 K 大小写与空白，全角连字符原样保留。"""
    assert clean_salary("  8-15k  ") == "8-15K"
    assert clean_salary("10－20K") == "10－20K"
    assert clean_salary(None) == ""


def test_join_location_skips_empty_segments():
    """验 `join_location` 跳过空段并用间隔号拼接。"""
    assert join_location("广州", "天河区", "棠下") == "广州·天河区·棠下"
    assert join_location("广州", "", "棠下") == "广州·棠下"
    assert join_location("", "", "") == ""


def test_from_api_maps_todo_fields():
    """验 `Job.from_api` 映射展示字段并保留有效性。"""
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
    """验经验/学历缺失时从 jobLabels 回退。"""
    job = Job.from_api(api_item(jobExperience="", jobDegree="", jobLabels=["1-3年", "大专", "Java"]))
    assert job.job_experience == "1-3年"
    assert job.job_degree == "大专"


def test_from_api_keeps_raw_json():
    """验原始 JSON 被完整保留。"""
    item = api_item()
    job = Job.from_api(item)
    assert json.loads(job.raw_json) == item


def test_is_valid_requires_id_name_brand():
    """验主键、职位名、公司名缺一即无效。"""
    assert not Job.from_api(api_item(encryptJobId="")).is_valid
    assert not Job.from_api(api_item(jobName="  ")).is_valid
    assert not Job.from_api(api_item(brandName="")).is_valid


def test_from_api_rejects_non_dict():
    """验 `from_api` 对非 dict 输入抛 `TypeError`。"""
    with pytest.raises(TypeError):
        Job.from_api("not-a-dict")


def test_clean_page_keeps_valid_drops_invalid():
    """验单页清洗保留有效职位并统计丢弃原因。"""
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
    """验同页内主键重复只留一条。"""
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
    """验翻页标记与 lid 落到清洗结果上。"""
    payload = ok_payload([api_item()], has_more=True, lid="page-lid")
    result = clean_page(payload, page=3)
    assert result.has_more is True
    assert result.lid == "page-lid"
    assert result.page == 3
    assert result.jobs[0].page == 3


def test_clean_page_handles_empty_or_broken_payload():
    """验残缺或错型 payload 一律视为空页不抛错。"""
    assert clean_page({"code": 0, "zpData": {}}, page=1).is_empty
    assert clean_page({"code": 0, "zpData": {"jobList": None}}, page=1).is_empty
    assert clean_page({"code": 0}, page=1).is_empty
    assert clean_page({"code": 0, "zpData": "oops"}, page=1).is_empty


def test_clean_pages_stitches_across_pages():
    """验多页清洗结果按顺序拼接并标注页码。"""
    pages = [
        ok_payload([api_item(encryptJobId="p1")]),
        ok_payload([api_item(encryptJobId="p2")]),
    ]
    jobs = clean_pages(pages)
    assert [j.encrypt_job_id for j in jobs] == ["p1", "p2"]
    assert [j.page for j in jobs] == [1, 2]


def test_clean_desc_strips_html_and_keeps_lines():
    """验职位描述去标签并保留段落空行。"""
    from boss_jobs.models import clean_desc

    raw = "<p>岗位职责：</p><br>1. 写代码&nbsp;&nbsp;\n\n\n\n2. 测试"
    assert clean_desc(raw) == "岗位职责：\n\n1. 写代码\n\n2. 测试"


def test_clean_desc_plain_text_passthrough():
    """验纯文本原样通过，空值转空串。"""
    from boss_jobs.models import clean_desc

    assert clean_desc("岗位职责：\n1. 写代码") == "岗位职责：\n1. 写代码"
    assert clean_desc(None) == ""
    assert clean_desc(123) == "123"


def test_extract_job_desc_reads_job_info():
    """验正文取自 `zpData.jobInfo.postDescription`。"""
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
    """验取不到 jobInfo 时回退顶层字段，缺字段给空串。"""
    from boss_jobs.models import extract_job_desc

    assert extract_job_desc({"zpData": {"postDescription": "顶层兜底"}}) == "顶层兜底"
    assert extract_job_desc({"zpData": {}}) == ""
    assert extract_job_desc({"code": 1}) == ""


def test_job_to_dict_includes_desc():
    """验 `to_dict` 含描述相关字段。"""
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


def test_clean_expects_三类字段映射():
    """验三类期望各自的字段映射与 label 拼装。"""
    payload = {
        "code": 0,
        "zpData": {
            "mixExpect": {
                "encryptExpectId": "MIX1",
                "mixExpectType": "2",
                "positionName": "前端开发工程师",
                "locationName": "佛山",
                "location": "101280800",
            },
            "expectList": [
                {"encryptId": "E1", "positionName": "Python", "locationName": "广州", "tagName": "全职"},
            ],
            "partTimeExpectList": [
                {"encryptExpectId": "PT1", "positionName": "派单", "locationName": "佛山"},
            ],
        },
    }
    result = clean_expects(payload)
    assert result.mix is not None
    assert result.mix.kind == "mix"
    assert result.mix.encrypt_expect_id == "MIX1"
    assert result.mix.mix_expect_type == "2"
    assert result.mix.location_code == "101280800"
    assert result.mix.location_name == "佛山"
    assert result.mix.label == "前端开发工程师(佛山)"

    assert len(result.expects) == 1
    assert result.expects[0].kind == "expect"
    assert result.expects[0].encrypt_expect_id == "E1"
    assert result.expects[0].label == "Python(广州)"

    assert len(result.part_time) == 1
    assert result.part_time[0].kind == "part_time"
    assert result.part_time[0].encrypt_expect_id == "PT1"


def test_clean_expects_两个id键名都认():
    """验 `encryptId` 与 `encryptExpectId` 两个键名都识别。"""
    payload = {
        "code": 0,
        "zpData": {
            "expectList": [{"encryptExpectId": "E-BOTH", "positionName": "岗位"}],
            "partTimeExpectList": [{"encryptId": "PT-BOTH", "positionName": "兼职"}],
        },
    }
    result = clean_expects(payload)
    assert result.expects[0].encrypt_expect_id == "E-BOTH"
    assert result.part_time[0].encrypt_expect_id == "PT-BOTH"


def test_clean_expects_缺字段按空处理不抛():
    """验缺字段按空列表处理，坏条目直接丢弃。"""
    assert clean_expects({"code": 0, "zpData": {}}) == ExpectList()
    assert clean_expects({"code": 0}) == ExpectList()
    assert clean_expects({"code": 0, "zpData": None}) == ExpectList()
    result = clean_expects({"code": 0, "zpData": {"expectList": [{"positionName": "没id"}]}})
    assert result.expects == ()
    result = clean_expects({"code": 0, "zpData": {"partTimeExpectList": ["oops", None]}})
    assert result.part_time == ()


def test_expect_item_label_缺哪段跳过哪段():
    """验 label 缺哪段就跳过哪段。"""
    assert ExpectItem(kind="expect", encrypt_expect_id="E1", position_name="Python").label == "Python"
    assert ExpectItem(kind="expect", encrypt_expect_id="E1", position_name="", location_name="广州").label == "广州"
    assert ExpectItem(kind="expect", encrypt_expect_id="E1", position_name="").label == "E1"


def test_expect_list_pick_按kind取首条():
    """验 `pick` 按 kind 取首条，未知 kind 返回 `None`。"""
    result = clean_expects(
        {
            "code": 0,
            "zpData": {
                "mixExpect": {"encryptExpectId": "MIX1", "positionName": "混合"},
                "expectList": [{"encryptId": "E1"}, {"encryptId": "E2"}],
                "partTimeExpectList": [{"encryptExpectId": "PT1"}],
            },
        }
    )
    assert result.pick("recommend").encrypt_expect_id == "MIX1"
    assert result.pick("expect").encrypt_expect_id == "E1"
    assert result.pick("part_time").encrypt_expect_id == "PT1"
    assert result.pick("nope") is None
