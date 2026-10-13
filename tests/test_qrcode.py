"""APP 扫码登录流程的离线单测。

用假会话替换网络层，覆盖 ``boss_login.qrcode`` 的完整状态机：

    randkey ─► scan(长轮询) ─► getSecondKey ─► [scanSecond 赛跑 / 1 秒兜底]
           ─► getHeadImg ─► scanLogin(长轮询) ─► dispatcher(换 Set-Cookie)

跑： python -m pytest tests/test_qrcode.py -q
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


# --------------------------------------------------------------------------- #
# 测试替身（与 tests/test_client.py 同形，但不互相 import，避免耦合）
# --------------------------------------------------------------------------- #


class FakeCookies:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data = dict(initial or {})

    def set(self, name: str, value: str, **_kwargs) -> None:
        self._data[name] = value

    def get_dict(self) -> dict[str, str]:
        return dict(self._data)

    def clear(self) -> None:
        self._data.clear()


class FakeResponse:
    def __init__(
        self,
        payload,
        *,
        status: int = 200,
        set_cookies: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)
        self.set_cookies = dict(set_cookies or {})


class FakeHttp:
    """按脚本依次回响应；每回一个就把 ``set_cookies`` 种进 cookie jar。"""

    def __init__(self, responses, cookies: dict[str, str] | None = None) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies(cookies)

    def request(self, method, url, **kwargs):
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
        return self.calls[-1]

    @property
    def last_params(self) -> dict:
        return dict(self.last_call.get("params") or {})

    @property
    def last_timeout(self) -> float | None:
        return self.last_call.get("timeout")


def ok(
    payload: dict | None = None,
    *,
    set_cookies: dict[str, str] | None = None,
    **top,
) -> FakeResponse:
    """标准包裹 ``{code:0, message, zpData}``；``**top`` 往顶层叠旗标。"""
    body = {"code": 0, "message": "success", "zpData": payload or {}}
    body.update(top)
    return FakeResponse(body, set_cookies=set_cookies)


def raw_ok(top: dict) -> FakeResponse:
    """扫码那族会把 ``scaned`` / ``login`` 摊在顶层，连 ``zpData`` 都不一定有。"""
    return FakeResponse({"code": 0, "message": "success", **top})


def fail(code: int, message: str = "", data: dict | None = None) -> FakeResponse:
    return FakeResponse({"code": code, "message": message, "zpData": data or {}})


def make_client(responses, *, cookies=None, **kwargs):
    http = FakeHttp(responses, cookies=cookies)
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("retries", 0)
    return ZhipinLoginClient(http=http, **kwargs), http


def make_login(responses, *, cookies=None, **kwargs) -> tuple[AppScanLogin, FakeHttp]:
    client, http = make_client(responses, cookies=cookies)
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("clock", lambda: 1_000.0)
    return AppScanLogin(client, **kwargs), http


def seed_ticket(login: AppScanLogin, *, qr_id: str = "qr-1", second: str = "") -> QrTicket:
    """直接注入票据，省掉 randkey 那一发——单步测试只关心自己那一步。"""
    ticket = QrTicket(qr_id=qr_id, second_qr_id=second, created_at=1_000.0, expires_at=1_030.0)
    login.ticket = ticket
    return ticket


class TickingClock:
    """每次调用往前走一步的时钟，用来跑超时/过期分支。"""

    def __init__(self, start: float = 1_000.0, step: float = 1.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now



# --------------------------------------------------------------------------- #
# QrTicket
# --------------------------------------------------------------------------- #


class TestQrTicket:
    def test_payload_prefers_second_qr_id(self):
        ticket = QrTicket(qr_id="uuid-1", second_qr_id="second-2")
        assert ticket.payload == "second-2"

    def test_payload_falls_back_to_qr_id(self):
        ticket = QrTicket(qr_id="uuid-1")
        assert ticket.payload == "uuid-1"

    def test_payload_ignores_second_when_disabled(self, monkeypatch):
        monkeypatch.setattr(C, "QR_CONTENT_PREFER_SECOND", False)
        ticket = QrTicket(qr_id="uuid-1", second_qr_id="second-2")
        assert ticket.payload == "uuid-1"

    def test_expiry(self):
        ticket = QrTicket(qr_id="u", expires_at=200.0)
        assert ticket.is_expired(now=199.9) is False
        assert ticket.is_expired(now=200.0) is True

    def test_no_expiry_when_unset(self):
        assert QrTicket(qr_id="u").is_expired(now=10**12) is False


# --------------------------------------------------------------------------- #
# 单步
# --------------------------------------------------------------------------- #


class TestCreate:
    def test_create_reads_qr_id(self):
        login, http = make_login(
            [ok({"qrId": "qr-abc", "randKey": "rk", "secretKey": "sk", "shortRandKey": "s"})]
        )
        ticket = login.create()
        assert ticket.qr_id == "qr-abc"
        assert ticket.rand_key == "rk"
        assert ticket.secret_key == "sk"
        assert ticket.short_rand_key == "s"
        assert login.payload == "qr-abc"
        # 登录页的 Ie() 构造器压根不带 pk，randkey 也确实是空体
        assert http.last_call["method"] == "POST"
        assert http.last_call["url"].endswith("/wapi/zppassport/captcha/randkey")

    def test_create_missing_qr_id_raises(self):
        login, _ = make_login([ok({})])
        with pytest.raises(LoginIncomplete, match="qrId"):
            login.create()

    def test_create_is_reusable(self):
        login, _ = make_login(
            [ok({"qrId": "one"}), ok({"qrId": "two"})]
        )
        assert login.create().qr_id == "one"
        assert login.create().qr_id == "two"
        assert login.payload == "two"


class TestWaitScanned:
    def test_returns_on_scaned(self):
        login, http = make_login([raw_ok({"scaned": True, "validate": "v-token", "allweb": False})])
        seed_ticket(login)
        outcome = login.wait_scanned()
        assert outcome.validate == "v-token"
        assert outcome.allweb is False
        assert http.last_params == {"uuid": "qr-1"}
        # 长轮询必须挂够 axios 那个 timeout: 1e5
        assert http.last_timeout == C.QR_POLL_TIMEOUT

    def test_keeps_polling_until_scanned(self):
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
        login, _ = make_login(
            [raw_ok({"scaned": False})],
            clock=TickingClock(start=1_000.0, step=50.0),
        )
        seed_ticket(login)  # expires_at=1030，下一步就翻过去
        with pytest.raises(BossLoginError, match="过期"):
            login.wait_scanned()

    def test_timeout_budget_exhausted(self):
        login, _ = make_login(
            [raw_ok({"scaned": False}) for _ in range(8)],
            clock=TickingClock(start=1_000.0, step=1.0),
        )
        seed_ticket(login)
        with pytest.raises(BossLoginError, match="超时"):
            login.wait_scanned(timeout=1.5)


class TestSecondKey:
    def test_reads_qr_id(self):
        login, http = make_login([ok({"qrId": "second-uuid"})])
        seed_ticket(login)
        assert login.fetch_second_key() == "second-uuid"
        assert login.payload == "second-uuid"
        assert http.last_params == {"uuid": "qr-1"}

    def test_missing_second_key_raises_with_inner_code(self):
        # 外层 code=0，zpData 里再套一层 code:1092 —— 实测形状
        login, _ = make_login([ok({"code": 1092, "msg": "not scanned"})])
        seed_ticket(login)
        with pytest.raises(ApiError) as excinfo:
            login.fetch_second_key()
        assert excinfo.value.code == 1092

    def test_payload_switches_after_second_key(self):
        login, _ = make_login([ok({"qrId": "s2"})])
        seed_ticket(login)
        assert login.payload == "qr-1"
        login.fetch_second_key()
        assert login.payload == "s2"


class TestHeadImg:
    def test_reads_large_and_wx_mp(self):
        login, http = make_login([ok({"large": "https://img/1.png", "wxMp": True})])
        seed_ticket(login)
        assert login.fetch_head_img() == "https://img/1.png"
        assert login.watched_mp is True
        assert http.last_params == {"qrId": "qr-1"}

    def test_missing_large_is_not_fatal(self):
        # 外层 code=0，zpData 里是 {code:1092}，没有 large
        login, _ = make_login([ok({"code": 1092, "msg": "no"})])
        seed_ticket(login)
        assert login.fetch_head_img() == ""
        assert login.head_img == ""


class TestWaitConfirmed:
    def test_confirmed(self):
        login, http = make_login([raw_ok({"scaned": True, "login": True})])
        seed_ticket(login)
        outcome = login.wait_confirmed()
        assert outcome.confirmed is True
        assert http.last_params == {"qrId": "qr-1"}
        assert http.last_timeout == C.QR_POLL_TIMEOUT

    def test_cancelled(self):
        login, _ = make_login([raw_ok({"scaned": True, "login": False})])
        seed_ticket(login)
        outcome = login.wait_confirmed()
        assert outcome.confirmed is False

    def test_keeps_polling_when_not_scanned(self):
        login, _ = make_login(
            [
                raw_ok({"scaned": False}),
                raw_ok({"scaned": True, "login": True}),
            ]
        )
        seed_ticket(login)
        assert login.wait_confirmed().confirmed is True


class TestDispatch:
    def test_builds_result_from_new_cookies(self):
        login, http = make_login([ok({}, set_cookies={"wt2": "cookie-1", "bst": "cookie-2"})])
        seed_ticket(login)
        response = login.dispatch()
        result = login.build_result(response)
        assert result.logged_in is True
        assert result.new_cookies == {"wt2": "cookie-1", "bst": "cookie-2"}
        assert http.last_params["qrId"] == "qr-1"
        assert http.last_params["pk"] == C.QR_DEFAULT_PK

    def test_missing_cookies_raises(self):
        login, _ = make_login([ok({"identity": 0})])
        seed_ticket(login)
        response = login.dispatch()
        with pytest.raises(LoginIncomplete, match="Cookie"):
            login.build_result(response)

    def test_fingerprint_is_encrypted(self):
        login, http = make_login([ok({}, set_cookies={"wt2": "c"})])
        seed_ticket(login)
        login.dispatch(fingerprint="fp-plain")
        fp = http.last_params["fp"]
        assert fp != "fp-plain"
        from boss_login.crypto import decrypt_text

        assert decrypt_text(fp) == "fp-plain"

    def test_no_fingerprint_by_default(self):
        login, http = make_login([ok({}, set_cookies={"wt2": "c"})])
        seed_ticket(login)
        login.dispatch()
        assert "fp" not in http.last_params

    def test_extra_params_are_merged(self):
        login, http = make_login([ok({}, set_cookies={"wt2": "c"})])
        seed_ticket(login)
        login.dispatch(extra_params={"environment": "BossZhipinBridge", "skip": ""})
        assert http.last_params["environment"] == "BossZhipinBridge"
        # 空值不往表单里塞
        assert "skip" not in http.last_params


# --------------------------------------------------------------------------- #
# 全流程
# --------------------------------------------------------------------------- #


def _happy_path(responses_prefix: list | None = None) -> list:
    """标准成功脚本：create → scan → secondKey → scanSecond → headImg → scanLogin → dispatcher。"""
    script = list(responses_prefix or [])
    script += [
        ok({"qrId": "qr-main"}),  # create
        raw_ok({"scaned": True, "validate": "tok", "allweb": False}),  # scan
        ok({"qrId": "qr-second"}),  # getSecondKey
        raw_ok({"scaned": True}),  # scanSecond（1 秒窗口里先到）
        ok({"large": "https://img/u.png"}),  # getHeadImg
        raw_ok({"scaned": True, "login": True}),  # scanLogin
        ok({"identity": 0}, set_cookies={"wt2": "sess", "bst": "b"}),  # dispatcher
    ]
    return script


class TestRun:
    def test_full_flow(self):
        login, http = make_login(_happy_path())
        events: list[tuple[str, dict]] = []
        payloads: list[str] = []
        result = login.run(
            on_event=lambda n, d: events.append((n, d)),
            on_payload=payloads.append,
        )
        assert result.logged_in is True
        assert result.new_cookies == {"wt2": "sess", "bst": "b"}
        # 二维码内容先出 qrId，getSecondKey 之后换成 secondUuid
        assert payloads == ["qr-main", "qr-second"]
        names = [n for n, _ in events]
        assert "qr_created" in names
        assert "qr_scanned" in names
        assert "qr_confirmed" in names
        assert "success" in names
        # 七个接口，一个不多一个不少
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
        script = [
            ok({"qrId": "qr-main"}),
            raw_ok({"scaned": True}),
            ok({"qrId": "qr-second"}),
            raw_ok({"scaned": True}),
            ok({"large": ""}),
            raw_ok({"scaned": True, "login": False}),  # App 端点了取消
        ]
        login, _ = make_login(script)
        events: list[tuple[str, dict]] = []
        with pytest.raises(BossLoginError, match="取消"):
            login.run(on_event=lambda n, d: events.append((n, d)))
        assert any(n == "qr_cancelled" for n, _ in events)

    def test_run_qr_login_helper(self):
        client, http = make_client(_happy_path())
        result = run_qr_login(client, sleeper=lambda _s: None, clock=lambda: 1_000.0)
        assert result.logged_in is True
        assert len(http.calls) == 7

    def test_login_limit_code_maps_to_account_blocked(self):
        login, _ = make_login([fail(4563, "登录受限")])
        seed_ticket(login)
        with pytest.raises(AccountBlocked):
            login.dispatch()

    def test_generic_error_maps_to_api_error(self):
        login, _ = make_login([fail(5001, "服务繁忙")])
        seed_ticket(login)
        with pytest.raises(ApiError):
            login.dispatch()

    @pytest.mark.parametrize("code", [4563, 4567, 4912, 4918, 4930, 4999, 5004, 4942, 4950, 4945])
    def test_limit_code_detection(self, code):
        assert _in_limit_range(code) or code in C.QR_LOGIN_LIMIT_CODES

    @pytest.mark.parametrize("code", [1092, 2001, 400061])
    def test_non_limit_codes_are_not_blocked(self, code):
        assert not (_in_limit_range(code) or code in C.QR_LOGIN_LIMIT_CODES)


# --------------------------------------------------------------------------- #
# 端点解析
# --------------------------------------------------------------------------- #


class TestEndpoints:
    def test_client_merges_qr_endpoints(self):
        client, _ = make_client([])
        assert client.endpoints["qr_randkey"] == "/wapi/zppassport/captcha/randkey"
        assert client.endpoints["qr_dispatcher"] == "/wapi/zppassport/qrcode/dispatcher"

    def test_client_override_wins(self):
        client, _ = make_client([], endpoints={"qr_scan": "/custom/scan"})
        login = AppScanLogin(client, sleeper=lambda _s: None)
        assert login._endpoint("qr_scan") == "/custom/scan"

    def test_fallback_to_config(self):
        class BareClient:
            endpoints = {}

        login = AppScanLogin(BareClient(), sleeper=lambda _s: None)
        assert login._endpoint("qr_scan") == C.QR_ENDPOINTS["qr_scan"]


# --------------------------------------------------------------------------- #
# 二维码渲染（依赖 segno；没装就跳过）
# --------------------------------------------------------------------------- #

segno = pytest.importorskip("segno", reason="segno 未安装，跳过二维码渲染测试")


class TestRender:
    def test_png_magic(self):
        from boss_login.qrcode import render_qr_png

        png = render_qr_png("qr-uuid")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_svg_is_markup(self):
        from boss_login.qrcode import render_qr_svg

        svg = render_qr_svg("qr-uuid")
        assert svg.lstrip().startswith("<svg")

    def test_terminal_returns_string_not_none(self):
        # segno 的 terminal() 默认往 sys.stdout 打、返回 None，这里必须接住
        from boss_login.qrcode import render_qr_terminal

        art = render_qr_terminal("qr-uuid")
        assert isinstance(art, str) and art.strip()

    def test_empty_payload_rejected(self):
        from boss_login.qrcode import render_qr_png

        with pytest.raises(ValueError):
            render_qr_png("")
