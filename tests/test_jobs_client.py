"""职位客户端：抓页 / 翻页流水线 / 节流 / 错误分类。

重点验「抓一页 → 立刻清洗 → 立刻入库 → 睡够间隔 → 下一页」这条顺序，
以及翻页停止条件。网络层用假会话，不打真实站点。
"""

from __future__ import annotations

import json

import pytest

import boss_jobs.cli as cli
from boss_jobs.client import JobClient, create_client, http_from_session
from boss_jobs.errors import (
    JobApiError,
    JobDataError,
    JobTransportError,
    format_api_message,
)
from boss_jobs.models import clean_page
from boss_jobs.store import JobStore


class FakeResponse:
    """假的 HTTP 响应体。"""

    def __init__(self, payload=None, *, status: int = 200, text: str | None = None) -> None:
        """初始化。"""
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        """返回预置载荷；载荷是异常时直接抛出。"""
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """按序回脚本化响应；异常实例直接抛。裸 dict 自动包成 FakeResponse。"""

    def __init__(self, responses) -> None:
        """初始化。"""
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookieJar()

    def request(self, method, url, **kwargs):
        """记录一次调用并返回下一条预置响应。"""
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"没有预置响应可给，却收到了第 {len(self.calls)} 次请求：{url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


class FakeCookieJar:
    """最小 Cookie 罐，只支持 set/get/items。"""

    def __init__(self) -> None:
        """初始化。"""
        self._data: dict[str, str] = {}

    def set(self, name, value, **_kwargs):
        """写入一枚 Cookie。"""
        self._data[name] = value

    def get(self, name, default=None):
        """按名取 Cookie，取不到回默认值。"""
        return self._data.get(name, default)

    def items(self):
        """回 name→value 迭代。"""
        return self._data.items()


class RecordingSleeper:
    """记下每次睡了多久，好断言「页间真的睡了」。"""

    def __init__(self) -> None:
        """初始化。"""
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        """记录本次睡眠秒数。"""
        self.calls.append(seconds)


class FakeProvider:
    """不打网络的 `__zp_stoken__` 桩，只验重试编排。"""

    def __init__(self, token: str = "0138FAKE") -> None:
        """初始化。"""
        self.token = token
        self.ensure_calls: list[bool] = []

    def ensure(self, *, force: bool = False) -> str:
        """回令牌并记下是否强制换新。"""
        self.ensure_calls.append(force)
        return self.token


def api_item(encrypt_job_id: str, job_name: str = "岗位", **overrides) -> dict:
    """构造一条列表接口的职位项。"""
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
    """构造一页成功的职位列表响应。"""
    return {"code": 0, "message": "Success", "zpData": {"jobList": items, "hasMore": has_more}}


def client_with(responses, *, sleeper=None, **kwargs) -> tuple[JobClient, FakeHttp, RecordingSleeper]:
    """用假会话造 JobClient，默认不重试并带记时睡眠。"""
    http = FakeHttp(responses)
    sleeper = sleeper or RecordingSleeper()
    kwargs.setdefault("retries", 0)
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


class _StubFilter:
    """最小可用的 JobSearchFilter 桩：只要 to_params/for_page。

    crawl/iter_pages 必须给 search_filter 或 expect_filter，走管线的用例都带上它。
    """

    def __init__(self, params=None):
        """初始化。"""
        self._params = dict(params or {"query": "python", "city": "101280100", "page": "1"})

    def to_params(self):
        """回当前查询参数副本。"""
        return dict(self._params)

    def for_page(self, page):
        """回换了页码的新桩。"""
        return _StubFilter({**self._params, "page": str(page)})


def test_crawl_cleans_and_stores_each_page_then_sleeps(tmp_path):
    """每页入库后才睡，睡完再要下一页——顺序错了就会被这个用例抓住。

    页与页之间各睡一次 1s（共 2 次；最后一页空，不再翻页所以不睡）。
    """
    pages = [
        ok_page([api_item("a1"), api_item("a2")], has_more=True),
        ok_page([api_item("b1")], has_more=True),
        ok_page([], has_more=False),
    ]
    client, http, sleeper = client_with(pages)
    store = JobStore(tmp_path / "jobs.db")

    report = client.crawl(store=store, page_interval=1.0, search_filter=_StubFilter())

    assert [int(c["params"]["page"]) for c in http.calls] == [1, 2, 3]
    assert sleeper.calls == [1.0, 1.0]
    assert store.count_pages() == 3
    assert store.count_jobs() == 3
    assert {j.encrypt_job_id for j in store.list_jobs(limit=10)} == {"a1", "a2", "b1"}

    assert report.stats.pages == 3
    assert report.stats.kept_count == 3
    assert report.stats.inserted == 3
    assert "空列表" in report.stats.stopped_reason


def test_crawl_saves_before_sleep(tmp_path):
    """睡眠之前必须已经入库——中途 Ctrl-C 也不能丢已到手的页。

    睡第 1 次时（翻第 2 页前）库里该已经有第 1 页。
    """
    pages = [
        ok_page([api_item("a1")], has_more=True),
        ok_page([api_item("b1")], has_more=True),
        ok_page([], has_more=False),
    ]
    client, _, sleeper = client_with(pages)
    store = JobStore(tmp_path / "jobs.db")

    seen: list[int] = []

    def spy(seconds: float) -> None:
        """记录每次睡眠前的已入库条数。"""
        seen.append(store.count_jobs())
        sleeper.calls.append(seconds)

    client._sleep = spy
    client.crawl(store=store, page_interval=1.0, search_filter=_StubFilter())

    assert seen == [1, 2]


def test_crawl_respects_max_pages(tmp_path):
    """验 max_pages 截断翻页。"""
    pages = [ok_page([api_item(f"id{i}")]) for i in range(1, 6)]
    client, http, sleeper = client_with(pages)
    with JobStore(tmp_path / "jobs.db") as store:
        report = client.crawl(store=store, max_pages=2, page_interval=1.0, search_filter=_StubFilter())

    assert len(http.calls) == 2
    assert sleeper.calls == [1.0]
    assert report.stats.pages == 2
    assert "max_pages=2" in report.stats.stopped_reason


def test_crawl_stops_on_partial_page_without_more(tmp_path):
    """不满页 + hasMore=false = 翻到头了，不再多要一页。"""
    from boss_jobs import config as C

    partial = [api_item(f"id{i}") for i in range(C.PAGE_SIZE - 3)]
    client, http, sleeper = client_with([ok_page(partial, has_more=False)])
    with JobStore(tmp_path / "jobs.db") as store:
        report = client.crawl(store=store, page_interval=1.0, search_filter=_StubFilter())

    assert len(http.calls) == 1
    assert sleeper.calls == []
    assert "不满页" in report.stats.stopped_reason


def test_crawl_ignores_unreliable_has_more_on_full_page(tmp_path):
    """page=1 常回 hasMore=false 但后面还有——满页时不能据此收手。"""
    from boss_jobs import config as C

    full = [api_item(f"id{i}") for i in range(C.PAGE_SIZE)]
    pages = [
        ok_page(full, has_more=False),
        ok_page([], has_more=False),
    ]
    client, http, _ = client_with(pages)
    with JobStore(tmp_path / "jobs.db") as store:
        report = client.crawl(store=store, page_interval=0.0, search_filter=_StubFilter())

    assert [int(c["params"]["page"]) for c in http.calls] == [1, 2]
    assert report.stats.kept_count == C.PAGE_SIZE


