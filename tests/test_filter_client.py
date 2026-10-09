"""筛选条件客户端：假会话单元测试 + 假服务端集成测试 + CLI。

运行：`python -m pytest tests/test_filter_client.py -q`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

import boss_filter.cli as cli
from boss_filter import FilterClient, get_filter_conditions
from boss_filter.client import load_industries, parse_conditions_payload
from boss_filter.errors import (
    FilterApiError,
    FilterDataError,
    FilterTransportError,
)
from tools.mock_server import MOCK_CONDITIONS, MOCK_HOT_CITIES, FILTER_ROUTES


# --------------------------------------------------------------------------- #
# 假会话
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload=None, *, status: int = 200, text: str | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """按序回脚本化响应；异常实例直接抛。裸 dict 自动包成 FakeResponse。"""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"没有预置响应可给，却收到了第 {len(self.calls)} 次请求：{url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    @property
    def last_url(self) -> str:
        return self.calls[-1]["url"]


def only_primary_endpoints() -> dict[str, str]:
    """关掉条件接口的降级链，让每个用例只打一次请求（或失败一次）。

    ``fetch_conditions`` 会按 conditions → recommend → search 逐个降级，
    单点故障用例不希望它一路试到底。
    """
    return {"recommend_conditions": "", "search_condition": ""}


def ok_conditions() -> dict:
    return {"code": 0, "message": "Success", "zpData": dict(MOCK_CONDITIONS)}


def ok_cities() -> dict:
    return {
        "code": 0,
        "message": "Success",
        "zpData": {
            "hotCityList": MOCK_HOT_CITIES,
            "cityList": [
                {
                    "code": 101280000,
                    "name": "广东",
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
            ],
        },
    }


def make_client(responses, **kwargs) -> FilterClient:
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("retries", 0)
    kwargs.setdefault("endpoints", only_primary_endpoints())
    return FilterClient(http=FakeHttp(responses), **kwargs)


def json_from_output(text: str) -> dict:
    """从 stdout 里抠出 JSON。

    假服务端的访问日志也打到同一个 stdout（同进程线程），所以不能直接
    ``json.loads(整段输出)``——找到第一个 ``{`` 再解析。
    """
    start = text.find("{")
    assert start >= 0, f"输出里没有 JSON：{text[:200]!r}"
    return json.loads(text[start:])


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #


class TestParseConditionsPayload:
    def test_maps_api_fields_to_model_fields(self):
        out = parse_conditions_payload(ok_conditions())
        assert [o.code for o in out["job_types"]] == ["0", "1901", "1903"]
        assert [o.code for o in out["salaries"]] == ["0", "402", "403", "404", "405", "406", "407"]
        assert out["salaries"][4].low_salary == 10
        assert [o.code for o in out["experiences"]][-1] == "107"
        assert [o.code for o in out["degrees"]][-1] == "205"
        assert [o.code for o in out["scales"]][-1] == "306"
        assert out["pay_types"] and out["stages"] and out["part_times"]

    def test_missing_optional_lists_become_empty(self):
        out = parse_conditions_payload({"code": 0, "zpData": {"jobTypeList": [{"code": 0, "name": "不限"}]}})
        assert out["stages"] == ()
        assert out["job_types"][0].code == "0"

    def test_empty_zpdata_raises(self):
        with pytest.raises(FilterDataError):
            parse_conditions_payload({"code": 0, "zpData": {}})

    def test_no_known_lists_raises(self):
        with pytest.raises(FilterDataError):
            parse_conditions_payload({"code": 0, "zpData": {"mystery": []}})


# --------------------------------------------------------------------------- #
# 客户端（假会话）
# --------------------------------------------------------------------------- #


class TestClientTransport:
    def test_uses_get_with_endpoint_path(self):
        http = FakeHttp([ok_conditions(), ok_cities()])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        client.get_filter_conditions()
        assert all(c["method"] == "GET" for c in http.calls)
        assert http.calls[0]["url"].endswith("/wapi/zpgeek/pc/all/filter/conditions.json")
        assert http.calls[1]["url"].endswith("/wapi/zpCommon/data/city.json")

    def test_retries_on_5xx_then_succeeds(self):
        http = FakeHttp([FakeResponse({}, status=500), ok_conditions(), ok_cities()])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=1, endpoints=only_primary_endpoints()
        )
        cond = client.get_filter_conditions()
        assert cond.source == "api"

    def test_transport_error_when_all_attempts_fail(self):
        http = FakeHttp([requests.ConnectionError("boom")])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        with pytest.raises(FilterTransportError):
            client.fetch_conditions()

    def test_non_json_response_is_transport_error(self):
        http = FakeHttp([FakeResponse(None, text="<html>oops</html>")])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        with pytest.raises(FilterTransportError):
            client.fetch_conditions()

    def test_business_error_raises_api_error(self):
        http = FakeHttp([{"code": 7, "message": "当前登录状态已失效", "zpData": {}}])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        with pytest.raises(FilterApiError) as exc_info:
            client.fetch_conditions()
        assert exc_info.value.code == 7
        assert exc_info.value.is_session_expired

    def test_conditions_endpoint_falls_back_to_recommend(self):
        """主力路由 500 时降到 recommend 那份，而不是整次失败。"""
        http = FakeHttp([FakeResponse({}, status=500), ok_conditions(), ok_cities()])
        client = FilterClient(http=http, sleeper=lambda _s: None, retries=0)
        cond = client.get_filter_conditions()
        assert "recommend/conditions" in http.calls[1]["url"]
        assert cond.job_types

    def test_city_payload_builds_tree(self):
        http = FakeHttp([ok_cities()])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        cities = client.fetch_cities()
        guangzhou = cities["cities"][0].children[0]
        assert guangzhou.name == "广州"
        assert guangzhou.pinyin == "guangzhou"
        assert [c.name for c in guangzhou.children] == ["天河区"]
        assert [o.code for o in cities["hot_cities"]][0] == "100010000"

    def test_city_payload_without_lists_raises(self):
        http = FakeHttp([{"code": 0, "zpData": {}}])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        with pytest.raises(FilterDataError):
            client.fetch_cities()

    def test_fetch_hot_cities_only(self):
        http = FakeHttp([{"code": 0, "zpData": {"hotCityList": MOCK_HOT_CITIES}}])
        client = FilterClient(
            http=http, sleeper=lambda _s: None, retries=0, endpoints=only_primary_endpoints()
        )
        assert len(client.fetch_hot_cities()) == len(MOCK_HOT_CITIES)


class TestClientAssembly:
    def test_industry_always_comes_from_tables(self):
        """行业没有接口，任何情况下都走写死表。"""
        industries = load_industries()
        assert len(industries) == 15
        assert sum(len(g.options) for g in industries) == 145

    def test_get_filter_conditions_merges_all_sources(self):
        client = make_client([ok_conditions(), ok_cities()])
        cond = client.get_filter_conditions()
        assert cond.source == "api"
        assert cond.degraded == {}
        assert cond.cities and cond.job_types and cond.salaries
        assert cond.industries and cond.scales and cond.degrees and cond.experiences

    def test_api_failure_degrades_to_fallback_table(self):
        client = make_client([requests.ConnectionError("down")] * 6)
        cond = client.get_filter_conditions(use_fallback=True)
        assert cond.source == "fallback"
        assert set(cond.degraded) == {"conditions", "cities"}
        assert cond.job_types and cond.industries
        assert cond.cities == ()  # 写死表没有城市树
        assert cond.hot_cities

    def test_api_failure_without_fallback_raises(self):
        client = make_client([requests.ConnectionError("down")])
        with pytest.raises(FilterTransportError):
            client.get_filter_conditions(use_fallback=False)

    def test_partial_failure_keeps_api_data_and_marks_degraded(self):
        client = make_client([ok_conditions(), requests.ConnectionError("city down")])
        cond = client.get_filter_conditions(use_fallback=True)
        assert cond.degraded == {"cities": "fallback"}
        assert cond.source == "fallback"  # 有降级就如实标注
        assert cond.job_types[1].code == "1901"  # 条件那份仍是接口给的
        assert cond.cities == ()

    def test_get_filter_conditions_helper_with_html_path(self, tmp_path: Path):
        html = tmp_path / "page.html"
        html.write_text(
            '<li ka="sel-job-rec-jobType-1901"> 全职<i></i></li>'
            '<li ka="sel-job-rec-salary-405"> 10-20K<i></i></li>'
            '<li ka="sel-job-rec-exp-104"> 1-3年<i></i></li>'
            '<li ka="sel-job-rec-degree-203"> 本科<i></i></li>'
            '<li ka="sel-job-rec-scale-303"> 100-499人<i></i></li>'
            '<div class="condition-industry-select"><span class="label">X</span>'
            '<div class="select-list"><a ka="sel-industry-0">互联网<i></i></a></div></div>'
            '<a ka="empty-filter">清空</a>',
            encoding="utf-8",
        )
        cond = get_filter_conditions(html_path=html)
        assert cond.source == "html"
        assert cond.job_types[0].name == "全职"

    def test_default_helper_uses_real_endpoints(self, monkeypatch):
        """get_filter_conditions() 不带参时用线上默认路由（这里只验拼出来的 URL）。"""
        seen: list[str] = []

        class SpyHttp:
            headers: dict = {}

            def request(self, method, url, **kwargs):
                seen.append(url)
                if "conditions" in url:
                    return FakeResponse(ok_conditions())
                return FakeResponse(ok_cities())

        client = FilterClient(http=SpyHttp(), sleeper=lambda _s: None, retries=0)
        client.get_filter_conditions()
        assert seen[0].startswith("https://www.zhipin.com/wapi/zpgeek/pc/all/filter/conditions.json")


# --------------------------------------------------------------------------- #
# 集成（假服务端）
# --------------------------------------------------------------------------- #


class TestAgainstMockServer:
    def test_fetch_conditions_and_cities(self, server: str):
        client = FilterClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None)
        cond = client.get_filter_conditions()
        assert cond.source == "api"
        assert [o.name for o in cond.job_types] == ["不限", "全职", "兼职"]
        assert cond.find_city("101280100").name == "广州"
        assert cond.find_city("440106").name == "天河区"
        assert [o.name for o in cond.hot_cities][:2] == ["全国", "北京"]

    def test_conditions_codes_match_live_contract(self, server: str):
        client = FilterClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None)
        parts = client.fetch_conditions()
        assert [o.code for o in parts["job_types"]] == ["0", "1901", "1903"]
        assert parts["salaries"][4].low_salary == 10

    def test_mock_server_exposes_all_filter_routes(self, server: str):
        for path in FILTER_ROUTES:
            resp = requests.get(server + path, timeout=5.0)
            assert resp.status_code == 200, path
            assert resp.json()["code"] == 0, path

    def test_api_down_falls_back(self, server: str):
        import tools.mock_server as mock_server

        mock_server.FILTER_API_DOWN = True
        try:
            client = FilterClient(base_url=server, retries=0, timeout=5.0, sleeper=lambda _s: None)
            cond = client.get_filter_conditions(use_fallback=True)
        finally:
            mock_server.FILTER_API_DOWN = False
        assert cond.source == "fallback"
        assert cond.job_types and cond.industries


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def run(argv: list[str]) -> int:
    return cli.main(argv)


class TestCli:
    def test_show_fallback_prints_seven_dimensions(self, capsys):
        assert run(["show", "--fallback"]) == 0
        out = capsys.readouterr().out
        for label in ("求职类型", "薪资待遇", "工作经验", "学历要求", "公司行业", "公司规模"):
            assert label in out

    def test_show_json_is_exportable(self, capsys):
        assert run(["show", "--fallback", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["source"] == "fallback"
        assert data["jobType"][1]["code"] == "1901"
        assert data["salary"][4]["lowSalary"] == 10

    def test_show_detail_lists_option_codes(self, capsys):
        assert run(["show", "--fallback", "--detail"]) == 0
        out = capsys.readouterr().out
        assert "1901" in out and "全职" in out
        assert "10-20K" in out

    def test_export_writes_file(self, tmp_path: Path, capsys):
        target = tmp_path / "out" / "filters.json"
        assert run(["export", "--fallback", "--out", str(target)]) == 0
        assert target.is_file()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["source"] == "fallback"
        assert len(data["industry"]) == 15

    def test_export_to_stdout(self, capsys):
        assert run(["export", "--fallback"]) == 0
        assert json.loads(capsys.readouterr().out)["source"] == "fallback"

    def test_show_against_mock_server(self, server: str, capsys):
        assert run(["--base-url", server, "show", "--json"]) == 0
        data = json_from_output(capsys.readouterr().out)
        assert data["source"] == "api"
        # 城市树按假服务端下发的顺序：北京在前，广东在后
        assert [c["name"] for c in data["city"]][:2] == ["北京", "广东"]

    def test_endpoint_override(self, server: str, capsys):
        assert run(
            [
                "--base-url",
                server,
                "--endpoint-conditions",
                "/wapi/zpgeek/search/job/condition.json",
                "--endpoint-city",
                "/wapi/zpCommon/data/city.json",
                "show",
                "--json",
            ]
        ) == 0
        data = json_from_output(capsys.readouterr().out)
        assert data["jobType"]

    def test_all_subcommands_registered(self):
        parser = cli.build_parser()
        for name in ("show", "export"):
            assert parser.parse_args([name])
