"""职位获取相关接口地址、请求头、节流参数等常量。

⚠️ 关于接口地址的可信度
--------------------------------------------------------------------------------
**已实测确认**（2026-09-30，Cookie 取自 ``boss_login`` 落盘的登录会话
（``data/boss.db`` 的 ``doc('session')``），直接打真实站点）：

  GET /wapi/zpgeek/pc/special/zone/joblist.json?page=…&type=1
      → 200 + {"code":0,"message":"Success","zpData":{"jobList":[…],"hasMore":…}}
        每页 15 条（pageSize 传了也不认），字段覆盖 todo.md 第四节全部 8 项。
        page=1 起步；page=0 回空列表；翻到头那页不足 15 条且 hasMore=false，
        再下一页回 0 条。

  GET /wapi/zpgeek/pc/recommend/job/list.json   → code 37「浏览器环境异常」
  POST /wapi/zpgeek/search/joblist.json         → code 37「浏览器环境异常」
        （带 ``__zp_stoken__`` 即可过；令牌按 :mod:`boss_jobs.stoken` 自动算）

special/zone 那条（推荐页 PageJobRecommend 在用的也是它）。

路由来自按需 chunk ``static.zhipin.com/zhipin-geek-spa/web/v6748/`` 的
``job-recommend-type.c303eca2.js``（``tb=(0,tc.iH)(v.zI)``，``zI`` 就是
``/wapi/zpgeek/pc/special/zone/joblist.json``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import boss_db

BASE_URL: Final[str] = "https://www.zhipin.com"

#: 职位推荐页（``/web/geek/jobs``）的 Referer。接口不校验它，带上更像浏览器。
GEEK_JOBS_REFERER: Final[str] = f"{BASE_URL}/web/geek/jobs"

ENDPOINTS: Final[dict[str, str]] = {
    #: 唯一在用的分页职位列表（GET）。见文件头的实测记录。
    "job_list": "/wapi/zpgeek/pc/special/zone/joblist.json",
    #: 职位搜索列表（GET）。要登录 **且** 要 ``__zp_stoken__``（缺了回 code 37）。
    #: 令牌由 :class:`boss_jobs.cdp_stoken.CdpStokenProvider` 全自动补（拉 Chrome
    #: 让站点自己算），见 :func:`boss_jobs.client.JobClient.fetch_search_page`。
    "job_search": "/wapi/zpgeek/search/joblist.json",
    #: 职位详情（JD 正文）。**已实测**（2026-10-08）：要登录 **且** 要
    #: ``__zp_stoken__``（缺了回 code 37，跟搜索一样），query 带
    #: ``securityId`` + ``lid`` 两参就够。正文在 ``zpData.jobInfo.postDescription``。
    "job_detail": "/wapi/zpgeek/job/detail.json",
    #: 打招呼 / 加好友（POST，form）。**已实测**（2026-10-08）：query 带
    #: ``securityId`` + ``jobId`` + ``lid``；body 是
    #: ``application/x-www-form-urlencoded``，带 ``encryptBossId`` /
    #: ``sessionId`` 等。**这条只建会话、不投递正文**：请求体里塞
    #: ``greeting`` 服务端直接忽略（回了 code 0，聊天框却还是空的）——
    #: 真的招呼语走聊天通道，见 :mod:`boss_jobs.chat`。
    "friend_add": "/wapi/zpgeek/friend/add.json",
    #: MQTT over WSS 的接入凭据（GET）。回 ``zpData.wt2``，当 MQTT 密码用。
    #: 来源：chat-new 前端 ``ChatWebsocket.init`` 里的 ``l()``。
    "get_wt": "/wapi/zppassport/get/wt",
    #: 当前登录用户（GET）。取 ``zpData.token``（MQTT 用户名前缀）+ ``userId``。
    "get_user_info": "/wapi/zpuser/wap/getUserInfo.json",
    #: 会话对象信息（GET，query ``bossId={encryptBossId}``）。回 ``zpData.data``，
    #: 里面有 **boss 的数字 uid**（``bossId``）与 ``bossSource``——
    #: 这俩正是发聊天消息要的 ``to``。**要先 ``friend/add`` 建了会话才查得到**
    #: （没会话回 code 1「聊天的Boss不存在」/「非好友关系」）。
    "get_boss_data": "/wapi/zpchat/geek/getBossData",
    #: 历史消息（GET，query ``bossId={数字 uid}``）。**当不了送达判据**：
    #: 对这条账号回 ``code 0`` + 空 ``zpData``，有消息的会话也读不出来
    #: （2026-10-08 实测，见 :mod:`boss_jobs.chat` 模块头）。留着只当探针。
    "chat_history": "/wapi/zpchat/geek/historyMsg",
}

#: 推荐页 ``pageType`` 10/45 → type=1（全职流），36 → type=2（兼职流）
REQUEST_PARAMS: Final[dict[str, str]] = {"type": "1"}

DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": GEEK_JOBS_REFERER,
    "X-Requested-With": "XMLHttpRequest",
}

#: 通用成功码（与 wapi 网关一致）
CODE_OK: Final[int] = 0

#: 业务码「未登录 / 登录态失效」。与 :mod:`boss_filter.config` 同判据。
CODE_SESSION_EXPIRED: Final[frozenset[int]] = frozenset({1, 7})

#: 业务码「浏览器环境异常」——缺 ``__zp_stoken__`` 安全网关令牌。
#: 服务端会顺手在 ``zpData`` 里下发一次性挑战 ``{seed,name,ts}``，
#: 拿挑战去跑 ``/web/common/security-js/{name}.js`` 的 ``ABC.z`` 即可算出令牌，
#: 来源与算法见 :mod:`boss_jobs.stoken`。
CODE_BROWSER_CHECK: Final[int] = 37

#: 业务码「您的账户存在异常行为」——账号维度风控，要人机验证（GeeTest）。
#: 不是缺令牌，客户端不去绕。
CODE_RISK_CONTROL: Final[int] = 36

#: 安全网关令牌的 Cookie 名。**不是服务端发的**，是浏览器按
#: ``/web/common/security-js/{name}.js`` 的 ``ABC.z(seed, ts)`` 算出来再回传的，
#: 里面还编了浏览器环境指纹。取它最稳的路子是拉真 Chrome 让站点自己写
#: （:mod:`boss_jobs.cdp_stoken`，``fetch`` 默认走这条）；
#: :mod:`boss_jobs.stoken` 那条 Node 硬算只用来验证算法，服务端不认它的指纹。
STOKEN_COOKIE: Final[str] = "__zp_stoken__"

#: 环境变量兜底：想跳过自动计算、直接用浏览器里拷出来的令牌时设它。
STOKEN_ENV: Final[str] = "BOSS_ZP_STOKEN"

#: 单次请求超时（秒）
DEFAULT_TIMEOUT: Final[float] = 10.0

#: 网络层/5xx 重试次数
DEFAULT_RETRIES: Final[int] = 2

#: 重试退避基数（秒），按 2 的幂递增
DEFAULT_BACKOFF: Final[float] = 0.8

# --------------------------------------------------------------------------- #
# 分页与风控节流
# --------------------------------------------------------------------------- #

#: 服务端固定每页 15 条，传 pageSize 也不认。留作客户端切分/校验用。
PAGE_SIZE: Final[int] = 15

#: 翻页间隔（秒）。**每拿完一页就清洗入库，再睡这么久才要下一页**，
#: 把请求频率压到人手滚动的量级，避免触发风控。
DEFAULT_PAGE_INTERVAL: Final[float] = 1.0

#: 补抓职位详情（JD）的条间隔（秒）。
#: **1 秒**：实测 0.3s 会被安全网关当过频（连环 code 37），1s 是人手点击的量级。
DETAIL_INTERVAL: Final[float] = 1.0

#: 打招呼（发送）之间的条间隔（秒）。同 DETAIL_INTERVAL 一个道理：
#: 1s 是人手点「立即沟通」的节奏，既不像脚本刷屏，也不至于慢到没法用。
DELIVER_INTERVAL: Final[float] = 1.0

# --------------------------------------------------------------------------- #
# 聊天通道（MQTT over WebSocket）
# --------------------------------------------------------------------------- #
#
# 站点**没有**「发一条聊天消息」的 HTTP 接口：消息是 MQTT 上的 protobuf 帧。
# chat-new 前端里 ``ChatWebsocket`` 直接用 Paho MQTT：
#
#     new Paho.MQTT.Client(server, port, "/chatws", "ws-"+rand16)
#     client.connect({token: wt, userName: <token>+"|0", password: wt,
#                     keepAliveInterval: 25, cleanSession: true, mqttVersion: 3,
#                     useSSL: true})
#     client.send("chat", <TechwolfChatProtocol>.toArrayBuffer(), 1, true)
#
# 2026-10-08 **全链路实测通过**（见 :mod:`boss_jobs.chat`）：
# 建 MQTT（CONNACK Success）→ 往 ``chat`` 主题 PUBLISH 一帧文本消息 →
# **站点自己的会话列表里出现这条招呼语并标「[送达]」**（重载页面、从服务端
# 重拉也还在，是服务端真收下了）。
#
# 三个坑：
#
# 1. **WebSocket 握手必须带登录 Cookie**，不带直接 HTTP 403（实测）。
# 2. **文本帧要跟站点逐字节对齐**：``from`` 必须显式带 ``source``（哪怕 0），
#    ``mid`` 要落在服务端消息 id 的数轴上（3.9e14 量级，不是毫秒时间戳）。
#    见 :mod:`boss_jobs.chat` 模块头。
# 3. **这条网关不给文本帧回 PUBACK**，PUBLISH 完约 150ms 直接把 WebSocket
#    关掉——**这是它的常态，不是拒收**（那几发都真送达了）。所以判据是
#    「帧发出去了」，不是「等到 PUBACK」（见 :data:`CHAT_PUBACK_WAIT`，默认
#    0 = 不等）。``GET /wapi/zpchat/geek/historyMsg`` 对这条账号回 ``code 0``
#    + 空 ``zpData``，连有消息的会话也读不出来，也当不了判据。
#
# 生产服务器的 host/port 来自 chunk ``26308``：
#    ``{useSSL:true, server:"ws6.zhipin.com", port:443,
#      uris:["ws6.zhipin.com","ws2.zhipin.com","ws.zhipin.com"]}``；
#    池子也可以问 ``GET /wapi/zpchat/config/ws``。

#: 聊天 MQTT 网关（WebSocket Secure）。
CHAT_WS_HOST: Final[str] = "ws6.zhipin.com"
CHAT_WS_PORT: Final[int] = 443
#: Paho 客户端构造里的 path（第三个参数）。
CHAT_WS_PATH: Final[str] = "/chatws"
#: 消息统一发到这一个主题，收件人在 protobuf 的 ``to`` 里。
CHAT_TOPIC: Final[str] = "chat"

#: MQTT 心跳（秒），对齐站点前端的 ``keepAliveInterval: 25``。
CHAT_KEEPALIVE: Final[int] = 25
#: 等 CONNACK 的超时（秒）。
CHAT_TIMEOUT: Final[float] = 15.0

#: 连上之后等多久去收服务端主动推的那帧**会话同步**（秒）。
#: 它带着每个会话最后一条消息的 id，是本地算 ``mid`` 的唯一现成基数（见
#: :data:`CHAT_MID_FLOOR`）。**只在手里没基数时才等**（整批第一条）——
#: 基数能跨条带过来（见 :class:`boss_jobs.chat.ChatSocket` 的 ``mid_base``），
#: 有了就直接发，别每条都白等这 4 秒。等不到就退回基数兜底值。
CHAT_PUSH_WAIT: Final[float] = 4.0

#: ``mid`` 的基数兜底值。**服务端的消息 id 是 3.9e14 量级的雪花号**
#: （2026-10-08 实测：会话最后一条消息 394570988736768，站点 ``maxMsgId``
#: 394570988769538），不是毫秒时间戳。发出去的帧 ``mid`` 必须落在这个数轴上、
#: 且大于对方会话已有的 id，否则网关判这帧非法、直接掐线（没有 PUBACK）。
#: 站点自己算的是 ``getMaxMsgId() + Date.now()``：拿本地见过的最大 id 再加当前
#: 毫秒，保证「比已知的都大、又不撞车」。这里同样先把基数抬到服务端量级，
#: 正常路径下这个兜底值会被收到的那帧会话同步里的真实 id 顶上去。
CHAT_MID_FLOOR: Final[int] = 394_000_000_000_000

#: 从推送里认「这是个消息 id」的合理区间。用来滤掉解析到的别的数字字段，
#: 免得把 ``mid`` 抬到离谱的地方去。
CHAT_MID_RANGE: Final[tuple[int, int]] = (10**13, 10**17)

#: 等那条 PUBACK 的超时（秒）——**只是记日志用，不是判据**。
#: 2026-10-08 实测：这条网关对文本帧**不回 PUBACK**，发完约 150ms 直接把
#: WebSocket 关掉（``close code=1000 reason="Bye"``）。可那几发（站点界面上
#: 都显示「[送达]」）是真真切切进了服务端的——所以「没等到 PUBACK」不等于
#: 失败。既然回执**从来不到**，默认 0 = 发完不等，省掉每条干烧的 1.5s；
#: 想抓「这回倒是有回执」这种非常态再临时调大。
CHAT_PUBACK_WAIT: Final[float] = 0.0

#: 发完再停多久才主动断（秒）。PUBACK 等不到（见上），这停顿只留给
#: 「网关还没来得及掐线」——它常态 **约 150ms** 就把 WebSocket 关掉，所以
#: 停 0.15s 就够，再多是每条干烧。
CHAT_FLUSH_WAIT: Final[float] = 0.15

#: 把 PUBLISH**真正写到 socket 上**最多等多久（秒）。``publish()`` 回
#: ``rc == 0`` 只是**入队成功**，真写出去是 paho 的 loop 线程干的——没写完
#: 就断连，这帧随连接一起丢，而这条网关又不回 PUBACK，没法用回执发现，
#: 只会表现成「会话建了、招呼语没了」。所以发完等它出队（一般几十毫秒），
#: 这个数只是硬上限。
CHAT_FLUSH_DEADLINE: Final[float] = 1.0

#: 正文帧的 MQTT ``retain``（保留消息）标志。**定案 ``False``**（2026-10-09 实测）。
#: 站点前端用的是 ``true``（``client.send("chat", frame, 1, true)``），照抄会出问题：
#: 留下 ``true`` 时，一条招呼语在对方会话里会**变成两条**（实测确认）；改成 ``false``
#: 后不再重复。原因是 ``retain=true`` 会让 broker 把这一帧**留在 ``chat`` 主题上**，
#: 收件人（重新）订阅 / 同步时会**再收到一遍留存的那份**——实时那份 + 留存那份 = 两条。
#: 站点前端用 ``true`` 不出这毛病，是因为它本地 ``pendingDeliverMap`` 会按 ``cmid``
#: 把回来的那份**并回占位**、不新加气泡；我们不在站点前端里，没有这套本地去重。
#: ``false`` = 只走实时投递（对方离线会收不到，但本来就是实时场景）。
#: **presence 帧仍随站点用 ``true``**（不是消息、不参与去重）。
CHAT_RETAIN: Final[bool] = False

#: 断线重连间隔（秒）。**故意开得很大**：一帧一条连接（发完主动断），
#: 重连由 :class:`boss_jobs.chat.ChatSocket` 显式重建，不让 paho 在后台
#: 按秒级节奏自己重连（那会变成连环握手，实测会被网关更早踢掉）。
CHAT_RECONNECT_MIN: Final[int] = 30
CHAT_RECONNECT_MAX: Final[int] = 60

#: 发消息时塞进 ``from``/``to`` 的 ``TechwolfUser.source``，站点前端按会话对象
#: 的 ``friendSource`` 填（老板侧实测回 ``0``）。
CHAT_DEFAULT_SOURCE: Final[int] = 0
#: 文本消息的 ``body.type`` / ``templateId``（站点 ``createMessage.text`` 里写死 1）。
CHAT_BODY_TEXT: Final[int] = 1
#: ``TechwolfChatProtocol.type``：1 普通消息 / 2 presence。
CHAT_PROTO_MESSAGE: Final[int] = 1
CHAT_PROTO_PRESENCE: Final[int] = 2
#: ``TechwolfClientInfo`` 里报的版本号与 appid，对齐站点（4.92 / 9019 / web）。
CHAT_CLIENT_VERSION: Final[str] = "4.92"
CHAT_APP_ID: Final[int] = 9019
#: presence 帧的 ``type``（1 = 上线）。
CHAT_PRESENCE_ONLINE: Final[int] = 1

#: 撞上安全网关 code 37 时先歇多久再拿同一枚令牌重试（秒）。
#: 37 有时只是「请求太快」，先退避；歇完还 37 才轮到强制换新（换新自己
#: 还有 ``RENEW_COOLDOWN`` 冷却，不会连环拉 Chrome）。
BROWSER_CHECK_BACKOFF: Final[float] = 2.0

#: 撞上安全网关 code 37 之后，下一条之前再多歇多久（秒）。
#: 37 有时是「令牌不对」，有时是**整段 IP / 会话被限速**——后者多打一发只会
#: 撞得更狠。失败后先躺平一会儿，再碰下一条。
BROWSER_CHECK_COOLOFF: Final[float] = 5.0

#: **连续**撞上 code 37 几次就停整批。
#: 实测（2026-10-08）：安全网关的限速墙是「一小段窗口里放行几发，然后整段拦」，
#: 撞墙后继续一条条砸只会把窗口越压越久。连环 3 次 = 这一轮大概率已经全线拦了，
#: 停批让人歇几分钟，比拿几十发请求去探墙厚道得多（也不容易把账号风控惹出来）。
BROWSER_CHECK_GIVEUP: Final[int] = 3

#: 默认最多翻几页。0 = 一直翻到接口回空页。
DEFAULT_MAX_PAGES: Final[int] = 0

# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #

#: 项目根（``F:\boss``），跟 cwd 无关。
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: 状态库路径。职位、登录态、筛选条件、stoken、LLM 配置、简历、分析全在这一份
#: ``data/boss.db`` 里（见 :mod:`boss_db`）。环境变量 ``BOSS_DB`` 可覆盖。
DEFAULT_DB_PATH: Final[Path] = boss_db.DEFAULT_DB_PATH

# --------------------------------------------------------------------------- #
# 薪资字体反混淆
# --------------------------------------------------------------------------- #

#: 列表页 HTML 里的薪资是私有区字符（防爬字体），映射见 chunk 的
#: ``S=["&#xe031;",…,"&#xe03a;"]`` + ``mixFont`` → 下标即数字 0-9。
#: **JSON 接口回的 salaryDesc 已经是明文**（"8-15K"），这表留给 HTML
#: 兜底解析和历史脏数据用。
FONT_DIGIT_ENTITIES: Final[tuple[str, ...]] = (
    "&#xe031;",
    "&#xe032;",
    "&#xe033;",
    "&#xe034;",
    "&#xe035;",
    "&#xe036;",
    "&#xe037;",
    "&#xe038;",
    "&#xe039;",
    "&#xe03a;",
)
#: 同一张表的字符形式（U+E031…U+E03A），下标即数字 0-9
FONT_DIGIT_CHARS: Final[tuple[str, ...]] = tuple(chr(0xE031 + i) for i in range(10))
