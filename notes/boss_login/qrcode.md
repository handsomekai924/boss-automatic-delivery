# `boss_login.qrcode` 协议笔记

> 摘自 `boss_login/qrcode.py` 的模块文档字符串 / `#` 注释。

## 一句话

APP 扫码登录 —— 复刻登录页 ``BossAppScan`` 的完整流程。

## 细节

来源是按需 chunk ``user-login.5398946e.js``（zhipin-sign/v5330）里的
``BossAppScan`` 类和 ``SignLoginScanApp`` 组件，接口形状 2026-10-13 打过真实
站点核对。整条链是这样的::

    POST /wapi/zppassport/captcha/randkey
         └─ zpData.qrId = uuid          ← 二维码的内容就是这串 uuid
    ┌──────────────────────────────────────────────────────────┐
    │  页面上画二维码（content = secondUuid || uuid）            │
    │  二维码 30 秒后整段刷新（expireTime=3e4）                  │
    └──────────────────────────────────────────────────────────┘
    GET  /wapi/zppassport/qrcode/scan?uuid=          长轮询，等手机扫
         └─ {scaned:true, validate, allweb}
    GET  /wapi/zppassport/captcha/getSecondKey?uuid=
         └─ zpData.qrId = secondUuid    ← 换发第二把钥匙
    ┌── 1 秒兜底定时器 与  scanSecond 长轮询 赛跑 ──────────────┐
    │ GET /wapi/zppassport/qrcode/scanSecond?uuid=secondUuid    │
    │      └─ {scaned:true}  = App 端已确认                     │
    └──────────────────────────────────────────────────────────┘
    GET  /wapi/zppassport/qrcode/getHeadImg?qrId=    拿扫码账号的头像
         └─ zpData: {large, wxMp}
    GET  /wapi/zppassport/qrcode/scanLogin?qrId=     长轮询，等确认/取消
         └─ {scaned, login}   login=true 确认 / false 取消
    GET  /wapi/zppassport/qrcode/dispatcher?qrId=&pk=&fp=
         └─ code=0 + Set-Cookie         ← 登录态就在这一发上

几个跟短信登录不一样的点：

* **鉴权只靠 Set-Cookie**，``dispatcher`` 的响应体里照样没有会话 token，
  成功处理只读 ``zpData`` 的路由字段（``identity`` / ``toUrl`` / …）。
* ``scan`` / ``scanSecond`` / ``scanLogin`` 三个是**长轮询**，单次要挂到 100 秒
  （登录页 axios ``timeout: 1e5``）。超时是「还没轮到」，不是失败。
* ``scanLogin`` 在**还没被扫**时会立刻回 ``{scaned:true, login:false}``；
  真正进入「等 App 确认」状态后才会挂住。所以这一发只在 ``scan`` 说扫过之后
  才打——照登录页的调用顺序来，别提前问。
* ``fp``（浏览器指纹）那步在页面上是 try/catch，取不到就不带。这里同理，
  默认不带；真要带就用 `boss_login.crypto.encrypt_text` 加密后传进来。

### `QrTicket`

`qr_id`：二维码内容。``getSecondKey`` 之前是 ``qrId``，之后换成 ``second_qr_id``
（登录页 ``qrCodeContent = secondUuid || uuid``）。

`expires_at`：本地展示层的过期时间，跟服务端票据寿命是两回事。

### `AppScanLogin.create`

页面那头是 ``refreshQRcode() → getRandomKey()``：请求体是**空的**——
``Ie`` 的构造器压根没把 ``pk`` 塞进去，别自作主张加上。

### `AppScanLogin.fetch_second_key`

未扫码时服务端回 ``zpData:{code:1092}``（外层 ``code`` 仍是 0），
所以要往 ``zpData`` 里再看一眼。拿到的 ``qrId`` 就是 ``secondUuid``，
页面上二维码的内容也会跟着换成它（虽然那时 UI 已经不露二维码了）。

### `AppScanLogin.fetch_head_img`

外层 ``code`` 是 0 不代表有头像——``zpData`` 里还会再套一层
``{code, msg}``，只有 ``zpData.large`` 才是真的图。

### `AppScanLogin.wait_scanned`

服务端没轮到会挂住；我们这边到点就当成还没扫，由调用方决定刷新还是继续。
页面上这张的展示寿命是 30 秒。

跟登录页一致：单次长轮询挂满 poll_timeout，整体预算在轮次之间卡。
不把剩余预算折进单次超时——浏览器那头也是整发挂满的。
``{scaned:false, msg:"timeout"}`` = 这一发没轮到，继续下一发。

### `AppScanLogin.wait_confirmed`

**调用时机很重要**：``scanLogin`` 在二维码还没被扫时会立刻回
``{scaned:true, login:false}``（等同「取消」）。登录页只在 ``scan``
说扫过、并且等过那 1 秒兜底之后才打这一发，这里保持一致。
登录页在未 scaned 一支会继续递归轮询；少见，但别当成失败。

### `AppScanLogin.dispatch`

表单形状跟登录页 ``scanJump()`` 对齐::

    {…extraForm, qrId: uuid, pk: …}
    + environment=BossZhipinBridge   （仅 App 内嵌 WebView）
    + fp=encrypt_text(fingerprint)   （可选，页面上是 try/catch）

``Set-Cookie`` 就在这一发的响应头上；``zpData`` 里是路由字段
（``identity`` / ``toUrl`` / ``pcToUrl`` / …），登录页的成功处理
只拿它们做跳转，不读 token。

### `AppScanLogin.run`

流程与登录页 ``BossAppScan`` 一一对应，顺序别调：

1. ``create`` 取票据，把二维码内容交出去；
2. ``wait_scanned`` 长轮询到手机扫了；
3. ``fetch_second_key`` 换第二把钥匙（内容变了，再交一次）；
4. 等 1 秒兜底（``QR_CONFIRM_DELAY_SECONDS``），或 ``scanSecond``
   先回 ``scaned``——两条路都是 ``getHeadImg`` + ``scanLogin``；
5. ``scanLogin`` 回 ``login``：确认则 ``dispatcher``，取消则抛错；
6. ``dispatcher`` 换到 ``Set-Cookie`` 就算登录成功。

### `AppScanLogin._wait_confirm_window`

复刻页面上「1 秒兜底定时器 与 scanSecond 赛跑」那一段。

页面在 ``getSecondKey`` 之后起了个 1 秒定时器，到点就开始 ``scanLogin``；
同时又打 ``scanSecond`` 长轮询，谁先到听谁的（``lock`` 防重入）。

这里压成一发 ``scanSecond``，超时给到 1 秒（= 那个兜底定时器）：
服务端在 App 确认前会挂住，所以正常就是「挂到 App 确认」或「1 秒超时」
两种结果；真回了 ``{scaned:false}`` 也不重试——兜底时间已经到了，
接下来 ``scanLogin`` 自己会挂着等确认，不会漏。

### `AppScanLogin._raise_for_qr`

这一族接口的错误码跟短信登录不是一套（``1092`` = 还没扫、
``4563``/``4942-4950`` = 登录限额），所以**不**走
``_handle_response``——那张表是给发码/登录用的。

### `AppScanLogin.build_result`

跟短信那条一样：响应体里没有 token，凭证全在 ``Set-Cookie`` 上。
拿不到 Cookie 就抛 `LoginIncomplete`，把实到的形状打出来。
登录页把 scan 回执的 validate/allweb 透传给成功处理，这里也留着。
