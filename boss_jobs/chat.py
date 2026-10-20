"""聊天通道：MQTT over WebSocket + 手写 protobuf 帧。

`friend/add` 只建会话不投递正文，招呼语走这里的 MQTT 通道。
协议细节与实测结论见 `notes/boss_jobs/chat.md`。
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
    """字段号 + varint 数值。"""
    return _varint(field << 3) + _varint(int(value))


def _sstr(field: int, value: str) -> bytes:
    """字段号 + 长度前缀字符串。"""
    raw = value.encode("utf-8")
    return _varint(field << 3 | 2) + _varint(len(raw)) + raw


def _sub(field: int, payload: bytes) -> bytes:
    """字段号 + 长度前缀嵌套消息。"""
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _iter_fields(buf: bytes):
    """极简 protobuf 遍历器：产出 `(字段号, wire, 值)`。

    wire 0 给 int，wire 2 给 bytes（不解嵌套）。
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
    """从偏移 i 读一个 varint，返回 `(值, 新偏移)`。"""
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

    取最大值当发消息时 `mid` 的基数；`CHAT_MID_RANGE` 之外的数字不认。
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
    """编一帧文本消息（`TechwolfChatProtocol` type=1）。

    Args:
        from_uid: 自己的数字 userId。
        to_uid: 对方数字 uid（不是 encryptBossId）。
        text: 正文。
        from_source: 自己的 source；字段必须出现，哪怕等于 0。
        to_source: 对方来源。
        to_name: 对方名字字段，站点塞 `encryptBossId`。
        temp_id: 这条消息的 `mid`；要落在服务端消息 id 数轴上。
        time_ms: 消息时间戳（毫秒），必须和 `mid` 分开。
        quote_id: 引用消息 id，0 = 不引用。

    Returns:
        帧字节。
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
        + _vint(3, 1)
        + _vint(4, stamp)
        + _vint(5, when)
        + _sub(6, body)
        + _vint(11, stamp)
    )
    if quote_id:
        message += _vint(20, quote_id)
    return _vint(1, C.CHAT_PROTO_MESSAGE) + _sub(3, message)


def encode_presence(*, uid: int, last_message_id: int = 0) -> bytes:
    """编一帧上线 presence（`TechwolfChatProtocol` type=2）。

    Args:
        uid: 自己的数字 userId。
        last_message_id: 本地最大消息 id，新连接传 0。

    Returns:
        帧字节。
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


@dataclass(frozen=True)
class ChatCredentials:
    """MQTT 接入凭据。

    Attributes:
        user_id: 自己的数字 userId。
        token: MQTT 用户名前缀。
        wt: MQTT 密码。
        cookie: 登录 Cookie 串；WebSocket 握手要带。
        name: 自己的昵称，仅日志用。
    """

    user_id: int
    token: str
    wt: str
    cookie: str = ""
    name: str = ""


