"""筛选条件的模型与写死表校验。

运行：`python -m pytest tests/test_filter_models.py -q`
"""

from __future__ import annotations

import pytest

from boss_filter import tables
from boss_filter.models import (
    CityNode,
    FilterConditions,
    FilterOption,
    IndustryGroup,
    normalize_code,
    options_from_api,
    options_from_rows,
)


class TestNormalizeCode:
    """`normalize_code` 的取值规范。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (104, "104"),
            ("104", "104"),
            (104.0, "104"),
            (None, ""),
            (0, "0"),
        ],
    )
    def test_ints_and_strings_become_str(self, raw, expected):
        """整数、浮点、字符串都规范成字符串；空值为 `''`。"""
        assert normalize_code(raw) == expected


class TestFilterOption:
    """`FilterOption` 的构造与序列化。"""

    def test_from_api_reads_salary_bounds(self):
        """`from_api` 读取薪资上下限。"""
        opt = FilterOption.from_api({"code": 405, "name": "10-20K", "lowSalary": 10, "highSalary": 20})
        assert opt == FilterOption(code="405", name="10-20K", low_salary=10, high_salary=20)

    def test_from_api_without_bounds(self):
        """接口未给薪资时上下限为 `None`。"""
        opt = FilterOption.from_api({"code": 1901, "name": "全职"})
        assert opt.low_salary is None and opt.high_salary is None

    def test_from_api_tolerates_missing_name(self):
        """接口缺 `name` 时按空串处理。"""
        assert FilterOption.from_api({"code": 1}).name == ""

    def test_from_tuple_two_fields(self):
        """二元组只填 code 与名称。"""
        assert FilterOption.from_tuple(("0", "不限")) == FilterOption(code="0", name="不限")

    def test_from_tuple_salary_row(self):
        """四元组可带上薪资上下限。"""
        opt = FilterOption.from_tuple(("405", "10-20K", 10, 20))
        assert (opt.low_salary, opt.high_salary) == (10, 20)

    def test_to_dict_round_trip(self):
        """`to_dict` 与 `from_api` 往返一致，空薪资不落键。"""
        opt = FilterOption.from_api({"code": 405, "name": "10-20K", "lowSalary": 10, "highSalary": 20})
        assert FilterOption.from_api(opt.to_dict()) == opt
        assert "lowSalary" not in FilterOption(code="1", name="x").to_dict()

    def test_from_api_rejects_non_dict(self):
        """非字典输入抛 `TypeError`。"""
        with pytest.raises(TypeError):
            FilterOption.from_api(["405"])  # type: ignore[arg-type]


class TestCityNode:
    """`CityNode` 的树形装配与遍历。"""

    def test_from_api_flattens_sub_level_model_list(self):
        """`from_api` 递归展开 `subLevelModelList` 成多级子节点。"""
        node = CityNode.from_api(
            {
                "code": 101280000,
                "name": "广东",
                "pinyin": None,
                "firstChar": "g",
                "subLevelModelList": [
                    {
                        "code": 101280100,
                        "name": "广州",
                        "pinyin": "guangzhou",
                        "firstChar": "g",
                        "subLevelModelList": [{"code": 440106, "name": "天河区"}],
                    }
                ],
            }
        )
        assert node.code == "101280000"
        assert node.first_char == "g"
        assert node.pinyin == ""
        assert [c.name for c in node.children] == ["广州"]
        assert [c.name for c in node.children[0].children] == ["天河区"]

    def test_walk_is_depth_first(self):
        """`walk` 深度优先遍历。"""
        root = CityNode("1", "省", children=(CityNode("2", "市", children=(CityNode("3", "区"),)),))
        assert [n.name for n in root.walk()] == ["省", "市", "区"]

    def test_to_dict_omits_empty_children(self):
        """无子节点时 `to_dict` 不输出 `children` 键。"""
        assert CityNode("1", "x").to_dict() == {"code": "1", "name": "x"}


class TestFilterConditions:
    """`FilterConditions` 的查找与汇总。"""

    def make(self) -> FilterConditions:
        """构造一份七维都有值的筛选条件。"""
        return FilterConditions(
            cities=(CityNode("101280000", "广东", children=(CityNode("101280100", "广州"),)),),
            hot_cities=(FilterOption("101280100", "广州"),),
            job_types=(FilterOption("0", "不限"), FilterOption("1901", "全职")),
            salaries=(FilterOption("405", "10-20K", 10, 20),),
            experiences=(FilterOption("104", "1-3年"),),
            degrees=(FilterOption("203", "本科"),),
            scales=(FilterOption("303", "100-499人"),),
            industries=(IndustryGroup("互联网/AI", (FilterOption("0", "互联网"),)),),
            source="api",
        )

    def test_find_option_by_code(self):
        """按 kind 与 code 找到筛选项。"""
        cond = self.make()
        assert cond.find_option("salaries", 405) == FilterOption("405", "10-20K", 10, 20)
        assert cond.find_option("job_types", "1901").name == "全职"
        assert cond.find_option("job_types", "9999") is None

    def test_find_option_rejects_unknown_kind(self):
        """未知 kind 抛 `KeyError`。"""
        with pytest.raises(KeyError):
            self.make().find_option("nope", "1")

    def test_find_city_walks_the_tree(self):
        """`find_city` 可沿树找到省、市节点。"""
        cond = self.make()
        assert cond.find_city("101280100").name == "广州"
        assert cond.find_city("101280000").name == "广东"
        assert cond.find_city("404") is None

    def test_find_city_by_name(self):
        """按名称定位城市节点。"""
        assert self.make().find_city_by_name("广州").code == "101280100"

    def test_industry_options_flattens_groups(self):
        """行业选项展开分组后的扁平列表。"""
        assert [o.code for o in self.make().industry_options()] == ["0"]

    def test_to_dict_keys_match_todo_dimensions(self):
        """`to_dict` 覆盖七个筛选维度。"""
        data = self.make().to_dict()
        for key in ("city", "jobType", "salary", "experience", "degree", "industry", "scale"):
            assert key in data, f"todo.md 要求的维度 {key} 缺失"
        assert data["source"] == "api"

    def test_summary_lines_cover_seven_dimensions(self):
        """汇总文案覆盖七个维度的中文标签。"""
        text = "\n".join(self.make().summary_lines())
        for label in ("城市", "求职类型", "薪资待遇", "工作经验", "学历要求", "公司行业", "公司规模"):
            assert label in text


class TestTables:
    """写死表要与线上接口一致，这里锁关键 code 与条数。"""

    def test_job_types_match_live_codes(self):
        """求职类型 code 与线上一致。"""
        assert [c for c, _ in tables.JOB_TYPES] == ["0", "1901", "1903"]

    def test_salaries_match_live_codes_and_bounds(self):
        """薪资档 code 与上下限与线上一致。"""
        rows = {code: (low, high) for code, _n, low, high in tables.SALARIES}
        assert list(rows) == ["0", "402", "403", "404", "405", "406", "407"]
        assert rows["405"] == (10, 20)
        assert rows["407"] == (50, 0)

    def test_experiences_match_live_codes(self):
        """工作经验 code 与线上一致。"""
        assert [c for c, _ in tables.EXPERIENCES] == [
            "0", "108", "102", "101", "103", "104", "105", "106", "107",
        ]

    def test_degrees_match_live_codes(self):
        """学历 code 与线上一致。"""
        assert [c for c, _ in tables.DEGREES] == ["0", "209", "208", "206", "202", "203", "204", "205"]

    def test_scales_match_live_codes(self):
        """公司规模 code 与线上一致。"""
        assert [c for c, _ in tables.SCALES] == ["0", "301", "302", "303", "304", "305", "306"]

    def test_industry_codes_are_real_api_codes(self):
        """行业 code 是接口的 1000xx 码，不是 HTML 的 0-144。"""
        codes = {c for _g, rows in tables.INDUSTRY_GROUPS for c, _n in rows}
        assert all(c.startswith(("100", "101")) for c in codes), f"发现非 1000xx 行业码：{codes}"
        assert "100020" in codes
        assert "100206" in codes
        assert "0" not in codes and "144" not in codes

    def test_industry_groups_have_names(self):
        """行业分组表有固定组名与条数。"""
        assert [g for g, _rows in tables.INDUSTRY_GROUPS][0] == "互联网/AI"
        assert len(tables.INDUSTRY_GROUPS) == 15

    def test_hot_cities_include_all_nation(self):
        """热门城市首条为「全国」，含广州。"""
        assert tables.HOT_CITIES[0] == ("100010000", "全国")
        assert ("101280100", "广州") in tables.HOT_CITIES

    def test_every_row_has_code_and_name(self):
        """每行都带非空 code 与 name。"""
        for rows in (
            tables.JOB_TYPES,
            tables.EXPERIENCES,
            tables.DEGREES,
            tables.SCALES,
            tables.PAY_TYPES,
            tables.STAGES,
            tables.PART_TIMES,
            tables.HOT_CITIES,
        ):
            for row in rows:
                assert row[0] and row[1], row
        for code, name, _low, _high in tables.SALARIES:
            assert code and name

    def test_options_from_rows_builds_models(self):
        """`options_from_rows` 从写死行构造模型。"""
        opts = options_from_rows(tables.SALARIES)
        assert isinstance(opts[1], FilterOption)
        assert opts[1].low_salary == 0 and opts[1].high_salary == 3

    def test_options_from_api_ignores_junk(self):
        """`options_from_api` 忽略非法项。"""
        assert options_from_api(None) == ()
        assert options_from_api([1, "x", {"code": 1, "name": "a"}]) == (FilterOption("1", "a"),)
