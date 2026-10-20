# `boss_jobs.errors` 协议笔记

> 摘自 `boss_jobs/errors.py` 的模块文档字符串 / 文档字符串。

## 一句话

「开聊提醒」每日沟通配额弹窗的包体形状、码位语义、以及与登录失效 / 风控的区分（2026-10-09 实测）。

## 细节

### `dig_chat_remind`

「开聊提醒」那类**每日沟通配额**弹窗就长这样（2026-10-09 实测 `friend/add.json`）：

```json
{"code":1,"message":"开聊提醒",
 "zpData":{"bizCode":1,"bizMessage":"开聊提醒",
           "bizData":{"chatRemindDialog":{"title":"温馨提示",
               "content":"您今天已与120位BOSS沟通，还剩30次沟通机会哦",
               "remindType":524288,"blockLevel":0, …}}}}
```

顶层 `message` 只是「开聊提醒」这个短标签，**真话在 dialog.content 里**。

### `format_api_message`

站点的顶层 `message` 常常只是个短标签（如「开聊提醒」）；有 `dig_chat_remind` 那种弹窗时，真话在 `content` 里，优先用它。

### `chat_remind_remaining`

实测话术长这样：「您今天已与120位BOSS沟通，还剩30次沟通机会哦」——**30 是还能主动沟通的次数**，不是「今天已经没了」，所以 > 0 时发送任务该继续用掉它，别一见弹窗就整批停手。

### `JobApiError.is_session_expired`

**code 1 不算**——它是业务失败的通用码，「聊天的Boss不存在」「非好友关系」「开聊提醒」（每日沟通配额）都走它；一律当登录失效会把人支去重新登录、登录了也还是发不出去（2026-10-09 实测踩过）。只有 code 7（实测的「当前登录状态已失效」）或话术里明说了登录问题才算。

### `JobApiError.is_chat_remind`

实测（2026-10-09）：`friend/add.json` 回 `code 1` + `chatRemindDialog`，content 是「您今天已与120位BOSS沟通，还剩30次沟通机会哦」，`blockLevel: 0`。**这是提示弹窗，不是硬拦**：

- 不是登录失效（别叫人去重新登录）；
- 不是账号风控（code 36）；
- 「还剩30次」= 今天**还能再主动沟通 30 次**，不是已经没了。

站点前端点弹窗上的「好」之后会带 `cid=1` 再打一次 `friend/add`（模拟点击确认，见 `boss_jobs.client.JobClient.greet`），带对了就能把剩下的次数用掉。

### `JobApiError.is_browser_check`

撞上安全网关（code 37「浏览器环境异常」）时缺的是 `__zp_stoken__` 安全网关令牌。令牌的来源与算法见 `boss_jobs.stoken`——本项目按站点前端那条链路**自动算**，算完重试即可，不用人工回浏览器。

### `ChatSendError`

聊天通道（MQTT over WebSocket）没连上 / 没发出去。跟 `JobApiError` 分开，是因为它**没有业务码**：失败在传输层（握手被拒、CONNACK 非成功、PUBLISH 直接抛 / `rc != 0`），重试策略也不一样——见 `boss_jobs.chat`。上层（发送任务）把它当「这条发送失败」记流水，**不重试**（避免重复打扰招聘方）。

⚠️ **没等到 PUBACK 不算失败**：这条网关对文本帧不回 PUBACK，发完就把连接关掉是常态（见 `boss_jobs.chat` 模块头）。本异常只在**握手/连接没成、或 PUBLISH 本身报错**时抛。
