# `boss_jobs.config` 协议笔记

> 摘自 `boss_jobs/config.py` 的模块文档字符串 / `#:` 注释。

## 一句话

接口路由、业务码、安全网关令牌与聊天通道常量背后的实测结论（含 2026-10-13 路由下线记录）。

## 细节

### 列表路由

在用的列表路由是两条：`GET /wapi/zpgeek/search/joblist.json`（关键词搜索流）和 `GET /wapi/zpgeek/pc/recommend/job/list.json`（求职期望流）。每页固定 15 条（pageSize 传了也不认），page=1 起步，page=0 回空列表，翻到头那页不足 15 条且 `hasMore=false`。

- 两条都站在安全网关后面，缺 `__zp_stoken__` 就回 code 37「浏览器环境异常」；令牌按 `boss_jobs.stoken` 自动算
- `job/detail.json`（JD 正文）同样要登录 + `__zp_stoken__`
- `friend/add.json` **只建会话、不投递正文**（body 塞 `greeting` 会被忽略），真的招呼语走聊天通道 `boss_jobs.chat`

**2026-10-13 移除**：`pc/special/zone/joblist.json`（推荐页，无筛选）那条流已下线——实测一律回 code 7「当前登录状态已失效」，且推荐 tab 的场景由期望流的 `kind=recommend` 承担。采集来源现在是二选一：搜索 / 期望。

### `GEEK_JOBS_REFERER`

求职页（`/web/geek/jobs`）的 Referer。接口不校验它，带上更像浏览器。**所有**请求的默认 Referer（见 `DEFAULT_HEADERS`），不是某条流专属。

### `ENDPOINTS`

- `job_search`：职位搜索列表（GET）。要登录 **且** 要 `__zp_stoken__`（缺了回 code 37）。令牌由 `boss_jobs.cdp_stoken.CdpStokenProvider` 全自动补（拉 Chrome 让站点自己算），见 `boss_jobs.client.JobClient.fetch_search_page`。
- `job_expect_flow`：求职期望流列表（GET，`recommend/job/list.json`）——推荐 / 兼职 / 指定期望三个 tab 共用的**第二条检索路径**，差异只在参数形态（见 `boss_filter.expect.JobExpectFilter`）。要登录 **且** 要 `__zp_stoken__`（缺了回 code 37），走 `boss_jobs.client.JobClient.fetch_expect_page` 的 stoken 重试。**实测（2026-10-13）**：GET 回 code 0 / 15 条 / `hasMore=true`；POST 回 code 10「未知请求方法」——**只走 GET**，别照前端 `hc` 封装切 POST form。
- `job_expect_list`：当前账号的求职期望列表（GET，`recommend/expect/list.json`）——元数据，不返回职位。前端绑定 `ZV`（GET）。**实测（2026-10-13）**：把 `__zp_stoken__` 从 cookie 里摘掉也回 code 0，**只要登录 Cookie**，裸 `JobClient._get_json` 就够，不用 stoken 重试。响应字段名不对称：`expectList[].encryptId`（数字 id 叫 `id`）/ `partTimeExpectList[].encryptExpectId`（数字 id 叫 `expectId`）/ `mixExpect.encryptExpectId`。**实测（2026-10-13，本账号配好期望后）**前两类的不对称是实锤；`mixExpect` 仍是 `null`，那一侧没验到，解析侧双键名都认。
- `job_detail`：职位详情（JD 正文）。要登录 **且** 要 `__zp_stoken__`（缺了回 code 37）；query 带 `securityId` + `lid` 两参就够。正文在 `zpData.jobInfo.postDescription`。
- `friend_add`：打招呼 / 加好友（POST，form）。query 带 `securityId` + `jobId` + `lid`；body 是 `application/x-www-form-urlencoded`，带 `encryptBossId` / `sessionId` 等。**只建会话、不投递正文**：body 里塞 `greeting` 会被服务端忽略——真的招呼语走聊天通道 `boss_jobs.chat`。
- `get_wt`：MQTT over WSS 的接入凭据（GET）。回 `zpData.wt2`，当 MQTT 密码用。
- `get_user_info`：当前登录用户（GET）。取 `zpData.token`（MQTT 用户名前缀）+ `userId`。
- `get_boss_data`：会话对象信息（GET，query `bossId={encryptBossId}`）。回 `zpData.data`，里面有 **boss 的数字 uid**（`bossId`）与 `bossSource`——这俩正是发聊天消息要的 `to`。**要先 `friend/add` 建了会话才查得到**（没会话回 code 1「聊天的Boss不存在」/「非好友关系」）。
- `chat_history`：历史消息（GET，query `bossId={数字 uid}`）。**当不了送达判据**：对这条账号回 `code 0` + 空 `zpData`，有消息的会话也读不出来。留着只当探针。
- `chatremind_log`：「开聊提醒」弹窗的埋点（POST，form）。站点在弹窗**弹出时**打 `action=addf-limit-popup-c` + 弹窗 `ba`，点「好」之后再打 `action=addf-limit-popup-connect` + `ba` + `p8=11`。模拟点击确认时把这两发补齐，跟真浏览器一致。**要带 `zp_token` 头**（cookie `bst`），缺了回 code 121「请求不合法」。

