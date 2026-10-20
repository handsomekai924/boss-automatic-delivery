"""端到端集成测试：用本地假服务端走完整登录链路。"""

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
    """完整登录流程的端到端用例。"""

    def test_complete_happy_path(self, client, tmp_path: Path):
        """验发码到登录再落盘复用的完整成功路径。"""
        events: list[str] = []

        def provider(attempt, ticket):
            """索要验证码时取回假服务端里的短信码。"""
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
        """验人工取回的 verify_token 可解开风控后继续发码。"""
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
        """验被封手机号发码抛 `AccountBlocked`。"""
        with pytest.raises(AccountBlocked):
            client.send_sms_code("13800000000")

    def test_server_side_rate_limit_over_real_http(self, client):
        """验服务端限流在真实 HTTP 上抛 `RateLimited`。"""
        client.send_sms_code("13800138000")

        with pytest.raises(RateLimited) as info:
            client.send_sms_code("13800138000", force=True)
        assert info.value.retry_after > 0

    def test_wrong_code_then_success(self, client):
        """验错码后可再次输入正确验证码登录成功。"""
        client.send_sms_code("13800139000")
        correct = read_code("13800139000")

        attempts: list[int] = []

        def provider(attempt, _ticket):
            """第一次回错码，之后回正确码。"""
            attempts.append(attempt)
            return "000000" if attempt == 1 else correct

        result = client.run_sms_login("13800139000", provider)
        assert result.logged_in
        assert result.new_cookies
        assert attempts == [1, 2]

    def test_giving_up_after_repeated_wrong_codes(self, client):
        """验连续错码后放弃并抛 `CodeRejected`。"""
        client.send_sms_code("13800137000")
        with pytest.raises(CodeRejected):
            client.run_sms_login("13800137000", lambda attempt, _t: "000000")

    def test_expired_code_leads_to_resend(self, client):
        """验验证码过期会触发重发并再次索要验证码。"""
        client.send_sms_code("13800136000")
        record = STORE["13800136000"]
        record["expire_at"] = time.time() - 1
        record["sent_at"] = time.time() - 61

        calls: list[int] = []

        def provider(attempt, _ticket):
            """记录索码次数并取回短信码。"""
            calls.append(attempt)
            return read_code("13800136000")

        result = client.run_sms_login("13800136000", provider)
        assert result.logged_in
        assert len(calls) >= 2, "首次过期后应重发并再次索要验证码"

    def test_logout_clears_server_session_and_cookies(self, client):
        """验登出清空服务端会话与本地 Cookie。"""
        client.send_sms_code("13800135000")
        result = client.run_sms_login("13800135000", lambda _a, _t: read_code("13800135000"))
        assert result.logged_in

        client.logout()
        assert client.is_logged_in() is False


