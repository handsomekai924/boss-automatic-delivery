"""聊天通道：MQTT over WebSocket + 手写 protobuf 帧。

**为什么要有这个模块**：``POST /wapi/zpgeek/friend/add.json`` 只**建会话**，
它不投递任何正文——请求体里带 ``greeting`` 服务端直接忽略（回 code 0，
聊天框还是空的）。站点自己也不靠它发招呼语：消息走 **MQTT**。

逆向结论（全部来自 chat-new 前端 ``static/js/app.*.js``，2026-10-08 实测通过）：

===========================================  ==========================================
站点前端                                    本模块对应
===========================================  ==========================================
``new Paho.MQTT.Client(server, port, "/chatws", uuid)``  :class:`ChatSocket`（paho-mqtt）
``connect({userName: token+"|0", password: wt, ...})``    :meth:`ChatSocket.connect`
``client.send("chat", frame.toArrayBuffer(), 1, true)``   :meth:`ChatSocket.send_text`（**retain 我们传 ``False``**，见 :data:`config.CHAT_RETAIN`）
``createMessage.text(stanza)``                            :func:`encode_text_message`
``createMessage.presence(...)``                           :func:`encode_presence`
===========================================  ==========================================

五个必须踩对的点：

1. **握手要带登录 Cookie**——不带的 WebSocket 升级请求被网关回 **HTTP 403**。
2. **MQTT 用户名是 ``<token>|0``、密码是 ``wt``**：``token`` 来自
   ``GET /wapi/zpuser/wap/getUserInfo.json`` 的 ``zpData.token``，
   ``wt`` 来自 ``GET /wapi/zppassport/get/wt`` 的 ``zpData.wt2``。
3. **``to.uid`` 是 boss 的数字 uid**，不是 ``encryptBossId``。``encryptBossId``
   要先换：``GET /wapi/zpchat/geek/getBossData?bossId={encryptBossId}``
   （见 :meth:`boss_jobs.client.JobClient.fetch_boss_data`），而且要
   **先 ``friend/add`` 建了会话**才查得到。
4. **``from`` 里必须带 ``source``**，**``mid`` 必须落在服务端的消息 id 数轴上**。
5. **正文帧的 ``retain`` 要 ``false``**（站点前端是 ``true``）——留着 ``true``
   会让一条消息在对方那里**变成两条**，见 :data:`config.CHAT_RETAIN`。

第 4 条踩了很久，值得单独说。一开始本模块发的文本帧 ``from`` 只有 ``uid``，
``mid`` 用的是当前毫秒；结果网关收到**立刻把连接关掉**（网页里
``close code=1000 reason="Bye"``，paho 这边是 ``DISCONNECT Unspecified error``），
而且**从不回 PUBACK**。当时试遍了 retain / qos / presence 内容 / MQTT 版本 /
各种 cookie，全都没用——因为**是这个帧本身被判非法**，网关的反应就是掐线。

定论来自两次活体对照（2026-10-08 深夜）：

* 挂上 ``WebSocket.prototype.send``，让站点自己在聊天页里发一条消息，抓到它线上
  那帧，和我们同参数的帧逐字节比——**唯一的字段差异就是 ``from`` 少了
  ``source:0``**（站点 ``createMessage.text`` 里 ``from`` 也走
  ``user(uid, encryptUid, source)``，proto2 显式写这个字段）。
* 补上 ``from.source`` 后**还是被掐**。再比才发现第二处差异在 **``mid``**：
  站点发的是 ``ChatWebsocket.getMaxMsgId() + Date.now()`` ≈ **3.96e14**
  （服务端消息 id 是 3.9e14 量级的雪花号），``time`` 才是纯 ``Date.now()``
  ≈ 1.79e12。我们的帧把 ``mid`` 也写成了毫秒时间戳——**比对方会话里已有的消息 id
  小了几个数量级**，网关按「id 太旧」毙掉。按站点算式（基数取服务端量级的 id、
  再加当前毫秒）发出去，消息就进了会话（站点界面显示「[送达]」）。

所以 ``mid`` 的基数从**连上后服务端推的那帧会话同步**里取：那帧带着每个会话最后
一条消息的 id（见 :func:`max_message_id`），取其中最大值当基数，等不到就用
:data:`config.CHAT_MID_FLOOR` 兜底。同一批里后续的帧**把上一条的基数带过来**
（:class:`ChatSocket` 的 ``mid_base``），不用每条都重新等那帧同步。

**别拿 PUBACK 当判据**（这条踩过，报告里也一度说错过）：这条网关对文本帧
**根本不回 PUBACK**——PUBLISH 完约 150ms 直接把 WebSocket 关掉，
``close code=1000 reason="Bye"``，**这是它的常态，不是拒收**。2026-10-08 实测的
那几发（站点会话列表里都出现了招呼语并标「[送达]」，重载页面、从服务端重拉也还在）
**一发 PUBACK 都没等到**。所以发送成功的判据是「帧发出去了」（PUBLISH 无异常、
``rc == 0``），PUBACK 只是顺带看一眼、记进日志，而且默认**整个不等**（回执
从来不到，等它纯烧时间，见 :data:`config.CHAT_PUBACK_WAIT`）。
「回读聊天记录」同样当不了判据：``GET /wapi/zpchat/geek/historyMsg`` 对这条账号
返回 ``code 0`` + 空 ``zpData``，**连着有消息、刚确认送达的会话也读不出来**。

protobuf 那套 ``Techwolf*`` 消息是从前端内嵌的 ``.proto`` 文本拿的；这里只
手写要发的两种帧（文本消息 / presence），不引 protobuf 依赖、也不用编 .proto。
"""

