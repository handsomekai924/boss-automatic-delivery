"""滑块验证协议层的纯函数测试。

不碰网络：traceId 生成、挑战/票据解析、帮助页渲染、请求头组装全可离线对拍。
"""

from __future__ import annotations

import json

import pytest

from boss_login import config as C
from boss_login.errors import ValidationError
from boss_login.verify import (
    SliderSolution,
    build_helper_html,
    generate_trace_id,
    parse_challenge,
    parse_solution,
    validate_request_headers,
    _compute_checksum,
)


class TestTraceId:
    """traceId 生成与校验符基准对拍。

    Attributes:
        KNOWN_CHECKSUMS: seed → 期望校验符基准表，含中间乘积超 `2**53` 的用例。
    """

    KNOWN_CHECKSUMS = {
        "abc123xyz": "BYG",
        "": "000",
        "0123456789abcdef012345": "pER",
        "F-0000000000000abcdef": "0ds",
        "0123456789abcdef0123456789012345678901234567890": "OYC",
    }

    @pytest.mark.parametrize("seed,expected", sorted(KNOWN_CHECKSUMS.items()))
    def test_checksum_matches_node_reference(self, seed, expected):
        """验校验符与 Node 参考基准逐位一致。"""
        assert _compute_checksum(seed) == expected

    def test_shape(self):
        """验 traceId 的前缀、长度与各段取值。"""
        value = generate_trace_id(now=lambda: 1700000000.123, rand=lambda: 0.5)
        assert value.startswith("F-")
        assert len(value) == 2 + 13 + 6 + 3
        body = value[2:]
        assert body[:13] == "0018bcfe5687b"
        assert body[13:19] == "vvvvvv"
        assert body[19:] == "4Mp"

    def test_is_deterministic_under_injected_clock(self):
        """验注入固定时钟与随机源时 traceId 确定。"""
        kwargs = {"now": lambda: 1234567890.0, "rand": lambda: 0.0}
        assert generate_trace_id(**kwargs) == generate_trace_id(**kwargs)

    def test_differs_when_rand_differs(self):
        """验随机源不同则 traceId 不同。"""
        a = generate_trace_id(now=lambda: 1.0, rand=lambda: 0.0)
        b = generate_trace_id(now=lambda: 1.0, rand=lambda: 1.0 - 1e-12)
        assert a != b


class TestParseChallenge:
    """挑战参数解析。"""

    def test_start_captcha_is_stringified_json(self):
        """验 startCaptcha 按字符串化 JSON 解析出 gt/challenge。"""
        raw = {
            "captchaType": 1,
            "captchaName": "极验验证",
            "wyCaptchaId": None,
            "wyCaptchaType": None,
            "startCaptcha": json.dumps(
                {"success": 1, "challenge": "ch-abc", "gt": "gt-xyz"}, ensure_ascii=False
            ),
            "randKey": None,
        }
        challenge = parse_challenge(raw, scene="passport-verify")
        assert challenge.gt == "gt-xyz"
        assert challenge.challenge == "ch-abc"
        assert challenge.captcha_type == C.CAPTCHA_TYPE_JIYAN
        assert challenge.is_geetest is True
        assert challenge.scene == "passport-verify"
        assert challenge.rand_key == ""

    def test_falls_back_to_top_level_fields(self):
        """验 gt/challenge 摊平放时也能解析。"""
        challenge = parse_challenge({"gt": "gt-1", "challenge": "ch-1", "randKey": "rk"})
        assert challenge.gt == "gt-1"
        assert challenge.challenge == "ch-1"
        assert challenge.rand_key == "rk"

    def test_malformed_start_captcha_raises(self):
        """验 startCaptcha 非法 JSON 时抛 `ValidationError`。"""
        with pytest.raises(ValidationError) as exc:
            parse_challenge({"startCaptcha": "{not json"})
        assert "startCaptcha" in exc.value.message

    def test_require_geetest_rejects_other_channels(self):
        """验非极验通道调 require_geetest 被拒。"""
        challenge = parse_challenge(
            {"captchaType": 2, "captchaName": "阿里云验证码", "gt": "g", "challenge": "c"}
        )
        with pytest.raises(ValidationError) as exc:
            challenge.require_geetest()
        assert "阿里云验证码" in exc.value.message

    def test_require_geetest_rejects_incomplete_params(self):
        """验极验参数不全时 require_geetest 被拒。"""
        challenge = parse_challenge({"captchaType": 1, "gt": "g"})
        with pytest.raises(ValidationError):
            challenge.require_geetest()


