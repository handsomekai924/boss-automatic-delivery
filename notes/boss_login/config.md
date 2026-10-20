# `boss_login.config` 协议笔记

> 摘自 `boss_login/config.py` 的模块文档字符串 / `#` 注释。

## 一句话

接口地址、请求头、业务码等常量配置；线上路径的核实状态与表单形状记在这里。

## 细节

### ⚠️ 关于接口地址的可信度

路径的来源分两档：

**已从按需 chunk 里读到**（``static.zhipin.com/zhipin-sign/v5330/static/js/
user-login.5398946e.js``，短信登录真正调的那一份）——发码/登录的路由、表单形状、
票据挂载方式都是从这里抄的，见下面 `send/smsCodeV2` 那一段。

**已实测确认**（2026-09-29 直接打真实站点）：
  - ``send/smsCode`` = ``/wapi/zppassport/send/smsCode``  → 回业务码 400061，路由正确
  - ``zppassport/captcha/getTypeV2`` → 业务链的滑块挑战（gt=64568016…，带真 randKey）
  - ``zppassport/captcha/validate``  → **不存在，404**
  - ``zpsecureflow/captcha/gettype`` / ``validate`` → verify.html 页那族，另一个 gt

**仍未核实**：``login/phone(V2)`` / ``user_info`` / ``logout`` 的线上行为。

未核实的路径提供了两条兜底：
  1. 运行 `python -m boss_login probe` 探测候选地址，确认后再写回 ENDPOINTS；
  2. 用命令行参数临时覆盖：--endpoint-send / --endpoint-login。

### `ENDPOINTS`

短信登录相关接口。若线上已变更，用 probe 或命令行参数覆盖。

V1 是「不带极验票据」那代；V2 是登录页在极验通道（verifyType=1）下真正调的
那个（见按需 chunk user-login.js 的 ``B = {1:{smsCode:"…/smsCodeV2", …}}``）。
滑块票据只有 V2 认——带着票据去重试 V1，服务端照样 400061。

`login_suggest`：登录前置检查。登录页 ``loginOrRegister`` 的中间件链末位就是它
（``Re({token: smsToken, encryptedAccount, code, regionCode})``），
失败会被吞掉，但**每次登录都会打**。

### `QR_ENDPOINTS`

APP 扫码登录（登录页 ``BossAppScan``）的接口。

来源是按需 chunk ``user-login.5398946e.js`` 里 BossAppScan 用到的那几个
请求构造器（``Ie/Fe/Ue/qe/Le/$e/ze``），路径与调用顺序都照抄。2026-10-13
打过真实站点：``randkey`` 能拿到 ``qrId``，``scan`` 会挂住长轮询，
``scanLogin`` 在没人扫时立刻回 ``{scaned:true, login:false}``（真正等人确认时
才会挂住），``getSecondKey`` 未扫码时回 ``zpData:{code:1092}``。

各键：
- `qr_randkey`：POST 无参。zpData: {qrId, randKey, secretKey, shortRandKey}
- `qr_scan`：GET ?uuid=<qrId>  长轮询，等手机扫；回 {scaned, validate?, allweb?}
- `qr_second_key`：GET ?uuid=<qrId>  扫过之后换发第二把钥匙；zpData.qrId → secondUuid
- `qr_scan_second`：GET ?uuid=<secondUuid>  长轮询，等 App 端二次确认；回 {scaned}
- `qr_head_img`：GET ?qrId=<qrId>  拿扫码账号头像；zpData: {large, wxMp}
- `qr_scan_login`：GET ?qrId=<qrId>  长轮询，等确认/取消；回 {scaned, login}
- `qr_dispatcher`：GET ?qrId=<qrId>&pk=…&fp=…  换登录态，Set-Cookie 就在这一发上

### `send/smsCodeV2` 的表单形状

来自 user-login.js 的 Ce(!0) + je()。登录页发短信的中间件链是
[_e, Oe, ke, Ee, Ce(!0), je()]，最终表单是::

    regionCode=+86
    pk=cpc_user_sign_up          # 构造参数，缺省 "cpc_user_sign_up"
    smsType=7                    # 写死的，不是用户选的场景码
    purpose=<identity>           # 0=求职者 / 1=招聘者，与 formData.identity 同值
    version=1                    # Ce(true) 才加，登录那条路没有
    encryptedAccount=<AES(phone)>   # je() 把 phone 换掉，表单里不再有 phone
    challenge=… validate=… seccode=…   # 仅极验通道；见 SliderSolution.as_form_fields

