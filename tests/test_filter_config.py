"""搜索条件的读写：状态库 ``doc('search_filter')`` + 用户点名的 JSON 文件。

规矩就一条——**库里没有就留空**（= 全部「不限」），不报错。有就按那行装配，
缺的键、空值都算「不限」。``page``/``scene`` 是运行期的，不进配置。

用户点名的 ``--filter my.json`` 走 :func:`search_filter_from_file`，只读不写库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import boss_db
from boss_filter.search import (
    JobSearchFilter,
    filter_path,
    load_search_filter,
    save_search_filter,
    search_filter_from_dict,
    search_filter_from_file,
)




def test_filter_path_默认是_data_boss_db(tmp_path, monkeypatch):
    monkeypatch.delenv(boss_db.DB_ENV, raising=False)
    assert filter_path() == boss_db.DEFAULT_DB_PATH
    assert filter_path().name == "boss.db"
    assert filter_path().parent.name == "data"


def test_filter_path_认环境变量(tmp_path, monkeypatch):
    monkeypatch.setenv(boss_db.DB_ENV, str(tmp_path / "x.db"))
    assert filter_path() == tmp_path / "x.db"


def test_filter_path_显式参数优先(tmp_path, monkeypatch):
    monkeypatch.setenv(boss_db.DB_ENV, "from-env.db")
    assert filter_path(tmp_path / "explicit.db") == tmp_path / "explicit.db"




def test_load_库里没有_留空不报错(tmp_path):
    f = load_search_filter(tmp_path / "空库.db")
    assert f == JobSearchFilter()
    assert f.is_blank
    assert f.to_params() == {"page": "1", "pageSize": "15", "scene": "1"}


def test_load_空payload_也留空(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, "", p)
    assert load_search_filter(p).is_blank


def test_load_空对象_全不限(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, {}, p)
    f = load_search_filter(p)
    assert f.is_blank
    assert f.to_params() == {"page": "1", "pageSize": "15", "scene": "1"}


def test_load_键都在但值都空_等于不限(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(
        boss_db.DOC_SEARCH_FILTER,
        {
            "query": "",
            "city": "",
            "jobType": "",
            "salary": "",
            "experience": [],
            "degree": "",
            "industry": [],
            "scale": [],
        },
        p,
    )
    assert load_search_filter(p).is_blank




def test_load_单选与多选都收(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(
        boss_db.DOC_SEARCH_FILTER,
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
        p,
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
    p = tmp_path / "boss.db"
    boss_db.doc_set(
        boss_db.DOC_SEARCH_FILTER, {"experience": "104,105", "degree": "209"}, p
    )
    f = load_search_filter(p)
    assert f.experience == ("104", "105")
    assert f.degree == ("209",)


def test_load_snake_case_也认(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, {"job_type": "1902", "pay_type": ["1"]}, p)
    f = load_search_filter(p)
    assert f.job_type == "1902"
    assert f.pay_type == ("1",)


def test_load_多余键不炸(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, {"query": "go", "乱写的键": 1, "page": 3}, p)
    f = load_search_filter(p)
    assert f.query == "go"
    assert f.page == 1


def test_load_pageSize_不是数字就报清楚(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, {"pageSize": "十"}, p)
    with pytest.raises(ValueError, match="pageSize"):
        load_search_filter(p)


def test_load_不是JSON就报清楚(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, "{不是 json", p)
    with pytest.raises(ValueError, match="不是合法 JSON"):
        load_search_filter(p)


def test_load_不是对象就报清楚(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, "[1, 2]", p)
    with pytest.raises(ValueError, match="JSON 对象"):
        load_search_filter(p)




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
    p = save_search_filter(src, tmp_path / "boss.db")
    assert boss_db.doc_get(boss_db.DOC_SEARCH_FILTER, p)["jobType"] == "1901"
    back = load_search_filter(p)
    assert back == src


def test_save_空条件写出来是空值(tmp_path):
    p = save_search_filter(JobSearchFilter(), tmp_path / "empty.db")
    data = boss_db.doc_get(boss_db.DOC_SEARCH_FILTER, p)
    assert data["query"] == "" and data["city"] == ""
    assert data["experience"] == [] and data["scale"] == []
    assert load_search_filter(p).is_blank




def test_from_file_读得到(tmp_path):
    p = tmp_path / "my.json"
    p.write_text(
        json.dumps({"query": "python", "city": "101280100", "salary": "405"}),
        encoding="utf-8",
    )
    f = search_filter_from_file(p)
    assert f.query == "python"
    assert f.city == "101280100"
    assert f.salary == "405"


def test_from_file_不写库(tmp_path):
    p = tmp_path / "my.json"
    p.write_text('{"query": "go"}', encoding="utf-8")
    search_filter_from_file(p)
    db = tmp_path / "boss.db"
    assert boss_db.doc_get(boss_db.DOC_SEARCH_FILTER, db) is None


def test_from_file_不存在就报清楚(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        search_filter_from_file(tmp_path / "没有这个文件.json")


def test_from_file_不是JSON就报清楚(tmp_path):
    p = tmp_path / "my.json"
    p.write_text("{不是 json", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        search_filter_from_file(p)


def test_from_file_不是对象就报清楚(tmp_path):
    p = tmp_path / "my.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 对象"):
        search_filter_from_file(p)


def test_from_file_空文件_留空(tmp_path):
    p = tmp_path / "my.json"
    p.write_text("   \n", encoding="utf-8")
    assert search_filter_from_file(p).is_blank




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
