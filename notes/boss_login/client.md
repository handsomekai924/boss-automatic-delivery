# `boss_login.client` 协议笔记

> 摘自 `boss_login/client.py` 的模块文档字符串 / `#` 注释。

## 一句话

短信登录客户端 —— 完整流程的核心实现。

## 细节

流程总览::

    校验手机号 ──► 本地冷却检查 ──► POST 发送验证码 ──► 记下冷却与票据
                                        │                （zpData.token = smsToken）
                                        ├─ 命中限流   → RateLimited(retry_after)
                                        ├─ 命中滑块   → 拉挑战 → 本人拖滑块
                                        │              → 带票据打 **smsCodeV2** 重试
                                        │              （不调 validate：票据一次性）
                                        ├─ 命中风控   → RiskControlRequired(seed/ts/name)
                                        └─ IP/账号封禁 → AccountBlocked
                                        ▼
              人工输入短信验证码 ──► getLoginSuggest ──► POST 登录 ──► 收 Set-Cookie
                                        │（失败不阻断）        │        ──► LoginResult
                                        │                     │
                                        │                     ├─ 命中滑块 → 先复用发码那张票打 **phoneV2**
                                        │                     │             → 不认就重新拖一次再打 phoneV2
                                        │                     ├─ 验证码错误/过期 → CodeRejected（重新输入）
                                        │                     └─ 成功            → 落盘登录态

设计要点
    * 网络、休眠、时钟全部可注入，便于离线单测（见 tests/）。
    * 服务端返回码 → 异常类型的映射集中在 ``_handle_response``，调用方按类型捕获即可。
    * **真实站点的登录响应体里没有会话 token**：登录页的成功处理只读路由字段，
      鉴权完全靠 ``Set-Cookie``。Cookie 名还可能不在 ``AUTH_COOKIES`` 里，
      所以「这次登录新种的 Cookie」本身就当凭证，由 ``user_info`` 验真伪。
    * **不实现验证码破解**：滑块的题由本人在浏览器里拖（官方极验组件），
      本模块只负责把挑战参数递过去、把票据交回来。其余风控仍然抛
      ``RiskControlRequired`` 交由使用者处理。
    * **APP 扫码登录**是另一条链（等手机扫、等 App 确认），实现在
      `boss_login.qrcode`，跟这里的短信流程共用同一个会话。

### `ZhipinLoginClient.send_sms_code`

- 参数校验放在最前：非法场景码应当在冷却检查等有状态判断之前就失败。
- 滑块可以人机协作过掉，过完带着票据重试一次；其余风控原样抛出。
- 注意：这里走 solve_slider，**不调 validate**。passport 族没有
  validate 路由，而且极验票据一次性——先交去 validate 会把它烧掉。
- 登录那一步会先试着复用这张票据（登录页 verifyInfo 就是跨两步共用的）。
- 重试打的是 **V2**：登录页在极验通道下只调 send/smsCodeV2，票据也
  只有那一层认。带着票据去重试 V1，服务端照样 400061。
- 票据挂在请求上了还被拦，说明还缺东西（键名不对、票据过期……）。
  别再抛一句「请完成滑块验证」把人绕回去——那条路刚走完。
- 发码响应里的 zpData.token 是 smsToken，登录前置的 getLoginSuggest 要用。

### `ZhipinLoginClient.login_by_sms`

- 登录页的 loginOrRegister 中间件链末位是 getLoginSuggest（Re），
  拿发码那步的 smsToken + 验证码打一发。失败会被吞掉，但每次登录都会打。
- 真实站点的登录响应体里没有会话 token，鉴权靠 Set-Cookie。拍个快照，
  好把「这次登录新种的 Cookie」单独认出来——名字可能不在 AUTH_COOKIES 里。
- 登录页在极验通道下走 ``login/phoneV2``，票据只在那层认。有票据就直接
  打 V2；没有就先按 V1 探一次——真实站点会回 400061，然后再解题重试。
- 复用失败 = 票据已烧 / 已过期；或者刚才那发 V1 压根没带票据。
  两种情况都得重新解一张，不能再拿旧的凑数。
  **不调 validate**：passport 族没有那条路由，而且极验票据一次性。
- 真实站点的登录响应体里没有 token（登录页的成功处理只读路由字段），
  鉴权靠 Set-Cookie。连 Cookie 都没种 = 这次登录根本没换到登录态。
  把实到的形状打出来，免得只能瞎猜该适配哪个字段。
- 种了 Cookie 但名字一个都不认识——先放行，让 user_info 去验真伪，
  同时把名字打出来，好把它们补进 AUTH_COOKIES。
- 登录成功后验证码即失效，清掉本地冷却，允许下次立即发起。

### `ZhipinLoginClient._call_login_suggest`

登录页 ``loginOrRegister`` 的中间件链**末位固定是它**（``Re``），每次登录
都打，请求体 ``{token: smsToken, encryptedAccount, code, regionCode}``。
整个调用裹在 try/catch 里，失败只上报埋点，登录照常往下走——所以这里也
绝不能让它抛出去。