注意：登录页构造 SmsAccount 时 **没有** 传 requestHeaders，所以业务请求上
不带 Zp-Captcha-* 头——那套头是 zpsecureflow validate 的形状，别混过来。

### APP 扫码登录调用顺序

登录页 ``SignLoginScanApp`` → ``new BossAppScan({expireTime: 3e4})``：

    randkey ──► uuid=qrId（二维码内容就是这串 uuid）
       │
       ├─ 长轮询 qrcode/scan?uuid=          等手机扫
       │        └─ scaned → captcha/getSecondKey?uuid= → secondUuid
       │                   （页面上的二维码内容跟着换成 secondUuid，但此时
       │                     UI 已经切成「扫码成功」步骤条，二维码不再露出）
       ├─ 1 秒兜底定时器 与 qrcode/scanSecond?uuid=secondUuid 赛跑
       │        └─ 先到者：getHeadImg + 长轮询 qrcode/scanLogin?qrId=
       └─ scanLogin 回 {scaned, login}：
                login=true  → qrcode/dispatcher?qrId=…&pk=…&fp=… 换 Set-Cookie
                login=false → 取消，整段刷新二维码

`QR_EXPIRE_SECONDS`：页面上二维码的存活时间（毫秒转秒）。超时只影响展示层，服务端的票据另有寿命。

`QR_POLL_TIMEOUT`：``scan`` / ``scanSecond`` / ``scanLogin`` 三个长轮询接口的单次请求超时。
登录页 axios 上写的是 ``timeout: 1e5``（100 秒）。

`QR_CONFIRM_DELAY_SECONDS`：扫码成功后，页面先等 1 秒再开始 ``scanLogin``（``secondScanTimer``）；
期间 ``scanSecond`` 若先返回 ``scaned``，就不再等这 1 秒。

`QR_CONFIRM_TIMEOUT_SECONDS`：本地对「扫码成功 → App 端确认」的兜底等待。登录页本身不设上限
（二维码过期才停），这里给个宽限，免得脚本挂死。

`QR_CONTENT_PREFER_SECOND`：App 扫码后二维码内容的取值顺序（``qrCodeContent = secondUuid || uuid``）。

`QR_LOGIN_LIMIT_CODES` / `QR_LOGIN_LIMIT_RANGE`：登录限额 / 风控码（登录页 ``loginLimitCode`` + 4942–4950 区间）。
命中就别再重试了，直接把提示语抛给使用者。

`QR_DEFAULT_PK`：页面的 ``pk`` 来自 ``#page_key_name``，扫码登录那页写的是 ``cpc_user_sign_up``。

### 业务码

`CODE_MESSAGES`：业务失败码（按接口语义归类，便于给出精准提示）。

风控码 —— 从登录页 passport SDK（vendors.c30ef7fa.js）中提取，可信度高::

  t.ZpPassportCode = { IP_BLOCK:31, UID_BLOCK:32, IP_GRAY:35, UID_GRAY:36,
                       SECURITY_CHECK:37, ANTI_SPIDER_LOGIN:38 }
  t.CmPassportCode = { IP_BLOCK:-1000031, UID_BLOCK:-1000032, IP_GRAY:-1000035,
                       UID_GRAY:-1000036, SECURITY_CHECK:-1000037 }

滑块验证 —— **实测确认**：真实站点 /wapi/zppassport/send/smsCode 返回
  {"code": 400061, "message": "请完成滑块验证"}
注意它不在上面的 SDK 常量表里：SDK 那套是 passport 内部码，400xxx 是 wapi 网关码，
两个码空间并存。

`RISK_MESSAGE_KEYWORDS`：兜底用的提示语关键字。线上验证码族还有若干近亲（400060/400062…），
离线拿不到完整码表，但这类响应的话术里必定带下列词之一，按文案判定比猜码更可靠。
特意收窄，避免误伤「验证码错误」这类普通业务失败。

`SLIDER_MESSAGE_KEYWORDS`：其中真正指「滑块」的话术 —— 用来把滑块与 SECURITY_CHECK(37) 那种
「跳去 security.html 的安全验证」分开。后者带 {seed,ts,name}，协议完全不同，不能套用滑块链路。

`TOKEN_FIELDS`：登录成功后 token 可能出现的字段。

