# `boss_jobs.chat` 协议笔记

> 摘自 `boss_jobs/chat.py` 的模块文档字符串 / 文档字符串 / `#` 注释。

## 一句话

MQTT over WebSocket + 手写 protobuf 帧的聊天通道协议（2026-10-08 实测通过）。

## 细节

### 为什么要有这个模块

`POST /wapi/zpgeek/friend/add.json` 只**建会话**，它不投递任何正文——请求体里带 `greeting` 服务端直接忽略（回 code 0，聊天框还是空的）。站点自己也不靠它发招呼语：消息走 **MQTT**。

逆向结论（全部来自 chat-new 前端 `static/js/app.*.js`，2026-10-08 实测通过）：

| 站点前端 | 本模块对应 |
|---|---|
| `new Paho.MQTT.Client(server, port, "/chatws", uuid)` | `ChatSocket`（paho-mqtt） |
| `connect({userName: token+"\|0", password: wt, ...})` | `ChatSocket.connect` |
| `client.send("chat", frame.toArrayBuffer(), 1, true)` | `ChatSocket.send_text`（**retain 我们传 `False`**，见 `config.CHAT_RETAIN`） |
| `createMessage.text(stanza)` | `encode_text_message` |
| `createMessage.presence(...)` | `encode_presence` |

### 五个必须踩对的点

1. **握手要带登录 Cookie**——不带的 WebSocket 升级请求被网关回 **HTTP 403**。
2. **MQTT 用户名是 `<token>|0`、密码是 `wt`**：`token` 来自 `GET /wapi/zpuser/wap/getUserInfo.json` 的 `zpData.token`，`wt` 来自 `GET /wapi/zppassport/get/wt` 的 `zpData.wt2`。
3. **`to.uid` 是 boss 的数字 uid**，不是 `encryptBossId`。`encryptBossId` 要先换：`GET /wapi/zpchat/geek/getBossData?bossId={encryptBossId}`（见 `boss_jobs.client.JobClient.fetch_boss_data`），而且要**先 `friend/add` 建了会话**才查得到。
4. **`from` 里必须带 `source`**，**`mid` 必须落在服务端的消息 id 数轴上**。
5. **正文帧的 `retain` 要 `false`**（站点前端是 `true`）——留着 `true` 会让一条消息在对方那里**变成两条**，见 `config.CHAT_RETAIN`。

### 第 4 条的活体对照（2026-10-08 深夜）

一开始本模块发的文本帧 `from` 只有 `uid`，`mid` 用的是当前毫秒；结果网关收到**立刻把连接关掉**（网页里 `close code=1000 reason="Bye"`，paho 这边是 `DISCONNECT Unspecified error`），而且**从不回 PUBACK**。当时试遍了 retain / qos / presence 内容 / MQTT 版本 / 各种 cookie，全都没用——因为**是这个帧本身被判非法**，网关的反应就是掐线。

定论来自两次活体对照：

* 挂上 `WebSocket.prototype.send`，让站点自己在聊天页里发一条消息，抓到它线上那帧，和我们同参数的帧逐字节比——**唯一的字段差异就是 `from` 少了 `source:0`**（站点 `createMessage.text` 里 `from` 也走 `user(uid, encryptUid, source)`，proto2 显式写这个字段）。
* 补上 `from.source` 后**还是被掐**。再比才发现第二处差异在 **`mid`**：站点发的是 `ChatWebsocket.getMaxMsgId() + Date.now()` ≈ **3.96e14**（服务端消息 id 是 3.9e14 量级的雪花号），`time` 才是纯 `Date.now()` ≈ 1.79e12。我们的帧把 `mid` 也写成了毫秒时间戳——**比对方会话里已有的消息 id 小了几个数量级**，网关按「id 太旧」毙掉。按站点算式（基数取服务端量级的 id、再加当前毫秒）发出去，消息就进了会话（站点界面显示「[送达]」）。

所以 `mid` 的基数从**连上后服务端推的那帧会话同步**里取：那帧带着每个会话最后一条消息的 id（见 `max_message_id`），取其中最大值当基数，等不到就用 `config.CHAT_MID_FLOOR` 兜底。同一批里后续的帧**把上一条的基数带过来**（`ChatSocket` 的 `mid_base`），不用每条都重新等那帧同步。

### 别拿 PUBACK 当判据

这条网关对文本帧**根本不回 PUBACK**——PUBLISH 完约 150ms 直接把 WebSocket 关掉，`close code=1000 reason="Bye"`，**这是它的常态，不是拒收**。2026-10-08 实测的那几发（站点会话列表里都出现了招呼语并标「[送达]」，重载页面、从服务端重拉也还在）**一发 PUBACK 都没等到**。所以发送成功的判据是「帧发出去了」（PUBLISH 无异常、`rc == 0`），PUBACK 只是顺带看一眼、记进日志，而且默认**整个不等**（回执从来不到，等它纯烧时间，见 `config.CHAT_PUBACK_WAIT`）。

「回读聊天记录」同样当不了判据：`GET /wapi/zpchat/geek/historyMsg` 对这条账号返回 `code 0` + 空 `zpData`，**连着有消息、刚确认送达的会话也读不出来**。

protobuf 那套 `Techwolf*` 消息是从前端内嵌的 `.proto` 文本拿的；这里只手写要发的两种帧（文本消息 / presence），不引 protobuf 依赖、也不用编 .proto。

### protobuf 线格式

protobuf 就两种线型在这儿用得上：

