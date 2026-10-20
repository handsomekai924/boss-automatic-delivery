# `tools.mock_server` 协议笔记

> 摘自 `tools/mock_server.py` 的模块文档字符串 / `#:` 注释 / `#` 注释 / 文档字符串。

## 一句话

本地假服务端：按真实接口形状离线演练登录 / 发码 / 滑块 / 筛选条件接口；滑块票据规则、两族挑战 gt、以及 2026-09-30 实测的筛选路由形状都在这里。

## 细节

### 模块：路由表（按真实接口形状实现）

```
POST /wapi/zppassport/send/smsCode     下发验证码（V1；**不认**滑块票据）
POST /wapi/zppassport/send/smsCodeV2   下发验证码（V2；认极验票据）
POST /wapi/zppassport/login/phone      登录（V1；**不认**滑块票据）
POST /wapi/zppassport/login/phoneV2    登录（V2；认极验票据）
POST /wapi/zppassport/validate/getLoginSuggest   登录前置检查（失败不阻断）
GET  /wapi/zpuser/wap/getUserInfo.json
POST /wapi/zppassport/user/logout
GET  /wapi/zppassport/captcha/getTypeV2        业务请求的滑块挑战（带真 randKey）
POST /wapi/zppassport/captcha/validate         **404** —— passport 族没这路由
GET  /wapi/zpsecureflow/captcha/gettype        verify.html 页的挑战（另一个 gt）
POST /wapi/zpsecureflow/captcha/validate       收下票据并**烧掉**（一次性）
```

用法：

```
python tools/mock_server.py --port 8765
# 另开一个终端：
python -m boss_login login --phone 13800138000 \
    --base-url http://127.0.0.1:8765
```

验证码不会真的发短信，而是直接打印在服务端日志里。触发风控的手机号是 13800138010，被封禁的手机号是 13800000000。

### 模块：滑块票据契约

滑块挑战（400061）的放行条件照着登录页 chunk `user-login.js` 的 `Ce` 建模：**极验票据只有三个表单键** `challenge` / `validate` / `seccode`，而且只有 V2 那层（`smsCodeV2` / `phoneV2`）认。带着票据去打 V1 照样 400061——真机正是这样。请求头 `Zp-Captcha-*` 是 zpsecureflow validate 那条链的形状，**业务接口不认**。

登录那步跟发码用的是**同一张**票：登录页的 `verifyInfo` 跨两步共用（`Ce(!0)` 和 `Ce()` 挂的是同一个对象），`clearVerify` 只在登录结束时调。所以业务请求**不烧**票据，只有 `zpsecureflow/captcha/validate` 会烧。

两族挑战故意下发不同的 gt（`mock-pass-gt-*` vs `mock-flow-gt-*`），方便测试认出客户端有没有取错族。线上实测过：拿 zpsecureflow 的票据去打 zppassport 的业务接口，服务端不认。

票据是一次性凭证：`validate` 收下就烧掉。所以「先 validate 再带同一张票重试」会被再次 400061——真机正是这样。假服务端跟着这个行为，免得把死路测成绿的。

### `SLIDER_*` / `JIYAN_TICKET_KEYS` / `REQUIRED_CAPTCHA_HEADERS`

复刻真实站点实测到的形状：`{"code": 400061, "message": "请完成滑块验证"}`。`zpData` 这里故意放**无法识别的字段名**：真实响应带什么字段离线没核实过，客户端必须原样回显出来，否则永远不知道该怎么适配。

通行 Cookie 名 `mock_slider_pass`：validate 会种它，**但业务接口不认**。

票据请求头（与 captcha-sdk onSuccess 的 headers 对齐）：`Zp-Captcha-Type` / `Zp-Captcha-Challenge` / `Zp-Captcha-Validate` / `Zp-Captcha-Seccode`。**业务接口不认**，只有 zpsecureflow/captcha/validate 那条链用它。

极验通道的票据 —— user-login.js 的 `1==verifyType` 分支，就 `challenge` / `validate` / `seccode` 三个表单键。

### 筛选条件接口（boss_filter）

路径与真实站点一致（2026-09-30 实测），形状照抄 wapi 返回：

- `GET /wapi/zpgeek/pc/all/filter/conditions.json` → zpData 里一堆 XxxList
- `GET /wapi/zpCommon/data/city.json` → cityList 省→市→区树 + hotCityList
- `GET /wapi/zpgeek/search/job/hot/city.json` → hotCityList

`FILTER_API_DOWN` 置真时这几个接口一律回失败码，用来验证客户端的写死表兜底。

`MOCK_CONDITIONS` 与线上同 code / 同文案（实测抄下来的），别手写凑数。

### `_captcha_gettype`

照实测形状回一个极验挑战（startCaptcha 是字符串化的 JSON）。两族下发**不同的 gt**：业务请求要用 passport 族的那张票据。

