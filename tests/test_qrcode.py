"""APP 扫码登录流程的离线单测：假会话覆盖 `boss_login.qrcode` 状态机。

运行：`python -m pytest tests/test_qrcode.py -q`
"""

from __future__ import annotations

import json

import pytest

from boss_login import (
    AccountBlocked,
    ApiError,
    AppScanLogin,
    BossLoginError,
    LoginIncomplete,
    QrTicket,
    ZhipinLoginClient,
    run_qr_login,
)
from boss_login import config as C
from boss_login.qrcode import _in_limit_range


class FakeCookies:
    """假 cookie 罐，支持 set / get_dict / clear。"""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        """初始化。"""
        self._data = dict(initial or {})

    def set(self, name: str, value: str, **_kwargs) -> None:
        """写入一枚 cookie。"""
        self._data[name] = value

    def get_dict(self) -> dict[str, str]:
        """返回 cookie 副本。"""
        return dict(self._data)

    def clear(self) -> None:
        """清空全部 cookie。"""
        self._data.clear()


class FakeResponse:
    """假的 HTTP 响应体，可附带要种进 jar 的 set_cookies。"""

    def __init__(
        self,
        payload,
        *,
        status: int = 200,
        set_cookies: dict[str, str] | None = None,
    ) -> None:
        """初始化。"""
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)
        self.set_cookies = dict(set_cookies or {})


class FakeHttp:
    """按脚本依次回响应；每回一个就把 `set_cookies` 种进 cookie jar。"""

    def __init__(self, responses, cookies: dict[str, str] | None = None) -> None:
        """初始化。"""
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies(cookies)

    def request(self, method, url, **kwargs):
        """记录一次调用并返回下一条预置响应。"""
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(
                f"没有预置更多响应，却收到了第 {len(self.calls)} 次请求：{url}"
            )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        for name, value in getattr(item, "set_cookies", {}).items():
            self.cookies.set(name, value)
        return item

    @property
    def last_call(self) -> dict:
        """最近一次调用记录。"""
        return self.calls[-1]

    @property
    def last_params(self) -> dict:
        """最近一次调用的查询参数。"""
        return dict(self.last_call.get("params") or {})

    @property
    def last_timeout(self) -> float | None:
        """最近一次调用的超时。"""
        return self.last_call.get("timeout")


def ok(
    payload: dict | None = None,
    *,
    set_cookies: dict[str, str] | None = None,
    **top,
) -> FakeResponse:
    """标准包裹 `{code:0, message, zpData}`；`**top` 往顶层叠旗标。"""
    body = {"code": 0, "message": "success", "zpData": payload or {}}
    body.update(top)
    return FakeResponse(body, set_cookies=set_cookies)


def raw_ok(top: dict) -> FakeResponse:
    """扫码那族的顶层响应：`scaned` / `login` 直接摊在顶层。"""
    return FakeResponse({"code": 0, "message": "success", **top})


def fail(code: int, message: str = "", data: dict | None = None) -> FakeResponse:
    """构造非 0 业务码响应。"""
    return FakeResponse({"code": code, "message": message, "zpData": data or {}})


def make_client(responses, *, cookies=None, **kwargs):
    """用假 HTTP 会话构造登录客户端，默认不睡不重试。"""
    http = FakeHttp(responses, cookies=cookies)
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("retries", 0)
    return ZhipinLoginClient(http=http, **kwargs), http


def make_login(responses, *, cookies=None, **kwargs) -> tuple[AppScanLogin, FakeHttp]:
    """构造 App 扫码登录机与假 HTTP，默认固定时钟且不睡。"""
    client, http = make_client(responses, cookies=cookies)
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("clock", lambda: 1_000.0)
    return AppScanLogin(client, **kwargs), http