### `CODE_SESSION_EXPIRED`

业务码「未登录 / 登录态失效」。**只有 7 是实测确认的**（Cookie 过期时回 `{"code":7,"message":"当前登录状态已失效"}`，与 `boss_filter.config` 同）。**code 1 是业务失败的通用码**，别一律当登录失效——「聊天的Boss不存在」「非好友关系」「开聊提醒」（每日沟通配额，见 `boss_jobs.errors.JobApiError.is_chat_remind`）都走它。1 只在话术明说了登录问题时才算，判据见 `boss_jobs.errors.JobApiError.is_session_expired`。

### `CODE_BROWSER_CHECK`

业务码「浏览器环境异常」——缺 `__zp_stoken__` 安全网关令牌。服务端会顺手在 `zpData` 里下发一次性挑战 `{seed,name,ts}`，拿挑战去跑 `/web/common/security-js/{name}.js` 的 `ABC.z` 即可算出令牌，来源与算法见 `boss_jobs.stoken`。

### `CODE_RISK_CONTROL`

业务码「您的账户存在异常行为」——账号维度风控，要人机验证（GeeTest）。不是缺令牌，客户端不去绕。

### `STOKEN_COOKIE`

安全网关令牌的 Cookie 名。**不是服务端发的**，是浏览器按 `/web/common/security-js/{name}.js` 的 `ABC.z(seed, ts)` 算出来再回传的，里面还编了浏览器环境指纹。取它最稳的路子是拉真 Chrome 让站点自己写（`boss_jobs.cdp_stoken`，`fetch` 默认走这条）；`boss_jobs.stoken` 那条 Node 硬算只用来验证算法，服务端不认它的指纹。

### `ZP_TOKEN_COOKIE` / `ZP_TOKEN_HEADER`

站点 axios 拦截器往**每个**请求上贴的 `zp_token` 头，取自 cookie `bst`。`friend/add` 这条不校验它，但「开聊提醒」确认侧的 `actionLog/geek/chatremind.json` / `friend/continuechat.json` **缺了就回 code 121「请求不合法」**——带上才谈得上模拟点击确认。

### `CHAT_REMIND_CONFIRM_CID`

「开聊提醒」弹窗点「好」时前端回传的确认位（chunk `1326.ad80b1c8.js` 的 `commonAction`：`k(job, {url: webUrl, cid: 1})`）。`friend/add` 带上它就从 code 1 弹窗变成 code 0 建会话——这正是「模拟点击确认」要复现的那一步。2026-10-09 实测：不带 `cid` 一直弹窗，带上 `cid=1` 直接 `{"code":0,"message":"Success"}`。

### 聊天通道常量

聊天通道（MQTT over WebSocket）。站点**没有**发聊天消息的 HTTP 接口——消息是 MQTT 上的 protobuf 帧（Paho，topic `chat`，userName `<token>|0`）。全链路协议与三个坑（握手要登录 Cookie；`from` 要带 `source`、`mid` 要落在服务端雪花号数轴上；网关对文本帧**不回 PUBACK**，发完约 150ms 就掐线）见 `boss_jobs.chat`。