from __future__ import annotations

import logging
import random
import string
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import config as C
from .errors import ChatSendError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# protobuf 线格式（只够本模块要发的帧）
# --------------------------------------------------------------------------- #
#
# protobuf 就两种线型在这儿用得上：
#
#   wire 0（varint）：``tag = field << 3 | 0``  后面跟 varint 数值
#   wire 2（长度前缀）：``tag = field << 3 | 2`` 后面跟长度 varint + 原始字节
#
# 字符串和嵌套消息都是 wire 2；嵌套消息就是把子帧的字节当值塞进去。


def _varint(value: int) -> bytes:
    """无符号 varint（protobuf 的整数编码）。"""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def _vint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(int(value))


def _sstr(field: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _varint(field << 3 | 2) + _varint(len(raw)) + raw


def _sub(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


# --------------------------------------------------------------------------- #
# 读推送：从服务端那帧会话同步里扒消息 id（发消息要用的 ``mid`` 基数）
# --------------------------------------------------------------------------- #


def _iter_fields(buf: bytes):
    """极简 protobuf 遍历器：产出 ``(字段号, wire, 值)``。

    wire 0 给 int，wire 2 给 bytes（不解嵌套，交给调用方按需再走一遍）。
    只用来读推送里的数字字段，编不了也解不了复杂消息——要发的两种帧还是
    :func:`encode_text_message` / :func:`encode_presence` 手写的。
    """
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint_at(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint_at(buf, i)
        elif wire == 2:
            ln, i = _varint_at(buf, i)
            val, i = buf[i : i + ln], i + ln
        elif wire == 5:
            val, i = buf[i : i + 4], i + 4
        elif wire == 1:
            val, i = buf[i : i + 8], i + 8
        else:  # pragma: no cover - 服务端不会发这两种
            raise ValueError(f"wire {wire} @{i}")
        yield field, wire, val


def _varint_at(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        byte = buf[i]
        i += 1
        val |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return val, i
        shift += 7


def max_message_id(payload: bytes) -> int:
    """从一帧推送里扒出最大的消息 id（没有就回 0）。

    用得上的是这两种帧的同一个形状——``TechwolfChatProtocol`` 的**字段 3 是
    一个重复的条目列表**，每条目**字段 4 是消息 id**（``mid``）：

    * 连上后服务端推的**会话同步**：字段 3 是会话列表，字段 4 是该会话最后
      一条消息的 id（实测 ``394570988736768`` 这种 3.9e14 量级的号）；
    * 收到新消息时的推送：字段 3 是消息列表，字段 4 就是这条消息的 ``mid``。

    取最大值当发消息时 ``mid`` 的基数——站点自己也是这么算的
    （``ChatWebsocket.getMaxMsgId()`` 就是本地见过的最大的 ``mid``）。
    :data:`config.CHAT_MID_RANGE` 之外的数字不认，免得把别的字段当 id。
    """
    low, high = C.CHAT_MID_RANGE
    best = 0
    try:
        for field, wire, value in _iter_fields(payload):
            if field != 3 or wire != 2:
                continue
            try:
                for inner, inner_wire, inner_value in _iter_fields(value):
                    if inner == 4 and inner_wire == 0 and low <= inner_value <= high:
                        best = max(best, inner_value)
            except (IndexError, ValueError):
                continue
    except (IndexError, ValueError):
        return best
    return best


def encode_text_message(
    *,
    from_uid: int,
    to_uid: int,
    text: str,
    from_source: int = C.CHAT_DEFAULT_SOURCE,
    to_source: int = C.CHAT_DEFAULT_SOURCE,
    to_name: str = "",
    temp_id: int | None = None,
    time_ms: int | None = None,
    quote_id: int = 0,
) -> bytes:
    """一帧文本消息：``TechwolfChatProtocol{type:1, messages:[TechwolfMessage]}``。

    字段号（来自前端内嵌的 ``.proto``）：::

        TechwolfChatProtocol : type=1  messages=3
        TechwolfMessage      : from=1  to=2  type=3  mid=4  time=5  body=6  quoteId=20
        TechwolfUser         : uid=1   name=2  source=7
        TechwolfMessageBody  : type=1  templateId=2  text=3

    ``from`` 带 ``uid`` **和 ``source``**——``source`` 哪怕等于 0 也**必须写出来**
    （缺了它网关判这帧非法、直接掐线，见模块头第 4 条）；``to`` 带
    ``uid`` + ``name``（站点这里塞的是 ``encryptUid``；空串就不写这个字段，
    跟站点 ``user()`` 只在有值时才 ``setName`` 一致）+ ``source``。

    :param from_uid: 自己的数字 userId（``getUserInfo.json`` 的 ``zpData.userId``）
    :param to_uid: **对方数字 uid**（``getBossData`` 的 ``data.bossId``）
    :param text: 正文
    :param from_source: 自己的 source，站点写 0（字段必须出现，见上）
    :param to_source: 对方来源（``getBossData`` 的 ``data.bossSource``）
    :param to_name: 对方名字字段，站点塞 ``encryptBossId``
    :param temp_id: 这条消息的 ``mid``（``cmid`` 同值）。**要落在服务端的消息 id
        数轴上**（3.9e14 量级），实际用的是「基数 + 当前毫秒」，基数从服务端推的
        会话同步里取，见 :func:`max_message_id` 和
        :meth:`ChatSocket.send_text`。缺省退回当前毫秒——**那只够单测用**，
        真发会被网关判「id 太旧」（见模块头第 4 条）
    :param time_ms: 消息时间戳（毫秒），缺省取当前毫秒。**必须和 ``mid`` 分开**：
        站点那边 ``mid`` 是 ``maxMsgId + now``、``time`` 是纯 ``now``，两个数差着
        几个数量级
    :param quote_id: 引用消息 id，0 = 不引用
    """
    if not from_uid:
        raise ValueError("from_uid 不能为空")
    if not to_uid:
        raise ValueError("to_uid 不能为空")
    if not text:
        raise ValueError("text 不能为空（不支持发空消息）")

    stamp = int(time.time() * 1000) if temp_id is None else int(temp_id)
    when = stamp if time_ms is None else int(time_ms)

    from_user = _vint(1, from_uid) + _vint(7, from_source)
    to_user = (
        _vint(1, to_uid)
        + (_sstr(2, to_name) if to_name else b"")
        + _vint(7, to_source)
    )
    body = _vint(1, C.CHAT_BODY_TEXT) + _vint(2, 1) + _sstr(3, text)
    message = (
        _sub(1, from_user)
        + _sub(2, to_user)
        + _vint(3, 1)  # TechwolfMessage.type = 1（单聊）
        + _vint(4, stamp)  # mid
        + _vint(5, when)  # time（站点这里是纯 Date.now()，跟 mid 不是一回事）
        + _sub(6, body)
        + _vint(11, stamp)  # cmid
    )
    if quote_id:
        message += _vint(20, quote_id)
    return _vint(1, C.CHAT_PROTO_MESSAGE) + _sub(3, message)


def encode_presence(*, uid: int, last_message_id: int = 0) -> bytes:
    """上线帧：``TechwolfChatProtocol{type:2, presence:TechwolfPresence}``。

    站点连上就发这一帧（``sendPresence``）。字段号：::

        TechwolfChatProtocol : type=1  presence=4
        TechwolfPresence     : type=1  uid=2  clientInfo=3  lastMessageId=5
        TechwolfClientInfo   : version=1  model=4  appid=7  platform=8  channel=9

    :param uid: 自己的数字 userId（``TechwolfPresence.uid`` 是 int32）
    :param last_message_id: 本地最大消息 id，新连接传 0
    """
    client_info = (
        _sstr(1, C.CHAT_CLIENT_VERSION)
        + _sstr(4, "web")
        + _vint(7, C.CHAT_APP_ID)
        + _sstr(8, "web")
        + _sstr(9, "-1")
    )
    presence = _vint(1, C.CHAT_PRESENCE_ONLINE) + _vint(2, uid) + _sub(3, client_info)
    if last_message_id:
        presence += _vint(5, last_message_id)
    return _vint(1, C.CHAT_PROTO_PRESENCE) + _sub(4, presence)


# --------------------------------------------------------------------------- #
# MQTT 连接
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChatCredentials:
    """MQTT 接入凭据（都是 HTTP 接口取来的，见 :mod:`boss_jobs.client`）。

    :param user_id: 自己的数字 userId（``from.uid``）
    :param token: ``getUserInfo.json`` 的 ``zpData.token``（MQTT 用户名前缀）
    :param wt: ``get/wt`` 的 ``zpData.wt2``（MQTT 密码）
    :param cookie: 登录 Cookie 串，WebSocket 握手要带（不带回 403）
    :param name: 自己的昵称，仅日志用
    """

    user_id: int
    token: str
    wt: str
    cookie: str = ""
    name: str = ""


class ChatSocket:
    """一条 MQTT over WSS 长连接，用来往 ``chat`` 主题发消息。

    **一次发送流程**（:meth:`send_text`）：:meth:`connect` 建连并等到 CONNACK，
    连上先报一帧 ``presence``（对齐站点），**手头没 ``mid`` 基数才等那帧会话
    同步**（见 :func:`max_message_id`；基数能跨条带过来就不再等），再 PUBLISH
    一帧文本消息，等它**真正写到 socket 上**（:meth:`_flush_to_socket`）、主动
    断开。

    **判据是「帧发出去了」**（PUBLISH 无异常、``rc == 0``），**不是「等到
    PUBACK」**：这条网关对文本帧根本不回 PUBACK，PUBLISH 完约 150ms 直接把连接
    关掉——**这是它的常态，不是拒收**（实测那几发都真送达了，见模块头）。
    :meth:`send_text` 仍然会 :attr:`puback_wait` 秒等一下，但只为把「这回倒是有
    回执」这种非常态记进日志，等不到不算失败。

    因为发一条就要重连一次，:meth:`connect` **每次都用全新的 paho 客户端**
    （旧的先 close），重连间隔也开得很大（:data:`config.CHAT_RECONNECT_MIN`），
    不让 paho 在背后自己按秒级重连。

    :param credentials: 见 :class:`ChatCredentials`
    :param host: MQTT 网关，默认 :data:`config.CHAT_WS_HOST`
    :param port: 默认 :data:`config.CHAT_WS_PORT`
    :param path: WebSocket path，默认 :data:`config.CHAT_WS_PATH`
    :param timeout: 等 CONNACK 的超时（秒）
    :param push_wait: 等那帧会话同步（``mid`` 基数）的超时（秒），默认
        :data:`config.CHAT_PUSH_WAIT`；**手头已有基数就整个不等**，等不到就用
        :data:`config.CHAT_MID_FLOOR` 兜底
    :param puback_wait: 发完顺带等 PUBACK 的超时（秒），默认
        :data:`config.CHAT_PUBACK_WAIT`；**等不到正常**，只记日志，``<= 0``
        就直接跳过这段等待
    :param flush_wait: 出站队列拿不到时的兜底停顿（秒），默认
        :data:`config.CHAT_FLUSH_WAIT`；正常走 :meth:`_flush_to_socket` 等真写完
    :param mid_base: 带过来的 ``mid`` 基数（上一条见过的最大消息 id）。
        一帧一条连接，每次重建都从 0 重新等会话同步太亏——把上一条的基数
        带过来，第二条起就不用等了。**必须单调不减**（网关按「id 太旧」毙帧，
        见模块头），带过来的基数只会被更新的推送抬高。
    :param client_factory: 造 paho ``Client`` 的工厂（测试里注入假的）
    :param sleeper: 可替换的 sleep（测试里注入 no-op）
    """

    def __init__(
        self,
        credentials: ChatCredentials,
        *,
        host: str = C.CHAT_WS_HOST,
        port: int = C.CHAT_WS_PORT,
        path: str = C.CHAT_WS_PATH,
        timeout: float = C.CHAT_TIMEOUT,
        push_wait: float = C.CHAT_PUSH_WAIT,
        puback_wait: float = C.CHAT_PUBACK_WAIT,
        flush_wait: float = C.CHAT_FLUSH_WAIT,
        mid_base: int = 0,
        client_factory: Any | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.credentials = credentials
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self.push_wait = push_wait
        self.puback_wait = puback_wait
        self.flush_wait = flush_wait
        self._seed_mid_base = max(0, int(mid_base))
        self._sleep = sleeper
        self._client_factory = client_factory
        self._client: Any = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        #: 等 PUBACK 用：paho 的包 id → 一个 Event（:meth:`_on_publish` 负责 set）
        self._pubacks: dict[int, threading.Event] = {}
        #: 收到过 PUBACK 的包 id（按序，日志/排查用）
        self.published: list[int] = []
        #: 最近一次 CONNACK（失败时拿出来说清楚）
        self.last_connack: Any = None
        #: 推送里见过的最大的消息 id——发消息的 ``mid`` 就从它往上加
        #: （见 :func:`max_message_id`）
        self.max_msg_id: int = self._seed_mid_base
        #: 收到过一帧能扒出消息 id 的推送（:meth:`send_text` 等它）
        self._seen_push = threading.Event()
        #: 上一发的分段耗时（秒）：``wait_push`` / ``publish`` / ``flush``。
        #: 上层拿去打一条「时间花在哪」的日志，别靠猜。
        self.last_send_stats: dict[str, float] = {}
        #: 最近一次建连（握手 → CONNACK）耗时（秒）
        self.connect_seconds: float = 0.0

    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._client is not None and self._connected.is_set()

    def _client_id(self) -> str:
        alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
        return "ws-" + "".join(random.choice(alphabet) for _ in range(16))

    def _build_client(self) -> Any:
        """造一个 paho 客户端（或测试注入的替身）并把回调接上。

        回调接线放在这儿而不是「造 paho」里，是为了让测试注入的假客户端
        也能走同一套 :meth:`connect` / :meth:`_on_connect` 编排。
        """
        client = (
            self._client_factory()
            if self._client_factory is not None
            else self._make_paho_client()
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_publish = self._on_publish
        return client

    def _make_paho_client(self) -> Any:
        try:
            import paho.mqtt.client as mqtt  # 延迟导入：只有真发消息才要 paho
        except ImportError as exc:  # 缺依赖别抛成 'No module named ...' 就完事
            raise ChatSendError(
                "缺 MQTT 依赖 paho-mqtt，招呼语发不出去"
                "（装上再试：pip install -r requirements.txt）"
            ) from exc

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id(),
            clean_session=True,
            protocol=mqtt.MQTTv31,
            transport="websockets",
        )
        # 握手头：**Cookie 是硬要求**，少了对端直接 403（实测）。
        headers = {
            "Origin": C.BASE_URL,
            "User-Agent": C.DEFAULT_HEADERS["User-Agent"],
        }
        if self.credentials.cookie:
            headers["Cookie"] = self.credentials.cookie
        client.ws_set_options(path=self.path, headers=headers)
        client.tls_set()
        client.username_pw_set(f"{self.credentials.token}|0", self.credentials.wt)
        # 重连间隔故意拉大：一帧一条连接（见 :meth:`send_text`），重连由
        # connect() 显式做，不让 paho 在后台按秒级节奏自己重连。
        client.reconnect_delay_set(
            min_delay=C.CHAT_RECONNECT_MIN, max_delay=C.CHAT_RECONNECT_MAX
        )
        return client

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        """服务端推送。

        除了记日志，还要**扒出里面的消息 id**：连上那一下服务端会推一帧会话
        同步（每个会话带着最后一条消息的 id），:meth:`send_text` 要拿这个当
        ``mid`` 的基数——用小了网关会判这帧非法（见模块头第 4 条）。
        """
        payload = bytes(message.payload)
        logger.debug("聊天推送：topic=%s %d 字节", message.topic, len(payload))
        found = max_message_id(payload)
        if found > self.max_msg_id:
            self.max_msg_id = found
            logger.debug("推送里的最大消息 id 抬到 %d", found)
        if found:
            self._seen_push.set()

    def _mid_base(self) -> int:
        """发消息时 ``mid`` 的基数：推送里见过的最大 id，没有就用兜底值。"""
        base = max(self.max_msg_id, C.CHAT_MID_FLOOR)
        logger.debug("mid 基数 = %d（推送见到 %d）", base, self.max_msg_id)
        return base

    def _on_publish(
        self,
        client: Any,
        userdata: Any,
        mid: int,
        reason_code: Any = None,
        props: Any = None,
    ) -> None:
        """broker 回 PUBACK 时的回调（**只是记录，不是判据**）。

        paho 的 ``VERSION2`` 回调签名是 ``(client, userdata, mid, reason_code,
        properties)``；``mid`` 就是 :meth:`paho.mqtt.client.Client.publish` 返回的
        ``info.mid``（包 id），所以能一帧一帧地对上。这条网关对文本帧通常不回
        PUBACK（见模块头），回执到不到都不影响 :meth:`send_text` 的成败判定。
        """
        self.published.append(mid)
        waiter = self._pubacks.get(mid)
        if waiter is not None:
            waiter.set()
        logger.debug("聊天 PUBACK：包 id=%s rc=%s", mid, reason_code)

    def _wait_puback(self, packet_id: int, wait: float | None = None) -> bool:
        """等某一帧的 PUBACK（最多 ``wait`` 秒，缺省用 :attr:`puback_wait`）。

        **只是记日志用**：等到了说明回执来了（非常态），等不到是常态，
        两种都不该改变 :meth:`send_text` 的返回。
        """
        waiter = self._pubacks.setdefault(packet_id, threading.Event())
        return waiter.wait(self.puback_wait if wait is None else wait)

    def connect(self) -> "ChatSocket":
        """建连并**等到 CONNACK**；失败抛 :class:`~boss_jobs.errors.ChatSendError`。

        **每次都重建**：一条连接发一帧就够（发完自己断），复用一个半死的
        socket 只会踩到「publish 进黑洞」。旧客户端先 :meth:`close` 掉，
        paho 的后台重连也就跟着停了。
        """
        creds = self.credentials
        if not creds.wt or not creds.token or not creds.user_id:
            raise ChatSendError(
                "聊天通道凭据不全（wt / token / userId），先重新登录再试"
            )
        with self._lock:
            if self.connected:
                return self
            self.close()
            self._connected.clear()
            self.last_connack = None
            # 新连接 = 新一批推送：上一轮**从推送里**抬到的基数作废，但
            # :attr:`_seed_mid_base`（上一条带过来的）留着——服务端重新推的
            # 会话同步基数一般还更大，真到了会再往上抬。没有它就得每条都
            # 重新等那帧推送，白烧 :data:`config.CHAT_PUSH_WAIT` 秒。
            self.max_msg_id = self._seed_mid_base
            self._seen_push.clear()
            self._client = self._build_client()
            logger.debug(
                "连聊天通道 wss://%s:%s%s（user=%s|0）",
                self.host,
                self.port,
                self.path,
                creds.token[:4] + "…" if creds.token else "",
            )
            started = time.monotonic()
            try:
                self._client.connect(self.host, self.port, keepalive=C.CHAT_KEEPALIVE)
                self._client.loop_start()
            except Exception as exc:  # noqa: BLE001 - 网络/paho 的异常都归一类
                self.close()
                raise ChatSendError(f"连聊天通道失败：{exc}") from exc

        if not self._connected.wait(self.timeout):
            self.close()
            raise ChatSendError(f"聊天通道 {self.timeout:g}s 内没收到 CONNACK")
        code = self.last_connack
        if int(getattr(code, "value", code if code is not None else -1)) != 0:
            self.close()
            raise ChatSendError(f"聊天通道被拒（CONNACK {code}）")
        self.connect_seconds = time.monotonic() - started
        logger.debug("聊天通道建连耗时 %.2fs", self.connect_seconds)
        return self

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, rc: Any, props: Any = None
    ) -> None:
        self.last_connack = rc
        if int(getattr(rc, "value", rc if rc is not None else -1)) == 0:
            # 站点连上就报一次在线，照着做（不报也发得出去，但保持一致）
            try:
                client.publish(
                    C.CHAT_TOPIC,
                    encode_presence(uid=self.credentials.user_id),
                    qos=1,
                    retain=True,
                )
            except Exception:  # noqa: BLE001 - presence 发不出去不该挡住正文
                logger.debug("presence 帧发送失败", exc_info=True)
            self._connected.set()
        else:
            logger.warning("聊天通道 CONNACK 非成功：%s", rc)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        flags: Any = None,
        reason_code: Any = None,
        props: Any = None,
    ) -> None:
        # paho 的 CallbackAPIVersion.VERSION2 给 on_disconnect 传
        # (client, userdata, disconnect_flags, reason_code, properties) 五个参。
        self._connected.clear()
        logger.debug("聊天通道断开：%s", reason_code)

    # ------------------------------------------------------------------ #

    def send_text(
        self,
        *,
        to_uid: int,
        text: str,
        to_source: int = C.CHAT_DEFAULT_SOURCE,
        to_name: str = "",
        quote_id: int = 0,
    ) -> int:
        """发一条文本消息，返回这条消息的 ``mid``。

        没连上会先 :meth:`connect`，然后**手头没基数才等那帧会话同步**（最多
        :attr:`push_wait` 秒）拿 ``mid`` 的基数，PUBLISH，等这帧写出 socket
        （:meth:`_flush_to_socket`）、主动断开。

        ``mid`` = 基数 + 当前毫秒，``time`` = 当前毫秒（站点就是
        ``getMaxMsgId() + Date.now()`` / ``Date.now()``）。**基数取服务端那个
        3.9e14 量级的消息 id**，跟站点同源。

        **判据是「帧发出去了」，不是「等到 PUBACK」**：这条网关对文本帧根本不回
        PUBACK，发完约 150ms 就把 WebSocket 关掉——这是它的常态，不是拒收
        （2026-10-08 实测：这么发的几发，站点会话列表里都出现了招呼语并标
        「[送达]」，重载页面还在）。所以 :attr:`puback_wait` 默认 0 = **整个
        不等**，只把「有回执」这种非常态记进日志（调大它才等），**等不到不算
        失败**。

        分段耗时记进 :attr:`last_send_stats`（``wait_push`` / ``publish`` /
        ``flush``），上层打一条汇总日志用。

        :raises ChatSendError: 没连上 / 凭据不全 / PUBLISH 直接抛 / ``rc != 0``
        """
        stats: dict[str, float] = {}
        if not self.connected:
            self.connect()
        # 等那帧会话同步落地——``mid`` 的基数就在里面。**手头已有基数就整个
        # 不等**（整批从上一条带过来的，见构造参数 ``mid_base``）：手里的
        # 基数已经够新，等推送只会再抬一点、不值那几秒。等不到就用兜底基数
        # （CHAT_MID_FLOOR），照样是服务端量级，只是不如真实 id 准。
        if self.max_msg_id == 0 and not self._seen_push.is_set():
            started = time.monotonic()
            self._seen_push.wait(self.push_wait)
            stats["wait_push"] = time.monotonic() - started
        else:
            stats["wait_push"] = 0.0
        when = int(time.time() * 1000)
        temp_id = self._mid_base() + when
        frame = encode_text_message(
            from_uid=self.credentials.user_id,
            to_uid=to_uid,
            text=text,
            to_source=to_source,
            to_name=to_name,
            temp_id=temp_id,
            time_ms=when,
            quote_id=quote_id,
        )
        started = time.monotonic()
        try:
            # retain 必须 False：站点前端传 true，但那样一条消息会在对方那里
            # 变成两条（留存的那份被收件人订阅/同步时再投一遍）。2026-10-09
            # 实测确认。见 :data:`config.CHAT_RETAIN`。
            info = self._client.publish(
                C.CHAT_TOPIC, frame, qos=1, retain=C.CHAT_RETAIN
            )
        except Exception as exc:  # noqa: BLE001 - paho 在断线时抛得五花八门
            self.close()
            raise ChatSendError(f"发聊天消息失败：{exc}") from exc
        if getattr(info, "rc", 0) != 0:
            self.close()
            raise ChatSendError(f"发聊天消息失败：broker 回 rc={info.rc}")
        stats["publish"] = time.monotonic() - started
        # 帧已经交给 socket 了（QoS1 的 PUBLISH，本地无异常）。
        # PUBACK 只是顺带看一眼：这条网关通常不回，还会顺手把连接关掉。
        # ``puback_wait <= 0``（默认）就整个跳过——实测回执从来不到，
        # 等它只是每条干烧 :data:`config.CHAT_PUBACK_WAIT` 秒。
        if self.puback_wait > 0:
            if self._wait_puback(info.mid, self.puback_wait):
                logger.debug("聊天消息 PUBACK 到了（包 id=%s）：%d 字节", info.mid, len(frame))
            else:
                logger.debug(
                    "聊天消息没等到 PUBACK（常态；网关随后会掐线）：mid=%s，%d 字节",
                    temp_id,
                    len(frame),
                )
        else:
            logger.debug(
                "聊天消息已交给 socket（%d 字节，mid=%s），不等 PUBACK",
                len(frame),
                temp_id,
            )
        # 把这帧真正写到 socket 上再断：``publish()`` 回 ``rc == 0`` 只是入队，
        # 写出去是 loop 线程干的。没写完就 close 会把这帧连同连接一起丢——
        # 而这条网关又不回 PUBACK，丢了只会表现成「会话建了、招呼语没了」。
        started = time.monotonic()
        self._flush_to_socket()
        stats["flush"] = time.monotonic() - started
        self.last_send_stats = stats
        self.close()
        return temp_id

    def _flush_to_socket(self) -> None:
        """等 PUBLISH 真正写到 socket 上（**不等 PUBACK**），写完就回。

        看 paho 的出站队列 ``_out_packet`` 排空（写完就出队）；拿不到这个
        属性（换版本 / 测试替身）就退回固定睡 :attr:`flush_wait`。整个等待
        不超过 :data:`config.CHAT_FLUSH_DEADLINE`。
        """
        queue = getattr(self._client, "_out_packet", None)
        if queue is None:
            self._sleep(self.flush_wait)
            return
        deadline = time.monotonic() + C.CHAT_FLUSH_DEADLINE
        while True:
            try:
                pending = len(queue)
            except Exception:  # noqa: BLE001 - 属性没了就当已写完
                break
            if pending == 0:
                break
            if time.monotonic() >= deadline:
                logger.warning("PUBLISH 还有 %d 帧没写出去就断连，这发可能丢", pending)
                break
            self._sleep(0.02)

    def close(self) -> None:
        """断开连接（幂等）。

        顺序是 ``disconnect()`` → ``loop_stop()``：先摘连接再停循环，
        免得后台线程在停的过程中又去重连（重连间隔已拉到 30s，正常也不会）。
        """
        client, self._client = self._client, None
        self._connected.clear()
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - 已经断了就别再抛
            logger.debug("disconnect 失败", exc_info=True)
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            logger.debug("loop_stop 失败", exc_info=True)

    def __enter__(self) -> "ChatSocket":
        return self.connect()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