def test_crawl_zero_interval_skips_sleep(tmp_path):
    """验页间隔为 0 时不睡。"""
    client, _, sleeper = client_with([ok_page([])])
    with JobStore(tmp_path / "jobs.db") as store:
        client.crawl(store=store, page_interval=0.0, search_filter=_StubFilter())
    assert sleeper.calls == []


def test_crawl_keeps_already_stored_pages_on_error(tmp_path):
    """验中途报错时已入库页保留。"""
    pages = [
        ok_page([api_item("a1")], has_more=True),
        {"code": 7, "message": "当前登录状态已失效"},
    ]
    client, _, _ = client_with(pages)
    store = JobStore(tmp_path / "jobs.db")

    with pytest.raises(JobApiError) as excinfo:
        client.crawl(store=store, page_interval=1.0, search_filter=_StubFilter())

    assert excinfo.value.is_session_expired
    assert store.count_jobs() == 1


def test_crawl_opens_default_store_when_not_given(tmp_path, monkeypatch):
    """验不传 store 时自动开默认库。"""
    db_path = tmp_path / "auto.db"
    monkeypatch.setattr("boss_jobs.client.open_store", lambda path=None: JobStore(db_path))
    client, _, _ = client_with([ok_page([])])
    report = client.crawl(page_interval=0.0, search_filter=_StubFilter())
    assert report.stats.pages == 1
    assert db_path.exists()


def test_crawl_带search_filter_走搜索流(tmp_path):
    """给了 search_filter 就打 search/joblist。"""
    client, http, _ = client_with(
        [{"code": 0, "zpData": {"jobList": [api_item("s1")], "hasMore": False}}],
        stoken_provider=FakeProvider(),
    )
    with JobStore(tmp_path / "s.db") as store:
        report = client.crawl(store=store, max_pages=1, page_interval=0.0, search_filter=_StubFilter())

    assert report.stats.pages == 1
    assert "search/joblist" in http.calls[0]["url"]
    assert http.calls[0]["params"]["query"] == "python"
    assert http.calls[0]["params"]["city"] == "101280100"


def test_crawl_两个filter都不给就报错(tmp_path):
    """验两个 filter 都不给时没有检索语义，抛 ValueError。"""
    client, http, _ = client_with([ok_page([api_item("r1")], has_more=False)])
    with JobStore(tmp_path / "r.db") as store:
        with pytest.raises(ValueError, match="expect_filter 或 search_filter"):
            client.crawl(store=store, max_pages=1, page_interval=0.0)
    assert http.calls == []


def test_crawl_搜索流_翻页时换页码(tmp_path):
    """翻页时换 page 参数。

    首页要塞满 15 条才不会被「不满页 + hasMore=false」提前收手。
    """
    pages = [
        {"code": 0, "zpData": {"jobList": [api_item(f"s{i}")], "hasMore": True}}
        for i in range(2)
    ]
    pages[0] = {"code": 0, "zpData": {"jobList": [api_item(f"s{i}") for i in range(15)], "hasMore": True}}
    pages[1] = {"code": 0, "zpData": {"jobList": [api_item("s99")], "hasMore": False}}
    client, http, _ = client_with(pages, stoken_provider=FakeProvider())
    with JobStore(tmp_path / "p.db") as store:
        report = client.crawl(store=store, max_pages=2, page_interval=0.0, search_filter=_StubFilter())

    assert [c["params"]["page"] for c in http.calls] == ["1", "2"]
    assert report.stats.pages == 2


def test_iter_pages_yields_without_store():
    """验 iter_pages 不落库也能逐页产出。"""
    pages = [
        ok_page([api_item("a1")], has_more=True),
        ok_page([api_item("b1")], has_more=False),
    ]
    client, _, sleeper = client_with(pages)
    results = list(client.iter_pages(page_interval=1.0, search_filter=_StubFilter()))
    assert [r.page for r in results] == [1, 2]
    assert [j.encrypt_job_id for r in results for j in r.jobs] == ["a1", "b1"]
    assert sleeper.calls == [1.0]


def test_iter_pages_两个filter都不给就报错():
    """验不给 filter 时 iter_pages 抛 ValueError。"""
    client, _, _ = client_with([])
    with pytest.raises(ValueError, match="expect_filter 或 search_filter"):
        list(client.iter_pages())


def test_session_expired_error_is_tagged():
    """验 code 7 标成登录失效。"""
    err = JobApiError(7, "当前登录状态已失效")
    assert err.is_session_expired
    assert not err.is_browser_check


def test_code1_non_login_message_is_not_session_expired():
    """code 1 是业务失败的通用码，不能一律当登录失效。"""
    err = JobApiError(1, "开聊提醒")
    assert not err.is_session_expired
    assert not err.is_browser_check
    assert JobApiError(1, "当前登录状态已失效").is_session_expired


def test_chat_remind_dialog_is_tagged():
    """「开聊提醒」弹窗：是沟通配额，不是登录失效。没 raw、只有话术的也认。"""
    raw = {
        "code": 1,
        "message": "开聊提醒",
        "zpData": {
            "bizCode": 1,
            "bizMessage": "开聊提醒",
            "bizData": {
                "chatRemindDialog": {
                    "title": "温馨提示",
                    "content": "您今天已与120位BOSS沟通，还剩30次沟通机会哦",
                    "remindType": 524288,
                    "blockLevel": 0,
                }
            },
        },
    }
    err = JobApiError(1, "开聊提醒", raw=raw)
    assert err.is_chat_remind
    assert not err.is_session_expired
    assert not err.is_risk_control
    assert JobApiError(1, "开聊提醒").is_chat_remind


def test_format_api_message_prefers_dialog_content():
    """顶层 message 常常只是短标签，真话在 chatRemindDialog.content 里。

    没弹窗就退回顶层 message / msg。
    """
    raw = {
        "code": 1,
        "message": "开聊提醒",
        "zpData": {
            "bizData": {
                "chatRemindDialog": {
                    "title": "温馨提示",
                    "content": "您今天已与120位BOSS沟通，还剩30次沟通机会哦",
                }
            }
        },
    }
    assert (
        format_api_message(raw)
        == "您今天已与120位BOSS沟通，还剩30次沟通机会哦"
    )
    assert format_api_message({"message": "参数错误"}) == "参数错误"
    assert format_api_message({"msg": "参数错误"}) == "参数错误"
    assert format_api_message({"code": 1}) == ""


def test_request_json_surfaces_dialog_content_as_message():
    """撞「开聊提醒」时，异常话术要是弹窗那句原话，不是「开聊提醒」四个字。"""
    raw = {
        "code": 1,
        "message": "开聊提醒",
        "zpData": {
            "bizData": {
                "chatRemindDialog": {
                    "title": "温馨提示",
                    "content": "您今天已与120位BOSS沟通，还剩30次沟通机会哦",
                }
            }
        },
    }
    client, _, _ = client_with([FakeResponse(raw)])
    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_StubFilter())
    assert excinfo.value.is_chat_remind
    assert "还剩30次沟通机会" in excinfo.value.message
    assert not excinfo.value.is_session_expired


def test_browser_check_error_is_tagged():
    """验 code 37 标成浏览器校验。"""
    err = JobApiError(37, "你的浏览器环境异常", raw={"zpData": {"seed": "x", "ts": 1, "name": "y"}})
    assert err.is_browser_check
    assert not err.is_session_expired


def test_transport_error_on_bad_json():
    """验非 JSON 响应归为传输错误。"""
    client, _, _ = client_with([FakeResponse(None, text="<html>oops</html>")])
    with pytest.raises(JobTransportError):
        client.fetch_search_page(_StubFilter())


