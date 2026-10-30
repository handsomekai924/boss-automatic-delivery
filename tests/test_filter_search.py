"""JobSearchFilter 的装配与解析。

验三件事：
    1. `to_params()` 按 chunk `getFormData()` 的契约拼参数（单选/多选/空值）
    2. `for_page()` 换页不动条件
    3. `resolve()` 把 code 换回人读名（含分组的公司行业）
"""

from __future__ import annotations

import pytest

from boss_filter.models import (
    CityNode,
    FilterConditions,
    FilterOption,
    IndustryGroup,
    options_from_rows,
)
from boss_filter.search import JobSearchFilter


def make_conditions() -> FilterConditions:
    """构造一份七维都有代表值的筛选条件。"""
    return FilterConditions(
        cities=[
            CityNode(code="101280100", name="广州"),
            CityNode(code="101010100", name="北京"),
        ],
        job_types=options_from_rows([("1901", "全职"), ("1902", "兼职")]),
        salaries=options_from_rows([("405", "10-20K"), ("406", "20-50K")]),
        experiences=options_from_rows([("104", "3-5年"), ("105", "5-10年")]),
        degrees=options_from_rows([("209", "本科"), ("208", "大专")]),
        industries=(
            IndustryGroup(
                name="互联网",
                options=options_from_rows([("100021", "互联网/电商")]),
            ),
        ),
        scales=options_from_rows([("301", "100-499人"), ("302", "500-999人")]),
    )


def test_to_params_最小集只带分页和场景():
    """空条件只输出分页与场景。"""
    params = JobSearchFilter().to_params()
    assert params == {"page": "1", "pageSize": "15", "scene": "1"}


def test_to_params_七维全填():
    """七维全填时参数键值齐全。"""
    f = JobSearchFilter(
        query="python",
        city="101280100",
        job_type="1901",
        salary="405",
        experience=("104", "105"),
        degree=("209",),
        industry=("100021",),
        scale=("301",),
    )
    assert f.to_params() == {
        "page": "1",
        "pageSize": "15",
        "scene": "1",
        "query": "python",
        "city": "101280100",
        "jobType": "1901",
        "salary": "405",
        "experience": "104,105",
        "degree": "209",
        "industry": "100021",
        "scale": "301",
    }


def test_to_params_多选维度拼逗号单选原样():
    """多选拼逗号串，单选原样。"""
    params = JobSearchFilter(city="101280100", experience=("104", "105", "106")).to_params()
    assert params["city"] == "101280100"
    assert params["experience"] == "104,105,106"


def test_to_params_空值不进参数():
    """空值与空白不进参数。"""
    params = JobSearchFilter(city="", experience=("", None), query="   ").to_params()
    assert "city" not in params
    assert "experience" not in params
    assert "query" not in params


def test_to_params_关键词两侧空白被掐掉():
    """关键词两侧空白被掐掉。"""
    assert JobSearchFilter(query="  python  ").to_params()["query"] == "python"


def test_to_params_辅助维度_payType_partTime_stage():
    """辅助维度也进参数。"""
    params = JobSearchFilter(pay_type=("1", "2"), part_time=("801",), stage=("10012",)).to_params()
    assert params["payType"] == "1,2"
    assert params["partTime"] == "801"
    assert params["stage"] == "10012"


def test_to_params_页码页容量能改():
    """页码与页容量可覆盖。"""
    params = JobSearchFilter(page=3, page_size=30).to_params()
    assert params["page"] == "3"
    assert params["pageSize"] == "30"


def test_to_params_scene_固定为搜索场景():
    """`scene` 固定为搜索场景。"""
    assert JobSearchFilter().to_params()["scene"] == "1"


def test_to_params_期望上下文非空才带encryptExpectId():
    """期望上下文非空才带 `encryptExpectId`。"""
    assert "encryptExpectId" not in JobSearchFilter().to_params()
    params = JobSearchFilter(query="python", encrypt_expect_id="E1").to_params()
    assert params["encryptExpectId"] == "E1"


def test_to_params_期望上下文空白被掐掉():
    """空白期望上下文不进参数。"""
    params = JobSearchFilter(encrypt_expect_id="  ").to_params()
    assert "encryptExpectId" not in params


def test_is_blank_不算期望上下文():
    """只带期望、没关键词 = 不是搜索流（走期望流），is_blank 不能把它当「有筛选」。"""
    assert JobSearchFilter(encrypt_expect_id="E1").is_blank
    assert not JobSearchFilter(query="python", encrypt_expect_id="E1").is_blank


