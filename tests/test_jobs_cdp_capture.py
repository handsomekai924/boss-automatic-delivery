"""``boss_jobs.cdp_capture`` 的测试。

运行：``python -m pytest tests/test_jobs_cdp_capture.py -q``

这里**不起真的 CDP 服务端**（全仓库都没有这个先例）。做法是把传输层和事件管线
拆开测：:class:`CaptureSession` 的 ``call`` 参数就是「发命令拿回包」的函数，测试
塞个按方法名回罐头的 :class:`FakeCall`，再用**合成的 CDP 事件**喂
``handle_event()``，最后读回磁盘上的产物断言。

传输层（:class:`boss_jobs.cdp_capture.CdpConnection`）另外用一个普通对象
:class:`FakeWs` 顶替 socket —— 它只是「按脚本吐字符串的对象」，不是网络服务端。
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import pytest
import websocket

from boss_jobs import cdp_capture as CC


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #


class FakeCall:
    """冒充 ``conn.send_call``：按方法名回罐头，并记录被问过什么。"""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, dict, str | None]] = []

    def __call__(self, method, params=None, *, session_id=None, timeout=30.0):
        self.calls.append((method, dict(params or {}), session_id))
        canned = self.responses.get(method)
        if canned is None:
            return {}
        if isinstance(canned, Exception):
            raise canned
        if callable(canned):
            return canned(params or {}, session_id)
        return canned

    def methods(self) -> list[str]:
        return [call[0] for call in self.calls]


class StubConn:
    """只记 ``send_call`` 的连接替身，给 ``setup_session`` 用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []

    def send_call(self, method, params=None, *, session_id=None, timeout=30.0):
        self.calls.append((method, dict(params or {}), session_id))
        return {"sessionId": "auto-1"}


def make_session(tmp_path: Path, *, responses=None, filters=None, **options):
    """建一条可喂事件的管线，返回 ``(session, fake_call, out_dir)``。"""
    out_dir = tmp_path / "cap"
    opts = CC.CaptureOptions(
        out_dir=out_dir,
        filters=filters if filters is not None else CC.CaptureFilters(),
        **options,
    )
    writer = CC.CaptureWriter(opts)
    fake = FakeCall(responses)
    session = CC.CaptureSession(call=fake, writer=writer, options=opts)
    return session, fake, out_dir


def request_event(
    request_id: str,
    url: str,
    *,
    rtype: str = "XHR",
    method: str = "GET",
    headers: dict | None = None,
    post_data: str | None = None,
    has_post_data: bool = False,
) -> dict:
    request: dict = {"url": url, "method": method, "headers": headers or {}}
    if post_data is not None:
        request["postData"] = post_data
    if has_post_data:
        request["hasPostData"] = True
    return {
        "requestId": request_id,
        "request": request,
        "type": rtype,
        "timestamp": 100.0,
        "wallTime": 1_700_000_000.0,
        "documentURL": "https://www.zhipin.com/web/geek/jobs",
        "frameId": "F1",
        "loaderId": "L1",
    }


def response_event(request_id: str, *, status: int = 200, mime: str = "application/json") -> dict:
    return {
        "requestId": request_id,
        "timestamp": 101.0,
        "type": "XHR",
        "response": {
            "status": status,
            "statusText": "OK",
            "mimeType": mime,
            "headers": {"Content-Type": mime},
            "protocol": "h2",
            "remoteIPAddress": "1.2.3.4",
            "remotePort": 443,
        },
    }


def finished_event(request_id: str, *, length: int = 10) -> dict:
    return {"requestId": request_id, "timestamp": 102.0, "encodedDataLength": length}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def index_rows(out_dir: Path) -> list[dict]:
    return read_jsonl(out_dir / "index.jsonl")