``smsToken`` 是发码响应的 ``zpData.token``。``send`` / ``login`` 若是两条
命令（两个进程），这一步可能拿不到它，那就照登录页那样传空——总比不打好。

### `ZhipinLoginClient.solve_slider`

- 业务请求的自动衔接就用这条。passport 族没有 validate 路由（实测 404）；
  captcha-sdk 的 onSuccess 也只把 ``Zp-Captcha-*`` 交回调用方，由调用方挂到
  被拦下的那个请求上。极验票据是一次性凭证，先交去 validate 会把它烧掉。
- 挑战里的 randKey 一并带上。getTypeV2 特意下发它，onSuccess 也回传它，
  多半就是要跟着业务请求走的。

### `ZhipinLoginClient.submit_slider_solution`

- 请求体是空的，票据全在请求头上——这是 verify.html 的 ``validate`` 调用方式，
  不是自创的。
- **不要**在业务请求的自动衔接里调它：passport 族没有这个路由，而且极验
  票据一次性，先交在这里会把票据烧掉，业务请求再带同一张就只剩 400061。

### `ZhipinLoginClient.fetch_slider_challenge`

默认走 **passport 族**（与 ``send/smsCode`` 同族）::

    GET /wapi/zppassport/captcha/getTypeV2
    → {"code":0,"zpData":{"captchaType":1,"captchaName":"极验验证",
       "startCaptcha":"{\\"gt\\":\\"…\\",\\"challenge\\":\\"…\\"}",
       "randKey":"…"}}

同族很重要：两边共用同一个极验 gt 和同一套服务端会话。verify.html 用的
zpsecureflow ``gettype`` 下发的是**另一个 gt**，拿它的票据去打 zppassport
的业务接口，服务端不认（真机踩过）。

### `ZhipinLoginClient._sms_v2_form` / `_retry_send_sms_v2`

来源是按需 chunk ``user-login.js``，不是猜的::

    Ce(t=true):  {...formData除 identity/phoneCode, pk, smsType:7,
                 purpose:identity, version:1}  + 票据
    je():       phone → encryptedAccount（AES，见 boss_login.crypto）

所以表单里**没有** ``phone``，也**没有** ``verifyToken`` / ``captchaToken``。
极验通道的票据只有三个键：``challenge`` / ``validate`` / ``seccode``。

三处都跟登录页对齐，错一处服务端就还是 400061：

1. **路径**：极验通道走 ``/wapi/zppassport/send/smsCodeV2``（``B[1].smsCode``），
   不是 V1 的 ``send/smsCode``。
2. **表单**：票据是 ``challenge`` / ``validate`` / ``seccode`` 三个键，
   叠在 V2 的表单形状上（``encryptedAccount`` / ``smsType=7`` / ``version=1`` …）。
3. **不带 Zp-Captcha 请求头**：那套是 zpsecureflow validate 的形状；
   登录页的 ``requestHeaders`` 是空的，业务请求只把票据放表单。

这里**不**调 validate——passport 族没有那条路由，而且极验票据一次性，
交出去就没了。

### `ZhipinLoginClient._login_v2_form` / `_retry_login_v2`

跟发码那张（``Ce(!0)``）只有两处差别，来源同样是 ``user-login.js``::

    Ce(t=false): {...formData除 identity/phoneCode, pk, smsType:7,
                  purpose:identity} + phoneCode   ← 没有 version:1
    je():        phone → encryptedAccount

所以登录表单里**没有** ``version``、**没有** ``phone``，但多一个
``phoneCode``。票据仍是 ``challenge`` / ``validate`` / ``seccode`` 三键。
``fp``（浏览器指纹）那步在登录页是 try/catch，取不到就跳过——这里先不带。

### `ZhipinLoginClient._resend_after_failure`

本地与服务端的冷却可能对不上（时钟漂移，或另一台设备刚发过验证码）。
以服务端给的剩余秒数为准再等一次；超过上限就放弃，避免无意义的长眠。

### `ZhipinLoginClient._is_slider_challenge`

刻意比 ``_is_risk_control`` 窄：SECURITY_CHECK(37) 那种「跳去
security.html」的安全验证带 ``{seed,ts,name}``，协议完全不同，
不能套滑块链路。滑块只认 400061 及其话术近亲。

### `ZhipinLoginClient._is_risk_control`

先认码表，再认提示语。之所以要认提示语：滑块验证的码位不止一个（实测到
400061，同类还有若干近亲），离线拿不到完整码表，而这类响应的话术是稳定的。
关键字在 config 里收得很窄，不会误伤「验证码错误」这种普通业务失败。

### `ZhipinLoginClient._extract_token`

⚠️ 真实站点的 ``login/phoneV2`` 响应体里**没有** token——登录页的成功
处理只读路由字段，鉴权靠 Set-Cookie。这里取到空是常态，不是失败。