passport 族：gt 用 `mock-pass-gt-` 前缀，且带真 randKey。verify.html 页那族（secureflow）：另一个 gt，randKey 为 null（线上实测形状）。

### `_captcha_validate`

收下票据并**烧掉**它。缺票据头就按线上话术拒掉。

为什么烧：极验的 geetest_validate 是一次性凭证。真机上「先 validate 再带同一张票去重试业务请求」会再次 400061——票据在 validate 那步就用掉了。假服务端跟着这个行为，免得把死路测成绿的。

### `_jiyan_ticket`

取出极验票据的 validate 值；三个键不齐就不算有票据。键名照抄登录页 chunk `user-login.js` 的 `1==verifyType` 分支，就 `challenge` / `validate` / `seccode` 三个。`verifyToken` / `captchaToken` 是客户端早期猜的键名，真机不认——这里也不认，免得测试把错的契约背书成绿的。`Zp-Captcha-*` 请求头同理，那是 zpsecureflow validate 的形状，业务接口不看。

### `_has_slider_ticket`

这个请求自己有没有带一张**没被烧掉**的极验票据。

不能只看 validate 种的 Cookie —— 线上实测那样不够。也不能认已烧掉的票：validate 那步已经把一次性凭证用掉了。

业务请求（smsCodeV2 / phoneV2）**不烧**票据：登录页的 `verifyInfo` 跨发码与登录共用，`Ce(!0)` 和 `Ce()` 把**同一张**票挂到两个请求上，`clearVerify` 只在登录结束时调。所以假服务端也不在这里作废它——只有 `zpsecureflow/captcha/validate` 那条链会烧（真机实测过）。

### `_phone_of`

出发码请求的手机号。V2 表单里没有 `phone`——登录页的 `je()` 中间件把它换成了 `encryptedAccount`。这里解回来才能记账；解不动就退回明文 `phone`。

### `_send_sms_code`

真实站点实测形状：码在 400xxx 网关空间，zpData 不含 seed/ts/name。

过掉滑块的条件照着登录页建模，三件事缺一不可：

1. 打的是 **V2** —— B[1].smsCode = send/smsCodeV2；V1 那层根本不读票据。
2. **这个请求自己**带极验三件套 challenge/validate/seccode。请求头 Zp-Captcha-* 不算（那是 zpsecureflow 的形状）。validate 顺手种的 Cookie 也不算（线上实测过）。
3. 票据没被烧掉 —— validate 收下就作废，「先 validate 再带同一张票重试」也会 400061。真机正是这样。

对照组 `PLAIN_REJECT_PHONE`：话术里带「验证码」但属于普通失败，不该被判成风控。

发码响应的 `zpData.token` 是 **smsToken**（短信会话票据），不是登录态。登录页把它存进 smsToken，登录前置的 getLoginSuggest 要拿它对账。

### `_login_suggest`

登录前置检查。照登录页的 `Re` 建模：失败也不该阻断登录。请求体 `{token: smsToken, encryptedAccount, code, regionCode}`。这里只做记录 + 轻校验（smsToken 对不对得上），不发登录态。

### `_login`

滑块放行条件与发码同一套：V2 + 极验三件套 + 没被 validate 烧掉。登录页 `Ce()` 会把 verifyInfo 原样挂到 login 上，所以两张请求共用一张票。

验证通过：验证码一次性失效，首次即注册。

真实站点的登录响应体里**没有**会话 token——登录页的成功处理（m.F）只读路由字段（identity / isCompletion / toUrl …），鉴权完全靠 Set-Cookie。这里照办：body 只放路由与用户字段，凭证只在 Cookie 里。早期假服务端把 token 塞进 zpData，等于把「响应体里有 token」这条死路测成了绿的——真机上根本抠不出来，登录就卡在「没拿到凭证」。

### `_filter_data`

按路径回筛选项，形状与真实 wapi 一致。`conditions.json` 三兄弟共用一份（recommend 没有 stageList，测试不依赖它）。

### `do_GET` / `do_POST` 路由分发

- 业务接口同族 `getTypeV2`：gt 用 `mock-pass-gt-` 前缀，且带真 randKey。
- verify.html 页那族 `gettype`：另一个 gt，randKey 为 null（线上实测形状）。
- 线上实测 404 —— passport 族根本没有 `captcha/validate` 路由。
- V1 `smsCode`：**不认**滑块票据。带着票据来照样 400061。
- V2 `smsCodeV2`：认极验三件套 challenge/validate/seccode（user-login.js 的 Ce）。
- `phoneV2`：极验通道 B[1].sms；票据规则跟 smsCodeV2 同一套。

### 路径说明

`python tools/mock_server.py` 时 sys.path[0] 是 tools/，import 不到 `boss_login`。测试里是 `from tools.mock_server import …`，那时项目根已经在路上。两边都要能跑。
