# `boss_login.verify` 协议笔记

> 摘自 `boss_login/verify.py` 的模块文档字符串 / `#` 注释。

## 一句话

滑块验证协议层 —— 拉挑战、把题交给本人解、组装票据。

## 细节

业务请求被 400061 拦下后的那条链（passport 族）::

    GET  /wapi/zppassport/captcha/getTypeV2  ──►  {gt, challenge, randKey}
                                        │
                                        ▼
                          本地帮助页加载官方极验组件（gt.0.5.0）
                          ┌──────────────────────────────────────┐
                          │  你本人拖动滑块，极验在浏览器里出结果  │
                          └──────────────────────────────────────┘
                                        │  {challenge, validate, seccode}
                                        ▼
                     重试 —— 打 **send/smsCodeV2**，票据在**表单**里：
                       challenge / validate / seccode
                     （极验通道就这三个键；user-login.js 的 Ce 写死了）

**这条链上没有 validate，也没有 Zp-Captcha 请求头**。passport 族根本没有
validate 路由（实测 404）。那套请求头是 zpsecureflow validate 的形状；登录页
chunk 里 ``Zp-Captcha`` 出现 0 次——业务请求只把票据放在表单上。极验票据是
一次性凭证——先拿去 ``zpsecureflow/validate`` 会把它烧掉，业务请求再带同一张
就只剩 400061（真机踩过）。

``zpsecureflow/captcha/gettype + validate`` 是 verify.html 独立验证页那条链，
下发的是**另一个 gt**，它的票据打不动 zppassport 的业务接口。见
`validate_request_headers` / `ZhipinLoginClient.run_slider_verify`。

设计要点
    * **不破解滑块**：缺口在哪、要拖多远、拖动轨迹怎么伪造，这里一概不管。
      解题永远是浏览器里的官方极验组件 + 你的手。
    * 协议层只做三件事：把挑战参数取出来、把官方组件架起来、把结果装回请求。
    * 网络层 / 时钟 / 随机数全部可注入，``generate_trace_id`` 等纯函数可离线对拍。

关于 traceId
    verify.html 的 ``getCommonHeaders()`` 会给每个请求带一个 ``traceId``，
    形状是 ``F-<13 位十六进制时间戳 + 6 位随机字符><3 位校验符>``。校验符有两条
    实现：Node 环境走 HMAC-SHA256，浏览器走一套 32 位位运算。真实站点是浏览器
    发的请求，所以这里复刻**浏览器那条**（见 ``_compute_checksum``）。

### `SliderChallenge`

``captcha_type``：通道号，1=极验滑块（实测返回的就是这个）。
``rand_key``：zppassport 族会带；zpsecureflow 族为 None。

`require_geetest`：目前只实现了极验滑块的求解链路，别的通道直接说清楚。
返回的实测样本里 ``captchaType`` 恒为 1，所以这里不是假想的防御——
真换了通道（阿里 / 易盾 / 图片），得先看清它的协议再补。

### `SliderSolution`

人工拖完滑块后，极验组件给出的三个票据。
字段名跟 ``captchaObj.getValidate()`` 对齐：
``geetest_challenge`` / ``geetest_validate`` / ``geetest_seccode``。

`as_headers`：按 captcha-sdk ``onSuccess().headers`` 的形状给出票据请求头。
键名取自 captcha-sdk@5.1.3 的 onSuccess，别改名字。这套请求头挂到
**被拦下的业务请求本身**上就是通行证——passport 族没有 validate 路由，
也不需要先登记。
Jiyan 通道的 onSuccess 只给 Type/Challenge/Validate/Seccode 四个头，
**不带** Randkey（Picture 通道才带）。这里在有 randKey 时顺手补上，
多一个头服务端不认识就忽略。

`as_form_fields`：把票据铺成 **业务请求的表单字段**。
键名不是猜的：登录页 chunk ``user-login.js`` 的 ``Ce`` 中间件按通道挂载::

    1==verifyType ? {challenge, validate, seccode}   # 极验（本项目走这条）
    3==verifyType ? {captcha, randKey}               # 图片
    4==verifyType ? {validate}                       # 易盾

**没有** ``verifyToken`` / ``captchaToken``，极验通道也**不带** ``randKey``
（那是图片通道的）。那份 chunk 里 ``Zp-Captcha`` 出现 0 次——业务请求不带
那套头，它们是 zpsecureflow validate 的形状。

### `_compute_checksum` / `generate_trace_id`

复刻 verify.html 的浏览器实现。``generate_trace_id`` 生成
``F-<13 位十六进制毫秒时间戳><6 位随机字符><3 位校验符>``，
与 verify.html 的 ``generateBossTraceID()`` 同构；校验符走浏览器那条分支。

`_js_mul_u32`：注意这一步在 JS 里是 **Number 乘法**（float64），乘积超过 2**53 时低位会被
舍掉。Python 的 int 是精确的，所以这里先转 float 相乘，才能和浏览器逐位对齐。

`_to_uint32`：JS 的 ``>>> 0``。传进来的可能是 float64 乘积，先按 JS 的 ToInteger 截断。

### `parse_challenge`

把 gettype / getTypeV2 的 ``zpData`` 拍成 `SliderChallenge`。
实测形状::

    {"captchaType":1,"captchaName":"极验验证","wyCaptchaId":null,
     "wyCaptchaType":null,
     "startCaptcha":"{\\"success\\":1,\\"challenge\\":\\"...\\",\\"gt\\":\\"...\\"}",
     "randKey":null}

``startCaptcha`` 是**字符串化的 JSON**，这点不看实测根本猜不到。

### `parse_solution`

同时认两套键名：极验原始的 ``geetest_*`` 与帮助页收的短键，免得调用方记混。

### 本地帮助页 / 帮助服务

`_HELPER_HTML`：页面里内嵌了挑战参数，打开就能拖，不需要再回服务端取。
页面里跑的是极验**官方**组件，答案由你本人拖出来。这里只是把
「挑战参数 → 浏览器 → 票据 → 客户端」这段管道接好。

`_HelperHandler`：由 SliderHelper 在启动前塞进 html / result_box / ready。

### `validate_request_headers`

组装 ``POST zpsecureflow/captcha/validate`` 要带的请求头。
verify.html 里是 ``c.send("")``：请求体为空，票据全在请求头上。
另外还带 ``traceId`` / ``zp_token``（取自 ``bst`` Cookie），这里一并给你。

**只给 verify.html 那条链用**。业务请求的自动衔接走
`SliderSolution.as_headers`，别把票据先交来这里——极验票据一次性。
