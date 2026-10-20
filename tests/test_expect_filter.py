"""`JobExpectFilter` 参数装配的单元测试。"""

from __future__ import annotations

import json

import pytest

from boss_filter.expect import (
    EXPECT_KINDS,
    NEVER_PARAMS,
    PART_TIME_JOB_TYPE,
    JobExpectFilter,
)
from boss_filter.search import JobSearchFilter


def test_to_params_推荐带city加id加混合类型():
    """验推荐态主参数含 city、id 与混合类型。"""
    params = JobExpectFilter(
        kind="recommend",
        encrypt_expect_id="MIX1",
        mix_expect_type="1",
        city="101280100",
    ).to_params()
    assert params["encryptExpectId"] == "MIX1"
    assert params["mixExpectType"] == "1"
    assert params["city"] == "101280100"
    assert params["page"] == "1"
    assert params["pageSize"] == "15"


def test_to_params_兼职写死jobType1903():
    """验兼职态写死 jobType 且不带混合类型。"""
    params = JobExpectFilter(kind="part_time", encrypt_expect_id="PT1").to_params()
    assert params["jobType"] == PART_TIME_JOB_TYPE == "1903"
    assert params["encryptExpectId"] == "PT1"
    assert "mixExpectType" not in params


def test_to_params_指定期望只带id():
    """验指定期望只带 id 主参数。"""
    params = JobExpectFilter(kind="expect", encrypt_expect_id="E1").to_params()
    assert params == {"page": "1", "pageSize": "15", "encryptExpectId": "E1"}


def test_to_params_临时期望发expectInfo不发id():
    """验临时期望发 expectInfo 而不发 id。"""
    info = '{"expectType":"1","position":"python","location":"101280100","lowSalary":"10","highSalary":"20"}'
    params = JobExpectFilter(kind="temp_expect", expect_info=info).to_params()
    assert params["expectInfo"] == info
    assert "encryptExpectId" not in params
    assert "mixExpectType" not in params


def test_to_params_未知kind报错():
    """验未知 kind 抛 `ValueError`。"""
    with pytest.raises(ValueError, match="未知期望类型"):
        JobExpectFilter(kind="nope").to_params()


def test_四种kind都是合法取值():
    """验合法 kind 集合恰为四种。"""
    assert set(EXPECT_KINDS) == {"recommend", "part_time", "expect", "temp_expect"}


def test_to_params_共享筛选照常进参数():
    """验共享筛选维度照常进参数。"""
    params = JobExpectFilter(
        kind="expect",
        encrypt_expect_id="E1",
        city="101280100",
        salary="405",
        experience=("104", "105"),
        degree=("209",),
        industry=("100021",),
        scale=("301",),
    ).to_params()
    assert params["city"] == "101280100"
    assert params["salary"] == "405"
    assert params["experience"] == "104,105"
    assert params["degree"] == "209"
    assert params["industry"] == "100021"
    assert params["scale"] == "301"


def test_to_params_空筛选不进参数():
    """验空筛选维度不写入参数。"""
    params = JobExpectFilter(kind="expect", encrypt_expect_id="E1").to_params()
    for key in ("city", "salary", "experience", "degree", "industry", "scale", "payType", "partTime"):
        assert key not in params


def test_to_params_兼职态不传salary只传payType_partTime():
    """验兼职态不传 salary，只传 payType/partTime。"""
    params = JobExpectFilter(
        kind="part_time",
        encrypt_expect_id="PT1",
        salary="405",
        pay_type=("2",),
        part_time=("3", "4"),
    ).to_params()
    assert "salary" not in params
    assert params["payType"] == "2"
    assert params["partTime"] == "3,4"


def test_to_params_非兼职态不传payType_partTime():
    """验非兼职态不写入 payType/partTime。"""
    params = JobExpectFilter(
        kind="expect",
        encrypt_expect_id="E1",
        pay_type=("2",),
        part_time=("3",),
    ).to_params()
    assert "payType" not in params
    assert "partTime" not in params


def test_to_params_永不出现Y类键():
    """验 Y 类键永不进参数。"""
    params = JobExpectFilter(
        kind="recommend",
        encrypt_expect_id="MIX1",
        mix_expect_type="1",
        city="101280100",
    ).to_params()
    for key in NEVER_PARAMS:
        assert key not in params
    assert "query" not in params
    assert "scene" not in params


def test_to_params_防御性兜底会剔掉误塞的Y类键():
    """验 `NEVER_PARAMS` 覆盖 query 等误塞键。"""
    f = JobExpectFilter(kind="expect", encrypt_expect_id="E1")
    params = f.to_params()
    params["query"] = "python"
    for key in NEVER_PARAMS:
        params.pop(key, None)
    assert "query" in NEVER_PARAMS