def test_for_page_带上期望上下文():
    """换页保留期望上下文。"""
    f = JobSearchFilter(query="python", encrypt_expect_id="E1", page=1)
    f2 = f.for_page(3)
    assert f2.encrypt_expect_id == "E1"
    assert f2.to_params()["encryptExpectId"] == "E1"
    assert f2.to_params()["page"] == "3"


def test_页码从一开始():
    """页码从 1 开始，0 报错。"""
    with pytest.raises(ValueError, match="从 1 开始"):
        JobSearchFilter(page=0)


def test_每页条数至少一():
    """每页条数至少 1。"""
    with pytest.raises(ValueError, match="至少 1"):
        JobSearchFilter(page_size=0)


def test_for_page_只换页码():
    """换页只改页码，条件不动。"""
    f = JobSearchFilter(query="python", city="101280100", experience=("104",), page=1)
    f2 = f.for_page(4)
    assert f2.page == 4
    assert f2.query == "python"
    assert f2.city == "101280100"
    assert f2.experience == ("104",)
    assert f2.to_params()["page"] == "4"


def test_for_page_不改原对象():
    """`for_page` 不改原对象。"""
    f = JobSearchFilter(query="python", page=1)
    f.for_page(2)
    assert f.page == 1


def test_from_codes_把可迭代的收成元组():
    """`from_codes` 把可迭代收成元组。"""
    f = JobSearchFilter.from_codes(
        query="python",
        city="101280100",
        experience=["104", "105"],
        degree=["209"],
        page=2,
    )
    assert f.experience == ("104", "105")
    assert f.degree == ("209",)
    assert f.page == 2


def test_resolve_七维都有人读名():
    """`resolve` 把七维 code 换成人读名。"""
    cond = make_conditions()
    f = JobSearchFilter(
        query="python",
        city="101280100",
        job_type="1901",
        salary="405",
        experience=("104", "105"),
        degree=("209",),
        industry=("100021",),
        scale=("301",),
    )
    r = f.resolve(cond)
    assert r.city_name == "广州"
    assert r.job_type_name == "全职"
    assert r.salary_name == "10-20K"
    assert r.experience_names == ("3-5年", "5-10年")
    assert r.degree_names == ("本科",)
    assert r.scale_names == ("100-499人",)


def test_resolve_行业走分组表():
    """行业名从分组表取。"""
    cond = make_conditions()
    r = JobSearchFilter(industry=("100021",)).resolve(cond)
    assert r.industry_names == ("互联网/电商",)


def test_resolve_认不出的码不报错():
    """认不出的 code 给空名，不报错。"""
    cond = make_conditions()
    r = JobSearchFilter(city="999999999", salary="999").resolve(cond)
    assert r.city_name == ""
    assert r.salary_name == ""


def test_resolve_summary_lines_含关键词和分页():
    """汇总含关键词、城市与分页。"""
    cond = make_conditions()
    r = JobSearchFilter(query="python", city="101280100", page=2, page_size=20).resolve(cond)
    lines = "\n".join(r.summary_lines())
    assert "python" in lines
    assert "广州" in lines
    assert "第 2 页" in lines
    assert "20 条" in lines


def test_resolve_to_dict_结构齐整():
    """`to_dict` 输出 code/name 结构。"""
    cond = make_conditions()
    data = JobSearchFilter(
        query="python", city="101280100", salary="405", experience=("104",), degree=("209",)
    ).resolve(cond).to_dict()
    assert data["city"] == {"code": "101280100", "name": "广州"}
    assert data["salary"] == {"code": "405", "name": "10-20K"}
    assert data["experience"] == [{"code": "104", "name": "3-5年"}]
    assert data["degree"] == [{"code": "209", "name": "本科"}]
    assert data["page"] == 1


def test_save_load_期望上下文往返(tmp_path):
    """存取往返保留期望上下文。"""
    from boss_filter.search import load_search_filter, save_search_filter

    f = JobSearchFilter(query="python", city="101280100", encrypt_expect_id="E1")
    save_search_filter(f, tmp_path / "boss.db")
    back = load_search_filter(tmp_path / "boss.db")
    assert back.encrypt_expect_id == "E1"
    assert back.query == "python"
    assert back.to_params()["encryptExpectId"] == "E1"


def test_load_旧库没有期望键就留空(tmp_path):
    """向后兼容：旧库没 encryptExpectId 这把 key → 空串，行为不变。"""
    import boss_db
    from boss_filter.search import load_search_filter

    boss_db.doc_set(
        boss_db.DOC_SEARCH_FILTER,
        {"query": "go", "city": "101010100"},
        tmp_path / "boss.db", site="boss")
    back = load_search_filter(tmp_path / "boss.db")
    assert back.encrypt_expect_id == ""
    assert "encryptExpectId" not in back.to_params()