def ws_rows(out_dir: Path) -> list[dict]:
    return read_jsonl(out_dir / "websocket.jsonl")


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/json", "json"),
        ("application/json; charset=utf-8", "json"),
        ("APPLICATION/JSON", "json"),
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("text/plain", "txt"),
        ("application/vnd.api+json", "json"),
        ("font/woff2", "woff2"),
        ("audio/mpeg", "mp3"),
        ("video/mp4", "mp4"),
        ("weird/thing", "bin"),
        ("", "bin"),
        (";charset=x", "bin"),
    ],
)
def test_ext_for_content_type(content_type, expected):
    assert CC.ext_for_content_type(content_type) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.b/wapi/zpgeek/friend/add.json?x=1", "add"),
        ("https://a.b/", "a.b"),
        ("https://a.b/../..", "req"),
        ("https://a.b/wapi/zpgeek/job/list.json", "list"),
    ],
)
def test_slugify_known_cases(url, expected):
    assert CC.slugify(url) == expected


def test_slugify_strips_unsafe_characters():
    slug = CC.slugify("https://a.b/wapi/%E4%B8%AD%E6%96%87/file%20name.exe")
    assert slug
    assert "/" not in slug and "\\" not in slug and ".." not in slug
    assert all(ch.isalnum() or ch in "._-" for ch in slug)


def test_slugify_truncates_long_names():
    assert len(CC.slugify("https://a.b/" + "x" * 500)) <= 60


def test_parse_query_collects_repeated_keys():
    assert CC.parse_query("https://a.b/x?a=1&a=2&b=") == {"a": ["1", "2"], "b": ""}


def test_get_header_is_case_insensitive():
    assert CC.get_header({"content-type": "application/json"}, "Content-Type") == "application/json"
    assert CC.get_header({}, "Content-Type") == ""


def test_default_filters_drop_html_css_js_only():
    filters = CC.filters_for_preset("all")
    assert not filters.keep(rtype="Document", url="u")
    assert not filters.keep(rtype="Stylesheet", url="u")
    assert not filters.keep(rtype="Script", url="u")
    for kept in ("XHR", "Fetch", "Image", "Font", "Media", "WebSocket", "Other"):
        assert filters.keep(rtype=kept, url="u"), kept


def test_media_and_api_presets():
    media = CC.filters_for_preset("media")
    assert not media.keep(rtype="Image", url="u")
    assert media.keep(rtype="XHR", url="u")

    api = CC.filters_for_preset("api")
    assert api.keep(rtype="XHR", url="u")
    assert api.keep(rtype="WebSocket", url="u")
    assert not api.keep(rtype="Image", url="u")
    assert not api.keep(rtype="Document", url="u")


def test_exclude_types_empty_string_means_keep_everything():
    filters = CC.filters_for_preset("all", exclude_types="")
    assert filters.keep(rtype="Document", url="u")
    assert filters.keep(rtype="Script", url="u")


def test_url_filters():
    filters = CC.filters_for_preset("all", url_substr="/wapi/")
    assert filters.keep(rtype="XHR", url="https://a.b/wapi/x")
    assert not filters.keep(rtype="XHR", url="https://a.b/other")

    excluded = CC.filters_for_preset("all", url_exclude="cdn")
    assert not excluded.keep(rtype="XHR", url="https://cdn.a.b/x")

    regexed = CC.filters_for_preset("all", url_regex=r"/joblist\.json")
    assert regexed.keep(rtype="XHR", url="https://a.b/joblist.json")
    assert not regexed.keep(rtype="XHR", url="https://a.b/other.json")


# --------------------------------------------------------------------------- #
# 过滤在管线里的效果
# --------------------------------------------------------------------------- #


def test_pipeline_only_keeps_non_js_css_html(tmp_path):
    session, _, out = make_session(tmp_path)
    for i, rtype in enumerate(("Document", "Stylesheet", "Script", "XHR", "Image")):
        rid = f"R{i}"
        session.handle_event("S1", "Network.requestWillBeSent", request_event(rid, f"https://a.b/{i}", rtype=rtype))
        session.handle_event("S1", "Network.loadingFinished", finished_event(rid))
    session.finalize(reason="t")

    rows = index_rows(out)
    assert [row["type"] for row in rows] == ["XHR", "Image"]
    assert session.requests_filtered == 3
    assert session.requests_total == 2


