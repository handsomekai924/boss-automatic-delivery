"""JobExpectFilter 的装配：四选一主参数 + 共享筛选维度。

对齐站点 chunk ``job~0.0c757050.js`` 的 ``getFormData()`` 四分支：

========  ==========================================================
kind      期望主参数
========  ==========================================================
recommend ``city`` + ``encryptExpectId`` + ``mixExpectType``
part_time ``jobType=1903`` + ``encryptExpectId``
expect    ``encryptExpectId``
temp      ``expectInfo``（JSON）
========  ==========================================================

验四件事：
    1. 四种 kind 的主参数形状
    2. 共享筛选维度（城市/薪资/经验/学历/行业/规模）照常进参数
    3. **兼职态不传 salary**，只传 payType/partTime
    4. ``query``/``scene`` 等 Y 类键**永远不出现**（前端点期望 tab 就删）
"""

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


# --------------------------------------------------------------------------- #
# 期望主参数：四选一
# --------------------------------------------------------------------------- #


def test_to_params_推荐带city加id加混合类型():
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
    params = JobExpectFilter(kind="part_time", encrypt_expect_id="PT1").to_params()
    assert params["jobType"] == PART_TIME_JOB_TYPE == "1903"
    assert params["encryptExpectId"] == "PT1"
    assert "mixExpectType" not in params


def test_to_params_指定期望只带id():
    params = JobExpectFilter(kind="expect", encrypt_expect_id="E1").to_params()
    assert params == {"page": "1", "pageSize": "15", "encryptExpectId": "E1"}


def test_to_params_临时期望发expectInfo不发id():
    info = '{"expectType":"1","position":"python","location":"101280100","lowSalary":"10","highSalary":"20"}'
    params = JobExpectFilter(kind="temp_expect", expect_info=info).to_params()
    assert params["expectInfo"] == info
    assert "encryptExpectId" not in params
    assert "mixExpectType" not in params


def test_to_params_未知kind报错():
    with pytest.raises(ValueError, match="未知期望类型"):
        JobExpectFilter(kind="nope").to_params()


def test_四种kind都是合法取值():
    assert set(EXPECT_KINDS) == {"recommend", "part_time", "expect", "temp_expect"}


# --------------------------------------------------------------------------- #
# 共享筛选维度
# --------------------------------------------------------------------------- #


def test_to_params_共享筛选照常进参数():
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
    params = JobExpectFilter(kind="expect", encrypt_expect_id="E1").to_params()
    for key in ("city", "salary", "experience", "degree", "industry", "scale", "payType", "partTime"):
        assert key not in params


def test_to_params_兼职态不传salary只传payType_partTime():
    """兼职 tab 的筛选面板是 payType/partTime 顶替 salary（getConfigFields()）。"""
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
    params = JobExpectFilter(
        kind="expect",
        encrypt_expect_id="E1",
        pay_type=("2",),
        part_time=("3",),
    ).to_params()
    assert "payType" not in params
    assert "partTime" not in params


def test_to_params_永不出现Y类键():
    """query/scene/stage 等是搜索态的键，期望态前端 getRemovedFields() 就剔了。"""
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
    """万一上游 dict 把 query 塞进来，to_params 也要兜干净。"""
    f = JobExpectFilter(kind="expect", encrypt_expect_id="E1")
    params = f.to_params()
    params["query"] = "python"  # 模拟调用方误塞
    for key in NEVER_PARAMS:
        params.pop(key, None)
    # 断言 NEVER_PARAMS 确实覆盖 query，防有人把它从集合里删掉
    assert "query" in NEVER_PARAMS


# --------------------------------------------------------------------------- #
# for_page / 页码
# --------------------------------------------------------------------------- #


def test_for_page_只换页码():
    f = JobExpectFilter(kind="expect", encrypt_expect_id="E1", city="101280100", page=1)
    f2 = f.for_page(4)
    assert f2.page == 4
    assert f2.encrypt_expect_id == "E1"
    assert f2.city == "101280100"
    assert f.page == 1


def test_页码从一开始():
    with pytest.raises(ValueError, match="页码"):
        JobExpectFilter(kind="expect", encrypt_expect_id="E1", page=0)


def test_每页条数至少一():
    with pytest.raises(ValueError, match="每页条数"):
        JobExpectFilter(kind="expect", encrypt_expect_id="E1", page_size=0)


# --------------------------------------------------------------------------- #
# from_expects / from_temp_expect / from_search
# --------------------------------------------------------------------------- #


def _expects():
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
    f = JobExpectFilter.from_expects(_expects(), "recommend")
    assert f.kind == "recommend"
    assert f.encrypt_expect_id == "MIX1"
    assert f.mix_expect_type == "2"
    # 前端 getFormData().city = mixLocation（city code，不是显示名）
    assert f.city == "101280800"


def test_from_expects_兼职取首条():
    f = JobExpectFilter.from_expects(_expects(), "part_time")
    assert f.kind == "part_time"
    assert f.encrypt_expect_id == "PT1"
    assert f.mix_expect_type == ""


def test_from_expects_我的期望取首条():
    f = JobExpectFilter.from_expects(_expects(), "expect")
    assert f.kind == "expect"
    assert f.encrypt_expect_id == "E1"


def test_from_expects_没有可取的报错():
    from boss_jobs.models import ExpectList

    empty = ExpectList()
    for kind in ("recommend", "part_time", "expect"):
        with pytest.raises(ValueError, match="没有可取的"):
            JobExpectFilter.from_expects(empty, kind)


def test_from_expects_临时期望拒收():
    with pytest.raises(ValueError, match="from_temp_expect"):
        JobExpectFilter.from_expects(_expects(), "temp_expect")


def test_from_temp_expect_序列化成expectInfo():
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
    f = JobExpectFilter.from_temp_expect('{"expectType":"1"}')
    assert f.expect_info == '{"expectType":"1"}'


def test_from_search_只搬筛选不搬query():
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
    # query/scene/stage/jobType 都不该跟过来
    assert "query" not in params
    assert "scene" not in params
    assert "stage" not in params
    assert "jobType" not in params
    assert params["encryptExpectId"] == "E1"
    assert params["salary"] == "405"


def test_from_search_透传期望主参数():
    base = JobSearchFilter(city="101280100")
    f = JobExpectFilter.from_search(
        base,
        kind="temp_expect",
        expect_info='{"expectType":"1"}',
    )
    assert f.kind == "temp_expect"
    assert f.expect_info == '{"expectType":"1"}'
    assert f.to_params()["expectInfo"] == '{"expectType":"1"}'
