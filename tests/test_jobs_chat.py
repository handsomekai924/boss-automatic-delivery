"""聊天通道：protobuf 帧 / 推送里扒 `mid` / MQTT 发送编排。

不打真实网关：paho 客户端用假的替身注入，只验本模块自己的两点——
① 帧编得跟站点逐字节对齐（`from.source` 必须在、`mid`/`time` 分开）；
② 发送判据是「帧发出去了」，不是等 PUBACK。
"""

from __future__ import annotations

import sys

import pytest

from boss_jobs import chat
from boss_jobs import config as C
from boss_jobs.chat import ChatCredentials, ChatSocket, encode_text_message, max_message_id
from boss_jobs.client import JobClient
from boss_jobs.errors import ChatSendError


class _Rc:
    """paho 返回码桩。"""

    def __init__(self, value: int) -> None:
        """初始化。"""
        self.value = value


class _Info:
    """paho publish 结果桩。"""

    def __init__(self, mid: int, rc: int = 0) -> None:
        """初始化。"""
        self.mid = mid
        self.rc = rc


class FakePahoClient:
    """够 `ChatSocket` 用的一版 paho 替身。

    `connect` 只记参数；`loop_start` 里回调 CONNACK（rc 可指定），
    对齐真实 paho「connect 之后回调 on_connect」的时序。
    `publish` 默认回 PUBACK（`puback=True`）；把它关掉就模拟这条网关
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
        """初始化。"""
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
        """记录连接参数。"""
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        """回调 CONNACK。"""
        if self.on_connect is not None:
            self.on_connect(self, None, {}, _Rc(self.connack_rc), None)

    def publish(self, topic, payload, qos=0, retain=False):
        """记录一次发布并按配置回 PUBACK。"""
        self._mid += 1
        self.published.append((topic, payload, qos, retain))
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        if self.puback and self.on_publish is not None:
            self.on_publish(self, None, self._mid, None, None)
        return _Info(self._mid, self.publish_rc)

    def disconnect(self):
        """标记已断开。"""
        self.disconnected = True

    def loop_stop(self):
        """标记 loop 已停。"""
        self.loop_stopped = True


CREDS = ChatCredentials(user_id=555, token="tok", wt="wt", cookie="a=b", name="我")


def _make_socket(client: FakePahoClient, **kw) -> ChatSocket:
    """用给定假客户端造一个 `ChatSocket`。"""
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
    """展开一帧里的字段列表。"""
    return list(chat._iter_fields(frame))


def _sub_field(frame: bytes, field: int, wire: int = 2):
    """取出指定编号的子字段。"""
    for f, w, v in _fields(frame):
        if f == field and w == wire:
            return v
    return None


def _message(frame: bytes) -> bytes:
    """取出 `TechwolfChatProtocol` 里那条 `TechwolfMessage`（字段 3，wire 2）。"""
    msg = _sub_field(frame, 3)
    assert msg is not None
    return msg


def _message_field(frame: bytes, field: int, wire: int = 0):
    """从 `TechwolfMessage` 里取出某字段。"""
    for f, w, v in chat._iter_fields(_message(frame)):
        if f == field and w == wire:
            return v
    return None


def _sync_push(*, uid: int, last_mid: int) -> bytes:
    """造一帧「连上后服务端推的会话同步」——字段 3 是条目，字段 4 是消息 id。"""
    entry = chat._vint(1, uid) + chat._vint(4, last_mid)
    return chat._vint(1, C.CHAT_PROTO_MESSAGE) + chat._sub(3, entry)


def test_帧的_from_必须带_source_哪怕等于0():
    """`from.source` 缺了网关判非法——原样写出来。"""
    frame = encode_text_message(from_uid=1, to_uid=2, text="你好", from_source=0)
    from_user = _sub_field(_message(frame), 1)
    assert from_user is not None
    pairs = {f: v for f, w, v in chat._iter_fields(from_user) if w == 0}
    assert pairs.get(1) == 1
    assert 7 in pairs and pairs[7] == 0


def test_帧的_mid_和_time_是两个不同的数():
    """`mid` 是服务端数轴上的号，`time` 是纯毫秒，不混用。"""
    frame = encode_text_message(
        from_uid=1, to_uid=2, text="你好", temp_id=394570_000_000_000, time_ms=1_790_000_000_000
    )
    assert _message_field(frame, 4) == 394570_000_000_000
    assert _message_field(frame, 5) == 1_790_000_000_000
    assert _message_field(frame, 11) == 394570_000_000_000


def test_帧的_to_带_uid_source_与可选_name():
    """`to` 带 uid/source 与可选 name。"""
    frame = encode_text_message(
        from_uid=1, to_uid=2, text="你好", to_source=9, to_name="EB1"
    )
    to_user = _sub_field(_message(frame), 2)
    ints = {f: v for f, w, v in chat._iter_fields(to_user) if w == 0}
    strs = {f: v for f, w, v in chat._iter_fields(to_user) if w == 2}
    assert ints.get(1) == 2 and ints.get(7) == 9
    assert strs.get(2) == b"EB1"


def test_帧的_to_name_空串就不写这个字段():
    """空 `to_name` 不写该字段。"""
    frame = encode_text_message(from_uid=1, to_uid=2, text="你好")
    to_user = _sub_field(_message(frame), 2)
    assert all(f != 2 or w != 2 for f, w, _ in chat._iter_fields(to_user))


def test_帧的_quoteId_有才写():
    """`quoteId` 有值才写。"""
    plain = encode_text_message(from_uid=1, to_uid=2, text="你好")
    quoted = encode_text_message(from_uid=1, to_uid=2, text="你好", quote_id=99)
    assert _message_field(plain, 20) is None
    assert _message_field(quoted, 20) == 99


def test_帧的正文在_body_text_里():
    """正文落在 `body.text` 字段。"""
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
    """缺任一必填字段都抛 `ValueError`。"""
    with pytest.raises(ValueError):
        encode_text_message(**kw)


def test_presence_帧形状():
    """presence 帧类型与 uid 落在对应字段。"""
    frame = chat.encode_presence(uid=555)
    assert _sub_field(frame, 1, 0) == C.CHAT_PROTO_PRESENCE
    presence = _sub_field(frame, 4)
    ints = {f: v for f, w, v in chat._iter_fields(presence) if w == 0}
    assert ints.get(2) == 555


def test_max_message_id_从会话同步里取最大():
    """会话同步帧里取最大消息 id。"""
    push = _sync_push(uid=1, last_mid=394570988736768)
    assert max_message_id(push) == 394570988736768


def test_max_message_id_多条取最大():
    """多条目取最大 id。"""
    frame = (
        chat._vint(1, C.CHAT_PROTO_MESSAGE)
        + chat._sub(3, chat._vint(1, 1) + chat._vint(4, 394000000000001))
        + chat._sub(3, chat._vint(1, 2) + chat._vint(4, 394000000000009))
    )
    assert max_message_id(frame) == 394000000000009


def test_max_message_id_区间外的数字不认():
    """`CHAT_MID_RANGE` 之外的数字不当 id。"""
    frame = chat._vint(1, 1) + chat._sub(3, chat._vint(4, 1_790_000_000_000))
    assert max_message_id(frame) == 0


def test_max_message_id_垃圾字节不抛():
    """垃圾字节返回 0，不抛。"""
    assert max_message_id(b"\xff\xff\xff") == 0


def test_connect_缺凭据直接报错():
    """缺凭据时 `connect` 抛 `ChatSendError`。"""
    sock = ChatSocket(ChatCredentials(user_id=0, token="", wt=""))
    with pytest.raises(ChatSendError):
        sock.connect()


def test_connect_connack_非成功抛():
    """CONNACK 非成功抛错并收干净连接。"""
    paho = FakePahoClient(connack_rc=5)
    sock = _make_socket(paho)
    with pytest.raises(ChatSendError) as ei:
        sock.connect()
    assert "CONNACK" in str(ei.value)
    assert paho.disconnected


def test_connect_成功时报一帧_presence():
    """连接成功时报一帧 presence。"""
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.connect()
    assert sock.connected
    topics = [t for t, *_ in paho.published]
    assert topics == [C.CHAT_TOPIC]


def test_没等到_puback_照样算成功():
    """帧发出去了就算成功，不等 PUBACK。"""
    paho = FakePahoClient(puback=False)
    sock = _make_socket(paho, puback_wait=0.01)
    mid = sock.send_text(to_uid=777, text="您好，方便聊聊吗？")
    assert mid > C.CHAT_MID_FLOOR
    assert sock.published == []
    assert paho.disconnected


def test_等到_puback_只记日志不改判据():
    """等到 PUBACK 也不改发送判据。"""
    paho = FakePahoClient(puback=True)
    sock = _make_socket(paho, puback_wait=0.01)
    mid = sock.send_text(to_uid=777, text="您好")
    assert mid > 0
    assert len(sock.published) == 2


def test_puback_wait_为0就整个不等():
    """默认 `config.CHAT_PUBACK_WAIT` = 0：整个不等回执。"""
    paho = FakePahoClient(puback=False)
    sock = _make_socket(paho)
    waited: list = []
    sock._wait_puback = lambda *a, **k: waited.append(a) or False
    sock.send_text(to_uid=777, text="您好")
    assert waited == []


def test_send_text_记下分段耗时():
    """分段耗时给上层打「时间花在哪」的日志。"""
    sock = _make_socket(FakePahoClient())
    sock.send_text(to_uid=777, text="您好")
    assert set(sock.last_send_stats) == {"wait_push", "publish", "flush"}


def test_发完等出站队列写空才断():
    """发完等出站队列写空再断，避免丢帧。"""
    paho = FakePahoClient(puback=False)
    paho._out_packet = ["p1", "p2"]

    def sleeper(_seconds):
        """睡一觉写出一帧。"""
        if paho._out_packet:
            paho._out_packet.pop()

    sock = _make_socket(paho, sleeper=sleeper)
    sock.send_text(to_uid=777, text="您好")
    assert paho._out_packet == []
    assert paho.disconnected


def test_没有出站队列就退回兜底停顿():
    """拿不到 `_out_packet` 时退回睡 `flush_wait`。"""
    paho = FakePahoClient()
    slept: list = []
    sock = _make_socket(paho, flush_wait=0.25, sleeper=lambda s: slept.append(s))
    sock.send_text(to_uid=777, text="您好")
    assert slept == [0.25]


def test_缺_paho_依赖时报一句能照做的提示(monkeypatch):
    """缺依赖时说清装什么，不只回「No module named」。"""
    sock = _make_socket(FakePahoClient())
    for name in [m for m in list(sys.modules) if m == "paho" or m.startswith("paho.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "paho", None)
    with pytest.raises(ChatSendError) as ei:
        sock._make_paho_client()
    assert "paho-mqtt" in str(ei.value)


def test_正文帧不带_retain_标志():
    """正文帧 `retain=False`，presence 才是 `True`。"""
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.send_text(to_uid=777, text="您好")
    assert paho.published[0][3] is True
    assert paho.published[-1][3] is False


def test_发送用推送里的最大_id_当基数():
    """`mid` 基数取自会话同步那帧的最大消息 id。"""
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.connect()
    last = 394570988736768
    sock._on_message(None, None, type("M", (), {"topic": "chat", "payload": _sync_push(uid=1, last_mid=last)})())
    mid = sock.send_text(to_uid=777, text="您好")
    assert last < mid < last + 10**13


class SpyEvent:
    """记下 `wait()` 收了几个超时，用来验「到底等没等」。"""

    def __init__(self) -> None:
        """初始化。"""
        self.waits: list = []
        self._set = False

    def is_set(self) -> bool:
        """返回是否已置位。"""
        return self._set

    def set(self) -> None:
        """置位。"""
        self._set = True

    def clear(self) -> None:
        """清位。"""
        self._set = False

    def wait(self, timeout=None):
        """记录等待并返回当前置位状态。"""
        self.waits.append(timeout)
        return self._set


def test_手头没基数才等会话同步():
    """第一条没基数，等那帧推送。"""
    sock = _make_socket(FakePahoClient(), push_wait=4.0)
    sock._seen_push = SpyEvent()
    sock.send_text(to_uid=777, text="您好")
    assert sock._seen_push.waits == [4.0]


def test_有基数就不再等那帧会话同步():
    """有基数就不再等会话同步。"""
    sock = _make_socket(FakePahoClient(), push_wait=4.0, mid_base=394570988736768)
    sock._seen_push = SpyEvent()
    sock.send_text(to_uid=777, text="您好")
    assert sock._seen_push.waits == []


def test_mid_直接用带过来的基数():
    """带过来的基数落在服务端数轴上，等不到推送也够用。"""
    last = 394570988736768
    sock = _make_socket(FakePahoClient(), mid_base=last)
    mid = sock.send_text(to_uid=777, text="您好")
    assert last < mid < last + 10**13


def test_重建连接不清掉带过来的基数():
    """`connect` 只作废「从推送里抬到的」，种子基数要留着（单调不减）。"""
    seed = 394570988736768
    sock = _make_socket(FakePahoClient(), mid_base=seed)
    sock.connect()
    assert sock.max_msg_id == seed
    sock._on_message(
        None, None, type("M", (), {"topic": "chat", "payload": _sync_push(uid=1, last_mid=seed + 5)})()
    )
    assert sock.max_msg_id == seed + 5
    sock.close()
    sock.connect()
    assert sock.max_msg_id == seed


def test_publish_抛异常包成_ChatSendError():
    """publish 异常包成 `ChatSendError`。"""
    paho = FakePahoClient(raise_on_publish=RuntimeError("socket 已关"))
    sock = _make_socket(paho)
    with pytest.raises(ChatSendError) as ei:
        sock.send_text(to_uid=777, text="您好")
    assert "socket 已关" in str(ei.value)


def test_publish_回非零_rc_抛():
    """publish 回非零 rc 抛错。"""
    paho = FakePahoClient(publish_rc=4)
    sock = _make_socket(paho)
    with pytest.raises(ChatSendError) as ei:
        sock.send_text(to_uid=777, text="您好")
    assert "rc=4" in str(ei.value)


def test_close_幂等():
    """`close` 幂等。"""
    paho = FakePahoClient()
    sock = _make_socket(paho)
    sock.connect()
    sock.close()
    sock.close()
    assert sock.connected is False


class FakeHttp:
    """假 HTTP 会话：按序吐预置响应并记下调用。"""

    def __init__(self, responses) -> None:
        """初始化。"""
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.cookies = None

    def request(self, method, url, **kwargs):
        """记一次请求并返回下一个预置响应。"""
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0)
        return type("R", (), {"status_code": 200, "json": lambda self_: item})()


class FakeChat:
    """够 `deliver_greeting` 用的聊天替身。"""

    connected = True

    def __init__(self) -> None:
        """初始化。"""
        self.sent: list[dict] = []

    def send_text(self, **kwargs) -> int:
        """记录一次正文发送。"""
        self.sent.append(kwargs)
        return 394570_000_000_001


def test_deliver_greeting_建会话后单独发正文(monkeypatch):
    """建会话后经聊天通道单独发正文。"""
    http = FakeHttp(
        [
            {"code": 0, "message": "Success", "zpData": {}},
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
            },
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
    assert any("friend/add" in c["url"] for c in http.calls)
    assert any("getBossData" in c["url"] for c in http.calls)
    assert len(fake_chat.sent) == 1
    assert fake_chat.sent[0]["to_uid"] == 888
    assert fake_chat.sent[0]["to_source"] == 3
    assert fake_chat.sent[0]["text"] == "您好，方便聊聊吗？"
    assert out.boss_uid == 888 and out.text == "您好，方便聊聊吗？"


def test_deliver_greeting_没正文不发(monkeypatch):
    """没正文不发。"""
    client = JobClient(http=FakeHttp([]))
    monkeypatch.setattr(client, "open_chat", lambda *a, **k: FakeChat())
    with pytest.raises(ChatSendError):
        client.deliver_greeting(
            security_id="SEC1", encrypt_job_id="J1", encrypt_boss_id="EB1", greeting="   "
        )


class StubChatSocket:
    """够 `open_chat` 用：记构造参数，connect/close 只翻连接状态。"""

    def __init__(self, credentials, **kw) -> None:
        """初始化。"""
        self.credentials = credentials
        self.kwargs = kw
        self.max_msg_id = int(kw.get("mid_base") or 0)
        self.connected = False

    def connect(self):
        """标记已连接。"""
        self.connected = True
        return self

    def close(self):
        """标记已断开。"""
        self.connected = False


def _stub_chat_client(monkeypatch):
    """造一个用 `StubChatSocket` 的 `JobClient`。"""
    client = JobClient(http=FakeHttp([]))
    fetched: list = []
    monkeypatch.setattr(client, "fetch_me", lambda: fetched.append(1) or CREDS)
    monkeypatch.setattr("boss_jobs.client.ChatSocket", StubChatSocket)
    return client, fetched


def test_open_chat_断了重建时凭据只取一次(monkeypatch):
    """发完就断（网关常态），但凭据整批只取一份。"""
    client, fetched = _stub_chat_client(monkeypatch)
    first = client.open_chat()
    first.close()
    second = client.open_chat()
    assert len(fetched) == 1
    assert second.credentials is first.credentials


def test_open_chat_把上一条的_mid_基数带给下一条(monkeypatch):
    """一帧一条连接；基数要跨条带过去。"""
    client, _ = _stub_chat_client(monkeypatch)
    first = client.open_chat()
    first.max_msg_id = 394570988736768
    first.close()
    second = client.open_chat()
    assert second.kwargs["mid_base"] == 394570988736768


def test_open_chat_基数只往上抬不往下走(monkeypatch):
    """网关按「id 太旧」毙帧：基数必须单调不减。"""
    client, _ = _stub_chat_client(monkeypatch)
    first = client.open_chat()
    first.max_msg_id = 394570988736768
    first.close()
    second = client.open_chat()
    second.max_msg_id = 394570988736700
    second.close()
    third = client.open_chat()
    assert third.kwargs["mid_base"] == 394570988736768


def test_close_chat_收尾也把基数留下(monkeypatch):
    """任务收尾 `close_chat` 之后接着发，基数同样不能丢。"""
    client, _ = _stub_chat_client(monkeypatch)
    chat = client.open_chat()
    chat.max_msg_id = 394570988736768
    client.close_chat()
    again = client.open_chat()
    assert again.kwargs["mid_base"] == 394570988736768