def test_filtered_request_leaves_no_record(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/", rtype="Document"))
    session.finalize(reason="t")
    assert index_rows(out) == []
    assert session._records == {}


# --------------------------------------------------------------------------- #
# ExtraInfo 合并（含乱序）
# --------------------------------------------------------------------------- #


def test_request_extra_merges_cookie_when_arriving_late(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/api"))
    session.handle_event(
        "S1",
        "Network.requestWillBeSentExtraInfo",
        {"requestId": "R1", "headers": {"Cookie": "__zp_stoken__=abc", "Referer": "https://a.b/"}},
    )
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    row = index_rows(out)[0]
    assert row["request_headers"]["Cookie"] == "__zp_stoken__=abc"
    assert row["request_headers_source"] == "requestWillBeSentExtraInfo"


def test_request_extra_merges_when_arriving_first(tmp_path):
    """ExtraInfo 早于 requestWillBeSent 到（缓存命中/快路径）也得合上。"""
    session, _, out = make_session(tmp_path)
    session.handle_event(
        "S1",
        "Network.requestWillBeSentExtraInfo",
        {"requestId": "R1", "headers": {"Cookie": "c=1"}},
    )
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/api"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    row = index_rows(out)[0]
    assert row["request_headers"]["Cookie"] == "c=1"
    assert row["request_headers_source"] == "requestWillBeSentExtraInfo"


def test_response_extra_supplies_set_cookie(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/api"))
    session.handle_event("S1", "Network.responseReceived", response_event("R1"))
    session.handle_event(
        "S1",
        "Network.responseReceivedExtraInfo",
        {"requestId": "R1", "statusCode": 200, "headers": {"Set-Cookie": "sid=1", "Content-Type": "application/json"}},
    )
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    row = index_rows(out)[0]
    assert row["response"]["headers"]["Set-Cookie"] == "sid=1"
    assert row["response"]["headers_source"] == "responseReceivedExtraInfo"
    assert row["response"]["status"] == 200


def test_response_received_does_not_clobber_extra_headers(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/api"))
    session.handle_event(
        "S1",
        "Network.responseReceivedExtraInfo",
        {"requestId": "R1", "headers": {"Set-Cookie": "sid=1"}},
    )
    session.handle_event("S1", "Network.responseReceived", response_event("R1"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    assert index_rows(out)[0]["response"]["headers"]["Set-Cookie"] == "sid=1"


# --------------------------------------------------------------------------- #
# 正文落盘
# --------------------------------------------------------------------------- #


def test_base64_body_is_written_as_raw_bytes(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n binary"
    encoded = base64.b64encode(raw).decode()
    session, fake, out = make_session(
        tmp_path,
        responses={"Network.getResponseBody": {"body": encoded, "base64Encoded": True}},
    )
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/logo.png", rtype="Image"))
    session.handle_event("S1", "Network.responseReceived", response_event("R1", mime="image/png"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    row = index_rows(out)[0]
    body = row["body"]
    assert body["base64"] is True
    assert body["bytes"] == len(raw)
    assert body["path"].startswith("bodies/") and body["path"].endswith(".png")
    assert (out / body["path"]).read_bytes() == raw  # 是原始字节，不是 base64 文本
    assert fake.methods() == ["Network.getResponseBody"]


def test_text_body_written_as_utf8(tmp_path):
    session, _, out = make_session(
        tmp_path, responses={"Network.getResponseBody": {"body": '{"code":0}', "base64Encoded": False}}
    )
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/joblist.json"))
    session.handle_event("S1", "Network.responseReceived", response_event("R1"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    row = index_rows(out)[0]
    assert row["body"]["base64"] is False
    assert row["body"]["encoding"] == "utf-8"
    assert row["body"]["path"].endswith(".json")
    assert (out / row["body"]["path"]).read_text(encoding="utf-8") == '{"code":0}'


def test_get_response_body_failure_is_recorded_not_raised(tmp_path):
    session, _, out = make_session(
        tmp_path,
        responses={"Network.getResponseBody": CC.CdpError("No resource with given identifier")},
    )
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/stream"))
    session.handle_event("S1", "Network.responseReceived", response_event("R1"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    # 后面这条还得照常处理，说明失败没把管线打挂
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R2", "https://a.b/two"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R2"))
    session.finalize(reason="t")

    rows = index_rows(out)
    assert len(rows) == 2
    assert rows[0]["body"]["path"] is None
    assert "取正文失败" in rows[0]["body"]["note"]
    assert rows[0]["status"] == "complete"


# --------------------------------------------------------------------------- #
# POST 正文
# --------------------------------------------------------------------------- #


def test_inline_post_data_is_written_to_disk(tmp_path):
    session, fake, out = make_session(tmp_path)
    session.handle_event(
        "S1",
        "Network.requestWillBeSent",
        request_event(
            "R1",
            "https://a.b/wapi/zpgeek/friend/add.json",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            post_data="securityId=abc&jobId=1",
            has_post_data=True,
        ),
    )
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    row = index_rows(out)[0]
    assert row["post"]["bytes"] == len("securityId=abc&jobId=1")
    assert row["post"]["path"].endswith(".txt")
    assert (out / row["post"]["path"]).read_text(encoding="utf-8") == "securityId=abc&jobId=1"
    assert "Network.getRequestPostData" not in fake.methods()


def test_post_data_fetched_when_missing_from_event(tmp_path):
    session, fake, out = make_session(
        tmp_path, responses={"Network.getRequestPostData": {"postData": "a=1&b=2"}}
    )
    session.handle_event(
        "S1",
        "Network.requestWillBeSent",
        request_event("R1", "https://a.b/submit", method="POST", has_post_data=True),
    )
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    assert "Network.getRequestPostData" in fake.methods()
    assert (out / index_rows(out)[0]["post"]["path"]).read_text(encoding="utf-8") == "a=1&b=2"


def test_post_data_lookup_failure_is_noted(tmp_path):
    session, _, out = make_session(
        tmp_path, responses={"Network.getRequestPostData": CC.CdpError("body evicted")}
    )
    session.handle_event(
        "S1",
        "Network.requestWillBeSent",
        request_event("R1", "https://a.b/submit", method="POST", has_post_data=True),
    )
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    post = index_rows(out)[0]["post"]
    assert post["path"] is None
    assert "getRequestPostData 失败" in post["note"]


# --------------------------------------------------------------------------- #
# 失败 / 重定向 / 未完成
# --------------------------------------------------------------------------- #


def test_loading_failed_marks_error_and_skips_body(tmp_path):
    session, fake, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/x"))
    session.handle_event(
        "S1",
        "Network.loadingFailed",
        {"requestId": "R1", "timestamp": 102.0, "errorText": "net::ERR_FAILED", "canceled": True},
    )
    session.finalize(reason="t")

    row = index_rows(out)[0]
    assert row["status"] == "failed"
    assert row["error"]["error_text"] == "net::ERR_FAILED"
    assert row["error"]["canceled"] is True
    assert row["body"] is None
    assert "Network.getResponseBody" not in fake.methods()
    assert session.failed == 1


def test_redirect_hop_becomes_its_own_row(tmp_path):
    session, fake, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/one"))
    second = request_event("R1", "https://a.b/two")
    second["redirectResponse"] = {
        "status": 302,
        "statusText": "Found",
        "headers": {"Location": "https://a.b/two"},
        "responseTime": 101.0,
    }
    session.handle_event("S1", "Network.requestWillBeSent", second)
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    rows = index_rows(out)
    assert len(rows) == 2
    assert rows[0]["url"] == "https://a.b/one"
    assert rows[0]["redirect"]["is_redirect"] is True
    assert rows[0]["response"]["status"] == 302
    assert rows[1]["url"] == "https://a.b/two"
    assert rows[1]["redirect"]["hop"] == 2
    assert rows[1]["redirect"]["redirected_from_request_id"] == "R1"
    # 中转那跳没有可取的正文；最后一跳是完整请求，照常取
    assert rows[0]["body"] is None
    assert rows[1]["body"] is not None
    assert fake.methods().count("Network.getResponseBody") == 1


def test_unfinished_request_is_marked_pending_on_finalize(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/slow"))
    session.finalize(reason="Ctrl+C")

    row = index_rows(out)[0]
    assert row["status"] == "pending"
    assert row["body"] is None
    assert session.pending == 1


def test_same_request_id_in_two_sessions_does_not_collide(tmp_path):
    """requestId 只在单个 session 内唯一——跨 session 撞车必须靠复合键挡住。"""
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/one"))
    session.handle_event("S2", "Network.requestWillBeSent", request_event("R1", "https://a.b/two"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    rows = {row["session_id"]: row for row in index_rows(out)}
    assert rows["S1"]["url"] == "https://a.b/one"
    assert rows["S2"]["url"] == "https://a.b/two"
    assert rows["S1"]["status"] == "complete"
    assert rows["S2"]["status"] == "pending"


def test_timings_compute_duration(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/x"))
    session.handle_event("S1", "Network.responseReceived", response_event("R1"))
    session.handle_event("S1", "Network.loadingFinished", finished_event("R1"))
    session.finalize(reason="t")

    timings = index_rows(out)[0]["timings"]
    assert timings["request_ts"] == 100.0
    assert timings["response_ts"] == 101.0
    assert timings["finished_ts"] == 102.0
    assert timings["duration_ms"] == 1000.0


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


def test_websocket_handshake_and_frames(tmp_path):
    session, _, out = make_session(tmp_path)
    url = "wss://gw.example.com:8080/chatws"
    session.handle_event("S1", "Network.webSocketCreated", {"requestId": "W1", "url": url, "timestamp": 1.0})
    session.handle_event(
        "S1",
        "Network.webSocketWillSendHandshakeRequest",
        {
            "requestId": "W1",
            "wallTime": 1_700_000_000.0,
            "request": {"headers": {"Cookie": "wt2=abc", "Sec-WebSocket-Protocol": "mqtt"}},
        },
    )
    session.handle_event(
        "S1",
        "Network.webSocketHandshakeResponseReceived",
        {"requestId": "W1", "response": {"status": 101, "statusText": "Switching Protocols", "headers": {"Upgrade": "websocket"}}},
    )
    session.handle_event(
        "S1",
        "Network.webSocketFrameSent",
        {"requestId": "W1", "timestamp": 2.0, "response": {"opcode": 2, "mask": True, "payloadData": "EAEABQ=="}},
    )
    session.handle_event(
        "S1",
        "Network.webSocketFrameReceived",
        {"requestId": "W1", "timestamp": 3.0, "response": {"opcode": 1, "mask": False, "payloadData": "hi"}},
    )
    session.handle_event("S1", "Network.webSocketClosed", {"requestId": "W1", "timestamp": 4.0})
    session.finalize(reason="t")

    rows = ws_rows(out)
    assert [row["event"] for row in rows] == [
        "created",
        "handshake_request",
        "handshake_response",
        "frame",
        "frame",
        "closed",
    ]
    sent, recv = rows[3], rows[4]
    assert sent["dir"] == "sent" and sent["binary"] is True and sent["payload_base64"] is True
    assert sent["payload"] == "EAEABQ=="  # 原样存 base64，不做二次编码
    assert sent["payload_bytes"] == 4
    assert recv["dir"] == "recv" and recv["binary"] is False and recv["payload"] == "hi"

    idx = index_rows(out)
    assert len(idx) == 1
    assert idx[0]["type"] == "WebSocket"
    assert idx[0]["ws"]["handshake_request"]["headers"]["Sec-WebSocket-Protocol"] == "mqtt"
    assert idx[0]["ws"]["handshake_response"]["status"] == 101
    assert idx[0]["ws"]["frame_counts"] == {"sent": 1, "recv": 1}
    assert idx[0]["status"] == "complete"
    assert session.ws_frames_sent == 1 and session.ws_frames_recv == 1


def test_websocket_frames_dropped_when_filtered_out(tmp_path):
    filters = CC.filters_for_preset("api", include_types="XHR")
    session, _, out = make_session(tmp_path, filters=filters)
    session.handle_event("S1", "Network.webSocketCreated", {"requestId": "W1", "url": "wss://a.b/chatws"})
    session.handle_event(
        "S1",
        "Network.webSocketFrameSent",
        {"requestId": "W1", "response": {"opcode": 1, "payloadData": "x"}},
    )
    session.finalize(reason="t")
    assert ws_rows(out) == []
    assert index_rows(out) == []


def test_websocket_request_will_be_sent_merges_into_existing_record(tmp_path):
    """握手也会走 requestWillBeSent，别再开一条记录。"""
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.webSocketCreated", {"requestId": "W1", "url": "wss://a.b/chatws"})
    session.handle_event(
        "S1",
        "Network.requestWillBeSent",
        request_event("W1", "wss://a.b/chatws", rtype="WebSocket", headers={"Origin": "https://a.b"}),
    )
    session.handle_event("S1", "Network.webSocketClosed", {"requestId": "W1", "timestamp": 5.0})
    session.finalize(reason="t")
    assert len(index_rows(out)) == 1


# --------------------------------------------------------------------------- #
# session 装配
# --------------------------------------------------------------------------- #


def test_setup_session_enables_network_and_nested_autoattach(tmp_path):
    session, _, _ = make_session(tmp_path)
    conn = StubConn()
    CC.setup_session(conn, session, "S1", {"targetId": "T1", "type": "page", "url": "https://a.b/"})

    methods = [call[0] for call in conn.calls]
    assert methods == ["Network.enable", "Target.setAutoAttach"]
    enable_params = conn.calls[0][1]
    # 设了 maxPostDataSize 会把 POST body 截断——绝不能出现
    assert "maxPostDataSize" not in enable_params
    assert enable_params == {}
    # 子 session 上得再设一次 autoAttach，否则 OOPIF/worker 收不到
    assert conn.calls[1][2] == "S1"
    assert conn.calls[1][1]["autoAttach"] is True
    assert session._targets["S1"]["target_id"] == "T1"


def test_setup_session_is_idempotent(tmp_path):
    session, _, _ = make_session(tmp_path)
    conn = StubConn()
    CC.setup_session(conn, session, "S1", {"targetId": "T1", "type": "page"})
    CC.setup_session(conn, session, "S1", {"targetId": "T1", "type": "page"})
    assert [call[0] for call in conn.calls].count("Network.enable") == 1


def test_setup_session_releases_paused_target_after_enable(tmp_path):
    session, _, _ = make_session(tmp_path)
    conn = StubConn()
    CC.setup_session(
        conn, session, "S1", {"targetId": "T1", "type": "page"},
        wait_for_debugger=True, waiting=True,
    )
    methods = [call[0] for call in conn.calls]
    assert methods == ["Network.enable", "Target.setAutoAttach", "Runtime.runIfWaitingForDebugger"]


def test_on_attach_callback_fires_for_attached_to_target(tmp_path):
    session, _, _ = make_session(tmp_path)
    seen: list[tuple] = []
    session._on_attach_cb = lambda sid, info, waiting: seen.append((sid, info.get("type"), waiting))
    session.handle_event(
        "S0",
        "Target.attachedToTarget",
        {"sessionId": "S9", "targetInfo": {"targetId": "T9", "type": "worker"}, "waitingForDebugger": True},
    )
    assert seen == [("S9", "worker", True)]
    assert session._targets["S9"]["type"] == "worker"


# --------------------------------------------------------------------------- #
# meta / 汇总
# --------------------------------------------------------------------------- #


def test_meta_and_summary_counts_match_disk(tmp_path):
    session, _, out = make_session(
        tmp_path, responses={"Network.getResponseBody": {"body": "hello", "base64Encoded": False}}
    )
    for i in range(3):
        rid = f"R{i}"
        session.handle_event("S1", "Network.requestWillBeSent", request_event(rid, f"https://a.b/{i}"))
        session.handle_event("S1", "Network.responseReceived", response_event(rid))
        session.handle_event("S1", "Network.loadingFinished", finished_event(rid))
    session.handle_event("S1", "Network.requestWillBeSent", request_event("RD", "https://a.b/", rtype="Document"))
    summary = session.finalize(reason="测试收工")

    assert summary.requests_written == len(index_rows(out)) == 3
    assert summary.requests_filtered == 1
    assert summary.bodies_written == 3
    assert summary.body_bytes == 3 * len("hello")
    assert summary.stop_reason == "测试收工"

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["stop_reason"] == "测试收工"
    assert meta["requests_written"] == 3
    assert "started_at" in meta and "ended_at" in meta


def test_finalize_is_idempotent_and_files_are_closed(tmp_path):
    session, _, out = make_session(tmp_path)
    session.handle_event("S1", "Network.requestWillBeSent", request_event("R1", "https://a.b/x"))
    first = session.finalize(reason="a")
    second = session.finalize(reason="b")
    assert first.requests_written == second.requests_written == 1
    assert len(index_rows(out)) == 1  # 没被写第二遍


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #


class FakeWs:
    """按脚本吐帧的 socket 替身。脚本元素可以是字符串，也可以是要抛的异常。

    走 ``responder`` 时是「收到什么才回什么」——真实链路上回包只会跟在请求后面，
    预先把回包排在 ``script`` 里会让读线程抢在 ``send_call`` 登记前就把连接读没了。
    """

    def __init__(self, script: list | None = None, responder=None) -> None:
        self.script: list = list(script or [])
        self.responder = responder
        self.sent: list[dict] = []
        self.closed = False

    def feed(self, item) -> None:
        self.script.append(item)

    def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        if self.responder is not None:
            for item in self.responder(message) or ():
                self.script.append(item)

    def recv(self):
        if not self.script:
            time.sleep(0.01)  # 空转别把 CPU 烧了
            raise websocket.WebSocketTimeoutException("idle")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def patch_ws(monkeypatch):
    def _patch(fake: FakeWs) -> FakeWs:
        monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: fake)
        return fake

    return _patch


def test_events_are_yielded_then_close_ends_iteration(patch_ws):
    fake = patch_ws(
        FakeWs(
            [
                json.dumps({"method": "Network.loadingFinished", "sessionId": "S1", "params": {"requestId": "R"}}),
                websocket.WebSocketConnectionClosedException("bye"),
            ]
        )
    )
    conn = CC.CdpConnection("ws://x")
    conn.start()
    try:
        got = list(conn.events())
    finally:
        conn.close()

    assert [(sid, method) for sid, method, _ in got] == [("S1", "Network.loadingFinished")]
    assert conn.closed and "bye" in conn.close_reason
    assert fake.closed is True


def test_send_call_matches_response_by_id(patch_ws):
    patch_ws(
        FakeWs(
            responder=lambda msg: [
                json.dumps({"id": msg["id"], "result": {"targetInfos": [{"targetId": "T"}]}})
            ]
        )
    )
    conn = CC.CdpConnection("ws://x")
    conn.start()
    try:
        assert conn.send_call("Target.getTargets") == {"targetInfos": [{"targetId": "T"}]}
    finally:
        conn.close()


def test_pending_call_is_woken_when_connection_drops(patch_ws):
    """连接断了必须唤醒等回包的人，否则消费线程要一直卡到超时。"""
    fake = patch_ws(FakeWs())
    conn = CC.CdpConnection("ws://x")
    conn.start()
    outcome: dict = {}

    def worker() -> None:
        try:
            conn.send_call("Network.enable", timeout=20.0)
        except Exception as exc:  # noqa: BLE001 - 就是要抓这个
            outcome["exc"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        deadline = time.time() + 2.0
        while not conn._pending and time.time() < deadline:
            time.sleep(0.01)
        assert conn._pending, "命令没登记进 pending，测试没意义"
        fake.feed(websocket.WebSocketConnectionClosedException("boom"))
        thread.join(timeout=5.0)
    finally:
        conn.close()

    assert not thread.is_alive()
    assert isinstance(outcome.get("exc"), CC.CdpError)


def test_send_call_raises_on_error_response(patch_ws):
    patch_ws(
        FakeWs(
            responder=lambda msg: [
                json.dumps({"id": msg["id"], "error": {"message": "Target already attached"}})
            ]
        )
    )
    conn = CC.CdpConnection("ws://x")
    conn.start()
    try:
        with pytest.raises(CC.CdpError, match="already attached"):
            conn.send_call("Target.attachToTarget", {"targetId": "T"})
    finally:
        conn.close()


def test_send_call_after_close_raises(patch_ws):
    patch_ws(FakeWs([websocket.WebSocketConnectionClosedException("bye")]))
    conn = CC.CdpConnection("ws://x")
    conn.start()
    try:
        deadline = time.time() + 2.0
        while not conn.closed and time.time() < deadline:
            time.sleep(0.01)
        assert conn.closed
        with pytest.raises(CC.CdpError, match="已关闭"):
            conn.send_call("Network.enable")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 端到端装配（还是假 Chrome，但把 bootstrap 到收尾整条路走通）
# --------------------------------------------------------------------------- #


def _fake_chrome() -> FakeWs:
    """冒充一台只有一个页面 target 的 Chrome，喂完一轮请求自己断线。"""

    def respond(msg):
        method = msg["method"]
        sid = msg.get("sessionId")
        out: list = []

        def reply(result=None):
            out.append(json.dumps({"id": msg["id"], "result": result or {}}))

        def event(session_id, name, params):
            out.append(json.dumps({"method": name, "sessionId": session_id, "params": params}))

        if method == "Target.setDiscoverTargets":
            reply()
        elif method == "Target.setAutoAttach":
            reply()
            if sid:  # 页面 session 上那次 = 订阅齐了，开始吐流量
                event(sid, "Network.requestWillBeSent", request_event(
                    "R1", "https://a.b/wapi/x.json",
                    rtype="XHR", method="POST", post_data="a=1", has_post_data=True,
                ))
                event(sid, "Network.responseReceived", response_event("R1"))
                event(sid, "Network.loadingFinished", finished_event("R1"))
        elif method == "Target.getTargets":
            reply({"targetInfos": [
                {"targetId": "T1", "type": "page", "url": "https://a.b/", "title": "首页"}
            ]})
        elif method == "Target.attachToTarget":
            reply({"sessionId": "S1"})
        elif method == "Network.getResponseBody":
            reply({"body": "ok", "base64Encoded": False})
            out.append(websocket.WebSocketConnectionClosedException("抓完收工"))
        else:
            reply()
        return out

    return FakeWs(responder=respond)


def test_run_capture_end_to_end_against_fake_chrome(patch_ws, tmp_path):
    fake = patch_ws(_fake_chrome())
    out = tmp_path / "cap"
    summary = CC.run_capture(
        ws_url="ws://fake/devtools/browser/abc",
        options=CC.CaptureOptions(out_dir=out),
        duration=10.0,  # 兜底：装配若卡住，别把测试吊死
    )

    # bootstrap 该发的命令一条不少
    sent = [msg["method"] for msg in fake.sent]
    assert sent[:4] == [
        "Target.setDiscoverTargets",
        "Target.setAutoAttach",
        "Target.getTargets",
        "Target.attachToTarget",
    ]
    assert "Network.enable" in sent

    # 请求走了完整一圈并落了盘
    rows = index_rows(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["method"] == "POST" and row["status"] == "complete"
    assert row["session_id"] == "S1"
    assert row["target"]["url"] == "https://a.b/"
    assert (out / row["post"]["path"]).read_text(encoding="utf-8") == "a=1"
    assert (out / row["body"]["path"]).read_text(encoding="utf-8") == "ok"

    assert summary.requests_written == 1
    assert summary.bodies_written == 1
    assert "连接关闭" in summary.stop_reason

    # 收尾那版 meta 是并入的——开抓时写的 ws_url/started_at 不能丢
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["ws_url"] == "ws://fake/devtools/browser/abc"
    assert meta["started_at"] and meta["ended_at"]
    assert meta["requests_written"] == 1
    assert "filters" in meta
