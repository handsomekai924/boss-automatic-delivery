"""聊天通道：protobuf 帧 / 推送里扒 ``mid`` / MQTT 发送编排。

**不打真实网关**：paho 客户端用假的替身注入，只验本模块自己的两点——
① 帧编得跟站点逐字节对齐（``from.source`` 必须在、``mid``/``time`` 分开）；
② 发送判据是「帧发出去了」，**不是等 PUBACK**（这条网关对文本帧不回 PUBACK，
   等不到是常态，见 :mod:`boss_jobs.chat` 模块头）。
"""

from __future__ import annotations

import pytest

from boss_jobs import chat
from boss_jobs import config as C
from boss_jobs.chat import ChatCredentials, ChatSocket, encode_text_message, max_message_id
from boss_jobs.client import JobClient
from boss_jobs.errors import ChatSendError


# --------------------------------------------------------------------------- #
# 假 paho 客户端 / 取字段
# --------------------------------------------------------------------------- #


class _Rc:
    def __init__(self, value: int) -> None:
        self.value = value


class _Info:
    def __init__(self, mid: int, rc: int = 0) -> None:
        self.mid = mid
        self.rc = rc


class FakePahoClient:
    """够 :class:`ChatSocket` 用的一版 paho 替身。

    ``connect`` 只记参数；``loop_start`` 里**回调 CONNACK**（rc 可指定），
    对齐真实 paho「connect 之后回调 on_connect」的时序。
    ``publish`` 默认**回 PUBACK**（``puback=True``）；把它关掉就模拟这条网关
    的真实行为——帧收下、不回执。
    """

    def __init__(
        self,
        *,
        connack_rc: int = 0,
        puback: bool = True,
        publish_rc: int = 0,
        raise_on_publish: Exception | None = None,
    ) -> None:
        self.connack_rc = connack_rc
        self.puback = puback
        self.publish_rc = publish_rc
        self.raise_on_publish = raise_on_publish
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.on_publish = None
        self.connect_args: tuple | None = None
        self.published: list[tuple] = []
        self.disconnected = False
        self.loop_stopped = False
        self._mid = 0

    def connect(self, host, port, keepalive=60):
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        if self.on_connect is not None:
            self.on_connect(self, None, {}, _Rc(self.connack_rc), None)

    def publish(self, topic, payload, qos=0, retain=False):
        self._mid += 1
        self.published.append((topic, payload, qos, retain))
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        if self.puback and self.on_publish is not None:
            self.on_publish(self, None, self._mid, None, None)
        return _Info(self._mid, self.publish_rc)

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        self.loop_stopped = True


CREDS = ChatCredentials(user_id=555, token="tok", wt="wt", cookie="a=b", name="我")


def _make_socket(client: FakePahoClient, **kw) -> ChatSocket:
    defaults = dict(
        timeout=0.5,
        push_wait=0.0,
        puback_wait=0.0,
        flush_wait=0.0,
        client_factory=lambda: client,
        sleeper=lambda _s: None,
    )
    defaults.update(kw)
    return ChatSocket(CREDS, **defaults)


def _fields(frame: bytes):
    return list(chat._iter_fields(frame))


def _sub_field(frame: bytes, field: int, wire: int = 2):
    for f, w, v in _fields(frame):
        if f == field and w == wire:
            return v
    return None


def _message(frame: bytes) -> bytes:
    """取出 ``TechwolfChatProtocol`` 里那条 ``TechwolfMessage``（字段 3，wire 2）。"""
    msg = _sub_field(frame, 3)
    assert msg is not None
    return msg


def _message_field(frame: bytes, field: int, wire: int = 0):
    """从 ``TechwolfChatProtocol`` 里取出那条 ``TechwolfMessage`` 的某字段。"""
    for f, w, v in chat._iter_fields(_message(frame)):
        if f == field and w == wire:
            return v
    return None


def _sync_push(*, uid: int, last_mid: int) -> bytes:
    """造一帧「连上后服务端推的会话同步」——字段 3 是条目，字段 4 是消息 id。"""
    entry = chat._vint(1, uid) + chat._vint(4, last_mid)
    return chat._vint(1, C.CHAT_PROTO_MESSAGE) + chat._sub(3, entry)


# --------------------------------------------------------------------------- #
# 帧编码
# --------------------------------------------------------------------------- #


def test_帧的_from_必须带_source_哪怕等于0():
    """``from.source`` 缺了网关判非法（模块头第 4 条）——原样写出来。"""
    frame = encode_text_message(from_uid=1, to_uid=2, text="你好", from_source=0)
    from_user = _sub_field(_message(frame), 1)
    assert from_user is not None
    pairs = {f: v for f, w, v in chat._iter_fields(from_user) if w == 0}
    assert pairs.get(1) == 1  # uid
    assert 7 in pairs and pairs[7] == 0  # source 显式写了