class TestSessionFileAcrossProcesses:
    """会话文件跨客户端复用的用例。"""

    def test_saved_session_survives_new_client(self, client, tmp_path: Path):
        """验落盘会话可被新客户端复用。"""
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
    """滑块验证链路的集成用例。"""

    def test_send_sms_rides_through_slider_and_retries(self, client):
        """验命中滑块后带票据重试 smsCodeV2 成功。"""
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
        """验 V2 发码表单字段与登录页一致。"""
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
        """验 V1 接口忽略滑块票据。"""
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
        """验业务请求的滑块挑战来自 passport 族。"""
        seen: list[str] = []

        def solver(challenge: SliderChallenge) -> SliderSolution:
            """记录挑战 gt 并交给假解题器。"""
            seen.append(challenge.gt)
            return fake_solver(challenge)

        client.send_sms_code(SLIDER_PHONE, slider_solver=solver, force=True)
        assert seen and seen[0].startswith("mock-pass-gt-"), seen

    def test_verify_page_uses_secureflow_family_challenge(self, client):
        """验核验页滑块挑战来自 secureflow 族。"""
        seen: list[str] = []

        def solver(challenge: SliderChallenge) -> SliderSolution:
            """记录挑战 gt 并交给假解题器。"""
            seen.append(challenge.gt)
            return fake_solver(challenge)

        client.run_slider_verify(solver=solver)
        assert seen and seen[0].startswith("mock-flow-gt-"), seen

    def test_full_login_through_slider(self, client):
        """验走完滑块后能完整登录成功。"""
        result = client.run_sms_login(
            SLIDER_PHONE,
            lambda _a, _t: read_code(SLIDER_PHONE),
            slider_solver=fake_solver,
        )
        assert result.logged_in is True

    def test_login_v2_form_matches_the_login_page_shape(self, client):
        """验登录表单字段与登录页一致。"""
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
        """验登录不带票据会被拦下。"""
        from tools.mock_server import LAST_LOGIN_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        client._slider_solutions.pop(SLIDER_PHONE, None)
        with pytest.raises(RiskControlRequired) as info:
            client.login_by_sms(SLIDER_PHONE, read_code(SLIDER_PHONE))
        assert info.value.code == 400061
        assert LAST_LOGIN_REQUEST["version"] == 1

    def test_login_reuses_the_send_ticket_like_the_page_does(self, client):
        """验登录复用发码那张滑块票据。"""
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
        """验复用票据失效时重新解一张再登录。"""
        from tools.mock_server import LAST_LOGIN_REQUEST

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        burned = client._slider_solutions[SLIDER_PHONE]
        client.submit_slider_solution(burned)

        def fresh(challenge: SliderChallenge) -> SliderSolution:
            """给出一张全新的假票据。"""
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
        """验 V1 登录接口忽略滑块票据。"""
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
        """验票据已挂仍被拒时提示已拖过滑块。"""
        from tools.mock_server import BURNED_TICKETS

        client.send_sms_code(SLIDER_PHONE, slider_solver=fake_solver, force=True)
        dead = client._slider_solutions[SLIDER_PHONE]
        BURNED_TICKETS.add(dead.validate)

        def also_dead(challenge: SliderChallenge) -> SliderSolution:
            """给出一张同样已被作废的假票据。"""
            return SliderSolution(
                challenge=challenge.challenge, validate="still-burned", seccode="s"
            )

        BURNED_TICKETS.add("still-burned")
        with pytest.raises(RiskControlRequired) as info:
            client.login_by_sms(SLIDER_PHONE, read_code(SLIDER_PHONE), slider_solver=also_dead)
        assert "phoneV2" in info.value.message
        assert "已经拖过了" in info.value.message

    def test_without_solver_still_raises_risk_control(self, client):
        """验未提供解题回调时仍抛风控异常。"""
        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code(SLIDER_PHONE)
        assert info.value.code == 400061

    def test_validate_alone_does_not_unlock_the_business_request(self, client):
        """验仅调 validate 不会放行业务请求。"""
        from tools.mock_server import LAST_SMS_REQUEST

        solution = client.run_slider_verify(solver=fake_solver)
        assert solution.validate.startswith("validate-for-")

        assert "mock_slider_pass" in client.http.cookies.get_dict()

        with pytest.raises(RiskControlRequired) as info:
            client.send_sms_code(SLIDER_PHONE, force=True)
        assert info.value.code == 400061
        assert not LAST_SMS_REQUEST["headers"].get("Zp-Captcha-Validate")

    def test_validate_burns_the_ticket_so_pre_validating_is_a_dead_end(self, client):
        """验 validate 会烧掉票据，先 validate 再重试是死路。"""
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
        """验滑块核验事件按序发出。"""
        events: list[str] = []
        client.run_slider_verify(
            solver=fake_solver, on_event=lambda name, _p: events.append(name)
        )
        assert events == ["slider_challenge", "slider_passed"]

    def test_non_geetest_channel_is_rejected(self, client, monkeypatch):
        """验非极验通道会停下报错。"""

        class FakeResponse:
            """假的接口响应对象。"""

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
        """验不完整的票据会被 `ApiError` 拒绝。"""
        with pytest.raises(ApiError):
            client.submit_slider_solution(
                SliderSolution(challenge="c", validate="", seccode="")
            )

    def test_solver_returning_none_abandons(self, client):
        """验解题回调返回 `None` 视为放弃。"""
        with pytest.raises(Exception) as info:
            client.solve_slider(solver=lambda _c: None)
        assert "放弃" in str(info.value)
