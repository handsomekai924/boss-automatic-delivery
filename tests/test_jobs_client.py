"""职位客户端：抓页 / 翻页流水线 / 节流 / 错误分类。

重点验的是「**抓一页 → 立刻清洗 → 立刻入库 → 睡够间隔 → 下一页**」
这条顺序，以及翻页停止条件。网络层用假会话，不打真实站点。
"""

from __future__ import annotations

import json

import pytest

import boss_jobs.cli as cli
from boss_jobs.client import JobClient, create_client, http_from_session
from boss_jobs.errors import JobApiError, JobDataError, JobTransportError
from boss_jobs.models import clean_page
from boss_jobs.store import JobStore


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
        self.cookies = FakeCookieJar()

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"没有预置响应可给，却收到了第 {len(self.calls)} 次请求：{url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


class FakeCookieJar:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, name, value, **_kwargs):
        self._data[name] = value

    def get(self, name, default=None):
        return self._data.get(name, default)

    def items(self):
        return self._data.items()


class RecordingSleeper:
    """记下每次睡了多久，好断言「页间真的睡了」。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeProvider:
    """不打网络的 ``__zp_stoken__`` 桩，只验重试编排（详见 test_jobs_stoken）。"""

    def __init__(self, token: str = "0138FAKE") -> None:
        self.token = token
        self.ensure_calls: list[bool] = []

    def ensure(self, *, force: bool = False) -> str:
        self.ensure_calls.append(force)
        return self.token


# --------------------------------------------------------------------------- #
# 脚本
# --------------------------------------------------------------------------- #


def api_item(encrypt_job_id: str, job_name: str = "岗位", **overrides) -> dict:
    item = {
        "encryptJobId": encrypt_job_id,
        "jobName": job_name,
        "brandName": "示例科技",
        "salaryDesc": "15-25K",
        "jobExperience": "3-5年",
        "jobDegree": "本科",
        "brandIndustry": "互联网/AI",
        "brandScaleName": "100-499人",
        "cityName": "广州",
        "areaDistrict": "天河区",
    }
    item.update(overrides)
    return item


def ok_page(items, *, has_more: bool = True) -> dict:
    return {"code": 0, "message": "Success", "zpData": {"jobList": items, "hasMore": has_more}}


def client_with(responses, *, sleeper=None, **kwargs) -> tuple[JobClient, FakeHttp, RecordingSleeper]:
    http = FakeHttp(responses)
    sleeper = sleeper or RecordingSleeper()
    kwargs.setdefault("retries", 0)  # 测试里不重试，除非用例自己要
    client = JobClient(
        base_url="https://example.test",
        http=http,
        sleeper=sleeper,
        **kwargs,
    )
    return client, http, sleeper


def cookie_map(http) -> dict[str, str]:
    """假 CookieJar 和 requests 的 CookieJar 迭代行为不一样，统一拿 name→value。"""
    jar = getattr(http, "cookies", None)
    getter = getattr(jar, "get_dict", None)
    if callable(getter):
        return dict(getter())
    data = getattr(jar, "_data", None)
    if isinstance(data, dict):
        return dict(data)
    return dict(jar.items())


# --------------------------------------------------------------------------- #
# fetch_page
# --------------------------------------------------------------------------- #


def test_fetch_page_hits_special_zone_route():
    client, http, _ = client_with([ok_page([api_item("a")])])
    payload = client.fetch_page(1)
    assert payload["code"] == 0
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://example.test/wapi/zpgeek/pc/special/zone/joblist.json"
    assert call["params"]["page"] == 1
    assert call["params"]["type"] == "1"  # 全职流
    assert http.headers.get("X-Requested-With") == "XMLHttpRequest"


def test_fetch_page_rejects_page_below_one():
    client, _, _ = client_with([])
    with pytest.raises(ValueError):
        client.fetch_page(0)


def test_fetch_page_clean_maps_fields():
    client, _, _ = client_with([ok_page([api_item("a", jobName="  爬虫 工程师  ")])])
    result = client.fetch_page_clean(1)
    assert result.jobs[0].job_name == "爬虫 工程师"
    assert result.page == 1


# --------------------------------------------------------------------------- #
# crawl：抓 → 洗 → 存 → 睡
# --------------------------------------------------------------------------- #


def test_crawl_cleans_and_stores_each_page_then_sleeps(tmp_path):
    """每页入库后才睡，睡完再要下一页——顺序错了就会被这个用例抓住。"""
    pages = [
        ok_page([api_item("a1"), api_item("a2")], has_more=True),
        ok_page([api_item("b1")], has_more=True),
        ok_page([], has_more=False),  # 空页收尾
    ]
    client, http, sleeper = client_with(pages)
    store = JobStore(tmp_path / "jobs.db")

    report = client.crawl(store=store, page_interval=1.0)

    # 三次请求，页码 1/2/3
    assert [c["params"]["page"] for c in http.calls] == [1, 2, 3]
    # 页与页之间各睡一次 1s（共 2 次；最后一页空，不再翻页所以不睡）
    assert sleeper.calls == [1.0, 1.0]
    # 三页都进了 fetch_pages 流水
    assert store.count_pages() == 3
    # 洗后 3 条全部入库
    assert store.count_jobs() == 3
    assert {j.encrypt_job_id for j in store.list_jobs(limit=10)} == {"a1", "a2", "b1"}

    assert report.stats.pages == 3
    assert report.stats.kept_count == 3
    assert report.stats.inserted == 3
    assert "空列表" in report.stats.stopped_reason


def test_crawl_saves_before_sleep(tmp_path):
    """睡眠之前必须已经入库——中途 Ctrl-C 也不能丢已到手的页。"""
    pages = [
        ok_page([api_item("a1")], has_more=True),
        ok_page([api_item("b1")], has_more=True),
        ok_page([], has_more=False),
    ]
    client, _, sleeper = client_with(pages)
    store = JobStore(tmp_path / "jobs.db")

    # 睡第 1 次时（翻第 2 页前）库里该已经有第 1 页
    seen: list[int] = []

    def spy(seconds: float) -> None:
        seen.append(store.count_jobs())
        sleeper.calls.append(seconds)

    client._sleep = spy
    client.crawl(store=store, page_interval=1.0)

    assert seen == [1, 2]  # 每次睡前，当前页已经落库


def test_crawl_respects_max_pages(tmp_path):
    pages = [ok_page([api_item(f"id{i}")]) for i in range(1, 6)]
    client, http, sleeper = client_with(pages)
    with JobStore(tmp_path / "jobs.db") as store:
        report = client.crawl(store=store, max_pages=2, page_interval=1.0)

    assert len(http.calls) == 2
    assert sleeper.calls == [1.0]  # 两页之间只睡一次
    assert report.stats.pages == 2
    assert "max_pages=2" in report.stats.stopped_reason


def test_crawl_stops_on_partial_page_without_more(tmp_path):
    """不满页 + hasMore=false = 翻到头了，不再多要一页。"""
    from boss_jobs import config as C

    partial = [api_item(f"id{i}") for i in range(C.PAGE_SIZE - 3)]
    client, http, sleeper = client_with([ok_page(partial, has_more=False)])
    with JobStore(tmp_path / "jobs.db") as store:
        report = client.crawl(store=store, page_interval=1.0)

    assert len(http.calls) == 1  # 没有再要第 2 页
    assert sleeper.calls == []
    assert "不满页" in report.stats.stopped_reason


def test_crawl_ignores_unreliable_has_more_on_full_page(tmp_path):
    """page=1 常回 hasMore=false 但后面还有——满页时不能据此收手。"""
    from boss_jobs import config as C

    full = [api_item(f"id{i}") for i in range(C.PAGE_SIZE)]
    pages = [
        ok_page(full, has_more=False),   # 满页 + hasMore=false → 继续
        ok_page([], has_more=False),     # 空页 → 真的没了
    ]
    client, http, _ = client_with(pages)
    with JobStore(tmp_path / "jobs.db") as store:
        report = client.crawl(store=store, page_interval=0.0)

    assert [c["params"]["page"] for c in http.calls] == [1, 2]
    assert report.stats.kept_count == C.PAGE_SIZE


def test_crawl_zero_interval_skips_sleep(tmp_path):
    client, _, sleeper = client_with([ok_page([])])
    with JobStore(tmp_path / "jobs.db") as store:
        client.crawl(store=store, page_interval=0.0)
    assert sleeper.calls == []


def test_crawl_keeps_already_stored_pages_on_error(tmp_path):
    pages = [
        ok_page([api_item("a1")], has_more=True),
        {"code": 7, "message": "当前登录状态已失效"},  # 第 2 页才失效
    ]
    client, _, _ = client_with(pages)
    store = JobStore(tmp_path / "jobs.db")

    with pytest.raises(JobApiError) as excinfo:
        client.crawl(store=store, page_interval=1.0)

    assert excinfo.value.is_session_expired
    assert store.count_jobs() == 1  # 第 1 页保住了


def test_crawl_opens_default_store_when_not_given(tmp_path, monkeypatch):
    db_path = tmp_path / "auto.db"
    monkeypatch.setattr("boss_jobs.client.open_store", lambda path=None: JobStore(db_path))
    client, _, _ = client_with([ok_page([])])
    report = client.crawl(page_interval=0.0)  # 不传 store
    assert report.stats.pages == 1  # 空页也算抓了一页
    assert db_path.exists()


class _StubFilter:
    """最小可用的 JobSearchFilter 桩：只要 to_params/for_page。"""

    def __init__(self, params=None):
        self._params = dict(params or {"query": "python", "city": "101280100", "page": "1"})

    def to_params(self):
        return dict(self._params)

    def for_page(self, page):
        return _StubFilter({**self._params, "page": str(page)})


def test_crawl_带search_filter_走搜索流(tmp_path):
    """给了 search_filter 就打 search/joblist，不打 special/zone。"""
    client, http, _ = client_with(
        [{"code": 0, "zpData": {"jobList": [api_item("s1")], "hasMore": False}}],
        stoken_provider=FakeProvider(),
    )
    with JobStore(tmp_path / "s.db") as store:
        report = client.crawl(store=store, max_pages=1, page_interval=0.0, search_filter=_StubFilter())

    assert report.stats.pages == 1
    assert "search/joblist" in http.calls[0]["url"]
    assert "special/zone" not in http.calls[0]["url"]
    assert http.calls[0]["params"]["query"] == "python"
    assert http.calls[0]["params"]["city"] == "101280100"


def test_crawl_不带search_filter_走推荐流(tmp_path):
    client, http, _ = client_with([ok_page([api_item("r1")], has_more=False)])
    with JobStore(tmp_path / "r.db") as store:
        client.crawl(store=store, max_pages=1, page_interval=0.0)
    assert "special/zone" in http.calls[0]["url"]


def test_crawl_搜索流_翻页时换页码(tmp_path):
    pages = [
        {"code": 0, "zpData": {"jobList": [api_item(f"s{i}")], "hasMore": True}}
        for i in range(2)
    ]
    # 塞满 15 条才不会被「不满页 + hasMore=false」提前收手
    pages[0] = {"code": 0, "zpData": {"jobList": [api_item(f"s{i}") for i in range(15)], "hasMore": True}}
    pages[1] = {"code": 0, "zpData": {"jobList": [api_item("s99")], "hasMore": False}}
    client, http, _ = client_with(pages, stoken_provider=FakeProvider())
    with JobStore(tmp_path / "p.db") as store:
        report = client.crawl(store=store, max_pages=2, page_interval=0.0, search_filter=_StubFilter())

    assert [c["params"]["page"] for c in http.calls] == ["1", "2"]
    assert report.stats.pages == 2


def test_iter_pages_yields_without_store():
    pages = [
        ok_page([api_item("a1")], has_more=True),
        ok_page([api_item("b1")], has_more=False),  # 不满页（1 < 15）→ 停
    ]
    client, _, sleeper = client_with(pages)
    results = list(client.iter_pages(page_interval=1.0))
    assert [r.page for r in results] == [1, 2]
    assert [j.encrypt_job_id for r in results for j in r.jobs] == ["a1", "b1"]
    assert sleeper.calls == [1.0]


# --------------------------------------------------------------------------- #
# 错误分类
# --------------------------------------------------------------------------- #


def test_session_expired_error_is_tagged():
    err = JobApiError(7, "当前登录状态已失效")
    assert err.is_session_expired
    assert not err.is_browser_check


def test_browser_check_error_is_tagged():
    err = JobApiError(37, "你的浏览器环境异常", raw={"zpData": {"seed": "x", "ts": 1, "name": "y"}})
    assert err.is_browser_check
    assert not err.is_session_expired


def test_transport_error_on_bad_json():
    client, _, _ = client_with([FakeResponse(None, text="<html>oops</html>")])
    with pytest.raises(JobTransportError):
        client.fetch_page(1)


def test_data_error_on_non_object_payload():
    client, _, _ = client_with([FakeResponse([1, 2, 3])])
    with pytest.raises(JobDataError):
        client.fetch_page(1)


def test_retries_then_succeeds():
    sleeper = RecordingSleeper()
    pages = [
        FakeResponse(None, status=502),
        ok_page([api_item("a")]),
    ]
    client, http, _ = client_with(pages, sleeper=sleeper, retries=1, backoff=0.5)
    assert client.fetch_page(1)["code"] == 0
    assert len(http.calls) == 2
    assert sleeper.calls == [0.5]  # 退避了一次


# --------------------------------------------------------------------------- #
# 会话装配
# --------------------------------------------------------------------------- #


def test_http_from_session_loads_cookies(tmp_path):
    session_path = tmp_path / "session.json"
    session_path.write_text(
        json.dumps(
            {
                "token": "",
                "cookies": {"wt2": "abc", "bst": "def"},
                "phone_masked": "176****6772",
                "user": {},
                "saved_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    # 用假会话验灌 Cookie 的逻辑；真 requests.Session 见下一个用例
    fake = FakeHttp([])
    http = http_from_session(session_path, http=fake)
    assert http is fake
    assert cookie_map(http) == {"wt2": "abc", "bst": "def"}


def test_http_from_session_explains_missing_session(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(JobApiError) as excinfo:
        http_from_session(missing)
    assert str(missing) in excinfo.value.message
    assert "boss_login" in excinfo.value.message


def test_create_client_uses_session(tmp_path):
    session_path = tmp_path / "session.json"
    session_path.write_text(
        json.dumps({"token": "", "cookies": {"wt2": "x"}, "saved_at": 1.0}),
        encoding="utf-8",
    )
    client = create_client(session_path=session_path, page_interval=2.5, sleeper=lambda _s: None)
    assert client.page_interval == 2.5
    assert cookie_map(client._http) == {"wt2": "x"}


# --------------------------------------------------------------------------- #
# __zp_stoken__：显式参数 → session.json → 环境变量
# --------------------------------------------------------------------------- #


def write_session(tmp_path, cookies: dict[str, str]):
    session_path = tmp_path / "session.json"
    session_path.write_text(
        json.dumps({"token": "", "cookies": cookies, "saved_at": 1.0}),
        encoding="utf-8",
    )
    return session_path


def test_stoken_从显式参数来(tmp_path):
    path = write_session(tmp_path, {"wt2": "x"})
    fake = FakeHttp([])
    http_from_session(path, http=fake, stoken="TOKEN-FROM-ARG")
    assert cookie_map(fake) == {"wt2": "x", "__zp_stoken__": "TOKEN-FROM-ARG"}


def test_stoken_从session_json来(tmp_path):
    path = write_session(tmp_path, {"wt2": "x", "__zp_stoken__": "TOKEN-FROM-SESSION"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-SESSION"


def test_stoken_从环境变量兜底(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_ZP_STOKEN", "TOKEN-FROM-ENV")
    path = write_session(tmp_path, {"wt2": "x"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-ENV"


def test_stoken_显式参数优先级最高(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_ZP_STOKEN", "TOKEN-FROM-ENV")
    path = write_session(tmp_path, {"__zp_stoken__": "TOKEN-FROM-SESSION"})
    fake = FakeHttp([])
    http_from_session(path, http=fake, stoken="TOKEN-FROM-ARG")
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-ARG"


def test_stoken_都没有就不带它(tmp_path, monkeypatch):
    monkeypatch.delenv("BOSS_ZP_STOKEN", raising=False)
    path = write_session(tmp_path, {"wt2": "x"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert "__zp_stoken__" not in cookie_map(fake)


def test_stoken_会话里有就不会被环境变量盖掉(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_ZP_STOKEN", "TOKEN-FROM-ENV")
    path = write_session(tmp_path, {"__zp_stoken__": "TOKEN-FROM-SESSION"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-SESSION"


# --------------------------------------------------------------------------- #
# fetch_search_page
# --------------------------------------------------------------------------- #


def search_filter(**kwargs):
    from boss_filter.search import JobSearchFilter

    kwargs.setdefault("query", "python")
    kwargs.setdefault("city", "101280100")
    return JobSearchFilter(**kwargs)


def test_fetch_search_page_打搜索路由():
    client, http, _ = client_with([ok_page([api_item("a")])])
    client.fetch_search_page(search_filter())
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/wapi/zpgeek/search/joblist.json")


def test_fetch_search_page_把筛选条件摊成查询串():
    client, http, _ = client_with([ok_page([])])
    client.fetch_search_page(search_filter(salary="405", experience=("104", "105")))
    params = http.calls[0]["params"]
    assert params["query"] == "python"
    assert params["city"] == "101280100"
    assert params["salary"] == "405"
    assert params["experience"] == "104,105"
    assert params["page"] == "1"
    assert params["scene"] == "1"


def test_fetch_search_page_翻页只改页码():
    client, http, _ = client_with([ok_page([api_item("a")]), ok_page([api_item("b")])])
    first = client.fetch_search_page(search_filter(), page=1)
    second = client.fetch_search_page(search_filter(), page=2)
    assert http.calls[0]["params"]["page"] == "1"
    assert http.calls[1]["params"]["page"] == "2"
    assert first.page == 1
    assert second.page == 2


def test_fetch_search_page_清洗后回PageResult():
    client, http, _ = client_with([ok_page([api_item("a", jobName="Python 开发")])])
    result = client.fetch_search_page(search_filter())
    assert [job.job_name for job in result.jobs] == ["Python 开发"]


def test_fetch_search_page_code37_标记成浏览器校验():
    """code 37 归到 is_browser_check；「去哪儿补令牌」的话术在 CLI 层（见
    test_cli_fetch_reports_browser_check），客户端只负责把码标对。"""
    client, http, _ = client_with(
        [{"code": 37, "message": "你的浏览器环境异常", "zpData": {"seed": "s", "ts": 1, "name": "n"}}]
    )
    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(search_filter())
    assert excinfo.value.code == 37
    assert excinfo.value.is_browser_check
    assert not excinfo.value.is_session_expired


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_fetch_writes_db(tmp_path, monkeypatch):
    from boss_jobs import config as C

    full = [api_item(f"id{i}") for i in range(C.PAGE_SIZE)]
    responses = [
        ok_page(full, has_more=True),
        ok_page([], has_more=False),
    ]
    fake_http = FakeHttp(responses)
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: fake_http,
    )
    db_path = tmp_path / "cli.db"
    code = cli.main(
        ["--db", str(db_path), "--session", str(tmp_path / "s.json"), "fetch", "--interval", "0"]
    )
    assert code == 0
    with JobStore(db_path) as store:
        assert store.count_jobs() == C.PAGE_SIZE


def test_cli_fetch_reports_browser_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: FakeHttp(
            [{"code": 37, "message": "你的浏览器环境异常", "zpData": {"seed": "s", "ts": 1, "name": "n"}}]
        ),
    )
    code = cli.main(
        ["--db", str(tmp_path / "x.db"), "--session", str(tmp_path / "s.json"), "fetch"]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "__zp_stoken__" in err
    # 现在会自动算令牌，提示也改成「自动补 + 仍被拒怎么办」
    assert "自动算" in err
    assert "security-js" in err


def test_cli_fetch_reports_risk_control(tmp_path, monkeypatch, capsys):
    """code 36 是账号风控，话术要明说「不去绕」。"""
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: FakeHttp(
            [{"code": 36, "message": "您的账户存在异常行为", "zpData": {}}]
        ),
    )
    code = cli.main(
        ["--db", str(tmp_path / "x.db"), "--session", str(tmp_path / "s.json"), "fetch"]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "风控" in err
    assert "不去绕" in err


class _Httplet:
    """只实现 crawl 要用的 request() 的极简会话。"""

    def __init__(self, request):
        self._request = request
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookieJar()

    def request(self, method, url, **kwargs):
        return self._request(method, url, **kwargs)


def _capturing_http(captured):
    """造一个会话，把每次请求的 url/params 记进 ``captured``。"""

    def _request(method, url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params") or {}
        return FakeResponse(ok_page([api_item("j1")], has_more=False))

    return _Httplet(_request)


def test_cli_fetch_条件文件_有筛选就走搜索流(tmp_path, monkeypatch, capsys):
    """配置文件里写了条件 → 打 search/joblist，并把条件带进查询串。"""
    monkeypatch.setenv("BOSS_SEARCH_FILTER", str(tmp_path / "f.json"))
    (tmp_path / "f.json").write_text(
        '{"query": "python", "city": "101280100", "salary": "405"}', encoding="utf-8"
    )
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session", lambda *a, **k: _capturing_http(captured)
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1"])
    assert code == 0
    assert "search/joblist" in captured["url"]
    assert captured["params"]["query"] == "python"
    assert captured["params"]["city"] == "101280100"
    assert captured["params"]["salary"] == "405"
    out = capsys.readouterr().out
    assert "搜索流" in out


def test_cli_fetch_条件文件不存在_留空走推荐流(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSS_SEARCH_FILTER", str(tmp_path / "没有.json"))
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session", lambda *a, **k: _capturing_http(captured)
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1"])
    assert code == 0
    assert "special/zone" in captured["url"]   # 空条件 = 不限 = 推荐流
    assert "query" not in captured["params"]
    out = capsys.readouterr().out
    assert "推荐流" in out


def test_cli_list_and_stats(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli.db"
    with JobStore(db_path) as store:
        store.save_page(clean_page(ok_page([api_item("a", jobName="Python 开发", cityName="广州")]), page=1))

    assert cli.main(["--db", str(db_path), "list", "--city", "广州"]) == 0
    out = capsys.readouterr().out
    assert "Python 开发" in out

    assert cli.main(["--db", str(db_path), "stats", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["jobs"] == 1
