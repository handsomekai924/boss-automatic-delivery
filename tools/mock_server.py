"""本地假服务端：离线完整演练登录流程，路由与行为贴近线上。

    POST /wapi/zppassport/send/smsCode|smsCodeV2    发码（V1 不认票据 / V2 认）
    POST /wapi/zppassport/login/phone|phoneV2       登录（同上）
    POST /wapi/zppassport/validate/getLoginSuggest  登录前置（失败不阻断）
    GET  /wapi/zpuser/wap/getUserInfo.json · POST /wapi/zppassport/user/logout
    GET  /wapi/zppassport/captcha/getTypeV2         业务滑块挑战（带真 randKey）
    POST /wapi/zppassport/captcha/validate          **404** —— passport 族没这路由
    GET  /wapi/zpsecureflow/captcha/gettype         verify.html 挑战（另一个 gt）
    POST /wapi/zpsecureflow/captcha/validate        收下票据并**烧掉**（一次性）

用法::

    python tools/mock_server.py --port 8765
    python -m boss_login login --phone 13800138000 --base-url http://127.0.0.1:8765

验证码打在日志里。风控 13800138010，封禁 13800000000，滑块 13800138020，对照 13800139010。
滑块（400061）放行三件事缺一不可：
    1. 打的是 **V2**（V1 不读票据）
    2. **这个请求自己**带极验三件套 ``challenge`` / ``validate`` / ``seccode``
    3. 票据没被烧掉 —— validate 收下即作废

``Zp-Captcha-*`` 是 zpsecureflow 的形状，业务接口不认。发码与登录共用同一张票
（``verifyInfo`` 跨两步），业务请求不烧票；两族 gt 不同，便于测出取错族。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# `python tools/mock_server.py` 时 sys.path[0] 是 tools/，import 不到 boss_login。
# 测试里是 `from tools.mock_server import …`，那时项目根已经在路上。两边都要能跑。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from boss_login.crypto import decrypt_account  # noqa: E402  - 见上面的 path 处理

CODE_TTL_SECONDS = 300
RESEND_COOLDOWN_SECONDS = 60
MAX_ATTEMPTS = 5

RISK_CONTROL_PHONES = {"13800138010"}
BLOCKED_PHONES = {"13800000000"}

#: 滑块响应形状 ``{"code": 400061, "message": "请完成滑块验证"}``。zpData 故意放
#: **无法识别的字段名**——真实响应字段离线没核实，客户端必须原样回显。
SLIDER_PHONES = {"13800138020"}
SLIDER_CODE = 400061
SLIDER_MESSAGE = "请完成滑块验证"
SLIDER_PAYLOAD = {"challenge": "mock-challenge-id", "gt": "mock-gt"}

#: 话术相近但属于普通业务失败的对照号码：不该被当成风控。
PLAIN_REJECT_PHONE = "13800139010"

#: 通行 Cookie 名。validate 会种它，**但业务接口不认**——见 _send_sms_code。
SLIDER_PASS_COOKIE = "mock_slider_pass"
#: 票据请求头（与 captcha-sdk onSuccess 对齐）。**业务接口不认**，只有
#: zpsecureflow/captcha/validate 那条链用它。
REQUIRED_CAPTCHA_HEADERS = (
    "Zp-Captcha-Type",
    "Zp-Captcha-Challenge",
    "Zp-Captcha-Validate",
    "Zp-Captcha-Seccode",
)
#: 极验通道的票据 —— user-login.js 的 ``1==verifyType`` 分支，就这三个表单键。
JIYAN_TICKET_KEYS = ("challenge", "validate", "seccode")

#: phone -> {"code": str, "sent_at": float, "expire_at": float, "attempts": int}
STORE: dict[str, dict] = {}
#: token -> user
SESSIONS: dict[str, dict] = {}
#: 已被 validate 收下（= 烧掉）的 geetest_validate。一次性凭证用过即废。
BURNED_TICKETS: set[str] = set()
#: 最近一次发码请求的路径/表单/请求头，供测试断言「票据有没有挂对地方」。
LAST_SMS_REQUEST: dict = {}
#: 最近一次登录请求的路径/表单/请求头。
LAST_LOGIN_REQUEST: dict = {}
#: 最近一次 getLoginSuggest 请求的表单。
LAST_SUGGEST_REQUEST: dict = {}
#: 发码登记下来的 smsToken（响应 zpData.token），getLoginSuggest 要拿它对账。
SMS_TOKENS: dict[str, str] = {}

#: 置真时筛选/城市路由一律回失败码，用来验证客户端的写死表兜底。
FILTER_API_DOWN = False

#: 路径与真实站点一致；形状照抄 wapi（conditions → XxxList，city → 省市区树
#: + hotCityList，hot/city → hotCityList）。
FILTER_ROUTES: tuple[str, ...] = (
    "/wapi/zpgeek/pc/all/filter/conditions.json",
    "/wapi/zpgeek/pc/recommend/conditions.json",
    "/wapi/zpgeek/search/job/condition.json",
    "/wapi/zpCommon/data/city.json",
    "/wapi/zpCommon/data/cityGroup.json",
    "/wapi/zpgeek/search/job/hot/city.json",
)

#: 与线上同 code / 同文案，别手写凑数。
MOCK_CONDITIONS: dict = {
    "payTypeList": [
        {"code": 0, "name": "不限"},
        {"code": 2501, "name": "日结"},
        {"code": 2502, "name": "周结"},
        {"code": 2503, "name": "月结"},
        {"code": 2504, "name": "完工结"},
    ],
    "experienceList": [
        {"code": 0, "name": "不限"},
        {"code": 108, "name": "在校生"},
        {"code": 102, "name": "应届生"},
        {"code": 101, "name": "经验不限"},
        {"code": 103, "name": "1年以内"},
        {"code": 104, "name": "1-3年"},
        {"code": 105, "name": "3-5年"},
        {"code": 106, "name": "5-10年"},
        {"code": 107, "name": "10年以上"},
    ],
    "salaryList": [
        {"code": 0, "name": "不限", "lowSalary": 0, "highSalary": 0},
        {"code": 402, "name": "3K以下", "lowSalary": 0, "highSalary": 3},
        {"code": 403, "name": "3-5K", "lowSalary": 3, "highSalary": 5},
        {"code": 404, "name": "5-10K", "lowSalary": 5, "highSalary": 10},
        {"code": 405, "name": "10-20K", "lowSalary": 10, "highSalary": 20},
        {"code": 406, "name": "20-50K", "lowSalary": 20, "highSalary": 50},
        {"code": 407, "name": "50K以上", "lowSalary": 50, "highSalary": 0},
    ],
    "stageList": [
        {"code": 0, "name": "不限"},
        {"code": 801, "name": "未融资"},
        {"code": 802, "name": "天使轮"},
        {"code": 803, "name": "A轮"},
        {"code": 804, "name": "B轮"},
        {"code": 805, "name": "C轮"},
        {"code": 806, "name": "D轮及以上"},
        {"code": 807, "name": "已上市"},
        {"code": 808, "name": "不需要融资"},
    ],
    "scaleList": [
        {"code": 0, "name": "不限"},
        {"code": 301, "name": "0-20人"},
        {"code": 302, "name": "20-99人"},
        {"code": 303, "name": "100-499人"},
        {"code": 304, "name": "500-999人"},
        {"code": 305, "name": "1000-9999人"},
        {"code": 306, "name": "10000人以上"},
    ],
    "partTimeList": [
        {"code": 0, "name": "不限"},
        {"code": 2701, "name": "周末/节假日"},
        {"code": 2702, "name": "寒暑假"},
        {"code": 2703, "name": "短期兼职"},
        {"code": 2704, "name": "长期兼职"},
        {"code": 2705, "name": "工作日"},
        {"code": 2706, "name": "夜班"},
    ],
    "degreeList": [
        {"code": 0, "name": "不限"},
        {"code": 209, "name": "初中及以下"},
        {"code": 208, "name": "中专/中技"},
        {"code": 206, "name": "高中"},
        {"code": 202, "name": "大专"},
        {"code": 203, "name": "本科"},
        {"code": 204, "name": "硕士"},
        {"code": 205, "name": "博士"},
    ],
    "jobTypeList": [
        {"code": 0, "name": "不限"},
        {"code": 1901, "name": "全职"},
        {"code": 1903, "name": "兼职"},
    ],
}

#: 热点城市（照线上 hot/city.json 形状）。
MOCK_HOT_CITIES: list[dict] = [
    {"code": 100010000, "name": "全国"},
    {"code": 101010100, "name": "北京"},
    {"code": 101020100, "name": "上海"},
    {"code": 101280100, "name": "广州"},
    {"code": 101280600, "name": "深圳"},
]

#: 城市树只要一小片够测递归解析即可（省→市→区三层，照线上字段名）。
MOCK_CITY_LIST: list[dict] = [
    {
        "code": 101010000,
        "name": "北京",
        "pinyin": None,
        "firstChar": "b",
        "subLevelModelList": [
            {
                "code": 101010100,
                "name": "北京",
                "pinyin": "beijing",
                "firstChar": "b",
                "subLevelModelList": [
                    {"code": 110101, "name": "东城区", "pinyin": None, "firstChar": "d", "subLevelModelList": None},
                    {"code": 110102, "name": "西城区", "pinyin": None, "firstChar": "x", "subLevelModelList": None},
                ],
            }
        ],
    },
    {
        "code": 101280000,
        "name": "广东",
        "pinyin": None,
        "firstChar": "g",
        "subLevelModelList": [
            {
                "code": 101280100,
                "name": "广州",
                "pinyin": "guangzhou",
                "firstChar": "g",
                "subLevelModelList": [
                    {"code": 440106, "name": "天河区", "pinyin": None, "firstChar": "t", "subLevelModelList": None},
                ],
            }
        ],
    },
]


def _json(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200, cookies=None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json;charset=UTF-8")
    handler.send_header("Content-Length", str(len(body)))
    for cookie in cookies or []:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(body)


def _announce(message: str) -> None:
    """打印提示。

    Windows 控制台默认是 GBK，遇到 emoji 会抛 UnicodeEncodeError。日志失败不能
    影响请求处理——否则客户端会看到一个 500 并重试，而这一次的冷却记录已经写进
    STORE 了，重试就会莫名其妙地撞上限流。所以这里降级成 ASCII，绝不外抛。
    """
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 保持父类签名
        _announce(f"[mock] {self.address_string()} {fmt % args}")


    def do_GET(self):  # noqa: N802 - 父类命名
        path = urlparse(self.path).path
        if path == "/wapi/zpuser/wap/getUserInfo.json":
            token = self._auth_token()
            if not token or token not in SESSIONS:
                return _json(self, {"code": 1, "message": "未登录", "zpData": {}})
            return _json(self, {"code": 0, "message": "success", "zpData": SESSIONS[token]})
        if path == "/wapi/zppassport/captcha/getTypeV2":
            return self._captcha_gettype(family="passport")
        if path == "/wapi/zpsecureflow/captcha/gettype":
            return self._captcha_gettype(family="secureflow")
        if path == "/wapi/zppassport/captcha/validate":
            return _json(self, {"status": 404, "error": "Not Found", "path": path}, status=404)
        if path in FILTER_ROUTES:
            return self._filter_data(path)
        _json(self, {"code": 404, "message": "not found", "zpData": {}}, status=404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        form = self._read_form()

        if path == "/wapi/zppassport/send/smsCode":
            return self._send_sms_code(form, version=1)
        if path == "/wapi/zppassport/send/smsCodeV2":
            return self._send_sms_code(form, version=2)
        if path == "/wapi/zppassport/login/phone":
            return self._login(form, version=1)
        if path == "/wapi/zppassport/login/phoneV2":
            return self._login(form, version=2)
        if path == "/wapi/zppassport/validate/getLoginSuggest":
            return self._login_suggest(form)
        if path == "/wapi/zppassport/user/logout":
            token = self._auth_token()
            SESSIONS.pop(token, None)
            return _json(
                self,
                {"code": 0, "message": "success", "zpData": {}},
                cookies=["zp_at=; Path=/; Max-Age=0"],
            )
        if path == "/wapi/zpsecureflow/captcha/validate":
            return self._captcha_validate()
        if path == "/wapi/zppassport/captcha/validate":
            return _json(self, {"status": 404, "error": "Not Found", "path": path}, status=404)
        _json(self, {"code": 404, "message": "not found", "zpData": {}}, status=404)


    def _captcha_gettype(self, *, family: str) -> None:
        """照实测形状回一个极验挑战（startCaptcha 是字符串化的 JSON）。

        两族下发**不同的 gt**：业务请求要用 passport 族的那张票据。
        """
        if family == "passport":
            gt = f"mock-pass-gt-{random.randint(10**6, 10**7 - 1)}"
            rand_key: str | None = f"mock-randkey-{random.randint(10**6, 10**7 - 1)}"
        else:
            gt = f"mock-flow-gt-{random.randint(10**6, 10**7 - 1)}"
            rand_key = None
        challenge = f"mock-challenge-{random.randint(10**6, 10**7 - 1)}"
        start = json.dumps({"success": 1, "challenge": challenge, "gt": gt}, ensure_ascii=False)
        _json(
            self,
            {
                "code": 0,
                "message": "Success",
                "zpData": {
                    "captchaType": 1,
                    "captchaName": "极验验证",
                    "wyCaptchaId": None if family != "passport" else "mock-wy-id",
                    "wyCaptchaType": None if family != "passport" else "0",
                    "startCaptcha": start,
                    "randKey": rand_key,
                },
            },
        )

    def _captcha_validate(self) -> None:
        """收下票据并**烧掉**它。缺票据头就按线上话术拒掉。

        烧票是因为极验 ``geetest_validate`` 是一次性凭证：真机上「先 validate
        再带同一张票重试业务请求」会再次 400061。假服务端跟着这个行为，免得把
        死路测成绿的。
        """
        missing = [h for h in REQUIRED_CAPTCHA_HEADERS if not self.headers.get(h)]
        if missing:
            return _json(
                self,
                {"code": 1, "message": "验证失败，请稍后重试", "zpData": {"missing": missing}},
            )
        validate = self.headers.get("Zp-Captcha-Validate") or ""
        if not validate:
            return _json(self, {"code": 1, "message": "验证失败，请稍后重试", "zpData": {}})

        BURNED_TICKETS.add(validate)
        challenge = self.headers.get("Zp-Captcha-Challenge") or "passed"
        _announce("[mock] ✓ 滑块票据已通过校验（并已作废，一次性凭证）")
        _json(
            self,
            {"code": 0, "message": "Success", "zpData": {}},
            cookies=[f"{SLIDER_PASS_COOKIE}={challenge}; Path=/"],
        )

    def _jiyan_ticket(self, form: dict) -> str:
        """取出极验票据的 validate 值；三个键不齐就不算有票据。

        键名照抄登录页 ``user-login.js`` 的 ``1==verifyType`` 分支，就
        ``challenge`` / ``validate`` / ``seccode`` 三个。``verifyToken`` /
        ``captchaToken`` 是客户端早期猜的键名，真机不认——这里也不认，免得
        测试把错的契约背书成绿的。``Zp-Captcha-*`` 请求头同理，那是
        zpsecureflow validate 的形状，业务接口不看。
        """
        if all(str(form.get(key) or "") for key in JIYAN_TICKET_KEYS):
            return str(form["validate"])
        return ""

    def _has_slider_ticket(self, form: dict) -> bool:
        """这个请求自己有没有带一张**没被烧掉**的极验票据。

        不能只看 validate 种的 Cookie —— 线上实测那样不够。也不能认已烧掉的票：
        validate 那步已经把一次性凭证用掉了。

        业务请求（smsCodeV2 / phoneV2）**不烧**票据：登录页的 ``verifyInfo`` 跨
        发码与登录共用，``Ce(!0)`` 和 ``Ce()`` 把**同一张**票挂到两个请求上，
        ``clearVerify`` 只在登录结束时调。所以假服务端也不在这里作废它——只有
        ``zpsecureflow/captcha/validate`` 那条链会烧（真机实测过）。
        """
        value = self._jiyan_ticket(form)
        if not value:
            return False
        if value in BURNED_TICKETS:
            _announce("[mock] ✗ 票据已被 validate 用掉（一次性），这次不算数")
            return False
        return True

    def _phone_of(self, form: dict) -> str:
        """出发码请求的手机号。

        V2 表单里没有 ``phone``——登录页的 ``je()`` 中间件把它换成了
        ``encryptedAccount``。这里解回来才能记账；解不动就退回明文 ``phone``。
        """
        if form.get("phone"):
            return str(form["phone"])
        token = str(form.get("encryptedAccount") or "")
        if not token:
            return ""
        try:
            return decrypt_account(token)
        except Exception:  # noqa: BLE001 - 解不动就当没给，让上层报「请输入手机号」
            _announce("[mock] ✗ encryptedAccount 解不出来，无法识别手机号")
            return ""


    def _send_sms_code(self, form: dict, *, version: int) -> None:
        phone = self._phone_of(form)
        LAST_SMS_REQUEST.clear()
        LAST_SMS_REQUEST.update(
            {
                "path": "smsCode" if version == 1 else "smsCodeV2",
                "version": version,
                "phone": phone,
                "form": dict(form),
                "headers": {k: v for k, v in self.headers.items()},
            }
        )
        if not phone:
            return _json(self, {"code": 1, "message": "请输入手机号", "zpData": {}})

        if phone in BLOCKED_PHONES:
            return _json(self, {"code": 31, "message": "当前 IP 已被封禁", "zpData": {}})

        # 过掉滑块三件事缺一不可：
        #   1. 打的是 **V2**；V1 那层根本不读票据
        #   2. **这个请求自己**带极验三件套 challenge/validate/seccode
        #      （Zp-Captcha-* 请求头不算，validate 种的 Cookie 也不算）
        #   3. 票据没被烧掉 —— validate 收下就作废，再带同一张票重试照样 400061
        if phone in SLIDER_PHONES:
            if version != 2:
                _announce("[mock] ✗ 滑块票据只有 smsCodeV2 认，V1 这条路走不通")
                return _json(
                    self,
                    {"code": SLIDER_CODE, "message": SLIDER_MESSAGE, "zpData": SLIDER_PAYLOAD},
                )
            if not self._has_slider_ticket(form):
                return _json(
                    self,
                    {"code": SLIDER_CODE, "message": SLIDER_MESSAGE, "zpData": SLIDER_PAYLOAD},
                )

        # 对照组：话术里带「验证码」但属于普通失败，不该被判成风控
        if phone == PLAIN_REJECT_PHONE:
            return _json(self, {"code": 1004, "message": "验证码发送失败，请稍后重试", "zpData": {}})

        if phone in RISK_CONTROL_PHONES and not form.get("verifyToken"):
            return _json(
                self,
                {
                    "code": 37,
                    "message": "请先完成安全验证",
                    "zpData": {"seed": "mock-seed", "ts": str(int(time.time() * 1000)), "name": "geetest"},
                },
            )

        record = STORE.get(phone)
        now = time.time()
        if record and now - record["sent_at"] < RESEND_COOLDOWN_SECONDS and not form.get("verifyToken"):
            remain = int(RESEND_COOLDOWN_SECONDS - (now - record["sent_at"])) + 1
            return _json(
                self,
                {"code": 1001, "message": "发送太频繁了，请稍后再试", "zpData": {"retryAfter": remain}},
            )

        code = f"{random.randint(0, 999999):06d}"
        STORE[phone] = {"code": code, "sent_at": now, "expire_at": now + CODE_TTL_SECONDS, "attempts": 0}
        # 发码响应的 zpData.token 是 **smsToken**（短信会话票据），不是登录态。
        # getLoginSuggest 要拿它对账。
        sms_token = f"mock-sms-{random.randint(10**6, 10**7 - 1)}"
        SMS_TOKENS[phone] = sms_token
        _announce(f"[mock] 📱 发给 {phone} 的验证码是 {code}")
        _json(
            self,
            {
                "code": 0,
                "message": "success",
                "zpData": {"retryAfter": RESEND_COOLDOWN_SECONDS, "token": sms_token},
            },
        )

    def _login_suggest(self, form: dict) -> None:
        """登录前置检查。照登录页的 ``Re`` 建模：失败也不该阻断登录。

        请求体 ``{token: smsToken, encryptedAccount, code, regionCode}``。
        这里只做记录 + 轻校验（smsToken 对不对得上），不发登录态。
        """
        phone = self._phone_of(form)
        LAST_SUGGEST_REQUEST.clear()
        LAST_SUGGEST_REQUEST.update({"phone": phone, "form": dict(form)})

        token = str(form.get("token") or "")
        expected = SMS_TOKENS.get(phone, "")
        if expected and token and token != expected:
            _announce(f"[mock] ✗ getLoginSuggest 的 smsToken 对不上（{token!r} != {expected!r}）")
            return _json(self, {"code": 1, "message": "smsToken 不合法", "zpData": {}})
        _announce(f"[mock] getLoginSuggest：{phone or '?'} ok")
        _json(self, {"code": 0, "message": "success", "zpData": {}})

    def _login(self, form: dict, *, version: int) -> None:
        phone = self._phone_of(form)
        code = form.get("phoneCode", "")
        LAST_LOGIN_REQUEST.clear()
        LAST_LOGIN_REQUEST.update(
            {
                "path": "phone" if version == 1 else "phoneV2",
                "version": version,
                "phone": phone,
                "form": dict(form),
                "headers": {k: v for k, v in self.headers.items()},
            }
        )

        # 滑块放行条件与发码同一套：V2 + 极验三件套 + 没被 validate 烧掉。
        # 登录页 Ce() 把 verifyInfo 原样挂到 login，两张请求共用一张票。
        if phone in SLIDER_PHONES:
            if version != 2:
                _announce("[mock] ✗ 滑块票据只有 phoneV2 认，V1 这条路走不通")
                return _json(
                    self,
                    {"code": SLIDER_CODE, "message": SLIDER_MESSAGE, "zpData": SLIDER_PAYLOAD},
                )
            if not self._has_slider_ticket(form):
                return _json(
                    self,
                    {"code": SLIDER_CODE, "message": SLIDER_MESSAGE, "zpData": SLIDER_PAYLOAD},
                )

        if not phone:
            return _json(self, {"code": 1, "message": "请输入手机号", "zpData": {}})

        record = STORE.get(phone)
        if not record or time.time() > record["expire_at"]:
            return _json(self, {"code": 2002, "message": "验证码已过期，请重新获取", "zpData": {}})
        if record["attempts"] >= MAX_ATTEMPTS:
            return _json(self, {"code": 2003, "message": "错误次数过多，请重新获取验证码", "zpData": {}})
        if record["code"] != code:
            record["attempts"] += 1
            if record["attempts"] >= MAX_ATTEMPTS:
                return _json(
                    self, {"code": 2003, "message": "错误次数过多，请重新获取验证码", "zpData": {}}
                )
            return _json(self, {"code": 2001, "message": "验证码错误，请重新输入", "zpData": {}})

        # 验证通过即失效；首次登录即注册。
        STORE.pop(phone, None)
        token = f"mock-token-{random.randint(10**11, 10**12 - 1)}"
        user = {
            "userId": 100000 + random.randint(0, 899999),
            "name": f"求职者{phone[-4:]}",
            "isNewUser": True,
        }
        SESSIONS[token] = {**user, "phone": phone}
        # 真实站点的登录响应体里**没有**会话 token——鉴权完全靠 Set-Cookie。
        # body 只放路由与用户字段，凭证只在 Cookie 里。
        _json(
            self,
            {
                "code": 0,
                "message": "success",
                "zpData": {
                    **user,
                    "identity": 0,
                    "isCompletion": True,
                    "toUrl": "/web/geek/guide/",
                    "webHost": "https://www.zhipin.com",
                },
            },
            cookies=[f"zp_at={token}; Path=/; HttpOnly", f"wt2={token[:8]}; Path=/"],
        )


    def _filter_data(self, path: str) -> None:
        """按路径回筛选项，形状与真实 wapi 一致。"""
        if FILTER_API_DOWN:
            return _json(self, {"code": 10001, "message": "system error", "zpData": {}})
        if path.endswith("/hot/city.json"):
            return _json(self, {"code": 0, "message": "Success", "zpData": {"hotCityList": MOCK_HOT_CITIES}})
        if path.endswith("/city.json"):
            return _json(
                self,
                {
                    "code": 0,
                    "message": "Success",
                    "zpData": {
                        "hotCityList": MOCK_HOT_CITIES,
                        "cityList": MOCK_CITY_LIST,
                        "locationCity": MOCK_CITY_LIST[1]["subLevelModelList"][0],
                    },
                },
            )
        if path.endswith("/cityGroup.json"):
            return _json(
                self,
                {
                    "code": 0,
                    "message": "Success",
                    "zpData": {
                        "cityGroup": [{"firstChar": "B", "cityList": [MOCK_CITY_LIST[0]]}],
                        "hotCityList": MOCK_HOT_CITIES,
                        "locationCity": None,
                    },
                },
            )
        # conditions.json 三兄弟共用一份（recommend 没有 stageList，测试不依赖它）
        return _json(self, {"code": 0, "message": "Success", "zpData": dict(MOCK_CONDITIONS)})


    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _auth_token(self) -> str:
        cookie_header = self.headers.get("Cookie") or ""
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "zp_at":
                return value
        return ""


def serve(port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    _announce(f"[mock] 假服务端已启动：http://{host}:{port}")
    _announce("[mock] 风控号码 13800138010 ｜ 封禁号码 13800000000")
    _announce("[mock] 滑块号码 13800138020 ｜ 普通失败对照 13800139010")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="BOSS直聘登录流程本地假服务端")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = serve(args.port, args.host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _announce("\n[mock] 已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