def seed_ticket(login: AppScanLogin, *, qr_id: str = "qr-1", second: str = "") -> QrTicket:
    """直接注入票据，省掉 randkey 那一发。"""
    ticket = QrTicket(qr_id=qr_id, second_qr_id=second, created_at=1_000.0, expires_at=1_030.0)
    login.ticket = ticket
    return ticket


class TickingClock:
    """每次调用往前走一步的时钟，用来跑超时/过期分支。"""

    def __init__(self, start: float = 1_000.0, step: float = 1.0) -> None:
        """初始化。"""
        self.now = start
        self.step = step

    def __call__(self) -> float:
        """向前拨一步并返回当前时刻。"""
        self.now += self.step
        return self.now


class TestQrTicket:
    """票据内容与过期判定。"""

    def test_payload_prefers_second_qr_id(self):
        """验 payload 优先用 second_qr_id。"""
        ticket = QrTicket(qr_id="uuid-1", second_qr_id="second-2")
        assert ticket.payload == "second-2"

    def test_payload_falls_back_to_qr_id(self):
        """验无 second 时回退 qr_id。"""
        ticket = QrTicket(qr_id="uuid-1")
        assert ticket.payload == "uuid-1"

    def test_payload_ignores_second_when_disabled(self, monkeypatch):
        """验关掉优先开关后仍用 qr_id。"""
        monkeypatch.setattr(C, "QR_CONTENT_PREFER_SECOND", False)
        ticket = QrTicket(qr_id="uuid-1", second_qr_id="second-2")
        assert ticket.payload == "uuid-1"

    def test_expiry(self):
        """验到点即过期。"""
        ticket = QrTicket(qr_id="u", expires_at=200.0)
        assert ticket.is_expired(now=199.9) is False
        assert ticket.is_expired(now=200.0) is True

    def test_no_expiry_when_unset(self):
        """验未设过期时间时永不过期。"""
        assert QrTicket(qr_id="u").is_expired(now=10**12) is False


class TestCreate:
    """创建二维码（randkey）步骤。"""

    def test_create_reads_qr_id(self):
        """验 create 读出各项密钥且请求为 randkey 空体 POST。"""
        login, http = make_login(
            [ok({"qrId": "qr-abc", "randKey": "rk", "secretKey": "sk", "shortRandKey": "s"})]
        )
        ticket = login.create()
        assert ticket.qr_id == "qr-abc"
        assert ticket.rand_key == "rk"
        assert ticket.secret_key == "sk"
        assert ticket.short_rand_key == "s"
        assert login.payload == "qr-abc"
        assert http.last_call["method"] == "POST"
        assert http.last_call["url"].endswith("/wapi/zppassport/captcha/randkey")

    def test_create_missing_qr_id_raises(self):
        """验缺 qrId 时抛不完整错误。"""
        login, _ = make_login([ok({})])
        with pytest.raises(LoginIncomplete, match="qrId"):
            login.create()

    def test_create_is_reusable(self):
        """验 create 可重复调用并更新 payload。"""
        login, _ = make_login(
            [ok({"qrId": "one"}), ok({"qrId": "two"})]
        )
        assert login.create().qr_id == "one"
        assert login.create().qr_id == "two"
        assert login.payload == "two"


