"""兜底表与已保存 HTML 的解析。

运行：`python -m pytest tests/test_filter_fallback.py -q`

HTML 用例两套：内联小片段（离线可跑）+ 项目里那份真实存盘页（在就顺手校对）。

Attributes:
    MINI_HTML: 按真实职位页筛选条结构裁下的最小片段（ka 标记与线上一致）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boss_filter import tables
from boss_filter.errors import FilterDataError
from boss_filter.fallback import (
    build_fallback_conditions,
    conditions_from_html,
    load_saved_html,
    parse_html_conditions,
    parse_html_industries,
)

MINI_HTML = """
<div class="c-filter-condition filter-condition-inner">
  <div class="condition-filter-select"><div class="current-select">
    <span class="placeholder-text">求职类型</span></div>
    <div class="filter-select-dropdown"><ul>
      <li ka="sel-job-rec-jobType-0"> 不限<i class="ui-icon-check"></i></li>
      <li ka="sel-job-rec-jobType-1901"> 全职<i class="ui-icon-check"></i></li>
    </ul></div>
  </div>
  <div class="condition-filter-select"><div class="current-select">
    <span class="placeholder-text">薪资待遇</span></div>
    <div class="filter-select-dropdown"><ul>
      <li ka="sel-job-rec-salary-405"> 10-20K<i class="ui-icon-check"></i></li>
    </ul></div>
  </div>
  <div class="condition-filter-select"><div class="current-select">
    <span class="placeholder-text">工作经验</span></div>
    <div class="filter-select-dropdown"><ul>
      <li ka="sel-job-rec-exp-104"> 1-3年<i class="ui-icon-check"></i></li>
    </ul></div>
  </div>
  <div class="condition-filter-select"><div class="current-select">
    <span class="placeholder-text">学历要求</span></div>
    <div class="filter-select-dropdown"><ul>
      <li ka="sel-job-rec-degree-203"> 本科<i class="ui-icon-check"></i></li>
    </ul></div>
  </div>
  <div class="condition-industry-select" ka-prefix="job-rec-industry">
    <div class="current-select"><span class="placeholder-text">公司行业</span></div>
    <div class="filter-select-dropdown"><ul>
      <li class="clearfix"><span class="label">互联网/AI</span>
        <div class="select-list">
          <a href="javascript:;" ka="sel-industry-0">互联网<i class="ui-icon-check"></i></a>
          <a href="javascript:;" ka="sel-industry-8">人工智能<i class="ui-icon-check"></i></a>
        </div>
      </li>
      <li class="clearfix"><span class="label">金融</span>
        <div class="select-list">
          <a href="javascript:;" ka="sel-industry-132">银行<i class="ui-icon-check"></i></a>
        </div>
      </li>
    </ul></div>
  </div>
  <div class="condition-filter-select"><div class="current-select">
    <span class="placeholder-text">公司规模</span></div>
    <div class="filter-select-dropdown"><ul>
      <li ka="sel-job-rec-scale-303"> 100-499人<i class="ui-icon-check"></i></li>
    </ul></div>
  </div>
  <a href="javascript:;" ka="empty-filter" class="clear-search-btn">清空</a>
