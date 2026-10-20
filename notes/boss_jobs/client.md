# `boss_jobs.client` 协议笔记

> 摘自 `boss_jobs/client.py` 的模块文档字符串 / 文档字符串 / `#` 注释。

## 一句话

职位列表的线上接口客户端：真实 wapi 路径 + 登录 Cookie + 每页即时清洗入库。

## 细节

### 职位列表要登录

调的是真实 wapi 路径（见 `boss_jobs.config`），不是界面模拟。
登录态复用 `boss_login` 落盘的 Cookie（状态库 `doc('session')`）——
职位列表**要登录**，Cookie 过期时接口回 code 7。

核心是 `JobClient.crawl`：**抓一页 → 立刻清洗 → 立刻入库 → 睡够间隔
→ 再要下一页**。清洗和入库从不攒批，睡眠是硬间隔，用来压请求频率、
避开风控；中途挂掉时已入库的页不丢。

### `__zp_stoken__` 取值顺序

1. 显式传进来的 `stoken`
2. `doc('session')` 的 `cookies.__zp_stoken__`
3. 环境变量 `config.STOKEN_ENV`（`BOSS_ZP_STOKEN`）

都没有就**先不带**——搜索类接口会回 code 37，由
`create_client` 挂上的 `CdpStokenProvider` 拉 Chrome（CDP）让站点自己算一枚补上。

写 `__zp_stoken__` 时走 `boss_jobs.stoken.put_cookie`：它会把上面
`cookies.set` 不带 domain 写进去的同名旧 Cookie 清掉，否则请求头里会同时
出现两枚，服务端照旧的那枚拒。

### 两条抓取流（2026-10-13）

**两条流二选一，优先级 expect > search**（调用方保证互斥，这里再兜一层）：

1. `expect_filter` 非空 → 期望流 `recommend/job/list.json`
2. 否则 `search_filter` 非空 → 搜索流 `search/joblist.json`
3. 两个都不给 → 抛 `ValueError`

推荐页 `special/zone` 那条流 **2026-10-13 已移除**（实测回 code 7，
推荐 tab 场景改由期望流 `kind=recommend` 承担），不再有「无筛选抓全站」。

分页 / 清洗 / 入库 / 黑名单逻辑两条流**全部复用**。

收手判据：接口回空页，或「**不满页**且 `hasMore=false`」。
`hasMore` 在 page=1 上不可靠（常回 `false` 却还有后续页），所以不单独
信它。下一页若已被 `max_pages` 拦下，也不再多等那一个页间隔。

`_enrich_page_details`：The list endpoint is not authoritative for JD or blacklist state.

Preserve the established ordinary crawl contract: persist the list page first,
then enrich its already-stored rows.

### 期望列表 / 期望页（实测 2026-10-13）

`fetch_expect_list` 打 `/wapi/zpgeek/pc/recommend/expect/list.json`（元数据接口，
不返回职位）。前端是 GET（`ZV`）。**实测（2026-10-13）**：把
`__zp_stoken__` 从 cookie 里摘掉也回 code 0——**只要登录 Cookie**，
所以这里刻意走裸 `_get_json`，不换 `_get_json_with_stoken_retry`（换上去反而多拉一次 Chrome）。

响应字段名不对称（`expectList[].encryptId` vs
`partTimeExpectList[].encryptExpectId`），解析见
`boss_jobs.models.clean_expects`。本账号期望列表是空的，
对称性没被真数据定案，解析侧双键名都认。

`fetch_expect_page`（`recommend/job/list.json`）与 `fetch_search_page`
同级：**走 stoken 重试**（这条接口在安全网关后面，缺令牌回 code 37）。
**实测（2026-10-13）**：GET 回 code 0 / 15 条 / `hasMore=true`；
同参数 POST 回 code 10「未知请求方法」。所以这里是 GET，别照前端
`hc` 封装切 POST form。

### `_request_with_stoken_retry` 的 37 重试

37 有两类原因：令牌不对、或者**请求太快 / 被安全网关拦着**。
连环 code 37 时换新**没用**——刚换的令牌一样被拒，
这时正确动作是**别再撞**，而不是多打一发。所以：

- 先退避 2s 拿同一枚重试（「太快了」这一支）；
- 还 37 再 `ensure(force=True)`；**没真换到新令牌**（换新冷却中）
  就不再打第三发，把上一个 37 抛给调用方去停整批。

### `greet` / 「开聊提醒」模拟确认

`POST /wapi/zpgeek/friend/add.json` **只建会话、不投递正文**——body 里带
`greeting` 服务端直接忽略。真正的招呼语正文要走聊天通道（`deliver_greeting`）。

形态：query 带 `securityId` / `jobId` / `lid`，body 走
`x-www-form-urlencoded` 透传 `encryptBossId` / `sessionId`。

**「开聊提醒」会自动模拟点击确认**：`friend/add` 回 `code 1` +
`chatRemindDialog`（blockLevel 0 的提示弹窗，content 像「您今天已与
120 位 BOSS 沟通，还剩 30 次沟通机会哦」）时，照站点前端那条链路
补两发——弹窗埋点 `addf-limit-popup-c`，再带 `cid=1` 重打一次
`friend/add`（chunk `1326.ad80b1c8.js` 的 `commonAction`）。

