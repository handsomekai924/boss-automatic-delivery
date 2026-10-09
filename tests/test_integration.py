"""端到端集成测试：起一个本地假服务端，用真实 HTTP 走完整登录流程。

与 test_client.py 的区别：这里不打桩网络层，Cookie 会话、表单编码、重定向、
错误码解析全部走真实链路，验证的是「装配起来能不能跑」而不是「单个函数对不对」。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from boss_login import (
    AccountBlocked,
    ApiError,
    CodeRejected,
    RateLimited,
    RiskControlRequired,
    SliderChallenge,
    SliderSolution,
    create_client,
    load_session,
    persist_login,
)
from tools.mock_server import RESEND_COOLDOWN_SECONDS, SLIDER_PASS_COOKIE, STORE


def read_code(phone: str) -> str:
    """从假服务端的存储里取出验证码，模拟「用户看短信」。"""
    return STORE[phone]["code"]




class TestFullFlow:
    def test_complete_happy_path(self, client, tmp_path: Path):
        """发码 → 输码 → 登录 → 落盘 → 带 Cookie 拉用户信息。

        真实站点登录响应体里**没有** token——鉴权靠 Set-Cookie；落盘后新客户端可复用。
    """
        events: list[str] = []

        def provider(attempt, ticket):
            assert ticket is not None
            assert ticket.phone_masked == "138****8000"
            return read_code("13800138000")

        result = client.run_sms_login(
            "13800138000", provider, on_event=lambda name, _p: events.append(name)
        )

        assert result.token == ""
        assert result.user.name == "求职者8000"
        assert result.is_new_user is True
        assert result.logged_in is True
        assert result.new_cookies["zp_at"].startswith("mock-token-")
        assert result.cookies["zp_at"] == result.new_cookies["zp_at"]

        assert events == ["code_sent", "need_code", "success"]

        path = persist_login(client, result, session_path=tmp_path / "session.json")
        restored = create_client(session_path=path, base_url=client.base_url)
        assert restored.is_logged_in() is True

        user = restored.fetch_user_info()
        assert user.name == "求职者8000"

    def test_verify_token_from_browser_unblocks_risk_control(self, client):
        """命中风控时抛出参数，带上人工取回的票据后即可继续。"""
        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code("13800138010")

        exc = info.value
        assert exc.code == 37
        assert exc.seed == "mock-seed"
        assert exc.name == "geetest"
        assert exc.ts.isdigit()

        ticket = client.send_sms_code("13800138010", verify_token="human-solved-token")
        assert ticket.retry_after == RESEND_COOLDOWN_SECONDS

    def test_blocked_phone(self, client):
        with pytest.raises(AccountBlocked):
            client.send_sms_code("13800000000")

    def test_server_side_rate_limit_over_real_http(self, client):
        """本地冷却会先拦住重发，用 ``force`` 跳过本地判断，专门验证服务端 1001 限流。"""
        client.send_sms_code("13800138000")

        with pytest.raises(RateLimited) as info:
            client.send_sms_code("13800138000", force=True)
        assert info.value.retry_after > 0

    def test_wrong_code_then_success(self, client):
        client.send_sms_code("13800139000")
        correct = read_code("13800139000")

        attempts: list[int] = []

        def provider(attempt, _ticket):
            attempts.append(attempt)
            return "000000" if attempt == 1 else correct

        result = client.run_sms_login("13800139000", provider)
        assert result.logged_in
        assert result.new_cookies
        assert attempts == [1, 2]

    def test_giving_up_after_repeated_wrong_codes(self, client):
        client.send_sms_code("13800137000")
        with pytest.raises(CodeRejected):
            client.run_sms_login("13800137000", lambda attempt, _t: "000000")

    def test_expired_code_leads_to_resend(self, client):
        client.send_sms_code("13800136000")
        # 模拟用户磨蹭太久：验证码已过期，且服务端的 60s 冷却也早已过去
        record = STORE["13800136000"]
        record["expire_at"] = time.time() - 1
        record["sent_at"] = time.time() - 61

        calls: list[int] = []

        def provider(attempt, _ticket):
            calls.append(attempt)
            return read_code("13800136000")

        result = client.run_sms_login("13800136000", provider)
        assert result.logged_in
        assert len(calls) >= 2, "首次过期后应重发并再次索要验证码"

    def test_logout_clears_server_session_and_cookies(self, client):
        client.send_sms_code("13800135000")
        result = client.run_sms_login("13800135000", lambda _a, _t: read_code("13800135000"))
        assert result.logged_in

        client.logout()
        assert client.is_logged_in() is False


class TestSessionFileAcrossProcesses:
    def test_saved_session_survives_new_client(self, client, tmp_path: Path):
        """鉴权靠 Cookie：落盘的就是登录那次种下的。"""
        client.send_sms_code("13800134000")
        result = client.run_sms_login("13800134000", lambda _a, _t: read_code("13800134000"))

        path = persist_login(client, result, session_path=tmp_path / "s.json")
        stored = load_session(path)
        assert stored.phone_masked == "138****4000"
        assert stored.token == result.token
        assert stored.age_seconds >= 0
        assert stored.cookies["zp_at"] == result.new_cookies["zp_at"]

        fresh = create_client(session_path=path, base_url=client.base_url)
        assert fresh.is_logged_in() is True
        assert fresh.http.cookies.get_dict()["zp_at"] == result.new_cookies["zp_at"]




SLIDER_PHONE = "13800138020"


def fake_solver(challenge: SliderChallenge) -> SliderSolution:
    """扮演「你本人拖完了滑块」：不碰网络，直接给出三张票据。"""
    return SliderSolution(
        challenge=challenge.challenge,
        validate=f"validate-for-{challenge.challenge}",
        seccode=f"seccode-for-{challenge.challenge}",
    )


class TestSliderVerifyChain:
    def test_send_sms_rides_through_slider_and_retries(self, client):
        """命中 400061 → 拉挑战 → 解题 → **打 smsCodeV2 带票据**重试。

        三处都要对，错一处服务端就还是 400061（真机踩过两轮）：
        路径是 V2、票据是表单三键 challenge/validate/seccode、不先调 validate
        （极验票据一次性，交出去就没了）。
        

        重试落在 V2，票据在**表单**（不是 Zp-Captcha-* 头）；业务链不烧票据。
    """
        events: list[str] = []
        from tools.mock_server import BURNED_TICKETS, LAST_SMS_REQUEST

        ticket = client.send_sms_code(
            SLIDER_PHONE,
            slider_solver=fake_solver,
            on_event=lambda name, _p: events.append(name),
        )

        assert ticket.phone_masked == "138****8020"
        assert SLIDER_PHONE in STORE, "重试应当真的把验证码发出去"
        assert "slider_required" in events
        assert "slider_retry" in events
        assert "slider_passed" not in events
        assert not BURNED_TICKETS, "业务链不能调 validate —— 那会把一次性票据烧掉"

        assert LAST_SMS_REQUEST["path"] == "smsCodeV2"
        assert LAST_SMS_REQUEST["form"].get("challenge")
        assert LAST_SMS_REQUEST["form"].get("validate")
        assert LAST_SMS_REQUEST["form"].get("seccode")
        assert not LAST_SMS_REQUEST["headers"].get("Zp-Captcha-Validate")

    def test_v2_form_matches_the_login_page_shape(self, client):
        """V2 表单照抄 user-login.js 的 Ce(!0)+je()，不是我们自己凑的。

        少了 encryptedAccount / version / smsType=7，或多带了 phone /
        verifyToken，线上就不会认。
        """
        from boss_login.crypto import decrypt_account
        from tools.mock_server import LAST_SMS_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        form = LAST_SMS_REQUEST["form"]

        assert form["smsType"] == "7"
        assert form["version"] == "1"
        assert form["pk"] == "cpc_user_sign_up"
        assert form["purpose"] == "0"
        assert form["regionCode"] == "+86"
        assert "phone" not in form
        assert decrypt_account(form["encryptedAccount"]) == SLIDER_PHONE
        assert "verifyToken" not in form
        assert "captchaToken" not in form
        assert "randKey" not in form

    def test_v1_ignores_slider_tickets(self, client):
        """回归护栏：票据只有 V2 认。带着三件套去砸 V1，真机照样 400061。"""
        from tools.mock_server import BURNED_TICKETS, LAST_SMS_REQUEST

        solution = client.solve_slider(solver=fake_solver)
        assert not BURNED_TICKETS, "solve_slider 不该碰 validate"

        response = client._post(
            client.endpoints["send_sms_code"],
            {
                "phone": SLIDER_PHONE,
                "regionCode": "86",
                "smsType": 1,
                **solution.as_form_fields(),
            },
        )
        assert response.code == 400061
        assert LAST_SMS_REQUEST["version"] == 1

    def test_business_slider_uses_passport_family_challenge(self, client):
        """业务请求的挑战必须取自 passport 族（getTypeV2），不是 verify.html 那族。

        两族下发不同的极验 gt；拿错族的票据去打业务接口，服务端不认（真机踩过）。
        """
        seen: list[str] = []

        def solver(challenge: SliderChallenge) -> SliderSolution:
            seen.append(challenge.gt)
            return fake_solver(challenge)

        client.send_sms_code(SLIDER_PHONE, slider_solver=solver, force=True)
        assert seen and seen[0].startswith("mock-pass-gt-"), seen

    def test_verify_page_uses_secureflow_family_challenge(self, client):
        """库层的 run_slider_verify 走的是 verify.html 那族，gt 跟业务族不同。"""
        seen: list[str] = []

        def solver(challenge: SliderChallenge) -> SliderSolution:
            seen.append(challenge.gt)
            return fake_solver(challenge)

        client.run_slider_verify(solver=solver)
        assert seen and seen[0].startswith("mock-flow-gt-"), seen

    def test_full_login_through_slider(self, client):
        result = client.run_sms_login(
            SLIDER_PHONE,
            lambda _a, _t: read_code(SLIDER_PHONE),
            slider_solver=fake_solver,
        )
        assert result.logged_in is True

    def test_login_v2_form_matches_the_login_page_shape(self, client):
        """登录表单照抄 user-login.js 的 ``Ce() + je()``。

        跟发码那张（``Ce(!0)``）只差两处：**没有** ``version``，但**多**一个
        ``phoneCode``。少写 phoneCode 服务端当没输验证码；留着 version 或
        phone 则跟登录页对不上。
        

        ``Ce()`` 不传 t → 无 version:1；票据仍是极验三键。
    """
        from boss_login.crypto import decrypt_account
        from tools.mock_server import LAST_LOGIN_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        code = read_code(SLIDER_PHONE)
        client.login_by_sms(
            SLIDER_PHONE,
            code,
            slider_solver=fake_solver,
            solution=client._slider_solutions[SLIDER_PHONE],
        )
        form = LAST_LOGIN_REQUEST["form"]

        assert LAST_LOGIN_REQUEST["path"] == "phoneV2"
        assert form["smsType"] == "7"
        assert form["purpose"] == "0"
        assert form["pk"] == "cpc_user_sign_up"
        assert form["regionCode"] == "+86"
        assert form["phoneCode"] == code
        assert "version" not in form
        assert "phone" not in form
        assert decrypt_account(form["encryptedAccount"]) == SLIDER_PHONE
        assert form["challenge"] and form["validate"] and form["seccode"]
        assert "verifyToken" not in form
        assert "captchaToken" not in form
        assert "randKey" not in form
        assert not LAST_LOGIN_REQUEST["headers"].get("Zp-Captcha-Validate")

    def test_login_without_ticket_is_rejected_like_the_real_site(self, client):
        """真实站点跑出来的形状：发码过了滑块，登录没带票据照样 400061。

        登录页 Ce() 会把 verifyInfo 原样挂到 login 上，所以那一步**必须**带
        票据。这里钉住「裸的 login/phone 会被拦」，免得哪天把登录的滑块
        检查拆掉还测成绿的。
        

        假装没有现成票据，钉住「裸的 login/phone 会被拦」。
    """
        from tools.mock_server import LAST_LOGIN_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        client._slider_solutions.pop(SLIDER_PHONE, None)
        with pytest.raises(RiskControlRequired) as info:
            client.login_by_sms(SLIDER_PHONE, read_code(SLIDER_PHONE))
        assert info.value.code == 400061
        assert LAST_LOGIN_REQUEST["version"] == 1

    def test_login_reuses_the_send_ticket_like_the_page_does(self, client):
        """登录页的 ``verifyInfo`` 跨发码 / 登录共用，``Ce(!0)`` 和 ``Ce()`` 挂
        的是**同一张**票。发码那次的票据登录先试着复用，不再让你拖一遍。
        

        业务请求不烧票据——只有 zpsecureflow validate 会（登录页也这么用）。
    """
        from tools.mock_server import BURNED_TICKETS, LAST_LOGIN_REQUEST, LAST_SMS_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        send_ticket = LAST_SMS_REQUEST["form"]["validate"]

        result = client.login_by_sms(
            SLIDER_PHONE, read_code(SLIDER_PHONE), slider_solver=fake_solver
        )
        assert result.logged_in is True
        assert LAST_LOGIN_REQUEST["form"]["validate"] == send_ticket
        assert not BURNED_TICKETS

    def test_login_falls_back_to_a_fresh_solve_when_reuse_is_burned(self, client):
        """复用那张已经被 validate 烧掉时，得重新解一张，不能硬着头皮再交一次。"""
        from tools.mock_server import LAST_LOGIN_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        burned = client._slider_solutions[SLIDER_PHONE]
        client.submit_slider_solution(burned)

        def fresh(challenge: SliderChallenge) -> SliderSolution:
            return SliderSolution(
                challenge=challenge.challenge,
                validate="fresh-validate",
                seccode="fresh-seccode",
            )

        result = client.login_by_sms(
            SLIDER_PHONE, read_code(SLIDER_PHONE), slider_solver=fresh
        )
        assert result.logged_in is True
        assert LAST_LOGIN_REQUEST["form"]["validate"] == "fresh-validate"
        assert LAST_LOGIN_REQUEST["path"] == "phoneV2"

    def test_login_v1_ignores_slider_tickets(self, client):
        """回归护栏：票据只有 phoneV2 认。带着三件套去砸 login/phone，照样 400061。"""
        from tools.mock_server import LAST_LOGIN_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        solution = client._slider_solutions[SLIDER_PHONE]

        response = client._post(
            client.endpoints["login_by_sms"],
            {
                "phone": SLIDER_PHONE,
                "phoneCode": read_code(SLIDER_PHONE),
                "regionCode": "86",
                "smsType": 1,
                **solution.as_form_fields(),
            },
        )
        assert response.code == 400061
        assert LAST_LOGIN_REQUEST["version"] == 1

    def test_login_slider_retry_failure_says_the_ticket_was_attached(self, client):
        """票据挂上了还不认，别再把人绕回「请完成滑块验证」——那条路刚走完。"""
        from tools.mock_server import BURNED_TICKETS

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        dead = client._slider_solutions[SLIDER_PHONE]
        BURNED_TICKETS.add(dead.validate)

        def also_dead(challenge: SliderChallenge) -> SliderSolution:
            return SliderSolution(
                challenge=challenge.challenge, validate="still-burned", seccode="s"
            )

        BURNED_TICKETS.add("still-burned")
        with pytest.raises(RiskControlRequired) as info:
            client.login_by_sms(SLIDER_PHONE, read_code(SLIDER_PHONE), slider_solver=also_dead)
        assert "phoneV2" in info.value.message
        assert "已经拖过了" in info.value.message

    def test_without_solver_still_raises_risk_control(self, client):
        """没给解题回调时不能自己瞎拖，得把决定权交回调用方。"""
        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code(SLIDER_PHONE)
        assert info.value.code == 400061

    def test_validate_alone_does_not_unlock_the_business_request(self, client):
        """回归护栏：validate 成功 ≠ 业务请求放行。

        这条曾经是错的——假服务端用 Cookie 放行，把「只调 validate」这条
        死路测成了绿的。线上真实行为是票据必须挂在业务请求上。
        

        Cookie 不是通行证：validate 被调过、Cookie 种了，裸业务请求仍要被拦。
    """
        from tools.mock_server import LAST_SMS_REQUEST

        solution = client.run_slider_verify(solver=fake_solver)
        assert solution.validate.startswith("validate-for-")

        assert "mock_slider_pass" in client.http.cookies.get_dict()

        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code(SLIDER_PHONE, force=True)
        assert info.value.code == 400061
        assert not LAST_SMS_REQUEST["headers"].get("Zp-Captcha-Validate")

    def test_validate_burns_the_ticket_so_pre_validating_is_a_dead_end(self, client):
        """先 validate 再带**同一张**票重试 = 死路。

        极验的 geetest_validate 是一次性凭证。真机上踩过：拖完滑块先调
        zpsecureflow/validate（code=0），再把同一套 Zp-Captcha-* 挂到 send/smsCode
        上，服务端照样回 400061——票据在 validate 那步已经用掉了。
        

        同一张票再挂业务请求也不算数——已经烧了。
    """
        from tools.mock_server import BURNED_TICKETS

        burned = SliderSolution(challenge="c-burn", validate="v-burn", seccode="s-burn")
        client.submit_slider_solution(burned)
        assert "v-burn" in BURNED_TICKETS

        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code(
                SLIDER_PHONE, slider_solver=lambda _c: burned, force=True
            )
        assert info.value.code == 400061
        assert "票据也挂在 smsCodeV2 的表单上了" in info.value.message

    def test_run_slider_verify_emits_events_in_order(self, client):
        events: list[str] = []
        client.run_slider_verify(
            solver=fake_solver, on_event=lambda name, _p: events.append(name)
        )
        assert events == ["slider_challenge", "slider_passed"]

    def test_non_geetest_channel_is_rejected(self, client, monkeypatch):
        """换了通道（阿里/易盾/图片）就得停下来看协议，不能硬套极验。"""

        class FakeResponse:
            ok = True
            code = 0
            message = "Success"
            data = {
                "captchaType": 2,
                "captchaName": "阿里云验证码",
                "startCaptcha": json.dumps({"gt": "g", "challenge": "c"}),
            }

        monkeypatch.setattr(client, "_get", lambda *_a, **_k: FakeResponse())
        with pytest.raises(Exception) as info:
            client.run_slider_verify(solver=fake_solver)
        assert "阿里云验证码" in str(info.value)

    def test_validate_rejects_incomplete_ticket(self, client):
        with pytest.raises(ApiError):
            client.submit_slider_solution(
                SliderSolution(challenge="c", validate="", seccode="")
            )

    def test_solver_returning_none_abandons(self, client):
        with pytest.raises(Exception) as info:
            client.solve_slider(solver=lambda _c: None)
        assert "放弃" in str(info.value)