def test_data_error_on_non_object_payload():
    """验非对象载荷抛数据错误。"""
    client, _, _ = client_with([FakeResponse([1, 2, 3])])
    with pytest.raises(JobDataError):
        client.fetch_search_page(_StubFilter())


def test_retries_then_succeeds():
    """验 5xx 后重试成功。"""
    sleeper = RecordingSleeper()
    pages = [
        FakeResponse(None, status=502),
        ok_page([api_item("a")]),
    ]
    client, http, _ = client_with(pages, sleeper=sleeper, retries=1, backoff=0.5)
    result = client.fetch_search_page(_StubFilter())
    assert [j.encrypt_job_id for j in result.jobs] == ["a"]
    assert len(http.calls) == 2
    assert sleeper.calls == [0.5]


def test_http_from_session_loads_cookies(tmp_path):
    """用假会话验灌 Cookie 的逻辑；真 requests.Session 见下一个用例。"""
    from boss_login.session import StoredSession, save_session

    session_path = tmp_path / "session.db"
    """用假会话验灌 Cookie 的逻辑；真 requests.Session 见下一个用例。"""
    save_session(
        StoredSession(
            token="",
            cookies={"wt2": "abc", "bst": "def"},
            phone_masked="176****6772",
            user={},
            saved_at=1.0,
        ),
        session_path,
    )
    fake = FakeHttp([])
    http = http_from_session(session_path, http=fake)
    assert http is fake
    assert cookie_map(http) == {"wt2": "abc", "bst": "def"}


def test_http_from_session_explains_missing_session(tmp_path):
    """验缺会话文件时错误话术带路径和 boss_login。"""
    missing = tmp_path / "nope.json"
    with pytest.raises(JobApiError) as excinfo:
        http_from_session(missing)
    assert str(missing) in excinfo.value.message
    assert "boss_login" in excinfo.value.message


def test_create_client_uses_session(tmp_path):
    """验 create_client 读会话 Cookie 并保留页间隔。"""
    from boss_login.session import StoredSession, save_session

    session_path = tmp_path / "session.db"
    save_session(StoredSession(token="", cookies={"wt2": "x"}, saved_at=1.0), session_path)
    client = create_client(session_path=session_path, page_interval=2.5, sleeper=lambda _s: None)
    assert client.page_interval == 2.5
    assert cookie_map(client._http) == {"wt2": "x"}


def write_session(tmp_path, cookies: dict[str, str]):
    """把 Cookie 落成会话文件并回路径。"""
    from boss_login.session import StoredSession, save_session

    session_path = tmp_path / "session.db"
    save_session(StoredSession(cookies=cookies, saved_at=1.0), session_path)
    return session_path


def test_stoken_从显式参数来(tmp_path):
    """验 stoken 显式参数写进 Cookie。"""
    path = write_session(tmp_path, {"wt2": "x"})
    fake = FakeHttp([])
    http_from_session(path, http=fake, stoken="TOKEN-FROM-ARG")
    assert cookie_map(fake) == {"wt2": "x", "__zp_stoken__": "TOKEN-FROM-ARG"}


