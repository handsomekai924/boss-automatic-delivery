"""接口地址、请求头、业务码等常量配置。

路由分两档可信度：发码/登录的路径、表单形状、票据挂载方式来自短信登录真正调的
按需 chunk ``user-login.5398946e.js``；滑块两族接口的形状来自 verify.html 加载的
``captcha-sdk@5.1.3.min.js``，并直接打真实站点对过。

  - ``zppassport/captcha/getTypeV2`` 给业务链滑块挑战（gt=64568016…）
  - ``zppassport/captcha/validate`` **不存在**（404）
  - ``zpsecureflow/captcha/{gettype,validate}``` 是 verify.html 那族，另一个 gt

``login/phone(V2)`` / ``user_info`` / ``logout`` 的线上行为仍未核实；探一探
``python -m boss_login probe``，或用 ``--endpoint-send`` / ``--endpoint-login``
临时覆盖，确认后再写回 :data:`ENDPOINTS`。
"""

from __future__ import annotations

from typing import Final


BASE_URL: Final[str] = "https://www.zhipin.com"

#: 短信登录接口。V1 不带极验票据；V2 是登录页在极验通道（verifyType=1）下调的
#: 那个，**只有 V2 认票据**——带着票据重试 V1 照样 400061。线上变了就用 probe
#: 或 ``--endpoint-send`` / ``--endpoint-login`` 覆盖。
ENDPOINTS: Final[dict[str, str]] = {
    "send_sms_code": "/wapi/zppassport/send/smsCode",
    "send_sms_code_v2": "/wapi/zppassport/send/smsCodeV2",
    "login_by_sms": "/wapi/zppassport/login/phone",
    "login_by_sms_v2": "/wapi/zppassport/login/phoneV2",
    #: 登录前置检查，登录页 ``loginOrRegister`` 中间件链末位。失败会被吞掉，
    #: 但**每次登录都会打**。
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


#: smsType 取值（V1 路由用）。1=短信登录，2=注册，7=绑定/其他
SMS_SCENES: Final[dict[str, int]] = {
    "login": 1,
    "register": 2,
    "bind": 7,
}

#: 发码表单的形状（登录页中间件链 ``Ce(!0)`` + ``je()``）：``pk`` 缺省见
#: :data:`DEFAULT_PK`；``smsType=7`` 写死、不是用户选的场景码；``purpose`` 来自
#: formData.identity（0=求职者 / 1=招聘者）；``version=1`` 只有 ``Ce(true)`` 才加，
#: 登录那条路没有；``encryptedAccount=<AES(phone)>``（``je()`` 换掉 phone，表单里
#: 不再有它）；``challenge/validate/seccode`` 仅极验通道。登录页构造 ``SmsAccount``
#: 时**不带** ``requestHeaders``，所以业务请求上没有 ``Zp-Captcha-*``——那是
#: zpsecureflow validate 的形状，别混过来。
SMS_TYPE_V2: Final[int] = 7
#: Ce(!0) 追加的协议版本号
SMS_VERSION_V2: Final[int] = 1
#: pk 缺省值：登录页 ``this.pk || "cpc_user_sign_up"``
DEFAULT_PK: Final[str] = "cpc_user_sign_up"
#: formData.identity 的取值，发码时被放进 purpose
IDENTITY_JOB_SEEKER: Final[int] = 0
IDENTITY_RECRUITER: Final[int] = 1


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

#: 风控码（passport SDK 内部码空间，正负两套并存，见各 frozenset）。
RISK_CODE_IP_BLOCK: Final[frozenset[int]] = frozenset({31, -1000031})
RISK_CODE_UID_BLOCK: Final[frozenset[int]] = frozenset({32, -1000032})
RISK_CODE_IP_GRAY: Final[frozenset[int]] = frozenset({35, -1000035})
RISK_CODE_UID_GRAY: Final[frozenset[int]] = frozenset({36, -1000036})
RISK_CODE_SECURITY_CHECK: Final[frozenset[int]] = frozenset({37, -1000037})
RISK_CODE_ANTI_SPIDER: Final[frozenset[int]] = frozenset({38})

#: 滑块验证码 400061（``send/smsCode`` 实测回 ``{"code":400061,"message":"请完成滑块验证"}``）。
#: **不在 SDK 那张常量表里**：SDK 是 passport 内部码，400xxx 是 wapi 网关码，两个码空间并存。
RISK_CODE_SLIDER: Final[frozenset[int]] = frozenset({400061})

#: 兜底判据用的话术关键字。线上验证码族还有近亲（400060/400062…），码表拿不全，
#: 但这类响应的话术必带下列词之一；特意收窄，免得误伤「验证码错误」这类普通失败。
RISK_MESSAGE_KEYWORDS: Final[tuple[str, ...]] = (
    "滑块",
    "安全验证",
    "人机验证",
    "图形验证",
)

#: 上面里真正指「滑块」的那几个——用来跟 ``SECURITY_CHECK``(37) 的「跳去
#: security.html」分开：后者带 ``{seed,ts,name}``，协议不同，不能套滑块链路。
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

#: 响应体里可能出现的会话 token 字段名。**真实站点 ``login/phoneV2`` 的响应体里
#: 没有 token**——鉴权完全靠 ``Set-Cookie``；这张表只给「响应体真给了 token」的
#: 兼容层用（假服务端就这么回），别指望线上能从 body 里抠出 token。
TOKEN_FIELDS: Final[tuple[str, ...]] = (
    "token",
    "zpToken",
    "zp_token",
    "accessToken",
    "access_token",
)

#: 发码响应 ``zpData.token`` 的字段名。这是 **smsToken**（短信会话票据，交给
#: ``getLoginSuggest``），**不是登录态**。
SMS_TOKEN_FIELD: Final[str] = "token"

#: 登录成功后通常下发的鉴权 Cookie。名字认不出来时客户端会把实到的 Cookie 名
#: 打出来，免得卡死在「既没 token 也没鉴权 Cookie」。
AUTH_COOKIES: Final[tuple[str, ...]] = ("zp_at", "wt2", "bst", "at", "zp_token")

#: 滑块验证两族接口，**不能混用**：zppassport 是业务链（票据只走表单字段
#: challenge/validate/seccode，不带 ``Zp-Captcha-*`` 头），zpsecureflow 是
#: verify.html 独立页（票据在 ``Zp-Captcha-*`` 头）。极验票据一次性：先交
#: zpsecureflow/validate 会烧掉，业务请求再带同一张只剩 400061。
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

#: 验证通道（captcha-sdk 的 ``CaptchaType``：Jiyan=1 / Ali=2 / Picture=3 / Yidun=4）。
#: gettype / getTypeV2 实测都回 1（极验滑块）。
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

#: 极验加载器。BOSS 把官方 ``gt.0.5.0`` 镜像在自己 CDN 上，内容与 Geetest Inc.
#: 发行版一致，用镜像即可。
GEETEST_LOADER_URL: Final[str] = (
    "https://static.zhipin.com/assets/zhipin/geek/verify-sdk/jiyan/gt.0.5.0.js"
)

#: initGeetest 的 api_server_v3，与 captcha-sdk 传的值一致
GEETEST_API_SERVERS: Final[tuple[str, ...]] = ("api.geetest.com", "api.geevisit.com")

#: 求解成功后随 validate 一起带的请求头。键名来自 captcha-sdk onSuccess 的
#: headers 对象。
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