def test_for_page_只换页码():
    """验 `for_page` 只换页码且不改动原实例。"""
    f = JobExpectFilter(kind="expect", encrypt_expect_id="E1", city="101280100", page=1)
    f2 = f.for_page(4)
    assert f2.page == 4
    assert f2.encrypt_expect_id == "E1"
    assert f2.city == "101280100"
    assert f.page == 1


def test_页码从一开始():
    """验页码从 1 起，0 抛 `ValueError`。"""
    with pytest.raises(ValueError, match="页码"):
        JobExpectFilter(kind="expect", encrypt_expect_id="E1", page=0)


def test_每页条数至少一():
    """验每页条数至少为 1。"""
    with pytest.raises(ValueError, match="每页条数"):
        JobExpectFilter(kind="expect", encrypt_expect_id="E1", page_size=0)


def _expects():
    """构造一份三类期望齐全的 `ExpectList`。"""
    from boss_jobs.models import ExpectItem, ExpectList

    return ExpectList(
        mix=ExpectItem(
            kind="mix",
            encrypt_expect_id="MIX1",
            position_name="前端开发工程师",
            location_name="佛山",
            mix_expect_type="2",
            location_code="101280800",
        ),
        expects=(
            ExpectItem(kind="expect", encrypt_expect_id="E1", position_name="Python", location_name="广州"),
        ),
        part_time=(
            ExpectItem(kind="part_time", encrypt_expect_id="PT1", position_name="派单", location_name="佛山"),
        ),
    )


def test_from_expects_推荐取mix并带上混合类型和城市code():
    """验推荐态取 mix 并带混合类型与城市 code。"""
    f = JobExpectFilter.from_expects(_expects(), "recommend")
    assert f.kind == "recommend"
    assert f.encrypt_expect_id == "MIX1"
    assert f.mix_expect_type == "2"
    assert f.city == "101280800"


def test_from_expects_兼职取首条():
    """验兼职态取首条兼职期望。"""
    f = JobExpectFilter.from_expects(_expects(), "part_time")
    assert f.kind == "part_time"
    assert f.encrypt_expect_id == "PT1"
    assert f.mix_expect_type == ""


def test_from_expects_我的期望取首条():
    """验指定期望取首条。"""
    f = JobExpectFilter.from_expects(_expects(), "expect")
    assert f.kind == "expect"
    assert f.encrypt_expect_id == "E1"


def test_from_expects_没有可取的报错():
    """验无可取期望时抛 `ValueError`。"""
    from boss_jobs.models import ExpectList

    empty = ExpectList()
    for kind in ("recommend", "part_time", "expect"):
        with pytest.raises(ValueError, match="没有可取的"):
            JobExpectFilter.from_expects(empty, kind)


def test_from_expects_临时期望拒收():
    """验 `from_expects` 拒收临时期望。"""
    with pytest.raises(ValueError, match="from_temp_expect"):
        JobExpectFilter.from_expects(_expects(), "temp_expect")


def test_from_temp_expect_序列化成expectInfo():
    """验临时期望序列化成 expectInfo。"""
    f = JobExpectFilter.from_temp_expect(
        {"expectType": "1", "position": "python", "location": "101280100", "lowSalary": "10", "highSalary": "20"}
    )
    assert f.kind == "temp_expect"
    data = json.loads(f.expect_info)
    assert data == {
        "expectType": "1",
        "position": "python",
        "location": "101280100",
        "lowSalary": "10",
        "highSalary": "20",
    }
    params = f.to_params()
    assert params["expectInfo"] == f.expect_info
    assert "encryptExpectId" not in params


def test_from_temp_expect_已经是串就原样透传():
    """验已是 JSON 串时原样透传。"""
    f = JobExpectFilter.from_temp_expect('{"expectType":"1"}')
    assert f.expect_info == '{"expectType":"1"}'


def test_from_search_只搬筛选不搬query():
    """验从搜索条件只搬筛选维度，不搬 query 等键。"""
    base = JobSearchFilter(
        query="python",
        city="101280100",
        job_type="1901",
        salary="405",
        experience=("104",),
        degree=("209",),
        industry=("100021",),
        scale=("301",),
        stage=("1",),
        scene="1",
    )
    f = JobExpectFilter.from_search(base, kind="expect", encrypt_expect_id="E1")
    assert f.city == "101280100"
    assert f.salary == "405"
    assert f.experience == ("104",)
    params = f.to_params()
    assert "query" not in params
    assert "scene" not in params
    assert "stage" not in params
    assert "jobType" not in params
    assert params["encryptExpectId"] == "E1"
    assert params["salary"] == "405"


def test_from_search_透传期望主参数():
    """验 `from_search` 透传期望主参数。"""
    base = JobSearchFilter(city="101280100")
    f = JobExpectFilter.from_search(
        base,
        kind="temp_expect",
        expect_info='{"expectType":"1"}',
    )
    assert f.kind == "temp_expect"
    assert f.expect_info == '{"expectType":"1"}'
    assert f.to_params()["expectInfo"] == '{"expectType":"1"}'
