"""职位列表的线上接口客户端。

调的是真实 wapi 路径（见 :mod:`boss_jobs.config`），不是界面模拟。
登录态复用 ``boss_login`` 落盘的 Cookie（状态库 ``doc('session')``）——
职位列表**要登录**，Cookie 过期时接口回 code 7。

核心是 :meth:`JobClient.crawl`：**抓一页 → 立刻清洗 → 立刻入库 → 睡够间隔
→ 再要下一页**。清洗和入库从不攒批，睡眠是硬间隔，用来压请求频率、
避开风控；中途挂掉时已入库的页不丢。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
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
)
from .models import PageResult, clean_page, extract_job_desc
from .store import JobStore, SaveOutcome, open_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrawlStats:
    """一次翻页抓取的累计结果。"""

    run_id: str
    pages: int = 0
    raw_count: int = 0
    kept_count: int = 0
    dropped_count: int = 0
    inserted: int = 0
    updated: int = 0
    stopped_reason: str = ""

    def add(self, result: PageResult, outcome: SaveOutcome) -> "CrawlStats":
        return CrawlStats(
            run_id=self.run_id,
            pages=self.pages + 1,
            raw_count=self.raw_count + result.raw_count,
            kept_count=self.kept_count + len(result.jobs),
            dropped_count=self.dropped_count + result.dropped_count,
            inserted=self.inserted + outcome.inserted,
            updated=self.updated + outcome.updated,
            stopped_reason=self.stopped_reason,
        )

    def with_reason(self, reason: str) -> "CrawlStats":
        return CrawlStats(
            run_id=self.run_id,
            pages=self.pages,
            raw_count=self.raw_count,
            kept_count=self.kept_count,
            dropped_count=self.dropped_count,
            inserted=self.inserted,
            updated=self.updated,
            stopped_reason=reason,
        )

    def summary_lines(self) -> list[str]:
        return [
            f"运行 id   {self.run_id}",
            f"抓取页数  {self.pages}",
            f"接口条数  {self.raw_count}",
            f"洗后条数  {self.kept_count}（丢弃 {self.dropped_count}）",
            f"入库条数  新增 {self.inserted} / 更新 {self.updated}",
            f"停止原因  {self.stopped_reason or '（未记录）'}",
        ]


@dataclass
class CrawlReport:
    """一次 crawl 跑完的明细：统计 + 每页流水。"""

    stats: CrawlStats
    pages: list[PageResult] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        return self.stats.summary_lines()


@dataclass(frozen=True)
class JobDetail:
    """一次职位详情调用的清洗结果。

    :param job_desc: 清洗后的 JD 正文（可能为空串——有的职位就是没写）
    :param raw: 整包响应，便于排查字段名对不上
    """

    job_desc: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_desc(self) -> bool:
        return bool(self.job_desc)


@dataclass(frozen=True)
class GreetResult:
    """一次打招呼（``friend/add.json``）的结果。

    接口回 ``code 0`` 即算成功（失败会由 :meth:`JobClient._request_json` 抛
    :class:`~boss_jobs.errors.JobApiError`，所以能构造出这个对象就是成功了）。

    **注意它只表示「会话建起来了」，不表示「话发出去了」**：这条接口不带正文。
    要真投递招呼语，用 :meth:`JobClient.deliver_greeting`。

    :param message: 服务端回的话术（成功时通常是无意义文案，留作日志）
    :param raw: 整包响应，便于排查
    """

    message: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BossData:
    """会话对象的身份信息（``GET /wapi/zpchat/geek/getBossData``）。

    聊天消息的 ``to`` 要的是**数字 uid**，不是 ``encryptBossId``，这里就是那次换算。

    :param uid: boss 的数字 uid（响应里的 ``bossId``），消息的 ``to.uid``
    :param source: boss 来源（``bossSource``），消息的 ``to.source``
    :param name: boss 昵称（日志/展示用）
    :param encrypt_boss_id: 加密 id（原样带回，省得调用方再拼）
    :param raw: ``zpData.data`` 整包
    """

    uid: int
    source: int = 0
    name: str = ""
    encrypt_boss_id: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GreetingDelivery:
    """一次「真·打招呼」的结果（见 :meth:`JobClient.deliver_greeting`）。

    :param boss_uid: 消息发给了谁（数字 uid）
    :param boss_source: 对端的 source
    :param temp_id: 本地消息 id（就是这一帧的 ``mid`` / ``cmid``）
    :param text: 实际发出去的正文
    """

    boss_uid: int
    boss_source: int = 0
    temp_id: int = 0
    text: str = ""


class JobClient:
    """职位列表客户端。

    :param base_url: 站点地址
    :param endpoints: 覆盖 :data:`config.ENDPOINTS` 里的路径（探路/测试用）
    :param http: 带 ``request()`` 的会话对象，默认新建 ``requests.Session``
    :param timeout: 单次请求超时（秒）
    :param retries: 失败重试次数（网络层 / 5xx）
    :param backoff: 重试退避基数（秒），按 2 的幂递增
    :param sleeper: 可替换的 sleep（测试里注入 no-op）
    :param page_interval: 翻页硬间隔（秒），默认 1.0，防风控
    :param stoken_provider: ``__zp_stoken__`` 自动获取器（见 :mod:`boss_jobs.cdp_stoken`）。
        每次搜索前会 ``ensure()`` 判过期、过期了自己换新；撞上 code 37 时会
        ``ensure(force=True)`` 强制再换一枚重试。不传就按老规矩报错。
    :param chat_host / chat_port / chat_path: 聊天 MQTT 网关（见
        :mod:`boss_jobs.chat`），默认取 :mod:`boss_jobs.config` 里的实测值。
    :param chat_timeout: 聊天通道等 CONNACK 的超时（秒）
    :param chat_push_wait: 连上后等那帧**会话同步**的超时（秒）——发消息的
        ``mid`` 基数从里面取（见 :func:`boss_jobs.chat.max_message_id`）
    :param chat_flush_wait: 正文发出去之后再停多久才断（秒）
    :param chat_puback_wait: 正文发完顺带等 PUBACK 的超时（秒）——**只记日志**：
        这条网关对文本帧不回 PUBACK，等不到是常态、不算失败（见
        :attr:`boss_jobs.config.CHAT_PUBACK_WAIT`）
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
        #: 本批的聊天连接（:meth:`open_chat` 懒建，:meth:`close_chat` 断）
        self._chat: ChatSocket | None = None

        self._http = http or requests.Session()
        merged = {**C.DEFAULT_HEADERS, **(headers or {})}
        if hasattr(self._http, "headers"):
            self._http.headers.update(merged)
        else:  # 简易假会话：退化成每次请求都带
            self._default_headers = merged

    # ------------------------------------------------------------------ #
    # 单页
    # ------------------------------------------------------------------ #

    def fetch_page(self, page: int = 1) -> dict[str, Any]:
        """打一页职位列表，返回完整响应体（未清洗）。

        :param page: 页码，**从 1 开始**（接口认 page=0，但回空列表）
        """
        if page < 1:
            raise ValueError(f"页码从 1 开始，收到 {page}")
        path = self.endpoints["job_list"]
        params = {**C.REQUEST_PARAMS, "page": page}
        return self._get_json(path, params=params, action=f"职位列表第 {page} 页")

    def fetch_page_clean(self, page: int = 1) -> PageResult:
        """抓一页并**立刻清洗**（不落库）。"""
        payload = self.fetch_page(page)
        return clean_page(payload, page=page)

    # ------------------------------------------------------------------ #
    # 搜索接口：JobSearchFilter → fetch_search_page
    # ------------------------------------------------------------------ #

    def fetch_search_page(self, search_filter: Any, *, page: int | None = None) -> PageResult:
        """按 :class:`~boss_filter.search.JobSearchFilter` 抓**一页搜索结果**并清洗。

        打的是 ``/wapi/zpgeek/search/joblist.json``（见 :data:`config.ENDPOINTS`）。
        与 :meth:`fetch_page` 的区别：这条是**关键词 + 筛选条件**的搜索流，
        而 ``fetch_page`` 走的是推荐页那条 ``special/zone`` 流。

        每次进来都先让 :class:`~boss_jobs.cdp_stoken.CdpStokenProvider`
        **判一次 ``__zp_stoken__`` 过期**：没过期直接用，过期了自动拉 Chrome
        （CDP）让站点自己算一枚、落盘、再带上去打接口。撞上安全网关
        （code 37）时还会**强制**换一枚重试，不需要人工回浏览器拷。

        :param search_filter: ``JobSearchFilter``；只要能出 ``to_params()`` 也行
        :param page: 覆盖条件里的页码（翻页时省得自己改条件）
        :raises JobApiError: code 37 且自动补令牌也救不回来时，会明说缺 ``__zp_stoken__``；
            code 36 是账号风控，要人机验证
        """
        if page is not None:
            search_filter = search_filter.for_page(page)
        params = dict(search_filter.to_params())
        path = self.endpoints["job_search"]
        action = f"搜索第 {params.get('page', '?')} 页"
        payload = self._get_json_with_stoken_retry(path, params=params, action=action)
        return clean_page(payload, page=int(params.get("page", 1)))

    def _get_json_with_stoken_retry(self, path: str, params: dict[str, Any], action: str) -> Any:
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
        """打一发；撞 code 37 时**先歇一下拿同一枚重试**，还不行才强制换新。

        37 有两类原因：令牌不对、或者**请求太快 / 被安全网关拦着**。
        实测（2026-10-08）连环 code 37 时换新**没用**——刚换的令牌一样被拒，
        这时正确动作是**别再撞**，而不是多打一发。所以：

        - 先退避 2s 拿同一枚重试（「太快了」这一支）；
        - 还 37 再 ``ensure(force=True)``；**没真换到新令牌**（换新冷却中）
          就不再打第三发，把上一个 37 抛给调用方去停整批。

        :param call: 收一个后缀（拼进日志/action）并真的打一发；GET 搜索、POST
            打招呼都走这儿，重试策略一处维护。
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
            # 冷却里没真换到新令牌：同一枚刚被拒过，再打一发纯属撞墙
            logger.info(
                "%s 没换到新令牌（换新冷却中），不打第三发，交给上层停批",
                C.STOKEN_COOKIE,
            )
            raise first
        return call("（补令牌后）")

    def _get_cookie(self, name: str) -> str:
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
        """有 :attr:`stoken_provider` 就先补一枚没过期的 ``__zp_stoken__``。

        详情接口跟搜索一样站在安全网关后面，缺令牌回 code 37。
        """
        if self.stoken_provider is None:
            return
        token = self.stoken_provider.ensure(force=force)
        self._set_cookie(C.STOKEN_COOKIE, token)

    # ------------------------------------------------------------------ #
    # 职位详情（JD 正文）
    # ------------------------------------------------------------------ #

    def fetch_job_detail(self, *, security_id: str, lid: str) -> JobDetail:
        """拉一条职位详情，抽出 JD 正文。

        打的是 ``/wapi/zpgeek/job/detail.json``（见 :data:`config.ENDPOINTS`），
        query 只带 ``securityId`` + ``lid``（调用方 ``tc({securityId, lid})`` 就这
        两参形态）。列表接口不回 JD，正文在 ``zpData.jobInfo.postDescription``。

        详情跟搜索一样要 ``__zp_stoken__``（实测 code 37），所以这里也走
        :meth:`_get_json_with_stoken_retry`（撞 37 先退避再换新）。

        :param security_id: 职位的 ``securityId``（列表 item 里有）
        :param lid: 列表 item / 页级 ``lid``
        :raises JobApiError: code 37 且自动补令牌也救不回来、code 36 账号风控等
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
        """对一条 :class:`~boss_jobs.models.Job` 抓详情并**立刻写回** ``job_desc``。

        单条失败往上抛，调用方决定要不要继续下一条（抓取流程里会只记流水）。
        """
        detail = self.fetch_job_detail(security_id=job.security_id, lid=job.lid)
        if store is not None:
            store.update_job_desc(job.encrypt_job_id, detail.job_desc)
        return detail

    def _set_cookie(self, name: str, value: str) -> None:
        from .stoken import put_cookie  # 延迟导入，避免跟 stoken 硬绑

        jar = getattr(self._http, "cookies", None)
        if jar is None:
            return
        put_cookie(jar, name, value)

    # ------------------------------------------------------------------ #
    # 打招呼 / 加好友
    # ------------------------------------------------------------------ #

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
        """发一条打招呼（``POST /wapi/zpgeek/friend/add.json``）。

        **这条只建会话、不投递正文**——已实测（2026-10-08）：body 里带
        ``greeting`` 服务端直接忽略，回 ``code 0`` 但聊天框还是空的。
        真正的招呼语正文要走聊天通道（:meth:`deliver_greeting`，见
        :mod:`boss_jobs.chat`）。

        形态：query 带 ``securityId`` / ``jobId`` / ``lid``，body 走
        ``x-www-form-urlencoded`` 透传 ``encryptBossId`` / ``sessionId``。

        跟搜索 / 详情一样要 ``__zp_stoken__``，走同一套「先退避再换新」的重试。

        :param security_id: 职位的 ``securityId``（列表 item 里有，**整串**别截断）
        :param encrypt_job_id: 职位加密 id（``jobId`` 参数）
        :param lid: 列表 / 详情里的 ``lid``，有就带
        :param encrypt_boss_id: ``raw_json.encryptBossId``（
            :attr:`~boss_jobs.models.Job.encrypt_boss_id`）
        :param session_id: 详情响应下发的 ``zpData.sessionId``，有就带
        :param extra: 额外的 form 字段（透传，值为 ``None`` 的丢掉）
        :raises JobApiError: code 36（账号风控，要人工处理）、code 1/7（登录失效）
            等非成功码
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
        payload = self._request_with_stoken_retry(
            lambda suffix: self._request_json(
                "POST", path, params=params, data=body, action=action + suffix
            ),
            action=action,
        )
        return GreetResult(message=str(payload.get("message") or ""), raw=payload)

    # ------------------------------------------------------------------ #
    # 聊天通道（建会话 + 真发招呼语正文）
    # ------------------------------------------------------------------ #

    def fetch_wt(self) -> str:
        """取 MQTT 密码：``GET /wapi/zppassport/get/wt`` → ``zpData.wt2``。"""
        payload = self._get_json(self.endpoints["get_wt"], action="取聊天凭据 wt")
        wt = str((payload.get("zpData") or {}).get("wt2") or "")
        if not wt:
            raise ChatSendError("取聊天凭据失败：get/wt 没回 wt2")
        return wt

    def fetch_me(self) -> ChatCredentials:
        """取自己的聊天身份：``token``（MQTT 用户名）+ ``userId``/``name``。

        另外把会话 Cookie 一起打包——聊天 WebSocket 握手**必须带 Cookie**
        （不带回 HTTP 403，实测）。
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
        """把 ``encryptBossId`` 换成**聊天要用的数字 uid**。

        打 ``GET /wapi/zpchat/geek/getBossData?bossId={encryptBossId}``
        （query 参数就叫 ``bossId``，但值是那串加密 id）。回 ``zpData.data``：
        ``bossId``（数字 uid）、``bossSource``（消息里的 ``to.source``）。

        **要先 :meth:`greet` 建了会话才查得到**——没会话回 code 1
        「聊天的Boss不存在」/「非好友关系」。
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
        """回读某个会话的历史消息（``GET /wapi/zpchat/geek/historyMsg``）。

        :param boss_uid: **对方的数字 uid**（``getBossData`` 的 ``bossId``），
            **不是** ``encryptBossId``——2026-10-08 实测：传加密串回
            **code 19「参数值错误」**，传数字 uid 回 code 0。

        **拿它核对送达是不行的**：2026-10-08 实测，对有消息、且刚确认送达到的
        会话，这个接口回 ``code 0`` + 空 ``zpData``（连 ``messages`` 都没有）。
        所以它现在只当「能不能读到历史」的探针留着，**不作为发送判据**。
        """
        payload = self._get_json_with_stoken_retry(
            self.endpoints["chat_history"],
            params={"bossId": boss_uid},
            action="回读聊天记录",
        )
        messages = (payload.get("zpData") or {}).get("messages") or []
        return [m for m in messages if isinstance(m, dict)]

    def open_chat(self, credentials: ChatCredentials | None = None) -> ChatSocket:
        """拿到一条聊天 MQTT 连接（:attr:`_chat` 缓存）；已连上就复用。

        缓存的意义只是省掉 ``getUserInfo`` / ``get/wt`` 两次 HTTP——
        :meth:`~boss_jobs.chat.ChatSocket.send_text` 发完会主动断开
        （网关自己也断），所以下一条进来时这里多半是「重建」而不是「复用」。
        """
        from .chat import ChatSocket  # 延迟导入，聊天通道只在真发消息时才碰

        if self._chat is not None and self._chat.connected:
            return self._chat
        if self._chat is not None:
            self._chat.close()
        self._chat = ChatSocket(
            credentials or self.fetch_me(),
            host=self.chat_host,
            port=self.chat_port,
            path=self.chat_path,
            timeout=self.chat_timeout,
            push_wait=self.chat_push_wait,
            puback_wait=self.chat_puback_wait,
            flush_wait=self.chat_flush_wait,
            sleeper=self._sleep,
        )
        self._chat.connect()
        return self._chat

    def close_chat(self) -> None:
        """断开聊天连接（幂等）。整批发完 / 任务收尾时调。"""
        if self._chat is not None:
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
        """**真·打招呼**：建会话 → 换 boss uid → MQTT 发正文。

        三步走（每步都能单独失败，失败往上抛，调用方记这条发送失败）：

        1. :meth:`greet` —— ``friend/add.json`` 建会话（幂等，已建过也无妨）；
        2. :meth:`fetch_boss_data` —— ``encryptBossId`` → 数字 ``uid`` / ``source``；
        3. :meth:`open_chat` + :meth:`~boss_jobs.chat.ChatSocket.send_text`
           —— MQTT 发正文。

        第 3 步的**成功判据是「帧发出去了」**（PUBLISH 无异常、``rc == 0``），
        **不是「等到 PUBACK」**：这条网关对文本帧根本不回 PUBACK，发完约 150ms
        直接把连接关掉——**这是它的常态，不是拒收**（2026-10-08 实测的那几发
        站点上都显示「[送达]」，见 :mod:`boss_jobs.chat` 模块头）。所以
        :meth:`~boss_jobs.chat.ChatSocket.send_text` 只把 PUBACK 记进日志，
        等不到不抛。``mid`` 由 :class:`boss_jobs.chat.ChatSocket` 自己算
        （基数取服务端推的会话同步里的最大消息 id，见
        :func:`boss_jobs.chat.max_message_id`），调用方不用管。

        **不再回读聊天记录核对**：``GET /wapi/zpchat/geek/historyMsg`` 对这条
        账号返回 ``code 0`` + 空 ``zpData``——**有消息、刚确认送达的会话也读不
        出来**（2026-10-08 实测），拿它当判据只会把成功报成失败。

        :param greeting: 招呼语正文。**空的不发**（抛 :class:`ChatSendError`）——
            站点那条 ``friend/add`` 只建会话，不带正文的「打招呼」在 App 里
            看着像打了、聊天框其实是空的，正是本次要修的症状。
        :raises ChatSendError: 正文为空 / 换不到 boss uid / 通道发失败
        :raises JobApiError: 建会话那步的登录态失效 / 账号风控
        """
        text = (greeting or "").strip()
        if not text:
            raise ChatSendError("没有招呼语正文，不发空消息（这条跳过或先补一句招呼语）")

        self.greet(
            security_id=security_id,
            encrypt_job_id=encrypt_job_id,
            lid=lid,
            encrypt_boss_id=encrypt_boss_id,
            session_id=session_id,
        )
        boss = self.fetch_boss_data(encrypt_boss_id)
        chat = self.open_chat()
        temp_id = chat.send_text(
            to_uid=boss.uid,
            text=text,
            to_source=boss.source,
            # 站点前端 to.name 塞的是会话对象的 encryptUid，这里能拿到同源的
            # encryptBossId（跟站点一样：有值就写，空串就不写这个字段）。
            to_name=boss.encrypt_boss_id or encrypt_boss_id,
        )
        return GreetingDelivery(
            boss_uid=boss.uid,
            boss_source=boss.source,
            temp_id=temp_id,
            text=text,
        )

    def _cookie_header(self) -> str:
        """把会话 Cookie 拼成握手用的 ``Cookie`` 头值。"""
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

    # ------------------------------------------------------------------ #
    # 翻页：抓 → 洗 → 存 → 睡
    # ------------------------------------------------------------------ #

    def crawl(
        self,
        *,
        store: JobStore | None = None,
        max_pages: int = C.DEFAULT_MAX_PAGES,
        page_interval: float | None = None,
        start_page: int = 1,
        db_path: Path | str | None = None,
        search_filter: Any | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        enrich_details: bool = False,
        detail_interval: float | None = None,
    ) -> CrawlReport:
        """分页抓取职位，**每页即时清洗入库**，页与页之间硬睡一会。

        :param store: 已打开的职位库；不传则按 ``db_path``（或默认路径）现开
        :param max_pages: 最多翻几页；0 = 翻到接口回空页
        :param page_interval: 覆盖构造时的翻页间隔（秒）
        :param start_page: 起始页码，续跑时用
        :param db_path: 不传 ``store`` 时的库路径
        :param search_filter: ``JobSearchFilter``（或任何能出 ``to_params()``/``for_page()``）。
            **给了就走搜索流** ``/wapi/zpgeek/search/joblist.json``，条件照搬；
            不给就走原来的推荐流 ``special/zone``（无筛选）。
            筛选条件一般从配置文件读，见 :func:`boss_filter.load_search_filter`。
        :param on_progress: 每页入库后回调 ``{page, raw_count, kept_count,
            inserted, updated, has_more, stopped_reason?}``；网页控制台用来推进度。
            ``enrich_details`` 开着时还会穿插 ``{event: "detail_*", …}`` 事件
            （**不带** ``page`` / 计数键，不会把页数搅浑）。
        :param should_stop: 翻页前（含起始页）调一次；返回 ``True`` 则协作式收手，
            已入库的页保留。适合后台任务的取消按钮。
        :param enrich_details: 每页入库后顺带补 JD（默认关，网页抓取流程会开）。
            单条详情失败只发一条 ``detail_error`` 事件，**不**中断列表抓取。
        :param detail_interval: 补详情的条间隔（秒），默认 :data:`config.DETAIL_INTERVAL`
        :return: :class:`CrawlReport`（统计 + 每页明细）
        """
        interval = self.page_interval if page_interval is None else max(0.0, page_interval)
        detail_gap = (
            C.DETAIL_INTERVAL if detail_interval is None else max(0.0, detail_interval)
        )
        own_store = store is None
        store = store or open_store(db_path)
        run_id = uuid.uuid4().hex[:12]
        stats = CrawlStats(run_id=run_id)
        report = CrawlReport(stats=stats)
        reason = "抓到空页"
        use_search = search_filter is not None

        def _push(result: PageResult, outcome: SaveOutcome, extra: dict[str, Any] | None = None) -> None:
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

                if use_search:
                    result = self.fetch_search_page(search_filter, page=page)
                else:
                    result = self.fetch_page_clean(page)
                outcome = store.save_page(result)  # 立刻入库
                report.pages.append(result)
                stats = stats.add(result, outcome)
                report.stats = stats
                _push(result, outcome)

                if enrich_details:
                    self._enrich_page_details(
                        result,
                        store=store,
                        interval=detail_gap,
                        on_detail=_push_detail,
                        should_stop=should_stop,
                    )

                if result.is_empty:
                    reason = f"第 {page} 页接口回空列表"
                    break
                # hasMore 在 page=1 上不可靠（实测常回 false 却还有后续页），
                # 所以只在「不满页 + 明说没有下一页」时提前收手。
                if not result.has_more and result.raw_count < C.PAGE_SIZE:
                    reason = f"第 {page} 页不满页且 hasMore=false"
                    break

                page += 1
                # 下一页会被 max_pages 拦下的话，就别白睡这一秒
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
    ) -> None:
        """一页入库后顺带把 JD 补上。

        **已经有描述的不再重复拉**（重抓一页时尤其重要：已有 JD 的不该再打
        一遍详情接口）。只对 ``detail_fetched_at`` 还空着的补。

        **单条失败不拖垮列表抓取**：只发一条 ``detail_error`` 事件就下一条。
        描述抓到了也立刻 ``update_job_desc`` 落库，中途断了不丢。
        """
        def _emit(payload: dict[str, Any]) -> None:
            if on_detail is not None:
                on_detail(payload)

        already = store.fetched_desc_ids([job.encrypt_job_id for job in result.jobs])
        todo = [job for job in result.jobs if job.encrypt_job_id not in already]
        if already:
            logger.debug(
                "本页 %d 条里 %d 条已有描述，跳过；只补剩下 %d 条",
                len(result.jobs),
                len(already),
                len(todo),
            )

        done = 0
        # 连续撞安全网关的计数：连环 N 次就别再砸了（见 C.BROWSER_CHECK_GIVEUP）
        consecutive_37 = 0
        for job in result.jobs:
            if job.encrypt_job_id in already:
                _emit(
                    {
                        "event": "detail_skipped",
                        "encrypt_job_id": job.encrypt_job_id,
                        "job_name": job.job_name,
                        "reason": "已有描述",
                    }
                )
                continue
            if should_stop is not None and should_stop():
                _emit({"event": "detail_stopped", "reason": "收到停止信号"})
                return
            try:
                detail = self.fetch_job_detail(
                    security_id=job.security_id, lid=job.lid
                )
            except Exception as exc:  # noqa: BLE001 - 详情失败不拖垮列表抓取
                logger.warning(
                    "补抓 JD 失败 %s / %s：%s", job.encrypt_job_id, job.job_name, exc
                )
                _emit(
                    {
                        "event": "detail_error",
                        "encrypt_job_id": job.encrypt_job_id,
                        "job_name": job.job_name,
                        "message": str(exc),
                    }
                )
                if isinstance(exc, JobApiError) and exc.is_browser_check:
                    consecutive_37 += 1
                    if consecutive_37 >= C.BROWSER_CHECK_GIVEUP:
                        # 整段被限速了：停掉本页的补 JD，列表抓取本身照常走
                        logger.warning(
                            "连续 %d 次撞安全网关 code 37，本页补 JD 停手；"
                            "过几分钟再试（别在这时候继续砸）",
                            consecutive_37,
                        )
                        _emit(
                            {
                                "event": "detail_stopped",
                                "reason": (
                                    f"连续 {consecutive_37} 次撞安全网关 code 37，"
                                    "已停补 JD；过几分钟再试"
                                ),
                            }
                        )
                        return
                    # 限速墙抬手前多躺一会儿，别拿下一条去探墙
                    self._sleep(C.BROWSER_CHECK_COOLOFF)
            else:
                consecutive_37 = 0
                store.update_job_desc(job.encrypt_job_id, detail.job_desc)
                _emit(
                    {
                        "event": "detail_done",
                        "encrypt_job_id": job.encrypt_job_id,
                        "job_name": job.job_name,
                        "has_desc": detail.has_desc,
                        "desc_len": len(detail.job_desc),
                    }
                )
            done += 1
            # 最后一条真抓的不睡，省掉尾部那一秒（下一页还有自己的页间隔）
            if interval > 0 and done < len(todo):
                self._sleep(interval)

    def iter_pages(
        self,
        *,
        max_pages: int = C.DEFAULT_MAX_PAGES,
        page_interval: float | None = None,
        start_page: int = 1,
    ) -> Iterator[PageResult]:
        """只翻页不入库，页与页之间照样睡（离线分析/测试用）。"""
        interval = self.page_interval if page_interval is None else max(0.0, page_interval)
        page = start_page
        yielded = 0
        while True:
            if max_pages and yielded >= max_pages:
                return
            result = self.fetch_page_clean(page)
            yield result
            yielded += 1
            if result.is_empty:
                return
            if not result.has_more and result.raw_count < C.PAGE_SIZE:
                return
            page += 1
            if interval > 0:
                self._sleep(interval)

    # ------------------------------------------------------------------ #
    # 传输层
    # ------------------------------------------------------------------ #

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        action: str = "",
    ) -> dict[str, Any]:
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

        ``data`` 非空时按 ``application/x-www-form-urlencoded`` 发（``requests``
        对 dict 的默认行为），打招呼那条 POST 用得上。
        """
        url = self.base_url + path
        headers = getattr(self, "_default_headers", None)
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

            if not isinstance(payload, dict):
                last_error = JobDataError(f"{action or path} 响应不是 JSON 对象")
                continue

            code = _biz_code(payload)
            if code != C.CODE_OK:
                message = str(payload.get("message") or payload.get("msg") or "")
                raise JobApiError(code, message or f"{action or path} 返回异常码 {code}", raw=payload)

            return payload

        raise last_error or JobTransportError(f"{action or path} 请求失败")