class ChatSocket:
    """一条 MQTT over WSS 长连接，用来往 `chat` 主题发消息。

    发送成功判据是「帧发出去了」，不是「等到 PUBACK」——该网关对文本帧不回
    PUBACK。发完主动断开；重连由显式 `connect()` 做。

    Args:
        credentials: 见 `ChatCredentials`。
        host: MQTT 网关主机。
        port: MQTT 网关端口。
        path: WebSocket path。
        timeout: 等 CONNACK 的超时（秒）。
        push_wait: 等会话同步（`mid` 基数）的超时（秒）。
        puback_wait: 发完顺带等 PUBACK 的超时（秒）；等不到是常态。
        flush_wait: 出站队列拿不到时的兜底停顿（秒）。
        mid_base: 上一条带过来的 `mid` 基数，必须单调不减。
        client_factory: 造 paho `Client` 的工厂（测试里注入假的）。
        sleeper: 可替换的 sleep（测试里注入 no-op）。
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
        """初始化。"""
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
        self._pubacks: dict[int, threading.Event] = {}
        self.published: list[int] = []
        self.last_connack: Any = None
        self.max_msg_id: int = self._seed_mid_base
        self._seen_push = threading.Event()
        self.last_send_stats: dict[str, float] = {}
        self.connect_seconds: float = 0.0

    @property
    def connected(self) -> bool:
        """当前是否已建连并收到 CONNACK。"""
        return self._client is not None and self._connected.is_set()

    def _client_id(self) -> str:
        """生成随机 MQTT clientId。"""
        alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
        return "ws-" + "".join(random.choice(alphabet) for _ in range(16))

    def _build_client(self) -> Any:
        """造一个 paho 客户端（或测试注入的替身）并把回调接上。"""
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
        """构造真 paho 客户端；缺依赖时抛 `ChatSendError`。"""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
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
        headers = {
            "Origin": C.BASE_URL,
            "User-Agent": C.DEFAULT_HEADERS["User-Agent"],
        }
        if self.credentials.cookie:
            headers["Cookie"] = self.credentials.cookie
        client.ws_set_options(path=self.path, headers=headers)
        client.tls_set()
        client.username_pw_set(f"{self.credentials.token}|0", self.credentials.wt)
        client.reconnect_delay_set(
            min_delay=C.CHAT_RECONNECT_MIN, max_delay=C.CHAT_RECONNECT_MAX
        )
        return client

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        """服务端推送回调：记日志并抬高 `mid` 基数。"""
        payload = bytes(message.payload)
        logger.debug("聊天推送：topic=%s %d 字节", message.topic, len(payload))
        found = max_message_id(payload)
        if found > self.max_msg_id:
            self.max_msg_id = found
            logger.debug("推送里的最大消息 id 抬到 %d", found)
        if found:
            self._seen_push.set()

    def _mid_base(self) -> int:
        """发消息时 `mid` 的基数：推送里见过的最大 id，没有就用兜底值。"""
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
        """broker 回 PUBACK 时的回调；只是记录，不是判据。"""
        self.published.append(mid)
        waiter = self._pubacks.get(mid)
        if waiter is not None:
            waiter.set()
        logger.debug("聊天 PUBACK：包 id=%s rc=%s", mid, reason_code)

    def _wait_puback(self, packet_id: int, wait: float | None = None) -> bool:
        """等某一帧的 PUBACK；等不到是常态，只记日志用。"""
        waiter = self._pubacks.setdefault(packet_id, threading.Event())
        return waiter.wait(self.puback_wait if wait is None else wait)

    def connect(self) -> "ChatSocket":
        """建连并等到 CONNACK；失败抛 `ChatSendError`。

        每次都重建 paho 客户端，旧的先 `close` 掉。
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
        """CONNACK 回调；成功时报一帧 presence。"""
        self.last_connack = rc
        if int(getattr(rc, "value", rc if rc is not None else -1)) == 0:
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
        """断开回调：清连接标记并记日志。"""
        self._connected.clear()
        logger.debug("聊天通道断开：%s", reason_code)

    def send_text(
        self,
        *,
        to_uid: int,
        text: str,
        to_source: int = C.CHAT_DEFAULT_SOURCE,
        to_name: str = "",
        quote_id: int = 0,
    ) -> int:
        """发一条文本消息，返回这条消息的 `mid`。

        没连上会先 `connect()`；手头没基数才等会话同步。PUBLISH 后等帧写到
        socket 再主动断开。分段耗时记进 `last_send_stats`。
        失败抛 `ChatSendError`（没连上 / 凭据不全 / PUBLISH 报错）。
        """
        stats: dict[str, float] = {}
        if not self.connected:
            self.connect()
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
        started = time.monotonic()
        self._flush_to_socket()
        stats["flush"] = time.monotonic() - started
        self.last_send_stats = stats
        self.close()
        return temp_id

    def _flush_to_socket(self) -> None:
        """等 PUBLISH 真正写到 socket 上（不等 PUBACK），写完就回。"""
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
        """断开连接（幂等）：先 `disconnect()` 再 `loop_stop()`。"""
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
        """建连并返回自身。"""
        return self.connect()

    def __exit__(self, *exc_info: Any) -> None:
        """退出时断开。"""
        self.close()
