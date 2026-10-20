"""`__zp_stoken__` 全自动获取单测：挑战解析 / 脚本缓存 / 算令牌 / 自动重试。

令牌算法见 `boss_jobs.stoken`。真 Node 冒烟用例单独一个，没装 Node 就跳过。

运行：`python -m pytest tests/test_jobs_stoken.py -q`
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from boss_jobs.client import JobClient
from boss_jobs.errors import JobApiError
from boss_jobs.stoken import (
    RUNNER_PATH,
    SAMPLE_CHALLENGE,
    StokenChallenge,
    StokenError,
    StokenProvider,
    compute_stoken,
    find_node,
    load_security_js,
    mint_offline,
    parse_challenge,
    security_js_path,
)

REAL_SCRIPT = Path(__file__).resolve().parent.parent / ".scratch" / "sec_e948d594.js"


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
        return self._payload


class FakeCookieJar:
    """假 cookie 罐，支持 set / get。"""

    def __init__(self) -> None:
        """初始化。"""
        self._data: dict[str, str] = {}

    def set(self, name, value, **_kwargs):
        """写入一枚 cookie。"""
        self._data[name] = value

    def get(self, name, default=None):
        """按名取 cookie，缺失时回默认值。"""
        return self._data.get(name, default)


class FakeHttp:
    """假 HTTP 会话：`get()` 给 StokenProvider，`request()` 给 JobClient。"""

    def __init__(self, get_responses=(), request_responses=()) -> None:
        """初始化。"""
        self._gets = list(get_responses)
        self._reqs = list(request_responses)
        self.get_calls: list[dict] = []
        self.request_calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookieJar()

    def get(self, url, **kwargs):
        """记录一次 GET 并返回下一条预置响应。"""
        self.get_calls.append({"url": url, **kwargs})
        if not self._gets:
            raise AssertionError(f"没有预置 GET 响应：{url}")
        item = self._gets.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    def request(self, method, url, **kwargs):
        """记录一次调用并返回下一条预置响应。"""
        self.request_calls.append({"method": method, "url": url, **kwargs})
        if not self._reqs:
            raise AssertionError(f"没有预置响应：{url}")
        item = self._reqs.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


class FakeProvider:
    """不打网络、不起 Node 的桩：只验 JobClient 的重试编排。"""

    def __init__(self, token: str = "0138FAKE") -> None:
        """初始化。"""
        self.token = token
        self.ensure_calls: list[bool] = []

    def ensure(self, *, force: bool = False) -> str:
        """记录 force 标记并回固定令牌。"""
        self.ensure_calls.append(force)
        return self.token


def challenge_payload(seed="Em6sUKm1q2j+AAA=", name="e948d594", ts=1790759239936):
    """构造 code 37 挑战响应体。"""
    return {
        "code": 37,
        "message": "您的环境存在异常.",
        "zpData": {"seed": seed, "name": name, "ts": ts},
    }


def test_parse_challenge_三样齐全():
    """验三样齐全时解出完整挑战。"""
    c = parse_challenge(challenge_payload())
    assert isinstance(c, StokenChallenge)
    assert (c.seed, c.name, c.ts) == ("Em6sUKm1q2j+AAA=", "e948d594", 1790759239936)
    assert c.is_complete


def test_parse_challenge_ts_是字符串也收():
    """验 ts 给字符串时也能解析成整数。"""
    c = parse_challenge(challenge_payload(ts="1790759239936"))
    assert c is not None and c.ts == 1790759239936


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"code": 37, "message": "x"},
        {"code": 37, "zpData": None},
        {"code": 37, "zpData": {"seed": "s", "ts": 1}},
        {"code": 37, "zpData": {"name": "n", "ts": 1}},
        {"code": 37, "zpData": {"seed": "s", "name": "n"}},
        {"code": 37, "zpData": {"seed": "s", "name": "n", "ts": "abc"}},
        {"code": 37, "zpData": {"seed": "s", "name": "n", "ts": 0}},
        {"code": 37, "zpData": {"seed": "s", "name": "n", "ts": -5}},
    ],
)
def test_parse_challenge_不齐就返回None(payload):
    """验任一挑战字段缺失或非法时返回 `None`。"""
    assert parse_challenge(payload) is None


def test_stoken_challenge_ts_0_不算完整():
    """验 ts=0 时挑战不算完整。"""
    c = StokenChallenge(seed="s", name="n", ts=0)
    assert not c.is_complete


def test_load_security_js_下载一次之后走缓存(tmp_path):
    """验 security-js 下载一次后走本地缓存。"""
    http = FakeHttp(get_responses=[FakeResponse(text="window.ABC=class{}")])
    first = load_security_js(http, "e948d594", cache_dir=tmp_path)
    assert "ABC" in first
    assert security_js_path("e948d594", tmp_path).read_text(encoding="utf-8") == first

    second = load_security_js(http, "e948d594", cache_dir=tmp_path)
    assert second == first
    assert http.get_calls == [{"url": "https://www.zhipin.com/web/common/security-js/e948d594.js", "timeout": 10.0}]


def test_load_security_js_force_绕过缓存(tmp_path):
    """验 force=True 时强制重新下载。"""
    http = FakeHttp(
        get_responses=[
            FakeResponse(text="/* v1 */"),
            FakeResponse(text="/* v2 */"),
        ]
    )
    assert load_security_js(http, "n", cache_dir=tmp_path) == "/* v1 */"
    assert load_security_js(http, "n", cache_dir=tmp_path, force=True) == "/* v2 */"


def test_load_security_js_非200_报错(tmp_path):
    """验非 200 响应抛 `StokenError`。"""
    http = FakeHttp(get_responses=[FakeResponse(text="", status=404)])
    with pytest.raises(StokenError, match="拿不到"):
        load_security_js(http, "gone", cache_dir=tmp_path)


def test_security_js_path_把怪字符洗掉(tmp_path):
    """验文件名中的怪字符被洗成下划线。"""
    p = security_js_path("a/b\\c:d", tmp_path)
    assert p.name == "a_b_c_d.js"
    assert p.parent == tmp_path


def test_compute_stoken_喂给Node的命令行对(monkeypatch, tmp_path):
    """验脚本路径 / seed / ts / 时区都送进了 runner，且 ts 原样传入。"""
    seen = {}

    def fake_run(cmd, **kwargs):
        """记录命令行并回桩产出。"""
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=b"0138stub", stderr=b"")

    monkeypatch.setattr("boss_jobs.stoken.subprocess.run", fake_run)
    monkeypatch.setattr("boss_jobs.stoken.find_node", lambda: "node")
    monkeypatch.setattr("boss_jobs.stoken._local_js_timezone_offset", lambda: -480)

    token = compute_stoken("/* js */", seed="SEED", ts=1790759239936, name="n1", cache_dir=tmp_path)

    assert token == "0138stub"
    cmd = seen["cmd"]
    assert cmd[0] == "node"
    assert cmd[1] == str(RUNNER_PATH)
    assert cmd[2].endswith("n1.js") and Path(cmd[2]).read_text(encoding="utf-8") == "/* js */"
    assert cmd[3] == "SEED"
    assert int(cmd[4]) == 1790759239936
    assert int(cmd[5]) == -480


STUB_ECHO_TS = """
global.ABC = class { z(seed, ts) { return "TS=" + ts; } };
"""


def test_run_abc_按前端公式调_z(tmp_path):
    """验 runner 按前端公式折算时区后再调 `z`。"""
    try:
        find_node()
    except StokenError:
        pytest.skip("本机没有 node")

    ts = 1790759239936

    def run(tz: int) -> str:
        """按给定时区偏移算一枚回显 ts 的伪令牌。"""
        return compute_stoken(
            STUB_ECHO_TS, seed="S", ts=ts, name="echo", cache_dir=tmp_path, tz_offset_minutes=tz
        )

    assert run(-480) == f"TS={ts}"
    assert run(0) == f"TS={ts + 480 * 60000}"
    assert run(60) == f"TS={ts + (480 + 60) * 60000}"


def test_compute_stoken_node失败带stderr(monkeypatch, tmp_path):
    """验 Node 失败时 `StokenError` 带上 stderr。"""

    def fake_run(cmd, **kwargs):
        """回一个退出码 1 的进程结果。"""
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr("boss_jobs.stoken.subprocess.run", fake_run)
    monkeypatch.setattr("boss_jobs.stoken.find_node", lambda: "node")

    with pytest.raises(StokenError, match="boom"):
        compute_stoken("/* js */", seed="S", ts=1, name="n", cache_dir=tmp_path)


def test_find_node_认环境变量(monkeypatch):
    """验 `BOSS_NODE_BIN` 优先生效。"""
    monkeypatch.setenv("BOSS_NODE_BIN", r"D:\node\node.exe")
    assert find_node() == r"D:\node\node.exe"


def test_mint_offline_本地没缓存就报清楚(tmp_path):
    """验无本地缓存时给出明确报错。"""
    with pytest.raises(StokenError, match="没有 security-js 缓存"):
        mint_offline(cache_dir=tmp_path)


def test_mint_offline_纯算法不打网络(tmp_path):
    """验样例挑战加本地脚本即可铸币，全程不起 HTTP。"""
    try:
        find_node()
    except StokenError:
        pytest.skip("本机没有 node")

    script = security_js_path(SAMPLE_CHALLENGE.name, tmp_path)
    script.write_text(STUB_ECHO_TS, encoding="utf-8")

    token = mint_offline(cache_dir=tmp_path)

    assert token.startswith("TS=")
    assert SAMPLE_CHALLENGE.is_complete


@pytest.mark.skipif(not REAL_SCRIPT.exists(), reason="没有本地 security-js 样本")
def test_compute_stoken_真Node出0138前缀(tmp_path):
    """验真实脚本产出 0138 前缀且同 seed+ts 连算两次不同。"""
    try:
        find_node()
    except StokenError:
        pytest.skip("本机没有 node")

    js = REAL_SCRIPT.read_text(encoding="utf-8")
    a = compute_stoken(js, seed="Em6sUKm1q2j+AAA=", ts=1790759239936, name="e948d594", cache_dir=tmp_path)
    b = compute_stoken(js, seed="Em6sUKm1q2j+AAA=", ts=1790759239936, name="e948d594", cache_dir=tmp_path)

    for tok in (a, b):
        assert tok.startswith("0138")
        assert len(tok) > 100
    assert a != b


def test_provider_拿挑战下脚本算令牌并写cookie(tmp_path, monkeypatch):
    """验一条龙后写 cookie，第二次 ensure 直接复用。"""
    monkeypatch.setattr("boss_jobs.stoken.compute_stoken", lambda *a, **k: "0138MINTED")
    http = FakeHttp(
        get_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse(text="/* security-js */"),
        ]
    )
    provider = StokenProvider(http=http, cache_dir=tmp_path)

    token = provider.ensure()

    assert token == "0138MINTED"
    assert http.cookies.get("__zp_stoken__") == "0138MINTED"
    assert provider.ensure() == "0138MINTED"
    assert len(http.get_calls) == 2


def test_provider_ensure_force_会重算(tmp_path, monkeypatch):
    """验 force=True 时强制重算并覆盖 cookie。"""
    monkeypatch.setattr("boss_jobs.stoken.compute_stoken", lambda *a, **k: "0138NEW")
    http = FakeHttp(
        get_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse(text="/* js */"),
            FakeResponse(challenge_payload()),
        ]
    )
    provider = StokenProvider(http=http, cache_dir=tmp_path)
    provider.ensure()
    assert provider.ensure(force=True) == "0138NEW"
    assert http.cookies.get("__zp_stoken__") == "0138NEW"


def test_provider_接口不给挑战就把码说清楚(tmp_path):
    """验接口不给挑战时错误里带上业务码。"""
    http = FakeHttp(get_responses=[FakeResponse({"code": 36, "message": "您的账户存在异常行为."})])
    with pytest.raises(StokenError, match="code=36"):
        StokenProvider(http=http, cache_dir=tmp_path).fetch_challenge()


def test_provider_会话不支持cookie就报错(tmp_path, monkeypatch):
    """验会话缺 `cookies.set` 时抛错。"""
    monkeypatch.setattr("boss_jobs.stoken.compute_stoken", lambda *a, **k: "0138X")
    http = FakeHttp(
        get_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse(text="/* js */"),
        ]
    )
    http.cookies = object()
    provider = StokenProvider(http=http, cache_dir=tmp_path)
    with pytest.raises(StokenError, match="cookies.set"):
        provider.ensure()


def test_provider_apply_用requests认的expires():
    """验 apply 写入的 cookie 使用 `expires` 而不是 `max_age`。"""
    import time as _time

    from requests.cookies import RequestsCookieJar

    class _Http:
        """只带 cookie 罐的极简会话桩。"""

        cookies = RequestsCookieJar()

    http = _Http()
    provider = StokenProvider(http=http, cache_dir=None)
    before = int(_time.time())
    provider.apply("0138REAL")

    cookie = next(iter(http.cookies))
    assert cookie.name == "__zp_stoken__" and cookie.value == "0138REAL"
    assert cookie.domain == ".zhipin.com" and cookie.path == "/"
    assert before + 230000 <= cookie.expires <= before + 231000


def test_fetch_search_page_撞37_自动补令牌重试():
    """验撞 37 后退避重试仍失败时强制换新令牌再打。"""

    class RenewingProvider:
        """force=True 时回一枚新令牌。"""

        def __init__(self) -> None:
            """初始化。"""
            self.ensure_calls: list[bool] = []

        def ensure(self, *, force: bool = False) -> str:
            """按 force 回旧或新令牌。"""
            self.ensure_calls.append(force)
            return "0138NEW" if force else "0138OLD"

    provider = RenewingProvider()
    http = FakeHttp(
        request_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse(challenge_payload()),
            FakeResponse({"code": 0, "zpData": {"jobList": [], "hasMore": False}}),
        ]
    )
    client = JobClient(
        base_url="https://example.test",
        http=http,
        stoken_provider=provider,
        retries=0,
        sleeper=lambda _s: None,
    )

    class _F:
        """假筛选参数。"""

        def to_params(self):
            """返回固定查询参数。"""
            return {"query": "python", "page": 1}

        def for_page(self, page):
            """返回自身，忽略页码。"""
            return self

    result = client.fetch_search_page(_F())

    assert result.raw_count == 0
    assert provider.ensure_calls == [False, True]
    assert http.cookies.get("__zp_stoken__") == "0138NEW"
    assert len(http.request_calls) == 3


def test_fetch_search_page_撞37_换新没换到_不再打第三发():
    """验换新仍是同一枚时不再多打一发请求。"""
    provider = FakeProvider(token="0138SAME")
    http = FakeHttp(
        request_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse(challenge_payload()),
        ]
    )
    client = JobClient(
        base_url="https://example.test",
        http=http,
        stoken_provider=provider,
        retries=0,
        sleeper=lambda _s: None,
    )

    class _F:
        """假筛选参数。"""

        def to_params(self):
            """返回固定查询参数。"""
            return {"query": "python", "page": 1}

        def for_page(self, page):
            """返回自身，忽略页码。"""
            return self

    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_F())
    assert excinfo.value.is_browser_check
    assert provider.ensure_calls == [False, True]
    assert len(http.request_calls) == 2


def test_fetch_search_page_撞37_歇会儿就好了_不换新():
    """验退避重试成功时不强制换新令牌。"""
    provider = FakeProvider(token="0138KEEP")
    http = FakeHttp(
        request_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse({"code": 0, "zpData": {"jobList": [], "hasMore": False}}),
        ]
    )
    client = JobClient(
        base_url="https://example.test",
        http=http,
        stoken_provider=provider,
        retries=0,
        sleeper=lambda _s: None,
    )

    class _F:
        """假筛选参数。"""

        def to_params(self):
            """返回固定查询参数。"""
            return {"query": "python", "page": 1}

        def for_page(self, page):
            """返回自身，忽略页码。"""
            return self

    result = client.fetch_search_page(_F())

    assert result.raw_count == 0
    assert provider.ensure_calls == [False]
    assert len(http.request_calls) == 2


def test_fetch_search_page_撞37_没挂provider_照旧报错():
    """验未挂 provider 时撞 37 直接报浏览器校验错。"""
    http = FakeHttp(request_responses=[FakeResponse(challenge_payload())])
    client = JobClient(base_url="https://example.test", http=http, retries=0)

    class _F:
        """假筛选参数。"""

        def to_params(self):
            """返回只含页码的参数。"""
            return {"page": 1}

        def for_page(self, page):
            """返回自身，忽略页码。"""
            return self

    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_F())
    assert excinfo.value.is_browser_check
    assert len(http.request_calls) == 1


def test_fetch_search_page_撞36_不强制换新():
    """验账号风控码 36 不会触发 force 换新。"""
    provider = FakeProvider()
    http = FakeHttp(
        request_responses=[FakeResponse({"code": 36, "message": "您的账户存在异常行为."})]
    )
    client = JobClient(base_url="https://example.test", http=http, stoken_provider=provider, retries=0)

    class _F:
        """假筛选参数。"""

        def to_params(self):
            """返回只含页码的参数。"""
            return {"page": 1}

        def for_page(self, page):
            """返回自身，忽略页码。"""
            return self

    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_F())
    assert excinfo.value.is_risk_control
    assert provider.ensure_calls == [False]