已明确达到今日上限时，首次响应就是终态；不要再模拟确认，
否则会额外打一发 `friend/add` 并把原始配额文案覆盖掉。

`_confirm_chat_remind`：站点前端（chunk `1326.ad80b1c8.js`）点弹窗上的「好」时干三件事：

1. 弹窗 `created` 时埋点 `POST /wapi/zpCommon/actionLog/geek/chatremind.json`
   body `{action: "addf-limit-popup-c", ba: <弹窗 ba>}`；
2. `commonAction` 里 `remindType > 262144`（实测 524288）且按钮
   `actionType == 11` 时，**带 `cid=1` 再打一次 `friend/add`**
   （这就是「确认」本体——不带 `cid` 服务端一直弹窗）；
3. 再埋点 `addf-limit-popup-connect` + `ba` + `p8=11`。

埋点两发**失败不挡确认**（少打一发埋点不影响建会话）；`friend/add`
那发才是判据。确认后仍弹窗就把原异常抛回去。

### `fetch_boss_data` / `fetch_chat_history`

把 `encryptBossId` 换成**聊天要用的数字 uid**。
打 `GET /wapi/zpchat/geek/getBossData?bossId={encryptBossId}`。
**要先 `greet` 建了会话才查得到**——没会话回 code 1「聊天的Boss不存在」/「非好友关系」。

`fetch_chat_history`（`GET /wapi/zpchat/geek/historyMsg`）：
`boss_uid` 要**对方的数字 uid**，传 `encryptBossId` 回 **code 19「参数值错误」**。
**拿它核对送达是不行的**：对有消息、且刚确认送达的会话，这个接口回
`code 0` + 空 `zpData`。现在只当探针留着，**不作为发送判据**。

### `open_chat` / `deliver_greeting`

一帧一条连接：`ChatSocket.send_text` 发完就主动断（网关自己也约 150ms 就掐线）。
真正要复用的是**凭据**和 **`mid` 基数**：

- 凭据（`getUserInfo` + `get/wt` 两发 HTTP）整批取一次；
- 上一条见过的最大消息 id 带给新连接当 `mid` 基数，第二条起就不用再等那帧会话同步。

`deliver_greeting` 三步：`greet` 建会话 → `fetch_boss_data` 换数字 uid → MQTT 发正文。
第 3 步的**成功判据是「帧发出去了」**（PUBLISH 无异常、`rc == 0`），
**不是「等到 PUBACK」**：这条网关对文本帧根本不回 PUBACK，发完约 150ms
直接把连接关掉——**这是它的常态，不是拒收**。

`to.name` 用会话对象的 `encryptUid`（同源的 `encryptBossId`）：
跟站点一样有值就写、空串就不写这个字段。

聊天 WebSocket 握手**必须带 Cookie**（不带回直接 HTTP 403）。

### `_request_json`

每发都补 `zp_token` 头（cookie `bst`）——站点 axios 拦截器对**所有**
请求都贴这个；「开聊提醒」确认侧的埋点缺了直接 code 121「请求不合法」。
顶层 message 常常只是个短标签（「开聊提醒」），真话在
`zpData.bizData.chatRemindDialog.content` 那类嵌套里——见 `format_api_message`。

### 黑名单 / JD 评估注释

- 入库并标记：命中要进审计流水（action=stored），后面 save/汇总都用本轮结果
- 不补 JD 时也要落完整安全快照：未查完的规则/缺公司名保守拦自动投递。
  纯名称规则不依赖正文，这里就地查；需要正文的规则只能标「未检查」。
- 公司命中可能已先落过快照，这里合并而不是覆盖
- `Job.from_api` 的默认值是 pending；有规则却查不了时改记 unchecked
- UPSERT 故意不覆盖安全列；这里按本轮评估显式回写，重抓才能刷新旧状态
- 本轮规则结果覆盖旧 jd_keyword 快照：规则删了/不再命中要清掉
- Persist immediately for the legacy save-before-enrich path.
  For a blacklist crawl the candidate may not exist yet; its
  complete state is written by the later filtered save_page.
- Mark remaining candidates as incomplete without more requests.
- 无 JD 规则 → 无需检查；有规则时：有正文可全查，或纯名称规则不需要正文
- 公司命中只认本轮规则结果，不沿用库里旧快照（规则删了要能翻案）
- JD 命中同理：本轮有 JD 规则才认快照里的 jd_keyword，否则清掉陈旧命中
- 公司名缺失无法确认安全，保守拦自动投递（不算命中）

### `CrawlBlacklistContext`

Immutable rule view supplied by the web crawl task.
The jobs client deliberately knows nothing about the web application's rule
tables. Callbacks return serialisable match snapshots; the client owns the
ordering of company-before-detail and JD-after-detail decisions.

`jd_scopes`：JD 规则的匹配范围（`job_name` / `job_desc` / `both`）。
旧调用方可省略，此时按 `both` 处理（需要 JD 正文才能算检查完成）。
