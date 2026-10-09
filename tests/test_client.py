"""离线单测：用假会话替换网络层，覆盖完整登录流程与各类异常分支。

运行： python -m pytest tests/ -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from boss_login import (
    AccountBlocked,
    ApiError,
    BossLoginError,
    CodeRejected,
    LoginIncomplete,
    RateLimited,
    RiskControlRequired,
    TransportError,
    ValidationError,
    ZhipinLoginClient,
    create_client,
    load_session,
    mask_phone,
    normalize_phone,
    persist_login,
    save_session,
    StoredSession,
)
from boss_login import client as client_module
from boss_login.config import ENDPOINTS, SMS_SCENES




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
        payload=None,
        *,
        status: int = 200,
        text: str | None = None,
        set_cookies: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
        #: 模拟 Set-Cookie：FakeHttp 收到这个响应时会把它们种进 cookie jar
        self.set_cookies = dict(set_cookies or {})


class FakeHttp:
    """记录请求并按脚本依次返回响应；响应可以是 FakeResponse 或异常实例。"""

    def __init__(
        self,
        responses,
        cookies: dict[str, str] | None = None,
        *,
        auto_suggest: bool = True,
    ) -> None:
        self._responses = list(responses)
        #: getLoginSuggest 是登录页**每次**都会打的 best-effort 前置调用。
        #: 默认由测试夹具直接回成功，免得每个登录用例都得手动往脚本里塞一发；
        #: 要专门控制它（比如让它失败）就关掉这个开关，自己排响应。
        self._auto_suggest = auto_suggest
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeCookies(cookies)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._auto_suggest and "getLoginSuggest" in url:
            return ok({})
        if not self._responses:
            raise AssertionError(f"没有预置更多响应，却收到了第 {len(self.calls)} 次请求：{url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        for name, value in getattr(item, "set_cookies", {}).items():
            self.cookies.set(name, value)
        return item


    @property
    def last_call(self) -> dict:
        return self.calls[-1]

    def form_of(self, index: int = -1) -> dict[str, str]:
        """解析请求表单。

        ``keep_blank_values``：``token=``（拿不到 smsToken 就传空）这类空值
        不能被 parse_qs 吃掉，否则断言会变成 KeyError 而不是「值不对」。
        """
        from urllib.parse import parse_qs

        body = self.calls[index].get("data") or ""
        return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def make_client(responses, *, cookies=None, auto_suggest: bool = True, **kwargs):
    http = FakeHttp(responses, cookies=cookies, auto_suggest=auto_suggest)
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("retries", 0)
    return ZhipinLoginClient(http=http, **kwargs), http


def ok(payload: dict | None = None) -> FakeResponse:
    return FakeResponse({"code": 0, "message": "success", "zpData": payload or {}})


def fail(code: int, message: str = "", data: dict | None = None) -> FakeResponse:
    return FakeResponse({"code": code, "message": message, "zpData": data or {}})




class TestValidators:
    def test_normalize_phone(self):
        assert normalize_phone(" 138 0013-8000 ") == "13800138000"
        assert normalize_phone("+86(138)00138000") == "8613800138000"
        assert normalize_phone(None) == ""

    @pytest.mark.parametrize(
        "phone,dial,expected",
        [
            ("12800138000", "86", "手机号格式不正确"),
            ("1380013800", "86", "手机号格式不正确"),
            ("138001380000", "86", "手机号格式不正确"),
            ("1234", "852", "手机号格式不正确，请检查国家区号"),
        ],
    )
    def test_invalid_phones(self, phone, dial, expected):
        with pytest.raises(ValidationError, match=expected):
            client_module.validate_phone(phone, dial)

    def test_empty_phone(self):
        with pytest.raises(ValidationError, match="请输入手机号"):
            client_module.validate_phone("", "86")

    def test_valid_phones(self):
        client_module.validate_phone("13800138000", "86")
        client_module.validate_phone("19912345678", "86")
        client_module.validate_phone("87654321", "852")

    def test_validate_code(self):
        client_module.validate_code("123456")
        with pytest.raises(ValidationError, match="请输入短信验证码"):
            client_module.validate_code("")
        with pytest.raises(ValidationError, match="6 位数字"):
            client_module.validate_code("12345")

    def test_mask_phone(self):
        assert mask_phone("13800138000") == "138****8000"
        assert mask_phone("12345678") == "12****78"




class TestSendSmsCode:
    def test_payload_is_correct(self):
        client, http = make_client([ok({"retryAfter": 60})])
        client.send_sms_code("138 0013 8000")

        form = http.form_of()
        assert form["phone"] == "13800138000"
        assert form["regionCode"] == "86"
        assert form["smsType"] == str(SMS_SCENES["login"])
        assert http.last_call["url"] == client.base_url + ENDPOINTS["send_sms_code"]
        assert http.last_call["method"] == "POST"

    def test_success_returns_ticket(self):
        client, _ = make_client([ok({"retryAfter": 45})])
        ticket = client.send_sms_code("13800138000")

        assert ticket.phone_masked == "138****8000"
        assert ticket.retry_after == 45
        assert ticket.remaining_cooldown(now=ticket.sent_at) == 45
        assert client.last_ticket("13800138000") == ticket

    def test_local_cooldown_blocks_without_network_call(self):
        client, http = make_client([ok()])
        client.send_sms_code("13800138000")

        with pytest.raises(RateLimited) as info:
            client.send_sms_code("13800138000")

        assert info.value.retry_after > 0
        assert len(http.calls) == 1, "被本地冷却拦下时不应该再打服务端"

    def test_cooldown_expires_by_clock(self):
        now = [1000.0]
        client, http = make_client([ok(), ok()], clock=lambda: now[0])
        client.send_sms_code("13800138000")

        now[0] += 61  # 冷却结束
        client.send_sms_code("13800138000")
        assert len(http.calls) == 2

    def test_force_skips_local_cooldown(self):
        client, http = make_client([ok(), ok()])
        client.send_sms_code("13800138000")
        client.send_sms_code("13800138000", force=True)
        assert len(http.calls) == 2

    def test_server_rate_limit_carries_retry_after(self):
        """服务端限流后本地也要进入冷却，避免连续重试。"""
        client, _ = make_client([fail(1001, "发送太频繁了", {"retryAfter": 33})])
        with pytest.raises(RateLimited) as info:
            client.send_sms_code("13800138000")

        assert info.value.retry_after == 33
        with pytest.raises(RateLimited):
            client.send_sms_code("13800138000")

    def test_validation_happens_before_request(self):
        client, http = make_client([ok()])
        with pytest.raises(ValidationError):
            client.send_sms_code("1234")
        assert http.calls == []

    def test_verify_token_is_attached(self):
        client, http = make_client([ok()])
        client.send_sms_code("13800138000", verify_token="tok-123")

        form = http.form_of()
        assert form["verifyToken"] == "tok-123"
        assert form["validate"] == "tok-123"

    def test_scene_maps_to_sms_type(self):
        client, http = make_client([ok()])
        client.send_sms_code("13800138000", scene="register")
        assert http.form_of()["smsType"] == str(SMS_SCENES["register"])

        with pytest.raises(ValidationError, match="不支持的短信场景"):
            client.send_sms_code("13800138000", scene="nope")




class TestRiskControl:
    def test_security_check_raises_with_slider_params(self):
        client, _ = make_client(
            [fail(37, "请先完成安全验证", {"seed": "s-1", "ts": "t-1", "name": "n-1"})]
        )
        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code("13800138000")

        exc = info.value
        assert (exc.seed, exc.ts, exc.name) == ("s-1", "t-1", "n-1")
        assert exc.is_hard_block is False
        assert "verify.html" in exc.verify_page

    def test_cm_security_check_negative_code(self):
        client, _ = make_client([fail(-1000037, "安全校验")])
        with pytest.raises(RiskControlRequired):
            client.send_sms_code("13800138000")

    @pytest.mark.parametrize("code", [31, 32, -1000031, -1000032])
    def test_block_codes_raise_account_blocked(self, code):
        client, _ = make_client([fail(code, "IP 已被封禁")])
        with pytest.raises(AccountBlocked) as info:
            client.send_sms_code("13800138000")
        assert info.value.code == code

    @pytest.mark.parametrize("code", [35, 36, -1000035, -1000036])
    def test_gray_codes_require_verification(self, code):
        client, _ = make_client([fail(code)])
        with pytest.raises(RiskControlRequired):
            client.send_sms_code("13800138000")

    def test_anti_spider_code(self):
        client, _ = make_client([fail(38)])
        with pytest.raises(RiskControlRequired):
            client.send_sms_code("13800138000")




class TestLoginBySms:
    def test_success_extracts_token_and_cookies(self):
        client, http = make_client(
            [ok({"token": "TK-1", "userId": 100123, "name": "张三", "isNewUser": True})],
            cookies={"zp_at": "cookie-token", "other": "x"},
        )
        result = client.login_by_sms("13800138000", "123456")

        assert result.token == "TK-1"
        assert result.logged_in is True
        assert result.is_new_user is True
        assert result.user.user_id == "100123"
        assert result.user.phone_masked == "138****8000"
        assert result.cookies["zp_at"] == "cookie-token"

        form = http.form_of()
        assert form["phone"] == "13800138000"
        assert form["phoneCode"] == "123456"
        assert http.last_call["url"] == client.base_url + ENDPOINTS["login_by_sms"]

    def test_logged_in_via_cookie_only(self):
        client, _ = make_client([ok({})], cookies={"wt2": "abc"})
        result = client.login_by_sms("13800138000", "123456")
        assert result.token == ""
        assert result.logged_in is True

    def test_success_without_credentials_raises_incomplete(self):
        """消息里要能看到实到的形状，否则线上没法据此适配字段。"""
        client, _ = make_client([ok({})])
        with pytest.raises(LoginIncomplete) as info:
            client.login_by_sms("13800138000", "123456")
        assert "zpData" in str(info.value)
        assert "probe" in str(info.value)

    def test_login_without_body_token_still_wins_on_set_cookie(self):
        """真实站点的登录响应体里**没有** token——鉴权完全靠 Set-Cookie。

        登录页的成功处理（m.F）只读路由字段（identity / isCompletion / toUrl），
        从不抠 token。早期客户端只认 body 里的 token，真机上就卡在
        「接口返回成功，但没拿到凭证」，登录态压根没落盘。
        """
        resp = ok({"identity": 0, "isCompletion": True, "toUrl": "/web/geek/guide/"})
        resp.set_cookies = {"wt2": "cookie-session", "bst": "b"}
        client, _ = make_client([resp])
        result = client.login_by_sms("13800138000", "123456")

        assert result.token == ""
        assert result.logged_in is True
        assert result.new_cookies == {"wt2": "cookie-session", "bst": "b"}

    def test_unfamiliar_cookie_names_still_count_as_a_session(self):
        """Cookie 名可能不在 AUTH_COOKIES 里——实到的也算登录态，由 user_info 验。"""
        resp = ok({"toUrl": "/"})
        resp.set_cookies = {"__zp_stoken__": "abc", "segs": "x"}
        client, _ = make_client([resp])
        result = client.login_by_sms("13800138000", "123456")

        assert result.logged_in is True
        assert client.is_logged_in() is True

    def test_preexisting_cookies_are_not_mistaken_for_the_login_session(self):
        """登录前就有的 Cookie 不算「这次登录换到的凭证」。

        真实站点的登录响应体里没有 token，鉴权靠 Set-Cookie。登录这一步没种下
        任何 Cookie = 根本没换到登录态，不能拿登录前就在 jar 里的 Cookie 充数。
        """
        client, _ = make_client([ok({})], cookies={"sid": "already-there", "bl": "x"})
        with pytest.raises(LoginIncomplete) as info:
            client.login_by_sms("13800138000", "123456")
        assert "['bl', 'sid']" in str(info.value)

    def test_sms_token_from_send_is_passed_to_login_suggest(self):
        """发码的 zpData.token 是 smsToken，登录前置的 getLoginSuggest 要拿它对账。"""
        login_resp = ok({"toUrl": "/"})
        login_resp.set_cookies = {"wt2": "cookie-session"}
        client, http = make_client(
            [
                ok({"retryAfter": 60, "token": "SMS-TK"}),   # send
                login_resp,                                   # login（suggest 由夹具代答）
            ]
        )
        client.send_sms_code("13800138000")
        result = client.login_by_sms("13800138000", "123456")
        assert result.logged_in is True

        assert [c["url"] for c in http.calls] == [
            client.base_url + ENDPOINTS["send_sms_code"],
            client.base_url + ENDPOINTS["login_suggest"],
            client.base_url + ENDPOINTS["login_by_sms"],
        ]
        suggest = http.form_of(1)
        assert suggest["token"] == "SMS-TK"
        assert suggest["code"] == "123456"
        assert suggest["regionCode"] == "+86"
        assert "encryptedAccount" in suggest

    def test_login_suggest_failure_does_not_block_login(self):
        """登录页把 getLoginSuggest 裹在 try/catch 里，失败照样登录。"""
        login_resp = ok({"toUrl": "/"})
        login_resp.set_cookies = {"wt2": "ok"}
        client, _ = make_client(
            [
                ok({"retryAfter": 60, "token": "SMS-TK"}),
                fail(500, "suggest 挂了"),
                login_resp,
            ],
            auto_suggest=False,
        )
        client.send_sms_code("13800138000")
        result = client.login_by_sms("13800138000", "123456")
        assert result.logged_in is True

    def test_login_suggest_is_called_even_without_sms_token(self):
        """登录页**每次**都打 getLoginSuggest，拿不到 smsToken 就传空。

        ``send`` / ``login`` 分成两条命令时（两个进程）就是这样——总比不打好。
        """
        client, http = make_client([ok({"toUrl": "/"})], cookies={"wt2": "abc"})
        client.login_by_sms("13800138000", "123456")

        assert [c["url"] for c in http.calls] == [
            client.base_url + ENDPOINTS["login_suggest"],
            client.base_url + ENDPOINTS["login_by_sms"],
        ]
        assert http.form_of(0)["token"] == ""

    @pytest.mark.parametrize("code,expected", [(2001, 2001), (2002, 2002), (2003, 2003)])
    def test_code_rejections(self, code, expected):
        client, _ = make_client([fail(code, "验证码不对")])
        with pytest.raises(CodeRejected) as info:
            client.login_by_sms("13800138000", "123456")
        assert info.value.code == expected

    def test_account_locked(self):
        client, _ = make_client([fail(2004)])
        with pytest.raises(AccountBlocked):
            client.login_by_sms("13800138000", "123456")

    def test_unknown_error_code_becomes_api_error(self):
        client, _ = make_client([fail(8888, "未知错误")])
        from boss_login import ApiError

        with pytest.raises(ApiError, match="未知错误"):
            client.login_by_sms("13800138000", "123456")

    def test_code_format_validated_locally(self):
        client, http = make_client([ok()])
        with pytest.raises(ValidationError):
            client.login_by_sms("13800138000", "12")
        assert http.calls == []

    def test_successful_login_clears_cooldown(self):
        client, _ = make_client([ok(), ok({"token": "T"})])
        client.send_sms_code("13800138000")
        client.login_by_sms("13800138000", "123456")
        assert client.last_ticket("13800138000") is None


class TestLoginSlider:
    """登录那步的滑块衔接。契约来源 user-login.js 的 ``Ce() + je()``。"""

    @staticmethod
    def _solution(challenge: str = "c1", validate: str = "v1", seccode: str = "s1"):
        from boss_login.verify import SliderSolution

        return SliderSolution(challenge=challenge, validate=validate, seccode=seccode)

    @staticmethod
    def _challenge(challenge: str = "c-new", gt: str = "gt-new") -> FakeResponse:
        """getTypeV2 的响应形状：startCaptcha 是**字符串化的 JSON**。"""
        return FakeResponse(
            {
                "code": 0,
                "message": "Success",
                "zpData": {
                    "captchaType": 1,
                    "startCaptcha": json.dumps(
                        {"success": 1, "challenge": challenge, "gt": gt}, ensure_ascii=False
                    ),
                    "randKey": "rk",
                },
            }
        )

    def test_v2_form_has_phone_code_but_no_version(self):
        """``Ce()`` 与发码的 ``Ce(!0)`` 只差两处：没有 ``version``，多 ``phoneCode``。"""
        from boss_login.crypto import decrypt_account

        client, _ = make_client([])
        form = client._login_v2_form(
            "13800138000", "123456", solution=self._solution()
        )

        assert form["smsType"] == 7
        assert form["purpose"] == 0
        assert form["pk"] == "cpc_user_sign_up"
        assert form["regionCode"] == "+86"
        assert form["phoneCode"] == "123456"
        assert "version" not in form, "Ce() 不传 t，登录表单不该有 version"
        assert "phone" not in form
        assert decrypt_account(form["encryptedAccount"]) == "13800138000"
        assert form["challenge"] == "c1"
        assert form["validate"] == "v1"
        assert form["seccode"] == "s1"
        assert set(form) == {
            "regionCode", "pk", "smsType", "purpose", "phoneCode",
            "encryptedAccount", "challenge", "validate", "seccode",
        }

    def test_with_solution_goes_straight_to_phone_v2(self):
        client, http = make_client(
            [ok({"token": "TK", "userId": 1, "name": "n"})], cookies={"wt2": "c"}
        )
        client.login_by_sms("13800138000", "123456", solution=self._solution())

        assert http.last_call["url"].endswith(ENDPOINTS["login_by_sms_v2"])
        form = http.form_of()
        assert form["validate"] == "v1"
        assert form["phoneCode"] == "123456"

    def test_reuses_the_stored_send_ticket(self):
        """发码那次解出来的票据存着，登录直接复用（登录页 verifyInfo 就是共用的）。"""
        from boss_login.verify import SliderChallenge

        def solver(challenge: SliderChallenge):
            return self._solution(challenge.challenge, "send-v", "send-s")

        client, http = make_client(
            [
                fail(400061, "请完成滑块验证"),          # 发码 V1 被拦
                self._challenge("c-send", "gt-send"),    # getTypeV2
                ok({"retryAfter": 60}),                  # smsCodeV2 过了
                ok({"token": "TK", "userId": 1, "name": "n"}),  # phoneV2
            ],
            cookies={"wt2": "c"},
        )
        client.send_sms_code("13800138000", slider_solver=solver, force=True)
        client.login_by_sms("13800138000", "123456", slider_solver=solver)

        assert http.calls[-1]["url"].endswith(ENDPOINTS["login_by_sms_v2"])
        assert http.form_of(-1)["validate"] == "send-v", "登录应当复用发码那张票"

    def test_falls_back_to_a_fresh_solve_when_reuse_fails(self):
        """复用被拒（票据已烧/过期）时重新解一张，而不是把人丢回风控。"""
        from boss_login.verify import SliderChallenge, SliderSolution

        def solver(challenge: SliderChallenge):
            return SliderSolution(
                challenge=challenge.challenge,
                validate=f"v-{challenge.challenge}",
                seccode="s",
            )

        client, http = make_client(
            [
                fail(400061, "请完成滑块验证"),            # 复用发码那张被拒
                self._challenge("c-new", "gt-new"),        # 重新拉挑战
                ok({"token": "TK", "userId": 1, "name": "n"}),  # 新票过了
            ],
            cookies={"wt2": "c"},
        )
        client._slider_solutions["13800138000"] = self._solution("old", "old-v", "old-s")

        result = client.login_by_sms("13800138000", "123456", slider_solver=solver)
        assert result.logged_in is True
        assert http.form_of(-1)["validate"] == "v-c-new"

    def test_without_solver_rethrows_the_slider(self):
        """没给解题回调时不能自己瞎拖。"""
        client, _ = make_client([fail(400061, "请完成滑块验证")])
        with pytest.raises(RiskControlRequired) as info:
            client.login_by_sms("13800138000", "123456")
        assert info.value.code == 400061

    def test_retry_failure_mentions_phone_v2_not_the_bare_slider(self):
        """票据挂上了仍被拦，提示得说清「已经交过了」，别让人再拖一次白搭。"""
        from boss_login.verify import SliderChallenge, SliderSolution

        def dead(challenge: SliderChallenge):
            return SliderSolution(challenge=challenge.challenge, validate="dead", seccode="s")

        client, _ = make_client(
            [
                fail(400061, "请完成滑块验证"),   # V1 被拦
                self._challenge(),                # 拉挑战
                fail(400061, "请完成滑块验证"),   # phoneV2 带着票据仍被拦
            ]
        )
        with pytest.raises(RiskControlRequired) as info:
            client.login_by_sms("13800138000", "123456", slider_solver=dead)
        assert "phoneV2" in info.value.message
        assert "已经拖过了" in info.value.message




class TestTransport:
    def test_retries_then_succeeds(self):
        sleeps: list[float] = []
        client, http = make_client(
            [requests.ConnectionError("boom"), ok({"retryAfter": 60})],
            retries=2,
            backoff=0.5,
            sleeper=sleeps.append,
        )
        ticket = client.send_sms_code("13800138000")

        assert ticket.retry_after == 60
        assert len(http.calls) == 2
        assert sleeps == [0.5], "重试前应退避一次"

    def test_retries_exhausted(self):
        client, _ = make_client(
            [requests.Timeout("t1"), requests.Timeout("t2")], retries=1
        )
        with pytest.raises(TransportError, match="网络请求失败"):
            client.send_sms_code("13800138000")

    def test_5xx_is_retried(self):
        client, http = make_client(
            [FakeResponse(status=502), ok()], retries=1
        )
        client.send_sms_code("13800138000")
        assert len(http.calls) == 2

    def test_non_json_response(self):
        client, _ = make_client(
            [FakeResponse(text="<html>404</html>")], retries=0
        )
        with pytest.raises(TransportError, match="不是合法 JSON"):
            client.send_sms_code("13800138000")

    def test_default_headers_are_applied(self):
        client, http = make_client([ok()])
        client.send_sms_code("13800138000")
        headers = http.last_call["headers"]
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert "zhipin.com" in headers["Referer"]




class TestRunSmsLogin:
    def test_happy_path(self):
        client, http = make_client([ok({"retryAfter": 60}), ok({"token": "TK", "name": "李四"})])
        events: list[tuple[str, dict]] = []

        result = client.run_sms_login(
            "13800138000",
            lambda attempt, ticket: "123456",
            on_event=lambda name, payload: events.append((name, payload)),
        )

        assert result.token == "TK"
        names = [name for name, _ in events]
        assert names == ["code_sent", "need_code", "success"]
        # send + getLoginSuggest + login —— 登录页每次都打 suggest，客户端照做
        assert len(http.calls) == 3

    def test_wrong_code_then_correct(self):
        client, http = make_client(
            [ok(), fail(2001, "验证码错误，请重新输入"), ok({"token": "TK"})]
        )
        provided: list[int] = []

        def provider(attempt, ticket):
            provided.append(attempt)
            return "000000" if attempt == 1 else "123456"

        result = client.run_sms_login("13800138000", provider)

        assert result.token == "TK"
        assert provided == [1, 2]
        # send + (suggest + login)×2 次尝试
        assert len(http.calls) == 5

    def test_provider_can_abort(self):
        client, http = make_client([ok()])
        with pytest.raises(BossLoginError, match="已取消登录"):
            client.run_sms_login("13800138000", lambda attempt, ticket: None)
        assert len(http.calls) == 1, "取消后不应调用登录接口"

    def test_gives_up_after_max_attempts(self):
        responses = [ok()] + [fail(2001, "验证码错误") for _ in range(4)]
        client, _ = make_client(responses, max_code_attempts=3)

        with pytest.raises(CodeRejected):
            client.run_sms_login("13800138000", lambda attempt, ticket: "000000")

    def test_expired_code_triggers_resend(self):
        sleeps: list[float] = []
        now = [1000.0]
        responses = [
            ok({"retryAfter": 60}),   # 首次发送
            fail(2002, "验证码已过期"),  # 首次登录：过期
            ok({"retryAfter": 60}),   # 重发
            ok({"token": "TK"}),      # 二次登录成功
        ]
        client, http = make_client(
            responses, sleeper=sleeps.append, clock=lambda: now[0]
        )

        result = client.run_sms_login("13800138000", lambda attempt, ticket: "123456")

        assert result.token == "TK"
        assert 60 in sleeps, "重发前应等满冷却时间"
        # send + (suggest + login过期) + 重发 + (suggest + login成功)
        assert len(http.calls) == 6

    def test_reuses_existing_code_during_cooldown(self):
        client, http = make_client([ok({"retryAfter": 60}), ok({"token": "TK"})])
        client.send_sms_code("13800138000")  # 已经发过一次，进入冷却

        events: list[str] = []
        result = client.run_sms_login(
            "13800138000", lambda attempt, ticket: "123456", on_event=lambda n, p: events.append(n)
        )

        assert result.token == "TK"
        assert "cooldown_reused" in events
        # 冷却期内不重复发码：手动那次 send + (suggest + login)
        assert len(http.calls) == 3, "冷却期内不应重复发码"

    def test_risk_control_propagates(self):
        client, _ = make_client([fail(37, "请先完成安全验证", {"seed": "s"})])
        with pytest.raises(RiskControlRequired):
            client.run_sms_login("13800138000", lambda attempt, ticket: "123456")

    def test_slider_code_from_live_site_is_risk_control(self):
        """实测：真实站点回 {"code": 400061, "message": "请完成滑块验证"}，zpData 为空。

        这个码不在 SDK 常量表里（那是 31~38 的 passport 内部码），漏判就会退化成
        普通 ApiError，用户拿不到该有的处置指引。
        """
        client, _ = make_client([fail(400061, "请完成滑块验证")])
        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code("13800138000")

        assert info.value.code == 400061
        assert not info.value.is_hard_block

    def test_unknown_code_with_risk_wording_is_risk_control(self):
        """码表拿不全，同族的近亲码靠话术兜底。"""
        for code, message in [
            (400060, "需要安全验证"),
            (400062, "请完成滑块验证"),
            (999999, "触发了人机验证"),
        ]:
            client, _ = make_client([fail(code, message)])
            with pytest.raises(RiskControlRequired):
                client.send_sms_code("13800138000")

    def test_ordinary_failure_is_not_mistaken_for_risk_control(self):
        """反例：话术里带「验证码」的普通失败不能被误判成风控。"""
        for code, message in [
            (1004, "验证码发送失败，请稍后重试"),
            (1003, "该手机号暂时无法接收短信，请联系客服"),
            (2001, "验证码错误，请重新输入"),
        ]:
            client, _ = make_client([fail(code, message)])
            with pytest.raises(ApiError) as info:
                client.send_sms_code("13800138000")
            assert not isinstance(info.value, RiskControlRequired)

    def test_slider_challenge_actionable_via_verify_token(self):
        """带上人工取回的票据后，同一个号码应该能过。"""
        client, http = make_client(
            [
                fail(400061, "请完成滑块验证"),
                ok({"retryAfter": 60}),
            ]
        )
        with pytest.raises(RiskControlRequired):
            client.send_sms_code("13800138000")

        ticket = client.send_sms_code("13800138000", verify_token="solved")
        assert ticket.retry_after == 60

        assert http.form_of()["verifyToken"] == "solved"

    def test_resend_waits_out_server_cooldown(self):
        """本地时钟与服务端对不上时，重发应按服务端给的秒数再等一次。"""
        now = [1000.0]
        sleeps: list[float] = []

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        client, http = make_client(
            [
                ok({"retryAfter": 60}),                  # 1. 首次发送
                fail(2002, "验证码已过期"),                # 2. 登录：过期
                fail(1001, "太频繁", {"retryAfter": 20}),  # 3. 重发：撞服务端限流
                ok({"retryAfter": 60}),                  # 4. 等满 20s 后重发成功
                ok({"token": "TK"}),                     # 5. 登录成功
            ],
            sleeper=sleeper,
            clock=lambda: now[0],
        )

        result = client.run_sms_login("13800138000", lambda attempt, ticket: "123456")

        assert result.token == "TK"
        assert 60 in sleeps, "本地冷却 60s 应先等满"
        assert 20 in sleeps, "服务端要求的 20s 也应等待"
        # send + (suggest + login过期) + 重发撞限流 + 等满重发 + (suggest + login成功)
        assert len(http.calls) == 7

    def test_resend_gives_up_when_wait_is_absurd(self):
        """服务端要求的等待时间过长时直接失败，不做无意义的长眠。"""
        now = [1000.0]
        sleeps: list[float] = []

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        client, _ = make_client(
            [
                ok({"retryAfter": 60}),
                fail(2002, "验证码已过期"),
                fail(1001, "太频繁", {"retryAfter": 99999}),
            ],
            sleeper=sleeper,
            clock=lambda: now[0],
        )

        with pytest.raises(RateLimited):
            client.run_sms_login("13800138000", lambda attempt, ticket: "123456")
        assert max(sleeps) < 99999, "不应真的睡 99999 秒"




class TestSessionPersistence:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "session.json"
        save_session(
            StoredSession(token="TK", cookies={"zp_at": "c"}, phone_masked="138****8000"),
            path,
        )
        stored = load_session(path)

        assert stored.token == "TK"
        assert stored.cookies == {"zp_at": "c"}
        assert stored.phone_masked == "138****8000"
        assert stored.is_empty is False

    def test_load_missing_file(self, tmp_path: Path):
        assert load_session(tmp_path / "nope.json").is_empty is True

    def test_load_corrupted_file(self, tmp_path: Path):
        path = tmp_path / "session.json"
        path.write_text("{ not json", encoding="utf-8")
        assert load_session(path).is_empty is True

    def test_clear(self, tmp_path: Path):
        from boss_login import clear_session

        path = tmp_path / "session.json"
        save_session(StoredSession(token="TK"), path)
        assert clear_session(path) is True
        assert clear_session(path) is False
        assert load_session(path).is_empty is True

    def test_persist_login_writes_full_state(self, tmp_path: Path):
        from boss_login import LoginResult, UserInfo

        client, _ = make_client([])
        result = LoginResult(
            token="TK",
            user=UserInfo(user_id="1", name="王五", phone_masked="138****8000"),
            cookies={"zp_at": "c"},
        )
        path = persist_login(client, result, session_path=tmp_path / "s.json")
        stored = load_session(path)

        assert stored.token == "TK"
        assert stored.user["name"] == "王五"
        assert stored.cookies == {"zp_at": "c"}

    def test_create_client_injects_stored_cookies(self, tmp_path: Path):
        path = tmp_path / "session.json"
        save_session(StoredSession(token="TK", cookies={"zp_at": "from-disk"}), path)

        client = create_client(session_path=path)
        assert client.http.cookies.get_dict()["zp_at"] == "from-disk"
        assert client.is_logged_in() is True

        fresh = create_client(session_path=path, load_stored=False)
        assert fresh.is_logged_in() is False

    def test_create_client_accepts_token_only_session(self, tmp_path: Path):
        """落盘的会话可能只剩 token——也得算登录态，不能说「本地没有登录态」。"""
        path = tmp_path / "session.json"
        save_session(StoredSession(token="TK-ONLY"), path)

        client = create_client(session_path=path)
        assert client.is_logged_in() is True
        assert client.auth_token == "TK-ONLY"

    def test_create_client_accepts_unfamiliar_cookie_names(self, tmp_path: Path):
        """Cookie 名不在 AUTH_COOKIES 里也得认——落盘时已经认定是登录凭证了。"""
        path = tmp_path / "session.json"
        save_session(StoredSession(cookies={"__zp_stoken__": "abc", "segs": "x"}), path)

        client = create_client(session_path=path)
        assert client.is_logged_in() is True




class TestProbe:
    def test_report_shape(self):
        from boss_login.config import CANDIDATE_ENDPOINTS

        expected = sum(len(v) for v in CANDIDATE_ENDPOINTS.values())
        responses = []
        for _ in range(expected):
            responses.append(FakeResponse({"code": 1, "message": "参数错误"}, status=200))

        client, _ = make_client(responses)
        report = client.probe_endpoints()

        assert len(report) == expected
        assert {"key", "path", "status", "alive"} <= set(report[0])
        assert all(item["alive"] for item in report)

    def test_404_marked_dead(self):
        from boss_login.config import CANDIDATE_ENDPOINTS

        expected = sum(len(v) for v in CANDIDATE_ENDPOINTS.values())
        client, _ = make_client([FakeResponse(status=404)] * expected)
        report = client.probe_endpoints()
        assert all(item["alive"] is False for item in report)
