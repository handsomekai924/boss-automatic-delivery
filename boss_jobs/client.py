"""职位列表的线上接口客户端。

调的是真实 wapi 路径（见 :mod:`boss_jobs.config`），不是界面模拟。
登录态复用 ``boss_login`` 落盘的 Cookie（``session.json``）——
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
from .errors import JobApiError, JobDataError, JobError, JobTransportError
from .models import PageResult, clean_page
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
    :param stoken_provider: ``__zp_stoken__`` 自动获取器（见 :mod:`boss_jobs.stoken`）。
        传了它，搜索接口撞上 code 37 时会自己算令牌重试；不传就按老规矩报错。
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoints = {**C.ENDPOINTS, **(endpoints or {})}
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.page_interval = max(0.0, page_interval)
        self._sleep = sleeper
        self.stoken_provider = stoken_provider

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

        撞上安全网关（code 37）时，会**自动**走一遍
        :mod:`boss_jobs.stoken` 的令牌获取（拿挑战 → 跑 ``security-js`` →
        算出 ``__zp_stoken__`` → 重试），不需要人工回浏览器。

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

        try:
            payload = self._get_json(path, params=params, action=action)
        except JobApiError as exc:
            if not exc.is_browser_check or self.stoken_provider is None:
                raise
            logger.info("搜索撞上安全网关 code 37，自动补 %s 后重试", C.STOKEN_COOKIE)
            token = self.stoken_provider.ensure(force=True)
            self._set_cookie(C.STOKEN_COOKIE, token)
            payload = self._get_json(path, params=params, action=action + "（补令牌后）")

        return clean_page(payload, page=int(params.get("page", 1)))

    def _set_cookie(self, name: str, value: str) -> None:
        jar = getattr(self._http, "cookies", None)
        if jar is not None and hasattr(jar, "set"):
            jar.set(name, value, domain=".zhipin.com", path="/")

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
    ) -> CrawlReport:
        """分页抓取职位，**每页即时清洗入库**，页与页之间硬睡一会。

        :param store: 已打开的职位库；不传则按 ``db_path``（或默认路径）现开
        :param max_pages: 最多翻几页；0 = 翻到接口回空页
        :param page_interval: 覆盖构造时的翻页间隔（秒）
        :param start_page: 起始页码，续跑时用
        :param db_path: 不传 ``store`` 时的库路径
        :return: :class:`CrawlReport`（统计 + 每页明细）
        """
        interval = self.page_interval if page_interval is None else max(0.0, page_interval)
        own_store = store is None
        store = store or open_store(db_path)
        run_id = uuid.uuid4().hex[:12]
        stats = CrawlStats(run_id=run_id)
        report = CrawlReport(stats=stats)
        reason = "抓到空页"

        try:
            page = start_page
            while True:
                if max_pages and (page - start_page) >= max_pages:
                    reason = f"达到 max_pages={max_pages}"
                    break

                result = self.fetch_page_clean(page)
                outcome = store.save_page(result)  # 立刻入库
                report.pages.append(result)
                stats = stats.add(result, outcome)
                report.stats = stats

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
                    "GET",
                    url,
                    params=dict(params or {}),
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
    2. ``session.json`` 的 ``cookies.__zp_stoken__``
    3. 环境变量 :data:`config.STOKEN_ENV`（``BOSS_ZP_STOKEN``）

    都没有就**先不带**——搜索类接口会回 code 37，由
    :func:`create_client` 挂上的 :class:`~boss_jobs.stoken.StokenProvider`
    按站点前端那条链路自动算一枚补上（见 :mod:`boss_jobs.stoken`）。
    """
    from boss_login.session import load_session  # 延迟导入，避免硬依赖

    path = Path(session_path) if session_path else C.DEFAULT_SESSION_PATH
    stored = load_session(path)
    if stored.is_empty:
        raise JobApiError(
            7,
            f"本地没有登录态：{path} 不存在、为空，或 token/Cookie 都没有。"
            f"先跑 `python -m boss_login login` 落一份。",
        )

    sess = http or requests.Session()
    for name, value in stored.cookies.items():
        if hasattr(sess, "cookies") and hasattr(sess.cookies, "set"):
            sess.cookies.set(name, value)

    # 取值顺序：显式参数 → session.json → 环境变量。先定序再落盘，
    # 这样显式传进来的一定盖得过会话里旧的那份。
    token = stoken or stored.cookies.get(C.STOKEN_COOKIE) or os.environ.get(C.STOKEN_ENV, "")
    if token and hasattr(sess, "cookies") and hasattr(sess.cookies, "set"):
        sess.cookies.set(C.STOKEN_COOKIE, token)
        logger.debug("已带上 %s（长度 %d）", C.STOKEN_COOKIE, len(token))
    else:
        logger.debug(
            "会话里还没有 %s：搜索接口会先回 code 37，由 StokenProvider 自动算一枚补上。",
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

    ``auto_stoken=True``（默认）时挂上 :class:`~boss_jobs.stoken.StokenProvider`，
    搜索接口撞上 code 37 就自动算 ``__zp_stoken__`` 重试。
    """
    http = http_from_session(session_path, stoken=stoken)
    provider = None
    if auto_stoken:
        from .stoken import StokenProvider  # 延迟导入

        provider = StokenProvider(http=http, base_url=base_url)
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