def test_帧的_mid_和_time_是两个不同的数():
    """``mid`` = 服务端数轴上的号，``time`` = 纯毫秒，别混成一个。"""
    frame = encode_text_message(
        from_uid=1, to_uid=2, text="你好", temp_id=394570_000_000_000, time_ms=1_790_000_000_000
    )
    assert _message_field(frame, 4) == 394570_000_000_000  # mid
    assert _message_field(frame, 5) == 1_790_000_000_000  # time
    assert _message_field(frame, 11) == 394570_000_000_000  # cmid = mid


def test_帧的_to_带_uid_source_与可选_name():
    frame = encode_text_message(
        from_uid=1, to_uid=2, text="你好", to_source=9, to_name="EB1"
    )
    to_user = _sub_field(_message(frame), 2)
    ints = {f: v for f, w, v in chat._iter_fields(to_user) if w == 0}
    strs = {f: v for f, w, v in chat._iter_fields(to_user) if w == 2}
    assert ints.get(1) == 2 and ints.get(7) == 9
    assert strs.get(2) == b"EB1"


def test_帧的_to_name_空串就不写这个字段():
    frame = encode_text_message(from_uid=1, to_uid=2, text="你好")
    to_user = _sub_field(_message(frame), 2)
    assert all(f != 2 or w != 2 for f, w, _ in chat._iter_fields(to_user))


def test_帧的_quoteId_有才写():
    plain = encode_text_message(from_uid=1, to_uid=2, text="你好")
    quoted = encode_text_message(from_uid=1, to_uid=2, text="你好", quote_id=99)
    assert _message_field(plain, 20) is None
    assert _message_field(quoted, 20) == 99


def test_帧的正文在_body_text_里():
    frame = encode_text_message(from_uid=1, to_uid=2, text="打个招呼")
    body = _message_field(frame, 6, 2)
    assert body is not None
    texts = {f: v for f, w, v in chat._iter_fields(body) if w == 2}
    assert texts.get(3) == "打个招呼".encode("utf-8")


@pytest.mark.parametrize(
    "kw",
    [
        {"from_uid": 0, "to_uid": 2, "text": "x"},
        {"from_uid": 1, "to_uid": 0, "text": "x"},
        {"from_uid": 1, "to_uid": 2, "text": ""},
    ],
)
def test_帧缺必填直接报错(kw):
    with pytest.raises(ValueError):
        encode_text_message(**kw)


def test_presence_帧形状():
    frame = chat.encode_presence(uid=555)
    assert _sub_field(frame, 1, 0) == C.CHAT_PROTO_PRESENCE
    presence = _sub_field(frame, 4)
    ints = {f: v for f, w, v in chat._iter_fields(presence) if w == 0}
    assert ints.get(2) == 555


# --------------------------------------------------------------------------- #
# 从推送里扒 mid
# --------------------------------------------------------------------------- #


def test_max_message_id_从会话同步里取最大():
    push = _sync_push(uid=1, last_mid=394570988736768)
    assert max_message_id(push) == 394570988736768


def test_max_message_id_多条取最大():
    frame = (
        chat._vint(1, C.CHAT_PROTO_MESSAGE)
        + chat._sub(3, chat._vint(1, 1) + chat._vint(4, 394000000000001))
        + chat._sub(3, chat._vint(1, 2) + chat._vint(4, 394000000000009))
    )
    assert max_message_id(frame) == 394000000000009


def test_max_message_id_区间外的数字不认():
    """``CHAT_MID_RANGE`` 之外（比如毫秒时间戳）不当 id，免得把基数弄乱。"""
    frame = chat._vint(1, 1) + chat._sub(3, chat._vint(4, 1_790_000_000_000))
    assert max_message_id(frame) == 0


def test_max_message_id_垃圾字节不抛():
    assert max_message_id(b"\xff\xff\xff") == 0


# --------------------------------------------------------------------------- #
# ChatSocket 发送编排
# --------------------------------------------------------------------------- #


def test_connect_缺凭据直接报错():
    sock = ChatSocket(ChatCredentials(user_id=0, token="", wt=""))
    with pytest.raises(ChatSendError):
        sock.connect()


def test_connect_connack_非成功抛():
    paho = FakePahoClient(connack_rc=5)
    sock = _make_socket(paho)
    with pytest.raises(ChatSendError) as ei:
        sock.connect()
    assert "CONNACK" in str(ei.value)
    assert paho.disconnected  # 失败要把连接收干净


def test_connect_成功时报一帧_presence():
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.connect()
    assert sock.connected
    topics = [t for t, *_ in paho.published]
    assert topics == [C.CHAT_TOPIC]  # 连上就报在线