class TestWaitScanned:
    """等待扫码（长轮询）。"""

    def test_returns_on_scaned(self):
        """验扫到后返回 validate，且长轮询挂上专用超时。"""
        login, http = make_login([raw_ok({"scaned": True, "validate": "v-token", "allweb": False})])
        seed_ticket(login)
        outcome = login.wait_scanned()
        assert outcome.validate == "v-token"
        assert outcome.allweb is False
        assert http.last_params == {"uuid": "qr-1"}
        assert http.last_timeout == C.QR_POLL_TIMEOUT

    def test_keeps_polling_until_scanned(self):
        """验未扫到时继续轮询。"""
        login, http = make_login(
            [
                raw_ok({"scaned": False, "msg": "timeout"}),
                raw_ok({"scaned": False, "msg": "timeout"}),
                raw_ok({"scaned": True, "validate": "v"}),
            ]
        )
        seed_ticket(login)
        outcome = login.wait_scanned()
        assert outcome.validate == "v"
        assert len(http.calls) == 3

    def test_expired_raises(self):
        """验票据过期时抛错。"""
        login, _ = make_login(
            [raw_ok({"scaned": False})],
            clock=TickingClock(start=1_000.0, step=50.0),
        )
        seed_ticket(login)
        with pytest.raises(BossLoginError, match="过期"):
            login.wait_scanned()

    def test_timeout_budget_exhausted(self):
        """验超时预算耗尽时抛错。"""
        login, _ = make_login(
            [raw_ok({"scaned": False}) for _ in range(8)],
            clock=TickingClock(start=1_000.0, step=1.0),
        )
        seed_ticket(login)
        with pytest.raises(BossLoginError, match="超时"):
            login.wait_scanned(timeout=1.5)


class TestSecondKey:
    """获取 second key。"""

    def test_reads_qr_id(self):
        """验取到 second uuid 并切到新 payload。"""
        login, http = make_login([ok({"qrId": "second-uuid"})])
        seed_ticket(login)
        assert login.fetch_second_key() == "second-uuid"
        assert login.payload == "second-uuid"
        assert http.last_params == {"uuid": "qr-1"}

    def test_missing_second_key_raises_with_inner_code(self):
        """验内层业务码会带上 `ApiError.code`。"""
        login, _ = make_login([ok({"code": 1092, "msg": "not scanned"})])
        seed_ticket(login)
        with pytest.raises(ApiError) as excinfo:
            login.fetch_second_key()
        assert excinfo.value.code == 1092

    def test_payload_switches_after_second_key(self):
        """验 second key 之后 payload 切到新 uuid。"""
        login, _ = make_login([ok({"qrId": "s2"})])
        seed_ticket(login)
        assert login.payload == "qr-1"
        login.fetch_second_key()
        assert login.payload == "s2"


class TestHeadImg:
    """获取头像与公众号关注标记。"""

    def test_reads_large_and_wx_mp(self):
        """验读出头像 URL 与 wxMp 标记。"""
        login, http = make_login([ok({"large": "https://img/1.png", "wxMp": True})])
        seed_ticket(login)
        assert login.fetch_head_img() == "https://img/1.png"
        assert login.watched_mp is True
        assert http.last_params == {"qrId": "qr-1"}

    def test_missing_large_is_not_fatal(self):
        """验缺 large 时头像为空而不是致命错。"""
        login, _ = make_login([ok({"code": 1092, "msg": "no"})])
        seed_ticket(login)
        assert login.fetch_head_img() == ""
        assert login.head_img == ""


class TestWaitConfirmed:
    """等待 App 端确认登录。"""

    def test_confirmed(self):
        """验确认登录时返回 confirmed。"""
        login, http = make_login([raw_ok({"scaned": True, "login": True})])
        seed_ticket(login)
        outcome = login.wait_confirmed()
        assert outcome.confirmed is True
        assert http.last_params == {"qrId": "qr-1"}
        assert http.last_timeout == C.QR_POLL_TIMEOUT

    def test_cancelled(self):
        """验取消登录时 confirmed 为假。"""
        login, _ = make_login([raw_ok({"scaned": True, "login": False})])
        seed_ticket(login)
        outcome = login.wait_confirmed()
        assert outcome.confirmed is False

    def test_keeps_polling_when_not_scanned(self):
        """验未扫到时继续轮询直到确认。"""
        login, _ = make_login(
            [
                raw_ok({"scaned": False}),
                raw_ok({"scaned": True, "login": True}),
            ]
        )
        seed_ticket(login)
        assert login.wait_confirmed().confirmed is True