```
wire 0（varint）：tag = field << 3 | 0   后面跟 varint 数值
wire 2（长度前缀）：tag = field << 3 | 2  后面跟长度 varint + 原始字节
```

字符串和嵌套消息都是 wire 2；嵌套消息就是把子帧的字节当值塞进去。

### `max_message_id`

用得上的是这两种帧的同一个形状——`TechwolfChatProtocol` 的**字段 3 是一个重复的条目列表**，每条目**字段 4 是消息 id**（`mid`）：

* 连上后服务端推的**会话同步**：字段 3 是会话列表，字段 4 是该会话最后一条消息的 id（实测 `394570988736768` 这种 3.9e14 量级的号）；
* 收到新消息时的推送：字段 3 是消息列表，字段 4 就是这条消息的 `mid`。

取最大值当发消息时 `mid` 的基数——站点自己也是这么算的（`ChatWebsocket.getMaxMsgId()` 就是本地见过的最大的 `mid`）。`config.CHAT_MID_RANGE` 之外的数字不认，免得把别的字段当 id。

### `encode_text_message`

字段号（来自前端内嵌的 `.proto`）：

```
TechwolfChatProtocol : type=1  messages=3
TechwolfMessage      : from=1  to=2  type=3  mid=4  time=5  body=6  quoteId=20
TechwolfUser         : uid=1   name=2  source=7
TechwolfMessageBody  : type=1  templateId=2  text=3
```

`from` 带 `uid` **和 `source`**——`source` 哪怕等于 0 也**必须写出来**（缺了它网关判这帧非法、直接掐线）；`to` 带 `uid` + `name`（站点这里塞的是 `encryptUid`；空串就不写这个字段，跟站点 `user()` 只在有值时才 `setName` 一致）+ `source`。

`temp_id` 是这条消息的 `mid`（`cmid` 同值）。**要落在服务端的消息 id 数轴上**（3.9e14 量级），实际用的是「基数 + 当前毫秒」。缺省退回当前毫秒——**那只够单测用**，真发会被网关判「id 太旧」。

`time_ms` 是消息时间戳（毫秒）。**必须和 `mid` 分开**：站点那边 `mid` 是 `maxMsgId + now`、`time` 是纯 `now`，两个数差着几个数量级。

### `encode_presence`

站点连上就发这一帧（`sendPresence`）。字段号：

```
TechwolfChatProtocol : type=1  presence=4
TechwolfPresence     : type=1  uid=2  clientInfo=3  lastMessageId=5
TechwolfClientInfo   : version=1  model=4  appid=7  platform=8  channel=9
```

### `ChatSocket`

**一次发送流程**（`send_text`）：`connect` 建连并等到 CONNACK，连上先报一帧 `presence`（对齐站点），**手头没 `mid` 基数才等那帧会话同步**，再 PUBLISH 一帧文本消息，等它**真正写到 socket 上**、主动断开。

**判据是「帧发出去了」**（PUBLISH 无异常、`rc == 0`），**不是「等到 PUBACK」**。

因为发一条就要重连一次，`connect` **每次都用全新的 paho 客户端**（旧的先 close），重连间隔也开得很大（`config.CHAT_RECONNECT_MIN`），不让 paho 在背后自己按秒级重连。

### `ChatSocket._make_paho_client`

握手头：**Cookie 是硬要求**，少了对端直接 403（实测）。重连间隔故意拉大：一帧一条连接，重连由 `connect()` 显式做。

### `ChatSocket.connect`

**每次都重建**：一条连接发一帧就够（发完自己断），复用一个半死的 socket 只会踩到「publish 进黑洞」。新连接 = 新一批推送：上一轮**从推送里**抬到的基数作废，但 `_seed_mid_base`（上一条带过来的）留着。

### `ChatSocket.send_text`

`mid` = 基数 + 当前毫秒，`time` = 当前毫秒（站点就是 `getMaxMsgId() + Date.now()` / `Date.now()`）。**基数取服务端那个 3.9e14 量级的消息 id**，跟站点同源。

retain 必须 False：站点前端传 true，但那样一条消息会在对方那里变成两条（留存的那份被收件人订阅/同步时再投一遍）。2026-10-09 实测确认。

帧已经交给 socket 了（QoS1 的 PUBLISH，本地无异常）。PUBACK 只是顺带看一眼。把这帧真正写到 socket 上再断：`publish()` 回 `rc == 0` 只是入队，写出去是 loop 线程干的。没写完就 close 会把这帧连同连接一起丢。

### `ChatSocket._flush_to_socket`

看 paho 的出站队列 `_out_packet` 排空（写完就出队）；拿不到这个属性（换版本 / 测试替身）就退回固定睡 `flush_wait`。

### `ChatSocket._on_publish`

paho 的 `VERSION2` 回调签名是 `(client, userdata, mid, reason_code, properties)`；`mid` 就是 `paho.mqtt.client.Client.publish` 返回的 `info.mid`（包 id）。

### `ChatSocket` 属性注释

- `_pubacks`：等 PUBACK 用：paho 的包 id → 一个 Event。
- `published`：收到过 PUBACK 的包 id（按序，日志/排查用）。
- `last_connack`：最近一次 CONNACK（失败时拿出来说清楚）。
- `max_msg_id`：推送里见过的最大的消息 id——发消息的 `mid` 就从它往上加。
- `_seen_push`：收到过一帧能扒出消息 id 的推送。
- `last_send_stats`：上一发的分段耗时（秒）：`wait_push` / `publish` / `flush`。
- `connect_seconds`：最近一次建连（握手 → CONNACK）耗时（秒）。