def test_没等到_puback_照样算成功():
    """**核心**：这条网关对文本帧不回 PUBACK，那不是失败。

    帧发出去了（PUBLISH 无异常、rc=0）就该正常返回 ``mid``，
    不能因为等不到回执把成功报成失败。
    """
    paho = FakePahoClient(puback=False)
    sock = _make_socket(paho)
    mid = sock.send_text(to_uid=777, text="您好，方便聊聊吗？")
    assert mid > C.CHAT_MID_FLOOR  # 落在服务端数轴上
    assert sock.published == []  # 一个 PUBACK 都没等到
    assert paho.disconnected  # 发完自己断


def test_等到_puback_只记日志不改判据():
    paho = FakePahoClient(puback=True)
    sock = _make_socket(paho)
    mid = sock.send_text(to_uid=777, text="您好")
    assert mid > 0
    # 两发 PUBACK 都到了：presence 一发、正文一发（记的是 paho 包 id）
    assert len(sock.published) == 2


def test_正文帧不带_retain_标志():
    """正文帧 retain=False（**不是**站点前端的 true）——2026-10-09 定案。

    retain=true 会让 broker 把这一帧留在 ``chat`` 主题上，收件人订阅/同步时
    再收到一遍，实时一份 + 留存一份 = 两条。见 :data:`config.CHAT_RETAIN`。
    """
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.send_text(to_uid=777, text="您好")
    # presence 是第一发（随站点 retain=true）；正文那一发 retain 必须是 False
    assert paho.published[0][3] is True
    assert paho.published[-1][3] is False


def test_发送用推送里的最大_id_当基数():
    """``mid`` 的基数取自会话同步那帧的最大消息 id（站点同款算式）。"""
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.connect()
    last = 394570988736768
    sock._on_message(None, None, type("M", (), {"topic": "chat", "payload": _sync_push(uid=1, last_mid=last)})())
    mid = sock.send_text(to_uid=777, text="您好")
    # 基数取自 last；再往上加的「当前毫秒」是 1.7e12 量级，留足余量
    assert last < mid < last + 10**13


def test_publish_抛异常包成_ChatSendError():
    paho = FakePahoClient(raise_on_publish=RuntimeError("socket 已关"))
    sock = _make_socket(paho)
    with pytest.raises(ChatSendError) as ei:
        sock.send_text(to_uid=777, text="您好")
    assert "socket 已关" in str(ei.value)


def test_publish_回非零_rc_抛():
    paho = FakePahoClient(publish_rc=4)
    sock = _make_socket(paho)
    with pytest.raises(ChatSendError) as ei:
        sock.send_text(to_uid=777, text="您好")
    assert "rc=4" in str(ei.value)


def test_close_幂等():
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.connect()
    sock.close()
    sock.close()  # 再关一次不该抛
    assert sock.connected is False


# --------------------------------------------------------------------------- #
# JobClient.deliver_greeting：建会话 → 换 uid → 发正文
# --------------------------------------------------------------------------- #


class FakeHttp:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = None

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0)
        return type("R", (), {"status_code": 200, "json": lambda self_: item})()


class FakeChat:
    """够 ``deliver_greeting`` 用的聊天替身。"""

    connected = True

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_text(self, **kwargs) -> int:
        self.sent.append(kwargs)
        return 394570_000_000_001


def test_deliver_greeting_建会话后单独发正文(monkeypatch):
    http = FakeHttp(
        [
            {"code": 0, "message": "Success", "zpData": {}},  # friend/add
            {
                "code": 0,
                "message": "Success",
                "zpData": {
                    "data": {
                        "bossId": 888,
                        "bossSource": 3,
                        "name": "张女士",
                        "encryptBossId": "EB1",
                    }
                },
            },  # getBossData
        ]
    )
    client = JobClient(http=http)
    fake_chat = FakeChat()
    monkeypatch.setattr(client, "open_chat", lambda *a, **k: fake_chat)

    out = client.deliver_greeting(
        security_id="SEC1",
        encrypt_job_id="J1",
        encrypt_boss_id="EB1",
        greeting="  您好，方便聊聊吗？  ",
    )
    # 两步都打了：先 friend/add，再 getBossData
    assert any("friend/add" in c["url"] for c in http.calls)
    assert any("getBossData" in c["url"] for c in http.calls)
    # 正文经聊天通道单独发，uid/source 来自 getBossData
    assert len(fake_chat.sent) == 1
    assert fake_chat.sent[0]["to_uid"] == 888
    assert fake_chat.sent[0]["to_source"] == 3
    assert fake_chat.sent[0]["text"] == "您好，方便聊聊吗？"  # 首尾空白已 strip
    assert out.boss_uid == 888 and out.text == "您好，方便聊聊吗？"


def test_deliver_greeting_没正文不发(monkeypatch):
    client = JobClient(http=FakeHttp([]))
    monkeypatch.setattr(client, "open_chat", lambda *a, **k: FakeChat())
    with pytest.raises(ChatSendError):
        client.deliver_greeting(
            security_id="SEC1", encrypt_job_id="J1", encrypt_boss_id="EB1", greeting="   "
        )