class TestDispatch:
    """dispatcher 换登录态。"""

    def test_builds_result_from_new_cookies(self):
        """验从响应 Set-Cookie 构建登录结果。"""
        login, http = make_login([ok({}, set_cookies={"wt2": "cookie-1", "bst": "cookie-2"})])
        seed_ticket(login)
        response = login.dispatch()
        result = login.build_result(response)
        assert result.logged_in is True
        assert result.new_cookies == {"wt2": "cookie-1", "bst": "cookie-2"}
        assert http.last_params["qrId"] == "qr-1"
        assert http.last_params["pk"] == C.QR_DEFAULT_PK

    def test_missing_cookies_raises(self):
        """验缺 Cookie 时抛不完整错误。"""
        login, _ = make_login([ok({"identity": 0})])
        seed_ticket(login)
        response = login.dispatch()
        with pytest.raises(LoginIncomplete, match="Cookie"):
            login.build_result(response)

    def test_fingerprint_is_encrypted(self):
        """验指纹参数入参前加密。"""
        login, http = make_login([ok({}, set_cookies={"wt2": "c"})])
        seed_ticket(login)
        login.dispatch(fingerprint="fp-plain")
        fp = http.last_params["fp"]
        assert fp != "fp-plain"
        from boss_login.crypto import decrypt_text

        assert decrypt_text(fp) == "fp-plain"

    def test_no_fingerprint_by_default(self):
        """验默认不带指纹参数。"""
        login, http = make_login([ok({}, set_cookies={"wt2": "c"})])
        seed_ticket(login)
        login.dispatch()
        assert "fp" not in http.last_params

    def test_extra_params_are_merged(self):
        """验额外参数并入表单且丢弃空值。"""
        login, http = make_login([ok({}, set_cookies={"wt2": "c"})])
        seed_ticket(login)
        login.dispatch(extra_params={"environment": "BossZhipinBridge", "skip": ""})
        assert http.last_params["environment"] == "BossZhipinBridge"
        assert "skip" not in http.last_params


def _happy_path(responses_prefix: list | None = None) -> list:
    """标准成功脚本：create → scan → secondKey → scanSecond → headImg → scanLogin → dispatcher。"""
    script = list(responses_prefix or [])
    script += [
        ok({"qrId": "qr-main"}),
        raw_ok({"scaned": True, "validate": "tok", "allweb": False}),
        ok({"qrId": "qr-second"}),
        raw_ok({"scaned": True}),
        ok({"large": "https://img/u.png"}),
        raw_ok({"scaned": True, "login": True}),
        ok({"identity": 0}, set_cookies={"wt2": "sess", "bst": "b"}),
    ]
    return script