# --------------------------------------------------------------------------- #
# 会话装配
# --------------------------------------------------------------------------- #


def http_from_session(
    session_path: Path | str | None = None,
    *,
    http: Any | None = None,
    stoken: str | None = None,
) -> Any:
    """用 ``boss_login`` 落盘的登录态搭一个会话。

    Cookie 直接灌进 ``requests.Session``；没有可用登录态时抛
    :class:`JobApiError`，把「查了哪儿、为什么不算」说清楚（跟
    ``boss_login.whoami`` 同一个规矩）。

    ``__zp_stoken__``（安全网关令牌）的取值顺序：

    1. 显式传进来的 ``stoken``
    2. ``doc('session')`` 的 ``cookies.__zp_stoken__``
    3. 环境变量 :data:`config.STOKEN_ENV`（``BOSS_ZP_STOKEN``）

    都没有就**先不带**——搜索类接口会回 code 37，由
    :func:`create_client` 挂上的 :class:`~boss_jobs.cdp_stoken.CdpStokenProvider`
    拉 Chrome（CDP）让站点自己算一枚补上（见 :mod:`boss_jobs.cdp_stoken`）。
    """
    from boss_login.session import load_session  # 延迟导入，避免硬依赖

    import boss_db

    # None → BOSS_DB → data/boss.db（调用时才解析，测试只改一个环境变量就隔离）
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

    # 取值顺序：显式参数 → doc('session') → 环境变量。先定序再落盘，
    # 这样显式传进来的一定盖得过会话里旧的那份。
    from .stoken import put_cookie  # 延迟导入

    token = stoken or stored.cookies.get(C.STOKEN_COOKIE) or os.environ.get(C.STOKEN_ENV, "")
    if token and hasattr(sess, "cookies") and hasattr(sess.cookies, "set"):
        # 走 put_cookie：会把上面那行不带 domain 写进去的同名旧 Cookie 清掉，
        # 否则请求头里会同时出现两枚 __zp_stoken__，服务端照旧的那枚拒。
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
    """一步拿到带登录态的 :class:`JobClient`。

    ``auto_stoken=True``（默认）时挂上 :class:`~boss_jobs.cdp_stoken.CdpStokenProvider`：
    每次搜索前判一次 ``__zp_stoken__`` 过期，过期了自动拉 Chrome（CDP）换新并
    落盘（``doc('stoken')`` + 镜像进 ``doc('session')``）。这是**真浏览器**自算，
    不是 Node 硬算——后者指纹对不上，服务端不认，见 :mod:`boss_jobs.cdp_stoken`。
    """
    import boss_db

    http = http_from_session(session_path, stoken=stoken)
    provider = None
    if auto_stoken:
        from .cdp_stoken import CdpStokenProvider  # 延迟导入

        # create_client 总要镜像一份回登录态；None 也要解析到 BOSS_DB，
        # 不能靠 CdpStokenProvider 的「不给就不镜像」契约。
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


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _biz_code(payload: Mapping[str, Any]) -> int:
    raw = payload.get("code", payload.get("status", C.CODE_OK))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1
