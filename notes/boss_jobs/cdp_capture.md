# `boss_jobs.cdp_capture` 协议笔记

> 摘自 `boss_jobs/cdp_capture.py` 的模块文档字符串 / 文档字符串 / `#:` 注释。

## 一句话

CDP 网络抓包：附着到带调试口的 Chrome，把整个浏览器实例的请求（含 WS 全帧）落盘。

## 细节

### 用途与边界

这个模块**不参与**取 `__zp_stoken__`（那条路在 `boss_jobs.cdp_stoken`）。
它的用途是「看清浏览器到底在发什么」：附着到一台已经在跑的 Chrome，订阅
`Network` 域，把所有**非 JS/CSS/HTML** 的请求连同请求头、POST 参数、响应头、
响应体一起写到磁盘，含 WebSocket 的握手与**全部帧**。

本模块**默认只附着、绝不关 Chrome**——那是用户自己开的那台。

为什么不能拿 `cdp_stoken.CdpClient` 改：那个类的 `call` 是严格一问一答，
非本 id 的帧（也就是**所有 CDP 事件**）会被静默丢掉。要收事件就得有自己的读循环。

### 为什么是两条线程（这条是理解本模块的钥匙）

处理 `Network.loadingFinished` 时要去调 `Network.getResponseBody` 取响应体，
而回包只能从**同一条 ws** 上 `recv()` 到。如果「读 ws」和「处理事件」是同一个
线程，那个等待的回包永远排在它自己后面——线程自己阻塞自己，死锁。所以：

* **读线程**只做一件事：`recv()` → 有 `id` 就唤醒等回包的人，有 `method`
  就塞进队列。它**不处理事件、不发命令、不碰磁盘**。
* **消费线程**从队列取事件，该发命令就发（回包由读线程投递），该落盘就落盘。
* 落盘因此是**单写者**，文件句柄不用加锁。

队列满时读线程会阻塞在这一侧，形成背压（Chrome 发不动就先攒着，不丢）。

### 输出的那点讲究

* `index.jsonl` 一行一个请求，请求头**优先取 ExtraInfo 的版本**——因为
  `requestWillBeSent.request.headers` 普遍不含 `Cookie`，
  `responseReceived.response.headers` 也不含 `Set-Cookie`，真实头只在
  `*ExtraInfo` 里。ExtraInfo 可能早于主事件到达，所以两边都要能「先到先存」。
* `requestId` **只在单个 session 内唯一**，跨 session 会撞车，所以内部一律用
  `(session_id, requestId)` 复合键。这是最容易写错、后果最隐蔽的地方。
* WebSocket 的二进制帧（`opcode=2`）按 CDP 约定 `payloadData` 已经是 base64，
  这里原样存 base64 + `binary=true`，**不做二次编码**，留给后续自己解 MQTT over WS
  的包结构（WS 帧边界与 MQTT 包边界不对齐，解包要另做重组）。
* `websocket.jsonl` 里 `opcode=0` 是分片的续帧，标 `continuation=true`；
  **消息重组不在本模块范围内**。
* 3xx 没有正文可取，别去问 `getResponseBody` 讨一个必然失败的答案。
* WS 没有「响应体」，别去问 `getResponseBody`。
* 在 `loadingFinished` 当刻把响应体抢下来——缓冲会被驱逐，晚一步就没了。
  取不到不是错误（`data:`/`blob:`/204/304/重定向/流式/已驱逐都会失败），记进 `body.note` 接着跑。

### 常量

- `ALL_RESOURCE_TYPES`：CDP `ResourceType` 的全部取值。写全是为了让 `--include-types` 的拼写错误能在 CLI 层被指出来，而不是静默抓不到东西。
- `DEFAULT_EXCLUDE_TYPES`：默认排除：正好是 HTML / CSS / JS 三种。其余全留（含图片、字体）。
- `_MEDIA_TYPES`：`--preset media` 在默认之上再排掉这些东西。
- `_API_TYPES`：`--preset api` 只留这些——实抓业务接口 + MQTT over WS 的推荐档。
- `ATTACHABLE_TARGET_TYPES`：值得 attach 并订阅 `Network` 的 target 类型。`browser` / `tab` / `other` 上没有网络栈，只记元数据。
- `_AV_SUBTYPE_EXT`：`audio/*` / `video/*` 的子类型 → 扩展名。表外的按字面用子类型，不安全就 bin。
- `_EVENT_HANDLERS`：事件方法名 → `CaptureSession` 上的处理方法。

### `CaptureOptions`

- `flush_every`：`websocket.jsonl` 攒几行 flush 一次（帧可能很密）。`1` = 逐行。
- `fetch_post_data`：`postData` 缺失时要不要回头调 `Network.getRequestPostData` 补。
- `buffer_mb`：`Network.enable` 的缓冲上限（MB）。`0` = 用 Chrome 默认。

### `CaptureWriter`

`meta.json` 的累积字段：收尾那版是**合并**不是替换，否则「开抓先写一版 started_at/ws_url」会被收尾那版盖掉。

### `RequestRecord`

- `written`：已经写过 index 行没有（redirect 复用 requestId 时靠它防重复）。
- `post_data_raw` / `post_note`：待落盘的 POST 正文（真取到了才非 None）。

WebSocket 的握手也会走 `requestWillBeSent`；`webSocketCreated` 已经建过记录了，往那条上并，别再开一条。
同一个 requestId 又来了且带着 `redirectResponse` = 上一跳是 3xx。先把上一跳单独收尾落盘，再开这一跳，别把两跳并成一条。
渲染进程给的头不含 Cookie 之类的凭证，ExtraInfo 到了会盖掉这份。
ExtraInfo 的头更全（含 Set-Cookie），已经拿到就别被 `responseReceived` 那份盖回去。

### `CaptureSession`

`_records`：`(session_id, requestId)` → 这条 id 上的历次记录（redirect 会复用 requestId）。
`_pending_req_extra` / `_pending_resp_extra`：ExtraInfo 可能早于主事件到，先存这儿。

### `setup_session`

每个新 session 都要走一遍——包括 `Target.attachedToTarget` 事件带来的。
这里**还要再发一次** `Target.setAutoAttach`：那个命令的作用域是「所在 target 的
直接子 target」，browser 级那次只覆盖顶层标签页，而 OOPIF 和 worker 是页面的
孙子层，不在那个页面的 session 上再设一次就收不到。

必须在 `Network.enable` 之后放行，否则首屏那批请求照样漏。

### `_bootstrap`

先设 autoAttach，堵住「枚举 target 到 attach 之间」新开的那些。
已经被 DevTools 之类占住的 target 会回 error，跳过就好，别中断整体。
已被 autoAttach 接上的会走 `attachedToTarget` 事件，`claim_session` 去重。

### `CdpConnection`

suppress_origin：Chrome 111+ 会对陌生 Origin 回 403，调试口本来就是本地闭环，不发 Origin 头最干净。
websocket-client 的 `send` 不保证线程安全。
读线程里 WebSocketTimeout 只是空闲，不是错——和 `CdpClient.call` 的判据不一样。
