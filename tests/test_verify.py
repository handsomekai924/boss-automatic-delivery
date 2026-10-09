"""滑块验证协议层的纯函数测试。

这一层不碰网络：traceId 生成、挑战/票据解析、帮助页渲染、请求头组装，
全都可以离线对拍。traceId 的校验符是按 verify.html 的**浏览器分支**逐位复刻的，
所以这里把 Node 跑出来的基准值钉死——谁改坏了浮点乘法语义，立刻就红。
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


# --------------------------------------------------------------------------- #
# traceId
# --------------------------------------------------------------------------- #


class TestTraceId:
    #: 与 .scratch/checksum.js（原样搬自 verify.html）逐位对拍过的结果
    KNOWN_CHECKSUMS = {
        "abc123xyz": "BYG",
        "": "000",
        "0123456789abcdef012345": "pER",
        "F-0000000000000abcdef": "0ds",
        # 这条的中间乘积会超过 2**53，专门用来咬住 float64 舍入语义
        "0123456789abcdef0123456789012345678901234567890": "OYC",
    }

    @pytest.mark.parametrize("seed,expected", sorted(KNOWN_CHECKSUMS.items()))
    def test_checksum_matches_node_reference(self, seed, expected):
        assert _compute_checksum(seed) == expected

    def test_shape(self):
        value = generate_trace_id(now=lambda: 1700000000.123, rand=lambda: 0.5)
        assert value.startswith("F-")
        assert len(value) == 2 + 13 + 6 + 3
        body = value[2:]
        assert body[:13] == "0018bcfe5687b"
        assert body[13:19] == "vvvvvv"  # rand=0.5 → CHARS[31]
        assert body[19:] == "4Mp"

    def test_is_deterministic_under_injected_clock(self):
        kwargs = {"now": lambda: 1234567890.0, "rand": lambda: 0.0}
        assert generate_trace_id(**kwargs) == generate_trace_id(**kwargs)

    def test_differs_when_rand_differs(self):
        a = generate_trace_id(now=lambda: 1.0, rand=lambda: 0.0)
        b = generate_trace_id(now=lambda: 1.0, rand=lambda: 1.0 - 1e-12)
        assert a != b


# --------------------------------------------------------------------------- #
# 挑战解析
# --------------------------------------------------------------------------- #


class TestParseChallenge:
    def test_start_captcha_is_stringified_json(self):
        """实测样本里 startCaptcha 是**字符串化的 JSON**，不是嵌套对象。

        按对象去取 gt/challenge 会全部拿到空串，整条链路就断了。
        """
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
        """zppassport 族的 getTypeV2 也可能把 gt/challenge 摊平放。"""
        challenge = parse_challenge({"gt": "gt-1", "challenge": "ch-1", "randKey": "rk"})
        assert challenge.gt == "gt-1"
        assert challenge.challenge == "ch-1"
        assert challenge.rand_key == "rk"

    def test_malformed_start_captcha_raises(self):
        with pytest.raises(ValidationError) as exc:
            parse_challenge({"startCaptcha": "{not json"})
        assert "startCaptcha" in exc.value.message

    def test_require_geetest_rejects_other_channels(self):
        challenge = parse_challenge(
            {"captchaType": 2, "captchaName": "阿里云验证码", "gt": "g", "challenge": "c"}
        )
        with pytest.raises(ValidationError) as exc:
            challenge.require_geetest()
        assert "阿里云验证码" in exc.value.message

    def test_require_geetest_rejects_incomplete_params(self):
        challenge = parse_challenge({"captchaType": 1, "gt": "g"})
        with pytest.raises(ValidationError):
            challenge.require_geetest()


# --------------------------------------------------------------------------- #
# 票据解析
# --------------------------------------------------------------------------- #


class TestParseSolution:
    def test_accepts_geetest_key_names(self):
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
        """帮助页回传用的是短键名，两套都得认。"""
        solution = parse_solution({"challenge": "c", "validate": "v", "seccode": "s"})
        assert solution.challenge == "c"

    def test_missing_field_raises(self):
        with pytest.raises(ValidationError):
            parse_solution({"challenge": "c", "validate": "v"})


# --------------------------------------------------------------------------- #
# 请求头
# --------------------------------------------------------------------------- #


class TestHeaders:
    def test_validate_headers_match_captcha_sdk(self):
        """键名取自 captcha-sdk onSuccess 的 headers 对象，改名就交不上去。"""
        solution = SliderSolution(challenge="c", validate="v", seccode="s")
        headers = validate_request_headers(solution, trace_id="F-x", zp_token="bst-value")
        assert headers[C.CAPTCHA_HEADER_TYPE] == "1"
        assert headers[C.CAPTCHA_HEADER_CHALLENGE] == "c"
        assert headers[C.CAPTCHA_HEADER_VALIDATE] == "v"
        assert headers[C.CAPTCHA_HEADER_SECCODE] == "s"
        assert headers["traceId"] == "F-x"
        assert headers["zp_token"] == "bst-value"
        # verify.html 是 c.send("")，请求体为空，票据全在头上
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert headers["X-Requested-With"] == "XMLHttpRequest"

    def test_optional_headers_omitted_when_blank(self):
        solution = SliderSolution(challenge="c", validate="v", seccode="s")
        headers = solution.as_headers()
        assert "traceId" not in headers
        assert "zp_token" not in headers

    def test_form_fields_carry_the_ticket_for_the_business_request(self):
        """业务请求的票据就是 user-login.js 里 ``1==verifyType`` 那三个键。

        早期版本铺了 verifyToken/captchaToken 一整排「保险」键名，真机不认——
        带着它们去重试照样 400061。契约以登录页 chunk 为准，不猜。
        """
        solution = SliderSolution(
            challenge="c", validate="v", seccode="s", rand_key="rk"
        )
        form = solution.as_form_fields()
        assert form == {"challenge": "c", "validate": "v", "seccode": "s"}
        # 极验通道不带 randKey（那是图片通道的），也没有 verifyToken/captchaToken
        assert "randKey" not in form
        assert "verifyToken" not in form
        assert "captchaToken" not in form

    def test_form_fields_per_captcha_channel(self):
        """换通道就得换键名——照抄 user-login.js 的 verifyType 分支。"""
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
        solution = SliderSolution(
            challenge="c", validate="v", seccode="s", rand_key="rk"
        )
        assert solution.as_headers()[C.CAPTCHA_HEADER_RANDKEY] == "rk"


# --------------------------------------------------------------------------- #
# 帮助页
# --------------------------------------------------------------------------- #


class TestHelperHtml:
    def test_embeds_challenge_and_official_loader(self):
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
        # 票据回传目标
        assert "/solution" in html

    def test_post_url_is_customizable(self):
        """网页控制台把帮助页挂在自己的路由下，回传地址要能改。"""
        challenge = parse_challenge(
            {"captchaType": 1, "startCaptcha": json.dumps({"gt": "gt-1", "challenge": "ch-1"})}
        )
        html = build_helper_html(
            challenge, post_url="/api/auth/slider/abc/solution"
        )
        assert '"/api/auth/slider/abc/solution"' in html
        # 默认值不受影响
        default_html = build_helper_html(challenge)
        assert '"/solution"' in default_html

    def test_keeps_javascript_valid_when_values_contain_quotes(self):
        """gt/challenge 用 json.dumps 嵌进 JS，带引号时不能把脚本弄断。"""
        challenge = parse_challenge({"gt": 'gt"quote', "challenge": "c"})
        html = build_helper_html(challenge)
        # 这两个片段若被错误拼接，就会出现裸的 gt"quote 在 JS 里
        assert '"gt\\"quote"' in html
