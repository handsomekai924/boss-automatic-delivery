"""搜索条件配置文件：``search_filter.json`` 的读写。

规矩就一条——**没有文件就留空**（= 全部「不限」），不报错。有文件就按文件
装配，缺的键、空值都算「不限」。``page``/``scene`` 是运行期的，不进配置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_filter import DEFAULT_FILTER_PATH, FILTER_ENV
from boss_filter.search import (
    JobSearchFilter,
    filter_path,
    load_search_filter,
    save_search_filter,
    search_filter_from_dict,
)


# --------------------------------------------------------------------------- #
# 定位路径
# --------------------------------------------------------------------------- #


def test_filter_path_默认在项目根(tmp_path, monkeypatch):
    monkeypatch.delenv(FILTER_ENV, raising=False)
    assert filter_path() == DEFAULT_FILTER_PATH
    assert DEFAULT_FILTER_PATH.name == "search_filter.json"


def test_filter_path_认环境变量(monkeypatch):
    monkeypatch.setenv(FILTER_ENV, r"D:\cfg\f.json")
    assert filter_path() == Path(r"D:\cfg\f.json")


def test_filter_path_显式参数优先(monkeypatch):
    monkeypatch.setenv(FILTER_ENV, "from-env.json")
    assert filter_path("explicit.json") == Path("explicit.json")


# --------------------------------------------------------------------------- #
# 没有文件 = 留空
# --------------------------------------------------------------------------- #


def test_load_文件不存在_留空不报错(tmp_path):
    f = load_search_filter(tmp_path / "没有这个文件.json")
    assert f == JobSearchFilter()
    assert f.is_blank
    assert f.to_params() == {"page": "1", "pageSize": "15", "scene": "1"}


def test_load_空文件_也留空(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text("   \n", encoding="utf-8")
    assert load_search_filter(p).is_blank


def test_load_空对象_全不限(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text("{}", encoding="utf-8")
    f = load_search_filter(p)
    assert f.is_blank
    assert f.to_params() == {"page": "1", "pageSize": "15", "scene": "1"}


def test_load_键都在但值都空_等于不限(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text(
        json.dumps(
            {
                "query": "",
                "city": "",
                "jobType": "",
                "salary": "",
                "experience": [],
                "degree": "",
                "industry": [],
                "scale": [],
            }
        ),
        encoding="utf-8",
    )
    assert load_search_filter(p).is_blank


# --------------------------------------------------------------------------- #
# 有文件 = 按文件装配
# --------------------------------------------------------------------------- #


def test_load_单选与多选都收(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text(
        json.dumps(
            {
                "query": "python",
                "city": "101280100",
                "jobType": "1901",
                "salary": "405",
                "experience": ["104", "105"],
                "degree": ["209"],
                "industry": ["100021"],
                "scale": ["301"],
                "pageSize": 20,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    f = load_search_filter(p)
    assert not f.is_blank
    assert f.to_params() == {
        "page": "1",
        "pageSize": "20",
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


def test_load_多选兼容逗号串(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text(json.dumps({"experience": "104,105", "degree": "209"}), encoding="utf-8")
    f = load_search_filter(p)
    assert f.experience == ("104", "105")
    assert f.degree == ("209",)


def test_load_snake_case_也认(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text(json.dumps({"job_type": "1902", "pay_type": ["1"]}), encoding="utf-8")
    f = load_search_filter(p)
    assert f.job_type == "1902"
    assert f.pay_type == ("1",)


def test_load_多余键不炸(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text(json.dumps({"query": "go", "乱写的键": 1, "page": 3}), encoding="utf-8")
    f = load_search_filter(p)
    assert f.query == "go"
    assert f.page == 1          # 页码是运行期的，配置说了不算


def test_load_pageSize_不是数字就报清楚(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text(json.dumps({"pageSize": "十"}), encoding="utf-8")
    with pytest.raises(ValueError, match="pageSize"):
        load_search_filter(p)


def test_load_不是JSON就报清楚(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text("{不是 json", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        load_search_filter(p)


def test_load_不是对象就报清楚(tmp_path):
    p = tmp_path / "search_filter.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 对象"):
        load_search_filter(p)


# --------------------------------------------------------------------------- #
# 写回
# --------------------------------------------------------------------------- #


def test_save_再读回来是同一个条件(tmp_path):
    src = JobSearchFilter(
        query="python",
        city="101280100",
        job_type="1901",
        salary="405",
        experience=("104", "105"),
        degree=("209",),
        industry=("100021",),
        scale=("301",),
        page_size=20,
    )
    p = save_search_filter(src, tmp_path / "out.json")
    assert json.loads(p.read_text(encoding="utf-8"))["jobType"] == "1901"
    back = load_search_filter(p)
    assert back == src


def test_save_空条件写出来是空值(tmp_path):
    p = save_search_filter(JobSearchFilter(), tmp_path / "empty.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["query"] == "" and data["city"] == ""
    assert data["experience"] == [] and data["scale"] == []
    assert load_search_filter(p).is_blank


# --------------------------------------------------------------------------- #
# is_blank
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "f,blank",
    [
        (JobSearchFilter(), True),
        (JobSearchFilter(query="  "), True),
        (JobSearchFilter(page=3), True),
        (JobSearchFilter(city="101280100"), False),
        (JobSearchFilter(query="python"), False),
        (JobSearchFilter(experience=("104",)), False),
    ],
)
def test_is_blank(f, blank):
    assert f.is_blank is blank


def test_search_filter_from_dict_空输入():
    assert search_filter_from_dict({}).is_blank
    with pytest.raises(ValueError, match="JSON 对象"):
        search_filter_from_dict([1])
