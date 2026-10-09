"""CDP 网络抓包：附着到带调试口的 Chrome，把整个浏览器实例的请求落盘。

**不参与**取 ``__zp_stoken__``（那条在 :mod:`boss_jobs.cdp_stoken`）。用途是「看清
浏览器到底在发什么」：订阅 ``Network`` 域，把所有**非 JS/CSS/HTML** 的请求连同
请求头、POST 参数、响应头、响应体写到磁盘，含 WebSocket 的握手与**全部帧**。
不能复用 ``cdp_stoken.CdpClient``——它的 ``call`` 一问一答，所有 CDP 事件会被
静默丢掉。

**两条线程是理解本模块的钥匙**：处理 ``loadingFinished`` 时要调
``getResponseBody``，而回包只能从同一条 ws 的 ``recv()`` 到——读和处理若是同一线程，
回包永远排在自己后面，死锁。所以**读线程**只 ``recv()``（有 ``id`` 就唤醒等回包的
人，有 ``method`` 就塞队列），**消费线程**取事件、发命令、落盘；落盘因此是单写者。

输出要点：``index.jsonl`` 的请求头**优先取 ExtraInfo 版本**（``requestWillBeSent``
的 headers 不含 Cookie、``responseReceived`` 的不含 Set-Cookie，真实头只在
``*ExtraInfo`` 里，且可能早于主事件到，先到先存）。``requestId`` **只在单个
session 内唯一**，内部一律用 ``(session_id, requestId)`` 复合键。WS 二进制帧
（``opcode=2``）按 CDP 约定 ``payloadData`` 已是 base64，原样存 + ``binary=true``，
**不做二次编码**；``opcode=0`` 是续帧（``continuation=true``），消息重组不在本模块。

**默认只附着、绝不关 Chrome**——那是用户自己开的那台。
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

logger = logging.getLogger(__name__)




class CaptureError(Exception):
    """抓包失败的基类。"""


class CdpError(CaptureError):
    """CDP 命令回了 error，或者连接已关闭。"""


class CdpTimeout(CaptureError):
    """等 CDP 回包超时。"""



#: CDP ``ResourceType`` 的全部取值。写全是为了让 ``--include-types`` 的拼写错误
#: 能在 CLI 层被指出来，而不是静默抓不到东西。
ALL_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "Document",
        "Stylesheet",
        "Image",
        "Media",
        "Font",
        "Script",
        "TextTrack",
        "XHR",
        "Fetch",
        "Prefetch",
        "EventSource",
        "WebSocket",
        "Manifest",
        "SignedExchange",
        "Ping",
        "CSPViolationReport",
        "Preflight",
        "Other",
    }
)

#: 默认排除：正好是 HTML / CSS / JS 三种。其余全留（含图片、字体）。
DEFAULT_EXCLUDE_TYPES: frozenset[str] = frozenset({"Document", "Stylesheet", "Script"})

#: ``--preset media`` 在默认之上再排掉这些东西。
_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "Image",
        "Media",
        "Font",
        "TextTrack",
        "Manifest",
        "SignedExchange",
        "Ping",
        "CSPViolationReport",
        "Preflight",
    }
)

#: ``--preset api`` 只留这些——实抓业务接口 + MQTT over WS 的推荐档。
_API_TYPES: frozenset[str] = frozenset(
    {"XHR", "Fetch", "EventSource", "WebSocket", "Prefetch", "Other"}
)

#: 值得 attach 并订阅 ``Network`` 的 target 类型。``browser`` / ``tab`` / ``other``
#: 上没有网络栈，只记元数据。
ATTACHABLE_TARGET_TYPES: frozenset[str] = frozenset(
    {"page", "iframe", "worker", "service_worker", "shared_worker"}
)


_CONTENT_TYPE_EXT: dict[str, str] = {
    "application/json": "json",
    "text/html": "html",
    "text/css": "css",
    "application/javascript": "js",
    "text/javascript": "js",
    "text/plain": "txt",
    "application/x-www-form-urlencoded": "txt",
    "multipart/form-data": "txt",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/event-stream": "sse",
    "application/wasm": "wasm",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
    "font/woff2": "woff2",
    "font/woff": "woff",
    "font/ttf": "ttf",
    "font/otf": "otf",
    "application/font-woff": "woff",
    "application/octet-stream": "bin",
    "application/protobuf": "pbf",
    "application/x-protobuf": "pbf",
}

#: ``audio/*`` / ``video/*`` 的子类型 → 扩展名。表外的按字面用子类型，不安全就 bin。
_AV_SUBTYPE_EXT: dict[str, str] = {
    "mpeg": "mp3",
    "mp4": "mp4",
    "webm": "webm",
    "ogg": "ogg",
    "wav": "wav",
    "aac": "aac",
    "quicktime": "mov",
    "x-msvideo": "avi",
}




def iso_now() -> str:
    """本地时间 ISO8601（带毫秒与时区），给落盘的行打时间戳。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def ext_for_content_type(content_type: str) -> str:
    """content-type → 落盘扩展名。先剥 ``;charset=`` 再查，认不出回 ``bin``。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "bin"
    ext = _CONTENT_TYPE_EXT.get(ct)
    if ext:
        return ext
    if ct.endswith("+json"):
        return "json"
    if ct.endswith("+xml"):
        return "xml"
    for prefix in ("image/", "audio/", "video/"):
        if ct.startswith(prefix):
            sub = ct[len(prefix) :]
            if prefix == "image/":
                return sub if re.fullmatch(r"[a-z0-9]{2,5}", sub) else "bin"
            return _AV_SUBTYPE_EXT.get(sub, "bin")
    if ct.startswith("text/"):
        return "txt"
    return "bin"


def slugify(url: str) -> str:
    """URL → 安全的文件名主干。

    取 path 的最后一段并**去掉原扩展名**（最终扩展名由 content-type 决定），
    白名单化到 ``[A-Za-z0-9._-]``，折叠连续下划线，去首尾的 ``._-``。
    ``..`` 这类会退化成 ``req``，避免路径穿越。
    """
    parts = urllib.parse.urlsplit(url or "")
    stem = Path(urllib.parse.unquote(parts.path or "")).stem
    if not stem:
        stem = parts.hostname or ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("._-")
    if not stem or set(stem) <= {".", "_"}:
        stem = "req"
    return stem[:60]


def parse_query(url: str) -> dict[str, Any]:
    """把 URL 的 query 拆成 dict；同名多值收成 list。"""
    out: dict[str, Any] = {}
    try:
        pairs = urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url or "").query, keep_blank_values=True
        )
    except ValueError:
        return out
    for key, value in pairs:
        if key not in out:
            out[key] = value
        elif isinstance(out[key], list):
            out[key].append(value)
        else:
            out[key] = [out[key], value]
    return out


def get_header(headers: Mapping[str, Any], name: str) -> str:
    """HTTP 头的名字大小写不保证，按名字不区分大小写取一个。"""
    want = name.lower()
    for key, value in headers.items():
        if str(key).lower() == want:
            return "" if value is None else str(value)
    return ""




@dataclass(frozen=True)
class CaptureFilters:
    """哪些请求值得落盘。

    ``include_types`` 非空就是白名单模式，优先级高于 ``exclude_types``。
    URL 三条是附加条件，全部满足才留。
    """

    exclude_types: frozenset[str] = DEFAULT_EXCLUDE_TYPES
    include_types: frozenset[str] = frozenset()
    url_substr: str = ""
    url_regex: str = ""
    url_exclude: str = ""

    def keep(self, *, rtype: str, url: str) -> bool:
        if self.include_types:
            if rtype not in self.include_types:
                return False
        elif rtype in self.exclude_types:
            return False
        if self.url_substr and self.url_substr not in url:
            return False
        if self.url_regex and not re.search(self.url_regex, url):
            return False
        if self.url_exclude and self.url_exclude in url:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclude_types": sorted(self.exclude_types),
            "include_types": sorted(self.include_types),
            "url_substr": self.url_substr,
            "url_regex": self.url_regex,
            "url_exclude": self.url_exclude,
        }


def filters_for_preset(
    preset: str,
    *,
    include_types: str = "",
    exclude_types: str | None = None,
    url_substr: str = "",
    url_regex: str = "",
    url_exclude: str = "",
) -> CaptureFilters:
    """把 ``--preset`` 和逐条覆盖拼成一份 :class:`CaptureFilters`。

    * ``all``   默认档，只排 HTML/CSS/JS
    * ``media`` 在默认之上再排图片/字体/媒体
    * ``api``   白名单，只留接口与 WebSocket

    ``exclude_types`` 传空串表示「一个都不排」；传 ``None`` 表示「按档位的默认」。
    """
    if preset == "media":
        base_exclude = DEFAULT_EXCLUDE_TYPES | _MEDIA_TYPES
        base_include: frozenset[str] = frozenset()
    elif preset == "api":
        base_exclude = frozenset()
        base_include = _API_TYPES
    else:
        base_exclude = DEFAULT_EXCLUDE_TYPES
        base_include = frozenset()

    if exclude_types is None:
        excludes = base_exclude
    elif not exclude_types.strip():
        excludes = frozenset()
    else:
        excludes = frozenset(_split_types(exclude_types))

    includes = frozenset(_split_types(include_types)) if include_types.strip() else base_include

    return CaptureFilters(
        exclude_types=excludes,
        include_types=includes,
        url_substr=url_substr,
        url_regex=url_regex,
        url_exclude=url_exclude,
    )


def _split_types(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]




@dataclass
class CaptureOptions:
    """一次抓包的参数。"""

    out_dir: Path
    filters: CaptureFilters = field(default_factory=CaptureFilters)
    #: ``websocket.jsonl`` 攒几行 flush 一次（帧可能很密）。``1`` = 逐行。
    flush_every: int = 20
    #: ``postData`` 缺失时要不要回头调 ``Network.getRequestPostData`` 补。
    fetch_post_data: bool = True
    #: ``Network.enable`` 的缓冲上限（MB）。``0`` = 用 Chrome 默认。
    buffer_mb: int = 0


class CaptureWriter:
    """会话目录的写手。**只许消费线程/finalize 调**，所以不加锁。"""

    def __init__(self, options: CaptureOptions) -> None:
        self.options = options
        self.out_dir = Path(options.out_dir)
        self.bodies_dir = self.out_dir / "bodies"
        self.post_dir = self.out_dir / "post"
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        self.post_dir.mkdir(parents=True, exist_ok=True)
        self._index = (self.out_dir / "index.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._ws = (self.out_dir / "websocket.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._flush_every = max(1, int(options.flush_every))
        self._ws_pending = 0
        self._ws_last_flush = time.time()
        self._seq = 0
        self._closed = False
        #: ``meta.json`` 的累积字段：收尾那版是**合并**不是替换，
        #: 否则「开抓先写一版 started_at/ws_url」会被收尾那版盖掉。
        self._meta: dict[str, Any] = {}
        #: 汇总用
        self.bodies_written = 0
        self.body_bytes = 0


    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _rel(self, path: Path) -> str:
        """相对 out_dir 的路径，一律用正斜杠（跨平台可读）。"""
        return path.relative_to(self.out_dir).as_posix()

    def _dump(self, row: Mapping[str, Any]) -> str:
        return json.dumps(row, ensure_ascii=False, default=str)

    def write_index(self, row: Mapping[str, Any]) -> None:
        """写一行请求元数据。**每行 flush**——请求量是人的尺度，丢数据更贵。"""
        if self._closed:
            return
        self._index.write(self._dump(row) + "\n")
        self._index.flush()

    def write_ws(self, row: Mapping[str, Any]) -> None:
        """写一行 WS 流水。帧可能很密，按行数/时间双触发 flush。"""
        if self._closed:
            return
        self._ws.write(self._dump(row) + "\n")
        self._ws_pending += 1
        now = time.time()
        if self._ws_pending >= self._flush_every or (now - self._ws_last_flush) >= 0.5:
            self._ws.flush()
            self._ws_pending = 0
            self._ws_last_flush = now


    def write_body(
        self,
        seq: int,
        url: str,
        content_type: str,
        *,
        text: str | None = None,
        b64: str | None = None,
    ) -> dict[str, Any]:
        """落一份响应体。``b64`` 非 None 表示 CDP 说这是二进制（走解码），否则当文本。"""
        path = self.bodies_dir / f"{seq:04d}_{slugify(url)}.{ext_for_content_type(content_type)}"
        if b64 is not None:
            try:
                raw = base64.b64decode(b64)
            except (ValueError, TypeError) as exc:
                return {
                    "path": None,
                    "bytes": 0,
                    "base64": True,
                    "encoding": None,
                    "note": f"base64 解码失败：{exc}",
                }
            path.write_bytes(raw)
            entry = {
                "path": self._rel(path),
                "bytes": len(raw),
                "base64": True,
                "encoding": None,
                "note": None,
            }
        else:
            raw = (text or "").encode("utf-8", "replace")
            path.write_bytes(raw)
            entry = {
                "path": self._rel(path),
                "bytes": len(raw),
                "base64": False,
                "encoding": "utf-8",
                "note": None,
            }
        self.bodies_written += 1
        self.body_bytes += int(entry["bytes"])
        return entry

    def write_post(
        self, seq: int, url: str, content_type: str, *, data: str, note: str | None = None
    ) -> dict[str, Any]:
        """落一份 POST 正文。CDP 给的 ``postData`` 恒为字符串，按文本写。"""
        ext = ext_for_content_type(content_type)
        if ext not in {"json", "xml", "txt", "html", "css", "js", "sse"}:
            ext = "txt"
        path = self.post_dir / f"{seq:04d}_{slugify(url)}.{ext}"
        raw = (data or "").encode("utf-8", "replace")
        path.write_bytes(raw)
        return {
            "path": self._rel(path),
            "bytes": len(raw),
            "content_type": content_type or "",
            "encoding": "utf-8",
            "base64": False,
            "note": note,
        }


    def write_meta(self, **fields: Any) -> None:
        """写 ``meta.json``。开抓先写一版，收尾再补一版——**后写是并入，不是替换**。"""
        self._meta.update(fields)
        self._meta.setdefault("out_dir", str(self.out_dir))
        path = self.out_dir / "meta.json"
        path.write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def flush(self) -> None:
        if self._closed:
            return
        self._index.flush()
        self._ws.flush()
        self._ws_pending = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handle in (self._index, self._ws):
            try:
                handle.flush()
                handle.close()
            except OSError:  # pragma: no cover - 关不掉的句柄不值得让收尾崩
                pass




@dataclass
class RequestRecord:
    """一条在跟的请求。字段名对着 ``index.jsonl`` 的列来。"""

    session_id: str = ""
    request_id: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    loader_id: str = ""
    frame_id: str = ""
    document_url: str = ""
    url: str = ""
    method: str = ""
    rtype: str = "Other"
    initiator: dict[str, Any] | None = None
    request_headers: dict[str, Any] = field(default_factory=dict)
    request_headers_source: str = "requestWillBeSent"
    query: dict[str, Any] = field(default_factory=dict)
    post: dict[str, Any] | None = None
    has_post_data: bool = False
    response: dict[str, Any] = field(default_factory=dict)
    response_headers_text: str | None = None
    body: dict[str, Any] | None = None
    timings: dict[str, Any] = field(default_factory=dict)
    redirect: dict[str, Any] = field(
        default_factory=lambda: {
            "is_redirect": False,
            "redirected_from_request_id": None,
            "hop": 1,
        }
    )
    ws: dict[str, Any] | None = None
    status: str = "pending"
    error: dict[str, Any] | None = None
    captured_at: str = ""
    seq: int = 0
    #: 已经写过 index 行没有（redirect 复用 requestId 时靠它防重复）
    written: bool = False
    #: 待落盘的 POST 正文（真取到了才非 None）
    post_data_raw: str | None = None
    post_note: str | None = None

    def content_type(self) -> str:
        """响应正文的类型：优先真实响应头，退回 CDP 的 mimeType。"""
        return get_header(self.response.get("headers") or {}, "content-type") or str(
            self.response.get("mime_type") or ""
        )

    def to_row(self) -> dict[str, Any]:
        timings = dict(self.timings)
        resp_ts = timings.get("response_ts")
        fin_ts = timings.get("finished_ts")
        if isinstance(resp_ts, (int, float)) and isinstance(fin_ts, (int, float)):
            timings["duration_ms"] = round((fin_ts - resp_ts) * 1000, 3)
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "target": self.target,
            "request_id": self.request_id,
            "loader_id": self.loader_id,
            "frame_id": self.frame_id,
            "document_url": self.document_url,
            "url": self.url,
            "method": self.method,
            "type": self.rtype,
            "initiator": self.initiator,
            "request_headers": self.request_headers,
            "request_headers_source": self.request_headers_source,
            "query": self.query,
            "post": self.post,
            "has_post_data": self.has_post_data,
            "response": self.response,
            "response_headers_text": self.response_headers_text,
            "body": self.body,
            "timings": timings,
            "redirect": self.redirect,
            "ws": self.ws,
            "status": self.status,
            "error": self.error,
            "captured_at": self.captured_at,
        }


@dataclass
class CaptureSummary:
    """一次抓包的收尾汇总。"""

    out_dir: Path
    stop_reason: str
    requests_total: int = 0
    requests_filtered: int = 0
    requests_written: int = 0
    bodies_written: int = 0
    body_bytes: int = 0
    ws_connections: int = 0
    ws_frames_sent: int = 0
    ws_frames_recv: int = 0
    pending: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "out_dir": str(self.out_dir),
            "stop_reason": self.stop_reason,
            "requests_total": self.requests_total,
            "requests_filtered": self.requests_filtered,
            "requests_written": self.requests_written,
            "bodies_written": self.bodies_written,
            "body_bytes": self.body_bytes,
            "ws_connections": self.ws_connections,
            "ws_frames_sent": self.ws_frames_sent,
            "ws_frames_recv": self.ws_frames_recv,
            "pending": self.pending,
            "failed": self.failed,
        }




class CaptureSession:
    """把 CDP 事件流变成磁盘上的文件。

    事件管线按到达顺序处理：``requestWillBeSent`` 建记录 → ``*ExtraInfo`` 补真实头
    → ``responseReceived`` 记响应 → ``loadingFinished``/``failed`` 取正文并落盘；
    WebSocket 走 ``webSocket*`` 一族，target 生命周期走 ``target*``。

    ``call`` 是「发一条 CDP 命令拿回包」的可调用对象——真跑时传
    :meth:`CdpConnection.send_call`，测试时传个按方法名回罐头的假货。
    这样整个管线不碰 socket 也能测。
    """

    def __init__(
        self,
        *,
        call: Callable[..., dict[str, Any]],
        writer: CaptureWriter,
        options: CaptureOptions,
        clock: Callable[[], float] = time.time,
        on_attach: Callable[[str, Mapping[str, Any], bool], None] | None = None,
    ) -> None:
        self._call = call
        self._writer = writer
        self._options = options
        self._clock = clock
        self._on_attach_cb = on_attach
        #: (session_id, requestId) → 这条 id 上的历次记录（redirect 会复用 requestId）
        self._records: dict[tuple[str, str], list[RequestRecord]] = {}
        #: ExtraInfo 可能早于主事件到，先存这儿
        self._pending_req_extra: dict[tuple[str, str], dict[str, Any]] = {}
        self._pending_resp_extra: dict[tuple[str, str], dict[str, Any]] = {}
        self._targets: dict[str, dict[str, Any]] = {}
        self._target_sessions: dict[str, str] = {}
        self._sessions: set[str] = set()
        self.requests_total = 0
        self.requests_filtered = 0
        self.requests_written = 0
        self.pending = 0
        self.failed = 0
        self.ws_connections = 0
        self.ws_frames_sent = 0
        self.ws_frames_recv = 0
        #: 兜底：即使收尾那版之前没人写过 meta（直接 new 出来喂事件），也得有时间戳
        self._writer.write_meta(started_at=iso_now())


    def claim_session(self, session_id: str) -> bool:
        """登记一个 session，返回「这次是新登记的吗」。防重复 ``Network.enable``。"""
        if not session_id or session_id in self._sessions:
            return False
        self._sessions.add(session_id)
        return True

    def note_target(self, session_id: str, target_info: Mapping[str, Any]) -> None:
        snapshot = {
            "target_id": target_info.get("targetId"),
            "type": target_info.get("type"),
            "url": target_info.get("url"),
            "title": target_info.get("title"),
        }
        if session_id:
            self._targets[session_id] = snapshot
        tid = str(target_info.get("targetId") or "")
        if tid and session_id:
            self._target_sessions[tid] = session_id

    def drop_session(self, session_id: str) -> None:
        """session 断了。**已写出的行里那份 target 快照保留**，只清活跃表。"""
        self._sessions.discard(session_id)
        self._targets.pop(session_id, None)

    def _target_of(self, session_id: str | None) -> dict[str, Any]:
        return dict(self._targets.get(session_id or "", {}))


    def _active(self, key: tuple[str, str]) -> RequestRecord | None:
        hops = self._records.get(key)
        if not hops:
            return None
        rec = hops[-1]
        return None if rec.written else rec


    def handle_event(
        self, session_id: str | None, method: str, params: Mapping[str, Any]
    ) -> None:
        name = _EVENT_HANDLERS.get(method)
        if name is None:
            return
        getattr(self, name)(session_id or "", params)


    def _on_request(self, sid: str, params: Mapping[str, Any]) -> None:
        """``requestWillBeSent``：建（或续）一条 :class:`RequestRecord`。

        两个特例：WebSocket 的握手也走这里，但 ``webSocketCreated`` 已经建过记录，
        往那条上并、别再开一条；同一个 ``requestId`` 带着 ``redirectResponse``
        再来 = 上一跳是 3xx，先把上一跳单独收尾落盘，再开这一跳，别把两跳并成一条。
        """
        rid = str(params.get("requestId") or "")
        key = (sid, rid)
        request = params.get("request") or {}
        rtype = str(params.get("type") or "Other")
        url = str(request.get("url") or "")
        redirect_response = params.get("redirectResponse")
        hops = self._records.get(key) or []
        active = hops[-1] if hops and not hops[-1].written else None

        if active is not None and active.rtype == "WebSocket" and rtype == "WebSocket":
            self._fill_request(active, params, request)
            return

        if active is not None and redirect_response:
            self._finish_redirect_hop(active, redirect_response)

        if not self._options.filters.keep(rtype=rtype, url=url):
            self.requests_filtered += 1
            self._pending_req_extra.pop(key, None)
            self._pending_resp_extra.pop(key, None)
            return

        self.requests_total += 1
        rec = RequestRecord(
            session_id=sid,
            request_id=rid,
            target=self._target_of(sid),
            rtype=rtype,
            redirect={
                "is_redirect": False,
                "redirected_from_request_id": rid if hops else None,
                "hop": len(hops) + 1,
            },
        )
        self._fill_request(rec, params, request)
        self._records.setdefault(key, []).append(rec)

        stashed = self._pending_req_extra.pop(key, None)
        if stashed is not None:
            self._merge_request_extra(rec, stashed)
        stashed_resp = self._pending_resp_extra.pop(key, None)
        if stashed_resp is not None:
            self._merge_response_extra(rec, stashed_resp)

    def _fill_request(
        self, rec: RequestRecord, params: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
        """把 ``requestWillBeSent`` 的字段填进记录。

        渲染进程给的头不含 Cookie 之类的凭证，``*ExtraInfo`` 到了会盖掉这份——
        所以只有在还没拿到 ExtraInfo 时才写。
        """
        rec.url = str(request.get("url") or rec.url)
        rec.method = str(request.get("method") or rec.method)
        rec.loader_id = str(params.get("loaderId") or rec.loader_id)
        rec.frame_id = str(params.get("frameId") or rec.frame_id)
        rec.document_url = str(params.get("documentURL") or rec.document_url)
        if not rec.query:
            rec.query = parse_query(rec.url)
        if not rec.initiator and params.get("initiator"):
            rec.initiator = dict(params["initiator"])
        headers = request.get("headers")
        if isinstance(headers, dict) and rec.request_headers_source == "requestWillBeSent":
            rec.request_headers = {str(k): str(v) for k, v in headers.items()}
        rec.has_post_data = bool(request.get("hasPostData")) or rec.has_post_data
        ts = params.get("timestamp")
        if isinstance(ts, (int, float)):
            rec.timings.setdefault("request_ts", ts)
        wall = params.get("wallTime")
        if isinstance(wall, (int, float)):
            rec.timings.setdefault("wall_time", wall)

        post_data = request.get("postData")
        if isinstance(post_data, str):
            rec.post_data_raw = post_data
        elif rec.has_post_data and self._options.fetch_post_data:
            self._fetch_post_data(rec)

    def _fetch_post_data(self, rec: RequestRecord) -> None:
        """``hasPostData`` 为真但没给正文（体积大/二进制）时回头补一手。"""
        try:
            result = self._call(
                "Network.getRequestPostData",
                {"requestId": rec.request_id},
                session_id=rec.session_id,
            )
        except CaptureError as exc:
            rec.post_note = f"getRequestPostData 失败：{exc}"
            return
        data = result.get("postData")
        if isinstance(data, str):
            rec.post_data_raw = data

    def _merge_request_extra(
        self, rec: RequestRecord, params: Mapping[str, Any]
    ) -> None:
        """``requestWillBeSentExtraInfo``：真实请求头（含 Cookie）盖掉渲染进程那份。"""
        headers = params.get("headers")
        if isinstance(headers, dict) and headers:
            rec.request_headers = {str(k): str(v) for k, v in headers.items()}
            rec.request_headers_source = "requestWillBeSentExtraInfo"
        cookies = params.get("associatedCookies")
        if cookies:
            rec.timings.setdefault("associated_cookies", cookies)

    def _on_request_extra(self, sid: str, params: Mapping[str, Any]) -> None:
        key = (sid, str(params.get("requestId") or ""))
        rec = self._active(key)
        if rec is None:
            self._pending_req_extra[key] = dict(params)
            return
        self._merge_request_extra(rec, params)


    def _on_response(self, sid: str, params: Mapping[str, Any]) -> None:
        """``responseReceived``。ExtraInfo 的头更全（含 Set-Cookie），已拿到就别盖回去。"""
        rec = self._active((sid, str(params.get("requestId") or "")))
        if rec is None:
            return
        response = params.get("response") or {}
        if rec.response.get("headers_source") != "responseReceivedExtraInfo":
            headers = response.get("headers")
            if isinstance(headers, dict):
                rec.response["headers"] = {str(k): str(v) for k, v in headers.items()}
                rec.response["headers_source"] = "responseReceived"
        for src, dst in (
            ("status", "status"),
            ("statusText", "status_text"),
            ("mimeType", "mime_type"),
            ("protocol", "protocol"),
            ("remoteIPAddress", "remote_ip"),
            ("remotePort", "remote_port"),
            ("fromDiskCache", "from_disk_cache"),
            ("fromServiceWorker", "from_service_worker"),
            ("fromPrefetchCache", "from_prefetch_cache"),
            ("encodedDataLength", "encoded_data_length"),
        ):
            if src in response:
                rec.response[dst] = response[src]
        ts = params.get("timestamp")
        if isinstance(ts, (int, float)):
            rec.timings["response_ts"] = ts

    def _on_response_extra(self, sid: str, params: Mapping[str, Any]) -> None:
        key = (sid, str(params.get("requestId") or ""))
        rec = self._active(key)
        if rec is None:
            self._pending_resp_extra[key] = dict(params)
            return
        self._merge_response_extra(rec, params)

    def _merge_response_extra(
        self, rec: RequestRecord, params: Mapping[str, Any]
    ) -> None:
        """``responseReceivedExtraInfo``：真实响应头（含 ``Set-Cookie``）盖掉简版。"""
        headers = params.get("headers")
        if isinstance(headers, dict) and headers:
            rec.response["headers"] = {str(k): str(v) for k, v in headers.items()}
            rec.response["headers_source"] = "responseReceivedExtraInfo"
        if params.get("statusCode") is not None:
            rec.response.setdefault("status", params["statusCode"])
        if params.get("headersText"):
            rec.response_headers_text = str(params["headersText"])


    def _on_finished(self, sid: str, params: Mapping[str, Any]) -> None:
        rec = self._active((sid, str(params.get("requestId") or "")))
        if rec is None:
            return
        ts = params.get("timestamp")
        if isinstance(ts, (int, float)):
            rec.timings["finished_ts"] = ts
        if params.get("encodedDataLength") is not None:
            rec.response["encoded_data_length"] = params["encodedDataLength"]
        self._finish(rec, status="complete")

    def _on_failed(self, sid: str, params: Mapping[str, Any]) -> None:
        rec = self._active((sid, str(params.get("requestId") or "")))
        if rec is None:
            return
        ts = params.get("timestamp")
        if isinstance(ts, (int, float)):
            rec.timings["finished_ts"] = ts
        rec.error = {
            "error_text": params.get("errorText"),
            "canceled": params.get("canceled"),
            "blocked_reason": params.get("blockedReason"),
        }
        self._finish(rec, status="failed")


    def _ws_record(self, sid: str, params: Mapping[str, Any]) -> RequestRecord | None:
        """按 requestId 找 WS 记录；``webSocketCreated`` 先到时负责建它。"""
        rid = str(params.get("requestId") or "")
        key = (sid, rid)
        hops = self._records.get(key) or []
        if hops and not hops[-1].written:
            return hops[-1]
        url = str(params.get("url") or "")
        if not self._options.filters.keep(rtype="WebSocket", url=url):
            self.requests_filtered += 1
            return None
        self.requests_total += 1
        rec = RequestRecord(
            session_id=sid,
            request_id=rid,
            target=self._target_of(sid),
            url=url,
            method="GET",
            rtype="WebSocket",
            query=parse_query(url),
            ws={
                "created": {"url": url, "ts": params.get("timestamp")},
                "handshake_request": None,
                "handshake_response": None,
                "frame_counts": {"sent": 0, "recv": 0},
                "error": None,
            },
        )
        self._records.setdefault(key, []).append(rec)
        self.ws_connections += 1
        return rec

    def _on_ws_created(self, sid: str, params: Mapping[str, Any]) -> None:
        rec = self._ws_record(sid, params)
        if rec is None:
            return
        self._write_ws_row(
            rec,
            {
                "event": "created",
                "url": rec.url,
                "ts": params.get("timestamp"),
            },
        )

    def _on_ws_handshake_request(self, sid: str, params: Mapping[str, Any]) -> None:
        rec = self._ws_record(sid, params)
        if rec is None or rec.ws is None:
            return
        request = params.get("request") or {}
        headers = request.get("headers")
        rec.ws["handshake_request"] = {
            "headers": headers,
            "wall_time": params.get("wallTime"),
        }
        self._write_ws_row(
            rec,
            {
                "event": "handshake_request",
                "url": rec.url,
                "headers": headers,
                "wall_time": params.get("wallTime"),
            },
        )

    def _on_ws_handshake_response(self, sid: str, params: Mapping[str, Any]) -> None:
        rec = self._ws_record(sid, params)
        if rec is None or rec.ws is None:
            return
        response = params.get("response") or {}
        rec.ws["handshake_response"] = {
            "status": response.get("status"),
            "status_text": response.get("statusText"),
            "headers": response.get("headers"),
        }
        self._write_ws_row(
            rec,
            {
                "event": "handshake_response",
                "url": rec.url,
                "status": response.get("status"),
                "headers": response.get("headers"),
            },
        )

    def _on_ws_frame(self, sid: str, params: Mapping[str, Any], direction: str) -> None:
        rec = self._active((sid, str(params.get("requestId") or "")))
        if rec is None or rec.ws is None:
            return  # 没建 WS 记录（被过滤）或已经收尾了 → 帧也不留
        frame = params.get("response") or {}
        opcode = int(frame.get("opcode") or 0)
        payload, nbytes, as_b64 = _ws_payload(frame.get("payloadData"), opcode)
        rec.ws["frame_counts"][direction] += 1
        if direction == "sent":
            self.ws_frames_sent += 1
        else:
            self.ws_frames_recv += 1
        self._write_ws_row(
            rec,
            {
                "event": "frame",
                "dir": direction,
                "opcode": opcode,
                "binary": opcode in (0, 2),
                "continuation": opcode == 0,
                "payload": payload,
                "payload_base64": as_b64,
                "payload_bytes": nbytes,
                "mask": frame.get("mask"),
                "ts": params.get("timestamp"),
            },
        )

    def _on_ws_frame_sent(self, sid: str, params: Mapping[str, Any]) -> None:
        self._on_ws_frame(sid, params, "sent")

    def _on_ws_frame_recv(self, sid: str, params: Mapping[str, Any]) -> None:
        self._on_ws_frame(sid, params, "recv")

    def _on_ws_error(self, sid: str, params: Mapping[str, Any]) -> None:
        rec = self._active((sid, str(params.get("requestId") or "")))
        if rec is None or rec.ws is None:
            return
        rec.ws["error"] = params.get("errorMessage")
        self._write_ws_row(
            rec,
            {"event": "error", "url": rec.url, "error_message": params.get("errorMessage")},
        )

    def _on_ws_closed(self, sid: str, params: Mapping[str, Any]) -> None:
        """WS 收尾。**不取正文**——WS 没有「响应体」，别去问 ``getResponseBody``。"""
        rec = self._active((sid, str(params.get("requestId") or "")))
        if rec is None or rec.ws is None:
            return
        self._write_ws_row(rec, {"event": "closed", "url": rec.url, "ts": params.get("timestamp")})
        ts = params.get("timestamp")
        if isinstance(ts, (int, float)):
            rec.timings["finished_ts"] = ts
        self._finish(rec, status="complete", fetch_body=False)

    def _write_ws_row(self, rec: RequestRecord, extra: Mapping[str, Any]) -> None:
        row: dict[str, Any] = {
            "session_id": rec.session_id,
            "request_id": rec.request_id,
            "target": self._target_of(rec.session_id) or rec.target,
        }
        row.update(extra)
        self._writer.write_ws(row)


    def _on_attached(self, sid: str, params: Mapping[str, Any]) -> None:
        new_sid = str(params.get("sessionId") or "")
        info = params.get("targetInfo") or {}
        self.note_target(new_sid, info)
        if self._on_attach_cb is not None:
            self._on_attach_cb(new_sid, info, bool(params.get("waitingForDebugger")))

    def _on_detached(self, sid: str, params: Mapping[str, Any]) -> None:
        self.drop_session(str(params.get("sessionId") or ""))

    def _on_target_info_changed(self, sid: str, params: Mapping[str, Any]) -> None:
        info = params.get("targetInfo") or {}
        tid = str(info.get("targetId") or "")
        target_sid = self._target_sessions.get(tid)
        if target_sid:
            self.note_target(target_sid, info)


    def _finish_redirect_hop(
        self, rec: RequestRecord, redirect_response: Mapping[str, Any]
    ) -> None:
        """把重定向的上一跳按一条完整请求写出去。3xx 没有正文，不取 ``getResponseBody``。"""
        headers = redirect_response.get("headers")
        rec.response["status"] = redirect_response.get("status")
        rec.response["status_text"] = redirect_response.get("statusText")
        if isinstance(headers, dict):
            rec.response.setdefault("headers", {str(k): str(v) for k, v in headers.items()})
        rec.redirect["is_redirect"] = True
        ts = redirect_response.get("responseTime")
        if isinstance(ts, (int, float)):
            rec.timings["finished_ts"] = ts
        self._finish(rec, status="complete", fetch_body=False)

    def _finish(
        self, rec: RequestRecord, *, status: str, fetch_body: bool = True
    ) -> None:
        """给一条记录分配 seq、取正文、写 index 行。**幂等**（``written`` 兜底）。"""
        if rec.written:
            return
        rec.status = status
        if not rec.captured_at:
            rec.captured_at = iso_now()
        rec.seq = self._writer.next_seq()

        if status == "complete" and fetch_body:
            self._capture_body(rec)
        if rec.post_data_raw is not None:
            content_type = get_header(rec.request_headers, "content-type")
            rec.post = self._writer.write_post(
                rec.seq, rec.url, content_type, data=rec.post_data_raw, note=rec.post_note
            )
        elif rec.post_note:
            rec.post = {
                "path": None,
                "bytes": 0,
                "content_type": "",
                "encoding": None,
                "base64": False,
                "note": rec.post_note,
            }

        fresh = self._target_of(rec.session_id)
        if fresh:
            rec.target = fresh
        self._writer.write_index(rec.to_row())
        rec.written = True
        self.requests_written += 1
        if status == "pending":
            self.pending += 1
        elif status == "failed":
            self.failed += 1

    def _capture_body(self, rec: RequestRecord) -> None:
        """在 ``loadingFinished`` 当刻把响应体抢下来——缓冲会被驱逐，晚一步就没了。

        取不到不是错误（``data:``/``blob:``/204/304/重定向/流式/已驱逐都会失败），
        记进 ``body.note`` 接着跑。
        """
        try:
            result = self._call(
                "Network.getResponseBody",
                {"requestId": rec.request_id},
                session_id=rec.session_id,
            )
        except CaptureError as exc:
            rec.body = {
                "path": None,
                "bytes": 0,
                "base64": False,
                "encoding": None,
                "note": f"取正文失败：{exc}",
            }
            return
        content_type = rec.content_type()
        if result.get("base64Encoded"):
            rec.body = self._writer.write_body(
                rec.seq, rec.url, content_type, b64=str(result.get("body") or "")
            )
        else:
            rec.body = self._writer.write_body(
                rec.seq, rec.url, content_type, text=str(result.get("body") or "")
            )

    def finalize(self, *, reason: str) -> CaptureSummary:
        """收尾：没结束的请求标 ``pending`` 写出去，补 meta，关文件。"""
        for hops in list(self._records.values()):
            for rec in hops:
                if not rec.written:
                    self._finish(rec, status="pending", fetch_body=False)
        self._records.clear()
        self._pending_req_extra.clear()
        self._pending_resp_extra.clear()
        self._writer.flush()
        summary = CaptureSummary(
            out_dir=self._writer.out_dir,
            stop_reason=reason,
            requests_total=self.requests_total,
            requests_filtered=self.requests_filtered,
            requests_written=self.requests_written,
            bodies_written=self._writer.bodies_written,
            body_bytes=self._writer.body_bytes,
            ws_connections=self.ws_connections,
            ws_frames_sent=self.ws_frames_sent,
            ws_frames_recv=self.ws_frames_recv,
            pending=self.pending,
            failed=self.failed,
        )
        meta = summary.to_dict()  # 含 stop_reason / out_dir
        meta.update(ended_at=iso_now(), filters=self._options.filters.to_dict())
        self._writer.write_meta(**meta)
        self._writer.close()
        return summary


def _ws_payload(payload: Any, opcode: int) -> tuple[str, int, bool]:
    """WS 帧载荷 → ``(落盘的字符串, 字节数, 是否 base64)``。

    CDP 约定 ``opcode=2``（二进制）的 ``payloadData`` 已经是 base64，所以原样留着、
    不做二次编码，留给后续自己解包。``opcode=0`` 是分片的续帧，按二进制处理。
    万一拿到的不是合法 base64（真按原始字节给了），退回按字面长度算，不猜。
    """
    text = payload if isinstance(payload, str) else ""
    if opcode in (0, 2):
        try:
            return text, len(base64.b64decode(text, validate=True)), True
        except (ValueError, TypeError):
            return text, len(text), False
    return text, len(text.encode("utf-8", "replace")), False


#: 事件方法名 → :class:`CaptureSession` 上的处理方法。
_EVENT_HANDLERS: dict[str, str] = {
    "Network.requestWillBeSent": "_on_request",
    "Network.requestWillBeSentExtraInfo": "_on_request_extra",
    "Network.responseReceived": "_on_response",
    "Network.responseReceivedExtraInfo": "_on_response_extra",
    "Network.loadingFinished": "_on_finished",
    "Network.loadingFailed": "_on_failed",
    "Network.webSocketCreated": "_on_ws_created",
    "Network.webSocketWillSendHandshakeRequest": "_on_ws_handshake_request",
    "Network.webSocketHandshakeResponseReceived": "_on_ws_handshake_response",
    "Network.webSocketFrameSent": "_on_ws_frame_sent",
    "Network.webSocketFrameReceived": "_on_ws_frame_recv",
    "Network.webSocketFrameError": "_on_ws_error",
    "Network.webSocketClosed": "_on_ws_closed",
    "Target.attachedToTarget": "_on_attached",
    "Target.detachedFromTarget": "_on_detached",
    "Target.targetInfoChanged": "_on_target_info_changed",
}




class _Pending:
    """一条等回包的 CDP 命令。"""

    __slots__ = ("method", "event", "result", "error")

    def __init__(self, method: str) -> None:
        self.method = method
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None


class CdpConnection:
    """browser 级 CDP 连接：一条读线程 + 一问一答的命令通道。

    读线程**只负责搬运**：有 ``id`` 的回包唤醒等它的人，有 ``method`` 的事件塞进队列。
    事件的真正处理在消费线程那边（:meth:`events` 的调用方），见模块 docstring。
    队列满时读线程阻塞在 ``put`` 上形成背压（Chrome 发不动就先攒着，不丢）。
    连接不发 Origin 头（Chrome 111+ 会对陌生 Origin 回 403；调试口是本地闭环）。
    """

    def __init__(
        self, ws_url: str, *, recv_timeout: float = 1.0, queue_max: int = 10_000
    ) -> None:
        try:
            import websocket  # 延迟导入：没装也只是用不了抓包，不影响别的
        except ImportError as exc:  # pragma: no cover
            raise CaptureError(
                "要走 CDP 得装 websocket-client：pip install websocket-client"
            ) from exc
        self._ws_mod = websocket
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._events: queue.Queue[Any] = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._close_reason = ""
        try:
            self._ws = websocket.create_connection(
                ws_url, timeout=recv_timeout, suppress_origin=True
            )
        except Exception as exc:  # noqa: BLE001
            raise CaptureError(f"连不上 CDP（{ws_url}）：{exc}") from exc


    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_reason(self) -> str:
        return self._close_reason

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._reader, name="cdp-capture-reader", daemon=True
        )
        self._thread.start()


    def _reader(self) -> None:
        websocket = self._ws_mod
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue  # 只是空闲，不是错——和 CdpClient.call 的判据不一样
            except websocket.WebSocketConnectionClosedException as exc:
                self._on_closed(f"连接被关闭：{exc}")
                return
            except OSError as exc:
                self._on_closed(f"socket 异常：{exc}")
                return
            except Exception as exc:  # noqa: BLE001 - 读线程崩了要收尾，不能裸奔
                self._on_closed(f"读线程异常：{exc}")
                return
            if not raw:
                self._on_closed("连接返回空帧")
                return
            try:
                message = json.loads(raw)
            except ValueError:
                logger.debug("CDP 帧不是 JSON，跳过：%r", str(raw)[:200])
                continue
            if not isinstance(message, dict):
                continue
            self._dispatch(message)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        """回包唤醒等它的人；事件塞队列。队列满就等着——读线程停住 = TCP 窗口收窄
        = Chrome 那边自然减速。
        """
        mid = message.get("id")
        if mid is not None:
            with self._pending_lock:
                pending = self._pending.pop(mid, None)
            if pending is None:
                return
            if "error" in message:
                err = message.get("error") or {}
                pending.error = CdpError(
                    f"CDP {pending.method} 报错：{err.get('message', err)}"
                )
            else:
                result = message.get("result")
                pending.result = result if isinstance(result, dict) else {}
            pending.event.set()
            return

        method = message.get("method")
        if not method:
            return
        item = (message.get("sessionId"), str(method), message.get("params") or {})
        while not self._stop.is_set():
            try:
                self._events.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def _on_closed(self, reason: str) -> None:
        """连接结束：唤醒所有正在等回包的人（否则消费线程要一直卡到超时），再放哨兵。"""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._close_reason = reason
        logger.info("CDP 连接结束：%s", reason)
        with self._pending_lock:
            pending, self._pending = list(self._pending.values()), {}
        for item in pending:
            item.error = CdpError(f"CDP 连接已关闭（{reason}）")
            item.event.set()
        self._stop.set()
        try:
            self._events.put(None, timeout=1.0)  # 哨兵；满了也没关系，events() 会兜底
        except queue.Full:
            pass


    def send_call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """发一条 CDP 命令并等它的回包。"""
        if self._closed:
            raise CdpError(f"CDP 连接已关闭（{self._close_reason}）")
        with self._id_lock:
            self._id += 1
            mid = self._id
        pending = _Pending(method)
        with self._pending_lock:
            self._pending[mid] = pending
        message: dict[str, Any] = {
            "id": mid,
            "method": method,
            "params": dict(params or {}),
        }
        if session_id:
            message["sessionId"] = session_id
        try:
            with self._send_lock:  # websocket-client 的 send 不保证线程安全
                self._ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            with self._pending_lock:
                self._pending.pop(mid, None)
            raise CdpError(f"CDP 发送失败（{method}）：{exc}") from exc
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(mid, None)
            raise CdpTimeout(f"CDP {method} 超时（{timeout}s）")
        if pending.error is not None:
            raise pending.error
        return pending.result or {}


    def events(self, *, idle_timeout: float = 0.5) -> Iterator[tuple[str | None, str, dict[str, Any]]]:
        """把事件一条条吐出来，直到连接关闭且队列排空。"""
        while True:
            try:
                item = self._events.get(timeout=idle_timeout)
            except queue.Empty:
                if self._stop.is_set() and self._events.empty():
                    return
                continue
            if item is None:
                return
            yield item

    def close(self) -> None:
        self._stop.set()
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 # pragma: no cover
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)




def _auto_attach_params(wait_for_debugger: bool) -> dict[str, Any]:
    return {
        "autoAttach": True,
        "waitForDebuggerOnStart": bool(wait_for_debugger),
        "flatten": True,
    }


def setup_session(
    conn: CdpConnection,
    session: CaptureSession,
    session_id: str,
    target_info: Mapping[str, Any],
    *,
    wait_for_debugger: bool = False,
    waiting: bool = False,
) -> None:
    """给一个 session 开网络监听。

    每个新 session 都要走一遍——包括 ``Target.attachedToTarget`` 事件带来的。
    这里**还要再发一次** ``Target.setAutoAttach``：那个命令的作用域是「所在 target 的
    直接子 target」，browser 级那次只覆盖顶层标签页，而 OOPIF 和 worker 是页面的
    孙子层，不在那个页面的 session 上再设一次就收不到。
    ``Runtime.runIfWaitingForDebugger`` 必须排在 ``Network.enable`` 之后放行，
    否则首屏那批请求照样漏。
    """
    if not session.claim_session(session_id):
        return
    session.note_target(session_id, target_info)
    try:
        conn.send_call("Network.enable", {}, session_id=session_id)
    except CaptureError as exc:
        logger.warning("Network.enable 失败（%s）：%s", target_info.get("type"), exc)
        return
    try:
        conn.send_call(
            "Target.setAutoAttach",
            _auto_attach_params(wait_for_debugger),
            session_id=session_id,
        )
    except CaptureError as exc:
        logger.debug("子 session 的 setAutoAttach 失败（%s）：%s", session_id, exc)
    if wait_for_debugger and waiting:
        try:
            conn.send_call("Runtime.runIfWaitingForDebugger", {}, session_id=session_id)
        except CaptureError as exc:
            logger.warning("放行被暂停的 target 失败（%s）：%s", session_id, exc)


def _bootstrap(
    conn: CdpConnection,
    session: CaptureSession,
    *,
    wait_for_debugger: bool,
    target_filter: list[dict[str, Any]] | None = None,
) -> None:
    """订阅发现 → 自动附着 → 把已经在的 target 逐个接上。

    autoAttach 要**先于**枚举：堵住「枚举 target 到 attach 之间」新开的那些。
    已被 autoAttach 接上的会走 ``attachedToTarget`` 事件，由 ``claim_session`` 去重。
    """
    try:
        conn.send_call("Target.setDiscoverTargets", {"discover": True})
    except CaptureError as exc:
        logger.debug("setDiscoverTargets 失败（不影响主体）：%s", exc)

    auto = _auto_attach_params(wait_for_debugger)
    if target_filter:
        try:
            conn.send_call("Target.setAutoAttach", {**auto, "filter": target_filter})
        except CaptureError as exc:
            logger.warning("setAutoAttach 不收 filter（%s），退回不带 filter 再试", exc)
            conn.send_call("Target.setAutoAttach", auto)
    else:
        conn.send_call("Target.setAutoAttach", auto)

    result = conn.send_call("Target.getTargets")
    for info in result.get("targetInfos") or []:
        if not isinstance(info, dict):
            continue
        if info.get("type") not in ATTACHABLE_TARGET_TYPES:
            continue
        try:
            attached = conn.send_call(
                "Target.attachToTarget",
                {"targetId": info.get("targetId"), "flatten": True},
            )
        except CaptureError as exc:
            # 已经被 DevTools 之类占住的 target 会回 error，跳过就好，别中断整体
            logger.debug("attach %s 失败（跳过）：%s", info.get("targetId"), exc)
            continue
        session_id = attached.get("sessionId")
        if session_id:
            setup_session(
                conn,
                session,
                str(session_id),
                info,
                wait_for_debugger=wait_for_debugger,
            )


def run_capture(
    *,
    ws_url: str,
    options: CaptureOptions,
    wait_for_debugger: bool = False,
    duration: float | None = None,
    max_requests: int | None = None,
    target_filter: list[dict[str, Any]] | None = None,
    on_start: Callable[[CaptureSession], None] | None = None,
) -> CaptureSummary:
    """附着到 ``ws_url``，抓到停止为止，返回汇总。**不关 Chrome。**"""
    writer = CaptureWriter(options)
    writer.write_meta(
        started_at=iso_now(),
        ws_url=ws_url,
        wait_for_debugger=bool(wait_for_debugger),
        filters=options.filters.to_dict(),
    )
    conn = CdpConnection(ws_url)
    session = CaptureSession(
        call=conn.send_call,
        writer=writer,
        options=options,
        on_attach=lambda sid, info, waiting: setup_session(
            conn, session, sid, info,
            wait_for_debugger=wait_for_debugger,
            waiting=waiting,
        ),
    )
    stop_reason = "未知"
    consumer: threading.Thread | None = None
    try:
        conn.start()
        _bootstrap(
            conn, session, wait_for_debugger=wait_for_debugger, target_filter=target_filter
        )
        if on_start is not None:
            on_start(session)
        consumer = threading.Thread(
            target=_consume,
            args=(conn, session),
            name="cdp-capture-consumer",
            daemon=True,
        )
        consumer.start()
        try:
            stop_reason = _wait_for_stop(conn, session, duration, max_requests)
        except KeyboardInterrupt:
            stop_reason = "Ctrl+C"
            print()  # 别和摘要挤在同一行
    finally:
        conn.close()
        if consumer is not None:
            consumer.join(timeout=5.0)
        summary = session.finalize(reason=stop_reason)
    return summary


def _consume(conn: CdpConnection, session: CaptureSession) -> None:
    for session_id, method, params in conn.events():
        try:
            session.handle_event(session_id, method, params)
        except Exception as exc:  # noqa: BLE001 - 单条事件处理失败不该拖垮整场抓包
            logger.warning("处理 %s 出错（已跳过）：%s", method, exc)


def _wait_for_stop(
    conn: CdpConnection,
    session: CaptureSession,
    duration: float | None,
    max_requests: int | None,
) -> str:
    deadline = (time.monotonic() + duration) if duration else None
    while True:
        if conn.closed:
            return f"CDP 连接关闭（{conn.close_reason}）"
        if deadline is not None and time.monotonic() >= deadline:
            return f"到达 --duration {duration}s"
        if max_requests is not None and session.requests_total >= max_requests:
            return f"到达 --max-requests {max_requests}"
        time.sleep(0.2)
