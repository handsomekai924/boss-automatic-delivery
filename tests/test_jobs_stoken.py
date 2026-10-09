"""``__zp_stoken__`` 全自动获取：挑战解析 / 脚本缓存 / 算令牌 / 自动重试。

令牌的来源与算法见 :mod:`boss_jobs.stoken` 的模块 docstring。这里验的是
「接口回 code 37 → 解出挑战 → 拿 ``security-js`` → ``ABC.z`` 算令牌 →
写 Cookie → 重试」这条链路的每一环。真 Node 冒烟用例单独一个，
没装 Node 就跳过。
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

# 324 KB 的真实生成脚本，冒烟测试才用（见 test_compute_stoken_真Node）
REAL_SCRIPT = Path(__file__).resolve().parent.parent / ".scratch" / "sec_e948d594.js"


# --------------------------------------------------------------------------- #
# 假会话（与 test_jobs_client 同形，省得跨文件 import）
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload=None, *, status: int = 200, text: str | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeCookieJar:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, name, value, **_kwargs):
        self._data[name] = value

    def get(self, name, default=None):
        return self._data.get(name, default)


class FakeHttp:
    """``get()`` 给 StokenProvider 用；``request()`` 给 JobClient 用。"""

    def __init__(self, get_responses=(), request_responses=()) -> None:
        self._gets = list(get_responses)
        self._reqs = list(request_responses)
        self.get_calls: list[dict] = []
        self.request_calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookieJar()

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if not self._gets:
            raise AssertionError(f"没有预置 GET 响应：{url}")
        item = self._gets.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    def request(self, method, url, **kwargs):
        self.request_calls.append({"method": method, "url": url, **kwargs})
        if not self._reqs:
            raise AssertionError(f"没有预置响应：{url}")
        item = self._reqs.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


class FakeProvider:
    """不打网络、不起 Node 的桩：只验 JobClient 的重试编排。"""

    def __init__(self, token: str = "0138FAKE") -> None:
        self.token = token
        self.ensure_calls: list[bool] = []

    def ensure(self, *, force: bool = False) -> str:
        self.ensure_calls.append(force)
        return self.token


def challenge_payload(seed="Em6sUKm1q2j+AAA=", name="e948d594", ts=1790759239936):
    return {
        "code": 37,
        "message": "您的环境存在异常.",
        "zpData": {"seed": seed, "name": name, "ts": ts},
    }


# --------------------------------------------------------------------------- #
# 挑战解析
# --------------------------------------------------------------------------- #


def test_parse_challenge_三样齐全():
    c = parse_challenge(challenge_payload())
    assert isinstance(c, StokenChallenge)
    assert (c.seed, c.name, c.ts) == ("Em6sUKm1q2j+AAA=", "e948d594", 1790759239936)
    assert c.is_complete


def test_parse_challenge_ts_是字符串也收():
    c = parse_challenge(challenge_payload(ts="1790759239936"))
    assert c is not None and c.ts == 1790759239936


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"code": 37, "message": "x"},                      # 没有 zpData
        {"code": 37, "zpData": None},
        {"code": 37, "zpData": {"seed": "s", "ts": 1}},     # 缺 name
        {"code": 37, "zpData": {"name": "n", "ts": 1}},     # 缺 seed
        {"code": 37, "zpData": {"seed": "s", "name": "n"}},  # 缺 ts
        {"code": 37, "zpData": {"seed": "s", "name": "n", "ts": "abc"}},
        {"code": 37, "zpData": {"seed": "s", "name": "n", "ts": 0}},   # ts=0 当缺
        {"code": 37, "zpData": {"seed": "s", "name": "n", "ts": -5}},  # 负数同理
    ],
)
def test_parse_challenge_不齐就返回None(payload):
    assert parse_challenge(payload) is None


def test_stoken_challenge_ts_0_不算完整():
    c = StokenChallenge(seed="s", name="n", ts=0)
    assert not c.is_complete


# --------------------------------------------------------------------------- #
# security-js 的下载与缓存
# --------------------------------------------------------------------------- #


def test_load_security_js_下载一次之后走缓存(tmp_path):
    http = FakeHttp(get_responses=[FakeResponse(text="window.ABC=class{}")])
    first = load_security_js(http, "e948d594", cache_dir=tmp_path)
    assert "ABC" in first
    assert security_js_path("e948d594", tmp_path).read_text(encoding="utf-8") == first

    # 第二次：不再发请求
    second = load_security_js(http, "e948d594", cache_dir=tmp_path)
    assert second == first
    assert http.get_calls == [{"url": "https://www.zhipin.com/web/common/security-js/e948d594.js", "timeout": 10.0}]


def test_load_security_js_force_绕过缓存(tmp_path):
    http = FakeHttp(
        get_responses=[
            FakeResponse(text="/* v1 */"),
            FakeResponse(text="/* v2 */"),
        ]
    )
    assert load_security_js(http, "n", cache_dir=tmp_path) == "/* v1 */"
    assert load_security_js(http, "n", cache_dir=tmp_path, force=True) == "/* v2 */"


def test_load_security_js_非200_报错(tmp_path):
    http = FakeHttp(get_responses=[FakeResponse(text="", status=404)])
    with pytest.raises(StokenError, match="拿不到"):
        load_security_js(http, "gone", cache_dir=tmp_path)


def test_security_js_path_把怪字符洗掉(tmp_path):
    p = security_js_path("a/b\\c:d", tmp_path)
    assert p.name == "a_b_c_d.js"
    assert p.parent == tmp_path


# --------------------------------------------------------------------------- #
# 算令牌
# --------------------------------------------------------------------------- #


def test_compute_stoken_喂给Node的命令行对(monkeypatch, tmp_path):
    """不起真 Node：只断言「脚本路径 / seed / ts / 时区」都送进 runner 了。

    时区换算在 :mod:`boss_jobs.assets.run_abc.js` 里做（跟前端公式同一条），
    所以这里看到的 ts 是**原样**的，换算另有用例盯。
    """
    seen = {}

    def fake_run(cmd, **kwargs):
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
    """runner 把 ``ts + (480 + getTimezoneOffset()) * 60000`` 算完再喂给 ``z``。

    北京（tz=-480）括号归零，UTC（tz=0）要多 480 分钟的毫秒数。
    """
    try:
        find_node()
    except StokenError:
        pytest.skip("本机没有 node")

    ts = 1790759239936

    def run(tz: int) -> str:
        return compute_stoken(
            STUB_ECHO_TS, seed="S", ts=ts, name="echo", cache_dir=tmp_path, tz_offset_minutes=tz
        )

    assert run(-480) == f"TS={ts}"
    assert run(0) == f"TS={ts + 480 * 60000}"
    assert run(60) == f"TS={ts + (480 + 60) * 60000}"


def test_compute_stoken_node失败带stderr(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr("boss_jobs.stoken.subprocess.run", fake_run)
    monkeypatch.setattr("boss_jobs.stoken.find_node", lambda: "node")

    with pytest.raises(StokenError, match="boom"):
        compute_stoken("/* js */", seed="S", ts=1, name="n", cache_dir=tmp_path)


def test_find_node_认环境变量(monkeypatch):
    monkeypatch.setenv("BOSS_NODE_BIN", r"D:\node\node.exe")
    assert find_node() == r"D:\node\node.exe"


def test_mint_offline_本地没缓存就报清楚(tmp_path):
    with pytest.raises(StokenError, match="没有 security-js 缓存"):
        mint_offline(cache_dir=tmp_path)


def test_mint_offline_纯算法不打网络(tmp_path):
    """样例挑战 + 本地脚本就能铸币，全程不起 HTTP。"""
    try:
        find_node()
    except StokenError:
        pytest.skip("本机没有 node")

    script = security_js_path(SAMPLE_CHALLENGE.name, tmp_path)
    script.write_text(STUB_ECHO_TS, encoding="utf-8")

    token = mint_offline(cache_dir=tmp_path)

    assert token.startswith("TS=")  # 样例脚本只回显 ts，证明 z() 真被调了
    assert SAMPLE_CHALLENGE.is_complete


@pytest.mark.skipif(not REAL_SCRIPT.exists(), reason="没有本地 security-js 样本")
def test_compute_stoken_真Node出0138前缀(tmp_path):
    """用真实生成脚本算一枚，形态断言：0138 前缀 + 长度 > 100 + 每次不同。"""
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
    # 生成器带随机量，同 seed+ts 连算两次不会一样
    assert a != b


# --------------------------------------------------------------------------- #
# StokenProvider：一条龙
# --------------------------------------------------------------------------- #


def test_provider_拿挑战下脚本算令牌并写cookie(tmp_path, monkeypatch):
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
    assert provider.ensure() == "0138MINTED"   # 第二次直接复用
    assert len(http.get_calls) == 2            # 没有再打网络


def test_provider_ensure_force_会重算(tmp_path, monkeypatch):
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
    http = FakeHttp(get_responses=[FakeResponse({"code": 36, "message": "您的账户存在异常行为."})])
    with pytest.raises(StokenError, match="code=36"):
        StokenProvider(http=http, cache_dir=tmp_path).fetch_challenge()


def test_provider_会话不支持cookie就报错(tmp_path, monkeypatch):
    monkeypatch.setattr("boss_jobs.stoken.compute_stoken", lambda *a, **k: "0138X")
    http = FakeHttp(
        get_responses=[
            FakeResponse(challenge_payload()),
            FakeResponse(text="/* js */"),
        ]
    )
    http.cookies = object()  # 没有 set
    provider = StokenProvider(http=http, cache_dir=tmp_path)
    with pytest.raises(StokenError, match="cookies.set"):
        provider.ensure()


def test_provider_apply_用requests认的expires():
    """``requests.create_cookie`` 不收 ``max_age``（那是浏览器概念），只收 ``expires``。"""
    import time as _time

    from requests.cookies import RequestsCookieJar

    class _Http:
        cookies = RequestsCookieJar()

    http = _Http()
    provider = StokenProvider(http=http, cache_dir=None)
    before = int(_time.time())
    provider.apply("0138REAL")

    cookie = next(iter(http.cookies))
    assert cookie.name == "__zp_stoken__" and cookie.value == "0138REAL"
    assert cookie.domain == ".zhipin.com" and cookie.path == "/"
    # 3840 分钟 ≈ 230400 秒
    assert before + 230000 <= cookie.expires <= before + 231000


# --------------------------------------------------------------------------- #
# JobClient：code 37 自动补令牌后重试
# --------------------------------------------------------------------------- #


def test_fetch_search_page_撞37_自动补令牌重试():
    class RenewingProvider:
        """force=True 时回一枚**新**令牌（真换新的样子）。"""

        def __init__(self) -> None:
            self.ensure_calls: list[bool] = []

        def ensure(self, *, force: bool = False) -> str:
            self.ensure_calls.append(force)
            return "0138NEW" if force else "0138OLD"

    provider = RenewingProvider()
    http = FakeHttp(
        request_responses=[
            FakeResponse(challenge_payload()),                      # 第一次：37
            FakeResponse(challenge_payload()),                      # 歇会儿再要，还 37
            FakeResponse({"code": 0, "zpData": {"jobList": [], "hasMore": False}}),  # 换新后成功
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
        def to_params(self):
            return {"query": "python", "page": 1}

        def for_page(self, page):
            return self

    result = client.fetch_search_page(_F())

    assert result.raw_count == 0
    # 先判过期 → 撞 37 歇会儿拿同一枚再试 → 还 37 才强制换新
    assert provider.ensure_calls == [False, True]
    assert http.cookies.get("__zp_stoken__") == "0138NEW"
    assert len(http.request_calls) == 3


def test_fetch_search_page_撞37_换新没换到_不再打第三发():
    """force 冷却中回的是**同一枚**：刚被拒过，再打一发纯属撞墙。"""
    provider = FakeProvider(token="0138SAME")  # force 也只回同一枚
    http = FakeHttp(
        request_responses=[
            FakeResponse(challenge_payload()),   # 第一次：37
            FakeResponse(challenge_payload()),   # 歇会儿再要，还 37
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
        def to_params(self):
            return {"query": "python", "page": 1}

        def for_page(self, page):
            return self

    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_F())
    assert excinfo.value.is_browser_check
    assert provider.ensure_calls == [False, True]  # 尝试了 force
    assert len(http.request_calls) == 2            # 但**没有**打第三发


def test_fetch_search_page_撞37_歇会儿就好了_不换新():
    """37 有时只是太快：退避重试成功就别去拉 Chrome 换新。"""
    provider = FakeProvider(token="0138KEEP")
    http = FakeHttp(
        request_responses=[
            FakeResponse(challenge_payload()),                      # 第一次：37
            FakeResponse({"code": 0, "zpData": {"jobList": [], "hasMore": False}}),  # 歇完成功
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
        def to_params(self):
            return {"query": "python", "page": 1}

        def for_page(self, page):
            return self

    result = client.fetch_search_page(_F())

    assert result.raw_count == 0
    assert provider.ensure_calls == [False]        # 没有 force
    assert len(http.request_calls) == 2


def test_fetch_search_page_撞37_没挂provider_照旧报错():
    http = FakeHttp(request_responses=[FakeResponse(challenge_payload())])
    client = JobClient(base_url="https://example.test", http=http, retries=0)

    class _F:
        def to_params(self):
            return {"page": 1}

        def for_page(self, page):
            return self

    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_F())
    assert excinfo.value.is_browser_check
    assert len(http.request_calls) == 1


def test_fetch_search_page_撞36_不强制换新():
    """账号风控不是缺令牌。请求前那一次 ``ensure()`` 是常规判过期，
    撞上 code 36 **不会**再 force 换一枚。"""
    provider = FakeProvider()
    http = FakeHttp(
        request_responses=[FakeResponse({"code": 36, "message": "您的账户存在异常行为."})]
    )
    client = JobClient(base_url="https://example.test", http=http, stoken_provider=provider, retries=0)

    class _F:
        def to_params(self):
            return {"page": 1}

        def for_page(self, page):
            return self

    with pytest.raises(JobApiError) as excinfo:
        client.fetch_search_page(_F())
    assert excinfo.value.is_risk_control
    assert provider.ensure_calls == [False]           # 只有请求前那一次，没有 force