class TestParseSolution:
    """票据参数解析。"""

    def test_accepts_geetest_key_names(self):
        """验极验长键名可解析出票据三元组。"""
        solution = parse_solution(
            {
                "geetest_challenge": "c",
                "geetest_validate": "v",
                "geetest_seccode": "s",
                "randKey": "rk",
            }
        )
        assert (solution.challenge, solution.validate, solution.seccode) == ("c", "v", "s")
        assert solution.rand_key == "rk"
        assert solution.captcha_type == C.CAPTCHA_TYPE_JIYAN

    def test_accepts_short_key_names(self):
        """验帮助页短键名也能解析。"""
        solution = parse_solution({"challenge": "c", "validate": "v", "seccode": "s"})
        assert solution.challenge == "c"

    def test_missing_field_raises(self):
        """验缺字段时抛 `ValidationError`。"""
        with pytest.raises(ValidationError):
            parse_solution({"challenge": "c", "validate": "v"})


class TestHeaders:
    """请求头与表单票据组装。"""

    def test_validate_headers_match_captcha_sdk(self):
        """验 validate 请求头键名与 captcha-sdk 一致。"""
        solution = SliderSolution(challenge="c", validate="v", seccode="s")
        headers = validate_request_headers(solution, trace_id="F-x", zp_token="bst-value")
        assert headers[C.CAPTCHA_HEADER_TYPE] == "1"
        assert headers[C.CAPTCHA_HEADER_CHALLENGE] == "c"
        assert headers[C.CAPTCHA_HEADER_VALIDATE] == "v"
        assert headers[C.CAPTCHA_HEADER_SECCODE] == "s"
        assert headers["traceId"] == "F-x"
        assert headers["zp_token"] == "bst-value"
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert headers["X-Requested-With"] == "XMLHttpRequest"

    def test_optional_headers_omitted_when_blank(self):
        """验空白 traceId/zp_token 不写入请求头。"""
        solution = SliderSolution(challenge="c", validate="v", seccode="s")
        headers = solution.as_headers()
        assert "traceId" not in headers
        assert "zp_token" not in headers

    def test_form_fields_carry_the_ticket_for_the_business_request(self):
        """验业务请求表单只带极验三键票据。"""
        solution = SliderSolution(
            challenge="c", validate="v", seccode="s", rand_key="rk"
        )
        form = solution.as_form_fields()
        assert form == {"challenge": "c", "validate": "v", "seccode": "s"}
        assert "randKey" not in form
        assert "verifyToken" not in form
        assert "captchaToken" not in form

    def test_form_fields_per_captcha_channel(self):
        """验不同验证码通道用各自的表单键名。"""
        picture = SliderSolution(
            challenge="c", validate="img", seccode="s",
            rand_key="rk", captcha_type=C.CAPTCHA_TYPE_PICTURE,
        )
        assert picture.as_form_fields() == {"captcha": "img", "randKey": "rk"}

        yidun = SliderSolution(
            challenge="c", validate="yd", seccode="s",
            captcha_type=C.CAPTCHA_TYPE_YIDUN,
        )
        assert yidun.as_form_fields() == {"validate": "yd"}

    def test_randkey_goes_into_headers_when_present(self):
        """验带 randKey 时写入请求头。"""
        solution = SliderSolution(
            challenge="c", validate="v", seccode="s", rand_key="rk"
        )
        assert solution.as_headers()[C.CAPTCHA_HEADER_RANDKEY] == "rk"


class TestHelperHtml:
    """帮助页 HTML 渲染。"""

    def test_embeds_challenge_and_official_loader(self):
        """验帮助页嵌入挑战参数与官方 loader。"""
        challenge = parse_challenge(
            {
                "captchaType": 1,
                "startCaptcha": json.dumps({"gt": "gt-1", "challenge": "ch-1"}),
            }
        )
        html = build_helper_html(challenge, host="127.0.0.1", port=8766)
        assert C.GEETEST_LOADER_URL in html
        assert '"gt-1"' in html
        assert '"ch-1"' in html
        assert "initGeetest" in html
        assert "/solution" in html

    def test_post_url_is_customizable(self):
        """验回传地址可自定义，且默认 `/solution` 不受影响。"""
        challenge = parse_challenge(
            {"captchaType": 1, "startCaptcha": json.dumps({"gt": "gt-1", "challenge": "ch-1"})}
        )
        html = build_helper_html(
            challenge, post_url="/api/auth/slider/abc/solution"
        )
        assert '"/api/auth/slider/abc/solution"' in html
        default_html = build_helper_html(challenge)
        assert '"/solution"' in default_html

    def test_keeps_javascript_valid_when_values_contain_quotes(self):
        """验 gt/challenge 带引号时嵌入的 JS 仍然合法。"""
        challenge = parse_challenge({"gt": 'gt"quote', "challenge": "c"})
        html = build_helper_html(challenge)
        assert '"gt\\"quote"' in html
