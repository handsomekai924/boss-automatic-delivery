"""职位列表的线上接口客户端。

调的是真实 wapi 路径（见 `boss_jobs.config`），不是界面模拟。
登录态复用 `boss_login` 落盘的 Cookie；核心是 `JobClient.crawl` 的
「抓一页 → 立刻清洗 → 立刻入库 → 睡够间隔 → 下一页」。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import requests

from . import config as C
from .chat import ChatCredentials, ChatSocket
from .errors import (
    ChatSendError,
    JobApiError,
    JobDataError,
    JobError,
    JobTransportError,
    dig_chat_remind,
    format_api_message,
)
from .models import ExpectList, PageResult, clean_expects, clean_page, extract_job_desc
from .store import JobStore, SaveOutcome, open_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrawlBlacklistContext:
    """web 抓取任务提供的不可变规则视图。

    职位客户端不碰 web 规则表；回调返回可序列化的命中快照。

    Attributes:
        task_id: 抓取任务号。
        store_blacklisted: 命中是否入库保留。
        has_jd_rules: 是否有 JD 规则。
        jd_scopes: JD 规则匹配范围（`job_name` / `job_desc` / `both`）。
        company_matches: 公司规则匹配回调。
        jd_matches: JD 规则匹配回调。
        record_hit: 命中记录回调。
    """

    task_id: str = ""
    store_blacklisted: bool = True
    has_jd_rules: bool = False
    jd_scopes: tuple[str, ...] = ()
    company_matches: Callable[[Any], list[dict[str, Any]]] | None = None
    jd_matches: Callable[[Any, str], list[dict[str, Any]]] | None = None
    record_hit: Callable[..., None] | None = None


def _jd_scope_flags(context: "CrawlBlacklistContext | None") -> tuple[bool, bool]:
    """按规则 scope 判断这轮能不能检查 JD，返回 `(can_check_name, requires_desc)`。"""
    if context is None or not context.has_jd_rules:
        return False, False
    scopes = set(context.jd_scopes or ()) or {"both"}
    can_check_name = bool(scopes & {"job_name", "both"})
    requires_desc = bool(scopes & {"job_desc", "both"})
    return can_check_name, requires_desc


@dataclass(frozen=True)
class CrawlStats:
    """一次翻页抓取的累计结果。

    Attributes:
        run_id: 本次运行 id。
        pages: 抓取页数。
        raw_count: 接口条数。
        kept_count: 洗后条数。
        dropped_count: 丢弃条数。
        inserted: 新增条数。
        updated: 更新条数。
        skipped: 跳过条数。
        company_hits: 公司规则命中数。
        jd_hits: JD 规则命中数。
        stopped_reason: 停止原因。
    """

    run_id: str
    pages: int = 0
    raw_count: int = 0
    kept_count: int = 0
    dropped_count: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    company_hits: int = 0
    jd_hits: int = 0
    stopped_reason: str = ""

    def add(self, result: PageResult, outcome: SaveOutcome, *, skipped: int = 0,
            company_hits: int = 0, jd_hits: int = 0) -> "CrawlStats":
        """把一页结果累加进统计，返回新的 `CrawlStats`。"""
        return CrawlStats(
            run_id=self.run_id,
            pages=self.pages + 1,
            raw_count=self.raw_count + result.raw_count,
            kept_count=self.kept_count + len(result.jobs),
            dropped_count=self.dropped_count + result.dropped_count,
            inserted=self.inserted + outcome.inserted,
            updated=self.updated + outcome.updated,
            skipped=self.skipped + skipped,
            company_hits=self.company_hits + company_hits,
            jd_hits=self.jd_hits + jd_hits,
            stopped_reason=self.stopped_reason,
        )

    def with_reason(self, reason: str) -> "CrawlStats":
        """返回带上停止原因的统计副本。"""
        return CrawlStats(
            run_id=self.run_id,
            pages=self.pages,
            raw_count=self.raw_count,
            kept_count=self.kept_count,
            dropped_count=self.dropped_count,
            inserted=self.inserted,
            updated=self.updated,
            skipped=self.skipped,
            company_hits=self.company_hits,
            jd_hits=self.jd_hits,
            stopped_reason=reason,
        )

    def summary_lines(self) -> list[str]:
        """人读摘要行（CLI / 日志用）。"""
        return [
            f"运行 id   {self.run_id}",
            f"抓取页数  {self.pages}",
            f"接口条数  {self.raw_count}",
            f"洗后条数  {self.kept_count}（丢弃 {self.dropped_count}）",
            f"入库条数  新增 {self.inserted} / 更新 {self.updated} / 跳过 {self.skipped}",
            f"黑名单命中  公司 {self.company_hits} / JD {self.jd_hits}",
            f"停止原因  {self.stopped_reason or '（未记录）'}",
        ]


@dataclass
class CrawlReport:
    """一次 crawl 跑完的明细：统计 + 每页流水。

    Attributes:
        stats: 累计统计。
        pages: 每页清洗结果。
    """

    stats: CrawlStats
    pages: list[PageResult] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        """人读摘要行（转给 `stats`）。"""
        return self.stats.summary_lines()


@dataclass(frozen=True)
class JobDetail:
    """一次职位详情调用的清洗结果。

    Attributes:
        job_desc: 清洗后的 JD 正文（可能为空串）。
        raw: 整包响应，便于排查字段名对不上。
    """

    job_desc: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_desc(self) -> bool:
        """是否抓到了 JD 正文。"""
        return bool(self.job_desc)


@dataclass(frozen=True)
class GreetResult:
    """一次打招呼（`friend/add.json`）的结果。

    只表示「会话建起来了」，不表示「话发出去了」；真投递用
    `JobClient.deliver_greeting`。

    Attributes:
        message: 服务端回的话术（留作日志）。
        raw: 整包响应，便于排查。
    """

    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BossData:
    """会话对象的身份信息。

    聊天消息的 `to` 要数字 uid，这里就是 `encryptBossId` 的换算结果。

    Attributes:
        uid: boss 的数字 uid，消息的 `to.uid`。
        source: boss 来源，消息的 `to.source`。
        name: boss 昵称（日志/展示用）。
        encrypt_boss_id: 加密 id（原样带回）。
        raw: `zpData.data` 整包。
    """

    uid: int
    source: int = 0
    name: str = ""
    encrypt_boss_id: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GreetingDelivery:
    """一次「真·打招呼」的结果。

    Attributes:
        boss_uid: 消息发给了谁（数字 uid）。
        boss_source: 对端的 source。
        temp_id: 本地消息 id（这一帧的 `mid`）。
        text: 实际发出去的正文。
    """

    boss_uid: int
    boss_source: int = 0
    temp_id: int = 0
    text: str = ""


class JobClient:
    """职位列表客户端。

    Args:
        base_url: 站点地址。
        endpoints: 覆盖 `config.ENDPOINTS` 里的路径（探路/测试用）。
        http: 带 `request()` 的会话对象，默认新建 `requests.Session`。
        timeout: 单次请求超时（秒）。
        retries: 失败重试次数（网络层 / 5xx）。
        backoff: 重试退避基数（秒），按 2 的幂递增。
        headers: 额外请求头。
        sleeper: 可替换的 sleep（测试里注入 no-op）。
        page_interval: 翻页硬间隔（秒），防风控。
        stoken_provider: `__zp_stoken__` 自动获取器（见 `boss_jobs.cdp_stoken`）。
        chat_host: 聊天 MQTT 网关主机。
        chat_port: 聊天 MQTT 网关端口。
        chat_path: 聊天 WebSocket path。
        chat_timeout: 聊天通道等 CONNACK 的超时（秒）。
        chat_push_wait: 等会话同步（`mid` 基数）的超时（秒）。
        chat_puback_wait: 发完顺带等 PUBACK 的超时（秒）；等不到是常态。
        chat_flush_wait: 正文发完再停多久才断（秒）。
    """

    def __init__(
        self,
        *,
        base_url: str = C.BASE_URL,
        endpoints: Mapping[str, str] | None = None,
        http: Any | None = None,
        timeout: float = C.DEFAULT_TIMEOUT,
        retries: int = C.DEFAULT_RETRIES,
        backoff: float = C.DEFAULT_BACKOFF,
        headers: Mapping[str, str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        page_interval: float = C.DEFAULT_PAGE_INTERVAL,
        stoken_provider: Any | None = None,
        chat_host: str = C.CHAT_WS_HOST,
        chat_port: int = C.CHAT_WS_PORT,
        chat_path: str = C.CHAT_WS_PATH,
        chat_timeout: float = C.CHAT_TIMEOUT,
        chat_push_wait: float = C.CHAT_PUSH_WAIT,
        chat_puback_wait: float = C.CHAT_PUBACK_WAIT,
        chat_flush_wait: float = C.CHAT_FLUSH_WAIT,
    ) -> None:
        """初始化。"""
        self.base_url = base_url.rstrip("/")
        self.endpoints = {**C.ENDPOINTS, **(endpoints or {})}
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.page_interval = max(0.0, page_interval)
        self._sleep = sleeper
        self.stoken_provider = stoken_provider
        self.chat_host = chat_host
        self.chat_port = chat_port
        self.chat_path = chat_path
        self.chat_timeout = chat_timeout
        self.chat_push_wait = chat_push_wait
        self.chat_puback_wait = chat_puback_wait
        self.chat_flush_wait = chat_flush_wait
        self._chat: ChatSocket | None = None
        self._chat_credentials: ChatCredentials | None = None
        self._chat_mid_base: int = 0

        self._http = http or requests.Session()
        merged = {**C.DEFAULT_HEADERS, **(headers or {})}
        if hasattr(self._http, "headers"):
            self._http.headers.update(merged)
        else:
            self._default_headers = merged


    def fetch_search_page(self, search_filter: Any, *, page: int | None = None) -> PageResult:
        """按搜索条件抓一页搜索结果并清洗。

        走 `search/joblist.json`；每次先补 `__zp_stoken__`，
        撞 code 37 时自动换新重试。条件页码可被 `page` 覆盖。
        """
        if page is not None:
            search_filter = search_filter.for_page(page)
        params = dict(search_filter.to_params())
        path = self.endpoints["job_search"]
        action = f"搜索第 {params.get('page', '?')} 页"
        payload = self._get_json_with_stoken_retry(path, params=params, action=action)
        return clean_page(payload, page=int(params.get("page", 1)))

    def fetch_expect_list(self) -> ExpectList:
        """拉当前账号的求职期望列表（推荐 / 兼职 / 我的期望）。

        只要登录 Cookie，刻意走裸 `_get_json`，不换 stoken 重试。
        """
        payload = self._get_json(
            self.endpoints["job_expect_list"],
            params={},
            action="求职期望列表",
        )
        return clean_expects(payload)

    def fetch_expect_page(self, expect_filter: Any, *, page: int | None = None) -> PageResult:
        """求职期望流一页（`recommend/job/list.json`）。

        与 `fetch_search_page` 同级，走 stoken 重试；只走 GET。
        """
        if page is not None:
            expect_filter = expect_filter.for_page(page)
        params = dict(expect_filter.to_params())
        payload = self._get_json_with_stoken_retry(
            self.endpoints["job_expect_flow"],
            params=params,
            action=f"期望流第 {params.get('page', '?')} 页",
        )
        return clean_page(payload, page=int(params.get("page", 1)))

    def _get_json_with_stoken_retry(self, path: str, params: dict[str, Any], action: str) -> Any:
        """带 stoken 重试的 GET JSON。"""
        return self._request_with_stoken_retry(
            lambda suffix: self._get_json(path, params=params, action=action + suffix),
            action=action,
        )

    def _request_with_stoken_retry(
        self,
        call: Callable[[str], dict[str, Any]],
        *,
        action: str,
    ) -> dict[str, Any]:
        """打一发；撞 code 37 先退避重试，还不行才强制换新。

        换新冷却中没换到令牌就不再打第三发，把 37 抛给上层停批。
        """
        self._ensure_stoken()
        try:
            return call("")
        except JobApiError as exc:
            if not exc.is_browser_check or self.stoken_provider is None:
                raise
            first = exc
        logger.info(
            "%s撞上安全网关 code 37，歇 %.1fs 拿同一枚 %s 重试",
            action,
            C.BROWSER_CHECK_BACKOFF,
            C.STOKEN_COOKIE,
        )
        self._sleep(C.BROWSER_CHECK_BACKOFF)
        try:
            return call("（歇会儿后）")
        except JobApiError as exc:
            if not exc.is_browser_check or self.stoken_provider is None:
                raise
            first = exc

        before = self._get_cookie(C.STOKEN_COOKIE)
        logger.info("歇完还 37，强制换新 %s 后再试一次", C.STOKEN_COOKIE)
        self._ensure_stoken(force=True)
        if self._get_cookie(C.STOKEN_COOKIE) == before:
            logger.info(
                "%s 没换到新令牌（换新冷却中），不打第三发，交给上层停批",
                C.STOKEN_COOKIE,
            )
            raise first
        return call("（补令牌后）")

    def _get_cookie(self, name: str) -> str:
        """读会话 cookiejar 里的一枚 Cookie；读不到回空串。"""
        jar = getattr(self._http, "cookies", None)
        if jar is None:
            return ""
        getter = getattr(jar, "get", None)
        if getter is None:
            return ""
        try:
            return str(getter(name) or "")
        except Exception:  # noqa: BLE001 - cookiejar 对非法字符会抛
            return ""

    def _ensure_stoken(self, *, force: bool = False) -> None:
        """有 `stoken_provider` 就先补一枚没过期的 `__zp_stoken__`。"""
        if self.stoken_provider is None:
            return
        token = self.stoken_provider.ensure(force=force)
        self._set_cookie(C.STOKEN_COOKIE, token)


    def fetch_job_detail(self, *, security_id: str, lid: str) -> JobDetail:
        """拉一条职位详情，抽出 JD 正文。

        query 只带 `securityId` + `lid`；也走 stoken 重试。
        """
        if not security_id:
            raise ValueError("security_id 不能为空")
        params: dict[str, Any] = {"securityId": security_id}
        if lid:
            params["lid"] = lid
        path = self.endpoints["job_detail"]
        payload = self._get_json_with_stoken_retry(path, params=params, action="职位详情")
        return JobDetail(job_desc=extract_job_desc(payload), raw=payload)

    def fetch_job_detail_for(
        self, job: Any, *, store: JobStore | None = None
    ) -> JobDetail:
        """对一条 `Job` 抓详情并立刻写回 `job_desc`。

        单条失败往上抛，调用方决定要不要继续。
        """
        detail = self.fetch_job_detail(security_id=job.security_id, lid=job.lid)
        if store is not None:
            store.update_job_desc(job.encrypt_job_id, detail.job_desc)
        return detail

    def _set_cookie(self, name: str, value: str) -> None:
        """把一枚 Cookie 稳稳写进会话（先清同名）。"""
        from .stoken import put_cookie

        jar = getattr(self._http, "cookies", None)
        if jar is None:
            return
        put_cookie(jar, name, value)


    def greet(
        self,
        *,
        security_id: str,
        encrypt_job_id: str,
        lid: str = "",
        encrypt_boss_id: str = "",
        session_id: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> GreetResult:
        """发一条打招呼（`friend/add.json`）：只建会话，不投递正文。

        撞「开聊提醒」弹窗时会自动模拟点击确认（埋点 + `cid=1` 重打），
        把剩余沟通次数用掉；确认完还被拦才抛出来。
        """
        if not security_id:
            raise ValueError("security_id 不能为空")
        if not encrypt_job_id:
            raise ValueError("encrypt_job_id 不能为空")

        params: dict[str, Any] = {"securityId": security_id, "jobId": encrypt_job_id}
        if lid:
            params["lid"] = lid

        body: dict[str, Any] = {}
        if encrypt_boss_id:
            body["encryptBossId"] = encrypt_boss_id
        if session_id:
            body["sessionId"] = session_id
        if extra:
            body.update({str(k): v for k, v in extra.items() if v is not None})

        action = f"打招呼 {encrypt_job_id}"
        path = self.endpoints["friend_add"]
        try:
            payload = self._request_with_stoken_retry(
                lambda suffix: self._request_json(
                    "POST", path, params=params, data=body, action=action + suffix
                ),
                action=action,
            )
        except JobApiError as exc:
            if not exc.is_chat_remind:
                raise
            if exc.is_chat_limit_exhausted:
                raise
            payload = self._confirm_chat_remind(
                exc,
                params=params,
                body=body,
                action=action,
            )
        return GreetResult(message=str(payload.get("message") or ""), raw=payload)

    def _confirm_chat_remind(
        self,
        exc: JobApiError,
        *,
        params: Mapping[str, Any],
        body: Mapping[str, Any],
        action: str,
    ) -> dict[str, Any]:
        """「开聊提醒」弹窗的模拟点击确认。

        补两发埋点并带 `cid=1` 重打一次 `friend/add`；埋点失败不挡确认。
        """
        dialog = exc.chat_remind or {}
        ba = str(dialog.get("ba") or "")
        self._log_chat_remind({"action": "addf-limit-popup-c", "ba": ba})

        confirm_body = {
            str(k): v
            for k, v in dict(body).items()
            if v is not None and str(k) not in {"securityId", "jobId", "lid"}
        }
        confirm_body["cid"] = C.CHAT_REMIND_CONFIRM_CID
        path = self.endpoints["friend_add"]
        payload = self._request_with_stoken_retry(
            lambda suffix: self._request_json(
                "POST",
                path,
                params=params,
                data=confirm_body,
                action=f"{action}（模拟点「好」）{suffix}",
            ),
            action=f"{action}（模拟点「好」）",
        )
        self._log_chat_remind(
            {"action": "addf-limit-popup-connect", "ba": ba, "p8": "11"}
        )
        logger.info(
            "%s 撞「开聊提醒」已模拟确认（cid=%s），会话建起来了；弹窗原话：%s",
            action,
            C.CHAT_REMIND_CONFIRM_CID,
            exc.message,
        )
        return payload

    def _log_chat_remind(self, data: Mapping[str, Any]) -> None:
        """补一发「开聊提醒」弹窗埋点；失败不抛。"""
        try:
            self._request_json(
                "POST",
                self.endpoints["chatremind_log"],
                data=dict(data),
                action="开聊提醒埋点",
            )
        except Exception:  # noqa: BLE001 - 埋点失败不影响确认
            logger.debug("开聊提醒埋点没打上", exc_info=True)


    def fetch_wt(self) -> str:
        """取 MQTT 密码：`get/wt` → `zpData.wt2`。"""
        payload = self._get_json(self.endpoints["get_wt"], action="取聊天凭据 wt")
        wt = str((payload.get("zpData") or {}).get("wt2") or "")
        if not wt:
            raise ChatSendError("取聊天凭据失败：get/wt 没回 wt2")
        return wt

    def fetch_me(self) -> ChatCredentials:
        """取自己的聊天身份（token + userId）并打包登录 Cookie。

        聊天 WebSocket 握手必须带 Cookie。
        """
        payload = self._get_json(self.endpoints["get_user_info"], action="取登录用户")
        data = payload.get("zpData") or {}
        user_id = int(data.get("userId") or 0)
        token = str(data.get("token") or "")
        if not user_id or not token:
            raise ChatSendError("取聊天凭据失败：getUserInfo 没回 userId/token")
        return ChatCredentials(
            user_id=user_id,
            token=token,
            wt=self.fetch_wt(),
            cookie=self._cookie_header(),
            name=str(data.get("name") or ""),
        )

    def fetch_boss_data(self, encrypt_boss_id: str) -> BossData:
        """把 `encryptBossId` 换成聊天要用的数字 uid。

        要先 `greet` 建了会话才查得到。
        """
        if not encrypt_boss_id:
            raise ValueError("encrypt_boss_id 不能为空")
        payload = self._get_json_with_stoken_retry(
            self.endpoints["get_boss_data"],
            params={"bossId": encrypt_boss_id},
            action="取会话对象",
        )
        data = (payload.get("zpData") or {}).get("data") or {}
        uid = int(data.get("bossId") or 0)
        if not uid:
            raise ChatSendError(f"{encrypt_boss_id} 没换到 boss uid（会话可能没建起来）")
        return BossData(
            uid=uid,
            source=int(data.get("bossSource") or 0),
            name=str(data.get("name") or ""),
            encrypt_boss_id=str(data.get("encryptBossId") or encrypt_boss_id),
            raw=data,
        )

    def fetch_chat_history(self, boss_uid: int | str) -> list[dict[str, Any]]:
        """回读某个会话的历史消息。

        当不了送达判据；传对方数字 uid，不是 encryptBossId。
        """
        payload = self._get_json_with_stoken_retry(
            self.endpoints["chat_history"],
            params={"bossId": boss_uid},
            action="回读聊天记录",
        )
        messages = (payload.get("zpData") or {}).get("messages") or []
        return [m for m in messages if isinstance(m, dict)]

    def open_chat(self, credentials: ChatCredentials | None = None) -> ChatSocket:
        """拿到一条聊天 MQTT 连接；已连上就复用，断了就重建。

        整批复用凭据与 `mid` 基数，不复用半死连接。
        """
        if self._chat is not None and self._chat.connected:
            return self._chat
        if self._chat is not None:
            self._stash_chat_state(self._chat)
            self._chat.close()
        creds = credentials or self._chat_credentials or self.fetch_me()
        self._chat_credentials = creds
        self._chat = ChatSocket(
            creds,
            host=self.chat_host,
            port=self.chat_port,
            path=self.chat_path,
            timeout=self.chat_timeout,
            push_wait=self.chat_push_wait,
            puback_wait=self.chat_puback_wait,
            flush_wait=self.chat_flush_wait,
            mid_base=self._chat_mid_base,
            sleeper=self._sleep,
        )
        self._chat.connect()
        return self._chat

    def _stash_chat_state(self, chat: ChatSocket) -> None:
        """收下这条连接见过的 `mid` 基数，重建时带给下一条（只抬不降）。"""
        seen = int(getattr(chat, "max_msg_id", 0) or 0)
        if seen > self._chat_mid_base:
            logger.debug("mid 基数抬到 %d（带给下一条连接）", seen)
            self._chat_mid_base = seen

    def close_chat(self) -> None:
        """断开聊天连接（幂等）。整批发完 / 任务收尾时调。"""
        if self._chat is not None:
            self._stash_chat_state(self._chat)
            self._chat.close()
            self._chat = None

    def deliver_greeting(
        self,
        *,
        security_id: str,
        encrypt_job_id: str,
        lid: str = "",
        encrypt_boss_id: str = "",
        greeting: str = "",
        session_id: str = "",
    ) -> GreetingDelivery:
        """真·打招呼：建会话 → 换 boss uid → MQTT 发正文。

        成功判据是「帧发出去了」，不是「等到 PUBACK」。
        正文为空抛 `ChatSendError`；建会话失败抛 `JobApiError`。
        """
        text = (greeting or "").strip()
        if not text:
            raise ChatSendError("没有招呼语正文，不发空消息（这条跳过或先补一句招呼语）")

        started = time.monotonic()
        self.greet(
            security_id=security_id,
            encrypt_job_id=encrypt_job_id,
            lid=lid,
            encrypt_boss_id=encrypt_boss_id,
            session_id=session_id,
        )
        after_greet = time.monotonic()
        boss = self.fetch_boss_data(encrypt_boss_id)
        after_boss = time.monotonic()
        chat = self.open_chat()
        after_open = time.monotonic()
        temp_id = chat.send_text(
            to_uid=boss.uid,
            text=text,
            to_source=boss.source,
            to_name=boss.encrypt_boss_id or encrypt_boss_id,
        )
        sent_at = time.monotonic()
        stats = getattr(chat, "last_send_stats", None) or {}
        logger.info(
            "打招呼 %s 耗时 %.2fs：建会话 %.2f + 换uid %.2f + 开通道 %.2f + 发帧 %.2f"
            "（等推送 %.2f / 收尾 %.2f）",
            encrypt_job_id,
            sent_at - started,
            after_greet - started,
            after_boss - after_greet,
            after_open - after_boss,
            sent_at - after_open,
            float(stats.get("wait_push", 0.0)),
            float(stats.get("flush", 0.0)),
        )
        return GreetingDelivery(
            boss_uid=boss.uid,
            boss_source=boss.source,
            temp_id=temp_id,
            text=text,
        )

    def _cookie_header(self) -> str:
        """把会话 Cookie 拼成握手用的 `Cookie` 头值。"""
        jar = getattr(self._http, "cookies", None)
        if jar is None:
            return ""
        try:
            items = list(jar)
        except TypeError:
            return ""
        return "; ".join(
            f"{c.name}={c.value}" for c in items if getattr(c, "value", None) is not None
        )


    def crawl(
        self,
        *,
        store: JobStore | None = None,
        max_pages: int = C.DEFAULT_MAX_PAGES,
        page_interval: float | None = None,
        start_page: int = 1,
        db_path: Path | str | None = None,
        search_filter: Any | None = None,
        expect_filter: Any | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        enrich_details: bool = False,
        detail_interval: float | None = None,
        blacklist_context: CrawlBlacklistContext | None = None,
        task_id: str = "",
    ) -> CrawlReport:
        """分页抓取职位，每页即时清洗入库，页与页之间硬睡一会。

        Args:
            store: 已打开的职位库；不传则按 `db_path` 现开。
            max_pages: 最多翻几页；0 = 翻到接口回空页。
            page_interval: 覆盖构造时的翻页间隔（秒）。
            start_page: 起始页码，续跑时用。
            db_path: 不传 `store` 时的库路径。
            search_filter: 搜索流条件；给了就走 `search/joblist.json`。
            expect_filter: 期望流条件；给了就走 `recommend/job/list.json`。
            on_progress: 每页入库后的进度回调。
            should_stop: 翻页前调一次；返回 True 则协作式收手。
            enrich_details: 每页入库后顺带补 JD。
            detail_interval: 补详情的条间隔（秒）。
            blacklist_context: 规则评估上下文；给了就边抓边筛。
            task_id: 抓取任务号，非空时记到任务归属表。

        Returns:
            `CrawlReport`（统计 + 每页明细）。

        两条流二选一（expect 优先），两个都不给抛 `ValueError`。
        收手判据：空页，或不满页且 `hasMore=false`。
        """
        interval = self.page_interval if page_interval is None else max(0.0, page_interval)
        detail_gap = (
            C.DETAIL_INTERVAL if detail_interval is None else max(0.0, detail_interval)
        )
        use_expect = expect_filter is not None
        use_search = search_filter is not None
        if not use_expect and not use_search:
            raise ValueError(
                "必须提供 expect_filter 或 search_filter："
                "推荐页 special/zone 检索流已下线，「全部不限」抓取不再支持"
            )
        own_store = store is None
        store = store or open_store(db_path)
        run_id = uuid.uuid4().hex[:12]
        stats = CrawlStats(run_id=run_id)
        report = CrawlReport(stats=stats)
        reason = "抓到空页"

        def _push(result: PageResult, outcome: SaveOutcome, extra: dict[str, Any] | None = None) -> None:
            """把一页进度回调出去。"""
            if on_progress is None:
                return
            payload: dict[str, Any] = {
                "run_id": run_id,
                "page": result.page,
                "raw_count": result.raw_count,
                "kept_count": len(result.jobs),
                "dropped_count": result.raw_count - len(result.jobs),
                "inserted": outcome.inserted,
                "updated": outcome.updated,
                "has_more": result.has_more,
            }
            if extra:
                payload.update(extra)
            on_progress(payload)

        def _push_detail(payload: dict[str, Any]) -> None:
            """把详情进度事件回调出去。"""
            if on_progress is None:
                return
            on_progress({"run_id": run_id, **payload})

        try:
            page = start_page
            while True:
                if should_stop is not None and should_stop():
                    reason = "收到停止信号"
                    break

                if max_pages and (page - start_page) >= max_pages:
                    reason = f"达到 max_pages={max_pages}"
                    break

                if use_expect:
                    result = self.fetch_expect_page(expect_filter, page=page)
                else:
                    result = self.fetch_search_page(search_filter, page=page)
                if blacklist_context is None:
                    outcome = store.save_page(result, task_id=task_id)
                    report.pages.append(result)
                    stats = stats.add(result, outcome)
                    report.stats = stats
                    _push(result, outcome)
                    if enrich_details:
                        self._enrich_page_details(
                            result, store=store, interval=detail_gap,
                            on_detail=_push_detail, should_stop=should_stop,
                        )
                else:
                    company_hits: list[dict[str, Any]] = []
                    candidates = []
                    skipped_ids: set[str] = set()
                    for job in result.jobs:
                        hits = blacklist_context.company_matches(job) if blacklist_context.company_matches else []
                        company_hits.extend(hits)
                        if hits and not blacklist_context.store_blacklisted:
                            skipped_ids.add(job.encrypt_job_id)
                            for hit in hits:
                                if blacklist_context.record_hit:
                                    blacklist_context.record_hit(job, hit, action="skipped", jd_status="unchecked")
                            _push_detail({"event": "company_blacklist_skipped", "encrypt_job_id": job.encrypt_job_id,
                                          "job_name": job.job_name, "brand_name": job.brand_name})
                            continue
                        if hits:
                            for h in hits:
                                if blacklist_context.record_hit:
                                    blacklist_context.record_hit(
                                        job, h, action="stored",
                                        jd_status=str(job.jd_status or "pending"),
                                    )
                            job = replace(
                                job,
                                company_blacklisted=True,
                                is_blacklisted=True,
                                blacklist_types=tuple(sorted(set(job.blacklist_types + ("company",)))),
                                matched_rule_ids=tuple(str(h.get("rule_id") or "") for h in hits),
                                matched_rule_snapshot=tuple(hits),
                                auto_delivery_blocked=True,
                            )
                        candidates.append(job)

                    page_result = PageResult(
                        page=result.page, jobs=tuple(candidates), has_more=result.has_more,
                        raw_count=result.raw_count, dropped=result.dropped, lid=result.lid,
                    )
                    jd_hits = 0
                    if enrich_details:
                        page_result, jd_hits = self._enrich_page_details(
                            page_result, store=store, interval=detail_gap, on_detail=_push_detail,
                            should_stop=should_stop, blacklist_context=blacklist_context,
                        )
                    else:
                        can_check_name, requires_desc = _jd_scope_flags(blacklist_context)
                        has_jd_rules = bool(blacklist_context.has_jd_rules)
                        name_only = has_jd_rules and can_check_name and not requires_desc
                        checked = (not has_jd_rules) or name_only
                        finalized = []
                        for j in page_result.jobs:
                            name_hits: list[dict[str, Any]] = []
                            if name_only and blacklist_context.jd_matches:
                                name_hits = blacklist_context.jd_matches(j, "")
                            for h in name_hits:
                                if blacklist_context.record_hit:
                                    blacklist_context.record_hit(
                                        j, h,
                                        action="skipped" if not blacklist_context.store_blacklisted else "stored",
                                        jd_status="unchecked",
                                    )
                                jd_hits += 1
                            if name_hits and not blacklist_context.store_blacklisted:
                                continue
                            missing_company = not str(j.brand_name or "").strip()
                            if name_hits:
                                prior = tuple(j.matched_rule_snapshot or ())
                                merged = prior + tuple(hits for hits in name_hits if hits not in prior)
                                j = replace(
                                    j,
                                    jd_blacklisted=True,
                                    is_blacklisted=True,
                                    blacklist_types=tuple(sorted(set(j.blacklist_types + ("jd_keyword",)))),
                                    matched_rule_ids=tuple(str(h.get("rule_id") or "") for h in merged),
                                    matched_rule_snapshot=merged,
                                )
                            if has_jd_rules and not checked:
                                next_status = "unchecked"
                            else:
                                next_status = j.jd_status or ("unchecked" if has_jd_rules else "pending")
                            j = replace(
                                j,
                                jd_status=next_status,
                                jd_rule_checked=checked,
                                auto_delivery_blocked=bool(
                                    j.company_blacklisted
                                    or j.jd_blacklisted
                                    or missing_company
                                    or (has_jd_rules and not checked)
                                ),
                            )
                            finalized.append(j)
                        page_result = PageResult(
                            page=page_result.page,
                            jobs=tuple(finalized),
                            has_more=page_result.has_more, raw_count=page_result.raw_count,
                            dropped=page_result.dropped, lid=page_result.lid,
                        )

                    outcome = store.save_page(page_result, task_id=task_id)
                    for job in page_result.jobs:
                        store.update_blacklist_state(
                            job.encrypt_job_id,
                            company_blacklisted=bool(job.company_blacklisted),
                            jd_blacklisted=bool(job.jd_blacklisted),
                            matched_rules=[dict(rule) for rule in job.matched_rule_snapshot],
                            jd_rule_checked=bool(job.jd_rule_checked),
                            jd_status=str(job.jd_status or "pending"),
                            auto_delivery_blocked=bool(job.auto_delivery_blocked),
                            checked_at=job.blacklist_checked_at or None,
                        )
                    report.pages.append(page_result)
                    stats = stats.add(result, outcome, skipped=len(skipped_ids),
                                      company_hits=len(company_hits), jd_hits=jd_hits)
                    report.stats = stats
                    _push(result, outcome, {"skipped": len(skipped_ids), "company_hits": len(company_hits), "jd_hits": jd_hits})

                if result.is_empty:
                    reason = f"第 {page} 页接口回空列表"
                    break
                if not result.has_more and result.raw_count < C.PAGE_SIZE:
                    reason = f"第 {page} 页不满页且 hasMore=false"
                    break

                page += 1
                if max_pages and (page - start_page) >= max_pages:
                    reason = f"达到 max_pages={max_pages}"
                    break
                if interval > 0:
                    logger.debug("翻页间隔 %.1fs 后要第 %s 页", interval, page)
                    self._sleep(interval)
        except JobError:
            report.stats = stats.with_reason("接口报错，已入库页保留")
            raise
        finally:
            report.stats = stats.with_reason(reason) if not report.stats.stopped_reason else report.stats
            if own_store:
                store.close()

        logger.info("抓取结束：%s", " / ".join(report.stats.summary_lines()))
        return report

    def _enrich_page_details(
        self,
        result: PageResult,
        *,
        store: JobStore,
        interval: float = C.DETAIL_INTERVAL,
        on_detail: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        blacklist_context: CrawlBlacklistContext | None = None,
    ) -> tuple[PageResult, int]:
        """补抓 JD 并在落库前做规则评估。

        返回 `(本页结果, JD 命中数)`；单条失败只记流水，不中断列表抓取。
        """
        def _emit(payload: dict[str, Any]) -> None:
            """发一条详情事件。"""
            if on_detail is not None:
                on_detail(payload)

        already = store.fetched_desc_ids([job.encrypt_job_id for job in result.jobs])
        done = 0
        consecutive_37 = 0
        kept: list[Any] = []
        jd_hit_count = 0
        for index, job in enumerate(result.jobs):
            existing = store.get_job(job.encrypt_job_id)
            if job.encrypt_job_id in already:
                job = replace(
                    job,
                    job_desc=existing.job_desc if existing else job.job_desc,
                    detail_fetched_at=existing.detail_fetched_at if existing else job.detail_fetched_at,
                    jd_status=existing.jd_status if existing else job.jd_status,
                    jd_error=existing.jd_error if existing else job.jd_error,
                    jd_rule_checked=existing.jd_rule_checked if existing else job.jd_rule_checked,
                    jd_rule_checked_at=existing.jd_rule_checked_at if existing else job.jd_rule_checked_at,
                    is_blacklisted=existing.is_blacklisted if existing else job.is_blacklisted,
                    company_blacklisted=existing.company_blacklisted if existing else job.company_blacklisted,
                    jd_blacklisted=existing.jd_blacklisted if existing else job.jd_blacklisted,
                    blacklist_types=existing.blacklist_types if existing else job.blacklist_types,
                    matched_rule_ids=existing.matched_rule_ids if existing else job.matched_rule_ids,
                    matched_rule_snapshot=existing.matched_rule_snapshot if existing else job.matched_rule_snapshot,
                    blacklist_checked_at=existing.blacklist_checked_at if existing else job.blacklist_checked_at,
                    auto_delivery_blocked=existing.auto_delivery_blocked if existing else job.auto_delivery_blocked,
                )
                _emit({"event": "detail_skipped", "encrypt_job_id": job.encrypt_job_id,
                       "job_name": job.job_name, "reason": "已有详情"})
                if blacklist_context and blacklist_context.has_jd_rules and blacklist_context.jd_matches:
                    can_check_name, requires_desc = _jd_scope_flags(blacklist_context)
                    can_check_desc = bool(job.job_desc) and requires_desc
                    can_check_jd = can_check_name or can_check_desc
                    checked = bool(job.job_desc) or (can_check_name and not requires_desc)
                    hits = (
                        blacklist_context.jd_matches(job, job.job_desc or "")
                        if can_check_jd
                        else []
                    )
                    if hits:
                        job = replace(
                            job,
                            jd_blacklisted=True,
                            is_blacklisted=True,
                            blacklist_types=tuple(sorted(set(job.blacklist_types + ("jd_keyword",)))),
                            matched_rule_ids=tuple(str(h.get("rule_id") or "") for h in hits),
                            matched_rule_snapshot=tuple(hits),
                            auto_delivery_blocked=True,
                            jd_rule_checked=checked,
                        )
                        for hit in hits:
                            if blacklist_context.record_hit:
                                blacklist_context.record_hit(
                                    job, hit, action="skipped" if not blacklist_context.store_blacklisted else "stored",
                                    jd_status=job.jd_status,
                                )
                        jd_hit_count += len(hits)
                    else:
                        job = replace(
                            job,
                            jd_blacklisted=False,
                            is_blacklisted=bool(job.company_blacklisted),
                            blacklist_types=tuple(t for t in job.blacklist_types if t != "jd_keyword"),
                            matched_rule_ids=tuple(
                                rid for rid, snap in zip(job.matched_rule_ids, job.matched_rule_snapshot)
                                if snap.get("rule_type") != "jd_keyword"
                            ) if job.matched_rule_ids else (),
                            matched_rule_snapshot=tuple(
                                snap for snap in job.matched_rule_snapshot
                                if snap.get("rule_type") != "jd_keyword"
                            ),
                            jd_rule_checked=checked,
                            auto_delivery_blocked=bool(
                                job.company_blacklisted or not checked or not str(job.brand_name or "").strip()
                            ),
                        )

            elif should_stop is not None and should_stop():
                job = replace(job, jd_status="pending", auto_delivery_blocked=True)
                _emit({"event": "detail_stopped", "reason": "收到停止信号"})
            else:
                try:
                    detail = self.fetch_job_detail(security_id=job.security_id, lid=job.lid)
                except Exception as exc:  # noqa: BLE001 - 详情失败不拖垮列表抓取
                    logger.warning("补抓 JD 失败 %s / %s：%s", job.encrypt_job_id, job.job_name, exc)
                    failed_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    job = replace(
                        job,
                        jd_status="failed",
                        jd_error=str(exc),
                        detail_fetched_at=failed_at,
                        jd_rule_checked=False,
                        auto_delivery_blocked=True,
                    )
                    store.update_job_desc(
                        job.encrypt_job_id,
                        "",
                        fetched_at=failed_at,
                        jd_status="failed",
                        jd_error=str(exc),
                        jd_rule_checked=False,
                        jd_rule_checked_at="",
                    )
                    _emit({"event": "detail_error", "encrypt_job_id": job.encrypt_job_id,
                           "job_name": job.job_name, "message": str(exc)})
                    if isinstance(exc, JobApiError) and exc.is_browser_check:
                        consecutive_37 += 1
                        if consecutive_37 >= C.BROWSER_CHECK_GIVEUP:
                            _emit({"event": "detail_stopped", "reason": f"连续 {consecutive_37} 次撞安全网关 code 37，已停补 JD"})
                            kept.append(job)
                            kept.extend(replace(rest, jd_status="pending", auto_delivery_blocked=True)
                                        for rest in result.jobs[index + 1:])
                            break
                        self._sleep(C.BROWSER_CHECK_COOLOFF)
                else:
                    consecutive_37 = 0
                    can_check_name, requires_desc = _jd_scope_flags(blacklist_context)
                    can_check_desc = bool(detail.job_desc) and requires_desc
                    can_check_jd = can_check_name or can_check_desc
                    has_jd_rules = bool(blacklist_context and blacklist_context.has_jd_rules)
                    checked = (not has_jd_rules) or bool(detail.job_desc) or (can_check_name and not requires_desc)
                    jd_status = "completed" if detail.job_desc else "empty"
                    job = replace(job, job_desc=detail.job_desc, detail_fetched_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), jd_status=jd_status,
                                  jd_error="", jd_rule_checked=checked,
                                  auto_delivery_blocked=bool(has_jd_rules and not checked))
                    store.update_job_desc(
                        job.encrypt_job_id,
                        detail.job_desc,
                        fetched_at=job.detail_fetched_at,
                        jd_status=jd_status,
                        jd_error="",
                        jd_rule_checked=checked,
                        jd_rule_checked_at=job.detail_fetched_at if checked else "",
                    )
                    if can_check_jd and blacklist_context and blacklist_context.jd_matches:
                        hits = blacklist_context.jd_matches(job, detail.job_desc or "")
                        if hits:
                            jd_hit_count += len(hits)
                            job = replace(job, jd_blacklisted=True, is_blacklisted=True,
                                          blacklist_types=tuple(sorted(set(job.blacklist_types + ("jd_keyword",)))),
                                          matched_rule_ids=tuple(str(h.get("rule_id") or "") for h in hits),
                                          matched_rule_snapshot=tuple(hits), auto_delivery_blocked=True)
                            for hit in hits:
                                if blacklist_context.record_hit:
                                    blacklist_context.record_hit(job, hit, action="skipped" if not blacklist_context.store_blacklisted else "stored",
                                                                jd_status=jd_status)
                            _emit({"event": "jd_blacklist_hit", "encrypt_job_id": job.encrypt_job_id,
                                   "job_name": job.job_name, "stored": blacklist_context.store_blacklisted})
                    _emit({"event": "detail_done", "encrypt_job_id": job.encrypt_job_id,
                           "job_name": job.job_name, "has_desc": bool(detail.job_desc),
                           "jd_status": jd_status, "desc_len": len(detail.job_desc)})
                done += 1
                if interval > 0 and done < len(result.jobs):
                    self._sleep(interval)

            company_hits: list[dict[str, Any]] = []
            if blacklist_context and blacklist_context.company_matches:
                company_hits = blacklist_context.company_matches(job)
            has_jd_rules = bool(blacklist_context and blacklist_context.has_jd_rules)
            jd_hits_now = (
                [dict(hit) for hit in job.matched_rule_snapshot if hit.get("rule_type") == "jd_keyword"]
                if has_jd_rules
                else []
            )
            matched_rules = company_hits + jd_hits_now
            company_blacklisted = bool(company_hits)
            jd_blacklisted = bool(jd_hits_now)
            types = {str(h.get("rule_type")) for h in matched_rules if h.get("rule_type")}
            checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            can_check_name, requires_desc = _jd_scope_flags(blacklist_context)
            checked = (not has_jd_rules) or bool(job.job_desc) or (can_check_name and not requires_desc)
            requires_jd = has_jd_rules
            missing_company = not str(job.brand_name or "").strip()
            blocked = bool(
                company_blacklisted
                or jd_blacklisted
                or missing_company
                or (requires_jd and not checked)
                or (requires_jd and job.jd_status in {"failed", "pending", "collecting", "unchecked"})
            )
            job = replace(job, company_blacklisted=company_blacklisted, jd_blacklisted=jd_blacklisted,
                          is_blacklisted=company_blacklisted or jd_blacklisted,
                          blacklist_types=tuple(sorted(types)),
                          matched_rule_ids=tuple(str(h.get("rule_id") or "") for h in matched_rules),
                          matched_rule_snapshot=tuple(matched_rules), blacklist_checked_at=checked_at,
                          jd_rule_checked=checked, jd_rule_checked_at=checked_at if checked else "",
                          auto_delivery_blocked=blocked)
            if jd_blacklisted and blacklist_context and not blacklist_context.store_blacklisted:
                continue
            kept.append(job)

        return PageResult(page=result.page, jobs=tuple(kept), has_more=result.has_more,
                          raw_count=result.raw_count, dropped=result.dropped, lid=result.lid), jd_hit_count

    def iter_pages(
        self,
        *,
        search_filter: Any | None = None,
        expect_filter: Any | None = None,
        max_pages: int = C.DEFAULT_MAX_PAGES,
        page_interval: float | None = None,
        start_page: int = 1,
    ) -> Iterator[PageResult]:
        """只翻页不入库，页与页之间照样睡（离线分析/测试用）。

        路由与 `crawl` 同款两分支；两个 filter 都不给抛 `ValueError`。
        """
        use_expect = expect_filter is not None
        if not use_expect and search_filter is None:
            raise ValueError(
                "必须提供 expect_filter 或 search_filter："
                "推荐页 special/zone 检索流已下线，「全部不限」抓取不再支持"
            )
        interval = self.page_interval if page_interval is None else max(0.0, page_interval)
        page = start_page
        yielded = 0
        while True:
            if max_pages and yielded >= max_pages:
                return
            if use_expect:
                result = self.fetch_expect_page(expect_filter, page=page)
            else:
                result = self.fetch_search_page(search_filter, page=page)
            yield result
            yielded += 1
            if result.is_empty:
                return
            if not result.has_more and result.raw_count < C.PAGE_SIZE:
                return
            page += 1
            if interval > 0:
                self._sleep(interval)


    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        action: str = "",
    ) -> dict[str, Any]:
        """GET 一发 JSON。"""
        return self._request_json("GET", path, params=params, action=action)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        action: str = "",
    ) -> dict[str, Any]:
        """打一发（GET / POST…）并把业务码翻译成异常。

        每发都补 `zp_token` 头；`data` 非空时按 form 发。
        """
        url = self.base_url + path
        headers = dict(getattr(self, "_default_headers", None) or {})
        zp_token = self._get_cookie(C.ZP_TOKEN_COOKIE)
        if zp_token:
            headers[C.ZP_TOKEN_HEADER] = zp_token
        headers = headers or None
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            if attempt:
                delay = self.backoff * (2 ** (attempt - 1))
                logger.debug("%s 第 %s 次重试，等待 %.1fs", action or path, attempt, delay)
                self._sleep(delay)
            try:
                raw = self._http.request(
                    method,
                    url,
                    params=dict(params or {}),
                    data=dict(data) if data else None,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = JobTransportError(f"{action or path} 请求失败：{exc}")
                continue

            status = getattr(raw, "status_code", 0)
            if status >= 500:
                last_error = JobTransportError(f"{action or path} 服务端错误 HTTP {status}")
                continue

            try:
                payload = raw.json()
            except ValueError as exc:
                last_error = JobTransportError(f"{action or path} 响应不是 JSON：{exc}")
                continue

            if path == self.endpoints.get("friend_add"):
                print(f"新建会话原始响应：{payload}", flush=True)

            if not isinstance(payload, dict):
                last_error = JobDataError(f"{action or path} 响应不是 JSON 对象")
                continue

            code = _biz_code(payload)
            if code != C.CODE_OK:
                message = format_api_message(payload)
                raise JobApiError(code, message or f"{action or path} 返回异常码 {code}", raw=payload)

            return payload

        raise last_error or JobTransportError(f"{action or path} 请求失败")


def http_from_session(
    session_path: Path | str | None = None,
    *,
    http: Any | None = None,
    stoken: str | None = None,
) -> Any:
    """用 `boss_login` 落盘的登录态搭一个会话。

    没有可用登录态时抛 `JobApiError`。`__zp_stoken__` 取值顺序：
    显式传入 → 库里的 cookies → 环境变量；都没有就先不带，
    由 `CdpStokenProvider` 自动补。
    """
    from boss_login.session import load_session

    import boss_db

    path = boss_db.resolve_db_path(session_path)
    stored = load_session(path)
    if stored.is_empty:
        raise JobApiError(
            7,
            f"本地没有登录态：状态库 {path} 里没有，或 token/Cookie 都没有。"
            f"先跑 `python -m boss_login login` 落一份。",
        )

    sess = http or requests.Session()
    for name, value in stored.cookies.items():
        if hasattr(sess, "cookies") and hasattr(sess.cookies, "set"):
            sess.cookies.set(name, value)

    from .stoken import put_cookie

    token = stoken or stored.cookies.get(C.STOKEN_COOKIE) or os.environ.get(C.STOKEN_ENV, "")
    if token and hasattr(sess, "cookies") and hasattr(sess.cookies, "set"):
        put_cookie(sess.cookies, C.STOKEN_COOKIE, token)
        logger.debug("已带上 %s（长度 %d）", C.STOKEN_COOKIE, len(token))
    else:
        logger.debug(
            "会话里还没有 %s：搜索接口会先回 code 37，由 CdpStokenProvider 拉 Chrome 算一枚补上。",
            C.STOKEN_COOKIE,
        )
    return sess


def create_client(
    *,
    session_path: Path | str | None = None,
    page_interval: float = C.DEFAULT_PAGE_INTERVAL,
    base_url: str = C.BASE_URL,
    sleeper: Callable[[float], None] = time.sleep,
    stoken: str | None = None,
    auto_stoken: bool = True,
    **kwargs: Any,
) -> JobClient:
    """一步拿到带登录态的 `JobClient`。

    `auto_stoken=True` 时挂上 `CdpStokenProvider` 自动补
    `__zp_stoken__`（真 Chrome 算，不是 Node 硬算）。
    """
    import boss_db

    http = http_from_session(session_path, stoken=stoken)
    provider = None
    if auto_stoken:
        from .cdp_stoken import CdpStokenProvider

        provider = CdpStokenProvider(
            http=http,
            session_path=boss_db.resolve_db_path(session_path),
        )
    return JobClient(
        base_url=base_url,
        http=http,
        page_interval=page_interval,
        sleeper=sleeper,
        stoken_provider=provider,
        **kwargs,
    )


def _biz_code(payload: Mapping[str, Any]) -> int:
    """从响应里取业务码；取不到回 -1。"""
    raw = payload.get("code", payload.get("status", C.CODE_OK))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1