- `CHAT_WS_HOST`：聊天 MQTT 网关（WebSocket Secure）。
- `CHAT_WS_PATH`：Paho 客户端构造里的 path（第三个参数）。
- `CHAT_TOPIC`：消息统一发到这一个主题，收件人在 protobuf 的 `to` 里。
- `CHAT_KEEPALIVE`：MQTT 心跳（秒），对齐站点前端的 `keepAliveInterval: 25`。
- `CHAT_TIMEOUT`：等 CONNACK 的超时（秒）。
- `CHAT_PUSH_WAIT`：连上后等多久去收服务端主动推的那帧**会话同步**（秒）。它带每个会话最后一条消息的 id，是本地算 `mid` 的唯一现成基数。**只在手里没基数时才等**（整批第一条）——基数能跨条带过来（`ChatSocket` 的 `mid_base`），有了就直接发。等不到就退回 `CHAT_MID_FLOOR`。
- `CHAT_MID_FLOOR`：`mid` 基数兜底值。**服务端的消息 id 是 3.9e14 量级的雪花号**（不是毫秒时间戳），发出的帧 `mid` 必须落在这个数轴上、且大于对方会话已有的 id，否则网关判非法直接掐线。站点自己算的是 `getMaxMsgId() + Date.now()`；正常路径下这个兜底值会被会话同步里的真实 id 顶上去。
- `CHAT_MID_RANGE`：从推送里认「这是个消息 id」的合理区间，滤掉别的数字字段，免得把 `mid` 抬到离谱的地方去。
- `CHAT_PUBACK_WAIT`：等 PUBACK 的超时（秒）——**只记日志，不是判据**。这条网关对文本帧**不回 PUBACK**，发完约 150ms 直接关 WebSocket（`close code=1000 reason="Bye"`），但那几发是真的进服务端了。回执从来不到，所以默认 0 = 发完不等。
- `CHAT_FLUSH_WAIT`：发完再停多久才主动断（秒）。PUBACK 等不到，这停顿只留给「网关还没来得及掐线」——它常态约 150ms 就关连接，停 0.15s 就够。
- `CHAT_FLUSH_DEADLINE`：把 PUBLISH **真正写到 socket 上**最多等多久（秒）。`publish()` 回 `rc == 0` 只是**入队成功**，真写出去是 paho 的 loop 线程干的；没写完就断连，这帧随连接一起丢，而这条网关不回 PUBACK，只会表现成「会话建了、招呼语没了」。发完等它出队，这个数只是硬上限。
- `CHAT_RETAIN`：正文帧的 MQTT `retain`。**定案 `False`**：站点前端用 `true`，但那会让 broker 把帧留在 `chat` 主题上，收件人（重新）订阅/同步时再收一遍留存的那份，一条招呼语变两条。站点自己不出这毛病是因为它有 `pendingDeliverMap` 按 `cmid` 去重，我们没有。`false` = 只走实时投递。**presence 帧仍用 `true`**（不是消息，不参与去重）。
- `CHAT_RECONNECT_MIN` / `CHAT_RECONNECT_MAX`：断线重连间隔（秒）。**故意开得很大**：一帧一条连接（发完主动断），重连由 `ChatSocket` 显式重建，不让 paho 在后台按秒级节奏自己重连（会变成连环握手，更快被网关踢掉）。
- `CHAT_DEFAULT_SOURCE`：发消息时塞进 `from`/`to` 的 `TechwolfUser.source`，站点前端按会话对象的 `friendSource` 填（老板侧实测回 `0`）。
- `CHAT_BODY_TEXT`：文本消息的 `body.type` / `templateId`（站点 `createMessage.text` 里写死 1）。
- `CHAT_PROTO_MESSAGE` / `CHAT_PROTO_PRESENCE`：`TechwolfChatProtocol.type`：1 普通消息 / 2 presence。
- `CHAT_CLIENT_VERSION` / `CHAT_APP_ID`：`TechwolfClientInfo` 里报的版本号与 appid，对齐站点（4.92 / 9019 / web）。
- `CHAT_PRESENCE_ONLINE`：presence 帧的 `type`（1 = 上线）。

### 安全网关退避

- `BROWSER_CHECK_BACKOFF`：撞上安全网关 code 37 时先歇多久再拿同一枚令牌重试（秒）。37 有时只是「请求太快」，先退避；歇完还 37 才轮到强制换新（换新自己有 `RENEW_COOLDOWN` 冷却，不会连环拉 Chrome）。
- `BROWSER_CHECK_COOLOFF`：撞上 code 37 之后、下一条之前再多歇多久（秒）。37 有时是「令牌不对」，有时是**整段 IP / 会话被限速**——后者多打一发只会撞得更狠。
- `BROWSER_CHECK_GIVEUP`：**连续**撞上 code 37 几次就停整批。安全网关的限速墙是「一小段窗口里放行几发，然后整段拦」，撞墙后继续砸只会把窗口越压越久；连环 3 次 = 这轮大概率全线拦了，停批歇几分钟比拿几十发请求探墙厚道，也不容易把账号风控惹出来。

### `FONT_DIGIT_ENTITIES` / `FONT_DIGIT_CHARS`

列表页 HTML 里薪资是私有区字符（防爬字体），下标即数字 0-9。**JSON 接口回的 `salaryDesc` 已是明文**（"8-15K"），这表留给 HTML 兜底解析和历史脏数据用。字符形式为 U+E031…U+E03A。