class TestRun:
    """整条登录流程。"""

    def test_full_flow(self):
        """验成功流程事件齐全、payload 切换且七步接口顺序正确。"""
        login, http = make_login(_happy_path())
        events: list[tuple[str, dict]] = []
        payloads: list[str] = []
        result = login.run(
            on_event=lambda n, d: events.append((n, d)),
            on_payload=payloads.append,
        )
        assert result.logged_in is True
        assert result.new_cookies == {"wt2": "sess", "bst": "b"}
        assert payloads == ["qr-main", "qr-second"]
        names = [n for n, _ in events]
        assert "qr_created" in names
        assert "qr_scanned" in names
        assert "qr_confirmed" in names
        assert "success" in names
        assert len(http.calls) == 7
        assert [c["url"].rsplit("/", 1)[-1] for c in http.calls] == [
            "randkey",
            "scan",
            "getSecondKey",
            "scanSecond",
            "getHeadImg",
            "scanLogin",
            "dispatcher",
        ]

    def test_cancelled_raises(self):
        """验 App 端取消登录会抛错并发取消事件。"""
        script = [
            ok({"qrId": "qr-main"}),
            raw_ok({"scaned": True}),
            ok({"qrId": "qr-second"}),
            raw_ok({"scaned": True}),
            ok({"large": ""}),
            raw_ok({"scaned": True, "login": False}),
        ]
        login, _ = make_login(script)
        events: list[tuple[str, dict]] = []
        with pytest.raises(BossLoginError, match="取消"):
            login.run(on_event=lambda n, d: events.append((n, d)))
        assert any(n == "qr_cancelled" for n, _ in events)

    def test_run_qr_login_helper(self):
        """验 `run_qr_login` 辅助函数跑通全流程。"""
        client, http = make_client(_happy_path())
        result = run_qr_login(client, sleeper=lambda _s: None, clock=lambda: 1_000.0)
        assert result.logged_in is True
        assert len(http.calls) == 7

    def test_login_limit_code_maps_to_account_blocked(self):
        """验登录受限码映射为 `AccountBlocked`。"""
        login, _ = make_login([fail(4563, "登录受限")])
        seed_ticket(login)
        with pytest.raises(AccountBlocked):
            login.dispatch()

    def test_generic_error_maps_to_api_error(self):
        """验其它业务错误映射为 `ApiError`。"""
        login, _ = make_login([fail(5001, "服务繁忙")])
        seed_ticket(login)
        with pytest.raises(ApiError):
            login.dispatch()

    @pytest.mark.parametrize("code", [4563, 4567, 4912, 4918, 4930, 4999, 5004, 4942, 4950, 4945])
    def test_limit_code_detection(self, code):
        """验登录受限码全集都能被判定命中。"""
        assert _in_limit_range(code) or code in C.QR_LOGIN_LIMIT_CODES

    @pytest.mark.parametrize("code", [1092, 2001, 400061])
    def test_non_limit_codes_are_not_blocked(self, code):
        """验非受限码不会被误判为封禁。"""
        assert not (_in_limit_range(code) or code in C.QR_LOGIN_LIMIT_CODES)


class TestEndpoints:
    """端点解析与覆盖。"""

    def test_client_merges_qr_endpoints(self):
        """验客户端默认带齐扫码端点。"""
        client, _ = make_client([])
        assert client.endpoints["qr_randkey"] == "/wapi/zppassport/captcha/randkey"
        assert client.endpoints["qr_dispatcher"] == "/wapi/zppassport/qrcode/dispatcher"

    def test_client_override_wins(self):
        """验客户端自定义端点优先。"""
        client, _ = make_client([], endpoints={"qr_scan": "/custom/scan"})
        login = AppScanLogin(client, sleeper=lambda _s: None)
        assert login._endpoint("qr_scan") == "/custom/scan"

    def test_fallback_to_config(self):
        """验无端点时回退配置默认值。"""
        class BareClient:
            """无端点覆盖的极简客户端桩。"""

            endpoints = {}

        login = AppScanLogin(BareClient(), sleeper=lambda _s: None)
        assert login._endpoint("qr_scan") == C.QR_ENDPOINTS["qr_scan"]


segno = pytest.importorskip("segno", reason="segno 未安装，跳过二维码渲染测试")


class TestRender:
    """二维码渲染（依赖 segno）。"""

    def test_png_magic(self):
        """验 PNG 输出带文件魔数。"""
        from boss_login.qrcode import render_qr_png

        png = render_qr_png("qr-uuid")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_svg_is_markup(self):
        """验 SVG 输出是标记文本。"""
        from boss_login.qrcode import render_qr_svg

        svg = render_qr_svg("qr-uuid")
        assert svg.lstrip().startswith("<svg")

    def test_terminal_returns_string_not_none(self):
        """验终端图形返回非空字符串而不是 `None`。"""
        from boss_login.qrcode import render_qr_terminal

        art = render_qr_terminal("qr-uuid")
        assert isinstance(art, str) and art.strip()

    def test_empty_payload_rejected(self):
        """验空载荷被拒绝。"""
        from boss_login.qrcode import render_qr_png

        with pytest.raises(ValueError):
            render_qr_png("")