def test_stoken_从session_json来(tmp_path):
    """验 stoken 取自会话文件。"""
    path = write_session(tmp_path, {"wt2": "x", "__zp_stoken__": "TOKEN-FROM-SESSION"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-SESSION"


def test_stoken_从环境变量兜底(tmp_path, monkeypatch):
    """验会话无 stoken 时用环境变量兜底。"""
    monkeypatch.setenv("BOSS_ZP_STOKEN", "TOKEN-FROM-ENV")
    path = write_session(tmp_path, {"wt2": "x"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-ENV"


def test_stoken_显式参数优先级最高(tmp_path, monkeypatch):
    """验显式 stoken 优先于会话和环境变量。"""
    monkeypatch.setenv("BOSS_ZP_STOKEN", "TOKEN-FROM-ENV")
    path = write_session(tmp_path, {"__zp_stoken__": "TOKEN-FROM-SESSION"})
    fake = FakeHttp([])
    http_from_session(path, http=fake, stoken="TOKEN-FROM-ARG")
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-ARG"


def test_stoken_都没有就不带它(tmp_path, monkeypatch):
    """验三处都没有 stoken 时不写 Cookie。"""
    monkeypatch.delenv("BOSS_ZP_STOKEN", raising=False)
    path = write_session(tmp_path, {"wt2": "x"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert "__zp_stoken__" not in cookie_map(fake)


def test_换新令牌时不会留下同名旧Cookie(tmp_path, monkeypatch):
    """换新令牌时清掉同名旧 Cookie，避免请求头里同时出现两枚。"""
    import requests

    from boss_jobs.stoken import put_cookie

    jar = requests.cookies.RequestsCookieJar()
    jar.set("__zp_stoken__", "OLD")
    put_cookie(jar, "__zp_stoken__", "NEW")
    assert [c.value for c in jar if c.name == "__zp_stoken__"] == ["NEW"]
    assert next(c for c in jar if c.name == "__zp_stoken__").domain == ".zhipin.com"

    prepared = requests.Request("GET", "https://www.zhipin.com/wapi/x").prepare()
    header = requests.cookies.get_cookie_header(jar, prepared) or ""
    assert header.count("__zp_stoken__=") == 1
    assert "NEW" in header and "OLD" not in header


def test_stoken_会话里有就不会被环境变量盖掉(tmp_path, monkeypatch):
    """验会话里的 stoken 不被环境变量覆盖。"""
    monkeypatch.setenv("BOSS_ZP_STOKEN", "TOKEN-FROM-ENV")
    path = write_session(tmp_path, {"__zp_stoken__": "TOKEN-FROM-SESSION"})
    fake = FakeHttp([])
    http_from_session(path, http=fake)
    assert cookie_map(fake)["__zp_stoken__"] == "TOKEN-FROM-SESSION"


def search_filter(**kwargs):
    """构造一条搜索筛选。"""
    from boss_filter.search import JobSearchFilter

    kwargs.setdefault("query", "python")
    kwargs.setdefault("city", "101280100")
    return JobSearchFilter(**kwargs)


def test_fetch_search_page_打搜索路由():
    """验搜索流打 search/joblist 路由。"""
    client, http, _ = client_with([ok_page([api_item("a")])])
    client.fetch_search_page(search_filter())
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/wapi/zpgeek/search/joblist.json")


def test_fetch_search_page_把筛选条件摊成查询串():
    """验筛选条件摊平进查询串。"""
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
    """验搜索流翻页只改 page 参数。"""
    client, http, _ = client_with([ok_page([api_item("a")]), ok_page([api_item("b")])])
    first = client.fetch_search_page(search_filter(), page=1)
    second = client.fetch_search_page(search_filter(), page=2)
    assert http.calls[0]["params"]["page"] == "1"
    assert http.calls[1]["params"]["page"] == "2"
    assert first.page == 1
    assert second.page == 2


def test_fetch_search_page_清洗后回PageResult():
    """验列表项清洗后进 PageResult。"""
    client, http, _ = client_with([ok_page([api_item("a", jobName="Python 开发")])])
    result = client.fetch_search_page(search_filter())
    assert [job.job_name for job in result.jobs] == ["Python 开发"]


def test_fetch_search_page_code37_标记成浏览器校验():
    """验 code 37 标成浏览器校验，补令牌话术留给 CLI 层。"""
    client, http, _ = client_with(
        [{"code": 37, "message": "你的浏览器环境异常", "zpData": {"seed": "s", "ts": 1, "name": "n"}}]
    )
    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(search_filter())
    assert excinfo.value.code == 37
    assert excinfo.value.is_browser_check
    assert not excinfo.value.is_session_expired


def expect_filter(**kwargs):
    """构造一条期望筛选。"""
    from boss_filter.expect import JobExpectFilter

    kwargs.setdefault("kind", "expect")
    kwargs.setdefault("encrypt_expect_id", "E1")
    return JobExpectFilter(**kwargs)


def ok_expects(*, mix=None, expects=(), part_time=()) -> dict:
    """构造成功的期望列表响应。"""
    zp: dict = {}
    if mix is not None:
        zp["mixExpect"] = mix
    if expects:
        zp["expectList"] = list(expects)
    if part_time:
        zp["partTimeExpectList"] = list(part_time)
    return {"code": 0, "message": "Success", "zpData": zp}


def test_fetch_expect_list_打期望元数据路由():
    """验期望元数据打 expect/list 路由。"""
    client, http, _ = client_with(
        [ok_expects(mix={"encryptExpectId": "MIX1", "positionName": "混合", "locationName": "佛山", "mixExpectType": "2"})]
    )
    result = client.fetch_expect_list()
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/wapi/zpgeek/pc/recommend/expect/list.json")
    assert result.mix is not None
    assert result.mix.encrypt_expect_id == "MIX1"


def test_fetch_expect_page_打期望流路由():
    """验期望流打 recommend/job/list 路由。"""
    client, http, _ = client_with([ok_page([api_item("a")])])
    client.fetch_expect_page(expect_filter())
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/wapi/zpgeek/pc/recommend/job/list.json")


def test_fetch_expect_page_四种kind的主参数():
    """验四种 kind 各自的主查询参数。"""
    cases = [
        (dict(kind="recommend", encrypt_expect_id="MIX1", mix_expect_type="2", city="101280100"),
         {"encryptExpectId": "MIX1", "mixExpectType": "2", "city": "101280100"}),
        (dict(kind="part_time", encrypt_expect_id="PT1"),
         {"encryptExpectId": "PT1", "jobType": "1903"}),
        (dict(kind="expect", encrypt_expect_id="E1"),
         {"encryptExpectId": "E1"}),
        (dict(kind="temp_expect", expect_info='{"expectType":"1"}'),
         {"expectInfo": '{"expectType":"1"}'}),
    ]
    for kwargs, want in cases:
        client, http, _ = client_with([ok_page([])])
        client.fetch_expect_page(expect_filter(**kwargs))
        params = http.calls[0]["params"]
        for key, value in want.items():
            assert params.get(key) == value, f"{kwargs} 的 {key} 不对：{params}"
        assert "query" not in params
        assert "scene" not in params


def test_fetch_expect_page_翻页只改页码():
    """验期望流翻页只改 page 参数。"""
    client, http, _ = client_with([ok_page([]), ok_page([])])
    f = expect_filter()
    first = client.fetch_expect_page(f, page=1)
    second = client.fetch_expect_page(f, page=2)
    assert http.calls[0]["params"]["page"] == "1"
    assert http.calls[1]["params"]["page"] == "2"
    assert first.page == 1
    assert second.page == 2


def test_fetch_expect_page_走stoken重试():
    """验期望流撞 37 先退避拿同一枚重试。"""
    from boss_jobs import config as C

    tokens = []

    class OnceProvider:
        """只回一枚令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return "TOKEN-1"

    http = FakeHttp(
        [
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            ok_page([api_item("a")]),
        ]
    )
    sleeper = RecordingSleeper()
    client = JobClient(http=http, stoken_provider=OnceProvider(), sleeper=sleeper)
    result = client.fetch_expect_page(expect_filter())
    assert [job.job_name for job in result.jobs] == ["岗位"]
    assert tokens == [False]
    assert sleeper.calls == [C.BROWSER_CHECK_BACKOFF]
    assert len(http.calls) == 2


def test_crawl_带expect_filter_走期望流(tmp_path):
    """给了 expect_filter 就打 recommend/job/list，不打 search。"""
    client, http, _ = client_with(
        [ok_page([api_item("e1")], has_more=False)],
        stoken_provider=FakeProvider(),
    )
    with JobStore(tmp_path / "e1.db") as store:
        report = client.crawl(
            store=store, max_pages=1, page_interval=0.0, expect_filter=expect_filter()
        )
    assert report.stats.pages == 1
    assert "recommend/job/list" in http.calls[0]["url"]
    assert "search/joblist" not in http.calls[0]["url"]
    assert http.calls[0]["params"]["encryptExpectId"] == "E1"


def test_crawl_expect_filter优先于search_filter(tmp_path):
    """两条都给时以期望流为准（配置里 expect_kind 非空就走期望流）。"""
    client, http, _ = client_with(
        [ok_page([api_item("e1")], has_more=False)],
        stoken_provider=FakeProvider(),
    )
    with JobStore(tmp_path / "e.db") as store:
        client.crawl(
            store=store,
            max_pages=1,
            page_interval=0.0,
            expect_filter=expect_filter(),
            search_filter=_StubFilter(),
        )
    assert "recommend/job/list" in http.calls[0]["url"]
    assert "search/joblist" not in http.calls[0]["url"]


def test_crawl_只有search_filter_仍走搜索流(tmp_path):
    """不破坏已有两条流：只给 search_filter 时仍是 search/joblist。"""
    client, http, _ = client_with(
        [ok_page([api_item("s1")], has_more=False)],
        stoken_provider=FakeProvider(),
    )
    with JobStore(tmp_path / "s2.db") as store:
        client.crawl(store=store, max_pages=1, page_interval=0.0, search_filter=_StubFilter())
    assert "search/joblist" in http.calls[0]["url"]
    assert "recommend/job/list" not in http.calls[0]["url"]


def test_cli_fetch_writes_db(tmp_path, monkeypatch):
    """验 CLI fetch 写库。"""
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
    f = tmp_path / "f.json"
    f.write_text('{"query": "python"}', encoding="utf-8")
    db_path = tmp_path / "cli.db"
    code = cli.main(
        ["--db", str(db_path), "fetch", "--interval", "0", "--filter", str(f)]
    )
    assert code == 0
    with JobStore(db_path) as store:
        assert store.count_jobs() == C.PAGE_SIZE


def test_cli_fetch_reports_browser_check(tmp_path, monkeypatch, capsys):
    """验自动补令牌后仍被拒时，提示用 CDP/Chrome 补救。"""
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: FakeHttp(
            [
                {"code": 37, "message": "你的浏览器环境异常", "zpData": {"seed": "s", "ts": 1, "name": "n"}},
                {"code": 37, "message": "你的浏览器环境异常", "zpData": {"seed": "s", "ts": 1, "name": "n"}},
            ]
        ),
    )
    f = tmp_path / "f.json"
    f.write_text('{"query": "python"}', encoding="utf-8")
    code = cli.main(
        ["--db", str(tmp_path / "x.db"), "fetch", "--filter", str(f)]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "__zp_stoken__" in err
    assert "CDP" in err
    assert "Chrome" in err


def test_cli_fetch_reports_risk_control(tmp_path, monkeypatch, capsys):
    """code 36 是账号风控，话术要明说「不去绕」。"""
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: FakeHttp(
            [{"code": 36, "message": "您的账户存在异常行为", "zpData": {}}]
        ),
    )
    f = tmp_path / "f.json"
    f.write_text('{"query": "python"}', encoding="utf-8")
    code = cli.main(
        ["--db", str(tmp_path / "x.db"), "fetch", "--filter", str(f)]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "风控" in err
    assert "不去绕" in err


class _Httplet:
    """只实现 crawl 要用的 request() 的极简会话。"""

    def __init__(self, request):
        """初始化。"""
        self._request = request
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookieJar()

    def request(self, method, url, **kwargs):
        """转发到构造时传入的 request。"""
        return self._request(method, url, **kwargs)


def _capturing_http(captured):
    """造一个会话，把每次请求的 url/params 记进 `captured`。"""

    def _request(method, url, **kwargs):
        """记下 URL/params 并回一页职位。"""
        captured["url"] = url
        captured["params"] = kwargs.get("params") or {}
        return FakeResponse(ok_page([api_item("j1")], has_more=False))

    return _Httplet(_request)


def test_cli_fetch_filter文件_有筛选就走搜索流(tmp_path, monkeypatch, capsys):
    """验 --filter JSON 有条件时走搜索流并把条件带进查询串。"""
    f = tmp_path / "f.json"
    f.write_text(
        '{"query": "python", "city": "101280100", "salary": "405"}', encoding="utf-8"
    )
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session", lambda *a, **k: _capturing_http(captured)
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1", "--filter", str(f)])
    assert code == 0
    assert "search/joblist" in captured["url"]
    assert captured["params"]["query"] == "python"
    assert captured["params"]["city"] == "101280100"
    assert captured["params"]["salary"] == "405"
    out = capsys.readouterr().out
    assert "搜索流" in out


def test_cli_fetch_库里的条件_走搜索流(tmp_path, monkeypatch, capsys):
    """不传 --filter 就用状态库里存的那份条件。"""
    import boss_db

    db = tmp_path / "d.db"
    boss_db.doc_set(
        boss_db.DOC_SEARCH_FILTER, {"query": "go", "city": "101010100"}, db, site="boss")
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session", lambda *a, **k: _capturing_http(captured)
    )
    code = cli.main(["--db", str(db), "fetch", "--max-pages", "1"])
    assert code == 0
    assert "search/joblist" in captured["url"]
    assert captured["params"]["query"] == "go"
    out = capsys.readouterr().out
    assert "搜索流" in out
    assert "库里的搜索条件" in out


def test_cli_fetch_两边都没有条件_报错(tmp_path, monkeypatch, capsys):
    """验库里条件全空且没给 --filter/--expect 时报错退出。"""
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session", lambda *a, **k: _capturing_http(captured)
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1"])
    assert code == cli.EXIT_USAGE
    assert captured == {}, "条件全空不该发请求"
    err = capsys.readouterr().err
    assert "搜索条件全空" in err
    assert "special/zone" in err


def _routing_http(captured, *, expects=None):
    """按 URL 路由：期望元数据回 expects，职位页回 ok_page 并记下参数。"""

    def _request(method, url, **kwargs):
        """按 URL 路由回响应并记下调用。"""
        captured["url"] = url
        captured.setdefault("calls", []).append({"url": url, "params": kwargs.get("params") or {}})
        if "expect/list" in url:
            return FakeResponse(expects if expects is not None else ok_expects())
        captured["params"] = kwargs.get("params") or {}
        return FakeResponse(ok_page([api_item("j1")], has_more=False))

    return _Httplet(_request)


def test_cli_fetch_expect_兼职(tmp_path, monkeypatch, capsys):
    """验 --expect part_time 走期望流，jobType=1903。"""
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: _routing_http(
            captured,
            expects=ok_expects(part_time=[{"encryptExpectId": "PT1", "positionName": "派单", "locationName": "佛山"}]),
        ),
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1", "--expect", "part_time"])
    assert code == 0
    assert "recommend/job/list" in captured["url"]
    assert captured["params"]["jobType"] == "1903"
    assert captured["params"]["encryptExpectId"] == "PT1"
    assert "query" not in captured["params"]
    out = capsys.readouterr().out
    assert "期望流" in out


def test_cli_fetch_expect_推荐(tmp_path, monkeypatch, capsys):
    """验 --expect recommend 走混合期望并带 mixExpectType。"""
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: _routing_http(
            captured,
            expects=ok_expects(
                mix={"encryptExpectId": "MIX1", "mixExpectType": "2", "positionName": "前端", "locationName": "佛山", "location": "101280800"}
            ),
        ),
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1", "--expect", "recommend"])
    assert code == 0
    assert "recommend/job/list" in captured["url"]
    assert captured["params"]["encryptExpectId"] == "MIX1"
    assert captured["params"]["mixExpectType"] == "2"
    assert captured["params"]["city"] == "101280800"


def test_cli_fetch_expect_指定期望(tmp_path, monkeypatch, capsys):
    """验 --expect expect:<id> 显式点名，不拉期望列表。"""
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: _routing_http(captured, expects=ok_expects()),
    )
    code = cli.main(
        ["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1", "--expect", "expect:E9"]
    )
    assert code == 0
    assert "recommend/job/list" in captured["url"]
    assert captured["params"]["encryptExpectId"] == "E9"

    assert all("expect/list" not in c["url"] for c in captured["calls"])


def test_cli_fetch_expect_临时期望(tmp_path, monkeypatch, capsys):
    """验 --expect temp:<file> 发 expectInfo 不发 encryptExpectId。"""
    info = tmp_path / "temp.json"
    info.write_text(
        '{"expectType":"1","position":"python","location":"101280100","lowSalary":"10","highSalary":"20"}',
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session",
        lambda *a, **k: _routing_http(captured, expects=ok_expects()),
    )
    code = cli.main(
        ["--db", str(tmp_path / "d.db"), "fetch", "--max-pages", "1", "--expect", f"temp:{info}"]
    )
    assert code == 0
    assert "recommend/job/list" in captured["url"]
    assert "expectInfo" in captured["params"]
    assert "encryptExpectId" not in captured["params"]


def test_cli_fetch_expect与filter互斥(tmp_path, capsys):
    """验 expect 与 filter 互斥，同时给就报错退出。"""
    f = tmp_path / "f.json"
    f.write_text('{"query": "python"}', encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--expect", "part_time", "--filter", str(f)])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "互斥" in err


def test_cli_fetch_expect_取值不认识就报错(tmp_path, monkeypatch, capsys):
    """验 --expect 取值不认识时报错退出。"""
    monkeypatch.setattr(
        "boss_jobs.client.http_from_session", lambda *a, **k: _routing_http({}, expects=ok_expects())
    )
    code = cli.main(["--db", str(tmp_path / "d.db"), "fetch", "--expect", "bogus"])
    assert code == 2
    err = capsys.readouterr().err
    assert "取值不认识" in err


def test_cli_list_and_stats(tmp_path, monkeypatch, capsys):
    """验 CLI list 与 stats 输出。"""
    db_path = tmp_path / "cli.db"
    with JobStore(db_path) as store:
        store.save_page(clean_page(ok_page([api_item("a", jobName="Python 开发", cityName="广州")]), page=1))

    assert cli.main(["--db", str(db_path), "list", "--city", "广州"]) == 0
    out = capsys.readouterr().out
    assert "Python 开发" in out

    assert cli.main(["--db", str(db_path), "stats", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["jobs"] == 1


def test_crawl_on_progress_fires_per_page(tmp_path):
    """每页入库后回调一次，payload 带上网页要的计数。"""
    pages = [
        ok_page([api_item("a1"), api_item("a2")], has_more=True),
        ok_page([api_item("b1")], has_more=False),
    ]
    client, _, _ = client_with(pages)
    events: list[dict] = []
    with JobStore(tmp_path / "p.db") as store:
        client.crawl(store=store, page_interval=0.0, on_progress=events.append, search_filter=_StubFilter())

    assert len(events) == 2
    assert events[0]["page"] == 1
    assert events[0]["kept_count"] == 2
    assert events[0]["inserted"] == 2
    assert events[1]["page"] == 2
    assert events[1]["has_more"] is False


def test_crawl_should_stop_breaks_before_next_page(tmp_path):
    """取消信号在翻页前生效，已入库的页保留。"""
    pages = [
        ok_page([api_item("a1")], has_more=True),
        ok_page([api_item("b1")], has_more=True),
        ok_page([api_item("c1")], has_more=True),
    ]
    client, http, _ = client_with(pages)
    calls = {"n": 0}

    def stop_after_first() -> bool:
        """第一页之后请求停止。"""
        return calls["n"] >= 1

    def counting(payload):
        """进度回调：累加页数。"""
        calls["n"] += 1

    with JobStore(tmp_path / "s.db") as store:
        report = client.crawl(
            store=store,
            page_interval=0.0,
            on_progress=counting,
            should_stop=stop_after_first,
            search_filter=_StubFilter(),
        )
        assert store.count_jobs() == 1

    assert len(http.calls) == 1
    assert "停止信号" in report.stats.stopped_reason


def test_crawl_without_hooks_still_works(tmp_path):
    """不传 on_progress / should_stop 时行为不变。"""
    client, _, _ = client_with([ok_page([])])
    with JobStore(tmp_path / "n.db") as store:
        report = client.crawl(store=store, page_interval=0.0, search_filter=_StubFilter())
    assert report.stats.pages == 1


def test_fetch_job_detail_hits_detail_endpoint():
    """验详情打 job/detail 路由并解析 JD。"""
    http = FakeHttp(
        [
            {
                "code": 0,
                "zpData": {
                    "jobInfo": {"postDescription": "岗位职责：\n写代码"},
                    "lid": "L1",
                },
            }
        ]
    )
    client = JobClient(http=http)
    detail = client.fetch_job_detail(security_id="SEC1", lid="L1")
    assert detail.job_desc == "岗位职责：\n写代码"
    assert detail.has_desc
    call = http.calls[0]
    assert call["method"] == "GET"
    assert "/wapi/zpgeek/job/detail.json" in call["url"]
    assert call["params"] == {"securityId": "SEC1", "lid": "L1"}


def test_fetch_job_detail_requires_security_id():
    """验缺 securityId 时不发请求。"""
    client = JobClient(http=FakeHttp([]))
    with pytest.raises(ValueError):
        client.fetch_job_detail(security_id="", lid="L1")


def test_fetch_job_detail_ensures_stoken():
    """验详情与搜索一样先 ensure `__zp_stoken__`。"""
    tokens = []

    class FakeProvider:
        """记录 ensure 并回固定令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return "TOKEN-1"

    http = FakeHttp(
        [
            {"code": 0, "zpData": {"jobInfo": {"postDescription": "JD"}}},
        ]
    )
    client = JobClient(http=http, stoken_provider=FakeProvider())
    detail = client.fetch_job_detail(security_id="SEC1", lid="L1")
    assert detail.job_desc == "JD"
    assert tokens == [False]
    assert http.cookies.get("__zp_stoken__") == "TOKEN-1"


def test_fetch_job_detail_撞37先歇会儿再用同一枚重试():
    """验 37 先退避拿同一枚重试，不急着换新。"""
    from boss_jobs import config as C

    tokens = []

    class OnceProvider:
        """只回一枚令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return "TOKEN-1"

    http = FakeHttp(
        [
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            {"code": 0, "zpData": {"jobInfo": {"postDescription": "歇完再要到的 JD"}}},
        ]
    )
    sleeper = RecordingSleeper()
    client = JobClient(http=http, stoken_provider=OnceProvider(), sleeper=sleeper)
    detail = client.fetch_job_detail(security_id="SEC1", lid="L1")
    assert detail.job_desc == "歇完再要到的 JD"
    assert tokens == [False]
    assert sleeper.calls == [C.BROWSER_CHECK_BACKOFF]
    assert len(http.calls) == 2


def test_fetch_job_detail_歇完还37才强制换新():
    """验歇完仍 37 才强制换新令牌。"""
    tokens = []

    class CountingProvider:
        """每次 ensure 换一枚递增令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return f"TOKEN-{len(tokens)}"

    http = FakeHttp(
        [
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            {"code": 0, "zpData": {"jobInfo": {"postDescription": "补令牌后的 JD"}}},
        ]
    )
    client = JobClient(http=http, stoken_provider=CountingProvider(), sleeper=lambda _s: None)
    detail = client.fetch_job_detail(security_id="SEC1", lid="L1")
    assert detail.job_desc == "补令牌后的 JD"
    assert tokens == [False, True]
    assert len(http.calls) == 3


def test_enrich_page_details_survives_single_failure(tmp_path):
    """验单条详情失败只记流水，下一条继续。"""
    from boss_jobs.models import Job, PageResult

    jobs = (
        Job.from_api({"encryptJobId": "a", "jobName": "A", "brandName": "甲", "securityId": "s-a", "lid": "l"}),
        Job.from_api({"encryptJobId": "b", "jobName": "B", "brandName": "乙", "securityId": "s-b", "lid": "l"}),
        Job.from_api({"encryptJobId": "c", "jobName": "C", "brandName": "丙", "securityId": "s-c", "lid": "l"}),
    )
    page = PageResult(page=1, jobs=jobs, has_more=False, raw_count=3)

    http = FakeHttp(
        [
            {"code": 0, "zpData": {"jobInfo": {"postDescription": "A 的 JD"}}},
            JobApiError(36, "账号异常", raw={}),
            {"code": 0, "zpData": {"jobInfo": {"postDescription": "C 的 JD"}}},
        ]
    )
    client = JobClient(http=http, sleeper=lambda _s: None)
    events = []
    with JobStore(tmp_path / "jobs.db") as store:
        store.save_page(page)
        client._enrich_page_details(
            page,
            store=store,
            interval=0.0,
            on_detail=events.append,
        )

        assert [e["event"] for e in events] == ["detail_done", "detail_error", "detail_done"]
        assert store.get_job("a").job_desc == "A 的 JD"
        assert store.get_job("b").job_desc == ""
        assert store.get_job("c").job_desc == "C 的 JD"


def test_enrich_page_details_已有描述不再重复获取(tmp_path):
    """重抓一页时，已入库且已有 JD 的不该再打详情接口。

    只给 b 准备一条响应——a / c 已有描述，不该再发请求。
    """
    from boss_jobs.models import Job, PageResult

    jobs = (
        Job.from_api({"encryptJobId": "a", "jobName": "A", "brandName": "甲", "securityId": "s-a", "lid": "l"}),
        Job.from_api({"encryptJobId": "b", "jobName": "B", "brandName": "乙", "securityId": "s-b", "lid": "l"}),
        Job.from_api({"encryptJobId": "c", "jobName": "C", "brandName": "丙", "securityId": "s-c", "lid": "l"}),
    )
    page = PageResult(page=1, jobs=jobs, has_more=False, raw_count=3)

    http = FakeHttp(
        [
            {"code": 0, "zpData": {"jobInfo": {"postDescription": "B 的 JD"}}},
        ]
    )
    client = JobClient(http=http, sleeper=lambda _s: None)
    events = []
    with JobStore(tmp_path / "jobs.db") as store:
        store.save_page(page)
        store.update_job_desc("a", "A 早抓过了", fetched_at="2026-10-08 10:00:00")
        store.update_job_desc("c", "C 也早抓过了", fetched_at="2026-10-08 10:00:00")

        client._enrich_page_details(page, store=store, interval=0.0, on_detail=events.append)

        kinds = [e["event"] for e in events]
        assert kinds == ["detail_skipped", "detail_done", "detail_skipped"]
        assert len(http.calls) == 1
        assert store.get_job("a").job_desc == "A 早抓过了"
        assert store.get_job("b").job_desc == "B 的 JD"
        assert store.get_job("c").job_desc == "C 也早抓过了"


def test_fetch_job_detail_换新冷却没换到_不再打第三发():
    """验 force 冷却没换到新令牌时不再打第三发。"""
    tokens = []

    class CooldownProvider:
        """假装换新冷却中——force 也只回同一枚令牌。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return "TOKEN-SAME"

    http = FakeHttp(
        [
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
        ]
    )
    client = JobClient(http=http, stoken_provider=CooldownProvider(), sleeper=lambda _s: None)
    with pytest.raises(JobApiError) as excinfo:
        client.fetch_job_detail(security_id="SEC1", lid="L1")
    assert excinfo.value.is_browser_check
    assert tokens == [False, True]
    assert len(http.calls) == 2


def test_enrich_page_details_连环撞37就停批(tmp_path):
    """验连环撞 N 次 code 37 后停批，不再补 JD。"""
    from boss_jobs import config as C
    from boss_jobs.models import Job, PageResult

    jobs = tuple(
        Job.from_api(
            {"encryptJobId": f"{name}", "jobName": name, "brandName": "甲",
             "securityId": f"s-{name}", "lid": "l"}
        )
        for name in ("a", "b", "c", "d", "e")
    )
    page = PageResult(page=1, jobs=jobs, has_more=False, raw_count=len(jobs))

    responses = [
        {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
    ] * 20

    class SameTokenProvider:
        """force 也只回同一枚令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            return "TOKEN-SAME"

    http = FakeHttp(responses)
    client = JobClient(
        http=http, stoken_provider=SameTokenProvider(), sleeper=lambda _s: None
    )
    events = []
    with JobStore(tmp_path / "jobs.db") as store:
        store.save_page(page)
        client._enrich_page_details(
            page, store=store, interval=0.0, on_detail=events.append
        )

        kinds = [e["event"] for e in events]
        assert kinds.count("detail_error") == C.BROWSER_CHECK_GIVEUP
        assert kinds[-1] == "detail_stopped"
        assert kinds.count("detail_done") == 0
        assert len(http.calls) == C.BROWSER_CHECK_GIVEUP * 2


def test_enrich_page_details_37失败后多躺一会(tmp_path):
    """验单条 37 后下一条前多睡 COOLOFF，且至少睡过 BACKOFF。"""
    from boss_jobs import config as C
    from boss_jobs.models import Job, PageResult

    jobs = (
        Job.from_api({"encryptJobId": "a", "jobName": "A", "brandName": "甲", "securityId": "s-a", "lid": "l"}),
        Job.from_api({"encryptJobId": "b", "jobName": "B", "brandName": "乙", "securityId": "s-b", "lid": "l"}),
    )
    page = PageResult(page=1, jobs=jobs, has_more=False, raw_count=2)

    class SameTokenProvider:
        """force 也只回同一枚令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            return "TOKEN-SAME"

    http = FakeHttp(
        [

            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},

            {"code": 0, "zpData": {"jobInfo": {"postDescription": "B 的 JD"}}},
        ]
    )
    sleeper = RecordingSleeper()
    client = JobClient(
        http=http, stoken_provider=SameTokenProvider(), sleeper=sleeper
    )
    events = []
    with JobStore(tmp_path / "jobs.db") as store:
        store.save_page(page)
        client._enrich_page_details(
            page, store=store, interval=0.0, on_detail=events.append
        )

        kinds = [e["event"] for e in events]
        assert kinds == ["detail_error", "detail_done"]
        assert store.get_job("b").job_desc == "B 的 JD"
        assert C.BROWSER_CHECK_BACKOFF in sleeper.calls
        assert C.BROWSER_CHECK_COOLOFF in sleeper.calls


def test_greet_posts_form_with_query_params():
    """验打招呼 POST friend/add.json，query/body 参数各就各位。"""
    http = FakeHttp([{"code": 0, "message": "Success", "zpData": {}}])
    client = JobClient(http=http)
    result = client.greet(
        security_id="SEC1",
        encrypt_job_id="J1",
        lid="L1",
        encrypt_boss_id="BOSS1",
        session_id="SESS1",
    )
    assert result.message == "Success"
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/wapi/zpgeek/friend/add.json")
    assert call["params"] == {"securityId": "SEC1", "jobId": "J1", "lid": "L1"}
    assert call["data"] == {"encryptBossId": "BOSS1", "sessionId": "SESS1"}


def test_greet_不带的可选字段就不发():
    """lid / encryptBossId / sessionId 都能空——空了就不进 query / body。"""
    http = FakeHttp([{"code": 0, "message": "Success", "zpData": {}}])
    client = JobClient(http=http)
    client.greet(security_id="SEC1", encrypt_job_id="J1")
    call = http.calls[0]
    assert call["params"] == {"securityId": "SEC1", "jobId": "J1"}
    assert call["data"] is None


def test_greet_这条接口没有招呼语参数():
    """验 greet 不接受 greeting 入参——正文走 `JobClient.deliver_greeting`。"""
    import inspect

    params = inspect.signature(JobClient.greet).parameters
    assert "greeting" not in params
    with pytest.raises(TypeError):
        JobClient(http=FakeHttp([])).greet(
            security_id="SEC1", encrypt_job_id="J1", greeting="您好"
        )


def test_greet_extra_透传非空字段():
    """extra 里的字段透传进 body；值为 None 的丢掉。"""
    http = FakeHttp([{"code": 0, "message": "Success", "zpData": {}}])
    client = JobClient(http=http)
    client.greet(
        security_id="SEC1",
        encrypt_job_id="J1",
        extra={"expectId": "E1", "drop": None},
    )
    assert http.calls[0]["data"] == {"expectId": "E1"}


def _chat_remind_payload(content: str = "您今天已与120位BOSS沟通，还剩30次沟通机会哦") -> dict:
    """构造「开聊提醒」弹窗的真实形态响应。"""
    return {
        "code": 1,
        "message": "开聊提醒",
        "zpData": {
            "bizCode": 1,
            "bizMessage": "开聊提醒",
            "bizData": {
                "chatRemindDialog": {
                    "actionType": 1,
                    "ba": "%7B%22action%22%3A%22addf-limit-popup-c%22%7D",
                    "title": "温馨提示",
                    "content": content,
                    "buttonList": [
                        {
                            "text": "好",
                            "actionType": 11,
                            "ba": "%7B%22action%22%3A%22server-remind-detail-boss-click%22%7D",
                        }
                    ],
                    "remindType": 524288,
                    "blockLevel": 0,
                }
            },
        },
    }


def test_greet_开聊提醒_模拟点击确认_cid1_后建会话():
    """验「开聊提醒」自动模拟点「好」：埋点 → friend/add 带 cid=1 → 埋点。"""
    http = FakeHttp(
        [
            _chat_remind_payload(),
            {"code": 0, "message": "Success", "zpData": True},
            {"code": 0, "message": "Success", "zpData": {"securityId": "SEC-NEW"}},
            {"code": 0, "message": "Success", "zpData": True},
        ]
    )
    client = JobClient(http=http)
    result = client.greet(
        security_id="SEC1",
        encrypt_job_id="J1",
        lid="L1",
        encrypt_boss_id="BOSS1",
    )

    assert result.raw["code"] == 0
    assert [c["url"].rsplit("/", 1)[-1] for c in http.calls] == [
        "add.json",
        "chatremind.json",
        "add.json",
        "chatremind.json",
    ]

    first, log_c, confirm, log_connect = http.calls

    assert first["data"] == {"encryptBossId": "BOSS1"}

    assert log_c["data"]["action"] == "addf-limit-popup-c"
    assert "ba" in log_c["data"]

    assert confirm["data"]["cid"] == 1
    assert confirm["data"] == {"encryptBossId": "BOSS1", "cid": 1}
    assert confirm["params"] == {"securityId": "SEC1", "jobId": "J1", "lid": "L1"}

    assert log_connect["data"]["action"] == "addf-limit-popup-connect"
    assert log_connect["data"]["p8"] == "11"


def test_greet_开聊提醒_确认后仍被拦才抛原弹窗():
    """验确认后仍被拦时抛原弹窗话术。"""
    http = FakeHttp(
        [
            _chat_remind_payload(),
            {"code": 0, "message": "Success", "zpData": True},
            _chat_remind_payload(),
            {"code": 0, "message": "Success", "zpData": True},
        ]
    )
    client = JobClient(http=http)
    with pytest.raises(JobApiError) as ei:
        client.greet(security_id="SEC1", encrypt_job_id="J1", encrypt_boss_id="BOSS1")
    assert ei.value.is_chat_remind
    assert "还剩30次沟通机会" in ei.value.message
    assert ei.value.chat_remind_remaining == 30


def test_greet_开聊提醒_埋点失败不挡确认():
    """埋点是锦上添花；挂了也照样带 cid=1 重打，别把确认一起吹掉。"""
    http = FakeHttp(
        [
            _chat_remind_payload(),
            JobTransportError("埋点挂了"),
            {"code": 0, "message": "Success", "zpData": {}},
            {"code": 0, "message": "Success", "zpData": True},
        ]
    )
    client = JobClient(http=http, retries=0)
    result = client.greet(security_id="SEC1", encrypt_job_id="J1", encrypt_boss_id="BOSS1")
    assert result.raw["code"] == 0
    assert http.calls[2]["data"]["cid"] == 1


def test_chat_remind_remaining_抠还剩次数():
    """从弹窗话术里抠「还剩 N 次」；抠不出来回 None（继续发，别瞎停批）。"""
    from boss_jobs.errors import chat_remind_remaining

    assert chat_remind_remaining(_chat_remind_payload()) == 30
    assert chat_remind_remaining("您今天已与150位BOSS沟通，还剩0次沟通机会哦") == 0
    assert chat_remind_remaining("开聊提醒") is None
    assert chat_remind_remaining(None) is None


def test_chat_limit_exhausted_recognizes_tomorrow_message_only():
    """明确的休息至明天话术表示额度耗尽，正常配额提醒仍不算耗尽。"""
    message = "您今天已与150位BOSS沟通，休息一下，明天再来吧"
    exhausted = JobApiError(1, message, raw=_chat_remind_payload(message))
    available = JobApiError(1, "开聊提醒", raw=_chat_remind_payload())
    unknown = JobApiError(1, "开聊提醒")

    assert exhausted.is_chat_limit_exhausted
    assert not available.is_chat_limit_exhausted
    assert not unknown.is_chat_limit_exhausted


def test_greet_does_not_confirm_when_daily_limit_is_exhausted():
    """验额度已尽时不再模拟确认。"""
    message = "您今天已与150位BOSS沟通，休息一下，明天再来吧～"
    http = FakeHttp([_chat_remind_payload(message)])

    with pytest.raises(JobApiError) as ei:
        JobClient(http=http, retries=0).greet(
            security_id="SEC1", encrypt_job_id="J1", encrypt_boss_id="BOSS1"
        )

    assert ei.value.is_chat_limit_exhausted
    assert len(http.calls) == 1


def test_chat_rate_limit_phrase_recognizes_real_api_error():
    """验「操作过于频繁」标成聊天限速。"""
    err = JobApiError(1, "您的操作过于频繁，请稍后再试")

    assert err.is_chat_rate_limited
    assert not err.is_chat_limit_exhausted


@pytest.mark.parametrize(
    ("security_id", "encrypt_job_id"),
    [("", "J1"), ("SEC1", "")],
)
def test_greet_缺关键参数直接报错(security_id, encrypt_job_id):
    """securityId / jobId 是必填，缺一个就别浪费一发请求。"""
    http = FakeHttp([])
    client = JobClient(http=http)
    with pytest.raises(ValueError):
        client.greet(security_id=security_id, encrypt_job_id=encrypt_job_id)
    assert http.calls == []


def test_greet_code36_标记成账号风控():
    """验 code 36 标成账号风控，交上层停整批。"""
    http = FakeHttp([{"code": 36, "message": "您的账户存在异常行为", "zpData": {}}])
    client = JobClient(http=http)
    with pytest.raises(JobApiError) as ei:
        client.greet(security_id="SEC1", encrypt_job_id="J1")
    assert ei.value.code == 36
    assert ei.value.is_risk_control
    assert not ei.value.is_browser_check


def test_greet_code37_标记成浏览器校验():
    """验 greet 的 code 37 标成浏览器校验。"""
    http = FakeHttp([{"code": 37, "message": "您的环境存在异常.", "zpData": {}}])
    client = JobClient(http=http)
    with pytest.raises(JobApiError) as ei:
        client.greet(security_id="SEC1", encrypt_job_id="J1")
    assert ei.value.is_browser_check


def test_greet_撞37先歇会儿再用同一枚重试():
    """验 greet 与搜索/详情同一套 37 退避重试。"""
    from boss_jobs import config as C

    tokens = []

    class OnceProvider:
        """只回一枚令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return "TOKEN-1"

    http = FakeHttp(
        [
            {"code": 37, "message": "您的环境存在异常.", "zpData": {}},
            {"code": 0, "message": "Success", "zpData": {}},
        ]
    )
    sleeper = RecordingSleeper()
    client = JobClient(http=http, stoken_provider=OnceProvider(), sleeper=sleeper)
    client.greet(security_id="SEC1", encrypt_job_id="J1")
    assert tokens == [False]
    assert sleeper.calls == [C.BROWSER_CHECK_BACKOFF]
    assert [c["method"] for c in http.calls] == ["POST", "POST"]


def test_greet_挂了stoken也能发():
    """greet 跟搜索一样会先 ensure 令牌，cookie 里要出现 __zp_stoken__。"""
    tokens = []

    class FakeProvider:
        """记录 ensure 并回固定令牌的 stoken 桩。"""

        def ensure(self, *, force: bool = False):
            """回令牌并记下是否强制换新。"""
            tokens.append(force)
            return "TOKEN-G"

    http = FakeHttp([{"code": 0, "message": "Success", "zpData": {}}])
    client = JobClient(http=http, stoken_provider=FakeProvider())
    client.greet(security_id="SEC1", encrypt_job_id="J1")
    assert tokens == [False]
    assert http.cookies.get("__zp_stoken__") == "TOKEN-G"
