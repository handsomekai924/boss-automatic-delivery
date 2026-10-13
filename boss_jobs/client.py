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
    """Immutable rule view supplied by the web crawl task.

    The jobs client deliberately knows nothing about the web application's rule
    tables.  Callbacks return serialisable match snapshots; the client owns the
    ordering of company-before-detail and JD-after-detail decisions.
    """

    task_id: str = ""
    store_blacklisted: bool = True
    has_jd_rules: bool = False
    #: JD 规则的匹配范围（``job_name`` / ``job_desc`` / ``both``）。
    #: 旧调用方可省略，此时按 ``both`` 处理（需要 JD 正文才能算检查完成）。
    jd_scopes: tuple[str, ...] = ()
    company_matches: Callable[[Any], list[dict[str, Any]]] | None = None
    jd_matches: Callable[[Any, str], list[dict[str, Any]]] | None = None
    record_hit: Callable[..., None] | None = None


def _jd_scope_flags(context: "CrawlBlacklistContext | None") -> tuple[bool, bool]:
    """``(can_check_name, requires_desc)``：按规则 scope 判断这轮能不能检查 JD。"""
    if context is None or not context.has_jd_rules:
        return False, False
    scopes = set(context.jd_scopes or ()) or {"both"}
    can_check_name = bool(scopes & {"job_name", "both"})
    requires_desc = bool(scopes & {"job_desc", "both"})
    return can_check_name, requires_desc


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
    skipped: int = 0
    company_hits: int = 0
    jd_hits: int = 0
    stopped_reason: str = ""

    def add(self, result: PageResult, outcome: SaveOutcome, *, skipped: int = 0,
            company_hits: int = 0, jd_hits: int = 0) -> "CrawlStats":
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
        :mod:`boss_jobs.chat`），默认取 :mod:`boss_jobs.config` 里的值。
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
        #: 整批一份聊天凭据（``getUserInfo`` + ``get/wt``）。一帧一条连接、
        #: 发完就断，凭据却是会话级的——别每条都重打这两发。
        self._chat_credentials: ChatCredentials | None = None
        #: 上一条见过的最大消息 id，重建连接时当 ``mid`` 基数带过去（不然
        #: 每条都要重新等那帧会话同步）。见 :meth:`open_chat`。
        self._chat_mid_base: int = 0

        self._http = http or requests.Session()
        merged = {**C.DEFAULT_HEADERS, **(headers or {})}
        if hasattr(self._http, "headers"):
            self._http.headers.update(merged)
        else:  # 简易假会话：退化成每次请求都带
            self._default_headers = merged


    def fetch_search_page(self, search_filter: Any, *, page: int | None = None) -> PageResult:
        """按 :class:`~boss_filter.search.JobSearchFilter` 抓**一页搜索结果**并清洗。

        打的是 ``/wapi/zpgeek/search/joblist.json``（见 :data:`config.ENDPOINTS`）——
        **关键词 + 筛选条件**的搜索流。与之相对的是
        :meth:`fetch_expect_page` 那条求职期望流（同一批筛选维度，但不带关键词）。

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

    def fetch_expect_list(self) -> ExpectList:
        """拉当前账号的求职期望列表（推荐 / 兼职 / 我的期望）。

        打的是 ``/wapi/zpgeek/pc/recommend/expect/list.json``（元数据接口，
        不返回职位）。前端是 GET（``ZV``）。**实测（2026-10-13）**：把
        ``__zp_stoken__`` 从 cookie 里摘掉也回 code 0——**只要登录 Cookie**，
        所以这里刻意走裸 :meth:`_get_json`，不换
        :meth:`_get_json_with_stoken_retry`（换上去反而多拉一次 Chrome）。

        响应字段名不对称（``expectList[].encryptId`` vs
        ``partTimeExpectList[].encryptExpectId``），解析见
        :func:`boss_jobs.models.clean_expects`。本账号期望列表是空的，
        对称性没被真数据定案，解析侧双键名都认。
        """
        payload = self._get_json(
            self.endpoints["job_expect_list"],
            params={},
            action="求职期望列表",
        )
        return clean_expects(payload)

    def fetch_expect_page(self, expect_filter: Any, *, page: int | None = None) -> PageResult:
        """期望流一页（``recommend/job/list.json``）。与 :meth:`fetch_search_page`
        同级：**走 stoken 重试**（这条接口在安全网关后面，缺令牌回 code 37），
        不能走裸 :meth:`_get_json`。

        **实测（2026-10-13）**：GET 回 code 0 / 15 条 / ``hasMore=true``；
        同参数 POST 回 code 10「未知请求方法」。所以这里是 GET，别照前端
        ``hc`` 封装切 POST form。

        :param expect_filter: :class:`boss_filter.expect.JobExpectFilter`；
            只要能出 ``to_params()``/``for_page()`` 也行
        :param page: 覆盖条件里的页码（翻页时省得自己改条件）
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
        连环 code 37 时换新**没用**——刚换的令牌一样被拒，
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


    def fetch_job_detail(self, *, security_id: str, lid: str) -> JobDetail:
        """拉一条职位详情，抽出 JD 正文。

        打的是 ``/wapi/zpgeek/job/detail.json``（见 :data:`config.ENDPOINTS`），
        query 只带 ``securityId`` + ``lid``（调用方 ``tc({securityId, lid})`` 就这
        两参形态）。列表接口不回 JD，正文在 ``zpData.jobInfo.postDescription``。

        详情跟搜索一样要 ``__zp_stoken__``（缺了回 code 37），所以这里也走
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
        """发一条打招呼（``POST /wapi/zpgeek/friend/add.json``）。

        **这条只建会话、不投递正文**——body 里带
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

        **「开聊提醒」会自动模拟点击确认**：``friend/add`` 回 ``code 1`` +
        ``chatRemindDialog``（blockLevel 0 的提示弹窗，content 像「您今天已与
        120 位 BOSS 沟通，还剩 30 次沟通机会哦」）时，照站点前端那条链路
        补两发——弹窗埋点 ``addf-limit-popup-c``，再带 ``cid=1`` 重打一次
        ``friend/add``（chunk ``1326.ad80b1c8.js`` 的 ``commonAction``）。
        确认后能建会话就回 :class:`GreetResult`，把剩下的沟通次数用掉；
        确认完还被拦才抛出来（话术仍是弹窗那句原话）。
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
                # 已明确达到今日上限时，首次响应就是终态；不要再模拟确认，
                # 否则会额外打一发 friend/add 并把原始配额文案覆盖掉。
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
        """「开聊提醒」弹窗的**模拟点击确认**，回确认后 ``friend/add`` 的响应。

        站点前端（chunk ``1326.ad80b1c8.js``）点弹窗上的「好」时干三件事：

        1. 弹窗 ``created`` 时埋点 ``POST /wapi/zpCommon/actionLog/geek/chatremind.json``
           body ``{action: "addf-limit-popup-c", ba: <弹窗 ba>}``；
        2. ``commonAction`` 里 ``remindType > 262144``（实测 524288）且按钮
           ``actionType == 11`` 时，**带 ``cid=1`` 再打一次 ``friend/add``**
           （这就是「确认」本体——不带 ``cid`` 服务端一直弹窗）；
        3. 再埋点 ``addf-limit-popup-connect`` + ``ba`` + ``p8=11``。

        埋点两发**失败不挡确认**（少打一发埋点不影响建会话）；``friend/add``
        那发才是判据。确认后仍弹窗就把原异常抛回去，让上层按话术记这条。
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
        """补一发「开聊提醒」弹窗埋点；**失败不抛**，别挡确认。

        这条要 ``zp_token`` 头（cookie ``bst``），见
        :data:`boss_jobs.config.ZP_TOKEN_HEADER`。
        """
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
        """取 MQTT 密码：``GET /wapi/zppassport/get/wt`` → ``zpData.wt2``。"""
        payload = self._get_json(self.endpoints["get_wt"], action="取聊天凭据 wt")
        wt = str((payload.get("zpData") or {}).get("wt2") or "")
        if not wt:
            raise ChatSendError("取聊天凭据失败：get/wt 没回 wt2")
        return wt

    def fetch_me(self) -> ChatCredentials:
        """取自己的聊天身份：``token``（MQTT 用户名）+ ``userId``/``name``。

        另外把会话 Cookie 一起打包——聊天 WebSocket 握手**必须带 Cookie**
        （不带回直接 HTTP 403）。
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
            **不是** ``encryptBossId``——传加密串回
            **code 19「参数值错误」**，传数字 uid 回 code 0。

        **拿它核对送达是不行的**：对有消息、且刚确认送达的
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
        """拿到一条聊天 MQTT 连接；已连上就复用，断了就重建。

        一帧一条连接：:meth:`~boss_jobs.chat.ChatSocket.send_text` 发完就主动
        断（网关自己也约 150ms 就掐线），所以「复用连接」基本不发生——真正
        要复用的是**凭据**和 **``mid`` 基数**：

        - 凭据（``getUserInfo`` + ``get/wt`` 两发 HTTP）整批取一次，存在
          :attr:`_chat_credentials`；token/wt 是会话级的，重取只是白打两发。
        - 上一条见过的最大消息 id 带给新连接当 ``mid`` 基数
          （:attr:`_chat_mid_base`），第二条起就不用再等那帧会话同步。

        :param credentials: 显式给一份就用它（并顶掉缓存）；不给走缓存 / 现取
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
        """收下这条连接见过的 ``mid`` 基数，重建时带给下一条。

        基数必须单调不减（网关按「id 太旧」毙帧，见 :mod:`boss_jobs.chat`
        模块头），所以只往上抬、不往下走。
        """
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
        """**真·打招呼**：建会话 → 换 boss uid → MQTT 发正文。

        三步走（每步都能单独失败，失败往上抛，调用方记这条发送失败）：

        1. :meth:`greet` —— ``friend/add.json`` 建会话（幂等，已建过也无妨）；
        2. :meth:`fetch_boss_data` —— ``encryptBossId`` → 数字 ``uid`` / ``source``；
        3. :meth:`open_chat` + :meth:`~boss_jobs.chat.ChatSocket.send_text`
           —— MQTT 发正文。

        第 3 步的**成功判据是「帧发出去了」**（PUBLISH 无异常、``rc == 0``），
        **不是「等到 PUBACK」**：这条网关对文本帧根本不回 PUBACK，发完约 150ms
        直接把连接关掉——**这是它的常态，不是拒收**（那几发
        站点上都显示「[送达]」，见 :mod:`boss_jobs.chat` 模块头）。所以
        :meth:`~boss_jobs.chat.ChatSocket.send_text` 只把 PUBACK 记进日志，
        等不到不抛。``mid`` 由 :class:`boss_jobs.chat.ChatSocket` 自己算
        （基数取服务端推的会话同步里的最大消息 id，见
        :func:`boss_jobs.chat.max_message_id`；整批内上一条的基数会带过来，
        第二条起不再等那帧同步），调用方不用管。

        **不再回读聊天记录核对**：``GET /wapi/zpchat/geek/historyMsg`` 对这条
        账号返回 ``code 0`` + 空 ``zpData``——**有消息、刚确认送达的会话也读不
        出来**，拿它当判据只会把成功报成失败。

        ``to.name`` 用会话对象的 ``encryptUid``（同源的 ``encryptBossId``）：
        跟站点一样有值就写、空串就不写这个字段。日志里的耗时拆成「建会话 /
        换 uid / 开通道 / 发帧」，其中「开通道」含取凭据（缓存命中就没有）
        + MQTT 握手，「发帧」里再拆等推送与收尾停顿
        （:attr:`ChatSocket.last_send_stats`）。

        :param greeting: 招呼语正文。**空的不发**（抛 :class:`ChatSendError`）——
            站点那条 ``friend/add`` 只建会话，不带正文的「打招呼」在 App 里
            看着像打了、聊天框其实是空的。
        :raises ChatSendError: 正文为空 / 换不到 boss uid / 通道发失败
        :raises JobApiError: 建会话那步的登录态失效 / 账号风控
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
    ) -> CrawlReport:
        """分页抓取职位，**每页即时清洗入库**，页与页之间硬睡一会。

        :param store: 已打开的职位库；不传则按 ``db_path``（或默认路径）现开
        :param max_pages: 最多翻几页；0 = 翻到接口回空页
        :param page_interval: 覆盖构造时的翻页间隔（秒）
        :param start_page: 起始页码，续跑时用
        :param db_path: 不传 ``store`` 时的库路径
        :param expect_filter: ``JobExpectFilter``（或任何能出 ``to_params()``/``for_page()``）。
            **给了就走期望流** ``/wapi/zpgeek/pc/recommend/job/list.json``，
            期望主参数 + 共享筛选维度照搬（**永远不带关键词**）。
        :param search_filter: ``JobSearchFilter``（或任何能出 ``to_params()``/``for_page()``）。
            **给了就走搜索流** ``/wapi/zpgeek/search/joblist.json``，条件照搬；
            现在还可以带 ``encrypt_expect_id``（期望 + 关键词组合，前端
            ``onSearch`` 的 ``lastExpectId`` 行为），参数走 ``to_params()`` 自然带出。
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

        **两条流二选一，优先级 expect > search**（调用方保证互斥，这里再兜一层）：

        1. ``expect_filter`` 非空 → 期望流 ``recommend/job/list.json``
        2. 否则 ``search_filter`` 非空 → 搜索流 ``search/joblist.json``
        3. 两个都不给 → 抛 :class:`ValueError`

        推荐页 ``special/zone`` 那条流 **2026-10-13 已移除**（实测回 code 7，
        推荐 tab 场景改由期望流 ``kind=recommend`` 承担），不再有「无筛选抓全站」。

        分页 / 清洗 / 入库 / 黑名单逻辑两条流**全部复用**。

        收手判据：接口回空页，或「**不满页**且 ``hasMore=false``」。
        ``hasMore`` 在 page=1 上不可靠（常回 ``false`` 却还有后续页），所以不单独
        信它。下一页若已被 ``max_pages`` 拦下，也不再多等那一个页间隔。
        """
        interval = self.page_interval if page_interval is None else max(0.0, page_interval)
        detail_gap = (
            C.DETAIL_INTERVAL if detail_interval is None else max(0.0, detail_interval)
        )
        use_expect = expect_filter is not None
        use_search = search_filter is not None
        # 推荐页 special/zone 已移除，两个 filter 都不给就没有检索语义了
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

                # 两条流二选一，优先级：expect > search
                if use_expect:
                    result = self.fetch_expect_page(expect_filter, page=page)
                else:
                    result = self.fetch_search_page(search_filter, page=page)
                if blacklist_context is None:
                    # Preserve the established ordinary crawl contract: persist
                    # the list page first, then enrich its already-stored rows.
                    outcome = store.save_page(result)
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
                            # 入库并标记：命中要进审计流水（action=stored），后面 save/汇总都用本轮结果
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
                        # 不补 JD 时也要落完整安全快照：未查完的规则/缺公司名保守拦自动投递。
                        # 纯名称规则不依赖正文，这里就地查；需要正文的规则只能标「未检查」。
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
                                # 公司命中可能已先落过快照，这里合并而不是覆盖
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
                            # Job.from_api 的默认值是 pending；有规则却查不了时改记 unchecked
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

                    outcome = store.save_page(page_result)
                    # UPSERT 故意不覆盖安全列；这里按本轮评估显式回写，重抓才能刷新旧状态
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
        """Fetch details and evaluate JD rules before the page is persisted."""
        def _emit(payload: dict[str, Any]) -> None:
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
                # The list endpoint is not authoritative for JD or blacklist state.
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
                    # 本轮规则结果覆盖旧 jd_keyword 快照：规则删了/不再命中要清掉
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
                    # Persist immediately for the legacy save-before-enrich path.
                    # For a blacklist crawl the candidate may not exist yet; its
                    # complete state is written by the later filtered save_page.
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
                            # Mark remaining candidates as incomplete without more requests.
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
                    # 无 JD 规则 → 无需检查；有规则时：有正文可全查，或纯名称规则不需要正文
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

            # 公司命中只认本轮规则结果，不沿用库里旧快照（规则删了要能翻案）
            company_hits: list[dict[str, Any]] = []
            if blacklist_context and blacklist_context.company_matches:
                company_hits = blacklist_context.company_matches(job)
            has_jd_rules = bool(blacklist_context and blacklist_context.has_jd_rules)
            # JD 命中同理：本轮有 JD 规则才认快照里的 jd_keyword，否则清掉陈旧命中
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
            # 公司名缺失无法确认安全，保守拦自动投递（不算命中）
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

        路由与 :meth:`crawl` 同款两分支：给了 ``expect_filter`` 走期望流，否则
        走搜索流。两个都不给抛 :class:`ValueError`（推荐页 ``special/zone``
        已移除，没有第三条流兜底）。
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

        每发都补 ``zp_token`` 头（cookie ``bst``）——站点 axios 拦截器对**所有**
        请求都贴这个；「开聊提醒」确认侧的埋点缺了直接 code 121「请求不合法」。
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
                # 顶层 message 常常只是个短标签（「开聊提醒」），真话在
                # zpData.bizData.chatRemindDialog.content 那类嵌套里——见
                # format_api_message。
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

    写 ``__zp_stoken__`` 时走 :func:`boss_jobs.stoken.put_cookie`：它会把上面
    ``cookies.set`` 不带 domain 写进去的同名旧 Cookie 清掉，否则请求头里会同时
    出现两枚，服务端照旧的那枚拒。
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
    """一步拿到带登录态的 :class:`JobClient`。

    ``auto_stoken=True``（默认）时挂上 :class:`~boss_jobs.cdp_stoken.CdpStokenProvider`：
    每次搜索前判一次 ``__zp_stoken__`` 过期，过期了自动拉 Chrome（CDP）换新并
    落盘（``doc('stoken')`` + 镜像进 ``doc('session')``）。这是**真浏览器**自算，
    不是 Node 硬算——后者指纹对不上，服务端不认，见 :mod:`boss_jobs.cdp_stoken`。

    会话路径总要解析一份传给 provider（``None`` 也要落到 ``BOSS_DB`` →
    ``data/boss.db``），不能靠它「不给就不镜像」的契约——否则落盘会漏。
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
    raw = payload.get("code", payload.get("status", C.CODE_OK))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1