</div>
"""


def saved_html_path() -> Path:
    """项目里那份真实存盘职位页的路径。"""
    return Path(__file__).resolve().parent.parent / ".saved_web" / "求职_找工作_招聘信息-BOSS直聘.html"


class TestFallbackTables:
    """写死表兜底。"""

    def test_builds_all_seven_dimensions(self):
        """验兜底表齐七个维度，城市走热点不写死。"""
        cond = build_fallback_conditions()
        assert cond.source == "fallback"
        assert cond.job_types and cond.salaries and cond.experiences
        assert cond.degrees and cond.scales and cond.industries
        assert cond.cities == ()
        assert cond.hot_cities

    def test_industry_matches_tables(self):
        """验行业分组与选项数同写死表对齐。"""
        cond = build_fallback_conditions()
        assert len(cond.industries) == len(tables.INDUSTRY_GROUPS)
        assert len(cond.industry_options()) == 134

    def test_fallback_is_deterministic(self):
        """验兜底表两次构造结果一致。"""
        assert build_fallback_conditions().to_dict() == build_fallback_conditions().to_dict()


class TestParseMiniHtml:
    """内联 HTML 解析。"""

    def test_parses_all_six_visible_dimensions(self):
        """验小片段解析出六个可见维度的选项码与行业分组。"""
        cond = parse_html_conditions(MINI_HTML)
        assert cond.source == "html"
        assert [o.code for o in cond.job_types] == ["0", "1901"]
        assert [o.code for o in cond.salaries] == ["405"]
        assert [o.code for o in cond.experiences] == ["104"]
        assert [o.code for o in cond.degrees] == ["203"]
        assert [o.code for o in cond.scales] == ["303"]
        assert [g.name for g in cond.industries] == ["互联网/AI", "金融"]

    def test_industry_options_keep_group_structure(self):
        """验行业选项保留分组结构。"""
        groups = parse_html_industries(MINI_HTML)
        assert [o.name for o in groups[0].options] == ["互联网", "人工智能"]
        assert [o.code for o in groups[1].options] == ["132"]

    def test_names_are_whitespace_cleaned(self):
        """验选项名去掉了首尾空白。"""
        cond = parse_html_conditions(MINI_HTML)
        assert cond.job_types[1].name == "全职"

    def test_city_is_empty_and_hot_cities_come_from_tables(self):
        """验城市树为空、热点城市取自写死表。"""
        cond = parse_html_conditions(MINI_HTML)
        assert cond.cities == ()
        assert [o.code for o in cond.hot_cities] == [c for c, _ in tables.HOT_CITIES]

    @pytest.mark.parametrize("field", ["job_types", "salaries", "experiences", "degrees", "scales"])
    def test_missing_dimension_raises(self, field):
        """验任一必备维度缺失都抛 `FilterDataError` 并点名维度。"""
        marker = {
            "job_types": "sel-job-rec-jobType-",
            "salaries": "sel-job-rec-salary-",
            "experiences": "sel-job-rec-exp-",
            "degrees": "sel-job-rec-degree-",
            "scales": "sel-job-rec-scale-",
        }[field]
        with pytest.raises(FilterDataError, match=field):
            parse_html_conditions(MINI_HTML.replace(marker, "ka=\"gone-"))

    def test_empty_html_raises(self):
        """验空 HTML 抛 `FilterDataError`。"""
        with pytest.raises(FilterDataError):
            parse_html_conditions("")

    def test_industry_missing_returns_empty_tuple(self):
        """验没有行业块时回空元组。"""
        assert parse_html_industries("<html></html>") == ()


@pytest.mark.skipif(not saved_html_path().is_file(), reason="没有 .saved_web 存盘页")
class TestSavedHtml:
    """项目里那份真实存盘页。"""

    def test_load_default_path(self):
        """验默认路径能读到筛选条。"""
        html = load_saved_html()
        assert "sel-job-rec-jobType-" in html

    def test_live_page_options_match_hardcoded_tables(self):
        """解析出来的选项要与写死表一致——表过期时这条会先红。"""
        cond = conditions_from_html()
        assert [(o.code, o.name) for o in cond.job_types] == list(tables.JOB_TYPES)
        assert [(o.code, o.name) for o in cond.experiences] == list(tables.EXPERIENCES)
        assert [(o.code, o.name) for o in cond.degrees] == list(tables.DEGREES)
        assert [(o.code, o.name) for o in cond.scales] == list(tables.SCALES)
        assert [(o.code, o.name) for o in cond.salaries] == [
            (c, n) for c, n, _lo, _hi in tables.SALARIES
        ]

    def test_live_industry_matches_hardcoded_table(self):
        """验真实页行业分组与写死表逐项一致。"""
        cond = conditions_from_html()
        parsed = [(g.name, [(o.code, o.name) for o in g.options]) for g in cond.industries]
        expected = [(g, list(rows)) for g, rows in tables.INDUSTRY_GROUPS]
        assert parsed == expected

    def test_load_saved_html_rejects_missing_path(self, tmp_path: Path):
        """验存盘页路径不存在时抛 `FilterDataError`。"""
        with pytest.raises(FilterDataError):
            load_saved_html(tmp_path / "nope.html")
