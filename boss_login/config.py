"""接口地址、请求头、业务码等常量配置。

⚠️ 关于接口地址的可信度
--------------------------------------------------------------------------------
路径的来源分两档：

**已从按需 chunk 里读到**（``static.zhipin.com/zhipin-sign/v5330/static/js/
user-login.5398946e.js``，短信登录真正调的那一份）——发码/登录的路由、表单形状、
票据挂载方式都是从这里抄的，见下面 ``send/smsCodeV2`` 那一段。

**已实测确认**（2026-09-29 直接打真实站点）：
  - ``send/smsCode`` = ``/wapi/zppassport/send/smsCode``  → 回业务码 400061，路由正确
  - ``zppassport/captcha/getTypeV2`` → 业务链的滑块挑战（gt=64568016…，带真 randKey）
  - ``zppassport/captcha/validate``  → **不存在，404**
  - ``zpsecureflow/captcha/gettype`` / ``validate`` → verify.html 页那族，另一个 gt

**仍未核实**：``login/phone(V2)`` / ``user_info`` / ``logout`` 的线上行为。

未核实的路径提供了两条兜底：
  1. 运行 `python -m boss_login probe` 探测候选地址，确认后再写回 ENDPOINTS；
  2. 用命令行参数临时覆盖：--endpoint-send / --endpoint-login。
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------- #
# 站点
# --------------------------------------------------------------------------- #

BASE_URL: Final[str] = "https://www.zhipin.com"

#: 短信登录相关接口。若线上已变更，用 probe 或命令行参数覆盖。
#:
#: V1 是「不带极验票据」那代；V2 是登录页在极验通道（verifyType=1）下真正调的
#: 那个（见按需 chunk user-login.js 的 ``B = {1:{smsCode:"…/smsCodeV2", …}}``）。
#: 滑块票据只有 V2 认——带着票据去重试 V1，服务端照样 400061。
ENDPOINTS: Final[dict[str, str]] = {
    "send_sms_code": "/wapi/zppassport/send/smsCode",
    "send_sms_code_v2": "/wapi/zppassport/send/smsCodeV2",
    "login_by_sms": "/wapi/zppassport/login/phone",
    "login_by_sms_v2": "/wapi/zppassport/login/phoneV2",
    #: 登录前置检查。登录页 ``loginOrRegister`` 的中间件链末位就是它
    #: （``Re({token: smsToken, encryptedAccount, code, regionCode})``），
    #: 失败会被吞掉，但**每次登录都会打**。
    "login_suggest": "/wapi/zppassport/validate/getLoginSuggest",
    "user_info": "/wapi/zpuser/wap/getUserInfo.json",
    "logout": "/wapi/zppassport/user/logout",
}

#: probe 时要依次尝试的候选地址（第一个命中即视为可用）
CANDIDATE_ENDPOINTS: Final[dict[str, list[str]]] = {
    "send_sms_code": [
        "/wapi/zppassport/send/smsCode",
        "/wapi/zppassport/send/smsCodeV2",
        "/wapi/zppassport/sms/sendSmsCode",
        "/wapi/zppassport/send/smsCodeByWy",
        "/wapi/zppassport/code/send",
    ],
    "login_by_sms": [
        "/wapi/zppassport/login/phone",
        "/wapi/zppassport/login/phoneV2",
        "/wapi/zppassport/user/smsLogin",
        "/wapi/zppassport/login/sms",
        "/wapi/zppassport/user/login",
    ],
}

# --------------------------------------------------------------------------- #
# 短信场景
# --------------------------------------------------------------------------- #

#: smsType 取值（V1 路由用）。1=短信登录，2=注册，7=绑定/其他
SMS_SCENES: Final[dict[str, int]] = {
    "login": 1,
    "register": 2,
    "bind": 7,
}

# --------------------------------------------------------------------------- #
# send/smsCodeV2 的表单形状（来自 user-login.js 的 Ce(!0) + je()）
# ---------------------------------------------------------------------------
# 登录页发短信的中间件链是 [_e, Oe, ke, Ee, Ce(!0), je()]，最终表单是::
#
#     regionCode=+86
#     pk=cpc_user_sign_up          # 构造参数，缺省 "cpc_user_sign_up"
#     smsType=7                    # 写死的，不是用户选的场景码
#     purpose=<identity>           # 0=求职者 / 1=招聘者，与 formData.identity 同值
#     version=1                    # Ce(true) 才加，登录那条路没有
#     encryptedAccount=<AES(phone)>   # je() 把 phone 换掉，表单里不再有 phone
#     challenge=… validate=… seccode=…   # 仅极验通道；见 SliderSolution.as_form_fields
#
# 注意：登录页构造 SmsAccount 时 **没有** 传 requestHeaders，所以业务请求上
# 不带 Zp-Captcha-* 头——那套头是 zpsecureflow validate 的形状，别混过来。

#: 极验通道下 smsType 是写死的 7，与 SMS_SCENES 的取值无关
SMS_TYPE_V2: Final[int] = 7
#: Ce(!0) 追加的协议版本号
SMS_VERSION_V2: Final[int] = 1
#: pk 缺省值：登录页 ``this.pk || "cpc_user_sign_up"``
DEFAULT_PK: Final[str] = "cpc_user_sign_up"
#: formData.identity 的取值，发码时被放进 purpose
IDENTITY_JOB_SEEKER: Final[int] = 0
IDENTITY_RECRUITER: Final[int] = 1

# --------------------------------------------------------------------------- #
# 请求头
# --------------------------------------------------------------------------- #

DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/web/user/",
    "X-Requested-With": "XMLHttpRequest",
}

# --------------------------------------------------------------------------- #
# 业务码
# --------------------------------------------------------------------------- #

#: 通用成功码
CODE_OK: Final[int] = 0

#: 业务失败码（按接口语义归类，便于给出精准提示）
CODE_MESSAGES: Final[dict[int, str]] = {
    1001: "发送太频繁了，请稍后再试",
    1002: "请先完成安全验证",
    1003: "该手机号暂时无法接收短信，请联系客服",
    1004: "验证码发送失败，请稍后重试",
    2001: "验证码错误，请重新输入",
    2002: "验证码已过期，请重新获取",
    2003: "错误次数过多，请重新获取验证码",
    2004: "该账号存在异常，请联系客服 400-065-5799",
    9000: "系统繁忙，请稍后重试",
}

#: 风控码 —— 从登录页 passport SDK（vendors.c30ef7fa.js）中提取，可信度高
#:
#:   t.ZpPassportCode = { IP_BLOCK:31, UID_BLOCK:32, IP_GRAY:35, UID_GRAY:36,
#:                        SECURITY_CHECK:37, ANTI_SPIDER_LOGIN:38 }
#:   t.CmPassportCode = { IP_BLOCK:-1000031, UID_BLOCK:-1000032, IP_GRAY:-1000035,
#:                        UID_GRAY:-1000036, SECURITY_CHECK:-1000037 }
RISK_CODE_IP_BLOCK: Final[frozenset[int]] = frozenset({31, -1000031})
RISK_CODE_UID_BLOCK: Final[frozenset[int]] = frozenset({32, -1000032})
RISK_CODE_IP_GRAY: Final[frozenset[int]] = frozenset({35, -1000035})
RISK_CODE_UID_GRAY: Final[frozenset[int]] = frozenset({36, -1000036})
RISK_CODE_SECURITY_CHECK: Final[frozenset[int]] = frozenset({37, -1000037})
RISK_CODE_ANTI_SPIDER: Final[frozenset[int]] = frozenset({38})

#: 滑块验证 —— **实测确认**：真实站点 /wapi/zppassport/send/smsCode 返回
#:   {"code": 400061, "message": "请完成滑块验证"}
#: 注意它不在上面的 SDK 常量表里：SDK 那套是 passport 内部码，400xxx 是 wapi 网关码，
#: 两个码空间并存。
RISK_CODE_SLIDER: Final[frozenset[int]] = frozenset({400061})

#: 兜底用的提示语关键字。线上验证码族还有若干近亲（400060/400062…），离线拿不到完整
#: 码表，但这类响应的话术里必定带下列词之一，按文案判定比猜码更可靠。
#: 特意收窄，避免误伤「验证码错误」这类普通业务失败。
RISK_MESSAGE_KEYWORDS: Final[tuple[str, ...]] = (
    "滑块",
    "安全验证",
    "人机验证",
    "图形验证",
)

#: 其中真正指「滑块」的话术 —— 用来把滑块与 SECURITY_CHECK(37) 那种
#: 「跳去 security.html 的安全验证」分开。后者带 {seed,ts,name}，
#: 协议完全不同，不能套用滑块链路。
SLIDER_MESSAGE_KEYWORDS: Final[tuple[str, ...]] = (
    "滑块",
    "人机验证",
    "图形验证",
)

ALL_RISK_CODES: Final[frozenset[int]] = (
    RISK_CODE_IP_BLOCK
    | RISK_CODE_UID_BLOCK
    | RISK_CODE_IP_GRAY
    | RISK_CODE_UID_GRAY
    | RISK_CODE_SECURITY_CHECK
    | RISK_CODE_ANTI_SPIDER
    | RISK_CODE_SLIDER
)

#: 登录成功后 token 可能出现的字段。
#:
#: ⚠️ 真实站点的 ``login/phoneV2`` 响应体里**没有**会话 token——登录页的成功
#: 处理（``m.F``）只读路由字段（``identity`` / ``isCompletion`` / ``toUrl`` …），
#: 鉴权完全靠 ``Set-Cookie``。这张表是给「响应体里真给了 token」的兼容层用的
#: （假服务端就是这么回的），别指望线上能从 body 里抠出 token。
TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "token",
    "zpToken",
    "zp_token",
    "accessToken",
    "access_token",
)

#: 发码响应 ``zpData.token`` 的字段名。这是 **smsToken**（短信会话票据），
#: 不是登录态——登录页把它存进 ``smsToken``，再交给 ``getLoginSuggest``。
SMS_TOKEN_FIELD: Final[str] = "token"

#: 登录成功后通常会下发的 Cookie。
#: 真实站点鉴权就在这里；名字认不出来时客户端会把实到的 Cookie 名打出来，
#: 免得永远卡在「既没有 token 也没有鉴权 Cookie」。
AUTH_COOKIES: Final[tuple[str, ...]] = ("zp_at", "wt2", "bst", "at", "zp_token")

# --------------------------------------------------------------------------- #
# 滑块验证（极验 Geetest）
# ---------------------------------------------------------------------------
# 接口契约来自两处实测（2026-09-29）：
#   1. 直接打真实站点，拿到的 JSON 形状；
#   2. 登录页 verify.html 的脚本 + 它按需加载的
#      captcha-sdk@5.1.3.min.js（static.zhipin.com）里写死的路径与请求头。
# 两条线互相印证，可信度高。

#: 验证接口。**两族不是同一条链，不能混用**（2026-09-29 实测）：
#:
#:   zppassport 族  —— 业务接口（send/smsCodeV2 等）同族，走这套
#:       GET  /wapi/zppassport/captcha/getTypeV2   下发挑战（gt=64568016…，带真 randKey）
#:       /wapi/zppassport/captcha/validate          **不存在，实测 404**
#:   zpsecureflow 族 —— verify.html 独立验证页用的那套，下发的是**另一个 gt**（413f33d3…）
#:       GET  /wapi/zpsecureflow/captcha/gettype?scene=passport-verify
#:       POST /wapi/zpsecureflow/captcha/validate   （请求体为空，票据在 Zp-Captcha-* 头）
#:
#: 业务请求上的票据怎么挂 —— user-login.js 的 ``Ce`` 写得很直白，**只有表单字段**::
#:
#:     1==verifyType ? {challenge, validate, seccode}     # 极验
#:     3==verifyType ? {captcha, randKey}                 # 图片
#:     4==verifyType ? {validate}                         # 易盾
#:
#: 那份 chunk 里 ``Zp-Captcha`` 出现 **0 次**——登录页压根不带这套头。它们是
#: captcha-sdk onSuccess 给 zpsecureflow validate 用的，别混到业务请求上。
#: 极验票据是一次性凭证：先交去 zpsecureflow/validate 会把它烧掉，业务请求再带
#: 同一张就只剩 400061（真机踩过）。
VERIFY_ENDPOINTS: Final[dict[str, str]] = {
    # passport 族：业务请求的滑块走这套（默认）
    "get_type_v2": "/wapi/zppassport/captcha/getTypeV2",
    "randkey": "/wapi/zppassport/captcha/randkey",
    # zpsecureflow 族：verify.html 页面专用，别拿它的票据去打业务接口
    "get_type": "/wapi/zpsecureflow/captcha/gettype",
    "validate": "/wapi/zpsecureflow/captcha/validate",
    "get_redirect": "/wapi/zpsecureflow/captcha/getredirect",
}

#: 业务请求的滑块挑战默认取自哪条 key —— 必须与业务接口同族
SLIDER_CHALLENGE_ENDPOINT_KEY: Final[str] = "get_type_v2"

#: 验证通道。来自 captcha-sdk@5.1.3 的 CaptchaType 枚举：
#:   Jiyan=1 / Ali=2 / Picture=3 / Yidun=4
#: 实测 gettype / getTypeV2 返回的都是 1（极验滑块）。
CAPTCHA_TYPE_JIYAN: Final[int] = 1
CAPTCHA_TYPE_ALI: Final[int] = 2
CAPTCHA_TYPE_PICTURE: Final[int] = 3
CAPTCHA_TYPE_YIDUN: Final[int] = 4

CAPTCHA_TYPE_NAMES: Final[dict[int, str]] = {
    CAPTCHA_TYPE_JIYAN: "极验验证",
    CAPTCHA_TYPE_ALI: "阿里云验证码",
    CAPTCHA_TYPE_PICTURE: "图片验证码",
    CAPTCHA_TYPE_YIDUN: "网易易盾",
}

#: 极验加载器。BOSS 把官方 gt.0.5.0 镜像在自己 CDN 上，内容与
#: Geetest Inc. 的发行版一致（首行 "v0.5.0 Geetest Inc."），用镜像即可。
GEETEST_LOADER_URL: Final[str] = (
    "https://static.zhipin.com/assets/zhipin/geek/verify-sdk/jiyan/gt.0.5.0.js"
)

#: initGeetest 的 api_server_v3，与 captcha-sdk 里传的值保持一致
GEETEST_API_SERVERS: Final[tuple[str, ...]] = ("api.geetest.com", "api.geevisit.com")

#: 求解成功后要随 validate 一起带上的请求头。
#: 键名来自 captcha-sdk onSuccess 的 headers 对象，不是猜的。
CAPTCHA_HEADER_TYPE: Final[str] = "Zp-Captcha-Type"
CAPTCHA_HEADER_CHALLENGE: Final[str] = "Zp-Captcha-Challenge"
CAPTCHA_HEADER_VALIDATE: Final[str] = "Zp-Captcha-Validate"
CAPTCHA_HEADER_SECCODE: Final[str] = "Zp-Captcha-Seccode"
#: Picture 通道会带；Jiyan 的 onSuccess 没塞，带上无害
CAPTCHA_HEADER_RANDKEY: Final[str] = "Zp-Captcha-Randkey"

#: getredirect 返回 true 表示这次不用验证，可直接跳走
REDIRECT_SKIP_VERIFY: Final[str] = "redirect"

#: 本地求解帮助页
SLIDER_HELPER_HOST: Final[str] = "127.0.0.1"
SLIDER_HELPER_PORT: Final[int] = 8766
#: 等人工拖完滑块的上限。超时直接失败，不让脚本静默长眠。
SLIDER_HELPER_TIMEOUT: Final[int] = 300

# --------------------------------------------------------------------------- #
# 本地上限（与服务端一致，避免无谓请求）
# --------------------------------------------------------------------------- #

#: 同一号码重发间隔（秒）
RESEND_COOLDOWN_SECONDS: Final[int] = 60

#: 验证码位数
CODE_LENGTH: Final[int] = 6

#: 同一号码连续输错几次后放弃
MAX_CODE_ATTEMPTS: Final[int] = 5

#: 重发验证码时，最多愿意等待服务端冷却多久（秒）。超过这个值直接失败，
#: 以免脚本静默地睡很久——这种情况通常意味着配额已耗尽或触发风控。
MAX_RESEND_WAIT_SECONDS: Final[int] = 120

#: 本地校验用的手机号规则
CN_PHONE_PREFIX_LENGTH: Final[int] = 11