⚠️ 真实站点的 ``login/phoneV2`` 响应体里**没有**会话 token——登录页的成功
处理（``m.F``）只读路由字段（``identity`` / ``isCompletion`` / ``toUrl`` …），
鉴权完全靠 ``Set-Cookie``。这张表是给「响应体里真给了 token」的兼容层用的
（假服务端就是这么回的），别指望线上能从 body 里抠出 token。

`SMS_TOKEN_FIELD`：发码响应 ``zpData.token`` 的字段名。这是 **smsToken**（短信会话票据），
不是登录态——登录页把它存进 ``smsToken``，再交给 ``getLoginSuggest``。

`AUTH_COOKIES`：登录成功后通常会下发的 Cookie。真实站点鉴权就在这里；
名字认不出来时客户端会把实到的 Cookie 名打出来，
免得永远卡在「既没有 token 也没有鉴权 Cookie」。

### 滑块验证（极验 Geetest）

接口契约来自两处实测（2026-09-29）：
  1. 直接打真实站点，拿到的 JSON 形状；
  2. 登录页 verify.html 的脚本 + 它按需加载的
     captcha-sdk@5.1.3.min.js（static.zhipin.com）里写死的路径与请求头。
两条线互相印证，可信度高。

`VERIFY_ENDPOINTS`：验证接口。**两族不是同一条链，不能混用**（2026-09-29 实测）：

  zppassport 族  —— 业务接口（send/smsCodeV2 等）同族，走这套
      GET  /wapi/zppassport/captcha/getTypeV2   下发挑战（gt=64568016…，带真 randKey）
      /wapi/zppassport/captcha/validate          **不存在，实测 404**
  zpsecureflow 族 —— verify.html 独立验证页用的那套，下发的是**另一个 gt**（413f33d3…）
      GET  /wapi/zpsecureflow/captcha/gettype?scene=passport-verify
      POST /wapi/zpsecureflow/captcha/validate   （请求体为空，票据在 Zp-Captcha-* 头）

业务请求上的票据怎么挂 —— user-login.js 的 ``Ce`` 写得很直白，**只有表单字段**::

    1==verifyType ? {challenge, validate, seccode}     # 极验
    3==verifyType ? {captcha, randKey}                 # 图片
    4==verifyType ? {validate}                         # 易盾

那份 chunk 里 ``Zp-Captcha`` 出现 **0 次**——登录页压根不带这套头。它们是
captcha-sdk onSuccess 给 zpsecureflow validate 用的，别混到业务请求上。
极验票据是一次性凭证：先交去 zpsecureflow/validate 会把它烧掉，业务请求再带
同一张就只剩 400061（真机踩过）。

`SLIDER_CHALLENGE_ENDPOINT_KEY`：业务请求的滑块挑战默认取自哪条 key —— 必须与业务接口同族。

验证通道。来自 captcha-sdk@5.1.3 的 CaptchaType 枚举：
  Jiyan=1 / Ali=2 / Picture=3 / Yidun=4
实测 gettype / getTypeV2 返回的都是 1（极验滑块）。

`GEETEST_LOADER_URL`：极验加载器。BOSS 把官方 gt.0.5.0 镜像在自己 CDN 上，内容与
Geetest Inc. 的发行版一致（首行 "v0.5.0 Geetest Inc."），用镜像即可。

`GEETEST_API_SERVERS`：initGeetest 的 api_server_v3，与 captcha-sdk 里传的值保持一致。

`CAPTCHA_HEADER_*`：求解成功后要随 validate 一起带上的请求头。
键名来自 captcha-sdk onSuccess 的 headers 对象，不是猜的。
`CAPTCHA_HEADER_RANDKEY`：Picture 通道会带；Jiyan 的 onSuccess 没塞，带上无害。

`REDIRECT_SKIP_VERIFY`：getredirect 返回 true 表示这次不用验证，可直接跳走。

`SLIDER_HELPER_*`：本地求解帮助页；等人工拖完滑块的上限。超时直接失败，不让脚本静默长眠。

### 本地上限（与服务端一致，避免无谓请求）

`RESEND_COOLDOWN_SECONDS`：同一号码重发间隔（秒）。
`CODE_LENGTH`：验证码位数。
`MAX_CODE_ATTEMPTS`：同一号码连续输错几次后放弃。
`MAX_RESEND_WAIT_SECONDS`：重发验证码时，最多愿意等待服务端冷却多久（秒）。超过这个值直接失败，
以免脚本静默地睡很久——这种情况通常意味着配额已耗尽或触发风控。
`CN_PHONE_PREFIX_LENGTH`：本地校验用的手机号规则。
