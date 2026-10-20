# `boss_web.services.deliver_task` 协议笔记

> 摘自 `boss_web/services/deliver_task.py` 的模块文档字符串 / `#` 注释。

## 一句话

一键投递任务：把匹配结果里的目标岗位串行打招呼并把招呼语发出去。

## 细节

一键 / 多条 / 单条三种入口只差目标集合，任务本身只认一串 `encrypt_job_id`
（todo.md C2）。规矩照 todo.md C6 / C7 写死，别在界面层再实现一遍：

- 确认弹窗是前端的事（C7），这里只收已确认的清单；
- 串行 + `boss_jobs.config.DELIVER_INTERVAL` 节流，可随时取消；
- `delivered_at` 有值跳过不重发；单条失败只记 `deliver_error`、不自动重试
  （失败条目可人工点行内「发送」再试）；
- 撞 **code 36**（账号异常 / 人机验证）**立即停整批**，继续砸只会更糟；
- 撞 **「开聊提醒」**（每日沟通配额提示弹窗，`friend/add` 回 code 1 +
  chatRemindDialog）**不是停批信号**：这是 blockLevel 0 的提示弹窗，话术
  「您今天已与120位BOSS沟通，还剩30次沟通机会哦」里那 30 是**还能再发的次数**。
  `boss_jobs.client.JobClient.greet` 已经做过「模拟点击确认」
  （埋点 + `cid=1` 重打 `friend/add`）；确认完还被拦才轮到这儿——
  只记这一条、**继续发剩下的**，除非话术明说「还剩 0 次」。
  **不是登录失效**，别叫人去重新登录（2026-10-09 实测踩过：code 1 被当
  登录态失效，人重新登录了照样发不出去）；
- 结果写回 `analysis` 的 `deliver_status` / `delivered_at` / `deliver_error`。

发送是「建会话 + 真投递正文」两步：`boss_jobs.client.JobClient.deliver_greeting`
先打 `friend/add.json`，再经 MQTT（`boss_jobs.chat`）发正文——站点
`friend/add` **只建会话、不投递任何文本**。成功判据是「这帧发出去了」
（`ChatSocket.send_text` PUBLISH 无异常且 `rc == 0`）；**没有回读接口可验**，
也别指望 PUBACK（网关对文本帧不回，发完约 150ms 关 WebSocket 是常态）。
细节见 `boss_jobs.chat`。

### 30 天发送冷却

30 天发送冷却窗（秒）。**写死常量**，不进配置——以后要调再进 `deliver_config`。
判定：`可发送 ⇔ now - last_greeted_at(job) ≥ 30 天`；恰好 30 天整 → 允许再发。

`last_greeted_at`（岗位维度）与 `delivered_at`（本条分析的即时状态）**取较晚者**，
再套 30 天窗——兼容上线前已发出的岗位（只有 `delivered_at`）与上线后的新发送。

读接口出门前给 match 条目补**当前**发送史与冷却状态（就地改）。
分析快照里的 `last_greeted_at` 是匹配那一刻的值；真正算冷却要看
`jobs` 表现在的 `last_greeted_at` 与 `item.delivered_at` 取较晚者。
读不到就按「从未发送」放行。

`parse_greet_time`：数字（含数字字符串）= Unix 时间戳；`%Y-%m-%d %H:%M:%S` 等
ISO 风格按服务器本地时钟解析，**不做时区换算**；读不到 → 按「从未发送」放行
（宁可多一次确认弹窗，不静默吞掉可发对象）。

### `_delivery_restriction` / `_all_restriction`

Any safety field marks a current-format match snapshot. 必须字段齐全才算有安全快照；
缺字段的旧分析不能进自动投递。

Legacy analyses can still be explicitly sent by a user-selected request for
backwards compatibility, but they are never eligible for automatic/all send.

### `DeliverTaskManager`

`client_factory` 造 `JobClient` 的工厂（测试里注入假的）；
`sleeper` 可替换的 sleep（测试里注入 no-op，免得真等一秒）。

串行发送：已成功的跳过；单条失败记 `deliver_error` 继续，不重试。
空招呼语不发（站点 `friend/add` 只建会话、不打字，空消息比不发更糟）；
`ChatSendError` 也不重试（会话可能已建、正文没出，重试会重复打扰）。

发送前统一开库：冷却判定以 `jobs.last_greeted_at` 为准（权威），成功后也写回这里。

双写：岗位维度 `last_greeted_at`（跨分析冷却依据）+ `item.delivered_at`（本次即时状态）。

### `_on_api_error`

分类处理业务错误，回 `stop` / `cooloff` / `continue`。

code 36（账号异常/人机验证）与登录态失效停整批；code 37 走 cooloff。
每日沟通配额明确耗尽时停批；其他「开聊提醒」只在话术明说「还剩 0 次」时才停。
